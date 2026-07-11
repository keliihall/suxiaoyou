"""Tests for app.memory.workspace_memory_updater — formatting & parsing helpers."""

from __future__ import annotations

from app.memory.workspace_memory_storage import MAX_WORKSPACE_MEMORY_LINES
from app.memory.workspace_memory_updater import (
    WORKSPACE_MEMORY_UPDATE_PROMPT,
    _extract_text_content,
    format_conversation_for_workspace_update,
    parse_workspace_memory_response,
)


class TestExtractTextContent:
    def test_string_content_stripped(self):
        assert _extract_text_content("  hello  ") == "hello"

    def test_multimodal_text_parts_joined(self):
        content = [
            {"type": "text", "text": "part one"},
            {"type": "image", "url": "http://x"},
            {"type": "text", "text": "part two"},
        ]
        assert _extract_text_content(content) == "part one\npart two"

    def test_multimodal_missing_text_key(self):
        assert _extract_text_content([{"type": "text"}]) == ""

    def test_non_text_only_list_returns_empty(self):
        assert _extract_text_content([{"type": "image", "url": "x"}]) == ""

    def test_unsupported_type_returns_empty(self):
        assert _extract_text_content(None) == ""
        assert _extract_text_content(42) == ""


class TestFormatConversation:
    def test_user_and_assistant_included(self):
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        result = format_conversation_for_workspace_update(messages)
        assert result == "User: hi\n\nAssistant: hello"

    def test_empty_messages_skipped(self):
        messages = [
            {"role": "user", "content": ""},
            {"role": "assistant", "content": "  "},
            {"role": "user", "content": "real"},
        ]
        assert format_conversation_for_workspace_update(messages) == "User: real"

    def test_non_user_assistant_roles_ignored(self):
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "tool", "content": "tool output"},
        ]
        assert format_conversation_for_workspace_update(messages) == ""

    def test_long_text_truncated_to_2000_chars(self):
        messages = [{"role": "user", "content": "x" * 5000}]
        result = format_conversation_for_workspace_update(messages)
        # "User: " prefix + 2000 chars
        assert result == "User: " + "x" * 2000

    def test_multimodal_content_extracted(self):
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "multi"}]},
        ]
        assert format_conversation_for_workspace_update(messages) == "User: multi"

    def test_empty_list_returns_empty_string(self):
        assert format_conversation_for_workspace_update([]) == ""


class TestParseResponse:
    def test_plain_text_stripped(self):
        assert parse_workspace_memory_response("  content here  ") == "content here"

    def test_strips_code_fence_with_language(self):
        text = "```markdown\nline1\nline2\n```"
        assert parse_workspace_memory_response(text) == "line1\nline2"

    def test_strips_bare_code_fence(self):
        text = "```\nhello\n```"
        assert parse_workspace_memory_response(text) == "hello"

    def test_opening_fence_without_closing(self):
        text = "```\nhello\nworld"
        assert parse_workspace_memory_response(text) == "hello\nworld"

    def test_no_fence_preserved(self):
        assert parse_workspace_memory_response("no fence here") == "no fence here"

    def test_enforces_line_cap(self):
        text = "\n".join(str(i) for i in range(MAX_WORKSPACE_MEMORY_LINES + 20))
        result = parse_workspace_memory_response(text)
        assert len(result.split("\n")) == MAX_WORKSPACE_MEMORY_LINES

    def test_fence_stripped_then_line_capped(self):
        body = "\n".join(str(i) for i in range(MAX_WORKSPACE_MEMORY_LINES + 20))
        text = f"```\n{body}\n```"
        result = parse_workspace_memory_response(text)
        assert len(result.split("\n")) == MAX_WORKSPACE_MEMORY_LINES
        assert result.startswith("0")


class TestPromptTemplate:
    def test_prompt_has_expected_placeholders(self):
        formatted = WORKSPACE_MEMORY_UPDATE_PROMPT.format(
            max_lines=200,
            current_memory="prev",
            conversation="convo",
        )
        assert "prev" in formatted
        assert "convo" in formatted
        assert "200 lines maximum" in formatted
