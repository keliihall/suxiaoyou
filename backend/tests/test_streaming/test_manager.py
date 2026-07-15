"""Streaming manager tests — GenerationJob lifecycle, interactive mode, permissions."""

import asyncio

import pytest

from app.streaming.events import DESYNC, SSEEvent, TEXT_DELTA, DONE, PERMISSION_REQUEST
from app.streaming.manager import GenerationJob, StreamManager


class TestGenerationJobInteractive:
    """Tests for the interactive permission flow."""

    def test_default_not_interactive(self):
        job = GenerationJob("s1", "sess1")
        assert job.interactive is False

    def test_set_interactive(self):
        job = GenerationJob("s1", "sess1")
        job.interactive = True
        assert job.interactive is True

    def test_default_depth_zero(self):
        job = GenerationJob("s1", "sess1")
        assert job._depth == 0

    @pytest.mark.asyncio
    async def test_wait_for_response(self):
        """Test that wait_for_response receives a submitted response."""
        job = GenerationJob("s1", "sess1")

        async def submit_later():
            await asyncio.sleep(0.05)
            job.submit_response("call-1", "allow")

        asyncio.create_task(submit_later())
        response = await job.wait_for_response("call-1", timeout=5.0)
        assert response == "allow"

    @pytest.mark.asyncio
    async def test_wait_for_response_timeout(self):
        """Test that wait_for_response raises on timeout."""
        job = GenerationJob("s1", "sess1")
        with pytest.raises(TimeoutError):
            await job.wait_for_response("call-1", timeout=0.05)

    @pytest.mark.asyncio
    async def test_submit_before_wait(self):
        """Response submitted before wait_for_response is called."""
        job = GenerationJob("s1", "sess1")
        job.submit_response("call-1", "deny")
        response = await job.wait_for_response("call-1", timeout=1.0)
        assert response == "deny"

    @pytest.mark.asyncio
    async def test_registered_response_is_idempotent_and_rejects_conflicts(self):
        job = GenerationJob("s1", "sess1")
        job.register_response_request(
            "permission-1",
            prompt_type="permission",
            timeout=5.0,
            tool_call_id="tool-1",
            tool="bash",
        )

        accepted = job.resolve_response(
            "permission-1",
            {"allowed": True},
            source="local",
        )
        assert accepted.status == "accepted"
        assert await job.wait_for_response("permission-1") == {"allowed": True}

        duplicate = job.resolve_response(
            "permission-1",
            {"allowed": True},
            source="remote",
        )
        assert duplicate.status == "already_resolved"
        assert duplicate.record is not None
        assert duplicate.record.source == "local"

        conflict = job.resolve_response(
            "permission-1",
            {"allowed": False},
            source="remote",
        )
        assert conflict.status == "conflict"

    @pytest.mark.asyncio
    async def test_response_status_distinguishes_unknown_and_expired_calls(self):
        job = GenerationJob("s1", "sess1")
        assert job.resolve_response("missing", "answer", source="local").status == "not_pending"

        job.register_response_request(
            "expired",
            prompt_type="question",
            timeout=0.0,
            tool_call_id="expired",
            tool="question",
        )
        assert job.resolve_response("expired", "answer", source="local").status == "expired"

        completed = GenerationJob("completed", "sess1")
        completed.register_response_request(
            "stale",
            prompt_type="question",
            timeout=5.0,
            tool_call_id="stale",
            tool="question",
        )
        completed.complete()
        assert completed.resolve_response("stale", "answer", source="local").status == "not_pending"


class TestStreamManagerCleanup:
    def test_cleanup_completed(self):
        sm = StreamManager()
        # Insert jobs directly to avoid auto-cleanup in create_job
        for i in range(60):
            job = GenerationJob(stream_id=f"s{i}", session_id=f"sess{i}")
            sm._jobs[f"s{i}"] = job
            if i < 55:
                job.complete()

        removed = sm.cleanup_completed(keep_last=10)
        assert removed == 45  # 55 completed, keep 10

    def test_active_jobs_excludes_completed(self):
        sm = StreamManager()
        j1 = sm.create_job("s1", "sess1")
        j2 = sm.create_job("s2", "sess2")
        j1.complete()

        active = sm.active_jobs()
        assert len(active) == 1
        assert active[0]["stream_id"] == "s2"


@pytest.mark.asyncio
async def test_complete_delivers_terminal_sentinel_to_full_subscriber() -> None:
    job = GenerationJob("stream", "session")
    queue = job.subscribe()
    for index in range(queue.maxsize):
        job.publish(SSEEvent(TEXT_DELTA, {"text": str(index)}))
    assert queue.full()

    job.complete()

    desync = queue.get_nowait()
    terminal = queue.get_nowait()
    assert desync is not None and desync.event == DESYNC
    assert terminal is None
def test_generation_job_source_is_server_validated_and_defaults_fail_closed() -> None:
    unknown = GenerationJob("unknown-stream", "unknown-session")
    assert unknown.invocation_source == "unknown"
    assert unknown.invocation_source_id is None

    scheduler = GenerationJob(
        "scheduler-stream",
        "scheduler-session",
        invocation_source="scheduler",
        invocation_source_id="  task-1  ",
    )
    assert scheduler.invocation_source == "scheduler"
    assert scheduler.invocation_source_id == "task-1"
    with pytest.raises(AttributeError):
        scheduler.invocation_source = "desktop"  # type: ignore[misc]

    with pytest.raises(ValueError, match="Unknown invocation source"):
        GenerationJob(
            "spoofed-stream",
            "spoofed-session",
            invocation_source="desktop-from-request",  # type: ignore[arg-type]
        )


def test_stream_manager_preserves_explicit_root_source() -> None:
    manager = StreamManager()
    job = manager.create_job(
        "source-stream",
        "source-session",
        invocation_source="goal",
        invocation_source_id="goal-1",
        goal_id="goal-1",
        goal_run_id="run-1",
    )
    assert job.invocation_source == "goal"
    assert job.invocation_source_id == "goal-1"
    assert job.goal_id == "goal-1"
    assert job.goal_run_id == "run-1"

    assert manager.active_jobs() == [
        {
            "stream_id": "source-stream",
            "session_id": "source-session",
            "goal_id": "goal-1",
            "goal_run_id": "run-1",
            "needs_input": False,
        }
    ]

    job.set_goal_run_id("run-2")
    assert job.goal_run_id == "run-2"
