from __future__ import annotations

from pathlib import Path

import pytest

from app.schemas.agent import AgentInfo
from app.session.managed_workspace import (
    ManagedInputError,
    managed_workspace_for_session,
    snapshot_attachments,
)
from app.session.prompt import _uses_managed_workspace
from app.tool.builtin.write import WriteTool
from app.tool.context import ToolContext


def test_folderless_attachment_is_copied_into_session_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed_root = tmp_path / "managed"
    source_dir = tmp_path / "QuickRecorder"
    source_dir.mkdir()
    source = source_dir / "recording.m4a"
    source.write_bytes(b"original audio")
    monkeypatch.setenv("SUXIAOYOU_MANAGED_WORKSPACE_ROOT", str(managed_root))

    [attachment] = snapshot_attachments(
        "session-1",
        [{"file_id": "file-1", "name": source.name, "path": str(source)}],
    )

    copied = Path(attachment["path"])
    assert copied.parent == managed_root / "session-1" / "inputs"
    assert copied.read_bytes() == b"original audio"
    assert attachment["original_path"] == str(source)
    assert attachment["source"] == "managed"

    copied.write_bytes(b"changed snapshot")
    assert source.read_bytes() == b"original audio"
    assert (managed_root / "session-1" / "suxiaoyou_written").is_dir()


def test_managed_session_id_cannot_escape_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SUXIAOYOU_MANAGED_WORKSPACE_ROOT", str(tmp_path))
    workspace = managed_workspace_for_session("../../outside")
    assert workspace.parent == tmp_path
    assert workspace.name == "outside"


def test_directory_attachment_with_symlink_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "secret.txt"
    target.write_text("secret", encoding="utf-8")
    (source / "escape").symlink_to(target)
    monkeypatch.setenv(
        "SUXIAOYOU_MANAGED_WORKSPACE_ROOT", str(tmp_path / "managed")
    )

    with pytest.raises(ManagedInputError, match="symbolic links"):
        snapshot_attachments(
            "session-1",
            [{"file_id": "dir-1", "name": "source", "path": str(source)}],
        )


def test_attachment_batch_limit_is_checked_before_any_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_a = tmp_path / "a.bin"
    source_b = tmp_path / "b.bin"
    source_a.write_bytes(b"123456")
    source_b.write_bytes(b"abcdef")
    managed_root = tmp_path / "managed"
    monkeypatch.setenv("SUXIAOYOU_MANAGED_WORKSPACE_ROOT", str(managed_root))
    monkeypatch.setattr(
        "app.session.managed_workspace._DEFAULT_MAX_INPUT_BYTES",
        10,
    )

    with pytest.raises(ManagedInputError, match="in total"):
        snapshot_attachments(
            "session-1",
            [
                {"file_id": "a", "name": "a.bin", "path": str(source_a)},
                {"file_id": "b", "name": "b.bin", "path": str(source_b)},
            ],
        )

    assert list((managed_root / "session-1" / "inputs").iterdir()) == []


def test_existing_folderless_session_ignores_stale_global_workspace() -> None:
    assert _uses_managed_workspace(".", "/previous/project") is True
    assert _uses_managed_workspace(None, "/explicit/new/project") is False


@pytest.mark.asyncio
async def test_folderless_relative_write_never_touches_attachment_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_dir = tmp_path / "Recorder"
    source_dir.mkdir()
    recording = source_dir / "meeting.m4a"
    recording.write_bytes(b"audio")
    managed_root = tmp_path / "managed"
    monkeypatch.setenv("SUXIAOYOU_MANAGED_WORKSPACE_ROOT", str(managed_root))

    [attachment] = snapshot_attachments(
        "session-1",
        [{"file_id": "recording-1", "name": recording.name, "path": str(recording)}],
    )
    workspace = managed_workspace_for_session("session-1")
    result = await WriteTool().execute(
        {"file_path": "meeting-notes.md", "content": "# Minutes\n"},
        ToolContext(
            session_id="session-1",
            message_id="message-1",
            agent=AgentInfo(name="build", description="", mode="primary"),
            call_id="write-1",
            workspace=str(workspace),
        ),
    )

    assert result.success
    assert Path(attachment["path"]).parent == workspace / "inputs"
    assert (workspace / "suxiaoyou_written" / "meeting-notes.md").read_text(
        encoding="utf-8"
    ) == "# Minutes\n"
    assert list(source_dir.iterdir()) == [recording]
