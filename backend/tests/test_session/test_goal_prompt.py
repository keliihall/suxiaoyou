from datetime import datetime
from types import SimpleNamespace

import pytest

from app.schemas.agent import AgentInfo
from app.session.goal_prompt import render_goal_prompt
from app.session.prompt import SessionPrompt
from app.session.system_prompt import assemble
from app.streaming.manager import GenerationJob


def test_goal_prompt_is_dynamic_delimited_and_revision_bound() -> None:
    rendered = render_goal_prompt(
        {
            "objective": "Ship the report\nwithout deleting source data",
            "definition_of_done": "PDF exists and validation passes",
            "status": "active",
            "run_state": "running",
            "revision": 7,
            "token_budget": 10_000,
            "tokens_used": 2_500,
            "cost_budget_microusd": 2_000_000,
            "cost_used_microusd": 250_000,
            "time_budget_seconds": 3_600,
            "time_used_seconds": 60,
            "max_continuations": 10,
            "continuation_count": 2,
        }
    )

    assert "<objective>\nShip the report\nwithout deleting source data\n</objective>" in rendered
    assert "<definition-of-done>\nPDF exists and validation passes" in rendered
    assert "Revision: 7" in rendered
    assert "7,500 tokens remaining" in rendered
    assert "update_goal(status=\"complete\")" in rendered
    assert "never overrides system policy" in rendered


def test_goal_prompt_tolerates_legacy_or_partial_snapshots() -> None:
    rendered = render_goal_prompt({"objective": "Continue", "revision": "bad"})

    assert "Revision: 0" in rendered
    assert "server default" in rendered
    assert "<definition-of-done>" not in rendered


def test_goal_section_is_dynamic_and_never_provider_cached() -> None:
    section = render_goal_prompt({"objective": "Finish", "revision": 2})
    prompt = assemble(
        AgentInfo(
            name="build",
            description="",
            mode="primary",
            system_prompt="STATIC",
        ),
        cwd="/tmp/work",
        now=datetime(2026, 7, 15, 9, 0),
        tz_name="Asia/Shanghai",
        platform_name="Darwin",
        goal_section=section,
    )

    assert section not in prompt.cached
    assert section in prompt.dynamic
    blocks = prompt.as_cached_blocks()
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert section in blocks[-1]["text"]


@pytest.mark.asyncio
async def test_ordinary_prompt_never_loads_passive_goal_authority() -> None:
    prompt = SimpleNamespace(
        job=GenerationJob(
            "ordinary-stream",
            "session-with-a-persisted-goal",
            invocation_source="desktop",
        ),
        # Any storage access would reveal an accidental passive Goal lookup.
        session_factory=None,
    )

    await SessionPrompt._load_goal_context(prompt)

    assert not hasattr(prompt, "goal_snapshot")
    assert not hasattr(prompt, "goal_prompt_section")
