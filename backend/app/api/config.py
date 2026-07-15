"""Configuration management endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from dotenv import dotenv_values
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.auth.credential_store import (
    CredentialCleanupTransaction,
    StagedEnvValue,
    is_credential_reference,
    prepare_stale_secret_cleanup,
    stage_protected_env_value,
)
from app.config import get_custom_endpoints
from app.dependencies import ProviderRegistryDep, SettingsDep
from app.provider.catalog import PROVIDER_CATALOG
from app.provider.factory import create_provider as create_desktop_provider
from app.provider.local import (
    LOCAL_BASE_URL_ENV,
    LOCAL_PROVIDER_ID,
    create_local_provider,
)
from app.provider.openrouter import OpenRouterProvider
from app.schemas.provider import (
    ApiKeyStatus,
    ApiKeyUpdate,
    CustomEndpointConfig,
    CustomEndpointCreate,
    CustomEndpointModel,
    CustomEndpointUpdate,
    ProviderInfo,
    ProviderKeyUpdate,
    RESERVED_CUSTOM_SLUGS,
)
from app.i18n import request_language
from app.utils.atomic_write import atomic_write_text

logger = logging.getLogger(__name__)

router = APIRouter()

_custom_endpoints_lock = asyncio.Lock()
_env_file_lock = threading.RLock()
_ENV_ASSIGNMENT = re.compile(
    r"^\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*="
)

# Persist runtime config in current working directory.
#
# Desktop mode (`run.py`) changes cwd to the app data directory, so this
# becomes a writable per-user `.env` (instead of the read-only app bundle path
# when running from a mounted DMG volume).
# Server mode runs with its deployment directory as working directory, so behavior
# remains compatible there as well.
_ENV_PATH = Path(".env")


def _mask_key(key: str) -> str:
    """Mask API key for display: show first 7 and last 4 chars."""
    if is_credential_reference(key):
        return "********"
    if len(key) <= 11:
        return "****"
    return f"{key[:7]}...{key[-4:]}"


def _mask_header_value(value: str) -> str:
    """Mask a header value so the form can show what's persisted without
    leaking the credential. Keep the first 4 / last 2 chars on long
    values; short ones get the universal ``****`` treatment."""
    if not value:
        return ""
    if is_credential_reference(value):
        return "********"
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}...{value[-2:]}"


def _mask_headers(headers: dict[str, str] | None) -> dict[str, str] | None:
    if not headers:
        return None
    return {k: _mask_header_value(v) for k, v in headers.items()}


def _models_for_response(ce: dict[str, Any]) -> list[CustomEndpointModel] | None:
    raw = ce.get("models") or []
    if not isinstance(raw, list) or not raw:
        return None
    out: list[CustomEndpointModel] = []
    for m in raw:
        if isinstance(m, dict) and isinstance(m.get("id"), str) and m["id"]:
            out.append(CustomEndpointModel(id=m["id"], name=m.get("name")))
    return out or None


def _build_custom_endpoint_info(
    ce: dict[str, Any],
    *,
    enabled: bool,
    status: str,
    model_count: int = 0,
) -> ProviderInfo:
    """Build ProviderInfo for a custom endpoint."""
    return ProviderInfo(
        id=ce["id"],
        name=ce.get("name", "Custom Endpoint"),
        is_configured=True,
        enabled=enabled,
        masked_key=_mask_key(ce.get("api_key", "")) if ce.get("api_key") else None,
        model_count=model_count,
        status=status,
        base_url=ce.get("base_url"),
        slug=ce.get("slug"),
        models=_models_for_response(ce),
        headers=_mask_headers(ce.get("headers")),
    )


def _env_line_matches(line: str, key: str) -> bool:
    match = _ENV_ASSIGNMENT.match(line)
    return bool(match and match.group("key") == key)


def _env_entry(key: str, value: str) -> str:
    # Single quotes prevent python-dotenv from treating ``#`` and whitespace
    # as syntax. python-dotenv decodes both ``\\`` and ``\'`` inside this form,
    # so escape backslashes first and apostrophes second. Shell's close-quote /
    # reopen idiom is not valid dotenv syntax.
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"{key}='{escaped}'"


@dataclass
class _StagedEnvFileUpdate:
    """One atomic env-file update plus its not-yet-committed credentials."""

    path: Path
    changes: dict[str, str | None]
    previous_values: dict[str, str | None]
    staged_values: dict[str, StagedEnvValue]
    previous_exists: bool
    previous_text: str
    next_text: str
    should_write: bool
    cleanup_transaction: CredentialCleanupTransaction | None
    installed: bool = False

    @classmethod
    def prepare(
        cls,
        path: Path,
        changes: dict[str, str | None],
    ) -> _StagedEnvFileUpdate:
        previous_exists = path.exists()
        previous_text = path.read_text(encoding="utf-8") if previous_exists else ""
        previous_values = dict(dotenv_values(path)) if previous_exists else {}
        staged_values: dict[str, StagedEnvValue] = {}
        try:
            for key, value in changes.items():
                if value is not None:
                    staged_values[key] = stage_protected_env_value(
                        key,
                        value,
                        previous_value=previous_values.get(key),
                    )
        except Exception:
            for staged in staged_values.values():
                staged.discard_unreferenced(previous_values.values())
            raise

        output: list[str] = []
        handled: set[str] = set()
        for line in previous_text.splitlines():
            matching_key = next(
                (key for key in changes if _env_line_matches(line, key)),
                None,
            )
            if matching_key is None:
                output.append(line)
                continue
            # Collapse duplicate assignments so dotenv cannot select a stale
            # value later in the file.
            if matching_key in handled:
                continue
            handled.add(matching_key)
            staged = staged_values.get(matching_key)
            if staged is not None:
                output.append(_env_entry(matching_key, staged.value))

        for key, staged in staged_values.items():
            if key not in handled:
                output.append(_env_entry(key, staged.value))

        next_text = "\n".join(output) + ("\n" if output else "")
        should_write = previous_exists or bool(staged_values)
        previous_changed_values = {
            key: previous_values.get(key) for key in changes
        }
        next_changed_values = {
            key: (staged_values[key].value if key in staged_values else None)
            for key in changes
        }
        try:
            cleanup_transaction = prepare_stale_secret_cleanup(
                previous_changed_values,
                next_changed_values,
                evidence_path=path,
                previous_exists=previous_exists,
                previous_content=previous_text,
                next_exists=should_write,
                next_content=next_text,
            )
        except Exception:
            for staged in staged_values.values():
                staged.discard_unreferenced(previous_values.values())
            raise
        return cls(
            path=path,
            changes=dict(changes),
            previous_values=previous_values,
            staged_values=staged_values,
            previous_exists=previous_exists,
            previous_text=previous_text,
            next_text=next_text,
            should_write=should_write,
            cleanup_transaction=cleanup_transaction,
        )

    def install(self) -> None:
        if not self.should_write:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.path, self.next_text, mode=0o600)
        self.installed = True

    def restore(self) -> None:
        if not self.installed:
            return
        if self.previous_exists:
            atomic_write_text(self.path, self.previous_text, mode=0o600)
        else:
            self.path.unlink(missing_ok=True)
        self.installed = False

    def discard_created_references(self) -> None:
        # Inspect what is actually installed.  If a rollback write itself ever
        # fails, a new reference that remains in config must not be deleted.
        try:
            configured = (
                dict(dotenv_values(self.path)).values()
                if self.path.exists()
                else ()
            )
            configured_values = tuple(configured)
        except Exception as exc:
            logger.error(
                "Cannot verify env references during credential rollback: %s",
                exc,
            )
            return
        for staged in self.staged_values.values():
            staged.discard_unreferenced(configured_values)
        if self.cleanup_transaction is not None:
            self.cleanup_transaction.cancel()

    def finalize_credentials(self) -> None:
        if self.cleanup_transaction is not None:
            self.cleanup_transaction.commit()


def _persist_env_then_commit_runtime(
    changes: dict[str, str | None],
    commit_runtime: Callable[[], None],
    rollback_runtime: Callable[[], None],
) -> None:
    """Install config first, then commit runtime state as one transaction.

    No await occurs inside this function, so desktop API tasks cannot observe a
    half-committed settings/registry state.  New secret references are retained
    only if the installed config still points at them.
    """

    with _env_file_lock:
        staged = _StagedEnvFileUpdate.prepare(_ENV_PATH, changes)
        try:
            staged.install()
        except Exception:
            staged.discard_created_references()
            raise

        try:
            commit_runtime()
        except Exception as exc:
            rollback_failures: list[str] = []
            try:
                rollback_runtime()
            except Exception as rollback_exc:
                rollback_failures.append(f"runtime rollback failed: {rollback_exc}")
                logger.exception("Runtime config rollback failed")
            try:
                staged.restore()
            except Exception as restore_exc:
                rollback_failures.append(f"persistent rollback failed: {restore_exc}")
                logger.exception("Persistent config rollback failed")
            staged.discard_created_references()
            if rollback_failures:
                exc.add_note("; ".join(rollback_failures))
            raise

        # Stale references are deleted only after both installed config and
        # runtime state have committed successfully.
        staged.finalize_credentials()


def _update_env_file(key: str, value: str) -> None:
    """Atomically add or replace one backend ``.env`` value."""

    _persist_env_then_commit_runtime({key: value}, lambda: None, lambda: None)


def _remove_env_key(key: str) -> None:
    """Atomically remove one backend ``.env`` value."""

    _persist_env_then_commit_runtime({key: None}, lambda: None, lambda: None)


def _restore_registry_provider(
    registry: Any,
    provider_id: str,
    previous_provider: Any,
) -> None:
    """Restore exactly the provider instance visible before a transaction."""

    if registry.get_provider(provider_id) is previous_provider:
        return
    if previous_provider is None:
        registry.unregister(provider_id)
    else:
        registry.register(previous_provider)


def persist_env_transaction(
    changes: dict[str, str | None],
    commit_runtime: Callable[[], None],
    rollback_runtime: Callable[[], None],
) -> None:
    """Commit one multi-key env/runtime update through the shared transaction."""

    _persist_env_then_commit_runtime(changes, commit_runtime, rollback_runtime)


def restore_registry_provider(
    registry: Any,
    provider_id: str,
    previous_provider: Any,
) -> None:
    """Restore the provider instance captured before a config transaction."""

    _restore_registry_provider(registry, provider_id, previous_provider)


class LocalProviderStatus(BaseModel):
    """Status for the locally-configured OpenAI-compatible endpoint."""

    base_url: str = ""
    is_configured: bool = False
    is_connected: bool = False
    status: str = "unconfigured"  # "connected" | "error" | "unconfigured"


class LocalProviderUpdate(BaseModel):
    """Request payload for configuring the local endpoint."""

    base_url: str


def _normalize_local_base_url(value: str) -> str:
    """Normalize user input and ensure it includes a scheme."""
    trimmed = value.strip()
    if not trimmed:
        raise HTTPException(400, "Base URL cannot be empty")
    parsed = urlparse(trimmed)
    if not parsed.scheme or not parsed.netloc:
        raise HTTPException(400, "Base URL must include http:// or https://")
    return trimmed.rstrip("/")


def _local_provider_status(settings: Any, registry: Any) -> LocalProviderStatus:
    """Build a status object from the current configuration + registry state."""
    base_url = settings.local_base_url or ""
    provider = registry.get_provider(LOCAL_PROVIDER_ID)
    is_connected = bool(base_url and provider)
    status = "connected" if is_connected else ("error" if base_url else "unconfigured")
    return LocalProviderStatus(
        base_url=base_url,
        is_configured=bool(base_url),
        is_connected=is_connected,
        status=status,
    )


@router.get("/config/api-key", response_model=ApiKeyStatus)
async def get_api_key_status(registry: ProviderRegistryDep) -> ApiKeyStatus:
    """Get the current API key configuration status."""
    provider = registry.get_provider("openrouter")

    if provider is None or not getattr(provider, "_api_key", ""):
        return ApiKeyStatus(is_configured=False)

    return ApiKeyStatus(
        is_configured=True,
        masked_key=_mask_key(provider._api_key),
    )


@router.post("/config/api-key", response_model=ApiKeyStatus)
async def update_api_key(
    settings: SettingsDep,
    registry: ProviderRegistryDep,
    body: ApiKeyUpdate,
) -> ApiKeyStatus:
    """Update the OpenRouter API key, validate it, and re-initialize the provider."""
    api_key = body.api_key.strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="API key cannot be empty")

    # Validate by attempting to fetch models with the new key
    test_provider = OpenRouterProvider(api_key)
    try:
        models = await test_provider.list_models()
        if not models:
            raise HTTPException(
                status_code=400,
                detail="API key is valid but returned no models",
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("API key validation failed: %s", e)
        raise HTTPException(
            status_code=400,
            detail=f"API key validation failed: {e}",
        )

    # The key is valid.  Install the durable reference before replacing the
    # live provider; a failed write must leave the working provider untouched.
    new_provider = OpenRouterProvider(api_key)
    previous_provider = registry.get_provider("openrouter")
    previous_api_key = settings.openrouter_api_key

    def commit_runtime() -> None:
        settings.openrouter_api_key = api_key
        registry.register(new_provider)

    def rollback_runtime() -> None:
        settings.openrouter_api_key = previous_api_key
        _restore_registry_provider(registry, "openrouter", previous_provider)

    _persist_env_then_commit_runtime(
        {"SUXIAOYOU_OPENROUTER_API_KEY": api_key},
        commit_runtime,
        rollback_runtime,
    )

    # Refresh the model index so the frontend picks up the new models
    try:
        await registry.refresh_models()
    except Exception as e:
        logger.warning("Model refresh failed after API key update: %s — will retry on next request", e)

    return ApiKeyStatus(
        is_configured=True,
        masked_key=_mask_key(api_key),
        is_valid=True,
    )


@router.delete("/config/api-key", response_model=ApiKeyStatus)
async def delete_api_key(settings: SettingsDep, registry: ProviderRegistryDep) -> ApiKeyStatus:
    """Delete the stored OpenRouter API key."""
    previous_api_key = settings.openrouter_api_key
    previous_provider = registry.get_provider("openrouter")

    def commit_runtime() -> None:
        settings.openrouter_api_key = ""
        registry.unregister("openrouter")

    def rollback_runtime() -> None:
        settings.openrouter_api_key = previous_api_key
        _restore_registry_provider(registry, "openrouter", previous_provider)

    _persist_env_then_commit_runtime(
        {"SUXIAOYOU_OPENROUTER_API_KEY": None},
        commit_runtime,
        rollback_runtime,
    )

    return ApiKeyStatus(is_configured=False)


# ── Ollama (Local LLM) ────────────────────────────────────────────────────


class OllamaStatus(BaseModel):
    is_configured: bool = False
    base_url: str | None = None
    model_count: int = 0
    error: str | None = None


class OllamaConnect(BaseModel):
    base_url: str = "http://localhost:11434"


@router.get("/config/ollama", response_model=OllamaStatus)
async def get_ollama_status(settings: SettingsDep, registry: ProviderRegistryDep) -> OllamaStatus:
    """Get the current Ollama configuration status."""
    provider = registry.get_provider("ollama")

    if provider is None or not settings.ollama_base_url:
        return OllamaStatus(is_configured=False)

    # Check live connectivity
    status = await provider.health_check()
    return OllamaStatus(
        is_configured=True,
        base_url=settings.ollama_base_url,
        model_count=status.model_count,
        error=status.error,
    )


@router.post("/config/ollama", response_model=OllamaStatus)
async def connect_ollama(
    settings: SettingsDep, registry: ProviderRegistryDep, body: OllamaConnect,
) -> OllamaStatus:
    """Connect to an Ollama instance: validate, register provider, persist."""
    from app.provider.ollama import OllamaProvider

    base_url = body.base_url.strip().rstrip("/")
    if not base_url:
        raise HTTPException(400, "base_url cannot be empty")

    # Validate by health-checking the target URL
    test_provider = OllamaProvider(base_url=base_url)
    status = await test_provider.health_check()
    if status.status != "connected":
        raise HTTPException(
            400,
            f"Cannot connect to Ollama at {base_url}: {status.error or 'unknown error'}",
        )

    previous_base_url = settings.ollama_base_url
    previous_provider = registry.get_provider("ollama")

    def commit_runtime() -> None:
        settings.ollama_base_url = base_url
        registry.register(test_provider)

    def rollback_runtime() -> None:
        settings.ollama_base_url = previous_base_url
        _restore_registry_provider(registry, "ollama", previous_provider)

    _persist_env_then_commit_runtime(
        {"SUXIAOYOU_OLLAMA_BASE_URL": base_url},
        commit_runtime,
        rollback_runtime,
    )

    try:
        await registry.refresh_models()
    except Exception as e:
        logger.warning("Model refresh failed after Ollama connect: %s", e)

    return OllamaStatus(
        is_configured=True,
        base_url=base_url,
        model_count=status.model_count,
    )


@router.delete("/config/ollama", response_model=OllamaStatus)
async def disconnect_ollama(settings: SettingsDep, registry: ProviderRegistryDep) -> OllamaStatus:
    """Disconnect Ollama: remove provider and clear config."""
    previous_base_url = settings.ollama_base_url
    previous_provider = registry.get_provider("ollama")

    def commit_runtime() -> None:
        settings.ollama_base_url = ""
        registry.unregister("ollama")

    def rollback_runtime() -> None:
        settings.ollama_base_url = previous_base_url
        _restore_registry_provider(registry, "ollama", previous_provider)

    _persist_env_then_commit_runtime(
        {"SUXIAOYOU_OLLAMA_BASE_URL": None},
        commit_runtime,
        rollback_runtime,
    )

    return OllamaStatus(is_configured=False)


# ── Generic Multi-Provider API ─────────────────────────────────────────────


def _get_disabled_set(settings) -> set[str]:
    return {s.strip() for s in settings.disabled_providers.split(",") if s.strip()}


@router.get("/config/providers", response_model=list[ProviderInfo])
async def list_providers(request: Request, settings: SettingsDep, registry: ProviderRegistryDep) -> list[ProviderInfo]:
    """List all BYOK providers with their configuration status."""
    disabled = _get_disabled_set(settings)
    language = request_language(request)

    result: list[ProviderInfo] = []
    for pid, pdef in PROVIDER_CATALOG.items():
        api_key = getattr(settings, pdef.settings_key, "")
        is_disabled = pid in disabled
        provider = registry.get_provider(pid)

        base_url = None
        if pdef.kind == "openai_compat_azure":
            base_url = getattr(settings, "azure_openai_base_url", "")

        if api_key and is_disabled:
            result.append(ProviderInfo(
                id=pid,
                name=pdef.display_name(language),
                is_configured=True,
                enabled=False,
                masked_key=_mask_key(api_key),
                status="disabled",
                base_url=base_url,
            ))
        elif provider and api_key:
            models = [m for p, m in registry._full_models if m.provider_id == pid]
            result.append(ProviderInfo(
                id=pid,
                name=pdef.display_name(language),
                is_configured=True,
                enabled=True,
                masked_key=_mask_key(api_key),
                model_count=len(models),
                status="connected",
                base_url=base_url,
            ))
        elif api_key:
            result.append(ProviderInfo(
                id=pid,
                name=pdef.display_name(language),
                is_configured=True,
                enabled=True,
                masked_key=_mask_key(api_key),
                status="error",
                base_url=base_url,
            ))
        else:
            result.append(ProviderInfo(
                id=pid,
                name=pdef.display_name(language),
                is_configured=False,
                enabled=not is_disabled,
                status="unconfigured",
                base_url=base_url,
            ))


    # Inject Custom Endpoints
    for ce in get_custom_endpoints(settings):
        pid = ce["id"]
        is_disabled = pid in disabled or not ce.get("enabled", True)
        provider = registry.get_provider(pid)

        # Heal-on-read: if persisted as enabled but the registry has no
        # provider for it (stale unregister, dropped during a partial
        # update, etc.), try to register from the persisted config. The
        # cost is bounded — one rebuild per missing endpoint per page
        # load — and most healing paths are instant because manual model
        # lists skip the /v1/models discovery call. We refresh only this
        # provider's models rather than the whole registry so unrelated
        # providers don't get re-polled on every settings page load.
        if not is_disabled and provider is None:
            try:
                healed = create_desktop_provider(
                    pid,
                    ce.get("api_key", ""),
                    base_url=ce.get("base_url"),
                    models_override=ce.get("models") or None,
                    extra_headers=ce.get("headers") or None,
                )
                await healed.list_models()
                registry.register(healed)
                await registry.refresh_provider(pid)
                provider = healed
            except Exception as e:
                logger.warning("Heal-on-read failed for %s: %s", pid, e)

        if is_disabled:
            result.append(_build_custom_endpoint_info(ce, enabled=False, status="disabled"))
        elif provider:
            models = [m for p, m in registry._full_models if m.provider_id == pid]
            result.append(_build_custom_endpoint_info(ce, enabled=True, status="connected", model_count=len(models)))
        else:
            result.append(_build_custom_endpoint_info(ce, enabled=True, status="error"))

    return result


@router.post("/config/providers/{provider_id}/key", response_model=ProviderInfo)
async def set_provider_key(
    provider_id: str, body: ProviderKeyUpdate, request: Request, settings: SettingsDep, registry: ProviderRegistryDep,
) -> ProviderInfo:
    """Set/update API key for a provider. Validates, registers, and persists."""
    pdef = PROVIDER_CATALOG.get(provider_id)
    if not pdef:
        raise HTTPException(404, f"Unknown provider: {provider_id}")

    api_key = body.api_key.strip()
    if not api_key:
        raise HTTPException(400, "API key cannot be empty")

    # Azure needs a base_url from the request body or existing settings
    extra_kwargs: dict[str, str] = {}
    if pdef.kind in ("openai_compat_azure",):
        url_setting_map = {
            "openai_compat_azure": "azure_openai_base_url",
        }
        url_setting = url_setting_map[pdef.kind]
        base_url = getattr(body, "base_url", None) or getattr(settings, url_setting, "")
        if not base_url:
            raise HTTPException(400, f"{pdef.name} requires a base_url to be set")
        extra_kwargs["base_url"] = base_url

    # Validate by creating a test provider and listing models
    try:
        test_provider = create_desktop_provider(provider_id, api_key, **extra_kwargs)
        models = await test_provider.list_models()
    except ImportError as e:
        raise HTTPException(
            400,
            f"Provider '{provider_id}' requires an additional package: {e}",
        )
    except Exception as e:
        logger.warning("API key validation failed for %s: %s", provider_id, e)
        raise HTTPException(400, f"API key validation failed: {e}")

    # Install all durable values in one atomic file update before changing
    # settings or replacing the live provider.
    new_provider = create_desktop_provider(provider_id, api_key, **extra_kwargs)
    previous_provider = registry.get_provider(provider_id)
    previous_api_key = getattr(settings, pdef.settings_key, "")
    previous_base_url = (
        getattr(settings, "azure_openai_base_url", "")
        if pdef.kind == "openai_compat_azure"
        else None
    )
    env_key = f"SUXIAOYOU_{pdef.settings_key.upper()}"
    env_changes: dict[str, str | None] = {env_key: api_key}
    if pdef.kind == "openai_compat_azure":
        env_changes["SUXIAOYOU_AZURE_OPENAI_BASE_URL"] = extra_kwargs["base_url"]

    def commit_runtime() -> None:
        setattr(settings, pdef.settings_key, api_key)
        if pdef.kind == "openai_compat_azure":
            settings.azure_openai_base_url = extra_kwargs["base_url"]
        registry.register(new_provider)

    def rollback_runtime() -> None:
        setattr(settings, pdef.settings_key, previous_api_key)
        if pdef.kind == "openai_compat_azure":
            settings.azure_openai_base_url = previous_base_url or ""
        _restore_registry_provider(registry, provider_id, previous_provider)

    _persist_env_then_commit_runtime(
        env_changes,
        commit_runtime,
        rollback_runtime,
    )

    try:
        await registry.refresh_models()
    except Exception as e:
        logger.warning(
            "Model refresh failed after %s key update: %s — will retry on next request",
            provider_id, e,
        )

    return ProviderInfo(
        id=provider_id,
        name=pdef.display_name(request_language(request)),
        is_configured=True,
        masked_key=_mask_key(api_key),
        model_count=len(models),
        status="connected",
        base_url=extra_kwargs.get("base_url"),
    )


@router.delete("/config/providers/{provider_id}/key", response_model=ProviderInfo)
async def delete_provider_key(
    provider_id: str, request: Request, settings: SettingsDep, registry: ProviderRegistryDep,
) -> ProviderInfo:
    """Remove API key for a provider."""
    pdef = PROVIDER_CATALOG.get(provider_id)
    if not pdef:
        raise HTTPException(404, f"Unknown provider: {provider_id}")

    env_key = f"SUXIAOYOU_{pdef.settings_key.upper()}"
    env_changes: dict[str, str | None] = {env_key: None}
    previous_api_key = getattr(settings, pdef.settings_key, "")
    previous_base_url = (
        getattr(settings, "azure_openai_base_url", "")
        if pdef.kind == "openai_compat_azure"
        else None
    )
    previous_provider = registry.get_provider(provider_id)
    if pdef.kind == "openai_compat_azure":
        env_changes["SUXIAOYOU_AZURE_OPENAI_BASE_URL"] = None

    def commit_runtime() -> None:
        setattr(settings, pdef.settings_key, "")
        if pdef.kind == "openai_compat_azure":
            settings.azure_openai_base_url = ""
        registry.unregister(provider_id)

    def rollback_runtime() -> None:
        setattr(settings, pdef.settings_key, previous_api_key)
        if pdef.kind == "openai_compat_azure":
            settings.azure_openai_base_url = previous_base_url or ""
        _restore_registry_provider(registry, provider_id, previous_provider)

    _persist_env_then_commit_runtime(
        env_changes,
        commit_runtime,
        rollback_runtime,
    )

    return ProviderInfo(
        id=provider_id,
        name=pdef.display_name(request_language(request)),
        is_configured=False,
        status="unconfigured",
    )


@router.post("/config/providers/{provider_id}/toggle", response_model=ProviderInfo)
async def toggle_provider(
    provider_id: str, request: Request, settings: SettingsDep, registry: ProviderRegistryDep,
) -> ProviderInfo:
    """Enable or disable a provider. Disabled providers keep their key but aren't used."""
    pdef = PROVIDER_CATALOG.get(provider_id)
    if not pdef:
        raise HTTPException(404, f"Unknown provider: {provider_id}")
    disabled = _get_disabled_set(settings)

    api_key = getattr(settings, pdef.settings_key, "")
    is_currently_disabled = provider_id in disabled
    previous_disabled = settings.disabled_providers
    previous_provider = registry.get_provider(provider_id)
    provider_to_enable = None

    if is_currently_disabled:
        disabled.discard(provider_id)
        if api_key:
            try:
                extra_kwargs: dict[str, str] = {}
                if pdef.kind == "openai_compat_azure":
                    azure_url = getattr(settings, "azure_openai_base_url", "")
                    if azure_url:
                        extra_kwargs["base_url"] = azure_url
                provider_to_enable = create_desktop_provider(
                    provider_id,
                    api_key,
                    **extra_kwargs,
                )
            except Exception as e:
                logger.warning("Failed to enable provider %s: %s", provider_id, e)
    else:
        disabled.add(provider_id)

    next_disabled = ",".join(sorted(disabled))

    def commit_runtime() -> None:
        settings.disabled_providers = next_disabled
        if is_currently_disabled:
            if provider_to_enable is not None:
                registry.register(provider_to_enable)
        else:
            registry.unregister(provider_id)

    def rollback_runtime() -> None:
        settings.disabled_providers = previous_disabled
        _restore_registry_provider(registry, provider_id, previous_provider)

    _persist_env_then_commit_runtime(
        {"SUXIAOYOU_DISABLED_PROVIDERS": next_disabled},
        commit_runtime,
        rollback_runtime,
    )

    if is_currently_disabled and provider_to_enable is not None:
        try:
            await registry.refresh_models()
        except Exception as e:
            logger.warning("Failed to refresh enabled provider %s: %s", provider_id, e)

    # Build response
    provider = registry.get_provider(provider_id)
    new_enabled = provider_id not in disabled
    if new_enabled and provider and api_key:
        models = [m for p, m in registry._full_models if m.provider_id == provider_id]
        return ProviderInfo(
            id=provider_id, name=pdef.display_name(request_language(request)), is_configured=True, enabled=True,
            masked_key=_mask_key(api_key), model_count=len(models), status="connected",
        )
    elif api_key and not new_enabled:
        return ProviderInfo(
            id=provider_id, name=pdef.display_name(request_language(request)), is_configured=True, enabled=False,
            masked_key=_mask_key(api_key), status="disabled",
        )
    else:
        return ProviderInfo(
            id=provider_id, name=pdef.display_name(request_language(request)), is_configured=bool(api_key),
            enabled=new_enabled, status="unconfigured",
        )


