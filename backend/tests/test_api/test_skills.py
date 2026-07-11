"""Tests for skill listing and optional store endpoints."""

from __future__ import annotations

import pytest

from app.api import skills as skills_api

pytestmark = pytest.mark.asyncio


async def test_store_search_is_disabled_empty_and_offline(app_client, monkeypatch):
    """The compatibility route must never consult an external catalog."""

    def forbid_network_client(*args, **kwargs):
        raise AssertionError("store search attempted to create a network client")

    monkeypatch.setattr(skills_api.httpx, "AsyncClient", forbid_network_client)

    response = await app_client.get(
        "/api/skills/store/search",
        params={"q": "browser", "page": -2, "limit": 100, "sort": "unknown"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["data"]["skills"] == []
    assert payload["data"]["pagination"] == {
        "page": 1,
        "limit": 50,
        "total": 0,
        "totalPages": 0,
        "hasNext": False,
        "hasPrev": False,
        "totalIsExact": True,
    }
    assert payload["data"]["filters"] == {
        "search": "browser",
        "sortBy": "stars",
    }
    assert payload["meta"]["available"] is False
    assert payload["meta"]["source"] == "disabled"
    assert "not included" in payload["meta"]["reason"]
