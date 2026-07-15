from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.schemas.agent import AgentInfo
from app.storage import file_versions as file_versions_module
from app.storage.file_versions import FileVersionStore
from app.tool.context import ToolContext
from app.tool import workspace_transaction as transaction_module
from app.tool.workspace_transaction import (
    WorkspaceMutationError,
    WorkspaceMutationTransaction,
    recover_pending_workspace_transactions,
)


def _context(workspace: Path) -> ToolContext:
    return ToolContext(
        session_id="session",
        message_id="message",
        agent=AgentInfo(name="test", description="", mode="primary"),
        call_id="call",
        workspace=str(workspace),
    )


def _transaction(
    workspace: Path,
    private: Path,
) -> WorkspaceMutationTransaction:
    return WorkspaceMutationTransaction(
        workspace,
        _context(workspace),
        operation="test.command",
        storage_root=private,
    )


@pytest.mark.skipif(
    transaction_module.guarded_file_mutation_unavailable_reason() is not None,
    reason="guarded mutation primitive unavailable",
)
def test_targeted_staging_ignores_unrelated_large_and_special_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    oversized = workspace / "unrelated-large.bin"
    oversized.touch()
    os.truncate(
        oversized,
        transaction_module.MAX_STAGED_WORKSPACE_BYTES + 1,
    )
    fifo = workspace / "unrelated-pipe"
    if hasattr(os, "mkfifo"):
        os.mkfifo(fifo)

    target = workspace / "nested" / "result.txt"
    transaction = _transaction(workspace, private)
    staged = transaction.prepare_paths([target])

    assert not (staged / oversized.name).exists()
    assert not (staged / fifo.name).exists()
    staged_target = transaction.staged_path(target)
    staged_target.parent.mkdir(parents=True)
    staged_target.write_text("done", encoding="utf-8")
    transaction.commit()

    assert target.read_text(encoding="utf-8") == "done"
    assert oversized.stat().st_size == transaction_module.MAX_STAGED_WORKSPACE_BYTES + 1
    if hasattr(os, "mkfifo"):
        assert fifo.exists()


@pytest.mark.skipif(
    transaction_module.guarded_file_mutation_unavailable_reason() is not None,
    reason="guarded mutation primitive unavailable",
)
def test_targeted_read_dependency_is_staged_but_never_publishable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    image = workspace / "image.png"
    image.write_bytes(b"original")
    target = workspace / "output.docx"
    transaction = _transaction(workspace, private)
    staged = transaction.prepare_paths([target], read_paths=[image])

    assert (staged / "image.png").read_bytes() == b"original"
    (staged / "image.png").write_bytes(b"mutated")
    (staged / "output.docx").write_bytes(b"output")

    with pytest.raises(WorkspaceMutationError, match="undeclared path"):
        transaction.commit()
    assert image.read_bytes() == b"original"
    assert not target.exists()


@pytest.mark.skipif(
    transaction_module.guarded_file_mutation_unavailable_reason() is not None,
    reason="guarded mutation primitive unavailable",
)
def test_targeted_read_dependency_change_conflicts_before_output_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    image = workspace / "image.png"
    image.write_bytes(b"original")
    target = workspace / "output.docx"
    transaction = _transaction(workspace, private)
    staged = transaction.prepare_paths([target], read_paths=[image])
    (staged / "output.docx").write_bytes(b"output")
    image.write_bytes(b"concurrent edit")

    with pytest.raises(WorkspaceMutationError, match="declared path changed"):
        transaction.commit()
    assert not target.exists()
    assert image.read_bytes() == b"concurrent edit"


