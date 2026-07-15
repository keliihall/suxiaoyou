"""Connector management endpoints — individual MCP server connections."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.api.oauth_redirect import loopback_redirect_uri
from app.auth.local import require_local_session
from app.connector.model import REMOTE_AUTH_RAW_AUTHORIZATION
from app.connector.registry import ConnectorPersistenceError
from app.dependencies import SessionFactoryDep
from app.mcp.local_approval import LocalMcpApprovalStoreError
from app.security.audit import record_security_event

router = APIRouter(prefix="/connectors")


def _get_registry(request: Request):
    return getattr(request.app.state, "connector_registry", None)


def _failure(error_code: str, error: str, **extra: Any) -> dict[str, Any]:
    """Return a stable machine code without removing the diagnostic text."""

    return {
        "success": False,
        "error_code": error_code,
        "error": error,
        **extra,
    }


# ------------------------------------------------------------------
# List
# ------------------------------------------------------------------


@router.get("")
async def list_connectors(request: Request) -> dict[str, Any]:
    """Return all connectors with status."""
    registry = _get_registry(request)
    if registry is None:
        return {"connectors": {}}
    return {"connectors": registry.status()}


# ------------------------------------------------------------------
# OAuth callback — MUST be before /{connector_id} to avoid conflict
# ------------------------------------------------------------------


class AuthCallbackBody(BaseModel):
    code: str
    state: str


@router.get("/oauth/callback")
async def oauth_callback(code: str, state: str, request: Request):
    """OAuth callback — receives auth code from provider redirect."""
    registry = _get_registry(request)
    if registry is None:
        return HTMLResponse("<p>Connector system not available</p>")

    success = await registry.complete_auth(state, code)

    from app.api.callback_html import render_callback
    return HTMLResponse(content=render_callback(
        success,
        extra_data={"state": state},
    ))


# ------------------------------------------------------------------
# Detail (after /oauth/callback to avoid route conflict)
# ------------------------------------------------------------------


@router.get("/{connector_id}")
async def connector_detail(connector_id: str, request: Request) -> dict[str, Any]:
    """Return details for a single connector."""
    registry = _get_registry(request)
    if registry is None:
        raise HTTPException(status_code=404, detail="Connector system not available")

    status = registry.status()
    detail = status.get(connector_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Connector not found: {connector_id}")
    return detail


# ------------------------------------------------------------------
# Custom connector CRUD
# ------------------------------------------------------------------


class AddConnectorBody(BaseModel):
    id: str
    name: str
    url: str
    description: str = ""
    category: str = "custom"


@router.post("")
async def add_custom_connector(body: AddConnectorBody, request: Request) -> dict[str, Any]:
    """Add a user-defined custom connector."""
    registry = _get_registry(request)
    if registry is None:
        return _failure("connector_system_unavailable", "Connector system not available")
    try:
        connector = registry.register_custom(
            id=body.id,
            name=body.name,
            url=body.url,
            description=body.description,
            category=body.category,
        )
        return {"success": True, "connector": connector.to_dict()}
    except ConnectorPersistenceError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except ValueError as e:
        return _failure("connector_invalid", str(e))


@router.delete("/{connector_id}")
async def remove_custom_connector(connector_id: str, request: Request) -> dict[str, Any]:
    """Remove a custom connector."""
    registry = _get_registry(request)
    if registry is None:
        return _failure("connector_system_unavailable", "Connector system not available")
    try:
        success = registry.remove_custom(connector_id)
    except ConnectorPersistenceError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if not success:
        return _failure(
            "connector_not_custom",
            "Not found or not a custom connector",
        )
    return {"success": True}


# ------------------------------------------------------------------
# Local stdio startup approval
# ------------------------------------------------------------------


class ApproveLocalStartupBody(BaseModel):
    fingerprint: str = Field(
        min_length=71,
        max_length=71,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    confirmed: bool = False


@router.post(
    "/{connector_id}/approve-local-startup",
    dependencies=[Depends(require_local_session)],
)
async def approve_local_startup(
    connector_id: str,
    body: ApproveLocalStartupBody,
    request: Request,
    session_factory: SessionFactoryDep,
) -> dict[str, Any]:
    """Approve the exact reviewed stdio launch from the local desktop only."""

    registry = _get_registry(request)
    if registry is None:
        return _failure("connector_system_unavailable", "Connector system not available")
    connector = registry.get(connector_id)
    if connector is None:
        return _failure("connector_not_found", f"Connector not found: {connector_id}")
    if connector.type != "local":
        return _failure(
            "connector_local_approval_not_required",
            "Remote connectors do not need local startup approval",
        )
    if not connector.enabled:
        return _failure(
            "connector_enable_required",
            "Enable the connector before approving its local process",
        )
    if not body.confirmed:
        return _failure(
            "explicit_confirmation_required",
            "Explicit confirmation is required",
        )

    manager = registry.mcp_manager
    approval = manager.local_startup_approval(connector_id) if manager else None
    if (
        not isinstance(approval, dict)
        or approval.get("fingerprint") != body.fingerprint
    ):
        return _failure(
            "local_command_changed",
            "The local command changed; review the current launch configuration",
            connectors=registry.status(),
        )

    # The durable audit commit is a precondition for writing approval state or
    # spawning the process.  This endpoint is local-session-only, so scheduled,
    # channel, remote, and other non-interactive sources cannot manufacture it.
    await record_security_event(
        session_factory,
        source_kind="connector",
        source_id=connector_id,
        invocation_source_kind="desktop",
        capability="process",
        action="approve_local_startup",
        decision="allow",
        outcome="started",
        details={
            "fingerprint": body.fingerprint,
            "environment_count": len(approval.get("environment_keys", [])),
        },
        required=True,
    )

    try:
        result = await registry.approve_local_startup(
            connector_id,
            body.fingerprint,
        )
    except LocalMcpApprovalStoreError:
        success = False
        approval_persisted = False
        connection_status = "blocked"
        error = "Local approval state could not be persisted safely"
        error_code = "local_approval_persist_failed"
    else:
        approval_persisted = result.approval_persisted
        success = result.connected
        connection_status = result.status
        error = result.error
        error_code = None
        if not success and error is None:
            error = (
                "Local startup approval was saved, but the connector did not connect"
                if approval_persisted
                else "The local command changed; review it again"
            )
        if not success:
            error_code = (
                "local_approval_connect_failed"
                if approval_persisted
                else "local_command_changed"
            )

    await record_security_event(
        session_factory,
        source_kind="connector",
        source_id=connector_id,
        invocation_source_kind="desktop",
        capability="process",
        action="approve_local_startup",
        decision="allow",
        outcome=(
            "success"
            if success
            else "error"
            if approval_persisted
            else "blocked"
        ),
        details={
            "fingerprint": body.fingerprint,
            "approval_persisted": approval_persisted,
            "connection_status": connection_status,
        },
    )
    response = {
        "success": success,
        "approval_persisted": approval_persisted,
        "connection_status": connection_status,
        "error": error,
        "connectors": registry.status(),
    }
    if error_code:
        response["error_code"] = error_code
    return response


# ------------------------------------------------------------------
# Token (PAT / API key)
# ------------------------------------------------------------------


class SetTokenBody(BaseModel):
    token: str


def _validated_token(value: str) -> str | None:
    """Validate without reflecting credential material in a 422 response."""

    token = value.strip()
    if not token or len(token) > 8192:
        return None
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in token):
        return None
    return token


@router.post("/{connector_id}/token")
async def set_connector_token(
    connector_id: str, body: SetTokenBody, request: Request
) -> dict[str, Any]:
    """Store a PAT/personal token securely and reconnect the connector."""
    registry = _get_registry(request)
    if registry is None:
        return _failure("connector_system_unavailable", "Connector system not available")

    connector = registry.get(connector_id)
    if not connector:
        return _failure("connector_not_found", f"Connector not found: {connector_id}")
    if connector_id == "google-workspace":
        return _failure(
            "google_oauth_required",
            "Google Workspace credentials require the direct OAuth flow",
        )

    token = _validated_token(body.token)
    if token is None:
        return _failure("invalid_connector_token", "Invalid connector token")

    # The manager persists through McpTokenStore -> CredentialStore.  The raw
    # token is never written to connector state or returned by this endpoint.
    mgr = registry.mcp_manager
    if not mgr or not mgr.set_static_token(connector_id, token):
        return _failure(
            "connector_token_unsupported",
            "Connector does not accept a token",
        )

    # Enable if not already
    if not connector.enabled:
        try:
            await registry.enable(connector_id)
        except ConnectorPersistenceError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
    else:
        await registry.reconnect(connector_id)

    return {"success": True, "connectors": registry.status()}


# ------------------------------------------------------------------
# Enable / disable
# ------------------------------------------------------------------


@router.post("/{connector_id}/enable")
async def enable_connector(connector_id: str, request: Request) -> dict[str, Any]:
    """Enable a connector."""
    registry = _get_registry(request)
    if registry is None:
        return _failure("connector_system_unavailable", "Connector system not available")
    try:
        success = await registry.enable(connector_id)
    except ConnectorPersistenceError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if not success:
        return _failure("connector_enable_failed", "Could not enable connector")
    return {"success": success, "connectors": registry.status()}


@router.post("/{connector_id}/disable")
async def disable_connector(connector_id: str, request: Request) -> dict[str, Any]:
    """Disable a connector."""
    registry = _get_registry(request)
    if registry is None:
        return _failure("connector_system_unavailable", "Connector system not available")
    try:
        success = await registry.disable(connector_id)
    except ConnectorPersistenceError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if not success:
        return _failure("connector_disable_failed", "Could not disable connector")
    return {"success": success, "connectors": registry.status()}


# ------------------------------------------------------------------
# OAuth connect / disconnect / reconnect
# ------------------------------------------------------------------


@router.post("/{connector_id}/connect")
async def connect_connector(connector_id: str, request: Request) -> dict[str, Any]:
    """Start OAuth flow for a connector."""
    registry = _get_registry(request)
    if registry is None:
        return _failure("connector_system_unavailable", "Connector system not available")
    if connector_id == "google-workspace":
        return _failure(
            "google_oauth_required",
            "Google Workspace credentials require the direct OAuth flow",
        )
    connector = registry.get(connector_id)
    if connector and connector.auth_mode == REMOTE_AUTH_RAW_AUTHORIZATION:
        return _failure(
            "personal_token_required",
            "This connector requires a personal token instead of OAuth",
        )

    settings = request.app.state.settings
    redirect_uri = loopback_redirect_uri(settings, "/api/connectors/oauth/callback")

    result = await registry.connect(connector_id, redirect_uri)
    if result is None:
        return _failure(
            "oauth_discovery_failed",
            "Could not discover OAuth server for this connector",
        )
    return {"success": True, **result}


@router.post("/{connector_id}/auth-callback")
async def auth_callback_api(
    connector_id: str, body: AuthCallbackBody, request: Request
) -> dict[str, Any]:
    """API-based auth callback."""
    registry = _get_registry(request)
    if registry is None:
        return _failure("connector_system_unavailable", "Connector system not available")
    if connector_id == "google-workspace":
        return _failure(
            "google_oauth_required",
            "Google Workspace credentials require the direct OAuth flow",
        )
    success = await registry.complete_auth(body.state, body.code)
    if not success:
        return _failure("connector_auth_callback_failed", "Connector authorization failed")
    return {"success": success, "connectors": registry.status()}


@router.post("/{connector_id}/disconnect")
async def disconnect_connector(connector_id: str, request: Request) -> dict[str, Any]:
    """Remove OAuth tokens and disconnect a connector."""
    registry = _get_registry(request)
    if registry is None:
        return _failure("connector_system_unavailable", "Connector system not available")
    success = await registry.disconnect(connector_id)
    if not success:
        return _failure("connector_disconnect_failed", "Could not disconnect connector")
    return {"success": success, "connectors": registry.status()}


@router.post("/{connector_id}/reconnect")
async def reconnect_connector(connector_id: str, request: Request) -> dict[str, Any]:
    """Reconnect a specific connector."""
    registry = _get_registry(request)
    if registry is None:
        return _failure("connector_system_unavailable", "Connector system not available")
    success = await registry.reconnect(connector_id)
    if not success:
        return _failure("connector_reconnect_failed", "Could not reconnect connector")
    return {"success": success, "connectors": registry.status()}
