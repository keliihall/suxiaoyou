"""Managed workspaces for conversations without a user-selected project.

An attachment's parent directory is an input location, not implicit consent to
write beside the source. Folderless conversations therefore receive a stable
per-session workspace with copied input snapshots and a dedicated
``suxiaoyou_written`` output directory.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.message import Part


_MANAGED_ROOT_ENV = "SUXIAOYOU_MANAGED_WORKSPACE_ROOT"
_LEGACY_MANAGED_ROOT_ENV = "SUXIAOYOU_LEGACY_MANAGED_WORKSPACE_ROOT"
_DEFAULT_MAX_INPUT_BYTES = 2 * 1024 * 1024 * 1024
_DEFAULT_MAX_INPUT_ENTRIES = 5_000
_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")


class ManagedInputError(RuntimeError):
    """Raised when an attachment cannot be snapshotted safely."""


def managed_workspace_root() -> Path:
    configured = os.environ.get(_MANAGED_ROOT_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.cwd() / "data" / "managed-workspaces").resolve()


def _legacy_managed_workspace_root() -> Path | None:
    configured = os.environ.get(_LEGACY_MANAGED_ROOT_ENV, "").strip()
    if not configured:
        return None
    return Path(configured).expanduser().resolve()


def managed_workspace_for_session(session_id: str, *, create: bool = True) -> Path:
    """Resolve a session, using the legacy root only when the current one is absent."""
    safe_session_id = _safe_component(session_id, fallback="session")
    workspace = managed_workspace_root() / safe_session_id
    legacy_root = _legacy_managed_workspace_root()
    if not workspace.exists() and legacy_root is not None:
        legacy_workspace = legacy_root / safe_session_id
        if legacy_workspace.is_dir():
            workspace = legacy_workspace
    if create:
        for child in ("inputs", "suxiaoyou_written", ".tmp"):
            (workspace / child).mkdir(parents=True, exist_ok=True)
    return workspace


def snapshot_attachments(
    session_id: str,
    attachments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Copy attachment paths into the session's managed ``inputs`` folder."""
    if not attachments:
        return []

    workspace = managed_workspace_for_session(session_id)
    input_dir = workspace / "inputs"
    prepared: list[
        tuple[dict[str, Any], str | None, Path | None, Path | None, tuple[int, int] | None]
    ] = []
    total_entries = 0
    total_size = 0
    for attachment in attachments:
        source_value = str(attachment.get("path") or "").strip()
        if not source_value:
            prepared.append((dict(attachment), None, None, None, None))
            continue

        source = Path(source_value).expanduser().resolve(strict=True)
        try:
            source.relative_to(workspace)
        except ValueError:
            pass
        else:
            prepared.append((dict(attachment), None, None, None, None))
            continue

        file_id = _safe_component(
            str(attachment.get("file_id") or attachment.get("content_hash") or "input"),
            fallback="input",
        )
        name = _safe_component(
            str(attachment.get("name") or source.name),
            fallback=source.name or "input",
        )
        destination = input_dir / f"{file_id}_{name}"
        size_info = _input_size(source)
        total_entries += size_info[0]
        total_size += size_info[1]
        if total_entries > _DEFAULT_MAX_INPUT_ENTRIES:
            raise ManagedInputError(
                "Attachments contain "
                f"{total_entries} entries in total; maximum is "
                f"{_DEFAULT_MAX_INPUT_ENTRIES}"
            )
        if total_size > _DEFAULT_MAX_INPUT_BYTES:
            raise ManagedInputError(
                f"Attachments are {total_size} bytes in total; maximum is "
                f"{_DEFAULT_MAX_INPUT_BYTES}"
            )
        prepared.append(
            (dict(attachment), source_value, source, destination, size_info)
        )

    # Finish every validation/size walk before writing the first byte. A bad
    # final attachment must not leave an unexplained half-snapshotted request.
    snapshotted: list[dict[str, Any]] = []
    for attachment, source_value, source, destination, size_info in prepared:
        if source is None or destination is None or source_value is None:
            snapshotted.append(attachment)
            continue
        _copy_input_atomically(source, destination, size_info=size_info)
        updated = attachment
        updated["original_path"] = source_value
        updated["path"] = str(destination.resolve())
        updated["source"] = "managed"
        snapshotted.append(updated)

    return snapshotted


async def snapshot_existing_session_attachments(
    session_factory: async_sessionmaker[AsyncSession],
    session_id: str,
) -> int:
    """Move legacy external file parts into the managed input boundary."""
    workspace = managed_workspace_for_session(session_id)
    async with session_factory() as db:
        result = await db.execute(select(Part).where(Part.session_id == session_id))
        candidates = [
            (part.id, dict(part.data or {}))
            for part in result.scalars().all()
            if (part.data or {}).get("type") == "file"
        ]

    updates: list[tuple[str, dict[str, Any]]] = []
    for part_id, data in candidates:
        path_value = str(data.get("path") or "").strip()
        if not path_value:
            continue
        try:
            Path(path_value).expanduser().resolve(strict=True).relative_to(workspace)
            continue
        except ValueError:
            pass
        except FileNotFoundError:
            # Preserve the original metadata so the UI can report a missing
            # source instead of silently replacing it with an empty snapshot.
            continue

        updated = await asyncio.to_thread(snapshot_attachments, session_id, [data])
        if updated:
            updates.append((part_id, updated[0]))

    if not updates:
        return 0

    async with session_factory() as db:
        async with db.begin():
            for part_id, data in updates:
                part = await db.get(Part, part_id)
                if part is not None:
                    part.data = data
    return len(updates)


def _copy_input_atomically(
    source: Path,
    destination: Path,
    *,
    size_info: tuple[int, int] | None = None,
) -> None:
    if destination.exists():
        return

    entries, size = size_info if size_info is not None else _input_size(source)
    if entries > _DEFAULT_MAX_INPUT_ENTRIES:
        raise ManagedInputError(
            f"Attachment contains {entries} entries; maximum is {_DEFAULT_MAX_INPUT_ENTRIES}"
        )
    if size > _DEFAULT_MAX_INPUT_BYTES:
        raise ManagedInputError(
            f"Attachment is {size} bytes; maximum is {_DEFAULT_MAX_INPUT_BYTES}"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_root = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent)
    )
    temporary = temporary_root / destination.name
    try:
        if source.is_dir():
            shutil.copytree(source, temporary, symlinks=True)
        else:
            shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)


def _input_size(source: Path) -> tuple[int, int]:
    if source.is_symlink():
        raise ManagedInputError(f"Symbolic-link attachments are not supported: {source}")
    if source.is_file():
        return 1, source.stat().st_size
    if not source.is_dir():
        raise ManagedInputError(f"Attachment is not a file or directory: {source}")

    entries = 1
    size = 0
    for child in source.rglob("*"):
        if child.is_symlink():
            raise ManagedInputError(
                f"Directory attachments containing symbolic links are not supported: {child}"
            )
        entries += 1
        if child.is_file():
            size += child.stat().st_size
        if entries > _DEFAULT_MAX_INPUT_ENTRIES or size > _DEFAULT_MAX_INPUT_BYTES:
            break
    return entries, size


def _safe_component(value: str, *, fallback: str) -> str:
    cleaned = _SAFE_COMPONENT.sub("_", value).strip("._")
    return (cleaned or fallback)[:180]
