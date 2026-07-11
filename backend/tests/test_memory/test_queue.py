"""Tests for app.memory.workspace_memory_queue — debounced refresh queue."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.memory import workspace_memory_queue as queue_mod
from app.memory.workspace_memory_queue import (
    WorkspaceConversationContext,
    WorkspaceMemoryUpdateQueue,
    get_workspace_memory_queue,
    set_workspace_memory_queue,
)
from app.memory.workspace_memory_storage import get_workspace_memory


@dataclass
class _Chunk:
    type: str
    data: dict[str, Any]


def _stream(chunks):
    async def _gen(*args, **kwargs):
        for c in chunks:
            yield c

    return _gen


class TestWorkspaceConversationContext:
    def test_defaults(self):
        ctx = WorkspaceConversationContext(
            session_id="s1", workspace_path="/proj", messages=[]
        )
        assert ctx.model_id is None
        assert isinstance(ctx.timestamp, float)


class TestModuleSingleton:
    def test_set_and_get(self):
        original = get_workspace_memory_queue()
        try:
            sentinel = object()
            set_workspace_memory_queue(sentinel)  # type: ignore[arg-type]
            assert get_workspace_memory_queue() is sentinel
        finally:
            queue_mod._queue = original


class TestAddAndClear:
    @pytest.mark.asyncio
    async def test_add_populates_pending_keyed_by_workspace(self, session_factory):
        q = WorkspaceMemoryUpdateQueue(session_factory, MagicMock(), debounce_seconds=60)
        q.add("s1", "/proj", [{"role": "user", "content": "hi"}], model_id="m1")
        assert set(q._pending) == {"/proj"}
        ctx = q._pending["/proj"]
        assert ctx.session_id == "s1"
        assert ctx.model_id == "m1"
        q.clear()

    @pytest.mark.asyncio
    async def test_add_same_workspace_replaces(self, session_factory):
        q = WorkspaceMemoryUpdateQueue(session_factory, MagicMock(), debounce_seconds=60)
        q.add("s1", "/proj", [{"role": "user", "content": "a"}])
        q.add("s2", "/proj", [{"role": "user", "content": "b"}])
        assert len(q._pending) == 1
        assert q._pending["/proj"].session_id == "s2"
        q.clear()

    @pytest.mark.asyncio
    async def test_add_collapses_path_variants_to_one_canonical_key(
        self, session_factory
    ):
        q = WorkspaceMemoryUpdateQueue(session_factory, MagicMock(), debounce_seconds=60)
        q.add("s1", "/home/me/proj", [{"role": "user", "content": "a"}])
        q.add("s2", "/home/me/./proj/", [{"role": "user", "content": "b"}])
        q.add(
            "s3",
            "/home/me/other/../proj",
            [{"role": "user", "content": "c"}],
        )

        assert set(q._pending) == {"/home/me/proj"}
        ctx = q._pending["/home/me/proj"]
        assert ctx.workspace_path == "/home/me/proj"
        assert ctx.session_id == "s3"
        q.clear()

    @pytest.mark.asyncio
    async def test_add_collapses_unc_share_variants_to_one_canonical_key(
        self, session_factory
    ):
        q = WorkspaceMemoryUpdateQueue(session_factory, MagicMock(), debounce_seconds=60)
        variants = [
            "//server/share",
            "//server/share/",
            "//server/share/.",
            "//server/share/folder/..",
            "//server/share/..",
        ]

        try:
            for index, path in enumerate(variants):
                q.add(
                    f"s{index}",
                    path,
                    [{"role": "user", "content": str(index)}],
                )

            assert set(q._pending) == {"//server/share"}
            ctx = q._pending["//server/share"]
            assert ctx.workspace_path == "//server/share"
            assert ctx.session_id == "s4"
        finally:
            q.clear()

    @pytest.mark.asyncio
    async def test_add_schedules_timer(self, session_factory):
        q = WorkspaceMemoryUpdateQueue(session_factory, MagicMock(), debounce_seconds=60)
        q.add("s1", "/proj", [{"role": "user", "content": "a"}])
        assert q._timer is not None
        q.clear()

    @pytest.mark.asyncio
    async def test_clear_empties_pending_and_cancels_timer(self, session_factory):
        q = WorkspaceMemoryUpdateQueue(session_factory, MagicMock(), debounce_seconds=60)
        q.add("s1", "/proj", [{"role": "user", "content": "a"}])
        q.clear()
        assert q._pending == {}
        assert q._timer is None


class TestProcess:
    @pytest.mark.asyncio
    async def test_process_no_pending_noop(self, session_factory):
        q = WorkspaceMemoryUpdateQueue(session_factory, MagicMock())
        q._refresh_workspace_memory = AsyncMock()
        await q._process()
        q._refresh_workspace_memory.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_refreshes_each_and_clears(self, session_factory):
        q = WorkspaceMemoryUpdateQueue(session_factory, MagicMock())
        q._refresh_workspace_memory = AsyncMock()
        q.add("s1", "/a", [{"role": "user", "content": "x"}])
        q.add("s2", "/b", [{"role": "user", "content": "y"}])
        await q._process()
        assert q._refresh_workspace_memory.await_count == 2
        assert q._pending == {}

    @pytest.mark.asyncio
    async def test_process_swallows_refresh_errors(self, session_factory):
        q = WorkspaceMemoryUpdateQueue(session_factory, MagicMock())
        q._refresh_workspace_memory = AsyncMock(side_effect=RuntimeError("boom"))
        q.add("s1", "/a", [{"role": "user", "content": "x"}])
        await q._process()  # should not raise
        assert q._pending == {}

    @pytest.mark.asyncio
    async def test_callback_during_active_processing_is_retried_after_release(
        self, session_factory
    ):
        q = WorkspaceMemoryUpdateQueue(
            session_factory, MagicMock(), debounce_seconds=0
        )
        first_refresh_started = asyncio.Event()
        release_first_refresh = asyncio.Event()
        overlapping_callback_started = asyncio.Event()
        second_refresh_finished = asyncio.Event()
        retry_process_finished = asyncio.Event()
        live_process_tasks = set()

        async def refresh(ctx):
            if ctx.workspace_path == "/first":
                first_refresh_started.set()
                await release_first_refresh.wait()
            elif ctx.workspace_path == "/second":
                second_refresh_finished.set()

        original_process = q._process

        async def observe_process(*args, **kwargs):
            task = asyncio.current_task()
            assert task is not None
            live_process_tasks.add(task)
            if q._processing:
                overlapping_callback_started.set()
            try:
                await original_process(*args, **kwargs)
            finally:
                live_process_tasks.discard(task)
                if second_refresh_finished.is_set() and not q._processing:
                    retry_process_finished.set()

        q._refresh_workspace_memory = refresh
        q._process = observe_process

        try:
            q.add("s1", "/first", [{"role": "user", "content": "x"}])
            await asyncio.wait_for(first_refresh_started.wait(), timeout=1)

            q.add("s2", "/second", [{"role": "user", "content": "y"}])
            await asyncio.wait_for(overlapping_callback_started.wait(), timeout=1)
            release_first_refresh.set()

            await asyncio.wait_for(second_refresh_finished.wait(), timeout=1)
            await asyncio.wait_for(retry_process_finished.wait(), timeout=1)
            assert q._pending == {}
            assert q._processing is False
            assert q._timer is None
            assert live_process_tasks == set()
        finally:
            release_first_refresh.set()
            q.clear()

    @pytest.mark.asyncio
    async def test_stale_timer_task_does_not_process_newer_pending_work(
        self, session_factory
    ):
        q = WorkspaceMemoryUpdateQueue(
            session_factory, MagicMock(), debounce_seconds=60
        )
        refreshed_paths = []

        async def refresh(ctx):
            refreshed_paths.append(ctx.workspace_path)

        q._refresh_workspace_memory = refresh

        try:
            q.add("old", "/old", [{"role": "user", "content": "old"}])
            h1 = q._timer
            assert h1 is not None

            tasks_before_h1 = asyncio.all_tasks()
            h1._run()
            h1_tasks = asyncio.all_tasks() - tasks_before_h1
            assert len(h1_tasks) == 1
            h1_task = h1_tasks.pop()
            assert not h1_task.done()

            q.clear()

            q.add("new", "/new", [{"role": "user", "content": "new"}])
            h2 = q._timer
            assert h2 is not None
            assert h2 is not h1

            await asyncio.wait_for(h1_task, timeout=1)
            assert refreshed_paths == []
            assert set(q._pending) == {"/new"}

            tasks_before_h2 = asyncio.all_tasks()
            h2._run()
            h2_tasks = asyncio.all_tasks() - tasks_before_h2
            assert len(h2_tasks) == 1
            h2_task = h2_tasks.pop()
            await asyncio.wait_for(h2_task, timeout=1)

            assert refreshed_paths == ["/new"]
            assert q._pending == {}
            assert q._processing is False
        finally:
            q.clear()

    @pytest.mark.asyncio
    async def test_shutdown_cancels_inflight_refresh(self, session_factory):
        q = WorkspaceMemoryUpdateQueue(
            session_factory,
            MagicMock(),
            debounce_seconds=0,
        )
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def refresh(_ctx):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        q._refresh_workspace_memory = refresh
        q.add("s1", "/project", [{"role": "user", "content": "remember"}])
        await asyncio.wait_for(started.wait(), timeout=1)

        await asyncio.wait_for(q.shutdown(), timeout=1)

        assert cancelled.is_set()
        assert q._timer is None
        assert q._pending == {}
        assert q._process_tasks == set()
        assert q._processing is False


class TestRefreshWorkspaceMemory:
    @pytest.mark.asyncio
    async def test_skips_when_no_conversation_text(self, session_factory):
        q = WorkspaceMemoryUpdateQueue(session_factory, MagicMock())
        q._call_llm = AsyncMock()
        ctx = WorkspaceConversationContext(
            session_id="s1",
            workspace_path="/proj",
            messages=[{"role": "tool", "content": "ignored"}],
        )
        await q._refresh_workspace_memory(ctx)
        q._call_llm.assert_not_called()

    @pytest.mark.asyncio
    async def test_saves_parsed_memory(self, session_factory):
        q = WorkspaceMemoryUpdateQueue(session_factory, MagicMock())
        q._call_llm = AsyncMock(return_value=("new memory content", {}, None))
        q._persist_usage = AsyncMock()
        ctx = WorkspaceConversationContext(
            session_id="s1",
            workspace_path="/proj",
            messages=[{"role": "user", "content": "remember this"}],
        )
        await q._refresh_workspace_memory(ctx)
        assert await get_workspace_memory(session_factory, "/proj") == "new memory content"

    @pytest.mark.asyncio
    async def test_empty_llm_response_does_not_save(self, session_factory):
        q = WorkspaceMemoryUpdateQueue(session_factory, MagicMock())
        q._call_llm = AsyncMock(return_value=(None, {}, None))
        ctx = WorkspaceConversationContext(
            session_id="s1",
            workspace_path="/proj",
            messages=[{"role": "user", "content": "hi"}],
        )
        await q._refresh_workspace_memory(ctx)
        assert await get_workspace_memory(session_factory, "/proj") is None

    @pytest.mark.asyncio
    async def test_persist_usage_uses_effective_fallback_model(self, session_factory):
        q = WorkspaceMemoryUpdateQueue(session_factory, MagicMock())
        usage = {"total": 5}
        q._call_llm = AsyncMock(return_value=("content", usage, "auto-model"))
        q._persist_usage = AsyncMock()
        ctx = WorkspaceConversationContext(
            session_id="s1",
            workspace_path="/proj",
            messages=[{"role": "user", "content": "hi"}],
        )
        await q._refresh_workspace_memory(ctx)
        q._persist_usage.assert_awaited_once_with("s1", usage, "auto-model")


class TestCallLlm:
    expected_system = (
        "You are a workspace memory manager. "
        "Maintain a concise plain-text document capturing important "
        "workspace context. Respond with pure plain text only. "
        "Never use Markdown syntax (no #, **, *, `, ```, >, []()])."
    )

    @pytest.mark.asyncio
    async def test_no_model_available_returns_none(self, session_factory):
        registry = MagicMock()
        registry.all_models.return_value = []
        q = WorkspaceMemoryUpdateQueue(session_factory, registry)
        text, usage, effective_model = await q._call_llm("prompt")
        assert text is None
        assert usage == {}
        assert effective_model is None

    @pytest.mark.asyncio
    async def test_unresolvable_model_returns_none(self, session_factory):
        registry = MagicMock()
        registry.resolve_model.return_value = None
        q = WorkspaceMemoryUpdateQueue(session_factory, registry)
        text, usage, effective_model = await q._call_llm("prompt", model_id="m1")
        assert text is None
        assert usage == {}
        assert effective_model == "m1"

    @pytest.mark.asyncio
    async def test_falls_back_to_first_model(self, session_factory):
        provider = MagicMock()
        provider.stream_chat = MagicMock(
            side_effect=_stream([_Chunk("text-delta", {"text": "hello"})])
        )
        registry = MagicMock()
        first_model = MagicMock(id="auto-model")
        registry.all_models.return_value = [first_model]
        registry.resolve_model.return_value = (provider, MagicMock())
        q = WorkspaceMemoryUpdateQueue(session_factory, registry)
        text, usage, effective_model = await q._call_llm("prompt")
        assert text == "hello"
        assert usage == {}
        assert effective_model == "auto-model"
        registry.resolve_model.assert_called_with("auto-model")
        provider.stream_chat.assert_called_once_with(
            "auto-model",
            [{"role": "user", "content": "prompt"}],
            system=self.expected_system,
            max_tokens=4000,
        )

    @pytest.mark.asyncio
    async def test_streams_text_and_usage(self, session_factory):
        provider = MagicMock()
        provider.stream_chat = MagicMock(
            side_effect=_stream([
                _Chunk("text-delta", {"text": "foo "}),
                _Chunk("text-delta", {"text": "bar"}),
                _Chunk("usage", {"total": 42}),
            ])
        )
        registry = MagicMock()
        registry.resolve_model.return_value = (provider, MagicMock())
        q = WorkspaceMemoryUpdateQueue(session_factory, registry)
        text, usage, effective_model = await q._call_llm("prompt", model_id="m1")
        assert text == "foo bar"
        assert usage == {"total": 42}
        assert effective_model == "m1"
        provider.stream_chat.assert_called_once_with(
            "m1",
            [{"role": "user", "content": "prompt"}],
            system=self.expected_system,
            max_tokens=4000,
        )

    @pytest.mark.asyncio
    async def test_whitespace_only_response_returns_none(self, session_factory):
        provider = MagicMock()
        provider.stream_chat = _stream([_Chunk("text-delta", {"text": "   "})])
        registry = MagicMock()
        registry.resolve_model.return_value = (provider, MagicMock())
        q = WorkspaceMemoryUpdateQueue(session_factory, registry)
        text, _, effective_model = await q._call_llm("prompt", model_id="m1")
        assert text is None
        assert effective_model == "m1"

    @pytest.mark.asyncio
    async def test_provider_exception_returns_none(self, session_factory):
        def _boom(*args, **kwargs):
            raise RuntimeError("stream failed")

        provider = MagicMock()
        provider.stream_chat = _boom
        registry = MagicMock()
        registry.resolve_model.return_value = (provider, MagicMock())
        q = WorkspaceMemoryUpdateQueue(session_factory, registry)
        text, usage, effective_model = await q._call_llm("prompt", model_id="m1")
        assert text is None
        assert usage == {}
        assert effective_model == "m1"


class TestPersistUsage:
    @pytest.mark.asyncio
    async def test_persists_synthetic_assistant_message(self, session_factory):
        from app.models.session import Session
        from app.models.message import Message
        from sqlalchemy import select

        async with session_factory() as db:
            async with db.begin():
                db.add(Session(id="sess1", title="t"))

        registry = MagicMock()
        registry.resolve_model.return_value = (MagicMock(id="prov1"), MagicMock(pricing=None))
        q = WorkspaceMemoryUpdateQueue(session_factory, registry)

        await q._persist_usage("sess1", {"total": 10}, "m1")

        async with session_factory() as db:
            rows = (await db.execute(select(Message))).scalars().all()
        assert len(rows) == 1
        assert rows[0].data["agent"] == "memory"
        assert rows[0].data["system"] is True

    @pytest.mark.asyncio
    async def test_swallows_errors(self, session_factory):
        registry = MagicMock()
        registry.resolve_model.side_effect = RuntimeError("boom")
        q = WorkspaceMemoryUpdateQueue(session_factory, registry)
        # Should not raise
        await q._persist_usage("sess1", {"total": 10}, "m1")
