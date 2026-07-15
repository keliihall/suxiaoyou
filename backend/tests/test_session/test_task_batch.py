from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent.permission import evaluate, serialize_permission_snapshot
from app.models.session import Session
from app.schemas.agent import PermissionRule, Ruleset
from app.schemas.chat import TaskBatchRequest
from app.session.manager import create_session
from app.session.processor import SessionProcessor
from app.session import processor as processor_module
from app.session.task_batch import run_task_batch
from app.streaming.events import (
    DONE,
    SSEEvent,
    TASK_BATCH_FINISH,
    TASK_BATCH_START,
    TASK_BATCH_UPDATE,
    TEXT_DELTA,
)
from app.streaming.manager import GenerationJob


pytestmark = pytest.mark.asyncio


def _task(title: str, prompt: str, *, model: str | None = None) -> dict:
    return {
        "title": title,
        "prompt": prompt,
        "agent": "explore",
        "model": model,
    }


async def test_parallel_task_batch_streams_progress_and_persists_children(
    session_factory,
    monkeypatch,
) -> None:
    calls: list[
        tuple[str, str | None, str, str | None, object, Ruleset, bool, bool, str, str | None]
    ] = []

    async def fake_run_generation(job, request, **_kwargs):
        calls.append((
            request.text,
            request.model,
            request.language,
            request.workspace,
            request.permission_presets,
            Ruleset.model_validate({"rules": request.permission_rules}),
            request._permission_rules_authoritative,
            job.abort_event is parent_job.abort_event,
            job.invocation_source,
            job.invocation_source_id,
        ))
        job.publish(SSEEvent(TEXT_DELTA, {"text": f"done {request.text}"}))

    monkeypatch.setattr("app.session.task_batch.run_generation", fake_run_generation)

    job = GenerationJob(
        "stream-1",
        "parent-1",
        invocation_source="channel",
        invocation_source_id="telegram",
    )
    parent_job = job
    body = TaskBatchRequest(
        session_id="parent-1",
        mode="parallel",
        tasks=[
            _task("One", "first", model="model-a"),
            _task("Two", "second", model="model-b"),
        ],
        workspace="/workspace",
        permission_presets={"run_commands": False},
        permission_rules=[
            {"action": "deny", "permission": "bash", "pattern": "*"},
        ],
        language="en",
    )

    await run_task_batch(
        job,
        body,
        session_factory=session_factory,
        provider_registry=MagicMock(),
        agent_registry=MagicMock(),
        tool_registry=MagicMock(),
    )

    assert [(call[0], call[1], call[2], call[3]) for call in calls] == [
        ("first", "model-a", "en", "/workspace"),
        ("second", "model-b", "en", "/workspace"),
    ]
    assert all(call[4] is None for call in calls)
    assert all(evaluate("bash", "*", call[5]) == "deny" for call in calls)
    assert all(call[6:8] == (True, True) for call in calls)
    assert all(call[8:] == ("channel", "telegram") for call in calls)
    assert [event.event for event in job.events].count(TASK_BATCH_START) == 1
    assert [event.event for event in job.events].count(TASK_BATCH_FINISH) == 1
    assert job.events[-1].event == DONE

    finish_event = next(event for event in job.events if event.event == TASK_BATCH_FINISH)
    assert [task["status"] for task in finish_event.data["tasks"]] == ["completed", "completed"]

    async with session_factory() as db:
        sessions = (await db.execute(Session.__table__.select())).mappings().all()

    parent_rows = [row for row in sessions if row["id"] == "parent-1"]
    child_rows = [row for row in sessions if row["parent_id"] == "parent-1"]
    assert len(parent_rows) == 1
    assert len(child_rows) == 2


async def test_sequential_task_batch_cancels_pending_after_failure(
    session_factory,
    monkeypatch,
    tmp_path,
) -> None:
    calls: list[str] = []

    async def fake_run_generation(job, request, **_kwargs):
        calls.append(request.text)
        if request.text == "fail":
            job.publish(SSEEvent("agent-error", {"error_message": "child failed"}))
        else:
            job.publish(SSEEvent(TEXT_DELTA, {"text": "ok"}))

    monkeypatch.setattr("app.session.task_batch.run_generation", fake_run_generation)

    job = GenerationJob("stream-1", "parent-1")
    body = TaskBatchRequest(
        session_id="parent-1",
        mode="sequential",
        tasks=[
            _task("One", "ok"),
            _task("Two", "fail"),
            _task("Three", "never"),
        ],
        workspace=str(tmp_path / "workspace"),
    )

    await run_task_batch(
        job,
        body,
        session_factory=session_factory,
        provider_registry=MagicMock(),
        agent_registry=MagicMock(),
        tool_registry=MagicMock(),
    )

    assert calls == ["ok", "fail"]
    finish_event = next(event for event in job.events if event.event == TASK_BATCH_FINISH)
    assert [task["status"] for task in finish_event.data["tasks"]] == [
        "completed",
        "failed",
        "cancelled",
    ]

    updates = [event for event in job.events if event.event == TASK_BATCH_UPDATE]
    assert updates[-1].data["tasks"][1]["error"] == "child failed"


