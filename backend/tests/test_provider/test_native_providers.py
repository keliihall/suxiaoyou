from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from google.genai import types as google_types

from app.provider.anthropic_provider import (
    AnthropicDesktopProvider,
    _anthropic_content,
    _anthropic_tools,
    _build_messages as build_anthropic_messages,
)
from app.provider.gemini_provider import (
    GeminiDesktopProvider,
    _build_contents as build_gemini_contents,
    _gemini_tools,
)
from app.provider.models_dev import models_dev


def test_anthropic_converts_openai_tool_history_and_specs() -> None:
    messages, system = build_anthropic_messages(
        [
            {"role": "system", "content": "embedded"},
            {
                "role": "assistant",
                "content": "checking",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {"name": "read", "arguments": '{"path":"a.txt"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "hello"},
        ]
    )

    assert system == [{"type": "text", "text": "embedded"}]
    assert messages[0]["role"] == "assistant"
    assert messages[0]["content"][1] == {
        "type": "tool_use",
        "id": "call-1",
        "name": "read",
        "input": {"path": "a.txt"},
    }
    assert messages[1]["content"][0]["tool_use_id"] == "call-1"
    assert _anthropic_tools(
        [
            {
                "type": "function",
                "function": {
                    "name": "read",
                    "description": "Read a file",
                    "parameters": {"type": "object"},
                },
            }
        ]
    )[0]["input_schema"] == {"type": "object"}


def test_anthropic_tool_only_turn_has_no_empty_text_and_preserves_cache_control() -> None:
    messages, _ = build_anthropic_messages(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {"name": "read", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "done"},
        ]
    )
    assert [part["type"] for part in messages[0]["content"]] == ["tool_use"]
    assert _anthropic_content(
        [
            {
                "type": "text",
                "text": "stable system prompt",
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            }
        ]
    ) == [
        {
            "type": "text",
            "text": "stable system prompt",
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        }
    ]


@pytest.mark.asyncio
async def test_anthropic_stream_normalizes_text_thinking_tools_usage_and_finish() -> None:
    events = [
        SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(
                usage=SimpleNamespace(
                    input_tokens=10,
                    cache_read_input_tokens=3,
                    cache_creation_input_tokens=2,
                )
            ),
        ),
        SimpleNamespace(
            type="content_block_delta",
            index=0,
            delta=SimpleNamespace(type="thinking_delta", thinking="plan"),
        ),
        SimpleNamespace(
            type="content_block_delta",
            index=1,
            delta=SimpleNamespace(type="text_delta", text="answer"),
        ),
        SimpleNamespace(
            type="content_block_start",
            index=2,
            content_block=SimpleNamespace(
                type="tool_use", id="call-1", name="read", input={}
            ),
        ),
        SimpleNamespace(
            type="content_block_delta",
            index=2,
            delta=SimpleNamespace(type="input_json_delta", partial_json='{"path":"a.txt"}'),
        ),
        SimpleNamespace(type="content_block_stop", index=2),
        SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(stop_reason="tool_use"),
            usage=SimpleNamespace(
                output_tokens=7,
                cache_read_input_tokens=3,
                cache_creation_input_tokens=2,
            ),
        ),
    ]

    async def stream():
        for event in events:
            yield event

    provider = AnthropicDesktopProvider.__new__(AnthropicDesktopProvider)
    provider._client = SimpleNamespace(
        messages=SimpleNamespace(create=AsyncMock(return_value=stream()))
    )
    chunks = [
        chunk
        async for chunk in provider.stream_chat(
            "claude-test",
            [{"role": "user", "content": "hello"}],
            max_tokens=100,
        )
    ]

    create_kwargs = provider._client.messages.create.await_args.kwargs
    assert "thinking" not in create_kwargs

    assert [(chunk.type, chunk.data) for chunk in chunks] == [
        ("reasoning-delta", {"text": "plan"}),
        ("text-delta", {"text": "answer"}),
        (
            "tool-call",
            {"id": "call-1", "name": "read", "arguments": {"path": "a.txt"}},
        ),
        ("finish", {"reason": "tool_calls"}),
        (
            "usage",
            {
                "input": 10,
                "output": 7,
                "reasoning": 0,
                "cache_read": 3,
                "cache_write": 2,
                "total": 20,
            },
        ),
    ]


@pytest.mark.asyncio
async def test_anthropic_model_shape_fallback_and_health_error(monkeypatch) -> None:
    monkeypatch.setattr(models_dev, "get_models", AsyncMock(return_value=[]))
    provider = AnthropicDesktopProvider.__new__(AnthropicDesktopProvider)
    provider._client = SimpleNamespace(
        models=SimpleNamespace(
            list=AsyncMock(
                return_value=SimpleNamespace(
                    data=[SimpleNamespace(id="claude-test", display_name="Claude Test")]
                )
            )
        )
    )
    models = await provider.list_models()
    assert models[0].capabilities.max_context == 200_000
    assert models[0].capabilities.max_output is None

    provider._client.models.list = AsyncMock(side_effect=RuntimeError("offline"))
    status = await provider.health_check()
    assert status.status == "error"
    assert status.error == "offline"


