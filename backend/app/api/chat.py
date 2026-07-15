"""Chat API endpoints — prompt, stream, abort, respond."""

from __future__ import annotations

import asyncio
import functools
import json
import logging
from typing import Any

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app import release_features
from app.dependencies import (
    AgentRegistryDep,
    IndexManagerDep,
    ProviderRegistryDep,
    SessionFactoryDep,
    StreamManagerDep,
    ToolRegistryDep,
)
from app.models.todo import Todo
from app.models.message import Message
from app.models.idempotency_record import IdempotencyRecord
from app.schemas.chat import (
    AbortRequest,
    CompactRequest,
    EditAndResendRequest,
    PromptRequest,
    PromptResponse,
    RespondRequest,
    TaskBatchRequest,
)
from app.session.compaction import run_compaction
from app.session.manager import get_session
from app.session.manager import delete_messages_after, update_message_file_parts, update_message_text
from app.session.processor import run_generation
from app.session.idempotency import (
    IdempotencyConflictError,
    canonical_request_hash,
    get_idempotency_record,
    mark_idempotency_status,
    validate_idempotent_replay,
)
from app.session.task_batch import (
    TaskBatchWorkspaceConflict,
    TaskBatchWorkspaceInvalid,
    TaskBatchWorkspaceRequired,
    resolve_task_batch_workspace,
    run_task_batch,
)
from app.session.utils import (
    compute_usable_context_window,
    get_effective_context_window,
    has_image_attachments,
)
from app.streaming.events import (
    AGENT_ERROR,
    COMPACTION_ERROR,
    DONE,
    PERMISSION_RESOLVED,
    PLAN_REVIEW_RESOLVED,
    QUESTION_RESOLVED,
    SSEEvent,
)
from app.streaming.manager import GenerationJob, SessionBusyError, StreamManager
from app.utils.id import generate_ulid
from app.i18n import request_language

logger = logging.getLogger(__name__)

router = APIRouter()

_MANUAL_COMPACTION_MIN_USAGE_RATIO = 0.5
MODEL_DOES_NOT_SUPPORT_IMAGES = "MODEL_DOES_NOT_SUPPORT_IMAGES"

# Heartbeat interval (seconds) — prevents proxy/CDN timeout
_HEARTBEAT_INTERVAL = 15.0


def _ensure_security_not_stopped(request: Request) -> None:
    control = getattr(request.app.state, "security_control", None)
    if control is not None and control.emergency_stop:
        raise HTTPException(
            status_code=423,
            detail={
                "code": "security_emergency_stop",
                "message": "Security emergency stop is active. Resume from Settings before starting a task.",
            },
        )


def _create_session_job(
    sm: StreamManager,
    *,
    stream_id: str,
    session_id: str,
) -> GenerationJob:
    try:
        return sm.create_job(
            stream_id=stream_id,
            session_id=session_id,
            invocation_source="desktop",
            invocation_source_id="desktop",
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


def _unsupported_images_error() -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={
            "code": MODEL_DOES_NOT_SUPPORT_IMAGES,
            "message": "The selected model does not support images. Choose a vision model and try again.",
        },
    )


def _ensure_image_attachments_supported(
    *,
    attachments: list[dict[str, Any]] | None,
    provider_registry,
    model_id: str | None,
    provider_id: str | None,
) -> None:
    """Reject image inputs unless the requested model is explicitly vision-capable."""
    if not has_image_attachments(attachments):
        return

    if not model_id:
        raise _unsupported_images_error()

    resolved = provider_registry.resolve_model(model_id, provider_id)
    if resolved is None:
        raise _unsupported_images_error()

    _provider, model_info = resolved
    if not model_info.capabilities.vision:
        raise _unsupported_images_error()


def _on_task_done(task: asyncio.Task[None], *, job: GenerationJob) -> None:
    """Callback for generation tasks — logs and publishes unhandled exceptions.

    Without this, an unhandled exception in run_generation would be silently
    swallowed and the frontend would never receive a DONE or AGENT_ERROR event,
    leaving the UI stuck in the "generating" state forever.
    """
    if task.cancelled():
        job.complete()
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Unhandled exception in generation task %s: %s", task.get_name(), exc, exc_info=exc)
        try:
            job.publish(SSEEvent(AGENT_ERROR, {"error_message": "An internal error occurred. Please try again."}))
        except Exception:
            logger.exception("Failed to publish AGENT_ERROR for task %s", task.get_name())
        finally:
            job.complete()


