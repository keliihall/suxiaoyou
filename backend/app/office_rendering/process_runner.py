"""No-shell process-tree runner used by concrete Office render providers.

The implementation delegates to the repository's hardened POSIX process-group
and Windows kill-on-close Job Object supervisors.  Those supervisors reap
descendants on success, timeout, cancellation, and failure.  This adapter keeps
the rendering package injectable and converts their platform-specific results
into one small async protocol.
"""

from __future__ import annotations

import asyncio
import math
import os
import stat
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from app.office_rendering.errors import (
    RenderContractError,
    RenderProcessError,
    RenderTimeoutError,
)
from app.tool.posix_process import PosixProcessResult, run_posix_process
from app.tool.windows_process import WindowsProcessResult, run_windows_process


DEFAULT_PROCESS_OUTPUT_BYTES = 64 * 1024
_ALLOWED_ENVIRONMENT_KEYS = frozenset(
    {
        "fontconfig_file",
        "fontconfig_path",
        "home",
        "lang",
        "lc_all",
        "path",
        "sal_disable_opencl",
        "sal_disablegl",
        "sal_use_vclplugin",
        "systemroot",
        "temp",
        "tmp",
        "tmpdir",
        "userprofile",
        "windir",
        "xdg_cache_home",
        "xdg_config_home",
        "xdg_data_home",
        "xdg_runtime_dir",
    }
)


@dataclass(frozen=True, slots=True)
class RenderProcessResult:
    """Bounded terminal output from one successfully supervised process."""

    returncode: int
    stdout: bytes
    stderr: bytes


@runtime_checkable
class RenderProcessRunner(Protocol):
    """Run one argv-form command without a shell and own its whole process tree."""

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_seconds: float,
    ) -> RenderProcessResult:
        """Return bounded output or raise after the process tree is reaped."""
        ...


PosixSupervisor = Callable[..., PosixProcessResult]
WindowsSupervisor = Callable[..., WindowsProcessResult]


class LocalProcessTreeRunner:
    """Async adapter over the hardened native process-tree supervisors."""

    def __init__(
        self,
        *,
        max_output_bytes: int = DEFAULT_PROCESS_OUTPUT_BYTES,
        platform_name: str | None = None,
        _posix_supervisor: PosixSupervisor = run_posix_process,
        _windows_supervisor: WindowsSupervisor = run_windows_process,
    ) -> None:
        if (
            not isinstance(max_output_bytes, int)
            or isinstance(max_output_bytes, bool)
            or max_output_bytes < 1
        ):
            raise RenderContractError("process output limit must be positive")
        selected = platform_name or os.name
        if selected not in {"posix", "nt"}:
            raise RenderContractError("unsupported Office renderer process platform")
        self.max_output_bytes = max_output_bytes
        self.platform_name = selected
        self._posix_supervisor = _posix_supervisor
        self._windows_supervisor = _windows_supervisor

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_seconds: float,
    ) -> RenderProcessResult:
        args = tuple(argv)
        if not args or any(not isinstance(item, str) or not item for item in args):
            raise RenderContractError("renderer argv must contain bounded strings")
        if any("\x00" in item or len(item) > 32_768 for item in args):
            raise RenderContractError("renderer argv contains invalid data")
        executable = Path(args[0])
        if not executable.is_absolute():
            raise RenderContractError("renderer executable path must be absolute")
        workdir = Path(cwd)
        if not workdir.is_absolute():
            raise RenderContractError("renderer cwd must be an existing absolute directory")
        try:
            executable_info = executable.lstat()
            resolved_executable = executable.resolve(strict=True)
            workdir_info = workdir.lstat()
            resolved_workdir = workdir.resolve(strict=True)
        except OSError as exc:
            raise RenderContractError("renderer process boundary is unavailable") from exc
        if (
            stat.S_ISLNK(executable_info.st_mode)
            or not stat.S_ISREG(executable_info.st_mode)
            or resolved_executable != executable
            or stat.S_ISLNK(workdir_info.st_mode)
            or not stat.S_ISDIR(workdir_info.st_mode)
            or resolved_workdir != workdir
        ):
            raise RenderContractError("renderer process boundary is redirected")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds <= 0
        ):
            raise RenderContractError("renderer timeout must be positive and finite")
        normalized_env = _validate_environment(
            env,
            windows=self.platform_name == "nt",
            workdir=workdir,
            executable=executable,
        )
        abort = threading.Event()

        if self.platform_name == "posix":
            worker = asyncio.create_task(
                asyncio.to_thread(
                    self._posix_supervisor,
                    args,
                    cwd=str(workdir),
                    env=normalized_env,
                    timeout_seconds=float(timeout_seconds),
                    should_abort=abort.is_set,
                    max_output_bytes=self.max_output_bytes,
                )
            )
        else:
            worker = asyncio.create_task(
                asyncio.to_thread(
                    self._windows_supervisor,
                    args,
                    cwd=str(workdir),
                    env=normalized_env,
                    timeout_seconds=float(timeout_seconds),
                    should_abort=abort.is_set,
                    max_output_bytes=self.max_output_bytes,
                )
            )

        try:
            result = await asyncio.shield(worker)
        except asyncio.CancelledError as cancellation:
            abort.set()
            # Cancellation is advisory until the native supervisor proves the
            # complete child tree has been reaped.  A second (or later)
            # ``cancel()`` must not interrupt this cleanup handshake and let a
            # renderer descendant escape the caller's lifetime.
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    abort.set()
                    continue
                except BaseException:
                    break
            if worker.done():
                try:
                    worker.result()
                except BaseException:
                    # Cleanup completion, rather than the supervisor's result,
                    # is authoritative once the caller has cancelled.
                    pass
            raise cancellation
        except Exception as exc:
            raise RenderProcessError("Office renderer process supervision failed") from exc

        return _normalize_result(result)


