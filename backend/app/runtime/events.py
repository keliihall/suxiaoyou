"""Stable, transport-neutral Agent runtime lifecycle events.

The desktop SSE stream predates ACP and lifecycle hooks.  Its event names and
payloads are a UI transport contract, so consumers outside the frontend must
not bind to them directly.  This module is the versioned boundary used by new
runtime integrations while the existing SSE protocol remains backwards
compatible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


LIFECYCLE_SCHEMA_VERSION = 1
REDACTED = "[REDACTED]"

_SENSITIVE_KEY_PARTS = (
    "access_token",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "password",
    "passwd",
    "private_key",
    "refresh_token",
    "secret",
    "session_token",
)

_TRANSPORT_EVENT_TYPES = {
    "text-delta": "assistant.text.delta",
    "reasoning-delta": "assistant.reasoning.delta",
    "tool-call": "tool.started",
    "tool-result": "tool.completed",
    "tool-error": "tool.failed",
    "step-start": "step.started",
    "step-finish": "step.completed",
    "compaction-start": "compaction.started",
    "compaction-phase": "compaction.progress",
    "compaction-progress": "compaction.progress",
    "compacted": "compaction.completed",
    "compaction-error": "compaction.failed",
    "permission-request": "permission.requested",
    "permission-resolved": "permission.resolved",
    "question": "interaction.question.requested",
    "question-resolved": "interaction.question.resolved",
    "plan-review": "plan.review.requested",
    "plan-review-resolved": "plan.review.resolved",
    "task-batch-start": "subagents.batch.started",
    "task-batch-update": "subagents.batch.progress",
    "task-batch-finish": "subagents.batch.completed",
    "subtask_start": "subagent.started",
    "subtask_stop": "subagent.completed",
    "input-queued": "input.queued",
    "input-applied": "input.applied",
    "input-started": "input.started",
    "input-failed": "input.failed",
    "goal-updated": "goal.updated",
    "goal-cleared": "goal.cleared",
    "goal-run-started": "goal.run.started",
    "goal-run-finished": "goal.run.completed",
    "goal-budget-warning": "goal.budget.warning",
    "goal-needs-user": "goal.needs_user",
    "retry": "provider.retry",
    "model-loading": "provider.model.loading",
    "title-update": "session.title.updated",
    "done": "turn.completed",
    "agent-error": "turn.failed",
    "desync": "runtime.desync",
}


def _normalized_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).casefold()).strip("_")


def _is_sensitive_key(value: object) -> bool:
    key = _normalized_key(value)
    return (
        key == "token"
        or key.endswith("_token")
        or any(part in key for part in _SENSITIVE_KEY_PARTS)
    )


def _redact_url(value: str) -> str:
    """Remove URL credentials and sensitive query values without hiding the host."""

    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if not parsed.scheme or not parsed.netloc:
        return value

    hostname = parsed.hostname or ""
    if parsed.port is not None:
        hostname = f"{hostname}:{parsed.port}"
    query = urlencode(
        [
            (key, REDACTED if _is_sensitive_key(key) else item_value)
            for key, item_value in parse_qsl(parsed.query, keep_blank_values=True)
        ]
    )
    return urlunsplit((parsed.scheme, hostname, parsed.path, query, parsed.fragment))


def _safe_value(value: Any, *, key: object | None = None, depth: int = 0) -> Any:
    if key is not None and _is_sensitive_key(key):
        return REDACTED
    if depth > 10:
        return "[TRUNCATED_DEPTH]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if key is not None and any(
            token in _normalized_key(key) for token in ("url", "uri", "endpoint")
        ):
            value = _redact_url(value)
        return value if len(value) <= 65_536 else value[:65_536] + "…[TRUNCATED]"
    if isinstance(value, bytes):
        return {"kind": "bytes", "length": len(value)}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {
            str(item_key): _safe_value(item_value, key=item_key, depth=depth + 1)
            for item_key, item_value in list(value.items())[:1_000]
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe_value(item, depth=depth + 1) for item in list(value)[:1_000]]
    return str(value)


def sanitize_lifecycle_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a bounded JSON-compatible payload with common secrets removed."""

    return {
        str(key): _safe_value(value, key=key)
        for key, value in list(payload.items())[:1_000]
    }


def _transport_event_type(event_name: str) -> str:
    known = _TRANSPORT_EVENT_TYPES.get(event_name)
    if known is not None:
        return known
    normalized = re.sub(r"[^a-z0-9]+", ".", event_name.casefold()).strip(".")
    return f"transport.{normalized or 'unknown'}"


@dataclass(frozen=True, slots=True)
class LifecycleEventV1:
    """One ordered, versioned runtime event suitable for ACP and hooks."""

    sequence: int
    event_type: str
    session_id: str
    stream_id: str
    invocation_source: str
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: str | None = None
    root_turn_id: str | None = None
    turn_run_id: str | None = None
    workspace_instance_id: str | None = None
    message_id: str | None = None
    call_id: str | None = None
    checkpoint_id: str | None = None
    occurred_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    event_version: int = LIFECYCLE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise ValueError("Lifecycle event sequence must be positive")
        if not self.session_id or not self.stream_id:
            raise ValueError("Lifecycle events require session_id and stream_id")
        if not re.fullmatch(r"[a-z][a-z0-9_.-]*", self.event_type):
            raise ValueError(f"Invalid lifecycle event type: {self.event_type!r}")
        if self.event_version != LIFECYCLE_SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported lifecycle event version: {self.event_version}"
            )
        if self.event_id is None:
            object.__setattr__(self, "event_id", f"{self.stream_id}:{self.sequence}")
        elif not str(self.event_id).strip():
            raise ValueError("Lifecycle event_id cannot be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_version": self.event_version,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at,
            "session_id": self.session_id,
            "stream_id": self.stream_id,
            "root_turn_id": self.root_turn_id,
            "turn_run_id": self.turn_run_id,
            "workspace_instance_id": self.workspace_instance_id,
            "message_id": self.message_id,
            "call_id": self.call_id,
            "checkpoint_id": self.checkpoint_id,
            "invocation_source": self.invocation_source,
            "payload": self.payload,
        }


def lifecycle_event_from_transport(
    *,
    sequence: int,
    transport_event: str,
    data: Mapping[str, Any],
    session_id: str,
    stream_id: str,
    invocation_source: str,
    root_turn_id: str | None = None,
    turn_run_id: str | None = None,
    workspace_instance_id: str | None = None,
) -> LifecycleEventV1:
    """Translate a legacy SSE event without exposing its transport contract."""

    call_id = data.get("call_id") or data.get("tool_call_id")
    message_id = data.get("message_id")
    checkpoint_id = data.get("checkpoint_id")
    return LifecycleEventV1(
        sequence=sequence,
        event_type=_transport_event_type(transport_event),
        session_id=session_id,
        stream_id=stream_id,
        root_turn_id=root_turn_id,
        turn_run_id=turn_run_id,
        workspace_instance_id=workspace_instance_id,
        message_id=str(message_id) if message_id is not None else None,
        call_id=str(call_id) if call_id is not None else None,
        checkpoint_id=str(checkpoint_id) if checkpoint_id is not None else None,
        invocation_source=invocation_source,
        payload=sanitize_lifecycle_payload(data),
    )