async def _run_with_semaphore(
    sm: StreamManager,
    job: GenerationJob,
    coro,
    *,
    on_rejected=None,
) -> None:
    """Run generation under the concurrency semaphore."""
    try:
        await asyncio.wait_for(sm._semaphore.acquire(), timeout=30)
    except asyncio.TimeoutError:
        close = getattr(coro, "close", None)
        if close is not None:
            close()
        # Serialize the final rejection with POST /chat/inputs. Any follow-up
        # accepted while this task waited must become visibly blocked; leaving
        # it queued would strand it forever with no worker.
        async with job.session_input_lock:
            job.close_session_input_admission()
            if on_rejected is not None:
                try:
                    await on_rejected()
                except Exception:
                    logger.exception(
                        "Failed to persist admission rejection for stream %s",
                        job.stream_id,
                    )
        job.publish(SSEEvent(AGENT_ERROR, {"error_message": "Server is busy. Please try again shortly."}))
        job.complete()
        return
    except BaseException:
        # The raw generation coroutine has not started yet.  Explicitly close
        # it when shutdown cancels a task waiting for a semaphore slot; leaving
        # it unclosed leaks resources and emits a RuntimeWarning.
        close = getattr(coro, "close", None)
        if close is not None:
            close()
        raise
    try:
        await coro
    finally:
        sm._semaphore.release()


async def _mark_prompt_admission_failed(
    session_factory,
    record_id: str | None,
    job: GenerationJob,
) -> None:
    from app.session.input_queue import block_unstarted_inputs_for_stream

    error_message = "Generation was rejected because the server remained busy"
    async with session_factory() as db:
        async with db.begin():
            if record_id is not None:
                await mark_idempotency_status(
                    db,
                    record_id,
                    status="failed",
                    error_message=error_message,
                )
            await block_unstarted_inputs_for_stream(
                db,
                session_id=job.session_id,
                stream_id=job.stream_id,
                error_message=(
                    "The owning task never started because the server remained busy"
                ),
            )


async def _persist_prompt_idempotency_record(
    session_factory,
    record: IdempotencyRecord,
) -> str:
    """Commit a prompt's durable acceptance record and return its id.

    ``start_prompt`` runs this in a dedicated task and shields it from request
    cancellation.  That lets admission finish installing the matching worker
    before cancellation is propagated to the HTTP stack.
    """

    async with session_factory() as db:
        async with db.begin():
            db.add(record)
            await db.flush()
    return record.id


async def _await_shielded_commit(
    commit_task: asyncio.Task[str],
) -> tuple[str, bool]:
    """Wait for a shielded DB commit, remembering client cancellation."""

    cancellation_requested = False
    while True:
        try:
            record_id = await asyncio.shield(commit_task)
            return record_id, cancellation_requested
        except asyncio.CancelledError:
            cancellation_requested = True
            # ``shield`` keeps the commit alive.  Consume the request
            # cancellation and wait until the database outcome is known; the
            # caller will finish the matching in-memory transition before
            # propagating cancellation.
            if commit_task.done():
                return commit_task.result(), cancellation_requested


def _reject_interrupted_prompt_replay(
    record: IdempotencyRecord,
    response: dict[str, Any],
) -> None:
    if record.status != "interrupted":
        return
    raise HTTPException(
        status_code=409,
        detail={
            "code": "idempotency_interrupted",
            "message": (
                "The previous request was interrupted before completion. "
                "Review the partial conversation, then send again to start a new task."
            ),
            "session_id": response.get("session_id"),
            "stream_id": response.get("stream_id"),
        },
    )


async def _get_session_context_usage_ratio(
    session_factory,
    session_id: str,
    provider_registry,
    model_id: str | None,
) -> float | None:
    resolved = provider_registry.resolve_model(model_id) if model_id else None
    if resolved is None and model_id is None:
        return None
    if resolved is None:
        return None

    _provider, model_info = resolved
    max_context = get_effective_context_window(model_info) or model_info.capabilities.max_context
    context_limit = compute_usable_context_window(
        max_context,
        model_max_output=model_info.capabilities.max_output,
    )
    if not context_limit or context_limit <= 0:
        return None

    async with session_factory() as db:
        async with db.begin():
            result = await db.execute(
                select(Message)
                .where(Message.session_id == session_id)
                .order_by(Message.time_created.desc())
            )
            messages = list(result.scalars().all())

    for msg in messages:
        data = msg.data or {}
        if data.get("role") != "assistant":
            continue
        tokens = data.get("tokens") or {}
        if not isinstance(tokens, dict):
            continue
        input_tokens = int(tokens.get("input", 0) or 0)
        cache_read = int(tokens.get("cache_read", 0) or 0)
        total_tokens = input_tokens + cache_read
        if total_tokens > 0:
            return total_tokens / context_limit
    return 0.0


