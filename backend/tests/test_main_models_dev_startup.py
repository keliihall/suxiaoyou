"""Cold-start contract for the models.dev background refresh."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import threading
from unittest.mock import AsyncMock, patch

import pytest

from app.main import (
    _BackgroundTaskManager,
    _rebuild_upload_hash_index,
    _shutdown_runtime,
    _start_models_dev_background_refresh,
    lifespan,
)
from app.config import Settings
from app.auth import credential_store
from app.auth.credential_store import CredentialStore
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
async def test_real_lifespan_reaches_ready_without_resolving_protected_credentials(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider and MCP references must stay inert through the lifespan yield."""

    class ExplodingReadBackend:
        def __init__(self) -> None:
            self.reads = 0

        def get_password(self, _service: str, _username: str) -> str | None:
            self.reads += 1
            raise AssertionError("native credential read before application readiness")

        def set_password(self, *_args) -> None:
            return None

        def delete_password(self, *_args) -> None:
            return None

    backend = ExplodingReadBackend()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        credential_store,
        "_discover_native_backend",
        lambda: backend,
    )
    credential_store.get_credential_store.cache_clear()

    project = tmp_path / "workspace"
    project.mkdir()
    scope_hash = hashlib.sha256(str(project.resolve()).encode()).hexdigest()[:20]
    mcp_path = tmp_path / "data" / "credentials" / "mcp" / f"{scope_hash}.json"
    mcp_path.parent.mkdir(parents=True)
    mcp_path.write_text(
        json.dumps(
            {
                "slack": {
                    "access_token": "suxiaoyou-credential://mcp-access",
                    "refresh_token": "suxiaoyou-credential://mcp-refresh",
                    "expires_at": 99999999.0,
                }
            }
        ),
        encoding="utf-8",
    )
    reference = "suxiaoyou-credential://env%3ASUXIAOYOU_DEEPSEEK_API_KEY%3Atest"
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'startup-credentials.db'}",
        project_dir=str(project),
        session_token_path=str(tmp_path / "session_token.json"),
        deepseek_api_key=reference,
        fts_enabled=False,
        channels_enabled=False,
    )
    app = type("App", (), {})()
    app.state = type("State", (), {"settings": settings})()

    try:
        async with asyncio.timeout(3):
            async with lifespan(app):  # type: ignore[arg-type]
                assert backend.reads == 0
                provider = app.state.provider_registry.get_provider("deepseek")
                assert provider is not None
                assert getattr(provider, "credential_deferred", False) is True
                # Let post-readiness startup tasks enter once. Automatic model
                # metadata refresh and connector discovery are still not an
                # authorization boundary for this provider credential.
                await asyncio.sleep(0)
                assert backend.reads == 0
    finally:
        credential_store.get_credential_store.cache_clear()


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


@pytest.mark.asyncio
async def test_runtime_shutdown_stops_consumers_before_providers_and_database() -> None:
    calls: list[str] = []

    class Background:
        async def cancel_and_wait(self):
            calls.append("background")

    class Scheduler:
        async def stop(self):
            calls.append("scheduler")

    class Streams:
        _jobs = {}

        def abort_all(self):
            calls.append("generations")
            return 0

    class Component:
        def __init__(self, name: str):
            self.name = name

        async def stop(self):
            calls.append(self.name)

        async def stop_all(self):
            calls.append(self.name)

        async def shutdown(self):
            calls.append(self.name)

        async def dispose(self):
            calls.append(self.name)

    ollama = Component("ollama")
    ollama.is_running = True
    rapid = Component("rapid")
    rapid.is_managed_process_alive = True

    await asyncio.wait_for(
        _shutdown_runtime(
            background_tasks=Background(),
            task_scheduler=Scheduler(),
            stream_manager=Streams(),
            shutdown_timeout=0.1,
            agent_adapter=Component("agent"),
            channel_manager=Component("channels"),
            workspace_memory_queue=Component("memory"),
            tunnel_manager=Component("tunnel"),
            connector_registry=Component("connectors"),
            index_manager=Component("index"),
            ollama_manager=ollama,
            rapid_mlx_manager=rapid,
            provider_registry=Component("providers"),
            engine=Component("database"),
        ),
        timeout=1,
    )

    assert calls == [
        "background",
        "scheduler",
        "generations",
        "agent",
        "channels",
        "memory",
        "tunnel",
        "connectors",
        "index",
        "ollama",
        "rapid",
        "providers",
        "database",
    ]


@pytest.mark.asyncio
async def test_runtime_shutdown_reaches_database_after_component_failure() -> None:
    calls: list[str] = []

    class Background:
        async def cancel_and_wait(self):
            calls.append("background")

    class Streams:
        _jobs = {}

        def abort_all(self):
            calls.append("generations")
            return 0

    class FailingChannels:
        async def stop_all(self):
            calls.append("channels")
            raise RuntimeError("channel shutdown failed")

    class Component:
        def __init__(self, name: str):
            self.name = name

        async def shutdown(self):
            calls.append(self.name)

        async def dispose(self):
            calls.append(self.name)

    await _shutdown_runtime(
        background_tasks=Background(),
        task_scheduler=None,
        stream_manager=Streams(),
        shutdown_timeout=0.1,
        agent_adapter=None,
        channel_manager=FailingChannels(),
        workspace_memory_queue=None,
        tunnel_manager=None,
        connector_registry=None,
        index_manager=None,
        ollama_manager=None,
        rapid_mlx_manager=None,
        provider_registry=Component("providers"),
        engine=Component("database"),
    )

    assert calls == ["background", "generations", "channels", "providers", "database"]
