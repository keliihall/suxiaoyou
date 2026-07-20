"""Fail-closed REST control plane for persistent session Goals."""

from __future__ import annotations

import asyncio
import functools
from dataclasses import dataclass
from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.exc import IntegrityError

from app import release_features
from app.agent.permission import (
    intersect_permission_rulesets,
    parse_permission_policy_baseline,
    parse_permission_snapshot,
    serialize_permission_snapshot,
)
from app.auth.local import require_local_session
from app.dependencies import (
    AgentRegistryDep,
    DbDep,
    IndexManagerDep,
    ProviderRegistryDep,
    SessionFactoryDep,
    SettingsDep,
    StreamManagerDep,
    ToolRegistryDep,
)
from app.i18n import Language, request_language
from app.models.idempotency_record import IdempotencyRecord
from app.models.session import Session
from app.models.session_goal import SessionGoal
from app.schemas.agent import PermissionRule, Ruleset
from app.schemas.chat import PromptRequest
from app.schemas.goal import (
    GoalChatRequest,
    GoalControlRequest,
    GoalCreateRequest,
    GoalResponse,
    GoalRunResponse,
    GoalStartResponse,
    GoalTokenUsageResponse,
    GoalUpdateRequest,
)
from app.session.idempotency import (
    IdempotencyConflictError,
    canonical_request_hash,
    get_idempotency_record,
    validate_idempotent_replay,
)
from app.session.goal_controller import (
    _request_for_continuation,
    run_goal_generation,
)
from app.session.goal_manager import (
    AutonomousGoalsUnavailableError,
    GoalAlreadyExistsError,
    GoalBudgetLimitError,
    GoalControlError,
    GoalIdempotencyConflictError,
    GoalInvalidTransitionError,
    GoalNotFoundError,
    GoalRevisionConflictError,
    GoalRunConflictError,
    GoalValidationError,
    clear_session_goal,
    create_session_goal,
    get_goal_token_usage_breakdown,
    get_session_goal,
    pause_session_goal,
    reserve_goal_run,
    resume_session_goal,
    update_session_goal,
)
from app.session.input_queue import claim_next_generation_input
from app.streaming.events import AGENT_ERROR, SSEEvent
from app.streaming.manager import GenerationJob, SessionBusyError
from app.tool.workspace import WorkspaceBoundaryViolation, validate_agent_workspace_root
from app.utils.id import generate_ulid


def _require_goal_release() -> None:
    if not release_features.GOALS_RELEASED:
        raise HTTPException(
            status_code=404,
            detail="Goals are not available in this release",
        )


router = APIRouter(
    dependencies=[Depends(_require_goal_release), Depends(require_local_session)]
)


@dataclass(slots=True)
class _GoalAdmission:
    response: GoalStartResponse
    goal_id: str
    run_id: str
    record_id: str
    initial_request: PromptRequest | None = None
    input_id: str | None = None


def _goal_chat_scope() -> str:
    return "chat.goal"


def _reject_interrupted_goal_replay(
    record: IdempotencyRecord,
    response: dict,
    *,
    message: str,
) -> None:
    if record.status not in {"interrupted", "failed"}:
        return
    raise HTTPException(
        status_code=409,
        detail={
            "code": "idempotency_interrupted",
            "message": message,
            "session_id": response.get("session_id"),
            "stream_id": response.get("stream_id"),
        },
    )


def _reject_archived_goal_resume(session: Session | None) -> None:
    if session is None or session.time_archived is None:
        return
    raise HTTPException(
        status_code=409,
        detail={
            "code": "session_archived",
            "message": "Unarchive this conversation before resuming its Goal.",
        },
    )


def _create_goal_job(sm, *, stream_id: str, session_id: str) -> GenerationJob:
    try:
        return sm.create_job(
            stream_id=stream_id,
            session_id=session_id,
            invocation_source="goal",
            invocation_source_id="persistent-goal",
        )
    except SessionBusyError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "session_busy",
                "message": "This conversation already has a running task.",
                "session_id": exc.session_id,
                "active_stream_id": exc.stream_id,
            },
        ) from exc


