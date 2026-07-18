"""GenerationJob and StreamManager for resumable SSE streaming."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

from app.runtime.events import (
    LifecycleEventV1,
    lifecycle_event_from_transport,
    sanitize_lifecycle_payload,
)
from app.streaming.events import AGENT_ERROR, DESYNC, DONE, SSEEvent
from app.i18n import Language
from app.security.capabilities import InvocationSource, normalize_invocation_source

# Events that MUST be delivered to the frontend even when the queue overflows.
# Losing these causes the UI to get permanently stuck in "generating" state.
_TERMINAL_EVENTS = frozenset({DONE, AGENT_ERROR})

logger = logging.getLogger(__name__)


class SessionBusyError(RuntimeError):
    def __init__(self, session_id: str, stream_id: str):
        self.session_id = session_id
        self.stream_id = stream_id
        super().__init__(f"Session {session_id} is already running stream {stream_id}")


ResponsePromptType = Literal["permission", "question", "plan", "unknown"]
ResponseRequestState = Literal["pending", "resolved", "expired"]
ResponseSubmitStatus = Literal[
    "accepted",
    "already_resolved",
    "not_pending",
    "expired",
    "conflict",
]


@dataclass
class ResponseRequestRecord:
    """Lifecycle record for one interactive prompt.

    The record deliberately outlives the Future.  POST /chat/respond can then
    distinguish an unknown call id from an expired prompt and can make retries
    idempotent after the original HTTP response was lost.
    """

    call_id: str
    prompt_type: ResponsePromptType
    tool_call_id: str | None
    tool: str | None
    expires_at: float | None
    future: asyncio.Future[Any]
    state: ResponseRequestState = "pending"
    response: Any = None
    source: str | None = None


@dataclass(frozen=True)
class ResponseSubmitResult:
    status: ResponseSubmitStatus
    record: ResponseRequestRecord | None


class GenerationJob:
    """Tracks a single generation lifecycle.

    - Buffers all events for replay on reconnect
    - Supports multiple subscriber queues
    - Provides abort signaling
    - Interactive mode for permission/question prompts
    """

    # Max events to keep in the replay buffer per job
    _MAX_EVENT_BUFFER = 5000

    def __init__(
        self,
        stream_id: str,
        session_id: str,
        *,
        language: Language = "zh",
        invocation_source: InvocationSource = "unknown",
        invocation_source_id: str | None = None,
        goal_id: str | None = None,
        goal_run_id: str | None = None,
        goal_session_id: str | None = None,
        root_turn_id: str | None = None,
        turn_run_id: str | None = None,
        parent_turn_id: str | None = None,
        workspace_instance_id: str | None = None,
        post_checkpoint_validation_scheduler: Any | None = None,
    ):
        self.stream_id = stream_id
        self.session_id = session_id
        # Until the durable TurnRun row is admitted, a root generation's stream
        # id is its stable runtime identity. Child jobs explicitly inherit the
        # root turn while retaining their own turn_run_id.
        self._root_turn_id = str(root_turn_id or stream_id)
        self._turn_run_id = str(turn_run_id or stream_id)
        self._parent_turn_id = (
            str(parent_turn_id) if parent_turn_id is not None else None
        )
        self._workspace_instance_id = (
            str(workspace_instance_id) if workspace_instance_id else None
        )
        # Server-injected only. Prompt/tool request payloads have no field that
        # can select a validator, checkpoint, or validation budget.
        self._post_checkpoint_validation_scheduler = (
            post_checkpoint_validation_scheduler
        )
        # The ingress owns this immutable root source.  It never comes from a
        # PromptRequest JSON field, and child agents inherit it unchanged.
        self._invocation_source = normalize_invocation_source(invocation_source)
        self._invocation_source_id = (
            " ".join(str(invocation_source_id).split())[:160]
            if invocation_source_id
            else None
        )
        # Goal identity is server-owned execution metadata.  Keeping it on the
        # job lets status and emergency-stop surfaces identify autonomous work
        # without trusting PromptRequest JSON or scraping an SSE payload.
        self._goal_id = str(goal_id) if goal_id else None
        self._goal_run_id = str(goal_run_id) if goal_run_id else None
        self._goal_session_id = (
            str(goal_session_id)
            if goal_session_id
            else (str(session_id) if goal_id else None)
        )
        self._goal_usage_accumulator: dict[str, int] = {
            "tokens": 0,
            "cost_microusd": 0,
        }
        # A Goal stream spans multiple durable GoalRuns.  Budget admission for
        # the current run must see usage already produced by parent/child
        # prompts before the aggregate is committed at the run boundary.
        self._goal_run_usage_baseline: tuple[int, int] = (0, 0)
        self._goal_wait_started_at: float | None = None
        self._goal_wait_accumulated = 0.0
        # Request-scoped display language for generated tool/API activity.
        # This is presentation state, not part of the persisted session identity.
        self.language: Language = language
        self.events: list[SSEEvent] = []
        self.subscribers: list[asyncio.Queue[SSEEvent | None]] = []
        self.lifecycle_events: list[LifecycleEventV1] = []
        self.lifecycle_subscribers: list[
            asyncio.Queue[LifecycleEventV1 | None]
        ] = []
        self._lifecycle_root: GenerationJob = self
        self._lifecycle_event_counter = 0
        self.abort_event = asyncio.Event()
        # Admission and the final durable-queue check share this lock.  Without
        # it, an input can commit immediately after the generation observes an
        # empty queue but immediately before the stream completes, leaving a
        # permanently queued instruction with no worker to consume it.
        self.session_input_lock = asyncio.Lock()
        self._accepting_session_inputs = True
        # Linearizes Provider/tool admission with Goal pause/edit/archive.
        # The in-memory gate closes before the durable transition awaits, so a
        # concurrent execution either starts before that control operation or
        # observes the closed gate; it cannot slip through after commit.
        self.execution_admission_lock = asyncio.Lock()
        self._execution_admission_state = {"open": True}
        self._completed = False
        self._event_counter = 0
        self._response_requests: dict[str, ResponseRequestRecord] = {}
        # Serializes validation, durable resolution commit, and Future wake-up
        # for POST /chat/respond.  The database remains the source of truth for
        # retries, while this lock prevents two local clients from racing the
        # pending in-memory prompt.
        self.response_resolution_lock = asyncio.Lock()

        # Strong reference to the asyncio.Task running this job's generation.
        # Prevents GC from silently cancelling fire-and-forget tasks.
        self.task: asyncio.Task[None] | None = None

        # Tool calls can run in child tasks while the provider is still
        # streaming.  Track them explicitly so an emergency stop can cancel and
        # await the actual external side effects, not merely set an advisory
        # event on the parent generation.
        self._tool_tasks: set[asyncio.Task[Any]] = set()

        # Interactive mode: True when a client can answer SSE prompts.
        # Headless/non-interactive permission "ask" requests fail closed.
        self.interactive: bool = False

        # Nesting depth for subtask recursion guard
        self._depth: int = 0

        # Artifact content cache: identifier → {content, type, title, language}
        # Populated from message history at generation start, updated by artifact tool
        self.artifact_cache: dict[str, dict[str, Any]] = {}
        # Todo reminders are useful once per unchanged Todo projection, not once
        # per tool call or autonomous Goal slice. Keep the signature on the
        # stream job so a multi-slice Goal does not repeatedly inject the same
        # reminder back into its own context.
        self.todo_reminder_signatures: dict[
            str,
            tuple[tuple[str, str, str], ...],
        ] = {}

    @property
    def completed(self) -> bool:
        return self._completed

    @property
    def is_quiescent(self) -> bool:
        """True only when no runner or tracked tool can still cause effects."""

        runner = self.task
        return bool(
            self._completed
            and not self._tool_tasks
            and (runner is None or runner.done())
        )

    @property
    def invocation_source(self) -> InvocationSource:
        return self._invocation_source

    @property
    def invocation_source_id(self) -> str | None:
        return self._invocation_source_id

    @property
    def root_turn_id(self) -> str:
        return self._root_turn_id

    @property
    def turn_run_id(self) -> str:
        return self._turn_run_id

    @property
    def parent_turn_id(self) -> str | None:
        return self._parent_turn_id

    @property
    def workspace_instance_id(self) -> str | None:
        return self._workspace_instance_id

    @property
    def post_checkpoint_validation_scheduler(self) -> Any | None:
        return self._post_checkpoint_validation_scheduler

    def begin_root_turn(self, turn_id: str) -> None:
        """Advance one transport stream to a distinct queued root turn."""

        if self._lifecycle_root is not self or self._parent_turn_id is not None:
            raise ValueError("A child generation job cannot begin a root turn")
        normalized = str(turn_id).strip()
        if not normalized:
            raise ValueError("turn_id cannot be empty")
        self._root_turn_id = normalized
        self._turn_run_id = normalized

    def bind_workspace_instance(self, workspace_instance_id: str) -> None:
        """Bind the job once to the durable workspace selected during setup."""

        normalized = str(workspace_instance_id).strip()
        if not normalized:
            raise ValueError("workspace_instance_id cannot be empty")
        if (
            self._workspace_instance_id is not None
            and self._workspace_instance_id != normalized
        ):
            raise ValueError("A generation job cannot change workspace instance")
        self._workspace_instance_id = normalized

    def inherit_runtime_context(self, parent: "GenerationJob") -> None:
        """Bind a child job to its parent's immutable root turn/workspace."""

        self._root_turn_id = parent.root_turn_id
        self._parent_turn_id = parent.turn_run_id
        self._workspace_instance_id = parent.workspace_instance_id
        self._post_checkpoint_validation_scheduler = (
            parent.post_checkpoint_validation_scheduler
        )
        lifecycle_root = parent._lifecycle_root
        self._lifecycle_root = lifecycle_root
        self.lifecycle_events = lifecycle_root.lifecycle_events
        self.lifecycle_subscribers = lifecycle_root.lifecycle_subscribers

    @property
    def goal_id(self) -> str | None:
        return self._goal_id

    @property
    def goal_run_id(self) -> str | None:
        return self._goal_run_id

    @property
    def goal_session_id(self) -> str | None:
        return self._goal_session_id

    def set_goal_run_id(self, goal_run_id: str | None) -> None:
        """Advance the current GoalRun identity at a continuation boundary."""

        normalized = str(goal_run_id) if goal_run_id else None
        if normalized != self._goal_run_id:
            self._goal_run_usage_baseline = self.goal_usage
        self._goal_run_id = normalized

    def set_goal_identity(
        self,
        *,
        goal_id: str,
        goal_run_id: str | None = None,
    ) -> None:
        """Install server-owned Goal identity after durable admission commits."""

        if self._goal_id is not None and self._goal_id != goal_id:
            raise ValueError("A generation job cannot change Goal ownership")
        self._goal_id = str(goal_id)
        if self._goal_session_id is None:
            self._goal_session_id = self.session_id
        self.set_goal_run_id(goal_run_id)

    def inherit_goal_context(self, parent: "GenerationJob") -> None:
        """Share a parent's immutable Goal gate and cumulative usage ledger."""

        if parent.goal_id is None:
            return
        self._goal_id = parent.goal_id
        self._goal_run_id = parent.goal_run_id
        self._goal_session_id = parent.goal_session_id or parent.session_id
        self._goal_usage_accumulator = parent._goal_usage_accumulator
        self._goal_run_usage_baseline = parent._goal_run_usage_baseline
        self.execution_admission_lock = parent.execution_admission_lock
        self._execution_admission_state = parent._execution_admission_state

    def record_goal_usage(self, *, tokens: int, cost_microusd: int) -> None:
        if self.goal_id is None:
            return
        self._goal_usage_accumulator["tokens"] += max(0, int(tokens))
        self._goal_usage_accumulator["cost_microusd"] += max(
            0,
            int(cost_microusd),
        )

    @property
    def goal_usage(self) -> tuple[int, int]:
        return (
            self._goal_usage_accumulator["tokens"],
            self._goal_usage_accumulator["cost_microusd"],
        )

    @property
    def goal_run_usage(self) -> tuple[int, int]:
        """Uncommitted shared usage accumulated in the current GoalRun."""

        tokens, cost = self.goal_usage
        baseline_tokens, baseline_cost = self._goal_run_usage_baseline
        return (
            max(0, tokens - baseline_tokens),
            max(0, cost - baseline_cost),
        )

    def set_goal_waiting(self, waiting: bool) -> None:
        """Track interactive wait time so Goal active-time budgets exclude it."""

        now = time.monotonic()
        if waiting:
            if self._goal_wait_started_at is None:
                self._goal_wait_started_at = now
            return
        if self._goal_wait_started_at is not None:
            self._goal_wait_accumulated += max(
                0.0,
                now - self._goal_wait_started_at,
            )
            self._goal_wait_started_at = None

    @property
    def goal_wait_seconds(self) -> float:
        current = self._goal_wait_accumulated
        if self._goal_wait_started_at is not None:
            current += max(0.0, time.monotonic() - self._goal_wait_started_at)
        return current

    def deny_pending_responses(self, *, source: str = "goal_pause") -> int:
        """Wake all interactive waits with a fail-closed denial."""

        resolved = 0
        for record in list(self._response_requests.values()):
            if record.state != "pending":
                continue
            result = self.resolve_response(
                record.call_id,
                False,
                source=source,
            )
            if result.status == "accepted":
                resolved += 1
        return resolved

    @property
    def accepting_session_inputs(self) -> bool:
        return self._accepting_session_inputs and not self._completed

    def close_session_input_admission(self) -> None:
        self._accepting_session_inputs = False

    @property
    def execution_admission_open(self) -> bool:
        return bool(self._execution_admission_state["open"]) and not self.abort_event.is_set()

    def close_execution_admission(self) -> None:
        self._execution_admission_state["open"] = False

    def open_execution_admission(self) -> None:
        if not self.abort_event.is_set():
            self._execution_admission_state["open"] = True

    def publish(self, event: SSEEvent) -> None:
        """Publish an event to all subscribers and buffer for replay."""
        self._event_counter += 1
        event.id = self._event_counter
        self.events.append(event)
        lifecycle_root = self._lifecycle_root
        lifecycle_root._lifecycle_event_counter += 1
        lifecycle_event = lifecycle_event_from_transport(
            sequence=lifecycle_root._lifecycle_event_counter,
            transport_event=event.event,
            data=event.data,
            session_id=self.session_id,
            stream_id=self.stream_id,
            root_turn_id=self.root_turn_id,
            turn_run_id=self.turn_run_id,
            workspace_instance_id=self.workspace_instance_id,
            invocation_source=self.invocation_source,
        )
        lifecycle_root.lifecycle_events.append(lifecycle_event)

        # Cap replay buffer to prevent unbounded memory growth
        if len(self.events) > self._MAX_EVENT_BUFFER:
            self.events = self.events[-self._MAX_EVENT_BUFFER:]
        if len(lifecycle_root.lifecycle_events) > self._MAX_EVENT_BUFFER:
            lifecycle_root.lifecycle_events[:] = lifecycle_root.lifecycle_events[
                -self._MAX_EVENT_BUFFER:
            ]

        for q in self.subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("Subscriber queue full, dropping event %d (type=%s)", event.id, event.event)
                # Make room by clearing queue
                while not q.empty():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        break

                if event.event in _TERMINAL_EVENTS:
                    # Terminal events MUST be delivered — losing DONE/AGENT_ERROR
                    # causes the frontend to stay stuck in "generating" forever.
                    try:
                        q.put_nowait(event)
                    except Exception:
                        pass
                else:
                    # Non-terminal: notify client that events were lost
                    try:
                        q.put_nowait(SSEEvent(DESYNC, {"dropped_event_id": event.id}))
                    except Exception:
                        pass

        for q in lifecycle_root.lifecycle_subscribers:
            try:
                q.put_nowait(lifecycle_event)
            except asyncio.QueueFull:
                while not q.empty():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                q.put_nowait(
                    LifecycleEventV1(
                        sequence=lifecycle_event.sequence,
                        event_type="runtime.desync",
                        session_id=self.session_id,
                        stream_id=self.stream_id,
                        event_id=(
                            f"{self.stream_id}:{lifecycle_event.sequence}:live-desync"
                        ),
                        root_turn_id=self.root_turn_id,
                        turn_run_id=self.turn_run_id,
                        workspace_instance_id=self.workspace_instance_id,
                        invocation_source=self.invocation_source,
                        payload={"dropped_through_sequence": lifecycle_event.sequence - 1},
                    )
                )
                q.put_nowait(lifecycle_event)

    def publish_lifecycle(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        message_id: str | None = None,
        call_id: str | None = None,
        checkpoint_id: str | None = None,
    ) -> LifecycleEventV1:
        """Publish a transport-neutral fact without inventing an SSE event."""

        lifecycle_root = self._lifecycle_root
        lifecycle_root._lifecycle_event_counter += 1
        event = LifecycleEventV1(
            sequence=lifecycle_root._lifecycle_event_counter,
            event_type=event_type,
            session_id=self.session_id,
            stream_id=self.stream_id,
            root_turn_id=self.root_turn_id,
            turn_run_id=self.turn_run_id,
            workspace_instance_id=self.workspace_instance_id,
            message_id=str(message_id) if message_id is not None else None,
            call_id=str(call_id) if call_id is not None else None,
            checkpoint_id=(
                str(checkpoint_id) if checkpoint_id is not None else None
            ),
            invocation_source=self.invocation_source,
            payload=sanitize_lifecycle_payload(payload or {}),
        )
        lifecycle_root.lifecycle_events.append(event)
        if len(lifecycle_root.lifecycle_events) > self._MAX_EVENT_BUFFER:
            lifecycle_root.lifecycle_events[:] = lifecycle_root.lifecycle_events[
                -self._MAX_EVENT_BUFFER:
            ]
        for queue in lifecycle_root.lifecycle_subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                while not queue.empty():
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                queue.put_nowait(
                    LifecycleEventV1(
                        sequence=event.sequence,
                        event_type="runtime.desync",
                        session_id=self.session_id,
                        stream_id=self.stream_id,
                        event_id=f"{self.stream_id}:{event.sequence}:live-desync",
                        root_turn_id=self.root_turn_id,
                        turn_run_id=self.turn_run_id,
                        workspace_instance_id=self.workspace_instance_id,
                        invocation_source=self.invocation_source,
                        payload={"dropped_through_sequence": event.sequence - 1},
                    )
                )
                queue.put_nowait(event)
        return event

    def subscribe_lifecycle(
        self,
        last_sequence: int = 0,
    ) -> asyncio.Queue[LifecycleEventV1 | None]:
        """Subscribe to the transport-neutral runtime event stream."""

        lifecycle_root = self._lifecycle_root
        queue: asyncio.Queue[LifecycleEventV1 | None] = asyncio.Queue(maxsize=5000)
        replay = [
            event
            for event in lifecycle_root.lifecycle_events
            if event.sequence > last_sequence
        ]
        first_available_sequence = (
            lifecycle_root.lifecycle_events[0].sequence
            if lifecycle_root.lifecycle_events
            else None
        )
        history_gap = (
            first_available_sequence is not None
            and last_sequence < first_available_sequence - 1
        )
        terminal_reserve = 1 if lifecycle_root._completed else 0
        available = queue.maxsize - terminal_reserve
        needs_desync = history_gap or len(replay) > available
        if needs_desync:
            replay_capacity = max(0, available - 1)
            if len(replay) > replay_capacity:
                replay = replay[-replay_capacity:] if replay_capacity else []
            first_sequence = (
                replay[0].sequence
                if replay
                else first_available_sequence or max(1, last_sequence + 1)
            )
            queue.put_nowait(
                LifecycleEventV1(
                    sequence=max(last_sequence, first_sequence - 1),
                    event_type="runtime.desync",
                    session_id=self.session_id,
                    stream_id=self.stream_id,
                    event_id=(
                        f"{self.stream_id}:{first_sequence}:replay-desync"
                    ),
                    root_turn_id=self.root_turn_id,
                    turn_run_id=self.turn_run_id,
                    workspace_instance_id=self.workspace_instance_id,
                    invocation_source=self.invocation_source,
                    payload={
                        "requested_last_sequence": last_sequence,
                        "first_available_sequence": first_sequence,
                    },
                )
            )
        for event in replay:
            queue.put_nowait(event)
        if lifecycle_root._completed:
            queue.put_nowait(None)
        else:
            lifecycle_root.lifecycle_subscribers.append(queue)
        return queue

    def unsubscribe_lifecycle(
        self,
        queue: asyncio.Queue[LifecycleEventV1 | None],
    ) -> None:
        lifecycle_root = self._lifecycle_root
        try:
            lifecycle_root.lifecycle_subscribers.remove(queue)
        except ValueError:
            pass

    def subscribe(self, last_event_id: int = 0) -> asyncio.Queue[SSEEvent | None]:
        """Create a subscriber queue. Replays missed events if last_event_id > 0."""
        q: asyncio.Queue[SSEEvent | None] = asyncio.Queue(maxsize=5000)

        # Replay buffered events after last_event_id. On long generations the
        # replay slice can be larger than the queue capacity; if that happens,
        # trim the oldest replay events instead of raising QueueFull (which
        # would turn a harmless reconnect into an HTTP 500 and strand the UI in
        # "finalizing"). The frontend treats DESYNC as a signal to refetch DB
        # state, so it is safe to explicitly notify it when replay is trimmed.
        replay_events = [
            event
            for event in self.events
            if event.id is not None and event.id > last_event_id
        ]
        first_available_id = (
            self.events[0].id
            if self.events and self.events[0].id is not None
            else None
        )
        history_gap = (
            first_available_id is not None
            and last_event_id < first_available_id - 1
        )
        terminal_reserve = 1 if self._completed else 0
        overflow_without_desync = (
            len(replay_events) > q.maxsize - terminal_reserve
        )
        needs_desync = history_gap or overflow_without_desync
        capacity = max(
            0,
            q.maxsize - terminal_reserve - (1 if needs_desync else 0),
        )
        dropped_event_id = (
            first_available_id - 1
            if history_gap and first_available_id is not None
            else None
        )
        if len(replay_events) > capacity:
            dropped = len(replay_events) - capacity
            logger.warning(
                "Replay buffer overflow for stream %s: dropping %d old replay events",
                self.stream_id,
                dropped,
            )
            dropped_event_id = replay_events[dropped - 1].id
            replay_events = replay_events[dropped:]

        if needs_desync:
            # A reconnect cursor older than the retained buffer is a data-loss
            # condition even when the remaining 5,000 events fit the subscriber
            # queue exactly.  Tell the client to refetch persisted state instead
            # of silently presenting an incomplete stream.
            assert dropped_event_id is not None
            desync = SSEEvent(
                DESYNC,
                {
                    "dropped_event_id": dropped_event_id,
                    "requested_last_event_id": last_event_id,
                    "first_available_event_id": (
                        replay_events[0].id
                        if replay_events
                        else first_available_id
                    ),
                },
            )
            desync.id = dropped_event_id
            q.put_nowait(desync)

        for event in replay_events:
            q.put_nowait(event)

        # If already completed, signal end immediately
        if self._completed:
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(None)
        else:
            self.subscribers.append(q)

        return q

    def unsubscribe(self, queue: asyncio.Queue[SSEEvent | None]) -> None:
        """Detach a live SSE subscriber; safe after completion and repeated calls."""

        try:
            self.subscribers.remove(queue)
        except ValueError:
            pass

    def complete(self) -> None:
        """Mark generation as complete. Signal all subscribers."""
        self.close_session_input_admission()
        self._completed = True
        for q in self.subscribers:
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                # Completion is part of the stream protocol, not a best-effort
                # notification.  A stalled subscriber with a full queue would
                # otherwise wait forever.  Drop the stale backlog, explicitly
                # request a DB refetch, then deliver the terminal sentinel.
                while not q.empty():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                desync = SSEEvent(
                    DESYNC,
                    {"dropped_event_id": self._event_counter},
                )
                desync.id = self._event_counter
                q.put_nowait(desync)
                q.put_nowait(None)
        self.subscribers.clear()
        # Child jobs share their root job's lifecycle stream. Completing one
        # child must not terminate ACP/Hook observers for the still-running root.
        if self._lifecycle_root is not self:
            return
        for q in self.lifecycle_subscribers:
            if q.full():
                while not q.empty():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                q.put_nowait(
                    LifecycleEventV1(
                        sequence=self._lifecycle_event_counter,
                        event_type="runtime.desync",
                        session_id=self.session_id,
                        stream_id=self.stream_id,
                        event_id=(
                            f"{self.stream_id}:{self._lifecycle_event_counter}:completion-desync"
                        ),
                        root_turn_id=self.root_turn_id,
                        turn_run_id=self.turn_run_id,
                        workspace_instance_id=self.workspace_instance_id,
                        invocation_source=self.invocation_source,
                        payload={
                            "dropped_through_sequence": self._lifecycle_event_counter
                        },
                    )
                )
            q.put_nowait(None)
        self.lifecycle_subscribers.clear()

    def abort(self) -> None:
        """Signal abort to the generation loop."""
        self.close_execution_admission()
        self.abort_event.set()
        for task in list(self._tool_tasks):
            if not task.done():
                task.cancel()

    def track_tool_task(self, task: asyncio.Task[Any]) -> None:
        """Own an in-flight tool task until completion or emergency cancellation."""

        self._tool_tasks.add(task)
        task.add_done_callback(self._tool_tasks.discard)
        if self.abort_event.is_set() and not task.done():
            task.cancel()

    async def wait_for_tool_tasks(self, timeout: float) -> bool:
        """Wait for tracked tools to quiesce; return False on the bounded timeout."""

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)
        while self._tool_tasks:
            remaining = deadline - loop.time()
            if remaining <= 0:
                for task in list(self._tool_tasks):
                    task.cancel()
                return False
            tasks = set(self._tool_tasks)
            _done, pending = await asyncio.wait(tasks, timeout=remaining)
            if pending:
                for task in pending:
                    task.cancel()
                return False
        return True

    async def wait_for_tool_tasks_to_finish(self) -> None:
        """Wait without forgetting cancelled tools that have not really exited."""

        while self._tool_tasks:
            tasks = set(self._tool_tasks)
            await asyncio.wait(tasks)

    def register_response_request(
        self,
        call_id: str,
        *,
        prompt_type: ResponsePromptType,
        timeout: float,
        tool_call_id: str | None = None,
        tool: str | None = None,
    ) -> ResponseRequestRecord:
        """Register a prompt before publishing its SSE request event.

        Registration-before-publish closes the race where a fast client could
        answer before ``wait_for_response`` created its Future.
        """
        existing = self._response_requests.get(call_id)
        if existing is not None:
            # ``submit_response`` keeps backwards compatibility with callers
            # that submit before waiting by creating an ``unknown`` record.
            # Enrich that record when the actual prompt is registered.
            if existing.prompt_type == "unknown":
                existing.prompt_type = prompt_type
                existing.tool_call_id = tool_call_id
                existing.tool = tool
                if existing.expires_at is None:
                    existing.expires_at = time.monotonic() + timeout
                return existing
            if (
                existing.prompt_type != prompt_type
                or existing.tool_call_id != tool_call_id
                or existing.tool != tool
            ):
                raise ValueError(f"Response call_id collision: {call_id}")
            return existing

        record = ResponseRequestRecord(
            call_id=call_id,
            prompt_type=prompt_type,
            tool_call_id=tool_call_id,
            tool=tool,
            expires_at=time.monotonic() + timeout,
            future=asyncio.get_running_loop().create_future(),
        )
        self._response_requests[call_id] = record
        return record

    def get_response_request(self, call_id: str) -> ResponseRequestRecord | None:
        return self._response_requests.get(call_id)

    def resolve_response(
        self,
        call_id: str,
        response: Any,
        *,
        source: str,
        allow_unregistered: bool = False,
    ) -> ResponseSubmitResult:
        """Resolve a registered prompt with idempotent call-id semantics."""
        result = self.preview_response(
            call_id,
            response,
            allow_unregistered=allow_unregistered,
        )
        if result.status != "accepted":
            return result

        record = result.record
        assert record is not None
        return self._apply_response(record, response, source=source)

    def preview_response(
        self,
        call_id: str,
        response: Any,
        *,
        allow_unregistered: bool = False,
    ) -> ResponseSubmitResult:
        """Validate a submission without waking the waiting generation."""

        record = self._response_requests.get(call_id)
        if record is None:
            if not allow_unregistered:
                return ResponseSubmitResult("not_pending", None)
            record = ResponseRequestRecord(
                call_id=call_id,
                prompt_type="unknown",
                tool_call_id=None,
                tool=None,
                expires_at=None,
                future=asyncio.get_running_loop().create_future(),
            )
            self._response_requests[call_id] = record

        if record.state == "resolved":
            if record.response == response:
                return ResponseSubmitResult("already_resolved", record)
            return ResponseSubmitResult("conflict", record)

        if record.state == "expired":
            return ResponseSubmitResult("expired", record)

        if self.completed:
            return ResponseSubmitResult("not_pending", record)

        if record.expires_at is not None and time.monotonic() >= record.expires_at:
            record.state = "expired"
            # Do not cancel the Future here.  A waiter is normally already
            # blocked on it and will reach its own timeout immediately; direct
            # cancellation would surface CancelledError instead of TimeoutError.
            return ResponseSubmitResult("expired", record)

        return ResponseSubmitResult("accepted", record)

    def apply_durable_response(
        self,
        call_id: str,
        response: Any,
        *,
        source: str,
    ) -> ResponseSubmitResult:
        """Wake a pending prompt from an already committed durable decision.

        Expiry is intentionally not rechecked: a durable row proves the
        submission passed validation before it was committed.  This closes the
        crash/cancellation window between database commit and Future wake-up.
        """

        record = self._response_requests.get(call_id)
        if record is None:
            return ResponseSubmitResult("not_pending", None)
        if record.state == "resolved":
            if record.response == response:
                return ResponseSubmitResult("already_resolved", record)
            return ResponseSubmitResult("conflict", record)
        return self._apply_response(record, response, source=source)

    @staticmethod
    def _apply_response(
        record: ResponseRequestRecord,
        response: Any,
        *,
        source: str,
    ) -> ResponseSubmitResult:
        record.state = "resolved"
        record.response = response
        record.source = source
        if not record.future.done():
            record.future.set_result(response)
        return ResponseSubmitResult("accepted", record)

    async def wait_for_response(self, call_id: str, timeout: float = 300.0) -> Any:
        """Wait for user response to a specific call_id.

        Uses per-call_id Futures instead of a shared queue to avoid
        busy-loop polling when multiple calls are pending.
        """
        record = self._response_requests.get(call_id)
        if record is None:
            record = self.register_response_request(
                call_id,
                prompt_type="unknown",
                timeout=timeout,
            )

        if record.state == "resolved":
            return record.response
        if record.state == "expired":
            raise TimeoutError(f"No response received for call_id={call_id}")

        remaining = timeout
        if record.expires_at is not None:
            remaining = min(timeout, max(0.0, record.expires_at - time.monotonic()))
        try:
            return await asyncio.wait_for(asyncio.shield(record.future), timeout=remaining)
        except asyncio.TimeoutError:
            # POST /chat/respond holds this lock from validation through the
            # durable commit and Future wake-up.  If the nominal timeout lands
            # while that commit is in progress, let the accepted decision win
            # instead of cancelling its waiter in the commit/wake gap.
            async with self.response_resolution_lock:
                if record.state == "resolved":
                    return record.response
                record.state = "expired"
                if not record.future.done():
                    record.future.cancel()
            raise TimeoutError(f"No response received for call_id={call_id}")

    def submit_response(self, call_id: str, response: Any) -> None:
        """Backward-compatible internal response submission helper.

        The HTTP endpoint uses strict ``resolve_response`` instead so an
        unregistered call id is rejected rather than queued forever.
        """
        self.resolve_response(
            call_id,
            response,
            source="internal",
            allow_unregistered=True,
        )


