"""Strict, no-follow loading for project command Hook configuration.

The project file is deliberately a much smaller surface than plugin manifests:
``<workspace>/.suxiaoyou/hooks.json`` may declare only v1 command Hooks.  It
cannot select a source, register built-ins/plugins, or ask the application to
interpret shell text.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from app.hooks.models import HookCommandDeclaration
from app.hooks.registry import CommandHook, HookRegistry


PROJECT_HOOK_CONFIG_VERSION = 1
MAX_PROJECT_HOOK_CONFIG_BYTES = 64 * 1024
MAX_PROJECT_HOOKS = 128
PROJECT_HOOK_CONFIG_RELATIVE_PATH = Path(".suxiaoyou") / "hooks.json"

_WINDOWS_REPARSE_POINT = 0x0400
_TOP_LEVEL_FIELDS = frozenset({"version", "hooks"})
_HOOK_FIELDS = frozenset({
    "hook_id",
    "event",
    "failure_policy",
    "timeout_seconds",
    "command",
    "environment",
})
_REQUIRED_HOOK_FIELDS = frozenset({
    "hook_id",
    "event",
    "failure_policy",
    "command",
})


class ProjectHookConfigError(ValueError):
    """Raised when the project Hook file is unsafe or violates the v1 schema."""


class ProjectHookConfigSecurityError(ProjectHookConfigError):
    """Raised when a symlink, reparse point, race, or non-file is observed."""


class ProjectHookConfigV1(BaseModel):
    """Validated contents of ``.suxiaoyou/hooks.json``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[PROJECT_HOOK_CONFIG_VERSION]
    hooks: tuple[HookCommandDeclaration, ...]

    @model_validator(mode="after")
    def _bounded_unique_hooks(self) -> "ProjectHookConfigV1":
        if len(self.hooks) > MAX_PROJECT_HOOKS:
            raise ValueError(
                f"Project Hook config permits at most {MAX_PROJECT_HOOKS} Hooks"
            )
        identifiers = [hook.hook_id for hook in self.hooks]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Project Hook config contains duplicate Hook ids")
        return self


