from __future__ import annotations

import asyncio
import inspect
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from app.agent.agent import AgentRegistry
from app.agent.permission import evaluate
from app.models.security_audit_event import SecurityAuditEvent
from app.models.session import Session
from app.models.session_checkpoint import SessionCheckpoint
from app.models.turn_run import TurnRun
from app.models.workspace_instance import WorkspaceInstance
from app.schemas.agent import Ruleset
from app.schemas.provider import ModelCapabilities, ModelInfo, StreamChunk
from app.storage.checkpoints import inspect_workspace_identity
from app.streaming.manager import GenerationJob
from app.validation_agent import (
    DeterministicValidationFailure,
    PostCheckpointValidationScheduler,
    ServerValidationIntent,
    ValidationAgentService,
    ValidationAgentUnavailable,
    ValidationBudgetLimits,
    ValidationRoundContext,
    ValidationRoundResult,
    ValidationSourceError,
    ValidationTask,
    build_validation_agent_registry,
    build_validation_tool_registry,
)


pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _release_checkpoint_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    """Validator coverage always exercises its required checkpoint gate too."""

    monkeypatch.setattr(
        "app.release_features.V11_CHECKPOINTS_RELEASED",
        True,
    )


def _model_output(
    verdict: str,
    *,
    summary: str = "Evidence supports the verdict.",
    extra: dict[str, Any] | None = None,
) -> str:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "verdict": verdict,
        "summary": summary,
        "evidence": [
            {
                "kind": "file",
                "source": "report.txt:1",
                "summary": "The expected marker is present.",
            }
        ],
    }
    if extra:
        payload.update(extra)
    return json.dumps(payload)


class QueueRunner:
    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.contexts: list[ValidationRoundContext] = []

    async def run_round(
        self,
        context: ValidationRoundContext,
    ) -> ValidationRoundResult:
        self.contexts.append(context)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            outcome = outcome(context)
        if inspect.isawaitable(outcome):
            outcome = await outcome
        assert isinstance(outcome, ValidationRoundResult)
        return outcome


