"""Tools for inspecting and restoring durable Agent file versions."""

from __future__ import annotations

from typing import Any

from app.storage.file_versions import FileVersionError, FileVersionStore
from app.tool.base import ToolDefinition, ToolResult
from app.tool.context import ToolContext


class FileVersionsTool(ToolDefinition):
    """Read-only listing of pre-mutation snapshots in the active workspace."""

    @property
    def id(self) -> str:
        return "file_versions"

    @property
    def description(self) -> str:
        return (
            "List recoverable versions captured before AI file writes, edits, "
            "patches, deletions, moves, and restores in the current workspace."
        )

    @property
    def is_concurrency_safe(self) -> bool:
        return True

    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Optional file path to filter; omit for all workspace versions",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum versions to return (1-500, default 100)",
                    "default": 100,
                },
            },
        }

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if not ctx.workspace:
            return ToolResult(error="File versions require an active workspace")
        try:
            versions = FileVersionStore(ctx.workspace).list_versions(
                file_path=args.get("file_path"),
                limit=int(args.get("limit", 100)),
            )
        except (FileVersionError, TypeError, ValueError) as exc:
            return ToolResult(error=str(exc))

        if not versions:
            return ToolResult(
                output=ctx.tr("没有可恢复的文件版本。", "No recoverable file versions."),
                title=ctx.tr("文件版本（0）", "File versions (0)"),
                metadata={"versions": []},
            )

        lines = [
            f"{version.id}  {version.created_at}  {version.operation}  "
            f"{version.relative_path}  {version.size} bytes  sha256:{version.sha256}"
            for version in versions
        ]
        return ToolResult(
            output="\n".join(lines),
            title=ctx.tr(
                f"文件版本（{len(versions)}）",
                f"File versions ({len(versions)})",
            ),
            metadata={"versions": [version.public_dict() for version in versions]},
        )


class RestoreFileVersionTool(ToolDefinition):
    """Mutating tool kept separate so permission rules can require approval."""

    @property
    def id(self) -> str:
        return "restore_file_version"

    @property
    def description(self) -> str:
        return (
            "Restore a version returned by file_versions to its original path. "
            "If the current file exists it is snapshotted first, so the restore can be undone."
        )

    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "version_id": {
                    "type": "string",
                    "description": "Version ID returned by file_versions",
                },
            },
            "required": ["version_id"],
        }

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if not ctx.workspace:
            return ToolResult(error="Restoring a file version requires an active workspace")
        try:
            restored, recovery, target = FileVersionStore(ctx.workspace).restore(
                args["version_id"],
                session_id=ctx.session_id,
                message_id=ctx.message_id,
                call_id=ctx.call_id,
            )
        except FileVersionError as exc:
            return ToolResult(error=str(exc))

        recovery_zh = (
            f"；恢复前内容已保存为 {recovery.id}" if recovery is not None else ""
        )
        recovery_en = (
            f"; displaced content saved as {recovery.id}" if recovery is not None else ""
        )
        return ToolResult(
            output=ctx.tr(
                f"已将 {restored.relative_path} 恢复到版本 {restored.id}{recovery_zh}",
                f"Restored {restored.relative_path} to {restored.id}{recovery_en}",
            ),
            title=ctx.tr(
                f"已恢复 {target.name}",
                f"Restored {target.name}",
            ),
            metadata={
                "file_path": str(target),
                "restored_version": restored.public_dict(),
                "recovery_version": (
                    recovery.public_dict() if recovery is not None else None
                ),
            },
        )
