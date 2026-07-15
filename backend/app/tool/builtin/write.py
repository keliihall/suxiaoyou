"""Write tool — create or overwrite a file."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from app.storage.file_versions import FileVersionStore
from app.tool.base import ToolDefinition, ToolResult
from app.tool.context import ToolContext
from app.tool.file_metadata import (
    UnsupportedFileMetadataError,
    ensure_mutation_metadata_supported,
)
from app.tool.file_versioning import version_metadata
from app.tool.workspace import WorkspaceViolation, resolve_for_write
from app.tool.workspace_transaction import (
    WorkspaceMutationError,
    WorkspaceMutationTransaction,
)
from app.utils.atomic_write import atomic_write_text


class WriteTool(ToolDefinition):

    @property
    def id(self) -> str:
        return "write"

    @property
    def description(self) -> str:
        return (
            "Create a new file or overwrite an existing file with the given content. "
            "Use the artifact tool for self-contained visual artifacts. "
            "After writing a final user-facing file, call present_file to show it."
        )

    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute or relative path to the file to write",
                },
                "content": {
                    "type": "string",
                    "description": "The content to write to the file",
                },
            },
            "required": ["file_path", "content"],
        }

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        file_path = args["file_path"]
        if not ctx.workspace:
            return ToolResult(
                error=ctx.tr(
                    "写入文件需要先选择工作区",
                    "Writing a file requires an active workspace",
                )
            )

        # Workspace restriction check (relative paths default to suxiaoyou_written/)
        try:
            file_path = resolve_for_write(file_path, ctx.workspace)
        except WorkspaceViolation as e:
            return ToolResult(error=str(e))

        content = args["content"]

        try:
            ensure_mutation_metadata_supported(file_path)
            transaction = WorkspaceMutationTransaction(
                ctx.workspace or "",
                ctx,
                operation="write",
            )
            transaction.prepare_paths([file_path])
            staged_path = transaction.staged_path(file_path)
            existed = staged_path.exists()
            staged_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(staged_path, content)
            ensure_mutation_metadata_supported(file_path)
            commit = transaction.commit()

            version_ids = set(commit.previous_version_ids)
            previous_versions = [
                version
                for version in FileVersionStore(Path(ctx.workspace or "")).list_versions()
                if version.id in version_ids
            ]
            previous_version = previous_versions[0] if previous_versions else None

            lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
            action = ctx.tr("已更新", "Updated") if existed else ctx.tr("已创建", "Created")

            return ToolResult(
                output=ctx.tr(
                    f"{action} {file_path}（{lines} 行）",
                    f"{action} {file_path} ({lines} lines)",
                ),
                title=f"{action} {os.path.basename(file_path)}",
                metadata={
                    "file_path": file_path,
                    **commit.metadata,
                    **version_metadata(previous_version),
                },
            )

        except PermissionError:
            return ToolResult(
                error=ctx.tr(
                    f"没有权限写入：{file_path}",
                    f"Permission denied writing: {file_path}",
                )
            )
        except (OSError, UnsupportedFileMetadataError, WorkspaceMutationError) as exc:
            transaction = locals().get("transaction")
            if isinstance(transaction, WorkspaceMutationTransaction):
                transaction.abort()
            return ToolResult(error=str(exc))
