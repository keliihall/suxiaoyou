from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import release_features
from app.models.goal_run import GoalRun
from app.models.message import Message, Part
from app.models.session import Session
from app.models.todo import Todo
from app.schemas.goal import (
    GoalControlRequest,
    GoalCreateRequest,
    GoalUpdateRequest,
)
from app.session.goal_manager import (
    AutonomousGoalsUnavailableError,
    GoalIdempotencyConflictError,
    GoalInvalidTransitionError,
    GoalRevisionConflictError,
    GoalRunConflictError,
    clear_session_goal,
    create_session_goal,
    finish_goal_run,
    get_goal_token_usage_breakdown,
    get_session_goal,
    interrupt_inflight_goal_runs,
    pause_session_goal,
    pause_active_goal_for_archive,
    record_goal_run_usage,
    reserve_goal_run,
    resume_session_goal,
    start_goal_run,
    transition_goal_status,
    update_session_goal,
)


async def _session(db: AsyncSession, session_id: str = "goal-session") -> Session:
    session = Session(
        id=session_id,
        directory=".",
        title="Goal test",
        version="1.0.0",
        permission_snapshot={"filesystem_write": False},
    )
    db.add(session)
    await db.flush()
    return session


def _create_body(**overrides) -> GoalCreateRequest:
    values = {
        "client_request_id": "goal-create-request",
        "objective": "Ship a verified Goal control plane",
        "definition_of_done": "Tests pass",
    }
    values.update(overrides)
    return GoalCreateRequest(**values)


@pytest.mark.asyncio
async def test_create_update_pause_resume_clear_are_durable_and_idempotent(
    db: AsyncSession,
) -> None:
    await _session(db)
    created = await create_session_goal(db, "goal-session", _create_body())
    replay = await create_session_goal(db, "goal-session", _create_body())
    assert replay.id == created.id
    assert created.revision == 1
    assert created.token_budget is None
    assert created.cost_budget_microusd == 5_000_000
    assert created.time_budget_seconds == 14_400
    assert created.max_continuations == 64
    assert created.permission_snapshot == {"filesystem_write": False}

    with pytest.raises(GoalIdempotencyConflictError):
        await create_session_goal(
            db,
            "goal-session",
            _create_body(objective="A different request"),
        )

    updated = await update_session_goal(
        db,
        "goal-session",
        GoalUpdateRequest(
            client_request_id="goal-update-request",
            expected_revision=1,
            definition_of_done="Migration and API tests pass",
        ),
    )
    assert updated.revision == 2
    assert updated.definition_of_done == "Migration and API tests pass"
    with pytest.raises(GoalRevisionConflictError):
        await update_session_goal(
            db,
            "goal-session",
            GoalUpdateRequest(
                client_request_id="stale-update-request",
                expected_revision=1,
                objective="Silently overwrite",
            ),
        )

    paused = await pause_session_goal(
        db,
        "goal-session",
        GoalControlRequest(
            client_request_id="goal-pause-request",
            expected_revision=2,
        ),
    )
    assert (paused.status, paused.run_state, paused.revision) == ("paused", "idle", 3)
    resumed = await resume_session_goal(
        db,
        "goal-session",
        GoalControlRequest(
            client_request_id="goal-resume-request",
            expected_revision=3,
        ),
    )
    assert (resumed.status, resumed.run_state, resumed.revision) == ("active", "idle", 4)

    db.add(
        Todo(
            session_id="goal-session",
            goal_id=created.id,
            content="Goal-only step",
            active_form="Working",
        )
    )
    await db.flush()
    clear = GoalControlRequest(
        client_request_id="goal-clear-request",
        expected_revision=4,
    )
    assert await clear_session_goal(db, "goal-session", clear) is True
    assert await clear_session_goal(db, "goal-session", clear) is False
    assert await get_session_goal(db, "goal-session") is None
    assert (await db.execute(select(Todo))).scalars().all() == []


