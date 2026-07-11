"""Security and release-scope tests for messaging channel endpoints."""

from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from app.api import channels


@pytest.mark.asyncio
async def test_login_rejects_every_unreleased_channel_before_manager_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hidden bridge cannot be reached through the public login API."""

    def fail_if_called(_request):
        raise AssertionError("channel manager must not be consulted")

    monkeypatch.setattr(channels, "_get_channel_manager", fail_if_called)

    app = FastAPI()
    app.include_router(channels.router)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        for channel_name in ("weixin", "feishu", "dingtalk", "wecom", "qq", "whatsapp"):
            response = await client.post(
                "/channels/login",
                json={"channel": channel_name},
            )
            assert response.status_code == 400
            assert response.json() == {"detail": "暂不支持该消息渠道"}
