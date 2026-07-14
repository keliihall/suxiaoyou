"""Code execution tool — run Python in a terminable OS sandbox."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from app.tool.base import ToolDefinition, ToolResult
from app.tool.builtin.bash import (
    _create_windows_process_job,
    _terminate_process_tree,
)
from app.tool.context import ToolContext
from app.tool.sandbox import (
    SandboxUnavailable,
    create_sandbox_scratch,
    prepare_sandbox_launch,
    validate_execution_platform,
    validate_workspace_private_boundary,
)
from app.tool.subprocess_compat import (
    IS_WINDOWS,
    decode_subprocess_output,
    get_subprocess_kwargs,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30  # seconds
MAX_TIMEOUT = 120
MAX_OUTPUT = 51200  # 50 KB per output stream
_PROCESS_POLL_SECONDS = 0.2
_PROGRESS_INTERVAL_SECONDS = 5.0


def _snapshot_workspace(workspace: str | None) -> dict[str, tuple[int, int]]:
    """Return a cheap file snapshot for detecting created/modified files."""
    if not workspace:
        return {}
    root = Path(workspace).resolve()
    if not root.is_dir():
        return {}

    snapshot: dict[str, tuple[int, int]] = {}
    try:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            snapshot[str(path.resolve())] = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return {}
    return snapshot


def _worker_command(code_path: Path) -> tuple[list[str], list[Path]]:
    """Return the Python worker argv and its read-only runtime resources."""

    if getattr(sys, "frozen", False):
        return (
            [sys.executable, "--sandbox-python-worker", str(code_path)],
            [Path(sys.executable)],
        )

    from app.tool import sandbox_worker

    worker_path = Path(sandbox_worker.__file__).resolve()
    return (
        [sys.executable, "-I", "-u", str(worker_path), str(code_path)],
        [worker_path],
    )


class CodeExecuteTool(ToolDefinition):

    @property
    def id(self) -> str:
        return "code_execute"

    @property
    def description(self) -> str:
        return (
            "Execute Python code in a fresh OS-sandboxed process. "
            "The code can access the selected workspace and read-only runtime "
            "files, has no network access, and is terminated with its child processes "
            "on timeout or abort. Installed packages such as pandas, numpy and "
            "matplotlib are available. No Python state persists between calls."
        )

    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python code to execute",
                },
                "timeout": {
                    "type": "integer",
                    "description": f"Timeout in seconds (default {DEFAULT_TIMEOUT}, max {MAX_TIMEOUT})",
                    "default": DEFAULT_TIMEOUT,
                },
            },
            "required": ["code"],
        }

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        code = args.get("code", "")
        if not code.strip():
            return ToolResult(error="No code provided")
        if not ctx.workspace:
            return ToolResult(error="Sandbox unavailable: execution requires a selected workspace")

        try:
            requested_timeout = int(args.get("timeout", DEFAULT_TIMEOUT))
        except (TypeError, ValueError):
            return ToolResult(error="Invalid execution timeout")
        timeout = max(1, min(requested_timeout, MAX_TIMEOUT))

        try:
            workspace = validate_workspace_private_boundary(ctx.workspace)
            validate_execution_platform()
        except SandboxUnavailable as exc:
            return ToolResult(error=f"Sandbox unavailable: {exc}")
        if not workspace.is_dir():
            return ToolResult(error=f"Sandbox unavailable: workspace does not exist: {workspace}")

        before_snapshot = _snapshot_workspace(str(workspace))
        scratch_dir: str | None = None
        try:
            scratch_dir = str(create_sandbox_scratch(
                workspace,
                prefix=f"python-{ctx.call_id}-",
            ))
            code_path = Path(scratch_dir) / "code.py"
            code_path.write_text(code, encoding="utf-8")
            command, read_paths = _worker_command(code_path)
            launch = prepare_sandbox_launch(
                command,
                workspace=str(workspace),
                cwd=str(workspace),
                scratch_dir=scratch_dir,
                read_only_paths=read_paths,
            )
        except (OSError, SandboxUnavailable) as exc:
            if scratch_dir is not None:
                shutil.rmtree(scratch_dir, ignore_errors=True)
            return ToolResult(error=f"Sandbox unavailable: {exc}")

        cancel_requested = threading.Event()

        def _run() -> tuple[int, bytes, bytes, str | None]:
            process_kwargs = dict(get_subprocess_kwargs())
            if IS_WINDOWS:
                process_kwargs["creationflags"] = (
                    int(process_kwargs.get("creationflags", 0))
                    | subprocess.CREATE_NEW_PROCESS_GROUP
                )
            else:
                process_kwargs["start_new_session"] = True

            process = subprocess.Popen(
                launch.argv,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=launch.cwd,
                env=launch.env,
                **process_kwargs,
            )
            windows_job = _create_windows_process_job(process)
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
                    windows_job.close()

        execution: asyncio.Task[tuple[int, bytes, bytes, str | None]] | None = None
        try:
            execution = asyncio.create_task(asyncio.to_thread(_run))
            started = time.monotonic()
            while not execution.done():
                done, _ = await asyncio.wait(
                    {execution}, timeout=_PROGRESS_INTERVAL_SECONDS
                )
                if not done:
                    ctx.publish_metadata(
                        title="Python",
                        metadata={
                            "elapsed_seconds": int(time.monotonic() - started),
                            "status": "running",
                            **launch.metadata,
                        },
                    )
            exit_code, stdout_bytes, stderr_bytes, termination = await execution
        except asyncio.CancelledError:
            cancel_requested.set()
            if execution is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(execution), timeout=3)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
            raise
        except (FileNotFoundError, PermissionError, OSError) as exc:
            return ToolResult(error=f"Sandbox launch failed: {exc}")
        finally:
            shutil.rmtree(scratch_dir, ignore_errors=True)

        if termination == "timeout":
            return ToolResult(
                error=f"Execution timed out after {timeout}s",
                metadata={"timeout": True, **launch.metadata},
            )
        if termination == "aborted":
            return ToolResult(
                error="Code execution aborted by user",
                metadata={"aborted": True, **launch.metadata},
            )

        stdout = decode_subprocess_output(stdout_bytes[:MAX_OUTPUT])
        stderr = decode_subprocess_output(stderr_bytes[:MAX_OUTPUT])
        parts: list[str] = []
        if stdout:
            parts.append(stdout)
        if stderr:
            parts.append(f"STDERR:\n{stderr}")
        output = "\n".join(parts) if parts else "(no output)"
        if exit_code != 0:
            output = f"Exit code: {exit_code}\n{output}"

        title = f"Python：{code[:60]}..." if len(code) > 60 else f"Python：{code}"
        after_snapshot = _snapshot_workspace(str(workspace))
        written_files = sorted(
            path
            for path, signature in after_snapshot.items()
            if before_snapshot.get(path) != signature
            and "/.suxiaoyou/sandbox/" not in path.replace("\\", "/")
        )

        return ToolResult(
            output=output,
            title=title,
            metadata={
                "exit_code": exit_code,
                "language": "python",
                "written_files": written_files,
                **launch.metadata,
            },
            error=(
                f"Code execution failed with exit code {exit_code}"
                if exit_code != 0
                else None
            ),
        )
