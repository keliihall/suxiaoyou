from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app import release_features
from app.main import _register_builtin_tools
from app.api import security as security_api
from app.auth.local import require_local_session
from app.hooks.config import register_project_hook_config
from app.hooks.registry import HookRegistry
from app.storage.workspace_identity import ensure_workspace_identity
from app.hooks.trust import HookTrustStore
from app.models.session import Session
from app.models.workspace_instance import WorkspaceInstance
from app.security.audit import AuditPersistenceError
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


async def _seed_project_hook(
    session_factory,
    workspace: Path,
    *,
    session_id: str = "hook-session",
) -> tuple[object, HookTrustStore]:
    workspace.mkdir(parents=True, exist_ok=True)
    executable = workspace / "policy"
    executable.write_text(
        f"#!{sys.executable}\nprint('ok')\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    config_dir = workspace / ".suxiaoyou"
    config_dir.mkdir()
    (config_dir / "hooks.json").write_text(
        json.dumps({
            "version": 1,
            "hooks": [{
                "hook_id": "local-policy",
                "event": "PreToolUse",
                "failure_policy": "required",
                "command": ["policy", "--strict"],
            }],
        }),
        encoding="utf-8",
    )
    identity = ensure_workspace_identity(workspace).durable_token
    async with session_factory() as db:
        async with db.begin():
            db.add(Session(
                id=session_id,
                directory=str(workspace.resolve()),
                title="Hook session",
                version="1.1.0",
            ))
            db.add(WorkspaceInstance(
                id=f"{session_id}-workspace",
                created_by_session_id=session_id,
                kind="direct",
                root_path=str(workspace.resolve()),
                identity_token=identity,
                status="active",
                details={"managed": False},
            ))
    registry = HookRegistry(workspace)
    hook, = register_project_hook_config(registry)
    trust = HookTrustStore(workspace)
    trust.approve(hook)
    return hook, trust


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
        "v11_checkpoints": True,
        "v11_rewind": True,
        "v11_hooks": True,
        "v11_acp": True,
        "v11_worktrees": True,
        "v11_validation_agent": True,
        "v11_office_v2": True,
        "v11_user_office_templates_beta": True,
    }
    assert payload["goal_limits"] == {
        "default_token_budget": None,
        "max_token_budget": None,
    }
    runtime = payload["v11_runtime_capabilities"]
    assert runtime["checkpoint_rewind"] == {
        "released": True,
        "local_session_only": True,
        "server_owned_workspace_identity_required": True,
        "pre_action_audit_required": True,
        "external_side_effects_reverted": False,
        "raw_runtime_payloads_exposed": False,
    }
    assert runtime["managed_worktrees"] == {
        "released": True,
        "local_session_only": True,
        "repository_derived_from_database": True,
        "force_remove_supported": False,
        "pre_action_audit_required": True,
        "raw_runtime_payloads_exposed": False,
    }
    readiness = payload["v11_readiness"]
    assert readiness["office_preview"]["released"] is True
    assert readiness["office_authoring"]["runtime_ready"] is False
    assert readiness["user_office_templates"]["released"] is True
    assert not any(
        marker in str(runtime).lower()
        for marker in ("workspace_path", "command", "hook_payload", "acp_payload")
    )
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


async def test_v11_runtime_capability_status_reads_dynamic_release_gates(
    app_client,
    security_runtime,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(release_features, "V11_CHECKPOINTS_RELEASED", True)
    monkeypatch.setattr(release_features, "V11_REWIND_RELEASED", True)
    monkeypatch.setattr(release_features, "V11_WORKTREES_RELEASED", True)

    payload = (await app_client.get("/api/security/overview")).json()
    assert payload["v11_runtime_capabilities"]["checkpoint_rewind"]["released"] is True
    assert payload["v11_runtime_capabilities"]["managed_worktrees"]["released"] is True
    assert payload["v11_readiness"]["rewind"]["released"] is True
    assert payload["v11_readiness"]["worktrees"]["released"] is True


async def test_hook_control_is_hidden_while_release_gate_is_closed(
    app_client,
    security_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release_features, "V11_HOOKS_RELEASED", False)
    listed = await app_client.get(
        "/api/security/hooks",
        params={"session_id": "hook-session"},
    )
    revoked = await app_client.post(
        "/api/security/hooks/revoke",
        json={"session_id": "hook-session", "hook_id": "local-policy"},
    )
    assert listed.status_code == revoked.status_code == 404
    assert listed.json()["code"] == "v11_hooks_not_available"
    assert revoked.json()["code"] == "v11_hooks_not_available"


async def test_hook_control_lists_safe_identity_and_durably_revokes_trust(
    app_client,
    security_runtime,
    session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release_features, "V11_HOOKS_RELEASED", True)
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(tmp_path / "private"))
    workspace = tmp_path / "workspace"
    hook, trust = await _seed_project_hook(session_factory, workspace)

    listed = await app_client.get(
        "/api/security/hooks",
        params={"session_id": "hook-session"},
    )
    assert listed.status_code == 200
    assert listed.headers["cache-control"] == "no-store"
    assert listed.json() == {
        "session_id": "hook-session",
        "trust_store_available": True,
        "hooks": [{
            "hook_id": "local-policy",
            "event": "PreToolUse",
            "source": "project",
            "failure_policy": "required",
            "timeout_seconds": 5.0,
            "fingerprint": hook.fingerprint,
            "approval_state": "approved",
        }],
    }
    serialized = listed.text.lower()
    assert str(workspace).lower() not in serialized
    assert "command" not in serialized
    assert "environment" not in serialized
    assert "executable" not in serialized

    revoked = await app_client.post(
        "/api/security/hooks/revoke",
        json={"session_id": "hook-session", "hook_id": "local-policy"},
    )
    assert revoked.status_code == 200
    assert revoked.json() == {
        "session_id": "hook-session",
        "hook_id": "local-policy",
        "revoked": True,
    }
    assert not HookTrustStore(workspace).is_approved(hook)
    assert not trust.is_approved(hook)

    relisted = await app_client.get(
        "/api/security/hooks",
        params={"session_id": "hook-session"},
    )
    assert relisted.json()["hooks"][0]["approval_state"] == "required"

    audit = await app_client.get(
        "/api/security/audit",
        params={"source_kind": "security_center"},
    )
    hook_events = [
        item for item in audit.json()["events"]
        if item["capability"] == "hook_trust"
    ]
    assert [item["outcome"] for item in reversed(hook_events)] == [
        "started",
        "success",
    ]
    assert str(workspace).lower() not in json.dumps(hook_events).lower()


async def test_hook_revocation_fails_closed_when_required_audit_is_unavailable(
    app_client,
    security_runtime,
    session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release_features, "V11_HOOKS_RELEASED", True)
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(tmp_path / "private"))
    workspace = tmp_path / "workspace"
    hook, _trust = await _seed_project_hook(session_factory, workspace)

    async def unavailable_audit(*_args, **kwargs) -> None:
        if kwargs.get("required"):
            raise AuditPersistenceError("audit unavailable")

    monkeypatch.setattr(security_api, "record_security_event", unavailable_audit)
    response = await app_client.post(
        "/api/security/hooks/revoke",
        json={"session_id": "hook-session", "hook_id": "local-policy"},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "hook_audit_unavailable"
    assert HookTrustStore(workspace).is_approved(hook)


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