@pytest.mark.asyncio
async def test_content_limit_is_enforced_for_create_and_partial_update(
    db: AsyncSession,
) -> None:
    with pytest.raises(ValidationError):
        _create_body(objective="x" * 3000, definition_of_done="y" * 1001)

    await _session(db)
    await create_session_goal(
        db,
        "goal-session",
        _create_body(objective="x" * 2500, definition_of_done="y" * 1000),
    )
    with pytest.raises(Exception, match="4000 characters"):
        await update_session_goal(
            db,
            "goal-session",
            GoalUpdateRequest(
                client_request_id="goal-too-long-update",
                expected_revision=1,
                definition_of_done="z" * 1600,
            ),
        )


@pytest.mark.asyncio
async def test_budget_limited_goal_can_resume_after_legal_budget_increase(
    db: AsyncSession,
) -> None:
    await _session(db)
    goal = await create_session_goal(
        db,
        "goal-session",
        _create_body(token_budget=100),
    )
    goal.tokens_used = 100
    await db.flush()
    limited = await transition_goal_status(
        db,
        goal_id=goal.id,
        expected_revision=1,
        target_status="budget_limited",
        blocker_code="token_budget",
    )
    raised = await update_session_goal(
        db,
        "goal-session",
        GoalUpdateRequest(
            client_request_id="raise-exhausted-budget",
            expected_revision=limited.revision,
            token_budget=101,
        ),
    )
    assert (raised.status, raised.token_budget, raised.revision) == (
        "budget_limited",
        101,
        3,
    )
    resumed = await resume_session_goal(
        db,
        "goal-session",
        GoalControlRequest(
            client_request_id="resume-raised-budget",
            expected_revision=raised.revision,
        ),
    )
    assert (resumed.status, resumed.run_state, resumed.revision) == (
        "active",
        "idle",
        4,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target,kwargs",
    [
        ("paused", {}),
        ("blocked", {"blocker_code": "needs_user"}),
        ("usage_limited", {"blocker_code": "provider_limit"}),
        ("budget_limited", {"blocker_code": "token_budget"}),
        (
            "complete",
            {
                "completion_summary": "Verified",
                "completion_evidence": [{"kind": "test", "passed": True}],
            },
        ),
    ],
)
async def test_status_transition_table_allows_terminal_and_resume_paths(
    db: AsyncSession,
    target: str,
    kwargs: dict,
) -> None:
    await _session(db)
    goal = await create_session_goal(db, "goal-session", _create_body())
    stopped = await transition_goal_status(
        db,
        goal_id=goal.id,
        expected_revision=1,
        target_status=target,
        **kwargs,
    )
    assert stopped.status == target
    active = await transition_goal_status(
        db,
        goal_id=goal.id,
        expected_revision=2,
        target_status="active",
    )
    assert (active.status, active.revision) == ("active", 3)


@pytest.mark.asyncio
async def test_status_transition_rejects_illegal_edge(db: AsyncSession) -> None:
    await _session(db)
    goal = await create_session_goal(db, "goal-session", _create_body())
    paused = await transition_goal_status(
        db,
        goal_id=goal.id,
        expected_revision=1,
        target_status="paused",
    )
    with pytest.raises(GoalInvalidTransitionError):
        await transition_goal_status(
            db,
            goal_id=goal.id,
            expected_revision=paused.revision,
            target_status="blocked",
        )


@pytest.mark.asyncio
async def test_goal_run_ledger_is_gated_reserved_once_and_reconciles_usage(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        release_features, "AUTONOMOUS_GOALS_RELEASED", False
    )
    await _session(db)
    goal = await create_session_goal(db, "goal-session", _create_body())
    # Explicit initial/resume/user-input slices remain available with only the
    # persistent Goal gate.  The autonomous gate protects unattended loops.
    with pytest.raises(AutonomousGoalsUnavailableError):
        await reserve_goal_run(
            db,
            goal_id=goal.id,
            expected_revision=1,
            idempotency_key="goal-auto-run-request",
            trigger="auto",
        )

    reserved = await reserve_goal_run(
        db,
        goal_id=goal.id,
        expected_revision=1,
        idempotency_key="goal-run-request",
        trigger="initial",
        stream_id="goal-stream",
    )
    replay = await reserve_goal_run(
        db,
        goal_id=goal.id,
        expected_revision=1,
        idempotency_key="goal-run-request",
        trigger="initial",
        stream_id="goal-stream",
    )
    assert replay.idempotent is True
    assert replay.run.id == reserved.run.id
    assert (reserved.goal.revision, reserved.goal.run_state) == (2, "reserved")

    started = await start_goal_run(db, reserved.run.id, lease_owner="test")
    assert (started.goal.revision, started.goal.run_state) == (3, "running")
    provider_usage = await record_goal_run_usage(
        db,
        goal_run_id=reserved.run.id,
        source_kind="provider",
        source_key="provider:assistant-1",
        tokens_used=40,
        cost_used_microusd=100,
    )
    replayed_usage = await record_goal_run_usage(
        db,
        goal_run_id=reserved.run.id,
        source_kind="provider",
        source_key="provider:assistant-1",
        tokens_used=40,
        cost_used_microusd=100,
    )
    assert replayed_usage.id == provider_usage.id
    with pytest.raises(GoalRunConflictError):
        await record_goal_run_usage(
            db,
            goal_run_id=reserved.run.id,
            source_kind="provider",
            source_key="provider:assistant-1",
            tokens_used=41,
            cost_used_microusd=100,
        )
    await record_goal_run_usage(
        db,
        goal_run_id=reserved.run.id,
        source_kind="compaction",
        source_key="compaction:assistant-2",
        tokens_used=83,
        cost_used_microusd=356,
    )
    finished = await finish_goal_run(
        db,
        reserved.run.id,
        status="completed",
        # Durable source records are authoritative even when the worker loses
        # its in-memory aggregate before finalization.
        tokens_used=0,
        cost_used_microusd=0,
        active_seconds=7,
        progress_summary="A checkpoint",
    )
    assert finished.run.status == "completed"
    assert finished.goal.tokens_used == 123
    assert finished.goal.cost_used_microusd == 456
    assert finished.goal.time_used_seconds == 7
    # A duplicate finalization cannot double-count usage.
    duplicate = await finish_goal_run(
        db,
        reserved.run.id,
        status="completed",
        tokens_used=999,
    )
    assert duplicate.goal.tokens_used == 123


@pytest.mark.asyncio
async def test_goal_token_breakdown_matches_3761404_without_double_counting_cache(
    db: AsyncSession,
) -> None:
    """Regression for the reported 3.761M-vs-activity Token discrepancy."""

    await _session(db)
    goal = await create_session_goal(db, "goal-session", _create_body())
    reserved = await reserve_goal_run(
        db,
        goal_id=goal.id,
        expected_revision=goal.revision,
        idempotency_key="exact-token-breakdown-run",
        trigger="initial",
    )
    await start_goal_run(db, reserved.run.id)

    message = Message(
        id="exact-token-breakdown-message",
        session_id="goal-session",
        data={"role": "assistant", "agent": "build"},
    )
    db.add(message)
    await db.flush()
    db.add(
        Part(
            message_id=message.id,
            session_id="goal-session",
            data={
                "type": "step-finish",
                "goal_run_id": reserved.run.id,
                "reason": "stop",
                "tokens": {
                    "input": 163_543,
                    "output": 17_519,
                    "reasoning": 11_702,
                    "cache_read": 3_568_640,
                    # Cache writes are diagnostic and are not added again.
                    "cache_write": 99_999,
                },
                "cost": 0.0,
            },
        )
    )
    await record_goal_run_usage(
        db,
        goal_run_id=reserved.run.id,
        source_kind="provider",
        source_key=f"provider:{message.id}",
        tokens_used=3_761_404,
        cost_used_microusd=0,
    )
    await db.flush()

    # In-flight source rows are visible before GoalRun finalization.
    active = await get_goal_token_usage_breakdown(db, goal.id)
    assert active.input == 163_543
    assert active.output == 17_519
    assert active.reasoning == 11_702
    assert active.cache_read == 3_568_640
    assert active.unattributed == 0
    assert active.total_tokens == 3_761_404
    assert active.source_count == 1

    await finish_goal_run(db, reserved.run.id, status="completed")
    committed = await get_goal_token_usage_breakdown(db, goal.id)
    # Committing the run must not add the same source rows a second time.
    assert committed == active


@pytest.mark.asyncio
async def test_goal_token_breakdown_preserves_pre_ledger_usage_as_unattributed(
    db: AsyncSession,
) -> None:
    await _session(db)
    goal = await create_session_goal(db, "goal-session", _create_body())
    goal.tokens_used = 321
    await db.flush()

    usage = await get_goal_token_usage_breakdown(db, goal.id)
    assert usage.total_tokens == 321
    assert usage.unattributed == 321
    assert usage.source_count == 0
    assert usage.input + usage.output + usage.reasoning + usage.cache_read == 0


@pytest.mark.asyncio
async def test_running_goal_edit_applies_at_boundary_and_stays_active(
    db: AsyncSession,
) -> None:
    await _session(db)
    goal = await create_session_goal(db, "goal-session", _create_body())
    reserved = await reserve_goal_run(
        db,
        goal_id=goal.id,
        expected_revision=goal.revision,
        idempotency_key="edit-boundary-run",
        trigger="initial",
    )
    started = await start_goal_run(db, reserved.run.id)

    edited = await update_session_goal(
        db,
        "goal-session",
        GoalUpdateRequest(
            client_request_id="edit-running-goal",
            expected_revision=started.goal.revision,
            objective="Ship the revised verified Goal",
        ),
    )
    assert edited.objective == "Ship the revised verified Goal"
    assert (edited.status, edited.run_state, edited.blocker_code) == (
        "active",
        "pausing",
        "goal_edited",
    )

    finished = await finish_goal_run(
        db,
        reserved.run.id,
        status="completed",
        progress_summary="Reached the edit boundary",
    )
    assert finished.run.status == "completed"
    assert (finished.goal.status, finished.goal.run_state) == ("active", "idle")
    assert finished.goal.blocker_code is None
    assert finished.goal.objective == "Ship the revised verified Goal"


@pytest.mark.asyncio
async def test_explicit_pause_after_running_edit_overrides_auto_restart(
    db: AsyncSession,
) -> None:
    await _session(db)
    goal = await create_session_goal(db, "goal-session", _create_body())
    reserved = await reserve_goal_run(
        db,
        goal_id=goal.id,
        expected_revision=goal.revision,
        idempotency_key="edit-then-pause-run",
        trigger="initial",
    )
    started = await start_goal_run(db, reserved.run.id)
    edited = await update_session_goal(
        db,
        "goal-session",
        GoalUpdateRequest(
            client_request_id="edit-before-pause",
            expected_revision=started.goal.revision,
            definition_of_done="The revised checks pass",
        ),
    )
    pausing = await pause_session_goal(
        db,
        "goal-session",
        GoalControlRequest(
            client_request_id="pause-after-edit",
            expected_revision=edited.revision,
        ),
    )
    assert (pausing.run_state, pausing.blocker_code) == ("pausing", "user_pause")

    finished = await finish_goal_run(
        db,
        reserved.run.id,
        status="completed",
    )
    assert (finished.goal.status, finished.goal.run_state) == ("paused", "idle")


