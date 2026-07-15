"""Code execution tool — run Python in a terminable OS sandbox."""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path
from typing import Any

from app.tool.base import ToolDefinition, ToolResult
from app.tool.context import ToolContext
from app.tool.execution_workspace import ExecutionWorkspace
from app.tool.posix_process import PosixProcessCleanupError, run_posix_process
from app.tool.sandbox import (
    SandboxUnavailable,
    prepare_sandbox_launch,
    validate_execution_platform,
    validate_workspace_private_boundary,
)
from app.tool.workspace_transaction import WorkspaceMutationError
from app.tool.subprocess_compat import (
    IS_WINDOWS,
    decode_subprocess_output,
)
from app.tool.windows_process import WindowsProcessError, run_windows_process

DEFAULT_TIMEOUT = 30  # seconds
MAX_TIMEOUT = 120
MAX_OUTPUT = 51200  # 50 KB per output stream
_PROGRESS_INTERVAL_SECONDS = 5.0


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
            "files. macOS and Linux deny network access; every platform "
            "terminates the process together with all children on completion, "
            "timeout, or abort. Installed packages such as pandas, numpy and "
            "matplotlib are available. HOME and package/configuration caches "
            "persist only within this session, while Python interpreter state "
            "and temporary files do not persist between calls. The result "
            "metadata reports the active isolation backend."
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

        transaction = ExecutionWorkspace(
            workspace,
            ctx,
            operation="code_execute",
        )
        try:
            staged_workspace = await asyncio.to_thread(transaction.prepare)
            scratch_dir, logical_scratch = transaction.create_scratch(
                prefix=f"python-{ctx.call_id}-",
            )
            persistent_environment = transaction.create_persistent_environment()
            code_path = Path(scratch_dir) / "code.py"
            execution_code = code
            if sys.platform == "darwin" and staged_workspace != workspace:
                # macOS cannot bind the private transaction at the logical
                # workspace path. Map only the already-validated canonical
                # workspace prefix inside user code, matching argv/cwd mapping
                # in the Seatbelt launcher.
                for source in sorted(
                    {str(workspace), str(Path(ctx.workspace).expanduser().absolute())},
                    key=len,
                    reverse=True,
                ):
                    execution_code = execution_code.replace(
                        source,
                        str(staged_workspace),
                    )
            code_path.write_text(execution_code, encoding="utf-8")
            if ctx.abort_event.is_set():
                transaction.abort()
                return ToolResult(
                    error="Code execution aborted by user",
                    metadata={
                        "aborted": True,
                        **transaction.failure_metadata,
                    },
                )
            logical_code_path = logical_scratch / "code.py"
            command, read_paths = _worker_command(logical_code_path)
            launch = prepare_sandbox_launch(
                command,
                workspace=str(workspace),
                workspace_source=str(staged_workspace),
                cwd=str(workspace),
                scratch_dir=scratch_dir,
                persistent_environment=persistent_environment,
                read_only_paths=read_paths,
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
                    max_output_bytes=MAX_OUTPUT,
                )
            else:
                result = run_posix_process(
                    launch.argv,
                    cwd=launch.cwd,
                    env=launch.env,
                    timeout_seconds=timeout,
                    should_abort=should_abort,
                    max_output_bytes=MAX_OUTPUT,
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

        execution: asyncio.Task[tuple[int, bytes, bytes, str | None, bool]] | None = None
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
            exit_code, stdout_bytes, stderr_bytes, termination, output_truncated = (
                await execution
            )
        except asyncio.CancelledError:
            cancel_requested.set()
            if execution is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(execution), timeout=20)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    execution.add_done_callback(lambda _future: transaction.abort())
                else:
                    transaction.abort()
            else:
                transaction.abort()
            raise
        except (
            FileNotFoundError,
            PermissionError,
            PosixProcessCleanupError,
            WindowsProcessError,
            OSError,
        ) as exc:
            transaction.abort()
            return ToolResult(
                error=transaction.redact_output(f"Sandbox launch failed: {exc}"),
                metadata=transaction.failure_metadata,
            )

        if termination == "timeout":
            transaction.abort()
            return ToolResult(
                error=f"Execution timed out after {timeout}s",
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
                error="Code execution aborted by user",
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
        parts: list[str] = []
        if stdout:
            parts.append(stdout)
        if stderr:
            parts.append(f"STDERR:\n{stderr}")
        output = "\n".join(parts) if parts else "(no output)"
        if output_truncated:
            output += "\n[output truncated after 50 KiB per stream]"
        if exit_code != 0:
            output = f"Exit code: {exit_code}\n{output}"

        raw_title = f"Python：{code[:60]}..." if len(code) > 60 else f"Python：{code}"
        title = transaction.redact_output(raw_title)
        if exit_code != 0:
            transaction.abort()
            return ToolResult(
                output=output,
                title=title,
                metadata={
                    "exit_code": exit_code,
                    "language": "python",
                    "output_truncated": output_truncated,
                    "process_tree_reaped": True,
                    **transaction.failure_metadata,
                    **launch.metadata,
                },
                error=f"Code execution failed with exit code {exit_code}",
            )

        try:
            commit = await asyncio.to_thread(transaction.commit)
        except WorkspaceMutationError as exc:
            transaction.abort()
            return ToolResult(
                output=output,
                title=title,
                metadata={
                    "exit_code": exit_code,
                    "language": "python",
                    "output_truncated": output_truncated,
                    "process_tree_reaped": True,
                    **transaction.failure_metadata,
                    **launch.metadata,
                },
                error=transaction.redact_output(
                    f"Code output could not be committed safely: {exc}",
                ),
            )

        return ToolResult(
            output=output,
            title=title,
            metadata={
                "exit_code": exit_code,
                "language": "python",
                "output_truncated": output_truncated,
                "process_tree_reaped": True,
                **transaction.success_metadata(commit),
                **launch.metadata,
            },
        )
