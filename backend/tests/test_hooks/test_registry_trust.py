from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from app.hooks.models import HookCommandDeclaration
from app.hooks.registry import HookRegistry, refresh_command_hook
from app.hooks.trust import HookTrustStore, HookTrustStoreError


def _declaration(command: list[str], **overrides) -> HookCommandDeclaration:
    payload = {
        "hook_id": "local-policy",
        "event": "PreToolUse",
        "failure_policy": "required",
        "command": command,
    }
    payload.update(overrides)
    return HookCommandDeclaration.model_validate(payload)


def _write_executable(path: Path, body: str = "print('ok')\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o700)
    return path


def test_project_command_identity_binds_final_path_sha_argv_env_and_cwd(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-leak")
    executable = _write_executable(tmp_path / "hooks" / "policy")
    registry = HookRegistry(tmp_path)

    hook, = registry.register_project_commands([
        _declaration(["hooks/policy", "--mode", "strict"]),
    ])

    assert hook.launch.command == (
        str(executable.resolve()),
        "--mode",
        "strict",
    )
    assert hook.launch.cwd == str(tmp_path.resolve())
    assert hook.launch.executable_path == str(executable.resolve())
    assert hook.launch.executable_sha256 == hashlib.sha256(
        executable.read_bytes()
    ).hexdigest()
    assert hook.fingerprint.startswith("sha256:")
    assert "AWS_SECRET_ACCESS_KEY" not in hook.launch.environment
    assert "HOME" not in hook.launch.environment
    assert set(hook.launch.environment) == {
        "LANG",
        "LC_ALL",
        "NO_COLOR",
        "PATH",
        "PYTHONUTF8",
    }
    descriptor = hook.public_descriptor()
    assert descriptor["fingerprint"] == hook.fingerprint
    assert "environment" not in descriptor
    assert descriptor["environment_keys"] == sorted(hook.launch.environment)


def test_project_and_plugin_commands_use_workspace_cwd_but_separate_roots(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    plugin = tmp_path / "plugin"
    workspace.mkdir()
    executable = _write_executable(plugin / "bin" / "policy")
    registry = HookRegistry(workspace)

    hook, = registry.register_plugin_commands(
        "review-plugin",
        plugin,
        [_declaration(["bin/policy"], hook_id="plugin-policy")],
    )

    assert hook.launch.executable_path == str(executable.resolve())
    assert hook.launch.cwd == str(workspace.resolve())
    assert hook.source_name == "review-plugin"


def test_local_command_cannot_escape_its_project_or_plugin_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = _write_executable(tmp_path / "outside")
    registry = HookRegistry(workspace)

    with pytest.raises(ValueError, match="inside its source root"):
        registry.register_project_commands([
            _declaration([str(outside)]),
        ])
    with pytest.raises(ValueError, match="inside its source root"):
        registry.register_project_commands([
            _declaration([sys.executable, str(workspace / "policy.py")]),
        ])


def test_hook_environment_rejects_secret_or_arbitrary_manifest_keys(
    tmp_path: Path,
) -> None:
    _write_executable(tmp_path / "policy")
    registry = HookRegistry(tmp_path)
    with pytest.raises(ValueError, match="non-allow-listed"):
        registry.register_project_commands([
            _declaration(
                ["policy"],
                environment={"OPENAI_API_KEY": "secret"},
            ),
        ])


def test_content_change_invalidates_persisted_trust_and_requires_new_approval(
    tmp_path: Path,
) -> None:
    executable = _write_executable(tmp_path / "policy", "print('v1')\n")
    registry = HookRegistry(tmp_path)
    original, = registry.register_project_commands([
        _declaration(["policy"]),
    ])
    trust_root = tmp_path / "private-trust"
    trust = HookTrustStore(tmp_path, storage_root=trust_root)
    trust.approve(original)
    assert trust.is_approved(original)

    executable.write_text(
        f"#!{sys.executable}\nprint('v2')\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    changed = refresh_command_hook(original)

    assert changed.trust_key == original.trust_key
    assert changed.launch.executable_sha256 != original.launch.executable_sha256
    assert changed.fingerprint != original.fingerprint
    assert not trust.is_approved(changed)
    reloaded = HookTrustStore(tmp_path, storage_root=trust_root)
    assert reloaded.is_approved(original)
    assert not reloaded.is_approved(changed)


def test_hook_trust_revocation_is_durable(tmp_path: Path) -> None:
    _write_executable(tmp_path / "policy")
    registry = HookRegistry(tmp_path)
    hook, = registry.register_project_commands([_declaration(["policy"])])
    trust_root = tmp_path / "private-trust"
    trust = HookTrustStore(tmp_path, storage_root=trust_root)
    trust.approve(hook)

    assert trust.revoke(hook) is True
    assert trust.revoke(hook) is False
    assert not HookTrustStore(
        tmp_path,
        storage_root=trust_root,
    ).is_approved(hook)


def test_trust_store_is_atomic_private_and_fails_closed_when_damaged(
    tmp_path: Path,
) -> None:
    _write_executable(tmp_path / "policy")
    registry = HookRegistry(tmp_path)
    hook, = registry.register_project_commands([_declaration(["policy"])])
    trust_root = tmp_path / "private-trust"
    trust = HookTrustStore(tmp_path, storage_root=trust_root)
    trust.approve(hook)

    payload = json.loads(trust.path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["approvals"] == {hook.trust_key: hook.fingerprint}
    if os.name != "nt":
        assert os.stat(trust.path).st_mode & 0o777 == 0o600
        assert os.stat(trust.path.parent).st_mode & 0o777 == 0o700

    trust.path.write_text("not-json", encoding="utf-8")
    if os.name != "nt":
        trust.path.chmod(0o600)
    damaged = HookTrustStore(tmp_path, storage_root=trust_root)
    assert not damaged.is_approved(hook)
    assert damaged.degraded_reason is not None
    with pytest.raises(HookTrustStoreError):
        damaged.approve(hook)
