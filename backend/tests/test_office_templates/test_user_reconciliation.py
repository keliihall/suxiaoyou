"""Fail-closed registry/DB reconciliation for user Office templates."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
import threading

import pytest

from app.office_rendering import OfficeRenderCache, RendererDescriptor
from app.office_templates.errors import TemplateIntegrityError
from app.office_templates.models import AllowedOutputRules, TemplatePackageManifest
from app.office_templates.user import (
    USER_TEMPLATE_MAX_SOURCE_BYTES,
    UserOfficeTemplateService,
    UserTemplateEvidenceError,
    UserTemplateRegistryOwner,
)
from app.office_validation.draft import OfficeDraftValidationService
from tests.test_office_rendering.helpers import FakeProvider
from tests.test_office_templates.helpers import make_docx_template, write_source


_PLACEHOLDERS = ("body", "client", "footer", "header", "table")


def _service(tmp_path: Path) -> UserOfficeTemplateService:
    descriptor = RendererDescriptor(
        renderer_id="user-template-reconciliation-test",
        renderer_version="1.0.0",
        font_digest="a" * 64,
        quality="authoritative",
    )
    draft = OfficeDraftValidationService(
        cache=OfficeRenderCache((tmp_path / "render-cache").absolute()),
        provider=FakeProvider(descriptor),
        parameters_version="reconciliation-test-v1",
        parameters={"dpi": 144},
    )
    return UserOfficeTemplateService(
        (tmp_path / "user-templates").absolute(),
        draft_validation=draft,
    )


def _template_ref(index: int) -> str:
    return f"utpl-{index:026d}"


def _import_user_record(
    service: UserOfficeTemplateService,
    tmp_path: Path,
    index: int,
    *,
    revision: int = 1,
    license_name: str = "User-provided template; rights not verified",
) -> tuple[str, int]:
    content = make_docx_template()
    template_ref = _template_ref(index)
    manifest = TemplatePackageManifest(
        template_id=template_ref,
        template_version=str(revision),
        format="docx",
        source_sha256=hashlib.sha256(content).hexdigest(),
        license=license_name,
        provenance=f"local-user-import:{template_ref}",
        required_placeholders=_PLACEHOLDERS,
        allowed_output_rules=AllowedOutputRules(
            extensions=(".docx",),
            max_output_bytes=USER_TEMPLATE_MAX_SOURCE_BYTES,
            allow_overwrite=False,
        ),
    )
    source = write_source(
        tmp_path / "sources",
        f"source-{index}-{revision}.docx",
        content,
    )
    service.registry.import_template(manifest, source)
    return template_ref, revision


def _registry_keys(service: UserOfficeTemplateService) -> set[tuple[str, int]]:
    return {
        (record.manifest.template_id, int(record.manifest.template_version))
        for record in service.registry.list_templates()
    }


@pytest.mark.asyncio
async def test_reconciliation_deletes_only_unowned_or_tombstoned_unreferenced_records(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    orphan = _import_user_record(service, tmp_path, 1)
    needs_confirmation = _import_user_record(service, tmp_path, 2)
    needs_review = _import_user_record(service, tmp_path, 3)
    approved = _import_user_record(service, tmp_path, 4)
    tombstoned = _import_user_record(service, tmp_path, 5)
    referenced_orphan = _import_user_record(service, tmp_path, 6)
    referenced_tombstone = _import_user_record(service, tmp_path, 7)
    service.registry.retain(*map(str, referenced_orphan), "checkpoint:keep-1")
    service.registry.retain(*map(str, referenced_tombstone), "checkpoint:keep-2")

    calls = 0

    async def owners() -> tuple[
        UserTemplateRegistryOwner | tuple[str, int, str], ...
    ]:
        nonlocal calls
        calls += 1
        return (
            UserTemplateRegistryOwner(*needs_confirmation, "needs_confirmation"),
            UserTemplateRegistryOwner(*needs_review, "needs_review"),
            UserTemplateRegistryOwner(*approved, "approved"),
            UserTemplateRegistryOwner(*tombstoned, "tombstoned"),
            UserTemplateRegistryOwner(*referenced_tombstone, "tombstoned"),
            # A global DB owner without a registry object is valid and must not
            # turn any different record into an owned record.
            (_template_ref(99), 1, "approved"),
        )

    report = await service.reconcile_registry_orphans_once(owners)

    assert report.scanned_records == 7
    assert report.owner_records == 6
    assert report.retained_active == tuple(
        sorted((needs_confirmation, needs_review, approved))
    )
    assert report.retained_referenced == tuple(
        sorted((referenced_orphan, referenced_tombstone))
    )
    assert report.deleted_orphans == (orphan,)
    assert report.deleted_tombstoned == (tombstoned,)
    assert report.deleted_records == 2
    assert _registry_keys(service) == {
        needs_confirmation,
        needs_review,
        approved,
        referenced_orphan,
        referenced_tombstone,
    }
    assert str(tmp_path) not in repr(report)

    async def must_not_run() -> tuple[UserTemplateRegistryOwner, ...]:
        raise AssertionError("a successful once reconciliation queried owners twice")

    assert await service.reconcile_registry_orphans_once(must_not_run) == report
    assert calls == 1


@pytest.mark.asyncio
async def test_reconciliation_once_serializes_concurrent_callers(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    orphan = _import_user_record(service, tmp_path, 10)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def owners() -> tuple[UserTemplateRegistryOwner, ...]:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return ()

    first = asyncio.create_task(service.reconcile_registry_orphans_once(owners))
    await entered.wait()
    second = asyncio.create_task(service.reconcile_registry_orphans_once(owners))
    release.set()
    first_report, second_report = await asyncio.gather(first, second)

    assert first_report == second_report
    assert first_report.deleted_orphans == (orphan,)
    assert calls == 1
    assert service.registry.list_templates() == ()


@pytest.mark.asyncio
async def test_owner_failure_or_cancellation_never_becomes_an_empty_snapshot(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    orphan = _import_user_record(service, tmp_path, 20)

    async def failed() -> tuple[UserTemplateRegistryOwner, ...]:
        raise RuntimeError("database unavailable")

    with pytest.raises(UserTemplateEvidenceError, match="could not be loaded"):
        await service.reconcile_registry_orphans_once(failed)
    assert _registry_keys(service) == {orphan}

    async def cancelled() -> tuple[UserTemplateRegistryOwner, ...]:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await service.reconcile_registry_orphans_once(cancelled)
    assert _registry_keys(service) == {orphan}

    async def loaded() -> tuple[UserTemplateRegistryOwner, ...]:
        return ()

    report = await service.reconcile_registry_orphans_once(loaded)
    assert report.deleted_orphans == (orphan,)


@pytest.mark.asyncio
async def test_cancellation_waits_for_started_reconciliation_and_caches_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    orphan = _import_user_record(service, tmp_path, 21)
    entered = threading.Event()
    release = threading.Event()
    original = service._reconcile_registry_orphans
    calls = 0

    def delayed(owners, max_records):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(timeout=5)
        return original(owners, max_records)

    async def no_owners() -> tuple[UserTemplateRegistryOwner, ...]:
        return ()

    monkeypatch.setattr(service, "_reconcile_registry_orphans", delayed)
    task = asyncio.create_task(service.reconcile_registry_orphans_once(no_owners))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls == 1
    assert _registry_keys(service) == set()

    async def must_not_reload() -> tuple[UserTemplateRegistryOwner, ...]:
        raise AssertionError("settled reconciliation result was not cached")

    report = await service.reconcile_registry_orphans_once(must_not_reload)
    assert report.deleted_orphans == (orphan,)
    assert calls == 1


@pytest.mark.asyncio
async def test_every_registry_record_is_validated_before_any_deletion(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    valid_orphan = _import_user_record(service, tmp_path, 30)
    corrupt = _import_user_record(service, tmp_path, 31)
    record_file = (
        service.registry.root
        / "records"
        / corrupt[0]
        / str(corrupt[1])
        / "record.json"
    )
    record_file.write_bytes(b"{}\n")

    async def owners() -> tuple[UserTemplateRegistryOwner, ...]:
        return ()

    with pytest.raises(TemplateIntegrityError):
        await service.reconcile_registry_orphans_once(owners)

    # The valid orphan sorted before the corrupt record was classified but was
    # not deleted because classification never mutates the registry.
    assert service.registry.read(*map(str, valid_orphan)).manifest.template_id == valid_orphan[0]


@pytest.mark.asyncio
async def test_budget_or_invalid_owner_snapshot_fails_closed_and_can_retry(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    first = _import_user_record(service, tmp_path, 40)
    second = _import_user_record(service, tmp_path, 41)

    async def no_owners() -> tuple[UserTemplateRegistryOwner, ...]:
        return ()

    with pytest.raises(UserTemplateEvidenceError, match="budget"):
        await service.reconcile_registry_orphans_once(no_owners, max_records=1)
    assert _registry_keys(service) == {first, second}

    async def too_many_owners() -> tuple[UserTemplateRegistryOwner, ...]:
        return (
            UserTemplateRegistryOwner(_template_ref(90), 1, "approved"),
            UserTemplateRegistryOwner(_template_ref(91), 1, "approved"),
        )

    with pytest.raises(ValueError, match="owner snapshot exceeds"):
        await service.reconcile_registry_orphans_once(
            too_many_owners,
            max_owner_records=1,
        )
    assert _registry_keys(service) == {first, second}

    report = await service.reconcile_registry_orphans_once(no_owners)
    assert set(report.deleted_orphans) == {first, second}
