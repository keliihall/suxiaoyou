"""Session CRUD endpoints — wired through the Route Module (ADR-0007).

The 11 endpoints split between:

- ``Route``-decorated CRUD: ``list / get / create / update / delete`` for
  ``/sessions`` and ``/sessions/{id}``, plus ``list`` for ``/sessions/search``.
  These call into ``app/session/manager.py`` cascades — ``create_session_and_index``,
  ``update_session``, ``delete_session_cascade`` — that own the multi-step
  orchestration (FTS reindex on directory change, stream abort + uploads
  cleanup on delete) per ADR-0007.
- ``Route.custom``: the four endpoints that are not CRUD — todos / files /
  compact / export-pdf / export-md. Each is a hand-written async handler
  that gets audit logging and ``DomainError`` mapping for free; their
  shapes (binary ``Response`` for exports, optional body for compact,
  in-line file-system probing for files) don't fit the typed-Manager
  contract and pretending otherwise would invent more decorator surface
  than warranted.
"""

from __future__ import annotations

import logging
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

from fastapi import Depends, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api._route import Route
from app.api.pdf import markdown_to_pdf
from app.dependencies import (
    AgentRegistryDep,
    ProviderRegistryDep,
    SessionFactoryDep,
    StreamManagerDep,
    get_db,
    get_session_factory,
)
from app.errors import DomainError, InternalError, NotFound
from app.models.session_file import SessionFile
from app.schemas.session import (
    SessionCompactionRequest,
    SessionCreate,
    SessionResponse,
    SessionSearchResult,
    SessionUpdate,
)
from app.session.manager import (
    compact_session_cascade,
    create_session_and_index,
    delete_session_cascade,
    get_messages,
    get_session,
    list_sessions,
    search_sessions,
    update_session,
)
from app.session.managed_workspace import managed_workspace_for_session

log = logging.getLogger(__name__)

_CREATION_HINT_PATTERN = re.compile(
    r"(?:\b(?:created|written|saved|generated|exported)\b|"
    r"已(?:创建|写入|保存|生成|导出))",
    re.IGNORECASE,
)
_CREATED_IN_PATTERN = re.compile(
    r"^\s*created in\s+(?P<directory>`[^`\r\n]+`|\"[^\"\r\n]+\"|"
    r"'[^'\r\n]+'|[^\s`\"']+)\s*$",
    re.IGNORECASE,
)
_BULLET_FILENAME_PATTERN = re.compile(
    r"^\s*[-*•]\s+(?P<name>.+?)\s*$"
)
_RESULT_PATH_LINE_PATTERN = re.compile(
    r"^\s*(?:created(?:\s+file)?(?:\s+(?:at|to))?|"
    r"written(?:\s+(?:to|at))?|"
    r"saved(?:\s+(?:file|output))?(?:\s+(?:to|at))?|"
    r"generated(?:\s+file)?(?:\s+(?:at|to))?|"
    r"exported(?:\s+(?:to|at))?|"
    r"已创建|已写入|已保存|已生成|已导出|已更新)"
    r"\s*(?::|：)?\s*(?P<value>.+?)\s*$",
    re.IGNORECASE,
)
_FILE_SUFFIX_PATTERN = re.compile(r"\.[A-Za-z0-9]{1,10}$")
_PATH_TRAILING_BOUNDARY = frozenset("()[]{}<>,;:（），；：")


@dataclass(frozen=True, slots=True)
class _WorkspaceBoundary:
    lexical_root: Path
    resolved_root: Path


def _path_is_within(candidate: Path, base: Path) -> bool:
    """Return whether two resolved paths have a real containment relation."""

    try:
        candidate.relative_to(base)
    except ValueError:
        return False
    return True