async def _commit_goal_admission(
    session_factory,
    *,
    body: GoalChatRequest,
    request_hash: str,
    session_id: str,
    stream_id: str,
    workspace: str | None,
    settings,
) -> _GoalAdmission:
    async with session_factory() as db:
        async with db.begin():
            session = await db.get(Session, session_id)
            if session is None:
                session = Session(
                    id=session_id,
                    directory=workspace or ".",
                    title=body.objective[:60] or "Goal",
                    version="1.0.0",
                )
                db.add(session)
                await db.flush()

            goal = await create_session_goal(
                db,
                session_id,
                GoalCreateRequest(
                    client_request_id=body.client_request_id,
                    objective=body.objective,
                    definition_of_done=body.definition_of_done,
                    token_budget=body.token_budget,
                    cost_budget_microusd=body.cost_budget_microusd,
                    time_budget_seconds=body.time_budget_seconds,
                    max_continuations=body.max_continuations,
                    model_id=body.model,
                    provider_id=body.provider_id,
                    agent=body.agent,
                    reasoning=body.reasoning,
                    language=body.language,
                ),
                settings=settings,
            )
            reservation = await reserve_goal_run(
                db,
                goal_id=goal.id,
                expected_revision=goal.revision,
                idempotency_key=f"{stream_id}:initial:{goal.id}",
                trigger="initial",
                stream_id=stream_id,
            )
            response = GoalStartResponse(
                stream_id=stream_id,
                session_id=session_id,
                goal=GoalResponse.model_validate(reservation.goal),
                run=GoalRunResponse.model_validate(reservation.run),
            )
            record = IdempotencyRecord(
                id=generate_ulid(),
                scope=_goal_chat_scope(),
                request_key=body.client_request_id,
                request_hash=request_hash,
                status="accepted",
                response=response.model_dump(mode="json"),
            )
            db.add(record)
            await db.flush()
            return _GoalAdmission(
                response=response,
                goal_id=goal.id,
                run_id=reservation.run.id,
                record_id=record.id,
            )


async def _commit_goal_resume(
    session_factory,
    *,
    session_id: str,
    body: GoalControlRequest,
    request_hash: str,
    stream_id: str,
    process_language: Language,
) -> _GoalAdmission:
    scope = f"goal.resume.run:{session_id}"
    async with session_factory() as db:
        async with db.begin():
            # Lock the owning lifecycle row before changing Goal state. An
            # archive racing this resume either commits first and is rejected
            # here, or waits and then pauses the freshly resumed Goal.
            session = await db.get(Session, session_id, with_for_update=True)
            if session is None:
                raise GoalNotFoundError("Session not found")
            _reject_archived_goal_resume(session)
            goal = await resume_session_goal(db, session_id, body)
            # Explicit resume is an authorization boundary. The old Goal
            # snapshot remains an immutable maximum; a newer server-computed
            # Session policy may only narrow it. Missing/legacy authority
            # sources deny all instead of falling back to current allows.
            old_ceiling = parse_permission_snapshot(goal.permission_snapshot)
            policy_baseline = parse_permission_policy_baseline(
                goal.permission_snapshot
            )
            current_policy = parse_permission_snapshot(
                session.permission_snapshot
            )
            if old_ceiling is None or current_policy is None:
                resumed_ceiling = Ruleset(rules=[
                    PermissionRule(
                        action="deny",
                        permission="*",
                        pattern="*",
                    ),
                ])
            else:
                resumed_ceiling = intersect_permission_rulesets(
                    old_ceiling,
                    current_policy,
                )
            goal.permission_snapshot = serialize_permission_snapshot(
                resumed_ceiling,
                global_permissions=(
                    policy_baseline[0] if policy_baseline is not None else None
                ),
                agent_permissions=(
                    policy_baseline[1] if policy_baseline is not None else None
                ),
            )
            await db.flush()
            item = await claim_next_generation_input(
                db,
                session_id,
                target_stream_id=stream_id,
                # A steer accepted by the previous stream before a safe pause
                # is still real user input and outranks this resume slice.
                include_stale_steer=True,
            )
            trigger = "user_input" if item is not None else "resume"
            reservation = await reserve_goal_run(
                db,
                goal_id=goal.id,
                expected_revision=goal.revision,
                idempotency_key=f"{stream_id}:resume:{goal.id}",
                trigger=trigger,
                stream_id=stream_id,
            )
            response = GoalStartResponse(
                stream_id=stream_id,
                session_id=session_id,
                goal=GoalResponse.model_validate(reservation.goal),
                run=GoalRunResponse.model_validate(reservation.run),
            )
            record = IdempotencyRecord(
                id=generate_ulid(),
                scope=scope,
                request_key=body.client_request_id,
                request_hash=request_hash,
                status="accepted",
                response=response.model_dump(mode="json"),
            )
            db.add(record)
            await db.flush()
            return _GoalAdmission(
                response=response,
                goal_id=goal.id,
                run_id=reservation.run.id,
                record_id=record.id,
                initial_request=_request_for_continuation(
                    reservation.goal,
                    session,
                    item=item,
                    process_language=process_language,
                ),
                input_id=item.id if item is not None else None,
            )