class _RuntimeProvider:
    id = "test-provider"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def stream_chat(self, _model, _messages, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            yield StreamChunk(
                type="tool-call",
                data={
                    "id": "read-report",
                    "name": "read",
                    "arguments": {"file_path": "report.txt"},
                },
            )
            yield StreamChunk(
                type="usage",
                data={"input": 5, "output": 1, "total": 6},
            )
            yield StreamChunk(type="finish", data={"reason": "tool_calls"})
            return
        yield StreamChunk(
            type="text-delta",
            data={"text": _model_output("pass")},
        )
        yield StreamChunk(
            type="usage",
            data={"input": 5, "output": 3, "total": 8},
        )
        yield StreamChunk(type="finish", data={"reason": "stop"})


class _ForbiddenToolProvider(_RuntimeProvider):
    def __init__(self, tool_name: str) -> None:
        super().__init__()
        self.tool_name = tool_name

    async def stream_chat(self, _model, _messages, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            yield StreamChunk(
                type="tool-call",
                data={
                    "id": f"attempt-{self.tool_name}",
                    "name": self.tool_name,
                    "arguments": {
                        "file_path": "owned.txt",
                        "content": "should never be written",
                    },
                },
            )
            yield StreamChunk(
                type="usage",
                data={"input": 5, "output": 1, "total": 6},
            )
            yield StreamChunk(type="finish", data={"reason": "tool_calls"})
            return
        yield StreamChunk(
            type="text-delta",
            data={"text": _model_output("pass")},
        )
        yield StreamChunk(
            type="usage",
            data={"input": 5, "output": 3, "total": 8},
        )
        yield StreamChunk(type="finish", data={"reason": "stop"})


class _RuntimeProviderRegistry:
    def __init__(self, provider: _RuntimeProvider) -> None:
        self.provider = provider
        self.model = ModelInfo(
            id="test-model",
            name="Test Model",
            provider_id=provider.id,
            capabilities=ModelCapabilities(
                function_calling=True,
                max_context=8_192,
                max_output=4_096,
            ),
        )

    def all_models(self):
        return [self.model]

    def resolve_model(self, model_id, provider_id=None):
        if model_id == self.model.id and provider_id in {None, self.provider.id}:
            return self.provider, self.model
        return None

    async def refresh_models(self):
        return {self.provider.id: [self.model]}


async def _seed_source(
    session_factory,
    workspace: Path,
    *,
    root_turn_id: str = "root-turn",
    workspace_instance_id: str = "workspace-instance",
    checkpoint_id: str = "checkpoint",
) -> GenerationJob:
    workspace.mkdir()
    canonical_workspace, identity_token = inspect_workspace_identity(workspace)
    now = datetime.now(timezone.utc)
    async with session_factory() as db:
        async with db.begin():
            db.add(
                Session(
                    id="parent-session",
                    directory=str(workspace),
                    title="Parent",
                    model_id="test-model",
                    provider_id="test-provider",
                )
            )
            await db.flush()
            db.add(
                WorkspaceInstance(
                    id=workspace_instance_id,
                    created_by_session_id="parent-session",
                    kind="direct",
                    root_path=canonical_workspace,
                    identity_token=identity_token,
                    status="active",
                    details={},
                )
            )
            await db.flush()
            db.add(
                TurnRun(
                    id=root_turn_id,
                    session_id="parent-session",
                    workspace_instance_id=workspace_instance_id,
                    root_turn_id=root_turn_id,
                    parent_turn_id=None,
                    depth=0,
                    source_kind="interactive",
                    status="completed",
                    external_side_effects=[],
                    details={},
                    time_started=now,
                    time_finished=now,
                )
            )
            await db.flush()
            db.add(
                SessionCheckpoint(
                    id=checkpoint_id,
                    session_id="parent-session",
                    workspace_instance_id=workspace_instance_id,
                    root_turn_id=root_turn_id,
                    turn_run_id=root_turn_id,
                    sequence=1,
                    todo_snapshot=[],
                    child_turn_ids=[],
                    state="finalized",
                    pin_state="pinned",
                    external_side_effects=[],
                    details={},
                    time_finalized=now,
                )
            )
    return GenerationJob(
        "parent-stream",
        "parent-session",
        invocation_source="desktop",
        invocation_source_id="desktop",
        root_turn_id=root_turn_id,
        turn_run_id=root_turn_id,
        workspace_instance_id=workspace_instance_id,
    )


def _service(session_factory, runner: QueueRunner) -> ValidationAgentService:
    return ValidationAgentService(
        session_factory=session_factory,
        provider_registry=MagicMock(),
        runner=runner,
    )


async def test_gate_false_has_no_runtime_or_database_effects(
    session_factory,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.release_features.V11_VALIDATION_AGENT_RELEASED",
        False,
    )
    runner = QueueRunner(
        ValidationRoundResult(_model_output("pass"), 1, successful_read_calls=1)
    )
    service = _service(session_factory, runner)
    parent = GenerationJob("stream", "missing", invocation_source="desktop")

    with pytest.raises(ValidationAgentUnavailable, match="release gate"):
        await service.validate(
            parent_job=parent,
            checkpoint_id="missing",
            task=ValidationTask(objective="check"),
        )

    assert runner.contexts == []
    assert parent.lifecycle_events == []
    assert AgentRegistry().get("validator") is None
    async with session_factory() as db:
        assert (await db.execute(select(Session))).scalars().all() == []


@pytest.mark.parametrize(
    "closed_gate",
    [
        "V11_CHECKPOINTS_RELEASED",
        "V11_VALIDATION_AGENT_RELEASED",
    ],
)
async def test_post_checkpoint_scheduler_composed_gate_false_has_zero_effects(
    monkeypatch: pytest.MonkeyPatch,
    closed_gate: str,
) -> None:
    monkeypatch.setattr(
        "app.release_features.V11_VALIDATION_AGENT_RELEASED",
        True,
    )
    monkeypatch.setattr(f"app.release_features.{closed_gate}", False)

    class _UnexpectedValidator:
        calls = 0

        async def validate(self, **_kwargs):
            self.calls += 1
            raise AssertionError("closed scheduler invoked its validator")

    validator = _UnexpectedValidator()
    scheduler = PostCheckpointValidationScheduler(validator)  # type: ignore[arg-type]
    parent = GenerationJob(
        "stream",
        "session",
        invocation_source="desktop",
    )

    request_id = await scheduler.request_validation(
        parent_job=parent,
        intent=ServerValidationIntent(
            policy_id="test.closed",
            objective="This must not be queued.",
        ),
    )
    outcomes = await scheduler.run_pending(
        parent_job=parent,
        checkpoint_id="checkpoint",
    )

    assert request_id is None
    assert outcomes == ()
    assert validator.calls == 0
    assert parent.lifecycle_events == []


async def test_server_owned_registry_has_no_write_approval_or_self_control_path(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.release_features.V11_VALIDATION_AGENT_RELEASED",
        True,
    )
    agent = build_validation_agent_registry().get("validator")
    assert agent is not None
    assert agent.mode == "hidden"
    assert agent.tools == ["read", "glob", "grep", "search"]
    assert all(
        evaluate(tool, "*", agent.permissions) == "allow"
        for tool in ("read", "glob", "grep", "search")
    )
    assert all(
        evaluate(tool, "*", agent.permissions) == "deny"
        for tool in (
            "write",
            "edit",
            "bash",
            "code_execute",
            "web_search",
            "office",
            "task",
            "question",
            "plan",
            "submit_plan",
            "update_goal",
        )
    )
    assert evaluate("read", "secrets.env", agent.permissions) == "deny"
    assert evaluate("read", "settings.env.example", agent.permissions) == "allow"

    registry = build_validation_tool_registry()
    assert [tool.id for tool in registry.all_tools()] == [
        "read",
        "glob",
        "grep",
        "search",
    ]
    for forbidden in (
        "write",
        "edit",
        "bash",
        "web_search",
        "office",
        "task",
        "question",
        "submit_plan",
        "update_goal",
    ):
        assert registry.get(forbidden) is None


async def test_valid_pass_runs_in_independent_read_only_child_with_audit(
    session_factory,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.release_features.V11_VALIDATION_AGENT_RELEASED",
        True,
    )
    parent = await _seed_source(session_factory, tmp_path / "workspace")
    runner = QueueRunner(
        ValidationRoundResult(_model_output("pass"), 120, successful_read_calls=1)
    )

    result = await _service(session_factory, runner).validate(
        parent_job=parent,
        checkpoint_id="checkpoint",
        task=ValidationTask(
            objective="Verify the report marker.",
            budget=ValidationBudgetLimits(
                max_rounds=2,
                max_tokens=1_000,
                timeout_ms=2_000,
            ),
        ),
    )

    assert result.verdict == "pass"
    assert result.reason_code == "model_verdict"
    assert result.round == 1
    assert result.budget.tokens_used == 120
    assert result.source.session_id == "parent-session"
    assert result.source.root_turn_id == "root-turn"
    assert result.source.checkpoint_id == "checkpoint"
    assert result.source.workspace_instance_id == "workspace-instance"
    assert len(result.validator_session_ids) == 1

    context = runner.contexts[0]
    assert context.job.invocation_source == "validator"
    assert context.job.invocation_source_id == result.validation_id
    assert context.job.root_turn_id == parent.root_turn_id
    assert context.job.parent_turn_id == parent.turn_run_id
    assert context.job.workspace_instance_id == parent.workspace_instance_id
    assert context.job.interactive is False
    assert context.request.workspace == str((tmp_path / "workspace").resolve())
    assert context.request.model == "test-model"
    assert context.request.provider_id == "test-provider"
    assert context.request._permission_rules_authoritative is True
    rules = Ruleset.model_validate({"rules": context.request.permission_rules})
    assert evaluate("read", "report.txt", rules) == "allow"
    assert evaluate("write", "report.txt", rules) == "deny"
    assert context.tool_registry.get("write") is None
    assert context.tool_registry.get("question") is None
    assert context.tool_registry.get("task") is None

    event_types = [event.event_type for event in parent.lifecycle_events]
    assert event_types == [
        "validation.started",
        "validation.round.started",
        "validation.round.completed",
        "validation.completed",
    ]
    assert all(
        event.root_turn_id == "root-turn" for event in parent.lifecycle_events
    )

    async with session_factory() as db:
        children = (
            await db.execute(
                select(Session).where(Session.parent_id == "parent-session")
            )
        ).scalars().all()
        audits = (
            await db.execute(
                select(SecurityAuditEvent).where(
                    SecurityAuditEvent.source_kind == "validator"
                )
            )
        ).scalars().all()
    assert [child.id for child in children] == list(result.validator_session_ids)
    assert {event.outcome for event in audits} == {"started", "success"}
    assert all(event.invocation_source_kind == "validator" for event in audits)
    assert all("prompt" not in event.details for event in audits)


async def test_production_runner_uses_real_session_prompt_and_clamps_output_budget(
    session_factory,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.release_features.V11_VALIDATION_AGENT_RELEASED",
        True,
    )
    parent = await _seed_source(session_factory, tmp_path / "workspace")
    (tmp_path / "workspace" / "report.txt").write_text(
        "expected marker\n",
        encoding="utf-8",
    )
    provider = _RuntimeProvider()
    provider_registry = _RuntimeProviderRegistry(provider)
    service = ValidationAgentService(
        session_factory=session_factory,
        provider_registry=provider_registry,  # type: ignore[arg-type]
    )
    scheduler = PostCheckpointValidationScheduler(
        service,
        budget=ValidationBudgetLimits(
            max_rounds=1,
            max_tokens=100,
            timeout_ms=2_000,
        ),
    )
    request_id = await scheduler.request_validation(
        parent_job=parent,
        intent=ServerValidationIntent(
            policy_id="test.real-runner",
            objective="Verify using the real SessionPrompt runtime.",
        ),
    )
    outcomes = await scheduler.run_pending(
        parent_job=parent,
        checkpoint_id="checkpoint",
    )
    assert len(outcomes) == 1
    assert outcomes[0].request_id == request_id
    assert outcomes[0].status == "completed"
    assert outcomes[0].record is not None
    result = outcomes[0].record

    assert result.verdict == "pass"
    assert result.round == 1
    assert result.budget.tokens_used == 14
    assert len(provider.calls) == 2
    for call in provider.calls:
        assert 1 <= call["max_tokens"] <= 100
        assert {
            spec["function"]["name"] for spec in call["tools"]
        } == {"read", "glob", "grep", "search"}
        assert call["response_format"] is None
    assert provider.calls[1]["max_tokens"] <= 94

    # The validator child is a read-only observer of the parent's finalized
    # source. Even with both release gates open it must not create a child
    # mutation TurnRun or checkpoint of its own.
    async with session_factory() as db:
        turn_runs = (await db.execute(select(TurnRun))).scalars().all()
        checkpoints = (
            await db.execute(select(SessionCheckpoint))
        ).scalars().all()
    assert [(turn.id, turn.parent_turn_id, turn.status) for turn in turn_runs] == [
        ("root-turn", None, "completed")
    ]
    assert [checkpoint.id for checkpoint in checkpoints] == ["checkpoint"]


async def test_scheduler_binds_actual_checkpoint_and_code_owned_budget(
    session_factory,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.release_features.V11_VALIDATION_AGENT_RELEASED",
        True,
    )
    parent = await _seed_source(session_factory, tmp_path / "workspace")
    runner = QueueRunner(
        ValidationRoundResult(_model_output("pass"), 7, successful_read_calls=1)
    )
    budget = ValidationBudgetLimits(
        max_rounds=1,
        max_tokens=41,
        timeout_ms=2_000,
    )
    scheduler = PostCheckpointValidationScheduler(
        _service(session_factory, runner),
        budget=budget,
    )

    # Neither checkpoint identity nor resource budget is accepted by the
    # request hook; both belong to the server-side dispatch boundary.
    assert set(inspect.signature(scheduler.request_validation).parameters) == {
        "parent_job",
        "intent",
    }
    request_id = await scheduler.request_validation(
        parent_job=parent,
        intent=ServerValidationIntent(
            policy_id="test.office-quality",
            objective="Verify the finalized Office artifact.",
        ),
    )
    assert request_id is not None

    outcomes = await scheduler.run_pending(
        parent_job=parent,
        checkpoint_id="checkpoint",
    )

    assert len(outcomes) == 1
    assert outcomes[0].request_id == request_id
    assert outcomes[0].status == "completed"
    assert outcomes[0].passed is True
    assert outcomes[0].record is not None
    assert outcomes[0].record.source.checkpoint_id == "checkpoint"
    assert outcomes[0].record.budget.max_rounds == 1
    assert outcomes[0].record.budget.max_tokens == 41
    assert runner.contexts[0].remaining_tokens == 41
    assert await scheduler.run_pending(
        parent_job=parent,
        checkpoint_id="checkpoint",
    ) == ()


async def test_scheduler_parent_cancellation_is_fail_closed_without_runner(
    session_factory,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.release_features.V11_VALIDATION_AGENT_RELEASED",
        True,
    )
    parent = await _seed_source(session_factory, tmp_path / "workspace")
    runner = QueueRunner(
        ValidationRoundResult(_model_output("pass"), 1, successful_read_calls=1)
    )
    scheduler = PostCheckpointValidationScheduler(_service(session_factory, runner))
    await scheduler.request_validation(
        parent_job=parent,
        intent=ServerValidationIntent(
            policy_id="test.cancelled",
            objective="Do not pass after parent cancellation.",
        ),
    )
    parent.abort()

    outcomes = await scheduler.run_pending(
        parent_job=parent,
        checkpoint_id="checkpoint",
    )

    assert len(outcomes) == 1
    assert outcomes[0].status == "completed"
    assert outcomes[0].passed is False
    assert outcomes[0].record is not None
    assert outcomes[0].record.verdict == "needs_review"
    assert outcomes[0].record.reason_code == "cancelled"
    assert runner.contexts == []


async def test_scheduler_runner_failure_and_dependency_failure_never_pass(
    session_factory,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.release_features.V11_VALIDATION_AGENT_RELEASED",
        True,
    )
    parent = await _seed_source(session_factory, tmp_path / "workspace")
    runner = QueueRunner(RuntimeError("runner failed"))
    scheduler = PostCheckpointValidationScheduler(
        _service(session_factory, runner),
        budget=ValidationBudgetLimits(
            max_rounds=1,
            max_tokens=100,
            timeout_ms=2_000,
        ),
    )
    await scheduler.request_validation(
        parent_job=parent,
        intent=ServerValidationIntent(
            policy_id="test.runner-failure",
            objective="A runtime failure must not become a pass.",
        ),
    )
    outcomes = await scheduler.run_pending(
        parent_job=parent,
        checkpoint_id="checkpoint",
    )
    assert len(outcomes) == 1
    assert outcomes[0].status == "completed"
    assert outcomes[0].passed is False
    assert outcomes[0].record is not None
    assert outcomes[0].record.verdict == "needs_review"
    assert outcomes[0].record.reason_code == "runner_error"

    class _UnavailableValidator:
        async def validate(self, **_kwargs):
            raise RuntimeError("dependency unavailable")

    unavailable = PostCheckpointValidationScheduler(
        _UnavailableValidator()  # type: ignore[arg-type]
    )
    await unavailable.request_validation(
        parent_job=parent,
        intent=ServerValidationIntent(
            policy_id="test.dependency-failure",
            objective="Unexpected dispatch failure must close safely.",
        ),
    )
    failed = await unavailable.run_pending(
        parent_job=parent,
        checkpoint_id="checkpoint",
    )
    assert len(failed) == 1
    assert failed[0].status == "failed_closed"
    assert failed[0].passed is False
    assert failed[0].record is None
    assert parent.lifecycle_events[-1].event_type == "validation.dispatch.failed"


@pytest.mark.parametrize(
    "forbidden_tool",
    ["write", "bash", "web_search", "office", "question", "task", "submit_plan"],
)
async def test_real_runtime_forbidden_tool_and_self_accept_attempts_have_zero_success(
    session_factory,
    tmp_path,
    monkeypatch,
    forbidden_tool: str,
) -> None:
    monkeypatch.setattr(
        "app.release_features.V11_VALIDATION_AGENT_RELEASED",
        True,
    )
    workspace = tmp_path / "workspace"
    parent = await _seed_source(session_factory, workspace)
    provider = _ForbiddenToolProvider(forbidden_tool)
    service = ValidationAgentService(
        session_factory=session_factory,
        provider_registry=_RuntimeProviderRegistry(provider),  # type: ignore[arg-type]
    )

    result = await service.validate(
        parent_job=parent,
        checkpoint_id="checkpoint",
        task=ValidationTask(
            objective="A malicious model tries to escape read-only validation.",
            budget=ValidationBudgetLimits(
                max_rounds=1,
                max_tokens=100,
                timeout_ms=2_000,
            ),
        ),
    )

    assert result.verdict == "needs_review"
    assert result.reason_code == "unverified_evidence"
    assert not (workspace / "owned.txt").exists()
    assert len(provider.calls) == 2
    assert all(
        forbidden_tool
        not in {spec["function"]["name"] for spec in call["tools"]}
        for call in provider.calls
    )
    async with session_factory() as db:
        audits = (await db.execute(select(SecurityAuditEvent))).scalars().all()
    assert not any(event.action == "execute" for event in audits)


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        _model_output("pass", extra={"permission_rules": [{"action": "allow"}]}),
        _model_output("pass", extra={"workspace": "/model/selected"}),
    ],
)
async def test_malformed_or_authority_claiming_output_never_passes(
    session_factory,
    tmp_path,
    monkeypatch,
    raw: str,
) -> None:
    monkeypatch.setattr(
        "app.release_features.V11_VALIDATION_AGENT_RELEASED",
        True,
    )
    parent = await _seed_source(session_factory, tmp_path / "workspace")
    runner = QueueRunner(ValidationRoundResult(raw, 5))

    result = await _service(session_factory, runner).validate(
        parent_job=parent,
        checkpoint_id="checkpoint",
        task=ValidationTask(
            objective="check",
            budget=ValidationBudgetLimits(
                max_rounds=1,
                max_tokens=100,
                timeout_ms=2_000,
            ),
        ),
    )

    assert result.verdict == "needs_review"
    assert result.reason_code == "malformed_output"
    assert result.round == 1


