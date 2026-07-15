from __future__ import annotations

import pytest
from sqlalchemy import select

from app import release_features
from app.agent.permission import (
    evaluate,
    serialize_permission_snapshot,
)
from app.models.goal_run import GoalRun
from app.models.session import Session
from app.models.session_goal import SessionGoal
from app.models.session_input import SessionInput
from app.schemas.agent import PermissionRule, Ruleset
from app.schemas.chat import PromptRequest
from app.schemas.goal import GoalControlRequest, GoalCreateRequest
from app.session.goal_controller import (
    GoalSliceResult,
    _request_for_continuation,
    run_goal_generation,
)
from app.session.goal_manager import (
    create_session_goal,
    get_goal_by_id,
    pause_session_goal,
    reserve_goal_run,
    transition_goal_status,
)
from app.session.input_queue import enqueue_session_input
from app.streaming.events import AGENT_ERROR, DONE
from app.streaming.manager import StreamManager


pytestmark = pytest.mark.asyncio


async def _admit(session_factory, *, token_budget=1000, max_continuations=8):
    async with session_factory() as db:
        async with db.begin():
            db.add(
                Session(
                    id="goal-controller-session",
                    directory=".",
                    title="Goal controller",
                    version="1.0.0",
                )
            )
            await db.flush()
            goal = await create_session_goal(
                db,
                "goal-controller-session",
                GoalCreateRequest(
                    client_request_id="controller-create",
                    objective="Finish through autonomous slices",
                    token_budget=token_budget,
                    max_continuations=max_continuations,
                ),
            )
            reservation = await reserve_goal_run(
                db,
                goal_id=goal.id,
                expected_revision=goal.revision,
                idempotency_key="controller-initial-run",
                trigger="initial",
                stream_id="goal-controller-stream",
            )
            return goal.id, reservation.run.id


def _job(sm: StreamManager, goal_id: str, run_id: str):
    job = sm.create_job(
        "goal-controller-stream",
        "goal-controller-session",
        invocation_source="goal",
        invocation_source_id="test",
        goal_id=goal_id,
        goal_run_id=run_id,
    )
    job.interactive = True
    return job


def _slice(*, tokens=1, finish_reason="stop") -> GoalSliceResult:
    return GoalSliceResult(
        tokens_used=tokens,
        cost_used_microusd=0,
        active_seconds=0,
        finish_reason=finish_reason,
        total_cost=0.0,
    )


async def test_continuation_intersects_old_goal_allow_with_current_session_deny() -> None:
    goal = SessionGoal(
        id="goal-permission-intersection",
        session_id="goal-permission-session",
        objective="Respect the latest permission ceiling",
        agent="build",
        language="zh",
        permission_snapshot=serialize_permission_snapshot(
            Ruleset(rules=[
                PermissionRule(action="allow", permission="*"),
                PermissionRule(action="allow", permission="web_search"),
            ])
        ),
    )
    session = Session(
        id=goal.session_id,
        directory=".",
        permission_snapshot=serialize_permission_snapshot(
            Ruleset(rules=[
                PermissionRule(action="allow", permission="*"),
                PermissionRule(action="deny", permission="web_search"),
            ])
        ),
    )

    request = _request_for_continuation(goal, session, item=None)

    assert request._trusted_permission_ruleset is not None
    assert evaluate(
        "web_search", "*", request._trusted_permission_ruleset
    ) == "deny"


async def test_continuation_with_invalid_goal_snapshot_fails_closed() -> None:
    goal = SessionGoal(
        id="goal-invalid-permission-snapshot",
        session_id="goal-invalid-permission-session",
        objective="Do not recover authority from an invalid snapshot",
        agent="build",
        language="zh",
        permission_snapshot={
            "version": 999,
            "kind": "effective_permission_snapshot",
            "rules": [
                {"action": "allow", "permission": "*", "pattern": "*"},
            ],
        },
    )
    session = Session(
        id=goal.session_id,
        directory=".",
        permission_snapshot=serialize_permission_snapshot(
            Ruleset(rules=[
                PermissionRule(action="allow", permission="*"),
            ])
        ),
    )

    request = _request_for_continuation(goal, session, item=None)

    assert request._trusted_permission_ruleset is not None
    assert evaluate("bash", "*", request._trusted_permission_ruleset) == "deny"
    assert evaluate(
        "web_search", "*", request._trusted_permission_ruleset
    ) == "deny"


