"""Tests for the durable local stdio MCP startup trust boundary."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from app.mcp.local_approval import (
    LocalMcpApprovalStore,
    LocalMcpApprovalStoreError,
    _validate_dynamic_package_pin,
    local_mcp_launch_spec,
)


def _executable(directory: Path, name: str, content: str = "#!/bin/sh\nexit 0\n") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    executable = directory / name
    executable.write_text(content, encoding="utf-8")
    executable.chmod(0o700)
    return executable


def test_fingerprint_covers_command_args_effective_environment_and_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    bin_dir = tmp_path / "bin"
    runner = _executable(bin_dir, "runner")
    (tmp_path / "other").mkdir()
    monkeypatch.setattr(
        "app.mcp.local_approval.get_default_environment",
        lambda: {"PATH": str(bin_dir), "HOME": "/home/user"},
    )
    base = {
        "type": "local",
        "command": ["runner", "--mode", "read"],
        "environment": {"TOKEN": "secret-one"},
        "cwd": str(tmp_path),
    }
    first = local_mcp_launch_spec(base)

    descriptor = first.public_descriptor()
    assert descriptor["fingerprint"] == first.fingerprint
    assert descriptor["command"] == [str(runner.resolve()), "--mode", "read"]
    assert descriptor["cwd"] == str(tmp_path.resolve())
    assert set(descriptor["environment_keys"]) >= {"HOME", "PATH", "TOKEN"}
    assert descriptor["executable_path"] == str(runner.resolve())
    assert descriptor["executable_sha256"] == first.executable_sha256
    assert "secret-one" not in json.dumps(first.public_descriptor())

    variants = [
        {**base, "command": ["runner", "--mode", "write"]},
        {**base, "environment": {"TOKEN": "secret-two"}},
        {**base, "cwd": str(tmp_path / "other")},
    ]
    assert all(
        local_mcp_launch_spec(variant).fingerprint != first.fingerprint
        for variant in variants
    )

    monkeypatch.setattr(
        "app.mcp.local_approval.get_default_environment",
        lambda: {"PATH": str(bin_dir), "HOME": "/different/home"},
    )
    assert local_mcp_launch_spec(base).fingerprint != first.fingerprint


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"command": "runner"},
        {"command": ["runner", 1]},
        {"command": ["runner"], "environment": {"TOKEN": 1}},
        {"command": ["runner"], "cwd": ""},
    ],
)
def test_invalid_launch_configuration_is_rejected(config) -> None:
    with pytest.raises(ValueError):
        local_mcp_launch_spec(config)


def test_executable_content_replacement_invalidates_approval(tmp_path) -> None:
    executable = _executable(tmp_path / "bin", "server", "#!/bin/sh\nexit 0\n")
    config = {"command": [str(executable)], "cwd": str(tmp_path)}
    first = local_mcp_launch_spec(config)

    executable.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
    executable.chmod(0o700)
    second = local_mcp_launch_spec(config)

    assert second.executable_path == first.executable_path
    assert second.executable_sha256 != first.executable_sha256
    assert second.fingerprint != first.fingerprint


def test_executable_locator_symlink_is_not_passed_to_sdk(tmp_path) -> None:
    target = _executable(tmp_path / "real", "server")
    locator = tmp_path / "server-link"
    try:
        locator.symlink_to(target)
    except OSError:
        pytest.skip("executable symlinks are unavailable")

    launch = local_mcp_launch_spec({"command": [str(locator)], "cwd": str(tmp_path)})

    assert launch.executable_path == str(target.resolve())
    assert launch.command[0] == str(target.resolve())
    assert not Path(launch.command[0]).is_symlink()


def test_path_target_replacement_changes_absolute_launch(tmp_path) -> None:
    first_bin = tmp_path / "first"
    second_bin = tmp_path / "second"
    first = _executable(first_bin, "server")
    second = _executable(second_bin, "server", "#!/bin/sh\nexit 2\n")
    active_bin = tmp_path / "active"
    try:
        active_bin.symlink_to(first_bin, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    base = {
        "command": ["server"],
        "cwd": str(tmp_path),
        "environment": {"PATH": str(active_bin)},
    }

    left = local_mcp_launch_spec(base)
    active_bin.unlink()
    active_bin.symlink_to(second_bin, target_is_directory=True)
    right = local_mcp_launch_spec(base)

    assert left.command[0] == str(first.resolve())
    assert right.command[0] == str(second.resolve())
    assert left.fingerprint != right.fingerprint


@pytest.mark.parametrize(
    "command",
    [
        ["uvx", "--from", "example", "example"],
        ["uvx", "example"],
        ["npx", "-y", "example"],
        ["npx", "-y", "example@latest"],
    ],
)
def test_dynamic_package_runners_require_exact_versions(command) -> None:
    with pytest.raises(ValueError, match="pinned|must use"):
        local_mcp_launch_spec({"command": command})


def test_all_bundled_dynamic_mcp_packages_are_exactly_pinned() -> None:
    data_root = Path(__file__).parents[2] / "app" / "data"
    configs = [data_root / "connectors.json", *data_root.glob("plugins/*/.mcp.json")]
    commands: list[tuple[str, ...]] = []

    def visit(value) -> None:
        if isinstance(value, dict):
            command_value = value.get("command")
            if isinstance(command_value, list):
                command = tuple(command_value)
                if command and Path(command[0]).name in {"uvx", "npx"}:
                    commands.append(command)
                    _validate_dynamic_package_pin(command)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for path in configs:
        payload = json.loads(path.read_text(encoding="utf-8"))
        visit(payload)

    assert commands
    flattened = "\n".join(" ".join(command) for command in commands)
    assert "google-workspace-mcp==2.0.8" in flattened
    assert "@softeria/ms-365-mcp-server@0.131.1" in flattened
    assert "pubmed-search-mcp==0.5.17" in flattened


def test_sdk_default_keys_are_frozen_even_when_currently_absent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    executable = _executable(tmp_path / "bin", "server")
    monkeypatch.setattr(
        "app.mcp.local_approval.get_default_environment",
        lambda: {"PATH": str(executable.parent)},
    )
    launch = local_mcp_launch_spec({"command": ["server"], "cwd": str(tmp_path)})

    from mcp.client.stdio import DEFAULT_INHERITED_ENV_VARS

    assert all(key in launch.environment for key in DEFAULT_INHERITED_ENV_VARS)
    assert launch.environment.get("HOME") == ""
    # This is the SDK merge operation: the approved explicit value wins over
    # a default that appears only after approval.
    merged = {"HOME": "/added/later", **launch.environment}
    assert merged["HOME"] == ""


def test_private_store_persists_only_fingerprints_with_restrictive_mode(tmp_path) -> None:
    store = LocalMcpApprovalStore("/workspace/a", storage_root=tmp_path)
    fingerprint = "sha256:" + "a" * 64

    store.approve("local-server", fingerprint)

    reloaded = LocalMcpApprovalStore("/workspace/a", storage_root=tmp_path)
    assert reloaded.get("local-server") == fingerprint
    assert json.loads(store.path.read_text(encoding="utf-8")) == {
        "version": 1,
        "approvals": {"local-server": fingerprint},
    }
    assert os.stat(store.path).st_mode & 0o777 == 0o600
    assert os.stat(store.path.parent).st_mode & 0o777 == 0o700


def test_private_store_revocation_is_durable_and_noop_for_absent_entry(tmp_path) -> None:
    store = LocalMcpApprovalStore("/workspace/a", storage_root=tmp_path)
    fingerprint = "sha256:" + "a" * 64
    store.approve("local-server", fingerprint)

    assert store.revoke("local-server") is True
    assert store.revoke("local-server") is False
    assert LocalMcpApprovalStore(
        "/workspace/a",
        storage_root=tmp_path,
    ).get("local-server") is None


def test_private_store_revocation_fails_closed_after_external_change(tmp_path) -> None:
    store = LocalMcpApprovalStore("/workspace/a", storage_root=tmp_path)
    store.approve("local-server", "sha256:" + "a" * 64)
    store.path.write_text(
        json.dumps({
            "version": 1,
            "approvals": {"local-server": "sha256:" + "b" * 64},
        }),
        encoding="utf-8",
    )
    if os.name != "nt":
        store.path.chmod(0o600)

    with pytest.raises(LocalMcpApprovalStoreError):
        store.revoke("local-server")
    assert store.degraded_reason == "local_mcp_approval_state_unreadable"
    assert json.loads(store.path.read_text(encoding="utf-8"))["approvals"] == {
        "local-server": "sha256:" + "b" * 64,
    }


def test_malformed_private_store_fails_closed_without_overwrite(tmp_path) -> None:
    store = LocalMcpApprovalStore("/workspace/a", storage_root=tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("not-json", encoding="utf-8")

    degraded = LocalMcpApprovalStore("/workspace/a", storage_root=tmp_path)

    assert degraded.get("local-server") is None
    assert degraded.degraded_reason == "local_mcp_approval_state_unreadable"
    with pytest.raises(LocalMcpApprovalStoreError):
        degraded.approve("local-server", "sha256:" + "b" * 64)
    assert store.path.read_text(encoding="utf-8") == "not-json"


def test_symlinked_private_store_fails_closed(tmp_path) -> None:
    target = tmp_path / "target.json"
    target.write_text('{"version": 1, "approvals": {}}', encoding="utf-8")
    store = LocalMcpApprovalStore("/workspace/a", storage_root=tmp_path / "state")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.symlink_to(target)

    degraded = LocalMcpApprovalStore(
        "/workspace/a",
        storage_root=tmp_path / "state",
    )
    assert degraded.degraded_reason == "local_mcp_approval_state_unreadable"
    with pytest.raises(LocalMcpApprovalStoreError):
        degraded.approve("local-server", "sha256:" + "c" * 64)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission contract")
def test_symlinked_ancestor_and_permissive_file_fail_closed(tmp_path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)

    redirected = LocalMcpApprovalStore(
        "/workspace/a",
        storage_root=linked / "nested",
    )
    assert redirected.degraded_reason == "local_mcp_approval_state_unreadable"

    safe = LocalMcpApprovalStore(
        "/workspace/b",
        storage_root=tmp_path / "safe",
    )
    safe.path.write_text('{"version": 1, "approvals": {}}', encoding="utf-8")
    safe.path.chmod(0o666)
    permissive = LocalMcpApprovalStore(
        "/workspace/b",
        storage_root=tmp_path / "safe",
    )
    assert permissive.degraded_reason == "local_mcp_approval_state_unreadable"
    with pytest.raises(LocalMcpApprovalStoreError):
        permissive.approve("server", "sha256:" + "d" * 64)


@pytest.mark.skipif(os.name == "nt", reason="POSIX store identity contract")
def test_replaced_private_root_invalidates_in_memory_approval(tmp_path) -> None:
    root = tmp_path / "approvals"
    store = LocalMcpApprovalStore("/workspace/a", storage_root=root)
    fingerprint = "sha256:" + "e" * 64
    store.approve("server", fingerprint)
    state = store.path.read_bytes()

    moved = tmp_path / "moved-approvals"
    root.rename(moved)
    root.mkdir(mode=0o700)
    replacement = root / store.path.name
    replacement.write_bytes(state)
    replacement.chmod(0o600)

    assert store.get("server") is None
    assert store.degraded_reason == "local_mcp_approval_state_unreadable"
