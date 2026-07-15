"""Apply-patch tool — apply a lightweight patch to create, update, or delete files."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from app.storage.file_versions import FileVersionStore
from app.tool.base import ToolDefinition, ToolResult
from app.tool.builtin.patch_parser import HunkType, apply_chunks, parse_patch
from app.tool.context import ToolContext
from app.tool.file_metadata import (
    UnsupportedFileMetadataError,
    ensure_mutation_metadata_supported,
)
from app.tool.workspace import WorkspaceViolation, resolve_and_validate, resolve_for_write
from app.tool.workspace_transaction import (
    WorkspaceMutationError,
    WorkspaceMutationTransaction,
)
from app.utils.atomic_write import atomic_write_text
from app.utils.diff import generate_unified_diff


MAX_PATCH_BYTES = 2 * 1024 * 1024
MAX_PATCH_HUNKS = 256


class ApplyPatchTool(ToolDefinition):

    @property
    def id(self) -> str:
        return "apply_patch"

    @property
    def description(self) -> str:
        return (
            "Apply a patch to create, update, or delete files. "
            "Uses a lightweight format:\n"
            "*** Begin Patch\n"
            "*** Add File: path\n"
            "+new line\n"
            "*** Update File: path\n"
            "@@ context line\n"
            "-old line\n"
            "+new line\n"
            "*** Delete File: path\n"
            "*** End Patch\n\n"
            "More token-efficient than individual edit calls for multi-file changes."
        )

    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "patch_text": {
                    "type": "string",
                    "description": "The patch content in *** Begin/End Patch format",
                },
            },
            "required": ["patch_text"],
        }

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        patch_text = args["patch_text"]

        if len(patch_text.encode("utf-8")) > MAX_PATCH_BYTES:
            return ToolResult(
                error=f"Patch exceeds the {MAX_PATCH_BYTES}-byte safety limit"
            )

        # Parse the patch
        parsed = parse_patch(patch_text)
        if parsed.errors:
            return ToolResult(error="Patch parse errors: " + "; ".join(parsed.errors))

        if not parsed.hunks:
            return ToolResult(error="No file operations found in patch")
        if len(parsed.hunks) > MAX_PATCH_HUNKS:
            return ToolResult(
                error=f"Patch exceeds the {MAX_PATCH_HUNKS}-file safety limit"
            )

        # Validate all paths first before making any changes
        resolved_paths: list[tuple[str, str | None]] = []  # (resolved_path, move_to)
        for hunk in parsed.hunks:
            try:
                if hunk.type == HunkType.DELETE:
                    resolved = resolve_and_validate(hunk.path, ctx.workspace)
                else:
                    resolved = resolve_for_write(hunk.path, ctx.workspace)
                move_to = None
                if hunk.move_to:
                    move_to = resolve_for_write(hunk.move_to, ctx.workspace)
                ensure_mutation_metadata_supported(resolved)
                if move_to is not None:
                    ensure_mutation_metadata_supported(move_to)
                resolved_paths.append((resolved, move_to))
            except (UnsupportedFileMetadataError, WorkspaceViolation) as e:
                return ToolResult(error=str(e))

        # Prepare one isolated copy of the workspace.  Every hunk is evaluated
        # against this private tree first; the real workspace is not touched
        # until the complete patch has validated.  WorkspaceMutationTransaction
        # then captures one retention-pinned version batch and commits through
        # fd-anchored guarded renames with rollback on any conflict.
        try:
            transaction = WorkspaceMutationTransaction(
                ctx.workspace or "",
                ctx,
                operation="apply_patch",
            )
            transaction.prepare_paths(
                [
                    path
                    for resolved, move_to in resolved_paths
                    for path in (resolved, move_to)
                    if path is not None
                ]
            )
        except WorkspaceMutationError as exc:
            return ToolResult(error=str(exc))

        summaries: list[str] = []
        diffs: list[str] = []
        try:
            for hunk, (resolved, move_to) in zip(parsed.hunks, resolved_paths):
                staged = transaction.staged_path(resolved)
                staged_move_to = (
                    transaction.staged_path(move_to) if move_to is not None else None
                )

                if hunk.type == HunkType.ADD:
                    staged.parent.mkdir(parents=True, exist_ok=True)
                    if staged.exists() or staged.is_symlink():
                        raise WorkspaceMutationError(
                            f"Cannot add file '{hunk.path}': already exists"
                        )
                    atomic_write_text(staged, hunk.contents)
                    summaries.append(
                        ctx.tr(f"+ 已新增 {hunk.path}", f"+ Added {hunk.path}")
                    )

                elif hunk.type == HunkType.DELETE:
                    if not staged.exists() or staged.is_dir():
                        raise WorkspaceMutationError(
                            f"Cannot delete file '{hunk.path}': not found"
                        )
                    staged.unlink()
                    summaries.append(
                        ctx.tr(f"- 已删除 {hunk.path}", f"- Deleted {hunk.path}")
                    )

                elif hunk.type == HunkType.UPDATE:
                    if not staged.exists() or staged.is_dir():
                        raise WorkspaceMutationError(
                            f"Cannot update file '{hunk.path}': not found"
                        )
                    try:
                        original = staged.read_text(encoding="utf-8")
                    except UnicodeDecodeError as exc:
                        raise WorkspaceMutationError(
                            f"Cannot update binary file: {hunk.path}"
                        ) from exc

                    modified = apply_chunks(original, hunk.chunks)
                    diff = generate_unified_diff(original, modified, hunk.path)
                    if diff:
                        diffs.append(diff)

                    target = staged_move_to if staged_move_to is not None else staged
                    target.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write_text(target, modified)
                    if staged_move_to is not None and staged != staged_move_to:
                        staged.unlink()
                        label = ctx.tr(
                            f"~ 已更新 {hunk.path} → {hunk.move_to}",
                            f"~ Updated {hunk.path} → {hunk.move_to}",
                        )
                    else:
                        label = ctx.tr(
                            f"~ 已更新 {hunk.path}",
                            f"~ Updated {hunk.path}",
                        )
                    summaries.append(label)

            for resolved, move_to in resolved_paths:
                ensure_mutation_metadata_supported(resolved)
                if move_to is not None:
                    ensure_mutation_metadata_supported(move_to)
            commit = transaction.commit()
        except (
            OSError,
            UnsupportedFileMetadataError,
            WorkspaceMutationError,
            ValueError,
        ) as exc:
            transaction.abort()
            return ToolResult(error=str(exc))

        output_parts = summaries.copy()
        if diffs:
            output_parts.append("")
            output_parts.extend(diffs)

        versions_by_id = {
            version.id: version
            for version in FileVersionStore(Path(ctx.workspace or "")).list_versions()
            if version.id in set(commit.previous_version_ids)
        }
        captured_versions = [
            versions_by_id[version_id]
            for version_id in commit.previous_version_ids
            if version_id in versions_by_id
        ]

        return ToolResult(
            output="\n".join(output_parts),
            title=ctx.tr(
                f"已应用补丁（{len(parsed.hunks)} 个文件）",
                f"Applied patch ({len(parsed.hunks)} files)",
            ),
            metadata={
                "files": len(parsed.hunks),
                **commit.metadata,
                "previous_versions": [
                    {
                        "id": version.id,
                        "file_path": version.relative_path,
                        "sha256": version.sha256,
                        "size": version.size,
                    }
                    for version in captured_versions
                ],
            },
        )
