"""Authority-preserving bridge contract for the ACP stdio boundary.

The ACP server deliberately has no dependency on providers, tools, workspace
transactions, or the database.  A production implementation of this bridge
must submit prompts through :class:`app.session.prompt.SessionPrompt` (or the
same reviewed admission path) so desktop permissions, Hooks, checkpoints, and
the mutation ledger remain authoritative.  It must never execute a model or a
tool directly from ACP input. ACP reverse permission requests are an answer
transport only: the application's normal permission ceiling remains
authoritative and the bridge must fail closed whenever that transport is not
available. Likewise, advertised MCP transports must still pass through the
existing local-launch trust and connector permission boundaries.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeAlias

from acp.schema import (
    AuthenticateRequest,
    AuthenticateResponse,
    InitializeRequest,
    LoadSessionRequest,
    LoadSessionResponse,
    NewSessionRequest,
    NewSessionResponse,
    PromptRequest,
    PromptResponse,
    RequestPermissionRequest,
    RequestPermissionResponse,
)
from pydantic import BaseModel


ACP_METHOD_NOT_FOUND = -32601
ACP_AUTH_REQUIRED = -32000
ACP_RESOURCE_NOT_FOUND = -32002

UpdatePayload: TypeAlias = BaseModel | Mapping[str, Any]
UpdateEmitter: TypeAlias = Callable[[UpdatePayload], Awaitable[None]]
PermissionRequester: TypeAlias = Callable[
    [RequestPermissionRequest], Awaitable[RequestPermissionResponse]
]


@dataclass(frozen=True, slots=True)
class BridgeCapabilities:
    """Capabilities backed by the injected, permission-aware session bridge.

    Every field defaults to unsupported. Text prompts and cancellation are ACP
    baseline operations and therefore do not need capability flags.
    """

    load_session: bool = False
    image_prompts: bool = False
    audio_prompts: bool = False
    embedded_context: bool = False
    additional_directories: bool = False
    mcp_stdio: bool = False
    mcp_http: bool = False
    mcp_sse: bool = False
    auth_methods: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)


class BridgeRpcError(Exception):
    """An intentional ACP/JSON-RPC error returned by a bridge adapter."""

    def __init__(
        self,
        code: int,
        message: str,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = int(code)
        self.data = dict(data) if data is not None else None

    @classmethod
    def method_not_found(cls, method: str) -> "BridgeRpcError":
        return cls(ACP_METHOD_NOT_FOUND, "Method not found", {"method": method})

    @classmethod
    def auth_required(cls) -> "BridgeRpcError":
        return cls(ACP_AUTH_REQUIRED, "Authentication required")

    @classmethod
    def session_not_found(cls, session_id: str) -> "BridgeRpcError":
        return cls(
            ACP_RESOURCE_NOT_FOUND,
            "Resource not found",
            {"uri": f"session:{session_id}"},
        )


class ReversePermissionUnavailable(RuntimeError):
    """The ACP client could not produce a valid permission decision.

    Details intentionally never cross into the model/tool runtime. Callers
    must translate this failure to an exact denial of the pending application
    response request.
    """


class SessionPromptBridge(ABC):
    """Injectable adapter from ACP requests to SuXiaoYou's normal session path.

    The server validates official ACP models before invoking these methods and
    validates every returned response/update again before writing it to the
    wire. Bridge implementations own application lookup and lifecycle, but do
    not own ACP framing.
    """

    capabilities = BridgeCapabilities()

    def bind_permission_requester(
        self,
        requester: PermissionRequester | None,
    ) -> None:
        """Bind the connection-owned reverse request transport, if supported.

        The default is deliberately inert so small protocol-test bridges do
        not gain interactive authority merely by being served over ACP.
        """

    async def initialize(self, request: InitializeRequest) -> None:
        """Observe negotiated client details without changing capabilities."""

    async def authenticate(
        self,
        request: AuthenticateRequest,
    ) -> AuthenticateResponse | Mapping[str, Any] | None:
        raise BridgeRpcError.method_not_found("authenticate")

    @abstractmethod
    async def new_session(
        self,
        request: NewSessionRequest,
    ) -> NewSessionResponse | Mapping[str, Any]:
        """Create a session through the application's normal session service."""

    async def load_session(
        self,
        request: LoadSessionRequest,
        emit_update: UpdateEmitter,
    ) -> LoadSessionResponse | Mapping[str, Any] | None:
        raise BridgeRpcError.method_not_found("session/load")

    @abstractmethod
    async def prompt(
        self,
        request: PromptRequest,
        emit_update: UpdateEmitter,
    ) -> PromptResponse | Mapping[str, Any]:
        """Run one turn through SessionPrompt and stream projected updates."""

    @abstractmethod
    async def cancel(self, session_id: str) -> None:
        """Cancel the normal SessionPrompt/GenerationJob for ``session_id``."""

    async def extension_request(self, method: str, params: Any) -> Any:
        raise BridgeRpcError.method_not_found(f"_{method}")

    async def extension_notification(self, method: str, params: Any) -> None:
        """Ignore unknown extension notifications, as required by JSON-RPC."""

    async def disconnect(self, session_ids: Sequence[str]) -> None:
        """Release connection-owned observers after EOF.

        Persistent application sessions are not deleted by disconnect.
        """


__all__ = [
    "ACP_AUTH_REQUIRED",
    "ACP_METHOD_NOT_FOUND",
    "ACP_RESOURCE_NOT_FOUND",
    "BridgeCapabilities",
    "BridgeRpcError",
    "PermissionRequester",
    "ReversePermissionUnavailable",
    "SessionPromptBridge",
    "UpdateEmitter",
    "UpdatePayload",
]
