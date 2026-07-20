"""Compaction tests."""

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models.goal_run import GoalRun
from app.models.goal_usage_record import GoalUsageRecord
from app.models.message import Message, Part
from app.models.session import Session
from app.models.session_goal import SessionGoal
from app.schemas.provider import ModelInfo, ModelPricing, StreamChunk
from app.session.compaction import _phase2_summarize, should_compact
from app.session.microcompact import context_collapse
from app.streaming.manager import GenerationJob


class _CompactionProvider:
    id = "compaction-provider"

    def __init__(self) -> None:
        self.started = False
        self.messages = None
        self.system = None

    async def stream_chat(self, _model_id, messages, **kwargs):
        self.started = True
        self.messages = messages
        self.system = kwargs.get("system")
        yield StreamChunk(type="text-delta", data={"text": "durable summary"})
        yield StreamChunk(
            type="usage",
            data={"input": 10, "output": 5, "total": 15},
        )


class _CompactionRegistry:
    def __init__(self, provider: _CompactionProvider) -> None:
        self.provider = provider
        self.model = ModelInfo(
            id="compaction-model",
            name="Compaction",
            provider_id=provider.id,
            pricing=ModelPricing(prompt=1.0, completion=2.0),
        )

    def all_models(self):
        return [self.model]

    def resolve_model(self, model_id):
        assert model_id == self.model.id
        return self.provider, self.model


async def _seed_goal_compaction(
    session_factory,
    *,
    language: str = "zh",
) -> GenerationJob:
    async with session_factory() as db:
        async with db.begin():
            db.add(Session(id="compact-session", directory=".", title="Compact"))
            db.add(
                SessionGoal(
                    id="compact-goal",
                    session_id="compact-session",
                    objective="Compact without escaping the Goal budget",
                    status="active",
                    run_state="running",
                    revision=2,
                    last_run_id="compact-run",
                    token_budget=1_000,
                    cost_budget_microusd=1_000_000,
                )
            )
            db.add(
                GoalRun(
                    id="compact-run",
                    goal_id="compact-goal",
                    ordinal=1,
                    goal_revision=2,
                    idempotency_key="goal-compaction-run",
                    trigger="initial",
                    status="running",
                )
            )
            message = Message(
                id="compact-user-message",
                session_id="compact-session",
                data={"role": "user"},
            )
            db.add(message)
            await db.flush()
            db.add(
                Part(
                    id="compact-user-text",
                    message_id=message.id,
                    session_id="compact-session",
                    data={"type": "text", "text": "Summarize this work"},
                )
            )
    return GenerationJob(
        "compact-stream",
        "compact-session",
        invocation_source="goal",
        goal_id="compact-goal",
        goal_run_id="compact-run",
        language=language,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("language", "expected", "excluded"),
    [
        ("zh", "总结上方对话", "Summarize the conversation above"),
        ("en", "Summarize the conversation above", "总结上方对话"),
    ],
)
@pytest.mark.asyncio
async def test_goal_compaction_persists_usage_in_same_durable_ledger(
    session_factory,
    language: str,
    expected: str,
    excluded: str,
) -> None:
    job = await _seed_goal_compaction(session_factory, language=language)
    provider = _CompactionProvider()

    summary = await _phase2_summarize(
        "compact-session",
        job=job,
        session_factory=session_factory,
        provider_registry=_CompactionRegistry(provider),  # type: ignore[arg-type]
        agent_registry=SimpleNamespace(
            get=lambda name: SimpleNamespace(system_prompt="Summarize")
            if name == "compaction"
            else None
        ),  # type: ignore[arg-type]
    )

    assert summary == "durable summary"
    assert provider.started is True
    assert provider.messages is not None
    compaction_request = provider.messages[-1]
    assert compaction_request["role"] == "user"
    assert expected in compaction_request["content"]
    assert excluded not in compaction_request["content"]
    assert (
        "不是真实用户消息" in compaction_request["content"]
        if language == "zh"
        else "not a genuine user message" in compaction_request["content"]
    )
    async with session_factory() as db:
        records = list((await db.execute(select(GoalUsageRecord))).scalars())
        usage_message = (
            await db.execute(
                select(Message).where(
                    Message.session_id == "compact-session",
                    Message.id != "compact-user-message",
                )
            )
        ).scalar_one()
    assert len(records) == 1
    assert records[0].source_kind == "compaction"
    assert records[0].source_key == f"compaction:{usage_message.id}"
    assert (records[0].tokens_used, records[0].cost_used_microusd) == (15, 20)
    assert job.goal_run_usage == (15, 20)