@router.post("/config/custom", response_model=ProviderInfo)
async def create_custom_endpoint(
    body: CustomEndpointCreate, settings: SettingsDep, registry: ProviderRegistryDep
) -> ProviderInfo:
    """Create a new custom endpoint."""
    slug = body.slug
    base_url = body.base_url
    api_key = body.api_key.strip() if body.api_key else ""
    name = body.name.strip() or "Custom Endpoint"
    models_payload = [{"id": m.id, "name": m.name} for m in body.models]
    headers_payload = dict(body.headers or {})

    endpoint_id = f"custom_{slug}"

    # Uniqueness: slug must not collide with an existing custom endpoint
    # nor with any reserved provider name (slug validator already rejects
    # reserved names; the catalog check defends against future additions).
    existing = get_custom_endpoints(settings)
    if any(e.get("slug") == slug or e.get("id") == endpoint_id for e in existing):
        raise HTTPException(400, f"Provider ID '{slug}' is already in use")
    if slug in PROVIDER_CATALOG or slug in RESERVED_CUSTOM_SLUGS:
        raise HTTPException(400, f"Provider ID '{slug}' is reserved")

    try:
        test_provider = create_desktop_provider(
            endpoint_id,
            api_key,
            base_url=base_url,
            models_override=models_payload or None,
            extra_headers=headers_payload or None,
        )
        models = await test_provider.list_models()
    except Exception as e:
        logger.warning("Failed validation for custom endpoint %s: %s", name, e)
        raise HTTPException(400, f"Validation failed: {e}")

    async with _custom_endpoints_lock:
        endpoints = get_custom_endpoints(settings)
        if any(e.get("slug") == slug or e.get("id") == endpoint_id for e in endpoints):
            raise HTTPException(400, f"Provider ID '{slug}' is already in use")
        new_config = {
            "id": endpoint_id,
            "slug": slug,
            "name": name,
            "base_url": base_url,
            "api_key": api_key,
            "enabled": True,
            "models": models_payload,
            "headers": headers_payload,
        }
        endpoints.append(new_config)
        next_custom_endpoints = json.dumps(endpoints)
        previous_custom_endpoints = settings.custom_endpoints
        previous_provider = registry.get_provider(endpoint_id)

        def commit_runtime() -> None:
            settings.custom_endpoints = next_custom_endpoints
            registry.register(test_provider)

        def rollback_runtime() -> None:
            settings.custom_endpoints = previous_custom_endpoints
            _restore_registry_provider(
                registry,
                endpoint_id,
                previous_provider,
            )

        _persist_env_then_commit_runtime(
            {"SUXIAOYOU_CUSTOM_ENDPOINTS": next_custom_endpoints},
            commit_runtime,
            rollback_runtime,
        )

    try:
        # Only refresh this provider — the BYOK/Ollama/Rapid-MLX providers
        # don't change when we add a custom endpoint, so the full sweep
        # was waste.
        await registry.refresh_provider(endpoint_id)
    except Exception as e:
        logger.warning("Failed to refresh models after adding custom endpoint %s: %s", endpoint_id, e)

    return _build_custom_endpoint_info(new_config, enabled=True, status="connected", model_count=len(models))