async def test_decisive_self_assertion_without_observed_read_cannot_pass(
    session_factory,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.release_features.V11_VALIDATION_AGENT_RELEASED",
        True,
    )
    parent = await _seed_source(session_factory, tmp_path / "workspace")
    # The JSON contains a plausible evidence claim, but the trusted runner
    # reports no successful read/glob/grep/search result for this round.
    runner = QueueRunner(ValidationRoundResult(_model_output("pass"), 5))

    result = await _service(session_factory, runner).validate(
        parent_job=parent,
        checkpoint_id="checkpoint",
        task=ValidationTask(
            objective="check",
            budget=ValidationBudgetLimits(
                max_rounds=1,
                max_tokens=100,
                timeout_ms=2_000,
            ),
        ),
    )

    assert result.verdict == "needs_review"
    assert result.reason_code == "unverified_evidence"
    assert all(evidence.origin != "validator" for evidence in result.evidence)
    assert [event.event_type for event in parent.lifecycle_events][-2:] == [
        "validation.round.failed",
        "validation.completed",
    ]


async def test_deterministic_failure_has_precedence_over_agent_pass(
    session_factory,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.release_features.V11_VALIDATION_AGENT_RELEASED",
        True,
    )
    parent = await _seed_source(session_factory, tmp_path / "workspace")
    runner = QueueRunner(
        ValidationRoundResult(_model_output("pass"), 10, successful_read_calls=1)
    )

    result = await _service(session_factory, runner).validate(
        parent_job=parent,
        checkpoint_id="checkpoint",
        task=ValidationTask(
            objective="Validate preview fidelity.",
            deterministic_failures=(
                DeterministicValidationFailure(
                    code="pixel_diff",
                    source="preview/page-1.png",
                    summary="The deterministic threshold was exceeded.",
                ),
            ),
            budget=ValidationBudgetLimits(
                max_rounds=2,
                max_tokens=100,
                timeout_ms=2_000,
            ),
        ),
    )

    assert result.verdict == "fail"
    assert result.reason_code == "deterministic_failure"
    assert result.round == 1
    assert result.evidence[0].origin == "deterministic"
    assert result.evidence[0].kind == "deterministic_failure"


