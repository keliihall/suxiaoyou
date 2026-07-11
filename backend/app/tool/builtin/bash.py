"""Bash tool — shell execution with timeout.

Uses subprocess.Popen in a worker thread to avoid Windows event-loop issues
(SelectorEventLoop does not support asyncio.create_subprocess_exec).
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import threading
import time
from typing import Any

from app.tool.base import ToolDefinition, ToolResult
from app.tool.context import ToolContext
from app.tool.subprocess_compat import (
    IS_WINDOWS,
    decode_subprocess_output,
    find_shell,
    get_subprocess_kwargs,
)
from app.tool.workspace import WorkspaceViolation, get_default_output_dir, validate_cwd


_PROGRESS_INTERVAL_SECONDS = 5.0
_PROCESS_POLL_SECONDS = 0.2


class _WindowsKillOnCloseJob:
    """Own a Win32 Job Object whose close terminates the assigned tree."""

    def __init__(self, handle: Any, close_handle: Any) -> None:
        self._handle = handle
        self._close_handle = close_handle

    def close(self) -> None:
        if self._handle is None:
            return
        handle, self._handle = self._handle, None
        self._close_handle(handle)


def _create_windows_process_job(
    process: subprocess.Popen[bytes],
) -> _WindowsKillOnCloseJob | None:
    """Assign a Windows shell to a per-command kill-on-close Job Object."""

    if not IS_WINDOWS:
        return None

    import ctypes
    from ctypes import wintypes

    class JobObjectBasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class JobObjectExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JobObjectBasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = JobObjectExtendedLimitInformation()
    info.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
    configured = kernel32.SetInformationJobObject(
        job,
        9,  # JobObjectExtendedLimitInformation
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    process_handle = wintypes.HANDLE(int(process._handle))  # type: ignore[attr-defined]
    assigned = configured and kernel32.AssignProcessToJobObject(job, process_handle)
    if not assigned:
        kernel32.CloseHandle(job)
        return None
    return _WindowsKillOnCloseJob(job, kernel32.CloseHandle)


def _terminate_process_tree(
    process: subprocess.Popen[bytes],
    windows_job: _WindowsKillOnCloseJob | None = None,
) -> None:
    """Terminate *process* and every child it spawned.

    Cancelling ``asyncio.to_thread`` does not stop the underlying OS process.
    Bash tools therefore launch in their own process group and explicitly reap
    that group on user abort, timeout, or coroutine cancellation.
    """
    if IS_WINDOWS:
        if windows_job is not None:
            windows_job.close()
            return
        # Fallback for platforms where nested Job assignment was denied.  Run
        # taskkill even if the shell already exited: its descendant may still
        # be discoverable briefly, and an early poll-return guarantees leakage.
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
                **get_subprocess_kwargs(),
            )
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass
        return

    # The shell/group leader may exit on SIGTERM while a child ignores it and
    # keeps stdout/stderr pipes open.  Always follow the grace period with a
    # group-wide SIGKILL; waiting only for the leader would make abort hang until
    # that surviving child exits.  Also attempt the group kill when the leader
    # already exited, because the process group can still contain descendants.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass

def _bash_cfg():
    from app.config import get_settings
    s = get_settings()
    return s.bash_timeout, s.bash_max_timeout


class BashTool(ToolDefinition):

    @property
    def id(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        return (
            "Execute a shell command. Returns stdout and stderr. "
            "Commands run in the project directory. "
            "Timeout defaults to 120 seconds (max 600)."
        )

    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds",
                    "default": _bash_cfg()[0],
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory for the command",
                },
            },
            "required": ["command"],
        }

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = args["command"]
        default_timeout, max_timeout = _bash_cfg()
        timeout = min(args.get("timeout", default_timeout), max_timeout)
        cwd = args.get("cwd")

        # Workspace restriction: validate/default cwd (defaults to suxiaoyou_written/)
        try:
            if not cwd and ctx.workspace:
                cwd = get_default_output_dir(ctx.workspace)
            cwd = validate_cwd(cwd, ctx.workspace)
        except WorkspaceViolation as e:
            return ToolResult(error=str(e))

        # Ensure cwd exists — suxiaoyou_written/ may not have been created yet
        if cwd:
            import pathlib
            try:
                pathlib.Path(cwd).mkdir(parents=True, exist_ok=True)
            except OSError:
                # If we can't create it, fall back to workspace or None
                cwd = ctx.workspace or None

        extra_kwargs = get_subprocess_kwargs()
        shell_prefix = find_shell()

        cancel_requested = threading.Event()

        def _run() -> tuple[int, bytes, bytes, str | None]:
            process_kwargs = dict(extra_kwargs)
            if IS_WINDOWS:
                process_kwargs["creationflags"] = (
                    int(process_kwargs.get("creationflags", 0))
                    | subprocess.CREATE_NEW_PROCESS_GROUP
                )
            else:
                process_kwargs["start_new_session"] = True

            launch_command = command
            if IS_WINDOWS:
                # Gate user code on one stdin line.  Popen returns before a
                # Win32 process can be assigned to our Job Object; without this
                # gate a very short PowerShell parent could spawn a detached
                # child and exit in that assignment window.
                launch_command = (
                    "[Console]::In.ReadLine() | Out-Null; " + command
                )
            process = subprocess.Popen(
                [*shell_prefix, launch_command],
                shell=False,
                stdin=subprocess.PIPE if IS_WINDOWS else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env={**os.environ},
                **process_kwargs,
            )
            try:
                windows_job = _create_windows_process_job(process)
            finally:
                if IS_WINDOWS and process.stdin is not None:
                    try:
                        process.stdin.write(b"\n")
                        process.stdin.flush()
                    finally:
                        process.stdin.close()
                        # ``communicate`` otherwise tries to flush the already
                        # closed stream on some Python versions.
                        process.stdin = None

            try:
                started = time.monotonic()
                while True:
                    try:
                        stdout, stderr = process.communicate(timeout=_PROCESS_POLL_SECONDS)
                        return process.returncode, stdout, stderr, None
                    except subprocess.TimeoutExpired:
                        if ctx.abort_event.is_set() or cancel_requested.is_set():
                            _terminate_process_tree(process, windows_job)
                            stdout, stderr = process.communicate()
                            return process.returncode or -1, stdout, stderr, "aborted"
                        if time.monotonic() - started >= timeout:
                            _terminate_process_tree(process, windows_job)
                            stdout, stderr = process.communicate()
                            return process.returncode or -1, stdout, stderr, "timeout"
            finally:
                if windows_job is not None:
                    # Also reap a detached background child when its shell exits
                    # normally.  Such a child is outside the command's declared
                    # lifetime and must not leak into later tasks.
                    windows_job.close()

        try:
            execution = asyncio.create_task(asyncio.to_thread(_run))
            started = time.monotonic()
            while not execution.done():
                done, _ = await asyncio.wait(
                    {execution}, timeout=_PROGRESS_INTERVAL_SECONDS
                )
                if not done:
                    ctx.publish_metadata(
                        title=command[:80],
                        metadata={
                            "elapsed_seconds": int(time.monotonic() - started),
                            "status": "running",
                        },
                    )
            exit_code, stdout_bytes, stderr_bytes, termination = await execution
        except asyncio.CancelledError:
            cancel_requested.set()
            try:
                await asyncio.wait_for(asyncio.shield(execution), timeout=3)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            raise
        except FileNotFoundError:
            return ToolResult(error="Shell not found")
        except PermissionError:
            return ToolResult(error="Permission denied")

        if termination == "timeout":
            return ToolResult(
                error=f"Command timed out after {timeout}s",
                metadata={"timeout": True},
            )
        if termination == "aborted":
            return ToolResult(
                error="Command aborted by user",
                metadata={"aborted": True},
            )

        stdout = decode_subprocess_output(stdout_bytes)
        stderr = decode_subprocess_output(stderr_bytes)

        output_parts = []
        if stdout:
            output_parts.append(stdout)
        if stderr:
            output_parts.append(f"STDERR:\n{stderr}")

        output = "\n".join(output_parts) if output_parts else "(no output)"
        if exit_code != 0:
            output = f"Exit code: {exit_code}\n{output}"

        return ToolResult(
            output=output,
            title=command[:80],
            metadata={"exit_code": exit_code},
            error=f"Command failed with exit code {exit_code}" if exit_code != 0 else None,
        )
