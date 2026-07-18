"""Deterministic registration and identity resolution for v1.1 Hooks."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias

from app.hooks.models import (
    BuiltinHookDeclaration,
    HookCommandDeclaration,
    HookDecision,
    HookEvent,
    HookEventName,
    HookSource,
)
from app.mcp.local_approval import LocalMcpLaunchSpec, local_mcp_launch_spec


BuiltinHookReturn: TypeAlias = (
    HookDecision
    | Mapping[str, Any]
    | Awaitable[HookDecision | Mapping[str, Any]]
)
BuiltinHookHandler: TypeAlias = Callable[[HookEvent], BuiltinHookReturn]

_HOOK_ENV_OVERRIDE_KEYS = frozenset({
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "NO_COLOR",
    "PYTHONIOENCODING",
    "PYTHONUTF8",
    "TZ",
})


def minimal_hook_environment(overrides: Mapping[str, str]) -> dict[str, str]:
    """Build a credential-free, explicit process environment.

    The executable is already canonical and absolute, so PATH is only a small
    system fallback for shebangs and deliberate descendants. Project/plugin
    manifests may adjust encoding/locale controls but cannot request arbitrary
    inherited variables or credential-shaped names.
    """

    unknown = set(overrides) - _HOOK_ENV_OVERRIDE_KEYS
    if unknown:
        raise ValueError(
            "Hook environment contains non-allow-listed keys: "
            + ", ".join(sorted(unknown))
        )

    if os.name == "nt":
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        temp_dir = tempfile.gettempdir()
        environment = {
            "COMSPEC": str(Path(system_root) / "System32" / "cmd.exe"),
            "NO_COLOR": "1",
            "PATH": str(Path(system_root) / "System32"),
            "PATHEXT": ".COM;.EXE;.BAT;.CMD",
            "PYTHONUTF8": "1",
            "SystemRoot": system_root,
            "TEMP": temp_dir,
            "TMP": temp_dir,
            "WINDIR": system_root,
        }
    else:
        environment = {
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "NO_COLOR": "1",
            "PATH": os.defpath,
            "PYTHONUTF8": "1",
        }
    environment.update(overrides)
    return environment


@dataclass(frozen=True, slots=True)
class CommandHook:
    declaration: HookCommandDeclaration
    source: HookSource
    source_name: str
    source_root: Path
    workspace_root: Path
    launch: LocalMcpLaunchSpec
    fingerprint: str
    trust_key: str

    def public_descriptor(self) -> dict[str, Any]:
        return {
            **self.launch.public_descriptor(),
            "hook_id": self.declaration.hook_id,
            "event": self.declaration.event.value,
            "source": self.source.value,
            "source_name": self.source_name,
            "failure_policy": self.declaration.failure_policy.value,
            "timeout_seconds": self.declaration.timeout_seconds,
            # The approval boundary is the complete Hook identity, not only
            # the underlying executable launch fingerprint.
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True, slots=True)
class BuiltinHook:
    declaration: BuiltinHookDeclaration
    handler: BuiltinHookHandler

    @property
    def source(self) -> HookSource:
        return HookSource.BUILTIN

    @property
    def source_name(self) -> str:
        return "application"


RegisteredHook: TypeAlias = BuiltinHook | CommandHook


def _canonical_directory(path: str | Path, *, label: str) -> Path:
    try:
        result = Path(path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"{label} must be an existing directory") from exc
    if not result.is_dir():
        raise ValueError(f"{label} must be an existing directory")
    return result


def _trust_key(source: HookSource, source_name: str, hook_id: str) -> str:
    identity = json.dumps(
        [source.value, source_name, hook_id],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"hook:{source.value}:{hashlib.sha256(identity).hexdigest()}"


def resolve_command_hook(
    declaration: HookCommandDeclaration,
    *,
    source: HookSource,
    source_name: str,
    source_root: str | Path,
    workspace_root: str | Path,
) -> CommandHook:
    """Resolve and hash the complete launch identity of a local command Hook."""

    if source not in {HookSource.PROJECT, HookSource.PLUGIN}:
        raise ValueError("Command Hooks must have project or plugin provenance")
    if not source_name or len(source_name) > 160:
        raise ValueError("Invalid Hook source name")

    canonical_source = _canonical_directory(source_root, label="Hook source root")
    canonical_workspace = _canonical_directory(
        workspace_root,
        label="Hook workspace root",
    )
    requested_executable = Path(declaration.command[0]).expanduser()
    if not requested_executable.is_absolute():
        requested_executable = canonical_source / requested_executable
    requested_command = (
        str(requested_executable),
        *declaration.command[1:],
    )
    environment = minimal_hook_environment(declaration.environment)
    launch = local_mcp_launch_spec({
        "command": requested_command,
        "cwd": str(canonical_workspace),
        "environment": environment,
        "inherit_environment": False,
    })

    executable = Path(launch.executable_path)
    if not executable.is_relative_to(canonical_source):
        raise ValueError(
            "Project/plugin Hook executable must resolve inside its source root"
        )

    canonical_identity = json.dumps(
        {
            "version": 1,
            "hook_id": declaration.hook_id,
            "event": declaration.event.value,
            "source": source.value,
            "source_name": source_name,
            "failure_policy": declaration.failure_policy.value,
            "timeout_seconds": declaration.timeout_seconds,
            "argv": list(launch.command),
            "cwd": launch.cwd,
            "environment": launch.environment,
            "executable_path": launch.executable_path,
            "executable_sha256": launch.executable_sha256,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    fingerprint = f"sha256:{hashlib.sha256(canonical_identity).hexdigest()}"
    return CommandHook(
        declaration=declaration,
        source=source,
        source_name=source_name,
        source_root=canonical_source,
        workspace_root=canonical_workspace,
        launch=launch,
        fingerprint=fingerprint,
        trust_key=_trust_key(source, source_name, declaration.hook_id),
    )


def refresh_command_hook(hook: CommandHook) -> CommandHook:
    """Re-resolve a command immediately before trust checking or launch."""

    return resolve_command_hook(
        hook.declaration,
        source=hook.source,
        source_name=hook.source_name,
        source_root=hook.source_root,
        workspace_root=hook.workspace_root,
    )


class HookRegistry:
    """Ordered registry for built-in, project, and plugin Hooks."""

    def __init__(self, workspace_root: str | Path) -> None:
        self.workspace_root = _canonical_directory(
            workspace_root,
            label="Hook workspace root",
        )
        self._hooks: list[RegisteredHook] = []
        self._keys: set[tuple[str, str, str]] = set()

    def _add(self, hook: RegisteredHook) -> None:
        key = (
            hook.source.value,
            hook.source_name,
            hook.declaration.hook_id,
        )
        if key in self._keys:
            raise ValueError(f"Duplicate Hook registration: {key!r}")
        self._keys.add(key)
        self._hooks.append(hook)

    def register_builtin(
        self,
        declaration: BuiltinHookDeclaration | Mapping[str, Any],
        handler: BuiltinHookHandler,
    ) -> BuiltinHook:
        if not callable(handler):
            raise ValueError("Built-in Hook handler must be callable")
        parsed = (
            declaration
            if isinstance(declaration, BuiltinHookDeclaration)
            else BuiltinHookDeclaration.model_validate(declaration)
        )
        hook = BuiltinHook(declaration=parsed, handler=handler)
        self._add(hook)
        return hook

    def register_project_commands(
        self,
        declarations: Iterable[HookCommandDeclaration | Mapping[str, Any]],
    ) -> tuple[CommandHook, ...]:
        return self._register_commands(
            declarations,
            source=HookSource.PROJECT,
            source_name="project",
            source_root=self.workspace_root,
        )

    def register_plugin_commands(
        self,
        plugin_name: str,
        plugin_root: str | Path,
        declarations: Iterable[HookCommandDeclaration | Mapping[str, Any]],
    ) -> tuple[CommandHook, ...]:
        return self._register_commands(
            declarations,
            source=HookSource.PLUGIN,
            source_name=plugin_name,
            source_root=plugin_root,
        )

    def _register_commands(
        self,
        declarations: Iterable[HookCommandDeclaration | Mapping[str, Any]],
        *,
        source: HookSource,
        source_name: str,
        source_root: str | Path,
    ) -> tuple[CommandHook, ...]:
        hooks: list[CommandHook] = []
        keys = set(self._keys)
        for declaration in declarations:
            parsed = (
                declaration
                if isinstance(declaration, HookCommandDeclaration)
                else HookCommandDeclaration.model_validate(declaration)
            )
            key = (source.value, source_name, parsed.hook_id)
            if key in keys:
                raise ValueError(f"Duplicate Hook registration: {key!r}")
            keys.add(key)
            hook = resolve_command_hook(
                parsed,
                source=source,
                source_name=source_name,
                source_root=source_root,
                workspace_root=self.workspace_root,
            )
            hooks.append(hook)
        # Registration is atomic: an unresolved/escaping command or duplicate
        # leaves the previous registry unchanged rather than enabling a prefix
        # of a project configuration.
        self._hooks.extend(hooks)
        self._keys = keys
        return tuple(hooks)

    def hooks_for(self, event: HookEventName | str) -> tuple[RegisteredHook, ...]:
        event_name = HookEventName(event)
        return tuple(
            hook for hook in self._hooks
            if hook.declaration.event is event_name
        )

    @property
    def hooks(self) -> tuple[RegisteredHook, ...]:
        return tuple(self._hooks)


__all__ = [
    "BuiltinHook",
    "BuiltinHookHandler",
    "CommandHook",
    "HookRegistry",
    "RegisteredHook",
    "minimal_hook_environment",
    "refresh_command_hook",
    "resolve_command_hook",
]
