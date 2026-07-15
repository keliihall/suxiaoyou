"""Edit tool — precise string replacement in files, single or batch.

Supports two calling modes:
  - Single edit: provide old_string + new_string at top level
  - Batch edit: provide an edits array for multiple sequential replacements
    (all succeed atomically, or none are applied)
"""

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
from app.utils.diff import generate_unified_diff


class EditTool(ToolDefinition):

    @property
    def id(self) -> str:
        return "edit"

    @property
    def description(self) -> str:
        return (
            "Make precise edits to a file by replacing exact string matches. "
            "Two modes: (1) Single edit — provide old_string and new_string at top level. "
            "(2) Batch edit — provide an edits array for multiple sequential replacements "
            "(all succeed or none are applied). Use replace_all=true to replace all occurrences."
        )

    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the file to edit",
                },
                "old_string": {
                    "type": "string",
                    "description": "The exact string to find and replace (single edit mode)",
                },
                "new_string": {
                    "type": "string",
                    "description": "The replacement string (single edit mode)",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all occurrences (default: false, single edit mode)",
                    "default": False,
                },
                "edits": {
                    "type": "array",
                    "description": "Ordered list of edits for batch mode (mutually exclusive with old_string/new_string)",
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_string": {
                                "type": "string",
                                "description": "The exact string to find and replace",
                            },
                            "new_string": {
                                "type": "string",
                                "description": "The replacement string",
                            },
                            "replace_all": {
                                "type": "boolean",
                                "description": "Replace all occurrences (default: false)",
                                "default": False,
                            },
                        },
                        "required": ["old_string", "new_string"],
                    },
                },
            },
            "required": ["file_path"],
        }

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        file_path = args["file_path"]
        has_single = "old_string" in args
        has_batch = "edits" in args

        # Determine mode
        if has_single and has_batch:
            return ToolResult(
                error="Provide either old_string/new_string (single edit) or edits array (batch edit), not both."
            )
        if not has_single and not has_batch:
            return ToolResult(
                error="Provide old_string and new_string for a single edit, or an edits array for batch edits."
            )

        # Normalize to a list of edits
        if has_single:
            if "new_string" not in args:
                return ToolResult(error="new_string is required for single edit mode")
            edits = [{
                "old_string": args["old_string"],
                "new_string": args["new_string"],
                "replace_all": args.get("replace_all", False),
            }]
        else:
            edits = args["edits"]
            if not edits:
                return ToolResult(error="No edits provided")

        if not ctx.workspace:
            return ToolResult(
                error=ctx.tr(
                    "编辑文件需要先选择工作区",
                    "Editing a file requires an active workspace",
                )
            )

        # Workspace restriction
        try:
            file_path = resolve_for_write(file_path, ctx.workspace)
        except WorkspaceViolation as e:
            return ToolResult(error=str(e))

        try:
            ensure_mutation_metadata_supported(file_path)
            transaction = WorkspaceMutationTransaction(
                ctx.workspace or "",
                ctx,
                operation="edit",
            )
            transaction.prepare_paths([file_path])
            staged_path = transaction.staged_path(file_path)
            if not staged_path.exists() or staged_path.is_dir():
                transaction.abort()
                return ToolResult(error=f"File not found: {file_path}")
            original = staged_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            transaction.abort()
            return ToolResult(error=f"Cannot edit binary file: {file_path}")
        except (OSError, UnsupportedFileMetadataError, WorkspaceMutationError) as exc:
            transaction = locals().get("transaction")
            if isinstance(transaction, WorkspaceMutationTransaction):
                transaction.abort()
            return ToolResult(error=str(exc))

        # Apply edits sequentially, validating each before proceeding
        content = original
        total_replacements = 0
        for i, edit in enumerate(edits, 1):
            old_string = edit["old_string"]
            new_string = edit["new_string"]
            replace_all = edit.get("replace_all", False)

            prefix = f"Edit {i}: " if len(edits) > 1 else ""

            if old_string == new_string:
                transaction.abort()
                return ToolResult(
                    error=f"{prefix}old_string and new_string are identical"
                )

            count = content.count(old_string)
            if count == 0:
                suffix = (
                    " (may have been modified by a previous edit in this batch)"
                    if i > 1 else ""
                )
                transaction.abort()
                return ToolResult(
                    error=f"{prefix}old_string not found in {file_path}{suffix}"
                )

            if count > 1 and not replace_all:
                transaction.abort()
                return ToolResult(
                    error=f"{prefix}old_string found {count} times in {file_path}. "
                    "Provide more context to make it unique, or use replace_all=true."
                )

            if replace_all:
                content = content.replace(old_string, new_string)
                total_replacements += count
            else:
                content = content.replace(old_string, new_string, 1)
                total_replacements += 1

        try:
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
        except (OSError, UnsupportedFileMetadataError, WorkspaceMutationError) as exc:
            transaction.abort()
            return ToolResult(error=str(exc))

        diff = generate_unified_diff(original, content, file_path)

        if len(edits) == 1:
            title = ctx.tr(
                f"已编辑 {os.path.basename(file_path)}（{total_replacements} 处替换）",
                f"Edited {os.path.basename(file_path)} ({total_replacements} replacements)",
            )
        else:
            title = ctx.tr(
                f"已编辑 {os.path.basename(file_path)}（{len(edits)} 个编辑，{total_replacements} 处替换）",
                f"Edited {os.path.basename(file_path)} ({len(edits)} edits, {total_replacements} replacements)",
            )

        return ToolResult(
            output=diff,
            title=title,
            metadata={
                "edits": len(edits),
                "replacements": total_replacements,
                "file_path": file_path,
                **commit.metadata,
                **version_metadata(previous_version),
            },
        )
