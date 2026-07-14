"""Native Anthropic provider backed by the official ``anthropic`` SDK."""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

from anthropic import AsyncAnthropic

from app.provider.base import BaseProvider
from app.provider.catalog import PROVIDER_CATALOG
from app.schemas.provider import (
    ModelCapabilities,
    ModelInfo,
    ModelPricing,
    ProviderStatus,
    StreamChunk,
)

logger = logging.getLogger(__name__)


def _metadata_model(raw: dict[str, Any], provider_id: str) -> ModelInfo:
    caps = raw.get("capabilities", {})
    pricing = raw.get("pricing", {})
    return ModelInfo(
        id=raw["id"],
        name=raw.get("name", raw["id"]),
        provider_id=provider_id,
        capabilities=ModelCapabilities(
            function_calling=caps.get("function_calling", True),
            vision=caps.get("vision", True),
            # Extended-thinking tool round-trips require signed thinking and
            # redacted blocks to be persisted verbatim. The common message
            # schema does not preserve those blocks yet, so do not advertise a
            # capability that would fail on a later tool turn.
            reasoning=False,
            json_output=caps.get("json_output", True),
            max_context=caps.get("max_context", 200_000),
            max_output=caps.get("max_output"),
            prompt_caching=caps.get("prompt_caching", True),
        ),
        pricing=ModelPricing(
            prompt=pricing.get("prompt", 0),
            completion=pricing.get("completion", 0),
        ),
        metadata=raw.get("metadata", {}),
    )


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {"_raw": value}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _anthropic_content(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        text = str(content or "")
        return [{"type": "text", "text": text}] if text else []

    converted: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            text = str(part)
            if text:
                converted.append({"type": "text", "text": text})
            continue
        if part.get("type") in {"text", "input_text"}:
            text = str(part.get("text", ""))
            if not text:
                continue
            block: dict[str, Any] = {"type": "text", "text": text}
            cache_control = part.get("cache_control")
            if isinstance(cache_control, dict) and cache_control.get("type") == "ephemeral":
                validated_cache_control: dict[str, str] = {"type": "ephemeral"}
                if cache_control.get("ttl") in {"5m", "1h"}:
                    validated_cache_control["ttl"] = cache_control["ttl"]
                block["cache_control"] = validated_cache_control
            converted.append(block)
            continue
        if part.get("type") in {"image", "image_url", "input_image"}:
            image = part.get("image_url", part.get("source", {}))
            url = image.get("url", "") if isinstance(image, dict) else str(image)
            if url.startswith("data:") and ";base64," in url:
                header, data = url.split(",", 1)
                converted.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": header[5:].split(";", 1)[0],
                            "data": data,
                        },
                    }
                )
            elif url:
                converted.append(
                    {"type": "image", "source": {"type": "url", "url": url}}
                )
    return converted


def _build_messages(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    result: list[dict[str, Any]] = []
    embedded_system: list[dict[str, Any]] = []

    def append(role: str, parts: list[dict[str, Any]]) -> None:
        if not parts:
            return
        if result and result[-1]["role"] == role:
            result[-1]["content"].extend(parts)
        else:
            result.append({"role": role, "content": parts})

    for message in messages:
        role = message.get("role")
        if role in {"system", "developer"}:
            embedded_system.extend(_anthropic_content(message.get("content", "")))
            continue
        if role == "tool":
            append(
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": str(message.get("tool_call_id", "")),
                        "content": _anthropic_content(message.get("content", "")),
                    }
                ],
            )
            continue

        target_role = "assistant" if role == "assistant" else "user"
        parts = _anthropic_content(message.get("content", ""))
        if target_role == "assistant":
            for tool_call in message.get("tool_calls") or []:
                function = tool_call.get("function", {})
                parts.append(
                    {
                        "type": "tool_use",
                        "id": str(tool_call.get("id", "")),
                        "name": str(function.get("name", "")),
                        "input": _json_object(function.get("arguments")),
                    }
                )
        append(target_role, parts)

    return result, embedded_system


def _anthropic_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted = []
    for tool in tools:
        function = tool.get("function", tool)
        converted.append(
            {
                "name": function["name"],
                "description": function.get("description", ""),
                "input_schema": function.get(
                    "parameters", {"type": "object", "properties": {}}
                ),
            }
        )
    return converted


