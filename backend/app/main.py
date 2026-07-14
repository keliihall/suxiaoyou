"""FastAPI application factory and lifespan."""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from functools import wraps
from pathlib import Path
from typing import AsyncGenerator, Awaitable, Callable

import httpx

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import router as health_router
from app.api.openai_compat import router as openai_compat_router
from app.api.router import api_router
from app.auth.csrf import CsrfProtectionMiddleware
from app.auth.middleware import AuthMiddleware
from app.auth.private_network import PrivateNetworkAccessMiddleware
from app.auth.token import ensure_session_token
from app.config import Settings
from app.errors import register_error_handlers
from app.dependencies import (
    get_index_manager,
    get_stream_manager,
    set_agent_registry,
    set_connector_registry,
    set_index_manager,
    set_plugin_manager,
    set_provider_registry,
    set_session_factory,
    set_settings,
    set_skill_registry,
    set_stream_manager,
    set_tool_registry,
)
from app.models.base import Base
from app.agent.agent import AgentRegistry
from app.provider.local import create_local_provider
from app.provider.registry import ProviderRegistry
from app.skill.registry import SkillRegistry
from app.storage.database import create_engine, create_session_factory
from app.tool.registry import ToolRegistry

logger = logging.getLogger(__name__)


class _BackgroundTaskManager:
    """Own startup jobs so failures are observed and shutdown is bounded."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[None]] = set()

    def create(self, awaitable: Awaitable[object], *, name: str) -> asyncio.Task[None]:
        async def _run() -> None:
            try:
                await awaitable
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Background startup job failed: %s", name)

        task = asyncio.create_task(_run(), name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def cancel_and_wait(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()


def _asyncio_exception_handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    """Custom handler for unhandled asyncio exceptions.

    Prevents silent swallowing of errors from fire-and-forget coroutines.
    """
    exc = context.get("exception")
    msg = context.get("message", "Unhandled asyncio exception")
    if exc:
        logger.error("%s: %s", msg, exc, exc_info=exc)
    else:
        logger.error("%s: %s", msg, context)


async def _models_dev_background_refresh_loop(
    service: object,
    *,
    interval_seconds: float,
    registry: ProviderRegistry | None = None,
) -> None:
    """Refresh models.dev immediately, then hourly, without gating startup."""
    while True:
        try:
            refreshed = await service.refresh()  # type: ignore[attr-defined]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "models.dev background refresh failed: %s — using cached/hardcoded data",
                exc,
            )
        else:
            if refreshed and registry is not None:
                try:
                    await registry.refresh_models()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "Provider refresh after models.dev update failed: %s",
                        exc,
                    )
        await asyncio.sleep(interval_seconds)


def _start_models_dev_background_refresh(
    service: object,
    *,
    interval_seconds: float = 3600,
    registry: ProviderRegistry | None = None,
    task_manager: _BackgroundTaskManager | None = None,
) -> asyncio.Task[None]:
    """Schedule models.dev refresh and return immediately to the caller."""
    awaitable = _models_dev_background_refresh_loop(
        service,
        interval_seconds=interval_seconds,
        registry=registry,
    )
    if task_manager is not None:
        return task_manager.create(
            awaitable,
            name="models-dev-background-refresh",
        )
    return asyncio.create_task(awaitable, name="models-dev-background-refresh")


async def _initialize_subscription_provider(provider: object) -> None:
    """Refresh OAuth after liveness is available."""
    try:
        await provider._ensure_valid_token()  # type: ignore[attr-defined]
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "Background subscription token refresh failed: %s — re-authorization may be required",
            exc,
        )


async def _initialize_ollama_runtime(
    *,
    manager: object,
    settings: Settings,
    registry: ProviderRegistry,
) -> None:
    """Auto-start and discover Ollama without gating application startup."""
    from app.provider.ollama import OllamaProvider

    base_url = settings.ollama_base_url
    if manager.is_binary_installed and settings.ollama_auto_start:  # type: ignore[attr-defined]
        try:
            base_url = await manager.start()  # type: ignore[attr-defined]
            settings.ollama_base_url = base_url
            registry.register(
                OllamaProvider(
                    base_url=base_url,
                    seed_model=settings.ollama_last_model,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Failed to auto-start managed Ollama: %s — trying configured URL",
                exc,
            )

    await registry.refresh_provider("ollama")

    last_model = settings.ollama_last_model
    if last_model:
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                await client.post(
                    f"{base_url.rstrip('/')}/api/generate",
                    json={"model": last_model, "prompt": "", "keep_alive": "10m"},
                )
            logger.info("Ollama: pre-warmed model %s", last_model)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("Ollama warmup skipped for %s: %s", last_model, exc)


async def _initialize_rapid_mlx_runtime(
    *,
    manager: object,
    settings: Settings,
    registry: ProviderRegistry,
) -> None:
    """Auto-start and discover Rapid-MLX without gating liveness."""
    from app.provider.rapid_mlx import RapidMLXProvider

    if (
        manager.platform_supported  # type: ignore[attr-defined]
        and manager.is_binary_installed  # type: ignore[attr-defined]
        and settings.rapid_mlx_auto_start
    ):
        try:
            from app.rapid_mlx.manager import DEFAULT_PORT, _port_from_base_url

            port = _port_from_base_url(settings.rapid_mlx_base_url) or DEFAULT_PORT
            settings.rapid_mlx_base_url = await manager.start(  # type: ignore[attr-defined]
                model=settings.rapid_mlx_model,
                port=port,
            )
            registry.register(
                RapidMLXProvider(
                    base_url=settings.rapid_mlx_base_url,
                    seed_model=settings.rapid_mlx_model,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Failed to auto-start Rapid-MLX: %s — trying configured URL",
                exc,
            )

    await registry.refresh_provider("rapid-mlx")


async def _start_remote_tunnel(tunnel_manager: object) -> None:
    """Start the optional network tunnel after the app is live."""
    try:
        tunnel_url = await tunnel_manager.start()  # type: ignore[attr-defined]
        logger.info("Remote access tunnel: %s", tunnel_url)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "Failed to start remote tunnel in background: %s — remote access disabled",
            exc,
        )


async def _rebuild_upload_hash_index(
    rebuild: Callable[..., None],
) -> None:
    """Run the upload scan off-loop and cooperatively stop it on shutdown."""
    import threading

    cancel_event = threading.Event()
    worker = asyncio.create_task(
        asyncio.to_thread(rebuild, cancel_event=cancel_event),
        name="upload-hash-index-worker",
    )
    try:
        await asyncio.shield(worker)
    except asyncio.CancelledError:
        cancel_event.set()
        await asyncio.gather(worker, return_exceptions=True)
        raise


async def _maintain_upload_store(
    rebuild: Callable[..., None],
    session_factory,
    upload_dir: Path,
) -> None:
    """Rebuild dedup state, collect committed orphans, then resync the index."""
    from app.session.upload_gc import collect_orphan_uploads

    await _rebuild_upload_hash_index(rebuild)
    deleted = await collect_orphan_uploads(session_factory, upload_dir)
    if deleted:
        await _rebuild_upload_hash_index(rebuild)


async def _stop_active_generation_jobs(stream_manager, timeout: float) -> None:
    if stream_manager is None:
        return
    aborted = stream_manager.abort_all()
    if aborted:
        logger.info(
            "Shutdown: aborted %d active generation job(s), waiting up to %.1fs",
            aborted,
            timeout,
        )
    current = asyncio.current_task()
    tasks = {
        job.task
        for job in stream_manager._jobs.values()
        if job.task is not None
        and job.task is not current
        and not job.task.done()
    }
    if not tasks:
        return
    _done, pending = await asyncio.wait(tasks, timeout=timeout)
    for task in pending:
        task.cancel()
    if pending:
        logger.warning(
            "Shutdown: force-cancelled %d lingering task(s)",
            len(pending),
        )
        await asyncio.gather(*pending, return_exceptions=True)


async def _shutdown_runtime(
    *,
    background_tasks,
    task_scheduler,
    stream_manager,
    shutdown_timeout: float,
    agent_adapter,
    channel_manager,
    workspace_memory_queue,
    tunnel_manager,
    connector_registry,
    index_manager,
    ollama_manager,
    rapid_mlx_manager,
    provider_registry,
    engine,
) -> None:
    """Stop producers/consumers before their provider and database resources."""

    pending_cancel: asyncio.CancelledError | None = None

    async def step(name: str, operation: Callable[[], Awaitable[object]]) -> None:
        nonlocal pending_cancel
        try:
            await operation()
        except asyncio.CancelledError as exc:
            # Finish the remaining teardown before honoring cancellation; an
            # interrupted cleanup must not leave providers or SQLite open.
            pending_cancel = pending_cancel or exc
            logger.warning("Shutdown step was cancelled: %s", name)
        except Exception:
            logger.exception("Shutdown step failed: %s", name)

    await step("startup background tasks", background_tasks.cancel_and_wait)
    if task_scheduler is not None:
        await step("task scheduler", task_scheduler.stop)
    await step(
        "active generations",
        lambda: _stop_active_generation_jobs(stream_manager, shutdown_timeout),
    )
    if agent_adapter is not None:
        await step("channel agent adapter", agent_adapter.stop)
    if channel_manager is not None:
        await step("channels", channel_manager.stop_all)
    if workspace_memory_queue is not None:
        shutdown = getattr(workspace_memory_queue, "shutdown", None)
        if shutdown is not None:
            await step("workspace memory queue", shutdown)
        else:
            workspace_memory_queue.clear()
    if tunnel_manager is not None:
        await step("remote tunnel", tunnel_manager.stop)
    if connector_registry is not None:
        await step("connector registry", connector_registry.shutdown)
    if index_manager is not None:
        await step("index manager", index_manager.shutdown)
    if ollama_manager is not None and ollama_manager.is_running:
        await step("Ollama runtime", ollama_manager.stop)
    if rapid_mlx_manager is not None and rapid_mlx_manager.is_managed_process_alive:
        await step("Rapid-MLX runtime", rapid_mlx_manager.stop)

    # Providers can be in use by every component above; the DB must outlive all
    # status/recovery writes.  These are intentionally the final two steps.
    await step("provider registry", provider_registry.shutdown)
    await step("database engine", engine.dispose)

    if pending_cancel is not None:
        raise pending_cancel


def _hold_database_lease_for_lifespan(factory):
    """Keep one app process attached to a file-backed database at a time."""

    @asynccontextmanager
    @wraps(factory)
    async def leased_lifespan(app: FastAPI):
        from app.storage.migrations import database_lease

        settings: Settings = app.state.settings
        with database_lease(settings.database_url) as lease:
            app.state.database_lease = lease
            try:
                async with factory(app):
                    yield
            finally:
                try:
                    # Normal shutdown already disposes this after every DB
                    # consumer. The idempotent fallback also covers startup
                    # failures after engine creation but before the inner
                    # lifespan reaches its yield/finally block.
                    engine = getattr(app.state, "engine", None)
                    if engine is not None:
                        await engine.dispose()
                finally:
                    # Release only after the final engine handle is closed.
                    # This closes the check/replace race for every cooperating
                    # backend and offline recovery process.
                    delattr(app.state, "database_lease")

    return leased_lifespan


@_hold_database_lease_for_lifespan
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan: startup and shutdown."""
    settings: Settings = app.state.settings

    from app.auth.credential_store import (
        get_credential_store,
        migrate_settings_credentials,
    )
    from app.auth.legacy_credentials import (
        migrate_historical_workspace_credentials,
        migrate_legacy_credential_artifacts,
    )

    from app.release_features import (
        MESSAGING_CHANNELS_RELEASED,
        REMOTE_ACCESS_RELEASED,
    )

    # Stale per-user configuration from an older build must not reopen either
    # high-privilege ingress while its code-owned release gate is closed.
    if not REMOTE_ACCESS_RELEASED:
        settings.remote_access_enabled = False
        settings.remote_permission_mode = "deny"

    # Rewrite app-owned plaintext credentials to opaque references before any
    # provider, connector, or channel sees them. Runtime settings remain
    # hydrated so existing consumers do not need storage-specific knowledge.
    migrate_settings_credentials(settings, Path(".env"))

    # Secure v0.8 credential artifacts even while their Remote/Channels
    # consumers are behind code-owned release gates. Unsupported runtime state
    # is retained behind a recoverable opaque reference; any migration failure
    # aborts startup instead of leaving plaintext silently stranded on disk.
    credential_store = get_credential_store()
    migrate_legacy_credential_artifacts(
        data_root=Path.cwd(),
        # The pre-database pass always covers app-private artifacts and the
        # historical global ~/.suxiaoyou location. Workspace paths are sourced
        # from the migrated database below instead of trusting only the current
        # Settings.project_dir value.
        project_dir=None,
        include_global_legacy=True,
        remote_token_path=settings.remote_token_path,
        store=credential_store,
    )

    # --- Startup ---

    # Configure app-level logging so logger.info() from app modules is visible
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s:     %(name)s - %(message)s",
    )

    # Install global asyncio exception handler
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(_asyncio_exception_handler)

    background_tasks = _BackgroundTaskManager()
    app.state.background_tasks = background_tasks

    # Session token — generate fresh on every startup, 0600 file so a
    # different local user on the same host cannot read it. Stored on
    # app.state so the AuthMiddleware can validate requests against it.
    dev_session_token = (
        settings.dev_session_token if settings.allow_dev_session_token else ""
    )
    app.state.session_token = ensure_session_token(
        Path(settings.session_token_path),
        token=dev_session_token or None,
    )

    # Runtime CSRF allowlist — mutated by remote-access handlers when a
    # cloudflared tunnel URL is acquired/released. Separate from the
    # static SUXIAOYOU_EXTRA_ALLOWED_ORIGINS override because quick-tunnel
    # URLs are random per session and cannot be known at env-load time.
    # The CsrfProtectionMiddleware snapshots this set on every request.
    app.state.runtime_allowed_origins = set()
    # If the user had remote access enabled and configured a manual
    # tunnel URL, seed the set now so mobile requests are accepted on
    # the first request after startup.
    if REMOTE_ACCESS_RELEASED and settings.remote_access_enabled and settings.remote_tunnel_mode == "manual" and settings.remote_tunnel_url:
        app.state.runtime_allowed_origins.add(settings.remote_tunnel_url.rstrip("/"))

    # Database.  File-backed desktop SQLite stores are upgraded in a staging
    # copy and atomically installed only after Alembic and quick_check pass.
    # Run the synchronous SQLite backup/migration work off the event loop, but
    # keep startup gated on it so no request can observe a half-upgraded schema.
    from app.storage.migrations import upgrade_sqlite_database

    migration_result = await asyncio.to_thread(
        upgrade_sqlite_database,
        settings.database_url,
        lease=app.state.database_lease,
    )
    engine = create_engine(settings)
    app.state.engine = engine
    session_factory = create_session_factory(engine)
    app.state.session_factory = session_factory
    set_session_factory(session_factory)

    if migration_result is None:
        # In-memory SQLite is used by tests and cannot be migrated through a
        # second file connection.  It is always empty, so create_all is an
        # initializer rather than the removed guess-based upgrade mechanism.
        import app.models as _models  # noqa: F401 — registers all ORM models
        from app.memory import workspace_memory_model as _ws_memory_models  # noqa: F401

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    # v0.8 Google/MCP tokens were stored inside the workspace selected by each
    # exclude folderless/private-overlap entries, and inspect only the two
    # known <workspace>/.suxiaoyou credential files. This remains independent
    # of connector and messaging feature gates.
    await migrate_historical_workspace_credentials(
        session_factory,
        configured_project_dir=settings.project_dir,
        private_data_root=Path.cwd(),
        store=credential_store,
    )

    # A process can exit after an input is durably claimed but before it is
    # safe to say whether its tools ran.  Never auto-replay that ambiguous
    # instruction on restart: surface it as blocked so the user can review and
    # explicitly retry or cancel it.
    from app.session.idempotency import interrupt_inflight_idempotency_records
    from app.session.input_queue import block_interrupted_inputs
    from app.session.recovery import interrupt_inflight_tool_parts

    async with session_factory() as recovery_db:
        async with recovery_db.begin():
            blocked_inputs = await block_interrupted_inputs(recovery_db)
            interrupted_requests = await interrupt_inflight_idempotency_records(
                recovery_db
            )
            interrupted_tools = await interrupt_inflight_tool_parts(recovery_db)
    if blocked_inputs:
        logger.warning(
            "Recovered %d interrupted queued input(s) as blocked",
            blocked_inputs,
        )
    if interrupted_requests:
        logger.warning(
            "Recovered %d in-flight request(s) as interrupted",
            interrupted_requests,
        )
    if interrupted_tools:
        logger.warning(
            "Recovered %d in-flight tool call(s) as interrupted",
            interrupted_tools,
        )

    # Provider registry
    registry = ProviderRegistry()

    # Provider model discovery consults models.dev. Prime it from disk only so
    # those startup paths use cached/hardcoded metadata and never wait for the
    # network before /livez is available. A force-refresh starts after yield.
    from app.provider.models_dev import models_dev

    models_dev.load_cached_for_startup()

    # Register OpenAI subscription provider from local state only.  Token
    # refresh is a background concern and must never delay /livez.
    sub_provider = None
    if settings.openai_oauth_access_token and settings.openai_oauth_account_id:
        from app.provider.openai_subscription import OpenAISubscriptionProvider

        sub_provider = OpenAISubscriptionProvider(
            access_token=settings.openai_oauth_access_token,
            account_id=settings.openai_oauth_account_id,
            refresh_token=settings.openai_oauth_refresh_token,
            expires_at_ms=settings.openai_oauth_expires_at,
            settings=settings,
        )
        registry.register(sub_provider)
        logger.info("OpenAI subscription provider registered from saved tokens")

    # Ollama runtime manager (always created — manages binary + process)
    from app.ollama.manager import OllamaManager

    data_dir = Path.cwd()  # Desktop mode: run.py sets cwd to the data directory
    ollama_manager = OllamaManager(data_dir=data_dir)
    app.state.ollama_manager = ollama_manager

    # Register the configured Ollama endpoint immediately.  Managed process
    # startup and discovery happen in a tracked background task after yield.
    if settings.ollama_base_url:
        from app.provider.ollama import OllamaProvider

        ollama_provider = OllamaProvider(
            base_url=settings.ollama_base_url,
            seed_model=settings.ollama_last_model,
        )
        registry.register(ollama_provider)

    # Rapid-MLX runtime manager (Apple Silicon macOS, user-installed via brew/pip)
    from app.rapid_mlx.manager import RapidMLXManager

    rapid_mlx_manager = RapidMLXManager(data_dir=data_dir)
    app.state.rapid_mlx_manager = rapid_mlx_manager

    if settings.rapid_mlx_base_url:
        from app.provider.rapid_mlx import RapidMLXProvider, normalize_rapid_mlx_model

        normalized_rapid_mlx_model = normalize_rapid_mlx_model(settings.rapid_mlx_model)
        if normalized_rapid_mlx_model != settings.rapid_mlx_model:
            from app.api.config import _update_env_file

            _update_env_file("SUXIAOYOU_RAPID_MLX_MODEL", normalized_rapid_mlx_model)
        settings.rapid_mlx_model = normalized_rapid_mlx_model

        rapid_mlx_provider = RapidMLXProvider(
            base_url=settings.rapid_mlx_base_url,
            seed_model=settings.rapid_mlx_model,
        )
        registry.register(rapid_mlx_provider)

    # Auto-register BYOK providers (OpenAI, Anthropic, Gemini, Groq, etc.)
    from app.provider.catalog import PROVIDER_CATALOG
    from app.provider.factory import create_provider as create_desktop_provider

    disabled = {s.strip() for s in settings.disabled_providers.split(",") if s.strip()}

    for pid, pdef in PROVIDER_CATALOG.items():
        if pid in disabled:
            continue
        api_key = getattr(settings, pdef.settings_key, "")
        if not api_key:
            continue
        try:
            # Azure needs a user-provided base_url
            extra_kwargs: dict[str, str] = {}
            if pdef.kind == "openai_compat_azure":
                azure_url = getattr(settings, "azure_openai_base_url", "")
                if not azure_url:
                    logger.warning("Azure API key set but SUXIAOYOU_AZURE_OPENAI_BASE_URL is missing — skipping")
                    continue
                extra_kwargs["base_url"] = azure_url

            provider = create_desktop_provider(pid, api_key, **extra_kwargs)
            registry.register(provider)
            logger.info("Registered BYOK provider: %s", pid)
        except Exception as e:
            logger.warning("Failed to register provider %s: %s", pid, e)

    # Auto-register custom endpoints
    from app.config import get_custom_endpoints
    for ce in get_custom_endpoints(settings):
        if not ce.get("enabled", True):
            continue
        try:
            pid = ce["id"]
            if pid in disabled:
                continue
            provider = create_desktop_provider(
                pid,
                ce.get("api_key", ""),
                base_url=ce.get("base_url"),
                models_override=ce.get("models") or None,
                extra_headers=ce.get("headers") or None,
            )
            registry.register(provider)
            logger.info("Registered custom provider: %s (%s)", pid, ce.get("name"))
        except Exception as e:
            logger.warning("Failed to register custom provider %s: %s", ce.get("id"), e)

    if settings.local_base_url:
        try:
            local_provider = create_local_provider(settings.local_base_url)
            registry.register(local_provider)
            logger.info("Registered local provider at %s", settings.local_base_url)
        except Exception as e:
            logger.warning("Failed to register local provider %s: %s", settings.local_base_url, e)

    # Prime a usable model index from cached/bundled/user-declared metadata.
    # This path is synchronous and guaranteed not to touch the network.
    registry.seed_registered_models()

    app.state.provider_registry = registry
    set_provider_registry(registry)

    # Agent registry (built-in + custom agents from config / .suxiaoyou/agents/*.md)
    agent_registry = AgentRegistry()
    agent_registry.load_custom_agents(settings.agents, settings.project_dir)
    app.state.agent_registry = agent_registry
    set_agent_registry(agent_registry)

    # Skill registry
    bundled_skills_dir = Path(__file__).parent / "data" / "skills"
    skill_registry = SkillRegistry(bundled_dir=bundled_skills_dir, project_dir=settings.project_dir)
    skill_registry.scan(project_dir=settings.project_dir)
    app.state.skill_registry = skill_registry
    set_skill_registry(skill_registry)

    # Connector registry (manages deduplicated MCP connections)
    from app.connector.registry import ConnectorRegistry

    connector_registry = ConnectorRegistry(project_dir=settings.project_dir)

    # Plugin loader (Claude knowledge-work-plugins → 苏小有 registries)
    from app.plugin import load_plugins_by_source
    from app.plugin.manager import PluginManager

    plugin_manager = PluginManager(
        skill_registry=skill_registry,
        project_dir=settings.project_dir,
    )

    for source, plugin_result in load_plugins_by_source(settings.project_dir):
        # Register skills + agents into their registries
        for skill in plugin_result.skills:
            skill_registry.register(skill)
        for agent in plugin_result.agents:
            agent_registry.register(agent)
        for err in plugin_result.errors:
            logger.warning("Plugin: %s", err)

        # Extract connectors from plugins into ConnectorRegistry (dedup)
        connector_ids_by_plugin: dict[str, list[str]] = {}
        for plugin_name, mcp_servers in plugin_result.mcp_by_plugin.items():
            cids = connector_registry.register_from_plugin(plugin_name, mcp_servers)
            connector_ids_by_plugin[plugin_name] = cids

        # Track in plugin manager (handles disable state)
        plugin_manager.register_loaded(
            plugin_result, source, plugin_result.meta_map,
            connector_ids_by_plugin=connector_ids_by_plugin,
        )

    app.state.plugin_manager = plugin_manager
    set_plugin_manager(plugin_manager)
    if plugin_manager.status():
        logger.info("Plugin manager: %d plugins loaded", len(plugin_manager.status()))

    # Build connector/MCP state locally.  Enabled connections are opened in a
    # tracked background job after yield so a slow server cannot block /livez.
    connector_registry.prepare()
    app.state.connector_registry = connector_registry
    set_connector_registry(connector_registry)
    # Backward compat: expose mcp_manager for any code that still uses it
    app.state.mcp_manager = connector_registry.mcp_manager

    # Tool registry (tools registered in Step 6)
    tool_registry = ToolRegistry()
    _register_builtin_tools(tool_registry, skill_registry=skill_registry, settings=settings)

    # Bind before background connection. ``connect_enabled`` calls sync_tools
    # after discovery, so tools appear incrementally without restarting.
    connector_registry.set_tool_registry(tool_registry)
    connector_registry.sync_tools()

    app.state.tool_registry = tool_registry
    set_tool_registry(tool_registry)

    # Clean up stale tool output files (from truncation overflow, 7-day retention)
    from app.tool.truncation import cleanup_old_outputs
    cleanup_old_outputs(workspace=settings.project_dir)

    # Prepare the callable now; the potentially large scan runs after yield.
    from app.api.files import UPLOAD_DIR, rebuild_hash_index

    # Construct the optional tunnel manager locally, but defer its network
    # startup until after the app can answer /livez.
    tunnel_mgr = None
    if REMOTE_ACCESS_RELEASED and settings.remote_access_enabled and settings.remote_tunnel_mode == "cloudflare":
        from app.api.remote import get_or_create_tunnel_manager

        tunnel_mgr = get_or_create_tunnel_manager(app)

    # Built-in FTS5 search (enabled by default)
    if settings.fts_enabled:
        from app.fts import IndexManager
        set_index_manager(IndexManager())
        logger.info("FTS5 search enabled")

    # Task scheduler (cron + interval automations)
    from app.scheduler.engine import TaskScheduler
    task_scheduler = TaskScheduler(session_factory, app.state)
    await task_scheduler.start()
    app.state.task_scheduler = task_scheduler

    # Messaging channels feed an unattended Agent and are not release-ready.
    # Do not even construct the consumer/adapter until both the code-owned gate
    # and the user setting are enabled in a future reviewed release.
    app.state.message_bus = None
    app.state.channel_manager = None
    app.state.agent_adapter = None
    if MESSAGING_CHANNELS_RELEASED and settings.channels_enabled:
        from app.channels.bus.queue import MessageBus
        from app.channels.config import load_channels_config
        from app.channels.manager import ChannelManager
        from app.channels.adapter import AgentAdapter

        message_bus = MessageBus()
        channels_config = load_channels_config(data_dir / "channels.json")
        channel_manager = ChannelManager(channels_config, message_bus)
        channel_manager.init_channels()

        agent_adapter = AgentAdapter(message_bus, app.state)
        await agent_adapter.start()
        await channel_manager.start_all()

        app.state.message_bus = message_bus
        app.state.channel_manager = channel_manager
        app.state.agent_adapter = agent_adapter

    # Workspace memory queue (async, debounced refresh)
    from app.memory.workspace_memory_queue import (
        WorkspaceMemoryUpdateQueue,
        set_workspace_memory_queue,
    )
    from app.memory.config import get_memory_config as _get_mem_cfg

    _mem_cfg = _get_mem_cfg()
    ws_memory_queue = WorkspaceMemoryUpdateQueue(
        session_factory,
        registry,
        debounce_seconds=_mem_cfg.debounce_seconds,
    )
    set_workspace_memory_queue(ws_memory_queue)
    app.state.ws_memory_queue = ws_memory_queue

    # Every network/process-bound provider startup action is tracked and
    # scheduled immediately before yield.  The registry already contains its
    # local seed, so the UI has a usable snapshot while these jobs run.
    background_tasks.create(
        connector_registry.connect_enabled(),
        name="mcp-connect-enabled",
    )
    background_tasks.create(
        _maintain_upload_store(rebuild_hash_index, session_factory, UPLOAD_DIR),
        name="upload-store-maintenance",
    )
    background_tasks.create(
        registry.refresh_models(),
        name="provider-initial-model-refresh",
    )
    _start_models_dev_background_refresh(
        models_dev,
        registry=registry,
        task_manager=background_tasks,
    )
    if sub_provider is not None:
        background_tasks.create(
            _initialize_subscription_provider(sub_provider),
            name="subscription-token-refresh",
        )
    if settings.ollama_base_url:
        background_tasks.create(
            _initialize_ollama_runtime(
                manager=ollama_manager,
                settings=settings,
                registry=registry,
            ),
            name="ollama-startup",
        )
    if settings.rapid_mlx_base_url:
        background_tasks.create(
            _initialize_rapid_mlx_runtime(
                manager=rapid_mlx_manager,
                settings=settings,
                registry=registry,
            ),
            name="rapid-mlx-startup",
        )
    if tunnel_mgr is not None:
        background_tasks.create(
            _start_remote_tunnel(tunnel_mgr),
            name="remote-tunnel-startup",
        )

    try:
        yield
    finally:
        # Stop producers and active work before closing the services they use.
        # Keeping the entire sequence inside ``finally`` also guarantees it runs
        # when the ASGI server injects an exception at the lifespan yield.
        await _shutdown_runtime(
            background_tasks=background_tasks,
            task_scheduler=getattr(app.state, "task_scheduler", None),
            stream_manager=get_stream_manager(),
            shutdown_timeout=settings.shutdown_timeout,
            agent_adapter=getattr(app.state, "agent_adapter", None),
            channel_manager=getattr(app.state, "channel_manager", None),
            workspace_memory_queue=getattr(app.state, "ws_memory_queue", None),
            tunnel_manager=getattr(app.state, "tunnel_manager", None),
            connector_registry=getattr(app.state, "connector_registry", None),
            index_manager=get_index_manager(),
            ollama_manager=getattr(app.state, "ollama_manager", None),
            rapid_mlx_manager=getattr(app.state, "rapid_mlx_manager", None),
            provider_registry=registry,
            engine=engine,
        )


