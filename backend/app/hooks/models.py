"""Versioned, authority-neutral contracts for v1.1 Hooks.

Hook messages deliberately contain no mutation surface.  In particular,
``PreToolUse`` responses can express only a restrictive decision and an
annotation; tool names, arguments, workspaces, sources, and permissions are
not fields in the response schema and unknown fields are rejected.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


HOOK_PROTOCOL_VERSION = 1
DEFAULT_HOOK_TIMEOUT_SECONDS = 5.0
MAX_HOOK_TIMEOUT_SECONDS = 30.0
MAX_HOOK_EVENT_BYTES = 256 * 1024
MAX_HOOK_ANNOTATION_CHARS = 4_096
MAX_HOOK_RESPONSE_BYTES = 1024 * 1024
MAX_HOOK_COMMAND_ARGS = 64
MAX_HOOK_COMMAND_ARG_CHARS = 4_096
MAX_HOOK_ENV_ENTRIES = 16
MAX_HOOK_ENV_VALUE_CHARS = 4_096

_HOOK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,159}$")


class HookProtocolError(ValueError):
    """Raised when a Hook event or response violates the v1 wire contract."""


class HookEventName(StrEnum):
    SESSION_START = "SessionStart"
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    STOP = "Stop"
    SUBAGENT_START = "SubagentStart"
    SUBAGENT_STOP = "SubagentStop"
    PRE_COMPACT = "PreCompact"
    POST_COMPACT = "PostCompact"


class HookDecisionKind(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"
    CONTINUE = "continue"
    ERROR = "error"


class HookFailurePolicy(StrEnum):
    REQUIRED = "required"
    OPTIONAL = "optional"


class HookSource(StrEnum):
    BUILTIN = "builtin"
    PROJECT = "project"
    PLUGIN = "plugin"


class HookEvent(BaseModel):
    """One immutable runtime-event projection delivered to a Hook."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[HOOK_PROTOCOL_VERSION] = HOOK_PROTOCOL_VERSION
    event_id: str = Field(min_length=1, max_length=160)
    event: HookEventName
    sequence: int = Field(ge=0)
    occurred_at: datetime
    session_id: str | None = Field(default=None, max_length=160)
    stream_id: str | None = Field(default=None, max_length=160)
    root_turn_id: str | None = Field(default=None, max_length=160)
    turn_run_id: str | None = Field(default=None, max_length=160)
    message_id: str | None = Field(default=None, max_length=160)
    call_id: str | None = Field(default=None, max_length=160)
    checkpoint_id: str | None = Field(default=None, max_length=160)
    workspace_instance_id: str | None = Field(default=None, max_length=160)
    invocation_source: str | None = Field(default=None, max_length=160)
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Hook occurred_at must include a timezone")
        return value

    @model_validator(mode="after")
    def _require_bounded_json(self) -> "HookEvent":
        try:
            # Validate the raw payload before Pydantic's JSON-mode serializer;
            # it would otherwise normalise non-finite floats to null and change
            # the signed event projection silently.
            json.dumps(
                self.payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            encoded = self.to_wire_bytes()
        except (TypeError, ValueError) as exc:
            raise ValueError("Hook event payload must be JSON serializable") from exc
        if len(encoded) > MAX_HOOK_EVENT_BYTES:
            raise ValueError("Hook event exceeds the maximum encoded size")
        return self

    def to_wire_bytes(self) -> bytes:
        return (
            json.dumps(
                self.model_dump(mode="json"),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")


class HookDecision(BaseModel):
    """Strict response schema shared by built-in and command Hooks."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[HOOK_PROTOCOL_VERSION] = HOOK_PROTOCOL_VERSION
    decision: HookDecisionKind
    annotation: str | None = Field(
        default=None,
        max_length=MAX_HOOK_ANNOTATION_CHARS,
    )

    def validate_for_event(self, event: HookEventName) -> "HookDecision":
        if event is HookEventName.PRE_TOOL_USE:
            allowed = {
                HookDecisionKind.ALLOW,
                HookDecisionKind.DENY,
                HookDecisionKind.ASK,
            }
        else:
            allowed = {HookDecisionKind.CONTINUE, HookDecisionKind.ERROR}
        if self.decision not in allowed:
            raise HookProtocolError(
                f"Decision {self.decision.value!r} is not valid for {event.value}"
            )
        return self

    @classmethod
    def from_wire_bytes(
        cls,
        data: bytes,
        *,
        event: HookEventName,
    ) -> "HookDecision":
        if not isinstance(data, bytes) or len(data) > MAX_HOOK_RESPONSE_BYTES:
            raise HookProtocolError("Hook response exceeds the v1 JSON boundary")

        def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise HookProtocolError(
                        f"Hook response contains duplicate field {key!r}"
                    )
                result[key] = value
            return result

        try:
            payload = json.loads(data, object_pairs_hook=reject_duplicate_keys)
            decision = cls.model_validate(payload)
        except HookProtocolError:
            raise
        except Exception as exc:
            raise HookProtocolError("Hook response is not valid v1 JSON") from exc
        return decision.validate_for_event(event)


class _HookDeclarationBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    hook_id: str
    event: HookEventName
    # No default: every registration must state its failure policy explicitly.
    failure_policy: HookFailurePolicy
    timeout_seconds: float = Field(
        default=DEFAULT_HOOK_TIMEOUT_SECONDS,
        gt=0,
        le=MAX_HOOK_TIMEOUT_SECONDS,
    )

    @field_validator("hook_id")
    @classmethod
    def _validate_hook_id(cls, value: str) -> str:
        if _HOOK_ID_RE.fullmatch(value) is None:
            raise ValueError("Invalid Hook id")
        return value


class BuiltinHookDeclaration(_HookDeclarationBase):
    """Registration metadata for trusted application code."""


class HookCommandDeclaration(_HookDeclarationBase):
    """Unresolved project/plugin local-command declaration."""

    command: tuple[str, ...]
    environment: dict[str, str] = Field(default_factory=dict)

    @field_validator("command")
    @classmethod
    def _validate_command(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(value) > MAX_HOOK_COMMAND_ARGS:
            raise ValueError("Hook command must contain 1-64 arguments")
        if any(
            not isinstance(item, str)
            or not item
            or "\x00" in item
            or len(item) > MAX_HOOK_COMMAND_ARG_CHARS
            for item in value
        ):
            raise ValueError("Hook command contains an invalid argument")
        return value

    @field_validator("environment")
    @classmethod
    def _validate_environment(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > MAX_HOOK_ENV_ENTRIES or any(
            not isinstance(key, str)
            or not key
            or len(key) > 64
            or "=" in key
            or "\x00" in key
            or not isinstance(item, str)
            or len(item) > MAX_HOOK_ENV_VALUE_CHARS
            or "\x00" in item
            for key, item in value.items()
        ):
            raise ValueError("Hook environment must map valid names to strings")
        return value


def combine_pre_tool_decisions(
    current: HookDecisionKind | str,
    candidate: HookDecisionKind | str,
) -> HookDecisionKind:
    """Return the least-authoritative combination (deny > ask > allow).

    This helper is also the integration contract with the ordinary permission
    engine: an ``allow`` from a Hook can never widen an existing ``ask`` or
    ``deny`` decision.
    """

    left = HookDecisionKind(current)
    right = HookDecisionKind(candidate)
    allowed = {
        HookDecisionKind.ALLOW,
        HookDecisionKind.ASK,
        HookDecisionKind.DENY,
    }
    if left not in allowed or right not in allowed:
        raise ValueError("Only PreToolUse decisions can be combined")
    rank = {
        HookDecisionKind.ALLOW: 0,
        HookDecisionKind.ASK: 1,
        HookDecisionKind.DENY: 2,
    }
    return left if rank[left] >= rank[right] else right


__all__ = [
    "BuiltinHookDeclaration",
    "DEFAULT_HOOK_TIMEOUT_SECONDS",
    "HOOK_PROTOCOL_VERSION",
    "HookCommandDeclaration",
    "HookDecision",
    "HookDecisionKind",
    "HookEvent",
    "HookEventName",
    "HookFailurePolicy",
    "HookProtocolError",
    "HookSource",
    "MAX_HOOK_EVENT_BYTES",
    "MAX_HOOK_RESPONSE_BYTES",
    "MAX_HOOK_TIMEOUT_SECONDS",
    "combine_pre_tool_decisions",
]
