"""Release-gated ACP stdio process owner."""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any, Awaitable, Callable

from app.acp.server import AcpFeatureDisabled, acp_runtime_enabled


logger = logging.getLogger(__name__)


class AcpDatabaseBoundaryError(RuntimeError):
    """ACP requires the desktop's single-process file-backed SQLite lease."""


def _require_file_backed_sqlite(application: Any) -> None:
    """Reject deployments whose session admission spans unleased processes."""

    settings = getattr(getattr(application, "state", None), "settings", None)
    database_url = getattr(settings, "database_url", None)
    if not isinstance(database_url, str):
        raise AcpDatabaseBoundaryError("ACP database configuration is unavailable")
    from app.storage.migrations import sqlite_file_path

    try:
        database_path = sqlite_file_path(database_url)
    except Exception as exc:
        raise AcpDatabaseBoundaryError("ACP database configuration is invalid") from exc
    if database_path is None:
        raise AcpDatabaseBoundaryError(
            "ACP requires a file-backed SQLite database with an exclusive process lease"
        )


async def run_initialized_acp(
    *,
    app_factory: Callable[[], Any] | None = None,
    bridge_factory: Callable[[], Any] | None = None,
    stdio_runner: Callable[[Any], Awaitable[None]] | None = None,
) -> None:
    """Start normal application dependencies, then own exactly one stdio link."""

    if not acp_runtime_enabled():
        # Check before database migration, provider discovery, credential
        # access, or either process pipe is opened.
        raise AcpFeatureDisabled("ACP stdio is disabled by the v1.1 release gate")
    if app_factory is None:
        from app.main import create_app

        app_factory = create_app
    if bridge_factory is None:
        from app.acp.session_bridge import ProductionSessionPromptBridge

        bridge_factory = ProductionSessionPromptBridge.from_app_dependencies
    if stdio_runner is None:
        from app.acp.stdio import run_stdio

        stdio_runner = run_stdio

    application = app_factory()
    _require_file_backed_sqlite(application)
    lifespan = getattr(getattr(application, "router", None), "lifespan_context", None)
    if not callable(lifespan):
        raise RuntimeError("ACP application lifespan is unavailable")
    async with lifespan(application):
        # The bridge pulls the initialized DB/registries/StreamManager and the
        # persistent emergency-stop guard from the same process state as the
        # desktop server.  No alternate provider or tool path is constructed.
        bridge = bridge_factory()
        await stdio_runner(bridge)


def main() -> None:
    """Console entry point. Stdout remains reserved for ACP NDJSON."""

    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    try:
        asyncio.run(run_initialized_acp())
    except AcpFeatureDisabled:
        logger.error("ACP stdio is disabled in this build")
        raise SystemExit(78) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as exc:
        # Never print exception text: startup failures may embed private paths
        # or provider/credential diagnostics. Detailed logs remain local.
        logger.error("ACP stdio failed to start (%s)", type(exc).__name__)
        raise SystemExit(1) from None


__all__ = ["AcpDatabaseBoundaryError", "main", "run_initialized_acp"]
