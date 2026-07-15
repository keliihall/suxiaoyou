"""MCP manager — lifecycle management for all configured MCP servers."""

from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
from typing import Any, Callable

from app.connector.model import REMOTE_AUTH_RAW_AUTHORIZATION
from app.mcp.client import McpClient
from app.mcp.local_approval import (
    LocalMcpApprovalResult,
    LocalMcpApprovalStore,
    local_mcp_launch_spec,
)
from app.mcp.oauth import (
    AuthServerMeta,
    PendingAuth,
    TokenSet,
    build_authorization_url,
    discover_auth_server,
    exchange_code,
    generate_pkce_pair,
    refresh_token,
)
from app.mcp.token_store import McpTokenStore
from app.mcp.tool_wrapper import McpToolWrapper
from app.tool.base import ToolDefinition

logger = logging.getLogger(__name__)


class McpManager:
    """Manages all MCP server connections and exposes their tools."""

    def __init__(
        self,
        mcp_config: dict[str, Any],
        project_dir: str | None = None,
        *,
        approval_store: LocalMcpApprovalStore | None = None,
        start_allowed: Callable[[], bool] | None = None,
    ) -> None:
        self._config = mcp_config
        self._clients: dict[str, McpClient] = {}
        self._token_store = McpTokenStore(project_dir)
        self._approval_store = approval_store or LocalMcpApprovalStore(project_dir)
        self._start_allowed = start_allowed or (lambda: True)
        self._accepting_starts = True
        self._shutdown_requests = 0
        self._operation_locks: dict[str, asyncio.Lock] = {}
        self._operation_generations: dict[str, int] = {}
        self._pending_auths: dict[str, PendingAuth] = {}  # keyed by state param

    def _new_client(self, name: str, config: dict[str, Any]) -> McpClient:
        """Construct a client with trust supplied only by private state."""

        is_local = config.get("type", "local") == "local"
        approved_fingerprint = self._approval_store.get(name) if is_local else None
        approval_check = (
            lambda fingerprint: self._local_approval_is_current(name, fingerprint)
            if is_local
            else None
        )
        return McpClient(
            name,
            config,
            approved_local_fingerprint=approved_fingerprint,
            local_approval_check=approval_check,
            start_allowed=self._can_start,
        )

    def _local_approval_is_current(self, name: str, fingerprint: str) -> bool:
        stored = self._approval_store.get(name)
        return bool(stored and hmac.compare_digest(stored, fingerprint))

    def _can_start(self) -> bool:
        if not self._accepting_starts:
            return False
        return self._runtime_guard_allows()

    def _runtime_guard_allows(self) -> bool:
        try:
            return bool(self._start_allowed())
        except Exception:
            logger.exception("MCP runtime start guard failed closed")
            return False

    def _operation_lock(self, name: str) -> asyncio.Lock:
        return self._operation_locks.setdefault(name, asyncio.Lock())

    def _advance_generation(self, name: str) -> None:
        self._operation_generations[name] = self._operation_generations.get(name, 0) + 1

    async def _inject_stored_token(self, name: str, client: McpClient) -> None:
        """Load/refresh protected credentials while the connector lock is held."""

        # Native credential APIs are synchronous and may display an OS consent
        # dialog. Enabled connectors auto-connect only after application
        # readiness; keep that explicit operation off the event loop so the UI
        # remains responsive while the user decides.
        stored = await asyncio.to_thread(self._token_store.get, name)
        if stored and not stored.expired:
            client.set_oauth_token(stored.access_token)
        elif stored and stored.expired and stored.refresh_token:
            auth_meta = self._token_store.get_auth_meta(name)
            if auth_meta:
                try:
                    new_tokens = await refresh_token(auth_meta, stored.refresh_token)
                    await asyncio.to_thread(
                        self._token_store.save,
                        name,
                        new_tokens,
                        auth_meta,
                    )
                    client.set_oauth_token(new_tokens.access_token)
                    logger.info("Refreshed OAuth token for MCP server '%s'", name)
                except Exception as exc:
                    logger.warning("Token refresh failed for '%s': %s", name, exc)

    async def _connect_locked(self, name: str, config: dict[str, Any]) -> bool:
        """Replace and connect one client; caller owns its operation lock."""

        previous = self._clients.pop(name, None)
        if previous is not None:
            await previous.close()
        if not self._can_start():
            return False

        client = self._new_client(name, config)
        await self._inject_stored_token(name, client)
        if (
            config.get("auth_mode") == REMOTE_AUTH_RAW_AUTHORIZATION
            and not client._oauth_token
        ):
            client.status = "needs_auth"
            client.error = None
            if self._can_start():
                self._clients[name] = client
            return False

        await client.connect()
        if (
            client.status == "failed"
            and client.server_type != "local"
            and not client._oauth_token
        ):
            client.status = "needs_auth"
            client.error = None

        # shutdown/emergency-stop can close the gate while connect awaits.  Do
        # not publish that client, and make a best-effort close before return.
        if not self._can_start():
            await client.close()
            return False
        self._clients[name] = client
        return client.status == "connected"

    async def startup(self) -> None:
        """Connect to all enabled MCP servers."""
        if self._shutdown_requests:
            return
        if not self._runtime_guard_allows():
            self._accepting_starts = False
            return
        self._accepting_starts = True
        for name, config in self._config.items():
            if not isinstance(config, dict):
                continue
            if not config.get("enabled", True):
                logger.info("MCP server '%s' is disabled, skipping", name)
                continue

            lock = self._operation_lock(name)
            async with lock:
                if not self._can_start():
                    break
                existing = self._clients.get(name)
                if existing is not None and existing.status == "connected":
                    continue
                try:
                    await self._connect_locked(name, config)
                except Exception as exc:
                    logger.error("MCP server '%s' failed to start: %s — skipping", name, exc)
                finally:
                    self._advance_generation(name)

        connected = sum(1 for c in self._clients.values() if c.status == "connected")
        total = len(self._clients)
        logger.info("MCP startup complete: %d/%d servers connected", connected, total)

    async def shutdown(self) -> None:
        """Disconnect from all MCP servers."""
        # Close the gate before the first await. Operations already inside a
        # connector lock will observe it after their current awaited connect.
        self._shutdown_requests += 1
        self._accepting_starts = False
        try:
            self._pending_auths.clear()
            names = set(self._config) | set(self._clients) | set(self._operation_locks)
            for name in sorted(names):
                async with self._operation_lock(name):
                    client = self._clients.pop(name, None)
                    if client is not None:
                        try:
                            await client.close()
                        except Exception:
                            logger.exception("Error closing MCP server '%s'", client.name)
                    self._advance_generation(name)
        finally:
            self._shutdown_requests -= 1

    def tools(self) -> list[ToolDefinition]:
        """Get all tools from connected MCP servers as ToolDefinitions."""
        result: list[ToolDefinition] = []
        for client in self._clients.values():
            if client.status != "connected":
                continue
            for mcp_tool in client.list_tools():
                wrapper = McpToolWrapper(client, mcp_tool)
                result.append(wrapper)
        return result

    def status(self) -> dict[str, dict[str, Any]]:
        """Return status of all MCP servers."""
        result: dict[str, dict[str, Any]] = {}
        for name, client in self._clients.items():
            entry: dict[str, Any] = {
                "status": client.status,
                "error": client.error,
                "type": client.server_type,
                "tools": len(client.list_tools()),
            }
            approval = self.local_startup_approval(name)
            if approval is not None:
                entry["local_approval"] = approval
            result[name] = entry
        return result

    def local_startup_approval(self, name: str) -> dict[str, Any] | None:
        """Return the current local approval request, with secrets redacted."""

        config = self._config.get(name)
        if (
            not isinstance(config, dict)
            or config.get("type", "local") != "local"
        ):
            return None
        try:
            launch = local_mcp_launch_spec(config)
        except (OSError, ValueError) as exc:
            return {
                "required": True,
                "approved": False,
                "fingerprint": None,
                "command": [],
                "cwd": None,
                "environment_keys": [],
                "executable_path": None,
                "executable_sha256": None,
                "error": str(exc),
            }

        stored = self._approval_store.get(name)
        approved = bool(
            stored
            and hmac.compare_digest(stored, launch.fingerprint)
        )
        descriptor = launch.public_descriptor()
        descriptor.update({
            "required": not approved,
            "approved": approved,
            "error": self._approval_store.degraded_reason,
        })
        return descriptor

    async def approve_local_startup(
        self,
        name: str,
        fingerprint: str,
    ) -> LocalMcpApprovalResult:
        """Persist explicit approval, then and only then attempt the spawn.

        The caller must be the local interactive approval API.  Every other
        startup/reconnect path can consume a prior exact approval but has no
        API for manufacturing one.
        """

        async with self._operation_lock(name):
            if not self._can_start():
                self._advance_generation(name)
                return LocalMcpApprovalResult(
                    approval_persisted=False,
                    connected=False,
                    status="blocked",
                    error="External runtime is stopped",
                )
            approval = self.local_startup_approval(name)
            if (
                approval is None
                or not isinstance(approval.get("fingerprint"), str)
                or not hmac.compare_digest(approval["fingerprint"], fingerprint)
            ):
                self._advance_generation(name)
                return LocalMcpApprovalResult(
                    approval_persisted=False,
                    connected=False,
                    status="needs_approval",
                    error="The local command changed; review it again",
                )
            # The mutation is idempotent.  A duplicate desktop request must
            # not restart a process whose startup may already have produced
            # side effects.
            if approval.get("approved"):
                client = self._clients.get(name)
                status = client.status if client is not None else "disconnected"
                self._advance_generation(name)
                return LocalMcpApprovalResult(
                    approval_persisted=True,
                    connected=status == "connected",
                    status=status,
                    error=client.error if client is not None else None,
                    duplicate=True,
                )

            config = self._config.get(name)
            if not isinstance(config, dict):  # pragma: no cover - guarded above
                self._advance_generation(name)
                return LocalMcpApprovalResult(False, False, "not_found")

            # Persistence is a precondition for spawn.  If app-private state is
            # damaged or unwritable, approve() raises and no process is opened.
            self._approval_store.approve(name, fingerprint)

            if not self._can_start():
                self._advance_generation(name)
                return LocalMcpApprovalResult(
                    approval_persisted=True,
                    connected=False,
                    status="blocked",
                    error="Approval was saved, but external runtime is stopped",
                )

            # Re-evaluate after the write.  A concurrent credential/config
            # update produces a new fingerprint and must be reviewed again.
            current = self.local_startup_approval(name)
            if (
                current is None
                or not current.get("approved")
                or current.get("fingerprint") != fingerprint
            ):
                self._advance_generation(name)
                return LocalMcpApprovalResult(
                    approval_persisted=True,
                    connected=False,
                    status="needs_approval",
                    error="The local command changed after approval was saved",
                )

            old_client = self._clients.pop(name, None)
            if old_client is not None:
                await old_client.close()

            if not self._can_start():
                self._advance_generation(name)
                return LocalMcpApprovalResult(
                    approval_persisted=True,
                    connected=False,
                    status="blocked",
                    error="Approval was saved, but external runtime is stopped",
                )

            client = self._new_client(name, config)
            await client.connect()
            # A final client-side check sits immediately before transport
            # creation.  If the config moved in the narrow race above, it is
            # safe (nothing spawned) and the UI receives a fresh approval.
            final_approval = self.local_startup_approval(name)
            if (
                final_approval is None
                or not final_approval.get("approved")
                or final_approval.get("fingerprint") != fingerprint
            ):
                await client.close()
                client.status = "needs_approval"
                client.error = None
                self._advance_generation(name)
                return LocalMcpApprovalResult(
                    approval_persisted=True,
                    connected=False,
                    status="needs_approval",
                    error="The local command changed during connection",
                )
            if not self._can_start():
                await client.close()
                self._advance_generation(name)
                return LocalMcpApprovalResult(
                    approval_persisted=True,
                    connected=False,
                    status="blocked",
                    error="Approval was saved, but external runtime is stopped",
                )

            self._clients[name] = client
            self._advance_generation(name)
            return LocalMcpApprovalResult(
                approval_persisted=True,
                connected=client.status == "connected",
                status=client.status,
                error=client.error,
            )

    async def reconnect(self, name: str) -> bool:
        """Reconnect a specific MCP server. Returns True if successful."""
        config = self._config.get(name)
        if not isinstance(config, dict) or not config.get("enabled", True):
            return False
        observed_generation = self._operation_generations.get(name, 0)
        async with self._operation_lock(name):
            # Coalesce reconnects that waited behind another lifecycle change.
            # This is important for duplicate UI requests because startup can
            # itself have externally visible side effects.
            if self._operation_generations.get(name, 0) != observed_generation:
                client = self._clients.get(name)
                return bool(client and client.status == "connected")
            if not self._can_start():
                return False
            try:
                return await self._connect_locked(name, config)
            finally:
                self._advance_generation(name)

    async def disable(self, name: str) -> bool:
        """Close one connector under the same lifecycle lock as startup."""

        async with self._operation_lock(name):
            client = self._clients.get(name)
            if client is None:
                self._advance_generation(name)
                return True
            await client.close()
            client.status = "disabled"
            client.error = None
            self._advance_generation(name)
            return True

    def has_stored_token(self, name: str) -> bool:
        """Return credential presence without exposing credential material."""

        return self._token_store.has_token(name)

    def set_static_token(self, name: str, token: str) -> bool:
        """Persist a PAT/personal token using the protected MCP token store."""

        config = self._config.get(name)
        if not isinstance(config, dict) or config.get("type", "local") == "local":
            return False

        # Construct the client before persistence so an invalid trusted auth
        # mode cannot leave an unusable credential behind.
        validator = McpClient(name, config, start_allowed=self._can_start)
        _ = validator.auth_mode

        self._token_store.save(
            name,
            TokenSet(
                access_token=token,
                refresh_token=None,
                expires_at=0,
                token_type=(
                    "RawAuthorization"
                    if validator.auth_mode == REMOTE_AUTH_RAW_AUTHORIZATION
                    else "Bearer"
                ),
                scope="",
            ),
        )
        # The next lifecycle operation injects the protected value while
        # holding this connector's async lock.  Do not mutate a live client
        # synchronously while reconnect/shutdown may be using it.
        return True

    # ------------------------------------------------------------------
    # OAuth flow
    # ------------------------------------------------------------------

    async def start_auth(
        self, name: str, redirect_uri: str
    ) -> dict[str, str] | None:
        """Start an OAuth flow for a server. Returns auth URL + state, or None."""
        config = self._config.get(name)
        if not isinstance(config, dict):
            return None
        if config.get("auth_mode") == REMOTE_AUTH_RAW_AUTHORIZATION:
            return None

        url = config.get("url", "")
        if not url:
            return None

        # Discover auth server
        auth_meta = await discover_auth_server(url)
        if not auth_meta or not auth_meta.authorization_endpoint:
            return None

        # Obtain client_id: stored → DCR → fallback probe
        client_id = self._token_store.get_client_id(name) or ""

        if not client_id:
            # Try Dynamic Client Registration (RFC 7591)
            reg_endpoint = auth_meta.registration_endpoint
            if not reg_endpoint:
                # Some servers support DCR but don't advertise it — try common paths
                from urllib.parse import urlparse
                parsed = urlparse(auth_meta.authorization_endpoint)
                reg_endpoint = f"{parsed.scheme}://{parsed.netloc}/register"

            if reg_endpoint:
                from app.mcp.oauth import register_client as _register
                client_id = await _register(
                    AuthServerMeta(
                        authorization_endpoint=auth_meta.authorization_endpoint,
                        token_endpoint=auth_meta.token_endpoint,
                        scopes=auth_meta.scopes,
                        resource_url=auth_meta.resource_url,
                        registration_endpoint=reg_endpoint,
                    ),
                    redirect_uris=[redirect_uri],
                ) or ""
                if client_id:
                    self._token_store.save_client_id(name, client_id)
                    logger.warning("[OAuth] Registered client_id for '%s': %s", name, client_id[:20])

        if not client_id:
            logger.warning("[OAuth] No client_id for '%s' — auth may fail (server may not support DCR)", name)

        # Generate PKCE + state
        verifier, challenge = generate_pkce_pair()
        state = secrets.token_urlsafe(32)

        # Build authorization URL
        auth_url = build_authorization_url(
            auth_meta=auth_meta,
            redirect_uri=redirect_uri,
            state=state,
            code_challenge=challenge,
            client_id=client_id,
        )

        # Store pending auth
        self._pending_auths[state] = PendingAuth(
            server_name=name,
            mcp_url=url,
            auth_meta=auth_meta,
            pkce_verifier=verifier,
            state=state,
            redirect_uri=redirect_uri,
            client_id=client_id,
        )

        return {"auth_url": auth_url, "state": state}

    async def complete_auth(self, state: str, code: str) -> bool:
        """Complete an OAuth flow with the auth code. Returns True if successful."""
        logger.warning("[OAuth] complete_auth called (state=%s..., %d pending)", state[:8], len(self._pending_auths))
        pending = self._pending_auths.pop(state, None)
        if not pending:
            logger.warning("[OAuth] No pending auth found for state=%s (keys: %s)", state[:8], list(self._pending_auths.keys())[:3])
            return False

        logger.warning(
            "[OAuth] Exchanging code for '%s' (client_id=%s, redirect=%s, token_ep=%s)",
            pending.server_name,
            pending.client_id[:20] if pending.client_id else "<none>",
            pending.redirect_uri,
            pending.auth_meta.token_endpoint,
        )
        try:
            tokens = await exchange_code(
                auth_meta=pending.auth_meta,
                code=code,
                redirect_uri=pending.redirect_uri,
                pkce_verifier=pending.pkce_verifier,
                client_id=pending.client_id,
            )
            logger.warning("[OAuth] Token exchange succeeded for '%s'!", pending.server_name)
        except Exception as e:
            logger.warning("[OAuth] Token exchange FAILED for '%s': %s", pending.server_name, e, exc_info=True)
            return False

        # Store tokens
        self._token_store.save(pending.server_name, tokens, pending.auth_meta)

        # Try to connect through the same serialized lifecycle path used by
        # every other reconnect. Token acquisition remains successful even if
        # the remote MCP endpoint is temporarily unavailable.
        try:
            connected = await self.reconnect(pending.server_name)
            if connected:
                logger.warning(
                    "[OAuth] MCP connection succeeded for '%s'",
                    pending.server_name,
                )
            else:
                logger.warning(
                    "[OAuth] MCP reconnect did not connect for '%s' (token stored OK)",
                    pending.server_name,
                )
        except Exception as exc:
            logger.warning(
                "[OAuth] MCP reconnect failed for '%s': %s (token stored OK)",
                pending.server_name,
                exc,
            )

        # Token was obtained — that's a success regardless of MCP connection
        return True

    async def disconnect_auth(self, name: str) -> bool:
        """Remove stored tokens and disconnect a server."""
        async with self._operation_lock(name):
            self._token_store.delete(name)

            client = self._clients.get(name)
            if client:
                client.set_oauth_token(None)
                await client.close()
                client.status = "needs_auth"
                client.error = None
            self._advance_generation(name)
            return True
