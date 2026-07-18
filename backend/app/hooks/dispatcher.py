"""Failure-policy and trust-aware Hook dispatch, intentionally not session-wired."""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.hooks.models import (
    HookDecision,
    HookDecisionKind,
    HookEvent,
    HookEventName,
    HookFailurePolicy,
    HookProtocolError,
    combine_pre_tool_decisions,
)
from app.hooks.registry import (
    BuiltinHook,
    CommandHook,
    HookRegistry,
    RegisteredHook,
    refresh_command_hook,
)
from app.hooks.runner import HookCommandRunner, HookRunResult, HookRunStatus
from app.hooks.trust import HookTrustStore
from app.release_features import V11_HOOKS_RELEASED


class HookDispatchState(StrEnum):
    DISABLED = "disabled"
    COMPLETED = "completed"
    APPROVAL_REQUIRED = "approval_required"
    FAILED_CLOSED = "failed_closed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class HookExecutionRecord:
    hook_id: str
    source: str
    source_name: str
    failure_policy: HookFailurePolicy
    status: HookRunStatus
    fingerprint: str | None
    decision: HookDecisionKind | None
    annotation: str | None
    logs: str
    error: str | None


@dataclass(frozen=True, slots=True)
class HookDispatchResult:
    state: HookDispatchState
    event_id: str
    event: HookEventName
    pre_tool_decision: HookDecisionKind | None
    annotations: tuple[str, ...]
    warnings: tuple[str, ...]
    executions: tuple[HookExecutionRecord, ...]
    approvals_required: tuple[dict[str, Any], ...]


