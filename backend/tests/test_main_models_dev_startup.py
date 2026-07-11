"""Cold-start contract for the models.dev background refresh."""

from __future__ import annotations

import asyncio
import inspect
import threading
from unittest.mock import AsyncMock, patch

import pytest

from app.main import (
    _BackgroundTaskManager,
    _rebuild_upload_hash_index,
    _start_models_dev_background_refresh,
    lifespan,
)
from app.config import Settings
from app.connector.registry import ConnectorRegistry
from app.provider.registry import ProviderRegistry
from app.schemas.provider import ModelCapabilities, ModelInfo


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


def test_lifespan_primes_local_seed_before_background_provider_refreshes() -> None:
    source = inspect.getsource(lifespan)
    prime_position = source.find("models_dev.load_cached_for_startup()")
    seed_position = source.find("registry.seed_registered_models()")
    provider_refresh_position = source.find("registry.refresh_models()")
    background_position = source.find("_start_models_dev_background_refresh(")

    assert prime_position >= 0
    assert seed_position >= 0
    assert provider_refresh_position >= 0
    assert prime_position < seed_position < provider_refresh_position
    assert background_position > provider_refresh_position
    assert "await models_dev.refresh()" not in source


def test_lifespan_has_no_provider_or_tunnel_network_await_before_yield() -> None:
    source = inspect.getsource(lifespan)
    before_yield, separator, _ = source.partition("\n        yield\n")
    assert separator
    forbidden = (
        "await sub_provider._ensure_valid_token()",
        "await registry.refresh_models()",
        "await ollama_manager.start()",
        "await rapid_mlx_manager.start(",
        "await tunnel_mgr.start()",
        "await models_dev.refresh()",
        "await connector_registry.startup()",
        "await asyncio.to_thread(rebuild_hash_index)",
    )
    for expression in forbidden:
        assert expression not in before_yield


@pytest.mark.asyncio
async def test_permanently_slow_provider_does_not_block_seeded_startup() -> None:
    refresh_started = asyncio.Event()
    refresh_cancelled = asyncio.Event()
    never_finishes = asyncio.Event()

    class SlowProvider:
        id = "slow"

        def local_models(self):
            return [
                ModelInfo(
                    id="slow/seed",
                    name="Seed",
                    provider_id=self.id,
                    capabilities=ModelCapabilities(),
                )
            ]

        def clear_cache(self):
            return None

        async def list_models(self):
            refresh_started.set()
            try:
                await never_finishes.wait()
            except asyncio.CancelledError:
                refresh_cancelled.set()
                raise
            return []

    registry = ProviderRegistry()
    registry.register(SlowProvider())  # type: ignore[arg-type]
    tasks = _BackgroundTaskManager()

    async def startup() -> None:
        registry.seed_registered_models()
        tasks.create(registry.refresh_models(), name="permanently-slow-provider")

    await asyncio.wait_for(startup(), timeout=0.1)
    await asyncio.wait_for(refresh_started.wait(), timeout=1)
    assert registry.resolve_model("slow/seed") is not None

    await tasks.cancel_and_wait()
    await registry.shutdown()
    assert refresh_cancelled.is_set()


@pytest.mark.asyncio
async def test_background_task_manager_cancels_and_gathers_shutdown_jobs() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def forever() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    tasks = _BackgroundTaskManager()
    task = tasks.create(forever(), name="forever")
    await asyncio.wait_for(started.wait(), timeout=1)

    await asyncio.wait_for(tasks.cancel_and_wait(), timeout=1)

    assert cancelled.is_set()
    assert task.done()
    assert task.cancelled()


@pytest.mark.asyncio
async def test_permanently_slow_mcp_connection_does_not_block_startup(
    tmp_path,
) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def never_connects() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    connectors = ConnectorRegistry(project_dir=str(tmp_path))
    connectors.prepare()
    assert connectors.mcp_manager is not None
    connectors.mcp_manager.startup = AsyncMock(  # type: ignore[method-assign]
        side_effect=never_connects
    )
    tasks = _BackgroundTaskManager()

    async def startup() -> None:
        tasks.create(connectors.connect_enabled(), name="slow-mcp-connect")

    await asyncio.wait_for(startup(), timeout=0.1)
    await asyncio.wait_for(started.wait(), timeout=1)
    await tasks.cancel_and_wait()

    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_real_lifespan_yields_while_network_startup_jobs_are_stuck(tmp_path) -> None:
    mcp_started = asyncio.Event()
    mcp_cancelled = asyncio.Event()
    provider_started = asyncio.Event()
    provider_cancelled = asyncio.Event()

    async def never_connects(_self) -> None:
        mcp_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            mcp_cancelled.set()
            raise

    async def never_refreshes(_self) -> dict:
        provider_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            provider_cancelled.set()
            raise

    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'startup.db'}",
        project_dir=str(tmp_path),
        session_token_path=str(tmp_path / "session_token.json"),
        fts_enabled=False,
        channels_enabled=False,
    )
    app = type("App", (), {})()
    app.state = type("State", (), {"settings": settings})()

    with (
        patch.object(ConnectorRegistry, "connect_enabled", never_connects),
        patch.object(ProviderRegistry, "refresh_models", never_refreshes),
    ):
        async with asyncio.timeout(3):
            async with lifespan(app):  # type: ignore[arg-type]
                await asyncio.wait_for(mcp_started.wait(), timeout=1)
                await asyncio.wait_for(provider_started.wait(), timeout=1)

    assert mcp_cancelled.is_set()
    assert provider_cancelled.is_set()


@pytest.mark.asyncio
async def test_upload_hash_scan_is_cooperatively_cancelled_on_shutdown() -> None:
    started = threading.Event()
    stopped = threading.Event()

    def slow_scan(*, cancel_event: threading.Event) -> None:
        started.set()
        cancel_event.wait(timeout=5)
        if cancel_event.is_set():
            stopped.set()

    tasks = _BackgroundTaskManager()
    tasks.create(_rebuild_upload_hash_index(slow_scan), name="slow-hash-scan")
    assert await asyncio.to_thread(started.wait, 1)

    await asyncio.wait_for(tasks.cancel_and_wait(), timeout=1)

    assert stopped.is_set()
