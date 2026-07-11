"""TaskScheduler lifecycle and catch-up regression tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models.scheduled_task import ScheduledTask
from app.models.task_run import TaskRun
from app.scheduler.engine import TaskScheduler


pytestmark = pytest.mark.asyncio


def _scheduled_task(task_id: str, **overrides) -> ScheduledTask:
    values = {
        "id": task_id,
        "name": f"Task {task_id}",
        "description": "",
        "prompt": "Do the work",
        "schedule_config": {"type": "interval", "minutes": 30},
        "agent": "build",
        "enabled": True,
        "run_count": 0,
        "timeout_seconds": 1800,
    }
    values.update(overrides)
    return ScheduledTask(**values)


async def test_start_recovers_then_catches_up_before_recompute(
    session_factory,
    monkeypatch,
) -> None:
    scheduler = TaskScheduler(session_factory, SimpleNamespace())
    calls: list[str] = []
    poll_started = asyncio.Event()

    async def recover(_reason: str) -> int:
        calls.append("recover")
        return 0

    async def catchup() -> None:
        calls.append("catchup")

    async def recompute() -> None:
        calls.append("recompute")

    async def poll() -> None:
        poll_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(scheduler, "_recover_interrupted_runs", recover)
    monkeypatch.setattr(scheduler, "_catchup_missed", catchup)
    monkeypatch.setattr(scheduler, "_recompute_all_next_run", recompute)
    monkeypatch.setattr(scheduler, "_poll_loop", poll)

    await scheduler.start()
    await poll_started.wait()
    assert calls == ["recover", "catchup", "recompute"]

    assert scheduler._task is not None
    scheduler._task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await scheduler._task


async def test_catchup_launches_recent_missed_run_with_catchup_reason(
    session_factory,
    monkeypatch,
) -> None:
    missed_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
    async with session_factory() as db:
        async with db.begin():
            db.add(_scheduled_task("missed", next_run_at=missed_at))

    scheduler = TaskScheduler(session_factory, SimpleNamespace())
    launched: list[tuple[str, str, str]] = []

    def launch(task_id: str, name: str, *, triggered_by: str) -> None:
        launched.append((task_id, name, triggered_by))

    monkeypatch.setattr(scheduler, "_launch_execution", launch)
    await scheduler._catchup_missed()

    assert launched == [("missed", "Task missed", "startup_catchup")]


async def test_catchup_reservation_preserves_due_time_until_execution_starts(
    session_factory,
    monkeypatch,
) -> None:
    missed_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
    async with session_factory() as db:
        async with db.begin():
            db.add(_scheduled_task("crash-window", next_run_at=missed_at))

    first_process = TaskScheduler(session_factory, SimpleNamespace())

    def reserve_without_start(task_id: str, _name: str, *, triggered_by: str) -> None:
        assert triggered_by == "startup_catchup"
        first_process._running_tasks.add(task_id)

    monkeypatch.setattr(first_process, "_launch_execution", reserve_without_start)
    await first_process._catchup_missed()
    await first_process._recompute_all_next_run()

    async with session_factory() as db:
        stored = await db.get(ScheduledTask, "crash-window")
        assert stored is not None
        assert stored.next_run_at == missed_at
        runs = list((await db.execute(select(TaskRun))).scalars().all())
        assert runs == []

    # Simulate a crash before the background coroutine creates TaskRun. A fresh
    # process has no in-memory reservation and must discover the same due time.
    restarted = TaskScheduler(session_factory, SimpleNamespace())
    relaunched: list[str] = []

    def launch_after_restart(task_id: str, _name: str, *, triggered_by: str) -> None:
        assert triggered_by == "startup_catchup"
        relaunched.append(task_id)

    monkeypatch.setattr(restarted, "_launch_execution", launch_after_restart)
    await restarted._catchup_missed()

    assert relaunched == ["crash-window"]


async def test_recover_interrupted_runs_closes_run_and_task_status(
    session_factory,
) -> None:
    started_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    async with session_factory() as db:
        async with db.begin():
            db.add(
                _scheduled_task(
                    "stale",
                    last_run_status="running:2/4",
                    last_session_id="session",
                )
            )
            db.add(
                TaskRun(
                    id="run",
                    task_id="stale",
                    session_id="session",
                    status="running",
                    started_at=started_at,
                    triggered_by="schedule",
                )
            )

    scheduler = TaskScheduler(session_factory, SimpleNamespace())
    recovered = await scheduler._recover_interrupted_runs("interrupted for test")
    assert recovered == 1

    async with session_factory() as db:
        run = (
            await db.execute(select(TaskRun).where(TaskRun.id == "run"))
        ).scalar_one()
        task = (
            await db.execute(select(ScheduledTask).where(ScheduledTask.id == "stale"))
        ).scalar_one()

    assert run.status == "error"
    assert run.error_message == "interrupted for test"
    assert run.finished_at is not None
    assert task.last_run_status == "error"
    assert task.last_run_at is not None


async def test_stop_cancels_tracked_execution_and_recovers_status(
    session_factory,
) -> None:
    async with session_factory() as db:
        async with db.begin():
            db.add(_scheduled_task("active", last_run_status="running"))
            db.add(
                TaskRun(
                    id="active-run",
                    task_id="active",
                    session_id="session",
                    status="running",
                    started_at=datetime.now(timezone.utc),
                    triggered_by="manual",
                )
            )

    started = asyncio.Event()

    async def hang() -> None:
        started.set()
        await asyncio.Event().wait()

    execution = asyncio.create_task(hang())
    await started.wait()

    scheduler = TaskScheduler(session_factory, SimpleNamespace())
    scheduler._execution_tasks.add(execution)
    scheduler._running_tasks.add("active")
    await scheduler.stop()

    assert execution.cancelled()
    assert scheduler._execution_tasks == set()
    assert scheduler._running_tasks == set()

    async with session_factory() as db:
        run = (
            await db.execute(select(TaskRun).where(TaskRun.id == "active-run"))
        ).scalar_one()
        task = (
            await db.execute(select(ScheduledTask).where(ScheduledTask.id == "active"))
        ).scalar_one()

    assert run.status == "error"
    assert run.error_message == "Application stopped before automation completed"
    assert task.last_run_status == "error"


async def test_background_execution_respects_concurrency_limit(
    session_factory,
    monkeypatch,
) -> None:
    from app.scheduler import engine

    scheduler = TaskScheduler(session_factory, SimpleNamespace())
    scheduler._execution_slots = asyncio.Semaphore(1)
    release = asyncio.Event()
    first_started = asyncio.Event()
    started = 0
    active = 0
    peak_active = 0

    async def execute(*_args, **_kwargs) -> None:
        nonlocal started, active, peak_active
        started += 1
        active += 1
        peak_active = max(peak_active, active)
        first_started.set()
        await release.wait()
        active -= 1

    monkeypatch.setattr(engine, "execute_scheduled_task", execute)
    first = asyncio.create_task(
        scheduler._execute_and_reschedule("one", "One")
    )
    second = asyncio.create_task(
        scheduler._execute_and_reschedule("two", "Two")
    )

    await first_started.wait()
    await asyncio.sleep(0)
    assert started == 1
    assert peak_active == 1

    release.set()
    await asyncio.gather(first, second)
    assert started == 2
    assert peak_active == 1