class HookDispatcher:
    """Dispatch registered Hooks without granting or mutating authority.

    The default is the code-owned v1.1 release gate. Callers may close the
    dispatcher explicitly for negative-path verification or stricter runtime
    policy without widening any authority.
    """

    def __init__(
        self,
        registry: HookRegistry,
        trust_store: HookTrustStore,
        *,
        runner: HookCommandRunner | None = None,
        enabled: bool = V11_HOOKS_RELEASED,
    ) -> None:
        self.registry = registry
        self.trust_store = trust_store
        self.runner = runner or HookCommandRunner()
        self.enabled = enabled

    async def dispatch(
        self,
        event: HookEvent | Mapping[str, Any],
        *,
        should_abort: Callable[[], bool] | None = None,
    ) -> HookDispatchResult:
        try:
            parsed_event = (
                event.model_copy(deep=True)
                if isinstance(event, HookEvent)
                else HookEvent.model_validate(event)
            )
        except Exception as exc:
            # An unknown version cannot safely be projected as a completed
            # runtime event, so the boundary raises instead of degrading.
            raise HookProtocolError("Invalid or unsupported Hook event") from exc

        initial_decision = (
            HookDecisionKind.ALLOW
            if parsed_event.event is HookEventName.PRE_TOOL_USE
            else None
        )
        if not self.enabled:
            return HookDispatchResult(
                state=HookDispatchState.DISABLED,
                event_id=parsed_event.event_id,
                event=parsed_event.event,
                pre_tool_decision=initial_decision,
                annotations=(),
                warnings=(),
                executions=(),
                approvals_required=(),
            )

        decision = initial_decision
        annotations: list[str] = []
        warnings: list[str] = []
        executions: list[HookExecutionRecord] = []
        approvals_required: list[dict[str, Any]] = []
        state = HookDispatchState.COMPLETED

        for registered in self.registry.hooks_for(parsed_event.event):
            if should_abort is not None and should_abort():
                state = HookDispatchState.CANCELLED
                if decision is not None:
                    decision = HookDecisionKind.DENY
                break

            if isinstance(registered, CommandHook):
                try:
                    hook = refresh_command_hook(registered)
                except Exception as exc:
                    record = self._failure_record(
                        registered,
                        HookRunStatus.IDENTITY_CHANGED,
                        f"Hook identity could not be revalidated: {exc}",
                    )
                    executions.append(record)
                    closed = self._apply_failure(
                        registered,
                        record,
                        warnings,
                    )
                    if closed:
                        state = HookDispatchState.FAILED_CLOSED
                        if decision is not None:
                            decision = HookDecisionKind.DENY
                        break
                    continue

                if not self.trust_store.is_approved(hook):
                    approvals_required.append(hook.public_descriptor())
                    executions.append(HookExecutionRecord(
                        hook_id=hook.declaration.hook_id,
                        source=hook.source.value,
                        source_name=hook.source_name,
                        failure_policy=hook.declaration.failure_policy,
                        status=HookRunStatus.APPROVAL_REQUIRED,
                        fingerprint=hook.fingerprint,
                        decision=None,
                        annotation=None,
                        logs="",
                        error="Hook command approval is required",
                    ))
                    state = HookDispatchState.APPROVAL_REQUIRED
                    if decision is not None:
                        decision = combine_pre_tool_decisions(
                            decision,
                            HookDecisionKind.ASK,
                        )
                    break

                run_result = await self._run_command(
                    hook,
                    parsed_event.model_copy(deep=True),
                    should_abort=should_abort,
                )
                record = self._command_record(hook, run_result)
            else:
                run_result = await self._run_builtin(
                    registered,
                    parsed_event.model_copy(deep=True),
                )
                record = self._builtin_record(registered, run_result)

            executions.append(record)
            if run_result.status is HookRunStatus.CANCELLED:
                state = HookDispatchState.CANCELLED
                if decision is not None:
                    decision = HookDecisionKind.DENY
                break
            if not run_result.succeeded:
                closed = self._apply_failure(
                    registered,
                    record,
                    warnings,
                )
                if closed:
                    state = HookDispatchState.FAILED_CLOSED
                    if decision is not None:
                        decision = HookDecisionKind.DENY
                    break
                continue

            hook_decision = run_result.decision
            if hook_decision is None:  # defensive; successful runs always decide
                record = self._failure_record(
                    registered,
                    HookRunStatus.INVALID_RESPONSE,
                    "Hook completed without a decision",
                )
                executions[-1] = record
                closed = self._apply_failure(
                    registered,
                    record,
                    warnings,
                )
                if closed:
                    state = HookDispatchState.FAILED_CLOSED
                    if decision is not None:
                        decision = HookDecisionKind.DENY
                    break
                continue

            if hook_decision.annotation:
                annotations.append(hook_decision.annotation)
            if decision is not None:
                decision = combine_pre_tool_decisions(
                    decision,
                    hook_decision.decision,
                )
                if decision is HookDecisionKind.DENY:
                    break

        return HookDispatchResult(
            state=state,
            event_id=parsed_event.event_id,
            event=parsed_event.event,
            pre_tool_decision=decision,
            annotations=tuple(annotations),
            warnings=tuple(warnings),
            executions=tuple(executions),
            approvals_required=tuple(approvals_required),
        )

    async def _run_command(
        self,
        hook: CommandHook,
        event: HookEvent,
        *,
        should_abort: Callable[[], bool] | None,
    ) -> HookRunResult:
        cancellation = threading.Event()

        def abort_probe() -> bool:
            return cancellation.is_set() or (
                should_abort is not None and should_abort()
            )

        task = asyncio.create_task(asyncio.to_thread(
            self.runner.run,
            hook,
            event,
            trust_store=self.trust_store,
            should_abort=abort_probe,
        ))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancellation.set()
            try:
                await asyncio.shield(task)
            except Exception:
                pass
            raise

    async def _run_builtin(
        self,
        hook: BuiltinHook,
        event: HookEvent,
    ) -> HookRunResult:
        started = asyncio.get_running_loop().time()

        async def invoke() -> HookDecision:
            if inspect.iscoroutinefunction(hook.handler):
                value = await hook.handler(event)
            else:
                value = await asyncio.to_thread(hook.handler, event)
                if inspect.isawaitable(value):
                    value = await value
            parsed = (
                value
                if isinstance(value, HookDecision)
                else HookDecision.model_validate(value)
            )
            return parsed.validate_for_event(event.event)

        try:
            decision = await asyncio.wait_for(
                invoke(),
                timeout=hook.declaration.timeout_seconds,
            )
        except asyncio.TimeoutError:
            status = HookRunStatus.TIMEOUT
            decision = None
            error = "Built-in Hook exceeded its timeout"
        except Exception as exc:
            status = HookRunStatus.INVALID_RESPONSE
            decision = None
            error = f"Built-in Hook failed: {exc}"
        else:
            if decision.decision is HookDecisionKind.ERROR:
                status = HookRunStatus.FAILED
                error = decision.annotation or "Built-in Hook reported an error"
            else:
                status = HookRunStatus.SUCCESS
                error = None
        return HookRunResult(
            status=status,
            hook_id=hook.declaration.hook_id,
            fingerprint=f"builtin:{hook.declaration.hook_id}",
            decision=decision,
            logs="",
            exit_code=None,
            duration_seconds=max(
                0.0,
                asyncio.get_running_loop().time() - started,
            ),
            error=error,
        )

    @staticmethod
    def _command_record(
        hook: CommandHook,
        result: HookRunResult,
    ) -> HookExecutionRecord:
        return HookExecutionRecord(
            hook_id=hook.declaration.hook_id,
            source=hook.source.value,
            source_name=hook.source_name,
            failure_policy=hook.declaration.failure_policy,
            status=result.status,
            fingerprint=result.fingerprint,
            decision=(
                result.decision.decision if result.decision is not None else None
            ),
            annotation=(
                result.decision.annotation if result.decision is not None else None
            ),
            logs=result.logs,
            error=result.error,
        )

    @staticmethod
    def _builtin_record(
        hook: BuiltinHook,
        result: HookRunResult,
    ) -> HookExecutionRecord:
        return HookExecutionRecord(
            hook_id=hook.declaration.hook_id,
            source=hook.source.value,
            source_name=hook.source_name,
            failure_policy=hook.declaration.failure_policy,
            status=result.status,
            fingerprint=result.fingerprint,
            decision=(
                result.decision.decision if result.decision is not None else None
            ),
            annotation=(
                result.decision.annotation if result.decision is not None else None
            ),
            logs=result.logs,
            error=result.error,
        )

    @staticmethod
    def _failure_record(
        hook: RegisteredHook,
        status: HookRunStatus,
        error: str,
    ) -> HookExecutionRecord:
        return HookExecutionRecord(
            hook_id=hook.declaration.hook_id,
            source=hook.source.value,
            source_name=hook.source_name,
            failure_policy=hook.declaration.failure_policy,
            status=status,
            fingerprint=(hook.fingerprint if isinstance(hook, CommandHook) else None),
            decision=None,
            annotation=None,
            logs="",
            error=error,
        )

    @staticmethod
    def _apply_failure(
        hook: RegisteredHook,
        record: HookExecutionRecord,
        warnings: list[str],
    ) -> bool:
        message = record.error or f"Hook {record.hook_id} failed"
        if hook.declaration.failure_policy is HookFailurePolicy.REQUIRED:
            return True
        warnings.append(message)
        return False


__all__ = [
    "HookDispatchResult",
    "HookDispatchState",
    "HookDispatcher",
    "HookExecutionRecord",
]