async def _await_goal_admission(task: asyncio.Task[_GoalAdmission]) -> tuple[_GoalAdmission, bool]:
    cancelled = False
    while True:
        try:
            return await asyncio.shield(task), cancelled
        except asyncio.CancelledError:
            cancelled = True
            if task.done():
                return task.result(), cancelled


def _goal_task_done(task: asyncio.Task[None], *, job: GenerationJob) -> None:
    if task.cancelled():
        job.complete()
        return
    error = task.exception()
    if error is not None:
        # run_goal_generation normally reconciles its own errors. This is a
        # final safety net for failures before its try/finally is entered.
        job.publish(
            SSEEvent(
                AGENT_ERROR,
                {"error_message": "The Goal worker stopped unexpectedly."},
            )
        )
        job.complete()


def _install_goal_worker(
    *,
    job: GenerationJob,
    admission: _GoalAdmission,
    prompt: PromptRequest,
    sm,
    session_factory,
    provider_registry,
    agent_registry,
    tool_registry,
    index_manager,
    initial_input_id: str | None = None,
    initial_skip_user_message: bool = True,
) -> None:
    job.set_goal_identity(
        goal_id=admission.goal_id,
        goal_run_id=admission.run_id,
    )
    job.language = prompt.language
    job.interactive = True
    task = asyncio.create_task(
        run_goal_generation(
            job,
            prompt,
            initial_run_id=admission.run_id,
            stream_manager=sm,
            session_factory=session_factory,
            provider_registry=provider_registry,
            agent_registry=agent_registry,
            tool_registry=tool_registry,
            index_manager=index_manager,
            idempotency_record_id=admission.record_id,
            initial_input_id=initial_input_id,
            initial_skip_user_message=initial_skip_user_message,
        ),
        name=f"goal-{job.stream_id}",
    )
    task.add_done_callback(functools.partial(_goal_task_done, job=job))
    job.task = task


def _raise_http(exc: GoalControlError) -> NoReturn:
    if isinstance(exc, GoalRevisionConflictError):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "goal_revision_conflict",
                "message": str(exc),
                "expected_revision": exc.expected_revision,
                "current_revision": exc.current_revision,
            },
        ) from exc
    if isinstance(exc, GoalIdempotencyConflictError):
        raise HTTPException(
            status_code=409,
            detail={"code": "idempotency_conflict", "message": str(exc)},
        ) from exc
    if isinstance(exc, GoalAlreadyExistsError):
        raise HTTPException(
            status_code=409,
            detail={"code": "goal_already_exists", "message": str(exc)},
        ) from exc
    if isinstance(exc, (GoalInvalidTransitionError, GoalRunConflictError)):
        raise HTTPException(
            status_code=409,
            detail={"code": "goal_invalid_transition", "message": str(exc)},
        ) from exc
    if isinstance(exc, GoalNotFoundError):
        raise HTTPException(
            status_code=404,
            detail={"code": "goal_not_found", "message": str(exc)},
        ) from exc
    if isinstance(exc, GoalBudgetLimitError):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "goal_budget_exceeds_maximum",
                "message": str(exc),
                "field": exc.field,
                "maximum": exc.maximum,
            },
        ) from exc
    if isinstance(exc, GoalValidationError):
        raise HTTPException(
            status_code=400,
            detail={"code": "goal_invalid_request", "message": str(exc)},
        ) from exc
    if isinstance(exc, AutonomousGoalsUnavailableError):
        raise HTTPException(
            status_code=404,
            detail={"code": "autonomous_goals_unavailable", "message": str(exc)},
        ) from exc
    raise HTTPException(status_code=500, detail="Goal operation failed") from exc