@pytest.mark.skipif(
    transaction_module.guarded_file_mutation_unavailable_reason() is not None,
    reason="guarded mutation primitive unavailable",
)
def test_targeted_multi_file_commit_rolls_back_first_install_on_second_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    first = workspace / "a.txt"
    second = workspace / "b.txt"
    first.write_text("a-before", encoding="utf-8")
    second.write_text("b-before", encoding="utf-8")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare_paths([first, second])
    (staged / "a.txt").write_text("a-after", encoding="utf-8")
    (staged / "b.txt").write_text("b-after", encoding="utf-8")
    real_install = transaction_module._exchange_prepared_at
    calls = 0

    def fail_second(workspace_fd: int, temporary: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated second install failure")
        real_install(workspace_fd, temporary)

    monkeypatch.setattr(transaction_module, "_exchange_prepared_at", fail_second)

    with pytest.raises(WorkspaceMutationError, match="rolled back"):
        transaction.commit()
    transaction.abort()

    assert first.read_text(encoding="utf-8") == "a-before"
    assert second.read_text(encoding="utf-8") == "b-before"


@pytest.mark.skipif(
    transaction_module.guarded_file_mutation_unavailable_reason() is not None,
    reason="guarded mutation primitive unavailable",
)
def test_targeted_read_dependency_is_rechecked_after_output_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    image = workspace / "image.png"
    image.write_bytes(b"original")
    target = workspace / "output.docx"
    transaction = _transaction(workspace, private)
    staged = transaction.prepare_paths([target], read_paths=[image])
    (staged / "output.docx").write_bytes(b"derived output")
    real_install = transaction_module._link_prepared_new_at

    def install_then_change_dependency(workspace_fd: int, temporary: object) -> None:
        real_install(workspace_fd, temporary)
        image.write_bytes(b"concurrent edit")

    monkeypatch.setattr(
        transaction_module,
        "_link_prepared_new_at",
        install_then_change_dependency,
    )

    with pytest.raises(WorkspaceMutationError, match="rolled back"):
        transaction.commit()
    transaction.abort()

    assert not target.exists()
    assert image.read_bytes() == b"concurrent edit"


def test_stages_then_versions_and_commits_create_modify_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    existing = workspace / "existing.txt"
    removed = workspace / "removed.txt"
    existing.write_text("before", encoding="utf-8")
    removed.write_text("remove me", encoding="utf-8")
    transaction = _transaction(workspace, private)

    staged = transaction.prepare()
    (staged / "existing.txt").write_text("after", encoding="utf-8")
    (staged / "removed.txt").unlink()
    (staged / "nested").mkdir()
    (staged / "nested" / "created.txt").write_text("new", encoding="utf-8")

    assert existing.read_text(encoding="utf-8") == "before"
    assert removed.exists()
    assert not (workspace / "nested").exists()

    result = transaction.commit()

    assert existing.read_text(encoding="utf-8") == "after"
    assert not removed.exists()
    assert (workspace / "nested" / "created.txt").read_text(encoding="utf-8") == "new"
    assert set(result.written_files) == {
        str(existing),
        str(workspace / "nested" / "created.txt"),
    }
    assert result.deleted_files == (str(removed),)
    versions = FileVersionStore(workspace).list_versions(limit=10)
    assert {version.relative_path for version in versions} == {
        "existing.txt",
        "removed.txt",
    }
    assert len(result.previous_version_ids) == 2
    assert result.metadata["recovery_files"] == list(result.recovery_sidecars)
    assert {
        Path(path).read_text(encoding="utf-8") for path in result.recovery_sidecars
    } == {"before", "remove me"}


def test_commit_keeps_old_open_fd_writes_reachable_in_recovery_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    target = workspace / "target.txt"
    target.write_bytes(b"before")
    descriptor = os.open(target, os.O_RDWR)
    try:
        transaction = _transaction(workspace, private)
        staged = transaction.prepare()
        (staged / target.name).write_bytes(b"command")

        result = transaction.commit()

        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(descriptor, b"late-fd-write")
        os.ftruncate(descriptor, len(b"late-fd-write"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

    assert target.read_bytes() == b"command"
    assert result.recovery_sidecars == tuple(result.metadata["recovery_sidecars"])
    assert any(Path(path).read_bytes() == b"late-fd-write" for path in result.recovery_sidecars)


def test_abort_discards_every_staged_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    target = workspace / "target.txt"
    target.write_text("before", encoding="utf-8")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / "target.txt").write_text("uncommitted", encoding="utf-8")
    (staged / "new.txt").write_text("uncommitted", encoding="utf-8")

    transaction.abort()

    assert target.read_text(encoding="utf-8") == "before"
    assert not (workspace / "new.txt").exists()
    assert FileVersionStore(workspace).list_versions() == []


def test_external_change_conflict_leaves_external_contents_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    target = workspace / "target.txt"
    target.write_text("before", encoding="utf-8")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / "target.txt").write_text("command", encoding="utf-8")
    target.write_text("external", encoding="utf-8")

    with pytest.raises(WorkspaceMutationError, match="outside the command transaction"):
        transaction.commit()

    transaction.abort()
    assert target.read_text(encoding="utf-8") == "external"
    assert FileVersionStore(workspace).list_versions() == []


def test_concurrently_created_output_is_never_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / "new.txt").write_text("command", encoding="utf-8")
    (workspace / "new.txt").write_text("external", encoding="utf-8")

    with pytest.raises(WorkspaceMutationError, match="created outside"):
        transaction.commit()

    transaction.abort()
    assert (workspace / "new.txt").read_text(encoding="utf-8") == "external"


