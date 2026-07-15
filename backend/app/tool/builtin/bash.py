"""Bash tool — shell execution with timeout.

Uses the shared POSIX process-group or Win32 suspended-Job runner in a worker
thread so timeout, cancellation, bounded output, and descendant cleanup have
the same contract on every desktop platform.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from app.tool.base import ToolDefinition, ToolResult
from app.tool.context import ToolContext
from app.tool.execution_workspace import ExecutionWorkspace
from app.tool.posix_process import PosixProcessCleanupError, run_posix_process
from app.tool.subprocess_compat import (
    IS_WINDOWS,
    decode_subprocess_output,
    find_shell,
    prepare_shell_command,
)
from app.tool.windows_process import WindowsProcessError, run_windows_process
from app.tool.sandbox import (
    SandboxUnavailable,
    prepare_sandbox_launch,
    validate_execution_platform,
    validate_workspace_private_boundary,
)
from app.tool.workspace import WorkspaceViolation, get_default_output_dir, validate_cwd
from app.tool.workspace_transaction import WorkspaceMutationError


_PROGRESS_INTERVAL_SECONDS = 5.0
_MAX_OUTPUT_BYTES = 2 * 1024 * 1024

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
            "Execute a real shell command (PowerShell on Windows; bash/sh on "
            "macOS and Linux) and return stdout and stderr. Commands run in "
            "the selected project, may use the network after permission, and "
            "are terminated together with all child processes on completion. "
            "HOME, package caches, and user-installed CLI locations persist "
            "for this session only; temporary files do not. Check whether a "
            "dependency is already available before installing it again. "
            "Install Python CLIs into a project-local virtual environment "
            "created with `python -m venv --copies .venv` instead of using "
            "the host/global pip environment. "
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

        try:
            if not ctx.workspace:
                raise SandboxUnavailable("Execution requires a selected workspace")
            workspace = validate_workspace_private_boundary(ctx.workspace)
            validate_execution_platform()
        except SandboxUnavailable as exc:
            return ToolResult(error=f"Sandbox unavailable: {exc}")

        # Workspace restriction: validate/default cwd (defaults to suxiaoyou_written/)
        try:
            if not cwd:
                cwd = get_default_output_dir(str(workspace))
            cwd = validate_cwd(cwd, str(workspace))
        except WorkspaceViolation as e:
            return ToolResult(error=str(e))

        # Run against an application-private copy mounted at the canonical
        # workspace path. The real workspace is touched only after exit=0,
        # write-ahead versions, and atomic replacement preparation succeed.
        transaction = ExecutionWorkspace(
            workspace,
            ctx,
            operation="bash",
        )
        try:
            staged_workspace = await asyncio.to_thread(transaction.prepare)
            staged_cwd = transaction.staged_path(cwd or workspace)
            staged_cwd.mkdir(parents=True, exist_ok=True)
            scratch_dir, _logical_scratch = transaction.create_scratch(
                prefix=f"bash-{ctx.call_id}-",
            )
            persistent_environment = transaction.create_persistent_environment()
            if ctx.abort_event.is_set():
                transaction.abort()
                return ToolResult(
                    error="Command aborted by user",
                    metadata={
                        "aborted": True,
                        **transaction.failure_metadata,
                    },
                )
        except asyncio.CancelledError:
            transaction.abort()
            raise
        except (OSError, SandboxUnavailable, WorkspaceMutationError) as exc:
            transaction.abort()
            return ToolResult(
                error=transaction.redact_output(f"Sandbox unavailable: {exc}"),
                metadata=transaction.failure_metadata,
            )

        shell_prefix = find_shell()
        try:
            shell_command = prepare_shell_command(shell_prefix, command)
        except ValueError as exc:
            transaction.abort()
            return ToolResult(
                error=str(exc),
                metadata=transaction.failure_metadata,
            )

        try:
            launch = prepare_sandbox_launch(
                [*shell_prefix, shell_command],
                workspace=str(workspace),
                workspace_source=str(staged_workspace),
                cwd=cwd,
                scratch_dir=scratch_dir,
                persistent_environment=persistent_environment,
                allow_network=True,
            )
        except SandboxUnavailable as exc:
            transaction.abort()
            return ToolResult(
                error=transaction.redact_output(f"Sandbox unavailable: {exc}"),
                metadata=transaction.failure_metadata,
            )

        cancel_requested = threading.Event()

        def _run() -> tuple[int, bytes, bytes, str | None, bool]:
            should_abort = lambda: (
                ctx.abort_event.is_set() or cancel_requested.is_set()
            )
            if IS_WINDOWS:
                result = run_windows_process(
                    launch.argv,
                    cwd=launch.cwd,
                    env=launch.env,
                    timeout_seconds=timeout,
                    should_abort=should_abort,
                    max_output_bytes=_MAX_OUTPUT_BYTES,
                )
            else:
                result = run_posix_process(
                    launch.argv,
                    cwd=launch.cwd,
                    env=launch.env,
                    timeout_seconds=timeout,
                    should_abort=should_abort,
                    max_output_bytes=_MAX_OUTPUT_BYTES,
                )
            truncated = bool(
                getattr(result, "truncated", False)
                or getattr(result, "stdout_truncated", False)
                or getattr(result, "stderr_truncated", False)
            )
            return (
                result.exit_code,
                result.stdout,
                result.stderr,
                result.termination,
                truncated,
            )

        try:
            execution = asyncio.create_task(asyncio.to_thread(_run))
            started = time.monotonic()
            while not execution.done():
                done, _ = await asyncio.wait(
                    {execution}, timeout=_PROGRESS_INTERVAL_SECONDS
                )
                if not done:
                    ctx.publish_metadata(
                        title=transaction.redact_output(command[:80]),
                        metadata={
                            "elapsed_seconds": int(time.monotonic() - started),
                            "status": "running",
                        },
                    )
            exit_code, stdout_bytes, stderr_bytes, termination, output_truncated = (
                await execution
            )
        except asyncio.CancelledError:
            cancel_requested.set()
            try:
                await asyncio.wait_for(asyncio.shield(execution), timeout=20)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                # Never delete the staged view out from under a worker that is
                # still proving process-tree teardown.
                execution.add_done_callback(lambda _future: transaction.abort())
            else:
                transaction.abort()
            raise
        except FileNotFoundError:
            transaction.abort()
            return ToolResult(
                error="Shell not found",
                metadata=transaction.failure_metadata,
            )
        except PermissionError:
            transaction.abort()
            return ToolResult(
                error="Permission denied",
                metadata=transaction.failure_metadata,
            )
        except (PosixProcessCleanupError, WindowsProcessError, OSError) as exc:
            transaction.abort()
            return ToolResult(
                error=transaction.redact_output(
                    f"Command process cleanup failed: {exc}",
                ),
                metadata=transaction.failure_metadata,
            )

        if termination == "timeout":
            transaction.abort()
            return ToolResult(
                error=f"Command timed out after {timeout}s",
                metadata={
                    "timeout": True,
                    "output_truncated": output_truncated,
                    "process_tree_reaped": True,
                    **transaction.failure_metadata,
                    **launch.metadata,
                },
            )
        if termination == "aborted":
            transaction.abort()
            return ToolResult(
                error="Command aborted by user",
                metadata={
                    "aborted": True,
                    "output_truncated": output_truncated,
                    "process_tree_reaped": True,
                    **transaction.failure_metadata,
                    **launch.metadata,
                },
            )

        stdout = transaction.redact_output(decode_subprocess_output(stdout_bytes))
        stderr = transaction.redact_output(decode_subprocess_output(stderr_bytes))

        output_parts = []
        if stdout:
            output_parts.append(stdout)
        if stderr:
            output_parts.append(f"STDERR:\n{stderr}")

        output = "\n".join(output_parts) if output_parts else "(no output)"
        if output_truncated:
            output += "\n[output truncated after 2 MiB per stream]"
        if exit_code != 0:
            output = f"Exit code: {exit_code}\n{output}"
            transaction.abort()
            return ToolResult(
                output=output,
                title=transaction.redact_output(command[:80]),
                metadata={
                    "exit_code": exit_code,
                    "output_truncated": output_truncated,
                    "process_tree_reaped": True,
                    **transaction.failure_metadata,
                    **launch.metadata,
                },
                error=f"Command failed with exit code {exit_code}",
            )

        try:
            commit = await asyncio.to_thread(transaction.commit)
        except WorkspaceMutationError as exc:
            transaction.abort()
            return ToolResult(
                output=output,
                title=transaction.redact_output(command[:80]),
                metadata={
                    "exit_code": exit_code,
                    "output_truncated": output_truncated,
                    "process_tree_reaped": True,
                    **transaction.failure_metadata,
                    **launch.metadata,
                },
                error=transaction.redact_output(
                    f"Command output could not be committed safely: {exc}",
                ),
            )

        return ToolResult(
            output=output,
            title=transaction.redact_output(command[:80]),
            metadata={
                "exit_code": exit_code,
                "output_truncated": output_truncated,
                "process_tree_reaped": True,
                **transaction.success_metadata(commit),
                **launch.metadata,
            },
        )
