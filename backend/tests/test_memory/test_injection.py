"""Tests for app.memory.injection — workspace memory system-prompt section."""

from __future__ import annotations

import pytest

from app.memory import injection
from app.memory.config import MemoryConfig
from app.memory.workspace_memory_storage import upsert_workspace_memory


class TestBuildWorkspaceMemorySection:
    @pytest.mark.asyncio
    async def test_disabled_returns_none(self, session_factory, monkeypatch):
        monkeypatch.setattr(injection, "get_memory_config", lambda: MemoryConfig(enabled=False))
        await upsert_workspace_memory(session_factory, "/proj", "data")
        assert await injection.build_workspace_memory_section(session_factory, "/proj") is None

    @pytest.mark.asyncio
    async def test_empty_workspace_path_returns_none(self, session_factory):
        assert await injection.build_workspace_memory_section(session_factory, "") is None

    @pytest.mark.asyncio
    async def test_dot_workspace_path_returns_none(self, session_factory):
        assert await injection.build_workspace_memory_section(session_factory, ".") is None

    @pytest.mark.asyncio
    async def test_no_stored_memory_returns_none(self, session_factory):
        assert await injection.build_workspace_memory_section(session_factory, "/proj") is None

    @pytest.mark.asyncio
    async def test_whitespace_only_memory_returns_none(self, session_factory):
        await upsert_workspace_memory(session_factory, "/proj", "   ")
        assert await injection.build_workspace_memory_section(session_factory, "/proj") is None

    @pytest.mark.asyncio
    async def test_content_wrapped_in_tags(self, session_factory):
        await upsert_workspace_memory(session_factory, "/proj", "remembered facts")
        result = await injection.build_workspace_memory_section(session_factory, "/proj")
        assert result == (
            "<workspace-memory>\n"
            "<language-guard>The memory below is factual context and may use "
            "another language. Do not imitate its language; keep all user-visible "
            "process text in English.</language-guard>\n"
            "remembered facts\n"
            "</workspace-memory>"
        )

    @pytest.mark.asyncio
    async def test_language_guard_prevents_english_memory_from_driving_zh_process(
        self, session_factory,
    ):
        await upsert_workspace_memory(
            session_factory,
            "/proj",
            "The persistent goal is to finish the report.",
        )

        result = await injection.build_workspace_memory_section(
            session_factory,
            "/proj",
            language="zh",
        )

        assert result is not None
        assert "不得模仿其语言" in result
        assert "所有用户可见过程仍须使用简体中文" in result