@router.post(
    "/chat/goal",
    response_model=GoalStartResponse,
    status_code=status.HTTP_201_CREATED,
)
async def start_goal(
    body: GoalChatRequest,
    request: Request,
    sm: StreamManagerDep,
    session_factory: SessionFactoryDep,
    settings: SettingsDep,
    provider_registry: ProviderRegistryDep,
    agent_registry: AgentRegistryDep,
    tool_registry: ToolRegistryDep,
    index_manager: IndexManagerDep,
) -> GoalStartResponse:
    """Atomically create Session + Goal + first reserved GoalRun + worker."""

    control = getattr(request.app.state, "security_control", None)
    if control is not None and control.emergency_stop:
        raise HTTPException(
            status_code=423,
            detail={
                "code": "security_emergency_stop",
                "message": "Security emergency stop is active.",
            },
        )

    body.language = request_language(request)
    workspace = body.workspace
    if workspace:
        try:
            workspace = str(validate_agent_workspace_root(workspace))
        except (OSError, ValueError, WorkspaceBoundaryViolation) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Validate images before durable admission so an unsupported model cannot
    # leave behind an orphaned Goal.
    from app.api.chat import _ensure_image_attachments_supported

    _ensure_image_attachments_supported(
        attachments=body.attachments,
        provider_registry=provider_registry,
        model_id=body.model,
        provider_id=body.provider_id,
    )

    request_hash = canonical_request_hash(
        body.model_dump(mode="json", exclude={"client_request_id"})
    )
    admission_cancelled = False
    async with sm.job_admission_lock:
        async with session_factory() as db:
            existing = await get_idempotency_record(
                db,
                scope=_goal_chat_scope(),
                request_key=body.client_request_id,
            )
            if existing is not None:
                try:
                    response = validate_idempotent_replay(
                        existing,
                        request_hash=request_hash,
                    )
                except IdempotencyConflictError as exc:
                    raise HTTPException(
                        status_code=409,
                        detail={"code": "idempotency_conflict", "message": str(exc)},
                    ) from exc
                stream_id = str(response.get("stream_id") or "")
                if not stream_id:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "idempotency_invalid",
                            "message": "The stored Goal admission is invalid.",
                        },
                    )
                _reject_interrupted_goal_replay(
                    existing,
                    response,
                    message=(
                        "The previous Goal start was interrupted. Review the "
                        "Goal state before resuming."
                    ),
                )
                if existing.status in {"accepted", "running"} and sm.get_job(stream_id) is None:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "idempotency_interrupted",
                            "message": (
                                "The previous Goal start was interrupted. Review "
                                "the Goal state before resuming."
                            ),
                            "session_id": response.get("session_id"),
                            "stream_id": stream_id,
                        },
                    )
                return GoalStartResponse(**response)

        session_id = body.session_id or generate_ulid()
        stream_id = generate_ulid()
        job = _create_goal_job(sm, stream_id=stream_id, session_id=session_id)
        try:
            commit_task = asyncio.create_task(
                _commit_goal_admission(
                    session_factory,
                    body=body,
                    request_hash=request_hash,
                    session_id=session_id,
                    stream_id=stream_id,
                    workspace=workspace,
                    settings=settings,
                ),
                name=f"goal-admission-{stream_id}",
            )
            admission, admission_cancelled = await _await_goal_admission(commit_task)
        except GoalControlError as exc:
            sm.remove_job(stream_id)
            _raise_http(exc)
        except IntegrityError as exc:
            sm.remove_job(stream_id)
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "goal_admission_conflict",
                    "message": "The Goal was admitted concurrently.",
                },
            ) from exc
        except BaseException:
            sm.remove_job(stream_id)
            raise

        prompt = PromptRequest(
            session_id=session_id,
            # The objective is genuine user-authored conversation content.
            # Persist it through SessionPrompt's normal user-message path so
            # the Goal has a visible turn boundary in history.
            text=body.objective,
            model=body.model,
            provider_id=body.provider_id,
            agent=body.agent,
            attachments=body.attachments,
            permission_presets=body.permission_presets,
            permission_rules=body.permission_rules,
            reasoning=body.reasoning,
            workspace=workspace,
            language=body.language,
        )
        _install_goal_worker(
            job=job,
            admission=admission,
            prompt=prompt,
            sm=sm,
            session_factory=session_factory,
            provider_registry=provider_registry,
            agent_registry=agent_registry,
            tool_registry=tool_registry,
            index_manager=index_manager,
            initial_skip_user_message=False,
        )

        if admission_cancelled:
            # Durable admission is paired with a live worker before the HTTP
            # cancellation is propagated; retrying the same key recovers it.
            raise asyncio.CancelledError()

    return admission.response