@pytest.mark.asyncio
async def test_goal_pause_closes_compaction_provider_admission(
    session_factory,
) -> None:
    job = await _seed_goal_compaction(session_factory)
    provider = _CompactionProvider()
    job.close_execution_admission()

    summary = await _phase2_summarize(
        "compact-session",
        job=job,
        session_factory=session_factory,
        provider_registry=_CompactionRegistry(provider),  # type: ignore[arg-type]
        agent_registry=SimpleNamespace(
            get=lambda _name: SimpleNamespace(system_prompt="Summarize")
        ),  # type: ignore[arg-type]
    )

    assert summary is None
    assert provider.started is False
    async with session_factory() as db:
        assert list((await db.execute(select(GoalUsageRecord))).scalars()) == []


@pytest.mark.parametrize(
    ("language", "expected", "excluded"),
    [
        ("zh", "上下文已折叠", "Context collapsed"),
        ("en", "Context collapsed", "上下文已折叠"),
    ],
)
def test_context_collapse_boundary_follows_process_language(
    language: str,
    expected: str,
    excluded: str,
) -> None:
    messages = [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"message-{index}-" + ("x" * 100),
        }
        for index in range(12)
    ]

    collapsed, tokens_saved = context_collapse(
        messages,
        collapse_fraction=0.5,
        min_messages_to_keep=4,
        language=language,
    )

    assert tokens_saved > 0
    assert expected in collapsed[0]["content"]
    assert excluded not in collapsed[0]["content"]


class TestShouldCompact:
    def test_below_threshold(self):
        usage = {"input": 1000, "output": 500}
        assert not should_compact(usage, model_max_context=128_000)

    def test_above_threshold(self):
        usage = {"input": 120_000, "output": 5_000}
        assert should_compact(usage, model_max_context=128_000, reserved=20_000)

    def test_below_output_safe_threshold(self):
        usage = {"input": 99_807, "output": 0}
        # usable = min(128000 - 8192(output) - 20000(reserved), 85% context) = 99808
        assert not should_compact(usage, model_max_context=128_000, reserved=20_000)

    def test_over_output_safe_threshold(self):
        usage = {"input": 99_809, "output": 0}
        assert should_compact(usage, model_max_context=128_000, reserved=20_000)

    def test_proactive_threshold_uses_eighty_five_percent_context(self):
        usage = {"input": 108_800, "output": 0}
        assert should_compact(
            usage,
            model_max_context=128_000,
            model_max_output=512,
        )

    def test_below_proactive_threshold_when_output_reserve_allows_more(self):
        usage = {"input": 108_799, "output": 0}
        assert not should_compact(
            usage,
            model_max_context=128_000,
            model_max_output=512,
        )

    def test_large_context_model_compacts_at_eighty_five_percent(self):
        usage = {"input": 892_500, "output": 0}
        assert should_compact(
            usage,
            model_max_context=1_050_000,
            model_max_output=128_000,
        )

    def test_empty_usage(self):
        assert not should_compact({}, model_max_context=128_000)

    def test_small_model(self):
        usage = {"input": 3500, "output": 500}
        assert should_compact(usage, model_max_context=4096, model_max_output=512, reserved=500)

    def test_uses_reported_total_when_present(self):
        usage = {
            "input": 10,
            "output": 10,
            "reasoning": 10,
            "cache_read": 10,
            "total": 108_001,
        }
        assert should_compact(usage, model_max_context=128_000, reserved=20_000)

    def test_includes_reasoning_and_cache_read_in_fallback_total(self):
        usage = {
            "input": 107_900,
            "output": 0,
            "reasoning": 50,
            "cache_read": 100,
        }
        assert should_compact(usage, model_max_context=128_000, reserved=20_000)