async def test_goal_runs_three_slices_then_completes_with_one_done(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release_features, "AUTONOMOUS_GOALS_RELEASED", True)
    goal_id, run_id = await _admit(session_factory)
    sm = StreamManager()
    job = _job(sm, goal_id, run_id)
    calls = 0

    async def fake_slice(job, request, **kwargs):
        nonlocal calls
        del request, kwargs
        calls += 1
        async with session_factory() as db:
            async with db.begin():
                run = await db.get(GoalRun, job.goal_run_id)
                assert run is not None
                run.side_effects_started = True
                if calls == 3:
                    goal = await get_goal_by_id(db, goal_id)
                    assert goal is not None
                    await transition_goal_status(
                        db,
                        goal_id=goal.id,
                        expected_revision=goal.revision,
                        target_status="complete",
                        completion_summary="All checks passed",
                        completion_evidence=[{"kind": "test", "passed": True}],
                    )
        return _slice()

    monkeypatch.setattr(
        "app.session.goal_controller._execute_goal_slice",
        fake_slice,
    )
    await run_goal_generation(
        job,
        PromptRequest(session_id=job.session_id, text="start"),
        initial_run_id=run_id,
        stream_manager=sm,
        session_factory=session_factory,
        provider_registry=object(),
        agent_registry=object(),
        tool_registry=object(),
    )

    async with session_factory() as db:
        goal = await get_goal_by_id(db, goal_id)
        runs = list(
            (
                await db.execute(
                    select(GoalRun)
                    .where(GoalRun.goal_id == goal_id)
                    .order_by(GoalRun.ordinal)
                )
            ).scalars()
        )
    assert goal is not None and goal.status == "complete"
    assert [run.trigger for run in runs] == ["initial", "auto", "auto"]
    assert [event.event for event in job.events].count(DONE) == 1
    assert job.completed is True


async def test_real_user_input_is_claimed_before_auto_continuation(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release_features, "AUTONOMOUS_GOALS_RELEASED", True)
    goal_id, run_id = await _admit(session_factory)
    sm = StreamManager()
    job = _job(sm, goal_id, run_id)
    seen_text: list[str] = []

    async def fake_slice(job, request, **kwargs):
        del kwargs
        seen_text.append(request.text)
        async with session_factory() as db:
            async with db.begin():
                run = await db.get(GoalRun, job.goal_run_id)
                assert run is not None
                run.side_effects_started = True
                if len(seen_text) == 1:
                    await enqueue_session_input(
                        db,
                        session_id=job.session_id,
                        client_request_id="priority-user-input",
                        mode="queue",
                        text="user correction wins",
                        attachments=[],
                        model_id=None,
                        provider_id=None,
                        agent="build",
                        language="zh",
                        workspace=None,
                        reasoning=None,
                        permission_presets=None,
                        permission_rules=None,
                        target_stream_id=None,
                    )
                else:
                    goal = await get_goal_by_id(db, goal_id)
                    assert goal is not None
                    await transition_goal_status(
                        db,
                        goal_id=goal.id,
                        expected_revision=goal.revision,
                        target_status="complete",
                        completion_summary="Applied the correction",
                        completion_evidence=[{"kind": "user_input", "applied": True}],
                    )
        return _slice()

    monkeypatch.setattr(
        "app.session.goal_controller._execute_goal_slice",
        fake_slice,
    )
    await run_goal_generation(
        job,
        PromptRequest(session_id=job.session_id, text="start"),
        initial_run_id=run_id,
        stream_manager=sm,
        session_factory=session_factory,
        provider_registry=object(),
        agent_registry=object(),
        tool_registry=object(),
    )

    async with session_factory() as db:
        runs = list(
            (
                await db.execute(
                    select(GoalRun)
                    .where(GoalRun.goal_id == goal_id)
                    .order_by(GoalRun.ordinal)
                )
            ).scalars()
        )
        queued = (
            await db.execute(
                select(SessionInput).where(
                    SessionInput.client_request_id == "priority-user-input"
                )
            )
        ).scalar_one()
    assert seen_text == ["start", "user correction wins"]
    assert [run.trigger for run in runs] == ["initial", "user_input"]
    assert queued.status == "consumed"


