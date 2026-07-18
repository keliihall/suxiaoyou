from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from app.hooks.dispatcher import HookDispatchState, HookDispatcher
from app.hooks.models import (
    BuiltinHookDeclaration,
    HookCommandDeclaration,
    HookDecisionKind,
    HookEventName,
)
from app.hooks.registry import HookRegistry
from app.hooks.runtime import (
    HOOK_LIFECYCLE_EVENT_TYPES,
    HookApprovalMismatch,
    HookApprovalStale,
    HookApprovalUnavailable,
    HookRuntime,
)
from app.hooks.trust import HookTrustStore
from app.runtime.events import REDACTED
from app.streaming.manager import GenerationJob


def _trust(root: Path) -> HookTrustStore:
    return HookTrustStore(root, storage_root=root / ".private-hook-trust")


def _runtime(
    root: Path,
    registry: HookRegistry,
) -> tuple[GenerationJob, HookRuntime, HookTrustStore]:
    job = GenerationJob(
        "stream-1",
        "session-1",
        invocation_source="desktop",
        root_turn_id="root-1",
        workspace_instance_id="workspace-1",
    )
    trust = _trust(root)
    dispatcher = HookDispatcher(registry, trust, enabled=True)
    return job, HookRuntime(job, dispatcher), trust


def _write_executable(path: Path, body: str) -> Path:
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o700)
    return path


def _register_command(registry: HookRegistry, executable: Path):
    hook, = registry.register_project_commands([
        HookCommandDeclaration(
            hook_id=executable.name,
            event="PreToolUse",
            failure_policy="required",
            command=(executable.name,),
        )
    ])
    return hook


def _contains_forbidden_audit_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).casefold() in {"logs", "stdout", "stderr", "error", "annotation"}
            or _contains_forbidden_audit_key(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_forbidden_audit_key(item) for item in value)
    return False


@pytest.mark.asyncio
async def test_runtime_projects_all_supported_generation_lifecycle_events(
    tmp_path: Path,
) -> None:
    registry = HookRegistry(tmp_path)
    observed = []

    def handler(event):
        observed.append(event)
        decision = "allow" if event.event is HookEventName.PRE_TOOL_USE else "continue"
        return {"version": 1, "decision": decision}

    for event in HookEventName:
        registry.register_builtin(
            BuiltinHookDeclaration(
                hook_id=f"observe-{event.value}",
                event=event,
                failure_policy="required",
            ),
            handler,
        )
    job, runtime, _trust_store = _runtime(tmp_path, registry)

    for event in HookEventName:
        options = {
            "message_id": "message-1",
            "call_id": "call-1",
            "checkpoint_id": "checkpoint-1",
        }
        if event is HookEventName.PRE_TOOL_USE:
            options["permission_decision"] = "allow"
        result = await runtime.emit(
            event,
            {"api_key": "must-not-reach-hook", "kind": event.value},
            **options,
        )

        assert result.state is HookDispatchState.COMPLETED
        assert result.hook_event.event is event
        assert result.hook_event.payload["api_key"] == REDACTED
        assert result.hook_event.session_id == "session-1"
        assert result.hook_event.stream_id == "stream-1"
        assert result.hook_event.root_turn_id == "root-1"
        assert result.hook_event.turn_run_id == "stream-1"
        assert result.hook_event.workspace_instance_id == "workspace-1"
        assert result.hook_event.invocation_source == "desktop"
        assert result.hook_event.message_id == "message-1"
        assert result.hook_event.call_id == "call-1"
        assert result.hook_event.checkpoint_id == "checkpoint-1"

    assert [item.event for item in observed] == list(HookEventName)
    assert [item.event_type for item in job.lifecycle_events[::2]] == [
        HOOK_LIFECYCLE_EVENT_TYPES[event] for event in HookEventName
    ]
    assert all(
        item.event_type == "hook.dispatch.completed"
        for item in job.lifecycle_events[1::2]
    )
    assert [item.sequence for item in job.lifecycle_events] == list(
        range(1, len(job.lifecycle_events) + 1)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prior", "candidate", "expected"),
    [
        ("allow", "allow", HookDecisionKind.ALLOW),
        ("ask", "allow", HookDecisionKind.ASK),
        ("allow", "ask", HookDecisionKind.ASK),
        ("deny", "allow", HookDecisionKind.DENY),
        ("ask", "deny", HookDecisionKind.DENY),
    ],
)
async def test_pre_tool_result_can_only_narrow_existing_authority(
    tmp_path: Path,
    prior: str,
    candidate: str,
    expected: HookDecisionKind,
) -> None:
    registry = HookRegistry(tmp_path)

    def handler(event):
        assert event.payload["permission_decision"] == prior
        return {"version": 1, "decision": candidate}

    registry.register_builtin(
        BuiltinHookDeclaration(
            hook_id="restrictive-policy",
            event="PreToolUse",
            failure_policy="required",
        ),
        handler,
    )
    _job, runtime, _trust_store = _runtime(tmp_path, registry)

    result = await runtime.pre_tool_use(
        {"tool_name": "write", "tool_args": {"path": "report.docx"}},
        permission_decision=prior,
        call_id="call-1",
    )

    assert result.pre_tool_decision is expected
    assert result.audit.pre_tool_decision is expected