class StreamManager:
    """Manages all active GenerationJobs.

    Thread-safe singleton for creating, looking up, and cleaning up jobs.
    """

    def __init__(self):
        from app.config import get_settings as _get_settings
        self._jobs: dict[str, GenerationJob] = {}
        # Runtime finalizers may need to outlive the client task or the
        # GenerationJob whose side effects they describe. Keep strong ownership
        # here so cancellation cannot orphan quiescence or durable-state work.
        self._reconciliation_tasks: set[asyncio.Task[Any]] = set()
        # Serializes durable request-ledger insertion with in-memory job
        # creation.  This prevents concurrent retries from both observing an
        # absent idempotency record before either job is registered.
        self.job_admission_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(_get_settings().max_concurrent_generations)
        self._post_checkpoint_validation_scheduler: Any | None = None

    def track_runtime_task(self, task: asyncio.Task[Any]) -> None:
        """Own a safety-critical finalizer until it settles or shutdown ends it."""

        self._reconciliation_tasks.add(task)

        def _finished(completed: asyncio.Task[Any]) -> None:
            self._reconciliation_tasks.discard(completed)
            if completed.cancelled():
                return
            try:
                error = completed.exception()
            except asyncio.CancelledError:
                return
            if error is not None:
                # Never interpolate the exception: database errors can include
                # private paths, statements, or bound values.
                logger.error(
                    "Durable-state reconciliation failed (%s)",
                    type(error).__name__,
                )

        task.add_done_callback(_finished)

    def track_reconciliation_task(self, task: asyncio.Task[Any]) -> None:
        """Backward-compatible name for durable-state reconciliation ownership."""

        self.track_runtime_task(task)

    async def wait_for_reconciliation_tasks(self, timeout: float) -> bool:
        """Wait for manager-owned finalizers, cancelling leftovers at deadline."""

        current = asyncio.current_task()
        tasks = {
            task
            for task in self._reconciliation_tasks
            if task is not current and not task.done()
        }
        if not tasks:
            return True
        _done, pending = await asyncio.wait(
            tasks,
            timeout=max(0.0, timeout),
        )
        if not pending:
            return True
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        return False

    def set_post_checkpoint_validation_scheduler(self, scheduler: Any | None) -> None:
        """Inject the app-owned scheduler used by newly admitted root jobs."""

        if scheduler is not None and not callable(
            getattr(scheduler, "run_pending", None)
        ):
            raise TypeError("post-checkpoint validation scheduler is invalid")
        self._post_checkpoint_validation_scheduler = scheduler

    def create_job(
        self,
        stream_id: str,
        session_id: str,
        *,
        invocation_source: InvocationSource = "unknown",
        invocation_source_id: str | None = None,
        goal_id: str | None = None,
        goal_run_id: str | None = None,
        goal_session_id: str | None = None,
        root_turn_id: str | None = None,
        turn_run_id: str | None = None,
        workspace_instance_id: str | None = None,
    ) -> GenerationJob:
        """Create a new generation job and auto-cleanup old completed ones."""
        active = self.active_job_for_session(session_id)
        if active is not None:
            raise SessionBusyError(session_id, active.stream_id)
        job = GenerationJob(
            stream_id=stream_id,
            session_id=session_id,
            invocation_source=invocation_source,
            invocation_source_id=invocation_source_id,
            goal_id=goal_id,
            goal_run_id=goal_run_id,
            goal_session_id=goal_session_id,
            root_turn_id=root_turn_id,
            turn_run_id=turn_run_id,
            workspace_instance_id=workspace_instance_id,
            post_checkpoint_validation_scheduler=(
                self._post_checkpoint_validation_scheduler
            ),
        )
        self._jobs[stream_id] = job
        # Proactively cleanup old completed jobs on each new creation
        self.cleanup_completed()
        return job

    def get_job(self, stream_id: str) -> GenerationJob | None:
        """Get a job by stream ID."""
        return self._jobs.get(stream_id)

    def active_job_for_session(self, session_id: str) -> GenerationJob | None:
        return next(
            (
                job
                for job in self._jobs.values()
                if job.session_id == session_id and not job.is_quiescent
            ),
            None,
        )

    def remove_job(self, stream_id: str) -> bool:
        """Remove only a truly quiescent job; never forget live side effects."""

        job = self._jobs.get(stream_id)
        if job is None:
            return False
        if not job.is_quiescent:
            # Admission failures may dispose a job before a worker was ever
            # attached. That state has no runner/tool side effects to forget.
            if job.task is not None or job._tool_tasks:
                return False
            job.abort()
            if not job.completed:
                job.complete()
        self._jobs.pop(stream_id, None)
        return True

    def active_jobs(self) -> list[dict[str, Any]]:
        """List all active (non-completed) jobs."""
        active: list[dict[str, Any]] = []
        for job in self._jobs.values():
            if job.is_quiescent:
                continue
            item: dict[str, Any] = {
                "stream_id": job.stream_id,
                "session_id": job.session_id,
                "needs_input": any(
                    request.state == "pending"
                    for request in job._response_requests.values()
                ),
            }
            # Preserve the established ordinary-job wire shape while exposing
            # immutable Goal identity only for Goal-owned streams.
            if job.goal_id is not None:
                item["goal_id"] = job.goal_id
                item["goal_run_id"] = job.goal_run_id
            active.append(item)
        return active

    def abort_session(self, session_id: str) -> int:
        """Abort all active jobs for a given session. Used when deleting a session."""
        count = 0
        for job in self._jobs.values():
            if job.session_id == session_id and not job.is_quiescent:
                job.abort()
                count += 1
        return count

    def abort_all(self) -> int:
        """Abort all active jobs. Used during graceful shutdown."""
        count = 0
        for job in self._jobs.values():
            if not job.is_quiescent:
                job.abort()
                count += 1
        return count

    async def abort_all_and_wait(self, *, timeout: float = 10.0) -> tuple[int, bool]:
        """Abort jobs and wait for workers plus durable finalizers to settle."""

        jobs = [job for job in self._jobs.values() if not job.is_quiescent]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)
        count, jobs_quiesced = await self._abort_jobs_and_wait(
            jobs,
            timeout=max(0.0, deadline - loop.time()),
        )
        reconciled = await self.wait_for_reconciliation_tasks(
            max(0.0, deadline - loop.time())
        )
        return count, jobs_quiesced and reconciled

    async def abort_session_and_wait(
        self,
        session_id: str,
        *,
        timeout: float = 10.0,
    ) -> tuple[int, bool]:
        jobs = [
            job
            for job in self._jobs.values()
            if job.session_id == session_id and not job.is_quiescent
        ]
        return await self._abort_jobs_and_wait(jobs, timeout=timeout)

    async def _abort_jobs_and_wait(
        self,
        jobs: list[GenerationJob],
        *,
        timeout: float,
    ) -> tuple[int, bool]:
        for job in jobs:
            job.abort()
        if not jobs:
            return 0, True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)
        tool_results = await asyncio.gather(
            *(
                job.wait_for_tool_tasks(max(0.0, deadline - loop.time()))
                for job in jobs
            ),
            return_exceptions=True,
        )
        tools_quiesced = all(result is True for result in tool_results)

        current = asyncio.current_task()
        worker_tasks = {
            job.task
            for job in jobs
            if job.task is not None
            and job.task is not current
            and not job.task.done()
        }
        workers_quiesced = True
        if worker_tasks:
            _done, pending = await asyncio.wait(
                worker_tasks,
                timeout=max(0.0, deadline - loop.time()),
            )
            if pending:
                workers_quiesced = False
                for task in pending:
                    task.cancel()
                # A coroutine is allowed to catch cancellation. Do not turn a
                # bounded shutdown/delete wait into an unbounded gather, and do
                # not remove its owning GenerationJob while it remains live.
                await asyncio.sleep(0)
        return len(jobs), tools_quiesced and workers_quiesced

    def cleanup_completed(self, keep_last: int = 50) -> int:
        """Remove old completed jobs, keeping the most recent ones."""
        completed = [sid for sid, job in self._jobs.items() if job.is_quiescent]
        to_remove = completed[:-keep_last] if len(completed) > keep_last else []
        for sid in to_remove:
            del self._jobs[sid]
        return len(to_remove)
