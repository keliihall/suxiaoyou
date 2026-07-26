"""Durability, boundary, integrity, and retention tests for file versions."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from app.storage import file_versions as file_versions_module
from app.storage.file_versions import (
    FileVersionError,
    FileVersionLimits,
    FileVersionNotFound,
    FileVersionStore,
)
from app.schemas.agent import AgentInfo
from app.tool.context import ToolContext
from app.tool.workspace_transaction import WorkspaceMutationTransaction


def _store(tmp_path: Path, **limit_overrides: int) -> tuple[Path, FileVersionStore]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    limits = FileVersionLimits(**limit_overrides) if limit_overrides else None
    return workspace, FileVersionStore(
        workspace,
        storage_root=tmp_path / "private" / "file-versions",
        limits=limits,
    )


def test_capture_lists_checksum_and_restores_binary_atomically(tmp_path: Path) -> None:
    workspace, store = _store(tmp_path)
    target = workspace / "report.bin"
    original = b"\x00old\xffcontents"
    target.write_bytes(original)

    version = store.capture_before_mutation(target, operation="test.write")
    assert version is not None
    assert version.sha256 == hashlib.sha256(original).hexdigest()
    assert version.relative_path == "report.bin"
    assert store.list_versions(file_path="report.bin") == [version]

    target.write_bytes(b"replacement")
    restored, recovery, restored_path = store.restore(version.id)

    assert restored == version
    assert restored_path == target
    assert target.read_bytes() == original
    assert recovery is not None
    assert recovery.sha256 == hashlib.sha256(b"replacement").hexdigest()
    assert len(store.list_versions(file_path="report.bin")) == 2


def test_materialize_version_is_limited_to_owned_transaction_stage(
    tmp_path: Path,
) -> None:
    workspace, store = _store(tmp_path)
    target = workspace / "reports" / "quarter.txt"
    target.parent.mkdir()
    target.write_text("before", encoding="utf-8")
    version = store.capture_before_mutation(target, operation="write")
    assert version is not None
    target.write_text("after", encoding="utf-8")
    ctx = ToolContext(
        session_id="session",
        message_id="message",
        call_id="rewind",
        agent=AgentInfo(name="test", description="", mode="primary"),
        workspace=str(workspace),
    )
    transaction = WorkspaceMutationTransaction(
        workspace,
        ctx,
        operation="rewind",
        storage_root=tmp_path / "private",
    )
    staged = transaction.prepare_paths(["reports/quarter.txt"])

    restored, staged_target = store.materialize_version_in_transaction(
        version.id,
        staged,
        expected_relative_path="reports/quarter.txt",
    )

    assert restored == version
    assert staged_target.read_text(encoding="utf-8") == "before"
    assert target.read_text(encoding="utf-8") == "after"
    with pytest.raises(FileVersionError, match="different workspace path"):
        store.materialize_version_in_transaction(
            version.id,
            staged,
            expected_relative_path="reports/other.txt",
        )
    outside = tmp_path / "not-a-transaction"
    outside.mkdir()
    with pytest.raises(FileVersionError, match="owned workspace transaction"):
        store.materialize_version_in_transaction(
            version.id,
            outside,
            expected_relative_path="reports/quarter.txt",
        )
    transaction.abort()


def test_restore_preserves_snapshot_mode_on_posix(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("Windows uses inherited per-user ACLs")
    workspace, store = _store(tmp_path)
    target = workspace / "mode.txt"
    target.write_text("old", encoding="utf-8")
    target.chmod(0o640)
    version = store.capture_before_mutation(target, operation="write")
    assert version is not None
    target.write_text("new", encoding="utf-8")
    target.chmod(0o600)

    store.restore(version.id)
    assert target.stat().st_mode & 0o777 == 0o640


def test_restore_keeps_old_open_fd_writes_reachable_in_sidecar(tmp_path: Path) -> None:
    workspace, store = _store(tmp_path)
    target = workspace / "open.txt"
    target.write_bytes(b"version")
    version = store.capture_before_mutation(target, operation="write")
    assert version is not None
    target.write_bytes(b"current")
    descriptor = os.open(target, os.O_RDWR)
    try:
        store.restore(version.id)
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(descriptor, b"late-fd-write")
        os.ftruncate(descriptor, len(b"late-fd-write"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

    assert target.read_bytes() == b"version"
    sidecars = list(workspace.glob(f".{target.name}.*.rollback.tmp"))
    assert any(path.read_bytes() == b"late-fd-write" for path in sidecars)


def test_restore_install_failure_leaves_current_bytes_intact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, store = _store(tmp_path)
    target = workspace / "atomic.txt"
    target.write_text("old", encoding="utf-8")
    version = store.capture_before_mutation(target, operation="write")
    assert version is not None
    target.write_text("current", encoding="utf-8")
    def fail_target_replace(*_args, **_kwargs):
        raise OSError("simulated restore install failure")

    monkeypatch.setattr(file_versions_module, "_version_atomic_rename", fail_target_replace)
    with pytest.raises(FileVersionError, match="simulated restore install failure"):
        store.restore(version.id)
    assert target.read_text(encoding="utf-8") == "current"
    assert not list(target.parent.glob(f".{target.name}.*.rollback.tmp"))


def test_restore_conflict_puts_later_edit_back_at_visible_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, store = _store(tmp_path)
    target = workspace / "conflict.txt"
    target.write_text("version", encoding="utf-8")
    version = store.capture_before_mutation(target, operation="write")
    assert version is not None
    target.write_text("current", encoding="utf-8")
    real_atomic_rename = file_versions_module._version_atomic_rename
    injected = False

    def edit_immediately_before_exchange(*args, **kwargs):
        nonlocal injected
        if kwargs.get("exchange") and not injected:
            injected = True
            target.write_text("later user edit", encoding="utf-8")
        return real_atomic_rename(*args, **kwargs)

    monkeypatch.setattr(
        file_versions_module,
        "_version_atomic_rename",
        edit_immediately_before_exchange,
    )

    with pytest.raises(FileVersionError, match="original visible object was restored"):
        store.restore(version.id)

    assert target.read_text(encoding="utf-8") == "later user edit"
    sidecars = list(workspace.glob(f".{target.name}.*.rollback.tmp"))
    assert any(path.read_text(encoding="utf-8") == "version" for path in sidecars)
    assert all(path.read_text(encoding="utf-8") != "later user edit" for path in sidecars)


def test_restore_of_deleted_file_and_filter_by_returned_relative_path(tmp_path: Path) -> None:
    workspace, store = _store(tmp_path)
    output = workspace / "suxiaoyou_written"
    output.mkdir()
    target = output / "draft.txt"
    target.write_text("recover me", encoding="utf-8")
    version = store.capture_before_mutation(target, operation="apply_patch.delete")
    assert version is not None
    target.unlink()

    # Listed relative paths can be fed back directly even though Agent-relative
    # writes normally resolve under suxiaoyou_written/.
    assert store.list_versions(file_path=version.relative_path) == [version]
    restored, recovery, _ = store.restore(version.id)
    assert restored == version
    assert recovery is None
    assert target.read_text(encoding="utf-8") == "recover me"


def test_workspace_escape_and_symlink_escape_are_rejected(tmp_path: Path) -> None:
    workspace, store = _store(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    with pytest.raises(FileVersionError, match="outside the workspace"):
        store.capture_before_mutation(outside, operation="write")

    link = workspace / "escape.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable")
    with pytest.raises(FileVersionError, match="outside the workspace"):
        store.capture_before_mutation(link, operation="write")
    assert outside.read_text(encoding="utf-8") == "secret"


def test_restore_refuses_a_replaced_symlink_target(tmp_path: Path) -> None:
    workspace, store = _store(tmp_path)
    target = workspace / "report.txt"
    other = workspace / "other.txt"
    target.write_text("version", encoding="utf-8")
    other.write_text("must stay unchanged", encoding="utf-8")
    version = store.capture_before_mutation(target, operation="write")
    assert version is not None
    target.unlink()
    try:
        target.symlink_to(other)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable")

    with pytest.raises(FileVersionError, match="symbolic link"):
        store.restore(version.id)
    assert target.is_symlink()
    assert other.read_text(encoding="utf-8") == "must stay unchanged"


def test_private_store_symlink_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    storage = tmp_path / "private" / "file-versions"
    storage.parent.mkdir()
    try:
        storage.symlink_to(redirected, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable")
    target = workspace / "file.txt"
    target.write_text("contents", encoding="utf-8")

    with pytest.raises(FileVersionError, match="redirected/non-directory"):
        FileVersionStore(workspace, storage_root=storage).capture_before_mutation(
            target,
            operation="write",
        )
    assert list(redirected.iterdir()) == []


def test_oversized_source_fails_closed(tmp_path: Path) -> None:
    workspace, store = _store(
        tmp_path,
        max_file_bytes=4,
        max_workspace_bytes=8,
        max_versions_per_file=5,
        max_total_versions=10,
    )
    target = workspace / "large.txt"
    target.write_text("12345", encoding="utf-8")

    with pytest.raises(FileVersionError, match="above the 4-byte recovery limit"):
        store.capture_before_mutation(target, operation="write")
    assert target.read_text(encoding="utf-8") == "12345"
    assert store.list_versions() == []


def test_retention_caps_versions_and_private_object_bytes(tmp_path: Path) -> None:
    workspace, store = _store(
        tmp_path,
        max_file_bytes=8,
        max_workspace_bytes=8,
        max_versions_per_file=2,
        max_total_versions=3,
    )
    target = workspace / "rotating.txt"
    target.write_text("aaaa", encoding="utf-8")
    first = store.capture_before_mutation(target, operation="edit")
    target.write_text("bbbb", encoding="utf-8")
    second = store.capture_before_mutation(target, operation="edit")
    target.write_text("cccc", encoding="utf-8")
    third = store.capture_before_mutation(target, operation="edit")

    assert first is not None and second is not None and third is not None
    retained = store.list_versions(file_path=target)
    assert [item.id for item in retained] == [third.id, second.id]
    blobs = list(store.objects_dir.glob("*.blob"))
    assert len(blobs) == 2
    assert sum(blob.stat().st_size for blob in blobs) <= 8


def test_batch_capture_pins_every_pre_mutation_source(tmp_path: Path) -> None:
    workspace, store = _store(
        tmp_path,
        max_file_bytes=8,
        max_workspace_bytes=16,
        max_versions_per_file=2,
        max_total_versions=2,
    )
    old = workspace / "old.txt"
    first = workspace / "first.txt"
    second = workspace / "second.txt"
    old.write_text("old", encoding="utf-8")
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    old_version = store.capture_before_mutation(old, operation="old")
    assert old_version is not None

    batch = store.capture_batch_before_mutation(
        [first, second],
        operation="command",
        call_id="call",
    )

    assert len(batch) == 2
    assert {item.relative_path for item in batch} == {"first.txt", "second.txt"}
    assert {item.id for item in store.list_versions()} == {item.id for item in batch}
    assert old_version.id not in {item.id for item in store.list_versions()}


def test_restore_pins_selected_version_when_per_file_retention_is_full(
    tmp_path: Path,
) -> None:
    workspace, store = _store(tmp_path, max_versions_per_file=2)
    target = workspace / "timeline.txt"

    target.write_text("A", encoding="utf-8")
    version_a = store.capture_before_mutation(target, operation="write")
    assert version_a is not None
    target.write_text("B", encoding="utf-8")
    version_b = store.capture_before_mutation(target, operation="write")
    assert version_b is not None
    target.write_text("C", encoding="utf-8")

    assert [item.id for item in store.list_versions(file_path=target)] == [
        version_b.id,
        version_a.id,
    ]
    selected_blob = store.objects_dir / f"{version_a.sha256}.blob"
    assert selected_blob.exists()

    restored_a, recovery_c, restored_path = store.restore(version_a.id)

    assert restored_a.id == version_a.id
    assert restored_path == target
    assert target.read_text(encoding="utf-8") == "A"
    assert recovery_c is not None
    assert recovery_c.sha256 == hashlib.sha256(b"C").hexdigest()
    # Capturing C must retain the selected A source instead of evicting its
    # blob as the ordinary newest-first policy would have done.
    retained_ids = {
        item.id for item in store.list_versions(file_path=target)
    }
    assert retained_ids == {version_a.id, recovery_c.id}
    assert selected_blob.exists()

    restored_c, recovery_a, _ = store.restore(recovery_c.id)
    assert restored_c.id == recovery_c.id
    assert target.read_text(encoding="utf-8") == "C"
    assert recovery_a is not None
    assert recovery_a.sha256 == version_a.sha256


def test_duplicate_contents_are_deduplicated(tmp_path: Path) -> None:
    workspace, store = _store(tmp_path)
    target = workspace / "same.txt"
    target.write_text("same", encoding="utf-8")
    first = store.capture_before_mutation(target, operation="write")
    second = store.capture_before_mutation(target, operation="write")
    assert first is not None and second is not None
    assert first.sha256 == second.sha256
    assert len(list(store.objects_dir.glob("*.blob"))) == 1


def test_corrupt_blob_never_replaces_current_file(tmp_path: Path) -> None:
    workspace, store = _store(tmp_path)
    target = workspace / "important.txt"
    target.write_text("known-good", encoding="utf-8")
    version = store.capture_before_mutation(target, operation="write")
    assert version is not None
    target.write_text("current", encoding="utf-8")
    (store.objects_dir / f"{version.sha256}.blob").write_bytes(b"corrupt")

    with pytest.raises(FileVersionError, match="wrong size|checksum mismatch"):
        store.restore(version.id)
    assert target.read_text(encoding="utf-8") == "current"


def test_symlinked_blob_never_replaces_current_file(tmp_path: Path) -> None:
    workspace, store = _store(tmp_path)
    target = workspace / "important.txt"
    target.write_text("known-good", encoding="utf-8")
    version = store.capture_before_mutation(target, operation="write")
    assert version is not None
    target.write_text("current", encoding="utf-8")
    blob = store.objects_dir / f"{version.sha256}.blob"
    blob.unlink()
    outside = tmp_path / "outside-blob"
    outside.write_text("known-good", encoding="utf-8")
    try:
        blob.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable")

    with pytest.raises(FileVersionError, match="symbolic link"):
        store.restore(version.id)
    assert target.read_text(encoding="utf-8") == "current"


def test_manifest_digest_cannot_traverse_private_storage(tmp_path: Path) -> None:
    workspace, store = _store(tmp_path)
    target = workspace / "x.txt"
    target.write_text("x", encoding="utf-8")
    store.capture_before_mutation(target, operation="write")
    manifest = json.loads(store.manifest_path.read_text(encoding="utf-8"))
    manifest["versions"][0]["sha256"] = "../../outside"
    store.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(FileVersionError, match="invalid entry"):
        store.list_versions()


def test_store_is_outside_workspace_and_manifest_has_relative_paths(tmp_path: Path) -> None:
    workspace, store = _store(tmp_path)
    target = workspace / "folder" / "private.txt"
    target.parent.mkdir()
    target.write_text("v1", encoding="utf-8")
    store.capture_before_mutation(target, operation="write")

    store.root.relative_to(tmp_path / "private")
    with pytest.raises(ValueError):
        store.root.relative_to(workspace)
    manifest = json.loads(store.manifest_path.read_text(encoding="utf-8"))
    assert manifest["versions"][0]["relative_path"] == "folder/private.txt"
    assert str(target) not in store.manifest_path.read_text(encoding="utf-8")


@pytest.mark.workspace_identity_v2
def test_recreated_workspace_path_is_isolated_from_previous_history(
    tmp_path: Path,
) -> None:
    workspace, store = _store(tmp_path)
    target = workspace / "secret.txt"
    target.write_text("previous workspace secret", encoding="utf-8")
    version = store.capture_before_mutation(target, operation="write")
    assert version is not None
    previous_history_root = store.root

    workspace.rename(tmp_path / "previous-workspace")
    workspace.mkdir()
    replacement_store = FileVersionStore(
        workspace,
        storage_root=tmp_path / "private" / "file-versions",
    )

    assert replacement_store.root != previous_history_root
    assert replacement_store.list_versions() == []
    with pytest.raises(FileVersionNotFound):
        replacement_store.restore(version.id)
    with pytest.raises(FileVersionError, match="Workspace root changed"):
        store.list_versions()
    assert not (workspace / "secret.txt").exists()


@pytest.mark.workspace_identity_v2
def test_manifest_workspace_identity_mismatch_fails_closed(tmp_path: Path) -> None:
    workspace, store = _store(tmp_path)
    target = workspace / "identity.txt"
    target.write_text("contents", encoding="utf-8")
    version = store.capture_before_mutation(target, operation="write")
    assert version is not None
    manifest = json.loads(store.manifest_path.read_text(encoding="utf-8"))
    manifest["workspace_identity"]["token"] = "marker-v2:" + ("0" * 64)
    store.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(FileVersionError, match="different workspace identity"):
        store.list_versions()