@router.delete("/config/custom/{endpoint_id}", response_model=ProviderInfo)
async def delete_custom_endpoint(
    endpoint_id: str, settings: SettingsDep, registry: ProviderRegistryDep
) -> ProviderInfo:
    async with _custom_endpoints_lock:
        endpoints = get_custom_endpoints(settings)
        found = None
        for i, e in enumerate(endpoints):
            if e.get("id") == endpoint_id:
                found = endpoints.pop(i)
                break

        if not found:
            raise HTTPException(404, "Custom endpoint not found")

        next_custom_endpoints = json.dumps(endpoints)
        previous_custom_endpoints = settings.custom_endpoints
        previous_provider = registry.get_provider(endpoint_id)

        def commit_runtime() -> None:
            settings.custom_endpoints = next_custom_endpoints
            registry.unregister(endpoint_id)

        def rollback_runtime() -> None:
            settings.custom_endpoints = previous_custom_endpoints
            _restore_registry_provider(
                registry,
                endpoint_id,
                previous_provider,
            )

        _persist_env_then_commit_runtime(
            {"SUXIAOYOU_CUSTOM_ENDPOINTS": next_custom_endpoints},
            commit_runtime,
            rollback_runtime,
        )

    return ProviderInfo(
        id=endpoint_id, name=found.get("name", "Custom Endpoint"),
        is_configured=False, status="unconfigured"
    )