async def test_deterministic_failure_remains_fail_when_agent_times_out(
    session_factory,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.release_features.V11_VALIDATION_AGENT_RELEASED",
        True,
    )
    parent = await _seed_source(session_factory, tmp_path / "workspace")

    async def block(_context: ValidationRoundContext) -> ValidationRoundResult:
        await asyncio.Future()

    result = await _service(session_factory, QueueRunner(block)).validate(
        parent_job=parent,
        checkpoint_id="checkpoint",
        task=ValidationTask(
            objective="check",
            deterministic_failures=(
                DeterministicValidationFailure(
                    code="known_failure",
                    source="deterministic/check",
                    summary="The deterministic check already failed.",
                ),
            ),
            budget=ValidationBudgetLimits(
                max_rounds=1,
                max_tokens=100,
                timeout_ms=50,
            ),
        ),
    )

    assert result.verdict == "fail"
    assert result.reason_code == "deterministic_failure"
    assert result.evidence[0].origin == "deterministic"
    assert any(evidence.source == "validation_timeout" for evidence in result.evidence)


async def test_needs_review_gets_exactly_one_server_owned_second_round(
    session_factory,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.release_features.V11_VALIDATION_AGENT_RELEASED",
        True,
    )
    parent = await _seed_source(session_factory, tmp_path / "workspace")
    runner = QueueRunner(
        ValidationRoundResult(_model_output("needs_review"), 20),
        ValidationRoundResult(
            _model_output("pass"), 30, successful_read_calls=1
        ),
    )

    result = await _service(session_factory, runner).validate(
        parent_job=parent,
        checkpoint_id="checkpoint",
        task=ValidationTask(
            objective="check",
            budget=ValidationBudgetLimits(
                max_rounds=2,
                max_tokens=100,
                timeout_ms=2_000,
            ),
        ),
    )

    assert result.verdict == "pass"
    assert result.round == 2
    assert result.budget.rounds_used == 2
    assert result.budget.tokens_used == 50
    assert len(runner.contexts) == 2
    assert len({context.job.session_id for context in runner.contexts}) == 2
    assert {
        context.job.root_turn_id for context in runner.contexts
    } == {parent.root_turn_id}
    assert {
        context.job.parent_turn_id for context in runner.contexts
    } == {parent.turn_run_id}


