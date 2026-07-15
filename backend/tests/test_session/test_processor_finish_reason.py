"""Finish-reason contract tests for the session processor."""

from types import SimpleNamespace

import pytest

from app.session import processor as processor_module
from app.session.processor import SessionProcessor, _normalize_step_finish_reason


def test_normalize_step_finish_reason_maps_tool_calls_to_tool_use() -> None:
    assert _normalize_step_finish_reason("tool_calls") == "tool_use"


def test_normalize_step_finish_reason_preserves_declared_values() -> None:
    assert _normalize_step_finish_reason("stop") == "stop"
    assert _normalize_step_finish_reason("tool_use") == "tool_use"
    assert _normalize_step_finish_reason("length") == "length"
    assert _normalize_step_finish_reason("error") == "error"


def test_normalize_step_finish_reason_rejects_empty_contract_hole() -> None:
    assert _normalize_step_finish_reason("empty") == "error"
    assert _normalize_step_finish_reason(None) == "error"


@pytest.mark.asyncio
async def test_step_finish_is_committed_before_its_sse_event(monkeypatch) -> None:
    order: list[str] = []

    class Transaction:
        async def __aenter__(self):
            order.append("begin")

        async def __aexit__(self, *_args):
            order.append("commit")

    class Database:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def begin(self):
            return Transaction()

    async def create_part(*_args, **_kwargs):
        order.append("part")

    monkeypatch.setattr(processor_module, "create_part", create_part)
    job = SimpleNamespace(
        session_id="session",
        goal_run_id=None,
        publish=lambda _event: order.append("publish"),
    )
    prompt = SimpleNamespace(
        job=job,
        session_factory=lambda: Database(),
        total_cost=0.0,
    )
    processor = SessionProcessor(prompt, [], "assistant")
    processor.finish_reason = "stop"

    await processor._persist_step_finish()

    assert order == ["begin", "part", "commit", "publish"]