def _register_builtin_tools(
    registry: ToolRegistry,
    *,
    skill_registry: SkillRegistry | None = None,
    settings: Settings | None = None,
) -> None:
    """Register all built-in tools."""
    from app.tool.builtin.apply_patch import ApplyPatchTool
    from app.tool.builtin.artifact import ArtifactTool
    from app.tool.builtin.edit import EditTool
    from app.tool.builtin.glob_tool import GlobTool
    from app.tool.builtin.grep import GrepTool
    from app.tool.builtin.invalid import InvalidTool
    from app.tool.builtin.plan import PlanTool
    from app.tool.builtin.present_file import PresentFileTool
    from app.tool.builtin.question import QuestionTool
    from app.tool.builtin.submit_plan import SubmitPlanTool
    from app.tool.builtin.read import ReadTool
    from app.tool.builtin.skill import SkillTool
    from app.tool.builtin.task import TaskTool
    from app.tool.builtin.todo import TodoTool
    from app.tool.builtin.web_fetch import WebFetchTool
    from app.tool.builtin.web_search import WebSearchTool
    from app.tool.builtin.write import WriteTool

    tool_classes = [
        ReadTool, WriteTool, EditTool, ApplyPatchTool,
        GlobTool, GrepTool, QuestionTool, TodoTool,
        TaskTool, WebFetchTool, WebSearchTool, InvalidTool,
        PlanTool, SubmitPlanTool, ArtifactTool, PresentFileTool,
    ]
    if sys.platform.startswith("linux"):
        # Only bubblewrap provides the complete filesystem/network/PID lifetime
        # boundary required by command and Python execution in v0.9. Do not
        # advertise tools that can only fail on macOS or Windows.
        from app.tool.builtin.bash import BashTool
        from app.tool.builtin.code_execute import CodeExecuteTool

        tool_classes.extend((BashTool, CodeExecuteTool))

    for tool_cls in tool_classes:
        registry.register(tool_cls())

    # SkillTool needs the skill registry injected
    registry.register(SkillTool(skill_registry=skill_registry))

    if settings is not None and settings.fts_enabled:
        from app.tool.builtin.search import SearchTool
        registry.register(SearchTool())


