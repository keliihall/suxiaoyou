"""Server-owned Office validation and bounded repair orchestration.

The orchestrator never edits an Office file.  It captures identities through
``OfficePreviewService``, runs deterministic structure/visual gates, may ask an
independent read-only validation Agent for supplemental evidence, and delegates
each repair to an explicitly supplied callback.  The callback receives no
approval authority: only a fresh deterministic pass can approve the result.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import re
import time
from typing import Awaitable, Callable, Literal, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.checkpoint_change import CheckpointChange
from app.models.session_checkpoint import SessionCheckpoint
from app.office_rendering.errors import OfficeRenderingError
from app.office_rendering.models import AUTHORITATIVE_QUALITY, RenderManifest
from app.office_rendering.service import (
    OfficePreviewBinding,
    OfficePreviewError,
    OfficePreviewService,
    OfficePreviewValidationSnapshot,
)
from app.office_validation.errors import (
    OfficeValidationContractError,
    OfficeValidationError,
    OfficeValidationSecurityError,
)
from app.office_validation.models import (
    ValidationCheck,
    ValidationVerdict,
    derive_verdict,
)
from app.office_validation.structure import (
    OOXMLPartManifest,
    compare_ooxml_parts,
    inspect_ooxml_path,
)
from app.office_validation.visual import VisualDiffPolicy, compare_rendered_pages
from app.streaming.manager import GenerationJob
from app.utils.id import generate_ulid
from app.validation_agent.contracts import (
    DeterministicValidationFailure,
    ValidationBudgetLimits,
    ValidationTask,
    ValidationVerdictRecord,
)


OFFICE_VALIDATION_LOOP_SCHEMA_VERSION = 1
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_REPAIR_ROUNDS = 2


class OfficeValidationStaleError(OfficeValidationError):
    """A source, cache, workspace, checkpoint, or rewind identity changed."""


class OfficeValidationBusyError(OfficeValidationError):
    """The same server-owned baseline already has an active validation run."""


class OfficeValidationCancelledError(OfficeValidationError):
    """The caller's cancellation boundary stopped validation or repair."""


class OfficeValidationBudgetError(OfficeValidationError):
    """The server-owned wall-clock budget was exhausted."""


def _bounded(value: object, field: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str):
        raise OfficeValidationContractError(f"{field} must be text")
    text = value.strip()
    if (
        not text
        or len(text) > maximum
        or any(ord(character) < 32 for character in text)
    ):
        raise OfficeValidationContractError(f"{field} is invalid")
    return text


def _sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise OfficeValidationContractError(f"{field} must be a lowercase SHA-256")
    return value


@dataclass(frozen=True, slots=True)
class OfficeSourceIdentity:
    """Path-free, server-observed source/render/checkpoint identity."""

    session_id: str
    workspace_instance_id: str
    relative_path: str
    source_sha256: str
    checkpoint_id: str
    root_turn_id: str
    cache_key: str
    renderer_id: str
    renderer_version: str
    font_digest: str
    parameters_version: str
    parameters_sha256: str
    quality: Literal["authoritative", "approximate"]

    def __post_init__(self) -> None:
        for field in (
            "session_id",
            "workspace_instance_id",
            "checkpoint_id",
            "root_turn_id",
        ):
            object.__setattr__(self, field, _bounded(getattr(self, field), field, maximum=200))
        object.__setattr__(
            self,
            "relative_path",
            _bounded(self.relative_path, "relative_path"),
        )
        for field in (
            "source_sha256",
            "cache_key",
            "font_digest",
            "parameters_sha256",
        ):
            object.__setattr__(self, field, _sha256(getattr(self, field), field))
        for field in ("renderer_id", "renderer_version", "parameters_version"):
            object.__setattr__(
                self,
                field,
                _bounded(getattr(self, field), field, maximum=256),
            )
        if self.quality not in {"authoritative", "approximate"}:
            raise OfficeValidationContractError("render quality is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "workspace_instance_id": self.workspace_instance_id,
            "relative_path": self.relative_path,
            "source_sha256": self.source_sha256,
            "checkpoint_id": self.checkpoint_id,
            "root_turn_id": self.root_turn_id,
            "cache_key": self.cache_key,
            "renderer_id": self.renderer_id,
            "renderer_version": self.renderer_version,
            "font_digest": self.font_digest,
            "parameters_version": self.parameters_version,
            "parameters_sha256": self.parameters_sha256,
            "quality": self.quality,
        }