@router.patch("/config/custom/{endpoint_id}", response_model=ProviderInfo)
async def update_custom_endpoint(
    endpoint_id: str,
    body: CustomEndpointUpdate,
    settings: SettingsDep,
    registry: ProviderRegistryDep,
) -> ProviderInfo:
    """Update a custom endpoint (partial update). Slug is immutable."""
    models: list = []
    test_provider = None

    # --- Phase 1: read current config (under lock) ---
    async with _custom_endpoints_lock:
        endpoints = get_custom_endpoints(settings)

        found = None
        for e in endpoints:
            if e.get("id") == endpoint_id:
                found = e
                break

        if not found:
            raise HTTPException(404, "Custom endpoint not found")

    existing_base_url = found.get("base_url", "")
    existing_api_key = found.get("api_key", "")
    existing_models = list(found.get("models") or [])
    existing_headers: dict[str, str] = dict(found.get("headers") or {})
    prev_enabled = bool(found.get("enabled", True))

    name = body.name.strip() if body.name is not None else found.get("name", "Custom Endpoint")
    base_url = body.base_url if body.base_url is not None else existing_base_url
    api_key = body.api_key.strip() if body.api_key is not None else existing_api_key
    enabled = body.enabled if body.enabled is not None else found.get("enabled", True)

    if body.models is not None:
        models_payload = [{"id": m.id, "name": m.name} for m in body.models]
    else:
        models_payload = list(existing_models)

    # Headers follow JSON Merge Patch semantics on PATCH — body.headers is
    # a delta, never a full replacement. We mask values on GET, so the
    # frontend can't safely echo them back; instead it only sends keys it
    # explicitly changed.
    if body.headers is None:
        headers_payload = dict(existing_headers)
    else:
        headers_payload = dict(existing_headers)
        for key, value in body.headers.items():
            if value is None:
                headers_payload.pop(key, None)
            else:
                headers_payload[key] = value

    # Only rebuild the provider when a constructor-relevant field's
    # *effective value* actually changed — comparing against the stored
    # config avoids wasted /v1/models calls when the client always sends
    # all fields (e.g. the edit form re-sends base_url even when the user
    # only edited Display name). Re-enabling a previously disabled
    # endpoint also rebuilds, because toggle-off explicitly unregisters.
    needs_rebuild = (
        base_url != existing_base_url
        or api_key != existing_api_key
        or models_payload != existing_models
        or headers_payload != existing_headers
        or (enabled and not prev_enabled)
    )

    # --- Phase 2: validate (outside lock — network I/O) ---
    if needs_rebuild:
        try:
            test_provider = create_desktop_provider(
                endpoint_id,
                api_key,
                base_url=base_url,
                models_override=models_payload or None,
                extra_headers=headers_payload or None,
            )
            models = await test_provider.list_models()
        except Exception as e:
            logger.warning("Failed validation for custom endpoint %s: %s", name, e)
            raise HTTPException(400, f"Validation failed: {e}")
    else:
        provider = registry.get_provider(endpoint_id)
        models = [m for p, m in registry._full_models if m.provider_id == endpoint_id] if provider else []

    # --- Phase 3: persist (under lock) ---
    async with _custom_endpoints_lock:
        # Re-read in case another request mutated while we validated.
        endpoints = get_custom_endpoints(settings)
        found_idx = next((i for i, e in enumerate(endpoints) if e.get("id") == endpoint_id), -1)
        if found_idx == -1:
            raise HTTPException(404, "Custom endpoint was deleted during update")

        prior = endpoints[found_idx]
        if prior != found:
            # Validation ran without the lock so slow network I/O cannot block
            # every custom-provider operation. Refuse a stale compare-and-swap
            # instead of overwriting fields committed by a concurrent PATCH.
            raise HTTPException(
                409,
                "Custom endpoint changed during validation; retry the update",
            )
        updated_config = {
            "id": endpoint_id,
            "slug": prior.get("slug") or endpoint_id[len("custom_"):],
            "name": name,
            "base_url": base_url,
            "api_key": api_key,
            "enabled": enabled,
            "models": models_payload,
            "headers": headers_payload,
        }
        endpoints[found_idx] = updated_config
        next_custom_endpoints = json.dumps(endpoints)
        previous_custom_endpoints = settings.custom_endpoints
        previous_provider = registry.get_provider(endpoint_id)

        def commit_runtime() -> None:
            settings.custom_endpoints = next_custom_endpoints
            if enabled and needs_rebuild and test_provider is not None:
                registry.register(test_provider)
            elif not enabled:
                registry.unregister(endpoint_id)

        def rollback_runtime() -> None:
            settings.custom_endpoints = previous_custom_endpoints
            _restore_registry_provider(
                registry,
                endpoint_id,
                previous_provider,
            )

        _persist_env_then_commit_runtime(
            {"SUXIAOYOU_CUSTOM_ENDPOINTS": next_custom_endpoints},
            commit_runtime,
            rollback_runtime,
        )

    if enabled and needs_rebuild and test_provider is not None:
        try:
            # Single-provider refresh — see create_custom_endpoint above.
            await registry.refresh_provider(endpoint_id)
        except Exception as e:
            logger.warning("Failed to refresh models after updating custom endpoint %s: %s", endpoint_id, e)

    return _build_custom_endpoint_info(
        updated_config,
        enabled=enabled,
        status="connected" if enabled else "disabled",
        model_count=len(models),
    )

