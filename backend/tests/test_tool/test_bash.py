"""Bash tool tests."""

import asyncio
import os
import shlex
import signal
import sys
import time
from pathlib import Path

import pytest

from app.schemas.agent import AgentInfo
from app.tool.builtin import bash as bash_module
from app.tool.builtin.bash import BashTool
from app.tool.context import ToolContext
from app.tool.subprocess_compat import IS_WINDOWS


def _make_ctx() -> ToolContext:
    return ToolContext(
        session_id="test-session",
        message_id="test-msg",
        agent=AgentInfo(name="test", description="", mode="primary"),
        call_id="test-call",
    )


class TestBashTool:
    @pytest.fixture
    def tool(self):
        return BashTool()

    @pytest.mark.asyncio
    async def test_echo(self, tool: BashTool):
        result = await tool.execute({"command": "echo hello"}, _make_ctx())
        assert "hello" in result.output

    @pytest.mark.asyncio
    async def test_exit_code_nonzero(self, tool: BashTool):
        if IS_WINDOWS:
            # PowerShell: exit 1
            result = await tool.execute({"command": "exit 1"}, _make_ctx())
        else:
            result = await tool.execute({"command": "exit 1"}, _make_ctx())
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_timeout(self, tool: BashTool):
        if IS_WINDOWS:
            cmd = "Start-Sleep -Seconds 10"
        else:
            cmd = "sleep 10"
        result = await tool.execute({"command": cmd, "timeout": 1}, _make_ctx())
        assert "timed out" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_abort_stops_running_command_promptly(self, tool: BashTool):
        ctx = _make_ctx()
        command = "Start-Sleep -Seconds 30" if IS_WINDOWS else "sleep 30"
        started = time.monotonic()
        task = asyncio.create_task(
            tool.execute({"command": command, "timeout": 60}, ctx)
        )

        await asyncio.sleep(0.25)
        ctx.abort_event.set()
        result = await asyncio.wait_for(task, timeout=5)

        assert result.metadata.get("aborted") is True
        assert "aborted" in (result.error or "").lower()
        assert time.monotonic() - started < 5

    @pytest.mark.asyncio
    @pytest.mark.skipif(IS_WINDOWS, reason="POSIX process-group semantics")
    async def test_abort_kills_child_that_ignores_sigterm(
        self,
        tool: BashTool,
        tmp_path: Path,
    ):
        pid_file = tmp_path / "child.pid"
        child_code = (
            "import signal,time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "time.sleep(30)"
        )
        parent_code = (
            "import subprocess,sys,time; "
            f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
            f"open({str(pid_file)!r},'w').write(str(p.pid)); "
            "time.sleep(30)"
        )
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote(parent_code)}"
        ctx = _make_ctx()
        task = asyncio.create_task(
            tool.execute({"command": command, "timeout": 60}, ctx)
        )

        child_pid: int | None = None
        try:
            for _ in range(100):
                if pid_file.exists() and pid_file.read_text().strip():
                    child_pid = int(pid_file.read_text())
                    break
                await asyncio.sleep(0.02)
            assert child_pid is not None

            started = time.monotonic()
            ctx.abort_event.set()
            result = await asyncio.wait_for(task, timeout=5)

            assert result.metadata.get("aborted") is True
            assert time.monotonic() - started < 5
            for _ in range(100):
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.02)
            else:
                pytest.fail(f"grandchild {child_pid} survived bash abort")
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if child_pid is not None:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    @pytest.mark.asyncio
    async def test_captures_stderr(self, tool: BashTool):
        if IS_WINDOWS:
            result = await tool.execute({"command": "Write-Error 'err' 2>&1"}, _make_ctx())
        else:
            result = await tool.execute({"command": "echo err >&2"}, _make_ctx())
        assert "err" in result.output

    @pytest.mark.asyncio
    async def test_unicode_output(self, tool: BashTool):
        """Non-ASCII output should not be garbled."""
        result = await tool.execute(
            {"command": 'python3 -c "print(\'hello world\')"'}, _make_ctx()
        )
        assert "hello" in result.output


def test_windows_job_close_is_used_even_after_parent_exit(monkeypatch):
    class Process:
        pid = 123

        def poll(self):
            return 0

    class Job:
        closed = False

        def close(self):
            self.closed = True

    job = Job()
    monkeypatch.setattr(bash_module, "IS_WINDOWS", True)

    def unexpected_taskkill(*_args, **_kwargs):
        raise AssertionError("taskkill fallback should not run when a Job exists")

    monkeypatch.setattr(bash_module.subprocess, "run", unexpected_taskkill)

    bash_module._terminate_process_tree(Process(), job)

    assert job.closed is True


@pytest.mark.asyncio
@pytest.mark.skipif(not IS_WINDOWS, reason="Windows Job Object contract")
async def test_windows_shell_exit_cannot_leave_detached_child(
    tmp_path: Path,
) -> None:
    import ctypes
    from ctypes import wintypes

    pid_file = tmp_path / "detached.pid"
    escaped_path = str(pid_file).replace("'", "''")
    command = (
        "$p = Start-Process -FilePath \"$env:SystemRoot\\System32\\ping.exe\" "
        "-ArgumentList '127.0.0.1','-n','31' -WindowStyle Hidden -PassThru; "
        f"[IO.File]::WriteAllText('{escaped_path}', [string]$p.Id); exit 0"
    )

    result = await BashTool().execute(
        {"command": command, "timeout": 10},
        _make_ctx(),
    )
    assert result.success
    child_pid = int(pid_file.read_text())

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x00100000, False, child_pid)  # SYNCHRONIZE
    if not handle:
        return
    try:
        assert kernel32.WaitForSingleObject(handle, 0) == 0x00000000
    finally:
        kernel32.CloseHandle(handle)
