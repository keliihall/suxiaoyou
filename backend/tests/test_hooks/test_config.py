from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from app.hooks.config import (
    MAX_PROJECT_HOOK_CONFIG_BYTES,
    ProjectHookConfigError,
    ProjectHookConfigSecurityError,
    load_project_hook_config,
    register_project_hook_config,
)
from app.hooks.models import HookSource
from app.hooks.registry import HookRegistry


def _config_path(workspace: Path) -> Path:
    path = workspace / ".suxiaoyou" / "hooks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _write_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"#!{sys.executable}\nprint('{{\"version\":1,\"decision\":\"allow\"}}')\n",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def _valid_config() -> dict[str, object]:
    return {
        "version": 1,
        "hooks": [
            {
                "hook_id": "project-policy",
                "event": "PreToolUse",
                "failure_policy": "required",
                "timeout_seconds": 4,
                "command": [".suxiaoyou/bin/policy", "--strict"],
                "environment": {"NO_COLOR": "1"},
            }
        ],
    }


def test_missing_project_config_is_an_empty_v1_registration(tmp_path: Path) -> None:
    config = load_project_hook_config(tmp_path)
    registry = HookRegistry(tmp_path)

    registered = register_project_hook_config(registry, config)

    assert config.version == 1
    assert config.hooks == ()
    assert registered == ()
    assert registry.hooks == ()


def test_valid_file_registers_only_project_command_hooks(tmp_path: Path) -> None:
    executable = _write_executable(tmp_path / ".suxiaoyou" / "bin" / "policy")
    _config_path(tmp_path).write_text(
        json.dumps(_valid_config(), ensure_ascii=False),
        encoding="utf-8",
    )
    registry = HookRegistry(tmp_path)

    config = load_project_hook_config(tmp_path)
    hook, = register_project_hook_config(registry, config)

    assert hook.source is HookSource.PROJECT
    assert hook.source_name == "project"
    assert hook.launch.executable_path == str(executable.resolve())
    assert hook.launch.command[-1] == "--strict"
    assert config.hooks[0].timeout_seconds == 4


def test_project_config_registration_is_atomic_on_command_resolution_failure(
    tmp_path: Path,
) -> None:
    _write_executable(tmp_path / "valid-policy")
    outside = _write_executable(tmp_path.parent / "outside-policy")
    payload = {
        "version": 1,
        "hooks": [
            {
                "hook_id": "valid",
                "event": "PreToolUse",
                "failure_policy": "required",
                "command": ["valid-policy"],
            },
            {
                "hook_id": "escape",
                "event": "Stop",
                "failure_policy": "required",
                "command": [str(outside)],
            },
        ],
    }
    _config_path(tmp_path).write_text(json.dumps(payload), encoding="utf-8")
    registry = HookRegistry(tmp_path)

    with pytest.raises(ValueError, match="inside its source root"):
        register_project_hook_config(registry)

    assert registry.hooks == ()


@pytest.mark.parametrize(
    "encoded",
    [
        '{"version":1,"version":1,"hooks":[]}',
        (
            '{"version":1,"hooks":[{"hook_id":"one","hook_id":"two",'
            '"event":"PreToolUse","failure_policy":"required",'
            '"command":["policy"]}]}'
        ),
    ],
)
def test_duplicate_json_keys_are_rejected_at_every_depth(
    tmp_path: Path,
    encoded: str,
) -> None:
    _config_path(tmp_path).write_text(encoded, encoding="utf-8")

    with pytest.raises(ProjectHookConfigError, match="duplicate field"):
        load_project_hook_config(tmp_path)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update({"plugin": "untrusted"}),
        lambda value: value["hooks"][0].update({"source": "plugin"}),
        lambda value: value["hooks"][0].update({"type": "builtin"}),
        lambda value: value["hooks"][0].update({"prompt": "do anything"}),
    ],
)
def test_unknown_fields_and_non_command_sources_are_rejected(
    tmp_path: Path,
    mutation,
) -> None:
    payload = _valid_config()
    mutation(payload)
    _config_path(tmp_path).write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ProjectHookConfigError, match="unknown fields"):
        load_project_hook_config(tmp_path)


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", "1"),
        ("version", 2),
        ("hooks", {}),
    ],
)
def test_version_and_container_types_are_exact(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    payload = _valid_config()
    payload[field] = value
    _config_path(tmp_path).write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ProjectHookConfigError):
        load_project_hook_config(tmp_path)


@pytest.mark.parametrize(
    "field,value",
    [
        ("timeout_seconds", "5"),
        ("command", ".suxiaoyou/bin/policy"),
        ("environment", {"NO_COLOR": 1}),
    ],
)
def test_nested_scalar_types_are_not_coerced(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    payload = _valid_config()
    payload["hooks"][0][field] = value
    _config_path(tmp_path).write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ProjectHookConfigError):
        load_project_hook_config(tmp_path)


def test_duplicate_hook_ids_are_rejected_before_registration(tmp_path: Path) -> None:
    payload = _valid_config()
    duplicate = dict(payload["hooks"][0])
    duplicate["event"] = "Stop"
    payload["hooks"].append(duplicate)
    _config_path(tmp_path).write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ProjectHookConfigError, match="duplicate Hook ids"):
        load_project_hook_config(tmp_path)


def test_config_larger_than_64_kib_is_rejected_before_parsing(tmp_path: Path) -> None:
    path = _config_path(tmp_path)
    path.write_bytes(b" " * (MAX_PROJECT_HOOK_CONFIG_BYTES + 1))

    with pytest.raises(ProjectHookConfigError, match="exceeds"):
        load_project_hook_config(tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation varies on Windows CI")
def test_config_file_symlink_is_rejected_even_when_target_is_valid(
    tmp_path: Path,
) -> None:
    target = tmp_path / "outside-hooks.json"
    target.write_text(json.dumps(_valid_config()), encoding="utf-8")
    path = _config_path(tmp_path)
    path.symlink_to(target)

    with pytest.raises(ProjectHookConfigSecurityError, match="symlink"):
        load_project_hook_config(tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation varies on Windows CI")
def test_config_directory_symlink_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "hooks.json").write_text(json.dumps(_valid_config()), encoding="utf-8")
    (tmp_path / ".suxiaoyou").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ProjectHookConfigSecurityError, match="symlink"):
        load_project_hook_config(tmp_path)


def test_config_path_must_be_a_regular_file(tmp_path: Path) -> None:
    path = _config_path(tmp_path)
    path.mkdir()

    with pytest.raises(ProjectHookConfigSecurityError, match="regular file"):
        load_project_hook_config(tmp_path)