@router.get("/config/local", response_model=LocalProviderStatus)
async def get_local_provider(settings: SettingsDep, registry: ProviderRegistryDep) -> LocalProviderStatus:
    """Return the stored local endpoint configuration."""
    return _local_provider_status(settings, registry)


@router.post("/config/local", response_model=LocalProviderStatus)
async def set_local_provider(
    settings: SettingsDep, registry: ProviderRegistryDep, body: LocalProviderUpdate,
) -> LocalProviderStatus:
    """Register a locally-hosted OpenAI-compatible endpoint."""
    base_url = _normalize_local_base_url(body.base_url)
    try:
        test_provider = create_local_provider(base_url)
        models = await test_provider.list_models()
        if not models:
            raise HTTPException(400, "Local endpoint returned no models")
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("Local provider validation failed for %s: %s", base_url, e)
        raise HTTPException(400, f"Local endpoint validation failed: {e}")
    previous_base_url = settings.local_base_url
    previous_provider = registry.get_provider(LOCAL_PROVIDER_ID)

    def commit_runtime() -> None:
        settings.local_base_url = base_url
        registry.register(test_provider)

    def rollback_runtime() -> None:
        settings.local_base_url = previous_base_url
        _restore_registry_provider(
            registry,
            LOCAL_PROVIDER_ID,
            previous_provider,
        )

    _persist_env_then_commit_runtime(
        {LOCAL_BASE_URL_ENV: base_url},
        commit_runtime,
        rollback_runtime,
    )

    try:
        await registry.refresh_models()
    except Exception as e:
        logger.warning("Model refresh failed after local provider registration: %s", e)

    return LocalProviderStatus(
        base_url=base_url,
        is_configured=True,
        is_connected=True,
        status="connected",
    )


@router.delete("/config/local", response_model=LocalProviderStatus)
async def delete_local_provider(settings: SettingsDep, registry: ProviderRegistryDep) -> LocalProviderStatus:
    """Remove the local endpoint configuration."""
    previous_base_url = settings.local_base_url
    previous_provider = registry.get_provider(LOCAL_PROVIDER_ID)

    def commit_runtime() -> None:
        settings.local_base_url = ""
        registry.unregister(LOCAL_PROVIDER_ID)

    def rollback_runtime() -> None:
        settings.local_base_url = previous_base_url
        _restore_registry_provider(
            registry,
            LOCAL_PROVIDER_ID,
            previous_provider,
        )

    _persist_env_then_commit_runtime(
        {LOCAL_BASE_URL_ENV: None},
        commit_runtime,
        rollback_runtime,
    )

    try:
        await registry.refresh_models()
    except Exception as e:
        logger.warning("Model refresh failed after removing local provider: %s", e)

    return LocalProviderStatus()
