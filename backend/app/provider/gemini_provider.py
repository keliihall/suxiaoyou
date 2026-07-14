"""Native Gemini provider backed by Google's official ``google-genai`` SDK."""

from __future__ import annotations

import base64
import json
import logging
from typing import Any, AsyncIterator

from google import genai
from google.genai import types

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


def _gemini_parts(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"text": content}] if content else []
    if not isinstance(content, list):
        text = str(content or "")
        return [{"text": text}] if text else []

    converted: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            text = str(part)
            if text:
                converted.append({"text": text})
            continue
        if part.get("type") in {"text", "input_text"}:
            text = str(part.get("text", ""))
            if text:
                converted.append({"text": text})
            continue
        if part.get("type") in {"image", "image_url", "input_image"}:
            image = part.get("image_url", part.get("source", {}))
            url = image.get("url", "") if isinstance(image, dict) else str(image)
            if url.startswith("data:") and ";base64," in url:
                header, data = url.split(",", 1)
                converted.append(
                    {
                        "inline_data": {
                            "mime_type": header[5:].split(";", 1)[0],
                            "data": base64.b64decode(data),
                        }
                    }
                )
            elif url:
                converted.append(
                    {
                        "file_data": {
                            "file_uri": url,
                            "mime_type": part.get("mime_type", "application/octet-stream"),
                        }
                    }
                )
    return converted


def _system_text(system: str | list[dict[str, Any]] | None) -> str | None:
    if isinstance(system, str):
        return system
    if not system:
        return None
    return "\n\n".join(
        str(part.get("text", ""))
        for part in system
        if isinstance(part, dict) and part.get("text")
    )


def _build_contents(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    contents: list[dict[str, Any]] = []
    embedded_system: list[str] = []
    tool_names: dict[str, str] = {}

    def append(role: str, parts: list[dict[str, Any]]) -> None:
        if not parts:
            return
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"].extend(parts)
        else:
            contents.append({"role": role, "parts": parts})

    for message in messages:
        role = message.get("role")
        if role in {"system", "developer"}:
            embedded_system.append(str(message.get("content", "")))
            continue
        if role == "tool":
            call_id = str(message.get("tool_call_id", ""))
            append(
                "user",
                [
                    {
                        "function_response": {
                            "id": call_id or None,
                            "name": tool_names.get(call_id, "tool"),
                            "response": {"output": message.get("content", "")},
                        }
                    }
                ],
            )
            continue

        target_role = "model" if role == "assistant" else "user"
        parts = _gemini_parts(message.get("content", ""))
        if target_role == "model":
            for tool_call in message.get("tool_calls") or []:
                function = tool_call.get("function", {})
                call_id = str(tool_call.get("id", ""))
                name = str(function.get("name", ""))
                if call_id:
                    tool_names[call_id] = name
                parts.append(
                    {
                        "function_call": {
                            "id": call_id or None,
                            "name": name,
                            "args": _json_object(function.get("arguments")),
                        }
                    }
                )
        append(target_role, parts)
    return contents, embedded_system


def _gemini_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    declarations = []
    for tool in tools:
        function = tool.get("function", tool)
        declarations.append(
            {
                "name": function["name"],
                "description": function.get("description", ""),
                "parameters_json_schema": function.get(
                    "parameters", {"type": "object", "properties": {}}
                ),
            }
        )
    return [{"function_declarations": declarations}]


class GeminiDesktopProvider(BaseProvider):
    """Gemini Generate Content adapter with native text, vision, and tools."""

    def __init__(self, api_key: str, **kwargs: Any):
        self._api_key = api_key
        self._client = genai.Client(api_key=api_key, **kwargs)

    @property
    def id(self) -> str:
        return "google"

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
                    max_context=1_048_576,
                ),
            )
            for model_id, name in PROVIDER_CATALOG[self.id].seed_models
        ]

    async def list_models(self) -> list[ModelInfo]:
        """Validate the key against Gemini, then enrich its live model list."""
        pager = await self._client.aio.models.list()
        live = [
            model
            async for model in pager
            if not model.supported_actions or "generateContent" in model.supported_actions
        ]

        metadata: dict[str, dict[str, Any]] = {}
        try:
            from app.provider.models_dev import models_dev

            metadata = {
                raw["id"].removeprefix("models/"): raw
                for raw in await models_dev.get_models(self.id) or []
            }
        except Exception as exc:
            logger.debug("models.dev unavailable for google: %s", exc)

        models: list[ModelInfo] = []
        for model in live:
            model_id = (model.name or "").removeprefix("models/")
            if not model_id:
                continue
            raw = metadata.get(model_id, {})
            caps = raw.get("capabilities", {})
            pricing = raw.get("pricing", {})
            models.append(
                ModelInfo(
                    id=model_id,
                    name=raw.get("name", model.display_name or model_id),
                    provider_id=self.id,
                    capabilities=ModelCapabilities(
                        function_calling=caps.get("function_calling", True),
                        vision=caps.get("vision", True),
                        # Thought signatures must be retained across tool turns
                        # before native Gemini reasoning can be advertised.
                        reasoning=False,
                        json_output=caps.get("json_output", True),
                        max_context=caps.get(
                            "max_context", model.input_token_limit or 1_048_576
                        ),
                        max_output=caps.get("max_output", model.output_token_limit),
                    ),
                    pricing=ModelPricing(
                        prompt=pricing.get("prompt", 0),
                        completion=pricing.get("completion", 0),
                    ),
                    metadata=raw.get("metadata", {}),
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
        contents, embedded_system = _build_contents(messages)
        system_instruction = _system_text(system)
        if embedded_system:
            system_instruction = "\n\n".join(
                part for part in [system_instruction, *embedded_system] if part
            )

        config: dict[str, Any] = {}
        if system_instruction:
            config["system_instruction"] = system_instruction
        if tools:
            config["tools"] = _gemini_tools(tools)
        if temperature is not None:
            config["temperature"] = temperature
        if max_tokens is not None:
            config["max_output_tokens"] = max_tokens
        if response_format and response_format.get("type") == "json_schema":
            config["response_mime_type"] = "application/json"
            config["response_json_schema"] = response_format.get("json_schema", {}).get(
                "schema", {}
            )
        # Omit thinking_config for every request path. The frontend can still
        # carry an old reasoning=true preference even though this provider
        # advertises reasoning=false; ignoring it keeps core prompts working
        # without opting into unpreservable thought signatures.

        try:
            stream = await self._client.aio.models.generate_content_stream(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(**config),
            )
            tool_index = 0
            async for response in stream:
                candidate = response.candidates[0] if response.candidates else None
                if candidate and candidate.content:
                    for part in candidate.content.parts or []:
                        if part.text:
                            yield StreamChunk(
                                type="reasoning-delta" if part.thought else "text-delta",
                                data={"text": part.text},
                            )
                        if part.function_call:
                            call = part.function_call
                            tool_index += 1
                            yield StreamChunk(
                                type="tool-call",
                                data={
                                    "id": call.id or f"call_google_{tool_index}",
                                    "name": call.name or "",
                                    "arguments": call.args or {},
                                },
                            )

                if candidate and candidate.finish_reason:
                    raw_reason = getattr(
                        candidate.finish_reason, "value", str(candidate.finish_reason)
                    )
                    reason = "length" if raw_reason == "MAX_TOKENS" else "stop"
                    if candidate.content and any(
                        part.function_call for part in candidate.content.parts or []
                    ):
                        reason = "tool_calls"
                    yield StreamChunk(type="finish", data={"reason": reason})

                usage = response.usage_metadata
                if usage and candidate and candidate.finish_reason:
                    normalized = {
                        "input": usage.prompt_token_count or 0,
                        "output": usage.candidates_token_count or 0,
                        "reasoning": usage.thoughts_token_count or 0,
                        "cache_read": usage.cached_content_token_count or 0,
                        "cache_write": 0,
                        "total": usage.total_token_count or 0,
                    }
                    yield StreamChunk(type="usage", data=normalized)
        except Exception as exc:
            logger.error("Gemini stream error for %s: %s", model, exc, exc_info=True)
            yield StreamChunk(type="error", data={"message": str(exc)})

    async def health_check(self) -> ProviderStatus:
        try:
            models = await self.list_models()
            return ProviderStatus(status="connected", model_count=len(models))
        except Exception as exc:
            return ProviderStatus(status="error", error=str(exc))

    def clear_cache(self) -> None:
        return None