@dataclass(frozen=True, slots=True)
class OfficeValidationPolicy:
    """Server-selected mutations, visual regions, and hard budgets."""

    allowed_changed_parts: tuple[str, ...]
    visual: VisualDiffPolicy
    max_repair_rounds: int = _MAX_REPAIR_ROUNDS
    timeout_ms: int = 60_000
    validator_budget: ValidationBudgetLimits = ValidationBudgetLimits(
        max_rounds=1,
        max_tokens=4_000,
        timeout_ms=30_000,
    )

    def __post_init__(self) -> None:
        try:
            patterns = tuple(self.allowed_changed_parts)
        except TypeError as exc:
            raise OfficeValidationContractError(
                "allowed changed parts are invalid"
            ) from exc
        if not isinstance(self.visual, VisualDiffPolicy):
            raise OfficeValidationContractError("visual policy is invalid")
        if not self.visual.require_authoritative:
            raise OfficeValidationContractError(
                "orchestrated validation always requires authoritative rendering"
            )
        if (
            not isinstance(self.max_repair_rounds, int)
            or isinstance(self.max_repair_rounds, bool)
            or not 0 <= self.max_repair_rounds <= _MAX_REPAIR_ROUNDS
        ):
            raise OfficeValidationContractError(
                "max_repair_rounds must be between zero and two"
            )
        if (
            not isinstance(self.timeout_ms, int)
            or isinstance(self.timeout_ms, bool)
            or not 50 <= self.timeout_ms <= 300_000
        ):
            raise OfficeValidationContractError("validation timeout is invalid")
        if not isinstance(self.validator_budget, ValidationBudgetLimits):
            raise OfficeValidationContractError("validator budget is invalid")
        object.__setattr__(self, "allowed_changed_parts", patterns)


@dataclass(frozen=True, slots=True)
class OfficeBaselineHandle:
    """Opaque handle whose identity is checked against server-owned state."""

    baseline_id: str
    source: OfficeSourceIdentity

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "baseline_id",
            _bounded(self.baseline_id, "baseline_id", maximum=128),
        )
        if not isinstance(self.source, OfficeSourceIdentity):
            raise OfficeValidationContractError("baseline source is invalid")


@dataclass(frozen=True, slots=True)
class OfficeValidationLoopReport:
    """Structured result; ``pass`` always means an authoritative fresh pass."""

    validation_id: str
    baseline: OfficeSourceIdentity
    candidate: OfficeSourceIdentity
    verdict: ValidationVerdict
    reason_code: Literal[
        "authoritative_pass",
        "deterministic_failure",
        "review_required",
        "repair_limit_reached",
    ]
    checks: tuple[ValidationCheck, ...]
    repair_rounds_used: int
    repair_rounds_remaining: int
    validator_record: ValidationVerdictRecord | None = None
    schema_version: int = OFFICE_VALIDATION_LOOP_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != OFFICE_VALIDATION_LOOP_SCHEMA_VERSION:
            raise OfficeValidationContractError("unsupported validation loop schema")
        _bounded(self.validation_id, "validation_id", maximum=128)
        if not isinstance(self.baseline, OfficeSourceIdentity) or not isinstance(
            self.candidate, OfficeSourceIdentity
        ):
            raise OfficeValidationContractError("validation source identity is invalid")
        checks = tuple(self.checks)
        if not checks or any(not isinstance(item, ValidationCheck) for item in checks):
            raise OfficeValidationContractError("validation loop checks are invalid")
        if self.verdict != derive_verdict(checks):
            raise OfficeValidationContractError(
                "validation loop verdict contradicts deterministic checks"
            )
        if (
            not isinstance(self.repair_rounds_used, int)
            or isinstance(self.repair_rounds_used, bool)
            or not 0 <= self.repair_rounds_used <= _MAX_REPAIR_ROUNDS
            or not isinstance(self.repair_rounds_remaining, int)
            or isinstance(self.repair_rounds_remaining, bool)
            or not 0 <= self.repair_rounds_remaining <= _MAX_REPAIR_ROUNDS
        ):
            raise OfficeValidationContractError("repair round accounting is invalid")
        authoritative = (
            self.baseline.quality == AUTHORITATIVE_QUALITY
            and self.candidate.quality == AUTHORITATIVE_QUALITY
        )
        if self.verdict == "pass" and (
            not authoritative or self.reason_code != "authoritative_pass"
        ):
            raise OfficeValidationContractError(
                "only a deterministic authoritative result can pass"
            )
        if self.validator_record is not None and not isinstance(
            self.validator_record, ValidationVerdictRecord
        ):
            raise OfficeValidationContractError("validator record is invalid")
        object.__setattr__(self, "checks", checks)

    @property
    def authoritative_pass(self) -> bool:
        return self.verdict == "pass"

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "validation_id": self.validation_id,
            "baseline": self.baseline.to_dict(),
            "candidate": self.candidate.to_dict(),
            "verdict": self.verdict,
            "authoritative_pass": self.authoritative_pass,
            "reason_code": self.reason_code,
            "repair_rounds_used": self.repair_rounds_used,
            "repair_rounds_remaining": self.repair_rounds_remaining,
            "checks": [item.to_dict() for item in self.checks],
            "validator_record": (
                self.validator_record.model_dump(mode="json")
                if self.validator_record is not None
                else None
            ),
        }


