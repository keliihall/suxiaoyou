"""Bash tool tests."""

import asyncio
import shlex
import sys
import time
from pathlib import Path

import pytest

from app.schemas.agent import AgentInfo
from app.tool.builtin import bash as bash_module
from app.tool.builtin.bash import BashTool
from app.tool.context import ToolContext
from app.tool.sandbox_self_test import _detached_probe_code
from app.tool.subprocess_compat import IS_WINDOWS


def _make_ctx(workspace: Path) -> ToolContext:
    return ToolContext(
        session_id="test-session",
        message_id="test-msg",
        agent=AgentInfo(name="test", description="", mode="primary"),
        call_id="test-call",
        workspace=str(workspace),
    )


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="v0.9 command execution is enabled only under Linux bubblewrap",
)
class TestBashTool:
    @pytest.fixture
    def tool(self):
        return BashTool()

    @pytest.mark.asyncio
    async def test_echo(self, tool: BashTool, tmp_path: Path):
        result = await tool.execute({"command": "echo hello"}, _make_ctx(tmp_path))
        assert "hello" in result.output
        assert result.metadata["filesystem_isolated"] is True
        assert result.metadata["network_isolated"] is True

    @pytest.mark.asyncio
    async def test_private_data_overlap_is_rejected_before_scratch_creation(
        self,
        tool: BashTool,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace = tmp_path / "workspace"
        private_root = workspace / "app-private"
        private_root.mkdir(parents=True)
        monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private_root))

        result = await tool.execute({"command": "echo approved"}, _make_ctx(workspace))

        assert "application-private" in (result.error or "")
        assert not (workspace / ".suxiaoyou").exists()
        assert not (workspace / "suxiaoyou_written").exists()

    @pytest.mark.asyncio
    async def test_exit_code_nonzero(self, tool: BashTool, tmp_path: Path):
        if IS_WINDOWS:
            # PowerShell: exit 1
            result = await tool.execute({"command": "exit 1"}, _make_ctx(tmp_path))
        else:
            result = await tool.execute({"command": "exit 1"}, _make_ctx(tmp_path))
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_timeout(self, tool: BashTool, tmp_path: Path):
        ready = tmp_path / "timeout.ready"
        survived = tmp_path / "timeout.survived"
        code = _detached_probe_code(ready, survived, keep_parent_alive=True)
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"

        result = await tool.execute(
            {"command": command, "timeout": 1},
            _make_ctx(tmp_path),
        )

        assert "timed out" in (result.error or "").lower()
        assert ready.exists()
        await asyncio.sleep(2.25)
        assert not survived.exists()

    @pytest.mark.asyncio
    async def test_normal_completion_kills_detached_descendant(
        self,
        tool: BashTool,
        tmp_path: Path,
    ):
        ready = tmp_path / "normal.ready"
        survived = tmp_path / "normal.survived"
        code = _detached_probe_code(ready, survived, keep_parent_alive=False)
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"

        result = await tool.execute({"command": command}, _make_ctx(tmp_path))

        assert result.success
        assert ready.exists()
        await asyncio.sleep(2.25)
        assert not survived.exists()

    @pytest.mark.asyncio
    async def test_abort_stops_running_command_promptly(self, tool: BashTool, tmp_path: Path):
        ctx = _make_ctx(tmp_path)
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
    async def test_abort_kills_detached_descendant(
        self,
        tool: BashTool,
        tmp_path: Path,
    ):
        ready = tmp_path / "abort.ready"
        survived = tmp_path / "abort.survived"
        code = _detached_probe_code(ready, survived, keep_parent_alive=True)
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
        ctx = _make_ctx(tmp_path)
        task = asyncio.create_task(
            tool.execute({"command": command, "timeout": 60}, ctx)
        )

        try:
            for _ in range(100):
                if ready.exists():
                    break
                await asyncio.sleep(0.02)
            assert ready.exists()

            started = time.monotonic()
            ctx.abort_event.set()
            result = await asyncio.wait_for(task, timeout=5)

            assert result.metadata.get("aborted") is True
            assert time.monotonic() - started < 5
            await asyncio.sleep(2.25)
            assert not survived.exists()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_captures_stderr(self, tool: BashTool, tmp_path: Path):
        if IS_WINDOWS:
            result = await tool.execute({"command": "Write-Error 'err' 2>&1"}, _make_ctx(tmp_path))
        else:
            result = await tool.execute({"command": "echo err >&2"}, _make_ctx(tmp_path))
        assert "err" in result.output

    @pytest.mark.asyncio
    async def test_unicode_output(self, tool: BashTool, tmp_path: Path):
        """Non-ASCII output should not be garbled."""
        result = await tool.execute(
            {"command": '/usr/bin/python3 -c "print(\'hello world\')"'}, _make_ctx(tmp_path)
        )
        assert "hello" in result.output

    @pytest.mark.asyncio
    async def test_outside_read_write_and_environment_are_denied(
        self,
        tool: BashTool,
        tmp_path: Path,
        monkeypatch,
    ):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        outside = tmp_path / "outside"
        outside.write_text("secret", encoding="utf-8")
        escaped = tmp_path / "escaped"
        monkeypatch.setenv("SUXIAOYOU_TEST_API_SECRET", "secret")
        command = (
            f"cat {shlex.quote(str(outside))}; read_rc=$?; "
            f"printf bad > {shlex.quote(str(escaped))}; write_rc=$?; "
            'test -z "$SUXIAOYOU_TEST_API_SECRET"; env_rc=$?; '
            'printf "read=%s write=%s env=%s" "$read_rc" "$write_rc" "$env_rc"'
        )
        result = await tool.execute({"command": command}, _make_ctx(workspace))

        assert "read=1 write=1 env=0" in result.output
        assert outside.read_text(encoding="utf-8") == "secret"
        assert not escaped.exists()

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
@pytest.mark.skip(reason="Windows command execution is disabled pending AppContainer")
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
        _make_ctx(tmp_path),
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


@pytest.mark.skipif(
    sys.platform not in {"darwin", "win32"},
    reason="macOS/Windows fail-closed contract",
)
@pytest.mark.asyncio
async def test_unsupported_platform_bash_execution_is_disabled(tmp_path: Path) -> None:
    result = await BashTool().execute(
        {"command": "echo must-not-run"},
        _make_ctx(tmp_path),
    )
    expected = "macOS" if sys.platform == "darwin" else "Windows"
    assert f"disabled on {expected}" in (result.error or "")
    assert not (tmp_path / ".suxiaoyou").exists()


@pytest.mark.asyncio
async def test_bash_rejects_symlinked_scratch_before_external_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    (workspace / ".suxiaoyou").mkdir(parents=True)
    external.mkdir()
    (workspace / ".suxiaoyou" / "sandbox").symlink_to(
        external,
        target_is_directory=True,
    )
    monkeypatch.setattr("app.tool.sandbox.sys.platform", "linux")

    result = await BashTool().execute(
        {"command": "echo must-not-run"},
        _make_ctx(workspace),
    )

    assert "symlink" in (result.error or "").lower()
    assert list(external.iterdir()) == []