async def test_hard_token_budget_stops_before_an_auto_run(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release_features, "AUTONOMOUS_GOALS_RELEASED", True)
    goal_id, run_id = await _admit(session_factory, token_budget=1)
    sm = StreamManager()
    job = _job(sm, goal_id, run_id)
    calls = 0

    async def fake_slice(*args, **kwargs):
        nonlocal calls
        del args, kwargs
        calls += 1
        return _slice(tokens=1)

    monkeypatch.setattr(
        "app.session.goal_controller._execute_goal_slice",
        fake_slice,
    )
    await run_goal_generation(
        job,
        PromptRequest(session_id=job.session_id, text="start"),
        initial_run_id=run_id,
        stream_manager=sm,
        session_factory=session_factory,
        provider_registry=object(),
        agent_registry=object(),
        tool_registry=object(),
    )

    async with session_factory() as db:
        goal = await get_goal_by_id(db, goal_id)
        run_count = len(
            list(
                (
                    await db.execute(select(GoalRun).where(GoalRun.goal_id == goal_id))
                ).scalars()
            )
        )
    assert calls == 1
    assert run_count == 1
    assert goal is not None and goal.status == "budget_limited"
    assert goal.tokens_used == 1


async def test_manual_goal_runs_one_explicit_slice_then_pauses(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release_features, "AUTONOMOUS_GOALS_RELEASED", False)
    goal_id, run_id = await _admit(session_factory)
    sm = StreamManager()
    job = _job(sm, goal_id, run_id)
    calls = 0

    async def fake_slice(*args, **kwargs):
        nonlocal calls
        del args, kwargs
        calls += 1
        return _slice()

    monkeypatch.setattr(
        "app.session.goal_controller._execute_goal_slice",
        fake_slice,
    )
    await run_goal_generation(
        job,
        PromptRequest(session_id=job.session_id, text="start"),
        initial_run_id=run_id,
        stream_manager=sm,
        session_factory=session_factory,
        provider_registry=object(),
        agent_registry=object(),
        tool_registry=object(),
    )

    async with session_factory() as db:
        goal = await get_goal_by_id(db, goal_id)
        runs = list(
            (await db.execute(select(GoalRun).where(GoalRun.goal_id == goal_id)))
            .scalars()
        )
    assert calls == 1
    assert len(runs) == 1 and runs[0].trigger == "initial"
    assert goal is not None
    assert (goal.status, goal.run_state) == ("paused", "idle")
    assert goal.blocker_code == "manual_goal_turn_complete"


