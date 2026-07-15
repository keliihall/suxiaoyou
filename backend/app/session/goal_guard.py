"""Fail-closed execution gates for persistent Goal work."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(frozen=True, slots=True)
class GoalExecutionGate:
    allowed: bool
    goal_id: str
    status: str
    run_state: str
    revision: int
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class GoalBudgetGate:
    allowed: bool
    warning: bool
    reason_code: str | None
    token_remaining: int | None
    cost_remaining_microusd: int | None
    time_remaining_seconds: int | None


async def read_goal_execution_gate(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    session_id: str,
    goal_id: str,
    goal_run_id: str | None,
) -> GoalExecutionGate:
    """Read the current durable gate immediately before model/tool admission."""

    try:
        from app.models.session_goal import SessionGoal
    except ImportError:
        return GoalExecutionGate(
            allowed=False,
            goal_id=goal_id,
            status="unavailable",
            run_state="interrupted",
            revision=0,
            reason="Goal persistence is unavailable",
        )

    async with session_factory() as db:
        goal = (
            await db.execute(
                select(SessionGoal).where(
                    SessionGoal.id == goal_id,
                    SessionGoal.session_id == session_id,
                )
            )
        ).scalar_one_or_none()

    if goal is None:
        return GoalExecutionGate(
            allowed=False,
            goal_id=goal_id,
            status="cleared",
            run_state="interrupted",
            revision=0,
            reason="Goal was cleared",
        )

    status = str(getattr(goal, "status", "unknown"))
    run_state = str(getattr(goal, "run_state", "idle"))
    revision = int(getattr(goal, "revision", 0) or 0)
    needs_review = bool(getattr(goal, "needs_review", False))
    current_run_id = getattr(goal, "last_run_id", None)

    reason: str | None = None
    if status != "active":
        reason = f"Goal status is {status}"
    elif run_state not in {"reserved", "running"}:
        reason = f"Goal run state is {run_state}"
    elif needs_review:
        reason = "Goal requires user review before it can continue"
    elif goal_run_id is not None and current_run_id not in {None, goal_run_id}:
        reason = "A newer GoalRun owns this Goal"

    return GoalExecutionGate(
        allowed=reason is None,
        goal_id=goal_id,
        status=status,
        run_state=run_state,
        revision=revision,
        reason=reason,
    )


async def goal_job_execution_allowed(
    session_factory: async_sessionmaker[AsyncSession],
    job: Any,
) -> tuple[bool, str | None]:
    """Tool-executor callback with a compact boolean contract."""

    goal_id = getattr(job, "goal_id", None)
    if goal_id is None:
        return True, None
    gate = await read_goal_execution_gate(
        session_factory,
        session_id=getattr(job, "goal_session_id", None) or job.session_id,
        goal_id=goal_id,
        goal_run_id=getattr(job, "goal_run_id", None),
    )
    return gate.allowed, gate.reason


def _remaining(limit: Any, used: Any, local_used: int) -> int | None:
    if limit is None:
        return None
    return int(limit or 0) - int(used or 0) - max(0, int(local_used))


async def read_goal_budget_gate(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    session_id: str,
    goal_id: str,
    local_tokens_used: int = 0,
    local_cost_microusd: int = 0,
    local_active_seconds: int = 0,
    warning_ratio: float = 0.8,
) -> GoalBudgetGate:
    """Evaluate cumulative hard budgets before admitting another model step."""

    try:
        from app.models.session_goal import SessionGoal
    except ImportError:
        return GoalBudgetGate(False, False, "unavailable", 0, 0, 0)

    async with session_factory() as db:
        goal = (
            await db.execute(
                select(SessionGoal).where(
                    SessionGoal.id == goal_id,
                    SessionGoal.session_id == session_id,
                )
            )
        ).scalar_one_or_none()
    if goal is None:
        return GoalBudgetGate(False, False, "cleared", 0, 0, 0)

    token_remaining = _remaining(
        getattr(goal, "token_budget", None),
        getattr(goal, "tokens_used", 0),
        local_tokens_used,
    )
    cost_remaining = _remaining(
        getattr(goal, "cost_budget_microusd", None),
        getattr(goal, "cost_used_microusd", 0),
        local_cost_microusd,
    )
    time_remaining = _remaining(
        getattr(goal, "time_budget_seconds", None),
        getattr(goal, "time_used_seconds", 0),
        local_active_seconds,
    )

    reason_code: str | None = None
    if token_remaining is not None and token_remaining <= 0:
        reason_code = "token_budget"
    elif cost_remaining is not None and cost_remaining <= 0:
        reason_code = "cost_budget"
    elif time_remaining is not None and time_remaining <= 0:
        reason_code = "time_budget"

    fractions: list[float] = []
    for limit_name, remaining in (
        ("token_budget", token_remaining),
        ("cost_budget_microusd", cost_remaining),
        ("time_budget_seconds", time_remaining),
    ):
        limit = getattr(goal, limit_name, None)
        if limit is not None and int(limit or 0) > 0 and remaining is not None:
            fractions.append(max(0.0, remaining / int(limit)))
    warning = bool(fractions) and min(fractions) <= max(0.0, 1.0 - warning_ratio)

    return GoalBudgetGate(
        allowed=reason_code is None,
        warning=warning,
        reason_code=reason_code,
        token_remaining=token_remaining,
        cost_remaining_microusd=cost_remaining,
        time_remaining_seconds=time_remaining,
    )


async def set_goal_waiting_user(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    session_id: str,
    goal_id: str,
    goal_run_id: str | None,
    waiting: bool,
    blocker_code: str | None = None,
    blocker_message: str | None = None,
) -> None:
    """Persist transient interactive wait state without changing Goal revision."""

    from app.models.goal_run import GoalRun
    from app.models.session_goal import SessionGoal

    async with session_factory() as db:
        async with db.begin():
            goal = await db.get(SessionGoal, goal_id)
            if goal is None or goal.session_id != session_id:
                return
            if goal_run_id is not None and goal.last_run_id not in {
                None,
                goal_run_id,
            }:
                return
            if waiting:
                if goal.status != "active":
                    return
                goal.run_state = "waiting_user"
                goal.blocker_code = blocker_code
                goal.blocker_message = blocker_message
            elif goal.status == "active" and goal.run_state == "waiting_user":
                goal.run_state = "running"
                goal.blocker_code = None
                goal.blocker_message = None

            if goal_run_id is not None:
                run = await db.get(GoalRun, goal_run_id)
                if run is not None and run.goal_id == goal_id:
                    if waiting and run.status in {"reserved", "running"}:
                        run.status = "waiting_user"
                    elif not waiting and run.status == "waiting_user":
                        run.status = "running"


async def block_goal_for_user_action(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    session_id: str,
    goal_id: str,
    goal_run_id: str | None,
    blocker_code: str,
    blocker_message: str,
) -> None:
    """Stop autonomy after an interactive request is denied or expires."""

    from app.models.session_goal import SessionGoal

    async with session_factory() as db:
        async with db.begin():
            goal = await db.get(SessionGoal, goal_id)
            if goal is None or goal.session_id != session_id:
                return
            if goal_run_id is not None and goal.last_run_id not in {
                None,
                goal_run_id,
            }:
                return
            if goal.status == "active":
                goal.status = "blocked"
                goal.run_state = "idle"
                goal.blocker_code = blocker_code
                goal.blocker_message = blocker_message
                goal.blocker_streak = int(goal.blocker_streak or 0) + 1
                goal.revision = int(goal.revision or 0) + 1
            # Keep the GoalRun non-terminal until the outer run boundary can
            # reconcile this slice's exact token/cost/time usage. Marking it
            # terminal here would make finish_goal_run's idempotent fast path
            # skip accounting for work completed before the interaction.