def test_parent_symlink_swap_cannot_redirect_commit_outside_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    folder = workspace / "folder"
    folder.mkdir()
    target = folder / "target.txt"
    target.write_text("before", encoding="utf-8")
    outside_target = outside / "target.txt"
    outside_target.write_text("outside", encoding="utf-8")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / "folder" / "target.txt").write_text("command", encoding="utf-8")

    saved = workspace / "saved-folder"
    folder.rename(saved)
    try:
        folder.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("filesystem does not support directory symlinks")

    with pytest.raises(WorkspaceMutationError, match="redirected|changed"):
        transaction.commit()
    transaction.abort()

    assert outside_target.read_text(encoding="utf-8") == "outside"
    assert (saved / "target.txt").read_text(encoding="utf-8") == "before"


def test_workspace_root_swap_during_version_capture_fails_before_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    target = workspace / "target.txt"
    target.write_text("before", encoding="utf-8")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / target.name).write_text("command", encoding="utf-8")
    moved = tmp_path / "moved-workspace"
    real_capture = FileVersionStore.capture_batch_before_mutation

    def swap_root_then_capture(store: FileVersionStore, *args, **kwargs):
        workspace.rename(moved)
        workspace.mkdir()
        (workspace / "target.txt").write_text("replacement root", encoding="utf-8")
        return real_capture(store, *args, **kwargs)

    monkeypatch.setattr(
        FileVersionStore,
        "capture_batch_before_mutation",
        swap_root_then_capture,
    )

    with pytest.raises(WorkspaceMutationError, match="Workspace root changed"):
        transaction.commit()
    transaction.abort()
    assert (moved / "target.txt").read_text(encoding="utf-8") == "before"
    assert (workspace / "target.txt").read_text(encoding="utf-8") == "replacement root"


def test_incomplete_capture_batch_never_reaches_journal_or_workspace_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    target = workspace / "target.txt"
    target.write_text("before", encoding="utf-8")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / target.name).write_text("command", encoding="utf-8")
    real_capture = FileVersionStore.capture_batch_before_mutation

    def omit_capture_result(store: FileVersionStore, *args, **kwargs):
        real_capture(store, *args, **kwargs)
        return []

    monkeypatch.setattr(
        FileVersionStore,
        "capture_batch_before_mutation",
        omit_capture_result,
    )

    with pytest.raises(WorkspaceMutationError, match="complete command mutation batch"):
        transaction.commit()
    transaction.abort()
    assert target.read_text(encoding="utf-8") == "before"
    assert not list(private.glob("execution-transactions/*/*/journal-v1.json"))


