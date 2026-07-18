"""Server-owned orchestration for the v1.1 read-only validation Agent."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.agent import AgentRegistry
from app.agent.permission import evaluate
from app.models.session import Session
from app.models.session_checkpoint import SessionCheckpoint
from app.models.workspace_instance import WorkspaceInstance
from app.provider.registry import ProviderRegistry
from app.schemas.chat import PromptRequest
from app.security.audit import record_security_event
from app.session.manager import create_session
from app.session.prompt import SessionPrompt
from app.storage.checkpoints import (
    CheckpointValidationError,
    inspect_workspace_identity,
)
from app.streaming.events import AGENT_ERROR, TEXT_DELTA, TOOL_RESULT
from app.streaming.manager import GenerationJob
from app.tool.builtin.glob_tool import GlobTool
from app.tool.builtin.grep import GrepTool
from app.tool.builtin.read import ReadTool
from app.tool.builtin.search import SearchTool
from app.tool.registry import ToolRegistry
from app.tool.workspace import validate_agent_workspace_root
from app.utils.id import generate_ulid
from app.validation_agent.contracts import (
    VALIDATION_CONTRACT_VERSION,
    DeterministicValidationFailure,
    ValidationBudgetLimits,
    ValidationBudgetReport,
    ValidationContractError,
    ValidationEvidence,
    ValidationModelVerdict,
    ValidationSource,
    ValidationTask,
    ValidationVerdictRecord,
    parse_validation_model_verdict,
)


logger = logging.getLogger(__name__)

_VALIDATOR_TOOLS = ("read", "glob", "grep", "search")
_FORBIDDEN_VALIDATOR_CAPABILITIES = (
    "bash",
    "code_execute",
    "write",
    "edit",
    "apply_patch",
    "office",
    "restore_file_version",
    "web_fetch",
    "web_search",
    "task",
    "question",
    "plan",
    "submit_plan",
    "todo",
    "get_goal",
    "update_goal",
    "tool_search",
)


class ValidationAgentUnavailable(RuntimeError):
    """The release gate is closed or the built-in policy is unavailable."""


class ValidationSourceError(ValueError):
    """The requested checkpoint does not belong to the parent runtime source."""


class ValidationRunnerError(RuntimeError):
    """The real Agent runtime failed without producing a usable response."""


def validation_agent_enabled() -> bool:
    """Read the dependency-composed release capability on every boundary."""

    from app.release_readiness import v11_capability_released

    return v11_capability_released("validator")


def build_validation_tool_registry() -> ToolRegistry:
    """Return an isolated registry containing only known built-in readers."""

    registry = ToolRegistry()
    for tool in (ReadTool(), GlobTool(), GrepTool(), SearchTool()):
        if not tool.is_concurrency_safe or tool.requires_approval:
            raise ValidationAgentUnavailable(
                f"validator tool {tool.id!r} no longer satisfies the read-only contract"
            )
        registry.register(tool)
    if tuple(tool.id for tool in registry.all_tools()) != _VALIDATOR_TOOLS:
        raise ValidationAgentUnavailable("validator tool registry contract mismatch")
    return registry


def build_validation_agent_registry() -> AgentRegistry:
    """Return a fresh registry and verify the reserved validator policy."""

    registry = AgentRegistry()
    agent = registry.get("validator")
    if (
        agent is None
        or agent.mode != "hidden"
        or tuple(agent.tools) != _VALIDATOR_TOOLS
        or agent.metadata.get("server_owned") is not True
        or agent.metadata.get("contract_version") != VALIDATION_CONTRACT_VERSION
    ):
        raise ValidationAgentUnavailable("built-in validator Agent contract mismatch")
    for capability in _VALIDATOR_TOOLS:
        if evaluate(capability, "*", agent.permissions) != "allow":
            raise ValidationAgentUnavailable(
                f"validator read capability {capability!r} is not allowed"
            )
    for capability in _FORBIDDEN_VALIDATOR_CAPABILITIES:
        if evaluate(capability, "*", agent.permissions) != "deny":
            raise ValidationAgentUnavailable(
                f"validator capability {capability!r} is not denied"
            )
    return registry


@dataclass(frozen=True, slots=True)
class ValidationRoundResult:
    """Raw runtime output plus measured Provider token use for one round."""

    raw_output: str
    tokens_used: int
    successful_read_calls: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.raw_output, str):
            raise TypeError("raw_output must be text")
        if (
            not isinstance(self.tokens_used, int)
            or isinstance(self.tokens_used, bool)
            or self.tokens_used < 0
        ):
            raise ValueError("tokens_used must be a non-negative integer")
        if (
            not isinstance(self.successful_read_calls, int)
            or isinstance(self.successful_read_calls, bool)
            or self.successful_read_calls < 0
        ):
            raise ValueError(
                "successful_read_calls must be a non-negative integer"
            )


@dataclass(frozen=True, slots=True)
class ValidationRoundContext:
    """All server-owned runtime objects for one validator round."""

    validation_id: str
    round: int
    remaining_tokens: int
    job: GenerationJob
    request: PromptRequest
    agent_registry: AgentRegistry
    tool_registry: ToolRegistry
    index_manager: Any | None


class ValidationRoundRunner(Protocol):
    async def run_round(
        self,
        context: ValidationRoundContext,
    ) -> ValidationRoundResult: ...


class _ValidationSessionPrompt(SessionPrompt):
    """Real SessionPrompt with validator-specific non-authority controls."""

    def __init__(self, *args: Any, max_output_tokens: int, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._validation_max_output_tokens = max(1, int(max_output_tokens))

    @staticmethod
    def _hooks_gate_enabled() -> bool:
        # Workspace Hooks may run arbitrary user-configured processes. They are
        # intentionally outside the validator's read-only authority.
        return False

    async def _admit_checkpoint_runtime(self) -> None:
        """Validators observe a finalized source; they never own mutations.

        The parent checkpoint is already finalized before this child runtime is
        admitted.  Creating a normal child TurnRun/checkpoint here would both
        violate the read-only validator contract and fail because the parent
        TurnRun is terminal.  Audit records and lifecycle events remain the
        durable provenance for this non-mutating child execution.
        """

        self.checkpoint_binding = None

    async def _finish_checkpoint_runtime(
        self,
        *,
        status: str,
        ledger_failed: bool = False,
    ) -> None:
        """Close only local prompt state; no mutation checkpoint exists."""

        del status, ledger_failed
        self.checkpoint_binding = None
        self._checkpoint_finished = True

    async def _setup(self) -> None:
        await super()._setup()
        if self.model_info is None:
            return
        self.model_info = self.model_info.model_copy(deep=True)
        capabilities = self.model_info.capabilities
        current = capabilities.max_output
        capabilities.max_output = min(
            self._validation_max_output_tokens,
            current if current is not None else self._validation_max_output_tokens,
        )


class SessionPromptValidationRunner:
    """Production runner that executes the real GenerationJob/SessionPrompt path."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        provider_registry: ProviderRegistry,
    ) -> None:
        self._session_factory = session_factory
        self._provider_registry = provider_registry

    async def run_round(
        self,
        context: ValidationRoundContext,
    ) -> ValidationRoundResult:
        prompt = _ValidationSessionPrompt(
            context.job,
            context.request,
            session_factory=self._session_factory,
            provider_registry=self._provider_registry,
            agent_registry=context.agent_registry,
            tool_registry=context.tool_registry,
            index_manager=context.index_manager,
            max_output_tokens=context.remaining_tokens,
        )
        await prompt.run()
        message_id = prompt.assistant_msg_id
        raw_output = "".join(
            str(event.data.get("text", ""))
            for event in context.job.events
            if event.event == TEXT_DELTA
            and (message_id is None or event.data.get("message_id") == message_id)
        )
        error = next(
            (
                str(
                    event.data.get("error_message")
                    or event.data.get("message")
                    or "validation Agent failed"
                )
                for event in reversed(context.job.events)
                if event.event == AGENT_ERROR
            ),
            None,
        )
        if error is not None:
            raise ValidationRunnerError(error[:1_000])
        return ValidationRoundResult(
            raw_output=raw_output,
            tokens_used=sum(
                max(0, int(value or 0))
                for value in prompt.total_tokens_accumulated.values()
            ),
            successful_read_calls=sum(
                1
                for event in context.job.events
                if event.event == TOOL_RESULT
                and str(event.data.get("tool", "")) in _VALIDATOR_TOOLS
            ),
        )


