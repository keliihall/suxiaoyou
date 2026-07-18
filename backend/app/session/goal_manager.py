"""Persistent Goal control plane and GoalRun ledger primitives.

This module deliberately contains no generation, prompt, or stream-manager
integration. It provides the transactional boundary those runtime components
can call once autonomous Goals are released.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import case, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import release_features
from app.config import Settings
from app.models.goal_run import GoalRun
from app.models.goal_usage_record import GoalUsageRecord
from app.models.idempotency_record import IdempotencyRecord
from app.models.message import Message, Part
from app.models.session import Session
from app.models.session_goal import SessionGoal
from app.schemas.goal import (
    GoalControlRequest,
    GoalCreateRequest,
    GoalRunTrigger,
    GoalUpdateRequest,
)
from app.session.idempotency import (
    IdempotencyConflictError,
    canonical_request_hash,
    get_idempotency_record,
    validate_idempotent_replay,
)
from app.utils.id import generate_ulid


ACTIVE_GOAL_RUN_STATUSES = frozenset({"reserved", "running", "waiting_user"})
TERMINAL_GOAL_RUN_STATUSES = frozenset(
    {"completed", "blocked", "interrupted", "failed"}
)
PAUSABLE_RUN_STATES = frozenset({"reserved", "running", "waiting_user"})

LEGAL_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "active": frozenset(
        {"paused", "blocked", "usage_limited", "budget_limited", "complete"}
    ),
    "paused": frozenset({"active"}),
    "blocked": frozenset({"active"}),
    "usage_limited": frozenset({"active"}),
    "budget_limited": frozenset({"active"}),
    "complete": frozenset({"active"}),
}


class GoalControlError(Exception):
    """Base class for framework-independent Goal control failures."""


class GoalNotFoundError(GoalControlError):
    pass


class GoalAlreadyExistsError(GoalControlError):
    pass


class GoalRevisionConflictError(GoalControlError):
    def __init__(self, *, expected_revision: int, current_revision: int | None) -> None:
        self.expected_revision = expected_revision
        self.current_revision = current_revision
        super().__init__(
            f"Goal revision changed (expected {expected_revision}, "
            f"current {current_revision})"
        )


class GoalInvalidTransitionError(GoalControlError):
    pass


class GoalValidationError(GoalControlError):
    pass


class GoalBudgetLimitError(GoalValidationError):
    def __init__(self, *, field: str, maximum: int) -> None:
        self.field = field
        self.maximum = maximum
        super().__init__(f"{field} exceeds the server maximum of {maximum}")


class GoalIdempotencyConflictError(GoalControlError):
    pass


class GoalRunConflictError(GoalControlError):
    pass


class AutonomousGoalsUnavailableError(GoalControlError):
    pass


@dataclass(frozen=True)
class GoalRunReservation:
    goal: SessionGoal
    run: GoalRun
    idempotent: bool = False


@dataclass(frozen=True)
class GoalRunUpdate:
    goal: SessionGoal
    run: GoalRun


@dataclass(frozen=True)
class GoalTokenUsageBreakdown:
    """Auditable Goal token totals using the canonical Provider semantics.

    ``input`` excludes cache hits, while ``cache_read`` contains those cached
    prompt tokens.  The Goal budget intentionally counts both fields together
    with output and reasoning tokens.  ``unattributed`` preserves the exact
    durable total for legacy/recovered rows whose original Provider component
    payload is no longer available.
    """

    input: int = 0
    output: int = 0
    reasoning: int = 0
    cache_read: int = 0
    unattributed: int = 0
    total_tokens: int = 0
    source_count: int = 0


@dataclass(frozen=True)
class _IdempotentReplay:
    found: bool
    goal: SessionGoal | None = None
    deleted: bool = False


def _settings(settings: Settings | None) -> Settings:
    return settings if settings is not None else Settings()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _goal_request_payload(
    body: GoalCreateRequest | GoalUpdateRequest | GoalControlRequest,
    *,
    exclude_unset: bool,
) -> dict[str, Any]:
    return body.model_dump(
        mode="json",
        exclude={"client_request_id"},
        exclude_unset=exclude_unset,
    )


async def _idempotent_replay(
    db: AsyncSession,
    *,
    scope: str,
    request_key: str,
    request_hash: str,
) -> _IdempotentReplay:
    record = await get_idempotency_record(
        db,
        scope=scope,
        request_key=request_key,
    )
    if record is None:
        return _IdempotentReplay(found=False)
    try:
        response = validate_idempotent_replay(record, request_hash=request_hash)
    except IdempotencyConflictError as exc:
        raise GoalIdempotencyConflictError(str(exc)) from exc
    if response.get("deleted") is True:
        return _IdempotentReplay(found=True, deleted=True)
    goal_id = response.get("goal_id")
    if not isinstance(goal_id, str):
        raise GoalIdempotencyConflictError(
            "The stored Goal idempotency response is invalid"
        )
    goal = await get_goal_by_id(db, goal_id)
    if goal is None:
        raise GoalIdempotencyConflictError(
            "The Goal created by this request has since been cleared"
        )
    stored_revision = response.get("goal_revision")
    if isinstance(stored_revision, int) and goal.revision != stored_revision:
        raise GoalIdempotencyConflictError(
            "The request was already applied, but the Goal has changed since "
            f"revision {stored_revision}; fetch the current Goal before acting"
        )
    return _IdempotentReplay(found=True, goal=goal)


async def _record_idempotent_success(
    db: AsyncSession,
    *,
    scope: str,
    request_key: str,
    request_hash: str,
    goal_id: str | None,
    goal_revision: int | None = None,
    deleted: bool = False,
) -> None:
    db.add(
        IdempotencyRecord(
            id=generate_ulid(),
            scope=scope,
            request_key=request_key,
            request_hash=request_hash,
            status="completed",
            response={
                "goal_id": goal_id,
                "goal_revision": goal_revision,
                "deleted": deleted,
            },
        )
    )
    await db.flush()


async def get_session_goal(db: AsyncSession, session_id: str) -> SessionGoal | None:
    return (
        await db.execute(
            select(SessionGoal)
            .where(SessionGoal.session_id == session_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def get_goal_by_id(db: AsyncSession, goal_id: str) -> SessionGoal | None:
    return (
        await db.execute(
            select(SessionGoal)
            .where(SessionGoal.id == goal_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


def _resolve_create_budgets(
    body: GoalCreateRequest,
    settings: Settings,
) -> dict[str, int | None]:
    values = {
        "token_budget": (
            settings.goal_default_token_budget
            if body.token_budget is None
            else body.token_budget
        ),
        "cost_budget_microusd": (
            settings.goal_default_cost_budget_microusd
            if body.cost_budget_microusd is None
            else body.cost_budget_microusd
        ),
        "time_budget_seconds": (
            settings.goal_default_active_time_seconds
            if body.time_budget_seconds is None
            else body.time_budget_seconds
        ),
        "max_continuations": (
            settings.goal_default_max_continuations
            if body.max_continuations is None
            else body.max_continuations
        ),
    }
    _validate_budget_caps(values, settings)
    return values


def _validate_budget_caps(
    values: dict[str, int | None],
    settings: Settings,
) -> None:
    caps = {
        "token_budget": settings.goal_max_token_budget,
        "cost_budget_microusd": settings.goal_max_cost_budget_microusd,
        "time_budget_seconds": settings.goal_max_active_time_seconds,
        "max_continuations": settings.goal_max_continuations,
    }
    for field, value in values.items():
        if value is None:
            continue
        if value < 0:
            raise GoalValidationError(f"{field} must be non-negative")
        maximum = caps[field]
        if maximum is not None and value > maximum:
            raise GoalBudgetLimitError(field=field, maximum=maximum)


async def create_session_goal(
    db: AsyncSession,
    session_id: str,
    body: GoalCreateRequest,
    *,
    settings: Settings | None = None,
) -> SessionGoal:
    """Create the session's only Goal and durably deduplicate the request."""

    scope = f"goal.create:{session_id}"
    payload = _goal_request_payload(body, exclude_unset=False)
    request_hash = canonical_request_hash(payload)
    replay = await _idempotent_replay(
        db,
        scope=scope,
        request_key=body.client_request_id,
        request_hash=request_hash,
    )
    if replay.found:
        if replay.goal is None:
            raise GoalIdempotencyConflictError("The create request was already cleared")
        return replay.goal

    session = await db.get(Session, session_id)
    if session is None:
        raise GoalNotFoundError("Session not found")
    if await get_session_goal(db, session_id) is not None:
        raise GoalAlreadyExistsError("This session already has a Goal")

    app_settings = _settings(settings)
    budgets = _resolve_create_budgets(body, app_settings)
    goal = SessionGoal(
        id=generate_ulid(),
        session_id=session_id,
        objective=body.objective,
        definition_of_done=body.definition_of_done,
        status="active",
        run_state="idle",
        revision=1,
        **budgets,
        model_id=body.model_id or session.model_id,
        provider_id=body.provider_id or session.provider_id,
        agent=body.agent,
        reasoning=body.reasoning,
        language=body.language,
        permission_snapshot=deepcopy(session.permission_snapshot),
    )
    db.add(goal)
    await db.flush()
    await _record_idempotent_success(
        db,
        scope=scope,
        request_key=body.client_request_id,
        request_hash=request_hash,
        goal_id=goal.id,
        goal_revision=goal.revision,
    )
    return goal