def test_new_output_fault_after_atomic_install_is_rolled_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / "new.txt").write_text("command", encoding="utf-8")

    def fail_after_install(_workspace_fd: int, _relative: str) -> None:
        raise OSError("simulated directory fsync failure")

    monkeypatch.setattr(transaction_module, "_fsync_parent_at", fail_after_install)

    with pytest.raises(WorkspaceMutationError, match="rolled back"):
        transaction.commit()
    transaction.abort()
    assert not (workspace / "new.txt").exists()
    assert any(
        path.read_text(encoding="utf-8") == "command"
        for path in workspace.glob(".new.txt.suyo-tx-*")
    )


def test_recursive_nonempty_directory_delete_is_rejected_before_versioning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    folder = workspace / "folder"
    folder.mkdir()
    (folder / "data.txt").write_text("keep", encoding="utf-8")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / "folder" / "data.txt").unlink()
    (staged / "folder").rmdir()

    with pytest.raises(WorkspaceMutationError, match="non-empty baseline directory"):
        transaction.commit()
    transaction.abort()
    assert (folder / "data.txt").read_text(encoding="utf-8") == "keep"
    assert FileVersionStore(workspace).list_versions() == []


def test_empty_directory_rollback_never_chmods_a_user_recreation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX directory modes are required")
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    folder = workspace / "empty"
    folder.mkdir(mode=0o700)
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / folder.name).rmdir()
    real_remove = transaction_module._remove_directory_at
    injected = False

    def remove_then_recreate(workspace_fd: int, relative: str) -> None:
        nonlocal injected
        real_remove(workspace_fd, relative)
        if not injected and relative == folder.name:
            injected = True
            folder.mkdir(mode=0o755)
            folder.chmod(0o755)

    monkeypatch.setattr(transaction_module, "_remove_directory_at", remove_then_recreate)

    with pytest.raises(WorkspaceMutationError, match="rollback failed"):
        transaction.commit()

    assert folder.is_dir()
    assert folder.stat().st_mode & 0o777 == 0o755


def test_edit_after_atomic_exchange_is_preserved_when_rollback_refuses_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    target = workspace / "target.txt"
    target.write_text("before", encoding="utf-8")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / target.name).write_text("command", encoding="utf-8")
    real_exchange = transaction_module._exchange_prepared_at
    calls = 0

    def exchange_then_edit(workspace_fd: int, temporary: object) -> None:
        nonlocal calls
        calls += 1
        real_exchange(workspace_fd, temporary)
        if calls == 1:
            target.write_text("later user edit", encoding="utf-8")

    monkeypatch.setattr(transaction_module, "_exchange_prepared_at", exchange_then_edit)

    with pytest.raises(WorkspaceMutationError, match="rollback failed"):
        transaction.commit()
    assert calls == 2
    assert target.read_text(encoding="utf-8") == "before"
    conflict_values = {
        path.read_text(encoding="utf-8")
        for path in workspace.glob(".target.txt.suyo-tx-*")
    }
    assert "later user edit" in conflict_values


def test_new_external_hardlink_at_exchange_refuses_automatic_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    target = workspace / "target.txt"
    target.write_text("before", encoding="utf-8")
    outside_link = tmp_path / "outside-link.txt"
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / target.name).write_text("command", encoding="utf-8")
    real_exchange = transaction_module._exchange_prepared_at
    injected = False

    def link_then_exchange(workspace_fd: int, temporary: object) -> None:
        nonlocal injected
        if not injected:
            injected = True
            os.link(target, outside_link)
        real_exchange(workspace_fd, temporary)

    monkeypatch.setattr(transaction_module, "_exchange_prepared_at", link_then_exchange)

    with pytest.raises(WorkspaceMutationError, match="rollback failed"):
        transaction.commit()
    assert target.read_text(encoding="utf-8") == "command"
    assert outside_link.read_text(encoding="utf-8") == "before"
    conflict_values = {
        path.read_text(encoding="utf-8")
        for path in workspace.glob(".target.txt.suyo-tx-*")
    }
    assert "before" in conflict_values


