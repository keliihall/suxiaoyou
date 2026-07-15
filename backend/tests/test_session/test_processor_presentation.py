"""Tests for deliverable presentation and workspace side-effect boundaries."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.session import processor as processor_module
from app.session.middleware import MiddlewareContext
from app.session.middlewares.factory import build_middleware_chain
from app.session.processor import (
    SessionProcessor,
    _artifact_delivery_paths,
    _presentation_reminder,
)
from app.streaming.manager import GenerationJob


def test_presentation_reminder_for_code_execute_deliverables():
    reminder = _presentation_reminder(
        "code_execute",
        {
            "written_files": [
                "/workspace/suxiaoyou_written/analyze_helper.py",
                "/workspace/suxiaoyou_written/final_report.md",
                "/workspace/suxiaoyou_written/final_summary.csv",
            ]
        },
    )

    assert "present_file" in reminder
    assert "final_report.md" in reminder
    assert "final_summary.csv" in reminder
    assert "analyze_helper.py" not in reminder


def test_presentation_reminder_for_shell_generated_audio():
    reminder = _presentation_reminder(
        "bash",
        {
            "written_files": [
                "/workspace/suxiaoyou_written/generate_audio.py",
                "/workspace/suxiaoyou_written/top20_news.mp3",
            ]
        },
    )

    assert "present_file" in reminder
    assert "top20_news.mp3" in reminder
    assert "generate_audio.py" not in reminder


def test_presentation_reminder_skips_dependency_tree_outputs():
    reminder = _presentation_reminder(
        "bash",
        {
            "written_files": [
                "/workspace/.venv/lib/package/metadata.json",
                "/workspace/suxiaoyou_written/final_audio.mp3",
            ]
        },
    )

    assert "metadata.json" not in reminder
    assert "final_audio.mp3" in reminder


def test_artifact_delivery_paths_supports_plugin_contract_and_deduplicates():
    assert _artifact_delivery_paths(
        "custom_generator",
        {
            "artifact_files": [
                {"path": "/workspace/result.wav"},
                {"file_path": "/workspace/result.mp3"},
            ],
            "written_files": ["/workspace/result.wav"],
        },
    ) == ["/workspace/result.wav", "/workspace/result.mp3"]


def test_presentation_reminder_skips_temp_outputs():
    reminder = _presentation_reminder(
        "write",
        {"file_path": "/workspace/suxiaoyou_written/temp_notes.md"},
    )

    assert reminder == ""


def test_presentation_reminder_skips_non_file_tools():
    reminder = _presentation_reminder(
        "read",
        {"file_path": "/workspace/suxiaoyou_written/final_report.md"},
    )

    assert reminder == ""


def test_image_generation_is_presented_by_its_own_tool_result():
    reminder = _presentation_reminder(
        "image_generate",
        {"file_path": "/workspace/suxiaoyou_written/generated.png"},
    )

    assert reminder == ""


@pytest.mark.asyncio
async def test_visual_artifact_does_not_silently_write_to_workspace(tmp_path):
    """Artifact preview state is not an implicit file-write authorization."""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    prompt = SimpleNamespace(
        job=SimpleNamespace(session_id="session-artifact-boundary"),
        session_factory=object(),
        workspace=str(workspace),
        current_todos=[],
    )
    processor = SessionProcessor(prompt, [], "assistant-message")

    await processor._apply_tool_side_effects(
        SimpleNamespace(id="artifact"),
        SimpleNamespace(
            success=True,
            metadata={
                "type": "markdown",
                "title": "Existing report",
                "content": "model-produced preview",
            },
        ),
    )

    assert list(workspace.rglob("*")) == []


@pytest.mark.asyncio
async def test_shell_generated_files_are_tracked_through_generic_delivery(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    prompt = SimpleNamespace(
        job=SimpleNamespace(session_id="session-shell-artifact"),
        session_factory=object(),
        workspace=str(workspace),
        current_todos=[],
    )
    processor = SessionProcessor(prompt, [], "assistant-message")
    track = AsyncMock()
    monkeypatch.setattr(processor_module, "_track_session_file", track)

    await processor._apply_tool_side_effects(
        SimpleNamespace(id="bash"),
        SimpleNamespace(
            success=True,
            metadata={"written_files": [str(workspace / "news.mp3")]},
        ),
    )

    track.assert_awaited_once_with(
        prompt.session_factory,
        session_id="session-shell-artifact",
        file_path=str(workspace / "news.mp3"),
        tool_id="bash",
    )


@pytest.mark.asyncio
async def test_todo_reminder_is_added_exactly_once_until_todo_state_changes():
    todos = [
        {"id": "todo-1", "content": "Create report", "status": "in_progress"},
        {"id": "todo-2", "content": "Verify report", "status": "pending"},
    ]
    job = GenerationJob("todo-reminder-stream", "todo-reminder-session")
    prompt = SimpleNamespace(
        job=job,
        current_todos=todos,
    )
    prompt.middleware_chain = build_middleware_chain(
        get_todos_fn=lambda: prompt.current_todos,
    )
    processor = SessionProcessor(prompt, [], "assistant-message")
    processor._mw_ctx = MiddlewareContext(
        session_id=job.session_id,
        step=1,
        job=job,
    )
    tool = SimpleNamespace(id="write")
    result = SimpleNamespace(success=True, output="written", error=None, metadata=None)
    loop_result = SimpleNamespace(action="allow", message=None)

    first = await processor._build_tool_persist_output(tool, {}, result, loop_result)
    # A Goal continuation constructs a fresh SessionPrompt/middleware chain but
    # retains the same GenerationJob. The unchanged projection must stay quiet
    # across that slice boundary too.
    prompt.middleware_chain = build_middleware_chain(
        get_todos_fn=lambda: prompt.current_todos,
    )
    next_processor = SessionProcessor(prompt, [], "assistant-message-2")
    next_processor._mw_ctx = MiddlewareContext(
        session_id=job.session_id,
        step=2,
        job=job,
    )
    unchanged = await next_processor._build_tool_persist_output(
        tool, {}, result, loop_result,
    )

    assert first.count("<reminder>You have an active todo list.") == 1
    assert "<reminder>You have an active todo list." not in unchanged

    todos[0]["status"] = "completed"
    changed = await next_processor._build_tool_persist_output(
        tool, {}, result, loop_result,
    )
    assert changed.count("<reminder>You have an active todo list.") == 1
