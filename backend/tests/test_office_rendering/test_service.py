from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import release_features
from app.models.checkpoint_change import CheckpointChange
from app.models.session import Session
from app.office_validation import OfficeValidationReport, ValidationCheck
from app.office_rendering import (
    OfficePreviewDisabledError,
    OfficePreviewProvenanceError,
    OfficePreviewService,
    OfficePreviewStaleError,
    OfficeRenderCache,
    RendererDescriptor,
)
from app.storage.checkpoints import (
    create_root_turn,
    prepare_checkpoint,
    record_checkpoint_change,
    register_workspace_instance,
    transition_checkpoint,
)
from tests.test_office_rendering.helpers import FakeProvider


async def _owners(
    session_factory: async_sessionmaker[AsyncSession],
    workspace: Path,
    *,
    session_id: str = "session",
) -> str:
    async with session_factory() as db:
        async with db.begin():
            db.add(
                Session(
                    id=session_id,
                    directory=str(workspace),
                    title="Office preview",
                )
            )
            instance = await register_workspace_instance(
                db,
                str(workspace),
                kind="direct",
                created_by_session_id=session_id,
            )
            return instance.id


def _service(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    *,
    enabled: bool | None = True,
) -> OfficePreviewService:
    descriptor = RendererDescriptor(
        renderer_id="preview-test",
        renderer_version="1",
        font_digest="f" * 64,
        quality="approximate",
    )
    return OfficePreviewService(
        session_factory,
        cache=OfficeRenderCache((tmp_path / "cache").absolute()),
        provider=FakeProvider(descriptor),
        parameters_version="preview-v1",
        parameters={"dpi": 144},
        enabled=enabled,
    )


