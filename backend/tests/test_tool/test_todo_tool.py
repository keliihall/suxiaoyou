"""Tests for app.tool.builtin.todo — TodoTool._build_result()."""

from __future__ import annotations

from app.tool.builtin.todo import TodoTool


class TestBuildResult:
    def test_summary_counts(self):
        todos = [
            {"content": "A", "status": "completed"},
            {"content": "B", "status": "in_progress"},
            {"content": "C", "status": "pending"},
        ]
        result = TodoTool._build_result(todos)
        assert "已完成 1/3" in result.output
        assert "1 个进行中" in result.output
        assert "1 个待处理" in result.output
        assert result.title == "待办清单"

    def test_all_completed(self):
        todos = [
            {"content": "A", "status": "completed"},
            {"content": "B", "status": "completed"},
        ]
        result = TodoTool._build_result(todos)
        assert "已完成 2/2" in result.output
        assert "待处理" not in result.output

    def test_empty_list(self):
        result = TodoTool._build_result([])
        assert "已完成 0/0" in result.output
