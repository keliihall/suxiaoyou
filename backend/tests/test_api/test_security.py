from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.main import _register_builtin_tools
from app.api import security as security_api
from app.auth.local import require_local_session
from app.security.control import SecurityControl
from app.tool.registry import ToolRegistry

pytestmark = pytest.mark.asyncio


class _BackgroundTasks:
    def __init__(self) -> None:
        self.names: list[str] = []

    def create(self, awaitable, *, name: str):
        self.names.append(name)
        awaitable.close()
        return None

    async def cancel_and_wait(self) -> None:
        return None


@pytest.fixture
def security_runtime(app_client, tmp_path):
    state = app_client.app.state
    control = SecurityControl(tmp_path / "security-state.json")
    tools = ToolRegistry()
    _register_builtin_tools(tools)
    tools.set_disabled(control.disabled_tools)

    state.security_control = control
    state.tool_registry = tools
    state.connector_registry = None
    state.provider_registry.get_provider.return_value = None
    state.provider_registry.shutdown = AsyncMock()
    state.task_scheduler = SimpleNamespace(
        _task=None,
        start=AsyncMock(),
        stop=AsyncMock(),
    )
    state.background_tasks = _BackgroundTasks()
    state.ollama_manager = None
    state.rapid_mlx_manager = None
    state.channel_manager = None
    state.agent_adapter = None
    state.tunnel_manager = None
    return state


async def test_overview_exposes_profiles_without_credentials(app_client, security_runtime):
    response = await app_client.get("/api/security/overview")
    assert response.status_code == 200
    payload = response.json()
    image = next(item for item in payload["tools"] if item["id"] == "image_generate")
    assert image["toggleable"] is True
    assert set(image["capabilities"]) >= {"network", "credential", "paid"}
    assert payload["release_gates"] == {
        "remote_access": False,
        "messaging_channels": False,
        "goals": True,
        "autonomous_goals": True,
    }
    assert payload["goal_limits"] == {
        "default_token_budget": None,
        "max_token_budget": None,
    }
    profiles = {item["source"]: item for item in payload["source_profiles"]}
    assert profiles["desktop"]["allowed_capabilities"] == ["*"]
    assert profiles["desktop"]["deny_unknown"] is True
    assert profiles["scheduler"]["deny_unknown"] is True
    assert profiles["channel"]["allowed_capabilities"] == ["model_inference"]
    assert all("api_key" not in provider for provider in payload["providers"])


async def test_security_center_is_restricted_to_the_local_desktop_session(
    app_client,
    security_runtime,
):
    def reject_non_local_request() -> None:
        raise HTTPException(status_code=403, detail="local desktop required")

    app_client.app.dependency_overrides[require_local_session] = reject_non_local_request
    try:
        overview = await app_client.get("/api/security/overview")
        tool_toggle = await app_client.put(
            "/api/security/tools/image_generate",
            json={"enabled": False},
        )
        emergency_stop = await app_client.post(
            "/api/security/emergency-stop",
            json={"active": True},
        )
    finally:
        app_client.app.dependency_overrides.pop(require_local_session, None)

    assert overview.status_code == 403
    assert tool_toggle.status_code == 403
    assert emergency_stop.status_code == 403


async def test_external_tool_switch_persists_and_is_audited(app_client, security_runtime):
    disabled = await app_client.put(
        "/api/security/tools/image_generate",
        json={"enabled": False},
    )
    assert disabled.status_code == 200
    assert "image_generate" in disabled.json()["state"]["disabled_tools"]
    assert security_runtime.tool_registry.get("image_generate") is None

    audit = await app_client.get("/api/security/audit")
    events = audit.json()["events"]
    assert events[0]["capability"] == "tool_control"
    assert events[0]["outcome"] == "success"
    assert events[0]["invocation_source_kind"] == "desktop"
    assert events[0]["invocation_source_id"] is None
    assert "credential" not in str(events[0]["details"]).lower()

    filtered = await app_client.get(
        "/api/security/audit",
        params={"invocation_source": "desktop"},
    )
    assert len(filtered.json()["events"]) == 2
    absent = await app_client.get(
        "/api/security/audit",
        params={"invocation_source": "scheduler"},
    )
    assert absent.json()["events"] == []


async def test_non_toggleable_tool_is_rejected(app_client, security_runtime):
    response = await app_client.put(
        "/api/security/tools/read",
        json={"enabled": False},
    )
    assert response.status_code == 400


