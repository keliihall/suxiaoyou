"""Tests for OS-sandboxed Python execution."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from app.schemas.agent import AgentInfo
from app.storage.file_versions import FileVersionStore
from app.tool.builtin.code_execute import CodeExecuteTool
from app.tool.context import ToolContext
from app.tool.sandbox_self_test import _detached_probe_code


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
class TestCodeExecuteExecution:
    @pytest.fixture
    def tool(self):
        return CodeExecuteTool()

    @pytest.mark.asyncio
    async def test_simple_print_runs_in_child_sandbox(self, tool: CodeExecuteTool, tmp_path: Path):
        result = await tool.execute(
            {"code": "import os; print('hello', os.getpid())"},
            _make_ctx(tmp_path),
        )
        assert "hello" in result.output
        assert int(result.output.strip().split()[-1]) != os.getpid()
        assert result.metadata["process_tree_isolated"] is True
        assert result.metadata["filesystem_isolated"] is True
        assert result.metadata["network_isolated"] is True

    @pytest.mark.asyncio
    async def test_private_data_overlap_is_rejected_before_workspace_snapshot(
        self,
        tool: CodeExecuteTool,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace = tmp_path / "workspace"
        private_root = workspace / "app-private"
        private_root.mkdir(parents=True)
        monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private_root))

        result = await tool.execute({"code": "print('approved')"}, _make_ctx(workspace))

        assert "application-private" in (result.error or "")
        assert not (workspace / ".suxiaoyou").exists()

    @pytest.mark.asyncio
    async def test_unicode_and_data_science_imports(self, tool: CodeExecuteTool, tmp_path: Path):
        result = await tool.execute(
            {
                "code": (
                    "import numpy as np, pandas as pd\n"
                    "print('你好世界', int(pd.Series(np.arange(4)).sum()))"
                )
            },
            _make_ctx(tmp_path),
        )
        assert result.success
        assert "你好世界 6" in result.output

    @pytest.mark.asyncio
    async def test_outside_read_write_and_network_are_denied(
        self,
        tool: CodeExecuteTool,
        tmp_path: Path,
    ):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        outside_read = tmp_path / "outside-secret"
        outside_read.write_text("secret", encoding="utf-8")
        outside_write = tmp_path / "outside-write"
        code = (
            "import socket\n"
            "checks=[]\n"
            f"\ntry:\n open({str(outside_read)!r}).read()\n checks.append('read-open')"
            "\nexcept OSError:\n checks.append('read-denied')\n"
            f"\ntry:\n open({str(outside_write)!r}, 'w').write('bad')\n checks.append('write-open')"
            "\nexcept OSError:\n checks.append('write-denied')\n"
            "\ntry:\n socket.create_connection(('1.1.1.1', 80), 1)\n checks.append('net-open')"
            "\nexcept OSError:\n checks.append('net-denied')\n"
            "print(','.join(checks))\n"
        )
        result = await tool.execute({"code": code}, _make_ctx(workspace))

        assert result.success
        assert "read-denied,write-denied,net-denied" in result.output
        assert outside_read.read_text(encoding="utf-8") == "secret"
        assert not outside_write.exists()

    @pytest.mark.asyncio
    async def test_backend_environment_is_not_inherited(
        self,
        tool: CodeExecuteTool,
        tmp_path: Path,
        monkeypatch,
    ):
        monkeypatch.setenv("SUXIAOYOU_TEST_API_SECRET", "secret")
        result = await tool.execute(
            {"code": "import os; print(os.getenv('SUXIAOYOU_TEST_API_SECRET'))"},
            _make_ctx(tmp_path),
        )
        assert result.success
        assert result.output.strip() == "None"

    @pytest.mark.asyncio
    async def test_home_persists_but_physical_stage_and_scratch_are_redacted(
        self,
        tool: CodeExecuteTool,
        tmp_path: Path,
    ) -> None:
        ctx = _make_ctx(tmp_path)
        first = await tool.execute(
            {
                "code": (
                    "import os\n"
                    "from pathlib import Path\n"
                    "Path.home().joinpath('marker').write_text('kept')\n"
                    "print(Path.home())\n"
                    "print(Path.cwd())\n"
                    "print(os.environ['TMPDIR'])\n"
                )
            },
            ctx,
        )
        second = await tool.execute(
            {
                "code": (
                    "from pathlib import Path\n"
                    "print(Path.home().joinpath('marker').read_text())\n"
                    "print(Path.home())\n"
                )
            },
            ctx,
        )

        assert first.success, first.error or first.output
        assert second.success, second.error or second.output
        assert "kept" in second.output
        assert first.output.splitlines()[0] == second.output.splitlines()[-1]
        assert str(tmp_path) in first.output
        assert "execution-transactions" not in first.output
        assert "tx-" not in first.output
        assert ".suxiaoyou/sandbox/" not in first.output
        assert "<temporary-execution-directory>" in first.output

    @pytest.mark.asyncio
    async def test_failure_output_does_not_leak_deleted_physical_paths(
        self,
        tool: CodeExecuteTool,
        tmp_path: Path,
    ) -> None:
        result = await tool.execute(
            {
                "code": (
                    "from pathlib import Path\n"
                    "print(Path.cwd())\n"
                    "raise RuntimeError('expected failure')\n"
                )
            },
            _make_ctx(tmp_path),
        )

        assert not result.success
        assert "expected failure" in result.output
        assert str(tmp_path) in result.output
        assert "execution-transactions" not in result.output
        assert "tx-" not in result.output
        assert ".suxiaoyou/sandbox/" not in result.output

    @pytest.mark.asyncio
    async def test_exception_discards_staged_writes(
        self,
        tool: CodeExecuteTool,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace = tmp_path / "workspace"
        private = tmp_path / "private"
        workspace.mkdir()
        target = workspace / "report.txt"
        target.write_text("before", encoding="utf-8")
        monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
        code = (
            "from pathlib import Path\n"
            f"Path({str(target)!r}).write_text('after')\n"
            "raise RuntimeError('stop')\n"
        )

        result = await tool.execute({"code": code}, _make_ctx(workspace))

        assert not result.success
        assert result.metadata["workspace_changes_committed"] is False
        assert target.read_text(encoding="utf-8") == "before"
        assert FileVersionStore(workspace).list_versions() == []

    @pytest.mark.asyncio
    async def test_successful_write_is_versioned_then_committed(
        self,
        tool: CodeExecuteTool,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace = tmp_path / "workspace"
        private = tmp_path / "private"
        workspace.mkdir()
        target = workspace / "report.txt"
        target.write_text("before", encoding="utf-8")
        monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
        code = (
            "from pathlib import Path\n"
            f"Path({str(target)!r}).write_text('after')\n"
        )

        result = await tool.execute({"code": code}, _make_ctx(workspace))

        assert result.success
        assert result.metadata["workspace_changes_committed"] is True
        assert result.metadata["written_files"] == [str(target)]
        assert target.read_text(encoding="utf-8") == "after"
        version = FileVersionStore(workspace).list_versions(file_path=target)[0]
        assert version.operation == "code_execute"

    @pytest.mark.asyncio
    async def test_normal_completion_kills_detached_descendant(
        self,
        tool: CodeExecuteTool,
        tmp_path: Path,
    ):
        ready = tmp_path / "normal.ready"
        survived = tmp_path / "normal.survived"

        result = await tool.execute(
            {
                "code": _detached_probe_code(
                    ready,
                    survived,
                    keep_parent_alive=False,
                    workspace_root=tmp_path,
                )
            },
            _make_ctx(tmp_path),
        )

        assert result.success
        assert ready.exists()
        await asyncio.sleep(2.25)
        assert not survived.exists()

    @pytest.mark.asyncio
    async def test_timeout_kills_detached_descendant(
        self,
        tool: CodeExecuteTool,
        tmp_path: Path,
    ):
        ready = tmp_path / "timeout.ready"
        survived = tmp_path / "timeout.survived"
        result = await tool.execute(
            {
                "code": _detached_probe_code(
                    ready,
                    survived,
                    keep_parent_alive=True,
                    workspace_root=tmp_path,
                ),
                "timeout": 1,
            },
            _make_ctx(tmp_path),
        )

        assert result.metadata.get("timeout") is True
        assert not ready.exists()
        await asyncio.sleep(2.25)
        assert not survived.exists()


@pytest.mark.skipif(
    sys.platform not in {"darwin", "win32"},
    reason="native macOS/Windows execution contract",
)
@pytest.mark.asyncio
async def test_native_desktop_python_execution_is_available(tmp_path: Path):
    result = await CodeExecuteTool().execute(
        {"code": "print('native-python-ok')"},
        _make_ctx(tmp_path),
    )
    assert result.success, result.error or result.output
    assert "native-python-ok" in result.output
    expected_backend = "macos-seatbelt" if sys.platform == "darwin" else "windows-job-object"
    assert result.metadata["sandbox"] == expected_backend


@pytest.mark.asyncio
async def test_code_rejects_symlinked_scratch_before_code_file_write(
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
    result = await CodeExecuteTool().execute(
        {"code": "print('must not run')"},
        _make_ctx(workspace),
    )

    assert "symlink" in (result.error or "").lower()
    assert list(external.iterdir()) == []