@router.post("/chat/prompt", response_model=PromptResponse)
async def start_prompt(
    body: PromptRequest,
    request: Request,
    sm: StreamManagerDep,
    session_factory: SessionFactoryDep,
    provider_registry: ProviderRegistryDep,
    agent_registry: AgentRegistryDep,
    tool_registry: ToolRegistryDep,
    index_manager: IndexManagerDep,
) -> PromptResponse:
    """Start a new generation. Returns stream_id for SSE subscription."""
    _ensure_security_not_stopped(request)
    body.language = request_language(request)
    request_key = body.client_request_id
    request_hash = canonical_request_hash(
        body.model_dump(mode="json", exclude={"client_request_id"})
    )
    record_id: str | None = None
    admission_cancelled = False

    # A keyless request is retained for compatibility with older clients, but
    # all v0.8.0 first-party clients send a key.  Keyed requests atomically
    # install their durable response and in-memory job under one admission
    # lock, so concurrent desktop/mobile retries converge on one execution.
    async with sm.job_admission_lock:
        if request_key:
            async with session_factory() as db:
                existing = await get_idempotency_record(
                    db,
                    scope="chat.prompt",
                    request_key=request_key,
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
                        detail={
                            "code": "idempotency_conflict",
                            "message": str(exc),
                        },
                    ) from exc
                _reject_interrupted_prompt_replay(existing, response)
                return PromptResponse(**response)

        _ensure_image_attachments_supported(
            attachments=body.attachments,
            provider_registry=provider_registry,
            model_id=body.model,
            provider_id=body.provider_id,
        )

        if body.session_id and release_features.GOALS_RELEASED:
            from app.models.session_goal import SessionGoal

            async with session_factory() as db:
                active_goal = (
                    await db.execute(
                        select(SessionGoal.id).where(
                            SessionGoal.session_id == body.session_id,
                            SessionGoal.status == "active",
                        )
                    )
                ).scalar_one_or_none()
            if active_goal is not None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "goal_active",
                        "message": (
                            "This conversation has an active Goal. Queue the "
                            "message on its current stream or pause the Goal first."
                        ),
                        "session_id": body.session_id,
                        "goal_id": active_goal,
                    },
                )

        session_id = body.session_id or generate_ulid()
        stream_id = generate_ulid()
        job = _create_session_job(sm, stream_id=stream_id, session_id=session_id)
        job.language = body.language

        if request_key:
            record = IdempotencyRecord(
                scope="chat.prompt",
                request_key=request_key,
                request_hash=request_hash,
                status="accepted",
                response={"stream_id": stream_id, "session_id": session_id},
            )
            try:
                commit_task = asyncio.create_task(
                    _persist_prompt_idempotency_record(session_factory, record),
                    name=f"prompt-admission-{stream_id}",
                )
                record_id, admission_cancelled = await _await_shielded_commit(
                    commit_task
                )
            except IntegrityError:
                sm.remove_job(stream_id)
                async with session_factory() as db:
                    concurrent = await get_idempotency_record(
                        db,
                        scope="chat.prompt",
                        request_key=request_key,
                    )
                if concurrent is None:
                    raise
                try:
                    response = validate_idempotent_replay(
                        concurrent,
                        request_hash=request_hash,
                    )
                except IdempotencyConflictError as exc:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "idempotency_conflict",
                            "message": str(exc),
                        },
                    ) from exc
                _reject_interrupted_prompt_replay(concurrent, response)
                return PromptResponse(**response)
            except Exception:
                sm.remove_job(stream_id)
                raise

        # Browser chat jobs are interactive as soon as they are created. The
        # generation task is installed before releasing admission so a request
        # cancellation cannot strand a committed ledger row without a worker.
        job.interactive = True
        coro = run_generation(
            job,
            body,
            session_factory=session_factory,
            provider_registry=provider_registry,
            agent_registry=agent_registry,
            tool_registry=tool_registry,
            index_manager=index_manager,
            idempotency_record_id=record_id,
        )
        task = asyncio.create_task(
            _run_with_semaphore(
                sm,
                job,
                coro,
                on_rejected=functools.partial(
                    _mark_prompt_admission_failed,
                    session_factory,
                    record_id,
                    job,
                ),
            ),
            name=f"gen-{stream_id}",
        )
        task.add_done_callback(functools.partial(_on_task_done, job=job))
        job.task = task  # prevent GC from silently cancelling the task

        if admission_cancelled:
            # The HTTP caller is gone, but the durable accepted response is now
            # paired with a live worker.  A retry with the same key can safely
            # recover the original session/stream instead of a dead stream.
            raise asyncio.CancelledError()

    return PromptResponse(stream_id=stream_id, session_id=session_id)


