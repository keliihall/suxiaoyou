from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.goal_run import GoalRun
from app.models.goal_usage_record import GoalUsageRecord
from app.models.session import Session
from app.models.session_goal import SessionGoal
from app.schemas.chat import PromptRequest
from app.session.prompt import SessionPrompt
from app.streaming.manager import GenerationJob


@pytest.mark.asyncio
async def test_provider_usage_is_durable_before_tools_and_shared_once(
    session_factory,
) -> None:
    async with session_factory() as db:
        async with db.begin():
            db.add(Session(id="usage-session", directory=".", title="Usage"))
            db.add(
                SessionGoal(
                    id="usage-goal",
                    session_id="usage-session",
                    objective="Account every provider call",
                    status="active",
                    run_state="running",
                    revision=2,
                    last_run_id="usage-run",
                )
            )
            db.add(
                GoalRun(
                    id="usage-run",
                    goal_id="usage-goal",
                    ordinal=1,
                    goal_revision=2,
                    idempotency_key="runtime-usage-run",
                    trigger="initial",
                    status="running",
                )
            )

    job = GenerationJob(
        "usage-stream",
        "usage-session",
        invocation_source="goal",
        goal_id="usage-goal",
        goal_run_id="usage-run",
    )
    prompt = SessionPrompt(
        job,
        PromptRequest(session_id="usage-session", text="continue"),
        session_factory=session_factory,
        provider_registry=object(),  # type: ignore[arg-type]
        agent_registry=object(),  # type: ignore[arg-type]
        tool_registry=object(),  # type: ignore[arg-type]
    )
    prompt.assistant_msg_id = "usage-assistant-message"

    await prompt._record_goal_step_usage_before_tools(
        {
            "input": 7,
            "output": 3,
            "reasoning": 2,
            "cache_read": 1,
        },
        0.25,
    )
    # A duplicate callback in the same live step must not inflate either the
    # durable ledger or the shared in-memory budget window.
    await prompt._record_goal_step_usage_before_tools(
        {
            "input": 7,
            "output": 3,
            "reasoning": 2,
            "cache_read": 1,
        },
        0.25,
    )

    async with session_factory() as db:
        records = list((await db.execute(select(GoalUsageRecord))).scalars())
    assert len(records) == 1
    assert records[0].source_key == "provider:usage-assistant-message"
    assert (records[0].tokens_used, records[0].cost_used_microusd) == (
        13,
        250_000,
    )
    assert job.goal_run_usage == (13, 250_000)

    child = GenerationJob("usage-child", "usage-child-session")
    child.inherit_goal_context(job)
    assert child.goal_run_usage == (13, 250_000)
