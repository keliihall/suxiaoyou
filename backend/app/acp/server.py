"""Bounded ACP v1 JSON-RPC server over newline-delimited streams.

The official Python SDK supplies the generated ACP schema used here. Its
general-purpose ``Connection`` intentionally accepts much larger frames and
does not impose application session/concurrency limits, so SuXiaoYou owns this
small framing layer while keeping wire models upstream-defined.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
import json
import logging
from pathlib import Path
from typing import Any, Protocol
import uuid

from acp.meta import PROTOCOL_VERSION
from acp.schema import (
    AuthenticateRequest,
    AuthenticateResponse,
    InitializeRequest,
    InitializeResponse,
    LoadSessionRequest,
    LoadSessionResponse,
    NewSessionRequest,
    NewSessionResponse,
    PermissionOption,
    PromptRequest,
    PromptResponse,
    RequestPermissionRequest,
    RequestPermissionResponse,
    SessionNotification,
    ToolCallUpdate,
)
from pydantic import BaseModel, ValidationError

from app.acp.bridge import (
    ACP_AUTH_REQUIRED,
    ACP_METHOD_NOT_FOUND,
    ACP_RESOURCE_NOT_FOUND,
    BridgeCapabilities,
    BridgeRpcError,
    ReversePermissionUnavailable,
    SessionPromptBridge,
    UpdatePayload,
)
from app.version import APP_VERSION


logger = logging.getLogger(__name__)

JSONRPC_VERSION = "2.0"
ACP_PROTOCOL_VERSION = int(PROTOCOL_VERSION)

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = ACP_METHOD_NOT_FOUND
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
AUTH_REQUIRED = ACP_AUTH_REQUIRED
RESOURCE_NOT_FOUND = ACP_RESOURCE_NOT_FOUND
SERVER_BUSY = -32003
SESSION_LIMIT_REACHED = -32004

_MISSING = object()
_ResponseId = str | int | None


class AsyncLineReader(Protocol):
    async def readline(self) -> bytes: ...


class AsyncByteWriter(Protocol):
    def write(self, data: bytes) -> Any: ...

    async def drain(self) -> Any: ...


class AcpFeatureDisabled(RuntimeError):
    """Raised before stdio is consumed while the code-owned gate is closed."""


class _OutboundMessageTooLarge(RuntimeError):
    pass


class _DuplicateJsonKey(ValueError):
    pass


@dataclass(slots=True)
class _PendingClientRequest:
    """One server-owned reverse request awaiting a client response."""

    session_id: str
    future: asyncio.Future[RequestPermissionResponse]
    # Wire option ids are server-owned. Values recover the bridge's private
    # option ids without ever emitting those ids to the client.
    option_ids: dict[str, str]


@dataclass(frozen=True, slots=True)
class AcpLimits:
    """Resource ceilings for one ACP stdio connection."""

    max_message_bytes: int = 1024 * 1024
    max_json_depth: int = 32
    max_concurrent_requests: int = 8
    max_pending_client_requests: int = 8
    max_sessions: int = 16
    max_prompt_blocks: int = 128
    max_session_id_chars: int = 256
    shutdown_timeout_seconds: float = 5.0
    reverse_request_timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        for name in (
            "max_message_bytes",
            "max_json_depth",
            "max_concurrent_requests",
            "max_pending_client_requests",
            "max_sessions",
            "max_prompt_blocks",
            "max_session_id_chars",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be positive")
        if not 0 < self.shutdown_timeout_seconds <= 30:
            raise ValueError("shutdown_timeout_seconds must be in (0, 30]")
        if not 0 < self.reverse_request_timeout_seconds <= 300:
            raise ValueError("reverse_request_timeout_seconds must be in (0, 300]")


def acp_runtime_enabled() -> bool:
    """Read the code-owned release gate dynamically for tests and rollout."""

    from app.release_readiness import v11_capability_released

    return v11_capability_released("acp")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _reject_non_finite(value: str) -> None:
    raise ValueError(f"Non-finite JSON number {value!r} is not permitted")


def _json_depth(value: Any) -> int:
    """Return container depth without recursion or attacker-controlled stack use."""

    maximum = 1
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        maximum = max(maximum, depth)
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
    return maximum


def _validation_data(exc: ValidationError) -> dict[str, Any]:
    """Return field locations without echoing potentially secret ACP inputs."""

    fields: list[str] = []
    for error in exc.errors(include_url=False, include_context=False, include_input=False):
        location = ".".join(str(part) for part in error.get("loc", ()))
        if location and location not in fields:
            fields.append(location)
        if len(fields) >= 16:
            break
    return {"reason": "schema_validation_failed", "fields": fields}


def _model_payload(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(
        mode="json",
        by_alias=True,
        exclude_none=True,
        exclude_unset=True,
    )


class AcpServer:
    """One ACP v1 connection backed by a permission-aware session bridge."""

    def __init__(
        self,
        bridge: SessionPromptBridge,
        *,
        limits: AcpLimits | None = None,
        enabled: bool | None = None,
    ) -> None:
        self.bridge = bridge
        self.limits = limits or AcpLimits()
        self._enabled_override = enabled
        self._writer: AsyncByteWriter | None = None
        self._write_lock = asyncio.Lock()
        self._request_tasks: set[asyncio.Task[None]] = set()
        self._request_ids: set[tuple[type[Any], Any]] = set()
        self._pending_client_requests: dict[str, _PendingClientRequest] = {}
        self._active_prompts: dict[str, asyncio.Task[Any]] = {}
        self._cancelled_sessions: set[str] = set()
        self._sessions: set[str] = set()
        self._initialized = False
        self._authenticated = False
        self._closing = False
        self._client_initialize: InitializeRequest | None = None

        capabilities = getattr(bridge, "capabilities", None)
        if not isinstance(capabilities, BridgeCapabilities):
            raise TypeError("ACP bridge capabilities must be BridgeCapabilities")
        self.capabilities = capabilities
        # Freeze a private copy so a mutable mapping supplied inside the
        # frozen dataclass cannot change authentication after negotiation.
        self._auth_methods = tuple(dict(method) for method in capabilities.auth_methods)

    @property
    def enabled(self) -> bool:
        if self._enabled_override is not None:
            return self._enabled_override
        return acp_runtime_enabled()

    async def serve(
        self,
        reader: AsyncLineReader,
        writer: AsyncByteWriter,
    ) -> None:
        """Serve NDJSON until EOF, then cancel turns and detach observers."""

        if not self.enabled:
            raise AcpFeatureDisabled("ACP stdio is disabled by the v1.1 release gate")
        if self._writer is not None:
            raise RuntimeError("AcpServer instances serve exactly one connection")

        self._writer = writer
        self.bridge.bind_permission_requester(self.request_permission)
        try:
            while not self._closing:
                try:
                    frame = await reader.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    await self._send_error(
                        None,
                        INVALID_REQUEST,
                        "Invalid request",
                        {"reason": "message_too_large"},
                    )
                    break
                if not frame:
                    break
                await self._accept_frame(frame)
        finally:
            try:
                await self._shutdown()
            finally:
                self.bridge.bind_permission_requester(None)

    async def _accept_frame(self, frame: bytes) -> None:
        if not frame.strip():
            return
        if len(frame) > self.limits.max_message_bytes:
            await self._send_error(
                None,
                INVALID_REQUEST,
                "Invalid request",
                {"reason": "message_too_large"},
            )
            return
        if not frame.endswith(b"\n"):
            await self._send_error(
                None,
                INVALID_REQUEST,
                "Invalid request",
                {"reason": "incomplete_ndjson_frame"},
            )
            return

        try:
            message = json.loads(
                frame.decode("utf-8", errors="strict"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_non_finite,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
            await self._send_error(None, PARSE_ERROR, "Parse error")
            return

        if _json_depth(message) > self.limits.max_json_depth:
            await self._send_error(
                None,
                INVALID_REQUEST,
                "Invalid request",
                {"reason": "message_too_deep"},
            )
            return
        if not isinstance(message, dict):
            await self._send_error(None, INVALID_REQUEST, "Invalid request")
            return

        raw_id = message.get("id", _MISSING)
        response_id: _ResponseId = None
        if raw_id is not _MISSING:
            if isinstance(raw_id, bool) or not isinstance(raw_id, (str, int, type(None))):
                await self._send_error(None, INVALID_REQUEST, "Invalid request")
                return
            response_id = raw_id
        if message.get("jsonrpc") != JSONRPC_VERSION:
            await self._send_error(response_id, INVALID_REQUEST, "Invalid request")
            return
        method = message.get("method", _MISSING)
        if method is _MISSING:
            # JSON-RPC is bidirectional. A response to a server-owned reverse
            # request has no method and must never enter normal ACP dispatch.
            if raw_id is _MISSING:
                await self._send_error(None, INVALID_REQUEST, "Invalid request")
                return
            self._accept_client_response(response_id, message)
            return
        if not isinstance(method, str) or not method:
            await self._send_error(response_id, INVALID_REQUEST, "Invalid request")
            return

        params = message.get("params", {})
        if raw_id is _MISSING:
            await self._handle_notification(method, params)
            return

        identity = (type(response_id), response_id)
        if identity in self._request_ids:
            await self._send_error(
                None,
                INVALID_REQUEST,
                "Invalid request",
                {"reason": "duplicate_request_id"},
            )
            return
        if len(self._request_tasks) >= self.limits.max_concurrent_requests:
            await self._send_error(
                response_id,
                SERVER_BUSY,
                "Server busy",
                {"limit": self.limits.max_concurrent_requests},
            )
            return

        self._request_ids.add(identity)
        task = asyncio.create_task(
            self._run_request(response_id, identity, method, params),
            name=f"acp.request.{method}",
        )
        self._request_tasks.add(task)
        task.add_done_callback(self._request_tasks.discard)
        # Give an earlier control request (especially initialize) a deterministic
        # chance to establish state before another already-buffered frame.
        await asyncio.sleep(0)

    def _accept_client_response(
        self,
        response_id: _ResponseId,
        message: Mapping[str, Any],
    ) -> None:
        """Resolve only a live, server-owned reverse request.

        Unknown and duplicate response ids are deliberately ignored: replying
        with another JSON-RPC error would create a response loop, while
        ignoring them cannot grant authority. A malformed response for a live
        id fails that request closed immediately.
        """

        if not isinstance(response_id, str):
            return
        pending = self._pending_client_requests.pop(response_id, None)
        if pending is None or pending.future.done():
            return

        has_result = "result" in message
        has_error = "error" in message
        if has_result == has_error:
            pending.future.set_exception(
                ReversePermissionUnavailable("malformed reverse response")
            )
            return
        if has_error:
            pending.future.set_exception(
                ReversePermissionUnavailable("client rejected reverse request")
            )
            return

        try:
            response = RequestPermissionResponse.model_validate(message["result"])
            outcome = response.outcome
            if outcome.outcome == "selected":
                private_option_id = pending.option_ids.get(outcome.option_id)
                if private_option_id is None:
                    raise ValueError("unknown permission option")
                response = RequestPermissionResponse.model_validate(
                    {
                        "outcome": {
                            "outcome": "selected",
                            "optionId": private_option_id,
                        }
                    }
                )
        except (ValidationError, ValueError, TypeError) as exc:
            pending.future.set_exception(
                ReversePermissionUnavailable("invalid reverse permission response")
            )
            logger.debug("Rejected malformed ACP reverse response: %s", type(exc).__name__)
            return
        pending.future.set_result(response)

    async def _run_request(
        self,
        request_id: _ResponseId,
        identity: tuple[type[Any], Any],
        method: str,
        params: Any,
    ) -> None:
        try:
            result = await self._dispatch_request(method, params)
            await self._send_result(request_id, result)
        except BridgeRpcError as exc:
            await self._send_error(request_id, exc.code, str(exc), exc.data)
        except ValidationError as exc:
            await self._send_error(
                request_id,
                INVALID_PARAMS,
                "Invalid params",
                _validation_data(exc),
            )
        except _OutboundMessageTooLarge:
            await self._send_error(
                request_id,
                INTERNAL_ERROR,
                "Internal error",
                {"reason": "outbound_message_too_large"},
            )
        except asyncio.CancelledError:
            if not self._closing:
                raise
        except Exception:
            logger.exception("ACP request failed: %s", method)
            await self._send_error(request_id, INTERNAL_ERROR, "Internal error")
        finally:
            self._request_ids.discard(identity)
            current = asyncio.current_task()
            if current is not None:
                # Do not leave a completed request counted as concurrent until
                # its done-callback gets another event-loop turn. A client may
                # legally send its next request immediately after the response.
                self._request_tasks.discard(current)

    async def _dispatch_request(self, method: str, params: Any) -> BaseModel | Any:
        if method == "initialize":
            return await self._initialize(params)
        if not self._initialized:
            raise BridgeRpcError(
                INVALID_REQUEST,
                "Invalid request",
                {"reason": "not_initialized"},
            )
        if method == "authenticate":
            return await self._authenticate(params)
        if not self._authenticated:
            raise BridgeRpcError.auth_required()

        if method == "session/new":
            return await self._new_session(params)
        if method == "session/load":
            return await self._load_session(params)
        if method == "session/prompt":
            return await self._prompt(params)
        if method.startswith("_"):
            return await self.bridge.extension_request(method[1:], params)
        raise BridgeRpcError.method_not_found(method)

    async def _initialize(self, params: Any) -> InitializeResponse:
        if self._initialized:
            raise BridgeRpcError(
                INVALID_REQUEST,
                "Invalid request",
                {"reason": "already_initialized"},
            )
        request = InitializeRequest.model_validate(params)
        if request.protocol_version < ACP_PROTOCOL_VERSION:
            raise BridgeRpcError(
                INVALID_PARAMS,
                "Invalid params",
                {
                    "reason": "unsupported_protocol_version",
                    "supported": [ACP_PROTOCOL_VERSION],
                },
            )

        response = self._initialize_response()
        await self.bridge.initialize(request)
        self._client_initialize = request
        self._authenticated = not bool(self._auth_methods)
        self._initialized = True
        return response

    def _initialize_response(self) -> InitializeResponse:
        agent_capabilities: dict[str, Any] = {}
        if self.capabilities.load_session:
            agent_capabilities["loadSession"] = True

        prompt_capabilities: dict[str, bool] = {}
        if self.capabilities.image_prompts:
            prompt_capabilities["image"] = True
        if self.capabilities.audio_prompts:
            prompt_capabilities["audio"] = True
        if self.capabilities.embedded_context:
            prompt_capabilities["embeddedContext"] = True
        if prompt_capabilities:
            agent_capabilities["promptCapabilities"] = prompt_capabilities

        mcp_capabilities: dict[str, bool] = {}
        if self.capabilities.mcp_http:
            mcp_capabilities["http"] = True
        if self.capabilities.mcp_sse:
            mcp_capabilities["sse"] = True
        if mcp_capabilities:
            agent_capabilities["mcpCapabilities"] = mcp_capabilities

        if self.capabilities.additional_directories:
            agent_capabilities["sessionCapabilities"] = {
                "additionalDirectories": {}
            }

        return InitializeResponse.model_validate(
            {
                "protocolVersion": ACP_PROTOCOL_VERSION,
                "agentCapabilities": agent_capabilities,
                "agentInfo": {
                    "name": "suxiaoyou",
                    "title": "苏小有",
                    "version": APP_VERSION,
                },
                "authMethods": list(self._auth_methods),
            }
        )

    async def _authenticate(self, params: Any) -> AuthenticateResponse:
        if not self._auth_methods:
            raise BridgeRpcError.method_not_found("authenticate")
        request = AuthenticateRequest.model_validate(params)
        offered = {
            str(method.get("id"))
            for method in self._auth_methods
            if isinstance(method.get("id"), str)
        }
        if request.method_id not in offered:
            raise BridgeRpcError(
                INVALID_PARAMS,
                "Invalid params",
                {"reason": "unknown_auth_method"},
            )
        raw_response = await self.bridge.authenticate(request)
        response = AuthenticateResponse.model_validate(
            {} if raw_response is None else raw_response
        )
        self._authenticated = True
        return response

    async def _new_session(self, params: Any) -> NewSessionResponse:
        request = NewSessionRequest.model_validate(params)
        self._validate_session_setup(request)
        if len(self._sessions) >= self.limits.max_sessions:
            raise BridgeRpcError(
                SESSION_LIMIT_REACHED,
                "Session limit reached",
                {"limit": self.limits.max_sessions},
            )
        response = NewSessionResponse.model_validate(
            await self.bridge.new_session(request)
        )
        self._validate_session_id(response.session_id)
        if response.session_id in self._sessions:
            raise BridgeRpcError(
                INTERNAL_ERROR,
                "Internal error",
                {"reason": "bridge_returned_duplicate_session"},
            )
        self._sessions.add(response.session_id)
        return response

    async def _load_session(self, params: Any) -> LoadSessionResponse:
        if not self.capabilities.load_session:
            raise BridgeRpcError.method_not_found("session/load")
        request = LoadSessionRequest.model_validate(params)
        self._validate_session_setup(request)
        self._validate_session_id(request.session_id)
        if (
            request.session_id not in self._sessions
            and len(self._sessions) >= self.limits.max_sessions
        ):
            raise BridgeRpcError(
                SESSION_LIMIT_REACHED,
                "Session limit reached",
                {"limit": self.limits.max_sessions},
            )

        async def emit(update: UpdatePayload) -> None:
            await self._emit_update(request.session_id, update)

        raw_response = await self.bridge.load_session(request, emit)
        response = LoadSessionResponse.model_validate(
            {} if raw_response is None else raw_response
        )
        self._sessions.add(request.session_id)
        return response

    async def _prompt(self, params: Any) -> PromptResponse:
        request = PromptRequest.model_validate(params)
        self._validate_session_id(request.session_id)
        if request.session_id not in self._sessions:
            raise BridgeRpcError.session_not_found(request.session_id)
        if len(request.prompt) > self.limits.max_prompt_blocks:
            raise BridgeRpcError(
                INVALID_PARAMS,
                "Invalid params",
                {"reason": "too_many_prompt_blocks"},
            )
        self._validate_prompt_capabilities(request)
        if request.session_id in self._active_prompts:
            raise BridgeRpcError(
                SERVER_BUSY,
                "Server busy",
                {"reason": "session_prompt_already_active"},
            )

        task = asyncio.current_task()
        if task is None:  # pragma: no cover - asyncio always provides one here
            raise RuntimeError("ACP prompt is not running in a task")
        self._active_prompts[request.session_id] = task

        async def emit(update: UpdatePayload) -> None:
            await self._emit_update(request.session_id, update)

        try:
            try:
                raw_response = await self.bridge.prompt(request, emit)
                response = PromptResponse.model_validate(raw_response)
            except asyncio.CancelledError:
                if self._closing:
                    raise
                if request.session_id not in self._cancelled_sessions:
                    raise
                response = PromptResponse.model_validate(
                    {"stopReason": "cancelled"}
                )
            if request.session_id in self._cancelled_sessions:
                payload = _model_payload(response)
                payload["stopReason"] = "cancelled"
                response = PromptResponse.model_validate(payload)
            return response
        finally:
            if self._active_prompts.get(request.session_id) is task:
                self._active_prompts.pop(request.session_id, None)
            self._cancelled_sessions.discard(request.session_id)

    def _validate_session_setup(
        self,
        request: NewSessionRequest | LoadSessionRequest,
    ) -> None:
        if not Path(request.cwd).is_absolute():
            raise BridgeRpcError(
                INVALID_PARAMS,
                "Invalid params",
                {"reason": "cwd_must_be_absolute"},
            )
        additional = request.additional_directories or []
        if additional and not self.capabilities.additional_directories:
            raise BridgeRpcError(
                INVALID_PARAMS,
                "Invalid params",
                {"reason": "additional_directories_not_supported"},
            )
        if any(not Path(item).is_absolute() for item in additional):
            raise BridgeRpcError(
                INVALID_PARAMS,
                "Invalid params",
                {"reason": "additional_directory_must_be_absolute"},
            )

        for server in request.mcp_servers:
            kind = getattr(server, "type", None)
            if kind == "http":
                supported = self.capabilities.mcp_http
            elif kind == "sse":
                supported = self.capabilities.mcp_sse
            else:
                supported = self.capabilities.mcp_stdio
            if not supported:
                raise BridgeRpcError(
                    INVALID_PARAMS,
                    "Invalid params",
                    {"reason": "mcp_transport_not_supported"},
                )

    def _validate_prompt_capabilities(self, request: PromptRequest) -> None:
        for block in request.prompt:
            kind = getattr(block, "type", None)
            if kind == "text":
                continue
            if kind == "image" and self.capabilities.image_prompts:
                continue
            if kind == "audio" and self.capabilities.audio_prompts:
                continue
            if kind in {"resource", "resource_link"} and self.capabilities.embedded_context:
                continue
            raise BridgeRpcError(
                INVALID_PARAMS,
                "Invalid params",
                {"reason": f"prompt_content_not_supported:{kind or 'unknown'}"},
            )

    def _validate_session_id(self, session_id: str) -> None:
        if not session_id or len(session_id) > self.limits.max_session_id_chars:
            raise BridgeRpcError(
                INVALID_PARAMS,
                "Invalid params",
                {"reason": "invalid_session_id"},
            )

    async def _emit_update(self, session_id: str, update: UpdatePayload) -> None:
        if isinstance(update, BaseModel):
            update_payload: Any = _model_payload(update)
        elif isinstance(update, Mapping):
            update_payload = dict(update)
        else:
            raise TypeError("ACP session update must be a schema model or mapping")
        notification = SessionNotification.model_validate(
            {"sessionId": session_id, "update": update_payload}
        )
        await self._send_notification("session/update", notification)

    async def _handle_notification(self, method: str, params: Any) -> None:
        if method == "session/cancel":
            try:
                from acp.schema import CancelNotification

                notification = CancelNotification.model_validate(params)
            except ValidationError:
                logger.warning("Ignored invalid ACP session/cancel notification")
                return
            await self._cancel_prompt(notification.session_id)
            return
        if method.startswith("_"):
            if not self._initialized or not self._authenticated:
                return
            try:
                await self.bridge.extension_notification(method[1:], params)
            except Exception:
                logger.exception("ACP extension notification failed: %s", method)
        # Unknown non-extension notifications are ignored by JSON-RPC/ACP.

    async def _cancel_prompt(self, session_id: str) -> None:
        if session_id not in self._active_prompts:
            return
        self._cancelled_sessions.add(session_id)
        self._fail_pending_client_requests(
            session_id=session_id,
            reason="ACP session was cancelled",
        )
        try:
            await self.bridge.cancel(session_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            # session/cancel is a notification and cannot receive an error.
            logger.exception("ACP bridge cancellation failed for session %s", session_id)
            task = self._active_prompts.get(session_id)
            if task is not None:
                task.cancel()

    async def request_permission(
        self,
        request: RequestPermissionRequest,
    ) -> RequestPermissionResponse:
        """Ask the ACP client for one permission decision, with strict bounds.

        The server rewrites all display data and identifiers before sending the
        official request. This means a future bridge regression cannot leak a
        tool argument, path, Hook command, secret, or internal call id through
        this transport.
        """

        if self._closing or self._writer is None:
            raise ReversePermissionUnavailable("ACP connection is closed")
        if not self._initialized or not self._authenticated:
            raise ReversePermissionUnavailable("ACP connection is not ready")
        if request.session_id not in self._active_prompts:
            raise ReversePermissionUnavailable("ACP prompt is not active")
        if not 1 <= len(request.options) <= 4:
            raise ReversePermissionUnavailable("invalid permission option count")
        if len(self._pending_client_requests) >= self.limits.max_pending_client_requests:
            raise ReversePermissionUnavailable("too many pending reverse requests")

        kinds = [option.kind for option in request.options]
        if len(set(kinds)) != len(kinds):
            raise ReversePermissionUnavailable("duplicate permission option kind")
        if any(kind not in {"allow_once", "reject_once"} for kind in kinds):
            # Persistent ACP choices are not equivalent to SuXiaoYou's scoped
            # remembered rules. Until an exact, ceiling-preserving mapping is
            # reviewed, the protocol boundary refuses to offer them.
            raise ReversePermissionUnavailable("persistent permission options unsupported")

        request_id = f"sxy-permission-{uuid.uuid4().hex}"
        while request_id in self._pending_client_requests:  # pragma: no cover
            request_id = f"sxy-permission-{uuid.uuid4().hex}"

        labels = {
            "allow_once": "Allow once",
            "reject_once": "Reject once",
        }
        option_ids: dict[str, str] = {}
        wire_options: list[PermissionOption] = []
        for option in request.options:
            wire_id = f"option-{len(wire_options) + 1}"
            option_ids[wire_id] = option.option_id
            wire_options.append(
                PermissionOption(
                    option_id=wire_id,
                    kind=option.kind,
                    name=labels[option.kind],
                )
            )

        wire_request = RequestPermissionRequest.model_validate(
            {
                "sessionId": request.session_id,
                "options": wire_options,
                "toolCall": ToolCallUpdate.model_validate(
                    {
                        "toolCallId": f"permission-{uuid.uuid4().hex}",
                        "title": "Permission required",
                        "kind": "other",
                        "status": "pending",
                    }
                ),
            }
        )
        future: asyncio.Future[RequestPermissionResponse] = (
            asyncio.get_running_loop().create_future()
        )
        pending = _PendingClientRequest(
            session_id=request.session_id,
            future=future,
            option_ids=option_ids,
        )
        self._pending_client_requests[request_id] = pending
        try:
            await self._write_message(
                {
                    "jsonrpc": JSONRPC_VERSION,
                    "id": request_id,
                    "method": "session/request_permission",
                    "params": _model_payload(wire_request),
                }
            )
            return await asyncio.wait_for(
                future,
                timeout=self.limits.reverse_request_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise ReversePermissionUnavailable(
                "ACP permission request timed out"
            ) from exc
        except _OutboundMessageTooLarge as exc:
            raise ReversePermissionUnavailable(
                "ACP permission request exceeded the wire limit"
            ) from exc
        finally:
            current = self._pending_client_requests.get(request_id)
            if current is pending:
                self._pending_client_requests.pop(request_id, None)
            if not future.done():
                future.cancel()

    def _fail_pending_client_requests(
        self,
        *,
        session_id: str | None = None,
        reason: str,
    ) -> None:
        for request_id, pending in tuple(self._pending_client_requests.items()):
            if session_id is not None and pending.session_id != session_id:
                continue
            self._pending_client_requests.pop(request_id, None)
            if not pending.future.done():
                pending.future.set_exception(ReversePermissionUnavailable(reason))

    async def _send_result(self, request_id: _ResponseId, result: Any) -> None:
        if isinstance(result, BaseModel):
            result = _model_payload(result)
        await self._write_message(
            {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}
        )

    async def _send_error(
        self,
        request_id: _ResponseId,
        code: int,
        message: str,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        error: dict[str, Any] = {"code": int(code), "message": message}
        if data is not None:
            error["data"] = dict(data)
        await self._write_message(
            {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": error},
            enforce_limit=False,
        )

    async def _send_notification(self, method: str, params: BaseModel | Any) -> None:
        if isinstance(params, BaseModel):
            params = _model_payload(params)
        await self._write_message(
            {"jsonrpc": JSONRPC_VERSION, "method": method, "params": params}
        )

    async def _write_message(
        self,
        message: Mapping[str, Any],
        *,
        enforce_limit: bool = True,
    ) -> None:
        if self._writer is None:
            raise RuntimeError("ACP writer is not connected")
        encoded = (
            json.dumps(
                dict(message),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        if enforce_limit and len(encoded) > self.limits.max_message_bytes:
            raise _OutboundMessageTooLarge
        async with self._write_lock:
            self._writer.write(encoded)
            await self._writer.drain()

    async def _shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._fail_pending_client_requests(reason="ACP connection closed")

        active_sessions = tuple(self._active_prompts)
        if active_sessions:
            self._cancelled_sessions.update(active_sessions)
            cancel_calls = [self.bridge.cancel(session_id) for session_id in active_sessions]
            try:
                await asyncio.wait_for(
                    asyncio.gather(*cancel_calls, return_exceptions=True),
                    timeout=self.limits.shutdown_timeout_seconds,
                )
            except TimeoutError:
                logger.warning("Timed out cancelling ACP prompts during EOF cleanup")

        current = asyncio.current_task()
        tasks = [task for task in self._request_tasks if task is not current]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        try:
            await asyncio.wait_for(
                self.bridge.disconnect(tuple(sorted(self._sessions))),
                timeout=self.limits.shutdown_timeout_seconds,
            )
        except TimeoutError:
            logger.warning("Timed out detaching ACP connection observers")
        except Exception:
            logger.exception("ACP bridge disconnect cleanup failed")


__all__ = [
    "ACP_PROTOCOL_VERSION",
    "AUTH_REQUIRED",
    "AcpFeatureDisabled",
    "AcpLimits",
    "AcpServer",
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "JSONRPC_VERSION",
    "METHOD_NOT_FOUND",
    "PARSE_ERROR",
    "RESOURCE_NOT_FOUND",
    "SERVER_BUSY",
    "SESSION_LIMIT_REACHED",
    "acp_runtime_enabled",
]
