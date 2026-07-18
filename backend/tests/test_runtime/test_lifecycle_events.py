from __future__ import annotations

from app.runtime.events import REDACTED, lifecycle_event_from_transport
from app.streaming.events import DONE, SSEEvent, TOOL_START
from app.streaming.manager import GenerationJob


def test_transport_event_is_versioned_and_redacts_nested_secrets() -> None:
    event = lifecycle_event_from_transport(
        sequence=7,
        transport_event=TOOL_START,
        data={
            "call_id": "call-1",
            "tool": "connector",
            "args": {
                "api_key": "top-secret",
                "endpoint": "https://alice:pwd@example.com/run?token=hidden&mode=safe",
                "headers": {"Authorization": "Bearer hidden", "X-Trace": "ok"},
            },
        },
        session_id="session-1",
        stream_id="stream-1",
        root_turn_id="turn-root",
        turn_run_id="turn-run",
        workspace_instance_id="workspace-1",
        invocation_source="desktop",
    )

    payload = event.to_dict()
    assert payload["event_id"] == "stream-1:7"
    assert payload["event_version"] == 1
    assert payload["event_type"] == "tool.started"
    assert payload["call_id"] == "call-1"
    assert payload["root_turn_id"] == "turn-root"
    assert payload["payload"]["args"]["api_key"] == REDACTED
    assert payload["payload"]["args"]["headers"]["Authorization"] == REDACTED
    assert "alice" not in payload["payload"]["args"]["endpoint"]
    assert "hidden" not in payload["payload"]["args"]["endpoint"]


def test_generation_job_publishes_transport_neutral_replay_stream() -> None:
    job = GenerationJob(
        "stream-1",
        "session-1",
        invocation_source="desktop",
        root_turn_id="root-1",
        workspace_instance_id="workspace-1",
    )
    job.publish(SSEEvent(TOOL_START, {"call_id": "call-1", "tool": "write"}))
    job.publish(SSEEvent(DONE, {"finish_reason": "stop"}))
    job.complete()

    queue = job.subscribe_lifecycle(last_sequence=1)
    completed = queue.get_nowait()
    sentinel = queue.get_nowait()

    assert completed is not None
    assert completed.sequence == 2
    assert completed.event_type == "turn.completed"
    assert completed.root_turn_id == "root-1"
    assert completed.workspace_instance_id == "workspace-1"
    assert sentinel is None


def test_child_job_inherits_root_turn_but_keeps_own_turn_run() -> None:
    parent = GenerationJob(
        "parent-stream",
        "parent-session",
        root_turn_id="root-turn",
        workspace_instance_id="workspace-1",
    )
    child = GenerationJob("child-stream", "child-session")

    child.inherit_runtime_context(parent)

    assert child.root_turn_id == "root-turn"
    assert child.turn_run_id == "child-stream"
    assert child.parent_turn_id == parent.turn_run_id
    assert child.workspace_instance_id == "workspace-1"


def test_root_stream_can_advance_to_a_queued_turn_but_child_cannot() -> None:
    parent = GenerationJob("stream", "session")
    parent.begin_root_turn("queued-input")

    assert parent.root_turn_id == "queued-input"
    assert parent.turn_run_id == "queued-input"

    child = GenerationJob("child", "child-session")
    child.inherit_runtime_context(parent)
    try:
        child.begin_root_turn("forged-root")
    except ValueError as exc:
        assert "child" in str(exc).lower()
    else:  # pragma: no cover
        raise AssertionError("child job unexpectedly changed its root turn")


def test_direct_lifecycle_fact_is_sanitized_and_ordered_with_sse() -> None:
    job = GenerationJob("stream", "session", invocation_source="desktop")
    job.publish(SSEEvent("before", {}))
    event = job.publish_lifecycle(
        "checkpoint.prepared",
        {"checkpoint_id": "checkpoint", "api_key": "hidden"},
        checkpoint_id="checkpoint",
    )
    job.publish(SSEEvent("after", {}))

    assert [item.sequence for item in job.lifecycle_events] == [1, 2, 3]
    assert event.event_type == "checkpoint.prepared"
    assert event.checkpoint_id == "checkpoint"
    assert event.payload["api_key"] == REDACTED


def test_child_events_share_monotonic_root_lifecycle_sequence() -> None:
    parent = GenerationJob("parent-stream", "parent-session")
    child = GenerationJob("child-stream", "child-session")
    child.inherit_runtime_context(parent)
    queue = parent.subscribe_lifecycle()

    parent.publish(SSEEvent("root-start", {}))
    child.publish(SSEEvent("child-progress", {}))
    child.complete()
    parent.publish(SSEEvent(DONE, {}))
    parent.complete()

    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    events = [item for item in items if item is not None]
    assert [event.sequence for event in events] == [1, 2, 3]
    assert [event.stream_id for event in events] == [
        "parent-stream",
        "child-stream",
        "parent-stream",
    ]
    assert items[-1] is None


def test_generation_job_workspace_binding_is_immutable() -> None:
    job = GenerationJob("stream", "session")
    job.bind_workspace_instance("workspace-1")
    job.bind_workspace_instance("workspace-1")

    assert job.workspace_instance_id == "workspace-1"

    try:
        job.bind_workspace_instance("workspace-2")
    except ValueError as exc:
        assert "cannot change" in str(exc)
    else:  # pragma: no cover - protects the immutable identity contract
        raise AssertionError("workspace instance mutation unexpectedly succeeded")


def test_unknown_transport_events_remain_forward_compatible() -> None:
    event = lifecycle_event_from_transport(
        sequence=1,
        transport_event="future_runtime:event",
        data={"value": 1},
        session_id="session",
        stream_id="stream",
        invocation_source="unknown",
    )

    assert event.event_type == "transport.future.runtime.event"
    assert event.payload == {"value": 1}


def test_unknown_lifecycle_version_fails_closed() -> None:
    from app.runtime.events import LifecycleEventV1

    try:
        LifecycleEventV1(
            sequence=1,
            event_type="turn.started",
            session_id="session",
            stream_id="stream",
            invocation_source="desktop",
            event_version=2,
        )
    except ValueError as exc:
        assert "Unsupported lifecycle event version" in str(exc)
    else:  # pragma: no cover - protects the version boundary
        raise AssertionError("unknown lifecycle version unexpectedly succeeded")


def test_completed_lifecycle_replay_reports_trim_and_keeps_sentinel() -> None:
    job = GenerationJob("stream", "session")
    for index in range(job._MAX_EVENT_BUFFER + 5):
        job.publish(SSEEvent("noise", {"index": index}))
    job.complete()

    queue = job.subscribe_lifecycle(last_sequence=1)
    first = queue.get_nowait()
    assert first is not None
    assert first.event_type == "runtime.desync"

    items = [first]
    while not queue.empty():
        items.append(queue.get_nowait())
    assert items[-1] is None
    assert len(items) == queue.maxsize
