"""Glue between Agent tool contexts and durable file-version storage."""

from __future__ import annotations

from app.storage.file_versions import FileVersion, FileVersionStore
from app.tool.context import ToolContext


def capture_before_mutation(
    file_path: str,
    ctx: ToolContext,
    *,
    operation: str,
) -> FileVersion | None:
    """Capture a recoverable version when the tool has a workspace boundary.

    Production sessions, including folderless conversations, always receive a
    selected or managed workspace.  Keeping the ``None`` case as a no-op
    preserves direct embedding/tests that intentionally use the legacy
    unrestricted tool context; such contexts cannot safely name a workspace
    whose history they would later be authorized to restore.
    """

    if not ctx.workspace:
        return None
    store = FileVersionStore(ctx.workspace)
    return store.capture_before_mutation(
        file_path,
        operation=operation,
        session_id=ctx.session_id,
        message_id=ctx.message_id,
        call_id=ctx.call_id,
    )


def version_metadata(version: FileVersion | None) -> dict[str, str | int]:
    if version is None:
        return {}
    return {
        "previous_version_id": version.id,
        "previous_sha256": version.sha256,
        "previous_size": version.size,
    }
