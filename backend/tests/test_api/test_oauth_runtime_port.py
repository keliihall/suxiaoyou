"""OAuth callback URLs must follow the desktop backend's runtime port."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlparse

import pytest

pytestmark = pytest.mark.asyncio


async def test_google_oauth_uses_runtime_port(app_client):
    settings = app_client.app.state.settings
    settings.port = 17321
    settings.google_client_id = "test-google-client"

    response = await app_client.post("/api/google/auth-start")

    assert response.status_code == 200
    auth_url = response.json()["auth_url"]
    query = parse_qs(urlparse(auth_url).query)
    assert query["redirect_uri"] == ["http://localhost:17321/api/google/callback"]


async def test_legacy_mcp_oauth_uses_runtime_port(app_client):
    settings = app_client.app.state.settings
    settings.port = 17321
    registry = MagicMock()
    registry.connect = AsyncMock(
        return_value={"auth_url": "https://example.test/auth", "state": "state"}
    )
    registry.mcp_manager = MagicMock()
    app_client.app.state.connector_registry = registry

    response = await app_client.post("/api/mcp/notion/auth-start")

    assert response.status_code == 200
    registry.connect.assert_awaited_once_with(
        "notion",
        "http://localhost:17321/api/connectors/oauth/callback",
    )
