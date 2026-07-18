from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.hooks.dispatcher import HookDispatchState, HookDispatcher
from app.hooks.models import (
    BuiltinHookDeclaration,
    HookCommandDeclaration,
    HookDecisionKind,
    HookEvent,
    HookEventName,
)
from app.hooks.registry import HookRegistry
from app.hooks.runner import HookRunStatus
from app.hooks.trust import HookTrustStore


def _trust(root: Path) -> HookTrustStore:
    return HookTrustStore(root, storage_root=root / ".private-hook-trust")


def _command_hook(
    registry: HookRegistry,
    executable: Path,
    *,
    failure_policy: str,
):
    hook, = registry.register_project_commands([
        HookCommandDeclaration(
            hook_id=executable.name,
            event="PreToolUse",
            failure_policy=failure_policy,
            command=(executable.name,),
        ),
    ])
    return hook


@pytest.mark.asyncio
async def test_dispatcher_feature_gate_can_be_closed_explicitly(
    tmp_path: Path,
    hook_event,
) -> None:
    called = False

    def handler(_event):
        nonlocal called
        called = True
        return {"version": 1, "decision": "deny"}

    registry = HookRegistry(tmp_path)
    registry.register_builtin(
        BuiltinHookDeclaration(
            hook_id="builtin-policy",
            event="PreToolUse",
            failure_policy="required",
        ),
        handler,
    )

    result = await HookDispatcher(
        registry,
        _trust(tmp_path),
        enabled=False,
    ).dispatch(hook_event)

    assert result.state is HookDispatchState.DISABLED
    assert result.pre_tool_decision is HookDecisionKind.ALLOW
    assert result.executions == ()
    assert called is False


@pytest.mark.asyncio
async def test_untrusted_project_hook_requests_approval_without_spawning(
    tmp_path: Path,
    executable_hook,
    hook_event,
) -> None:
    marker = tmp_path / "must-not-run"
    executable = executable_hook("needs-trust", f"""
from pathlib import Path
Path({str(marker)!r}).write_text('bad')
print('{{"version":1,"decision":"allow"}}')
""")
    registry = HookRegistry(tmp_path)
    _command_hook(registry, executable, failure_policy="required")

    result = await HookDispatcher(
        registry,
        _trust(tmp_path),
        enabled=True,
    ).dispatch(hook_event)

    assert result.state is HookDispatchState.APPROVAL_REQUIRED
    assert result.pre_tool_decision is HookDecisionKind.ASK
    assert result.executions[0].status is HookRunStatus.APPROVAL_REQUIRED
    assert len(result.approvals_required) == 1
    assert not marker.exists()


@pytest.mark.asyncio
async def test_approved_command_deny_is_returned_without_mutation_surface(
    tmp_path: Path,
    executable_hook,
    hook_event,
) -> None:
    executable = executable_hook("deny-policy", """
import json, sys
event = json.load(sys.stdin)
assert event['payload']['tool_args']['file_path'] == 'report.docx'
print(json.dumps({'version': 1, 'decision': 'deny', 'annotation': 'policy'}))
""")
    registry = HookRegistry(tmp_path)
    hook = _command_hook(registry, executable, failure_policy="required")
    trust = _trust(tmp_path)
    trust.approve(hook)

    result = await HookDispatcher(
        registry,
        trust,
        enabled=True,
    ).dispatch(hook_event)

    assert result.state is HookDispatchState.COMPLETED
    assert result.pre_tool_decision is HookDecisionKind.DENY
    assert result.annotations == ("policy",)
    assert hook_event.payload["tool_args"] == {"file_path": "report.docx"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_policy", "expected_state", "expected_decision", "warning_count"),
    [
        ("required", HookDispatchState.FAILED_CLOSED, HookDecisionKind.DENY, 0),
        ("optional", HookDispatchState.COMPLETED, HookDecisionKind.ALLOW, 1),
    ],
)
async def test_invalid_or_rewriting_response_obeys_failure_policy(
    tmp_path: Path,
    executable_hook,
    hook_event,
    failure_policy,
    expected_state,
    expected_decision,
    warning_count,
) -> None:
    executable = executable_hook(f"rewrite-{failure_policy}", """
print('{"version":1,"decision":"allow","permission":"allow"}')
""")
    registry = HookRegistry(tmp_path)
    hook = _command_hook(
        registry,
        executable,
        failure_policy=failure_policy,
    )
    trust = _trust(tmp_path)
    trust.approve(hook)

    result = await HookDispatcher(
        registry,
        trust,
        enabled=True,
    ).dispatch(hook_event)

    assert result.state is expected_state
    assert result.pre_tool_decision is expected_decision
    assert len(result.warnings) == warning_count
    assert result.executions[0].status is HookRunStatus.INVALID_RESPONSE


