"""Security Center Lite overview, controls, and redacted audit history."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from app.auth.local import require_local_session
from app.config import get_custom_endpoints
from app.dependencies import DbDep, SessionFactoryDep
from app.hooks.config import (
    ProjectHookConfigError,
    register_project_hook_config,
)
from app.hooks.registry import CommandHook, HookRegistry
from app.hooks.trust import HookTrustStore, HookTrustStoreError
from app.models.scheduled_task import ScheduledTask
from app.models.security_audit_event import SecurityAuditEvent
from app.models.session import Session
from app.models.workspace_instance import WorkspaceInstance
from app.provider.catalog import PROVIDER_CATALOG
from app import release_features
from app.i18n import request_language
from app.security.audit import AuditPersistenceError, record_security_event
from app.security.capabilities import describe_tool_source, source_capability_profiles
from app.security.control import TOGGLEABLE_BUILTIN_TOOLS
from app.storage.checkpoints import inspect_workspace_identity

router = APIRouter(
    prefix="/security",
    dependencies=[Depends(require_local_session)],
)


class ToolToggleBody(BaseModel):
    enabled: bool


class EmergencyStopBody(BaseModel):
    active: bool


class HookControlBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1, max_length=128)
    hook_id: str = Field(min_length=1, max_length=128)


def _hook_error(*, status_code: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"detail": detail, "code": code},
    )


def _hooks_unavailable() -> JSONResponse:
    return _hook_error(
        status_code=404,
        code="v11_hooks_not_available",
        detail="Project Hooks are not available in this release",
    )


async def _project_hook_control(
    session_factory: SessionFactoryDep,
    *,
    session_id: str,
) -> tuple[tuple[CommandHook, ...], HookTrustStore]:
    """Resolve project Hooks only from a durable server-owned workspace."""

    async with session_factory() as db:
        session = await db.get(Session, session_id)
        if session is None or not session.directory or session.directory == ".":
            raise LookupError("session_workspace_not_found")
        try:
            canonical, identity = await asyncio.to_thread(
                inspect_workspace_identity,
                session.directory,
            )
        except Exception as exc:
            raise LookupError("session_workspace_not_found") from exc
        instance = (
            await db.execute(
                select(WorkspaceInstance)
                .where(
                    WorkspaceInstance.root_path == canonical,
                    WorkspaceInstance.identity_token == identity,
                    WorkspaceInstance.status == "active",
                    WorkspaceInstance.project_id == session.project_id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if instance is None:
            raise LookupError("session_workspace_not_found")

    registry = HookRegistry(Path(canonical))
    hooks = register_project_hook_config(registry)
    return hooks, HookTrustStore(Path(canonical))


def _public_hook(
    hook: CommandHook,
    *,
    approval_state: str,
) -> dict[str, Any]:
    """Return an intentionally path-, command-, and environment-free view."""

    declaration = hook.declaration
    return {
        "hook_id": declaration.hook_id,
        "event": declaration.event.value,
        "source": hook.source.value,
        "failure_policy": declaration.failure_policy.value,
        "timeout_seconds": declaration.timeout_seconds,
        "fingerprint": hook.fingerprint,
        "approval_state": approval_state,
    }


async def _build_overview(request: Request, db: DbDep) -> dict[str, Any]:
    control = request.app.state.security_control
    tool_registry = request.app.state.tool_registry
    connector_registry = getattr(request.app.state, "connector_registry", None)
    provider_registry = request.app.state.provider_registry
    settings = request.app.state.settings

    tools: list[dict[str, Any]] = []
    for tool in sorted(tool_registry.registered_tools(), key=lambda item: item.id):
        if tool.id == "invalid":
            continue
        source_kind, source_id, capabilities = describe_tool_source(tool)
        tools.append({
            "id": tool.id,
            "description": tool.description,
            "source_kind": source_kind,
            "source_id": source_id,
            "capabilities": capabilities,
            "enabled": tool_registry.is_enabled(tool.id),
            "requires_approval": bool(getattr(tool, "requires_approval", False)),
            "toggleable": tool.id in TOGGLEABLE_BUILTIN_TOOLS,
        })

    connectors: list[dict[str, Any]] = []
    if connector_registry is not None:
        for connector_id, status in sorted(connector_registry.status().items()):
            capabilities = ["network", "remote_data", "credential"]
            if status.get("type") == "local":
                capabilities.append("process")
            connectors.append({
                "id": connector_id,
                "name": status.get("name", connector_id),
                "enabled": bool(status.get("enabled")),
                "connected": bool(status.get("connected")),
                "status": str(status.get("status", "disabled")),
                "credential_configured": bool(status.get("credential_configured")),
                "capabilities": capabilities,
            })

    providers: list[dict[str, Any]] = []
    for provider_id, definition in PROVIDER_CATALOG.items():
        configured = bool(str(getattr(settings, definition.settings_key, "")).strip())
        providers.append({
            "id": provider_id,
            "name": definition.display_name(request_language(request)),
            "configured": configured,
            "enabled": provider_registry.get_provider(provider_id) is not None,
            "capabilities": ["network", "credential", "paid", "model_inference"],
        })
    for endpoint in get_custom_endpoints(settings):
        provider_id = str(endpoint["id"])
        providers.append({
            "id": provider_id,
            "name": str(endpoint.get("name") or provider_id),
            "configured": bool(endpoint.get("base_url")),
            "enabled": provider_registry.get_provider(provider_id) is not None,
            "capabilities": ["network", "credential", "paid", "model_inference"],
        })

    enabled_count = int(
        (await db.execute(
            select(func.count()).select_from(ScheduledTask).where(ScheduledTask.enabled.is_(True))
        )).scalar_one()
    )
    scheduler = getattr(request.app.state, "task_scheduler", None)
    runtime_running = bool(
        scheduler is not None
        and getattr(scheduler, "_task", None) is not None
        and not scheduler._task.done()
    )

    from app.runtime.v11_readiness import v11_runtime_readiness

    v11_readiness = v11_runtime_readiness(request.app.state)

    return {
        "state": control.snapshot(),
        "source_profiles": source_capability_profiles(),
        "tools": tools,
        "connectors": connectors,
        "providers": providers,
        "automations": {
            "enabled_count": enabled_count,
            "runtime_running": runtime_running,
        },
        "goal_limits": {
            "default_token_budget": settings.goal_default_token_budget,
            "max_token_budget": settings.goal_max_token_budget,
        },
        "release_gates": {
            "remote_access": release_features.REMOTE_ACCESS_RELEASED,
            "messaging_channels": release_features.MESSAGING_CHANNELS_RELEASED,
            "goals": release_features.GOALS_RELEASED,
            "autonomous_goals": release_features.AUTONOMOUS_GOALS_RELEASED,
            "v11_checkpoints": release_features.V11_CHECKPOINTS_RELEASED,
            "v11_rewind": release_features.V11_REWIND_RELEASED,
            "v11_hooks": release_features.V11_HOOKS_RELEASED,
            "v11_acp": release_features.V11_ACP_RELEASED,
            "v11_worktrees": release_features.V11_WORKTREES_RELEASED,
            "v11_validation_agent": release_features.V11_VALIDATION_AGENT_RELEASED,
            "v11_office_v2": release_features.V11_OFFICE_V2_RELEASED,
            "v11_user_office_templates_beta": bool(
                getattr(
                    release_features,
                    "V11_USER_OFFICE_TEMPLATES_BETA_RELEASED",
                    False,
                )
            ),
        },
        "v11_readiness": v11_readiness,
        "v11_runtime_capabilities": {
            "checkpoint_rewind": {
                "released": bool(v11_readiness["rewind"]["released"]),
                "local_session_only": True,
                "server_owned_workspace_identity_required": True,
                "pre_action_audit_required": True,
                "external_side_effects_reverted": False,
                "raw_runtime_payloads_exposed": False,
            },
            "managed_worktrees": {
                "released": bool(v11_readiness["worktrees"]["released"]),
                "local_session_only": True,
                "repository_derived_from_database": True,
                "force_remove_supported": False,
                "pre_action_audit_required": True,
                "raw_runtime_payloads_exposed": False,
            },
        },
    }


@router.get("/overview")
async def security_overview(request: Request, db: DbDep) -> dict[str, Any]:
    return await _build_overview(request, db)


@router.get("/audit")
async def security_audit(
    db: DbDep,
    limit: int = Query(default=100, ge=1, le=500),
    source_kind: str | None = Query(default=None, max_length=32),
    invocation_source: str | None = Query(default=None, max_length=32),
    outcome: str | None = Query(default=None, max_length=32),
) -> dict[str, Any]:
    statement = select(SecurityAuditEvent)
    if source_kind:
        statement = statement.where(SecurityAuditEvent.source_kind == source_kind)
    if invocation_source:
        statement = statement.where(
            SecurityAuditEvent.invocation_source_kind == invocation_source
        )
    if outcome:
        statement = statement.where(SecurityAuditEvent.outcome == outcome)
    events = (
        await db.execute(
            statement.order_by(SecurityAuditEvent.time_created.desc()).limit(limit)
        )
    ).scalars().all()
    return {
        "events": [
            {
                "id": event.id,
                "source_kind": event.source_kind,
                "source_id": event.source_id,
                "invocation_source_kind": event.invocation_source_kind,
                "invocation_source_id": event.invocation_source_id,
                "capability": event.capability,
                "action": event.action,
                "decision": event.decision,
                "outcome": event.outcome,
                "session_id": event.session_id,
                "call_id": event.call_id,
                "details": event.details,
                "time_created": event.time_created.isoformat(),
            }
            for event in events
        ]
    }


@router.get("/hooks")
async def project_hooks(
    session_factory: SessionFactoryDep,
    session_id: str = Query(min_length=1, max_length=128),
) -> JSONResponse:
    if not release_features.V11_HOOKS_RELEASED:
        return _hooks_unavailable()
    try:
        hooks, trust = await _project_hook_control(
            session_factory,
            session_id=session_id,
        )
    except LookupError:
        return _hook_error(
            status_code=404,
            code="hook_workspace_not_found",
            detail="The session does not have a verified active workspace",
        )
    except (ProjectHookConfigError, ValueError):
        return _hook_error(
            status_code=409,
            code="hook_configuration_invalid",
            detail="The project Hook configuration could not be loaded safely",
        )

    approval_states = [
        "approved" if await asyncio.to_thread(trust.is_approved, hook) else "required"
        for hook in hooks
    ]
    if trust.degraded_reason is not None:
        approval_states = ["unavailable" for _hook in hooks]
    return JSONResponse(
        content={
            "session_id": session_id,
            "trust_store_available": trust.degraded_reason is None,
            "hooks": [
                _public_hook(hook, approval_state=approval_state)
                for hook, approval_state in zip(hooks, approval_states)
            ],
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/hooks/revoke")
async def revoke_project_hook(
    body: HookControlBody,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    if not release_features.V11_HOOKS_RELEASED:
        return _hooks_unavailable()
    try:
        hooks, trust = await _project_hook_control(
            session_factory,
            session_id=body.session_id,
        )
    except LookupError:
        return _hook_error(
            status_code=404,
            code="hook_workspace_not_found",
            detail="The session does not have a verified active workspace",
        )
    except (ProjectHookConfigError, ValueError):
        return _hook_error(
            status_code=409,
            code="hook_configuration_invalid",
            detail="The project Hook configuration could not be loaded safely",
        )

    hook = next(
        (item for item in hooks if item.declaration.hook_id == body.hook_id),
        None,
    )
    if hook is None:
        return _hook_error(
            status_code=404,
            code="hook_not_found",
            detail="The project Hook was not found",
        )

    audit_details = {
        "hook_id": hook.declaration.hook_id,
        "event": hook.declaration.event.value,
        "source": hook.source.value,
        "fingerprint": hook.fingerprint,
    }
    try:
        await record_security_event(
            session_factory,
            source_kind="security_center",
            source_id="desktop",
            invocation_source_kind="desktop",
            capability="hook_trust",
            action="revoke",
            decision="system",
            outcome="started",
            session_id=body.session_id,
            details=audit_details,
            required=True,
        )
    except AuditPersistenceError:
        return _hook_error(
            status_code=503,
            code="hook_audit_unavailable",
            detail="Hook trust was not changed because audit persistence is unavailable",
        )

    try:
        revoked = await asyncio.to_thread(trust.revoke, hook)
    except HookTrustStoreError:
        await record_security_event(
            session_factory,
            source_kind="security_center",
            source_id="desktop",
            invocation_source_kind="desktop",
            capability="hook_trust",
            action="revoke",
            decision="system",
            outcome="error",
            session_id=body.session_id,
            details={**audit_details, "reason": "trust_store_unavailable"},
        )
        return _hook_error(
            status_code=503,
            code="hook_trust_unavailable",
            detail="Hook trust could not be changed safely",
        )

    await record_security_event(
        session_factory,
        source_kind="security_center",
        source_id="desktop",
        invocation_source_kind="desktop",
        capability="hook_trust",
        action="revoke",
        decision="system",
        outcome="success",
        session_id=body.session_id,
        details={**audit_details, "changed": revoked},
    )
    return JSONResponse(
        content={
            "session_id": body.session_id,
            "hook_id": body.hook_id,
            "revoked": revoked,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.put("/tools/{tool_id}")
async def set_security_tool(
    tool_id: str,
    body: ToolToggleBody,
    request: Request,
    db: DbDep,
    session_factory: SessionFactoryDep,
) -> dict[str, Any]:
    if tool_id not in TOGGLEABLE_BUILTIN_TOOLS:
        raise HTTPException(status_code=400, detail="This tool is not independently toggleable")
    registry = request.app.state.tool_registry
    if not any(tool.id == tool_id for tool in registry.registered_tools()):
        raise HTTPException(status_code=404, detail="Tool not found")

    control = request.app.state.security_control
    # Tool-control changes are themselves privileged.  Reserve a durable
    # audit record before mutating either persisted or runtime state.
    await record_security_event(
        session_factory,
        source_kind="security_center",
        source_id="desktop",
        invocation_source_kind="desktop",
        capability="tool_control",
        action="enable" if body.enabled else "disable",
        decision="system",
        outcome="started",
        details={"tool_id": tool_id},
        required=True,
    )
    await control.set_tool_enabled(tool_id, body.enabled)
    registry.set_enabled(tool_id, body.enabled)
    await record_security_event(
        session_factory,
        source_kind="security_center",
        source_id="desktop",
        invocation_source_kind="desktop",
        capability="tool_control",
        action="enable" if body.enabled else "disable",
        decision="system",
        outcome="success",
        details={"tool_id": tool_id},
    )
    return await _build_overview(request, db)


@router.post("/emergency-stop")
async def set_emergency_stop(
    body: EmergencyStopBody,
    request: Request,
    db: DbDep,
    session_factory: SessionFactoryDep,
) -> dict[str, Any]:
    control = request.app.state.security_control
    warnings: list[str] = []
    changed = False

    # Never let an audit outage prevent an emergency activation.  Resuming,
    # however, re-enables external side effects and therefore requires a
    # durable pre-action record.
    if not body.active:
        await record_security_event(
            session_factory,
            source_kind="security_center",
            source_id="desktop",
            invocation_source_kind="desktop",
            capability="emergency_stop",
            action="resume",
            decision="system",
            outcome="started",
            required=True,
        )

    # Persisted state and runtime side effects form one serialized transition.
    # Without this lock, concurrent activate/deactivate requests can leave the
    # last persisted value and the last runtime action pointing in opposite
    # directions.
    async with control.transition_lock:
        try:
            changed = await control.set_emergency_stop(body.active)
        except OSError:
            warnings.append("security_state_not_persisted")

        if body.active:
            try:
                from app.dependencies import get_stream_manager
                from app.session.goal_manager import (
                    prepare_active_goals_for_emergency_stop,
                )

                active_goal_jobs = [
                    job
                    for job in get_stream_manager()._jobs.values()
                    if not job.completed and job.goal_id is not None
                ]
                for job in active_goal_jobs:
                    async with job.execution_admission_lock:
                        job.close_execution_admission()
                        job.deny_pending_responses(source="security_emergency_stop")
                async with session_factory() as goal_db:
                    async with goal_db.begin():
                        await prepare_active_goals_for_emergency_stop(goal_db)
            except Exception as exc:
                # Runtime cancellation still proceeds, but surface that Goal
                # state may require startup recovery before any resume.
                warnings.append(f"goal_emergency_state:{type(exc).__name__}")
            warnings.extend(await _stop_external_runtime(request))
        elif control.emergency_stop:
            # A failed deactivation remains fail-closed and must never resume
            # external runtimes merely because the requested value was false.
            warnings.append("emergency_resume_blocked")
            warnings.extend(await _stop_external_runtime(request))
        else:
            # A caller may be retrying after a previous partial resume.  The
            # startup operations are idempotent, so an explicit inactive
            # request must attempt them even when the persisted state is
            # already false.
            warnings.extend(await _resume_external_runtime(request))

    await record_security_event(
        session_factory,
        source_kind="security_center",
        source_id="desktop",
        invocation_source_kind="desktop",
        capability="emergency_stop",
        action="activate" if body.active else "resume",
        decision="system",
        outcome="success" if not warnings else "error",
        details={"changed": changed, "warning_count": len(warnings)},
    )
    overview = await _build_overview(request, db)
    overview["warnings"] = warnings
    return overview


async def _stop_external_runtime(request: Request) -> list[str]:
    state = request.app.state
    from app.dependencies import get_stream_manager
    from app.api.google_auth import fence_google_auth_disconnect

    warnings: list[str] = []
    try:
        _aborted, quiesced = await get_stream_manager().abort_all_and_wait(timeout=10.0)
        if not quiesced:
            warnings.append("tool_shutdown_timeout")
    except Exception as exc:
        warnings.append(f"active_tools:{type(exc).__name__}")
    try:
        fence_google_auth_disconnect(state.settings.project_dir)
    except Exception as exc:
        warnings.append(f"google_auth:{type(exc).__name__}")

    background = getattr(state, "background_tasks", None)
    if background is not None:
        try:
            await background.cancel_and_wait()
        except Exception as exc:
            warnings.append(f"background_tasks:{type(exc).__name__}")
    provider_registry = getattr(state, "provider_registry", None)
    if provider_registry is not None:
        try:
            await provider_registry.shutdown()
        except Exception as exc:
            warnings.append(f"provider_registry:{type(exc).__name__}")

    operations: list[tuple[str, Any]] = []
    for attribute, method in (
        ("task_scheduler", "stop"),
        ("connector_registry", "shutdown"),
        ("agent_adapter", "stop"),
        ("channel_manager", "stop_all"),
        ("tunnel_manager", "stop"),
        ("ollama_manager", "stop"),
        ("rapid_mlx_manager", "stop"),
    ):
        target = getattr(state, attribute, None)
        operation = getattr(target, method, None) if target is not None else None
        if operation is not None:
            if attribute == "ollama_manager" and not target.is_running:
                continue
            if attribute == "rapid_mlx_manager" and not target.is_managed_process_alive:
                continue
            try:
                operations.append((attribute, operation()))
            except Exception as exc:
                warnings.append(f"{attribute}:{type(exc).__name__}")
    if operations:
        results = await asyncio.gather(
            *(operation for _name, operation in operations),
            return_exceptions=True,
        )
        for (name, _operation), result in zip(operations, results):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, Exception):
                warnings.append(f"{name}:{type(result).__name__}")
    connector_registry = getattr(state, "connector_registry", None)
    if connector_registry is not None:
        try:
            connector_registry.sync_tools()
        except Exception as exc:
            warnings.append(f"connector_tools:{type(exc).__name__}")
    return warnings


async def _resume_external_runtime(request: Request) -> list[str]:
    state = request.app.state
    warnings: list[str] = []
    scheduler = getattr(state, "task_scheduler", None)
    connector_registry = getattr(state, "connector_registry", None)
    if scheduler is not None:
        try:
            await scheduler.start()
        except Exception as exc:
            warnings.append(f"task_scheduler:{type(exc).__name__}")
    if connector_registry is not None:
        try:
            # set_emergency_stop already owns SecurityControl.transition_lock
            # while resuming. Re-entering it here would deadlock.
            await connector_registry.connect_enabled(transition_owned=True)
        except Exception as exc:
            warnings.append(f"connector_registry:{type(exc).__name__}")

    background = getattr(state, "background_tasks", None)
    provider_registry = getattr(state, "provider_registry", None)
    if background is not None and provider_registry is not None:
        from app.main import (
            _initialize_ollama_runtime,
            _initialize_rapid_mlx_runtime,
            _initialize_subscription_provider,
            _start_models_dev_background_refresh,
            _start_remote_tunnel,
        )
        from app.provider.models_dev import models_dev

        try:
            background.create(
                provider_registry.refresh_models(),
                name="security-resume-provider-refresh",
            )
            _start_models_dev_background_refresh(
                models_dev,
                registry=provider_registry,
                task_manager=background,
            )
        except Exception as exc:
            warnings.append(f"provider_refresh:{type(exc).__name__}")
        subscription = provider_registry.get_provider("openai-subscription")
        if subscription is not None:
            try:
                background.create(
                    _initialize_subscription_provider(subscription),
                    name="security-resume-subscription",
                )
            except Exception as exc:
                warnings.append(f"subscription_provider:{type(exc).__name__}")
        if state.settings.ollama_base_url:
            try:
                background.create(
                    _initialize_ollama_runtime(
                        manager=state.ollama_manager,
                        settings=state.settings,
                        registry=provider_registry,
                    ),
                    name="security-resume-ollama",
                )
            except Exception as exc:
                warnings.append(f"ollama_runtime:{type(exc).__name__}")
        if state.settings.rapid_mlx_base_url:
            try:
                background.create(
                    _initialize_rapid_mlx_runtime(
                        manager=state.rapid_mlx_manager,
                        settings=state.settings,
                        registry=provider_registry,
                    ),
                    name="security-resume-rapid-mlx",
                )
            except Exception as exc:
                warnings.append(f"rapid_mlx_runtime:{type(exc).__name__}")
        tunnel = getattr(state, "tunnel_manager", None)
        if tunnel is not None:
            try:
                background.create(
                    _start_remote_tunnel(tunnel),
                    name="security-resume-remote-tunnel",
                )
            except Exception as exc:
                warnings.append(f"remote_tunnel:{type(exc).__name__}")
    return warnings
