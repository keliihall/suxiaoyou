"""Skill listing, toggle, and installation endpoints.

The public release does not bundle or query a third-party skill-discovery
catalog.  The store search route remains as a compatibility endpoint and
returns a clearly disabled, empty result without performing network I/O.

Installing a skill from an explicit GitHub URL remains available.  Users must
review the source and license of a skill before installing it.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.dependencies import SkillRegistryDep
from app.skill.registry import SkillPersistenceError, SkillRegistry

logger = logging.getLogger(__name__)

router = APIRouter()

_INSTALL_HTTP_TIMEOUT = 10.0


def _skill_source(skill_name: str, location: str) -> str:
    """Determine the source of a skill: 'plugin', 'bundled', or 'project'."""
    if ":" in skill_name:
        return "plugin"
    if "/data/skills/" in location or "\\data\\skills\\" in location:
        return "bundled"
    return "project"


def _plugin_source(skill_name: str, request: Request) -> str | None:
    """Return the owning plugin's installation source, when available.

    Namespaced skill names alone cannot distinguish built-in plugins from
    global or project plugins. The manager is authoritative and also reflects
    source precedence when a user plugin overrides a built-in plugin.
    """
    if ":" not in skill_name:
        return None

    manager = getattr(request.app.state, "plugin_manager", None)
    if manager is None:
        return None

    plugin_name = skill_name.split(":", 1)[0]
    detail = manager.detail(plugin_name)
    if not isinstance(detail, dict):
        return None

    source = detail.get("source")
    return source if source in {"builtin", "global", "project"} else None


def _skill_to_dict(skill, registry: SkillRegistry, request: Request) -> dict[str, Any]:
    """Convert a SkillInfo to an API response dict."""
    source = _skill_source(skill.name, skill.location)
    plugin_source = _plugin_source(skill.name, request)
    return {
        "name": skill.name,
        "description": skill.description,
        "location": skill.location,
        "source": source,
        "plugin_source": plugin_source,
        "catalog_managed": source == "bundled" or plugin_source == "builtin",
        "enabled": not registry.is_disabled(skill.name),
    }


@router.get("/skills")
async def list_skills(request: Request, registry: SkillRegistryDep) -> list[dict[str, Any]]:
    """List all discovered skills."""
    return [_skill_to_dict(skill, registry, request) for skill in registry.all_skills()]


@router.get("/skills/{skill_name}")
async def get_skill(registry: SkillRegistryDep, skill_name: str) -> dict[str, Any]:
    """Get skill details including full content."""
    skill = registry.get(skill_name)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"Skill not found: {skill_name}")
    return {
        "name": skill.name,
        "description": skill.description,
        "location": skill.location,
        "content": skill.content,
    }


@router.post("/skills/{skill_name}/enable")
async def enable_skill(
    request: Request,
    registry: SkillRegistryDep,
    skill_name: str,
) -> dict[str, Any]:
    """Enable a disabled skill."""
    skill = registry.get(skill_name)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"Skill not found: {skill_name}")
    try:
        success = registry.enable(skill_name)
    except SkillPersistenceError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    result: dict[str, Any] = {
        "success": success,
        "skills": [_skill_to_dict(s, registry, request) for s in registry.all_skills()],
    }
    if not success:
        result["error"] = "Skill is already enabled"
    return result


@router.post("/skills/{skill_name}/disable")
async def disable_skill(
    request: Request,
    registry: SkillRegistryDep,
    skill_name: str,
) -> dict[str, Any]:
    """Disable a skill (excludes it from LLM available skills)."""
    skill = registry.get(skill_name)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"Skill not found: {skill_name}")
    try:
        success = registry.disable(skill_name)
    except SkillPersistenceError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    result: dict[str, Any] = {
        "success": success,
        "skills": [_skill_to_dict(s, registry, request) for s in registry.all_skills()],
    }
    if not success:
        result["error"] = "Skill is already disabled"
    return result


# ---------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------


class InstallRequest(BaseModel):
    """Body for ``POST /api/skills/install``."""

    github_url: str
    # Optional display name; if absent we derive it from the resolved
    # SKILL.md frontmatter or the GitHub path.
    name: str | None = None


_GITHUB_BLOB = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/(?:blob|tree)/"
    r"(?P<ref>[^/]+)/(?P<path>.+?)(?:/SKILL\.md)?/?$"
)


def _github_to_raw(url: str) -> str:
    """Convert a github.com URL to its raw.githubusercontent.com equivalent.

    Handles both ``blob`` (file) and ``tree`` (directory) URLs. For
    directories we assume the target is ``SKILL.md`` at the root of the
    directory — this matches the skills-ecosystem convention.
    """
    m = _GITHUB_BLOB.match(url.strip())
    if not m:
        raise ValueError(f"Unsupported GitHub URL: {url!r}")
    owner, repo, ref, path = m["owner"], m["repo"], m["ref"], m["path"]
    # Strip a trailing ``SKILL.md`` the regex already handled, but if the
    # user passed a bare directory URL the regex drops the segment too;
    # re-append unconditionally so raw-content always points at the file.
    if not path.endswith("SKILL.md"):
        path = f"{path.rstrip('/')}/SKILL.md"
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"


def _slug(name: str) -> str:
    """Filesystem-safe slug for the install directory name."""
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip()).strip("-").lower()
    return slug or "skill"


def _global_skills_dir() -> Path:
    d = Path.home() / ".suxiaoyou" / "skills"
    d.mkdir(parents=True, exist_ok=True)
    return d


@router.get("/skills/store/search")
async def search_skill_store(
    q: str = "",
    page: int = 1,
    limit: int = 20,
    sort: str = "stars",
) -> dict[str, Any]:
    """Return a disabled empty store response without network access.

    The route is retained so older frontends receive a stable response instead
    of failing.  A future catalog may be enabled only after its sources and
    redistribution terms have been reviewed.
    """
    limit = max(1, min(limit, 50))
    page = max(1, page)
    sort = sort if sort in ("stars", "recent") else "stars"
    return {
        "success": True,
        "data": {
            "skills": [],
            "pagination": {
                "page": page,
                "limit": limit,
                "total": 0,
                "totalPages": 0,
                "hasNext": False,
                "hasPrev": False,
                "totalIsExact": True,
            },
            "filters": {"search": q, "sortBy": sort},
        },
        "meta": {
            "available": False,
            "source": "disabled",
            "reason": (
                "Online skill discovery is not included in this release. "
                "Install only skills whose source and license you have reviewed."
            ),
        },
    }


@router.post("/skills/install")
async def install_skill(
    registry: SkillRegistryDep,
    body: InstallRequest,
) -> dict[str, Any]:
    """Download a SKILL.md from GitHub and install it to the global
    user skills directory (``~/.suxiaoyou/skills/<slug>/SKILL.md``).

    The registry is rescanned so the new skill is immediately available
    without restarting the backend. Existing skills with the same
    filesystem slug are overwritten (enabling a simple "update" flow).
    """
    try:
        raw_url = _github_to_raw(body.github_url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    try:
        async with httpx.AsyncClient(
            timeout=_INSTALL_HTTP_TIMEOUT,
            follow_redirects=True,
        ) as client:
            resp = await client.get(raw_url)
    except httpx.HTTPError as e:
        logger.warning("Skill download failed: %s", e)
        raise HTTPException(status_code=502, detail="Could not download skill from GitHub") from e

    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail=f"SKILL.md not found at {raw_url}")
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"GitHub returned {resp.status_code} for SKILL.md",
        )

    content = resp.text
    if not content.lstrip().startswith("---"):
        raise HTTPException(
            status_code=422,
            detail="Downloaded file does not look like a valid SKILL.md (no YAML frontmatter)",
        )

    slug = _slug(body.name or body.github_url.rstrip("/").rsplit("/", 1)[-1])
    target_dir = _global_skills_dir() / slug
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / "SKILL.md"
    target_path.write_text(content, encoding="utf-8")

    # Rescan the registry so the new skill shows up in /api/skills
    # without a backend restart. scan() is additive (it doesn't clear
    # existing entries), which is what we want here.
    registry.scan()

    return {
        "success": True,
        "location": str(target_path),
        "skills": [_skill_to_dict(s, registry) for s in registry.all_skills()],
    }
