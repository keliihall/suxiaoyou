from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException
import pytest

from app import release_features
from app.auth.local import require_local_session
from app.models.session import Session
from app.office_rendering import (
    OfficePreviewService,
    OfficeRenderCache,
    RendererDescriptor,
)
from app.storage.checkpoints import register_workspace_instance
from tests.test_office_rendering.helpers import FakeProvider


pytestmark = pytest.mark.asyncio


async def _install_service(app_client, session_factory, tmp_path: Path) -> tuple[str, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "report.docx"
    source.write_bytes(b"Office API source")
    async with session_factory() as db:
        async with db.begin():
            db.add(
                Session(
                    id="office-session",
                    directory=str(workspace.resolve()),
                    title="Office API",
                )
            )
            instance = await register_workspace_instance(
                db,
                workspace,
                kind="direct",
                created_by_session_id="office-session",
            )
            instance_id = instance.id
    descriptor = RendererDescriptor(
        renderer_id="api-test-renderer",
        renderer_version="1",
        font_digest="a" * 64,
        quality="approximate",
    )
    app_client.app.state.office_preview_service = OfficePreviewService(
        session_factory,
        cache=OfficeRenderCache((tmp_path / "office-cache").absolute()),
        provider=FakeProvider(descriptor),
        parameters_version="api-preview-v1",
        parameters={"dpi": 144},
        enabled=None,
    )
    return instance_id, source


async def test_office_v2_routes_are_dynamically_hidden_while_gate_is_closed(
    app_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release_features, "V11_OFFICE_V2_RELEASED", False)
    response = await app_client.get(
        "/api/office-v2/context",
        params={"session_id": "office-session"},
    )
    assert response.status_code == 404
    assert response.json()["code"] == "v11_office_v2_not_available"


async def test_office_v2_requires_local_session_and_rejects_path_authority(
    app_client,
    session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release_features, "V11_OFFICE_V2_RELEASED", True)
    instance_id, _source = await _install_service(
        app_client,
        session_factory,
        tmp_path,
    )

    def reject_non_local_request() -> None:
        raise HTTPException(status_code=403, detail="local desktop required")

    app_client.app.dependency_overrides[require_local_session] = reject_non_local_request
    try:
        denied = await app_client.get(
            "/api/office-v2/context",
            params={"session_id": "office-session"},
        )
    finally:
        app_client.app.dependency_overrides.pop(require_local_session, None)
    assert denied.status_code == 403

    forged = await app_client.post(
        "/api/office-v2/render",
        json={
            "session_id": "office-session",
            "workspace_instance_id": instance_id,
            "relative_path": "report.docx",
            "workspace_path": "/caller/selected/root",
        },
    )
    absolute = await app_client.post(
        "/api/office-v2/render",
        json={
            "session_id": "office-session",
            "workspace_instance_id": instance_id,
            "relative_path": "/outside/report.docx",
        },
    )
    assert forged.status_code == 422
    assert absolute.status_code == 409
    assert absolute.json()["code"] == "office_preview_provenance_mismatch"


async def test_office_context_render_page_and_rewind_style_staleness(
    app_client,
    session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release_features, "V11_OFFICE_V2_RELEASED", True)
    instance_id, source = await _install_service(
        app_client,
        session_factory,
        tmp_path,
    )

    context = await app_client.get(
        "/api/office-v2/context",
        params={"session_id": "office-session"},
    )
    assert context.status_code == 200
    assert context.json() == {
        "session_id": "office-session",
        "workspace_instance_id": instance_id,
        "renderer_available": True,
        "renderer_id": "api-test-renderer",
        "renderer_version": "1",
        "font_digest": "a" * 64,
        "preview_quality": "approximate",
        "formula_values_recalculated": False,
    }
    assert context.headers["cache-control"] == "no-store"

    validation = await app_client.get(
        "/api/office-v2/validation",
        params={
            "session_id": "office-session",
            "workspace_instance_id": instance_id,
            "relative_path": "report.docx",
        },
    )
    assert validation.status_code == 200
    assert validation.json()["status"] == "unvalidated"
    assert validation.json()["report"] is None
    assert validation.headers["cache-control"] == "no-store"

    rendered = await app_client.post(
        "/api/office-v2/render",
        json={
            "session_id": "office-session",
            "workspace_instance_id": instance_id,
            "relative_path": "report.docx",
        },
    )
    assert rendered.status_code == 200
    payload = rendered.json()
    assert payload["preview_quality"] == "approximate"
    assert payload["formula_values_recalculated"] is False
    assert payload["manifest"]["pages"][0]["page_number"] == 1
    assert payload["manifest"]["pdf"]["mime_type"] == "application/pdf"
    assert len(payload["manifest"]["pages"][0]["pixel_sha256"]) == 64

    validation = await app_client.get(
        "/api/office-v2/validation",
        params={
            "session_id": "office-session",
            "workspace_instance_id": instance_id,
            "relative_path": "report.docx",
        },
    )
    assert validation.status_code == 200
    assert validation.headers["cache-control"] == "no-store"
    assert validation.json() == {
        "session_id": "office-session",
        "workspace_instance_id": instance_id,
        "relative_path": "report.docx",
        "source_sha256": payload["source_sha256"],
        "status": "unvalidated",
        "stale_reason": None,
        "report": None,
    }

    query = {
        "session_id": "office-session",
        "workspace_instance_id": instance_id,
        "relative_path": "report.docx",
        "cache_key": payload["manifest"]["cache_key"],
        "page_number": 1,
    }
    page = await app_client.get("/api/office-v2/page", params=query)
    assert page.status_code == 200
    assert page.headers["content-type"] == "image/png"
    assert page.headers["cache-control"] == "no-store, max-age=0"
    assert page.content.startswith(b"\x89PNG\r\n\x1a\n")

    # A normal edit or rewind changes the source bytes. The old URL/cache key
    # must stop serving immediately instead of exposing a stale visual.
    source.write_bytes(b"rewound or edited source")
    stale = await app_client.get("/api/office-v2/page", params=query)
    assert stale.status_code == 409
    assert stale.json()["code"] == "office_preview_stale"


async def test_office_v2_never_leaks_renderer_diagnostics(
    app_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release_features, "V11_OFFICE_V2_RELEASED", True)
    response = await app_client.get(
        "/api/office-v2/context",
        params={"session_id": "missing"},
    )
    assert response.status_code == 503
    assert response.json() == {
        "code": "office_renderer_unavailable",
        "detail": "An approved local Office renderer is unavailable",
    }
    assert "/" not in response.json()["detail"]
