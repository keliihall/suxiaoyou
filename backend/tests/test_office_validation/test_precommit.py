from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.office_rendering import (
    OfficeRenderCache,
    ProviderAvailability,
    RenderManifest,
    RendererDescriptor,
    RenderRequest,
)
from app.office_validation import (
    DeterministicOfficePrecommitCoordinator,
    OfficeCreateValidationPlan,
    OfficeDraftValidationService,
    OfficeEditValidationPlan,
    OfficeStandaloneCreateValidationPlan,
    OfficeGoldenPolicy,
    OfficePrecommitRejectedError,
    OfficePrecommitRequest,
    OfficePrecommitStateError,
    VisualDiffPolicy,
)
from app.schemas.agent import AgentInfo
from app.tool import workspace_transaction as transaction_module
from app.tool.context import ToolContext
from app.tool.workspace_transaction import (
    WorkspaceMutationTransaction,
    WorkspacePrecommitSealError,
)
from tests.test_office_rendering.helpers import png_bytes, write_render_artifacts
from tests.test_office_templates.helpers import (
    make_docx_template,
    manifest_for,
    rewrite_zip,
    zip_entries,
)


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        transaction_module.guarded_file_mutation_unavailable_reason() is not None,
        reason="guarded mutation primitive unavailable",
    ),
]


