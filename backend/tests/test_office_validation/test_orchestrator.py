from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.session import Session
from app.office_rendering import (
    OfficePreviewService,
    OfficeRenderCache,
    ProviderAvailability,
    RenderManifest,
    RendererDescriptor,
    RenderRequest,
)
from app.office_validation import (
    OfficeRepairAttemptReceipt,
    OfficeValidationCancelledError,
    OfficeValidationOrchestrator,
    OfficeValidationPolicy,
    OfficeValidationStaleError,
    ServerOwnedOfficeValidationState,
    VisualDiffPolicy,
)
from app.storage.checkpoints import (
    create_root_turn,
    prepare_checkpoint,
    record_checkpoint_change,
    register_workspace_instance,
    transition_checkpoint,
)
from app.streaming.manager import GenerationJob
from app.validation_agent import (
    ValidationBudgetReport,
    ValidationEvidence,
    ValidationSource,
    ValidationVerdictRecord,
)
from tests.test_office_rendering.helpers import png_bytes, write_render_artifacts
from tests.test_office_templates.helpers import (
    make_docx_template,
    rewrite_zip,
    zip_entries,
)


pytestmark = pytest.mark.asyncio


class SourceMappedProvider:
    """Authoritative test provider with source-controlled visual evidence."""

    def __init__(self, colors: dict[str, int], *, quality: str = "authoritative") -> None:
        self.colors = colors
        self._descriptor = RendererDescriptor(
            renderer_id="attested-test-renderer",
            renderer_version="1",
            font_digest="f" * 64,
            quality=quality,  # type: ignore[arg-type]
        )

    @property
    def descriptor(self) -> RendererDescriptor:
        return self._descriptor

    def availability(self) -> ProviderAvailability:
        return ProviderAvailability(available=True)

    async def render(self, request: RenderRequest, output_dir: Path) -> RenderManifest:
        content = png_bytes(red=self.colors.get(request.source_sha256, 24))
        pdf, pages = write_render_artifacts(output_dir, (content,))
        return RenderManifest.for_request(
            request,
            self._descriptor,
            pages,
            pdf=pdf,
        )


class PassingValidator:
    """Attempts to pass even when the supplied deterministic gates failed."""

    def __init__(self, *, root_turn_id: str, workspace_instance_id: str) -> None:
        self.root_turn_id = root_turn_id
        self.workspace_instance_id = workspace_instance_id
        self.tasks: list[Any] = []

    async def validate(self, *, parent_job, checkpoint_id, task):
        self.tasks.append(task)
        return ValidationVerdictRecord(
            schema_version=1,
            validation_id="validator-record",
            verdict="pass",
            reason_code="model_verdict",
            source=ValidationSource(
                session_id="session",
                root_turn_id=self.root_turn_id,
                checkpoint_id=checkpoint_id,
                workspace_instance_id=self.workspace_instance_id,
            ),
            round=1,
            budget=ValidationBudgetReport(
                max_rounds=1,
                max_tokens=4_000,
                timeout_ms=30_000,
                rounds_used=1,
                tokens_used=10,
                elapsed_ms=2,
            ),
            summary="The model claims the document is acceptable.",
            evidence=(
                ValidationEvidence(
                    evidence_id="evidence-1",
                    origin="validator",
                    kind="observation",
                    source="report.docx",
                    summary="A read-only observation was made.",
                ),
            ),
            validator_session_ids=("validator-session",),
        )


def _variant(content: bytes, old: bytes, new: bytes) -> bytes:
    entries = zip_entries(content)
    document = entries["word/document.xml"].replace(old, new, 1)
    assert document != entries["word/document.xml"]
    return rewrite_zip(
        content,
        replacements={"word/document.xml": document},
    )


async def _owners(
    session_factory: async_sessionmaker[AsyncSession],
    workspace: Path,
) -> str:
    async with session_factory() as db:
        async with db.begin():
            db.add(Session(id="session", directory=str(workspace), title="Office"))
            instance = await register_workspace_instance(
                db,
                str(workspace),
                kind="direct",
                created_by_session_id="session",
            )
            return instance.id