@router.get(
    "/sessions/{session_id}/goal",
    response_model=GoalResponse | None,
)
async def read_session_goal(session_id: str, db: DbDep) -> GoalResponse | None:
    if await db.get(Session, session_id) is None:
        raise HTTPException(status_code=404, detail="Session not found")
    goal = await get_session_goal(db, session_id)
    return GoalResponse.model_validate(goal) if goal is not None else None


@router.get(
    "/sessions/{session_id}/goal/usage",
    response_model=GoalTokenUsageResponse,
)
async def read_session_goal_usage(
    session_id: str,
    db: DbDep,
) -> GoalTokenUsageResponse:
    if await db.get(Session, session_id) is None:
        raise HTTPException(status_code=404, detail="Session not found")
    goal = await get_session_goal(db, session_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    usage = await get_goal_token_usage_breakdown(db, goal.id)
    return GoalTokenUsageResponse(
        input=usage.input,
        output=usage.output,
        reasoning=usage.reasoning,
        cache_read=usage.cache_read,
        unattributed=usage.unattributed,
        total_tokens=usage.total_tokens,
        source_count=usage.source_count,
    )


@router.post(
    "/sessions/{session_id}/goal",
    response_model=GoalResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_goal(
    session_id: str,
    body: GoalCreateRequest,
    db: DbDep,
    settings: SettingsDep,
) -> GoalResponse:
    try:
        goal = await create_session_goal(db, session_id, body, settings=settings)
    except GoalControlError as exc:
        _raise_http(exc)
    return GoalResponse.model_validate(goal)


@router.patch(
    "/sessions/{session_id}/goal",
    response_model=GoalResponse,
)
async def update_goal(
    session_id: str,
    body: GoalUpdateRequest,
    db: DbDep,
    settings: SettingsDep,
    sm: StreamManagerDep,
) -> GoalResponse:
    active = sm.active_job_for_session(session_id)
    if active is not None and active.goal_id is not None:
        async with active.execution_admission_lock:
            was_open = active.execution_admission_open
            active.close_execution_admission()
            try:
                goal = await update_session_goal(db, session_id, body, settings=settings)
            except GoalControlError as exc:
                if was_open:
                    active.open_execution_admission()
                _raise_http(exc)
        return GoalResponse.model_validate(goal)
    try:
        goal = await update_session_goal(db, session_id, body, settings=settings)
    except GoalControlError as exc:
        _raise_http(exc)
    return GoalResponse.model_validate(goal)


@router.post(
    "/sessions/{session_id}/goal/pause",
    response_model=GoalResponse,
)
async def pause_goal(
    session_id: str,
    body: GoalControlRequest,
    db: DbDep,
    sm: StreamManagerDep,
) -> GoalResponse:
    active = sm.active_job_for_session(session_id)
    if active is not None and active.goal_id is not None:
        async with active.execution_admission_lock:
            was_open = active.execution_admission_open
            active.close_execution_admission()
            try:
                goal = await pause_session_goal(db, session_id, body)
            except GoalControlError as exc:
                if was_open:
                    active.open_execution_admission()
                _raise_http(exc)
            if active.goal_id == goal.id:
                active.deny_pending_responses(source="goal_safe_pause")
        return GoalResponse.model_validate(goal)
    try:
        goal = await pause_session_goal(db, session_id, body)
    except GoalControlError as exc:
        _raise_http(exc)
    return GoalResponse.model_validate(goal)


@router.post(
    "/sessions/{session_id}/goal/resume",
    response_model=GoalStartResponse,
)
async def resume_goal(
    session_id: str,
    body: GoalControlRequest,
    request: Request,
    sm: StreamManagerDep,
    session_factory: SessionFactoryDep,
    provider_registry: ProviderRegistryDep,
    agent_registry: AgentRegistryDep,
    tool_registry: ToolRegistryDep,
    index_manager: IndexManagerDep,
) -> GoalStartResponse:
    control = getattr(request.app.state, "security_control", None)
    if control is not None and control.emergency_stop:
        raise HTTPException(
            status_code=423,
            detail={
                "code": "security_emergency_stop",
                "message": "Security emergency stop is active.",
            },
        )

    scope = f"goal.resume.run:{session_id}"
    process_language = request_language(request)
    request_hash = canonical_request_hash(
        body.model_dump(mode="json", exclude={"client_request_id"})
    )
    admission_cancelled = False
    async with sm.job_admission_lock:
        async with session_factory() as replay_db:
            replay_session = await replay_db.get(Session, session_id)
            _reject_archived_goal_resume(replay_session)
            existing = await get_idempotency_record(
                replay_db,
                scope=scope,
                request_key=body.client_request_id,
            )
            if existing is not None:
                try:
                    response = validate_idempotent_replay(
                        existing,
                        request_hash=request_hash,
                    )
                except IdempotencyConflictError as exc:
                    raise HTTPException(
                        status_code=409,
                        detail={"code": "idempotency_conflict", "message": str(exc)},
                    ) from exc
                stream_id = str(response.get("stream_id") or "")
                _reject_interrupted_goal_replay(
                    existing,
                    response,
                    message=(
                        "The prior Goal resume was interrupted; review before retrying."
                    ),
                )
                if existing.status in {"accepted", "running"} and sm.get_job(stream_id) is None:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "idempotency_interrupted",
                            "message": "The prior Goal resume was interrupted; review before retrying.",
                            "session_id": session_id,
                            "stream_id": stream_id,
                        },
                    )
                return GoalStartResponse(**response)

        stream_id = generate_ulid()
        job = _create_goal_job(sm, stream_id=stream_id, session_id=session_id)
        try:
            # Pair the durable final queue observation with this job's public
            # input-admission lock. A concurrent POST /chat/inputs can either
            # commit before the resume reservation (and be claimed first) or
            # wait until after it; it cannot fall into the middle.
            async with job.session_input_lock:
                commit_task = asyncio.create_task(
                    _commit_goal_resume(
                        session_factory,
                        session_id=session_id,
                        body=body,
                        request_hash=request_hash,
                        stream_id=stream_id,
                        process_language=process_language,
                    ),
                    name=f"goal-resume-admission-{stream_id}",
                )
                admission, admission_cancelled = await _await_goal_admission(commit_task)
        except GoalControlError as exc:
            sm.remove_job(stream_id)
            _raise_http(exc)
        except IntegrityError as exc:
            sm.remove_job(stream_id)
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "goal_resume_conflict",
                    "message": "The Goal was resumed concurrently.",
                },
            ) from exc
        except BaseException:
            sm.remove_job(stream_id)
            raise

        prompt = admission.initial_request
        if prompt is None:  # committed admission always carries its exact input snapshot
            sm.remove_job(stream_id)
            raise HTTPException(status_code=409, detail="Goal resume request disappeared")
        _install_goal_worker(
            job=job,
            admission=admission,
            prompt=prompt,
            sm=sm,
            session_factory=session_factory,
            provider_registry=provider_registry,
            agent_registry=agent_registry,
            tool_registry=tool_registry,
            index_manager=index_manager,
            initial_input_id=admission.input_id,
            initial_skip_user_message=admission.input_id is None,
        )
        if admission_cancelled:
            raise asyncio.CancelledError()
    return admission.response


@router.delete(
    "/sessions/{session_id}/goal",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def clear_goal(
    session_id: str,
    db: DbDep,
    client_request_id: str = Query(..., min_length=8, max_length=128),
    expected_revision: int = Query(..., ge=1),
) -> Response:
    body = GoalControlRequest(
        client_request_id=client_request_id,
        expected_revision=expected_revision,
    )
    try:
        await clear_session_goal(db, session_id, body)
    except GoalControlError as exc:
        _raise_http(exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