class _Provider:
    def __init__(self, *, quality: str = "authoritative") -> None:
        self._descriptor = RendererDescriptor(
            renderer_id="precommit-test-renderer",
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
        content = png_bytes(red=30)
        pdf, pages = write_render_artifacts(output_dir, (content,))
        return RenderManifest.for_request(
            request,
            self._descriptor,
            pages,
            pdf=pdf,
        )


class _Policies:
    def __init__(
        self,
        *,
        create: OfficeCreateValidationPlan | None = None,
    ) -> None:
        self.create = create

    def resolve_edit(self, request, baseline) -> OfficeEditValidationPlan:
        del request, baseline
        return OfficeEditValidationPlan(
            allowed_changed_parts=("word/document.xml",),
            visual_policy=VisualDiffPolicy(max_blank_fraction_increase=1.0),
        )

    def resolve_create(self, request) -> OfficeCreateValidationPlan:
        del request
        if self.create is None:
            raise OfficePrecommitRejectedError(
                "No trusted signed golden is configured"
            )
        return self.create

    def resolve_standalone_create(
        self,
        request,
    ) -> OfficeStandaloneCreateValidationPlan:
        return OfficeStandaloneCreateValidationPlan(
            policy_id="test/standalone-create/1",
            document_format=request.document_format,
            renderer_id="precommit-test-renderer",
            renderer_version="1",
            font_digest="f" * 64,
            parameters_version="precommit-v1",
            parameters_sha256=hashlib.sha256(b'{"dpi":144}').hexdigest(),
            visual_policy=VisualDiffPolicy(
                max_candidate_blank_fraction=0.999,
            ),
        )


def _context(workspace: Path) -> ToolContext:
    return ToolContext(
        session_id="session",
        message_id="message",
        agent=AgentInfo(name="test", description="", mode="primary"),
        call_id="office-call",
        workspace=str(workspace),
        root_turn_id="root-turn",
        turn_run_id="turn-run",
        checkpoint_id="checkpoint",
        workspace_instance_id="workspace-instance",
    )


def _service(
    tmp_path: Path,
    *,
    quality: str = "authoritative",
) -> OfficeDraftValidationService:
    return OfficeDraftValidationService(
        cache=OfficeRenderCache((tmp_path / f"cache-{quality}").resolve()),
        provider=_Provider(quality=quality),
        parameters_version="precommit-v1",
        parameters={"dpi": 144},
    )


def _variant(content: bytes) -> bytes:
    entries = zip_entries(content)
    document = entries["word/document.xml"].replace(
        "正文".encode(),
        "报告".encode(),
        1,
    )
    assert document != entries["word/document.xml"]
    return rewrite_zip(
        content,
        replacements={"word/document.xml": document},
    )


def _request(view, *, operation: str) -> OfficePrecommitRequest:
    return OfficePrecommitRequest(
        operation=operation,  # type: ignore[arg-type]
        document_format="docx",
        relative_path=view.relative_path,
        session_id=view.session_id,
        message_id=view.message_id,
        call_id=view.call_id,
        root_turn_id=view.root_turn_id,
        turn_run_id=view.turn_run_id,
        checkpoint_id=view.checkpoint_id,
        workspace_instance_id=view.workspace_instance_id,
        template_id="report" if operation == "create" else None,
        template_version="1.0.0" if operation == "create" else None,
    )


async def test_edit_session_binds_transaction_and_consumes_only_latest_result(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    private = (tmp_path / "private").resolve()
    workspace.mkdir()
    target = workspace / "suxiaoyou_written" / "report.docx"
    target.parent.mkdir()
    baseline = make_docx_template()
    candidate = _variant(baseline)
    target.write_bytes(baseline)
    transaction = WorkspaceMutationTransaction(
        workspace,
        _context(workspace),
        operation="office.edit",
        storage_root=private,
    )
    transaction.prepare_paths([target])
    view = transaction.arm_office_precommit_validation(target)
    coordinator = DeterministicOfficePrecommitCoordinator(
        service=_service(tmp_path),
        policies=_Policies(),
    )
    session = await coordinator.begin(
        request=_request(view, operation="edit"),
        view=view,
    )
    view.staged_target.write_bytes(candidate)
    result = await session.validate_candidate()

    assert result.report.checkpoint_id == "checkpoint"
    assert result.report.root_turn_id == "root-turn"
    with pytest.raises(WorkspacePrecommitSealError, match="requires its precommit seal"):
        transaction.commit()
    seal = session.consume_commit_seal(result)
    with pytest.raises(OfficePrecommitStateError, match="stale"):
        session.consume_commit_seal(result)
    transaction.commit_with_precommit_office_seal(seal)
    session.mark_committed(result)
    assert target.read_bytes() == candidate


async def test_request_identity_cannot_be_mixed_with_another_transaction_view(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    target = workspace / "report.docx"
    target.write_bytes(make_docx_template())
    transaction = WorkspaceMutationTransaction(
        workspace,
        _context(workspace),
        operation="office.edit",
        storage_root=tmp_path / "private",
    )
    transaction.prepare_paths([target])
    view = transaction.arm_office_precommit_validation(target)
    request = _request(view, operation="edit")
    request = OfficePrecommitRequest(
        operation=request.operation,
        document_format=request.document_format,
        relative_path=request.relative_path,
        session_id=request.session_id,
        message_id=request.message_id,
        call_id="different-call",
        root_turn_id=request.root_turn_id,
        turn_run_id=request.turn_run_id,
        checkpoint_id=request.checkpoint_id,
        workspace_instance_id=request.workspace_instance_id,
    )
    coordinator = DeterministicOfficePrecommitCoordinator(
        service=_service(tmp_path),
        policies=_Policies(),
    )

    with pytest.raises(OfficePrecommitRejectedError, match="runtime identity"):
        await coordinator.begin(request=request, view=view)
    transaction.abort()
    assert target.read_bytes() == make_docx_template()


async def test_signed_golden_create_can_publish_nested_target(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    golden_root = (tmp_path / "golden").resolve()
    private = (tmp_path / "private").resolve()
    workspace.mkdir()
    golden_root.mkdir()
    golden_path = golden_root / "template.docx"
    golden_bytes = make_docx_template()
    candidate = _variant(golden_bytes)
    golden_path.write_bytes(golden_bytes)
    manifest = manifest_for(
        golden_bytes,
        "docx",
        ("body", "client", "footer", "header", "table"),
        template_id="report",
        version="1.0.0",
    )
    policy = OfficeGoldenPolicy(
        policy_id="first-party/report/1",
        template_id="report",
        template_version="1.0.0",
        template_manifest_sha256=manifest.template_sha256,
        baseline_sha256=hashlib.sha256(golden_bytes).hexdigest(),
        renderer_id="precommit-test-renderer",
        renderer_version="1",
        font_digest="f" * 64,
        parameters_version="precommit-v1",
        parameters_sha256=hashlib.sha256(b'{"dpi":144}').hexdigest(),
        allowed_changed_parts=("word/document.xml",),
        visual=VisualDiffPolicy(max_blank_fraction_increase=1.0),
    )
    plan = OfficeCreateValidationPlan(
        golden_root=golden_root,
        golden_path=golden_path,
        golden_policy=policy,
        template_manifest=manifest,
    )
    target = workspace / "suxiaoyou_written" / "nested" / "report.docx"
    transaction = WorkspaceMutationTransaction(
        workspace,
        _context(workspace),
        operation="office.create",
        storage_root=private,
    )
    transaction.prepare_paths([target])
    view = transaction.arm_office_precommit_validation(target)
    coordinator = DeterministicOfficePrecommitCoordinator(
        service=_service(tmp_path),
        policies=_Policies(create=plan),
    )
    session = await coordinator.begin(
        request=_request(view, operation="create"),
        view=view,
    )
    view.staged_target.parent.mkdir(parents=True)
    view.staged_target.write_bytes(candidate)
    result = await session.validate_candidate()
    seal = session.consume_commit_seal(result)
    transaction.commit_with_precommit_office_seal(seal)
    session.mark_committed(result)

    assert target.read_bytes() == candidate


async def test_ordinary_create_can_publish_with_authoritative_standalone_policy(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    target = workspace / "suxiaoyou_written" / "ordinary.docx"
    transaction = WorkspaceMutationTransaction(
        workspace,
        _context(workspace),
        operation="office.create",
        storage_root=tmp_path / "private",
    )
    transaction.prepare_paths([target])
    view = transaction.arm_office_precommit_validation(target)
    request = OfficePrecommitRequest(
        operation="create",
        document_format="docx",
        relative_path=view.relative_path,
        session_id=view.session_id,
        message_id=view.message_id,
        call_id=view.call_id,
        root_turn_id=view.root_turn_id,
        turn_run_id=view.turn_run_id,
        checkpoint_id=view.checkpoint_id,
        workspace_instance_id=view.workspace_instance_id,
    )
    coordinator = DeterministicOfficePrecommitCoordinator(
        service=_service(tmp_path),
        policies=_Policies(),
    )

    session = await coordinator.begin(request=request, view=view)
    view.staged_target.parent.mkdir(parents=True)
    candidate = make_docx_template()
    view.staged_target.write_bytes(candidate)
    result = await session.validate_candidate()

    assert result.report.verdict == "pass"
    assert result.report.baseline_sha256 == result.report.candidate_sha256
    assert next(
        check
        for check in result.report.checks
        if check.code == "standalone_runtime_identity"
    ).outcome == "pass"
    seal = session.consume_commit_seal(result)
    transaction.commit_with_precommit_office_seal(seal)
    session.mark_committed(result)
    assert target.read_bytes() == candidate


async def test_approximate_renderer_cannot_authorize_armed_edit(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    target = workspace / "report.docx"
    baseline = make_docx_template()
    target.write_bytes(baseline)
    transaction = WorkspaceMutationTransaction(
        workspace,
        _context(workspace),
        operation="office.edit",
        storage_root=tmp_path / "private",
    )
    transaction.prepare_paths([target])
    view = transaction.arm_office_precommit_validation(target)
    coordinator = DeterministicOfficePrecommitCoordinator(
        service=_service(tmp_path, quality="approximate"),
        policies=_Policies(),
    )
    session = await coordinator.begin(
        request=_request(view, operation="edit"),
        view=view,
    )
    view.staged_target.write_bytes(_variant(baseline))
    result = await session.validate_candidate()

    assert result.report.verdict == "needs_review"
    with pytest.raises(OfficePrecommitRejectedError, match="authoritative"):
        session.consume_commit_seal(result)
    transaction.abort()
    assert target.read_bytes() == baseline