def _is_link_or_reparse(value: os.stat_result) -> bool:
    return stat.S_ISLNK(value.st_mode) or bool(
        getattr(value, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT
    )


def _identity(value: os.stat_result) -> tuple[int, int]:
    return (int(value.st_dev), int(value.st_ino))


def _snapshot(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(getattr(value, "st_mtime_ns", int(value.st_mtime * 1_000_000_000))),
        int(getattr(value, "st_ctime_ns", int(value.st_ctime * 1_000_000_000))),
    )


def _validate_directory(value: os.stat_result, *, label: str) -> None:
    if _is_link_or_reparse(value):
        raise ProjectHookConfigSecurityError(
            f"{label} must not be a symlink or reparse point"
        )
    if not stat.S_ISDIR(value.st_mode):
        raise ProjectHookConfigSecurityError(f"{label} must be a directory")


def _validate_regular_file(value: os.stat_result) -> None:
    if _is_link_or_reparse(value):
        raise ProjectHookConfigSecurityError(
            "Project Hook config must not be a symlink or reparse point"
        )
    if not stat.S_ISREG(value.st_mode):
        raise ProjectHookConfigSecurityError(
            "Project Hook config must be a regular file"
        )
    if value.st_size > MAX_PROJECT_HOOK_CONFIG_BYTES:
        raise ProjectHookConfigError(
            f"Project Hook config exceeds {MAX_PROJECT_HOOK_CONFIG_BYTES} bytes"
        )


def _read_bounded(fd: int) -> bytes:
    chunks: list[bytes] = []
    remaining = MAX_PROJECT_HOOK_CONFIG_BYTES + 1
    while remaining > 0:
        chunk = os.read(fd, min(64 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    if len(payload) > MAX_PROJECT_HOOK_CONFIG_BYTES:
        raise ProjectHookConfigError(
            f"Project Hook config exceeds {MAX_PROJECT_HOOK_CONFIG_BYTES} bytes"
        )
    return payload


def _read_posix(config_dir: Path) -> bytes | None:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    file_flags |= getattr(os, "O_NOFOLLOW", 0)

    before_directory = os.lstat(config_dir)
    _validate_directory(before_directory, label="Project Hook config directory")
    directory_fd = os.open(config_dir, directory_flags)
    try:
        opened_directory = os.fstat(directory_fd)
        _validate_directory(opened_directory, label="Project Hook config directory")
        if _identity(before_directory) != _identity(opened_directory):
            raise ProjectHookConfigSecurityError(
                "Project Hook config directory changed while it was opened"
            )

        try:
            before_file = os.stat(
                "hooks.json",
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return None
        _validate_regular_file(before_file)
        try:
            file_fd = os.open("hooks.json", file_flags, dir_fd=directory_fd)
        except OSError as exc:
            raise ProjectHookConfigSecurityError(
                "Project Hook config could not be opened without following links"
            ) from exc
        try:
            opened_file = os.fstat(file_fd)
            _validate_regular_file(opened_file)
            if _identity(before_file) != _identity(opened_file):
                raise ProjectHookConfigSecurityError(
                    "Project Hook config changed while it was opened"
                )
            payload = _read_bounded(file_fd)
            after_file = os.fstat(file_fd)
        finally:
            os.close(file_fd)

        try:
            current_file = os.stat(
                "hooks.json",
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            current_directory = os.lstat(config_dir)
        except FileNotFoundError as exc:
            raise ProjectHookConfigSecurityError(
                "Project Hook config changed while it was read"
            ) from exc
        _validate_regular_file(current_file)
        _validate_directory(current_directory, label="Project Hook config directory")
        if (
            _snapshot(opened_file) != _snapshot(after_file)
            or _snapshot(after_file) != _snapshot(current_file)
            or _identity(opened_directory) != _identity(current_directory)
        ):
            raise ProjectHookConfigSecurityError(
                "Project Hook config changed while it was read"
            )
        return payload
    finally:
        os.close(directory_fd)


def _read_portable(config_dir: Path) -> bytes | None:
    """Fallback for platforms without POSIX dir-fd/no-follow semantics.

    Windows reparse points are rejected before and after the handle is opened,
    and the handle/path identities must stay equal for the entire read.
    """

    before_directory = os.lstat(config_dir)
    _validate_directory(before_directory, label="Project Hook config directory")
    config_path = config_dir / "hooks.json"
    try:
        before_file = os.lstat(config_path)
    except FileNotFoundError:
        return None
    _validate_regular_file(before_file)

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(config_path, flags)
    except OSError as exc:
        raise ProjectHookConfigSecurityError(
            "Project Hook config could not be opened safely"
        ) from exc
    try:
        opened_file = os.fstat(fd)
        _validate_regular_file(opened_file)
        if _identity(before_file) != _identity(opened_file):
            raise ProjectHookConfigSecurityError(
                "Project Hook config changed while it was opened"
            )
        payload = _read_bounded(fd)
        after_file = os.fstat(fd)
    finally:
        os.close(fd)

    try:
        current_file = os.lstat(config_path)
        current_directory = os.lstat(config_dir)
    except FileNotFoundError as exc:
        raise ProjectHookConfigSecurityError(
            "Project Hook config changed while it was read"
        ) from exc
    _validate_regular_file(current_file)
    _validate_directory(current_directory, label="Project Hook config directory")
    if (
        _snapshot(opened_file) != _snapshot(after_file)
        or _snapshot(after_file) != _snapshot(current_file)
        or _identity(before_directory) != _identity(current_directory)
    ):
        raise ProjectHookConfigSecurityError(
            "Project Hook config changed while it was read"
        )
    return payload


def _read_config_bytes(workspace_root: str | Path) -> bytes | None:
    try:
        workspace = Path(workspace_root).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ProjectHookConfigError(
            "Hook workspace must be an existing directory"
        ) from exc
    if not workspace.is_dir():
        raise ProjectHookConfigError("Hook workspace must be an existing directory")

    config_dir = workspace / ".suxiaoyou"
    try:
        os.lstat(config_dir)
    except FileNotFoundError:
        return None
    try:
        if os.name == "posix":
            return _read_posix(config_dir)
        return _read_portable(config_dir)
    except ProjectHookConfigError:
        raise
    except OSError as exc:
        raise ProjectHookConfigSecurityError(
            "Project Hook config could not be read safely"
        ) from exc


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProjectHookConfigError(
                f"Project Hook config contains duplicate field {key!r}"
            )
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ProjectHookConfigError(
        f"Project Hook config contains invalid JSON constant {value!r}"
    )


def _require_exact_raw_types(payload: Any) -> dict[str, Any]:
    if type(payload) is not dict:
        raise ProjectHookConfigError("Project Hook config must be a JSON object")
    fields = set(payload)
    if fields != _TOP_LEVEL_FIELDS:
        unknown = fields - _TOP_LEVEL_FIELDS
        missing = _TOP_LEVEL_FIELDS - fields
        detail = "unknown fields" if unknown else "missing fields"
        names = unknown or missing
        raise ProjectHookConfigError(
            f"Project Hook config has {detail}: {', '.join(sorted(names))}"
        )
    if type(payload["version"]) is not int or payload["version"] != 1:
        raise ProjectHookConfigError("Project Hook config requires integer version 1")
    hooks = payload["hooks"]
    if type(hooks) is not list:
        raise ProjectHookConfigError("Project Hook config hooks must be an array")
    if len(hooks) > MAX_PROJECT_HOOKS:
        raise ProjectHookConfigError(
            f"Project Hook config permits at most {MAX_PROJECT_HOOKS} Hooks"
        )

    identifiers: set[str] = set()
    for index, declaration in enumerate(hooks):
        if type(declaration) is not dict:
            raise ProjectHookConfigError(
                f"Project Hook entry {index} must be an object"
            )
        fields = set(declaration)
        unknown = fields - _HOOK_FIELDS
        missing = _REQUIRED_HOOK_FIELDS - fields
        if unknown:
            raise ProjectHookConfigError(
                f"Project Hook entry {index} has unknown fields: "
                + ", ".join(sorted(unknown))
            )
        if missing:
            raise ProjectHookConfigError(
                f"Project Hook entry {index} has missing fields: "
                + ", ".join(sorted(missing))
            )
        for field in ("hook_id", "event", "failure_policy"):
            if type(declaration[field]) is not str:
                raise ProjectHookConfigError(
                    f"Project Hook entry {index} field {field!r} must be a string"
                )
        if declaration["hook_id"] in identifiers:
            raise ProjectHookConfigError(
                "Project Hook config contains duplicate Hook ids"
            )
        identifiers.add(declaration["hook_id"])
        command = declaration["command"]
        if type(command) is not list or any(
            type(item) is not str for item in command
        ):
            raise ProjectHookConfigError(
                f"Project Hook entry {index} command must be an array of strings"
            )
        if "timeout_seconds" in declaration and (
            type(declaration["timeout_seconds"]) not in (int, float)
        ):
            raise ProjectHookConfigError(
                f"Project Hook entry {index} timeout_seconds must be a number"
            )
        environment = declaration.get("environment", {})
        if type(environment) is not dict or any(
            type(key) is not str or type(value) is not str
            for key, value in environment.items()
        ):
            raise ProjectHookConfigError(
                f"Project Hook entry {index} environment must map strings to strings"
            )
    return payload


def load_project_hook_config(workspace_root: str | Path) -> ProjectHookConfigV1:
    """Load the optional project file, rejecting ambiguity and unsafe paths.

    A missing ``.suxiaoyou`` directory or ``hooks.json`` means no project
    Hooks.  Once either path exists in an unsafe form, loading fails closed.
    """

    encoded = _read_config_bytes(workspace_root)
    if encoded is None:
        return ProjectHookConfigV1(version=1, hooks=())
    try:
        text = encoded.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ProjectHookConfigError("Project Hook config must be UTF-8") from exc
    try:
        raw = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except ProjectHookConfigError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ProjectHookConfigError("Project Hook config is not valid JSON") from exc
    payload = _require_exact_raw_types(raw)
    try:
        return ProjectHookConfigV1.model_validate(payload)
    except ValidationError as exc:
        raise ProjectHookConfigError("Project Hook config does not match v1") from exc


def register_project_hook_config(
    registry: HookRegistry,
    config: ProjectHookConfigV1 | None = None,
) -> tuple[CommandHook, ...]:
    """Register only project command Hooks from the strict project file."""

    loaded = config or load_project_hook_config(registry.workspace_root)
    if not isinstance(loaded, ProjectHookConfigV1):
        raise TypeError("config must be a ProjectHookConfigV1")
    return registry.register_project_commands(loaded.hooks)


__all__ = [
    "MAX_PROJECT_HOOK_CONFIG_BYTES",
    "MAX_PROJECT_HOOKS",
    "PROJECT_HOOK_CONFIG_RELATIVE_PATH",
    "PROJECT_HOOK_CONFIG_VERSION",
    "ProjectHookConfigError",
    "ProjectHookConfigSecurityError",
    "ProjectHookConfigV1",
    "load_project_hook_config",
    "register_project_hook_config",
]
