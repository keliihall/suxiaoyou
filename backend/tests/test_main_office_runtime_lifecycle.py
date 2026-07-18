"""Lifecycle boundaries for the process-global Office v1.1 runtime."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.main import _shutdown_runtime
from app.office_templates.user import set_user_office_template_service
from app.office_validation import runtime as office_runtime
from app.office_validation.precommit import (
    get_office_precommit_coordinator,
    set_office_precommit_coordinator,
)
from app.office_validation.runtime import OfficeV11Runtime


class _Coordinator:
    async def begin(self, *, request, view):  # pragma: no cover - never invoked
        raise AssertionError((request, view))


class _BackgroundTasks:
    async def cancel_and_wait(self) -> None:
        return None


class _FinalResource:
    def __init__(self, name: str, calls: list[str]) -> None:
        self._name = name
        self._calls = calls

    async def shutdown(self) -> None:
        self._calls.append(self._name)

    async def dispose(self) -> None:
        self._calls.append(self._name)


@pytest.fixture(autouse=True)
def _reset_office_runtime_globals():
    set_office_precommit_coordinator(None)
    set_user_office_template_service(None)
    yield
    set_office_precommit_coordinator(None)
    set_user_office_template_service(None)


def _installed_state(tmp_path: Path) -> tuple[SimpleNamespace, _Coordinator]:
    coordinator = _Coordinator()
    runtime = OfficeV11Runtime(
        cache=cast(Any, object()),
        provider=cast(Any, object()),
        preview=cast(Any, object()),
        draft=cast(Any, object()),
        coordinator=cast(Any, coordinator),
        data_dir=tmp_path,
    )
    state = SimpleNamespace(
        office_v11_runtime=runtime,
        office_precommit_coordinator=coordinator,
        office_preview_service=runtime.preview,
    )
    set_office_precommit_coordinator(coordinator)
    return state, coordinator


async def _shutdown(*, stream_manager, app_state, calls: list[str]) -> None:
    await _shutdown_runtime(
        background_tasks=_BackgroundTasks(),
        task_scheduler=None,
        stream_manager=stream_manager,
        shutdown_timeout=1.0,
        agent_adapter=None,
        channel_manager=None,
        workspace_memory_queue=None,
        tunnel_manager=None,
        connector_registry=None,
        index_manager=None,
        ollama_manager=None,
        rapid_mlx_manager=None,
        provider_registry=_FinalResource("providers", calls),
        engine=_FinalResource("database", calls),
        app_state=app_state,
    )


@pytest.mark.asyncio
async def test_shutdown_uninstalls_office_only_after_generations_quiesce(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    generations_started = asyncio.Event()
    allow_generations_to_finish = asyncio.Event()
    state, coordinator = _installed_state(tmp_path)

    class Streams:
        async def abort_all_and_wait(self, *, timeout: float) -> tuple[int, bool]:
            assert timeout == 1.0
            assert get_office_precommit_coordinator() is coordinator
            calls.append("generations-start")
            generations_started.set()
            await allow_generations_to_finish.wait()
            calls.append("generations-finished")
            return 1, True

        def set_post_checkpoint_validation_scheduler(self, value) -> None:
            assert value is None
            assert get_office_precommit_coordinator() is None
            calls.append("validation-scheduler")

    shutdown = asyncio.create_task(
        _shutdown(stream_manager=Streams(), app_state=state, calls=calls)
    )
    try:
        await asyncio.wait_for(generations_started.wait(), timeout=1)
        assert get_office_precommit_coordinator() is coordinator
        assert not shutdown.done()
        assert calls == ["generations-start"]

        allow_generations_to_finish.set()
        await asyncio.wait_for(shutdown, timeout=1)
    finally:
        allow_generations_to_finish.set()
        if not shutdown.done():
            shutdown.cancel()
        await asyncio.gather(shutdown, return_exceptions=True)

    assert get_office_precommit_coordinator() is None
    assert not hasattr(state, "office_v11_runtime")
    assert calls == [
        "generations-start",
        "generations-finished",
        "validation-scheduler",
        "providers",
        "database",
    ]


@pytest.mark.asyncio
async def test_office_uninstall_failure_does_not_skip_final_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    state, _coordinator = _installed_state(tmp_path)
    real_uninstall = office_runtime.uninstall_office_v11_runtime

    def failing_uninstall(app_state: object) -> None:
        real_uninstall(app_state)
        calls.append("office-uninstall")
        raise RuntimeError("simulated Office teardown failure")

    monkeypatch.setattr(
        office_runtime,
        "uninstall_office_v11_runtime",
        failing_uninstall,
    )

    await _shutdown(stream_manager=None, app_state=state, calls=calls)

    assert get_office_precommit_coordinator() is None
    assert calls == ["office-uninstall", "providers", "database"]
