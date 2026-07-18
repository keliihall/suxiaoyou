"""Production ACP adapter for the existing SessionPrompt/GenerationJob path.

This module intentionally owns no provider or tool execution.  It creates and
loads conversations through ``app.session.manager``, admits a normal
``GenerationJob`` through ``StreamManager``, and invokes ``SessionPrompt.run``
under the same global generation semaphore used by the desktop API.

ACP v1's official ``session/request_permission`` reverse request is used for
ordinary permissions and each separate Hook approval. Questions and plan
reviews remain unsupported and fail closed. Runtime events are projected
through a small allowlist; tool arguments/results, reasoning, Hook payloads,
paths, and exception messages never cross the ACP wire.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
import logging
from pathlib import Path
from typing import Any, Literal, TypeAlias
import uuid

from acp.schema import (
    LoadSessionRequest,
    LoadSessionResponse,
    NewSessionRequest,
    NewSessionResponse,
    PermissionOption,
    PromptRequest as AcpPromptRequest,
    PromptResponse as AcpPromptResponse,
    RequestPermissionRequest,
    RequestPermissionResponse,
    ToolCallUpdate,
)
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.acp.bridge import (
    BridgeCapabilities,
    BridgeRpcError,
    PermissionRequester,
    SessionPromptBridge,
    UpdateEmitter,
)
from app.models.idempotency_record import IdempotencyRecord
from app.models.message import Message
from app.runtime.events import LifecycleEventV1
from app.schemas.chat import PromptRequest as SessionPromptRequest
from app.session.idempotency import canonical_request_hash, get_idempotency_record
from app.session.manager import create_session, get_messages, get_session
from app.streaming.events import (
    AGENT_ERROR,
    DESYNC,
    DONE,
    PERMISSION_REQUEST,
    PLAN_REVIEW,
    QUESTION,
    TEXT_DELTA,
    TOOL_ERROR,
    TOOL_RESULT,
    TOOL_START,
    SSEEvent,
)
from app.streaming.manager import (
    GenerationJob,
    SessionBusyError,
    StreamManager,
)
from app.utils.id import generate_ulid


logger = logging.getLogger(__name__)

ACP_INVALID_PARAMS = -32602
ACP_SERVER_BUSY = -32003
ACP_RUNTIME_LOCKED = -32005
ACP_IDEMPOTENCY_REJECTED = -32006

_ACP_UUID_NAMESPACE = uuid.UUID("1eeab8f9-834a-4c03-8f63-fd53646ffbec")
_INTERACTIVE_SSE_EVENTS: dict[str, str] = {
    PERMISSION_REQUEST: "permission",
    QUESTION: "question",
    PLAN_REVIEW: "plan_review",
}
_INTERACTIVE_LIFECYCLE_EVENTS: dict[str, str] = {
    "interaction.question.requested": "question",
    "plan.review.requested": "plan_review",
}
_QUESTION_TOOLS = frozenset({"question"})
_PLAN_REVIEW_TOOLS = frozenset({"submit_plan", "plan"})
_LEDGER_FINALIZE_RETRY_DELAYS = (0.0, 0.01, 0.05)
_LEDGER_RECONCILE_MAX_DELAY = 5.0

InteractionKind: TypeAlias = Literal[
    "permission", "question", "plan_review", "hook_approval"
]
SessionPromptFactory: TypeAlias = Callable[..., Any]
SessionPromptRunner: TypeAlias = Callable[[Any], Awaitable[None]]
AdmissionGuard: TypeAlias = Callable[[], bool]


def _default_session_prompt_factory(*args: Any, **kwargs: Any) -> Any:
    # Keep the heavy runtime import lazy and make the exact SessionPrompt
    # construction boundary replaceable in focused tests.
    from app.session.prompt import SessionPrompt

    return SessionPrompt(*args, **kwargs)


async def _default_session_prompt_runner(prompt: Any) -> None:
    # SessionPrompt.run owns workspace admission, permission merging, Hooks,
    # checkpoints, the mutation ledger, provider streaming, and tool dispatch.
    await prompt.run()


@dataclass(frozen=True, slots=True)
class _InteractionRefusal:
    kind: InteractionKind
    code: str = "acp_reverse_interaction_unavailable"


@dataclass(slots=True)
class _ActiveTurn:
    job: GenerationJob
    sse_queue: asyncio.Queue[SSEEvent | None]
    lifecycle_queue: asyncio.Queue[LifecycleEventV1 | None]
    assistant_message_id: str
    recorded_user_message_id: str | None = None
    runner_task: asyncio.Task[None] | None = None
    observer_tasks: tuple[asyncio.Task[None], ...] = ()
    settle_task: asyncio.Task[None] | None = None
    cleanup_task: asyncio.Task[None] | None = None
    refusal: _InteractionRefusal | None = None
    cancelled: bool = False
    failure_code: str | None = None
    finish_reason: str | None = None
    refusal_update_emitted: bool = False
    tool_call_ids: dict[str, str] = field(default_factory=dict)
    tool_calls_started: set[str] = field(default_factory=set)
    handled_permission_ids: set[str] = field(default_factory=set)
    settle_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(frozen=True, slots=True)
class _PromptLedger:
    """Connection-independent authority for one keyed ACP prompt."""

    record_id: str
    scope: str
    request_key: str
    request_hash: str


class ProductionSessionPromptBridge(SessionPromptBridge):
    """One-connection ACP bridge backed by the normal application runtime."""

    capabilities = BridgeCapabilities(
        load_session=True,
        image_prompts=False,
        audio_prompts=False,
        embedded_context=False,
        additional_directories=False,
        mcp_stdio=False,
        mcp_http=False,
        mcp_sse=False,
    )

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        stream_manager: StreamManager,
        provider_registry: Any,
        agent_registry: Any,
        tool_registry: Any,
        index_manager: Any | None = None,
        prompt_factory: SessionPromptFactory = _default_session_prompt_factory,
        prompt_runner: SessionPromptRunner = _default_session_prompt_runner,
        semaphore_timeout_seconds: float = 30.0,
        cancellation_timeout_seconds: float = 5.0,
        max_text_chunk_chars: int = 16_384,
        admission_guard: AdmissionGuard | None = None,
        permission_requester: PermissionRequester | None = None,
    ) -> None:
        if not 0 < semaphore_timeout_seconds <= 120:
            raise ValueError("Invalid ACP semaphore timeout")
        if not 0 < cancellation_timeout_seconds <= 30:
            raise ValueError("Invalid ACP cancellation timeout")
        if not 256 <= max_text_chunk_chars <= 65_536:
            raise ValueError("Invalid ACP text chunk limit")

        self.session_factory = session_factory
        self.stream_manager = stream_manager
        self.provider_registry = provider_registry
        self.agent_registry = agent_registry
        self.tool_registry = tool_registry
        self.index_manager = index_manager
        self._prompt_factory = prompt_factory
        self._prompt_runner = prompt_runner
        self._semaphore_timeout_seconds = semaphore_timeout_seconds
        self._cancellation_timeout_seconds = cancellation_timeout_seconds
        self._max_text_chunk_chars = max_text_chunk_chars
        self._admission_guard = admission_guard or (lambda: True)
        self._permission_requester = permission_requester
        self._connection_id = uuid.uuid4().hex

        # These maps are connection-local authority.  A session must be
        # created/loaded on this ACP connection before it can be prompted.
        self._session_directories: dict[str, str] = {}
        self._active_turns: dict[str, _ActiveTurn] = {}
        self._claimed_sessions: set[str] = set()
        self._cancel_requested: set[str] = set()
        self._state_lock = asyncio.Lock()

    def bind_permission_requester(
        self,
        requester: PermissionRequester | None,
    ) -> None:
        """Bind the sole connection-owned ACP permission response channel."""

        if requester is not None and self._permission_requester is not None:
            raise RuntimeError("ACP permission requester is already bound")
        self._permission_requester = requester

    @classmethod
    def from_app_dependencies(cls, **overrides: Any) -> "ProductionSessionPromptBridge":
        """Build the bridge from initialized application singletons.

        This is deliberately not a process launcher; the release-gated stdio
        owner still decides if and when an ACP connection is started.
        """

        from app.dependencies import (
            get_agent_registry,
            get_index_manager,
            get_provider_registry,
            get_session_factory,
            get_stream_manager,
            get_tool_registry,
        )
        from app.security.control import get_security_control

        dependencies: dict[str, Any] = {
            "session_factory": get_session_factory(),
            "stream_manager": get_stream_manager(),
            "provider_registry": get_provider_registry(),
            "agent_registry": get_agent_registry(),
            "tool_registry": get_tool_registry(),
            "index_manager": get_index_manager(),
            "admission_guard": lambda: not get_security_control().emergency_stop,
        }
        dependencies.update(overrides)
        return cls(**dependencies)

    def _require_runtime_admission(self) -> None:
        try:
            allowed = self._admission_guard()
        except Exception:
            allowed = False
        if allowed is not True:
            raise BridgeRpcError(
                ACP_RUNTIME_LOCKED,
                "Runtime locked",
                {"reason": "security_emergency_stop"},
            )

    async def new_session(self, request: NewSessionRequest) -> NewSessionResponse:
        self._require_runtime_admission()
        cwd = self._canonical_directory(request.cwd)
        async with self.session_factory() as db:
            async with db.begin():
                session = await create_session(db, directory=cwd)
        self._session_directories[session.id] = cwd
        return NewSessionResponse.model_validate({"sessionId": session.id})

    async def load_session(
        self,
        request: LoadSessionRequest,
        emit_update: UpdateEmitter,
    ) -> LoadSessionResponse:
        cwd = self._canonical_directory(request.cwd)
        messages = await self._read_bound_session(request.session_id, cwd)
        self._bind_session_directory(request.session_id, cwd)

        # History replay is intentionally text-only.  Reasoning, tool parts,
        # synthetic summaries, files, errors, and Hook data remain private to
        # the application's native history/runtime surfaces.
        for message in messages:
            message_data = (
                message.data if isinstance(message.data, Mapping) else {}
            )
            role = message_data.get("role")
            if role not in {"user", "assistant"}:
                continue
            update_kind = (
                "user_message_chunk" if role == "user" else "agent_message_chunk"
            )
            stored_message_id = message_data.get("acp_message_id")
            message_id = (
                stored_message_id
                if self._is_canonical_uuid(stored_message_id)
                else self._opaque_id(
                    "history-message", request.session_id, message.id
                )
            )
            for part in message.parts:
                data = part.data if isinstance(part.data, Mapping) else {}
                if data.get("type") != "text" or bool(data.get("synthetic")):
                    continue
                text = data.get("text")
                if not isinstance(text, str) or not text:
                    continue
                for chunk in self._text_chunks(text):
                    await emit_update(
                        {
                            "sessionUpdate": update_kind,
                            "messageId": message_id,
                            "content": {"type": "text", "text": chunk},
                        }
                    )
        return LoadSessionResponse.model_validate({})

    async def prompt(
        self,
        request: AcpPromptRequest,
        emit_update: UpdateEmitter,
    ) -> AcpPromptResponse:
        self._require_runtime_admission()
        request_key = self._canonical_prompt_message_id(request.message_id)
        active: _ActiveTurn | None = None
        job: GenerationJob | None = None
        ledger: _PromptLedger | None = None
        terminal_finalizer: asyncio.Task[bool] | None = None
        claimed = False
        ledger_terminal = False
        try:
            # Claim before waiting for the process-wide admission lock. A
            # cancel received while another session is admitting work must be
            # remembered and applied before this prompt can execute.
            await self._claim_prompt(request.session_id)
            claimed = True
            # The durable lookup/insert and GenerationJob registration share
            # the process-wide admission lock. Thus retries from separate ACP
            # connections cannot both observe an absent record and execute.
            async with self.stream_manager.job_admission_lock:
                cwd = self._session_directories.get(request.session_id)
                if cwd is None:
                    raise BridgeRpcError.session_not_found(request.session_id)
                text = self._prompt_text(request)
                request_hash = self._prompt_request_hash(
                    session_id=request.session_id,
                    text=text,
                )

                await self._read_bound_session(
                    request.session_id,
                    cwd,
                    include_messages=False,
                )
                # Emergency stop may be activated after session validation but
                # before GenerationJob admission. Recheck at the last
                # no-side-effect boundary.
                self._require_runtime_admission()

                if request_key is not None:
                    scope = self._prompt_ledger_scope(request.session_id)
                    existing = await self._load_prompt_ledger(
                        scope=scope,
                        request_key=request_key,
                    )
                    if existing is not None:
                        return await self._replay_prompt_ledger(
                            existing,
                            request_hash=request_hash,
                            request_key=request_key,
                        )
                session_request = SessionPromptRequest(
                    session_id=request.session_id,
                    text=text,
                    agent="build",
                    workspace=cwd,
                    permission_presets=None,
                    permission_rules=None,
                    language="zh",
                )
                session_request._external_user_message_id = request_key

                try:
                    job = self.stream_manager.create_job(
                        stream_id=generate_ulid(),
                        session_id=request.session_id,
                        # This server-owned source applies an independent
                        # ceiling above ordinary tool permission and ACP
                        # reverse consent.
                        invocation_source="acp",
                        invocation_source_id=f"acp:{self._connection_id}",
                    )
                except SessionBusyError as exc:
                    raise BridgeRpcError(
                        ACP_SERVER_BUSY,
                        "Server busy",
                        {"reason": "session_prompt_already_active"},
                    ) from exc

                if request_key is not None:
                    record = IdempotencyRecord(
                        scope=scope,
                        request_key=request_key,
                        request_hash=request_hash,
                        status="accepted",
                        response={},
                    )
                    try:
                        record_id, admission_cancelled = (
                            await self._persist_prompt_ledger(record)
                        )
                    except IntegrityError:
                        self.stream_manager.remove_job(job.stream_id)
                        job = None
                        concurrent = await self._load_prompt_ledger(
                            scope=scope,
                            request_key=request_key,
                        )
                        if concurrent is None:
                            raise
                        return await self._replay_prompt_ledger(
                            concurrent,
                            request_hash=request_hash,
                            request_key=request_key,
                        )
                    except Exception:
                        self.stream_manager.remove_job(job.stream_id)
                        job = None
                        raise
                    ledger = _PromptLedger(
                        record_id=record_id,
                        scope=scope,
                        request_key=request_key,
                        request_hash=request_hash,
                    )
                    if admission_cancelled:
                        raise asyncio.CancelledError()

            assert job is not None
            # Permission interaction is enabled only while this bridge is
            # attached to an authenticated ACP connection that owns a reverse
            # requester. Question and plan-review interactions remain denied
            # by the observers below.
            job.interactive = self._permission_requester is not None
            # ACP has no queued/steer-input surface. Closing this admission
            # prevents a desktop endpoint from splicing input into an ACP turn.
            job.close_session_input_admission()
            sse_queue = job.subscribe()
            lifecycle_queue = job.subscribe_lifecycle()
            active = _ActiveTurn(
                job=job,
                sse_queue=sse_queue,
                lifecycle_queue=lifecycle_queue,
                assistant_message_id=self._opaque_id(
                    "assistant-message", request.session_id, job.stream_id
                ),
            )
            async with self._state_lock:
                self._active_turns[request.session_id] = active
                if request.session_id in self._cancel_requested:
                    active.cancelled = True
                    job.deny_pending_responses(source="acp_cancel")
                    job.abort()

            if ledger is not None:
                transitioned = await self._transition_prompt_ledger(
                    ledger,
                    from_statuses=("accepted",),
                    status="running",
                )
                if not transitioned:
                    raise BridgeRpcError(
                        ACP_IDEMPOTENCY_REJECTED,
                        "Request replay rejected",
                        {"reason": "idempotency_state_changed"},
                    )

            sse_observer = asyncio.create_task(
                self._observe_sse(active, emit_update),
                name=f"acp-sse-{job.stream_id}",
            )
            lifecycle_observer = asyncio.create_task(
                self._observe_lifecycle(active, emit_update),
                name=f"acp-lifecycle-{job.stream_id}",
            )
            active.observer_tasks = (sse_observer, lifecycle_observer)
            runner_task = asyncio.create_task(
                self._run_admitted_prompt(active, session_request),
                name=f"acp-prompt-{job.stream_id}",
            )
            active.runner_task = runner_task
            job.task = runner_task

            try:
                await asyncio.gather(runner_task, *active.observer_tasks)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A broken ACP writer/observer must not detach while the
                # underlying SessionPrompt continues executing side effects.
                await asyncio.shield(
                    self._start_settle_aborted_turn(
                        active, source="acp_observer_failure"
                    )
                )
                raise

            pending_kind = self._pending_interaction_kind(job)
            if pending_kind is not None and active.refusal is None:
                await self._refuse_interaction(active, pending_kind, emit_update)
            if job.abort_event.is_set():
                await asyncio.shield(
                    self._start_settle_aborted_turn(
                        active, source="acp_turn_stopped"
                    )
                )

            response = self._prompt_response(active, request_key)
            if ledger is not None:
                if active.failure_code is not None:
                    terminal_status = "failed"
                    error_code = "acp_prompt_failed"
                elif response.stop_reason == "cancelled":
                    terminal_status = "interrupted"
                    error_code = "acp_prompt_interrupted"
                else:
                    terminal_status = "completed"
                    error_code = None
                terminal_finalizer = self._start_prompt_ledger_finalizer(
                    ledger,
                    from_statuses=("accepted", "running"),
                    status=terminal_status,
                    response=self._path_free_prompt_response(response),
                    error_message=error_code,
                )
                ledger_terminal = True
                transitioned = await asyncio.shield(terminal_finalizer)
                if not transitioned:
                    raise BridgeRpcError(
                        ACP_IDEMPOTENCY_REJECTED,
                        "Request replay rejected",
                        {"reason": "idempotency_state_changed"},
                    )
            return response
        except asyncio.CancelledError:
            if active is not None:
                active.cancelled = True
                settle_task = self._start_settle_aborted_turn(
                    active, source="acp_disconnect"
                )
                try:
                    await asyncio.shield(settle_task)
                except asyncio.CancelledError:
                    # The manager-owned settle task keeps this session busy
                    # until the runner and every tracked tool truly stop.
                    pass
            if ledger is not None and not ledger_terminal:
                terminal_finalizer = self._start_prompt_ledger_finalizer(
                    ledger,
                    from_statuses=("accepted", "running"),
                    status="interrupted",
                    error_message="acp_prompt_interrupted",
                )
                ledger_terminal = True
                await asyncio.shield(terminal_finalizer)
            raise
        except Exception:
            if ledger is not None and not ledger_terminal:
                terminal_finalizer = self._start_prompt_ledger_finalizer(
                    ledger,
                    from_statuses=("accepted", "running"),
                    status="failed",
                    error_message="acp_prompt_failed",
                )
                ledger_terminal = True
                await asyncio.shield(terminal_finalizer)
            raise
        finally:
            if active is not None:
                cleanup = self._start_cleanup_turn(request.session_id, active)
                await asyncio.shield(cleanup)
            elif job is not None:
                job.abort()
                if not job.completed:
                    job.complete()
                self.stream_manager.remove_job(job.stream_id)
            if claimed and active is None:
                async with self._state_lock:
                    self._claimed_sessions.discard(request.session_id)
                    self._cancel_requested.discard(request.session_id)

    async def cancel(self, session_id: str) -> None:
        async with self._state_lock:
            if (
                session_id not in self._claimed_sessions
                and session_id not in self._active_turns
            ):
                return
            self._cancel_requested.add(session_id)
            active = self._active_turns.get(session_id)
            if active is not None:
                active.cancelled = True
        if active is not None:
            await asyncio.shield(
                self._start_settle_aborted_turn(active, source="acp_cancel")
            )

    async def disconnect(self, session_ids: Sequence[str]) -> None:
        # Cancel only jobs created by this bridge instance.  In particular, do
        # not call StreamManager.abort_session(), which could kill a desktop or
        # another ACP connection's turn for the same persistent conversation.
        async with self._state_lock:
            active_turns = tuple(self._active_turns.values())
            for active in active_turns:
                active.cancelled = True
                self._cancel_requested.add(active.job.session_id)
        if active_turns:
            await asyncio.gather(
                *(
                    asyncio.shield(
                        self._start_settle_aborted_turn(
                            active, source="acp_disconnect"
                        )
                    )
                    for active in active_turns
                ),
                return_exceptions=True,
            )
        async with self._state_lock:
            for session_id in session_ids:
                self._session_directories.pop(session_id, None)
                self._cancel_requested.discard(session_id)
            self._claimed_sessions.difference_update(session_ids)

    async def _claim_prompt(self, session_id: str) -> None:
        async with self._state_lock:
            if session_id in self._claimed_sessions:
                raise BridgeRpcError(
                    ACP_SERVER_BUSY,
                    "Server busy",
                    {"reason": "session_prompt_already_active"},
                )
            self._claimed_sessions.add(session_id)

    @staticmethod
    def _canonical_prompt_message_id(raw: str | None) -> str | None:
        """Accept only the UUID wire form used as a durable request key.

        ACP keeps ``messageId`` optional, so omitting it retains the legacy
        non-idempotent behavior. Supplying any alternative UUID spelling (or
        an arbitrary client string) is rejected rather than silently falling
        back to unsafe keyless execution.
        """

        if raw is None:
            return None
        try:
            parsed = uuid.UUID(raw)
        except (AttributeError, ValueError) as exc:
            raise BridgeRpcError(
                ACP_INVALID_PARAMS,
                "Invalid params",
                {"reason": "message_id_must_be_canonical_uuid"},
            ) from exc
        if str(parsed) != raw:
            raise BridgeRpcError(
                ACP_INVALID_PARAMS,
                "Invalid params",
                {"reason": "message_id_must_be_canonical_uuid"},
            )
        return raw

    @staticmethod
    def _is_canonical_uuid(raw: object) -> bool:
        if not isinstance(raw, str):
            return False
        try:
            return str(uuid.UUID(raw)) == raw
        except ValueError:
            return False

    @staticmethod
    def _prompt_ledger_scope(session_id: str) -> str:
        # The session identity is structural, not merely part of a JSON
        # response, so a key can never collide across conversations.
        return f"acp.prompt:{session_id}"

    @staticmethod
    def _prompt_request_hash(*, session_id: str, text: str) -> str:
        # Hash the exact text delivered to SessionPrompt. ACP ``messageId`` and
        # transport ``_meta`` are deliberately absent, while the session and
        # effective prompt remain bound to the durable key.
        return canonical_request_hash(
            {"session_id": session_id, "prompt_text": text}
        )

    async def _load_prompt_ledger(
        self,
        *,
        scope: str,
        request_key: str,
    ) -> IdempotencyRecord | None:
        async with self.session_factory() as db:
            return await get_idempotency_record(
                db,
                scope=scope,
                request_key=request_key,
            )

    async def _commit_prompt_ledger(self, record: IdempotencyRecord) -> str:
        async with self.session_factory() as db:
            async with db.begin():
                db.add(record)
                await db.flush()
        return record.id

    async def _own_prompt_ledger_admission(
        self,
        record: IdempotencyRecord,
        cancellation_requested: asyncio.Event,
    ) -> str:
        """Commit admission and close it if its request task already detached."""

        record_id = await self._commit_prompt_ledger(record)
        if cancellation_requested.is_set():
            ledger = _PromptLedger(
                record_id=record_id,
                scope=record.scope,
                request_key=record.request_key,
                request_hash=record.request_hash,
            )
            await self._finalize_prompt_ledger(
                ledger,
                from_statuses=("accepted", "running"),
                status="interrupted",
                error_message="acp_prompt_interrupted",
            )
        return record_id

    async def _persist_prompt_ledger(
        self,
        record: IdempotencyRecord,
    ) -> tuple[str, bool]:
        """Commit admission with bounded caller wait and manager ownership."""

        cancellation_requested = asyncio.Event()
        task = asyncio.create_task(
            self._own_prompt_ledger_admission(record, cancellation_requested),
            name=f"acp-prompt-admission-{record.request_key}",
        )
        self.stream_manager.track_runtime_task(task)
        try:
            return await asyncio.shield(task), False
        except asyncio.CancelledError:
            cancellation_requested.set()
            try:
                record_id = await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=self._cancellation_timeout_seconds,
                )
            except asyncio.TimeoutError:
                # The owned task will either roll back or commit+interrupt. Do
                # not make ACP EOF/shutdown wait forever on a wedged database.
                raise asyncio.CancelledError()
            return record_id, True

    async def _transition_prompt_ledger(
        self,
        ledger: _PromptLedger,
        *,
        from_statuses: tuple[str, ...],
        status: str,
        response: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> bool:
        values: dict[str, Any] = {
            "status": status,
            "error_message": error_message,
        }
        if response is not None:
            values["response"] = response
        async with self.session_factory() as db:
            async with db.begin():
                result = await db.execute(
                    update(IdempotencyRecord)
                    .where(
                        IdempotencyRecord.id == ledger.record_id,
                        IdempotencyRecord.scope == ledger.scope,
                        IdempotencyRecord.request_key == ledger.request_key,
                        IdempotencyRecord.request_hash == ledger.request_hash,
                        IdempotencyRecord.status.in_(from_statuses),
                    )
                    .values(**values)
                )
        return int(result.rowcount or 0) == 1

    async def _finalize_prompt_ledger(
        self,
        ledger: _PromptLedger,
        *,
        from_statuses: tuple[str, ...],
        status: str,
        response: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> bool:
        """Commit a terminal ledger state or transfer it to tracked ownership.

        A false return is a real compare-and-swap conflict. Database failures
        are retried briefly on the request path and then handed to a
        StreamManager-owned reconciler, so connection cleanup cannot forget a
        row that still says accepted/running after execution has settled.
        """

        for delay in _LEDGER_FINALIZE_RETRY_DELAYS:
            if delay:
                await asyncio.sleep(delay)
            try:
                return await self._transition_prompt_ledger(
                    ledger,
                    from_statuses=from_statuses,
                    status=status,
                    response=response,
                    error_message=error_message,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "ACP terminal ledger write will be retried (%s)",
                    type(exc).__name__,
                )

        task = asyncio.create_task(
            self._reconcile_prompt_ledger(
                ledger,
                from_statuses=from_statuses,
                status=status,
                response=response,
                error_message=error_message,
            ),
            name=f"acp-ledger-reconcile-{ledger.record_id}",
        )
        self.stream_manager.track_reconciliation_task(task)
        return True

    def _start_prompt_ledger_finalizer(
        self,
        ledger: _PromptLedger,
        *,
        from_statuses: tuple[str, ...],
        status: str,
        response: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> asyncio.Task[bool]:
        """Create and immediately transfer terminal persistence ownership."""

        task = asyncio.create_task(
            self._finalize_prompt_ledger(
                ledger,
                from_statuses=from_statuses,
                status=status,
                response=response,
                error_message=error_message,
            ),
            name=f"acp-ledger-finalize-{ledger.record_id}",
        )
        self.stream_manager.track_runtime_task(task)
        return task

    async def _reconcile_prompt_ledger(
        self,
        ledger: _PromptLedger,
        *,
        from_statuses: tuple[str, ...],
        status: str,
        response: dict[str, Any] | None,
        error_message: str | None,
    ) -> None:
        """Eventually close one ledger using capped, non-secret backoff."""

        delay = 0.1
        while True:
            try:
                # False means deletion or another valid state transition won;
                # either way this task no longer owns an in-flight row.
                await self._transition_prompt_ledger(
                    ledger,
                    from_statuses=from_statuses,
                    status=status,
                    response=response,
                    error_message=error_message,
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "ACP terminal ledger reconciliation pending (%s)",
                    type(exc).__name__,
                )
            await asyncio.sleep(delay)
            delay = min(delay * 2, _LEDGER_RECONCILE_MAX_DELAY)

    async def _replay_prompt_ledger(
        self,
        record: IdempotencyRecord,
        *,
        request_hash: str,
        request_key: str,
    ) -> AcpPromptResponse:
        if record.request_hash != request_hash:
            raise BridgeRpcError(
                ACP_INVALID_PARAMS,
                "Invalid params",
                {"reason": "idempotency_conflict"},
            )
        if record.status in {"accepted", "running"}:
            raise BridgeRpcError(
                ACP_SERVER_BUSY,
                "Request in flight",
                {
                    "reason": "idempotency_in_flight",
                    "status": record.status,
                },
            )
        if record.status in {"interrupted", "failed"}:
            raise BridgeRpcError(
                ACP_IDEMPOTENCY_REJECTED,
                "Request replay rejected",
                {"reason": f"idempotency_{record.status}"},
            )
        if record.status != "completed":
            raise BridgeRpcError(
                ACP_IDEMPOTENCY_REJECTED,
                "Request replay rejected",
                {"reason": "idempotency_record_invalid"},
            )
        try:
            response = AcpPromptResponse.model_validate(record.response)
        except Exception as exc:
            raise BridgeRpcError(
                ACP_IDEMPOTENCY_REJECTED,
                "Request replay rejected",
                {"reason": "idempotency_record_invalid"},
            ) from exc
        if response.user_message_id != request_key:
            raise BridgeRpcError(
                ACP_IDEMPOTENCY_REJECTED,
                "Request replay rejected",
                {"reason": "idempotency_record_invalid"},
            )
        async with self.session_factory() as db:
            matches = list(
                (
                    await db.execute(
                        select(Message)
                        .where(
                            Message.session_id
                            == record.scope.removeprefix("acp.prompt:"),
                            Message.data["acp_message_id"].as_string()
                            == request_key,
                        )
                        .options(selectinload(Message.parts))
                    )
                ).scalars()
            )
        if len(matches) != 1 or matches[0].data.get("role") != "user":
            raise BridgeRpcError(
                ACP_IDEMPOTENCY_REJECTED,
                "Request replay rejected",
                {"reason": "idempotency_message_binding_invalid"},
            )
        visible_text = [
            part.data.get("text")
            for part in matches[0].parts
            if isinstance(part.data, Mapping)
            and part.data.get("type") == "text"
            and not bool(part.data.get("synthetic"))
            and isinstance(part.data.get("text"), str)
        ]
        session_id = record.scope.removeprefix("acp.prompt:")
        if (
            len(visible_text) != 1
            or self._prompt_request_hash(
                session_id=session_id,
                text=visible_text[0],
            )
            != record.request_hash
        ):
            raise BridgeRpcError(
                ACP_IDEMPOTENCY_REJECTED,
                "Request replay rejected",
                {"reason": "idempotency_message_binding_invalid"},
            )
        return response

    @staticmethod
    def _path_free_prompt_response(
        response: AcpPromptResponse,
    ) -> dict[str, Any]:
        """Serialize only bridge-generated ACP response fields.

        In particular, no runtime exception, prompt, tool payload, workspace,
        checkpoint, or provider field is admitted to this durable replay row.
        """

        payload: dict[str, Any] = {"stopReason": response.stop_reason}
        if response.user_message_id is not None:
            payload["userMessageId"] = response.user_message_id
        meta = response.field_meta
        if isinstance(meta, Mapping):
            raw_suxiaoyou = meta.get("suxiaoyou")
            if isinstance(raw_suxiaoyou, Mapping):
                safe_meta = {
                    key: value
                    for key, value in raw_suxiaoyou.items()
                    if key in {"code", "interactionType"}
                    and isinstance(value, str)
                }
                if safe_meta:
                    payload["_meta"] = {"suxiaoyou": safe_meta}
        return payload

    def _bind_session_directory(self, session_id: str, cwd: str) -> None:
        existing = self._session_directories.get(session_id)
        if existing is not None and existing != cwd:
            raise BridgeRpcError(
                ACP_INVALID_PARAMS,
                "Invalid params",
                {"reason": "session_cwd_mismatch"},
            )
        self._session_directories[session_id] = cwd

    async def _read_bound_session(
        self,
        session_id: str,
        cwd: str,
        *,
        include_messages: bool = True,
    ) -> list[Any]:
        async with self.session_factory() as db:
            session = await get_session(db, session_id)
            if session is None:
                raise BridgeRpcError.session_not_found(session_id)
            stored_cwd = self._canonical_directory(session.directory)
            if stored_cwd != cwd:
                raise BridgeRpcError(
                    ACP_INVALID_PARAMS,
                    "Invalid params",
                    {"reason": "session_cwd_mismatch"},
                )
            if include_messages:
                return await get_messages(db, session_id)
        return []

    def _canonical_directory(self, raw: str) -> str:
        try:
            path = Path(raw)
            if not path.is_absolute():
                raise ValueError("not absolute")
            resolved = path.resolve(strict=True)
            if not resolved.is_dir():
                raise ValueError("not a directory")
        except (OSError, RuntimeError, ValueError) as exc:
            raise BridgeRpcError(
                ACP_INVALID_PARAMS,
                "Invalid params",
                {"reason": "invalid_cwd"},
            ) from exc
        return str(resolved)

    def _prompt_text(self, request: AcpPromptRequest) -> str:
        blocks: list[str] = []
        for block in request.prompt:
            if getattr(block, "type", None) != "text":
                raise BridgeRpcError(
                    ACP_INVALID_PARAMS,
                    "Invalid params",
                    {"reason": "prompt_content_not_supported"},
                )
            text = getattr(block, "text", None)
            if not isinstance(text, str):
                raise BridgeRpcError(
                    ACP_INVALID_PARAMS,
                    "Invalid params",
                    {"reason": "prompt_text_required"},
                )
            blocks.append(text)
        return "\n\n".join(blocks)

    async def _run_admitted_prompt(
        self,
        active: _ActiveTurn,
        request: SessionPromptRequest,
    ) -> None:
        job = active.job
        acquired = False
        prompt: Any | None = None
        try:
            await asyncio.wait_for(
                self.stream_manager._semaphore.acquire(),
                timeout=self._semaphore_timeout_seconds,
            )
            acquired = True
            if job.abort_event.is_set():
                return
            prompt = self._prompt_factory(
                job,
                request,
                session_factory=self.session_factory,
                provider_registry=self.provider_registry,
                agent_registry=self.agent_registry,
                tool_registry=self.tool_registry,
                index_manager=self.index_manager,
                skip_user_message=False,
                require_existing_session=True,
                external_user_message_id=request.external_user_message_id,
            )
            await self._prompt_runner(prompt)
        except asyncio.TimeoutError:
            job.publish(
                SSEEvent(
                    AGENT_ERROR,
                    {
                        "error_type": "server_busy",
                        "error_message": "Server is busy.",
                    },
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Do not interpolate exception text: provider/Hook failures may
            # contain credentials, command output, or private paths.
            logger.error(
                "ACP SessionPrompt failed for stream %s (%s)",
                job.stream_id,
                type(exc).__name__,
            )
            job.publish(
                SSEEvent(
                    AGENT_ERROR,
                    {
                        "error_type": "internal_error",
                        "error_message": "An internal error occurred.",
                    },
                )
            )
        finally:
            recorded = getattr(
                prompt,
                "recorded_external_user_message_id",
                None,
            )
            if self._is_canonical_uuid(recorded):
                active.recorded_user_message_id = recorded
            if acquired:
                self.stream_manager._semaphore.release()
            if not job.completed:
                job.complete()

    async def _observe_sse(
        self,
        active: _ActiveTurn,
        emit_update: UpdateEmitter,
    ) -> None:
        while True:
            event = await active.sse_queue.get()
            if event is None:
                return

            interaction = _INTERACTIVE_SSE_EVENTS.get(event.event)
            if interaction is not None:
                if interaction == "permission":
                    await self._answer_permission(active, event, emit_update)
                else:
                    await self._refuse_interaction(active, interaction, emit_update)
                continue

            if event.event == AGENT_ERROR:
                interaction = self._interaction_from_agent_error(event.data)
                if interaction is not None:
                    await self._refuse_interaction(active, interaction, emit_update)
                elif active.failure_code is None:
                    error_type = event.data.get("error_type")
                    active.failure_code = (
                        "server_busy" if error_type == "server_busy" else "generation_failed"
                    )
                continue

            if event.event == DESYNC:
                if active.failure_code is None:
                    active.failure_code = "runtime_desynchronized"
                active.job.abort()
                continue

            if event.event == TEXT_DELTA:
                text = event.data.get("text")
                if not isinstance(text, str) or not text:
                    continue
                for chunk in self._text_chunks(text):
                    await emit_update(
                        {
                            "sessionUpdate": "agent_message_chunk",
                            "messageId": active.assistant_message_id,
                            "content": {"type": "text", "text": chunk},
                        }
                    )
                continue

            if event.event in {TOOL_START, TOOL_RESULT, TOOL_ERROR}:
                await self._emit_safe_tool_update(active, event, emit_update)
                continue

            if event.event == DONE:
                reason = event.data.get("finish_reason")
                active.finish_reason = reason if isinstance(reason, str) else None
                continue
            # All other SSE payloads (including reasoning, retry/error text,
            # file metadata, plans, titles, task output, and Hook-derived UI
            # events) are intentionally not projected.

    async def _observe_lifecycle(
        self,
        active: _ActiveTurn,
        emit_update: UpdateEmitter,
    ) -> None:
        while True:
            event = await active.lifecycle_queue.get()
            if event is None:
                return

            interaction = _INTERACTIVE_LIFECYCLE_EVENTS.get(event.event_type)
            if interaction is not None:
                await self._refuse_interaction(active, interaction, emit_update)
                continue

            if event.event_type == "hook.dispatch.completed":
                # Hook approvals publish the authoritative PERMISSION_REQUEST
                # after registering their exact application waiter. Lifecycle
                # events are audit observations only; answering them here
                # would race or duplicate the SSE permission interaction.
                continue

            if event.event_type == "runtime.desync":
                if active.failure_code is None:
                    active.failure_code = "runtime_desynchronized"
                active.job.abort()
            # Lifecycle payloads never cross this bridge.  In particular, raw
            # Hook event/dispatch data and checkpoint paths stay server-side.

    async def _answer_permission(
        self,
        active: _ActiveTurn,
        event: SSEEvent,
        emit_update: UpdateEmitter,
    ) -> None:
        """Round-trip one exact normal/Hook permission through official ACP."""

        raw_call_id = event.data.get("call_id")
        if not isinstance(raw_call_id, str) or not raw_call_id:
            await self._refuse_interaction(active, "permission", emit_update)
            return
        if raw_call_id in active.handled_permission_ids:
            return
        active.handled_permission_ids.add(raw_call_id)

        record = active.job.get_response_request(raw_call_id)
        if (
            record is None
            or record.state != "pending"
            or record.prompt_type != "permission"
        ):
            await self._refuse_interaction(active, "permission", emit_update)
            return
        is_hook = bool(record.tool and record.tool.startswith("hook_"))
        requester = self._permission_requester
        if requester is None:
            await self._refuse_interaction(
                active,
                "hook_approval" if is_hook else "permission",
                emit_update,
            )
            return

        private_allow_id = "hook-allow-once" if is_hook else "allow-once"
        private_reject_id = "hook-reject-once" if is_hook else "reject-once"
        # No arguments, locations, content, paths, commands, results, or raw
        # tool ids are included. The ACP server additionally rewrites this
        # already-opaque id before it reaches the wire.
        request = RequestPermissionRequest(
            session_id=active.job.session_id,
            options=[
                PermissionOption(
                    option_id=private_allow_id,
                    kind="allow_once",
                    name="Allow once",
                ),
                PermissionOption(
                    option_id=private_reject_id,
                    kind="reject_once",
                    name="Reject once",
                ),
            ],
            tool_call=ToolCallUpdate(
                tool_call_id=self._opaque_id(
                    "permission", active.job.session_id, active.job.stream_id, raw_call_id
                ),
                title=(
                    "Project Hook approval required"
                    if is_hook
                    else "Permission required"
                ),
                kind="other",
                status="pending",
            ),
        )

        response_payload: dict[str, bool] = {
            "allowed": False,
            # Persistent choices are intentionally not offered. Remembered
            # permission rules affect future turns and need a separately
            # reviewed mapping against the server-side permission ceiling.
            "remember": False,
        }
        resolution_source = "acp_reverse_permission_denied"
        try:
            # Serialize this ACP decision with the desktop durable response
            # path. Holding the job-owned lock makes this connection the sole
            # resolver for its private call id while the client is deciding.
            async with active.job.response_resolution_lock:
                response = await requester(request)
                if not isinstance(response, RequestPermissionResponse):
                    response = RequestPermissionResponse.model_validate(response)
                outcome = response.outcome
                if outcome.outcome == "selected" and outcome.option_id == private_allow_id:
                    response_payload["allowed"] = True
                    resolution_source = "acp_reverse_permission_allow_once"
                elif (
                    outcome.outcome == "selected"
                    and outcome.option_id != private_reject_id
                ):
                    # An unknown or non-offered option can never authorize.
                    resolution_source = "acp_reverse_permission_invalid_option"
                result = active.job.resolve_response(
                    raw_call_id,
                    response_payload,
                    source=resolution_source,
                )
                if result.status not in {"accepted", "already_resolved"}:
                    active.job.deny_pending_responses(
                        source="acp_reverse_permission_resolution_failed"
                    )
                    active.job.abort()
        except asyncio.CancelledError:
            active.job.resolve_response(
                raw_call_id,
                False,
                source="acp_reverse_permission_cancelled",
            )
            raise
        except Exception as exc:
            logger.warning(
                "ACP reverse permission failed for stream %s (%s)",
                active.job.stream_id,
                type(exc).__name__,
            )
            active.job.resolve_response(
                raw_call_id,
                False,
                source="acp_reverse_permission_failed",
            )

    async def _refuse_interaction(
        self,
        active: _ActiveTurn,
        kind: str,
        emit_update: UpdateEmitter,
    ) -> None:
        if kind not in {"permission", "question", "plan_review", "hook_approval"}:
            kind = "permission"
        if active.refusal is None:
            active.refusal = _InteractionRefusal(kind=kind)  # type: ignore[arg-type]
        active.job.deny_pending_responses(source="acp_interaction_unavailable")
        active.job.abort()
        if active.cancelled or active.refusal_update_emitted:
            return
        active.refusal_update_emitted = True
        await emit_update(
            {
                "sessionUpdate": "agent_message_chunk",
                "messageId": active.assistant_message_id,
                "content": {
                    "type": "text",
                    "text": (
                        "This turn was stopped because the ACP client cannot "
                        "answer a required interactive request."
                    ),
                },
            }
        )

    async def _emit_safe_tool_update(
        self,
        active: _ActiveTurn,
        event: SSEEvent,
        emit_update: UpdateEmitter,
    ) -> None:
        raw_call_id = event.data.get("call_id")
        raw_key = raw_call_id if isinstance(raw_call_id, str) else "unidentified"
        call_id = active.tool_call_ids.get(raw_key)
        if call_id is None:
            call_id = self._opaque_id(
                "tool-call", active.job.session_id, active.job.stream_id, raw_key
            )
            active.tool_call_ids[raw_key] = call_id

        if event.event == TOOL_START:
            status = "in_progress"
            title = "Running a tool"
        elif event.event == TOOL_RESULT:
            status = "completed"
            title = "Tool completed"
        else:
            status = "failed"
            title = "Tool failed"

        if call_id not in active.tool_calls_started:
            active.tool_calls_started.add(call_id)
            await emit_update(
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": call_id,
                    "title": title,
                    "kind": "other",
                    "status": status,
                }
            )
            return
        await emit_update(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": call_id,
                "title": title,
                "status": status,
            }
        )

    @staticmethod
    def _interaction_from_agent_error(data: Mapping[str, Any]) -> str | None:
        error_type = data.get("error_type")
        if error_type == "permission_required":
            return "permission"
        if error_type != "invocation_source_denied":
            return None
        tool = data.get("tool")
        if tool in _QUESTION_TOOLS:
            return "question"
        if tool in _PLAN_REVIEW_TOOLS:
            return "plan_review"
        return None

    @staticmethod
    def _pending_interaction_kind(job: GenerationJob) -> InteractionKind | None:
        for record in job._response_requests.values():
            if record.state != "pending":
                continue
            if record.prompt_type == "question":
                return "question"
            if record.prompt_type == "plan":
                return "plan_review"
            return "permission"
        return None

    def _prompt_response(
        self,
        active: _ActiveTurn,
        user_message_id: str | None,
    ) -> AcpPromptResponse:
        payload: dict[str, Any]
        if active.cancelled or (
            active.job.abort_event.is_set() and active.refusal is None
        ):
            payload = {"stopReason": "cancelled"}
        elif active.refusal is not None:
            payload = {
                "stopReason": "refusal",
                "_meta": {
                    "suxiaoyou": {
                        "code": active.refusal.code,
                        "interactionType": active.refusal.kind,
                    }
                },
            }
        elif active.failure_code is not None:
            payload = {
                "stopReason": "refusal",
                "_meta": {"suxiaoyou": {"code": active.failure_code}},
            }
        elif active.finish_reason in {"length", "max_tokens"}:
            payload = {"stopReason": "max_tokens"}
        elif active.finish_reason in {
            "usage_limited",
            "budget_limited",
            "max_turn_requests",
        }:
            payload = {"stopReason": "max_turn_requests"}
        else:
            payload = {"stopReason": "end_turn"}
        if (
            user_message_id is not None
            and active.recorded_user_message_id == user_message_id
        ):
            payload["userMessageId"] = user_message_id
        return AcpPromptResponse.model_validate(payload)

    async def _settle_aborted_turn(
        self,
        active: _ActiveTurn,
        *,
        source: str,
    ) -> None:
        async with active.settle_lock:
            active.job.deny_pending_responses(source=source)
            active.job.abort()
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self._cancellation_timeout_seconds
            tools_quiesced = await active.job.wait_for_tool_tasks(
                max(0.0, deadline - loop.time())
            )
            if not tools_quiesced:
                # Cancellation is advisory in Python and native work may not
                # stop at the deadline. Keep this manager-owned task and the
                # session admission alive until every side effect is actually
                # quiescent.
                await active.job.wait_for_tool_tasks_to_finish()
            task = active.runner_task
            if (
                task is None
                or task is asyncio.current_task()
                or task.done()
            ):
                return
            try:
                await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=max(0.0, deadline - loop.time()),
                )
            except asyncio.TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    def _start_settle_aborted_turn(
        self,
        active: _ActiveTurn,
        *,
        source: str,
    ) -> asyncio.Task[None]:
        """Start one manager-owned quiescer before the caller can be cancelled."""

        existing = active.settle_task
        if existing is not None:
            return existing
        # Close all execution/interaction admission synchronously, before the
        # new Task gets its first event-loop slice.
        active.job.deny_pending_responses(source=source)
        active.job.abort()
        task = asyncio.create_task(
            self._settle_aborted_turn(active, source=source),
            name=f"acp-settle-{active.job.stream_id}",
        )
        active.settle_task = task
        self.stream_manager.track_runtime_task(task)
        return task

    def _start_cleanup_turn(
        self,
        session_id: str,
        active: _ActiveTurn,
    ) -> asyncio.Task[None]:
        """Transfer cleanup ownership before an outer cancellation can recur."""

        existing = active.cleanup_task
        if existing is not None:
            return existing
        task = asyncio.create_task(
            self._cleanup_turn(session_id, active),
            name=f"acp-cleanup-{active.job.stream_id}",
        )
        active.cleanup_task = task
        self.stream_manager.track_runtime_task(task)
        return task

    async def _cleanup_turn(
        self,
        session_id: str,
        active: _ActiveTurn,
    ) -> None:
        settle = active.settle_task
        if settle is not None and settle is not asyncio.current_task():
            await asyncio.shield(settle)
        runner = active.runner_task
        if (
            runner is not None
            and runner is not asyncio.current_task()
            and not runner.done()
        ):
            await asyncio.shield(runner)
        await active.job.wait_for_tool_tasks_to_finish()
        active.job.unsubscribe(active.sse_queue)
        active.job.unsubscribe_lifecycle(active.lifecycle_queue)
        current = asyncio.current_task()
        pending = [
            task
            for task in active.observer_tasks
            if task is not current and not task.done()
        ]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if not active.job.completed:
            active.job.complete()
        async with self._state_lock:
            if self._active_turns.get(session_id) is active:
                self._active_turns.pop(session_id, None)
            self._claimed_sessions.discard(session_id)
            self._cancel_requested.discard(session_id)

    def _text_chunks(self, value: str) -> list[str]:
        return [
            value[index : index + self._max_text_chunk_chars]
            for index in range(0, len(value), self._max_text_chunk_chars)
        ]

    @staticmethod
    def _opaque_id(*parts: str) -> str:
        return str(uuid.uuid5(_ACP_UUID_NAMESPACE, "\x1f".join(parts)))


__all__ = [
    "ACP_IDEMPOTENCY_REJECTED",
    "ACP_INVALID_PARAMS",
    "ACP_RUNTIME_LOCKED",
    "ACP_SERVER_BUSY",
    "ProductionSessionPromptBridge",
    "SessionPromptFactory",
    "SessionPromptRunner",
]
