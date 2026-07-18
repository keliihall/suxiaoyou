"""Autonomous, persistent Goal execution controller.

The controller owns top-level GoalRun boundaries.  A single SSE stream may
span many runs, but the global generation semaphore is released between each
run so one long-lived Goal cannot monopolize the server.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.models.goal_run import GoalRun
from app.models.message import Part
from app.models.session import Session
from app.models.session_goal import SessionGoal
from app.schemas.chat import PromptRequest
from app.schemas.goal import GoalResponse, GoalRunResponse
from app.session.goal_manager import (
    AutonomousGoalsUnavailableError,
    GoalControlError,
    GoalInvalidTransitionError,
    GoalRevisionConflictError,
    GoalRunConflictError,
    finish_goal_run,
    get_goal_by_id,
    reserve_goal_run,
    start_goal_run,
    transition_goal_status,
)
from app.session.input_queue import (
    block_unstarted_inputs_for_stream,
    claim_next_generation_input,
    finish_session_input,
    requeue_unstarted_session_input,
)
from app.session.retry import is_retryable
from app.streaming.events import (
    AGENT_ERROR,
    DONE,
    GOAL_RUN_FINISHED,
    GOAL_RUN_STARTED,
    GOAL_UPDATED,
    INPUT_STARTED,
    SSEEvent,
)
from app.streaming.manager import GenerationJob, StreamManager


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class GoalSliceResult:
    tokens_used: int
    cost_used_microusd: int
    active_seconds: int
    finish_reason: str
    total_cost: float
    agent_error: str | None = None
    agent_error_code: str | None = None
    exception: BaseException | None = None


@dataclass(slots=True)
class NextGoalRun:
    run_id: str
    request: PromptRequest
    skip_user_message: bool
    input_id: str | None = None


def _goal_payload(goal: SessionGoal) -> dict[str, Any]:
    return GoalResponse.model_validate(goal).model_dump(mode="json")


def _run_payload(run: GoalRun) -> dict[str, Any]:
    return GoalRunResponse.model_validate(run).model_dump(mode="json")


def _publish_goal(job: GenerationJob, goal: SessionGoal) -> None:
    job.publish(SSEEvent(GOAL_UPDATED, {"goal": _goal_payload(goal)}))


async def _execute_goal_slice(
    job: GenerationJob,
    request: PromptRequest,
    *,
    stream_manager: StreamManager,
    session_factory: async_sessionmaker[AsyncSession],
    provider_registry: Any,
    agent_registry: Any,
    tool_registry: Any,
    index_manager: Any | None,
    skip_user_message: bool,
) -> GoalSliceResult:
    """Execute one SessionPrompt while holding the global slot only for it."""

    from app.session.prompt import SessionPrompt

    event_start_id = job._event_counter
    started = time.monotonic()
    wait_started = job.goal_wait_seconds
    usage_started = job.goal_usage
    prompt = SessionPrompt(
        job,
        request,
        session_factory=session_factory,
        provider_registry=provider_registry,
        agent_registry=agent_registry,
        tool_registry=tool_registry,
        index_manager=index_manager,
        skip_user_message=skip_user_message,
    )
    caught: BaseException | None = None
    acquired = False
    try:
        await asyncio.wait_for(stream_manager._semaphore.acquire(), timeout=30.0)
        acquired = True
        await prompt.run(publish_done=False)
    except BaseException as exc:  # reconciled by the durable outer boundary
        caught = exc
    finally:
        if acquired:
            stream_manager._semaphore.release()

    elapsed = max(
        0.0,
        time.monotonic()
        - started
        - max(0.0, job.goal_wait_seconds - wait_started),
    )
    prompt_tokens = sum(
        max(0, int(prompt.total_tokens_accumulated.get(key, 0) or 0))
        for key in ("input", "output", "reasoning", "cache_read")
    )
    prompt_cost_microusd = max(0, round(prompt.total_cost * 1_000_000))
    usage_finished = job.goal_usage
    tokens = max(0, usage_finished[0] - usage_started[0])
    cost_microusd = max(0, usage_finished[1] - usage_started[1])
    # If setup/streaming raised before SessionPrompt._post_loop could record
    # its own metrics, retain the partial top-level usage for reconciliation.
    if caught is not None:
        tokens = max(tokens, prompt_tokens)
        cost_microusd = max(cost_microusd, prompt_cost_microusd)
    agent_error_event = next(
        (
            event
            for event in job.events
            if event.event == AGENT_ERROR
            and event.id is not None
            and event.id > event_start_id
        ),
        None,
    )
    agent_error = (
        str(
            agent_error_event.data.get("error_message")
            or "Goal generation failed"
        )
        if agent_error_event is not None
        else None
    )
    return GoalSliceResult(
        tokens_used=tokens,
        cost_used_microusd=cost_microusd,
        # A sequence of sub-second slices must not round down to zero forever
        # and bypass the cumulative wall-time budget. Conservatively charge
        # every non-zero active slice to the next whole second.
        active_seconds=max(0, math.ceil(elapsed)),
        finish_reason=prompt.finish_reason,
        total_cost=max(0.0, prompt.total_cost),
        agent_error=agent_error,
        agent_error_code=(
            str(agent_error_event.data.get("error_type") or "") or None
            if agent_error_event is not None
            else None
        ),
        exception=caught,
    )


async def _capture_permission_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    goal_id: str,
    trigger: str,
) -> None:
    """Capture the first server-computed ceiling at Goal creation."""

    # Resume admission tightens the durable Goal snapshot before reserving a
    # run. Re-copying Session.permission_snapshot after that run could replace
    # the old ceiling with a later, wider policy.
    if trigger != "initial":
        return
    async with session_factory() as db:
        async with db.begin():
            goal = await db.get(SessionGoal, goal_id)
            if goal is None:
                return
            session = await db.get(Session, goal.session_id)
            if session is not None and session.permission_snapshot is not None:
                goal.permission_snapshot = dict(session.permission_snapshot)


async def _made_durable_progress(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    session_id: str,
    goal_id: str,
    run_id: str,
) -> bool:
    """Require a persisted side effect, never prose, planning, or a no-op."""

    del goal_id  # retained in the private API for call-site compatibility

    async with session_factory() as db:
        run = await db.get(GoalRun, run_id)
        if run is None:
            return False
        since = run.time_started or run.time_created
        parts = list(
            (
                await db.execute(
                    select(Part).where(
                        Part.session_id == session_id,
                        Part.time_created >= since,
                    )
                )
            ).scalars()
        )
        for part in parts:
            data = part.data or {}
            state = data.get("state") or {}
            if data.get("type") != "tool" or state.get("status") != "completed":
                continue
            tool = str(data.get("tool") or "")
            if tool in {
                "get_goal",
                "glob",
                "grep",
                "read",
                "todo",
                "web_fetch",
                "web_search",
            }:
                continue
            if tool in {"bash", "code_execute"}:
                metadata = state.get("metadata") or {}
                if metadata.get("written_files") or metadata.get("deleted_files"):
                    return True
                continue
            else:
                return True
        return False


_TERMINAL_GOAL_ERROR_TYPES = frozenset(
    {
        "security_emergency_stop",
        "MODEL_DOES_NOT_SUPPORT_IMAGES",
        "loop_detected",
        "invocation_source_denied",
        "permission_required",
        "security_audit_unavailable",
    }
)


def _generation_failure_kind(
    result: GoalSliceResult,
    message: str | None,
) -> Literal["none", "retryable", "usage_limited", "terminal"]:
    """Classify an exhausted slice failure for durable Goal policy."""

    if message is None and result.exception is None:
        return "none"
    if result.agent_error_code in _TERMINAL_GOAL_ERROR_TYPES:
        return "terminal"

    detail = str(result.exception or message or "").strip()
    lowered = detail.lower()
    if any(
        marker in lowered
        for marker in (
            "429",
            "rate limit",
            "too_many_requests",
            "insufficient_quota",
            "quota exceeded",
            "billing limit",
            "credit balance",
            "payment required",
        )
    ):
        return "usage_limited"

    retry_reason = is_retryable(RuntimeError(detail)) if detail else None
    if retry_reason in {
        "Provider overloaded",
        "Network error",
    } or (retry_reason or "").startswith("Server error"):
        return "retryable"
    return "terminal"


async def _apply_progress_breaker(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    goal_id: str,
    made_progress: bool,
    generation_error: str | None,
) -> SessionGoal | None:
    """Persist monotonic breaker counters and block after three no-progress runs."""

    settings = get_settings()
    async with session_factory() as db:
        async with db.begin():
            goal = await get_goal_by_id(db, goal_id)
            if goal is None or goal.status != "active" or goal.run_state != "idle":
                return goal

            if generation_error is not None:
                next_error_count = goal.consecutive_error_count + 1
                values: dict[str, Any] = {
                    "revision": goal.revision + 1,
                    "no_progress_count": 0,
                    "consecutive_error_count": next_error_count,
                }
                if next_error_count >= settings.goal_consecutive_error_limit:
                    values.update(
                        status="blocked",
                        blocker_code="generation_error",
                        blocker_message=generation_error[:1000],
                        needs_review=True,
                    )
            elif made_progress:
                values = {
                    "revision": goal.revision + 1,
                    "no_progress_count": 0,
                    "consecutive_error_count": 0,
                }
            else:
                next_count = goal.no_progress_count + 1
                values = {
                    "revision": goal.revision + 1,
                    "no_progress_count": next_count,
                    "consecutive_error_count": 0,
                }
                if next_count >= settings.goal_no_progress_limit:
                    values.update(
                        status="blocked",
                        blocker_code="no_progress",
                        blocker_message=(
                            "The Goal made no verifiable progress for "
                            f"{next_count} consecutive runs"
                        ),
                        needs_review=False,
                    )

            result = await db.execute(
                update(SessionGoal)
                .where(
                    SessionGoal.id == goal.id,
                    SessionGoal.revision == goal.revision,
                    SessionGoal.status == "active",
                    SessionGoal.run_state == "idle",
                )
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                return await get_goal_by_id(db, goal_id)
            return await get_goal_by_id(db, goal_id)


def _request_for_continuation(
    goal: SessionGoal,
    session: Session,
    *,
    item: Any | None,
) -> PromptRequest:
    from app.agent.permission import (
        intersect_permission_rulesets,
        parse_permission_policy_baseline,
        parse_permission_snapshot,
        presets_to_ruleset,
        serialize_permission_snapshot,
        tighten_permission_snapshot,
    )
    from app.schemas.agent import PermissionRule, Ruleset

    parent = parse_permission_snapshot(goal.permission_snapshot)
    current = parse_permission_snapshot(session.permission_snapshot)
    if parent is None or current is None:
        # Missing, legacy, or malformed authority sources fail closed. An
        # empty ordered Ruleset is not sufficient here because the ordinary
        # authoritative merge still contains global/agent allows.
        ceiling = Ruleset(rules=[
            PermissionRule(action="deny", permission="*", pattern="*"),
        ])
    else:
        requested_parent = parent
        if item is not None:
            requested = list(item.permission_rules or [])
            requested.extend(
                rule.model_dump(mode="json")
                for rule in presets_to_ruleset(item.permission_presets).rules
            )
            requested_parent = tighten_permission_snapshot(parent, requested)
        # The historical Goal snapshot is an immutable maximum. The latest
        # server-owned Session snapshot may only narrow it, never replace it.
        ceiling = intersect_permission_rulesets(requested_parent, current)

    rules = serialize_permission_snapshot(ceiling)["rules"]

    request = PromptRequest(
        session_id=goal.session_id,
        text=(
            "Continue working autonomously toward the persistent Goal. "
            "Verify the result and update the Goal only when its completion "
            "contract is satisfied."
            if item is None
            else item.text
        ),
        model=(goal.model_id if item is None else item.model_id or goal.model_id),
        provider_id=(
            goal.provider_id
            if item is None
            else item.provider_id or goal.provider_id
        ),
        agent=(goal.agent if item is None else item.agent or goal.agent),
        attachments=[] if item is None else list(item.attachments or []),
        permission_rules=rules,
        reasoning=(goal.reasoning if item is None else item.reasoning),
        workspace=(session.directory if session.directory != "." else None),
        language=(goal.language if item is None else item.language),
    )
    request._permission_rules_authoritative = True
    request._trusted_permission_ruleset = ceiling
    request._enforce_current_permission_ceiling = True
    request._goal_permission_baseline = parse_permission_policy_baseline(
        goal.permission_snapshot
    )
    return request


async def _reserve_next_run(
    job: GenerationJob,
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> NextGoalRun | None:
    """Claim real user input and reserve its run before considering autonomy."""

    assert job.goal_id is not None
    async with job.session_input_lock:
        if job.abort_event.is_set():
            job.close_session_input_admission()
            return None
        async with session_factory() as db:
            async with db.begin():
                goal = await get_goal_by_id(db, job.goal_id)
                if goal is None or goal.status != "active" or goal.run_state != "idle":
                    job.close_session_input_admission()
                    return None
                session = await db.get(Session, goal.session_id)
                if session is None:
                    job.close_session_input_admission()
                    return None

                item = await claim_next_generation_input(
                    db,
                    goal.session_id,
                    target_stream_id=job.stream_id,
                    include_stale_steer=True,
                )
                trigger = "user_input" if item is not None else "auto"
                key = (
                    f"{job.stream_id}:input:{item.id}"
                    if item is not None
                    else (
                        f"{job.stream_id}:auto:"
                        f"{goal.continuation_count + 1}:{goal.revision}"
                    )
                )
                reservation = await reserve_goal_run(
                    db,
                    goal_id=goal.id,
                    expected_revision=goal.revision,
                    idempotency_key=key,
                    trigger=trigger,
                    stream_id=job.stream_id,
                )
                request = _request_for_continuation(
                    reservation.goal,
                    session,
                    item=item,
                )
                return NextGoalRun(
                    run_id=reservation.run.id,
                    request=request,
                    skip_user_message=item is None,
                    input_id=item.id if item is not None else None,
                )


async def _mark_budget_limited(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    goal_id: str,
    code: str,
    message: str,
) -> SessionGoal | None:
    async with session_factory() as db:
        async with db.begin():
            goal = await get_goal_by_id(db, goal_id)
            if goal is None or goal.status != "active":
                return goal
            try:
                return await transition_goal_status(
                    db,
                    goal_id=goal.id,
                    expected_revision=goal.revision,
                    target_status="budget_limited",
                    blocker_code=code,
                    blocker_message=message,
                )
            except (GoalRevisionConflictError, GoalInvalidTransitionError):
                return await get_goal_by_id(db, goal_id)


async def _mark_usage_limited(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    goal_id: str,
    message: str,
) -> SessionGoal | None:
    async with session_factory() as db:
        async with db.begin():
            goal = await get_goal_by_id(db, goal_id)
            if goal is None or goal.status != "active":
                return goal
            try:
                return await transition_goal_status(
                    db,
                    goal_id=goal.id,
                    expected_revision=goal.revision,
                    target_status="usage_limited",
                    blocker_code="provider_usage_limited",
                    blocker_message=message[:1000],
                    needs_review=True,
                )
            except (GoalRevisionConflictError, GoalInvalidTransitionError):
                return await get_goal_by_id(db, goal_id)


async def _mark_terminal_generation_failure(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    goal_id: str,
    message: str,
) -> SessionGoal | None:
    async with session_factory() as db:
        async with db.begin():
            goal = await get_goal_by_id(db, goal_id)
            if goal is None or goal.status != "active":
                return goal
            try:
                return await transition_goal_status(
                    db,
                    goal_id=goal.id,
                    expected_revision=goal.revision,
                    target_status="blocked",
                    blocker_code="generation_error",
                    blocker_message=message[:1000],
                    needs_review=True,
                )
            except (GoalRevisionConflictError, GoalInvalidTransitionError):
                return await get_goal_by_id(db, goal_id)


async def _mark_manual_turn_paused(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    goal_id: str,
) -> SessionGoal | None:
    """End an explicit slice cleanly when unattended continuation is gated."""

    async with session_factory() as db:
        async with db.begin():
            goal = await get_goal_by_id(db, goal_id)
            if goal is None or goal.status != "active" or goal.run_state != "idle":
                return goal
            try:
                return await transition_goal_status(
                    db,
                    goal_id=goal.id,
                    expected_revision=goal.revision,
                    target_status="paused",
                    blocker_code="manual_goal_turn_complete",
                    blocker_message=(
                        "This release requires an explicit resume for the next Goal turn"
                    ),
                )
            except (GoalRevisionConflictError, GoalInvalidTransitionError):
                return await get_goal_by_id(db, goal_id)


async def _budget_reason(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    goal_id: str,
) -> str | None:
    async with session_factory() as db:
        goal = await get_goal_by_id(db, goal_id)
        if goal is None:
            return "goal_cleared"
        for limit, used, code in (
            (goal.token_budget, goal.tokens_used, "token_budget"),
            (goal.cost_budget_microusd, goal.cost_used_microusd, "cost_budget"),
            (goal.time_budget_seconds, goal.time_used_seconds, "time_budget"),
        ):
            if limit is not None and used >= limit:
                return code
        return None


async def _finish_current_input(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    input_id: str | None,
    stream_id: str,
    failed: str | None,
    aborted: bool,
) -> None:
    if input_id is None:
        return
    async with session_factory() as db:
        async with db.begin():
            await finish_session_input(
                db,
                input_id,
                status=("blocked" if aborted else "failed" if failed else "consumed"),
                applied_stream_id=stream_id,
                error_message=(
                    "Goal execution stopped before this input completed"
                    if aborted
                    else failed
                ),
            )


async def _reconcile_unstarted_inputs(
    job: GenerationJob,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    terminal_reason: str,
) -> None:
    """Atomically close input admission and leave no accepted input stranded.

    A safe pause intentionally preserves queued rows for explicit resume (which
    claims them before reserving a resume-only run). Every other terminal state
    makes those rows visibly blocked so the client can edit/cancel/resubmit
    instead of showing an input that no worker can ever consume.
    """

    async with job.session_input_lock:
        job.close_session_input_admission()
        if terminal_reason == "paused":
            return
        async with session_factory() as db:
            async with db.begin():
                await block_unstarted_inputs_for_stream(
                    db,
                    session_id=job.session_id,
                    stream_id=job.stream_id,
                    include_stale_steer=True,
                    error_message=(
                        "The persistent Goal stopped before this input could start "
                        f"({terminal_reason})"
                    ),
                )


async def run_goal_generation(
    job: GenerationJob,
    initial_request: PromptRequest,
    *,
    initial_run_id: str,
    stream_manager: StreamManager,
    session_factory: async_sessionmaker[AsyncSession],
    provider_registry: Any,
    agent_registry: Any,
    tool_registry: Any,
    index_manager: Any | None = None,
    idempotency_record_id: str | None = None,
    initial_input_id: str | None = None,
    initial_skip_user_message: bool = True,
) -> None:
    """Run Goal slices until complete, paused, blocked, limited, or aborted."""

    if job.goal_id is None:
        raise ValueError("Goal generation requires a server-owned goal_id")

    current = NextGoalRun(
        run_id=initial_run_id,
        request=initial_request,
        skip_user_message=initial_skip_user_message,
        input_id=initial_input_id,
    )
    total_cost = 0.0
    terminal_reason = "stop"
    done_published = False
    record_status = "accepted"
    record_error: str | None = None
    input_admission_reconciled = False
    try:
        if idempotency_record_id is not None:
            from app.session.idempotency import mark_idempotency_status

            async with session_factory() as db:
                async with db.begin():
                    await mark_idempotency_status(
                        db,
                        idempotency_record_id,
                        status="running",
                    )
            record_status = "running"
        while current is not None:
            # Every durable GoalRun is an independent background root turn;
            # it must not borrow the previous slice's checkpoint identity.
            job.begin_root_turn(current.run_id)
            job.set_goal_run_id(current.run_id)
            try:
                async with job.execution_admission_lock:
                    async with session_factory() as db:
                        async with db.begin():
                            started = await start_goal_run(
                                db,
                                current.run_id,
                                lease_owner=f"stream:{job.stream_id}",
                            )
                    # Re-open only after the durable start succeeds while the
                    # same control lock excludes pause/edit/archive. If a
                    # control operation won first, start_goal_run fails and
                    # the closed gate is never resurrected.
                    job.open_execution_admission()
            except GoalControlError:
                # A pause/edit can win after reservation but before Provider
                # admission. Finalize the reservation without starting work.
                async with session_factory() as db:
                    async with db.begin():
                        finished = await finish_goal_run(
                            db,
                            current.run_id,
                            status="completed",
                            stop_reason="Goal changed before the run started",
                        )
                        if current.input_id is not None:
                            await requeue_unstarted_session_input(
                                db,
                                current.input_id,
                            )
                job.publish(
                    SSEEvent(
                        GOAL_RUN_FINISHED,
                        {
                            "goal": _goal_payload(finished.goal),
                            "run": _run_payload(finished.run),
                        },
                    )
                )
                _publish_goal(job, finished.goal)
                if finished.goal.status == "active":
                    try:
                        current = await _reserve_next_run(
                            job,
                            session_factory=session_factory,
                        )
                    except GoalControlError:
                        current = None
                    if current is not None:
                        continue
                terminal_reason = finished.goal.status
                break

            job.publish(
                SSEEvent(
                    GOAL_RUN_STARTED,
                    {
                        "goal": _goal_payload(started.goal),
                        "run": _run_payload(started.run),
                    },
                )
            )
            if current.input_id is not None:
                job.publish(
                    SSEEvent(
                        INPUT_STARTED,
                        {
                            "input_id": current.input_id,
                            "mode": "queue",
                            "session_id": job.session_id,
                        },
                    )
                )
            _publish_goal(job, started.goal)

            result = await _execute_goal_slice(
                job,
                current.request,
                stream_manager=stream_manager,
                session_factory=session_factory,
                provider_registry=provider_registry,
                agent_registry=agent_registry,
                tool_registry=tool_registry,
                index_manager=index_manager,
                skip_user_message=current.skip_user_message,
            )
            total_cost += result.total_cost
            await _capture_permission_snapshot(
                session_factory,
                goal_id=job.goal_id,
                trigger=started.run.trigger,
            )

            failed_message = result.agent_error
            if result.exception is not None and not isinstance(
                result.exception,
                asyncio.CancelledError,
            ):
                failed_message = failed_message or str(result.exception)
                job.publish(
                    SSEEvent(
                        AGENT_ERROR,
                        {"error_message": "Goal execution failed at a safe boundary."},
                    )
                )
            failure_kind = _generation_failure_kind(result, failed_message)
            await _finish_current_input(
                session_factory,
                input_id=current.input_id,
                stream_id=job.stream_id,
                failed=failed_message,
                aborted=job.abort_event.is_set(),
            )

            async with session_factory() as db:
                async with db.begin():
                    before_finish = await get_goal_by_id(db, job.goal_id)
                    safe_pause = bool(
                        before_finish is not None
                        and before_finish.run_state == "pausing"
                        and before_finish.blocker_code != "goal_edited"
                        and not job.abort_event.is_set()
                    )
                    lifecycle_interrupted = bool(
                        before_finish is not None
                        and before_finish.run_state == "interrupted"
                    )
                    run_status = (
                        "interrupted"
                        if job.abort_event.is_set() or lifecycle_interrupted
                        else "failed"
                        if failed_message
                        else "completed"
                    )
                    finished = await finish_goal_run(
                        db,
                        current.run_id,
                        status=run_status,
                        tokens_used=result.tokens_used,
                        cost_used_microusd=result.cost_used_microusd,
                        active_seconds=result.active_seconds,
                        progress_summary=(
                            "Stopped at a safe pause boundary"
                            if safe_pause
                            else result.finish_reason
                        ),
                        stop_reason=(
                            "Immediate stop requested"
                            if job.abort_event.is_set()
                            else "Session lifecycle interrupted this Goal run"
                            if lifecycle_interrupted
                            else failed_message
                        ),
                        error_code=(
                            "immediate_stop"
                            if job.abort_event.is_set()
                            else "usage_limited"
                            if failed_message and failure_kind == "usage_limited"
                            else "retryable_generation_error"
                            if failed_message and failure_kind == "retryable"
                            else "generation_error"
                            if failed_message
                            else None
                        ),
                    )
            job.publish(
                SSEEvent(
                    GOAL_RUN_FINISHED,
                    {
                        "goal": _goal_payload(finished.goal),
                        "run": _run_payload(finished.run),
                    },
                )
            )
            _publish_goal(job, finished.goal)

            if result.exception is not None and isinstance(
                result.exception,
                asyncio.CancelledError,
            ):
                raise result.exception
            if finished.goal.status != "active":
                terminal_reason = finished.goal.status
                break

            if failure_kind == "usage_limited":
                goal = await _mark_usage_limited(
                    session_factory,
                    goal_id=job.goal_id,
                    message=failed_message or "Provider usage is currently limited",
                )
                if goal is not None:
                    _publish_goal(job, goal)
                    terminal_reason = goal.status
                break
            if failure_kind == "terminal":
                goal = await _mark_terminal_generation_failure(
                    session_factory,
                    goal_id=job.goal_id,
                    message=failed_message or "Goal generation failed",
                )
                if goal is not None:
                    _publish_goal(job, goal)
                    terminal_reason = goal.status
                break

            budget_code = await _budget_reason(
                session_factory,
                goal_id=job.goal_id,
            )
            if result.finish_reason == "budget_limited" and budget_code is None:
                budget_code = "budget"
            if budget_code is not None:
                goal = await _mark_budget_limited(
                    session_factory,
                    goal_id=job.goal_id,
                    code=budget_code,
                    message="The Goal reached its configured execution budget",
                )
                if goal is not None:
                    _publish_goal(job, goal)
                    terminal_reason = goal.status
                break

            made_progress = await _made_durable_progress(
                session_factory,
                session_id=job.session_id,
                goal_id=job.goal_id,
                run_id=current.run_id,
            )
            goal = await _apply_progress_breaker(
                session_factory,
                goal_id=job.goal_id,
                made_progress=made_progress,
                generation_error=failed_message,
            )
            if goal is None or goal.status != "active":
                if goal is not None:
                    _publish_goal(job, goal)
                    terminal_reason = goal.status
                else:
                    terminal_reason = "cleared"
                break
            _publish_goal(job, goal)

            try:
                current = await _reserve_next_run(
                    job,
                    session_factory=session_factory,
                )
            except AutonomousGoalsUnavailableError:
                goal = await _mark_manual_turn_paused(
                    session_factory,
                    goal_id=job.goal_id,
                )
                if goal is not None:
                    _publish_goal(job, goal)
                    terminal_reason = goal.status
                current = None
            except GoalInvalidTransitionError:
                goal = await _mark_budget_limited(
                    session_factory,
                    goal_id=job.goal_id,
                    code="continuation_budget",
                    message="The Goal reached its autonomous continuation limit",
                )
                if goal is not None:
                    _publish_goal(job, goal)
                    terminal_reason = goal.status
                current = None
            except (GoalRevisionConflictError, GoalRunConflictError):
                current = None

        await _reconcile_unstarted_inputs(
            job,
            session_factory=session_factory,
            terminal_reason=terminal_reason,
        )
        input_admission_reconciled = True
        job.publish(
            SSEEvent(
                DONE,
                {
                    "session_id": job.session_id,
                    "finish_reason": terminal_reason,
                    "total_cost": total_cost,
                    "goal_id": job.goal_id,
                },
            )
        )
        done_published = True
        record_status = "stopped" if job.abort_event.is_set() else "completed"
    except asyncio.CancelledError:
        terminal_reason = "interrupted"
        record_status = "interrupted"
        record_error = "Goal worker was cancelled before reaching a safe boundary"
        try:
            async with session_factory() as db:
                async with db.begin():
                    await finish_goal_run(
                        db,
                        current.run_id,
                        status="interrupted",
                        stop_reason=record_error,
                        error_code="worker_cancelled",
                    )
        except GoalControlError:
            logger.warning(
                "Could not reconcile cancelled GoalRun %s",
                current.run_id,
                exc_info=True,
            )
        raise
    except Exception as exc:
        logger.exception("Unhandled Goal controller error for stream %s", job.stream_id)
        terminal_reason = "failed"
        record_status = "failed"
        record_error = str(exc)
        try:
            async with session_factory() as db:
                async with db.begin():
                    await finish_goal_run(
                        db,
                        current.run_id,
                        status="failed",
                        stop_reason="The Goal controller failed unexpectedly",
                        error_code="controller_error",
                    )
        except GoalControlError:
            logger.warning(
                "Could not reconcile failed GoalRun %s",
                current.run_id,
                exc_info=True,
            )
        job.publish(
            SSEEvent(
                AGENT_ERROR,
                {"error_message": "The Goal controller stopped unexpectedly."},
            )
        )
    finally:
        if not input_admission_reconciled:
            try:
                await _reconcile_unstarted_inputs(
                    job,
                    session_factory=session_factory,
                    terminal_reason=terminal_reason,
                )
            except Exception:
                logger.warning(
                    "Failed to reconcile queued Goal inputs for stream %s",
                    job.stream_id,
                    exc_info=True,
                )
                async with job.session_input_lock:
                    job.close_session_input_admission()
        if not done_published:
            job.publish(
                SSEEvent(
                    DONE,
                    {
                        "session_id": job.session_id,
                        "finish_reason": terminal_reason,
                        "total_cost": total_cost,
                        "goal_id": job.goal_id,
                    },
                )
            )
        if idempotency_record_id is not None:
            try:
                from app.session.idempotency import mark_idempotency_status

                async with session_factory() as db:
                    async with db.begin():
                        await mark_idempotency_status(
                            db,
                            idempotency_record_id,
                            status=record_status,
                            error_message=record_error,
                        )
            except Exception:
                logger.warning(
                    "Failed to finalize Goal admission ledger %s",
                    idempotency_record_id,
                    exc_info=True,
                )
        job.complete()
