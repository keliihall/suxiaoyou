"""GenerationJob lifecycle adapter for authority-neutral Hook dispatch.

The adapter publishes the observable runtime fact first, projects the returned
``LifecycleEventV1`` into a ``HookEvent``, and then dispatches it.  It never
places command stdout/stderr, Hook annotations, errors, paths, or approval
descriptors in lifecycle payloads; only a redacted categorical audit summary is
published after dispatch.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from app.hooks.dispatcher import (
    HookDispatchResult,
    HookDispatchState,
    HookDispatcher,
    HookExecutionRecord,
)
from app.hooks.models import (
    HookDecisionKind,
    HookEvent,
    HookEventName,
    combine_pre_tool_decisions,
)
from app.hooks.registry import CommandHook, refresh_command_hook
from app.runtime.events import LifecycleEventV1


HOOK_AUDIT_VERSION = 1
MAX_PENDING_HOOK_APPROVALS = 64
MAX_APPROVAL_DESCRIPTOR_BYTES = 64 * 1024

HOOK_LIFECYCLE_EVENT_TYPES: dict[HookEventName, str] = {
    HookEventName.SESSION_START: "session.started",
    HookEventName.USER_PROMPT_SUBMIT: "user_prompt.submitted",
    HookEventName.PRE_TOOL_USE: "tool.pre_use",
    HookEventName.POST_TOOL_USE: "tool.post_use",
    HookEventName.STOP: "turn.stopped",
    HookEventName.SUBAGENT_START: "subagent.started",
    HookEventName.SUBAGENT_STOP: "subagent.stopped",
    HookEventName.PRE_COMPACT: "compaction.pre",
    HookEventName.POST_COMPACT: "compaction.post",
}


class HookRuntimeError(RuntimeError):
    """Base error for the lifecycle-to-Hook integration boundary."""


class HookApprovalError(HookRuntimeError):
    """Base error for one-shot exact Hook command approval."""


class HookApprovalUnavailable(HookApprovalError):
    """Raised for an unknown, consumed, or evicted approval request."""


class HookApprovalMismatch(HookApprovalError):
    """Raised when the caller does not echo the exact descriptor/fingerprint."""


class HookApprovalStale(HookApprovalError):
    """Raised when command content or its resolved launch identity changed."""


class _LifecyclePublisher(Protocol):
    def publish_lifecycle(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        message_id: str | None = None,
        call_id: str | None = None,
        checkpoint_id: str | None = None,
    ) -> LifecycleEventV1: ...


@dataclass(frozen=True, slots=True)
class HookExecutionAudit:
    """Redacted identity/status projection of one dispatch execution record."""

    hook_ref: str
    source: str
    failure_policy: str
    status: str
    fingerprint: str | None
    decision: str | None

    def to_payload(self) -> dict[str, Any]:
        return {
            "hook_ref": self.hook_ref,
            "source": self.source,
            "failure_policy": self.failure_policy,
            "status": self.status,
            "fingerprint": self.fingerprint,
            "decision": self.decision,
        }


@dataclass(frozen=True, slots=True)
class HookDispatchAuditSummary:
    """Safe lifecycle payload for a completed Hook dispatcher invocation."""

    event_id: str
    event: HookEventName
    state: HookDispatchState
    pre_tool_decision: HookDecisionKind | None
    annotation_count: int
    warning_count: int
    approval_required_count: int
    executions: tuple[HookExecutionAudit, ...]
    version: int = HOOK_AUDIT_VERSION

    def to_payload(self) -> dict[str, Any]:
        # Keep this schema deliberately explicit.  In particular, do not use
        # ``asdict`` on HookExecutionRecord: it contains logs/error/annotation.
        return {
            "version": self.version,
            "event_id": self.event_id,
            "event": self.event.value,
            "state": self.state.value,
            "pre_tool_decision": (
                self.pre_tool_decision.value
                if self.pre_tool_decision is not None
                else None
            ),
            "annotation_count": self.annotation_count,
            "warning_count": self.warning_count,
            "approval_required_count": self.approval_required_count,
            "executions": [item.to_payload() for item in self.executions],
        }


@dataclass(frozen=True, slots=True)
class HookApprovalRequest:
    """Displayable one-shot request; the descriptor is returned as a copy."""

    request_id: str
    event_id: str
    fingerprint: str
    _descriptor_json: str

    @property
    def descriptor(self) -> dict[str, Any]:
        value = json.loads(self._descriptor_json)
        if not isinstance(value, dict):  # pragma: no cover - construction invariant
            raise HookRuntimeError("Stored Hook approval descriptor is invalid")
        return value


@dataclass(frozen=True, slots=True)
class HookRuntimeResult:
    """Sanitized result returned to a future session-loop integration."""

    hook_event: HookEvent
    state: HookDispatchState
    pre_tool_decision: HookDecisionKind | None
    annotations: tuple[str, ...]
    warning_count: int
    audit: HookDispatchAuditSummary
    audit_event_id: str
    approval_required: HookApprovalRequest | None


@dataclass(frozen=True, slots=True)
class _PendingApproval:
    event: HookEvent
    prior_permission: HookDecisionKind | None
    descriptor_json: str
    fingerprint: str
    source: str
    source_name: str
    hook_id: str


def _canonical_descriptor(value: Mapping[str, Any]) -> str:
    if not isinstance(value, Mapping):
        raise HookApprovalMismatch("Hook approval descriptor must be an object")
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise HookApprovalMismatch(
            "Hook approval descriptor must be bounded JSON"
        ) from exc
    if len(encoded.encode("utf-8")) > MAX_APPROVAL_DESCRIPTOR_BYTES:
        raise HookApprovalMismatch("Hook approval descriptor is too large")
    return encoded


def _hook_ref(record: HookExecutionRecord) -> str:
    identity = json.dumps(
        [record.source, record.source_name, record.hook_id],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(identity).hexdigest()}"


def _audit_fingerprint(value: str | None) -> str | None:
    if value is None or value.startswith("sha256:"):
        return value
    # Built-in fingerprints contain their code-owned Hook id rather than a
    # content SHA. Hash them before they cross the lifecycle boundary.
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise HookRuntimeError("Lifecycle event has an invalid occurred_at") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HookRuntimeError("Lifecycle event occurred_at must be timezone-aware")
    return parsed


def hook_event_from_lifecycle(
    lifecycle: LifecycleEventV1,
    event: HookEventName | str,
) -> HookEvent:
    """Create the immutable Hook projection of a published lifecycle fact."""

    if not isinstance(lifecycle, LifecycleEventV1) or lifecycle.event_version != 1:
        raise HookRuntimeError("Unsupported lifecycle event projection")
    event_name = HookEventName(event)
    expected_type = HOOK_LIFECYCLE_EVENT_TYPES[event_name]
    if lifecycle.event_type != expected_type:
        raise HookRuntimeError(
            f"Lifecycle event type {lifecycle.event_type!r} does not match "
            f"{event_name.value}"
        )
    return HookEvent(
        event_id=str(lifecycle.event_id),
        event=event_name,
        sequence=lifecycle.sequence,
        occurred_at=_parse_timestamp(lifecycle.occurred_at),
        session_id=lifecycle.session_id,
        stream_id=lifecycle.stream_id,
        root_turn_id=lifecycle.root_turn_id,
        turn_run_id=lifecycle.turn_run_id,
        message_id=lifecycle.message_id,
        call_id=lifecycle.call_id,
        checkpoint_id=lifecycle.checkpoint_id,
        workspace_instance_id=lifecycle.workspace_instance_id,
        invocation_source=lifecycle.invocation_source,
        payload=lifecycle.payload,
    )


class HookRuntime:
    """Adapt ``GenerationJob.publish_lifecycle`` to the Hook dispatcher.

    ``approve_exact`` is the only command-trust mutation exposed here.  Each
    request id can be attempted once, and approval is persisted only after the
    caller echoes the exact descriptor/fingerprint and the current command
    identity is re-resolved to the same values.
    """

    def __init__(
        self,
        job: _LifecyclePublisher,
        dispatcher: HookDispatcher,
        *,
        max_pending_approvals: int = MAX_PENDING_HOOK_APPROVALS,
    ) -> None:
        if (
            isinstance(max_pending_approvals, bool)
            or not isinstance(max_pending_approvals, int)
            or not 0 < max_pending_approvals <= MAX_PENDING_HOOK_APPROVALS
        ):
            raise ValueError("Invalid pending Hook approval limit")
        self.job = job
        self.dispatcher = dispatcher
        self.max_pending_approvals = max_pending_approvals
        self._pending: OrderedDict[str, _PendingApproval] = OrderedDict()
        self._pending_lock = asyncio.Lock()

    async def emit(
        self,
        event: HookEventName | str,
        payload: Mapping[str, Any] | None = None,
        *,
        permission_decision: HookDecisionKind | str | None = None,
        message_id: str | None = None,
        call_id: str | None = None,
        checkpoint_id: str | None = None,
        should_abort: Callable[[], bool] | None = None,
    ) -> HookRuntimeResult:
        """Publish, project, dispatch, and audit one supported Hook event."""

        event_name = HookEventName(event)
        if payload is not None and not isinstance(payload, Mapping):
            raise TypeError("Hook runtime payload must be a mapping")
        projected_payload = dict(payload or {})
        prior_permission = self._permission_for_event(
            event_name,
            projected_payload,
            permission_decision,
        )
        lifecycle = self.job.publish_lifecycle(
            HOOK_LIFECYCLE_EVENT_TYPES[event_name],
            projected_payload,
            message_id=message_id,
            call_id=call_id,
            checkpoint_id=checkpoint_id,
        )
        hook_event = hook_event_from_lifecycle(lifecycle, event_name)
        dispatch = await self.dispatcher.dispatch(
            hook_event,
            should_abort=should_abort,
        )
        return await self._finish_dispatch(
            hook_event,
            dispatch,
            prior_permission=prior_permission,
        )

    async def dispatch_event(
        self,
        event: HookEventName | str,
        payload: Mapping[str, Any] | None = None,
        **options: Any,
    ) -> HookRuntimeResult:
        """Named alias for integrations that call the adapter a dispatcher."""

        return await self.emit(event, payload, **options)

    async def session_start(
        self,
        payload: Mapping[str, Any] | None = None,
        **options: Any,
    ) -> HookRuntimeResult:
        return await self.emit(HookEventName.SESSION_START, payload, **options)

    async def user_prompt_submit(
        self,
        payload: Mapping[str, Any] | None = None,
        **options: Any,
    ) -> HookRuntimeResult:
        return await self.emit(HookEventName.USER_PROMPT_SUBMIT, payload, **options)

    async def pre_tool_use(
        self,
        payload: Mapping[str, Any] | None = None,
        *,
        permission_decision: HookDecisionKind | str,
        **options: Any,
    ) -> HookRuntimeResult:
        return await self.emit(
            HookEventName.PRE_TOOL_USE,
            payload,
            permission_decision=permission_decision,
            **options,
        )

    async def post_tool_use(
        self,
        payload: Mapping[str, Any] | None = None,
        **options: Any,
    ) -> HookRuntimeResult:
        return await self.emit(HookEventName.POST_TOOL_USE, payload, **options)

    async def stop(
        self,
        payload: Mapping[str, Any] | None = None,
        **options: Any,
    ) -> HookRuntimeResult:
        return await self.emit(HookEventName.STOP, payload, **options)

    async def subagent_start(
        self,
        payload: Mapping[str, Any] | None = None,
        **options: Any,
    ) -> HookRuntimeResult:
        return await self.emit(HookEventName.SUBAGENT_START, payload, **options)

    async def subagent_stop(
        self,
        payload: Mapping[str, Any] | None = None,
        **options: Any,
    ) -> HookRuntimeResult:
        return await self.emit(HookEventName.SUBAGENT_STOP, payload, **options)

    async def pre_compact(
        self,
        payload: Mapping[str, Any] | None = None,
        **options: Any,
    ) -> HookRuntimeResult:
        return await self.emit(HookEventName.PRE_COMPACT, payload, **options)

    async def post_compact(
        self,
        payload: Mapping[str, Any] | None = None,
        **options: Any,
    ) -> HookRuntimeResult:
        return await self.emit(HookEventName.POST_COMPACT, payload, **options)

    async def approve_exact(
        self,
        request_id: str,
        *,
        descriptor: Mapping[str, Any],
        fingerprint: str,
        should_abort: Callable[[], bool] | None = None,
    ) -> HookRuntimeResult:
        """Consume one request, approve its unchanged identity, and redispatch."""

        if not isinstance(request_id, str) or not request_id:
            raise HookApprovalUnavailable("Hook approval request is unavailable")
        async with self._pending_lock:
            pending = self._pending.pop(request_id, None)
        if pending is None:
            raise HookApprovalUnavailable(
                "Hook approval request is unknown or has already been consumed"
            )

        supplied = _canonical_descriptor(descriptor)
        if (
            not isinstance(fingerprint, str)
            or not hmac.compare_digest(fingerprint, pending.fingerprint)
            or not hmac.compare_digest(supplied, pending.descriptor_json)
        ):
            raise HookApprovalMismatch(
                "Hook approval requires the exact displayed descriptor and fingerprint"
            )

        hook = self._registered_command(pending)
        try:
            current = refresh_command_hook(hook)
            current_descriptor = _canonical_descriptor(current.public_descriptor())
        except Exception as exc:
            raise HookApprovalStale(
                "Hook command identity could not be revalidated"
            ) from exc
        if (
            not hmac.compare_digest(current.fingerprint, pending.fingerprint)
            or not hmac.compare_digest(current_descriptor, pending.descriptor_json)
        ):
            raise HookApprovalStale(
                "Hook command content or resolved launch identity changed"
            )

        self.dispatcher.trust_store.approve(current)
        if not self.dispatcher.trust_store.is_approved(current):
            raise HookApprovalError("Hook command approval was not persisted")

        event = pending.event.model_copy(deep=True)
        dispatch = await self.dispatcher.dispatch(event, should_abort=should_abort)
        return await self._finish_dispatch(
            event,
            dispatch,
            prior_permission=pending.prior_permission,
        )

    @staticmethod
    def _permission_for_event(
        event: HookEventName,
        payload: dict[str, Any],
        permission: HookDecisionKind | str | None,
    ) -> HookDecisionKind | None:
        if event is not HookEventName.PRE_TOOL_USE:
            if permission is not None:
                raise ValueError("permission_decision is valid only for PreToolUse")
            return None
        if permission is None:
            raise ValueError("PreToolUse requires the existing permission_decision")
        try:
            current = HookDecisionKind(permission)
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid existing PreToolUse permission decision") from exc
        if current not in {
            HookDecisionKind.ALLOW,
            HookDecisionKind.ASK,
            HookDecisionKind.DENY,
        }:
            raise ValueError("Invalid existing PreToolUse permission decision")
        existing = payload.get("permission_decision")
        if existing is not None and existing != current.value:
            raise ValueError(
                "Payload permission_decision does not match runtime authority"
            )
        payload["permission_decision"] = current.value
        return current

    async def _finish_dispatch(
        self,
        event: HookEvent,
        dispatch: HookDispatchResult,
        *,
        prior_permission: HookDecisionKind | None,
    ) -> HookRuntimeResult:
        effective_decision: HookDecisionKind | None = None
        if event.event is HookEventName.PRE_TOOL_USE:
            if prior_permission is None or dispatch.pre_tool_decision is None:
                raise HookRuntimeError(
                    "PreToolUse dispatch omitted a restrictive decision"
                )
            effective_decision = combine_pre_tool_decisions(
                prior_permission,
                dispatch.pre_tool_decision,
            )

        audit = self._audit_summary(
            dispatch,
            effective_pre_tool_decision=effective_decision,
        )
        audit_event = self.job.publish_lifecycle(
            "hook.dispatch.completed",
            audit.to_payload(),
            message_id=event.message_id,
            call_id=event.call_id,
            checkpoint_id=event.checkpoint_id,
        )
        approval = await self._remember_approval(
            event,
            dispatch,
            prior_permission=prior_permission,
        )
        return HookRuntimeResult(
            hook_event=event.model_copy(deep=True),
            state=dispatch.state,
            pre_tool_decision=effective_decision,
            annotations=tuple(dispatch.annotations),
            warning_count=len(dispatch.warnings),
            audit=audit,
            audit_event_id=str(audit_event.event_id),
            approval_required=approval,
        )

    async def _remember_approval(
        self,
        event: HookEvent,
        dispatch: HookDispatchResult,
        *,
        prior_permission: HookDecisionKind | None,
    ) -> HookApprovalRequest | None:
        if dispatch.state is not HookDispatchState.APPROVAL_REQUIRED:
            return None
        if len(dispatch.approvals_required) != 1:
            raise HookRuntimeError(
                "Approval-required dispatch must identify one command"
            )
        descriptor = dispatch.approvals_required[0]
        descriptor_json = _canonical_descriptor(descriptor)
        fingerprint = descriptor.get("fingerprint")
        source = descriptor.get("source")
        source_name = descriptor.get("source_name")
        hook_id = descriptor.get("hook_id")
        if not all(
            isinstance(item, str) and item
            for item in (fingerprint, source, source_name, hook_id)
        ):
            raise HookRuntimeError(
                "Approval descriptor lacks a stable command identity"
            )
        request_id = uuid.uuid4().hex
        pending = _PendingApproval(
            event=event.model_copy(deep=True),
            prior_permission=prior_permission,
            descriptor_json=descriptor_json,
            fingerprint=fingerprint,
            source=source,
            source_name=source_name,
            hook_id=hook_id,
        )
        async with self._pending_lock:
            if len(self._pending) >= self.max_pending_approvals:
                raise HookApprovalUnavailable(
                    "Pending Hook approval capacity is exhausted"
                )
            self._pending[request_id] = pending
        return HookApprovalRequest(
            request_id=request_id,
            event_id=event.event_id,
            fingerprint=fingerprint,
            _descriptor_json=descriptor_json,
        )

    def _registered_command(self, pending: _PendingApproval) -> CommandHook:
        matches = [
            hook
            for hook in self.dispatcher.registry.hooks_for(pending.event.event)
            if isinstance(hook, CommandHook)
            and hook.source.value == pending.source
            and hook.source_name == pending.source_name
            and hook.declaration.hook_id == pending.hook_id
        ]
        if len(matches) != 1:
            raise HookApprovalStale("Registered Hook command is no longer available")
        return matches[0]

    @staticmethod
    def _audit_summary(
        dispatch: HookDispatchResult,
        *,
        effective_pre_tool_decision: HookDecisionKind | None,
    ) -> HookDispatchAuditSummary:
        executions = tuple(
            HookExecutionAudit(
                hook_ref=_hook_ref(record),
                source=record.source,
                failure_policy=record.failure_policy.value,
                status=record.status.value,
                fingerprint=_audit_fingerprint(record.fingerprint),
                decision=(
                    record.decision.value if record.decision is not None else None
                ),
            )
            for record in dispatch.executions
        )
        return HookDispatchAuditSummary(
            event_id=dispatch.event_id,
            event=dispatch.event,
            state=dispatch.state,
            pre_tool_decision=effective_pre_tool_decision,
            annotation_count=len(dispatch.annotations),
            warning_count=len(dispatch.warnings),
            approval_required_count=len(dispatch.approvals_required),
            executions=executions,
        )


HookRuntimeAdapter = HookRuntime


__all__ = [
    "HOOK_AUDIT_VERSION",
    "HOOK_LIFECYCLE_EVENT_TYPES",
    "MAX_PENDING_HOOK_APPROVALS",
    "HookApprovalError",
    "HookApprovalMismatch",
    "HookApprovalRequest",
    "HookApprovalStale",
    "HookApprovalUnavailable",
    "HookDispatchAuditSummary",
    "HookExecutionAudit",
    "HookRuntime",
    "HookRuntimeAdapter",
    "HookRuntimeError",
    "HookRuntimeResult",
    "hook_event_from_lifecycle",
]
