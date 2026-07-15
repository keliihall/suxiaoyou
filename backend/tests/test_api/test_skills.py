"""Tests for skill listing and optional store endpoints."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.api import skills as skills_api
from app.skill.registry import SkillPersistenceError

pytestmark = pytest.mark.asyncio


async def test_skill_list_exposes_catalog_ownership_by_plugin_source(app_client):
    """Only bundled metadata may be replaced by the built-in translation catalog."""
    skills = [
        SimpleNamespace(
            name="browser",
            description="Bundled browser skill",
            location="/opt/suxiaoyou/app/data/skills/browser/SKILL.md",
        ),
        SimpleNamespace(
            name="office:documents",
            description="Built-in plugin skill",
            location="/opt/suxiaoyou/app/data/plugins/office/skills/documents/SKILL.md",
        ),
        SimpleNamespace(
            name="custom:documents",
            description="Project plugin skill",
            location="/workspace/.suxiaoyou/plugins/custom/skills/documents/SKILL.md",
        ),
        SimpleNamespace(
            name="shared:documents",
            description="Global plugin skill",
            location="/home/user/.suxiaoyou/plugins/shared/skills/documents/SKILL.md",
        ),
        SimpleNamespace(
            name="my-project-skill",
            description="Project skill",
            location="/workspace/.suxiaoyou/skills/my-project-skill/SKILL.md",
        ),
    ]
    registry = app_client.app.state.skill_registry
    registry.all_skills.return_value = skills
    registry.is_disabled.return_value = False

    manager = MagicMock()
    manager.detail.side_effect = lambda name: {
        "office": {"source": "builtin"},
        "custom": {"source": "project"},
        "shared": {"source": "global"},
    }.get(name)
    app_client.app.state.plugin_manager = manager

    response = await app_client.get("/api/skills")

    assert response.status_code == 200
    payload = {skill["name"]: skill for skill in response.json()}
    assert payload["browser"]["catalog_managed"] is True
    assert payload["browser"]["plugin_source"] is None
    assert payload["office:documents"]["catalog_managed"] is True
    assert payload["office:documents"]["plugin_source"] == "builtin"
    assert payload["custom:documents"]["catalog_managed"] is False
    assert payload["custom:documents"]["plugin_source"] == "project"
    assert payload["shared:documents"]["catalog_managed"] is False
    assert payload["shared:documents"]["plugin_source"] == "global"
    assert payload["my-project-skill"]["catalog_managed"] is False


async def test_namespaced_skill_without_manager_is_not_catalog_managed(app_client):
    registry = app_client.app.state.skill_registry
    registry.all_skills.return_value = [
        SimpleNamespace(
            name="unknown:skill",
            description="Unknown plugin skill",
            location="/workspace/plugins/unknown/skills/skill/SKILL.md",
        )
    ]
    registry.is_disabled.return_value = False
    app_client.app.state.plugin_manager = None

    response = await app_client.get("/api/skills")

    assert response.status_code == 200
    assert response.json()[0]["catalog_managed"] is False
    assert response.json()[0]["plugin_source"] is None


async def test_enable_skill_reports_noop_instead_of_fixed_success(app_client):
    registry = app_client.app.state.skill_registry
    skill = SimpleNamespace(
        name="already-enabled",
        description="test",
        location="/workspace/SKILL.md",
    )
    registry.get.return_value = skill
    registry.all_skills.return_value = [skill]
    registry.is_disabled.return_value = False
    registry.enable.return_value = False

    response = await app_client.post("/api/skills/already-enabled/enable")

    assert response.status_code == 200
    assert response.json()["success"] is False
    assert response.json()["error"] == "Skill is already enabled"


async def test_disable_skill_reports_persistence_failure(app_client):
    registry = app_client.app.state.skill_registry
    skill = SimpleNamespace(
        name="project-skill",
        description="test",
        location="/workspace/SKILL.md",
    )
    registry.get.return_value = skill
    registry.disable.side_effect = SkillPersistenceError(
        "Skill state could not be saved; no runtime change was applied"
    )

    response = await app_client.post("/api/skills/project-skill/disable")

    assert response.status_code == 500
    assert "could not be saved" in response.json()["detail"]


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