@pytest.mark.asyncio
async def test_restart_recovery_interrupts_without_replay(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release_features, "AUTONOMOUS_GOALS_RELEASED", True)
    await _session(db)
    goal = await create_session_goal(db, "goal-session", _create_body())
    reserved = await reserve_goal_run(
        db,
        goal_id=goal.id,
        expected_revision=1,
        idempotency_key="restart-run-request",
        trigger="initial",
    )
    await start_goal_run(db, reserved.run.id)
    message = Message(
        id="restart-usage-message",
        session_id="goal-session",
        data={"role": "assistant", "agent": "build"},
    )
    db.add(message)
    await db.flush()
    db.add(
        Part(
            message_id=message.id,
            session_id="goal-session",
            data={
                "type": "step-finish",
                "reason": "tool_use",
                "tokens": {
                    "input": 10,
                    "output": 4,
                    "reasoning": 1,
                    "cache_read": 2,
                },
                "cost": 0.25,
            },
        )
    )
    db.add(
        Message(
            id="restart-compaction-usage",
            session_id="goal-session",
            data={
                "role": "assistant",
                "agent": "compaction",
                "system": True,
                "tokens": {"input": 3, "output": 2},
                "cost": 0.1,
            },
        )
    )
    await db.flush()
    assert await interrupt_inflight_goal_runs(db) == 1
    recovered = await get_session_goal(db, "goal-session")
    assert recovered is not None
    assert recovered.status == "blocked"
    assert recovered.run_state == "interrupted"
    assert recovered.needs_review is True
    run = await db.get(GoalRun, reserved.run.id)
    assert run is not None and run.status == "interrupted"
    assert run.tokens_used == 22
    assert run.cost_used_microusd == 350_000
    assert recovered.tokens_used == 22
    assert recovered.cost_used_microusd == 350_000
    # Recovery is idempotent once the run is terminal.
    assert await interrupt_inflight_goal_runs(db) == 0
    recovered_again = await get_session_goal(db, "goal-session")
    assert recovered_again is not None and recovered_again.tokens_used == 22


@pytest.mark.asyncio
async def test_manual_initial_run_ignores_zero_autonomous_continuation_budget(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release_features, "AUTONOMOUS_GOALS_RELEASED", True)
    await _session(db)
    goal = await create_session_goal(
        db,
        "goal-session",
        _create_body(max_continuations=0),
    )
    initial = await reserve_goal_run(
        db,
        goal_id=goal.id,
        expected_revision=1,
        idempotency_key="zero-budget-initial",
        trigger="initial",
    )
    assert initial.run.status == "reserved"


@pytest.mark.asyncio
async def test_run_finish_accounts_usage_after_goal_completion_revision(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release_features, "AUTONOMOUS_GOALS_RELEASED", True)
    await _session(db)
    goal = await create_session_goal(db, "goal-session", _create_body())
    reserved = await reserve_goal_run(
        db,
        goal_id=goal.id,
        expected_revision=1,
        idempotency_key="complete-before-finish",
        trigger="initial",
    )
    started = await start_goal_run(db, reserved.run.id)
    completed_goal = await transition_goal_status(
        db,
        goal_id=goal.id,
        expected_revision=started.goal.revision,
        target_status="complete",
        completion_summary="Evidence accepted",
        completion_evidence=[{"kind": "artifact", "exists": True}],
    )
    assert completed_goal.revision == 4
    finished = await finish_goal_run(
        db,
        reserved.run.id,
        status="completed",
        tokens_used=42,
        active_seconds=3,
    )
    assert finished.goal.status == "complete"
    assert finished.goal.completion_summary == "Evidence accepted"
    assert finished.goal.tokens_used == 42
    assert finished.goal.revision == 5


@pytest.mark.asyncio
async def test_archiving_interrupts_active_goal_and_requires_review(
    db: AsyncSession,
) -> None:
    await _session(db)
    goal = await create_session_goal(db, "goal-session", _create_body())
    goal.run_state = "running"
    await db.flush()

    assert await pause_active_goal_for_archive(db, "goal-session") is True
    archived = await get_session_goal(db, "goal-session")
    assert archived is not None
    assert archived.status == "paused"
    assert archived.run_state == "interrupted"
    assert archived.revision == 2
    assert archived.needs_review is True
    assert archived.blocker_code == "session_archived"
    assert await pause_active_goal_for_archive(db, "goal-session") is False
