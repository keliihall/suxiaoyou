from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException
import pytest
from sqlalchemy import func, select

from app import release_features
from app.api import office_user_templates as user_template_api
from app.auth.local import require_local_session
from app.models.office_user_template import OfficeUserTemplate
from app.models.session import Session
from app.office_rendering import (
    OfficePreviewService,
    OfficeRenderCache,
    RendererDescriptor,
)
from app.office_templates import user as user_template_service_module
from app.office_templates.user import UserOfficeTemplateService
from app.office_validation.draft import OfficeDraftValidationService
from app.office_validation.precommit import DeterministicOfficePrecommitCoordinator
from app.security.audit import AuditPersistenceError
from app.storage.checkpoints import register_workspace_instance
from tests.test_office_rendering.helpers import FakeProvider
from tests.test_office_templates.helpers import make_docx_template


pytestmark = pytest.mark.asyncio

_PLACEHOLDERS = ("body", "client", "footer", "header", "table")


class _NeverPolicies:
    def resolve_create(self, request: object) -> object:
        raise AssertionError("readiness probe must not resolve a policy")

    def resolve_edit(self, request: object, baseline: object) -> object:
        raise AssertionError("readiness probe must not resolve a policy")


def _release_beta(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "V11_CHECKPOINTS_RELEASED",
        "V11_REWIND_RELEASED",
        "V11_VALIDATION_AGENT_RELEASED",
        "V11_OFFICE_V2_RELEASED",
        "V11_USER_OFFICE_TEMPLATES_BETA_RELEASED",
    ):
        monkeypatch.setattr(release_features, name, True)


def _schema(*, value_type: str = "text") -> str:
    return json.dumps(
        [
            {
                "name": name,
                "type": value_type,
                "required": True,
                "min_chars": 1,
                "max_chars": 200,
                "description": f"Value for {name}",
            }
            for name in _PLACEHOLDERS
        ],
        separators=(",", ":"),
    )


async def _workspace(session_factory, tmp_path: Path, *, suffix: str = "one"):
    workspace = tmp_path / f"workspace-{suffix}"
    workspace.mkdir()
    session_id = f"template-session-{suffix}"
    async with session_factory() as db:
        async with db.begin():
            db.add(
                Session(
                    id=session_id,
                    directory=str(workspace.resolve()),
                    title="User Office templates",
                )
            )
            await db.flush()
            instance = await register_workspace_instance(
                db,
                workspace,
                kind="direct",
                created_by_session_id=session_id,
            )
            instance_id = instance.id
    return session_id, instance_id, workspace


def _install_service(
    app_client,
    tmp_path: Path,
    *,
    quality: str,
) -> FakeProvider:
    descriptor = RendererDescriptor(
        renderer_id=f"template-{quality}-renderer",
        renderer_version="1.0.0",
        font_digest=("a" if quality == "authoritative" else "b") * 64,
        quality=quality,  # type: ignore[arg-type]
    )
    provider = FakeProvider(descriptor)
    validation = OfficeDraftValidationService(
        cache=OfficeRenderCache((tmp_path / f"cache-{quality}").absolute()),
        provider=provider,
        parameters_version="user-template-test-v1",
        parameters={"dpi": 144},
    )
    app_client.app.state.office_user_template_service = UserOfficeTemplateService(
        (tmp_path / f"user-template-service-{quality}").absolute(),
        draft_validation=validation,
    )
    app_client.app.state.office_preview_service = SimpleNamespace(provider=provider)
    app_client.app.state.office_precommit_coordinator = (
        DeterministicOfficePrecommitCoordinator(
            service=validation,
            policies=_NeverPolicies(),
        )
    )
    app_client.app.state.validation_agent_service = object()
    return provider