@router.post("/chat/task-batch", response_model=PromptResponse)
async def start_task_batch(
    body: TaskBatchRequest,
    request: Request,
    sm: StreamManagerDep,
    session_factory: SessionFactoryDep,
    provider_registry: ProviderRegistryDep,
    agent_registry: AgentRegistryDep,
    tool_registry: ToolRegistryDep,
    index_manager: IndexManagerDep,
) -> PromptResponse:
    """Start an explicit sequential or parallel multi-agent task batch."""
    _ensure_security_not_stopped(request)
    body.language = request_language(request)
    session_id = body.session_id or generate_ulid()
    try:
        body.workspace = await resolve_task_batch_workspace(
            session_factory=session_factory,
            session_id=session_id,
            body=body,
        )
    except TaskBatchWorkspaceRequired as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "workspace_required", "message": str(exc)},
        ) from exc
    except TaskBatchWorkspaceConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "workspace_conflict", "message": str(exc)},
        ) from exc
    except TaskBatchWorkspaceInvalid as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "workspace_invalid", "message": str(exc)},
        ) from exc
    stream_id = generate_ulid()

    job = _create_session_job(sm, stream_id=stream_id, session_id=session_id)
    job.language = body.language
    job.interactive = True
    # A task batch owns child streams and cannot safely splice a user follow-up
    # into its parent orchestration stream.
    job.close_session_input_admission()

    coro = run_task_batch(
        job,
        body,
        session_factory=session_factory,
        provider_registry=provider_registry,
        agent_registry=agent_registry,
        tool_registry=tool_registry,
        index_manager=index_manager,
    )
    task = asyncio.create_task(
        _run_with_semaphore(sm, job, coro),
        name=f"task-batch-{stream_id}",
    )
    task.add_done_callback(functools.partial(_on_task_done, job=job))
    job.task = task

    return PromptResponse(stream_id=stream_id, session_id=session_id)


@router.post("/chat/compact", response_model=PromptResponse)
async def start_compaction(
    body: CompactRequest,
    request: Request,
    sm: StreamManagerDep,
    session_factory: SessionFactoryDep,
    provider_registry: ProviderRegistryDep,
    agent_registry: AgentRegistryDep,
) -> PromptResponse:
    """Start a manual compaction stream. Reuses the normal SSE/abort lifecycle."""
    _ensure_security_not_stopped(request)
    async with session_factory() as db:
        async with db.begin():
            session = await get_session(db, body.session_id)
            if session is None:
                raise HTTPException(status_code=404, detail="Session not found")

    usage_ratio = await _get_session_context_usage_ratio(
        session_factory,
        body.session_id,
        provider_registry,
        body.model_id,
    )
    if usage_ratio is not None and usage_ratio < _MANUAL_COMPACTION_MIN_USAGE_RATIO:
        raise HTTPException(
            status_code=409,
            detail="Manual compaction is available only after context usage reaches 50%",
        )

    if any(job.session_id == body.session_id and not job.completed for job in sm._jobs.values()):
        raise HTTPException(status_code=409, detail="Session is currently busy")

    stream_id = generate_ulid()
    job = _create_session_job(sm, stream_id=stream_id, session_id=body.session_id)
    job.close_session_input_admission()

    async def _run_compaction_job() -> None:
        try:
            async with session_factory() as db:
                async with db.begin():
                    session = await get_session(db, body.session_id)
                    if session is not None:
                        session.time_compacting = datetime.now(timezone.utc)

            result = await run_compaction(
                body.session_id,
                job=job,
                session_factory=session_factory,
                provider_registry=provider_registry,
                agent_registry=agent_registry,
                model_id=body.model_id,
                visible_summary=True,
            )

            if not job.abort_event.is_set():
                if not result.summary and result.pruned_parts == 0:
                    job.publish(SSEEvent(COMPACTION_ERROR, {"error_message": "Nothing to compact yet"}))
                elif not result.summary:
                    job.publish(SSEEvent(COMPACTION_ERROR, {"error_message": "Compaction stopped before an AI summary was produced"}))
        except Exception:
            logger.exception("Compaction error for stream %s", job.stream_id)
            job.publish(SSEEvent(COMPACTION_ERROR, {"error_message": "Context compaction failed. Please try again."}))
        finally:
            async with session_factory() as db:
                async with db.begin():
                    session = await get_session(db, body.session_id)
                    if session is not None:
                        session.time_compacting = None
            job.publish(
                SSEEvent(
                    DONE,
                    {
                        "session_id": body.session_id,
                        "finish_reason": "aborted" if job.abort_event.is_set() else "stop",
                    },
                )
            )
            job.complete()

    task = asyncio.create_task(
        _run_with_semaphore(sm, job, _run_compaction_job()),
        name=f"compact-{stream_id}",
    )
    task.add_done_callback(functools.partial(_on_task_done, job=job))
    job.task = task

    return PromptResponse(stream_id=stream_id, session_id=body.session_id)


