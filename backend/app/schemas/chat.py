"""Chat request/response schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, PrivateAttr

from app.schemas.agent import Ruleset


class PromptRequest(BaseModel):
    """Start a new generation."""

    client_request_id: str | None = Field(None, min_length=8, max_length=128)
    session_id: str | None = None
    text: str
    model: str | None = None  # e.g. "claude-sonnet-4-20250514"
    provider_id: str | None = None  # e.g. "anthropic" — which provider to use for the model
    agent: str = "build"
    attachments: list[dict[str, Any]] = []
    permission_presets: dict[str, bool] | None = None
    permission_rules: list[dict[str, Any]] | None = None
    # Internal-only: child agents receive the parent's effective rules as a
    # ceiling. PrivateAttr prevents external JSON from opting into this merge
    # mode and bypassing a persisted session denial.
    _permission_rules_authoritative: bool = PrivateAttr(default=False)
    # Goal continuations carry a server-created compound ceiling here.  A
    # plain list cannot faithfully encode the intersection of two ordered
    # glob policies, and PrivateAttr prevents external request JSON from
    # supplying a forged ceiling.
    _trusted_permission_ruleset: Ruleset | None = PrivateAttr(default=None)
    _enforce_current_permission_ceiling: bool = PrivateAttr(default=False)
    _goal_permission_baseline: tuple[Ruleset, Ruleset] | None = PrivateAttr(
        default=None
    )
    # Server-created specialist runtimes (currently the validation Agent) can
    # impose a smaller Provider output ceiling than the interactive UI's
    # normal minimum. PrivateAttr keeps request JSON from changing spend.
    _max_output_tokens_ceiling: int | None = PrivateAttr(default=None)
    # ACP supplies a UUID that is acknowledged only after SessionPrompt stores
    # it on the real user Message. PrivateAttr prevents HTTP clients from
    # forging this protocol-owned binding.
    _external_user_message_id: str | None = PrivateAttr(default=None)
    reasoning: bool | None = None  # Explicitly enable/disable reasoning
    workspace: str | None = None  # Workspace directory restriction
    format: dict[str, Any] | None = None  # e.g. {"type": "json_schema", "json_schema": {...}}
    # Request-scoped backend display language.  API handlers derive this from
    # Accept-Language; exclude it from wire serialization/idempotency hashes.
    language: Literal["zh", "en"] = Field("zh", exclude=True)

    @property
    def external_user_message_id(self) -> str | None:
        return self._external_user_message_id


class PromptResponse(BaseModel):
    """Response after starting generation."""

    stream_id: str
    session_id: str


class TaskBatchTask(BaseModel):
    """One explicit child-agent task in a multi-agent batch."""

    title: str = Field(..., min_length=1, max_length=120)
    prompt: str = Field(..., min_length=1)
    agent: str = "explore"
    model: str | None = None
    provider_id: str | None = None


class TaskBatchRequest(BaseModel):
    """Start a sequential or parallel multi-agent task batch."""

    session_id: str | None = None
    mode: Literal["sequential", "parallel"] = "parallel"
    tasks: list[TaskBatchTask] = Field(..., min_length=1, max_length=12)
    workspace: str | None = None
    permission_presets: dict[str, bool] | None = None
    permission_rules: list[dict[str, Any]] | None = None
    language: Literal["zh", "en"] = Field("zh", exclude=True)


class CompactRequest(BaseModel):
    """Start a manual compaction stream for an existing session."""

    session_id: str
    model_id: str | None = None


class EditAndResendRequest(BaseModel):
    """Edit a user message and re-generate from that point."""

    session_id: str
    message_id: str  # The user message to edit
    text: str  # New text content
    model: str | None = None
    provider_id: str | None = None
    agent: str = "build"
    attachments: list[dict[str, Any]] = []
    permission_presets: dict[str, bool] | None = None
    permission_rules: list[dict[str, Any]] | None = None
    reasoning: bool | None = None
    workspace: str | None = None  # Workspace directory restriction
    format: dict[str, Any] | None = None  # e.g. {"type": "json_schema", "json_schema": {...}}
    language: Literal["zh", "en"] = Field("zh", exclude=True)


class AbortRequest(BaseModel):
    """Abort an active generation."""

    stream_id: str


class RespondRequest(BaseModel):
    """User responds to a question tool or permission request."""

    stream_id: str
    call_id: str
    response: Any  # depends on context — string for question, bool for permission


class SessionInputRequest(BaseModel):
    """Queue a follow-up or steer input while a session is running."""

    session_id: str
    client_request_id: str = Field(..., min_length=8, max_length=128)
    mode: Literal["queue", "steer"] = "queue"
    text: str = ""
    attachments: list[dict[str, Any]] = []
    model: str | None = None
    provider_id: str | None = None
    agent: str = "build"
    workspace: str | None = None
    reasoning: bool | None = None
    permission_presets: dict[str, bool] | None = None
    permission_rules: list[dict[str, Any]] | None = None


class SessionInputUpdateRequest(BaseModel):
    """Adjust a queued follow-up before execution begins."""

    mode: Literal["queue", "steer"] | None = None
    move: Literal["up", "down"] | None = None
    position: int | None = Field(None, ge=1)


class SessionInputResponse(BaseModel):
    id: str
    session_id: str
    client_request_id: str
    mode: str
    status: str
    position: int
    text: str
    attachments: list[dict[str, Any]]
    target_stream_id: str | None = None
    error_message: str | None = None