async def _checkpoint(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    instance_id: str,
    content: bytes,
    turn_id: str,
) -> tuple[str, str]:
    digest = hashlib.sha256(content).hexdigest()
    async with session_factory() as db:
        async with db.begin():
            turn = await create_root_turn(
                db,
                session_id="session",
                workspace_instance_id=instance_id,
                source_kind="desktop",
                turn_id=turn_id,
            )
            checkpoint = await prepare_checkpoint(
                db,
                turn_run_id=turn.id,
                anchor_message_id=None,
                todo_snapshot=[],
            )
            await transition_checkpoint(db, checkpoint.id, target_state="committing")
            await record_checkpoint_change(
                db,
                checkpoint_id=checkpoint.id,
                turn_run_id=turn.id,
                operation="created",
                node_kind="file",
                relative_path="report.docx",
                after_sha256=digest,
                after_mode=0o600,
                after_size=len(content),
                call_id=f"call-{turn_id}",
            )
            await transition_checkpoint(db, checkpoint.id, target_state="finalized")
            return checkpoint.id, turn.id


def _service(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    provider: SourceMappedProvider,
) -> OfficePreviewService:
    return OfficePreviewService(
        session_factory,
        cache=OfficeRenderCache((tmp_path / "cache").absolute()),
        provider=provider,
        parameters_version="office-validation-v1",
        parameters={"dpi": 144},
        enabled=True,
    )


def _policy(
    *,
    allowed_parts: tuple[str, ...] = ("word/document.xml",),
    max_repair_rounds: int = 2,
) -> OfficeValidationPolicy:
    return OfficeValidationPolicy(
        allowed_changed_parts=allowed_parts,
        visual=VisualDiffPolicy(max_blank_fraction_increase=1.0),
        max_repair_rounds=max_repair_rounds,
        timeout_ms=5_000,
    )