def _workspace_boundary(path: Path) -> _WorkspaceBoundary | None:
    """Create a trusted root while allowing the selected root itself to alias."""

    try:
        lexical = Path(os.path.abspath(path.expanduser()))
        resolved = lexical.resolve(strict=True)
        mode = resolved.stat(follow_symlinks=False).st_mode
    except (OSError, RuntimeError, ValueError):
        return None
    if not stat.S_ISDIR(mode):
        return None
    return _WorkspaceBoundary(lexical_root=lexical, resolved_root=resolved)


def _is_link_or_junction(path: Path, mode: int) -> bool:
    if stat.S_ISLNK(mode):
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction is None:
        return False
    return bool(is_junction())


def _resolve_owned_path(
    raw_path: str | Path,
    boundary: _WorkspaceBoundary,
    *,
    expect_directory: bool = False,
) -> Path | None:
    """Resolve one workspace entry without following child links/junctions."""

    try:
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = boundary.lexical_root / candidate
        lexical = Path(os.path.abspath(candidate))

        relative: Path | None = None
        walk_root: Path | None = None
        for root in (boundary.lexical_root, boundary.resolved_root):
            try:
                relative = lexical.relative_to(root)
                walk_root = root
                break
            except ValueError:
                continue
        if relative is None or walk_root is None:
            return None

        current = walk_root
        for component in relative.parts:
            current = current / component
            mode = current.lstat().st_mode
            if _is_link_or_junction(current, mode):
                return None

        resolved = lexical.resolve(strict=True)
        if not _path_is_within(resolved, boundary.resolved_root):
            return None
        mode = resolved.stat(follow_symlinks=False).st_mode
        if expect_directory:
            return resolved if stat.S_ISDIR(mode) else None
        return resolved if stat.S_ISREG(mode) else None
    except (OSError, RuntimeError, ValueError):
        # Files may disappear or become inaccessible while the panel refreshes.
        return None


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path))


def _absolute_path_flavour(value: str, *, require_file: bool) -> str | None:
    """Classify an absolute POSIX/drive/UNC token without touching the FS."""

    value = value.strip()
    if not value or "\x00" in value:
        return None
    is_windows_form = bool(re.match(r"^(?:[A-Za-z]:[\\/]|\\\\)", value))
    pure_path = PureWindowsPath(value) if is_windows_form else PurePosixPath(value)
    if not pure_path.is_absolute():
        return None
    if require_file and not _FILE_SUFFIX_PATTERN.fullmatch(pure_path.suffix):
        return None
    return "windows" if is_windows_form else "posix"


def _native_path_from_token(value: str, *, require_file: bool) -> Path | None:
    flavour = _absolute_path_flavour(value, require_file=require_file)
    if flavour is None:
        return None
    if (flavour == "windows") != (os.name == "nt"):
        return None
    return Path(value)


def _strip_paired_delimiters(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] in "`\"'" and value[-1] == value[0]:
        return value[1:-1].strip()
    return value


def _unquoted_absolute_file_prefix(value: str) -> str | None:
    """Return a no-space absolute file token before result decorations."""

    token = value.strip().split(maxsplit=1)[0] if value.strip() else ""
    for end in range(1, len(token) + 1):
        following = token[end] if end < len(token) else ""
        if following and following not in _PATH_TRAILING_BOUNDARY:
            continue
        candidate = token[:end]
        if _absolute_path_flavour(candidate, require_file=True) is not None:
            return candidate
    return None


def _extract_absolute_file_path_strings(payload: str) -> list[str]:
    """Extract only explicit, completed-result absolute file paths."""

    found: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        candidate = value.strip()
        if (
            _absolute_path_flavour(candidate, require_file=True) is not None
            and candidate not in seen
        ):
            seen.add(candidate)
            found.append(candidate)

    for line in payload.splitlines():
        match = _RESULT_PATH_LINE_PATTERN.match(line)
        if match is None:
            continue
        value = match.group("value").strip()
        delimited = _strip_paired_delimiters(value)
        if delimited != value:
            add(delimited)
            continue
        candidate = _unquoted_absolute_file_prefix(value)
        if candidate is not None:
            add(candidate)

    return found