class AnthropicDesktopProvider(BaseProvider):
    """Anthropic Messages API adapter with native text, vision, and tools."""

    def __init__(self, api_key: str, **kwargs: Any):
        self._api_key = api_key
        self._client = AsyncAnthropic(api_key=api_key, **kwargs)

    @property
    def id(self) -> str:
        return "anthropic"

    def local_models(self) -> list[ModelInfo]:
        return [
            ModelInfo(
                id=model_id,
                name=name,
                provider_id=self.id,
                capabilities=ModelCapabilities(
                    function_calling=True,
                    vision=True,
                    reasoning=False,
                    json_output=True,
                    max_context=200_000,
                    prompt_caching=True,
                ),
            )
            for model_id, name in PROVIDER_CATALOG[self.id].seed_models
        ]

    async def list_models(self) -> list[ModelInfo]:
        """Validate the key against Anthropic, then enrich its live model list."""
        page = await self._client.models.list(limit=100)
        live = list(page.data)

        metadata: dict[str, ModelInfo] = {}
        try:
            from app.provider.models_dev import models_dev

            for raw in await models_dev.get_models(self.id) or []:
                metadata[raw["id"]] = _metadata_model(raw, self.id)
        except Exception as exc:
            logger.debug("models.dev unavailable for anthropic: %s", exc)

        models: list[ModelInfo] = []
        for model in live:
            enriched = metadata.get(model.id)
            if enriched is not None:
                models.append(enriched)
                continue
            models.append(
                ModelInfo(
                    id=model.id,
                    name=model.display_name or model.id,
                    provider_id=self.id,
                    capabilities=ModelCapabilities(
                        function_calling=True,
                        vision=True,
                        reasoning=False,
                        json_output=True,
                        max_context=getattr(model, "max_input_tokens", None) or 200_000,
                        max_output=getattr(model, "max_tokens", None),
                        prompt_caching=True,
                    ),
                )
            )
        return models

    async def stream_chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        system: str | list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra_body: dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        native_messages, embedded_system = _build_messages(messages)
        system_blocks = _anthropic_content(system) if system else []
        system_blocks.extend(embedded_system)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": native_messages,
            "max_tokens": max_tokens or 8192,
            "stream": True,
        }
        if system_blocks:
            kwargs["system"] = system_blocks
        if tools:
            kwargs["tools"] = _anthropic_tools(tools)
        # Do not send a thinking field for any request path in this release.
        # The current frontend historically submits reasoning=true even for
        # models that do not advertise it, so this adapter defensively ignores
        # that stale preference rather than breaking ordinary prompts.
        # This is valid for Claude 4.5 (which rejects adaptive thinking) and
        # avoids claiming extended-thinking support without signed-block storage.
        if temperature is not None:
            kwargs["temperature"] = temperature
        if response_format and response_format.get("type") == "json_schema":
            schema = response_format.get("json_schema", {}).get("schema", {})
            kwargs["output_config"] = {
                "format": {"type": "json_schema", "schema": schema}
            }
        passthrough = dict(extra_body or {})
        passthrough.pop("reasoning", None)
        if passthrough:
            kwargs["extra_body"] = passthrough

        tool_calls: dict[int, dict[str, Any]] = {}
        usage = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0, "total": 0}
        try:
            stream = await self._client.messages.create(**kwargs)
            async for event in stream:
                event_type = getattr(event, "type", "")
                if event_type == "message_start":
                    raw_usage = event.message.usage
                    usage["input"] = raw_usage.input_tokens or 0
                    usage["cache_read"] = (
                        getattr(raw_usage, "cache_read_input_tokens", None) or 0
                    )
                    usage["cache_write"] = (
                        getattr(raw_usage, "cache_creation_input_tokens", None) or 0
                    )
                elif event_type == "content_block_start":
                    block = event.content_block
                    if getattr(block, "type", "") == "tool_use":
                        tool_calls[event.index] = {
                            "id": block.id,
                            "name": block.name,
                            "arguments": json.dumps(block.input) if block.input else "",
                        }
                elif event_type == "content_block_delta":
                    delta = event.delta
                    if delta.type == "text_delta":
                        yield StreamChunk(type="text-delta", data={"text": delta.text})
                    elif delta.type == "thinking_delta":
                        yield StreamChunk(
                            type="reasoning-delta", data={"text": delta.thinking}
                        )
                    elif delta.type == "input_json_delta":
                        tool_calls.setdefault(
                            event.index, {"id": "", "name": "", "arguments": ""}
                        )["arguments"] += delta.partial_json
                elif event_type == "content_block_stop" and event.index in tool_calls:
                    call = tool_calls.pop(event.index)
                    yield StreamChunk(
                        type="tool-call",
                        data={
                            "id": call["id"],
                            "name": call["name"],
                            "arguments": _json_object(call["arguments"]),
                        },
                    )
                elif event_type == "message_delta":
                    raw_usage = event.usage
                    raw_output = raw_usage.output_tokens or 0
                    output_details = getattr(raw_usage, "output_tokens_details", None)
                    usage["reasoning"] = min(
                        raw_output,
                        getattr(output_details, "thinking_tokens", 0) or 0,
                    )
                    usage["output"] = raw_output - usage["reasoning"]
                    usage["cache_read"] = (
                        getattr(raw_usage, "cache_read_input_tokens", None)
                        or usage["cache_read"]
                    )
                    usage["cache_write"] = (
                        getattr(raw_usage, "cache_creation_input_tokens", None)
                        or usage["cache_write"]
                    )
                    usage["total"] = usage["input"] + usage["output"] + usage["reasoning"] + usage["cache_read"]
                    reason = event.delta.stop_reason or "stop"
                    yield StreamChunk(
                        type="finish",
                        data={"reason": "tool_calls" if reason == "tool_use" else ("length" if reason == "max_tokens" else "stop")},
                    )
                    yield StreamChunk(type="usage", data=usage)
        except Exception as exc:
            logger.error("Anthropic stream error for %s: %s", model, exc, exc_info=True)
            yield StreamChunk(type="error", data={"message": str(exc)})

    async def health_check(self) -> ProviderStatus:
        try:
            models = await self.list_models()
            return ProviderStatus(status="connected", model_count=len(models))
        except Exception as exc:
            return ProviderStatus(status="error", error=str(exc))

    def clear_cache(self) -> None:
        return None
