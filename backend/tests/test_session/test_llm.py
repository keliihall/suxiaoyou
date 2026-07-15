"""Tests for LLM streaming bridge."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.schemas.agent import AgentInfo
from app.schemas.provider import StreamChunk
from app.provider.openai_subscription import OpenAISubscriptionProvider
from app.provider.deferred import DeferredCredentialProvider
from app.session.llm import stream_llm
from app.tool.registry import ToolRegistry

pytestmark = pytest.mark.asyncio


def _chunk(text: str) -> StreamChunk:
    return StreamChunk(type="text-delta", data={"text": text})


def _make_provider(chunks: list[StreamChunk]):
    p = MagicMock()

    async def _stream(*args, **kwargs):
        for c in chunks:
            yield c

    p.stream_chat = MagicMock(side_effect=lambda *a, **kw: _stream(*a, **kw))
    return p


def _agent(**kw) -> AgentInfo:
    defaults = {"name": "build", "description": "Build agent", "mode": "primary", "system_prompt": "You are helpful.", "tools": ["*"]}
    defaults.update(kw)
    return AgentInfo(**defaults)


class TestStreamLlm:
    async def test_normal_stream(self):
        provider = _make_provider([_chunk("Hello"), _chunk(" world")])
        tr = MagicMock(spec=ToolRegistry)
        tr.to_openai_specs.return_value = [{"type": "function", "function": {"name": "read"}}]

        chunks = []
        async for c in stream_llm(
            provider, "m1", [{"role": "user", "content": "Hi"}],
            system_prompt="sys", agent=_agent(), tool_registry=tr,
        ):
            chunks.append(c)
        assert len(chunks) == 2
        assert chunks[0].data["text"] == "Hello"

    async def test_response_format_disables_tools(self):
        provider = _make_provider([_chunk("json")])
        tr = MagicMock(spec=ToolRegistry)
        tr.to_openai_specs.return_value = [{"type": "function"}]

        async for _ in stream_llm(
            provider, "m1", [], system_prompt="s", agent=_agent(),
            tool_registry=tr, response_format={"type": "json_schema"},
        ):
            pass
        # tools should be None when response_format is set
        call_kw = provider.stream_chat.call_args[1]
        assert call_kw["tools"] is None

    async def test_exclude_tools(self):
        provider = _make_provider([])
        tr = MagicMock(spec=ToolRegistry)
        tr.to_openai_specs.return_value = None

        async for _ in stream_llm(
            provider, "m1", [], system_prompt="s", agent=_agent(),
            tool_registry=tr, exclude_tools={"bash"},
        ):
            pass
        tr.to_openai_specs.assert_called_once()
        call_args = tr.to_openai_specs.call_args
        assert "bash" in call_args[1].get("exclude", call_args.kwargs.get("exclude", set()))

    async def test_temperature_passed(self):
        provider = _make_provider([])
        tr = MagicMock(spec=ToolRegistry)
        tr.to_openai_specs.return_value = None

        async for _ in stream_llm(
            provider, "m1", [], system_prompt="s",
            agent=_agent(temperature=0.7), tool_registry=tr,
        ):
            pass
        call_kw = provider.stream_chat.call_args[1]
        assert call_kw["temperature"] == 0.7

    async def test_subscription_native_search_security_switch_is_forwarded(self):
        provider = OpenAISubscriptionProvider("access", "account")

        async def _stream(*args, **kwargs):
            if False:
                yield _chunk("")

        provider.stream_chat = MagicMock(  # type: ignore[method-assign]
            side_effect=lambda *a, **kw: _stream(*a, **kw)
        )
        tr = MagicMock(spec=ToolRegistry)
        tr.to_openai_specs.return_value = [
            {
                "type": "function",
                "function": {
                    "name": "read",
                    "description": "Read",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

        async for _ in stream_llm(
            provider,
            "openai-subscription/gpt-5",
            [],
            system_prompt="s",
            agent=_agent(),
            tool_registry=tr,
            native_web_search_enabled=False,
        ):
            pass

        assert provider.stream_chat.call_args.kwargs["native_web_search_enabled"] is False

    async def test_deferred_subscription_preserves_native_search_switch(self):
        metadata = OpenAISubscriptionProvider("metadata", "account")
        live = OpenAISubscriptionProvider("access", "account")

        async def _stream(*args, **kwargs):
            if False:
                yield _chunk("")

        live.stream_chat = MagicMock(  # type: ignore[method-assign]
            side_effect=lambda *a, **kw: _stream(*a, **kw)
        )
        provider = DeferredCredentialProvider(
            provider_id="openai-subscription",
            metadata_provider=metadata,
            activate=lambda: live,
        )
        tr = MagicMock(spec=ToolRegistry)
        tr.to_openai_specs.return_value = None

        async for _ in stream_llm(
            provider,
            "openai-subscription/gpt-5",
            [],
            system_prompt="s",
            agent=_agent(),
            tool_registry=tr,
            native_web_search_enabled=False,
        ):
            pass

        assert live.stream_chat.call_args.kwargs["native_web_search_enabled"] is False

    async def test_subscription_request_omits_native_search_when_disabled(self):
        provider = OpenAISubscriptionProvider("access", "account")
        body = provider._build_wham_request(
            "gpt-5",
            [{"role": "user", "content": "hello"}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "read",
                        "description": "Read",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            native_web_search_enabled=False,
        )

        assert all(tool.get("type") != "web_search" for tool in body["tools"])
        assert "include" not in body

    async def test_subscription_keeps_custom_search_for_permission_ask_flow(self):
        provider = OpenAISubscriptionProvider("access", "account")
        body = provider._build_wham_request(
            "gpt-5",
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "fc_custom_search",
                        "type": "function",
                        "function": {
                            "name": "web_search",
                            "arguments": '{"query":"confirmed"}',
                        },
                    }],
                },
                {
                    "role": "tool",
                    "tool_call_id": "fc_custom_search",
                    "content": "confirmed output",
                },
            ],
            tools=[{
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "Search after permission confirmation",
                    "parameters": {"type": "object", "properties": {}},
                },
            }],
            native_web_search_enabled=False,
        )

        assert any(
            tool.get("type") == "function" and tool.get("name") == "web_search"
            for tool in body["tools"]
        )
        assert any(
            item.get("type") == "function_call"
            and item.get("call_id") == "fc_custom_search"
            for item in body["input"]
        )
        assert any(
            item.get("type") == "function_call_output"
            and item.get("call_id") == "fc_custom_search"
            for item in body["input"]
        )