@router.post("/chat/edit", response_model=PromptResponse)
async def edit_and_resend(
    body: EditAndResendRequest,
    request: Request,
    sm: StreamManagerDep,
    session_factory: SessionFactoryDep,
    provider_registry: ProviderRegistryDep,
    agent_registry: AgentRegistryDep,
    tool_registry: ToolRegistryDep,
    index_manager: IndexManagerDep,
) -> PromptResponse:
    """Edit a user message, delete all subsequent messages, and re-generate."""
    _ensure_security_not_stopped(request)
    body.language = request_language(request)
    active = sm.active_job_for_session(body.session_id)
    if active is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "session_busy",
                "message": "Stop the running task before editing conversation history.",
                "session_id": body.session_id,
                "active_stream_id": active.stream_id,
            },
        )
    _ensure_image_attachments_supported(
        attachments=body.attachments,
        provider_registry=provider_registry,
        model_id=body.model,
        provider_id=body.provider_id,
    )

    stream_id = generate_ulid()

    # Atomic DB operation: update message text + delete subsequent messages
    async with session_factory() as db:
        async with db.begin():
            from app.session.goal_manager import get_session_goal

            goal = await get_session_goal(db, body.session_id)
            if goal is not None and goal.status not in {"paused", "complete"}:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "goal_history_edit_blocked",
                        "message": (
                            "Pause or complete the Goal before editing conversation history."
                        ),
                        "goal_id": goal.id,
                        "goal_status": goal.status,
                        "goal_revision": goal.revision,
                    },
                )
            await update_message_text(db, body.message_id, body.text)
            await update_message_file_parts(
                db, body.message_id, body.session_id, body.attachments or []
            )
            await delete_messages_after(db, body.session_id, body.message_id)
            # Clear stale todos so re-fetches return empty until new generation populates them
            await db.execute(sa_delete(Todo).where(Todo.session_id == body.session_id))

    job = _create_session_job(sm, stream_id=stream_id, session_id=body.session_id)
    job.language = body.language
    job.interactive = True

    # Build a PromptRequest for run_generation (reuses existing flow)
    edit_request = PromptRequest(
        session_id=body.session_id,
        text=body.text,
        model=body.model,
        provider_id=body.provider_id,
        agent=body.agent,
        attachments=body.attachments,
        permission_presets=body.permission_presets,
        permission_rules=body.permission_rules,
        reasoning=body.reasoning,
        workspace=body.workspace,
        language=body.language,
    )

    coro = run_generation(
        job,
        edit_request,
        session_factory=session_factory,
        provider_registry=provider_registry,
        agent_registry=agent_registry,
        tool_registry=tool_registry,
        index_manager=index_manager,
        skip_user_message=True,
    )
    task = asyncio.create_task(
        _run_with_semaphore(sm, job, coro),
        name=f"gen-edit-{stream_id}",
    )
    task.add_done_callback(functools.partial(_on_task_done, job=job))
    job.task = task

    return PromptResponse(stream_id=stream_id, session_id=body.session_id)