def _find_frontend_dir() -> Path | None:
    """Locate the frontend static build (out/) directory.

    Searches common locations relative to the backend package.
    Returns None if not found (dev mode without static build).
    """
    candidates = [
        Path(__file__).parent.parent.parent / "frontend" / "out",  # monorepo: backend/../frontend/out
        Path(__file__).parent.parent / "frontend_out",  # bundled: backend/frontend_out
    ]
    for p in candidates:
        if p.is_dir() and (p / "index.html").exists():
            return p
    return None


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and return the FastAPI application."""
    if settings is None:
        settings = Settings()

    app = FastAPI(
        title="suyo",
        version="0.0.1",
        lifespan=lifespan,
    )
    app.state.settings = settings
    set_settings(settings)

    # CORS — restricted to the 苏小有 frontend origins. Wildcard would let
    # any webpage read responses from this local server cross-origin, which
    # is a PII-leak vector on top of the CSRF risk handled below.
    #   - Tauri desktop shell: tauri://localhost (macOS/Linux) and
    #     http(s)://tauri.localhost (Windows).
    #   - Loopback on any port: http://localhost:*, http://127.0.0.1:*
    #     (the backend picks a random free port; the Next.js dev server uses
    #     a user-configurable port).
    extra_origins = [
        o.strip().rstrip("/")
        for o in settings.extra_allowed_origins.split(",")
        if o.strip()
    ]
    allowed_origin_regex = (
        r"^(?:tauri://localhost"
        r"|https?://tauri\.localhost"
        r"|http://localhost(?::\d+)?"
        r"|http://127\.0\.0\.1(?::\d+)?)$"
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=extra_origins,
        allow_origin_regex=allowed_origin_regex,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["Content-Disposition"],
    )

    # Chromium/WebView2 Private Network Access preflights need an explicit
    # opt-in header for trusted app-origin → loopback requests.
    app.add_middleware(
        PrivateNetworkAccessMiddleware,
        extra_allowed_origins=extra_origins,
    )

    # CSRF — rejects cross-site state-changing requests at the server before
    # they reach any handler. This is the authoritative defense: CORS only
    # controls browser-side response reads, not whether the request lands.
    app.add_middleware(
        CsrfProtectionMiddleware,
        extra_allowed_origins=extra_origins,
    )

    # Bearer-token auth — must be added AFTER CORS/CSRF so it is the
    # outermost layer (Starlette runs last-added first). That way the
    # token check short-circuits before CORS/CSRF and protects every
    # privileged endpoint regardless of which interface the request
    # arrived on.
    app.add_middleware(AuthMiddleware)

    # DomainError → JSONResponse. Registered before routers so handlers
    # raised from any subsequently mounted endpoint are mapped consistently.
    register_error_handlers(app)

    # Mount routers
    app.include_router(health_router)
    app.include_router(api_router, prefix="/api")
    app.include_router(openai_compat_router, tags=["openai-compat"])

    # Serve frontend static files for remote access (phone browser).
    # In desktop mode, Tauri serves the frontend — this is only needed
    # when the phone accesses the backend directly via the tunnel.
    frontend_dir = _find_frontend_dir()
    if frontend_dir:
        from starlette.staticfiles import StaticFiles
        from starlette.responses import FileResponse

        # Mount _next/ static assets (JS, CSS, etc.)
        next_dir = frontend_dir / "_next"
        if next_dir.is_dir():
            app.mount("/_next", StaticFiles(directory=str(next_dir)), name="next-static")

        # Serve static files at root (favicon, manifest, etc.)
        @app.get("/favicon.svg")
        @app.get("/manifest.json")
        async def serve_root_static(request: Request):
            filename = request.url.path.lstrip("/")
            file_path = frontend_dir / filename
            if file_path.exists():
                return FileResponse(str(file_path))
            return FileResponse(str(frontend_dir / "404.html"), status_code=404)

        # SPA catch-all: serve the correct HTML for known routes.
        # Must be AFTER /api and /_next mounts to avoid conflicts.
        # Using StarletteRequest to prevent FastAPI from parsing query params.
        @app.get("/m")
        @app.get("/m/{rest:path}")
        async def serve_mobile_spa(request: Request):
            """Serve mobile PWA pages — SPA fallback to the appropriate HTML."""
            path = request.url.path.rstrip("/")
            # Try exact HTML file first (e.g. /m/settings → m/settings.html)
            html_file = frontend_dir / (path.lstrip("/") + ".html")
            if html_file.exists():
                return FileResponse(str(html_file))
            # Dynamic routes: /m/task/xxx → m/task/_.html (Next.js static export pattern)
            if "/task/" in path:
                task_html = frontend_dir / "m" / "task" / "_.html"
                if task_html.exists():
                    return FileResponse(str(task_html))
            # Fallback to /m index
            m_html = frontend_dir / "m.html"
            if m_html.exists():
                return FileResponse(str(m_html))
            return FileResponse(str(frontend_dir / "404.html"), status_code=404)

        logger.info("Frontend static files served from %s", frontend_dir)

    return app


# Default instance for `uvicorn app.main:app`
app = create_app()