@dataclass(frozen=True, slots=True)
class OfficeRepairAttemptRequest:
    """Read-only context handed to an externally authorized repair boundary."""

    validation_id: str
    repair_round: int
    baseline: OfficeSourceIdentity
    candidate: OfficeSourceIdentity
    report: OfficeValidationLoopReport
    remaining_time_ms: int


@dataclass(frozen=True, slots=True)
class OfficeRepairAttemptReceipt:
    """Untrusted callback claim that is re-resolved and checked by the server."""

    source_sha256: str
    checkpoint_id: str
    root_turn_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_sha256",
            _sha256(self.source_sha256, "source_sha256"),
        )
        object.__setattr__(
            self,
            "checkpoint_id",
            _bounded(self.checkpoint_id, "checkpoint_id", maximum=200),
        )
        object.__setattr__(
            self,
            "root_turn_id",
            _bounded(self.root_turn_id, "root_turn_id", maximum=200),
        )


OfficeRepairCallback = Callable[
    [OfficeRepairAttemptRequest], Awaitable[OfficeRepairAttemptReceipt]
]


class IndependentOfficeValidator(Protocol):
    async def validate(
        self,
        *,
        parent_job: GenerationJob,
        checkpoint_id: str,
        task: ValidationTask,
    ) -> ValidationVerdictRecord: ...


@dataclass(frozen=True, slots=True)
class _CapturedArtifact:
    identity: OfficeSourceIdentity
    structural: OOXMLPartManifest
    manifest: RenderManifest
    entry_path: Path


@dataclass(frozen=True, slots=True)
class _BaselineRecord:
    artifact: _CapturedArtifact
    policy: OfficeValidationPolicy


@dataclass(slots=True)
class _StateItem:
    record: _BaselineRecord
    active: bool = False
    repair_rounds_used: int = 0