@router.api_route("/chat/stream/{stream_id}", methods=["GET", "POST"])
async def stream_events(
    request: Request,
    sm: StreamManagerDep,
    stream_id: str,
    last_event_id: int = 0,
):
    """SSE endpoint. Supports reconnect via ?last_event_id=N.

    Includes heartbeat every 15s to prevent proxy/CDN timeouts (matching OpenCode).
    A job's interactive capability is fixed by its creator. Subscribing to a
    stream is observational and must never promote a headless automation job.
    """
    # Native EventSource reconnects send Last-Event-ID as an HTTP header rather
    # than as a query param. The local desktop app uses native EventSource for
    # SSE, so if we only honor ?last_event_id=... then auto-reconnect falls back
    # to replaying from event 0, which can stall or desync the frontend on long
    # generations. Prefer the explicit query param when provided, otherwise
    # accept the standard header.
    if last_event_id == 0:
        header_value = request.headers.get("last-event-id")
        if header_value:
            try:
                last_event_id = int(header_value)
            except ValueError:
                last_event_id = 0

    job = sm.get_job(stream_id)

    if job is None:
        # Return 200 (not 404) so that EventSource reads the body.
        # EventSource ignores response bodies on non-2xx status codes,
        # causing the frontend to never receive the agent_error event.
        #
        # Tag it JOB_NOT_FOUND: an absent in-memory job almost always means the
        # backend restarted out from under an in-flight generation, which the
        # client can recover from silently (the conversation is safe in the DB).
        return StreamingResponse(
            _error_stream("Job not found", code="JOB_NOT_FOUND"),
            media_type="text/event-stream",
        )

    queue = job.subscribe(last_event_id=last_event_id)

    # Padding to push SSE data past Cloudflare tunnel's response buffer.
    # Without this, small SSE chunks are held by the tunnel and never
    # delivered to the client until enough data accumulates (~4KB).
    _SSE_PADDING = ": " + "x" * 4096 + "\n\n"

    async def event_generator():
        done_sent = False
        try:
            # Send padding first to flush the tunnel buffer
            yield _SSE_PADDING
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_INTERVAL)
                    if event is None:
                        break
                    yield event.encode()
                    if event.event in ("done", "agent-error"):
                        done_sent = True
                except asyncio.TimeoutError:
                    # Send heartbeat as a named SSE event so the frontend
                    # EventSource triggers listeners and resets its timer.
                    yield "event: heartbeat\ndata: {}\n\n"
            if done_sent:
                # Yield an SSE comment to force an extra write/flush cycle.
                # Keep this on the normal terminal path: yielding from an async
                # generator's finally block raises during client disconnect.
                yield ": flush\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            job.unsubscribe(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/chat/abort")
async def abort_generation(
    sm: StreamManagerDep,
    session_factory: SessionFactoryDep,
    body: AbortRequest,
) -> dict:
    """Abort an active generation."""
    job = sm.get_job(body.stream_id)
    if job is None:
        return {"status": "not_found"}
    if job.goal_id is not None:
        from app.schemas.goal import GoalResponse
        from app.session.goal_manager import request_immediate_goal_stop
        from app.streaming.events import GOAL_UPDATED

        async with session_factory() as db:
            async with db.begin():
                goal = await request_immediate_goal_stop(
                    db,
                    goal_id=job.goal_id,
                )
        if goal is not None:
            job.publish(
                SSEEvent(
                    GOAL_UPDATED,
                    {
                        "goal": GoalResponse.model_validate(goal).model_dump(
                            mode="json"
                        )
                    },
                )
            )
        job.deny_pending_responses(source="goal_immediate_stop")
    job.abort()
    return {
        "status": "aborted",
        "goal_stop": job.goal_id is not None,
    }


@router.get("/chat/active")
async def list_active(sm: StreamManagerDep) -> list[dict[str, Any]]:
    """List active generation jobs."""
    return sm.active_jobs()


def _response_ledger_scope(stream_id: str) -> str:
    return f"chat.respond:{stream_id}"


async def _load_interaction_resolution(
    session_factory,
    *,
    stream_id: str,
    call_id: str,
) -> IdempotencyRecord | None:
    async with session_factory() as db:
        return await get_idempotency_record(
            db,
            scope=_response_ledger_scope(stream_id),
            request_key=call_id,
        )