def _safe_bullet_filename(value: str) -> str | None:
    name = _strip_paired_delimiters(value)
    if (
        not name
        or name in {".", ".."}
        or any(char in name for char in ("/", "\\", "\x00", ":"))
        or _FILE_SUFFIX_PATTERN.fullmatch(PurePosixPath(name).suffix) is None
    ):
        return None
    return name


def _owned_directory_files_by_mtime(
    directory: Path,
    boundary: _WorkspaceBoundary,
) -> list[Path]:
    """List regular owned entries; one racing/broken entry cannot fail all."""

    try:
        entries = list(directory.iterdir())
    except OSError:
        return []

    sortable: list[tuple[int, str, Path]] = []
    for entry in entries:
        resolved = _resolve_owned_path(entry, boundary)
        if resolved is None:
            continue
        try:
            mtime_ns = resolved.stat(follow_symlinks=False).st_mtime_ns
        except OSError:
            continue
        sortable.append((mtime_ns, _path_key(resolved), resolved))
    sortable.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in sortable]


# ---------------------------------------------------------------------------
# Route registrations — order matters for FastAPI: more specific paths
# (`/sessions/search`) must register before parameterised ones
# (`/sessions/{session_id}`) so the literal route wins.
# ---------------------------------------------------------------------------

route = Route(tags=["sessions"])

route.list(
    "/sessions",
    manager=list_sessions,
    response_model=list[SessionResponse],
)

route.list(
    "/sessions/search",
    manager=search_sessions,
    response_model=list[SessionSearchResult],
)

route.create(
    "/sessions",
    manager=create_session_and_index,
    body=SessionCreate,
    response_model=SessionResponse,
    status_code=201,
)

route.get(
    "/sessions/{session_id}",
    manager=get_session,
    response_model=SessionResponse,
    not_found_on_none=True,
    not_found_message="Session not found",
)

route.update(
    "/sessions/{session_id}",
    manager=update_session,
    body=SessionUpdate,
    response_model=SessionResponse,
)

route.delete(
    "/sessions/{session_id}",
    manager=delete_session_cascade,
)


# ---------------------------------------------------------------------------
# Hand-written custom handlers — non-CRUD shapes
# ---------------------------------------------------------------------------


async def _list_session_todos(
    session_id: str,
    request: Request,
) -> dict:
    """Return the durable, owner-safe Todo projection for ``session_id``."""
    del request
    from app.tool.builtin.todo import get_todo_reload_state

    try:
        session_factory = get_session_factory()
    except RuntimeError as exc:
        raise InternalError("Todo storage is unavailable") from exc
    return await get_todo_reload_state(session_id, session_factory)