def _import_request(
    *,
    session_id: str,
    workspace_instance_id: str,
    request_id: str = "import-1",
    display_name: str = "Quarterly template",
    schema: str | None = None,
    content: bytes | None = None,
) -> tuple[dict[str, str], dict[str, tuple[str, bytes, str]]]:
    return (
        {
            "session_id": session_id,
            "workspace_instance_id": workspace_instance_id,
            "client_request_id": request_id,
            "display_name": display_name,
            "placeholder_schema": schema or _schema(),
        },
        {
            "file": (
                "quarterly.docx",
                content or make_docx_template(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )


def _assert_path_free(payload: object, tmp_path: Path) -> None:
    serialized = json.dumps(payload, ensure_ascii=False)
    assert str(tmp_path) not in serialized
    assert "registry/objects" not in serialized
    assert "import-staging" not in serialized
    assert "workspace-" not in serialized


async def test_user_template_beta_is_composed_closed_and_local_only(
    app_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        release_features,
        "V11_USER_OFFICE_TEMPLATES_BETA_RELEASED",
        False,
    )
    closed = await app_client.get(
        "/api/office-v2/user-templates",
        params={
            "session_id": "missing-session",
            "workspace_instance_id": "missing-workspace",
        },
    )
    assert closed.status_code == 404
    assert closed.json()["code"] == "v11_user_office_templates_not_available"

    _release_beta(monkeypatch)

    def reject_non_local_request() -> None:
        raise HTTPException(status_code=403, detail="local desktop required")

    app_client.app.dependency_overrides[require_local_session] = (
        reject_non_local_request
    )
    try:
        denied = await app_client.get(
            "/api/office-v2/user-templates",
            params={
                "session_id": "missing-session",
                "workspace_instance_id": "missing-workspace",
            },
        )
    finally:
        app_client.app.dependency_overrides.pop(require_local_session, None)
    assert denied.status_code == 403


async def test_authoritative_import_idempotency_approval_cas_and_tombstone(
    app_client,
    session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _release_beta(monkeypatch)
    provider = _install_service(
        app_client,
        tmp_path,
        quality="authoritative",
    )
    session_id, instance_id, _workspace_path = await _workspace(
        session_factory,
        tmp_path,
    )
    data, files = _import_request(
        session_id=session_id,
        workspace_instance_id=instance_id,
    )

    imported = await app_client.post(
        "/api/office-v2/user-templates/import",
        data=data,
        files=files,
    )
    assert imported.status_code == 201, imported.text
    payload = imported.json()
    template = payload["template"]
    assert payload["idempotent"] is False
    assert template["revision"] == template["state_version"] == 1
    assert template["status"] == "needs_confirmation"
    assert template["can_approve"] is True
    assert template["can_instantiate"] is False
    assert template["allowed_operations"] == ["instantiate_text"]
    assert template["render_evidence"]["quality"] == "authoritative"
    assert template["validation_report"]["independent_reopen"] == "pass"
    _assert_path_free(payload, tmp_path)

    replay = await app_client.post(
        "/api/office-v2/user-templates/import",
        data=data,
        files=files,
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["idempotent"] is True
    assert replay.json()["template"]["template_ref"] == template["template_ref"]
    assert provider.calls == 1

    listed = await app_client.get(
        "/api/office-v2/user-templates",
        params={
            "session_id": session_id,
            "workspace_instance_id": instance_id,
        },
    )
    assert listed.status_code == 200
    assert [item["template_ref"] for item in listed.json()["templates"]] == [
        template["template_ref"]
    ]
    _assert_path_free(listed.json(), tmp_path)

    approval_body = {
        "session_id": session_id,
        "workspace_instance_id": instance_id,
        "revision": 1,
        "expected_state_version": 1,
        "expected_source_sha256": template["source"]["sha256"],
        "expected_render_cache_key": template["render_evidence"]["cache_key"],
    }
    approved = await app_client.post(
        f"/api/office-v2/user-templates/{template['template_ref']}/approve",
        json=approval_body,
    )
    assert approved.status_code == 200, approved.text
    approved_template = approved.json()["template"]
    assert approved_template["status"] == "approved"
    assert approved_template["state_version"] == 2
    assert approved_template["can_instantiate"] is True
    assert approved_template["time_approved"] is not None

    delattr(app_client.app.state, "validation_agent_service")
    not_ready = await app_client.get(
        "/api/office-v2/user-templates",
        params={
            "session_id": session_id,
            "workspace_instance_id": instance_id,
        },
    )
    assert not_ready.status_code == 200
    assert not_ready.json()["templates"][0]["can_instantiate"] is False
    app_client.app.state.validation_agent_service = object()

    original_descriptor = provider._descriptor
    provider._descriptor = RendererDescriptor(
        renderer_id="replacement-authoritative-renderer",
        renderer_version="2.0.0",
        font_digest="d" * 64,
        quality="authoritative",
    )
    renderer_changed = await app_client.get(
        "/api/office-v2/user-templates",
        params={
            "session_id": session_id,
            "workspace_instance_id": instance_id,
        },
    )
    assert renderer_changed.status_code == 200
    assert renderer_changed.json()["templates"][0]["can_instantiate"] is False
    provider._descriptor = original_descriptor

    # A durable DB owner alone is not enough to advertise availability.  The
    # immutable local registry source must still reopen and match its evidence.
    service = app_client.app.state.office_user_template_service
    service.registry.delete(template["template_ref"], "1")
    missing_registry = await app_client.get(
        "/api/office-v2/user-templates",
        params={
            "session_id": session_id,
            "workspace_instance_id": instance_id,
        },
    )
    assert missing_registry.status_code == 200
    assert missing_registry.json()["templates"][0]["can_instantiate"] is False

    stale = await app_client.post(
        f"/api/office-v2/user-templates/{template['template_ref']}/approve",
        json=approval_body,
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "user_office_template_conflict"

    deleted = await app_client.request(
        "DELETE",
        f"/api/office-v2/user-templates/{template['template_ref']}",
        json={
            "session_id": session_id,
            "workspace_instance_id": instance_id,
            "revision": 1,
            "expected_state_version": 2,
        },
    )
    assert deleted.status_code == 200, deleted.text
    deleted_template = deleted.json()["template"]
    assert deleted_template["status"] == "tombstoned"
    assert deleted_template["state_version"] == 3
    assert deleted_template["can_instantiate"] is False
    assert deleted_template["time_tombstoned"] is not None

    after_delete = await app_client.get(
        "/api/office-v2/user-templates",
        params={
            "session_id": session_id,
            "workspace_instance_id": instance_id,
        },
    )
    assert after_delete.json()["templates"] == []
    async with session_factory() as db:
        row = (
            await db.execute(
                select(OfficeUserTemplate).where(
                    OfficeUserTemplate.template_ref == template["template_ref"]
                )
            )
        ).scalar_one()
        assert row.revision == 1
        assert row.status == "tombstoned"


async def test_approximate_import_requires_review_and_cannot_be_approved(
    app_client,
    session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _release_beta(monkeypatch)
    _install_service(app_client, tmp_path, quality="approximate")
    session_id, instance_id, _workspace_path = await _workspace(
        session_factory,
        tmp_path,
    )
    data, files = _import_request(
        session_id=session_id,
        workspace_instance_id=instance_id,
    )
    imported = await app_client.post(
        "/api/office-v2/user-templates/import",
        data=data,
        files=files,
    )
    assert imported.status_code == 201, imported.text
    template = imported.json()["template"]
    assert template["status"] == "needs_review"
    assert template["can_approve"] is False
    assert template["validation_report"]["approval_eligible"] is False

    approval = await app_client.post(
        f"/api/office-v2/user-templates/{template['template_ref']}/approve",
        json={
            "session_id": session_id,
            "workspace_instance_id": instance_id,
            "revision": 1,
            "expected_state_version": 1,
            "expected_source_sha256": template["source"]["sha256"],
            "expected_render_cache_key": template["render_evidence"]["cache_key"],
        },
    )
    assert approval.status_code == 409
    async with session_factory() as db:
        row = await db.get(
            OfficeUserTemplate,
            (
                await db.execute(
                    select(OfficeUserTemplate.id).where(
                        OfficeUserTemplate.template_ref == template["template_ref"]
                    )
                )
            ).scalar_one(),
        )
        assert row is not None
        assert row.status == "needs_review"
        assert row.time_approved is None


async def test_import_rejects_schema_conflicts_and_cross_workspace_authority(
    app_client,
    session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _release_beta(monkeypatch)
    _install_service(app_client, tmp_path, quality="authoritative")
    session_id, instance_id, _workspace_path = await _workspace(
        session_factory,
        tmp_path,
        suffix="owner",
    )
    other_session, other_instance, _other_path = await _workspace(
        session_factory,
        tmp_path,
        suffix="other",
    )

    invalid_data, invalid_files = _import_request(
        session_id=session_id,
        workspace_instance_id=instance_id,
        schema=_schema(value_type="number"),
    )
    invalid = await app_client.post(
        "/api/office-v2/user-templates/import",
        data=invalid_data,
        files=invalid_files,
    )
    assert invalid.status_code == 422

    missing_schema = json.loads(_schema())
    missing_schema.append(
        {
            "name": "not_present",
            "type": "text",
            "required": True,
            "min_chars": 1,
            "max_chars": 20,
        }
    )
    missing_data, missing_files = _import_request(
        session_id=session_id,
        workspace_instance_id=instance_id,
        request_id="missing-placeholder",
        schema=json.dumps(missing_schema),
    )
    missing = await app_client.post(
        "/api/office-v2/user-templates/import",
        data=missing_data,
        files=missing_files,
    )
    assert missing.status_code == 422
    _assert_path_free(missing.json(), tmp_path)

    forged_data, forged_files = _import_request(
        session_id=other_session,
        workspace_instance_id=instance_id,
    )
    forged = await app_client.post(
        "/api/office-v2/user-templates/import",
        data=forged_data,
        files=forged_files,
    )
    assert forged.status_code == 409
    assert forged.json()["code"] == "user_office_template_provenance_mismatch"

    data, files = _import_request(
        session_id=session_id,
        workspace_instance_id=instance_id,
        request_id="same-key",
    )
    first = await app_client.post(
        "/api/office-v2/user-templates/import",
        data=data,
        files=files,
    )
    assert first.status_code == 201
    conflict_data, conflict_files = _import_request(
        session_id=session_id,
        workspace_instance_id=instance_id,
        request_id="same-key",
        display_name="Different immutable request",
    )
    conflict = await app_client.post(
        "/api/office-v2/user-templates/import",
        data=conflict_data,
        files=conflict_files,
    )
    assert conflict.status_code == 409

    cross_list = await app_client.get(
        "/api/office-v2/user-templates",
        params={
            "session_id": session_id,
            "workspace_instance_id": other_instance,
        },
    )
    assert cross_list.status_code == 409
    async with session_factory() as db:
        count = (
            await db.execute(select(func.count()).select_from(OfficeUserTemplate))
        ).scalar_one()
        assert count == 1


async def test_required_audit_failure_prevents_registry_and_db_mutation(
    app_client,
    session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _release_beta(monkeypatch)
    _install_service(app_client, tmp_path, quality="authoritative")
    session_id, instance_id, _workspace_path = await _workspace(
        session_factory,
        tmp_path,
        suffix="audit-failure",
    )
    data, files = _import_request(
        session_id=session_id,
        workspace_instance_id=instance_id,
        request_id="audit-failure-1",
    )
    original_audit = user_template_api.record_security_event

    async def unavailable(*args: Any, **kwargs: Any) -> None:
        if kwargs.get("required") is True:
            raise AuditPersistenceError("audit unavailable")

    monkeypatch.setattr(user_template_api, "record_security_event", unavailable)
    denied_import = await app_client.post(
        "/api/office-v2/user-templates/import",
        data=data,
        files=files,
    )
    assert denied_import.status_code == 503
    assert denied_import.json()["code"] == (
        "user_office_template_audit_unavailable"
    )
    service = app_client.app.state.office_user_template_service
    assert service.registry.list_templates() == ()
    async with session_factory() as db:
        assert (
            await db.execute(select(func.count()).select_from(OfficeUserTemplate))
        ).scalar_one() == 0

    monkeypatch.setattr(
        user_template_api,
        "record_security_event",
        original_audit,
    )
    data, files = _import_request(
        session_id=session_id,
        workspace_instance_id=instance_id,
        request_id="audit-failure-2",
    )
    imported = await app_client.post(
        "/api/office-v2/user-templates/import",
        data=data,
        files=files,
    )
    assert imported.status_code == 201
    template = imported.json()["template"]

    monkeypatch.setattr(user_template_api, "record_security_event", unavailable)
    approval = await app_client.post(
        f"/api/office-v2/user-templates/{template['template_ref']}/approve",
        json={
            "session_id": session_id,
            "workspace_instance_id": instance_id,
            "revision": 1,
            "expected_state_version": 1,
            "expected_source_sha256": template["source"]["sha256"],
            "expected_render_cache_key": template["render_evidence"]["cache_key"],
        },
    )
    assert approval.status_code == 503
    deletion = await app_client.request(
        "DELETE",
        f"/api/office-v2/user-templates/{template['template_ref']}",
        json={
            "session_id": session_id,
            "workspace_instance_id": instance_id,
            "revision": 1,
            "expected_state_version": 1,
        },
    )
    assert deletion.status_code == 503
    async with session_factory() as db:
        row = (
            await db.execute(
                select(OfficeUserTemplate).where(
                    OfficeUserTemplate.template_ref == template["template_ref"]
                )
            )
        ).scalar_one()
        assert row.status == "needs_confirmation"
        assert row.state_version == 1
        assert row.time_approved is None
        assert row.time_tombstoned is None


async def test_mutation_audit_is_path_free_and_preserves_idempotency(
    app_client,
    session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _release_beta(monkeypatch)
    _install_service(app_client, tmp_path, quality="authoritative")
    session_id, instance_id, _workspace_path = await _workspace(
        session_factory,
        tmp_path,
        suffix="audit-events",
    )
    events: list[dict[str, Any]] = []

    async def collect(*args: Any, **kwargs: Any) -> None:
        events.append(dict(kwargs))

    monkeypatch.setattr(user_template_api, "record_security_event", collect)
    data, files = _import_request(
        session_id=session_id,
        workspace_instance_id=instance_id,
        request_id="audit-events-1",
        display_name="Sensitive display name",
    )
    imported = await app_client.post(
        "/api/office-v2/user-templates/import",
        data=data,
        files=files,
    )
    assert imported.status_code == 201
    replay = await app_client.post(
        "/api/office-v2/user-templates/import",
        data=data,
        files=files,
    )
    assert replay.status_code == 200
    assert replay.json()["idempotent"] is True
    template = imported.json()["template"]
    approval_body = {
        "session_id": session_id,
        "workspace_instance_id": instance_id,
        "revision": 1,
        "expected_state_version": 1,
        "expected_source_sha256": template["source"]["sha256"],
        "expected_render_cache_key": template["render_evidence"]["cache_key"],
    }
    approved = await app_client.post(
        f"/api/office-v2/user-templates/{template['template_ref']}/approve",
        json=approval_body,
    )
    assert approved.status_code == 200
    idempotent_approval = await app_client.post(
        f"/api/office-v2/user-templates/{template['template_ref']}/approve",
        json={**approval_body, "expected_state_version": 2},
    )
    assert idempotent_approval.status_code == 200
    assert idempotent_approval.json()["idempotent"] is True
    deleted = await app_client.request(
        "DELETE",
        f"/api/office-v2/user-templates/{template['template_ref']}",
        json={
            "session_id": session_id,
            "workspace_instance_id": instance_id,
            "revision": 1,
            "expected_state_version": 2,
        },
    )
    assert deleted.status_code == 200

    required = [event for event in events if event.get("required") is True]
    assert [(item["action"], item["outcome"]) for item in required] == [
        ("import", "started"),
        ("import", "started"),
        ("approve", "started"),
        ("approve", "started"),
        ("delete", "started"),
    ]
    successes = [event for event in events if event["outcome"] == "success"]
    assert [item["details"].get("idempotent") for item in successes] == [
        False,
        True,
        False,
        True,
        False,
    ]
    allowed = {
        "workspace_instance_id",
        "template_ref",
        "revision",
        "state_version",
        "format",
        "idempotent",
    }
    assert all(set(event["details"]).issubset(allowed) for event in events)
    serialized = json.dumps(events, ensure_ascii=False, default=str)
    assert str(tmp_path) not in serialized
    assert "Sensitive display name" not in serialized
    assert "quarterly.docx" not in serialized
    assert template["source"]["sha256"] not in serialized
    assert template["render_evidence"]["cache_key"] not in serialized


async def test_initial_reconciliation_serializes_full_import_publication(
    app_client,
    session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _release_beta(monkeypatch)
    provider = _install_service(app_client, tmp_path, quality="authoritative")
    session_id, instance_id, _workspace_path = await _workspace(
        session_factory,
        tmp_path,
        suffix="reconcile-import-race",
    )
    service = app_client.app.state.office_user_template_service
    original = service.reconcile_registry_orphans_once
    entered = asyncio.Event()
    release = asyncio.Event()
    reconciliation_calls = 0

    async def delayed(loader: Any) -> Any:
        nonlocal reconciliation_calls
        reconciliation_calls += 1
        entered.set()
        await release.wait()
        return await original(loader)

    monkeypatch.setattr(service, "reconcile_registry_orphans_once", delayed)
    first_data, first_files = _import_request(
        session_id=session_id,
        workspace_instance_id=instance_id,
        request_id="race-first",
    )
    second_data, second_files = _import_request(
        session_id=session_id,
        workspace_instance_id=instance_id,
        request_id="race-second",
    )
    first = asyncio.create_task(
        app_client.post(
            "/api/office-v2/user-templates/import",
            data=first_data,
            files=first_files,
        )
    )
    await entered.wait()
    second = asyncio.create_task(
        app_client.post(
            "/api/office-v2/user-templates/import",
            data=second_data,
            files=second_files,
        )
    )
    await asyncio.sleep(0)
    assert not first.done()
    assert not second.done()
    assert provider.calls == 0

    release.set()
    first_response, second_response = await asyncio.gather(first, second)
    assert first_response.status_code == 201, first_response.text
    assert second_response.status_code == 201, second_response.text
    assert reconciliation_calls == 1
    async with session_factory() as db:
        count = (
            await db.execute(select(func.count()).select_from(OfficeUserTemplate))
        ).scalar_one()
    assert count == 2


async def test_read_only_list_never_runs_orphan_reconciliation(
    app_client,
    session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _release_beta(monkeypatch)
    _install_service(app_client, tmp_path, quality="authoritative")
    session_id, instance_id, _workspace_path = await _workspace(
        session_factory,
        tmp_path,
        suffix="read-only-list",
    )
    service = app_client.app.state.office_user_template_service

    async def forbidden_reconciliation(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("GET must not run deletion-capable reconciliation")

    monkeypatch.setattr(
        service,
        "reconcile_registry_orphans_once",
        forbidden_reconciliation,
    )
    listed = await app_client.get(
        "/api/office-v2/user-templates",
        params={
            "session_id": session_id,
            "workspace_instance_id": instance_id,
        },
    )

    assert listed.status_code == 200, listed.text
    assert listed.json()["templates"] == []
    assert not hasattr(
        app_client.app.state,
        "office_user_template_reconciled_service",
    )


async def test_global_owner_snapshot_is_bounded_in_sql() -> None:
    statements: list[Any] = []

    class EmptyRows:
        def all(self) -> list[Any]:
            return []

    class FakeSession:
        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def execute(self, statement: Any) -> EmptyRows:
            statements.append(statement)
            return EmptyRows()

    def session_factory() -> FakeSession:
        return FakeSession()

    owners = await user_template_api._load_global_template_owners(
        session_factory,  # type: ignore[arg-type]
    )

    assert owners == ()
    assert len(statements) == 1
    compiled = str(
        statements[0].compile(compile_kwargs={"literal_binds": True})
    )
    expected_limit = (
        user_template_api.USER_TEMPLATE_RECONCILIATION_MAX_OWNERS + 1
    )
    assert f"LIMIT {expected_limit}" in compiled


async def test_cancelled_import_records_cancelled_and_propagates(
    app_client,
    session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _release_beta(monkeypatch)
    _install_service(app_client, tmp_path, quality="authoritative")
    session_id, instance_id, _workspace_path = await _workspace(
        session_factory,
        tmp_path,
        suffix="audit-cancelled",
    )
    # Complete once-reconciliation first so the controlled pause below is
    # strictly after the required pre-action audit and before registry import.
    listed = await app_client.get(
        "/api/office-v2/user-templates",
        params={
            "session_id": session_id,
            "workspace_instance_id": instance_id,
        },
    )
    assert listed.status_code == 200
    service = app_client.app.state.office_user_template_service
    entered = asyncio.Event()
    never = asyncio.Event()

    async def blocked_import(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await never.wait()
        raise AssertionError("cancelled import unexpectedly resumed")

    events: list[dict[str, Any]] = []

    async def collect(*args: Any, **kwargs: Any) -> None:
        events.append(dict(kwargs))

    monkeypatch.setattr(service, "validate_and_register", blocked_import)
    monkeypatch.setattr(user_template_api, "record_security_event", collect)
    data, files = _import_request(
        session_id=session_id,
        workspace_instance_id=instance_id,
        request_id="audit-cancelled",
    )
    request_task = asyncio.create_task(
        app_client.post(
            "/api/office-v2/user-templates/import",
            data=data,
            files=files,
        )
    )
    await entered.wait()
    request_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request_task

    assert [(event["outcome"], event["required"]) for event in events] == [
        ("started", True),
        ("cancelled", False),
    ]
    assert service.registry.list_templates() == ()
    async with session_factory() as db:
        count = (
            await db.execute(select(func.count()).select_from(OfficeUserTemplate))
        ).scalar_one()
        assert count == 0


@pytest.mark.parametrize(
    ("quality", "expected_status"),
    [
        ("authoritative", "needs_confirmation"),
        ("approximate", "needs_review"),
    ],
)
async def test_private_preview_revalidates_full_evidence_without_paths(
    quality: str,
    expected_status: str,
    app_client,
    session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _release_beta(monkeypatch)
    _install_service(app_client, tmp_path, quality=quality)
    session_id, instance_id, _workspace_path = await _workspace(
        session_factory,
        tmp_path,
        suffix=f"preview-{quality}",
    )
    data, files = _import_request(
        session_id=session_id,
        workspace_instance_id=instance_id,
        request_id=f"preview-{quality}",
    )
    imported = await app_client.post(
        "/api/office-v2/user-templates/import",
        data=data,
        files=files,
    )
    assert imported.status_code == 201, imported.text
    template = imported.json()["template"]
    assert template["status"] == expected_status
    assert template["can_instantiate"] is False
    params = {
        "session_id": session_id,
        "workspace_instance_id": instance_id,
        "revision": 1,
        "expected_state_version": 1,
        "page_number": 1,
    }
    route = (
        f"/api/office-v2/user-templates/{template['template_ref']}/page"
    )
    page = await app_client.get(route, params=params)
    assert page.status_code == 200, page.text
    assert page.headers["content-type"] == "image/png"
    assert page.headers["cache-control"].startswith("no-store")
    assert page.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in page.headers["content-security-policy"]
    assert page.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert str(tmp_path) not in json.dumps(dict(page.headers), ensure_ascii=False)

    original_read = user_template_service_module._read_private_regular_file

    def forged_page(_path: Path, _maximum: int) -> bytes:
        return b"forged-render-page"

    monkeypatch.setattr(
        user_template_service_module,
        "_read_private_regular_file",
        forged_page,
    )
    forged = await app_client.get(route, params=params)
    assert forged.status_code == 409
    assert forged.json()["code"] == "user_office_template_evidence_invalid"
    monkeypatch.setattr(
        user_template_service_module,
        "_read_private_regular_file",
        original_read,
    )

    stale = await app_client.get(
        route,
        params={**params, "expected_state_version": 2},
    )
    assert stale.status_code == 409
    missing_page = await app_client.get(
        route,
        params={**params, "page_number": 2},
    )
    assert missing_page.status_code == 404

    other_session, other_instance, _other_workspace = await _workspace(
        session_factory,
        tmp_path,
        suffix=f"preview-other-{quality}",
    )
    cross_workspace = await app_client.get(
        route,
        params={
            **params,
            "session_id": other_session,
            "workspace_instance_id": other_instance,
        },
    )
    assert cross_workspace.status_code == 404

    cache_key = template["render_evidence"]["cache_key"]
    cache_page = (
        tmp_path
        / f"cache-{quality}"
        / "entries"
        / cache_key[:2]
        / cache_key
        / "page-1.png"
    )
    cache_page.write_bytes(b"corrupt")
    corrupted = await app_client.get(route, params=params)
    assert corrupted.status_code == 409
    _assert_path_free(corrupted.json(), tmp_path)


async def test_lazy_approximate_preview_service_never_reports_instantiation_ready(
    app_client,
    session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _release_beta(monkeypatch)
    private = tmp_path / "lazy-private"
    private.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    provider = FakeProvider(
        RendererDescriptor(
            renderer_id="lazy-approximate-renderer",
            renderer_version="1.0.0",
            font_digest="c" * 64,
            quality="approximate",
        )
    )
    app_client.app.state.office_preview_service = OfficePreviewService(
        session_factory,
        cache=OfficeRenderCache((tmp_path / "lazy-cache").absolute()),
        provider=provider,
        parameters_version="lazy-preview-v1",
        parameters={"dpi": 144},
        enabled=None,
    )
    app_client.app.state.office_precommit_coordinator = object()
    app_client.app.state.validation_agent_service = object()
    try:
        delattr(app_client.app.state, "office_user_template_service")
    except (AttributeError, KeyError):
        pass
    session_id, instance_id, _workspace_path = await _workspace(
        session_factory,
        tmp_path,
        suffix="lazy-approximate",
    )
    data, files = _import_request(
        session_id=session_id,
        workspace_instance_id=instance_id,
        request_id="lazy-approximate",
    )
    imported = await app_client.post(
        "/api/office-v2/user-templates/import",
        data=data,
        files=files,
    )

    assert imported.status_code == 201, imported.text
    template = imported.json()["template"]
    assert template["status"] == "needs_review"
    assert template["can_instantiate"] is False
    assert isinstance(
        app_client.app.state.office_user_template_service,
        UserOfficeTemplateService,
    )
