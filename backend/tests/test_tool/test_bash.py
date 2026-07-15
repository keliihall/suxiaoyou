"""Bash tool tests."""

import asyncio
import shlex
import sys
import time
from pathlib import Path

import pytest

from app.schemas.agent import AgentInfo
from app.storage.file_versions import FileVersionStore
from app.tool.builtin.bash import BashTool
from app.tool.context import ToolContext
from app.tool.sandbox_self_test import _detached_probe_code
from app.tool.subprocess_compat import IS_WINDOWS
from app.tool.workspace import get_default_output_dir


def _make_ctx(workspace: Path) -> ToolContext:
    return ToolContext(
        session_id="test-session",
        message_id="test-msg",
        agent=AgentInfo(name="test", description="", mode="primary"),
        call_id="test-call",
        workspace=str(workspace),
    )


@pytest.mark.skipif(
    sys.platform not in {"linux", "darwin"},
    reason="POSIX sandbox execution contract",
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
        assert result.metadata["network_isolated"] is False

    @pytest.mark.asyncio
    async def test_pipeline_propagates_nonzero_producer_status(
        self,
        tool: BashTool,
        tmp_path: Path,
    ) -> None:
        result = await tool.execute(
            {"command": "(exit 17) | cat"},
            _make_ctx(tmp_path),
        )

        assert result.success is False
        assert result.metadata["exit_code"] == 17

    @pytest.mark.asyncio
    async def test_home_and_user_cli_path_persist_across_calls(
        self,
        tool: BashTool,
        tmp_path: Path,
    ) -> None:
        ctx = _make_ctx(tmp_path)
        first = await tool.execute(
            {
                "command": (
                    "mkdir -p \"$HOME/.local/bin\"; "
                    "printf '#!/bin/sh\\nprintf session-home-ok\\n' "
                    "> \"$HOME/.local/bin/session-home-probe\"; "
                    "chmod +x \"$HOME/.local/bin/session-home-probe\"; "
                    "printf '%s' \"$HOME\""
                )
            },
            ctx,
        )
        second = await tool.execute(
            {"command": "session-home-probe; printf '\\n%s' \"$HOME\""},
            ctx,
        )

        assert first.success, first.error or first.output
        assert second.success, second.error or second.output
        assert "session-home-ok" in second.output
        assert first.output.strip() == second.output.splitlines()[-1]
        assert second.metadata["execution_environment_scope"] == "session"
        assert second.metadata["home_persistent"] is True

    @pytest.mark.asyncio
    async def test_output_maps_stage_to_logical_workspace_and_hides_scratch(
        self,
        tool: BashTool,
        tmp_path: Path,
    ) -> None:
        result = await tool.execute(
            {"command": "printf '%s\\n%s' \"$PWD\" \"$TMPDIR\""},
            _make_ctx(tmp_path),
        )

        assert result.success, result.error or result.output
        assert str(Path(get_default_output_dir(str(tmp_path)))) in result.output
        assert "execution-transactions" not in result.output
        assert "tx-" not in result.output
        assert ".suxiaoyou/sandbox/" not in result.output
        assert "<temporary-execution-directory>" in result.output

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
    async def test_nonzero_exit_discards_staged_workspace_writes(
        self,
        tool: BashTool,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace = tmp_path / "workspace"
        private = tmp_path / "private"
        workspace.mkdir()
        target = workspace / "report.txt"
        target.write_text("before", encoding="utf-8")
        monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
        command = f"printf after > {shlex.quote(str(target))}; exit 7"

        result = await tool.execute({"command": command}, _make_ctx(workspace))

        assert result.error == "Command failed with exit code 7"
        assert result.metadata["workspace_changes_committed"] is False
        assert target.read_text(encoding="utf-8") == "before"
        assert FileVersionStore(workspace).list_versions() == []

    @pytest.mark.asyncio
    async def test_successful_write_is_versioned_then_committed(
        self,
        tool: BashTool,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace = tmp_path / "workspace"
        private = tmp_path / "private"
        workspace.mkdir()
        target = workspace / "report.txt"
        target.write_text("before", encoding="utf-8")
        monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
        command = f"printf after > {shlex.quote(str(target))}"

        result = await tool.execute({"command": command}, _make_ctx(workspace))

        assert result.success
        assert result.metadata["workspace_changes_committed"] is True
        assert result.metadata["written_files"] == [str(target)]
        assert target.read_text(encoding="utf-8") == "after"
        version = FileVersionStore(workspace).list_versions(file_path=target)[0]
        assert version.operation == "bash"

    @pytest.mark.asyncio
    async def test_timeout(self, tool: BashTool, tmp_path: Path):
        ready = tmp_path / "timeout.ready"
        survived = tmp_path / "timeout.survived"
        code = _detached_probe_code(
            ready,
            survived,
            keep_parent_alive=True,
            workspace_root=tmp_path,
        )
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"

        result = await tool.execute(
            {"command": command, "timeout": 1},
            _make_ctx(tmp_path),
        )

        assert "timed out" in (result.error or "").lower()
        # Timed-out commands ran only in the private transaction view; none of
        # their partial files become visible in the selected workspace.
        assert not ready.exists()
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
        code = _detached_probe_code(
            ready,
            survived,
            keep_parent_alive=False,
            workspace_root=tmp_path,
        )
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
        code = _detached_probe_code(
            ready,
            survived,
            keep_parent_alive=True,
            workspace_root=tmp_path,
        )
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
        ctx = _make_ctx(tmp_path)
        task = asyncio.create_task(
            tool.execute({"command": command, "timeout": 60}, ctx)
        )

        try:
            # Staged writes are intentionally invisible while the command is
            # running, so abort after the child has had time to create them in
            # its private view instead of polling the real workspace.
            await asyncio.sleep(0.25)

            started = time.monotonic()
            ctx.abort_event.set()
            result = await asyncio.wait_for(task, timeout=5)

            assert result.metadata.get("aborted") is True
            assert time.monotonic() - started < 5
            assert not ready.exists()
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
            {
                "command": (
                    f"{shlex.quote(sys.executable)} -c \"print('hello world')\""
                )
            },
            _make_ctx(tmp_path),
        )
        assert "hello" in result.output

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS xcode-select runtime")
    @pytest.mark.asyncio
    async def test_macos_system_python3_runtime_is_readable(
        self,
        tool: BashTool,
        tmp_path: Path,
    ) -> None:
        if not Path("/usr/bin/python3").exists():
            pytest.skip("/usr/bin/python3 is unavailable")

        result = await tool.execute(
            {"command": "xcrun_verbose=1 /usr/bin/python3 --version"},
            _make_ctx(tmp_path),
        )

        assert result.success, result.error or result.output
        assert "Python 3" in result.output
        assert "couldn't create cache file" not in result.output
        assert "couldn't replace cache file" not in result.output

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

@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "win32", reason="native Windows Job integration")
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
    reason="native macOS/Windows execution contract",
)
@pytest.mark.asyncio
async def test_native_desktop_bash_execution_is_available(tmp_path: Path) -> None:
    result = await BashTool().execute(
        {"command": "echo native-shell-ok"},
        _make_ctx(tmp_path),
    )
    assert result.success, result.error or result.output
    assert "native-shell-ok" in result.output
    expected_backend = "macos-seatbelt" if sys.platform == "darwin" else "windows-job-object"
    assert result.metadata["sandbox"] == expected_backend


@pytest.mark.asyncio
async def test_bash_rejects_symlinked_scratch_before_external_write(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    (workspace / ".suxiaoyou").mkdir(parents=True)
    external.mkdir()
    (workspace / ".suxiaoyou" / "sandbox").symlink_to(
        external,
        target_is_directory=True,
    )
    result = await BashTool().execute(
        {"command": "echo must-not-run"},
        _make_ctx(workspace),
    )

    assert "symlink" in (result.error or "").lower()
    assert list(external.iterdir()) == []