async def test_emergency_stop_aborts_runtime_and_can_resume(app_client, security_runtime):
    stopped = await app_client.post(
        "/api/security/emergency-stop",
        json={"active": True},
    )
    assert stopped.status_code == 200
    assert stopped.json()["state"]["emergency_stop"] is True
    security_runtime.task_scheduler.stop.assert_awaited_once()

    resumed = await app_client.post(
        "/api/security/emergency-stop",
        json={"active": False},
    )
    assert resumed.status_code == 200
    assert resumed.json()["state"]["emergency_stop"] is False
    security_runtime.task_scheduler.start.assert_awaited_once()
    assert "security-resume-provider-refresh" in security_runtime.background_tasks.names


async def test_emergency_stop_rejects_new_desktop_generation(app_client, security_runtime):
    await security_runtime.security_control.set_emergency_stop(True)
    response = await app_client.post(
        "/api/chat/prompt",
        json={"text": "must not start"},
    )
    assert response.status_code == 423
    assert response.json()["detail"]["code"] == "security_emergency_stop"


async def test_emergency_transition_serializes_runtime_stop_and_resume(
    app_client,
    security_runtime,
    monkeypatch,
):
    stop_started = asyncio.Event()
    release_stop = asyncio.Event()
    transitions: list[str] = []

    async def fake_stop(_request):
        transitions.append("stop-start")
        stop_started.set()
        await release_stop.wait()
        transitions.append("stop-end")
        return []

    async def fake_resume(_request):
        transitions.append("resume")
        return []

    monkeypatch.setattr(security_api, "_stop_external_runtime", fake_stop)
    monkeypatch.setattr(security_api, "_resume_external_runtime", fake_resume)

    activate = asyncio.create_task(app_client.post(
        "/api/security/emergency-stop",
        json={"active": True},
    ))
    await asyncio.wait_for(stop_started.wait(), timeout=1)
    deactivate = asyncio.create_task(app_client.post(
        "/api/security/emergency-stop",
        json={"active": False},
    ))
    await asyncio.sleep(0)

    assert transitions == ["stop-start"]
    release_stop.set()
    activated, deactivated = await asyncio.gather(activate, deactivate)

    assert activated.status_code == 200
    assert deactivated.status_code == 200
    assert transitions == ["stop-start", "stop-end", "resume"]
    assert security_runtime.security_control.emergency_stop is False


async def test_failed_stop_persistence_stays_active_and_returns_warning(
    app_client,
    security_runtime,
    tmp_path,
    monkeypatch,
):
    state_path = security_runtime.security_control.state_path
    outside = tmp_path / "outside-security-state.json"
    outside.write_text("{}")
    try:
        state_path.symlink_to(outside)
    except OSError:
        pytest.skip("symbolic links unavailable")
    stop_runtime = AsyncMock(return_value=[])
    monkeypatch.setattr(security_api, "_stop_external_runtime", stop_runtime)

    response = await app_client.post(
        "/api/security/emergency-stop",
        json={"active": True},
    )

    assert response.status_code == 200
    assert response.json()["state"]["emergency_stop"] is True
    assert "security_state_not_persisted" in response.json()["warnings"]
    stop_runtime.assert_awaited_once()
    assert outside.read_text() == "{}"


async def test_inactive_request_retries_a_partially_failed_resume(
    app_client,
    security_runtime,
    monkeypatch,
):
    stop_runtime = AsyncMock(return_value=[])
    resume_runtime = AsyncMock(side_effect=[
        ["task_scheduler:RuntimeError"],
        [],
    ])
    monkeypatch.setattr(security_api, "_stop_external_runtime", stop_runtime)
    monkeypatch.setattr(security_api, "_resume_external_runtime", resume_runtime)
    await app_client.post(
        "/api/security/emergency-stop",
        json={"active": True},
    )

    first = await app_client.post(
        "/api/security/emergency-stop",
        json={"active": False},
    )
    retry = await app_client.post(
        "/api/security/emergency-stop",
        json={"active": False},
    )

    assert first.status_code == 200
    assert first.json()["state"]["emergency_stop"] is False
    assert first.json()["warnings"] == ["task_scheduler:RuntimeError"]
    assert retry.status_code == 200
    assert retry.json()["state"]["emergency_stop"] is False
    assert retry.json()["warnings"] == []
    assert resume_runtime.await_count == 2
