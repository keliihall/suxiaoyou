from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app import release_features
from app.acp.cli import AcpDatabaseBoundaryError, run_initialized_acp
from app.acp.server import AcpFeatureDisabled


@pytest.mark.asyncio
async def test_cli_gate_closes_before_application_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release_features, "V11_ACP_RELEASED", False)
    called = False

    def app_factory():
        nonlocal called
        called = True
        raise AssertionError("closed ACP gate started the application")

    with pytest.raises(AcpFeatureDisabled):
        await run_initialized_acp(app_factory=app_factory)
    assert called is False


@pytest.mark.asyncio
async def test_cli_runs_stdio_only_inside_initialized_application_lifespan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release_features, "V11_ACP_RELEASED", True)
    order: list[str] = []
    bridge = object()

    @asynccontextmanager
    async def lifespan(_application):
        order.append("startup")
        try:
            yield
        finally:
            order.append("shutdown")

    application = SimpleNamespace(
        router=SimpleNamespace(lifespan_context=lifespan),
        state=SimpleNamespace(
            settings=SimpleNamespace(
                database_url="sqlite+aiosqlite:////tmp/suxiaoyou-acp-test.db"
            )
        ),
    )

    def bridge_factory():
        order.append("bridge")
        return bridge

    async def stdio_runner(value):
        assert value is bridge
        order.append("stdio")

    await run_initialized_acp(
        app_factory=lambda: application,
        bridge_factory=bridge_factory,
        stdio_runner=stdio_runner,
    )

    assert order == ["startup", "bridge", "stdio", "shutdown"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql+asyncpg://localhost/suxiaoyou",
        "sqlite+aiosqlite:///:memory:",
    ],
)
async def test_cli_rejects_unleased_multi_process_database_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    database_url: str,
) -> None:
    monkeypatch.setattr(release_features, "V11_ACP_RELEASED", True)
    lifespan_entered = False

    @asynccontextmanager
    async def lifespan(_application):
        nonlocal lifespan_entered
        lifespan_entered = True
        yield

    application = SimpleNamespace(
        router=SimpleNamespace(lifespan_context=lifespan),
        state=SimpleNamespace(
            settings=SimpleNamespace(database_url=database_url)
        ),
    )

    with pytest.raises(AcpDatabaseBoundaryError):
        await run_initialized_acp(app_factory=lambda: application)

    assert lifespan_entered is False