async def _baseline_and_candidate(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    *,
    validator=None,
    allowed_parts: tuple[str, ...] = ("word/document.xml",),
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "report.docx"
    baseline = make_docx_template()
    candidate = _variant(baseline, "正文".encode(), "报告".encode())
    baseline_digest = hashlib.sha256(baseline).hexdigest()
    candidate_digest = hashlib.sha256(candidate).hexdigest()
    provider = SourceMappedProvider(
        {baseline_digest: 20, candidate_digest: 220}
    )
    instance_id = await _owners(session_factory, workspace)
    source.write_bytes(baseline)
    await _checkpoint(
        session_factory,
        instance_id=instance_id,
        content=baseline,
        turn_id="baseline-turn",
    )
    service = _service(session_factory, tmp_path, provider)
    orchestrator = OfficeValidationOrchestrator(
        session_factory=session_factory,
        preview_service=service,
        validator=validator,
    )
    handle = await orchestrator.capture_baseline(
        session_id="session",
        workspace_instance_id=instance_id,
        relative_path="report.docx",
        policy=_policy(allowed_parts=allowed_parts),
    )
    source.write_bytes(candidate)
    checkpoint_id, root_turn_id = await _checkpoint(
        session_factory,
        instance_id=instance_id,
        content=candidate,
        turn_id="candidate-turn",
    )
    return (
        orchestrator,
        handle,
        service,
        source,
        instance_id,
        candidate,
        checkpoint_id,
        root_turn_id,
        provider,
    )


async def test_seeded_structural_and_visual_defects_have_exact_evidence(
    session_factory,
    tmp_path: Path,
) -> None:
    orchestrator, handle, *_rest = await _baseline_and_candidate(
        session_factory,
        tmp_path,
        allowed_parts=(),
    )

    report = await orchestrator.validate_and_repair(handle)

    assert report.verdict == "fail"
    assert report.reason_code == "deterministic_failure"
    outcomes = {item.code: item for item in report.checks}
    assert outcomes["structural_parts"].outcome == "fail"
    assert outcomes["pixel_delta"].outcome == "fail"
    assert outcomes["pixel_delta"].box is not None
    assert outcomes["pixel_delta"].box.to_dict() == {
        "page_number": 1,
        "x": 0,
        "y": 0,
        "width": 2,
        "height": 2,
    }
    assert report.to_dict()["authoritative_pass"] is False


async def test_fresh_authoritative_deterministic_result_passes_without_agent(
    session_factory,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "report.docx"
    content = make_docx_template()
    digest = hashlib.sha256(content).hexdigest()
    source.write_bytes(content)
    instance_id = await _owners(session_factory, workspace)
    await _checkpoint(
        session_factory,
        instance_id=instance_id,
        content=content,
        turn_id="baseline-turn",
    )
    service = _service(
        session_factory,
        tmp_path,
        SourceMappedProvider({digest: 20}),
    )
    orchestrator = OfficeValidationOrchestrator(
        session_factory=session_factory,
        preview_service=service,
    )
    handle = await orchestrator.capture_baseline(
        session_id="session",
        workspace_instance_id=instance_id,
        relative_path="report.docx",
        policy=_policy(),
    )

    report = await orchestrator.validate_and_repair(handle)

    assert report.verdict == "pass"
    assert report.reason_code == "authoritative_pass"
    assert report.authoritative_pass
    assert all(item.outcome == "pass" for item in report.checks)

    cancelled = asyncio.Event()
    cancelled.set()
    with pytest.raises(OfficeValidationCancelledError):
        await orchestrator.validate_and_repair(handle, cancel_event=cancelled)


async def test_deterministic_failure_precedes_independent_model_pass(
    session_factory,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "report.docx"
    baseline = make_docx_template()
    candidate = _variant(baseline, "正文".encode(), "报告".encode())
    colors = {
        hashlib.sha256(baseline).hexdigest(): 20,
        hashlib.sha256(candidate).hexdigest(): 220,
    }
    instance_id = await _owners(session_factory, workspace)
    source.write_bytes(baseline)
    await _checkpoint(
        session_factory,
        instance_id=instance_id,
        content=baseline,
        turn_id="baseline-turn",
    )
    service = _service(session_factory, tmp_path, SourceMappedProvider(colors))
    state = ServerOwnedOfficeValidationState()
    capture = OfficeValidationOrchestrator(
        session_factory=session_factory,
        preview_service=service,
        state=state,
    )
    handle = await capture.capture_baseline(
        session_id="session",
        workspace_instance_id=instance_id,
        relative_path="report.docx",
        policy=_policy(allowed_parts=()),
    )
    source.write_bytes(candidate)
    candidate_checkpoint, candidate_turn = await _checkpoint(
        session_factory,
        instance_id=instance_id,
        content=candidate,
        turn_id="candidate-turn",
    )
    validator = PassingValidator(
        root_turn_id=candidate_turn,
        workspace_instance_id=instance_id,
    )
    orchestrator = OfficeValidationOrchestrator(
        session_factory=session_factory,
        preview_service=service,
        state=state,
        validator=validator,
    )
    parent = GenerationJob(
        "stream",
        "session",
        invocation_source="desktop",
        root_turn_id=candidate_turn,
        turn_run_id=candidate_turn,
        workspace_instance_id=instance_id,
    )

    report = await orchestrator.validate_and_repair(handle, parent_job=parent)

    assert report.candidate.checkpoint_id == candidate_checkpoint
    assert report.verdict == "fail"
    assert report.validator_record is not None
    assert report.validator_record.verdict == "pass"
    assert validator.tasks[0].deterministic_failures
    validator_check = next(
        item for item in report.checks if item.code == "independent_validator"
    )
    assert validator_check.outcome == "pass"


async def test_server_owned_two_total_repair_round_limit_survives_repeat_call(
    session_factory,
    tmp_path: Path,
) -> None:
    (
        orchestrator,
        handle,
        _service_instance,
        source,
        instance_id,
        candidate,
        _checkpoint_id,
        _root_turn_id,
        provider,
    ) = await _baseline_and_candidate(session_factory, tmp_path)
    first = _variant(candidate, "报告".encode(), "缺陷".encode())
    second = _variant(first, "缺陷".encode(), "异常".encode())
    provider.colors[hashlib.sha256(first).hexdigest()] = 220
    provider.colors[hashlib.sha256(second).hexdigest()] = 220
    versions = (first, second)
    calls: list[int] = []

    async def repair(request):
        calls.append(request.repair_round)
        content = versions[request.repair_round - 1]
        source.write_bytes(content)
        checkpoint_id, turn_id = await _checkpoint(
            session_factory,
            instance_id=instance_id,
            content=content,
            turn_id=f"repair-turn-{request.repair_round}",
        )
        return OfficeRepairAttemptReceipt(
            source_sha256=hashlib.sha256(content).hexdigest(),
            checkpoint_id=checkpoint_id,
            root_turn_id=turn_id,
        )

    first_report = await orchestrator.validate_and_repair(handle, repair=repair)
    second_report = await orchestrator.validate_and_repair(handle, repair=repair)

    assert calls == [1, 2]
    assert first_report.verdict == "fail"
    assert first_report.reason_code == "repair_limit_reached"
    assert first_report.repair_rounds_used == 2
    assert first_report.repair_rounds_remaining == 0
    assert second_report.reason_code == "repair_limit_reached"
    assert second_report.repair_rounds_used == 2


async def test_approximate_renderer_can_never_yield_authoritative_pass(
    session_factory,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "report.docx"
    content = make_docx_template()
    digest = hashlib.sha256(content).hexdigest()
    source.write_bytes(content)
    instance_id = await _owners(session_factory, workspace)
    await _checkpoint(
        session_factory,
        instance_id=instance_id,
        content=content,
        turn_id="baseline-turn",
    )
    service = _service(
        session_factory,
        tmp_path,
        SourceMappedProvider({digest: 20}, quality="approximate"),
    )
    orchestrator = OfficeValidationOrchestrator(
        session_factory=session_factory,
        preview_service=service,
    )
    handle = await orchestrator.capture_baseline(
        session_id="session",
        workspace_instance_id=instance_id,
        relative_path="report.docx",
        policy=_policy(max_repair_rounds=0),
    )

    report = await orchestrator.validate_and_repair(handle)

    assert report.verdict == "needs_review"
    assert not report.authoritative_pass
    assert next(
        item for item in report.checks if item.code == "authoritative_quality"
    ).outcome == "needs_review"


async def test_later_rewind_digest_and_tampered_cache_both_fail_closed(
    session_factory,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "report.docx"
    baseline = make_docx_template()
    changed = _variant(baseline, "正文".encode(), "报告".encode())
    baseline_digest = hashlib.sha256(baseline).hexdigest()
    changed_digest = hashlib.sha256(changed).hexdigest()
    source.write_bytes(baseline)
    instance_id = await _owners(session_factory, workspace)
    await _checkpoint(
        session_factory,
        instance_id=instance_id,
        content=baseline,
        turn_id="baseline-turn",
    )
    provider = SourceMappedProvider({baseline_digest: 20, changed_digest: 220})
    service = _service(session_factory, tmp_path, provider)
    orchestrator = OfficeValidationOrchestrator(
        session_factory=session_factory,
        preview_service=service,
    )
    handle = await orchestrator.capture_baseline(
        session_id="session",
        workspace_instance_id=instance_id,
        relative_path="report.docx",
        policy=_policy(),
    )

    # Corrupting an immutable cache page can never become a miss or a pass.
    page = await service.page_path(
        session_id="session",
        workspace_instance_id=instance_id,
        relative_path="report.docx",
        cache_key=handle.source.cache_key,
        page_number=1,
    )
    page.write_bytes(page.read_bytes() + b"tamper")
    with pytest.raises(OfficeValidationStaleError, match="cache"):
        await orchestrator.validate_and_repair(handle)

    # Use a fresh cache/orchestrator to isolate the rewind provenance check.
    clean_service = _service(session_factory, tmp_path / "clean", provider)
    clean = OfficeValidationOrchestrator(
        session_factory=session_factory,
        preview_service=clean_service,
    )
    clean_handle = await clean.capture_baseline(
        session_id="session",
        workspace_instance_id=instance_id,
        relative_path="report.docx",
        policy=_policy(),
    )
    source.write_bytes(changed)
    rewind_checkpoint, _ = await _checkpoint(
        session_factory,
        instance_id=instance_id,
        content=changed,
        turn_id="rewind-target-turn",
    )
    source.write_bytes(baseline)
    async with session_factory() as db:
        async with db.begin():
            checkpoint = await transition_checkpoint(
                db,
                rewind_checkpoint,
                target_state="rewinding",
            )
            await transition_checkpoint(
                db,
                rewind_checkpoint,
                target_state="rewound",
            )
            checkpoint.details = {
                "rewind_result": {
                    "restored_paths": [
                        {
                            "relative_path": "report.docx",
                            "exists": True,
                            "node_kind": "file",
                            "sha256": baseline_digest,
                            "mode": 0o600,
                            "size": len(baseline),
                        }
                    ]
                }
            }

    with pytest.raises(OfficeValidationStaleError, match="rewind"):
        await clean.validate_and_repair(clean_handle)