@dataclass(frozen=True, slots=True)
class _ResolvedSource:
    contract: ValidationSource
    workspace: str
    project_id: str | None
    model_id: str | None
    provider_id: str | None


@dataclass(frozen=True, slots=True)
class _RoundTerminal:
    kind: str
    result: ValidationRoundResult | None = None


class ValidationAgentService:
    """Run at most two read-only child rounds under one immutable root turn."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        provider_registry: ProviderRegistry,
        index_manager: Any | None = None,
        runner: ValidationRoundRunner | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._provider_registry = provider_registry
        self._index_manager = index_manager
        self._runner = runner or SessionPromptValidationRunner(
            session_factory=session_factory,
            provider_registry=provider_registry,
        )

    async def validate(
        self,
        *,
        parent_job: GenerationJob,
        checkpoint_id: str,
        task: ValidationTask,
    ) -> ValidationVerdictRecord:
        """Validate one finalized checkpoint without accepting model authority."""

        if not validation_agent_enabled():
            raise ValidationAgentUnavailable(
                "v1.1 validation Agent release gate is closed"
            )
        if parent_job.invocation_source == "validator":
            raise ValidationSourceError("a validator child cannot validate itself")

        validation_id = generate_ulid()
        source = await self._resolve_source(parent_job, checkpoint_id)
        started = time.monotonic()
        deadline = started + task.budget.timeout_ms / 1_000
        collected: list[ValidationEvidence] = []
        child_session_ids: list[str] = []
        tokens_used = 0
        rounds_used = 0

        await self._audit(
            validation_id=validation_id,
            parent_job=parent_job,
            checkpoint_id=checkpoint_id,
            outcome="started",
            required=True,
        )
        parent_job.publish_lifecycle(
            "validation.started",
            {
                "validation_id": validation_id,
                "max_rounds": task.budget.max_rounds,
                "max_tokens": task.budget.max_tokens,
            },
            checkpoint_id=checkpoint_id,
        )

        try:
            if parent_job.abort_event.is_set():
                return await self._finish_fail_closed(
                    parent_job=parent_job,
                    validation_id=validation_id,
                    source=source.contract,
                    budget=task.budget,
                    rounds_used=0,
                    tokens_used=0,
                    started=started,
                    child_session_ids=(),
                    evidence=(),
                    deterministic_failures=task.deterministic_failures,
                    reason="cancelled",
                    summary="Validation was cancelled before the first round.",
                    audit_outcome="cancelled",
                )

            last_model: ValidationModelVerdict | None = None
            last_reason = "model_verdict"
            last_summary = "Validation did not produce a conclusive verdict."

            for round_number in range(1, task.budget.max_rounds + 1):
                if not validation_agent_enabled():
                    return await self._finish_fail_closed(
                        parent_job=parent_job,
                        validation_id=validation_id,
                        source=source.contract,
                        budget=task.budget,
                        rounds_used=rounds_used,
                        tokens_used=tokens_used,
                        started=started,
                        child_session_ids=tuple(child_session_ids),
                        evidence=tuple(collected),
                        deterministic_failures=task.deterministic_failures,
                        reason="cancelled",
                        summary="Validation stopped because its release gate closed.",
                        audit_outcome="cancelled",
                    )
                remaining_seconds = deadline - time.monotonic()
                remaining_tokens = task.budget.max_tokens - tokens_used
                if remaining_seconds <= 0:
                    return await self._finish_fail_closed(
                        parent_job=parent_job,
                        validation_id=validation_id,
                        source=source.contract,
                        budget=task.budget,
                        rounds_used=rounds_used,
                        tokens_used=tokens_used,
                        started=started,
                        child_session_ids=tuple(child_session_ids),
                        evidence=tuple(collected),
                        deterministic_failures=task.deterministic_failures,
                        reason="timeout",
                        summary="Validation exceeded its server time budget.",
                        audit_outcome="timeout",
                    )
                if remaining_tokens <= 0:
                    return await self._finish_fail_closed(
                        parent_job=parent_job,
                        validation_id=validation_id,
                        source=source.contract,
                        budget=task.budget,
                        rounds_used=rounds_used,
                        tokens_used=tokens_used,
                        started=started,
                        child_session_ids=tuple(child_session_ids),
                        evidence=tuple(collected),
                        deterministic_failures=task.deterministic_failures,
                        reason="budget_exhausted",
                        summary="Validation exhausted its server token budget.",
                        audit_outcome="blocked",
                    )

                child_job, context = await self._prepare_round(
                    parent_job=parent_job,
                    validation_id=validation_id,
                    source=source,
                    task=task,
                    round_number=round_number,
                    remaining_tokens=remaining_tokens,
                )
                child_session_ids.append(child_job.session_id)
                rounds_used = round_number
                child_job.publish_lifecycle(
                    "validation.round.started",
                    {
                        "validation_id": validation_id,
                        "round": round_number,
                        "max_rounds": task.budget.max_rounds,
                    },
                    checkpoint_id=checkpoint_id,
                )

                terminal = await self._await_round(
                    parent_job=parent_job,
                    child_job=child_job,
                    context=context,
                    timeout_seconds=remaining_seconds,
                )
                if terminal.kind == "cancelled":
                    return await self._finish_fail_closed(
                        parent_job=parent_job,
                        validation_id=validation_id,
                        source=source.contract,
                        budget=task.budget,
                        rounds_used=rounds_used,
                        tokens_used=tokens_used,
                        started=started,
                        child_session_ids=tuple(child_session_ids),
                        evidence=tuple(collected),
                        deterministic_failures=task.deterministic_failures,
                        reason="cancelled",
                        summary="Validation was cancelled by its parent turn.",
                        audit_outcome="cancelled",
                    )
                if terminal.kind == "timeout":
                    return await self._finish_fail_closed(
                        parent_job=parent_job,
                        validation_id=validation_id,
                        source=source.contract,
                        budget=task.budget,
                        rounds_used=rounds_used,
                        tokens_used=tokens_used,
                        started=started,
                        child_session_ids=tuple(child_session_ids),
                        evidence=tuple(collected),
                        deterministic_failures=task.deterministic_failures,
                        reason="timeout",
                        summary="Validation exceeded its server time budget.",
                        audit_outcome="timeout",
                    )
                if terminal.kind == "runner_error":
                    last_reason = "runner_error"
                    last_summary = "The validation runtime failed closed."
                    self._append_runtime_evidence(
                        collected,
                        source="validation_runtime",
                        summary=last_summary,
                    )
                    child_job.publish_lifecycle(
                        "validation.round.failed",
                        {
                            "validation_id": validation_id,
                            "round": round_number,
                            "reason": last_reason,
                        },
                        checkpoint_id=checkpoint_id,
                    )
                    continue

                assert terminal.result is not None
                tokens_used += terminal.result.tokens_used
                if tokens_used > task.budget.max_tokens:
                    return await self._finish_fail_closed(
                        parent_job=parent_job,
                        validation_id=validation_id,
                        source=source.contract,
                        budget=task.budget,
                        rounds_used=rounds_used,
                        tokens_used=tokens_used,
                        started=started,
                        child_session_ids=tuple(child_session_ids),
                        evidence=tuple(collected),
                        deterministic_failures=task.deterministic_failures,
                        reason="budget_exhausted",
                        summary="Validation exceeded its server token budget.",
                        audit_outcome="blocked",
                    )
                if not validation_agent_enabled():
                    return await self._finish_fail_closed(
                        parent_job=parent_job,
                        validation_id=validation_id,
                        source=source.contract,
                        budget=task.budget,
                        rounds_used=rounds_used,
                        tokens_used=tokens_used,
                        started=started,
                        child_session_ids=tuple(child_session_ids),
                        evidence=tuple(collected),
                        deterministic_failures=task.deterministic_failures,
                        reason="cancelled",
                        summary="Validation stopped because its release gate closed.",
                        audit_outcome="cancelled",
                    )
                try:
                    last_model = parse_validation_model_verdict(
                        terminal.result.raw_output,
                        max_bytes=task.budget.max_output_bytes,
                    )
                except ValidationContractError:
                    last_model = None
                    last_reason = "malformed_output"
                    last_summary = (
                        "The validation Agent returned malformed structured output."
                    )
                    self._append_runtime_evidence(
                        collected,
                        source="validation_contract_v1",
                        summary=last_summary,
                    )
                    child_job.publish_lifecycle(
                        "validation.round.failed",
                        {
                            "validation_id": validation_id,
                            "round": round_number,
                            "reason": last_reason,
                        },
                        checkpoint_id=checkpoint_id,
                    )
                    continue

                # Deterministic checks are authoritative and cannot be
                # downgraded by a model's pass/needs_review output.
                if task.deterministic_failures:
                    if terminal.result.successful_read_calls:
                        self._append_model_evidence(collected, last_model)
                    child_job.publish_lifecycle(
                        "validation.round.completed",
                        {
                            "validation_id": validation_id,
                            "round": round_number,
                            "verdict": "fail",
                            "evidence_count": len(last_model.evidence),
                            "tokens_used": terminal.result.tokens_used,
                        },
                        checkpoint_id=checkpoint_id,
                    )
                    deterministic = self._deterministic_evidence(
                        task.deterministic_failures
                    )
                    record = self._record(
                        validation_id=validation_id,
                        verdict="fail",
                        reason="deterministic_failure",
                        source=source.contract,
                        rounds_used=rounds_used,
                        tokens_used=tokens_used,
                        started=started,
                        limits=task.budget,
                        summary=(
                            f"{len(task.deterministic_failures)} deterministic "
                            "validation failure(s) cannot be overridden by the Agent."
                        ),
                        evidence=self._prioritize_deterministic(
                            deterministic,
                            tuple(collected),
                        ),
                        child_session_ids=tuple(child_session_ids),
                    )
                    return await self._publish_complete(
                        parent_job,
                        record,
                        audit_outcome="success",
                    )

                # A model assertion is not evidence. A decisive verdict must
                # be grounded in at least one successful read-only tool result
                # observed by the server in this exact child round.
                if (
                    last_model.verdict in {"pass", "fail"}
                    and terminal.result.successful_read_calls == 0
                ):
                    last_model = None
                    last_reason = "unverified_evidence"
                    last_summary = (
                        "The Agent returned a decisive verdict without any "
                        "server-observed read-only evidence collection."
                    )
                    self._append_runtime_evidence(
                        collected,
                        source="validation_evidence_attestation",
                        summary=last_summary,
                    )
                    child_job.publish_lifecycle(
                        "validation.round.failed",
                        {
                            "validation_id": validation_id,
                            "round": round_number,
                            "reason": last_reason,
                        },
                        checkpoint_id=checkpoint_id,
                    )
                    continue

                self._append_model_evidence(collected, last_model)
                child_job.publish_lifecycle(
                    "validation.round.completed",
                    {
                        "validation_id": validation_id,
                        "round": round_number,
                        "verdict": last_model.verdict,
                        "evidence_count": len(last_model.evidence),
                        "tokens_used": terminal.result.tokens_used,
                    },
                    checkpoint_id=checkpoint_id,
                )

                if last_model.verdict in {"pass", "fail"}:
                    record = self._record(
                        validation_id=validation_id,
                        verdict=last_model.verdict,
                        reason="model_verdict",
                        source=source.contract,
                        rounds_used=rounds_used,
                        tokens_used=tokens_used,
                        started=started,
                        limits=task.budget,
                        summary=last_model.summary,
                        evidence=tuple(collected),
                        child_session_ids=tuple(child_session_ids),
                    )
                    return await self._publish_complete(
                        parent_job,
                        record,
                        audit_outcome="success",
                    )

                last_reason = "model_verdict"
                last_summary = last_model.summary

            # All server-owned rounds are consumed. Opening another child/root
            # session is not an escape hatch because this loop is the only
            # round allocator and the source remains bound to the checkpoint.
            if task.deterministic_failures:
                deterministic = self._deterministic_evidence(
                    task.deterministic_failures
                )
                record = self._record(
                    validation_id=validation_id,
                    verdict="fail",
                    reason="deterministic_failure",
                    source=source.contract,
                    rounds_used=rounds_used,
                    tokens_used=tokens_used,
                    started=started,
                    limits=task.budget,
                    summary=(
                        f"{len(task.deterministic_failures)} deterministic "
                        "validation failure(s) cannot be overridden by the Agent."
                    ),
                    evidence=self._prioritize_deterministic(
                        deterministic,
                        tuple(collected),
                    ),
                    child_session_ids=tuple(child_session_ids),
                )
            else:
                record = self._record(
                    validation_id=validation_id,
                    verdict="needs_review",
                    reason=last_reason,
                    source=source.contract,
                    rounds_used=rounds_used,
                    tokens_used=tokens_used,
                    started=started,
                    limits=task.budget,
                    summary=last_summary,
                    evidence=tuple(collected),
                    child_session_ids=tuple(child_session_ids),
                )
            return await self._publish_complete(
                parent_job,
                record,
                audit_outcome="success",
            )
        except asyncio.CancelledError:
            parent_job.publish_lifecycle(
                "validation.cancelled",
                {"validation_id": validation_id, "rounds_used": rounds_used},
                checkpoint_id=checkpoint_id,
            )
            await self._audit(
                validation_id=validation_id,
                parent_job=parent_job,
                checkpoint_id=checkpoint_id,
                outcome="cancelled",
                required=False,
                details={"rounds_used": rounds_used},
            )
            raise

    async def _resolve_source(
        self,
        parent_job: GenerationJob,
        checkpoint_id: str,
    ) -> _ResolvedSource:
        checkpoint_id = str(checkpoint_id).strip()
        if not checkpoint_id:
            raise ValidationSourceError("checkpoint_id cannot be blank")
        workspace_instance_id = parent_job.workspace_instance_id
        if not workspace_instance_id:
            raise ValidationSourceError(
                "parent job has no server-owned workspace instance"
            )
        async with self._session_factory() as db:
            session = await db.get(Session, parent_job.session_id)
            checkpoint = await db.get(SessionCheckpoint, checkpoint_id)
            workspace = await db.get(WorkspaceInstance, workspace_instance_id)
        if session is None:
            raise ValidationSourceError("parent session does not exist")
        if checkpoint is None:
            raise ValidationSourceError("checkpoint does not exist")
        if workspace is None or workspace.status != "active":
            raise ValidationSourceError("workspace instance is not active")
        if checkpoint.state != "finalized" or checkpoint.pin_state != "pinned":
            raise ValidationSourceError("checkpoint is not a finalized pinned source")
        if checkpoint.session_id != parent_job.session_id:
            raise ValidationSourceError("checkpoint belongs to another session")
        if checkpoint.root_turn_id != parent_job.root_turn_id:
            raise ValidationSourceError("checkpoint belongs to another root turn")
        if checkpoint.workspace_instance_id != workspace_instance_id:
            raise ValidationSourceError("checkpoint belongs to another workspace")
        try:
            canonical_path, identity_token = inspect_workspace_identity(
                workspace.root_path
            )
        except CheckpointValidationError as exc:
            raise ValidationSourceError("workspace identity is unavailable") from exc
        if (
            canonical_path != workspace.root_path
            or identity_token != workspace.identity_token
        ):
            raise ValidationSourceError("workspace filesystem identity changed")
        canonical = validate_agent_workspace_root(canonical_path)
        if not canonical.is_dir():
            raise ValidationSourceError("workspace root is not a directory")
        try:
            session_workspace = Path(session.directory).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValidationSourceError("session workspace is unavailable") from exc
        if session_workspace != canonical:
            raise ValidationSourceError(
                "session and workspace instance roots do not match"
            )
        return _ResolvedSource(
            contract=ValidationSource(
                session_id=parent_job.session_id,
                root_turn_id=parent_job.root_turn_id,
                checkpoint_id=checkpoint_id,
                workspace_instance_id=workspace_instance_id,
            ),
            workspace=str(canonical),
            project_id=session.project_id,
            model_id=session.model_id,
            provider_id=session.provider_id,
        )

    async def _prepare_round(
        self,
        *,
        parent_job: GenerationJob,
        validation_id: str,
        source: _ResolvedSource,
        task: ValidationTask,
        round_number: int,
        remaining_tokens: int,
    ) -> tuple[GenerationJob, ValidationRoundContext]:
        child_session_id = generate_ulid()
        async with self._session_factory() as db:
            async with db.begin():
                await create_session(
                    db,
                    id=child_session_id,
                    project_id=source.project_id,
                    parent_id=parent_job.session_id,
                    directory=source.workspace,
                    title=f"Validation {validation_id[:10]} r{round_number}",
                )

        child_job = GenerationJob(
            stream_id=generate_ulid(),
            session_id=child_session_id,
            language=parent_job.language,
            invocation_source="validator",
            invocation_source_id=validation_id,
        )
        child_job.inherit_runtime_context(parent_job)
        child_job.interactive = False
        child_job._depth = getattr(parent_job, "_depth", 0) + 1
        if (
            child_job.root_turn_id != source.contract.root_turn_id
            or child_job.workspace_instance_id
            != source.contract.workspace_instance_id
            or child_job.parent_turn_id != parent_job.turn_run_id
        ):
            raise ValidationSourceError("child runtime provenance changed unexpectedly")

        agent_registry = build_validation_agent_registry()
        agent = agent_registry.get("validator")
        assert agent is not None
        permission_rules = [
            rule.model_dump(mode="json") for rule in agent.permissions.rules
        ]
        request = PromptRequest(
            session_id=child_session_id,
            text=self._round_prompt(
                source=source.contract,
                task=task,
                round_number=round_number,
            ),
            model=source.model_id,
            provider_id=source.provider_id,
            agent="validator",
            workspace=source.workspace,
            permission_presets=None,
            permission_rules=permission_rules,
            reasoning=False,
            language=parent_job.language,
        )
        request._permission_rules_authoritative = True
        request._max_output_tokens_ceiling = remaining_tokens
        return child_job, ValidationRoundContext(
            validation_id=validation_id,
            round=round_number,
            remaining_tokens=remaining_tokens,
            job=child_job,
            request=request,
            agent_registry=agent_registry,
            tool_registry=build_validation_tool_registry(),
            index_manager=self._index_manager,
        )

    @staticmethod
    def _round_prompt(
        *,
        source: ValidationSource,
        task: ValidationTask,
        round_number: int,
    ) -> str:
        schema = {
            "schema_version": 1,
            "verdict": "pass | fail | needs_review",
            "summary": "non-empty string",
            "evidence": [
                {
                    "kind": "file | search | observation",
                    "source": "workspace-relative path or read-only observation",
                    "summary": "what the evidence proves",
                }
            ],
        }
        deterministic = [
            failure.model_dump(mode="json")
            for failure in task.deterministic_failures
        ]
        return (
            "Perform read-only validation for the server-owned source below. "
            "The identifiers, workspace, permissions, budget, and round are "
            "immutable and are not fields in your output. Do not treat a tool "
            "call, permission request, delegated task, or your own assertion as "
            "successful validation.\n\n"
            f"Source: {json.dumps(source.model_dump(mode='json'), ensure_ascii=True)}\n"
            f"Round: {round_number}/{task.budget.max_rounds}\n"
            f"Objective: {task.objective}\n"
            "Authoritative deterministic failures (you may add context but "
            f"cannot clear them): {json.dumps(deterministic, ensure_ascii=True)}\n\n"
            "Return exactly one plain JSON object matching this v1 shape: "
            f"{json.dumps(schema, ensure_ascii=True)}"
        )

    async def _await_round(
        self,
        *,
        parent_job: GenerationJob,
        child_job: GenerationJob,
        context: ValidationRoundContext,
        timeout_seconds: float,
    ) -> _RoundTerminal:
        runner_task = asyncio.create_task(
            self._runner.run_round(context),
            name=f"validation-{context.validation_id}-round-{context.round}",
        )
        abort_task = asyncio.create_task(parent_job.abort_event.wait())
        try:
            done, _pending = await asyncio.wait(
                {runner_task, abort_task},
                timeout=max(0.0, timeout_seconds),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if abort_task in done and parent_job.abort_event.is_set():
                child_job.abort()
                runner_task.cancel()
                await asyncio.gather(runner_task, return_exceptions=True)
                return _RoundTerminal("cancelled")
            if runner_task not in done:
                child_job.abort()
                runner_task.cancel()
                await asyncio.gather(runner_task, return_exceptions=True)
                return _RoundTerminal("timeout")
            try:
                return _RoundTerminal("result", await runner_task)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "Validation runner failed in round %s",
                    context.round,
                    exc_info=True,
                )
                return _RoundTerminal("runner_error")
        except asyncio.CancelledError:
            child_job.abort()
            runner_task.cancel()
            await asyncio.gather(runner_task, return_exceptions=True)
            raise
        finally:
            abort_task.cancel()
            await asyncio.gather(abort_task, return_exceptions=True)
            if not child_job.completed:
                child_job.complete()

    @staticmethod
    def _append_model_evidence(
        collected: list[ValidationEvidence],
        verdict: ValidationModelVerdict,
    ) -> None:
        for evidence in verdict.evidence:
            if len(collected) >= 32:
                break
            collected.append(
                ValidationEvidence(
                    evidence_id=generate_ulid(),
                    origin="validator",
                    kind=evidence.kind,
                    source=evidence.source,
                    summary=evidence.summary,
                )
            )

    @staticmethod
    def _append_runtime_evidence(
        collected: list[ValidationEvidence],
        *,
        source: str,
        summary: str,
    ) -> None:
        if len(collected) >= 32:
            return
        collected.append(
            ValidationEvidence(
                evidence_id=generate_ulid(),
                origin="runtime",
                kind="runtime",
                source=source,
                summary=summary,
            )
        )

    @staticmethod
    def _deterministic_evidence(
        failures: tuple[DeterministicValidationFailure, ...],
    ) -> tuple[ValidationEvidence, ...]:
        return tuple(
            ValidationEvidence(
                evidence_id=generate_ulid(),
                origin="deterministic",
                kind="deterministic_failure",
                source=failure.source,
                summary=f"{failure.code}: {failure.summary}"[:4_000],
            )
            for failure in failures
        )

    @staticmethod
    def _prioritize_deterministic(
        deterministic: tuple[ValidationEvidence, ...],
        other: tuple[ValidationEvidence, ...],
    ) -> tuple[ValidationEvidence, ...]:
        available = max(0, 32 - len(deterministic))
        return deterministic + other[:available]

    @staticmethod
    def _record(
        *,
        validation_id: str,
        verdict: str,
        reason: str,
        source: ValidationSource,
        rounds_used: int,
        tokens_used: int,
        started: float,
        limits: ValidationBudgetLimits,
        summary: str,
        evidence: tuple[ValidationEvidence, ...],
        child_session_ids: tuple[str, ...],
    ) -> ValidationVerdictRecord:
        return ValidationVerdictRecord(
            schema_version=VALIDATION_CONTRACT_VERSION,
            validation_id=validation_id,
            verdict=verdict,
            reason_code=reason,
            source=source,
            round=rounds_used,
            budget=ValidationBudgetReport(
                max_rounds=limits.max_rounds,
                max_tokens=limits.max_tokens,
                timeout_ms=limits.timeout_ms,
                rounds_used=rounds_used,
                tokens_used=tokens_used,
                elapsed_ms=max(0, round((time.monotonic() - started) * 1_000)),
            ),
            summary=summary,
            evidence=evidence[:32],
            validator_session_ids=child_session_ids,
        )

    async def _finish_fail_closed(
        self,
        *,
        parent_job: GenerationJob,
        validation_id: str,
        source: ValidationSource,
        budget: ValidationBudgetLimits,
        rounds_used: int,
        tokens_used: int,
        started: float,
        child_session_ids: tuple[str, ...],
        evidence: tuple[ValidationEvidence, ...],
        deterministic_failures: tuple[DeterministicValidationFailure, ...],
        reason: str,
        summary: str,
        audit_outcome: str,
    ) -> ValidationVerdictRecord:
        mutable = list(evidence)
        self._append_runtime_evidence(
            mutable,
            source=f"validation_{reason}",
            summary=summary,
        )
        deterministic = self._deterministic_evidence(deterministic_failures)
        verdict = "fail" if deterministic else "needs_review"
        record_reason = "deterministic_failure" if deterministic else reason
        record_summary = (
            f"{len(deterministic_failures)} deterministic validation failure(s) "
            "remain authoritative despite the runtime interruption."
            if deterministic
            else summary
        )
        record = self._record(
            validation_id=validation_id,
            verdict=verdict,
            reason=record_reason,
            source=source,
            rounds_used=rounds_used,
            tokens_used=tokens_used,
            started=started,
            limits=budget,
            summary=record_summary,
            evidence=self._prioritize_deterministic(
                deterministic,
                tuple(mutable),
            ),
            child_session_ids=child_session_ids,
        )
        return await self._publish_complete(
            parent_job,
            record,
            audit_outcome=audit_outcome,
        )

    async def _publish_complete(
        self,
        parent_job: GenerationJob,
        record: ValidationVerdictRecord,
        *,
        audit_outcome: str,
    ) -> ValidationVerdictRecord:
        parent_job.publish_lifecycle(
            "validation.completed",
            {
                "validation_id": record.validation_id,
                "verdict": record.verdict,
                "reason": record.reason_code,
                "rounds_used": record.round,
                "tokens_used": record.budget.tokens_used,
                "evidence_count": len(record.evidence),
            },
            checkpoint_id=record.source.checkpoint_id,
        )
        await self._audit(
            validation_id=record.validation_id,
            parent_job=parent_job,
            checkpoint_id=record.source.checkpoint_id,
            outcome=audit_outcome,
            required=False,
            details={
                "verdict": record.verdict,
                "reason": record.reason_code,
                "rounds_used": record.round,
                "evidence_count": len(record.evidence),
            },
        )
        return record

    async def _audit(
        self,
        *,
        validation_id: str,
        parent_job: GenerationJob,
        checkpoint_id: str,
        outcome: str,
        required: bool,
        details: dict[str, Any] | None = None,
    ) -> None:
        safe_details: dict[str, Any] = {
            "root_turn_id": parent_job.root_turn_id,
            "checkpoint_id": checkpoint_id,
        }
        if details:
            safe_details.update(details)
        await record_security_event(
            self._session_factory,
            source_kind="validator",
            source_id=validation_id,
            invocation_source_kind="validator",
            invocation_source_id=validation_id,
            capability="model_inference",
            action="validate",
            decision="system",
            outcome=outcome,
            session_id=parent_job.session_id,
            call_id=validation_id,
            details=safe_details,
            required=required,
        )


__all__ = [
    "SessionPromptValidationRunner",
    "ValidationAgentService",
    "ValidationAgentUnavailable",
    "ValidationRoundContext",
    "ValidationRoundResult",
    "ValidationRoundRunner",
    "ValidationRunnerError",
    "ValidationSourceError",
    "build_validation_agent_registry",
    "build_validation_tool_registry",
    "validation_agent_enabled",
]
