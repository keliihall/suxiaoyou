"""API coverage for session-scoped file history and explicit restore."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.models.session import Session
from app.storage.file_versions import FileVersionStore
from app.session.managed_workspace import managed_workspace_for_session
from app.tool.workspace import APP_PRIVATE_DIR_ENV

pytestmark = pytest.mark.asyncio


async def _create_session(session_factory, *, session_id: str, workspace: Path) -> None:
    async with session_factory() as db:
        async with db.begin():
            db.add(
                Session(
                    id=session_id,
                    slug=session_id,
                    directory=str(workspace),
                    title="Version test",
                )
            )


async def test_api_lists_and_restores_only_the_session_workspace(
    app_client,
    session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(APP_PRIVATE_DIR_ENV, str(tmp_path / "private"))
    workspace = tmp_path / "workspace"
    other_workspace = tmp_path / "other-workspace"
    workspace.mkdir()
    other_workspace.mkdir()
    await _create_session(session_factory, session_id="session-a", workspace=workspace)
    await _create_session(
        session_factory,
        session_id="session-b",
        workspace=other_workspace,
    )

    target = workspace / "report.txt"
    target.write_text("version one", encoding="utf-8")
    version = FileVersionStore(workspace).capture_before_mutation(
        target,
        operation="write",
        session_id="session-a",
    )
    assert version is not None
    target.write_text("version two", encoding="utf-8")

    listed = await app_client.get(
        "/api/file-versions",
        params={"session_id": "session-a", "file_path": str(target)},
    )
    assert listed.status_code == 200
    assert listed.json()["versions"][0]["id"] == version.id

    wrong_workspace = await app_client.post(
        f"/api/file-versions/{version.id}/restore",
        json={"session_id": "session-b"},
    )
    assert wrong_workspace.status_code == 404
    assert target.read_text(encoding="utf-8") == "version two"

    restored = await app_client.post(
        f"/api/file-versions/{version.id}/restore",
        json={"session_id": "session-a"},
    )
    assert restored.status_code == 200
    payload = restored.json()
    assert payload["restored_version"]["id"] == version.id
    assert payload["recovery_version"] is not None
    assert target.read_text(encoding="utf-8") == "version one"


async def test_api_rejects_path_escape_and_unknown_session(
    app_client,
    session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(APP_PRIVATE_DIR_ENV, str(tmp_path / "private"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    await _create_session(session_factory, session_id="session-safe", workspace=workspace)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")

    escaped = await app_client.get(
        "/api/file-versions",
        params={"session_id": "session-safe", "file_path": str(outside)},
    )
    assert escaped.status_code == 422

    missing = await app_client.get(
        "/api/file-versions",
        params={"session_id": "does-not-exist"},
    )
    assert missing.status_code == 404


async def test_api_accepts_inherited_folderless_managed_workspace(
    app_client,
    session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private = tmp_path / "private"
    monkeypatch.setenv(APP_PRIVATE_DIR_ENV, str(private))
    monkeypatch.setenv(
        "SUXIAOYOU_MANAGED_WORKSPACE_ROOT",
        str(private / "managed-workspaces"),
    )
    managed = managed_workspace_for_session("parent-session")
    async with session_factory() as db:
        async with db.begin():
            db.add_all(
                [
                    Session(
                        id="parent-session",
                        slug="parent",
                        directory=".",
                        title="Parent",
                    ),
                    Session(
                        id="child-session",
                        parent_id="parent-session",
                        slug="child",
                        directory=str(managed),
                        title="Child",
                    ),
                ]
            )
    target = managed / "suxiaoyou_written" / "child.txt"
    target.write_text("child version", encoding="utf-8")
    version = FileVersionStore(managed).capture_before_mutation(
        target,
        operation="write",
        session_id="child-session",
    )
    assert version is not None

    response = await app_client.get(
        "/api/file-versions",
        params={"session_id": "child-session"},
    )
    assert response.status_code == 200
    assert response.json()["versions"][0]["id"] == version.id