def test_parent_moved_after_exchange_never_mutates_replacement_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    folder = workspace / "folder"
    folder.mkdir()
    target = folder / "target.txt"
    target.write_text("before", encoding="utf-8")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / "folder" / "target.txt").write_text("command", encoding="utf-8")
    moved = workspace / "moved-folder"
    real_rename = transaction_module._renameat_with_flags
    injected = False

    def exchange_then_move_parent(*args, **kwargs) -> None:
        nonlocal injected
        real_rename(*args, **kwargs)
        if kwargs.get("exchange") and not injected:
            injected = True
            folder.rename(moved)
            folder.mkdir()
            (folder / "target.txt").write_text("replacement parent", encoding="utf-8")

    monkeypatch.setattr(
        transaction_module,
        "_renameat_with_flags",
        exchange_then_move_parent,
    )

    with pytest.raises(WorkspaceMutationError, match="rollback failed"):
        transaction.commit()
    assert (folder / "target.txt").read_text(encoding="utf-8") == "replacement parent"
    assert (moved / "target.txt").read_text(encoding="utf-8") == "command"


def test_delete_conflict_restores_exact_atomically_captured_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    target = workspace / "target.txt"
    target.write_text("before", encoding="utf-8")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / target.name).unlink()
    real_read = transaction_module._read_prepared_entry
    injected = False

    def replace_captured_object(workspace_fd: int, temporary: object):
        nonlocal injected
        if not injected:
            injected = True
            (workspace / temporary.temporary_name).write_text(
                "concurrent object",
                encoding="utf-8",
            )
        return real_read(workspace_fd, temporary)

    monkeypatch.setattr(
        transaction_module,
        "_read_prepared_entry",
        replace_captured_object,
    )

    with pytest.raises(WorkspaceMutationError, match="rolled back"):
        transaction.commit()
    transaction.abort()
    assert target.read_text(encoding="utf-8") == "concurrent object"


def test_commit_failure_rolls_back_already_installed_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    first = workspace / "a.txt"
    second = workspace / "b.txt"
    first.write_text("a-before", encoding="utf-8")
    second.write_text("b-before", encoding="utf-8")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / "a.txt").write_text("a-after", encoding="utf-8")
    (staged / "b.txt").write_text("b-after", encoding="utf-8")
    real_install = transaction_module._exchange_prepared_at
    calls = 0

    def fail_second(workspace_fd: int, temporary: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated second install failure")
        real_install(workspace_fd, temporary)

    monkeypatch.setattr(transaction_module, "_exchange_prepared_at", fail_second)

    with pytest.raises(WorkspaceMutationError, match="rolled back"):
        transaction.commit()

    transaction.abort()
    assert first.read_text(encoding="utf-8") == "a-before"
    assert second.read_text(encoding="utf-8") == "b-before"
    assert any(
        path.read_text(encoding="utf-8") == "a-after"
        for path in workspace.glob(".a.txt.suyo-tx-*")
    )


def test_new_symlink_must_resolve_inside_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / "escaped").symlink_to(tmp_path / "outside")

    with pytest.raises(WorkspaceMutationError, match="outside the workspace"):
        transaction.collect_changes()

    transaction.abort()


def test_existing_symlink_mutation_is_discarded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    (workspace / "target.txt").write_text("target", encoding="utf-8")
    link = workspace / "link"
    link.symlink_to("target.txt")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / "link").unlink()

    with pytest.raises(WorkspaceMutationError, match="existing symbolic link"):
        transaction.collect_changes()

    transaction.abort()
    assert link.is_symlink()


