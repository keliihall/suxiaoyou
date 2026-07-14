"""Tests for OS-sandboxed Python execution."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from app.schemas.agent import AgentInfo
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
    sys.platform != "linux",
    reason="v0.9 Python execution is enabled only under Linux bubblewrap",
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
                ),
                "timeout": 1,
            },
            _make_ctx(tmp_path),
        )

        assert result.metadata.get("timeout") is True
        assert ready.exists()
        await asyncio.sleep(2.25)
        assert not survived.exists()


@pytest.mark.skipif(
    sys.platform not in {"darwin", "win32"},
    reason="macOS/Windows fail-closed contract",
)
@pytest.mark.asyncio
async def test_unsupported_platform_execution_is_disabled(tmp_path: Path):
    result = await CodeExecuteTool().execute(
        {"code": "print('must not run')"},
        _make_ctx(tmp_path),
    )
    expected = "macOS" if sys.platform == "darwin" else "Windows"
    assert f"disabled on {expected}" in (result.error or "")
    assert not (tmp_path / ".suxiaoyou").exists()


@pytest.mark.asyncio
async def test_code_rejects_symlinked_scratch_before_code_file_write(
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

    result = await CodeExecuteTool().execute(
        {"code": "print('must not run')"},
        _make_ctx(workspace),
    )

    assert "symlink" in (result.error or "").lower()
    assert list(external.iterdir()) == []