@pytest.mark.asyncio
async def test_invalid_pre_tool_authority_is_rejected_before_lifecycle_publish(
    tmp_path: Path,
) -> None:
    registry = HookRegistry(tmp_path)
    job, runtime, _trust_store = _runtime(tmp_path, registry)

    with pytest.raises(ValueError, match="existing PreToolUse"):
        await runtime.pre_tool_use({}, permission_decision="continue")
    with pytest.raises(ValueError, match="does not match"):
        await runtime.pre_tool_use(
            {"permission_decision": "deny"},
            permission_decision="allow",
        )

    assert job.lifecycle_events == []


@pytest.mark.asyncio
async def test_exact_approval_is_one_shot_then_redispatches_same_event(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "approved-ran"
    secret_log = "stderr-secret-must-not-enter-lifecycle"
    secret_annotation = "annotation-secret-must-not-enter-lifecycle"
    executable = _write_executable(
        tmp_path / "approval-policy",
        (
            "import json, pathlib, sys\n"
            "event = json.load(sys.stdin)\n"
            f"pathlib.Path({str(marker)!r}).write_text(event['event_id'])\n"
            f"print({secret_log!r}, file=sys.stderr)\n"
            "print(json.dumps({'version': 1, 'decision': 'allow', "
            f"'annotation': {secret_annotation!r}}}))\n"
        ),
    )
    registry = HookRegistry(tmp_path)
    _register_command(registry, executable)
    job, runtime, _trust_store = _runtime(tmp_path, registry)

    initial = await runtime.pre_tool_use(
        {"tool_name": "write", "tool_args": {"path": "report.docx"}},
        permission_decision="allow",
        call_id="call-1",
    )

    assert initial.state is HookDispatchState.APPROVAL_REQUIRED
    assert initial.pre_tool_decision is HookDecisionKind.ASK
    assert initial.approval_required is not None
    assert not marker.exists()
    approval = initial.approval_required

    completed = await runtime.approve_exact(
        approval.request_id,
        descriptor=approval.descriptor,
        fingerprint=approval.fingerprint,
    )

    assert completed.state is HookDispatchState.COMPLETED
    assert completed.pre_tool_decision is HookDecisionKind.ALLOW
    assert completed.hook_event.event_id == initial.hook_event.event_id
    assert marker.read_text(encoding="utf-8") == initial.hook_event.event_id
    assert completed.annotations == (secret_annotation,)
    with pytest.raises(HookApprovalUnavailable, match="consumed"):
        await runtime.approve_exact(
            approval.request_id,
            descriptor=approval.descriptor,
            fingerprint=approval.fingerprint,
        )

    assert [event.event_type for event in job.lifecycle_events] == [
        "tool.pre_use",
        "hook.dispatch.completed",
        "hook.dispatch.completed",
    ]
    serialized = json.dumps(
        [event.to_dict() for event in job.lifecycle_events],
        ensure_ascii=False,
    )
    assert secret_log not in serialized
    assert secret_annotation not in serialized
    assert str(executable.resolve()) not in serialized
    assert not any(
        _contains_forbidden_audit_key(event.payload)
        for event in job.lifecycle_events
        if event.event_type == "hook.dispatch.completed"
    )


@pytest.mark.asyncio
async def test_wrong_exact_fingerprint_consumes_request_without_execution(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "must-not-run"
    executable = _write_executable(
        tmp_path / "exact-policy",
        (
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('bad')\n"
            "print('{\"version\":1,\"decision\":\"allow\"}')\n"
        ),
    )
    registry = HookRegistry(tmp_path)
    _register_command(registry, executable)
    _job, runtime, _trust_store = _runtime(tmp_path, registry)
    initial = await runtime.pre_tool_use({}, permission_decision="allow")
    approval = initial.approval_required
    assert approval is not None

    with pytest.raises(HookApprovalMismatch, match="exact"):
        await runtime.approve_exact(
            approval.request_id,
            descriptor=approval.descriptor,
            fingerprint="sha256:" + "0" * 64,
        )
    with pytest.raises(HookApprovalUnavailable, match="consumed"):
        await runtime.approve_exact(
            approval.request_id,
            descriptor=approval.descriptor,
            fingerprint=approval.fingerprint,
        )

    assert not marker.exists()


@pytest.mark.asyncio
async def test_content_change_fails_exact_approval_and_cannot_spawn(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "changed-must-not-run"
    executable = _write_executable(
        tmp_path / "changing-policy",
        "print('{\"version\":1,\"decision\":\"allow\"}')\n",
    )
    registry = HookRegistry(tmp_path)
    original = _register_command(registry, executable)
    _job, runtime, trust = _runtime(tmp_path, registry)
    initial = await runtime.pre_tool_use({}, permission_decision="allow")
    approval = initial.approval_required
    assert approval is not None

    _write_executable(
        executable,
        (
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('bad')\n"
            "print('{\"version\":1,\"decision\":\"allow\"}')\n"
        ),
    )

    with pytest.raises(HookApprovalStale, match="changed"):
        await runtime.approve_exact(
            approval.request_id,
            descriptor=approval.descriptor,
            fingerprint=approval.fingerprint,
        )
    with pytest.raises(HookApprovalUnavailable, match="consumed"):
        await runtime.approve_exact(
            approval.request_id,
            descriptor=approval.descriptor,
            fingerprint=approval.fingerprint,
        )

    assert not marker.exists()
    assert not trust.is_approved(original)
