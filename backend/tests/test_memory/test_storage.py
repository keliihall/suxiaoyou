"""Tests for app.memory.workspace_memory_storage — workspace memory CRUD."""

from __future__ import annotations

import pytest

from app.memory.workspace_memory_storage import (
    MAX_WORKSPACE_MEMORY_LINES,
    _enforce_line_cap,
    _normalize_path,
    delete_workspace_memory,
    get_workspace_memory,
    get_workspace_memory_with_timestamp,
    list_workspace_memories,
    upsert_workspace_memory,
)


class TestNormalizePath:
    def test_backslashes_converted_to_forward_slashes(self):
        assert _normalize_path("C:\\Users\\me\\proj") == "C:/Users/me/proj"

    def test_trailing_slash_stripped(self):
        assert _normalize_path("/home/me/proj/") == "/home/me/proj"

    def test_single_dot_collapsed(self):
        # PurePath collapses "." segments (but does not resolve "..").
        assert _normalize_path("/home/me/./proj") == "/home/me/proj"

    def test_dotdot_collapsed_lexically(self):
        assert _normalize_path("/home/me/proj/../proj") == "/home/me/proj"

    def test_posix_root_preserved(self):
        assert _normalize_path("/") == "/"

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("C:/", "C:/"),
            ("C:/.", "C:/"),
            ("C:/foo/..", "C:/"),
            ("C:/foo/../..", "C:/"),
            ("C:/../data", "C:/data"),
        ],
    )
    def test_drive_root_preserved(self, path, expected):
        assert _normalize_path(path) == expected

    @pytest.mark.parametrize(
        "path",
        [
            "//server/share",
            "//server/share/",
            "//server/share/.",
            "//server/share/folder/..",
            "//server/share/..",
            "//server/share/folder/../..",
        ],
    )
    def test_unc_share_root_has_one_canonical_form(self, path):
        assert _normalize_path(path) == "//server/share"

    @pytest.mark.parametrize(
        "path",
        [
            "/home/me/proj",
            "///home/me/proj",
            "////home/me/proj",
        ],
    )
    def test_posix_absolute_leading_slashes_are_collapsed(self, path):
        assert _normalize_path(path) == "/home/me/proj"

    def test_relative_posix_colon_component_is_not_a_drive(self):
        assert _normalize_path("a:b/../c") == "c"

    def test_mixed_separators_normalized_consistently(self):
        a = _normalize_path("/home/me/proj")
        b = _normalize_path("\\home\\me\\proj\\")
        assert a == b == "/home/me/proj"

    def test_idempotent(self):
        once = _normalize_path("/home/me/proj/")
        assert _normalize_path(once) == once


class TestEnforceLineCap:
    def test_short_content_unchanged(self):
        content = "line1\nline2\nline3"
        assert _enforce_line_cap(content) == content

    def test_truncates_to_max_lines(self):
        content = "\n".join(str(i) for i in range(MAX_WORKSPACE_MEMORY_LINES + 50))
        result = _enforce_line_cap(content)
        assert len(result.split("\n")) == MAX_WORKSPACE_MEMORY_LINES

    def test_respects_custom_max_lines(self):
        content = "\n".join(str(i) for i in range(10))
        result = _enforce_line_cap(content, max_lines=3)
        assert result.split("\n") == ["0", "1", "2"]

    def test_exactly_at_cap_unchanged(self):
        content = "\n".join(str(i) for i in range(MAX_WORKSPACE_MEMORY_LINES))
        assert _enforce_line_cap(content) == content


class TestWorkspaceMemoryCrud:
    @pytest.mark.asyncio
    async def test_get_missing_returns_none(self, session_factory):
        assert await get_workspace_memory(session_factory, "/nope") is None

    @pytest.mark.asyncio
    async def test_upsert_then_get(self, session_factory):
        await upsert_workspace_memory(session_factory, "/proj", "hello world")
        assert await get_workspace_memory(session_factory, "/proj") == "hello world"

    @pytest.mark.asyncio
    async def test_upsert_strips_and_caps(self, session_factory):
        content = "  " + "\n".join(str(i) for i in range(MAX_WORKSPACE_MEMORY_LINES + 10)) + "  "
        await upsert_workspace_memory(session_factory, "/proj", content)
        stored = await get_workspace_memory(session_factory, "/proj")
        assert stored is not None
        assert stored.startswith("0")
        assert len(stored.split("\n")) == MAX_WORKSPACE_MEMORY_LINES

    @pytest.mark.asyncio
    async def test_upsert_updates_existing_row(self, session_factory):
        await upsert_workspace_memory(session_factory, "/proj", "first")
        await upsert_workspace_memory(session_factory, "/proj", "second")
        assert await get_workspace_memory(session_factory, "/proj") == "second"
        # Only one row should exist for the workspace
        rows = await list_workspace_memories(session_factory)
        assert len([r for r in rows if r["workspace_path"] == "/proj"]) == 1

    @pytest.mark.asyncio
    async def test_path_normalization_maps_variants_to_same_row(self, session_factory):
        await upsert_workspace_memory(session_factory, "/home/me/proj", "v1")
        # A differently-spelled path for the same directory updates the same row
        await upsert_workspace_memory(session_factory, "\\home\\me\\proj\\", "v2")
        assert await get_workspace_memory(session_factory, "/home/me/proj") == "v2"
        rows = await list_workspace_memories(session_factory)
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_get_with_timestamp_missing(self, session_factory):
        content, ts = await get_workspace_memory_with_timestamp(session_factory, "/nope")
        assert content is None
        assert ts is None

    @pytest.mark.asyncio
    async def test_get_with_timestamp_present(self, session_factory):
        await upsert_workspace_memory(session_factory, "/proj", "data")
        content, ts = await get_workspace_memory_with_timestamp(session_factory, "/proj")
        assert content == "data"
        assert isinstance(ts, str) and ts

    @pytest.mark.asyncio
    async def test_list_empty(self, session_factory):
        assert await list_workspace_memories(session_factory) == []

    @pytest.mark.asyncio
    async def test_list_returns_metadata(self, session_factory):
        await upsert_workspace_memory(session_factory, "/proj", "a\nb\nc")
        rows = await list_workspace_memories(session_factory)
        assert len(rows) == 1
        row = rows[0]
        assert row["workspace_path"] == "/proj"
        assert row["content"] == "a\nb\nc"
        assert row["line_count"] == 3
        assert row["time_updated"]

    @pytest.mark.asyncio
    async def test_delete_existing(self, session_factory):
        await upsert_workspace_memory(session_factory, "/proj", "data")
        assert await delete_workspace_memory(session_factory, "/proj") is True
        assert await get_workspace_memory(session_factory, "/proj") is None

    @pytest.mark.asyncio
    async def test_delete_missing_returns_false(self, session_factory):
        assert await delete_workspace_memory(session_factory, "/nope") is False

    @pytest.mark.asyncio
    async def test_delete_uses_normalized_path(self, session_factory):
        await upsert_workspace_memory(session_factory, "/home/me/proj", "data")
        assert await delete_workspace_memory(session_factory, "\\home\\me\\proj\\") is True
        assert await get_workspace_memory(session_factory, "/home/me/proj") is None