def test_changed_hardlinked_files_are_rejected_without_breaking_link_topology(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    first = workspace / "first.txt"
    second = workspace / "second.txt"
    first.write_text("before", encoding="utf-8")
    try:
        os.link(first, second)
    except OSError:
        pytest.skip("filesystem does not support hard links")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    assert (staged / "first.txt").stat().st_ino == (staged / "second.txt").stat().st_ino

    (staged / "first.txt").write_text("after", encoding="utf-8")
    with pytest.raises(WorkspaceMutationError, match="hard-linked path"):
        transaction.commit()
    transaction.abort()

    assert first.read_text(encoding="utf-8") == "before"
    assert second.read_text(encoding="utf-8") == "before"
    assert first.stat().st_ino == second.stat().st_ino
    assert FileVersionStore(workspace).list_versions() == []


def test_startup_recovery_rolls_back_a_process_crash_mid_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    existing = workspace / "a-existing.txt"
    created = workspace / "b-created.txt"
    existing.write_text("before", encoding="utf-8")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / existing.name).write_text("after", encoding="utf-8")
    (staged / created.name).write_text("new", encoding="utf-8")
    real_install_new = transaction_module._link_prepared_new_at

    def crash_after_new_file(workspace_fd: int, temporary: object) -> None:
        real_install_new(workspace_fd, temporary)
        raise KeyboardInterrupt("simulated process death")

    monkeypatch.setattr(
        transaction_module,
        "_link_prepared_new_at",
        crash_after_new_file,
    )

    with pytest.raises(KeyboardInterrupt, match="simulated process death"):
        transaction.commit()

    assert existing.read_text(encoding="utf-8") == "after"
    assert created.read_text(encoding="utf-8") == "new"
    recovered = recover_pending_workspace_transactions(storage_root=private)

    assert len(recovered) == 1
    assert existing.read_text(encoding="utf-8") == "before"
    assert not created.exists()
    sidecar_values = {
        path.read_text(encoding="utf-8")
        for path in workspace.iterdir()
        if path.is_file() and path.name.startswith(".")
    }
    assert {"after", "new"} <= sidecar_values
    assert not (private / "execution-transactions").exists()


def test_startup_recovery_leaves_matching_undeleted_directory_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX directory modes are required")
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    folder = workspace / "empty"
    folder.mkdir(mode=0o710)
    folder.chmod(0o710)
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / folder.name).rmdir()

    def crash_before_remove(_workspace_fd: int, _relative: str) -> None:
        raise KeyboardInterrupt("simulated process death")

    monkeypatch.setattr(transaction_module, "_remove_directory_at", crash_before_remove)
    with pytest.raises(KeyboardInterrupt, match="simulated process death"):
        transaction.commit()

    monkeypatch.setattr(transaction_module, "_remove_directory_at", lambda *_args: None)

    def unexpected_install(*_args, **_kwargs) -> None:
        raise AssertionError("matching existing directory must not be replaced or chmodded")

    monkeypatch.setattr(
        transaction_module,
        "_install_empty_directory_noreplace_at",
        unexpected_install,
    )
    recovered = recover_pending_workspace_transactions(storage_root=private)

    assert len(recovered) == 1
    assert folder.is_dir()
    assert folder.stat().st_mode & 0o777 == 0o710


def test_startup_recovery_preserves_unproven_same_name_directory_created_after_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / "later-empty").mkdir()
    real_create = transaction_module._create_directory_at

    def crash_before_create(*_args, **_kwargs) -> None:
        raise KeyboardInterrupt("simulated process death")

    monkeypatch.setattr(
        transaction_module,
        "_create_directory_at",
        crash_before_create,
    )
    with pytest.raises(KeyboardInterrupt, match="simulated process death"):
        transaction.commit()
    assert not (workspace / "later-empty").exists()

    # This directory was created after the crash, not by the interrupted
    # transaction.  A prepared journal without an inode proof must preserve it.
    later = workspace / "later-empty"
    later.mkdir()
    monkeypatch.setattr(transaction_module, "_create_directory_at", real_create)

    recovered = recover_pending_workspace_transactions(storage_root=private)

    assert len(recovered) == 1
    assert later.is_dir()


