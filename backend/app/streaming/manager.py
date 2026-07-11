"""GenerationJob and StreamManager for resumable SSE streaming."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

from app.streaming.events import AGENT_ERROR, DESYNC, DONE, SSEEvent

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

    def __init__(self, stream_id: str, session_id: str):
        self.stream_id = stream_id
        self.session_id = session_id
        self.events: list[SSEEvent] = []
        self.subscribers: list[asyncio.Queue[SSEEvent | None]] = []
        self.abort_event = asyncio.Event()
        # Admission and the final durable-queue check share this lock.  Without
        # it, an input can commit immediately after the generation observes an
        # empty queue but immediately before the stream completes, leaving a
        # permanently queued instruction with no worker to consume it.
        self.session_input_lock = asyncio.Lock()
        self._accepting_session_inputs = True
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

        # Interactive mode: True when a client is connected via SSE.
        # When False (tests, headless), permission "ask" auto-approves.
        self.interactive: bool = False

        # Nesting depth for subtask recursion guard
        self._depth: int = 0

        # Artifact content cache: identifier → {content, type, title, language}
        # Populated from message history at generation start, updated by artifact tool
        self.artifact_cache: dict[str, dict[str, Any]] = {}

    @property
    def completed(self) -> bool:
        return self._completed

    @property
    def accepting_session_inputs(self) -> bool:
        return self._accepting_session_inputs and not self._completed

    def close_session_input_admission(self) -> None:
        self._accepting_session_inputs = False

    def publish(self, event: SSEEvent) -> None:
        """Publish an event to all subscribers and buffer for replay."""
        self._event_counter += 1
        event.id = self._event_counter
        self.events.append(event)

        # Cap replay buffer to prevent unbounded memory growth
        if len(self.events) > self._MAX_EVENT_BUFFER:
            self.events = self.events[-self._MAX_EVENT_BUFFER:]

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

    def abort(self) -> None:
        """Signal abort to the generation loop."""
        self.abort_event.set()

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
        # Serializes durable request-ledger insertion with in-memory job
        # creation.  This prevents concurrent retries from both observing an
        # absent idempotency record before either job is registered.
        self.job_admission_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(_get_settings().max_concurrent_generations)

    def create_job(self, stream_id: str, session_id: str) -> GenerationJob:
        """Create a new generation job and auto-cleanup old completed ones."""
        active = self.active_job_for_session(session_id)
        if active is not None:
            raise SessionBusyError(session_id, active.stream_id)
        job = GenerationJob(stream_id=stream_id, session_id=session_id)
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
                if job.session_id == session_id and not job.completed
            ),
            None,
        )

    def remove_job(self, stream_id: str) -> None:
        """Remove a completed job."""
        self._jobs.pop(stream_id, None)

    def active_jobs(self) -> list[dict[str, Any]]:
        """List all active (non-completed) jobs."""
        return [
            {
                "stream_id": j.stream_id,
                "session_id": j.session_id,
                "needs_input": any(
                    request.state == "pending"
                    for request in j._response_requests.values()
                ),
            }
            for j in self._jobs.values()
            if not j.completed
        ]

    def abort_session(self, session_id: str) -> int:
        """Abort all active jobs for a given session. Used when deleting a session."""
        count = 0
        for job in self._jobs.values():
            if job.session_id == session_id and not job.completed:
                job.abort()
                count += 1
        return count

    def abort_all(self) -> int:
        """Abort all active jobs. Used during graceful shutdown."""
        count = 0
        for job in self._jobs.values():
            if not job.completed:
                job.abort()
                count += 1
        return count

    def cleanup_completed(self, keep_last: int = 50) -> int:
        """Remove old completed jobs, keeping the most recent ones."""
        completed = [
            sid for sid, j in self._jobs.items() if j.completed
        ]
        to_remove = completed[:-keep_last] if len(completed) > keep_last else []
        for sid in to_remove:
            del self._jobs[sid]
        return len(to_remove)