async def test_two_inconclusive_rounds_cannot_allocate_a_third(
    session_factory,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.release_features.V11_VALIDATION_AGENT_RELEASED",
        True,
    )
    parent = await _seed_source(session_factory, tmp_path / "workspace")
    runner = QueueRunner(
        ValidationRoundResult(_model_output("needs_review"), 1),
        ValidationRoundResult(_model_output("needs_review"), 1),
        ValidationRoundResult(_model_output("pass"), 1, successful_read_calls=1),
    )

    result = await _service(session_factory, runner).validate(
        parent_job=parent,
        checkpoint_id="checkpoint",
        task=ValidationTask(objective="check"),
    )

    assert result.verdict == "needs_review"
    assert result.round == 2
    assert len(runner.contexts) == 2
    assert len(runner.outcomes) == 1


async def test_token_budget_overrun_overrides_agent_pass(
    session_factory,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.release_features.V11_VALIDATION_AGENT_RELEASED",
        True,
    )
    parent = await _seed_source(session_factory, tmp_path / "workspace")
    runner = QueueRunner(
        ValidationRoundResult(_model_output("pass"), 11, successful_read_calls=1)
    )

    result = await _service(session_factory, runner).validate(
        parent_job=parent,
        checkpoint_id="checkpoint",
        task=ValidationTask(
            objective="check",
            budget=ValidationBudgetLimits(
                max_rounds=1,
                max_tokens=10,
                timeout_ms=2_000,
            ),
        ),
    )

    assert result.verdict == "needs_review"
    assert result.reason_code == "budget_exhausted"
    assert result.budget.tokens_used == 11


