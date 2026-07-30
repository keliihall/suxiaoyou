"""Authenticated API for workspace file history and explicit restore."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.dependencies import SessionFactoryDep
from app.i18n import Language, localize, request_language
from app.session.manager import get_session
from app.session.managed_workspace import managed_workspace_for_session
from app.storage.file_versions import (
    FileVersionError,
    FileVersionNotFound,
    FileVersionStore,
)
from app.tool.workspace import (
    WorkspaceBoundaryViolation,
    validate_agent_workspace_root,
)

router = APIRouter(prefix="/file-versions")


class RestoreFileVersionRequest(BaseModel):
    session_id: str = Field(min_length=1)


async def _session_workspace(
    session_factory: SessionFactoryDep,
    session_id: str,
    *,
    language: Language,
) -> Path:
    async with session_factory() as db:
        session = await get_session(db, session_id)
        parent = (
            await get_session(db, session.parent_id)
            if session is not None and session.parent_id
            else None
        )
    if session is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "file_version_session_not_found",
                "message": localize(
                    language,
                    "未找到对应的对话。",
                    "The conversation was not found.",
                ),
            },
        )
    managed = not session.directory or session.directory == "."
    workspace = (
        managed_workspace_for_session(session.id, create=False)
        if managed
        else Path(session.directory).expanduser().resolve()
    )
    inherited_managed: Path | None = None
    if (
        not managed
        and parent is not None
        and (not parent.directory or parent.directory == ".")
    ):
        expected = managed_workspace_for_session(parent.id, create=False).resolve()
        if workspace == expected:
            inherited_managed = expected
    try:
        workspace = validate_agent_workspace_root(
            workspace,
            allowed_managed_workspace=(
                workspace if managed else inherited_managed
            ),
        )
    except WorkspaceBoundaryViolation as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "file_version_workspace_invalid",
                "message": localize(
                    language,
                    "当前文件夹不符合版本恢复的安全要求。",
                    "The current folder does not satisfy the recovery safety requirements.",
                ),
            },
        ) from exc
    if not workspace.is_dir():
        raise HTTPException(
            status_code=409,
            detail={
                "code": "file_version_workspace_unavailable",
                "message": localize(
                    language,
                    "当前文件夹暂不可用。",
                    "The current folder is unavailable.",
                ),
            },
        )
    return workspace


@router.get("")
async def list_file_versions(
    request: Request,
    session_factory: SessionFactoryDep,
    session_id: str = Query(min_length=1),
    file_path: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    """List persistent pre-mutation versions for a session's workspace."""

    language = request_language(request)
    workspace = await _session_workspace(
        session_factory,
        session_id,
        language=language,
    )
    try:
        versions = await asyncio.to_thread(
            FileVersionStore(workspace).list_versions,
            file_path=file_path,
            limit=limit,
        )
    except FileVersionError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "file_version_list_failed",
                "message": localize(
                    language,
                    "无法读取文件版本历史。",
                    "The file version history could not be loaded.",
                ),
            },
        ) from exc
    return {
        "workspace": str(workspace),
        "versions": [version.public_dict() for version in versions],
    }


@router.post("/{version_id}/restore")
async def restore_file_version(
    request: Request,
    version_id: str,
    body: RestoreFileVersionRequest,
    session_factory: SessionFactoryDep,
) -> dict[str, Any]:
    """Restore one version, preserving the displaced file as a new version."""

    language = request_language(request)
    workspace = await _session_workspace(
        session_factory,
        body.session_id,
        language=language,
    )
    try:
        restored, recovery, target = await asyncio.to_thread(
            FileVersionStore(workspace).restore,
            version_id,
            session_id=body.session_id,
            call_id="api.restore_file_version",
        )
    except FileVersionNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "file_version_not_found",
                "message": localize(
                    language,
                    "未找到这个文件版本。",
                    "The file version was not found.",
                ),
            },
        ) from exc
    except FileVersionError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "file_version_restore_failed",
                "message": localize(
                    language,
                    "无法安全恢复这个文件版本。",
                    "The file version could not be restored safely.",
                ),
            },
        ) from exc
    return {
        "file_path": str(target),
        "restored_version": restored.public_dict(),
        "recovery_version": recovery.public_dict() if recovery is not None else None,
    }
