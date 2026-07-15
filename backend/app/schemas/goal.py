"""Public request and response contracts for persistent session Goals."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator


GoalStatus = Literal[
    "active",
    "paused",
    "blocked",
    "usage_limited",
    "budget_limited",
    "complete",
]
GoalRunState = Literal[
    "idle",
    "reserved",
    "running",
    "pausing",
    "waiting_user",
    "interrupted",
]
GoalRunTrigger = Literal["initial", "auto", "resume", "user_input"]
GoalRunStatus = Literal[
    "reserved",
    "running",
    "waiting_user",
    "completed",
    "blocked",
    "interrupted",
    "failed",
]


class _GoalContentValidation(BaseModel):
    objective: str | None = None
    definition_of_done: str | None = None

    @field_validator("objective")
    @classmethod
    def normalize_objective(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("objective must not be empty")
        return normalized

    @field_validator("definition_of_done")
    @classmethod
    def normalize_definition(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @model_validator(mode="after")
    def validate_content_length(self) -> Self:
        if len(self.objective or "") + len(self.definition_of_done or "") > 4000:
            raise ValueError(
                "objective and definition_of_done must total at most 4000 characters"
            )
        return self


class GoalCreateRequest(_GoalContentValidation):
    client_request_id: str = Field(..., min_length=8, max_length=128)
    objective: str
    token_budget: int | None = Field(default=None, ge=0)
    cost_budget_microusd: int | None = Field(default=None, ge=0)
    time_budget_seconds: int | None = Field(default=None, ge=0)
    max_continuations: int | None = Field(default=None, ge=0)
    model_id: str | None = Field(default=None, max_length=255)
    provider_id: str | None = Field(default=None, max_length=255)
    agent: str = Field(default="build", min_length=1, max_length=80)
    reasoning: bool | None = None
    language: Literal["zh", "en"] = "zh"


class GoalUpdateRequest(_GoalContentValidation):
    client_request_id: str = Field(..., min_length=8, max_length=128)
    expected_revision: int = Field(..., ge=1)
    token_budget: int | None = Field(default=None, ge=0)
    cost_budget_microusd: int | None = Field(default=None, ge=0)
    time_budget_seconds: int | None = Field(default=None, ge=0)
    max_continuations: int | None = Field(default=None, ge=0)
    model_id: str | None = Field(default=None, max_length=255)
    provider_id: str | None = Field(default=None, max_length=255)
    agent: str | None = Field(default=None, min_length=1, max_length=80)
    reasoning: bool | None = None
    language: Literal["zh", "en"] | None = None

    @model_validator(mode="after")
    def require_update_field(self) -> Self:
        editable = self.model_fields_set - {"client_request_id", "expected_revision"}
        if not editable:
            raise ValueError("at least one editable field is required")
        return self


class GoalControlRequest(BaseModel):
    client_request_id: str = Field(..., min_length=8, max_length=128)
    expected_revision: int = Field(..., ge=1)


class GoalChatRequest(_GoalContentValidation):
    """Atomically create a session Goal and its first autonomous run."""

    client_request_id: str = Field(..., min_length=8, max_length=128)
    session_id: str | None = None
    objective: str
    definition_of_done: str | None = None
    token_budget: int | None = Field(default=None, ge=0)
    cost_budget_microusd: int | None = Field(default=None, ge=0)
    time_budget_seconds: int | None = Field(default=None, ge=0)
    max_continuations: int | None = Field(default=None, ge=0)
    model: str | None = Field(default=None, max_length=255)
    provider_id: str | None = Field(default=None, max_length=255)
    agent: str = Field(default="build", min_length=1, max_length=80)
    reasoning: bool | None = None
    workspace: str | None = None
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    permission_presets: dict[str, bool] | None = None
    permission_rules: list[dict[str, Any]] | None = None
    language: Literal["zh", "en"] = Field(default="zh", exclude=True)


class GoalResponse(BaseModel):
    id: str
    session_id: str
    objective: str
    definition_of_done: str | None = None
    status: GoalStatus
    run_state: GoalRunState
    revision: int

    token_budget: int | None = None
    tokens_used: int
    cost_budget_microusd: int | None = None
    cost_used_microusd: int
    time_budget_seconds: int | None = None
    time_used_seconds: int
    max_continuations: int | None = None
    continuation_count: int
    no_progress_count: int
    blocker_streak: int
    consecutive_error_count: int

    blocker_code: str | None = None
    blocker_message: str | None = None
    needs_review: bool
    next_retry_at: datetime | None = None
    completion_summary: str | None = None
    completion_evidence: dict[str, Any] | list[Any] | None = None

    model_id: str | None = None
    provider_id: str | None = None
    agent: str
    reasoning: bool | None = None
    language: str
    last_run_id: str | None = None
    last_stream_id: str | None = None
    time_started: datetime | None = None
    time_completed: datetime | None = None
    time_created: datetime
    time_updated: datetime

    model_config = {"from_attributes": True, "protected_namespaces": ()}


class GoalTokenUsageResponse(BaseModel):
    """Canonical Goal token total with auditable Provider components."""

    input: int = Field(default=0, ge=0)
    output: int = Field(default=0, ge=0)
    reasoning: int = Field(default=0, ge=0)
    cache_read: int = Field(default=0, ge=0)
    unattributed: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    source_count: int = Field(default=0, ge=0)


class GoalRunResponse(BaseModel):
    id: str
    goal_id: str
    ordinal: int
    goal_revision: int
    idempotency_key: str
    stream_id: str | None = None
    trigger: GoalRunTrigger
    status: GoalRunStatus
    tokens_used: int
    cost_used_microusd: int
    active_seconds: int
    progress_summary: str | None = None
    stop_reason: str | None = None
    error_code: str | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    side_effects_started: bool
    time_started: datetime | None = None
    time_finished: datetime | None = None
    time_created: datetime
    time_updated: datetime

    model_config = {"from_attributes": True}


class GoalStartResponse(BaseModel):
    stream_id: str
    session_id: str
    goal: GoalResponse
    run: GoalRunResponse