async def test_timeout_cancels_child_and_never_returns_pass(
    session_factory,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.release_features.V11_VALIDATION_AGENT_RELEASED",
        True,
    )
    parent = await _seed_source(session_factory, tmp_path / "workspace")
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def block(_context: ValidationRoundContext) -> ValidationRoundResult:
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    runner = QueueRunner(block)
    result = await _service(session_factory, runner).validate(
        parent_job=parent,
        checkpoint_id="checkpoint",
        task=ValidationTask(
            objective="check",
            budget=ValidationBudgetLimits(
                max_rounds=1,
                max_tokens=100,
                timeout_ms=50,
            ),
        ),
    )

    assert started.is_set()
    assert cancelled.is_set()
    assert result.verdict == "needs_review"
    assert result.reason_code == "timeout"
    assert runner.contexts[0].job.abort_event.is_set()


async def test_parent_abort_before_round_is_fail_closed_without_runner(
    session_factory,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.release_features.V11_VALIDATION_AGENT_RELEASED",
        True,
    )
    parent = await _seed_source(session_factory, tmp_path / "workspace")
    parent.abort_event.set()
    runner = QueueRunner(
        ValidationRoundResult(_model_output("pass"), 1, successful_read_calls=1)
    )

    result = await _service(session_factory, runner).validate(
        parent_job=parent,
        checkpoint_id="checkpoint",
        task=ValidationTask(objective="check"),
    )

    assert result.verdict == "needs_review"
    assert result.reason_code == "cancelled"
    assert result.round == 0
    assert runner.contexts == []