@pytest.mark.asyncio
async def test_builtin_receives_deep_copy_and_allow_cannot_mutate_call(
    tmp_path: Path,
    hook_event,
) -> None:
    def handler(event):
        event.payload["tool_args"]["file_path"] = "rewritten.txt"
        return {"version": 1, "decision": "allow"}

    registry = HookRegistry(tmp_path)
    registry.register_builtin(
        BuiltinHookDeclaration(
            hook_id="copy-policy",
            event="PreToolUse",
            failure_policy="required",
        ),
        handler,
    )

    result = await HookDispatcher(
        registry,
        _trust(tmp_path),
        enabled=True,
    ).dispatch(hook_event)

    assert result.pre_tool_decision is HookDecisionKind.ALLOW
    assert hook_event.payload["tool_args"]["file_path"] == "report.docx"


@pytest.mark.asyncio
async def test_required_non_pre_tool_failure_is_fail_closed(
    tmp_path: Path,
) -> None:
    async def failure(_event):
        raise RuntimeError("required policy unavailable")

    event = HookEvent(
        event_id="stop-event",
        event=HookEventName.STOP,
        sequence=8,
        occurred_at=datetime.now(timezone.utc),
        payload={},
    )
    registry = HookRegistry(tmp_path)
    registry.register_builtin(
        BuiltinHookDeclaration(
            hook_id="required-stop",
            event="Stop",
            failure_policy="required",
        ),
        failure,
    )

    result = await HookDispatcher(
        registry,
        _trust(tmp_path),
        enabled=True,
    ).dispatch(event)

    assert result.state is HookDispatchState.FAILED_CLOSED
    assert result.pre_tool_decision is None
    assert result.executions[0].status is HookRunStatus.INVALID_RESPONSE


@pytest.mark.asyncio
async def test_content_change_after_approval_returns_new_approval_request(
    tmp_path: Path,
    executable_hook,
    hook_event,
) -> None:
    marker = tmp_path / "changed-ran"
    executable = executable_hook(
        "changing-policy",
        "print('{\"version\":1,\"decision\":\"allow\"}')\n",
    )
    registry = HookRegistry(tmp_path)
    hook = _command_hook(registry, executable, failure_policy="required")
    trust = _trust(tmp_path)
    trust.approve(hook)
    executable.write_text(
        f"#!{sys.executable}\n"
        f"from pathlib import Path; Path({str(marker)!r}).write_text('bad')\n"
        "print('{\"version\":1,\"decision\":\"allow\"}')\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)

    result = await HookDispatcher(
        registry,
        trust,
        enabled=True,
    ).dispatch(hook_event)

    assert result.state is HookDispatchState.APPROVAL_REQUIRED
    assert result.pre_tool_decision is HookDecisionKind.ASK
    assert result.approvals_required[0]["fingerprint"] != hook.fingerprint
    assert not marker.exists()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="POSIX cancellation evidence")
async def test_dispatch_task_cancellation_waits_for_process_tree_cleanup(
    tmp_path: Path,
    executable_hook,
    hook_event,
) -> None:
    marker = tmp_path / "cancelled-dispatch-survived"
    executable = executable_hook("cancel-dispatch", f"""
import pathlib, time
time.sleep(1)
pathlib.Path({str(marker)!r}).write_text('bad')
""")
    registry = HookRegistry(tmp_path)
    hook = _command_hook(registry, executable, failure_policy="required")
    trust = _trust(tmp_path)
    trust.approve(hook)
    dispatcher = HookDispatcher(registry, trust, enabled=True)

    task = asyncio.create_task(dispatcher.dispatch(hook_event))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    time.sleep(1.1)
    assert not marker.exists()
