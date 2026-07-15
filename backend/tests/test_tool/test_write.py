"""Tests for app.tool.builtin.write — file write tool."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.schemas.agent import AgentInfo
from app.tool.builtin.write import WriteTool
from app.tool.context import ToolContext
from app.tool import workspace_transaction as transaction_module


def _make_ctx(workspace: str | None = None) -> ToolContext:
    return ToolContext(
        session_id="test-session",
        message_id="test-msg",
        agent=AgentInfo(name="test", description="", mode="primary"),
        call_id="test-call",
        workspace=workspace,
    )


class TestWriteTool:
    @pytest.fixture
    def tool(self):
        return WriteTool()

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        transaction_module.guarded_file_mutation_unavailable_reason() is not None,
        reason="guarded mutation primitive unavailable",
    )
    async def test_workspace_write_does_not_stage_unrelated_large_or_special_files(
        self,
        tool: WriteTool,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        workspace = tmp_path / "workspace"
        private = tmp_path / "private"
        workspace.mkdir()
        monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
        oversized = workspace / "unrelated-large.bin"
        oversized.touch()
        os.truncate(
            oversized,
            transaction_module.MAX_STAGED_WORKSPACE_BYTES + 1,
        )
        fifo = workspace / "unrelated-pipe"
        if hasattr(os, "mkfifo"):
            os.mkfifo(fifo)

        result = await tool.execute(
            {"file_path": "result.txt", "content": "targeted"},
            _make_ctx(workspace=str(workspace)),
        )

        assert result.success, result.error
        assert (
            workspace / "suxiaoyou_written" / "result.txt"
        ).read_text(encoding="utf-8") == "targeted"
        assert oversized.stat().st_size == transaction_module.MAX_STAGED_WORKSPACE_BYTES + 1
        if hasattr(os, "mkfifo"):
            assert fifo.exists()

    @pytest.mark.asyncio
    async def test_create_new_file(self, tool: WriteTool, tmp_path: Path):
        f = tmp_path / "new.txt"
        result = await tool.execute(
            {"file_path": str(f), "content": "hello\nworld\n"},
            _make_ctx(workspace=str(tmp_path)),
        )
        assert result.success
        assert f.read_text() == "hello\nworld\n"
        assert "已创建" in result.output
        assert result.title == "已创建 new.txt"

    @pytest.mark.asyncio
    async def test_overwrite_existing(self, tool: WriteTool, tmp_path: Path):
        f = tmp_path / "existing.txt"
        f.write_text("old content")
        result = await tool.execute(
            {"file_path": str(f), "content": "new content"},
            _make_ctx(workspace=str(tmp_path)),
        )
        assert result.success
        assert f.read_text() == "new content"
        assert "已更新" in result.output
        assert result.title == "已更新 existing.txt"

    @pytest.mark.asyncio
    async def test_creates_parent_dirs(self, tool: WriteTool, tmp_path: Path):
        f = tmp_path / "a" / "b" / "c" / "file.txt"
        result = await tool.execute(
            {"file_path": str(f), "content": "deep"},
            _make_ctx(workspace=str(tmp_path)),
        )
        assert result.success
        assert f.read_text() == "deep"

    @pytest.mark.asyncio
    async def test_line_count_in_output(self, tool: WriteTool, tmp_path: Path):
        f = tmp_path / "lines.txt"
        result = await tool.execute(
            {"file_path": str(f), "content": "a\nb\nc\n"},
            _make_ctx(workspace=str(tmp_path)),
        )
        assert result.success
        assert "3" in result.output  # 3 lines

    @pytest.mark.asyncio
    async def test_missing_workspace_fails_before_writing(
        self,
        tool: WriteTool,
        tmp_path: Path,
    ):
        target = tmp_path / "must-not-exist.txt"

        result = await tool.execute(
            {"file_path": str(target), "content": "unsafe"},
            _make_ctx(),
        )

        assert not result.success
        assert "工作区" in result.error
        assert not target.exists()

    @pytest.mark.asyncio
    async def test_workspace_violation(self, tool: WriteTool, tmp_path: Path):
        result = await tool.execute(
            {"file_path": "/etc/should-not-write", "content": "bad"},
            _make_ctx(workspace=str(tmp_path)),
        )
        assert not result.success

    @pytest.mark.asyncio
    async def test_relative_path_suxiaoyou_written(self, tool: WriteTool, tmp_path: Path):
        result = await tool.execute(
            {"file_path": "output.txt", "content": "relative"},
            _make_ctx(workspace=str(tmp_path)),
        )
        assert result.success
        expected = tmp_path / "suxiaoyou_written" / "output.txt"
        assert expected.read_text() == "relative"