async def test_cancelling_service_task_cancels_child_and_propagates(
    session_factory,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.release_features.V11_VALIDATION_AGENT_RELEASED",
        True,
    )
    parent = await _seed_source(session_factory, tmp_path / "workspace")
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def block(_context: ValidationRoundContext) -> ValidationRoundResult:
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    runner = QueueRunner(block)
    task = asyncio.create_task(
        _service(session_factory, runner).validate(
            parent_job=parent,
            checkpoint_id="checkpoint",
            task=ValidationTask(objective="check"),
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert cancelled.is_set()
    assert runner.contexts[0].job.abort_event.is_set()
    assert parent.lifecycle_events[-1].event_type == "validation.cancelled"


async def test_source_mismatch_and_recursive_validator_are_rejected(
    session_factory,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.release_features.V11_VALIDATION_AGENT_RELEASED",
        True,
    )
    parent = await _seed_source(session_factory, tmp_path / "workspace")
    runner = QueueRunner(
        ValidationRoundResult(_model_output("pass"), 1, successful_read_calls=1)
    )
    service = _service(session_factory, runner)

    parent._root_turn_id = "other-turn"
    with pytest.raises(ValidationSourceError, match="another root turn"):
        await service.validate(
            parent_job=parent,
            checkpoint_id="checkpoint",
            task=ValidationTask(objective="check"),
        )

    recursive = GenerationJob(
        "validator-stream",
        "parent-session",
        invocation_source="validator",
        root_turn_id="root-turn",
        workspace_instance_id="workspace-instance",
    )
    with pytest.raises(ValidationSourceError, match="cannot validate itself"):
        await service.validate(
            parent_job=recursive,
            checkpoint_id="checkpoint",
            task=ValidationTask(objective="check"),
        )
    assert runner.contexts == []


async def test_replaced_workspace_directory_cannot_inherit_checkpoint_identity(
    session_factory,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.release_features.V11_VALIDATION_AGENT_RELEASED",
        True,
    )
    workspace = tmp_path / "workspace"
    parent = await _seed_source(session_factory, workspace)
    moved = tmp_path / "workspace-old"
    workspace.rename(moved)
    workspace.mkdir()
    runner = QueueRunner(
        ValidationRoundResult(_model_output("pass"), 1, successful_read_calls=1)
    )

    with pytest.raises(ValidationSourceError, match="identity changed"):
        await _service(session_factory, runner).validate(
            parent_job=parent,
            checkpoint_id="checkpoint",
            task=ValidationTask(objective="check"),
        )

    assert runner.contexts == []