def test_startup_recovery_removes_directory_with_durable_creation_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    created = staged / "transaction-empty"
    created.mkdir()

    def crash_before_commit_marker(_state: str) -> None:
        raise KeyboardInterrupt("simulated process death")

    monkeypatch.setattr(transaction, "_write_journal_state", crash_before_commit_marker)
    with pytest.raises(KeyboardInterrupt, match="simulated process death"):
        transaction.commit()
    visible = workspace / created.name
    assert visible.is_dir()

    recovered = recover_pending_workspace_transactions(storage_root=private)

    assert len(recovered) == 1
    assert not visible.exists()


def test_startup_recovery_installs_an_actually_removed_empty_directory_noreplace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX directory modes are required")
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    folder = workspace / "empty"
    folder.mkdir(mode=0o710)
    folder.chmod(0o710)
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / folder.name).rmdir()
    real_remove = transaction_module._remove_directory_at

    def crash_after_remove(workspace_fd: int, relative: str) -> None:
        real_remove(workspace_fd, relative)
        raise KeyboardInterrupt("simulated process death")

    monkeypatch.setattr(transaction_module, "_remove_directory_at", crash_after_remove)
    with pytest.raises(KeyboardInterrupt, match="simulated process death"):
        transaction.commit()
    assert not folder.exists()

    monkeypatch.setattr(transaction_module, "_remove_directory_at", real_remove)
    recovered = recover_pending_workspace_transactions(storage_root=private)

    assert len(recovered) == 1
    assert folder.is_dir()
    assert folder.stat().st_mode & 0o777 == 0o710


def test_startup_recovery_never_chmods_a_recreated_deleted_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX directory modes are required")
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    folder = workspace / "empty"
    folder.mkdir(mode=0o710)
    folder.chmod(0o710)
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / folder.name).rmdir()
    real_remove = transaction_module._remove_directory_at

    def crash_after_remove(workspace_fd: int, relative: str) -> None:
        real_remove(workspace_fd, relative)
        raise KeyboardInterrupt("simulated process death")

    monkeypatch.setattr(transaction_module, "_remove_directory_at", crash_after_remove)
    with pytest.raises(KeyboardInterrupt, match="simulated process death"):
        transaction.commit()
    folder.mkdir(mode=0o755)
    folder.chmod(0o755)

    monkeypatch.setattr(transaction_module, "_remove_directory_at", real_remove)
    with pytest.raises(WorkspaceMutationError, match="later recreation"):
        recover_pending_workspace_transactions(storage_root=private)

    assert folder.is_dir()
    assert folder.stat().st_mode & 0o777 == 0o755
    assert (private / "execution-transactions").exists()


def test_startup_recovery_keeps_a_fully_committed_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    target = workspace / "target.txt"
    target.write_text("before", encoding="utf-8")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / target.name).write_text("after", encoding="utf-8")
    monkeypatch.setattr(transaction, "abort", lambda: None)

    result = transaction.commit()

    assert result.written_files == (str(target),)
    assert target.read_text(encoding="utf-8") == "after"
    assert len(result.recovery_sidecars) == 1
    sidecar = Path(result.recovery_sidecars[0])
    assert sidecar.read_text(encoding="utf-8") == "before"
    recovered = recover_pending_workspace_transactions(storage_root=private)
    assert len(recovered) == 1
    assert target.read_text(encoding="utf-8") == "after"
    assert sidecar.read_text(encoding="utf-8") == "before"
    assert not (private / "execution-transactions").exists()


