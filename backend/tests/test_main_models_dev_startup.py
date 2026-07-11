"""Cold-start contract for the models.dev background refresh."""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock

import pytest

from app.main import _start_models_dev_background_refresh, lifespan


@pytest.mark.asyncio
async def test_models_dev_network_refresh_does_not_block_startup() -> None:
    refresh_started = asyncio.Event()
    allow_refresh_to_finish = asyncio.Event()

    async def slow_refresh() -> bool:
        refresh_started.set()
        await allow_refresh_to_finish.wait()
        return True

    service = AsyncMock()
    service.refresh.side_effect = slow_refresh

    task = _start_models_dev_background_refresh(service, interval_seconds=3600)
    await asyncio.wait_for(refresh_started.wait(), timeout=1)

    assert not task.done(), "startup must not await the remote models.dev request"

    allow_refresh_to_finish.set()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert task.done()


@pytest.mark.asyncio
async def test_models_dev_background_refresh_keeps_running_after_network_failure() -> None:
    refreshed_twice = asyncio.Event()
    calls = 0

    async def flaky_refresh() -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("offline")
        refreshed_twice.set()
        return True

    service = AsyncMock()
    service.refresh.side_effect = flaky_refresh

    task = _start_models_dev_background_refresh(service, interval_seconds=0.01)
    await asyncio.wait_for(refreshed_twice.wait(), timeout=1)

    assert not task.done()
    assert calls >= 2

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert task.done()


def test_lifespan_primes_models_dev_cache_before_provider_model_refreshes() -> None:
    source = inspect.getsource(lifespan)
    prime_position = source.find("models_dev.load_cached_for_startup()")
    provider_refresh_position = source.find("await registry.refresh_models()")
    background_position = source.find("_start_models_dev_background_refresh(models_dev)")

    assert prime_position >= 0
    assert provider_refresh_position >= 0
    assert prime_position < provider_refresh_position
    assert background_position > provider_refresh_position
    assert "await models_dev.refresh()" not in source
