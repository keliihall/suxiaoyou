from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import release_features
from app.models.goal_run import GoalRun
from app.models.goal_usage_record import GoalUsageRecord
from app.models.message import Message, Part
from app.models.session import Session
from app.models.session_goal import SessionGoal
from app.office_validation.precommit import set_office_precommit_coordinator
from app.office_validation.repair_agent import (
    OfficePrecommitRepairAgentError,
    OfficeRepairExecutionReceipt,
    ProviderOfficePrecommitRepairer,
)
from app.provider.registry import ProviderRegistry
from app.schemas.provider import ModelCapabilities, ModelInfo, ModelPricing
from app.session.goal_manager import get_goal_token_usage_breakdown
from app.session.processor import (
    _office_precommit_repairer_for_prompt,
    _office_repair_admission_factory,
    _office_repair_execution_observer,
)
from app.streaming.manager import GenerationJob


class _Coordinator:
    async def begin(self, *, request, view):  # pragma: no cover - identity only
        raise AssertionError((request, view))


@pytest.fixture(autouse=True)
def _reset_coordinator() -> None:
    set_office_precommit_coordinator(None)
    yield
    set_office_precommit_coordinator(None)


def _open_authoring_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "V11_CHECKPOINTS_RELEASED",
        "V11_REWIND_RELEASED",
        "V11_VALIDATION_AGENT_RELEASED",
        "V11_OFFICE_V2_RELEASED",
    ):
        monkeypatch.setattr(release_features, name, True)


def _prompt(*, json_output: bool = True) -> SimpleNamespace:
    registry = ProviderRegistry()
    return SimpleNamespace(
        provider_registry=registry,
        provider=SimpleNamespace(id="provider-a"),
        model_id="model-a",
        model_info=ModelInfo(
            id="model-a",
            name="Model A",
            provider_id="provider-a",
            capabilities=ModelCapabilities(json_output=json_output),
        ),
    )


def test_repairer_injection_requires_source_gates_and_authoritative_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = _prompt()

    assert _office_precommit_repairer_for_prompt(prompt) is None

    _open_authoring_gates(monkeypatch)
    assert _office_precommit_repairer_for_prompt(prompt) is None

    set_office_precommit_coordinator(_Coordinator())
    repairer = _office_precommit_repairer_for_prompt(prompt)
    assert isinstance(repairer, ProviderOfficePrecommitRepairer)
    assert repairer.provider_id == "provider-a"
    assert repairer.model_id == "model-a"


def test_repairer_binding_is_reused_until_exact_model_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _open_authoring_gates(monkeypatch)
    set_office_precommit_coordinator(_Coordinator())
    prompt = _prompt()

    first = _office_precommit_repairer_for_prompt(prompt)
    assert _office_precommit_repairer_for_prompt(prompt) is first

    prompt.model_id = "model-b"
    prompt.model_info = ModelInfo(
        id="model-b",
        name="Model B",
        provider_id="provider-a",
        capabilities=ModelCapabilities(json_output=True),
    )
    second = _office_precommit_repairer_for_prompt(prompt)
    assert isinstance(second, ProviderOfficePrecommitRepairer)
    assert second is not first
    assert second.model_id == "model-b"


def test_repairer_is_not_injected_without_json_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _open_authoring_gates(monkeypatch)
    set_office_precommit_coordinator(_Coordinator())

    assert _office_precommit_repairer_for_prompt(
        _prompt(json_output=False)
    ) is None


async def _goal_prompt(session_factory, *, token_budget: int = 1_000):
    async with session_factory() as db:
        async with db.begin():
            db.add(Session(id="repair-session", directory=".", title="Repair"))
            db.add(
                SessionGoal(
                    id="repair-goal",
                    session_id="repair-session",
                    objective="Account for Office repair inference",
                    status="active",
                    run_state="running",
                    revision=2,
                    last_run_id="repair-run",
                    token_budget=token_budget,
                    cost_budget_microusd=1_000_000,
                )
            )
            db.add(
                GoalRun(
                    id="repair-run",
                    goal_id="repair-goal",
                    ordinal=1,
                    goal_revision=2,
                    idempotency_key="repair-goal-run",
                    trigger="initial",
                    status="running",
                )
            )
            db.add(
                Message(
                    id="repair-assistant",
                    session_id="repair-session",
                    data={"role": "assistant"},
                )
            )
    job = GenerationJob(
        "repair-stream",
        "repair-session",
        invocation_source="goal",
        goal_id="repair-goal",
        goal_run_id="repair-run",
    )
    return SimpleNamespace(
        job=job,
        session_factory=session_factory,
        assistant_msg_id="repair-assistant",
        total_cost=0.0,
        total_tokens_accumulated={
            "input": 0,
            "output": 0,
            "reasoning": 0,
            "cache_read": 0,
            "cache_write": 0,
        },
        _goal_usage_recorded_tokens=0,
        _goal_usage_recorded_cost_microusd=0,
        _goal_active_started_monotonic=time.monotonic(),
        _goal_wait_seconds_at_start=0.0,
    )


@pytest.mark.asyncio
async def test_repair_observer_persists_path_free_usage_and_goal_ledger(
    session_factory,
) -> None:
    prompt = await _goal_prompt(session_factory)
    observer = _office_repair_execution_observer(prompt)
    assert callable(observer)
    receipt = OfficeRepairExecutionReceipt(
        execution_id="repair-execution",
        provider_id="provider-a",
        model_id="model-a",
        outcome="failed",
        usage={
            "input": 10,
            "output": 5,
            "reasoning": 0,
            "cache_read": 0,
            "cache_write": 0,
            "total": 15,
        },
        model_info=ModelInfo(
            id="model-a",
            name="Model A",
            provider_id="provider-a",
            pricing=ModelPricing(prompt=1.0, completion=2.0),
        ),
    )

    await observer(receipt)

    async with session_factory() as db:
        part = (
            await db.execute(
                select(Part).where(Part.data["type"].as_string() == "office-repair-usage")
            )
        ).scalar_one()
        record = (await db.execute(select(GoalUsageRecord))).scalar_one()
        breakdown = await get_goal_token_usage_breakdown(db, "repair-goal")
    assert part.data["outcome"] == "failed"
    assert "path" not in str(part.data).casefold()
    assert record.source_kind == "office_repair"
    assert record.source_key == f"office_repair:{part.id}"
    assert (record.tokens_used, record.cost_used_microusd) == (15, 20)
    assert prompt.total_tokens_accumulated["input"] == 10
    assert prompt.total_tokens_accumulated["output"] == 5
    assert prompt.total_cost == pytest.approx(0.00002)
    assert prompt.job.goal_run_usage == (15, 20)
    assert breakdown.input == 10
    assert breakdown.output == 5
    assert breakdown.unattributed == 0


@pytest.mark.asyncio
async def test_repair_goal_admission_clamps_budget_and_honors_pause(
    session_factory,
) -> None:
    prompt = await _goal_prompt(session_factory, token_budget=12)
    prompt.job.record_goal_usage(tokens=10, cost_microusd=0)
    admission = _office_repair_admission_factory(prompt)
    assert callable(admission)

    async with admission(4_096) as max_tokens:
        assert max_tokens == 2

    prompt.job.close_execution_admission()
    with pytest.raises(OfficePrecommitRepairAgentError, match="rejected"):
        async with admission(4_096):
            pass
