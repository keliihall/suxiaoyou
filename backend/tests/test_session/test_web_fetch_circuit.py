"""Focused coverage for response-scoped tool failure circuits."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.schemas.agent import PermissionRule, Ruleset
from app.session import processor as processor_module
from app.session.loop_detection import (
    WEB_FETCH_CIRCUIT_OPEN_MSG,
    WEB_FETCH_NON_PUBLIC_ERROR,
    WEB_SEARCH_LIMIT_MSG,
    LoopCheckResult,
    LoopDetector,
    TOOL_FAILURE_LIMIT,
    loop_detector,
    web_fetch_circuit_scope,
)
from app.session.processor import SessionProcessor
from app.streaming.events import TOOL_ERROR, TOOL_START
from app.streaming.manager import GenerationJob
from app.tool.base import ToolResult


def test_repeated_tool_failures_open_response_scoped_circuit() -> None:
    detector = LoopDetector(warn_threshold=10, hard_limit=20)
    scope = "session:stream"

    for index in range(TOOL_FAILURE_LIMIT):
        detector.record_tool_result(
            scope,
            "office",
            success=False,
            error=f"failure {index}",
        )

    assert detector.is_tool_failure_circuit_open(scope, "OFFICE")
    assert detector.blocked_tools(scope) == {"office"}
    assert not detector.is_tool_failure_circuit_open("session:other", "office")


def test_tool_failure_circuit_resets_after_success_and_session_reset() -> None:
    detector = LoopDetector(warn_threshold=10, hard_limit=20)
    scope = "session:stream"

    for _ in range(TOOL_FAILURE_LIMIT):
        detector.record_tool_result(scope, "bash", success=False, error="failed")
    detector.record_tool_result(scope, "bash", success=True)
    assert not detector.is_tool_failure_circuit_open(scope, "bash")

    for _ in range(TOOL_FAILURE_LIMIT):
        detector.record_tool_result(scope, "office", success=False, error="failed")
    detector.reset("session")
    assert detector.blocked_tools(scope) == set()


def _policy_error() -> str:
    return f"Web fetch blocked: {WEB_FETCH_NON_PUBLIC_ERROR}"


def test_web_fetch_circuit_opens_after_three_consecutive_policy_blocks() -> None:
    detector = LoopDetector(warn_threshold=10, hard_limit=20)
    scope = web_fetch_circuit_scope("session-a", "stream-a")

    for _ in range(2):
        detector.record_tool_result(
            scope,
            "web_fetch",
            success=False,
            error=_policy_error(),
        )
        assert detector.is_web_fetch_circuit_open(scope) is False

    detector.record_tool_result(
        scope,
        "web_fetch",
        success=False,
        error=_policy_error(),
    )

    assert detector.is_web_fetch_circuit_open(scope) is True
    assert detector.is_web_fetch_circuit_open(
        web_fetch_circuit_scope("another-session", "stream-a")
    ) is False


def test_web_fetch_success_and_reset_clear_policy_circuit_state() -> None:
    detector = LoopDetector(warn_threshold=10, hard_limit=20)
    scope = web_fetch_circuit_scope("session-a", "stream-a")

    for _ in range(2):
        detector.record_tool_result(
            scope,
            "web_fetch",
            success=False,
            error=_policy_error(),
        )
    detector.record_tool_result(
        scope,
        "web_fetch",
        success=True,
    )
    for _ in range(2):
        detector.record_tool_result(
            scope,
            "web_fetch",
            success=False,
            error=_policy_error(),
        )
    assert detector.is_web_fetch_circuit_open(scope) is False

    detector.record_tool_result(
        scope,
        "web_fetch",
        success=False,
        error=_policy_error(),
    )
    assert detector.is_web_fetch_circuit_open(scope) is True

    detector.reset("session-a")
    assert detector.is_web_fetch_circuit_open(scope) is False


def test_other_web_fetch_result_breaks_consecutive_policy_failures() -> None:
    detector = LoopDetector(warn_threshold=10, hard_limit=20)
    scope = web_fetch_circuit_scope("session-a", "stream-a")

    for _ in range(2):
        detector.record_tool_result(
            scope,
            "web_fetch",
            success=False,
            error=_policy_error(),
        )
    detector.record_tool_result(
        scope,
        "web_fetch",
        success=False,
        error="HTTP 404: https://example.com/missing",
    )
    for _ in range(2):
        detector.record_tool_result(
            scope,
            "web_fetch",
            success=False,
            error=_policy_error(),
        )

    assert detector.is_web_fetch_circuit_open(scope) is False


def test_new_stream_in_same_session_starts_with_closed_circuit() -> None:
    detector = LoopDetector(warn_threshold=10, hard_limit=20)
    first_scope = web_fetch_circuit_scope("session-a", "stream-a")
    next_scope = web_fetch_circuit_scope("session-a", "stream-b")

    for _ in range(3):
        detector.record_tool_result(
            first_scope,
            "web_fetch",
            success=False,
            error=_policy_error(),
        )

    assert detector.is_web_fetch_circuit_open(first_scope) is True
    assert detector.is_web_fetch_circuit_open(next_scope) is False


def test_custom_web_search_allows_five_then_blocks_sixth_per_stream() -> None:
    detector = LoopDetector(warn_threshold=10, hard_limit=20)
    first_scope = web_fetch_circuit_scope("session-a", "stream-a")
    next_scope = web_fetch_circuit_scope("session-a", "stream-b")

    # The limiter deliberately does not hash query arguments: five different
    # queries consume the same response-wide allowance.
    assert [
        detector.admit_custom_web_search(first_scope)
        for _query in ("one", "two", "three", "four", "five")
    ] == [True, True, True, True, True]
    assert detector.admit_custom_web_search(first_scope) is False
    assert detector.admit_custom_web_search(next_scope) is True


def test_web_search_failure_circuit_opens_after_one_failed_retry() -> None:
    detector = LoopDetector(warn_threshold=10, hard_limit=20)
    scope = web_fetch_circuit_scope("session-a", "stream-a")

    detector.record_tool_result(scope, "web_search", success=False)
    assert detector.is_tool_failure_circuit_open(scope, "web_search") is False

    detector.record_tool_result(scope, "web_search", success=False)
    assert detector.is_tool_failure_circuit_open(scope, "web_search") is True
    assert detector.blocked_tools(scope) == {"web_search"}


@pytest.mark.asyncio
async def test_processor_records_completed_web_fetch_result(
    session_factory,
    monkeypatch,
) -> None:
    session_id = "web-fetch-result-session"
    job = GenerationJob("web-fetch-result-stream", session_id)
    prompt = SimpleNamespace(job=job, session_factory=session_factory)
    processor = SessionProcessor(prompt, [], "assistant-message")
    processor._apply_tool_side_effects = AsyncMock()  # type: ignore[method-assign]
    processor._build_tool_persist_output = AsyncMock(  # type: ignore[method-assign]
        return_value=_policy_error(),
    )
    monkeypatch.setattr(processor_module, "_audit_tool_event", AsyncMock())
    monkeypatch.setattr(processor_module, "update_part_data", AsyncMock())
    record_result = MagicMock()
    monkeypatch.setattr(loop_detector, "record_tool_result", record_result)

    await processor._finalize_one_tool_result(
        {
            "tool_part_id": "tool-part",
            "loop_result": LoopCheckResult(action="allow"),
            "tool": SimpleNamespace(id="web_fetch"),
            "tool_args": {"url": "https://blocked.example/article"},
            "call_id": "fetch-call",
        },
        SimpleNamespace(
            timed_out=False,
            error=None,
            result=ToolResult(error=_policy_error()),
        ),
    )

    record_result.assert_called_once_with(
        web_fetch_circuit_scope(session_id, job.stream_id),
        "web_fetch",
        success=False,
        error=_policy_error(),
    )


@pytest.mark.asyncio
async def test_processor_skips_open_circuit_without_hard_stopping_answer(
    monkeypatch,
) -> None:
    session_id = "web-fetch-circuit-session"
    loop_detector.reset(session_id)
    job = GenerationJob("web-fetch-circuit-stream", session_id)
    scope = web_fetch_circuit_scope(session_id, job.stream_id)
    for _ in range(3):
        loop_detector.record_tool_result(
            scope,
            "web_fetch",
            success=False,
            error=_policy_error(),
        )

    class Registry:
        def get(self, _name: str):
            raise AssertionError("an open circuit must not resolve or execute web_fetch")

    prompt = SimpleNamespace(
        job=job,
        session_factory=object(),
        tool_registry=Registry(),
    )
    processor = SessionProcessor(prompt, [], "assistant-message")
    processor._init_step_state()
    processor._has_tool_calls = True  # Mirrors the streaming chunk handler.
    persist_error = AsyncMock()
    monkeypatch.setattr(processor_module, "_persist_tool_error", persist_error)

    try:
        await processor._handle_tool_call_chunk(SimpleNamespace(data={
            "id": "blocked-fetch",
            "name": "web_fetch",
            "arguments": {"url": "https://another-blocked.example/article"},
        }))

        assert processor._streaming_executor.has_submissions is False
        assert processor._exec_blocked is False
        assert await processor._dispatch_tool_calls() is None
        assert any(
            event.event == TOOL_ERROR
            and event.data.get("error") == WEB_FETCH_CIRCUIT_OPEN_MSG
            for event in job.events
        )
        assert "web_search result summaries" in WEB_FETCH_CIRCUIT_OPEN_MSG
        persist_error.assert_awaited_once()
    finally:
        loop_detector.reset(session_id)


@pytest.mark.asyncio
async def test_processor_allows_five_different_searches_and_skips_sixth(
    session_factory,
    monkeypatch,
) -> None:
    session_id = "custom-search-limit-session"
    job = GenerationJob("custom-search-limit-stream", session_id)

    class SearchTool:
        id = "web_search"
        requires_approval = False
        is_concurrency_safe = False

    tool = SearchTool()
    prompt = SimpleNamespace(
        job=job,
        session_factory=session_factory,
        tool_registry=SimpleNamespace(
            get=lambda name: tool if name == "web_search" else None,
        ),
        merged_permissions=Ruleset(rules=[
            PermissionRule(action="allow", permission="*"),
        ]),
        request=SimpleNamespace(
            language="en",
            _goal_permission_baseline=None,
        ),
        agent=SimpleNamespace(),
        workspace=None,
        index_manager=None,
        discovered_tools=set(),
        attachment_paths=set(),
        provider_registry=SimpleNamespace(),
        agent_registry=SimpleNamespace(),
        model_id="test-model",
    )
    processor = SessionProcessor(prompt, [], "assistant-message")
    processor._init_step_state()
    persist_error = AsyncMock()
    monkeypatch.setattr(processor_module, "_persist_tool_error", persist_error)
    monkeypatch.setattr(processor_module, "create_part", AsyncMock())
    monkeypatch.setattr(processor_module, "_audit_tool_event", AsyncMock())
    monkeypatch.setattr(
        processor_module,
        "denied_tool_capabilities",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        processor_module,
        "tool_requires_durable_audit",
        lambda _tool: False,
    )

    try:
        for index, query in enumerate(
            ("one", "two", "three", "four", "five", "six"),
            start=1,
        ):
            await processor._handle_tool_call_chunk(SimpleNamespace(data={
                "id": f"search-{index}",
                "name": "web_search",
                "arguments": {"query": query},
            }))

        assert processor._exec_index == 5
        assert processor._streaming_executor.has_submissions is True
        assert processor._exec_blocked is False
        assert sum(event.event == TOOL_START for event in job.events) == 5
        assert any(
            event.event == TOOL_ERROR
            and event.data.get("error") == WEB_SEARCH_LIMIT_MSG
            for event in job.events
        )
        persist_error.assert_awaited_once()
    finally:
        loop_detector.reset(session_id)