async def test_security_stop_reconciles_goal_before_closing_shared_stream(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An inner SessionPrompt must not terminate the controller-owned SSE."""

    monkeypatch.setattr(release_features, "AUTONOMOUS_GOALS_RELEASED", True)

    class _StoppedControl:
        emergency_stop = True

    monkeypatch.setattr(
        "app.security.control.get_security_control",
        lambda: _StoppedControl(),
    )
    goal_id, run_id = await _admit(session_factory)
    sm = StreamManager()
    job = _job(sm, goal_id, run_id)
    subscriber = job.subscribe()

    await run_goal_generation(
        job,
        PromptRequest(session_id=job.session_id, text="start"),
        initial_run_id=run_id,
        stream_manager=sm,
        session_factory=session_factory,
        provider_registry=object(),
        agent_registry=object(),
        tool_registry=object(),
    )

    delivered: list[str | None] = []
    while not subscriber.empty():
        event = subscriber.get_nowait()
        delivered.append(None if event is None else event.event)

    assert AGENT_ERROR in delivered
    assert DONE in delivered
    assert None in delivered
    assert delivered.index(DONE) < delivered.index(None)
    async with session_factory() as db:
        goal = await get_goal_by_id(db, goal_id)
        run = await db.get(GoalRun, run_id)
    assert goal is not None and goal.status == "blocked"
    assert goal.needs_review is True
    assert run is not None and run.status == "failed"


async def test_safe_pause_finishes_current_run_without_starting_another(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release_features, "AUTONOMOUS_GOALS_RELEASED", True)
    goal_id, run_id = await _admit(session_factory)
    sm = StreamManager()
    job = _job(sm, goal_id, run_id)

    async def fake_slice(*args, **kwargs):
        del args, kwargs
        async with session_factory() as db:
            async with db.begin():
                goal = await get_goal_by_id(db, goal_id)
                assert goal is not None
                await pause_session_goal(
                    db,
                    goal.session_id,
                    GoalControlRequest(
                        client_request_id="controller-safe-pause",
                        expected_revision=goal.revision,
                    ),
                )
        return _slice()

    monkeypatch.setattr(
        "app.session.goal_controller._execute_goal_slice",
        fake_slice,
    )
    await run_goal_generation(
        job,
        PromptRequest(session_id=job.session_id, text="start"),
        initial_run_id=run_id,
        stream_manager=sm,
        session_factory=session_factory,
        provider_registry=object(),
        agent_registry=object(),
        tool_registry=object(),
    )

    async with session_factory() as db:
        goal = await get_goal_by_id(db, goal_id)
        runs = list(
            (
                await db.execute(select(GoalRun).where(GoalRun.goal_id == goal_id))
            ).scalars()
        )
    assert goal is not None
    assert (goal.status, goal.run_state, goal.needs_review) == ("paused", "idle", False)
    assert len(runs) == 1 and runs[0].status == "completed"


async def test_edit_between_reservation_and_start_requeues_a_fresh_revision(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.schemas.goal import GoalUpdateRequest
    from app.session import goal_controller as controller_module
    from app.session.goal_manager import update_session_goal

    monkeypatch.setattr(release_features, "AUTONOMOUS_GOALS_RELEASED", True)
    goal_id, run_id = await _admit(session_factory)
    sm = StreamManager()
    job = _job(sm, goal_id, run_id)
    executed_runs: list[str] = []

    # The API commits the edit in its own transaction after reservation and
    # before the worker reaches start_goal_run.
    job.close_execution_admission()
    async with session_factory() as db:
        async with db.begin():
            goal = await get_goal_by_id(db, goal_id)
            assert goal is not None
            await update_session_goal(
                db,
                goal.session_id,
                GoalUpdateRequest(
                    client_request_id="edit-before-run-start",
                    expected_revision=goal.revision,
                    objective="Execute only the revised Goal",
                ),
            )

    async def fake_slice(job, request, **kwargs):
        del request, kwargs
        assert job.goal_run_id is not None
        executed_runs.append(job.goal_run_id)
        async with session_factory() as db:
            async with db.begin():
                goal = await get_goal_by_id(db, goal_id)
                assert goal is not None
                assert goal.objective == "Execute only the revised Goal"
                await transition_goal_status(
                    db,
                    goal_id=goal.id,
                    expected_revision=goal.revision,
                    target_status="complete",
                    completion_summary="Revised Goal executed",
                    completion_evidence=[{"kind": "revision", "verified": True}],
                )
        return _slice()

    monkeypatch.setattr(controller_module, "_execute_goal_slice", fake_slice)
    await run_goal_generation(
        job,
        PromptRequest(session_id=job.session_id, text="start"),
        initial_run_id=run_id,
        stream_manager=sm,
        session_factory=session_factory,
        provider_registry=object(),
        agent_registry=object(),
        tool_registry=object(),
    )

    async with session_factory() as db:
        goal = await get_goal_by_id(db, goal_id)
        runs = list(
            (
                await db.execute(
                    select(GoalRun)
                    .where(GoalRun.goal_id == goal_id)
                    .order_by(GoalRun.ordinal)
                )
            ).scalars()
        )
    assert goal is not None and goal.status == "complete"
    assert len(runs) == 2
    assert runs[0].id == run_id and runs[0].side_effects_started is False
    assert executed_runs == [runs[1].id]
    assert [run.status for run in runs] == ["completed", "completed"]


async def test_three_text_only_slices_trip_no_progress_breaker(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release_features, "AUTONOMOUS_GOALS_RELEASED", True)
    goal_id, run_id = await _admit(session_factory)
    sm = StreamManager()
    job = _job(sm, goal_id, run_id)
    calls = 0

    async def fake_slice(*args, **kwargs):
        nonlocal calls
        del args, kwargs
        calls += 1
        return _slice()

    monkeypatch.setattr(
        "app.session.goal_controller._execute_goal_slice",
        fake_slice,
    )
    await run_goal_generation(
        job,
        PromptRequest(session_id=job.session_id, text="start"),
        initial_run_id=run_id,
        stream_manager=sm,
        session_factory=session_factory,
        provider_registry=object(),
        agent_registry=object(),
        tool_registry=object(),
    )

    async with session_factory() as db:
        goal = await get_goal_by_id(db, goal_id)
    assert calls == 3
    assert goal is not None
    assert goal.status == "blocked"
    assert goal.blocker_code == "no_progress"
    assert goal.no_progress_count == 3


async def test_retryable_provider_failures_trip_consecutive_error_breaker(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release_features, "AUTONOMOUS_GOALS_RELEASED", True)
    goal_id, run_id = await _admit(session_factory)
    sm = StreamManager()
    job = _job(sm, goal_id, run_id)
    calls = 0

    async def failing_slice(*args, **kwargs):
        nonlocal calls
        del args, kwargs
        calls += 1
        return GoalSliceResult(
            tokens_used=0,
            cost_used_microusd=0,
            active_seconds=1,
            finish_reason="error",
            total_cost=0.0,
            agent_error="LLM stream error: HTTP 503 provider unavailable",
        )

    monkeypatch.setattr(
        "app.session.goal_controller._execute_goal_slice",
        failing_slice,
    )
    await run_goal_generation(
        job,
        PromptRequest(session_id=job.session_id, text="start"),
        initial_run_id=run_id,
        stream_manager=sm,
        session_factory=session_factory,
        provider_registry=object(),
        agent_registry=object(),
        tool_registry=object(),
    )

    async with session_factory() as db:
        goal = await get_goal_by_id(db, goal_id)
        runs = list(
            (await db.execute(select(GoalRun).where(GoalRun.goal_id == goal_id)))
            .scalars()
        )
    assert calls == 3
    assert len(runs) == 3 and all(run.status == "failed" for run in runs)
    assert goal is not None and goal.status == "blocked"
    assert goal.blocker_code == "generation_error"
    assert goal.consecutive_error_count == 3


async def test_provider_rate_limit_moves_goal_to_usage_limited_without_looping(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release_features, "AUTONOMOUS_GOALS_RELEASED", True)
    goal_id, run_id = await _admit(session_factory)
    sm = StreamManager()
    job = _job(sm, goal_id, run_id)
    calls = 0

    async def rate_limited_slice(*args, **kwargs):
        nonlocal calls
        del args, kwargs
        calls += 1
        return GoalSliceResult(
            tokens_used=0,
            cost_used_microusd=0,
            active_seconds=1,
            finish_reason="error",
            total_cost=0.0,
            agent_error="LLM stream error: HTTP 429 rate limit exceeded",
        )

    monkeypatch.setattr(
        "app.session.goal_controller._execute_goal_slice",
        rate_limited_slice,
    )
    await run_goal_generation(
        job,
        PromptRequest(session_id=job.session_id, text="start"),
        initial_run_id=run_id,
        stream_manager=sm,
        session_factory=session_factory,
        provider_registry=object(),
        agent_registry=object(),
        tool_registry=object(),
    )

    async with session_factory() as db:
        goal = await get_goal_by_id(db, goal_id)
        runs = list(
            (await db.execute(select(GoalRun).where(GoalRun.goal_id == goal_id)))
            .scalars()
        )
    assert calls == 1
    assert len(runs) == 1 and runs[0].status == "failed"
    assert goal is not None and goal.status == "usage_limited"
    assert goal.blocker_code == "provider_usage_limited"


async def test_read_only_tool_does_not_reset_durable_progress_breaker(
    session_factory,
) -> None:
    from app.models.message import Message, Part
    from app.session.goal_controller import _made_durable_progress
    from app.session.goal_manager import start_goal_run

    goal_id, run_id = await _admit(session_factory)
    async with session_factory() as db:
        async with db.begin():
            await start_goal_run(db, run_id)
            run = await db.get(GoalRun, run_id)
            assert run is not None
            run.side_effects_started = True
            message = Message(
                id="read-only-progress-message",
                session_id="goal-controller-session",
                data={"role": "assistant"},
            )
            db.add(message)
            await db.flush()
            db.add(
                Part(
                    id="read-only-progress-part",
                    message_id=message.id,
                    session_id="goal-controller-session",
                    data={
                        "type": "tool",
                        "tool": "read",
                        "state": {"status": "completed"},
                    },
                )
            )

    assert await _made_durable_progress(
        session_factory,
        session_id="goal-controller-session",
        goal_id=goal_id,
        run_id=run_id,
    ) is False

    async with session_factory() as db:
        async with db.begin():
            message = await db.get(Message, "read-only-progress-message")
            assert message is not None
            db.add(
                Part(
                    id="write-progress-part",
                    message_id=message.id,
                    session_id="goal-controller-session",
                    data={
                        "type": "tool",
                        "tool": "write",
                        "state": {"status": "completed"},
                    },
                )
            )

    assert await _made_durable_progress(
        session_factory,
        session_id="goal-controller-session",
        goal_id=goal_id,
        run_id=run_id,
    ) is True


async def test_noop_command_and_todo_bookkeeping_are_not_durable_progress(
    session_factory,
) -> None:
    from app.models.message import Message, Part
    from app.session.goal_controller import _made_durable_progress
    from app.session.goal_manager import start_goal_run

    goal_id, run_id = await _admit(session_factory)
    async with session_factory() as db:
        async with db.begin():
            await start_goal_run(db, run_id)
            message = Message(
                id="no-op-command-message",
                session_id="goal-controller-session",
                data={"role": "assistant"},
            )
            db.add(message)
            await db.flush()
            db.add_all([
                Part(
                    id="no-op-bash-part",
                    message_id=message.id,
                    session_id="goal-controller-session",
                    data={
                        "type": "tool",
                        "tool": "bash",
                        "state": {
                            "status": "completed",
                            "metadata": {
                                "exit_code": 0,
                                "written_files": [],
                                "deleted_files": [],
                            },
                        },
                    },
                ),
                Part(
                    id="todo-bookkeeping-part",
                    message_id=message.id,
                    session_id="goal-controller-session",
                    data={
                        "type": "tool",
                        "tool": "todo",
                        "state": {"status": "completed"},
                    },
                ),
            ])

    assert await _made_durable_progress(
        session_factory,
        session_id="goal-controller-session",
        goal_id=goal_id,
        run_id=run_id,
    ) is False

    async with session_factory() as db:
        async with db.begin():
            message = await db.get(Message, "no-op-command-message")
            assert message is not None
            db.add(Part(
                id="artifact-bash-part",
                message_id=message.id,
                session_id="goal-controller-session",
                data={
                    "type": "tool",
                    "tool": "bash",
                    "state": {
                        "status": "completed",
                        "metadata": {
                            "written_files": ["/workspace/result.mp3"],
                            "deleted_files": [],
                        },
                    },
                },
            ))

    assert await _made_durable_progress(
        session_factory,
        session_id="goal-controller-session",
        goal_id=goal_id,
        run_id=run_id,
    ) is True
