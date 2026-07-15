from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.security.control import SecurityControl


@pytest.mark.asyncio
async def test_state_persists_and_reloads_fail_closed_controls(tmp_path: Path) -> None:
    path = tmp_path / "security-state.json"
    control = SecurityControl(path)

    assert await control.set_tool_enabled("image_generate", False)
    assert await control.set_emergency_stop(True)

    reloaded = SecurityControl(path)
    assert reloaded.emergency_stop is True
    assert reloaded.disabled_tools == frozenset({"image_generate"})
    assert reloaded.updated_at
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_invalid_or_unknown_state_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "security-state.json"
    path.write_text(json.dumps({
        "version": 999,
        "emergency_stop": True,
        "disabled_tools": ["image_generate"],
    }))

    control = SecurityControl(path)
    assert control.emergency_stop is True
    assert not control.disabled_tools
    assert control.degraded_reason == "security_state_unreadable"


@pytest.mark.asyncio
async def test_rejects_invalid_tool_identifier(tmp_path: Path) -> None:
    control = SecurityControl(tmp_path / "state.json")
    with pytest.raises(ValueError):
        await control.set_tool_enabled("../../escape", False)


@pytest.mark.asyncio
async def test_refuses_symbolic_link_state_target(tmp_path: Path) -> None:
    target = tmp_path / "outside.json"
    target.write_text("{}")
    link = tmp_path / "security-state.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symbolic links unavailable")

    control = SecurityControl(link)
    with pytest.raises(OSError):
        await control.set_emergency_stop(True)
    assert control.emergency_stop is True
    assert control.degraded_reason == "security_state_not_persisted"
    assert target.read_text() == "{}"


@pytest.mark.asyncio
async def test_failed_deactivation_keeps_last_persisted_active_state(tmp_path: Path) -> None:
    path = tmp_path / "security-state.json"
    control = SecurityControl(path)
    await control.set_emergency_stop(True)

    path.unlink()
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    try:
        path.symlink_to(outside)
    except OSError:
        pytest.skip("symbolic links unavailable")

    with pytest.raises(OSError):
        await control.set_emergency_stop(False)
    assert control.emergency_stop is True
    assert json.loads(outside.read_text()) == {}


@pytest.mark.asyncio
async def test_tool_state_commits_only_after_persistence(tmp_path: Path) -> None:
    path = tmp_path / "security-state.json"
    control = SecurityControl(path)
    await control.set_tool_enabled("web_search", False)

    path.unlink()
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    try:
        path.symlink_to(outside)
    except OSError:
        pytest.skip("symbolic links unavailable")

    with pytest.raises(OSError):
        await control.set_tool_enabled("web_search", True)
    assert control.disabled_tools == frozenset({"web_search"})