async def _persist_interaction_resolution(
    session_factory,
    record: IdempotencyRecord,
) -> str:
    """Commit an interaction decision before its waiting Future is woken."""

    async with session_factory() as db:
        async with db.begin():
            db.add(record)
            await db.flush()
    return record.id


def _interaction_public_payload(
    stored: dict[str, Any],
    *,
    status: str,
    idempotent: bool,
) -> dict[str, Any]:
    return {
        "status": status,
        "call_id": stored["call_id"],
        "tool_call_id": stored.get("tool_call_id"),
        "tool": stored.get("tool"),
        "prompt_type": stored.get("prompt_type", "unknown"),
        "decision": stored.get("decision", "answered"),
        "source": stored.get("source", "local"),
        "idempotent": idempotent,
    }


def _raise_interaction_conflict(
    *,
    stream_id: str,
    call_id: str,
    stored: dict[str, Any],
) -> None:
    raise HTTPException(
        status_code=409,
        detail={
            "code": "response_conflict",
            "message": (
                "This interaction was already resolved with a different response."
            ),
            "stream_id": stream_id,
            "call_id": call_id,
            "existing_decision": stored.get("decision"),
            "source": stored.get("source"),
            "tool_call_id": stored.get("tool_call_id"),
            "resolved_at": stored.get("resolved_at"),
        },
    )


def _publish_interaction_resolution(
    job: GenerationJob,
    stored: dict[str, Any],
) -> None:
    prompt_type = str(stored.get("prompt_type", "unknown"))
    event_type = {
        "permission": PERMISSION_RESOLVED,
        "plan": PLAN_REVIEW_RESOLVED,
    }.get(prompt_type, QUESTION_RESOLVED)
    job.publish(
        SSEEvent(
            event_type,
            {
                key: value
                for key, value in _interaction_public_payload(
                    stored,
                    status="accepted",
                    idempotent=False,
                ).items()
                if key != "status"
            },
        )
    )


def _replay_interaction_resolution(
    *,
    record: IdempotencyRecord,
    request_hash: str,
    body: RespondRequest,
    job: GenerationJob | None,
) -> dict[str, Any]:
    stored = dict(record.response or {})
    if record.request_hash != request_hash:
        _raise_interaction_conflict(
            stream_id=body.stream_id,
            call_id=body.call_id,
            stored=stored,
        )

    # A retry can arrive after the DB commit but before the original handler
    # woke the Future (or after that handler was cancelled).  Reapply the
    # durable fact locally; it is safe and idempotent.
    if job is not None:
        applied = job.apply_durable_response(
            body.call_id,
            stored.get("submitted_response"),
            source=str(stored.get("source") or "local"),
        )
        if applied.status == "accepted":
            _publish_interaction_resolution(job, stored)
        elif applied.status == "conflict":
            _raise_interaction_conflict(
                stream_id=body.stream_id,
                call_id=body.call_id,
                stored=stored,
            )

    return _interaction_public_payload(
        stored,
        status="already_resolved",
        idempotent=True,
    )


