"""Google Workspace OAuth — direct Google OAuth flow for Gmail/Calendar/Drive.

Unlike remote MCP connectors (Notion, Slack) that handle OAuth via MCP protocol,
Google Workspace requires us to manage Google OAuth directly because Google
doesn't provide a hosted MCP endpoint for consumer Workspace products.

Flow:
  1. User clicks Connect on Google Workspace connector
  2. We redirect to Google OAuth consent screen (using our client_id)
  3. User authorizes
  4. Google redirects to our callback
  5. We exchange code for tokens, store them
  6. We restart the google-workspace MCP server with injected tokens
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.api.oauth_redirect import loopback_redirect_uri
from app.auth.credential_store import (
    CredentialStoreError,
    StagedSecretTree,
    prepare_stale_secret_cleanup,
    resolve_secret_tree,
    stage_protected_secret_tree,
)
from app.utils.atomic_write import atomic_write_text

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/google")

# Google OAuth endpoints
_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Scopes for Gmail + Calendar + Drive
_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/drive.readonly",
]

# In-memory pending states and per-workspace disconnect fences.  The generation
# check prevents an OAuth exchange that was already in flight when the user
# disconnected from writing fresh tokens back afterward.
_pending_states: dict[str, dict[str, Any]] = {}
_auth_generations: dict[str, int] = {}
_auth_state_lock = threading.RLock()
_AUTH_GENERATION_FIELD = "_suxiaoyou_auth_generation"


def _get_token_path(project_dir: str | None) -> Path:
    """Where to store Google OAuth tokens."""
    if project_dir:
        return Path(project_dir).resolve() / ".suxiaoyou" / "google-tokens.json"
    return Path.home() / ".suxiaoyou" / "google-tokens.json"


def _credential_namespace(project_dir: str | None) -> str:
    scope = str(Path(project_dir).expanduser().resolve()) if project_dir else "global"
    return f"google:{hashlib.sha256(scope.encode('utf-8')).hexdigest()[:20]}"


def _invalidate_pending_auth(project_dir: str | None) -> None:
    with _auth_state_lock:
        scope = _credential_namespace(project_dir)
        _auth_generations[scope] = _auth_generations.get(scope, 0) + 1
        for state, pending in tuple(_pending_states.items()):
            if pending.get("scope") == scope:
                _pending_states.pop(state, None)


def fence_google_auth_disconnect(project_dir: str | None) -> None:
    """Fence OAuth callbacks before disconnect performs its first await."""

    _invalidate_pending_auth(project_dir)


def _discard_failed_token_stage(path: Path, staged: StagedSecretTree) -> None:
    if not path.is_file():
        staged.discard_unreferenced()
        return
    try:
        installed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # An unreadable file might already contain the new references if an
        # unusual writer failed after replacement. Retain rather than break it.
        installed = staged.value
    staged.discard_unreferenced((installed,))


def load_google_tokens(project_dir: str | None) -> dict[str, Any] | None:
    """Load stored Google tokens from disk."""
    path = _get_token_path(project_dir)
    if not path.is_file():
        return None
    try:
        previous_content = path.read_bytes()
        data = json.loads(previous_content.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    staged = stage_protected_secret_tree(
        _credential_namespace(project_dir),
        data,
        previous_value=data,
    )
    protected = staged.value
    if protected != data:
        cleanup_transaction = None
        try:
            next_text = json.dumps(protected, indent=2, ensure_ascii=False) + "\n"
            cleanup_transaction = prepare_stale_secret_cleanup(
                data,
                protected,
                evidence_path=path,
                previous_exists=True,
                previous_content=previous_content,
                next_exists=True,
                next_content=next_text,
            )
            atomic_write_text(
                path,
                next_text,
                mode=0o600,
            )
        except Exception as exc:
            if cleanup_transaction is not None:
                cleanup_transaction.cancel()
            _discard_failed_token_stage(path, staged)
            raise CredentialStoreError(
                f"Cannot erase plaintext Google OAuth credentials in {path}: {exc}"
            ) from exc
        if cleanup_transaction is not None:
            cleanup_transaction.commit()
    else:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return resolve_secret_tree(protected)


def _save_tokens(project_dir: str | None, tokens: dict[str, Any]) -> None:
    path = _get_token_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    previous: dict[str, Any] = {}
    previous_exists = path.is_file()
    previous_content = b""
    if previous_exists:
        try:
            previous_content = path.read_bytes()
            loaded = json.loads(previous_content.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CredentialStoreError(
                f"Cannot safely replace unreadable Google OAuth credentials in {path}: {exc}"
            ) from exc
        if not isinstance(loaded, dict):
            raise CredentialStoreError(
                f"Cannot safely replace invalid Google OAuth credentials in {path}"
            )
        previous = loaded
    staged = stage_protected_secret_tree(
        _credential_namespace(project_dir),
        tokens,
        previous_value=previous,
    )
    cleanup_transaction = None
    try:
        next_text = json.dumps(staged.value, indent=2, ensure_ascii=False) + "\n"
        cleanup_transaction = prepare_stale_secret_cleanup(
            previous,
            staged.value,
            evidence_path=path,
            previous_exists=previous_exists,
            previous_content=previous_content,
            next_exists=True,
            next_content=next_text,
        )
        atomic_write_text(
            path,
            next_text,
            mode=0o600,
        )
    except Exception:
        if cleanup_transaction is not None:
            cleanup_transaction.cancel()
        _discard_failed_token_stage(path, staged)
        raise
    if cleanup_transaction is not None:
        cleanup_transaction.commit()


def _delete_google_tokens_if_generation(
    project_dir: str | None,
    *,
    expected_generation: int | None = None,
) -> bool:
    """Delete installed metadata, optionally only when owned by one callback."""

    with _auth_state_lock:
        path = _get_token_path(project_dir)
        if not path.is_file():
            return False
        try:
            previous_content = path.read_bytes()
            previous = json.loads(previous_content.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CredentialStoreError(
                f"Cannot safely delete unreadable Google OAuth credentials in {path}: {exc}"
            ) from exc
        if not isinstance(previous, dict):
            raise CredentialStoreError(
                f"Cannot safely delete invalid Google OAuth credentials in {path}"
            )
        if (
            expected_generation is not None
            and previous.get(_AUTH_GENERATION_FIELD) != expected_generation
        ):
            return False

        cleanup_transaction = prepare_stale_secret_cleanup(
            previous,
            {},
            evidence_path=path,
            previous_exists=True,
            previous_content=previous_content,
            next_exists=False,
            next_content=b"",
        )
        try:
            path.unlink()
        except Exception:
            if cleanup_transaction is not None:
                cleanup_transaction.cancel()
            raise
        if cleanup_transaction is not None:
            cleanup_transaction.commit()
        return True


def delete_google_tokens(project_dir: str | None) -> None:
    """Disconnect Google OAuth without exposing or prematurely deleting secrets.

    A durable, evidence-bound cleanup intent is prepared first.  Removing the
    metadata file is the commit point, and its old references are retired only
    afterward.  A failed unlink leaves both metadata and references intact.
    """

    _invalidate_pending_auth(project_dir)
    _delete_google_tokens_if_generation(project_dir)


def _commit_google_tokens_for_generation(
    project_dir: str | None,
    tokens: dict[str, Any],
    *,
    scope: str,
    generation: int,
) -> bool:
    """CAS one callback's tokens against its disconnect generation."""

    committed_tokens = dict(tokens)
    committed_tokens[_AUTH_GENERATION_FIELD] = generation
    with _auth_state_lock:
        if _auth_generations.get(scope, 0) != generation:
            return False
        _save_tokens(project_dir, committed_tokens)
        if _auth_generations.get(scope, 0) == generation:
            return True
        # Defensive write-after CAS for callers that invalidate re-entrantly or
        # from an alternate request thread.  Delete only this generation's file
        # so a newer successful authorization cannot be removed accidentally.
        _delete_google_tokens_if_generation(
            project_dir,
            expected_generation=generation,
        )
        return False


