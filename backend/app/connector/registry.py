"""ConnectorRegistry — manages deduplicated MCP server connections.

Wraps McpManager (composition) and adds:
- Deduplication by URL for remote servers, by name for local
- Independent enable/disable per connector
- Custom connector CRUD
- Persistence of user state (enabled set + custom connectors)
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.connector.model import (
    CONNECTOR_PROVENANCE_CUSTOM,
    REMOTE_AUTH_OAUTH_BEARER,
    SUPPORTED_CONNECTOR_PROVENANCE,
    SUPPORTED_REMOTE_AUTH_MODES,
    ConnectorInfo,
)
from app.mcp.manager import McpManager
from app.mcp.local_approval import LocalMcpApprovalResult
from app.mcp.tool_wrapper import McpToolWrapper
from app.tool.base import ToolDefinition
from app.utils.atomic_write import atomic_write_text

logger = logging.getLogger(__name__)


class ConnectorPersistenceError(RuntimeError):
    """A connector state transition could not be durably persisted."""


def _external_runtime_stopped() -> bool:
    try:
        from app.security.control import get_security_control

        return get_security_control().emergency_stop
    except RuntimeError:
        return False


@asynccontextmanager
async def _external_runtime_transition(*, already_owned: bool = False):
    """Serialize all starts against the emergency-stop state transition."""

    if already_owned:
        yield
        return
    try:
        from app.security.control import get_security_control

        control = get_security_control()
    except RuntimeError:
        yield
        return
    async with control.transition_lock:
        yield


class ConnectorRegistry:
    """Single source of truth for all MCP connector state."""

    def __init__(self, project_dir: str | None = None) -> None:
        self._connectors: dict[str, ConnectorInfo] = {}
        self._mcp_manager: McpManager | None = None
        self._tool_registry: Any | None = None  # set via set_tool_registry()
        self._project_dir = project_dir
        # Direct Google OAuth writes credentials outside McpTokenStore and then
        # restarts the local MCP process.  Keep its callback/disconnect runtime
        # transitions ordered on this registry instance so a stale callback
        # cannot tear down a newer authorization.
        self._google_auth_operation_lock = asyncio.Lock()

        # Persistence paths
        if project_dir:
            self._state_path = Path(project_dir).resolve() / ".suxiaoyou" / "connectors.json"
        else:
            self._state_path = Path.home() / ".suxiaoyou" / "connectors.json"

        self._persisted_state = self._load_state()

        # Load static catalog (enriched metadata for known connectors)
        self._catalog = self._load_catalog()

    # ------------------------------------------------------------------
    # Registration (called during startup)
    # ------------------------------------------------------------------

    def register_from_plugin(
        self,
        plugin_name: str,
        mcp_servers: dict[str, dict[str, Any]],
        *,
        source: str = CONNECTOR_PROVENANCE_CUSTOM,
    ) -> list[str]:
        """Extract unique connectors from a plugin's MCP config.

        Deduplicates remote servers by URL, local servers by name.
        Returns the list of connector IDs this plugin references.
        """
        connector_ids: list[str] = []
        # Only bundled plugins are trusted as built-ins.  Any absent or
        # malformed provenance is conservatively treated as user supplied.
        connector_provenance = (
            source
            if isinstance(source, str)
            and source in SUPPORTED_CONNECTOR_PROVENANCE
            else CONNECTOR_PROVENANCE_CUSTOM
        )

        for raw_key, config in mcp_servers.items():
            if not isinstance(config, dict):
                continue

            # Strip plugin namespace if present (e.g. "engineering:slack" → "slack")
            if ":" in raw_key:
                connector_id = raw_key.split(":", 1)[1]
            else:
                connector_id = raw_key

            url = config.get("url", "")
            server_type = config.get("type", "remote")

            # Skip entries with no URL for remote servers
            if server_type == "remote" and not url:
                continue

            # Check if this connector already exists (dedup)
            existing = self._find_by_url(url) if url else self._connectors.get(connector_id)

            if existing:
                # Add plugin reference if not already there
                if plugin_name not in existing.referenced_by:
                    existing.referenced_by.append(plugin_name)
                connector_ids.append(existing.id)
                continue

            # Create new connector, enriched with catalog metadata
            catalog_entry = self._catalog.get(connector_id, {})
            auth_mode = catalog_entry.get("auth_mode", REMOTE_AUTH_OAUTH_BEARER)
            if auth_mode not in SUPPORTED_REMOTE_AUTH_MODES:
                # Catalog data ships with the application, but still fail
                # closed if it is malformed instead of turning it into a
                # generic header injection mechanism.
                logger.warning(
                    "Connector '%s' has unsupported auth mode %r; using OAuth bearer",
                    connector_id,
                    auth_mode,
                )
                auth_mode = REMOTE_AUTH_OAUTH_BEARER

            allowed_tool_patterns = self._catalog_patterns(
                catalog_entry,
                "allowed_tool_patterns",
            )
            approval_required_tool_patterns = self._catalog_patterns(
                catalog_entry,
                "approval_required_tool_patterns",
            )

            connector = ConnectorInfo(
                id=connector_id,
                name=catalog_entry.get("name", connector_id.replace("-", " ").title()),
                url=url,
                type=server_type,
                description=catalog_entry.get(
                    "description",
                    f"{connector_id.replace('-', ' ').title()} integration",
                ),
                category=catalog_entry.get("category", "other"),
                enabled=connector_id in self._persisted_state.get("enabled", []),
                source=connector_provenance,
                local_config=(
                    {
                        k: v
                        for k, v in config.items()
                        if k not in ("type", "url", "enabled")
                    }
                    if server_type == "local"
                    else {}
                ),
                referenced_by=[plugin_name],
                auth_mode=auth_mode,
                credential_url=str(catalog_entry.get("credential_url", "")),
                allowed_tool_patterns=allowed_tool_patterns,
                approval_required_tool_patterns=approval_required_tool_patterns,
            )

            self._connectors[connector_id] = connector
            connector_ids.append(connector_id)

        return connector_ids

    def register_custom(
        self,
        id: str,
        name: str,
        url: str,
        description: str = "",
        category: str = "custom",
    ) -> ConnectorInfo:
        """Add a user-defined custom connector."""
        if id in self._connectors:
            raise ValueError(f"Connector '{id}' already exists")

        connector = ConnectorInfo(
            id=id,
            name=name,
            url=url,
            type="remote",
            description=description or f"{name} (custom connector)",
            category=category,
            enabled=False,
            source=CONNECTOR_PROVENANCE_CUSTOM,
        )
        desired = deepcopy(self._persisted_state)
        customs = desired.setdefault("custom", [])
        customs.append({
            "id": id,
            "name": name,
            "url": url,
            "description": description,
            "category": category,
        })
        self._persist_state(desired)
        self._persisted_state = desired
        self._connectors[id] = connector

        return connector

    def remove_custom(self, id: str) -> bool:
        """Remove a custom connector. Returns False if not found or not custom."""
        connector = self._connectors.get(id)
        if not connector or connector.source != "custom":
            return False

        desired = deepcopy(self._persisted_state)
        customs = desired.get("custom", [])
        desired["custom"] = [c for c in customs if c.get("id") != id]
        desired["enabled"] = [
            connector_id
            for connector_id in desired.get("enabled", [])
            if connector_id != id
        ]
        self._persist_state(desired)
        self._persisted_state = desired
        del self._connectors[id]

        return True

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def prepare(self) -> None:
        """Build local connector state without opening network/process connections."""
        if self._mcp_manager is not None:
            return

        # Restore custom connectors from persisted state
        for custom in self._persisted_state.get("custom", []):
            cid = custom.get("id", "")
            if cid and cid not in self._connectors:
                self._connectors[cid] = ConnectorInfo(
                    id=cid,
                    name=custom.get("name", cid),
                    url=custom.get("url", ""),
                    type="remote",
                    description=custom.get("description", ""),
                    category=custom.get("category", "custom"),
                    enabled=cid in self._persisted_state.get("enabled", []),
                    source=CONNECTOR_PROVENANCE_CUSTOM,
                )

        # Inject credentials into local connectors that need them
        # Preparing the registry is part of the readiness-critical startup
        # path. Keep protected Google values opaque here; enabled connectors
        # hydrate them only in the post-yield connection task below.
        self._inject_local_credentials(resolve_secrets=False)

        # Build MCP config dict from all connectors (enabled or not)
        mcp_config: dict[str, Any] = {}
        for cid, connector in self._connectors.items():
            if connector.type == "local":
                mcp_config[cid] = {
                    "type": "local",
                    "enabled": connector.enabled,
                    **connector.local_config,
                    # This server-owned value must follow user/plugin config
                    # so a local config cannot override its trust boundary.
                    "connector_provenance": connector.source,
                }
            else:
                mcp_config[cid] = {
                    "type": "remote",
                    "url": connector.url,
                    "enabled": connector.enabled,
                    "auth_mode": connector.auth_mode,
                    "allowed_tool_patterns": list(connector.allowed_tool_patterns),
                    "approval_required_tool_patterns": list(
                        connector.approval_required_tool_patterns
                    ),
                    "connector_provenance": connector.source,
                }

        self._mcp_manager = McpManager(
            mcp_config,
            project_dir=self._project_dir,
            start_allowed=lambda: not _external_runtime_stopped(),
        )

        logger.info(
            "ConnectorRegistry prepared: %d connectors (%d enabled)",
            len(self._connectors),
            sum(1 for c in self._connectors.values() if c.enabled),
        )

    async def connect_enabled(self, *, transition_owned: bool = False) -> None:
        """Connect enabled servers, then publish their discovered tools."""
        async with _external_runtime_transition(already_owned=transition_owned):
            if _external_runtime_stopped():
                self.sync_tools()
                return
            if self._mcp_manager is None:
                self.prepare()
            if self._mcp_manager is None:  # pragma: no cover - defensive invariant
                return
            await asyncio.to_thread(
                self._inject_local_credentials,
                resolve_secrets=True,
            )
            await self._mcp_manager.startup()
            self.sync_tools()

    async def startup(self) -> None:
        """Backward-compatible combined prepare/connect lifecycle."""
        self.prepare()
        await self.connect_enabled()

    async def shutdown(self) -> None:
        """Disconnect all MCP servers."""
        if self._mcp_manager:
            await self._mcp_manager.shutdown()

    # ------------------------------------------------------------------
    # CRUD / state
    # ------------------------------------------------------------------

    def list_connectors(self) -> list[ConnectorInfo]:
        """Return all connectors sorted by name."""
        return sorted(self._connectors.values(), key=lambda c: c.name)

    def get(self, id: str) -> ConnectorInfo | None:
        """Get a single connector by ID."""
        return self._connectors.get(id)

    async def enable(self, id: str) -> bool:
        """Enable a connector and attempt to connect it."""
        if id == "google-workspace":
            async with self._google_auth_operation_lock:
                await asyncio.to_thread(self._inject_local_credentials)
                return await self._enable_unlocked(id)
        return await self._enable_unlocked(id)

    async def _enable_unlocked(self, id: str) -> bool:
        async with _external_runtime_transition():
            connector = self._connectors.get(id)
            if not connector or connector.enabled:
                return False

            desired = deepcopy(self._persisted_state)
            enabled = desired.setdefault("enabled", [])
            if id not in enabled:
                enabled.append(id)
            self._persist_state(desired)
            self._persisted_state = desired
            connector.enabled = True
            logger.info("Connector enabled: %s", id)
            if self._mcp_manager:
                config = self._mcp_manager._config.get(id)
                if isinstance(config, dict):
                    config["enabled"] = True

            # Actually connect the MCP server only while the same transition
            # lock still protects the emergency-stop decision.
            if self._mcp_manager and not _external_runtime_stopped():
                try:
                    await self._mcp_manager.reconnect(id)
                except Exception as e:
                    logger.warning("Failed to connect '%s' after enable: %s", id, e)
                self.sync_tools()

            return True

    async def disable(self, id: str) -> bool:
        """Disable a connector and disconnect it."""
        if id == "google-workspace":
            from app.api.google_auth import fence_google_auth_disconnect

            # Disabling must also cancel a direct OAuth callback already in
            # flight; otherwise it could persist tokens and reconnect the
            # runtime immediately after the user disabled the connector.
            fence_google_auth_disconnect(self._project_dir)
            async with self._google_auth_operation_lock:
                return await self._disable_unlocked(id)
        return await self._disable_unlocked(id)

    async def _disable_unlocked(self, id: str) -> bool:
        connector = self._connectors.get(id)
        if not connector or not connector.enabled:
            return False

        desired = deepcopy(self._persisted_state)
        desired["enabled"] = [
            connector_id
            for connector_id in desired.get("enabled", [])
            if connector_id != id
        ]
        self._persist_state(desired)
        self._persisted_state = desired
        connector.enabled = False
        logger.info("Connector disabled: %s", id)

        # Disconnect the MCP server
        if self._mcp_manager:
            config = self._mcp_manager._config.get(id)
            if isinstance(config, dict):
                config["enabled"] = False
            try:
                await self._mcp_manager.disable(id)
            except Exception as e:
                logger.warning("Failed to disconnect '%s' after disable: %s", id, e)
            self.sync_tools()

        return True

    async def connect(self, id: str, redirect_uri: str) -> dict[str, str] | None:
        """Start OAuth flow for a connector. Returns auth URL info or None."""
        async with _external_runtime_transition():
            if _external_runtime_stopped():
                return None
            # Google Workspace uses /api/google/auth-start and a generation-fenced
            # direct OAuth state machine. Never create a second generic MCP OAuth
            # flow for the same local runtime.
            if id == "google-workspace":
                return None
            if not self._mcp_manager:
                return None
            return await self._mcp_manager.start_auth(id, redirect_uri)

    async def complete_auth(self, state: str, code: str) -> bool:
        """Complete OAuth flow with auth code."""
        async with _external_runtime_transition():
            if _external_runtime_stopped():
                return False
            if not self._mcp_manager:
                return False
            result = await self._mcp_manager.complete_auth(state, code)
            if result:
                self.sync_tools()
            return result

    async def disconnect(self, id: str) -> bool:
        """Revoke OAuth tokens and disconnect."""
        if id == "google-workspace":
            from app.api.google_auth import (
                delete_google_tokens,
                fence_google_auth_disconnect,
            )

            # Fence before the first await. Callback commits for this workspace
            # cannot pass their generation CAS while this operation waits.
            fence_google_auth_disconnect(self._project_dir)
            async with self._google_auth_operation_lock:
                result = await self._disconnect_unlocked(id)
                if result:
                    delete_google_tokens(self._project_dir)
                    await asyncio.to_thread(self._inject_local_credentials)
                return result
        return await self._disconnect_unlocked(id)

    async def _disconnect_unlocked(self, id: str) -> bool:
        if not self._mcp_manager:
            return False
        result = await self._mcp_manager.disconnect_auth(id)
        self.sync_tools()
        return result

    async def reconnect(self, id: str) -> bool:
        """Reconnect a specific connector."""
        if id == "google-workspace":
            async with self._google_auth_operation_lock:
                await asyncio.to_thread(self._inject_local_credentials)
                return await self._reconnect_unlocked(id)
        return await self._reconnect_unlocked(id)

    async def approve_local_startup(
        self,
        id: str,
        fingerprint: str,
    ) -> LocalMcpApprovalResult:
        """Approve one exact local launch and immediately attempt connection."""

        async with _external_runtime_transition():
            connector = self._connectors.get(id)
            if (
                connector is None
                or connector.type != "local"
                or not connector.enabled
                or self._mcp_manager is None
                or _external_runtime_stopped()
            ):
                return LocalMcpApprovalResult(
                    approval_persisted=False,
                    connected=False,
                    status="blocked",
                    error="Connector is not eligible for local startup approval",
                )
            approved = await self._mcp_manager.approve_local_startup(id, fingerprint)
            self.sync_tools()
            return approved

    async def _reconnect_unlocked(self, id: str) -> bool:
        async with _external_runtime_transition():
            connector = self._connectors.get(id)
            if (
                _external_runtime_stopped()
                or connector is None
                or not connector.enabled
                or not self._mcp_manager
            ):
                return False
            result = await self._mcp_manager.reconnect(id)
            self.sync_tools()
            return result

    async def reconnect_google_runtime_locked(self) -> bool:
        """Reconnect Google while its direct-OAuth operation lock is held."""

        if not self._google_auth_operation_lock.locked():
            raise RuntimeError("Google OAuth operation lock is not held")
        return await self._reconnect_unlocked("google-workspace")

    async def disconnect_google_runtime_locked(self) -> bool:
        """Close Google runtime only; direct-token cleanup is caller-owned."""

        if not self._google_auth_operation_lock.locked():
            raise RuntimeError("Google OAuth operation lock is not held")
        return await self._disconnect_unlocked("google-workspace")

    # ------------------------------------------------------------------
    # Tool registry integration
    # ------------------------------------------------------------------

    def set_tool_registry(self, registry: Any) -> None:
        """Bind a ToolRegistry so MCP tool changes are synced automatically."""
        self._tool_registry = registry

    def sync_tools(self) -> None:
        """Synchronise MCP tools in the ToolRegistry with current connections.

        Removes stale MCP tools and adds newly available ones.
        Called automatically after enable/disable/connect/disconnect.
        """
        if not self._tool_registry:
            return

        # Remove all existing MCP tools (they start with a connector id prefix)
        existing_ids = [
            tid for tid, tool in list(self._tool_registry._tools.items())
            if isinstance(tool, McpToolWrapper)
        ]
        for tid in existing_ids:
            self._tool_registry.unregister(tid)

        # Re-add tools from currently connected servers
        mcp_tools = self.tools()
        for tool in mcp_tools:
            self._tool_registry.register(tool)

        # Register/unregister ToolSearchTool based on MCP tool availability
        from app.tool.builtin.tool_search import ToolSearchTool

        has_search = self._tool_registry.get("tool_search") is not None
        if mcp_tools and not has_search:
            self._tool_registry.register(ToolSearchTool(self._tool_registry))
        elif not mcp_tools and has_search:
            self._tool_registry.unregister("tool_search")

        logger.info(
            "MCP tools synced: %d tools from connected servers",
            len(mcp_tools),
        )

    # ------------------------------------------------------------------
    # Tool / status access (delegates to McpManager)
    # ------------------------------------------------------------------

    def tools(self) -> list[ToolDefinition]:
        """Get all tools from connected MCP servers."""
        if not self._mcp_manager:
            return []
        return self._mcp_manager.tools()

    def status(self) -> dict[str, dict[str, Any]]:
        """Return connection status of all connectors.

        Merges McpManager runtime status with ConnectorInfo metadata.
        """
        mcp_status = self._mcp_manager.status() if self._mcp_manager else {}

        result: dict[str, dict[str, Any]] = {}
        for cid, connector in self._connectors.items():
            runtime = mcp_status.get(cid, {})
            mcp_connected = runtime.get("status") == "connected"
            effective_status = runtime.get("status", "disabled" if not connector.enabled else "disconnected")

            local_approval = None
            if self._mcp_manager is not None and connector.type == "local":
                candidate = self._mcp_manager.local_startup_approval(cid)
                if isinstance(candidate, dict):
                    local_approval = candidate
                    if connector.enabled and candidate.get("required"):
                        effective_status = "needs_approval"
                        mcp_connected = False

            # Google Workspace: MCP server can start without OAuth tokens.
            # Run direct OAuth before local-process approval so the one approval
            # presented after callback covers the exact credential environment.
            if cid == "google-workspace" and connector.enabled:
                from app.api.google_auth import load_google_tokens
                tokens = load_google_tokens(self._project_dir)
                if not tokens or not tokens.get("refresh_token"):
                    mcp_connected = False
                    effective_status = "needs_auth"

            result[cid] = {
                **connector.to_dict(),
                "connected": mcp_connected,
                "status": effective_status,
                "error": runtime.get("error"),
                "tools_count": runtime.get("tools", 0),
                "credential_configured": (
                    self._mcp_manager.has_stored_token(cid)
                    if self._mcp_manager and connector.type != "local"
                    else False
                ),
                "local_approval": local_approval,
            }

        return result

    @property
    def mcp_manager(self) -> McpManager | None:
        """Access the underlying McpManager (for backward compatibility)."""
        return self._mcp_manager

    @property
    def google_auth_operation_lock(self) -> asyncio.Lock:
        """Serialize direct Google OAuth runtime transitions."""

        return self._google_auth_operation_lock

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _inject_local_credentials(self, *, resolve_secrets: bool = True) -> None:
        """Inject stored credentials into local connectors as environment vars.

        google-workspace-mcp expects:
          GOOGLE_WORKSPACE_CLIENT_ID
          GOOGLE_WORKSPACE_CLIENT_SECRET
          GOOGLE_WORKSPACE_REFRESH_TOKEN
        """
        gw = self._connectors.get("google-workspace")
        if not gw or gw.type != "local":
            return

        try:
            from app.config import get_settings
            settings = get_settings()
            client_id = settings.google_client_id
            client_secret = settings.google_client_secret
            if resolve_secrets:
                from app.auth.credential_store import resolve_env_value

                client_secret = resolve_env_value(
                    "SUXIAOYOU_GOOGLE_CLIENT_SECRET",
                    client_secret,
                )
        except Exception:
            return

        if not client_id:
            return

        from app.api.google_auth import load_google_tokens
        tokens = load_google_tokens(self._project_dir)

        env = gw.local_config.setdefault("environment", {})
        env["GOOGLE_WORKSPACE_CLIENT_ID"] = client_id
        if resolve_secrets:
            env["GOOGLE_WORKSPACE_CLIENT_SECRET"] = client_secret
        else:
            # Never hand an opaque reference to a child process as though it
            # were a usable secret. The shared config dict is populated by the
            # post-readiness connection path before an enabled connector runs.
            env.pop("GOOGLE_WORKSPACE_CLIENT_SECRET", None)

        if tokens and tokens.get("refresh_token"):
            env["GOOGLE_WORKSPACE_REFRESH_TOKEN"] = tokens["refresh_token"]
        else:
            env.pop("GOOGLE_WORKSPACE_REFRESH_TOKEN", None)

    def _find_by_url(self, url: str) -> ConnectorInfo | None:
        """Find a connector by URL (for dedup)."""
        if not url:
            return None
        normalized = self._normalize_url(url)
        for connector in self._connectors.values():
            if self._normalize_url(connector.url) == normalized:
                return connector
        return None

    @staticmethod
    def _normalize_url(url: str) -> str:
        """Normalize URL for deduplication comparison."""
        parsed = urlparse(url)
        # Strip trailing slashes, lowercase host
        path = parsed.path.rstrip("/")
        return f"{parsed.scheme}://{parsed.netloc.lower()}{path}"

    def _load_catalog(self) -> dict[str, dict[str, Any]]:
        """Load the static connector catalog with enriched metadata."""
        catalog_path = Path(__file__).parent.parent / "data" / "connectors.json"
        if not catalog_path.is_file():
            return {}
        try:
            data = json.loads(catalog_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Cannot read connector catalog: %s", e)
            return {}

    @staticmethod
    def _catalog_patterns(entry: dict[str, Any], key: str) -> list[str]:
        """Return a small, validated list of trusted tool-name patterns."""

        raw = entry.get(key, [])
        if not isinstance(raw, list):
            return []
        patterns: list[str] = []
        for item in raw:
            if not isinstance(item, str):
                continue
            pattern = item.strip()
            if pattern and len(pattern) <= 160:
                patterns.append(pattern)
        return patterns

    def _load_state(self) -> dict[str, Any]:
        """Load persisted user state (enabled set + custom connectors)."""
        if not self._state_path.is_file():
            return {"enabled": [], "custom": []}
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Cannot read connector state: %s", e)
        return {"enabled": [], "custom": []}

    def _persist_state(self, state: dict[str, Any] | None = None) -> None:
        """Save user state to disk."""
        desired = self._persisted_state if state is None else state
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(
                self._state_path,
                json.dumps(desired, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.error("Cannot persist connector state: %s", exc)
            raise ConnectorPersistenceError(
                "Connector state could not be saved; no runtime change was applied"
            ) from exc
