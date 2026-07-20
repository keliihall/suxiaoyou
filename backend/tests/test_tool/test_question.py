from __future__ import annotations

import pytest

from app.agent.agent import BUILTIN_AGENTS
from app.tool.builtin.question import QuestionTool
from app.tool.context import ToolContext


def _context(events: list[tuple[str, dict]]) -> ToolContext:
    context = ToolContext(
        session_id="question-session",
        message_id="question-message",
        agent=BUILTIN_AGENTS["build"],
        call_id="question-call",
    )
    context._publish_fn = lambda event, payload: events.append((event, payload))
    return context


@pytest.mark.asyncio
async def test_malformed_json_arguments_never_open_an_empty_question() -> None:
    events: list[tuple[str, dict]] = []

    result = await QuestionTool()(
        {"_raw": '{"questions":[{"question":""unescaped""}]}'},
        _context(events),
    )

    assert not result.success
    assert "valid JSON" in (result.error or "")
    assert events == []


@pytest.mark.asyncio
async def test_empty_question_never_registers_or_publishes_a_prompt() -> None:
    events: list[tuple[str, dict]] = []

    result = await QuestionTool().execute({}, _context(events))

    assert not result.success
    assert result.metadata["code"] == "invalid_question_payload"
    assert events == []


@pytest.mark.asyncio
async def test_valid_question_is_trimmed_before_it_is_published() -> None:
    events: list[tuple[str, dict]] = []

    result = await QuestionTool().execute(
        {"question": "  请选择海报风格？  ", "options": ["商务", "科技"]},
        _context(events),
    )

    assert result.success
    assert events == [
        (
            "question",
            {
                "call_id": "question-call",
                "session_id": "question-session",
                "question": "请选择海报风格？",
                "options": ["商务", "科技"],
                "arguments": {
                    "question": "请选择海报风格？",
                    "options": ["商务", "科技"],
                },
            },
        )
    ]
