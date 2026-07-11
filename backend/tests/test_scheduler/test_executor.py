"""Scheduled task execution status regression tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import dependencies
from app.models.scheduled_task import ScheduledTask
from app.models.task_run import TaskRun
from app.scheduler import executor
from app.streaming.events import AGENT_ERROR, SSEEvent
from app.streaming.manager import StreamManager


pytestmark = pytest.mark.asyncio


def _task_snapshot(**overrides):
    values = {
        "name": "Loop task",
        "prompt": "Do the work",
        "agent": "build",
        "model": "test-model",
        "workspace": None,
        "timeout": 30,
        "loop_max_iterations": 3,
        "loop_preset": None,
        "loop_stop_marker": "[LOOP_DONE]",
    }
    values.update(overrides)
    return values


async def _insert_task(session_factory, task_id: str) -> None:
    async with session_factory() as db:
        async with db.begin():
            db.add(
                ScheduledTask(
                    id=task_id,
                    name="Loop task",
                    description="",
                    prompt="Do the work",
                    schedule_config={"type": "interval", "minutes": 30},
                    agent="build",
                    model="test-model",
                    enabled=True,
                    run_count=0,
                    timeout_seconds=30,
                    loop_max_iterations=3,
                )
            )


async def test_loop_failure_after_success_is_persisted_as_error(
    session_factory,
    monkeypatch,
) -> None:
    await _insert_task(session_factory, "loop")
    results = iter([("success", None), ("error", "provider failed")])

    async def run_session(*_args, **_kwargs):
        return next(results)

    monkeypatch.setattr(executor, "_run_session", run_session)
    monkeypatch.setattr(executor, "_extract_session_output", lambda _session_id: "")
    monkeypatch.setattr(executor, "_set_session_title", lambda *_args, **_kwargs: None)

    session_id = await executor._execute_loop(
        "loop",
        _task_snapshot(),
        session_factory=session_factory,
        app_state=SimpleNamespace(),
        triggered_by="schedule",
    )
    assert session_id is not None

    async with session_factory() as db:
        task = (
            await db.execute(select(ScheduledTask).where(ScheduledTask.id == "loop"))
        ).scalar_one()
        runs = (
            await db.execute(
                select(TaskRun)
                .where(TaskRun.task_id == "loop")
                .order_by(TaskRun.started_at)
            )
        ).scalars().all()

    assert [run.status for run in runs] == ["success", "error"]
    assert runs[-1].error_message == "provider failed"
    assert task.last_run_status == "error"
    assert task.run_count == 2


async def test_headless_generation_registers_job_and_maps_agent_error(
    session_factory,
    monkeypatch,
) -> None:
    stream_manager = StreamManager()
    monkeypatch.setattr(dependencies, "_stream_manager", stream_manager)

    async def fail_generation(job, _request, **_kwargs) -> None:
        job.publish(SSEEvent(AGENT_ERROR, {"error_message": "provider unavailable"}))
        job.complete()

    monkeypatch.setattr(executor, "run_generation", fail_generation)
    app_state = SimpleNamespace(
        provider_registry=object(),
        agent_registry=object(),
        tool_registry=object(),
    )

    status, error = await executor._run_session(
        "automation-session",
        _task_snapshot(loop_max_iterations=None),
        session_factory=session_factory,
        app_state=app_state,
    )

    assert status == "error"
    assert error == "provider unavailable"
    jobs = list(stream_manager._jobs.values())
    assert len(jobs) == 1
    assert jobs[0].session_id == "automation-session"
    assert jobs[0].completed is True