@router.post("/chat/respond")
async def respond_to_prompt(
    request: Request,
    sm: StreamManagerDep,
    session_factory: SessionFactoryDep,
    body: RespondRequest,
) -> dict:
    """User responds to question tool or permission request."""
    job = sm.get_job(body.stream_id)
    if job is None:
        durable = await _load_interaction_resolution(
            session_factory,
            stream_id=body.stream_id,
            call_id=body.call_id,
        )
        if durable is not None:
            return _replay_interaction_resolution(
                record=durable,
                request_hash=canonical_request_hash({"response": body.response}),
                body=body,
                job=None,
            )
        raise HTTPException(
            status_code=404,
            detail={
                "code": "job_not_found",
                "message": "The generation job no longer exists.",
                "stream_id": body.stream_id,
                "call_id": body.call_id,
            },
        )

    request_hash = canonical_request_hash({"response": body.response})
    source = str(getattr(getattr(request, "state", None), "source", None) or "local")
    async with job.response_resolution_lock:
        durable = await _load_interaction_resolution(
            session_factory,
            stream_id=body.stream_id,
            call_id=body.call_id,
        )
        if durable is not None:
            return _replay_interaction_resolution(
                record=durable,
                request_hash=request_hash,
                body=body,
                job=job,
            )

        result = job.preview_response(body.call_id, body.response)
        if result.status in {"not_pending", "expired", "conflict"}:
            messages = {
                "not_pending": "This interaction is not awaiting a response.",
                "expired": "This interaction has expired.",
                "conflict": (
                    "This interaction was already resolved with a different response."
                ),
            }
            codes = {
                "not_pending": "not_pending",
                "expired": "expired",
                "conflict": "response_conflict",
            }
            conflict_record = result.record if result.status == "conflict" else None
            raise HTTPException(
                status_code=409,
                detail={
                    "code": codes[result.status],
                    "message": messages[result.status],
                    "stream_id": body.stream_id,
                    "call_id": body.call_id,
                    "existing_decision": (
                        _response_decision(
                            conflict_record.prompt_type,
                            conflict_record.response,
                        )
                        if conflict_record is not None
                        else None
                    ),
                    "source": (
                        conflict_record.source
                        if conflict_record is not None
                        else None
                    ),
                    "tool_call_id": (
                        conflict_record.tool_call_id
                        if conflict_record is not None
                        else None
                    ),
                },
            )

        prompt = result.record
        assert prompt is not None
        submitted_response = (
            prompt.response if result.status == "already_resolved" else body.response
        )
        stored = {
            "stream_id": body.stream_id,
            "session_id": job.session_id,
            "call_id": body.call_id,
            "submitted_response": submitted_response,
            "tool_call_id": prompt.tool_call_id,
            "tool": prompt.tool,
            "prompt_type": prompt.prompt_type,
            "decision": _response_decision(prompt.prompt_type, submitted_response),
            "source": prompt.source or source,
            "resolved_at": datetime.now(timezone.utc).isoformat(),
        }
        ledger = IdempotencyRecord(
            scope=_response_ledger_scope(body.stream_id),
            request_key=body.call_id,
            request_hash=request_hash,
            status="resolved",
            response=stored,
        )
        commit_cancelled = False
        try:
            commit_task = asyncio.create_task(
                _persist_interaction_resolution(session_factory, ledger),
                name=f"interaction-resolution-{body.call_id}",
            )
            _, commit_cancelled = await _await_shielded_commit(commit_task)
        except IntegrityError:
            durable = await _load_interaction_resolution(
                session_factory,
                stream_id=body.stream_id,
                call_id=body.call_id,
            )
            if durable is None:
                raise
            return _replay_interaction_resolution(
                record=durable,
                request_hash=request_hash,
                body=body,
                job=job,
            )

        applied = job.apply_durable_response(
            body.call_id,
            submitted_response,
            source=str(stored["source"]),
        )
        if applied.status == "accepted":
            _publish_interaction_resolution(job, stored)

        if commit_cancelled:
            # The decision is durable and the generation has been resumed;
            # only now is it safe to propagate the disconnected HTTP caller.
            raise asyncio.CancelledError()

        return _interaction_public_payload(
            stored,
            status=result.status,
            idempotent=result.status == "already_resolved",
        )


def _response_decision(prompt_type: str, response: Any) -> str:
    """Return a non-sensitive decision label for acknowledgement events."""
    parsed = response
    if isinstance(response, str):
        try:
            parsed = json.loads(response)
        except (json.JSONDecodeError, TypeError):
            parsed = response

    if prompt_type == "permission":
        allowed = parsed.get("allowed") if isinstance(parsed, dict) else parsed
        if isinstance(allowed, bool):
            return "allowed" if allowed else "denied"
        normalized = str(allowed).lower()
        return "allowed" if normalized in {"allow", "yes", "true", "1"} else "denied"
    if prompt_type == "plan":
        if isinstance(parsed, dict):
            action = parsed.get("action")
            if action in {"accept", "revise", "stop"}:
                return str(action)
        return "submitted"
    if isinstance(parsed, dict) and parsed.get("__cancelled__") in {True, "true"}:
        return "cancelled"
    return "answered"


async def _error_stream(message: str, code: str | None = None):
    """Yield a single error event.

    ``code`` is an optional machine-readable tag (e.g. ``"JOB_NOT_FOUND"``) so
    the client can recover quietly from benign cases instead of surfacing the
    raw message as an alarming toast.
    """
    data: dict[str, Any] = {"error_message": message}
    if code is not None:
        data["code"] = code
    event = SSEEvent(AGENT_ERROR, data)
    event.id = 1
    yield event.encode()
