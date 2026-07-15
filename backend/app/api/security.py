"""Security Center Lite overview, controls, and redacted audit history."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, select

from app.auth.local import require_local_session
from app.config import get_custom_endpoints
from app.dependencies import DbDep, SessionFactoryDep
from app.models.scheduled_task import ScheduledTask
from app.models.security_audit_event import SecurityAuditEvent
from app.provider.catalog import PROVIDER_CATALOG
from app.release_features import (
    AUTONOMOUS_GOALS_RELEASED,
    GOALS_RELEASED,
    MESSAGING_CHANNELS_RELEASED,
    REMOTE_ACCESS_RELEASED,
)
from app.i18n import request_language
from app.security.audit import record_security_event
from app.security.capabilities import describe_tool_source, source_capability_profiles
from app.security.control import TOGGLEABLE_BUILTIN_TOOLS

router = APIRouter(
    prefix="/security",
    dependencies=[Depends(require_local_session)],
)


class ToolToggleBody(BaseModel):
    enabled: bool


class EmergencyStopBody(BaseModel):
    active: bool


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
            "remote_access": REMOTE_ACCESS_RELEASED,
            "messaging_channels": MESSAGING_CHANNELS_RELEASED,
            "goals": GOALS_RELEASED,
            "autonomous_goals": AUTONOMOUS_GOALS_RELEASED,
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