@pytest.mark.asyncio
async def test_anthropic_ignores_stale_frontend_reasoning_without_thinking_config() -> None:
    async def stream():
        if False:
            yield None

    create = AsyncMock(return_value=stream())
    provider = AnthropicDesktopProvider.__new__(AnthropicDesktopProvider)
    provider._client = SimpleNamespace(messages=SimpleNamespace(create=create))
    chunks = [
        chunk
        async for chunk in provider.stream_chat(
            "claude-sonnet-4-5",
            [{"role": "user", "content": "hello"}],
            extra_body={"reasoning": {"enabled": True}},
        )
    ]
    assert chunks == []
    assert "thinking" not in create.await_args.kwargs


def test_gemini_converts_openai_tool_history_and_specs() -> None:
    contents, system = build_gemini_contents(
        [
            {"role": "system", "content": "embedded"},
            {
                "role": "assistant",
                "content": "checking",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {"name": "read", "arguments": '{"path":"a.txt"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "hello"},
        ]
    )

    assert system == ["embedded"]
    assert contents[0]["role"] == "model"
    assert contents[0]["parts"][1]["function_call"]["args"] == {"path": "a.txt"}
    response = contents[1]["parts"][0]["function_response"]
    assert response["id"] == "call-1"
    assert response["name"] == "read"
    assert _gemini_tools(
        [
            {
                "type": "function",
                "function": {"name": "read", "parameters": {"type": "object"}},
            }
        ]
    )[0]["function_declarations"][0]["parameters_json_schema"] == {
        "type": "object"
    }


def test_gemini_tool_only_turn_has_no_empty_text() -> None:
    contents, _ = build_gemini_contents(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {"name": "read", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "done"},
        ]
    )
    assert list(contents[0]["parts"][0]) == ["function_call"]


@pytest.mark.asyncio
async def test_gemini_stream_normalizes_thought_text_tool_usage_and_finish() -> None:
    responses = [
        google_types.GenerateContentResponse(
            candidates=[
                google_types.Candidate(
                    content=google_types.Content(
                        role="model",
                        parts=[
                            google_types.Part(text="plan", thought=True),
                            google_types.Part(text="answer"),
                        ],
                    )
                )
            ]
        ),
        google_types.GenerateContentResponse(
            candidates=[
                google_types.Candidate(
                    content=google_types.Content(
                        role="model",
                        parts=[
                            google_types.Part(
                                function_call=google_types.FunctionCall(
                                    id="call-1", name="read", args={"path": "a.txt"}
                                )
                            )
                        ],
                    ),
                    finish_reason=google_types.FinishReason.STOP,
                )
            ],
            usage_metadata=google_types.GenerateContentResponseUsageMetadata(
                prompt_token_count=10,
                candidates_token_count=7,
                thoughts_token_count=2,
                cached_content_token_count=3,
                total_token_count=22,
            ),
        ),
    ]

    async def stream():
        for response in responses:
            yield response

    generate = AsyncMock(return_value=stream())
    provider = GeminiDesktopProvider.__new__(GeminiDesktopProvider)
    provider._client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content_stream=generate))
    )
    chunks = [
        chunk
        async for chunk in provider.stream_chat(
            "gemini-test",
            [{"role": "user", "content": "hello"}],
            max_tokens=100,
        )
    ]

    generate_kwargs = generate.await_args.kwargs
    assert generate_kwargs["config"].thinking_config is None

    assert [(chunk.type, chunk.data) for chunk in chunks] == [
        ("reasoning-delta", {"text": "plan"}),
        ("text-delta", {"text": "answer"}),
        (
            "tool-call",
            {"id": "call-1", "name": "read", "arguments": {"path": "a.txt"}},
        ),
        ("finish", {"reason": "tool_calls"}),
        (
            "usage",
            {
                "input": 10,
                "output": 7,
                "reasoning": 2,
                "cache_read": 3,
                "cache_write": 0,
                "total": 22,
            },
        ),
    ]


@pytest.mark.asyncio
async def test_gemini_health_error_is_reported() -> None:
    provider = GeminiDesktopProvider.__new__(GeminiDesktopProvider)
    provider._client = SimpleNamespace(
        aio=SimpleNamespace(
            models=SimpleNamespace(list=AsyncMock(side_effect=RuntimeError("offline")))
        )
    )
    status = await provider.health_check()
    assert status.status == "error"
    assert status.error == "offline"


@pytest.mark.asyncio
async def test_gemini_ignores_stale_frontend_reasoning_without_thinking_config() -> None:
    async def stream():
        if False:
            yield None

    generate = AsyncMock(return_value=stream())
    provider = GeminiDesktopProvider.__new__(GeminiDesktopProvider)
    provider._client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content_stream=generate))
    )
    chunks = [
        chunk
        async for chunk in provider.stream_chat(
            "gemini-2.5-pro",
            [{"role": "user", "content": "hello"}],
            extra_body={"reasoning": {"enabled": True}},
        )
    ]
    assert chunks == []
    assert generate.await_args.kwargs["config"].thinking_config is None
