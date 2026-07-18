"""Bounded local-command execution for v1.1 Hooks."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from app.hooks.models import (
    HookDecision,
    HookDecisionKind,
    HookEvent,
    HookProtocolError,
    MAX_HOOK_EVENT_BYTES,
)
from app.hooks.registry import CommandHook, refresh_command_hook
from app.hooks.trust import HookTrustStore
from app.tool.posix_process import run_posix_process
from app.tool.subprocess_compat import decode_subprocess_output
from app.tool.windows_process import run_windows_process


DEFAULT_HOOK_OUTPUT_BYTES = 64 * 1024
MAX_HOOK_OUTPUT_BYTES = 1024 * 1024


class HookRunStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    OUTPUT_LIMIT = "output_limit"
    INVALID_RESPONSE = "invalid_response"
    IDENTITY_CHANGED = "identity_changed"
    APPROVAL_REQUIRED = "approval_required"


@dataclass(frozen=True, slots=True)
class HookRunResult:
    status: HookRunStatus
    hook_id: str
    fingerprint: str
    decision: HookDecision | None
    logs: str
    exit_code: int | None
    duration_seconds: float
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status is HookRunStatus.SUCCESS


class HookCommandRunner:
    """Execute one approved command using the shared process supervisors."""

    def __init__(
        self,
        *,
        max_output_bytes: int = DEFAULT_HOOK_OUTPUT_BYTES,
        max_event_bytes: int = MAX_HOOK_EVENT_BYTES,
    ) -> None:
        if (
            isinstance(max_output_bytes, bool)
            or not isinstance(max_output_bytes, int)
            or not 0 < max_output_bytes <= MAX_HOOK_OUTPUT_BYTES
        ):
            raise ValueError("Invalid Hook output limit")
        if (
            isinstance(max_event_bytes, bool)
            or not isinstance(max_event_bytes, int)
            or not 0 < max_event_bytes <= MAX_HOOK_EVENT_BYTES
        ):
            raise ValueError("Invalid Hook event limit")
        self.max_output_bytes = max_output_bytes
        self.max_event_bytes = max_event_bytes

    def run(
        self,
        hook: CommandHook,
        event: HookEvent,
        *,
        trust_store: HookTrustStore,
        should_abort: Callable[[], bool] | None = None,
    ) -> HookRunResult:
        started = time.monotonic()
        abort_probe = should_abort or (lambda: False)

        def result(
            status: HookRunStatus,
            *,
            decision: HookDecision | None = None,
            logs: str = "",
            exit_code: int | None = None,
            error: str | None = None,
        ) -> HookRunResult:
            return HookRunResult(
                status=status,
                hook_id=hook.declaration.hook_id,
                fingerprint=hook.fingerprint,
                decision=decision,
                logs=logs,
                exit_code=exit_code,
                duration_seconds=max(0.0, time.monotonic() - started),
                error=error,
            )

        try:
            current = refresh_command_hook(hook)
        except Exception as exc:
            return result(
                HookRunStatus.IDENTITY_CHANGED,
                error=f"Hook launch identity could not be revalidated: {exc}",
            )
        if current.fingerprint != hook.fingerprint:
            return result(
                HookRunStatus.IDENTITY_CHANGED,
                error="Hook launch identity changed after approval check",
            )
        if not trust_store.is_approved(current):
            return result(
                HookRunStatus.APPROVAL_REQUIRED,
                error="Hook command approval is required at the launch boundary",
            )

        event_bytes = event.to_wire_bytes()
        if len(event_bytes) > self.max_event_bytes:
            return result(
                HookRunStatus.FAILED,
                error="Hook event exceeds the runner input limit",
            )

        try:
            if os.name == "nt":
                process_result = run_windows_process(
                    current.launch.command,
                    cwd=current.launch.cwd,
                    env=current.launch.environment,
                    timeout_seconds=current.declaration.timeout_seconds,
                    should_abort=abort_probe,
                    max_output_bytes=self.max_output_bytes,
                    stdin_bytes=event_bytes,
                )
                truncated = (
                    process_result.stdout_truncated
                    or process_result.stderr_truncated
                )
            elif os.name == "posix":
                process_result = run_posix_process(
                    current.launch.command,
                    cwd=current.launch.cwd,
                    env=current.launch.environment,
                    timeout_seconds=current.declaration.timeout_seconds,
                    should_abort=abort_probe,
                    max_output_bytes=self.max_output_bytes,
                    stdin_bytes=event_bytes,
                )
                truncated = process_result.truncated
            else:  # pragma: no cover - supported desktop platforms are nt/posix
                return result(
                    HookRunStatus.FAILED,
                    error=f"Unsupported Hook process platform: {os.name}",
                )
        except Exception as exc:
            return result(
                HookRunStatus.FAILED,
                error=f"Hook process supervision failed: {exc}",
            )

        logs = decode_subprocess_output(process_result.stderr)
        if process_result.termination == "timeout":
            return result(
                HookRunStatus.TIMEOUT,
                logs=logs,
                exit_code=process_result.exit_code,
                error="Hook command exceeded its timeout",
            )
        if process_result.termination == "aborted":
            return result(
                HookRunStatus.CANCELLED,
                logs=logs,
                exit_code=process_result.exit_code,
                error="Hook command was cancelled",
            )
        if truncated:
            return result(
                HookRunStatus.OUTPUT_LIMIT,
                logs=logs,
                exit_code=process_result.exit_code,
                error="Hook stdout or log output exceeded its limit",
            )
        if process_result.exit_code != 0:
            return result(
                HookRunStatus.FAILED,
                logs=logs,
                exit_code=process_result.exit_code,
                error=f"Hook command exited with status {process_result.exit_code}",
            )

        try:
            decision = HookDecision.from_wire_bytes(
                process_result.stdout,
                event=event.event,
            )
        except HookProtocolError as exc:
            return result(
                HookRunStatus.INVALID_RESPONSE,
                logs=logs,
                exit_code=process_result.exit_code,
                error=str(exc),
            )
        if decision.decision is HookDecisionKind.ERROR:
            return result(
                HookRunStatus.FAILED,
                decision=decision,
                logs=logs,
                exit_code=process_result.exit_code,
                error=decision.annotation or "Hook reported an error",
            )
        return result(
            HookRunStatus.SUCCESS,
            decision=decision,
            logs=logs,
            exit_code=process_result.exit_code,
        )


__all__ = [
    "DEFAULT_HOOK_OUTPUT_BYTES",
    "HookCommandRunner",
    "HookRunResult",
    "HookRunStatus",
    "MAX_HOOK_OUTPUT_BYTES",
]
