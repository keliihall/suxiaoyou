"""Focused contracts for durable, replacement-aware workspace identities."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import errno
import os
from pathlib import Path
import re
import sys
import threading

import pytest

from app.storage import workspace_identity as identity_module
from app.storage.workspace_identity import (
    WorkspaceIdentityError,
    WorkspaceIdentityState,
    ensure_workspace_identity,
    inspect_workspace_identity,
    parse_legacy_stat_token,
)


pytestmark = pytest.mark.workspace_identity_v2


_REAL_INSPECT_POSIX_XATTR = identity_module._inspect_posix_xattr


@pytest.fixture(autouse=True)
def _exercise_marker_fallback_by_default(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """Keep the existing adversarial suite focused on the file fallback."""

    if sys.platform == "win32" and not request.node.name.startswith("test_windows_"):
        pytest.skip("POSIX marker contracts use dir_fd and O_NOFOLLOW")
    monkeypatch.setattr(
        identity_module,
        "_inspect_posix_xattr",
        lambda _canonical, *, create: None,
    )


def test_parse_legacy_stat_token_is_strict_and_non_throwing() -> None:
    assert parse_legacy_stat_token("stat-v1:16777233:9001") == (16777233, 9001)
    assert parse_legacy_stat_token("stat-v1:0001:02") == (1, 2)
    assert parse_legacy_stat_token("marker-v2:" + "a" * 64) is None
    assert parse_legacy_stat_token("stat-v1:-1:2") is None
    assert parse_legacy_stat_token("stat-v1:+1:2") is None
    assert parse_legacy_stat_token("stat-v1:1:2 ") is None
    assert parse_legacy_stat_token("stat-v1:١:2") is None
    assert parse_legacy_stat_token("stat-v1:1") is None
    assert parse_legacy_stat_token("stat-v1:" + ("9" * 5_000) + ":2") is None
    assert parse_legacy_stat_token(None) is None


def test_inspect_is_read_only_when_identity_is_absent(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(WorkspaceIdentityError, match="directory is missing"):
        inspect_workspace_identity(workspace)

    assert list(workspace.iterdir()) == []


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin xattr ABI contract")
def test_darwin_prefers_directory_xattr_without_dirtying_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(
        identity_module,
        "_inspect_posix_xattr",
        _REAL_INSPECT_POSIX_XATTR,
    )

    created = ensure_workspace_identity(workspace)
    inspected = inspect_workspace_identity(workspace)

    assert created == inspected
    assert re.fullmatch(r"marker-v2:[0-9a-f]{64}", created.durable_token)
    assert list(workspace.iterdir()) == []


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin xattr ABI contract")
def test_existing_fallback_marker_wins_when_xattrs_later_become_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fallback = ensure_workspace_identity(workspace)
    marker = workspace / ".suxiaoyou" / "workspace-identity-v2"
    assert marker.is_file()

    monkeypatch.setattr(
        identity_module,
        "_inspect_posix_xattr",
        _REAL_INSPECT_POSIX_XATTR,
    )
    repeated = ensure_workspace_identity(workspace)

    assert repeated == fallback
    assert marker.read_text(encoding="ascii") == fallback.durable_token + "\n"
    descriptor = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(OSError) as missing:
            identity_module._get_workspace_xattr(descriptor)
        assert missing.value.errno in identity_module._XATTR_MISSING_ERRNOS
    finally:
        os.close(descriptor)


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin xattr ABI contract")
def test_conflicting_xattr_and_fallback_marker_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fallback = ensure_workspace_identity(workspace)
    assert fallback.durable_token.startswith("marker-v2:")
    descriptor = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
    try:
        identity_module._set_workspace_xattr_create(
            descriptor,
            ("marker-v2:" + ("0" * 64) + "\n").encode("ascii"),
        )
    finally:
        os.close(descriptor)
    monkeypatch.setattr(
        identity_module,
        "_inspect_posix_xattr",
        _REAL_INSPECT_POSIX_XATTR,
    )

    with pytest.raises(WorkspaceIdentityError, match="representations conflict"):
        inspect_workspace_identity(workspace)


def test_ensure_creates_durable_marker_idempotently_and_preserves_files(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    app_directory = workspace / ".suxiaoyou"
    workspace.mkdir()
    app_directory.mkdir()
    user_file = app_directory / "existing-user-state.json"
    user_file.write_text('{"keep": true}\n', encoding="utf-8")

    created = ensure_workspace_identity(workspace / ".")
    inspected = inspect_workspace_identity(workspace)
    repeated = ensure_workspace_identity(workspace)

    assert isinstance(created, WorkspaceIdentityState)
    assert created == inspected == repeated
    assert created.canonical_path == workspace.resolve()
    assert created.volatile_identity == (
        workspace.stat().st_dev,
        workspace.stat().st_ino,
    )
    assert re.fullmatch(r"marker-v2:[0-9a-f]{64}", created.durable_token)
    marker = app_directory / "workspace-identity-v2"
    assert marker.read_text(encoding="ascii") == created.durable_token + "\n"
    assert user_file.read_text(encoding="utf-8") == '{"keep": true}\n'
    assert sorted(path.name for path in app_directory.iterdir()) == [
        "existing-user-state.json",
        "workspace-identity-v2",
    ]


def test_marker_creation_uses_exclusive_nofollow_open_and_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    real_open = identity_module.os.open
    real_fsync = identity_module.os.fsync
    temporary_flags: list[int] = []
    synced: list[int] = []

    def tracking_open(path, flags, *args, **kwargs):
        if isinstance(path, str) and path.startswith(".workspace-identity-v2."):
            temporary_flags.append(flags)
        return real_open(path, flags, *args, **kwargs)

    def tracking_fsync(descriptor: int) -> None:
        synced.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(identity_module.os, "open", tracking_open)
    monkeypatch.setattr(identity_module.os, "fsync", tracking_fsync)

    ensure_workspace_identity(workspace)

    creation_flags = next(flags for flags in temporary_flags if flags & os.O_CREAT)
    assert creation_flags & os.O_EXCL
    assert creation_flags & os.O_NOFOLLOW
    # Root directory, marker, and .suxiaoyou directory are each synchronized.
    assert len(synced) >= 3


def test_marker_removal_is_detected_without_recreation(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ensure_workspace_identity(workspace)
    marker = workspace / ".suxiaoyou" / "workspace-identity-v2"
    marker.unlink()

    with pytest.raises(WorkspaceIdentityError, match="marker is missing"):
        inspect_workspace_identity(workspace)

    assert not marker.exists()


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"marker-v2:" + b"a" * 63 + b"\n",
        b"marker-v2:" + b"A" * 64 + b"\n",
        b"marker-v2:" + b"g" * 64 + b"\n",
        b"marker-v2:" + b"a" * 64,
        b"marker-v2:" + b"a" * 64 + b"\nextra",
    ],
)
def test_corrupt_or_tampered_marker_fails_closed(
    tmp_path: Path,
    payload: bytes,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ensure_workspace_identity(workspace)
    marker = workspace / ".suxiaoyou" / "workspace-identity-v2"
    marker.write_bytes(payload)

    with pytest.raises(WorkspaceIdentityError, match="marker"):
        inspect_workspace_identity(workspace)

    assert marker.read_bytes() == payload


def test_marker_symlink_is_rejected_without_reading_or_changing_target(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    app_directory = workspace / ".suxiaoyou"
    workspace.mkdir()
    app_directory.mkdir()
    target = tmp_path / "unrelated-user-file"
    original = b"marker-v2:" + b"b" * 64 + b"\n"
    target.write_bytes(original)
    (app_directory / "workspace-identity-v2").symlink_to(target)

    with pytest.raises(WorkspaceIdentityError, match="unsafe|marker"):
        ensure_workspace_identity(workspace)

    assert target.read_bytes() == original
    assert (app_directory / "workspace-identity-v2").is_symlink()


def test_symlinked_app_directory_is_rejected_and_user_files_are_preserved(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    external = tmp_path / "external-user-directory"
    workspace.mkdir()
    external.mkdir()
    user_file = external / "keep.txt"
    user_file.write_text("keep me", encoding="utf-8")
    (workspace / ".suxiaoyou").symlink_to(external, target_is_directory=True)

    with pytest.raises(WorkspaceIdentityError, match="safe directory"):
        ensure_workspace_identity(workspace)

    assert user_file.read_text(encoding="utf-8") == "keep me"
    assert not (external / "workspace-identity-v2").exists()


def test_complete_marker_with_crash_leftover_temporary_link_is_accepted(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = ensure_workspace_identity(workspace)
    marker = workspace / ".suxiaoyou" / "workspace-identity-v2"
    alias = workspace / ".suxiaoyou" / (
        ".workspace-identity-v2." + "c" * 32 + ".tmp"
    )
    os.link(marker, alias)

    assert inspect_workspace_identity(workspace) == state

    assert marker.exists()
    assert alias.exists()
    assert marker.samefile(alias)


def test_preexisting_partial_temporary_marker_is_ignored_and_preserved(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    app_directory = workspace / ".suxiaoyou"
    workspace.mkdir()
    app_directory.mkdir()
    abandoned = app_directory / (
        ".workspace-identity-v2." + "d" * 32 + ".tmp"
    )
    abandoned.write_bytes(b"marker-v2:partial")

    state = ensure_workspace_identity(workspace)

    assert inspect_workspace_identity(workspace) == state
    assert abandoned.read_bytes() == b"marker-v2:partial"


def test_failed_publish_leaves_no_final_marker_and_retry_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    real_publish = identity_module._publish_marker_noreplace
    fail_once = True

    def interrupted_publish(*args, **kwargs):
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise OSError("simulated publish interruption")
        return real_publish(*args, **kwargs)

    monkeypatch.setattr(
        identity_module,
        "_publish_marker_noreplace",
        interrupted_publish,
    )

    with pytest.raises(WorkspaceIdentityError, match="Cannot inspect"):
        ensure_workspace_identity(workspace)

    app_directory = workspace / ".suxiaoyou"
    assert not (app_directory / "workspace-identity-v2").exists()
    assert len(list(app_directory.glob(".workspace-identity-v2.*.tmp"))) == 1

    state = ensure_workspace_identity(workspace)
    assert inspect_workspace_identity(workspace) == state


def test_concurrent_publishers_atomically_adopt_one_complete_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    real_publish = identity_module._publish_marker_noreplace
    publishers_ready = threading.Barrier(2)

    def synchronized_publish(*args, **kwargs):
        publishers_ready.wait(timeout=5)
        return real_publish(*args, **kwargs)

    monkeypatch.setattr(
        identity_module,
        "_publish_marker_noreplace",
        synchronized_publish,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(ensure_workspace_identity, workspace)
            for _index in range(2)
        ]
        states = [future.result(timeout=5) for future in futures]

    assert states[0] == states[1]
    assert inspect_workspace_identity(workspace) == states[0]
    app_directory = workspace / ".suxiaoyou"
    leftovers = list(app_directory.glob(".workspace-identity-v2.*.tmp"))
    assert len(leftovers) == 1
    assert re.fullmatch(
        rb"marker-v2:[0-9a-f]{64}\n",
        leftovers[0].read_bytes(),
    )


def test_fallback_publish_does_not_require_hard_links(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def reject_hard_link(*_args, **_kwargs):
        raise OSError(errno.ENOTSUP, "hard links unavailable")

    monkeypatch.setattr(identity_module.os, "link", reject_hard_link)

    state = ensure_workspace_identity(workspace)

    assert inspect_workspace_identity(workspace) == state
    assert (workspace / ".suxiaoyou" / "workspace-identity-v2").is_file()


def test_failed_marker_write_cleans_up_only_the_new_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    app_directory = workspace / ".suxiaoyou"
    workspace.mkdir()
    app_directory.mkdir()
    user_file = app_directory / "keep.txt"
    user_file.write_text("untouched", encoding="utf-8")

    def fail_write(_descriptor: int, _payload: bytes) -> None:
        raise OSError("simulated storage failure")

    monkeypatch.setattr(identity_module, "_write_all", fail_write)

    with pytest.raises(WorkspaceIdentityError, match="Cannot inspect"):
        ensure_workspace_identity(workspace)

    assert user_file.read_text(encoding="utf-8") == "untouched"
    assert not (app_directory / "workspace-identity-v2").exists()


def test_workspace_root_replacement_during_inspection_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "original.txt").write_text("original", encoding="utf-8")
    ensure_workspace_identity(workspace)
    displaced = tmp_path / "displaced-workspace"
    real_read_marker = identity_module._read_marker
    replaced = False

    def replace_after_read(app_fd: int) -> str:
        nonlocal replaced
        token = real_read_marker(app_fd)
        if not replaced:
            replaced = True
            workspace.rename(displaced)
            workspace.mkdir()
            (workspace / "replacement.txt").write_text(
                "replacement", encoding="utf-8"
            )
        return token

    monkeypatch.setattr(identity_module, "_read_marker", replace_after_read)

    with pytest.raises(WorkspaceIdentityError, match="root was removed or replaced"):
        inspect_workspace_identity(workspace)

    assert (displaced / "original.txt").read_text(encoding="utf-8") == "original"
    assert (workspace / "replacement.txt").read_text(encoding="utf-8") == (
        "replacement"
    )


def test_windows_uses_native_file_identity_without_creating_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(identity_module.sys, "platform", "win32")
    calls: list[tuple[Path, bool]] = []

    def native_identity(path: Path, *, directory: bool) -> tuple[int, int]:
        calls.append((path, directory))
        return 4_294_967_297, 2**96 + 17

    monkeypatch.setattr(identity_module, "windows_path_identity", native_identity)

    ensured = ensure_workspace_identity(workspace)
    inspected = inspect_workspace_identity(workspace)

    assert ensured == inspected == WorkspaceIdentityState(
        canonical_path=workspace.resolve(),
        durable_token=f"winfile-v2:{4_294_967_297}:{2**96 + 17}",
        volatile_identity=(4_294_967_297, 2**96 + 17),
    )
    assert calls == [(workspace.resolve(), True)] * 4
    assert not (workspace / ".suxiaoyou").exists()


def test_windows_root_replacement_signal_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(identity_module.sys, "platform", "win32")
    identities = iter(((7, 11), (7, 12)))
    monkeypatch.setattr(
        identity_module,
        "windows_path_identity",
        lambda _path, *, directory: next(identities),
    )

    with pytest.raises(WorkspaceIdentityError, match="changed during"):
        inspect_workspace_identity(workspace)

    assert not (workspace / ".suxiaoyou").exists()