async def test_task_batch_without_workspace_fails_before_creating_children(
    session_factory,
    monkeypatch,
) -> None:
    run_generation = MagicMock()
    monkeypatch.setattr("app.session.task_batch.run_generation", run_generation)
    job = GenerationJob("stream-1", "parent-1")
    body = TaskBatchRequest(
        session_id="parent-1",
        tasks=[_task("One", "must not run")],
    )

    await run_task_batch(
        job,
        body,
        session_factory=session_factory,
        provider_registry=MagicMock(),
        agent_registry=MagicMock(),
        tool_registry=MagicMock(),
    )

    run_generation.assert_not_called()
    assert job.events[-1].event == "agent-error"
    assert "Select a workspace" in job.events[-1].data["error_message"]
    async with session_factory() as db:
        rows = (await db.execute(Session.__table__.select())).mappings().all()
    assert [row for row in rows if row["id"] == "parent-1"] == []


async def test_existing_parent_workspace_is_canonical_for_every_child(
    session_factory,
    monkeypatch,
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    async with session_factory() as db:
        async with db.begin():
            await create_session(
                db,
                id="parent-1",
                directory=str(workspace),
            )

    captured: list[tuple[str | None, bool]] = []

    async def fake_run_generation(job, request, **_kwargs):
        captured.append((request.workspace, request._permission_rules_authoritative))
        job.publish(SSEEvent(TEXT_DELTA, {"text": "ok"}))

    monkeypatch.setattr("app.session.task_batch.run_generation", fake_run_generation)
    job = GenerationJob("stream-1", "parent-1")
    body = TaskBatchRequest(
        session_id="parent-1",
        mode="parallel",
        tasks=[_task("One", "first"), _task("Two", "second")],
        workspace=str(workspace / "."),
    )

    await run_task_batch(
        job,
        body,
        session_factory=session_factory,
        provider_registry=MagicMock(),
        agent_registry=MagicMock(),
        tool_registry=MagicMock(),
    )

    assert captured == [(str(workspace.resolve()), True)] * 2
    async with session_factory() as db:
        rows = (await db.execute(Session.__table__.select())).mappings().all()
    assert {
        row["directory"] for row in rows if row["parent_id"] == "parent-1"
    } == {str(workspace.resolve())}


async def test_existing_parent_rejects_conflicting_batch_workspace(
    session_factory,
    monkeypatch,
    tmp_path,
) -> None:
    parent_workspace = tmp_path / "parent-workspace"
    conflicting_workspace = tmp_path / "other-workspace"
    parent_workspace.mkdir()
    conflicting_workspace.mkdir()
    async with session_factory() as db:
        async with db.begin():
            await create_session(
                db,
                id="parent-1",
                directory=str(parent_workspace),
            )

    run_generation = MagicMock()
    monkeypatch.setattr("app.session.task_batch.run_generation", run_generation)
    job = GenerationJob("stream-1", "parent-1")
    body = TaskBatchRequest(
        session_id="parent-1",
        tasks=[_task("One", "must not run")],
        workspace=str(conflicting_workspace),
    )

    await run_task_batch(
        job,
        body,
        session_factory=session_factory,
        provider_registry=MagicMock(),
        agent_registry=MagicMock(),
        tool_registry=MagicMock(),
    )

    run_generation.assert_not_called()
    assert job.events[-1].event == "agent-error"
    assert "conflicts" in job.events[-1].data["error_message"]
    async with session_factory() as db:
        rows = (await db.execute(Session.__table__.select())).mappings().all()
    assert [row for row in rows if row["parent_id"] == "parent-1"] == []


@pytest.mark.parametrize(
    "untrusted_permissions",
    [
        {},
        {
            "permission_presets": {"run_commands": True, "file_changes": True},
            "permission_rules": [
                {"action": "allow", "permission": "bash", "pattern": "*"},
            ],
        },
    ],
    ids=["omitted", "forged-allow"],
)
async def test_batch_body_cannot_bypass_parent_deny_snapshot(
    session_factory,
    monkeypatch,
    tmp_path,
    untrusted_permissions,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    parent_rules = Ruleset(rules=[
        PermissionRule(action="allow", permission="*", pattern="*"),
        PermissionRule(action="deny", permission="bash", pattern="*"),
    ])
    async with session_factory() as db:
        async with db.begin():
            parent = await create_session(
                db,
                id="parent-deny",
                directory=str(workspace),
            )
            parent.permission_snapshot = serialize_permission_snapshot(parent_rules)

    captured: list[Ruleset] = []

    async def fake_run_generation(job, request, **_kwargs):
        captured.append(Ruleset.model_validate({"rules": request.permission_rules}))
        job.publish(SSEEvent(TEXT_DELTA, {"text": "ok"}))

    monkeypatch.setattr("app.session.task_batch.run_generation", fake_run_generation)
    body = TaskBatchRequest(
        session_id="parent-deny",
        tasks=[_task("One", "must remain denied")],
        workspace=str(workspace),
        **untrusted_permissions,
    )

    await run_task_batch(
        GenerationJob("stream-deny", "parent-deny"),
        body,
        session_factory=session_factory,
        provider_registry=MagicMock(),
        agent_registry=MagicMock(),
        tool_registry=MagicMock(),
    )

    assert len(captured) == 1
    assert evaluate("bash", "*", captured[0]) == "deny"


async def test_batch_body_can_tighten_parent_allow_snapshot(
    session_factory,
    monkeypatch,
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    parent_rules = Ruleset(rules=[
        PermissionRule(action="allow", permission="*", pattern="*"),
        PermissionRule(action="allow", permission="bash", pattern="*"),
    ])
    async with session_factory() as db:
        async with db.begin():
            parent = await create_session(
                db,
                id="parent-allow",
                directory=str(workspace),
            )
            parent.permission_snapshot = serialize_permission_snapshot(parent_rules)

    captured: list[Ruleset] = []

    async def fake_run_generation(job, request, **_kwargs):
        captured.append(Ruleset.model_validate({"rules": request.permission_rules}))
        job.publish(SSEEvent(TEXT_DELTA, {"text": "ok"}))

    monkeypatch.setattr("app.session.task_batch.run_generation", fake_run_generation)
    body = TaskBatchRequest(
        session_id="parent-allow",
        tasks=[_task("One", "run with a tighter ceiling")],
        workspace=str(workspace),
        permission_rules=[
            {"action": "deny", "permission": "bash", "pattern": "*"},
        ],
    )

    await run_task_batch(
        GenerationJob("stream-tighten", "parent-allow"),
        body,
        session_factory=session_factory,
        provider_registry=MagicMock(),
        agent_registry=MagicMock(),
        tool_registry=MagicMock(),
    )

    assert len(captured) == 1
    assert evaluate("bash", "*", captured[0]) == "deny"
    assert evaluate("read", "*", captured[0]) == "allow"


async def test_legacy_permission_and_forged_allow_fall_back_to_headless_ask(
    session_factory,
    monkeypatch,
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    async with session_factory() as db:
        async with db.begin():
            parent = await create_session(
                db,
                id="legacy-parent",
                directory=str(workspace),
            )
            # This legacy field was publicly writable.  Even an allow-shaped
            # value must not become a delegation authority after upgrading.
            parent.permission = [
                {"action": "allow", "permission": "bash", "pattern": "*"},
            ]

    observed: dict[str, object] = {}

    class BashTool:
        id = "bash"

    class BashRegistry:
        def get(self, name):
            return BashTool() if name == "bash" else None

    monkeypatch.setattr(processor_module, "_persist_tool_error", AsyncMock())

    async def exercise_permission_gate(job, request, **_kwargs):
        ceiling = Ruleset.model_validate({"rules": request.permission_rules})
        observed["action"] = evaluate("bash", "*", ceiling)
        observed["interactive"] = job.interactive
        prompt = SimpleNamespace(
            job=job,
            session_factory=session_factory,
            tool_registry=BashRegistry(),
            merged_permissions=ceiling,
        )
        processor = SessionProcessor(prompt, [], "assistant-message")
        processor._init_step_state()
        await processor._handle_tool_call_chunk(SimpleNamespace(data={
            "id": "bash-call",
            "name": "bash",
            "arguments": {"command": "must-not-run"},
        }))

    monkeypatch.setattr(
        "app.session.task_batch.run_generation",
        exercise_permission_gate,
    )
    body = TaskBatchRequest(
        session_id="legacy-parent",
        tasks=[_task("One", "attempt forged command")],
        workspace=str(workspace),
        permission_presets={"run_commands": True},
        permission_rules=[
            {"action": "allow", "permission": "bash", "pattern": "*"},
        ],
    )
    job = GenerationJob(
        "stream-legacy",
        "legacy-parent",
        invocation_source="desktop",
    )

    await run_task_batch(
        job,
        body,
        session_factory=session_factory,
        provider_registry=MagicMock(),
        agent_registry=MagicMock(),
        tool_registry=MagicMock(),
    )

    assert observed == {"action": "ask", "interactive": False}
    finish = next(event for event in job.events if event.event == TASK_BATCH_FINISH)
    assert finish.data["tasks"][0]["status"] == "failed"
    assert "non-interactive tasks cannot grant permissions" in (
        finish.data["tasks"][0]["error"]
    )