class ServerOwnedOfficeValidationState:
    """Process-owned baseline registry and atomic repair-round allocator.

    The state cannot be supplied or reset by model output.  It survives repeat
    calls for the lifetime of this service instance, so reopening a run does
    not grant more than two total repair callbacks.  Deployments needing crash
    persistence can replace this class at the application composition boundary
    without changing the orchestrator's callback authority model.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._items: dict[str, _StateItem] = {}

    async def register(self, record: _BaselineRecord) -> OfficeBaselineHandle:
        baseline_id = generate_ulid()
        async with self._lock:
            self._items[baseline_id] = _StateItem(record=record)
        return OfficeBaselineHandle(
            baseline_id=baseline_id,
            source=record.artifact.identity,
        )

    async def acquire(self, handle: OfficeBaselineHandle) -> _BaselineRecord:
        if not isinstance(handle, OfficeBaselineHandle):
            raise OfficeValidationContractError("baseline handle is invalid")
        async with self._lock:
            item = self._items.get(handle.baseline_id)
            if item is None or item.record.artifact.identity != handle.source:
                raise OfficeValidationStaleError(
                    "Office validation baseline is missing or was tampered with"
                )
            if item.active:
                raise OfficeValidationBusyError(
                    "Office validation baseline already has an active run"
                )
            item.active = True
            return item.record

    async def release(self, baseline_id: str) -> None:
        async with self._lock:
            item = self._items.get(baseline_id)
            if item is not None:
                item.active = False

    async def claim_repair_round(
        self,
        baseline_id: str,
        *,
        maximum: int,
    ) -> int | None:
        async with self._lock:
            item = self._items.get(baseline_id)
            if item is None or not item.active:
                raise OfficeValidationStaleError(
                    "Office validation state is no longer active"
                )
            if item.repair_rounds_used >= maximum:
                return None
            item.repair_rounds_used += 1
            return item.repair_rounds_used

    async def accounting(self, baseline_id: str, *, maximum: int) -> tuple[int, int]:
        async with self._lock:
            item = self._items.get(baseline_id)
            if item is None:
                raise OfficeValidationStaleError(
                    "Office validation state is unavailable"
                )
            used = item.repair_rounds_used
            return used, max(0, maximum - used)


class OfficeValidationOrchestrator:
    """Deterministic high-fidelity validation with at most two repair callbacks."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        preview_service: OfficePreviewService,
        state: ServerOwnedOfficeValidationState | None = None,
        validator: IndependentOfficeValidator | None = None,
    ) -> None:
        if not isinstance(preview_service, OfficePreviewService):
            raise TypeError("preview_service must be OfficePreviewService")
        self._session_factory = session_factory
        self._preview = preview_service
        self._state = state or ServerOwnedOfficeValidationState()
        self._validator = validator

    async def capture_baseline(
        self,
        *,
        session_id: str,
        workspace_instance_id: str,
        relative_path: str,
        policy: OfficeValidationPolicy,
        expected_source_sha256: str | None = None,
    ) -> OfficeBaselineHandle:
        """Capture immutable structure/render evidence under server-owned policy."""

        if not isinstance(policy, OfficeValidationPolicy):
            raise OfficeValidationContractError("validation policy is invalid")
        artifact = await self._capture_current(
            session_id=session_id,
            workspace_instance_id=workspace_instance_id,
            relative_path=relative_path,
            expected_source_sha256=expected_source_sha256,
            deadline=time.monotonic() + policy.timeout_ms / 1_000,
            cancel_event=None,
        )
        # Validate the allow-list now and retain only this server-owned copy.
        compare_ooxml_parts(
            artifact.structural,
            artifact.structural,
            allowed_changed_parts=policy.allowed_changed_parts,
        )
        return await self._state.register(
            _BaselineRecord(artifact=artifact, policy=policy)
        )

    async def validate_and_repair(
        self,
        handle: OfficeBaselineHandle,
        *,
        repair: OfficeRepairCallback | None = None,
        parent_job: GenerationJob | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> OfficeValidationLoopReport:
        """Validate current bytes and optionally cross a bounded repair callback."""

        record = await self._state.acquire(handle)
        validation_id = generate_ulid()
        deadline = time.monotonic() + record.policy.timeout_ms / 1_000
        try:
            candidate = await self._capture_current(
                session_id=record.artifact.identity.session_id,
                workspace_instance_id=record.artifact.identity.workspace_instance_id,
                relative_path=record.artifact.identity.relative_path,
                expected_source_sha256=None,
                deadline=deadline,
                cancel_event=cancel_event,
            )
            while True:
                report = await self._evaluate(
                    validation_id=validation_id,
                    baseline=record.artifact,
                    candidate=candidate,
                    policy=record.policy,
                    baseline_id=handle.baseline_id,
                    parent_job=parent_job,
                    deadline=deadline,
                    cancel_event=cancel_event,
                )
                if report.verdict == "pass" or repair is None:
                    return report

                repair_round = await self._state.claim_repair_round(
                    handle.baseline_id,
                    maximum=record.policy.max_repair_rounds,
                )
                if repair_round is None:
                    return await self._with_repair_limit(
                        report,
                        baseline_id=handle.baseline_id,
                        maximum=record.policy.max_repair_rounds,
                    )

                remaining_ms = self._remaining_ms(deadline, cancel_event)
                request = OfficeRepairAttemptRequest(
                    validation_id=validation_id,
                    repair_round=repair_round,
                    baseline=record.artifact.identity,
                    candidate=candidate.identity,
                    report=report,
                    remaining_time_ms=remaining_ms,
                )
                receipt = await self._await_controlled(
                    repair(request),
                    deadline=deadline,
                    cancel_event=cancel_event,
                )
                if not isinstance(receipt, OfficeRepairAttemptReceipt):
                    raise OfficeValidationContractError(
                        "repair callback returned an invalid receipt"
                    )
                if receipt.source_sha256 == candidate.identity.source_sha256:
                    raise OfficeValidationStaleError(
                        "repair callback did not produce a new source identity"
                    )
                repaired = await self._capture_current(
                    session_id=record.artifact.identity.session_id,
                    workspace_instance_id=record.artifact.identity.workspace_instance_id,
                    relative_path=record.artifact.identity.relative_path,
                    expected_source_sha256=receipt.source_sha256,
                    deadline=deadline,
                    cancel_event=cancel_event,
                )
                if (
                    repaired.identity.checkpoint_id != receipt.checkpoint_id
                    or repaired.identity.root_turn_id != receipt.root_turn_id
                ):
                    raise OfficeValidationStaleError(
                        "repair receipt does not match the server checkpoint"
                    )
                candidate = repaired
        finally:
            await self._state.release(handle.baseline_id)

    async def _capture_current(
        self,
        *,
        session_id: str,
        workspace_instance_id: str,
        relative_path: str,
        expected_source_sha256: str | None,
        deadline: float,
        cancel_event: asyncio.Event | None,
    ) -> _CapturedArtifact:
        try:
            binding = await self._await_controlled(
                self._preview.render(
                    session_id=session_id,
                    workspace_instance_id=workspace_instance_id,
                    relative_path=relative_path,
                    expected_source_sha256=expected_source_sha256,
                ),
                deadline=deadline,
                cancel_event=cancel_event,
            )
        except (OfficePreviewError, OfficeRenderingError) as exc:
            raise OfficeValidationStaleError(
                "Office source or render cache is no longer current"
            ) from exc
        if binding.checkpoint_id is None or binding.root_turn_id is None:
            raise OfficeValidationStaleError(
                "Office source is not bound to a finalized checkpoint"
            )
        try:
            snapshot = await self._validation_snapshot(binding, deadline, cancel_event)
        except (OfficePreviewError, OfficeRenderingError) as exc:
            raise OfficeValidationStaleError(
                "Office validation snapshot is no longer current"
            ) from exc
        identity = self._identity(snapshot.binding)
        await self._assert_checkpoint_fresh(identity)
        try:
            structural = inspect_ooxml_path(
                snapshot.source_path,
                snapshot.binding.manifest.document_format,
            )
        except (OfficeValidationSecurityError, OSError) as exc:
            raise OfficeValidationStaleError(
                "Office package changed during validation capture"
            ) from exc
        if structural.package_sha256 != identity.source_sha256:
            raise OfficeValidationStaleError(
                "Office structure manifest does not match source identity"
            )
        return _CapturedArtifact(
            identity=identity,
            structural=structural,
            manifest=snapshot.binding.manifest,
            entry_path=snapshot.entry_path,
        )

    async def _evaluate(
        self,
        *,
        validation_id: str,
        baseline: _CapturedArtifact,
        candidate: _CapturedArtifact,
        policy: OfficeValidationPolicy,
        baseline_id: str,
        parent_job: GenerationJob | None,
        deadline: float,
        cancel_event: asyncio.Event | None,
    ) -> OfficeValidationLoopReport:
        self._remaining_ms(deadline, cancel_event)
        await self._assert_checkpoint_fresh(
            baseline.identity,
            require_latest_path_version=False,
        )
        await self._assert_checkpoint_fresh(candidate.identity)
        structural = compare_ooxml_parts(
            baseline.structural,
            candidate.structural,
            allowed_changed_parts=policy.allowed_changed_parts,
        )
        rejected = structural.rejected_parts
        structural_check = ValidationCheck(
            code="structural_parts",
            outcome="pass" if structural.passed else "fail",
            message=(
                "OOXML changes are confined to server-approved parts."
                if structural.passed
                else "OOXML changed outside approved parts: "
                + ", ".join(rejected[:8])
            ),
            metrics=tuple(
                sorted(
                    {
                        "changed_parts": len(structural.changes),
                        "rejected_parts": len(rejected),
                    }.items()
                )
            ),
        )
        try:
            visual = compare_rendered_pages(
                baseline.manifest,
                baseline.entry_path,
                candidate.manifest,
                candidate.entry_path,
                policy.visual,
                checkpoint_id=candidate.identity.checkpoint_id,
                root_turn_id=candidate.identity.root_turn_id,
            )
        except (OfficeValidationSecurityError, OfficeRenderingError, OSError) as exc:
            raise OfficeValidationStaleError(
                "Office render evidence changed during comparison"
            ) from exc
        checks: list[ValidationCheck] = [structural_check, *visual.checks]

        validator_record: ValidationVerdictRecord | None = None
        if self._validator is not None:
            if parent_job is None:
                checks.append(
                    ValidationCheck(
                        code="independent_validator",
                        outcome="needs_review",
                        message=(
                            "Independent validation was configured without a "
                            "parent runtime source."
                        ),
                    )
                )
            else:
                validator_record, validator_check = await self._independent_evidence(
                    parent_job=parent_job,
                    candidate=candidate.identity,
                    checks=tuple(checks),
                    policy=policy,
                    deadline=deadline,
                    cancel_event=cancel_event,
                )
                checks.append(validator_check)

        # Close source/cache/checkpoint races after all deterministic and
        # optional model work.  The baseline cache pages were re-hashed by the
        # visual comparator; the current candidate is re-resolved in full.
        try:
            current = await self._validation_snapshot_from_identity(
                candidate.identity,
                deadline=deadline,
                cancel_event=cancel_event,
            )
        except (OfficePreviewError, OfficeRenderingError) as exc:
            raise OfficeValidationStaleError(
                "Office source or render cache changed during validation"
            ) from exc
        if current.binding.manifest != candidate.manifest:
            raise OfficeValidationStaleError(
                "Office render manifest changed during validation"
            )
        await self._assert_checkpoint_fresh(
            baseline.identity,
            require_latest_path_version=False,
        )
        await self._assert_checkpoint_fresh(candidate.identity)
        try:
            refreshed = inspect_ooxml_path(
                current.source_path,
                current.binding.manifest.document_format,
            )
        except (OfficeValidationSecurityError, OSError) as exc:
            raise OfficeValidationStaleError(
                "Office package changed during final validation"
            ) from exc
        if refreshed != candidate.structural:
            raise OfficeValidationStaleError(
                "Office package changed during deterministic validation"
            )

        frozen = tuple(checks)
        verdict = derive_verdict(frozen)
        if verdict == "fail":
            reason: Literal[
                "authoritative_pass",
                "deterministic_failure",
                "review_required",
                "repair_limit_reached",
            ] = "deterministic_failure"
        elif verdict == "needs_review":
            reason = "review_required"
        else:
            reason = "authoritative_pass"
        used, remaining = await self._state.accounting(
            baseline_id,
            maximum=policy.max_repair_rounds,
        )
        return OfficeValidationLoopReport(
            validation_id=validation_id,
            baseline=baseline.identity,
            candidate=candidate.identity,
            verdict=verdict,
            reason_code=reason,
            checks=frozen,
            repair_rounds_used=used,
            repair_rounds_remaining=remaining,
            validator_record=validator_record,
        )

    async def _independent_evidence(
        self,
        *,
        parent_job: GenerationJob,
        candidate: OfficeSourceIdentity,
        checks: tuple[ValidationCheck, ...],
        policy: OfficeValidationPolicy,
        deadline: float,
        cancel_event: asyncio.Event | None,
    ) -> tuple[ValidationVerdictRecord | None, ValidationCheck]:
        assert self._validator is not None
        failures = tuple(
            DeterministicValidationFailure(
                code=item.code,
                source=(
                    f"page:{item.box.page_number}"
                    if item.box is not None
                    else f"office:{item.code}"
                ),
                summary=item.message,
            )
            for item in checks
            if item.outcome == "fail"
        )
        task = ValidationTask(
            objective=(
                "Independently inspect the current Office artifact for semantic "
                "or layout anomalies. Report evidence only; deterministic gates "
                "and repair approval remain server-owned."
            ),
            deterministic_failures=failures,
            budget=policy.validator_budget,
        )
        try:
            record = await self._await_controlled(
                self._validator.validate(
                    parent_job=parent_job,
                    checkpoint_id=candidate.checkpoint_id,
                    task=task,
                ),
                deadline=deadline,
                cancel_event=cancel_event,
            )
        except (OfficeValidationCancelledError, OfficeValidationBudgetError):
            raise
        except Exception:
            return None, ValidationCheck(
                code="independent_validator",
                outcome="needs_review",
                message="Independent read-only validation failed closed.",
            )
        if not isinstance(record, ValidationVerdictRecord) or (
            record.source.session_id != candidate.session_id
            or record.source.workspace_instance_id != candidate.workspace_instance_id
            or record.source.checkpoint_id != candidate.checkpoint_id
            or record.source.root_turn_id != candidate.root_turn_id
        ):
            return None, ValidationCheck(
                code="independent_validator",
                outcome="needs_review",
                message="Independent validation evidence had stale provenance.",
            )
        if record.verdict == "pass":
            return record, ValidationCheck(
                code="independent_validator",
                outcome="pass",
                message=(
                    "Independent read-only evidence found no issue; it did not "
                    "approve or override deterministic checks."
                ),
                metrics=(("evidence_items", len(record.evidence)),),
            )
        return record, ValidationCheck(
            code="independent_validator",
            outcome="needs_review",
            message=(
                "Independent read-only evidence requested review; only a fresh "
                "deterministic pass can approve a repair."
            ),
            metrics=(("evidence_items", len(record.evidence)),),
        )

    async def _with_repair_limit(
        self,
        report: OfficeValidationLoopReport,
        *,
        baseline_id: str,
        maximum: int,
    ) -> OfficeValidationLoopReport:
        checks = (
            *report.checks,
            ValidationCheck(
                code="repair_round_limit",
                outcome="needs_review",
                message="The server-owned limit of two total repair rounds was reached.",
                metrics=(("maximum_repair_rounds", maximum),),
            ),
        )
        used, remaining = await self._state.accounting(
            baseline_id,
            maximum=maximum,
        )
        return OfficeValidationLoopReport(
            validation_id=report.validation_id,
            baseline=report.baseline,
            candidate=report.candidate,
            verdict=derive_verdict(checks),
            reason_code="repair_limit_reached",
            checks=checks,
            repair_rounds_used=used,
            repair_rounds_remaining=remaining,
            validator_record=report.validator_record,
        )

    async def _validation_snapshot(
        self,
        binding: OfficePreviewBinding,
        deadline: float,
        cancel_event: asyncio.Event | None,
    ) -> OfficePreviewValidationSnapshot:
        return await self._await_controlled(
            self._preview.validation_snapshot(
                session_id=binding.session_id,
                workspace_instance_id=binding.workspace_instance_id,
                relative_path=binding.relative_path,
                expected_source_sha256=binding.source_sha256,
                expected_cache_key=binding.manifest.cache_key,
                expected_checkpoint_id=binding.checkpoint_id,
                expected_root_turn_id=binding.root_turn_id,
            ),
            deadline=deadline,
            cancel_event=cancel_event,
        )

    async def _validation_snapshot_from_identity(
        self,
        identity: OfficeSourceIdentity,
        *,
        deadline: float,
        cancel_event: asyncio.Event | None,
    ) -> OfficePreviewValidationSnapshot:
        return await self._await_controlled(
            self._preview.validation_snapshot(
                session_id=identity.session_id,
                workspace_instance_id=identity.workspace_instance_id,
                relative_path=identity.relative_path,
                expected_source_sha256=identity.source_sha256,
                expected_cache_key=identity.cache_key,
                expected_checkpoint_id=identity.checkpoint_id,
                expected_root_turn_id=identity.root_turn_id,
            ),
            deadline=deadline,
            cancel_event=cancel_event,
        )

    @staticmethod
    def _identity(binding: OfficePreviewBinding) -> OfficeSourceIdentity:
        if binding.checkpoint_id is None or binding.root_turn_id is None:
            raise OfficeValidationStaleError(
                "Office source lacks checkpoint provenance"
            )
        manifest = binding.manifest
        return OfficeSourceIdentity(
            session_id=binding.session_id,
            workspace_instance_id=binding.workspace_instance_id,
            relative_path=binding.relative_path,
            source_sha256=binding.source_sha256,
            checkpoint_id=binding.checkpoint_id,
            root_turn_id=binding.root_turn_id,
            cache_key=manifest.cache_key,
            renderer_id=manifest.renderer_id,
            renderer_version=manifest.renderer_version,
            font_digest=manifest.font_digest,
            parameters_version=manifest.parameters_version,
            parameters_sha256=manifest.parameters_sha256,
            quality=manifest.quality,
        )

    async def _assert_checkpoint_fresh(
        self,
        identity: OfficeSourceIdentity,
        *,
        require_latest_path_version: bool = True,
    ) -> None:
        async with self._session_factory() as db:
            checkpoint = await db.get(SessionCheckpoint, identity.checkpoint_id)
            if checkpoint is None:
                raise OfficeValidationStaleError("Office checkpoint is unavailable")
            if (
                checkpoint.state != "finalized"
                or checkpoint.pin_state != "pinned"
                or checkpoint.session_id != identity.session_id
                or checkpoint.workspace_instance_id != identity.workspace_instance_id
                or checkpoint.root_turn_id != identity.root_turn_id
            ):
                raise OfficeValidationStaleError(
                    "Office checkpoint was rewound, released, or rebound"
                )
            exact_change = (
                await db.execute(
                    select(CheckpointChange.id).where(
                        CheckpointChange.checkpoint_id == checkpoint.id,
                        CheckpointChange.relative_path == identity.relative_path,
                        CheckpointChange.after_exists.is_(True),
                        CheckpointChange.node_kind == "file",
                        CheckpointChange.after_sha256 == identity.source_sha256,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if exact_change is None:
                raise OfficeValidationStaleError(
                    "Office checkpoint digest does not match the current source"
                )
            later = list(
                (
                    await db.execute(
                        select(SessionCheckpoint).where(
                            SessionCheckpoint.session_id == identity.session_id,
                            SessionCheckpoint.workspace_instance_id
                            == identity.workspace_instance_id,
                            SessionCheckpoint.sequence > checkpoint.sequence,
                        )
                        .order_by(SessionCheckpoint.sequence)
                        .limit(10_001)
                    )
                ).scalars()
            )
            if len(later) > 10_000:
                raise OfficeValidationStaleError(
                    "Office checkpoint history exceeds the freshness budget"
                )
        later_ids = tuple(item.id for item in later)
        changed_by_checkpoint: dict[str, set[str]] = {}
        if later_ids:
            async with self._session_factory() as db:
                rows = (
                    await db.execute(
                        select(
                            CheckpointChange.checkpoint_id,
                            CheckpointChange.relative_path,
                        ).where(CheckpointChange.checkpoint_id.in_(later_ids))
                    )
                ).all()
            for checkpoint_id, relative_path in rows:
                changed_by_checkpoint.setdefault(checkpoint_id, set()).add(
                    relative_path
                )
        for item in later:
            result = dict(item.details or {}).get("rewind_result")
            restored = result.get("restored_paths") if isinstance(result, dict) else None
            restored_touched = (
                isinstance(restored, list)
                and len(restored) <= 50_000
                and any(
                    isinstance(value, dict)
                    and value.get("relative_path") == identity.relative_path
                    for value in restored
                )
            )
            ledger_touched = identity.relative_path in changed_by_checkpoint.get(
                item.id,
                set(),
            )
            if restored_touched or ledger_touched:
                if item.state in {"rewinding", "rewound"}:
                    raise OfficeValidationStaleError(
                        "A later rewind touched the Office validation source"
                    )
        if require_latest_path_version and any(
            identity.relative_path in paths
            for paths in changed_by_checkpoint.values()
        ):
            raise OfficeValidationStaleError(
                "A newer checkpoint owns the Office source path"
            )

    @staticmethod
    def _remaining_ms(deadline: float, cancel_event: asyncio.Event | None) -> int:
        if cancel_event is not None and cancel_event.is_set():
            raise OfficeValidationCancelledError("Office validation was cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OfficeValidationBudgetError("Office validation budget was exhausted")
        return max(1, int(remaining * 1_000))

    async def _await_controlled(
        self,
        awaitable: Awaitable[object],
        *,
        deadline: float,
        cancel_event: asyncio.Event | None,
    ):
        try:
            remaining_ms = self._remaining_ms(deadline, cancel_event)
        except (OfficeValidationCancelledError, OfficeValidationBudgetError):
            # Call sites deliberately construct the coroutine before entering
            # this boundary. Close an unstarted coroutine when admission is
            # already cancelled/expired so fail-closed paths leak no work or
            # RuntimeWarning.
            if asyncio.iscoroutine(awaitable):
                awaitable.close()
            raise
        work = asyncio.create_task(awaitable)
        cancellation: asyncio.Task[bool] | None = None
        if cancel_event is not None:
            cancellation = asyncio.create_task(cancel_event.wait())
        try:
            waiting = {work}
            if cancellation is not None:
                waiting.add(cancellation)
            done, _pending = await asyncio.wait(
                waiting,
                timeout=remaining_ms / 1_000,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancellation is not None and cancellation in done:
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
                raise OfficeValidationCancelledError(
                    "Office validation was cancelled"
                )
            if work not in done:
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
                raise OfficeValidationBudgetError(
                    "Office validation budget was exhausted"
                )
            return await work
        except BaseException:
            if not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
            raise
        finally:
            if cancellation is not None:
                cancellation.cancel()
                await asyncio.gather(cancellation, return_exceptions=True)


__all__ = [
    "OFFICE_VALIDATION_LOOP_SCHEMA_VERSION",
    "IndependentOfficeValidator",
    "OfficeBaselineHandle",
    "OfficeRepairAttemptReceipt",
    "OfficeRepairAttemptRequest",
    "OfficeRepairCallback",
    "OfficeSourceIdentity",
    "OfficeValidationBudgetError",
    "OfficeValidationBusyError",
    "OfficeValidationCancelledError",
    "OfficeValidationLoopReport",
    "OfficeValidationOrchestrator",
    "OfficeValidationPolicy",
    "OfficeValidationStaleError",
    "ServerOwnedOfficeValidationState",
]