def test_startup_recovery_rejects_workspace_root_identity_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    target = workspace / "target.txt"
    target.write_text("before", encoding="utf-8")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / target.name).write_text("command", encoding="utf-8")
    real_exchange = transaction_module._exchange_prepared_at

    def crash_after_exchange(workspace_fd: int, temporary: object) -> None:
        real_exchange(workspace_fd, temporary)
        raise KeyboardInterrupt("simulated process death")

    monkeypatch.setattr(transaction_module, "_exchange_prepared_at", crash_after_exchange)
    with pytest.raises(KeyboardInterrupt):
        transaction.commit()
    moved = tmp_path / "moved-workspace"
    workspace.rename(moved)
    workspace.mkdir()
    (workspace / "target.txt").write_text("replacement root", encoding="utf-8")

    with pytest.raises(WorkspaceMutationError, match="Workspace root changed"):
        recover_pending_workspace_transactions(storage_root=private)
    assert (workspace / "target.txt").read_text(encoding="utf-8") == "replacement root"
    assert (moved / "target.txt").read_text(encoding="utf-8") == "command"
    assert (private / "execution-transactions").exists()


def test_startup_recovery_never_overwrites_a_later_external_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    target = workspace / "target.txt"
    target.write_text("before", encoding="utf-8")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / target.name).write_text("command", encoding="utf-8")
    real_install = transaction_module._exchange_prepared_at

    def crash_after_install(workspace_fd: int, temporary: object) -> None:
        real_install(workspace_fd, temporary)
        raise KeyboardInterrupt("simulated process death")

    monkeypatch.setattr(
        transaction_module,
        "_exchange_prepared_at",
        crash_after_install,
    )
    with pytest.raises(KeyboardInterrupt):
        transaction.commit()
    target.write_text("later user edit", encoding="utf-8")

    with pytest.raises(WorkspaceMutationError, match="conflicts with a later edit"):
        recover_pending_workspace_transactions(storage_root=private)

    assert target.read_text(encoding="utf-8") == "later user edit"
    assert (private / "execution-transactions").exists()


def test_startup_recovery_rechecks_after_preflight_before_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    target = workspace / "target.txt"
    target.write_text("before", encoding="utf-8")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / target.name).write_text("command", encoding="utf-8")
    real_exchange = transaction_module._exchange_prepared_at

    def crash_after_exchange(workspace_fd: int, temporary: object) -> None:
        real_exchange(workspace_fd, temporary)
        raise KeyboardInterrupt("simulated process death")

    monkeypatch.setattr(
        transaction_module,
        "_exchange_prepared_at",
        crash_after_exchange,
    )
    with pytest.raises(KeyboardInterrupt):
        transaction.commit()
    monkeypatch.setattr(transaction_module, "_exchange_prepared_at", real_exchange)
    assert target.read_text(encoding="utf-8") == "command"

    real_restore = FileVersionStore.restore_failed_mutation_batch

    def edit_between_preflight_and_restore(
        store: FileVersionStore,
        version_ids: list[str],
        *,
        expected_current: dict[str, dict[str, object] | None] | None = None,
    ):
        target.write_text("later user edit", encoding="utf-8")
        return real_restore(
            store,
            version_ids,
            expected_current=expected_current,
        )

    monkeypatch.setattr(
        FileVersionStore,
        "restore_failed_mutation_batch",
        edit_between_preflight_and_restore,
    )
    real_atomic_rename = file_versions_module._version_atomic_rename
    atomic_exchange_calls = 0

    def count_atomic_exchange(*args, **kwargs):
        nonlocal atomic_exchange_calls
        if kwargs.get("exchange"):
            atomic_exchange_calls += 1
        return real_atomic_rename(*args, **kwargs)

    monkeypatch.setattr(
        file_versions_module,
        "_version_atomic_rename",
        count_atomic_exchange,
    )

    with pytest.raises(WorkspaceMutationError, match="later edit"):
        recover_pending_workspace_transactions(storage_root=private)
    assert atomic_exchange_calls == 2
    assert target.read_text(encoding="utf-8") == "later user edit"
    conflict_values = {
        path.read_text(encoding="utf-8")
        for path in workspace.glob(".target.txt.*.rollback.tmp")
    }
    assert "before" in conflict_values
    assert "later user edit" not in conflict_values
    assert (private / "execution-transactions").exists()