def _extract_file_paths_from_messages(
    messages: list,
    session_directory: str | Path | None,
) -> list[str]:
    """Best-effort recovery of files created during older sessions.

    Conservative: recovers explicit creation outputs from ``code_execute``-
    style sessions but does not treat files merely *read* during analysis
    as generated workspace files.
    """
    if not session_directory:
        return []

    boundary = _workspace_boundary(Path(session_directory))
    if boundary is None:
        return []
    found: list[str] = []
    seen: set[str] = set()

    def add_candidate(raw_path: object) -> None:
        value = str(raw_path or "").strip()
        native_path = _native_path_from_token(value, require_file=True)
        if native_path is None:
            return
        resolved = _resolve_owned_path(native_path, boundary)
        if resolved is None:
            return
        key = _path_key(resolved)
        if key in seen:
            return
        seen.add(key)
        found.append(str(resolved))

    for msg in messages:
        message_data = getattr(msg, "data", {}) or {}
        if not isinstance(message_data, dict):
            message_data = {}
        for part in getattr(msg, "parts", []):
            data = getattr(part, "data", {}) or {}
            if not isinstance(data, dict):
                continue
            payload = ""

            if data.get("type") == "tool":
                tool_name = str(data.get("tool", ""))
                if tool_name not in {"code_execute", "write", "edit", "artifact", "bash"}:
                    continue
                state = data.get("state") or {}
                if not isinstance(state, dict):
                    continue
                if state.get("status") not in {None, "completed"}:
                    continue
                metadata = state.get("metadata") or {}
                if isinstance(metadata, dict):
                    file_path = metadata.get("file_path")
                    if file_path:
                        add_candidate(file_path)
                    written_files = metadata.get("written_files") or []
                    if isinstance(written_files, (list, tuple)):
                        for file_path in written_files:
                            add_candidate(file_path)
                payload = str(state.get("output", ""))
            elif data.get("type") == "text":
                if message_data.get("role") != "assistant":
                    continue
                payload = str(data.get("text", ""))
                if not _CREATION_HINT_PATTERN.search(payload):
                    continue
            else:
                continue

            for raw_path in _extract_absolute_file_path_strings(payload):
                add_candidate(raw_path)

            lines = payload.splitlines()
            for index, line in enumerate(lines):
                created_in_match = _CREATED_IN_PATTERN.match(line)
                if created_in_match is None:
                    continue
                raw_directory = _strip_paired_delimiters(
                    created_in_match.group("directory")
                )
                native_directory = _native_path_from_token(
                    raw_directory,
                    require_file=False,
                )
                if native_directory is None:
                    continue
                target_dir = _resolve_owned_path(
                    native_directory,
                    boundary,
                    expect_directory=True,
                )
                if target_dir is None:
                    continue
                for following in lines[index + 1 :]:
                    if not following.strip():
                        continue
                    bullet_match = _BULLET_FILENAME_PATTERN.match(following)
                    if bullet_match is None:
                        break
                    filename = _safe_bullet_filename(bullet_match.group("name"))
                    if filename is not None:
                        add_candidate(target_dir / filename)

    return found


