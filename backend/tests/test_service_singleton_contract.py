"""Regression coverage for ADR-0008 service singleton access."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import dependencies
from app.streaming.events import SSEEvent, TEXT_DELTA
from app.streaming.manager import GenerationJob


_MANAGER_NAMES = frozenset({"index_manager", "stream_manager"})


def _is_state_or_app_state(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "state"
    ) or (
        isinstance(node, ast.Name)
        and node.id == "app_state"
    )


class _LegacyManagerAccessVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.violations: list[tuple[int, str]] = []

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in _MANAGER_NAMES and _is_state_or_app_state(node.value):
            self.violations.append((node.lineno, node.attr))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and _is_state_or_app_state(node.args[0])
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in _MANAGER_NAMES
        ):
            self.violations.append((node.lineno, str(node.args[1].value)))
        self.generic_visit(node)


def test_runtime_code_has_no_legacy_manager_access() -> None:
    """Prevent partial regressions back to FastAPI-local manager state."""
    app_root = Path(__file__).parents[1] / "app"
    violations: list[str] = []

    for path in app_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = _LegacyManagerAccessVisitor()
        visitor.visit(tree)
        violations.extend(
            f"{path.relative_to(app_root)}:{line}: {manager}"
            for line, manager in visitor.violations
        )

    assert violations == []


@pytest.mark.asyncio
async def test_fts_routes_use_registered_singleton_not_app_state(
    app_client,
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "note.txt"
    source.write_text("hello", encoding="utf-8")

    manager = SimpleNamespace(
        _dbs={"workspace": object()},
        _sessions={"session": object()},
        ensure_index=AsyncMock(),
        ingest_file=AsyncMock(),
    )
    monkeypatch.setattr(dependencies, "_index_manager", manager)
    # A decoy catches accidental reintroduction of request.app.state reads.
    app_client.app.state.index_manager = SimpleNamespace(_dbs={}, _sessions={})

    status = await app_client.get("/api/fts/status")
    assert status.status_code == 200
    assert status.json() == {
        "enabled": True,
        "active_workspaces": 1,
        "active_sessions": 1,
    }

    ingested = await app_client.post(
        "/api/files/ingest",
        json={
            "session_id": "session",
            "workspace": str(tmp_path),
            "paths": [str(source)],
        },
    )
    assert ingested.status_code == 200
    assert ingested.json()["ingested"] == 1
    manager.ensure_index.assert_awaited_once_with(str(tmp_path), "session")
    manager.ingest_file.assert_awaited_once_with(str(tmp_path), str(source))


def test_scheduler_output_uses_registered_stream_singleton(monkeypatch) -> None:
    from app.scheduler.executor import _extract_session_output

    job = GenerationJob(stream_id="stream", session_id="session")
    job.publish(SSEEvent(TEXT_DELTA, {"text": "first "}))
    job.publish(SSEEvent(TEXT_DELTA, {"text": "second"}))
    manager = SimpleNamespace(_jobs={job.stream_id: job})
    monkeypatch.setattr(dependencies, "_stream_manager", manager)

    assert _extract_session_output("session") == "first second"


@pytest.mark.asyncio
async def test_remote_task_routes_use_registered_stream_singleton(
    app_client,
    monkeypatch,
) -> None:
    job = GenerationJob(stream_id="stream", session_id="session")
    manager = SimpleNamespace(
        _jobs={job.stream_id: job},
        active_jobs=lambda: [
            {
                "stream_id": "stream",
                "session_id": "session",
                "needs_input": True,
            }
        ],
    )
    monkeypatch.setattr(dependencies, "_stream_manager", manager)
    app_client.app.state.stream_manager = SimpleNamespace(
        _jobs={},
        active_jobs=lambda: [],
    )

    status = await app_client.get("/api/remote/status")
    assert status.status_code == 200
    assert status.json()["active_tasks"] == 1

    tasks = await app_client.get("/api/remote/tasks")
    assert tasks.status_code == 200
    assert tasks.json() == [
        {
            "stream_id": "stream",
            "session_id": "session",
            "status": "waiting_permission",
        }
    ]
