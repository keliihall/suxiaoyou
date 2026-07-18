from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.session import Session
from app.models.workspace_instance import WorkspaceInstance
from app.storage.checkpoints import CheckpointConflictError, create_root_turn
from app.worktree import (
    WorktreeActiveError,
    WorktreeFeatureDisabled,
    WorktreeRuntime,
    WorktreeService,
    WorktreeState,
)


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="Git is required for managed worktree runtime tests",
)


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-C",
            os.fspath(repository),
            *arguments,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        shell=False,
    )


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "source repository"
    root.mkdir()
    subprocess.run(
        ["git", "init", "--initial-branch=main", os.fspath(root)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        shell=False,
    )
    (root / "report.txt").write_text("version one\n", encoding="utf-8")
    _git(root, "add", "--", "report.txt")
    _git(
        root,
        "-c",
        "user.name=Runtime Test",
        "-c",
        "user.email=runtime@example.invalid",
        "commit",
        "-m",
        "initial",
    )
    return root


async def _create_session(
    session_factory: async_sessionmaker[AsyncSession],
    repository: Path,
) -> None:
    async with session_factory() as db:
        async with db.begin():
            db.add(
                Session(
                    id="session",
                    directory=str(repository),
                    title="worktree runtime",
                    version="1.1.0",
                )
            )


def _runtime(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    *,
    enabled: bool | None,
) -> tuple[WorktreeRuntime, WorktreeService]:
    service = WorktreeService(
        managed_root=tmp_path / "private" / "git-worktrees-v1",
        enabled=True,
    )
    return (
        WorktreeRuntime(
            session_factory,
            service,
            enabled=enabled,
        ),
        service,
    )


@pytest.mark.asyncio
async def test_runtime_gate_is_closed_before_git_or_database_mutation(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    repository: Path,
) -> None:
    await _create_session(session_factory, repository)
    runtime, service = _runtime(session_factory, tmp_path, enabled=False)

    with pytest.raises(WorktreeFeatureDisabled):
        await runtime.create_and_bind_session(
            session_id="session",
            repository=repository,
        )

    assert not service.managed_root.exists()
    async with session_factory() as db:
        assert await db.get(WorkspaceInstance, "session") is None


@pytest.mark.asyncio
async def test_runtime_binds_and_releases_one_database_owned_worktree(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    repository: Path,
) -> None:
    await _create_session(session_factory, repository)
    runtime, service = _runtime(session_factory, tmp_path, enabled=True)

    binding = await runtime.create_and_bind_session(
        session_id="session",
        repository=repository,
    )

    checkout = Path(binding.checkout_path)
    assert checkout.is_dir()
    assert binding.record.state is WorktreeState.BOUND
    assert binding.workspace_instance_id == binding.record.workspace_instance_id
    with pytest.raises(WorktreeActiveError, match="persistent reservation"):
        service.detach(binding.workspace_instance_id)
    async with session_factory() as db:
        session = await db.get(Session, "session")
        instance = await db.get(
            WorkspaceInstance,
            binding.workspace_instance_id,
        )
        parent = await db.get(
            WorkspaceInstance,
            binding.parent_workspace_instance_id,
        )
    assert session is not None and session.directory == str(checkout)
    assert instance is not None and instance.kind == "git_worktree"
    assert instance.details["worktree_state"] == "bound"
    assert parent is not None and parent.root_path == str(repository.resolve())

    released = await runtime.release_session(
        session_id="session",
        workspace_instance_id=binding.workspace_instance_id,
    )

    assert released.record.state is WorktreeState.REMOVED
    assert released.gc.collected == (binding.workspace_instance_id,)
    assert not checkout.exists()
    async with session_factory() as db:
        session = await db.get(Session, "session")
        instance = await db.get(
            WorkspaceInstance,
            binding.workspace_instance_id,
        )
    assert session is not None and session.directory == str(repository.resolve())
    assert instance is not None and instance.status == "released"
    assert instance.details["worktree_state"] == "removed"

    replay = await runtime.release_session(
        session_id="session",
        workspace_instance_id=binding.workspace_instance_id,
    )
    assert replay.record.to_json() == released.record.to_json()
    assert replay.gc == replay.gc.__class__()


@pytest.mark.asyncio
async def test_failed_dirty_release_stays_reserved_and_retryable(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    repository: Path,
) -> None:
    await _create_session(session_factory, repository)
    runtime, _service = _runtime(session_factory, tmp_path, enabled=True)
    binding = await runtime.create_and_bind_session(
        session_id="session",
        repository=repository,
    )
    checkout = Path(binding.checkout_path)
    dirty = checkout / "keep-me.txt"
    dirty.write_text("do not delete", encoding="utf-8")

    with pytest.raises(Exception, match="dirty managed worktree"):
        await runtime.release_session(
            session_id="session",
            workspace_instance_id=binding.workspace_instance_id,
        )

    assert dirty.read_text(encoding="utf-8") == "do not delete"
    async with session_factory() as db:
        session = await db.get(Session, "session")
        instance = await db.get(
            WorkspaceInstance,
            binding.workspace_instance_id,
        )
        assert session is not None and session.directory == str(repository.resolve())
        assert instance is not None and instance.status == "active"
        assert instance.details["worktree_state"] == "releasing"
        with pytest.raises(CheckpointConflictError, match="not accepting new turns"):
            await create_root_turn(
                db,
                session_id="session",
                workspace_instance_id=instance.id,
            )

    dirty.unlink()
    released = await runtime.release_session(
        session_id="session",
        workspace_instance_id=binding.workspace_instance_id,
    )
    assert released.record.state is WorktreeState.REMOVED
    assert not checkout.exists()