async def _raise_revision_or_not_found(
    db: AsyncSession,
    *,
    session_id: str | None = None,
    goal_id: str | None = None,
    expected_revision: int,
) -> None:
    if session_id is not None:
        current = await get_session_goal(db, session_id)
    elif goal_id is not None:
        current = await get_goal_by_id(db, goal_id)
    else:  # pragma: no cover - internal programming guard
        raise RuntimeError("session_id or goal_id is required")
    if current is None:
        raise GoalNotFoundError("Goal not found")
    raise GoalRevisionConflictError(
        expected_revision=expected_revision,
        current_revision=current.revision,
    )


async def _refresh_goal(db: AsyncSession, goal_id: str) -> SessionGoal:
    goal = (
        await db.execute(
            select(SessionGoal)
            .where(SessionGoal.id == goal_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if goal is None:  # pragma: no cover - protected by the preceding mutation
        raise GoalNotFoundError("Goal not found")
    return goal


def _update_values(
    goal: SessionGoal,
    body: GoalUpdateRequest,
    settings: Settings,
) -> dict[str, Any]:
    fields = body.model_fields_set
    values: dict[str, Any] = {}
    prospective_objective = goal.objective
    prospective_definition = goal.definition_of_done
    if "objective" in fields:
        if body.objective is None:
            raise GoalValidationError("objective must not be empty")
        prospective_objective = body.objective
        values["objective"] = body.objective
    if "definition_of_done" in fields:
        prospective_definition = body.definition_of_done
        values["definition_of_done"] = body.definition_of_done
    if len(prospective_objective) + len(prospective_definition or "") > 4000:
        raise GoalValidationError(
            "objective and definition_of_done must total at most 4000 characters"
        )

    budget_defaults = {
        "token_budget": settings.goal_default_token_budget,
        "cost_budget_microusd": settings.goal_default_cost_budget_microusd,
        "time_budget_seconds": settings.goal_default_active_time_seconds,
        "max_continuations": settings.goal_default_max_continuations,
    }
    changed_budgets: dict[str, int | None] = {}
    for field, default in budget_defaults.items():
        if field in fields:
            incoming = getattr(body, field)
            changed_budgets[field] = default if incoming is None else incoming
    _validate_budget_caps(changed_budgets, settings)
    values.update(changed_budgets)

    for field in ("model_id", "provider_id", "reasoning"):
        if field in fields:
            values[field] = getattr(body, field)
    for field in ("agent", "language"):
        if field in fields:
            incoming = getattr(body, field)
            if incoming is None:
                raise GoalValidationError(f"{field} must not be null")
            values[field] = incoming

    if goal.status == "active" and goal.run_state in PAUSABLE_RUN_STATES:
        # Reuse the pausing execution gate but mark this as an edit boundary,
        # not a user pause. finish_goal_run will keep the Goal active and the
        # controller will reserve a fresh run against the new revision.
        values.update(
            run_state="pausing",
            blocker_code="goal_edited",
            blocker_message="Applying the edited Goal at the next safe boundary",
            needs_review=False,
        )
    return values


async def update_session_goal(
    db: AsyncSession,
    session_id: str,
    body: GoalUpdateRequest,
    *,
    settings: Settings | None = None,
) -> SessionGoal:
    scope = f"goal.update:{session_id}"
    payload = _goal_request_payload(body, exclude_unset=True)
    request_hash = canonical_request_hash(payload)
    replay = await _idempotent_replay(
        db,
        scope=scope,
        request_key=body.client_request_id,
        request_hash=request_hash,
    )
    if replay.found:
        if replay.goal is None:
            raise GoalIdempotencyConflictError("The update request was already cleared")
        return replay.goal

    goal = await get_session_goal(db, session_id)
    if goal is None:
        raise GoalNotFoundError("Goal not found")
    values = _update_values(goal, body, _settings(settings))
    values["revision"] = body.expected_revision + 1
    result = await db.execute(
        update(SessionGoal)
        .where(
            SessionGoal.id == goal.id,
            SessionGoal.revision == body.expected_revision,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await _raise_revision_or_not_found(
            db,
            session_id=session_id,
            expected_revision=body.expected_revision,
        )
    goal = await _refresh_goal(db, goal.id)
    await _record_idempotent_success(
        db,
        scope=scope,
        request_key=body.client_request_id,
        request_hash=request_hash,
        goal_id=goal.id,
        goal_revision=goal.revision,
    )
    return goal


async def pause_session_goal(
    db: AsyncSession,
    session_id: str,
    body: GoalControlRequest,
) -> SessionGoal:
    scope = f"goal.pause:{session_id}"
    request_hash = canonical_request_hash(
        _goal_request_payload(body, exclude_unset=False)
    )
    replay = await _idempotent_replay(
        db,
        scope=scope,
        request_key=body.client_request_id,
        request_hash=request_hash,
    )
    if replay.found:
        if replay.goal is None:
            raise GoalIdempotencyConflictError("The pause request was already cleared")
        return replay.goal

    goal = await get_session_goal(db, session_id)
    if goal is None:
        raise GoalNotFoundError("Goal not found")
    if goal.revision != body.expected_revision:
        raise GoalRevisionConflictError(
            expected_revision=body.expected_revision,
            current_revision=goal.revision,
        )
    if goal.status == "paused" or (
        goal.status == "active"
        and goal.run_state == "pausing"
        and goal.blocker_code != "goal_edited"
    ):
        await _record_idempotent_success(
            db,
            scope=scope,
            request_key=body.client_request_id,
            request_hash=request_hash,
            goal_id=goal.id,
            goal_revision=goal.revision,
        )
        return goal
    if goal.status != "active":
        raise GoalInvalidTransitionError(
            f"Cannot pause a Goal in {goal.status!r} status"
        )

    values: dict[str, Any] = {"revision": body.expected_revision + 1}
    if goal.run_state in PAUSABLE_RUN_STATES or (
        goal.run_state == "pausing" and goal.blocker_code == "goal_edited"
    ):
        values.update(
            run_state="pausing",
            blocker_code="user_pause",
            blocker_message=None,
        )
    else:
        values.update(status="paused", run_state="idle")
    result = await db.execute(
        update(SessionGoal)
        .where(
            SessionGoal.id == goal.id,
            SessionGoal.revision == body.expected_revision,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await _raise_revision_or_not_found(
            db,
            session_id=session_id,
            expected_revision=body.expected_revision,
        )
    goal = await _refresh_goal(db, goal.id)
    await _record_idempotent_success(
        db,
        scope=scope,
        request_key=body.client_request_id,
        request_hash=request_hash,
        goal_id=goal.id,
        goal_revision=goal.revision,
    )
    return goal


def _assert_remaining_budget(
    goal: SessionGoal,
    *,
    include_continuations: bool = True,
) -> None:
    checks = [
        (goal.token_budget, goal.tokens_used, "token"),
        (goal.cost_budget_microusd, goal.cost_used_microusd, "cost"),
        (goal.time_budget_seconds, goal.time_used_seconds, "time"),
    ]
    if include_continuations:
        checks.append(
            (goal.max_continuations, goal.continuation_count, "continuation")
        )
    for limit, used, name in checks:
        if limit is not None and used >= limit:
            raise GoalInvalidTransitionError(
                f"Cannot resume until the {name} budget is increased"
            )


async def resume_session_goal(
    db: AsyncSession,
    session_id: str,
    body: GoalControlRequest,
) -> SessionGoal:
    scope = f"goal.resume:{session_id}"
    request_hash = canonical_request_hash(
        _goal_request_payload(body, exclude_unset=False)
    )
    replay = await _idempotent_replay(
        db,
        scope=scope,
        request_key=body.client_request_id,
        request_hash=request_hash,
    )
    if replay.found:
        if replay.goal is None:
            raise GoalIdempotencyConflictError("The resume request was already cleared")
        return replay.goal

    goal = await get_session_goal(db, session_id)
    if goal is None:
        raise GoalNotFoundError("Goal not found")
    if goal.revision != body.expected_revision:
        raise GoalRevisionConflictError(
            expected_revision=body.expected_revision,
            current_revision=goal.revision,
        )
    if goal.status == "active" and goal.run_state == "idle":
        await _record_idempotent_success(
            db,
            scope=scope,
            request_key=body.client_request_id,
            request_hash=request_hash,
            goal_id=goal.id,
            goal_revision=goal.revision,
        )
        return goal
    if "active" not in LEGAL_STATUS_TRANSITIONS.get(goal.status, frozenset()):
        raise GoalInvalidTransitionError(
            f"Cannot resume a Goal in {goal.status!r}/{goal.run_state!r} state"
        )
    _assert_remaining_budget(goal)
    result = await db.execute(
        update(SessionGoal)
        .where(
            SessionGoal.id == goal.id,
            SessionGoal.revision == body.expected_revision,
        )
        .values(
            status="active",
            run_state="idle",
            revision=body.expected_revision + 1,
            blocker_code=None,
            blocker_message=None,
            needs_review=False,
            next_retry_at=None,
            completion_summary=None,
            completion_evidence=None,
            time_completed=None,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await _raise_revision_or_not_found(
            db,
            session_id=session_id,
            expected_revision=body.expected_revision,
        )
    goal = await _refresh_goal(db, goal.id)
    await _record_idempotent_success(
        db,
        scope=scope,
        request_key=body.client_request_id,
        request_hash=request_hash,
        goal_id=goal.id,
        goal_revision=goal.revision,
    )
    return goal


async def clear_session_goal(
    db: AsyncSession,
    session_id: str,
    body: GoalControlRequest,
) -> bool:
    scope = f"goal.clear:{session_id}"
    request_hash = canonical_request_hash(
        _goal_request_payload(body, exclude_unset=False)
    )
    replay = await _idempotent_replay(
        db,
        scope=scope,
        request_key=body.client_request_id,
        request_hash=request_hash,
    )
    if replay.found:
        if replay.deleted:
            return False
        raise GoalIdempotencyConflictError("The clear request has an invalid response")

    goal = await get_session_goal(db, session_id)
    if goal is None:
        raise GoalNotFoundError("Goal not found")
    if goal.revision != body.expected_revision:
        raise GoalRevisionConflictError(
            expected_revision=body.expected_revision,
            current_revision=goal.revision,
        )
    if goal.run_state in PAUSABLE_RUN_STATES or goal.run_state == "pausing":
        raise GoalInvalidTransitionError(
            "Pause the active Goal run before clearing the Goal"
        )
    # The conditional revision write is the clear operation's CAS. Deleting
    # the refreshed ORM instance afterwards preserves relationship cascades in
    # test SQLite connections where PRAGMA foreign_keys may not be enabled.
    cas = await db.execute(
        update(SessionGoal)
        .where(
            SessionGoal.id == goal.id,
            SessionGoal.revision == body.expected_revision,
        )
        .values(revision=body.expected_revision + 1)
        .execution_options(synchronize_session=False)
    )
    if cas.rowcount != 1:
        await _raise_revision_or_not_found(
            db,
            session_id=session_id,
            expected_revision=body.expected_revision,
        )
    goal = await _refresh_goal(db, goal.id)
    await db.delete(goal)
    await db.flush()
    await _record_idempotent_success(
        db,
        scope=scope,
        request_key=body.client_request_id,
        request_hash=request_hash,
        goal_id=None,
        deleted=True,
    )
    return True


async def transition_goal_status(
    db: AsyncSession,
    *,
    goal_id: str,
    expected_revision: int,
    target_status: Literal[
        "active",
        "paused",
        "blocked",
        "usage_limited",
        "budget_limited",
        "complete",
    ],
    blocker_code: str | None = None,
    blocker_message: str | None = None,
    needs_review: bool = False,
    completion_summary: str | None = None,
    completion_evidence: dict[str, Any] | list[Any] | None = None,
) -> SessionGoal:
    """Apply a legal runtime/model status transition with revision CAS."""

    goal = await get_goal_by_id(db, goal_id)
    if goal is None:
        raise GoalNotFoundError("Goal not found")
    if goal.revision != expected_revision:
        raise GoalRevisionConflictError(
            expected_revision=expected_revision,
            current_revision=goal.revision,
        )
    if target_status not in LEGAL_STATUS_TRANSITIONS.get(goal.status, frozenset()):
        raise GoalInvalidTransitionError(
            f"Illegal Goal transition {goal.status!r} -> {target_status!r}"
        )
    if target_status == "active":
        _assert_remaining_budget(goal)
    if target_status == "complete" and (
        not (completion_summary or "").strip() or completion_evidence is None
    ):
        raise GoalValidationError(
            "Completing a Goal requires a summary and structured evidence"
        )

    values: dict[str, Any] = {
        "status": target_status,
        "run_state": "idle",
        "revision": expected_revision + 1,
        "needs_review": needs_review,
    }
    if target_status == "active":
        values.update(
            blocker_code=None,
            blocker_message=None,
            next_retry_at=None,
            completion_summary=None,
            completion_evidence=None,
            time_completed=None,
        )
    else:
        values.update(
            blocker_code=blocker_code,
            blocker_message=blocker_message,
        )
    if target_status == "complete":
        values.update(
            completion_summary=completion_summary.strip(),
            completion_evidence=completion_evidence,
            time_completed=_now(),
        )
    result = await db.execute(
        update(SessionGoal)
        .where(
            SessionGoal.id == goal_id,
            SessionGoal.revision == expected_revision,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await _raise_revision_or_not_found(
            db,
            goal_id=goal_id,
            expected_revision=expected_revision,
        )
    return await _refresh_goal(db, goal_id)


async def pause_active_goal_for_archive(
    db: AsyncSession,
    session_id: str,
) -> bool:
    """Fail closed when a conversation is archived.

    Archiving is a session lifecycle action rather than a revision-aware Goal
    command. The conditional update therefore pauses whichever active revision
    is current in the same transaction as the Session archive. An in-flight
    run becomes ``interrupted`` and is never replayed automatically.
    """

    result = await db.execute(
        update(SessionGoal)
        .where(
            SessionGoal.session_id == session_id,
            SessionGoal.status == "active",
        )
        .values(
            status="paused",
            run_state=case(
                (SessionGoal.run_state == "idle", "idle"),
                else_="interrupted",
            ),
            revision=SessionGoal.revision + 1,
            blocker_code="session_archived",
            blocker_message="Review this Goal before resuming the archived session",
            needs_review=True,
            next_retry_at=None,
        )
        .execution_options(synchronize_session="fetch")
    )
    await db.flush()
    return result.rowcount == 1


async def request_immediate_goal_stop(
    db: AsyncSession,
    *,
    goal_id: str,
    reason_code: str = "immediate_stop",
    reason_message: str = "Immediate stop requested; review possible partial side effects",
) -> SessionGoal | None:
    """Close future admission before cancelling in-flight Goal side effects."""

    goal = await get_goal_by_id(db, goal_id)
    if goal is None:
        return None
    if goal.status != "active":
        return goal
    values: dict[str, Any] = {
        "revision": goal.revision + 1,
        "needs_review": True,
        "blocker_code": reason_code,
        "blocker_message": reason_message,
    }
    if goal.run_state in PAUSABLE_RUN_STATES or goal.run_state == "pausing":
        values["run_state"] = "pausing"
    else:
        values.update(status="paused", run_state="idle")
    result = await db.execute(
        update(SessionGoal)
        .where(
            SessionGoal.id == goal.id,
            SessionGoal.revision == goal.revision,
            SessionGoal.status == "active",
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        return await get_goal_by_id(db, goal_id)
    return await _refresh_goal(db, goal_id)


async def prepare_active_goals_for_emergency_stop(db: AsyncSession) -> int:
    """Persist a fail-closed gate before the runtime cancels active jobs."""

    result = await db.execute(
        update(SessionGoal)
        .where(SessionGoal.status == "active")
        .values(
            status=case(
                (SessionGoal.run_state == "idle", "paused"),
                else_=SessionGoal.status,
            ),
            run_state=case(
                (SessionGoal.run_state == "idle", "idle"),
                else_="pausing",
            ),
            revision=SessionGoal.revision + 1,
            needs_review=True,
            blocker_code="security_emergency_stop",
            blocker_message=(
                "Security emergency stop interrupted autonomous work; review before resuming"
            ),
        )
        .execution_options(synchronize_session="fetch")
    )
    await db.flush()
    return int(result.rowcount or 0)


async def prepare_active_goals_for_shutdown(db: AsyncSession) -> int:
    """Make normal application exit converge on paused, reviewable Goals."""

    result = await db.execute(
        update(SessionGoal)
        .where(SessionGoal.status == "active")
        .values(
            status=case(
                (SessionGoal.run_state == "idle", "paused"),
                else_=SessionGoal.status,
            ),
            run_state=case(
                (SessionGoal.run_state == "idle", "idle"),
                else_="pausing",
            ),
            revision=SessionGoal.revision + 1,
            needs_review=True,
            blocker_code="application_shutdown",
            blocker_message=(
                "The application stopped this Goal; review the last boundary before resuming"
            ),
        )
        .execution_options(synchronize_session="fetch")
    )
    await db.flush()
    return int(result.rowcount or 0)


def _require_goal_run_trigger(trigger: GoalRunTrigger) -> None:
    # The persistent Goal gate owns explicit initial/resume/user-input slices.
    # The stricter autonomous gate controls only unattended continuation.
    if trigger == "auto" and not release_features.AUTONOMOUS_GOALS_RELEASED:
        raise AutonomousGoalsUnavailableError(
            "Autonomous Goal continuations are not available in this release"
        )


async def reserve_goal_run(
    db: AsyncSession,
    *,
    goal_id: str,
    expected_revision: int,
    idempotency_key: str,
    trigger: GoalRunTrigger,
    stream_id: str | None = None,
) -> GoalRunReservation:
    """Atomically reserve at most one active run for an active Goal."""

    _require_goal_run_trigger(trigger)
    existing = (
        await db.execute(
            select(GoalRun).where(GoalRun.idempotency_key == idempotency_key)
        )
    ).scalar_one_or_none()
    if existing is not None:
        if (
            existing.goal_id != goal_id
            or existing.trigger != trigger
            or existing.stream_id != stream_id
        ):
            raise GoalIdempotencyConflictError(
                "The GoalRun idempotency key was reused with different parameters"
            )
        goal = await get_goal_by_id(db, goal_id)
        if goal is None:
            raise GoalNotFoundError("Goal not found")
        return GoalRunReservation(goal=goal, run=existing, idempotent=True)

    if len(idempotency_key) < 8 or len(idempotency_key) > 128:
        raise GoalValidationError("idempotency_key must contain 8 to 128 characters")
    goal = await get_goal_by_id(db, goal_id)
    if goal is None:
        raise GoalNotFoundError("Goal not found")
    if goal.revision != expected_revision:
        raise GoalRevisionConflictError(
            expected_revision=expected_revision,
            current_revision=goal.revision,
        )
    if goal.status != "active" or goal.run_state not in {"idle", "interrupted"}:
        raise GoalRunConflictError(
            f"Goal cannot reserve a run from {goal.status!r}/{goal.run_state!r}"
        )
    # ``max_continuations`` is an autonomous-loop ceiling, not a limit on the
    # initial slice, a real user-input slice, or an explicit manual resume.
    _assert_remaining_budget(goal, include_continuations=trigger == "auto")
    active = (
        await db.execute(
            select(GoalRun.id).where(
                GoalRun.goal_id == goal_id,
                GoalRun.status.in_(ACTIVE_GOAL_RUN_STATUSES),
            )
        )
    ).scalar_one_or_none()
    if active is not None:
        raise GoalRunConflictError("This Goal already has an active run")
    ordinal = int(
        (
            await db.execute(
                select(func.coalesce(func.max(GoalRun.ordinal), 0)).where(
                    GoalRun.goal_id == goal_id
                )
            )
        ).scalar_one()
    ) + 1
    run_id = generate_ulid()
    next_revision = expected_revision + 1
    continuation_delta = 1 if trigger == "auto" else 0
    cas = await db.execute(
        update(SessionGoal)
        .where(
            SessionGoal.id == goal_id,
            SessionGoal.revision == expected_revision,
            SessionGoal.status == "active",
            SessionGoal.run_state.in_(("idle", "interrupted")),
        )
        .values(
            revision=next_revision,
            run_state="reserved",
            last_run_id=run_id,
            last_stream_id=stream_id,
            continuation_count=SessionGoal.continuation_count
            + continuation_delta,
        )
        .execution_options(synchronize_session=False)
    )
    if cas.rowcount != 1:
        await _raise_revision_or_not_found(
            db,
            goal_id=goal_id,
            expected_revision=expected_revision,
        )
    run = GoalRun(
        id=run_id,
        goal_id=goal_id,
        ordinal=ordinal,
        goal_revision=next_revision,
        idempotency_key=idempotency_key,
        stream_id=stream_id,
        trigger=trigger,
        status="reserved",
    )
    db.add(run)
    await db.flush()
    return GoalRunReservation(
        goal=await _refresh_goal(db, goal_id),
        run=run,
    )


async def start_goal_run(
    db: AsyncSession,
    run_id: str,
    *,
    lease_owner: str | None = None,
    lease_expires_at: datetime | None = None,
) -> GoalRunUpdate:
    """Move a reservation to running only if its Goal was not edited/paused."""

    run = await db.get(GoalRun, run_id)
    if run is None:
        raise GoalNotFoundError("Goal run not found")
    goal = await get_goal_by_id(db, run.goal_id)
    if goal is None:
        raise GoalNotFoundError("Goal not found")
    if run.status == "running":
        return GoalRunUpdate(goal=goal, run=run)
    if run.status != "reserved":
        raise GoalRunConflictError(f"Cannot start a {run.status!r} Goal run")
    cas = await db.execute(
        update(SessionGoal)
        .where(
            SessionGoal.id == goal.id,
            SessionGoal.revision == run.goal_revision,
            SessionGoal.status == "active",
            SessionGoal.run_state == "reserved",
            SessionGoal.last_run_id == run.id,
        )
        .values(
            revision=run.goal_revision + 1,
            run_state="running",
            time_started=func.coalesce(SessionGoal.time_started, _now()),
        )
        .execution_options(synchronize_session=False)
    )
    if cas.rowcount != 1:
        raise GoalRevisionConflictError(
            expected_revision=run.goal_revision,
            current_revision=goal.revision,
        )
    await db.execute(
        update(GoalRun)
        .where(GoalRun.id == run.id, GoalRun.status == "reserved")
        .values(
            status="running",
            lease_owner=lease_owner,
            lease_expires_at=lease_expires_at,
            time_started=_now(),
        )
        .execution_options(synchronize_session=False)
    )
    await db.flush()
    run = (
        await db.execute(
            select(GoalRun)
            .where(GoalRun.id == run.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return GoalRunUpdate(goal=await _refresh_goal(db, goal.id), run=run)


async def finish_goal_run(
    db: AsyncSession,
    run_id: str,
    *,
    status: Literal["completed", "blocked", "interrupted", "failed"],
    tokens_used: int = 0,
    cost_used_microusd: int = 0,
    active_seconds: int = 0,
    progress_summary: str | None = None,
    stop_reason: str | None = None,
    error_code: str | None = None,
) -> GoalRunUpdate:
    """Finalize one run and reconcile its aggregate usage exactly once."""

    if min(tokens_used, cost_used_microusd, active_seconds) < 0:
        raise GoalValidationError("Goal run usage values must be non-negative")
    run = await db.get(GoalRun, run_id)
    if run is None:
        raise GoalNotFoundError("Goal run not found")
    goal = await get_goal_by_id(db, run.goal_id)
    if goal is None:
        raise GoalNotFoundError("Goal not found")
    if run.status in TERMINAL_GOAL_RUN_STATUSES:
        return GoalRunUpdate(goal=goal, run=run)
    if run.status not in ACTIVE_GOAL_RUN_STATUSES:
        raise GoalRunConflictError(f"Cannot finish a {run.status!r} Goal run")

    recorded_tokens, recorded_cost = await get_goal_run_recorded_usage(db, run.id)
    tokens_used = max(tokens_used, recorded_tokens)
    cost_used_microusd = max(cost_used_microusd, recorded_cost)
    now = _now()
    run_update = await db.execute(
        update(GoalRun)
        .where(
            GoalRun.id == run.id,
            GoalRun.status.in_(ACTIVE_GOAL_RUN_STATUSES),
        )
        .values(
            status=status,
            tokens_used=tokens_used,
            cost_used_microusd=cost_used_microusd,
            active_seconds=active_seconds,
            progress_summary=progress_summary,
            stop_reason=stop_reason,
            error_code=error_code,
            lease_owner=None,
            lease_expires_at=None,
            time_finished=now,
        )
        .execution_options(synchronize_session=False)
    )
    if run_update.rowcount != 1:
        raise GoalRunConflictError("Goal run was finalized concurrently")

    goal_values: dict[str, Any] = {
        "revision": goal.revision + 1,
        "run_state": "idle",
        "tokens_used": SessionGoal.tokens_used + tokens_used,
        "cost_used_microusd": SessionGoal.cost_used_microusd
        + cost_used_microusd,
        "time_used_seconds": SessionGoal.time_used_seconds + active_seconds,
    }
    if goal.run_state == "pausing" and goal.blocker_code == "goal_edited":
        goal_values.update(
            status="active",
            run_state="idle",
            blocker_code=None,
            blocker_message=None,
            needs_review=False,
        )
    elif goal.run_state == "pausing":
        goal_values.update(status="paused", needs_review=status != "completed")
    elif (
        status == "failed"
        and error_code in {"retryable_generation_error", "usage_limited"}
        and goal.status == "active"
    ):
        # The controller owns bounded retry/usage-limit policy. Reconcile this
        # run's ledger while leaving the Goal active/idle so it can atomically
        # increment the breaker or move to usage_limited next.
        pass
    elif status in {"blocked", "failed", "interrupted"} and goal.status == "active":
        goal_values.update(
            status="blocked",
            blocker_code=error_code or status,
            blocker_message=stop_reason,
            needs_review=status in {"failed", "interrupted"},
        )
        if status == "interrupted":
            goal_values["run_state"] = "interrupted"
    goal_update = await db.execute(
        update(SessionGoal)
        .where(
            SessionGoal.id == goal.id,
            SessionGoal.revision == goal.revision,
            SessionGoal.last_run_id == run.id,
        )
        .values(**goal_values)
        .execution_options(synchronize_session=False)
    )
    if goal_update.rowcount != 1:
        raise GoalRevisionConflictError(
            expected_revision=goal.revision,
            current_revision=(await get_goal_by_id(db, goal.id)).revision,
        )
    await db.flush()
    refreshed_run = (
        await db.execute(
            select(GoalRun)
            .where(GoalRun.id == run.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return GoalRunUpdate(
        goal=await _refresh_goal(db, goal.id),
        run=refreshed_run,
    )


async def record_goal_run_usage(
    db: AsyncSession,
    *,
    goal_run_id: str,
    source_kind: str,
    source_key: str,
    tokens_used: int,
    cost_used_microusd: int,
) -> GoalUsageRecord:
    """Persist one Provider/child/compaction/repair charge exactly once."""

    def _validate_replay(record: GoalUsageRecord) -> GoalUsageRecord:
        if (
            record.source_kind != normalized_kind
            or record.tokens_used != tokens_used
            or record.cost_used_microusd != cost_used_microusd
        ):
            raise GoalRunConflictError(
                "A Goal usage source was replayed with different accounting"
            )
        return record

    if min(tokens_used, cost_used_microusd) < 0:
        raise GoalValidationError("Goal usage values must be non-negative")
    run = await db.get(GoalRun, goal_run_id)
    if run is None:
        raise GoalNotFoundError("Goal run not found")
    normalized_kind = " ".join(source_kind.split())[:32]
    normalized_key = " ".join(source_key.split())[:255]
    if not normalized_kind or not normalized_key:
        raise GoalValidationError("Goal usage source identity is required")
    existing = (
        await db.execute(
            select(GoalUsageRecord).where(
                GoalUsageRecord.goal_run_id == goal_run_id,
                GoalUsageRecord.source_key == normalized_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return _validate_replay(existing)
    record = GoalUsageRecord(
        goal_run_id=goal_run_id,
        source_kind=normalized_kind,
        source_key=normalized_key,
        tokens_used=tokens_used,
        cost_used_microusd=cost_used_microusd,
    )
    try:
        # A read-before-insert check alone is racy.  Isolate the INSERT in a
        # savepoint so a concurrent writer of the same source cannot poison
        # the caller's outer transaction; after the unique conflict we can
        # validate and reuse the winning durable row.
        async with db.begin_nested():
            db.add(record)
            await db.flush()
        return record
    except IntegrityError:
        existing = (
            await db.execute(
                select(GoalUsageRecord).where(
                    GoalUsageRecord.goal_run_id == goal_run_id,
                    GoalUsageRecord.source_key == normalized_key,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            raise
        return _validate_replay(existing)


async def get_goal_run_recorded_usage(
    db: AsyncSession,
    goal_run_id: str,
) -> tuple[int, int]:
    row = (
        await db.execute(
            select(
                func.coalesce(func.sum(GoalUsageRecord.tokens_used), 0),
                func.coalesce(func.sum(GoalUsageRecord.cost_used_microusd), 0),
            ).where(GoalUsageRecord.goal_run_id == goal_run_id)
        )
    ).one()
    return int(row[0] or 0), int(row[1] or 0)


_CANONICAL_TOKEN_COMPONENTS = ("input", "output", "reasoning", "cache_read")


def _persisted_token_components(data: Any) -> tuple[int, int, int, int] | None:
    """Read one persisted Provider token payload without trusting its total."""

    if not isinstance(data, dict):
        return None
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        return None
    values: list[int] = []
    for key in _CANONICAL_TOKEN_COMPONENTS:
        try:
            value = int(tokens.get(key, 0) or 0)
        except (TypeError, ValueError, OverflowError):
            value = 0
        values.append(max(0, value))
    return values[0], values[1], values[2], values[3]


def _usage_source_id(record: GoalUsageRecord, source_kind: str) -> str | None:
    if record.source_kind != source_kind:
        return None
    prefix = f"{source_kind}:"
    if not record.source_key.startswith(prefix):
        return None
    source_id = record.source_key[len(prefix):]
    return source_id or None


async def get_goal_token_usage_breakdown(
    db: AsyncSession,
    goal_id: str,
) -> GoalTokenUsageBreakdown:
    """Reconcile the Goal ledger with persisted per-call token components.

    GoalUsageRecord remains the authoritative, idempotent usage ledger.  This
    read model resolves each Provider/compaction source back to its stored token
    payload only when the four canonical components add up to the ledger row.
    Missing or legacy payloads are retained as ``unattributed`` instead of
    being guessed, discarded, or counted twice.

    Terminal GoalRun totals may exceed their source rows after crash recovery,
    and pre-ledger Goal rows may have no GoalRun detail at all.  The two gap
    calculations below preserve those historic totals exactly.  Active source
    rows are included once so the UI can show current spend before finalization.
    """

    goal = await get_goal_by_id(db, goal_id)
    if goal is None:
        raise GoalNotFoundError("Goal not found")

    runs = list(
        (
            await db.execute(
                select(GoalRun).where(GoalRun.goal_id == goal_id)
            )
        ).scalars()
    )
    run_ids = [run.id for run in runs]
    records = (
        list(
            (
                await db.execute(
                    select(GoalUsageRecord).where(
                        GoalUsageRecord.goal_run_id.in_(run_ids)
                    )
                )
            ).scalars()
        )
        if run_ids
        else []
    )

    provider_ids = {
        source_id
        for record in records
        if (source_id := _usage_source_id(record, "provider")) is not None
    }
    compaction_ids = {
        source_id
        for record in records
        if (source_id := _usage_source_id(record, "compaction")) is not None
    }
    office_repair_ids = {
        source_id
        for record in records
        if (source_id := _usage_source_id(record, "office_repair")) is not None
    }

    provider_payloads: dict[
        str, list[tuple[str | None, tuple[int, int, int, int]]]
    ] = {}
    if provider_ids:
        parts = list(
            (
                await db.execute(
                    select(Part).where(Part.message_id.in_(provider_ids))
                )
            ).scalars()
        )
        for part in parts:
            data = part.data or {}
            if data.get("type") != "step-finish":
                continue
            components = _persisted_token_components(data)
            if components is None:
                continue
            payload_run_id = data.get("goal_run_id")
            provider_payloads.setdefault(part.message_id, []).append(
                (
                    payload_run_id if isinstance(payload_run_id, str) else None,
                    components,
                )
            )

    compaction_payloads: dict[
        str, tuple[str | None, tuple[int, int, int, int]]
    ] = {}
    if compaction_ids:
        messages = list(
            (
                await db.execute(
                    select(Message).where(Message.id.in_(compaction_ids))
                )
            ).scalars()
        )
        for message in messages:
            data = message.data or {}
            if data.get("agent") != "compaction" or not data.get("system"):
                continue
            components = _persisted_token_components(data)
            if components is None:
                continue
            payload_run_id = data.get("goal_run_id")
            compaction_payloads[message.id] = (
                payload_run_id if isinstance(payload_run_id, str) else None,
                components,
            )

    office_repair_payloads: dict[
        str, tuple[str | None, tuple[int, int, int, int]]
    ] = {}
    if office_repair_ids:
        parts = list(
            (
                await db.execute(
                    select(Part).where(Part.id.in_(office_repair_ids))
                )
            ).scalars()
        )
        for part in parts:
            data = part.data or {}
            if data.get("type") != "office-repair-usage":
                continue
            components = _persisted_token_components(data)
            if components is None:
                continue
            payload_run_id = data.get("goal_run_id")
            office_repair_payloads[part.id] = (
                payload_run_id if isinstance(payload_run_id, str) else None,
                components,
            )

    input_tokens = 0
    output_tokens = 0
    reasoning_tokens = 0
    cache_read_tokens = 0
    unattributed_tokens = 0
    recorded_by_run: dict[str, int] = {}

    for record in records:
        recorded_tokens = max(0, int(record.tokens_used or 0))
        recorded_by_run[record.goal_run_id] = (
            recorded_by_run.get(record.goal_run_id, 0) + recorded_tokens
        )
        components: tuple[int, int, int, int] | None = None
        provider_id = _usage_source_id(record, "provider")
        if provider_id is not None:
            matching = [
                payload
                for payload_run_id, payload in provider_payloads.get(provider_id, [])
                if payload_run_id is None or payload_run_id == record.goal_run_id
            ]
            if matching:
                components = (
                    sum(payload[0] for payload in matching),
                    sum(payload[1] for payload in matching),
                    sum(payload[2] for payload in matching),
                    sum(payload[3] for payload in matching),
                )
        else:
            compaction_id = _usage_source_id(record, "compaction")
            persisted = (
                compaction_payloads.get(compaction_id)
                if compaction_id is not None
                else None
            )
            if persisted is not None:
                payload_run_id, payload = persisted
                if payload_run_id is None or payload_run_id == record.goal_run_id:
                    components = payload
            else:
                office_repair_id = _usage_source_id(record, "office_repair")
                repair_persisted = (
                    office_repair_payloads.get(office_repair_id)
                    if office_repair_id is not None
                    else None
                )
                if repair_persisted is not None:
                    payload_run_id, payload = repair_persisted
                    if (
                        payload_run_id is None
                        or payload_run_id == record.goal_run_id
                    ):
                        components = payload

        if components is None or sum(components) != recorded_tokens:
            unattributed_tokens += recorded_tokens
            continue
        input_tokens += components[0]
        output_tokens += components[1]
        reasoning_tokens += components[2]
        cache_read_tokens += components[3]

    terminal_run_tokens = 0
    for run in runs:
        if run.status not in TERMINAL_GOAL_RUN_STATUSES:
            continue
        run_tokens = max(0, int(run.tokens_used or 0))
        terminal_run_tokens += run_tokens
        unattributed_tokens += max(
            0,
            run_tokens - recorded_by_run.get(run.id, 0),
        )

    # Older databases can have a committed Goal aggregate with no GoalRun or
    # source ledger detail.  Keep it visible and exact rather than rewriting
    # user history during migration.
    unattributed_tokens += max(
        0,
        max(0, int(goal.tokens_used or 0)) - terminal_run_tokens,
    )
    total_tokens = (
        input_tokens
        + output_tokens
        + reasoning_tokens
        + cache_read_tokens
        + unattributed_tokens
    )
    return GoalTokenUsageBreakdown(
        input=input_tokens,
        output=output_tokens,
        reasoning=reasoning_tokens,
        cache_read=cache_read_tokens,
        unattributed=unattributed_tokens,
        total_tokens=total_tokens,
        source_count=len(records),
    )


async def interrupt_inflight_goal_runs(db: AsyncSession) -> int:
    """Fail closed after restart without replaying uncertain side effects."""

    active_runs = list(
        (
            await db.execute(
                select(GoalRun).where(
                    GoalRun.status.in_(ACTIVE_GOAL_RUN_STATUSES)
                )
            )
        ).scalars()
    )
    if not active_runs:
        return 0
    now = _now()
    session_rows = list(
        (await db.execute(select(Session.id, Session.parent_id))).all()
    )
    usage_by_goal: dict[str, tuple[int, int]] = {}
    for run in active_runs:
        goal = await db.get(SessionGoal, run.goal_id)
        recovered_tokens = 0
        recovered_cost = 0
        if goal is not None:
            session_ids = {goal.session_id}
            changed = True
            while changed:
                changed = False
                for child_id, parent_id in session_rows:
                    if parent_id in session_ids and child_id not in session_ids:
                        session_ids.add(child_id)
                        changed = True

            since = run.time_started or run.time_created
            parts = list(
                (
                    await db.execute(
                        select(Part).where(
                            Part.session_id.in_(session_ids),
                            Part.time_created >= since,
                            Part.time_created <= now,
                        )
                    )
                ).scalars()
            )
            for part in parts:
                data = part.data or {}
                if data.get("type") not in {
                    "step-finish",
                    "office-repair-usage",
                }:
                    continue
                tokens = data.get("tokens") or {}
                if isinstance(tokens, dict):
                    recovered_tokens += sum(
                        max(0, int(tokens.get(key, 0) or 0))
                        for key in ("input", "output", "reasoning", "cache_read")
                    )
                recovered_cost += max(
                    0,
                    round(float(data.get("cost", 0.0) or 0.0) * 1_000_000),
                )

            # LLM compaction persists usage on a synthetic Message rather than
            # a step-finish Part. Include it once without counting the final
            # assistant aggregate Message (which duplicates step parts).
            messages = list(
                (
                    await db.execute(
                        select(Message).where(
                            Message.session_id.in_(session_ids),
                            Message.time_created >= since,
                            Message.time_created <= now,
                        )
                    )
                ).scalars()
            )
            for message in messages:
                data = message.data or {}
                if data.get("agent") != "compaction" or not data.get("system"):
                    continue
                tokens = data.get("tokens") or {}
                if isinstance(tokens, dict):
                    recovered_tokens += sum(
                        max(0, int(tokens.get(key, 0) or 0))
                        for key in ("input", "output", "reasoning", "cache_read")
                    )
                recovered_cost += max(
                    0,
                    round(float(data.get("cost", 0.0) or 0.0) * 1_000_000),
                )

        recorded_tokens, recorded_cost = await get_goal_run_recorded_usage(
            db,
            run.id,
        )
        recovered_tokens = max(recovered_tokens, recorded_tokens)
        recovered_cost = max(recovered_cost, recorded_cost)

        run.status = "interrupted"
        run.tokens_used = max(int(run.tokens_used or 0), recovered_tokens)
        run.cost_used_microusd = max(
            int(run.cost_used_microusd or 0),
            recovered_cost,
        )
        run.error_code = "restart_uncertain"
        run.stop_reason = "Application restarted before the Goal run finished"
        run.lease_owner = None
        run.lease_expires_at = None
        run.time_finished = now
        old_tokens, old_cost = usage_by_goal.get(run.goal_id, (0, 0))
        usage_by_goal[run.goal_id] = (
            old_tokens + run.tokens_used,
            old_cost + run.cost_used_microusd,
        )

    for goal_id, (tokens, cost) in usage_by_goal.items():
        goal = await db.get(SessionGoal, goal_id)
        if goal is None:
            continue
        goal.tokens_used += tokens
        goal.cost_used_microusd += cost
        goal.status = "blocked"
        goal.run_state = "interrupted"
        goal.revision += 1
        goal.blocker_code = "restart_uncertain"
        goal.blocker_message = "Review the last run before resuming this Goal"
        goal.needs_review = True
    await db.flush()
    return len(active_runs)
