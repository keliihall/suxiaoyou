"""Native Win32 contracts for durable workspace identity v2."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.workspace_instance import WorkspaceInstance
from app.storage.workspace_identity import (
    ensure_workspace_identity,
    inspect_workspace_identity,
)
from app.storage.workspace_identity_migration import (
    migrate_legacy_workspace_identities,
)
from app.tool.workspace import APP_PRIVATE_DIR_ENV
from app.utils.windows_guarded_file import windows_path_identity


pytestmark = [
    pytest.mark.workspace_identity_v2,
    pytest.mark.skipif(sys.platform != "win32", reason="requires native Win32 IDs"),
]


def test_native_file_id_is_stable_and_replacement_aware(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    native = windows_path_identity(workspace, directory=True)

    created = ensure_workspace_identity(workspace)
    inspected = inspect_workspace_identity(workspace)

    assert created == inspected
    assert created.durable_token == f"winfile-v2:{native[0]}:{native[1]}"
    assert created.volatile_identity == native
    assert not (workspace / ".suxiaoyou").exists()

    displaced = tmp_path / "workspace-old"
    workspace.rename(displaced)
    workspace.mkdir()
    replacement = ensure_workspace_identity(workspace)

    assert replacement.durable_token != created.durable_token


@pytest.mark.asyncio
async def test_native_stat_v1_row_migrates_to_winfile_v2(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    native = windows_path_identity(workspace, directory=True)
    legacy = f"stat-v1:{native[0]}:{native[1]}"
    async with session_factory() as db:
        async with db.begin():
            db.add(
                WorkspaceInstance(
                    id="native-windows-workspace",
                    root_path=str(workspace.resolve()),
                    identity_token=legacy,
                    status="active",
                    details={},
                )
            )
    monkeypatch.setenv(APP_PRIVATE_DIR_ENV, str(tmp_path / "private"))

    result = await migrate_legacy_workspace_identities(session_factory)

    assert result == {"migrated": 1, "missing": 0, "blocked": 0}
    async with session_factory() as db:
        instance = await db.get(WorkspaceInstance, "native-windows-workspace")
    assert instance is not None
    assert instance.identity_token == f"winfile-v2:{native[0]}:{native[1]}"
    assert not (workspace / ".suxiaoyou").exists()