def _validate_environment(
    env: Mapping[str, str],
    *,
    windows: bool,
    workdir: Path,
    executable: Path,
) -> dict[str, str]:
    if not isinstance(env, Mapping):
        raise RenderContractError("renderer environment must be a mapping")
    normalized: dict[str, str] = {}
    seen_casefolded: set[str] = set()
    for key, value in env.items():
        if (
            not isinstance(key, str)
            or not key
            or "=" in key
            or "\x00" in key
            or len(key) > 256
        ):
            raise RenderContractError("renderer environment key is invalid")
        if not isinstance(value, str) or "\x00" in value or len(value) > 32_768:
            raise RenderContractError("renderer environment value is invalid")
        folded = key.casefold()
        if folded not in _ALLOWED_ENVIRONMENT_KEYS:
            raise RenderContractError("renderer environment key is not allowed")
        if folded in seen_casefolded:
            raise RenderContractError("renderer environment key is ambiguous")
        seen_casefolded.add(folded)
        normalized[key] = value
    _validate_environment_paths(
        normalized,
        windows=windows,
        workdir=workdir,
        executable=executable,
    )
    return normalized


def _validate_environment_paths(
    environment: Mapping[str, str],
    *,
    windows: bool,
    workdir: Path,
    executable: Path,
) -> None:
    by_folded = {key.casefold(): value for key, value in environment.items()}
    private_directories = {
        "home",
        "userprofile",
        "temp",
        "tmp",
        "tmpdir",
        "xdg_cache_home",
        "xdg_config_home",
        "xdg_data_home",
        "xdg_runtime_dir",
        "fontconfig_path",
    }
    for key in private_directories & set(by_folded):
        path = Path(by_folded[key])
        try:
            info = path.lstat()
            resolved = path.resolve(strict=True)
            resolved.relative_to(workdir)
        except (OSError, ValueError) as exc:
            raise RenderContractError(
                "renderer private environment escaped its work directory"
            ) from exc
        if (
            not path.is_absolute()
            or stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or resolved != path
        ):
            raise RenderContractError("renderer private environment is redirected")

    fontconfig_file = by_folded.get("fontconfig_file")
    fontconfig_path = by_folded.get("fontconfig_path")
    if (fontconfig_file is None) != (fontconfig_path is None):
        raise RenderContractError("renderer Fontconfig environment is incomplete")
    if fontconfig_file is not None:
        path = Path(fontconfig_file)
        try:
            info = path.lstat()
            resolved = path.resolve(strict=True)
            resolved.relative_to(workdir)
        except (OSError, ValueError) as exc:
            raise RenderContractError("renderer Fontconfig escaped its work directory") from exc
        if (
            not path.is_absolute()
            or stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or resolved != path
            or resolved.parent != Path(fontconfig_path)
        ):
            raise RenderContractError("renderer Fontconfig environment is redirected")

    path_value = by_folded.get("path")
    if path_value is not None:
        entries = tuple(Path(value) for value in path_value.split(os.pathsep))
        safe_entries = True
        for path in entries:
            try:
                info = path.lstat()
                resolved = path.resolve(strict=True)
            except OSError:
                safe_entries = False
                break
            if (
                not path.is_absolute()
                or stat.S_ISLNK(info.st_mode)
                or not stat.S_ISDIR(info.st_mode)
                or resolved != path
            ):
                safe_entries = False
                break
        if (
            not entries
            or not safe_entries
            or executable.parent not in entries
        ):
            raise RenderContractError("renderer PATH is invalid")

    fixed_values = {
        "sal_disable_opencl": "1",
        "sal_disablegl": "1",
        "sal_use_vclplugin": "svp",
    }
    if any(
        key in by_folded and by_folded[key] != expected
        for key, expected in fixed_values.items()
    ):
        raise RenderContractError("renderer process policy environment is invalid")

    if windows:
        for key in ("systemroot", "windir"):
            value = by_folded.get(key)
            if value is not None and not os.path.isabs(value):
                raise RenderContractError("renderer Windows environment is invalid")


def _normalize_result(result: Any) -> RenderProcessResult:
    if isinstance(result, PosixProcessResult):
        termination = result.termination
        returncode = result.exit_code
        stdout = result.stdout
        stderr = result.stderr
    elif isinstance(result, WindowsProcessResult):
        termination = result.termination
        returncode = result.exit_code
        stdout = result.stdout
        stderr = result.stderr
    else:
        raise RenderProcessError("Office renderer supervisor returned an invalid result")

    if termination == "timeout":
        raise RenderTimeoutError(
            "Office renderer timed out and its process tree was terminated"
        )
    if termination == "aborted":
        raise RenderProcessError("Office renderer process tree was aborted")
    return RenderProcessResult(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )
