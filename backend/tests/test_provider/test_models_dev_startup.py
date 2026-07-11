"""Startup-specific cache behavior for models.dev metadata."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from app.provider.models_dev import ModelsDevService


@pytest.mark.asyncio
async def test_startup_cache_prime_uses_stale_disk_data_without_network(tmp_path) -> None:
    cached = {
        "openai": {
            "models": {
                "gpt-test": {
                    "name": "Cached GPT",
                    "cost": {"input": 1, "output": 2},
                }
            }
        }
    }
    (tmp_path / "models_dev_cache.json").write_text(json.dumps(cached), encoding="utf-8")
    service = ModelsDevService(cache_dir=tmp_path)
    service.refresh = AsyncMock(return_value=True)

    assert service.load_cached_for_startup() is True
    provider = await service.get_provider("openai")

    assert provider == cached["openai"]
    service.refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_cache_prime_uses_empty_fallback_without_network(tmp_path) -> None:
    service = ModelsDevService(cache_dir=tmp_path)
    service.refresh = AsyncMock(return_value=True)

    assert service.load_cached_for_startup() is False
    assert await service.get_provider("openai") is None
    service.refresh.assert_not_awaited()