async def _list_session_files(
    session_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Return tracked workspace files for ``session_id``."""
    session = await get_session(db, session_id)
    if session is None or not session.directory:
        return {"files": []}

    try:
        raw_workspace = (
            Path(session.directory)
            if session.directory != "."
            else managed_workspace_for_session(session_id, create=False)
        )
    except (OSError, RuntimeError, ValueError):
        return {"files": []}
    boundary = _workspace_boundary(raw_workspace)
    if boundary is None:
        return {"files": []}
    effective_workspace = boundary.resolved_root

    tracked = await db.execute(
        select(SessionFile)
        .where(SessionFile.session_id == session_id)
        .order_by(SessionFile.time_created.asc())
    )
    tracked_files = tracked.scalars().all()
    files: list[dict[str, str]] = []
    seen_paths: set[str] = set()

    for entry in tracked_files:
        resolved = _resolve_owned_path(entry.file_path, boundary)
        if resolved is None:
            continue
        key = _path_key(resolved)
        if key in seen_paths:
            continue
        seen_paths.add(key)
        files.append({
            "name": resolved.name,
            "path": str(resolved),
            "type": entry.file_type,
            "tool": entry.tool_id,
        })

    output_dir = _resolve_owned_path(
        effective_workspace / "suxiaoyou_written",
        boundary,
        expect_directory=True,
    )
    if output_dir is not None:
        for resolved in _owned_directory_files_by_mtime(output_dir, boundary):
            key = _path_key(resolved)
            if key in seen_paths:
                continue
            seen_paths.add(key)
            files.append({
                "name": resolved.name,
                "path": str(resolved),
                "type": "generated",
                "tool": "artifact",
            })

    messages = await get_messages(db, session_id, limit=500, offset=0)
    recovered_paths = _extract_file_paths_from_messages(
        messages, effective_workspace
    )
    for recovered in recovered_paths:
        resolved = _resolve_owned_path(recovered, boundary)
        if resolved is None:
            continue
        key = _path_key(resolved)
        if key in seen_paths:
            continue
        seen_paths.add(key)
        files.append({
            "name": resolved.name,
            "path": str(resolved),
            "type": "generated",
            "tool": "code_execute",
        })

    return {"files": files}


async def _compact_session(
    session_id: str,
    session_factory: SessionFactoryDep,
    provider_registry: ProviderRegistryDep,
    agent_registry: AgentRegistryDep,
    stream_manager: StreamManagerDep,
    db: AsyncSession = Depends(get_db),
    body: SessionCompactionRequest | None = None,
) -> dict[str, object]:
    """Trigger manual context compaction.

    Custom (not ``route.create``) because the body is genuinely optional —
    clients may POST with no body. ``route.create`` would force a required
    ``SessionCompactionRequest`` body and break that contract.
    """
    return await compact_session_cascade(
        db,
        session_id,
        body,
        session_factory,
        provider_registry,
        agent_registry,
        stream_manager,
    )


def _messages_to_markdown(title: str, messages: list) -> str:
    """Format a list of Message ORM objects as a Markdown transcript."""
    now_str = datetime.now(timezone.utc).strftime("%B %d, %Y at %I:%M %p UTC")
    lines = [f"# {title}", f"*Exported on {now_str}*", "", "---", ""]

    for msg in messages:
        data = msg.data or {}
        role = data.get("role", "user")
        label = "You" if role == "user" else "Assistant"

        text_parts: list[str] = []
        for part in msg.parts:
            pd = part.data or {}
            if pd.get("type") == "text":
                text = pd.get("text", "").strip()
                if text:
                    text_parts.append(text)

        if not text_parts:
            continue

        lines.append(f"**{label}:**")
        lines.append("")
        lines.append("\n\n".join(text_parts))
        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def _content_disposition(title: str, ext: str) -> str:
    """RFC-5987 ``Content-Disposition`` for an export filename."""
    from urllib.parse import quote

    safe_title = "".join(
        c if c.isascii() and (c.isalnum() or c in " _-") else "_" for c in title
    )
    utf8_title = quote(title, safe="")
    return (
        f'attachment; filename="{safe_title}.{ext}"; '
        f"filename*=UTF-8''{utf8_title}.{ext}"
    )


async def _export_session_pdf(
    session_id: str,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Export a session as PDF."""
    session = await get_session(db, session_id)
    if session is None:
        raise NotFound("Session not found")

    messages = await get_messages(db, session_id)
    title = session.title or "Conversation"

    try:
        md_content = _messages_to_markdown(title, messages)
        pdf_bytes = markdown_to_pdf(md_content)
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": _content_disposition(title, "pdf")},
        )
    except DomainError:
        raise
    except Exception as exc:
        log.exception("Session PDF export failed")
        raise InternalError(str(exc)) from exc


async def _export_session_markdown(
    session_id: str,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Export a session as Markdown."""
    session = await get_session(db, session_id)
    if session is None:
        raise NotFound("Session not found")

    messages = await get_messages(db, session_id)
    title = session.title or "Conversation"
    md_content = _messages_to_markdown(title, messages)

    return Response(
        content=md_content.encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": _content_disposition(title, "md")},
    )


route.custom("GET", "/sessions/{session_id}/todos", handler=_list_session_todos)
route.custom("GET", "/sessions/{session_id}/files", handler=_list_session_files)
route.custom("POST", "/sessions/{session_id}/compact", handler=_compact_session)
route.custom("GET", "/sessions/{session_id}/export-pdf", handler=_export_session_pdf)
route.custom("GET", "/sessions/{session_id}/export-md", handler=_export_session_markdown)


# Exposed for app/api/router.py — preserves the existing
# `from app.api import sessions as sessions_api; include_router(sessions_api.router)`
# contract.
router = route.api_router