@pytest.mark.asyncio
async def test_preview_is_bound_to_current_session_workspace_and_source_hash(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "report.docx"
    source.write_bytes(b"first Office version")
    instance_id = await _owners(session_factory, workspace)
    service = _service(session_factory, tmp_path)

    preview = await service.render(
        session_id="session",
        workspace_instance_id=instance_id,
        relative_path="report.docx",
    )
    page = await service.page_path(
        session_id="session",
        workspace_instance_id=instance_id,
        relative_path="report.docx",
        cache_key=preview.manifest.cache_key,
        page_number=1,
    )

    assert preview.source_sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert preview.manifest.source_sha256 == preview.source_sha256
    assert preview.manifest.quality == "approximate"
    assert preview.checkpoint_id is None
    assert page.is_file()

    source.write_bytes(b"second Office version")
    with pytest.raises(OfficePreviewStaleError, match="not current"):
        await service.page_path(
            session_id="session",
            workspace_instance_id=instance_id,
            relative_path="report.docx",
            cache_key=preview.manifest.cache_key,
            page_number=1,
        )


@pytest.mark.asyncio
async def test_preview_checkpoint_binding_requires_exact_after_digest(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "report.docx"
    source.write_bytes(b"checkpoint bytes")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    instance_id = await _owners(session_factory, workspace)
    async with session_factory() as db:
        async with db.begin():
            turn = await create_root_turn(
                db,
                session_id="session",
                workspace_instance_id=instance_id,
                source_kind="desktop",
                turn_id="turn",
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
                after_size=source.stat().st_size,
                call_id="call",
            )
            await transition_checkpoint(db, checkpoint.id, target_state="finalized")
            checkpoint_id = checkpoint.id
    service = _service(session_factory, tmp_path)

    preview = await service.render(
        session_id="session",
        workspace_instance_id=instance_id,
        relative_path="report.docx",
    )

    assert preview.checkpoint_id == checkpoint_id
    assert preview.root_turn_id == "turn"


@pytest.mark.asyncio
async def test_validation_status_becomes_stale_after_a_newer_path_change(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "report.docx"
    source.write_bytes(b"validated bytes")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    instance_id = await _owners(session_factory, workspace)
    async with session_factory() as db:
        async with db.begin():
            turn = await create_root_turn(
                db,
                session_id="session",
                workspace_instance_id=instance_id,
                source_kind="desktop",
                turn_id="validated-turn",
            )
            checkpoint = await prepare_checkpoint(
                db,
                turn_run_id=turn.id,
                anchor_message_id=None,
                todo_snapshot=[],
            )
            await transition_checkpoint(db, checkpoint.id, target_state="committing")
            report = OfficeValidationReport(
                document_format="docx",
                baseline_sha256="0" * 64,
                candidate_sha256=digest,
                renderer_id="attested-test",
                renderer_version="1",
                font_digest="f" * 64,
                verdict="pass",
                checkpoint_id=checkpoint.id,
                root_turn_id=turn.id,
                checks=(
                    ValidationCheck(
                        code="authoritative_quality",
                        outcome="pass",
                        message="Both render sets are authoritative.",
                    ),
                ),
            )
            await record_checkpoint_change(
                db,
                checkpoint_id=checkpoint.id,
                turn_run_id=turn.id,
                operation="created",
                node_kind="file",
                relative_path="report.docx",
                after_sha256=digest,
                after_mode=0o600,
                after_size=source.stat().st_size,
                call_id="validated-call",
                details={"tool": "office", "office_validation": report.to_dict()},
            )
            await transition_checkpoint(db, checkpoint.id, target_state="finalized")
            checkpoint_id = checkpoint.id
    service = _service(session_factory, tmp_path)

    fresh = await service.validation_status(
        session_id="session",
        workspace_instance_id=instance_id,
        relative_path="report.docx",
    )

    assert fresh.status == "authoritative_pass"
    assert fresh.stale_reason is None
    assert fresh.report is not None
    assert fresh.report["checkpoint_id"] == checkpoint_id
    assert "cache" not in fresh.to_dict()

    # Persisted evidence is revalidated at the read boundary.  A local DB
    # corruption cannot make a non-authoritative report appear current.
    async with session_factory() as db:
        async with db.begin():
            change = (
                await db.execute(
                    select(CheckpointChange).where(
                        CheckpointChange.checkpoint_id == checkpoint_id
                    )
                )
            ).scalar_one()
            corrupted = dict(change.details)
            invalid_report = dict(corrupted["office_validation"])
            invalid_checks = list(invalid_report["checks"])
            invalid_checks[0] = {
                **invalid_checks[0],
                "code": "structural_delta",
            }
            invalid_report["checks"] = invalid_checks
            corrupted["office_validation"] = invalid_report
            change.details = corrupted

    invalid = await service.validation_status(
        session_id="session",
        workspace_instance_id=instance_id,
        relative_path="report.docx",
    )
    assert invalid.status == "invalid"
    assert invalid.stale_reason == "evidence_binding_invalid"
    assert invalid.report is None

    async with session_factory() as db:
        async with db.begin():
            change = (
                await db.execute(
                    select(CheckpointChange).where(
                        CheckpointChange.checkpoint_id == checkpoint_id
                    )
                )
            ).scalar_one()
            restored = dict(change.details)
            restored["office_validation"] = report.to_dict()
            change.details = restored

    source.write_bytes(b"newer bytes")
    newer_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    async with session_factory() as db:
        async with db.begin():
            turn = await create_root_turn(
                db,
                session_id="session",
                workspace_instance_id=instance_id,
                source_kind="desktop",
                turn_id="newer-turn",
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
                after_sha256=newer_digest,
                after_mode=0o600,
                after_size=source.stat().st_size,
                call_id="newer-call",
            )
            await transition_checkpoint(db, checkpoint.id, target_state="finalized")

    stale = await service.validation_status(
        session_id="session",
        workspace_instance_id=instance_id,
        relative_path="report.docx",
    )

    assert stale.status == "stale"
    assert stale.stale_reason == "newer_path_change"
    assert stale.report is not None
    assert stale.report["candidate_sha256"] == digest


@pytest.mark.asyncio
async def test_preview_binds_exact_restored_digest_to_rewind_checkpoint(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "report.docx"
    source.write_bytes(b"restored Office bytes")
    restored_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    instance_id = await _owners(session_factory, workspace)
    async with session_factory() as db:
        async with db.begin():
            turn = await create_root_turn(
                db,
                session_id="session",
                workspace_instance_id=instance_id,
                source_kind="desktop",
                turn_id="rewind-turn",
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
                after_sha256="d" * 64,
                after_mode=0o600,
                after_size=4,
                call_id="call",
            )
            await transition_checkpoint(db, checkpoint.id, target_state="finalized")
            await transition_checkpoint(db, checkpoint.id, target_state="rewinding")
            await transition_checkpoint(db, checkpoint.id, target_state="rewound")
            checkpoint.details = {
                "rewind_result": {
                    "restored_paths": [
                        {
                            "relative_path": "report.docx",
                            "exists": True,
                            "node_kind": "file",
                            "sha256": restored_digest,
                            "mode": 0o600,
                            "size": source.stat().st_size,
                        }
                    ]
                }
            }
            checkpoint_id = checkpoint.id
    service = _service(session_factory, tmp_path)

    preview = await service.render(
        session_id="session",
        workspace_instance_id=instance_id,
        relative_path="report.docx",
    )

    assert preview.checkpoint_id == checkpoint_id
    assert preview.root_turn_id == "rewind-turn"


@pytest.mark.asyncio
async def test_preview_gate_and_provenance_fail_closed(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "report.docx").write_bytes(b"Office")
    instance_id = await _owners(session_factory, workspace)
    monkeypatch.setattr(release_features, "V11_OFFICE_V2_RELEASED", False)
    closed = _service(session_factory, tmp_path / "closed", enabled=None)

    with pytest.raises(OfficePreviewDisabledError):
        await closed.render(
            session_id="session",
            workspace_instance_id=instance_id,
            relative_path="report.docx",
        )

    service = _service(session_factory, tmp_path / "open")
    with pytest.raises(OfficePreviewProvenanceError, match="escapes"):
        await service.render(
            session_id="session",
            workspace_instance_id=instance_id,
            relative_path="../report.docx",
        )


@pytest.mark.asyncio
async def test_preview_rejects_symbolic_link_source(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "real.docx").write_bytes(b"Office")
    link = workspace / "report.docx"
    try:
        link.symlink_to("real.docx")
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable")
    instance_id = await _owners(session_factory, workspace)
    service = _service(session_factory, tmp_path)

    with pytest.raises(OfficePreviewProvenanceError, match="symbolic"):
        await service.render(
            session_id="session",
            workspace_instance_id=instance_id,
            relative_path="report.docx",
        )
