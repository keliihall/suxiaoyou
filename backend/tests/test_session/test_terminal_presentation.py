from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models.message import Message, Part
from app.models.session import Session
from app.models.session_goal import SessionGoal
from app.session.prompt import SessionPrompt
from app.streaming.events import STEP_FINISH, TEXT_DELTA
from app.streaming.manager import GenerationJob


@pytest.mark.asyncio
async def test_tool_only_goal_completion_gets_visible_text_and_terminal_step(
    session_factory,
) -> None:
    async with session_factory() as db:
        async with db.begin():
            db.add(Session(id="session", directory=".", title="Goal completion"))
            db.add(
                SessionGoal(
                    id="goal",
                    session_id="session",
                    objective="Produce the final artifact",
                    status="complete",
                    run_state="idle",
                    completion_summary="Final artifact verified and delivered.",
                    completion_evidence=[{"criterion": "artifact", "passed": True}],
                    time_completed=datetime.now(timezone.utc),
                )
            )
            db.add(
                Message(
                    id="assistant",
                    session_id="session",
                    data={"role": "assistant", "agent": "build"},
                )
            )
            db.add_all(
                [
                    Part(
                        id="tool",
                        message_id="assistant",
                        session_id="session",
                        data={
                            "type": "tool",
                            "tool": "update_goal",
                            "call_id": "complete-goal",
                            "state": {
                                "status": "completed",
                                "input": {"status": "complete"},
                                "output": "Goal completed",
                            },
                        },
                    ),
                    Part(
                        id="tool-finish",
                        message_id="assistant",
                        session_id="session",
                        data={
                            "type": "step-finish",
                            "reason": "tool_use",
                            "tokens": {"input": 12, "output": 3},
                            "cost": 0.0,
                        },
                    ),
                ]
            )

    job = GenerationJob(
        "stream",
        "session",
        invocation_source="goal",
        goal_id="goal",
        goal_run_id="run",
    )
    prompt = SessionPrompt.__new__(SessionPrompt)
    prompt.session_factory = session_factory
    prompt.job = job
    prompt.request = SimpleNamespace(language="en")
    prompt.assistant_msg_id = "assistant"
    prompt.finish_reason = "complete"
    prompt.total_cost = 0.0
    prompt._has_any_text = False

    await prompt._ensure_goal_completion_presentation()
    await prompt._ensure_terminal_step_finish()
    # Both repairs are idempotent across retries/re-entry.
    await prompt._ensure_goal_completion_presentation()
    await prompt._ensure_terminal_step_finish()

    async with session_factory() as db:
        parts = list(
            (
                await db.execute(
                    select(Part)
                    .where(Part.message_id == "assistant")
                    .order_by(Part.time_created.asc(), Part.id.asc())
                )
            ).scalars()
        )

    text_parts = [part.data for part in parts if part.data.get("type") == "text"]
    step_finishes = [
        part.data for part in parts if part.data.get("type") == "step-finish"
    ]
    assert text_parts == [
        {
            "type": "text",
            "text": "Final artifact verified and delivered.",
            "synthetic": True,
        }
    ]
    assert [part["reason"] for part in step_finishes] == ["tool_use", "stop"]
    assert [event.event for event in job.events].count(TEXT_DELTA) == 1
    assert [event.event for event in job.events].count(STEP_FINISH) == 1
    assert [event.event for event in job.events][-2:] == [TEXT_DELTA, STEP_FINISH]
    assert job.events[-2].data["text"] == "Final artifact verified and delivered."


@pytest.mark.asyncio
async def test_goal_completion_summary_is_appended_after_existing_reply(
    session_factory,
) -> None:
    async with session_factory() as db:
        async with db.begin():
            db.add(Session(id="session", directory=".", title="Goal completion"))
            db.add(
                SessionGoal(
                    id="goal",
                    session_id="session",
                    objective="Produce the final artifact",
                    status="complete",
                    run_state="idle",
                    completion_summary="All acceptance checks passed.",
                    completion_evidence=[{"criterion": "checks", "passed": True}],
                    time_completed=datetime.now(timezone.utc),
                )
            )
            db.add(
                Message(
                    id="assistant",
                    session_id="session",
                    data={"role": "assistant", "agent": "build"},
                )
            )
            db.add(
                Part(
                    id="prose",
                    message_id="assistant",
                    session_id="session",
                    data={"type": "text", "text": "The artifact is ready."},
                )
            )

    job = GenerationJob(
        "stream",
        "session",
        invocation_source="goal",
        goal_id="goal",
        goal_run_id="run",
    )
    prompt = SessionPrompt.__new__(SessionPrompt)
    prompt.session_factory = session_factory
    prompt.job = job
    prompt.request = SimpleNamespace(language="en")
    prompt.assistant_msg_id = "assistant"
    prompt.finish_reason = "complete"
    prompt._has_any_text = True

    await prompt._ensure_goal_completion_presentation()
    await prompt._ensure_goal_completion_presentation()

    async with session_factory() as db:
        parts = list(
            (
                await db.execute(
                    select(Part)
                    .where(Part.message_id == "assistant")
                    .order_by(Part.time_created.asc(), Part.id.asc())
                )
            ).scalars()
        )

    assert [
        part.data["text"]
        for part in parts
        if part.data.get("type") == "text"
    ] == ["The artifact is ready.", "All acceptance checks passed."]
    assert [event.event for event in job.events] == [TEXT_DELTA]
    assert job.events[0].data["text"] == "\n\nAll acceptance checks passed."