@router.post("/auth-start")
async def google_auth_start(request: Request) -> dict[str, Any]:
    """Start Google OAuth flow. Returns auth URL to open in browser."""
    control = getattr(request.app.state, "security_control", None)
    if control is not None and control.emergency_stop:
        return {"success": False, "error": "Security emergency stop is active"}
    settings = request.app.state.settings

    if not settings.google_client_id:
        return {"success": False, "error": "Google OAuth not configured (missing SUXIAOYOU_GOOGLE_CLIENT_ID)"}

    redirect_uri = loopback_redirect_uri(settings, "/api/google/callback")

    state = secrets.token_urlsafe(32)
    scope = _credential_namespace(settings.project_dir)
    with _auth_state_lock:
        _pending_states[state] = {
            "redirect_uri": redirect_uri,
            "project_dir": settings.project_dir,
            "scope": scope,
            "generation": _auth_generations.get(scope, 0),
        }

    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(_SCOPES),
        "access_type": "offline",  # get refresh_token
        "prompt": "consent",  # force consent to get refresh_token
        "state": state,
    }

    auth_url = f"{_GOOGLE_AUTH_URL}?{urlencode(params)}"
    return {"success": True, "auth_url": auth_url, "state": state}


@router.get("/callback")
async def google_callback(code: str, state: str, request: Request):
    """Google OAuth callback — exchange code for tokens."""
    control = getattr(request.app.state, "security_control", None)
    if control is not None and control.emergency_stop:
        return HTMLResponse("<p>Security emergency stop is active.</p>")
    settings = request.app.state.settings

    with _auth_state_lock:
        pending = _pending_states.pop(state, None)
    if not pending:
        return HTMLResponse("<p>Invalid state. Please try again.</p>")

    redirect_uri = pending["redirect_uri"]

    # Exchange code for tokens
    try:
        from app.auth.credential_store import resolve_env_value

        client_secret = resolve_env_value(
            "SUXIAOYOU_GOOGLE_CLIENT_SECRET",
            settings.google_client_secret,
        )
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                _GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": settings.google_client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            if resp.status_code != 200:
                logger.warning("Google token exchange failed: %d %s", resp.status_code, resp.text[:300])
                return HTMLResponse(f"<p>Token exchange failed: {resp.status_code}</p>")

            token_data = resp.json()
    except Exception as e:
        logger.warning("Google token exchange error: %s", e)
        return HTMLResponse(f"<p>Error: {html.escape(str(e))}</p>")

    scope = pending.get("scope")
    generation = pending.get("generation")
    if not isinstance(scope, str) or not isinstance(generation, int):
        return HTMLResponse("<p>This Google authorization was cancelled.</p>")

    # Store tokens
    tokens = {
        "access_token": token_data["access_token"],
        "refresh_token": token_data.get("refresh_token", ""),
        "expires_at": time.time() + token_data.get("expires_in", 3600),
        "token_type": token_data.get("token_type", "Bearer"),
        "scope": token_data.get("scope", ""),
    }
    pending_project_dir = pending.get("project_dir")
    project_dir = pending_project_dir if isinstance(pending_project_dir, str) else None
    connector_registry = getattr(request.app.state, "connector_registry", None)
    if connector_registry:
        async with connector_registry.google_auth_operation_lock:
            if not _commit_google_tokens_for_generation(
                project_dir,
                tokens,
                scope=scope,
                generation=generation,
            ):
                return HTMLResponse(
                    "<p>This Google authorization was cancelled.</p>"
                )
            logger.warning("[Google OAuth] Tokens stored successfully!")

            # Restart google-workspace with the credentials committed by this
            # generation. Disconnect requests fence before waiting for this
            # lock, so an overlapping callback detects cancellation here before
            # a newer callback can enter and establish its runtime.
            await asyncio.to_thread(connector_registry._inject_local_credentials)
            try:
                await connector_registry.reconnect_google_runtime_locked()
            except Exception as e:
                logger.warning("Failed to reconnect google-workspace: %s", e)

            if _auth_generations.get(scope, 0) != generation:
                try:
                    await connector_registry.disconnect_google_runtime_locked()
                except Exception as e:
                    logger.warning(
                        "Failed to close cancelled google-workspace session: %s",
                        e,
                    )
                _delete_google_tokens_if_generation(
                    project_dir,
                    expected_generation=generation,
                )
                await asyncio.to_thread(connector_registry._inject_local_credentials)
                return HTMLResponse(
                    "<p>This Google authorization was cancelled.</p>"
                )
    else:
        if not _commit_google_tokens_for_generation(
            project_dir,
            tokens,
            scope=scope,
            generation=generation,
        ):
            return HTMLResponse("<p>This Google authorization was cancelled.</p>")
        logger.warning("[Google OAuth] Tokens stored successfully!")

    from app.api.callback_html import render_callback
    return HTMLResponse(content=render_callback(
        True,
        extra_data={"connector": "google-workspace"},
    ))


@router.get("/status")
async def google_status(request: Request) -> dict[str, Any]:
    """Check if Google tokens are stored."""
    settings = request.app.state.settings
    tokens = load_google_tokens(settings.project_dir)
    if not tokens:
        return {"connected": False}

    expired = tokens.get("expires_at", 0) < time.time()
    return {
        "connected": True,
        "expired": expired,
        "scope": tokens.get("scope", ""),
        "has_refresh": bool(tokens.get("refresh_token")),
    }
