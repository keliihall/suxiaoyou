from __future__ import annotations

import asyncio

from app.config import Settings
from app.main import _shutdown_runtime, lifespan
from app.models.goal_run import GoalRun
from app.models.session import Session
from app.models.session_goal import SessionGoal
from app.storage.database import create_engine, create_session_factory
from app.storage.migrations import upgrade_sqlite_database
from app.streaming.manager import StreamManager

import pytest
from sqlalchemy import func, select


@pytest.mark.asyncio
async def test_real_startup_interrupts_goal_run_without_replaying_it(tmp_path) -> None:
    database = tmp_path / "goal-recovery.db"
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database}",
        project_dir=str(tmp_path),
        session_token_path=str(tmp_path / "session_token.json"),
        fts_enabled=False,
        channels_enabled=False,
    )
    upgrade_sqlite_database(settings.database_url)
    seed_engine = create_engine(settings)
    seed_factory = create_session_factory(seed_engine)
    async with seed_factory() as db:
        async with db.begin():
            db.add(
                Session(
                    id="restart-session",
                    directory=".",
                    title="Restart Goal",
                    version="1.0.0",
                )
            )
            db.add(
                SessionGoal(
                    id="restart-goal",
                    session_id="restart-session",
                    objective="Do not replay uncertain work",
                    status="active",
                    run_state="running",
                    revision=3,
                    token_budget=1000,
                    cost_budget_microusd=1000,
                    time_budget_seconds=1000,
                    max_continuations=10,
                    last_run_id="restart-run",
                )
            )
            db.add(
                GoalRun(
                    id="restart-run",
                    goal_id="restart-goal",
                    ordinal=1,
                    goal_revision=2,
                    idempotency_key="restart-run-request",
                    trigger="initial",
                    status="running",
                    side_effects_started=True,
                )
            )
    await seed_engine.dispose()

    app = type("App", (), {})()
    app.state = type("State", (), {"settings": settings})()
    async with lifespan(app):  # type: ignore[arg-type]
        async with app.state.session_factory() as db:
            goal = await db.get(SessionGoal, "restart-goal")
            run = await db.get(GoalRun, "restart-run")
            run_count = int(
                (
                    await db.execute(
                        select(func.count()).select_from(GoalRun).where(
                            GoalRun.goal_id == "restart-goal"
                        )
                    )
                ).scalar_one()
            )
        assert goal is not None
        assert goal.status == "blocked"
        assert goal.run_state == "interrupted"
        assert goal.blocker_code == "restart_uncertain"
        assert goal.needs_review is True
        assert run is not None and run.status == "interrupted"
        assert run_count == 1


@pytest.mark.asyncio
async def test_shutdown_persists_goal_boundary_before_waiting_for_workers(
    tmp_path,
) -> None:
    database = tmp_path / "goal-shutdown.db"
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database}",
        project_dir=str(tmp_path),
        session_token_path=str(tmp_path / "session_token.json"),
        fts_enabled=False,
        channels_enabled=False,
    )
    upgrade_sqlite_database(settings.database_url)
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    async with session_factory() as db:
        async with db.begin():
            db.add_all(
                [
                    Session(
                        id="shutdown-idle-session",
                        directory=".",
                        title="Idle Goal",
                        version="1.0.0",
                    ),
                    Session(
                        id="shutdown-running-session",
                        directory=".",
                        title="Running Goal",
                        version="1.0.0",
                    ),
                    SessionGoal(
                        id="shutdown-idle-goal",
                        session_id="shutdown-idle-session",
                        objective="Pause before shutdown",
                        status="active",
                        run_state="idle",
                        revision=4,
                    ),
                    SessionGoal(
                        id="shutdown-running-goal",
                        session_id="shutdown-running-session",
                        objective="Mark the in-flight boundary",
                        status="active",
                        run_state="running",
                        revision=7,
                    ),
                ]
            )

    stream_manager = StreamManager()
    job = stream_manager.create_job(
        "shutdown-stream",
        "shutdown-running-session",
        invocation_source="goal",
        goal_id="shutdown-running-goal",
    )
    worker_observed_boundary = asyncio.Event()
    allow_worker_exit = asyncio.Event()
    observed_states: dict[str, tuple[str, str, int, str | None]] = {}

    async def worker() -> None:
        await job.abort_event.wait()
        async with session_factory() as db:
            idle = await db.get(SessionGoal, "shutdown-idle-goal")
            running = await db.get(SessionGoal, "shutdown-running-goal")
            assert idle is not None and running is not None
            observed_states["idle"] = (
                idle.status,
                idle.run_state,
                idle.revision,
                idle.blocker_code,
            )
            observed_states["running"] = (
                running.status,
                running.run_state,
                running.revision,
                running.blocker_code,
            )
        worker_observed_boundary.set()
        await allow_worker_exit.wait()
        job.complete()

    class Background:
        async def cancel_and_wait(self) -> None:
            return None

    class ProviderRegistry:
        async def shutdown(self) -> None:
            return None

    job.task = asyncio.create_task(worker(), name="goal-shutdown-test-worker")
    shutdown_task = asyncio.create_task(
        _shutdown_runtime(
            background_tasks=Background(),
            task_scheduler=None,
            stream_manager=stream_manager,
            shutdown_timeout=2.0,
            agent_adapter=None,
            channel_manager=None,
            workspace_memory_queue=None,
            tunnel_manager=None,
            connector_registry=None,
            index_manager=None,
            ollama_manager=None,
            rapid_mlx_manager=None,
            provider_registry=ProviderRegistry(),
            engine=engine,
            session_factory=session_factory,
        ),
        name="goal-shutdown-test-runtime",
    )
    try:
        await asyncio.wait_for(worker_observed_boundary.wait(), timeout=1)
        assert observed_states == {
            "idle": ("paused", "idle", 5, "application_shutdown"),
            "running": ("active", "pausing", 8, "application_shutdown"),
        }
        assert job.execution_admission_open is False
        assert shutdown_task.done() is False

        allow_worker_exit.set()
        await asyncio.wait_for(shutdown_task, timeout=1)
        assert job.task.done()
    finally:
        allow_worker_exit.set()
        if not shutdown_task.done():
            shutdown_task.cancel()
        await asyncio.gather(shutdown_task, job.task, return_exceptions=True)
