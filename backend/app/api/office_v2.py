"""Local-only, source-pinned Office v1.1 preview HTTP boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.auth.local import require_local_session
from app.office_rendering import (
    CacheIntegrityError,
    CacheWriteError,
    OfficePreviewBusyError,
    OfficePreviewDisabledError,
    OfficePreviewError,
    OfficePreviewNotFoundError,
    OfficePreviewProvenanceError,
    OfficePreviewService,
    OfficePreviewStaleError,
    PathEscapeError,
    ProviderUnavailableError,
    RenderContractError,
    RenderProcessError,
    RenderTimeoutError,
    StaleSourceError,
)


router = APIRouter(
    prefix="/office-v2",
    dependencies=[Depends(require_local_session)],
)


class _StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OfficeRenderBody(_StrictBody):
    session_id: str = Field(min_length=1, max_length=128)
    workspace_instance_id: str = Field(min_length=1, max_length=128)
    relative_path: str = Field(min_length=1, max_length=4096)
    expected_source_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )


def _error(status_code: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"code": code, "detail": detail},
        headers={"Cache-Control": "no-store"},
    )


def _service(request: Request) -> OfficePreviewService:
    from app import release_features

    if not release_features.V11_OFFICE_V2_RELEASED:
        raise OfficePreviewDisabledError("Office v1.1 preview is not released")
    service = getattr(request.app.state, "office_preview_service", None)
    if not isinstance(service, OfficePreviewService):
        raise ProviderUnavailableError("Office preview runtime is unavailable")
    return service


def _office_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, OfficePreviewDisabledError):
        return _error(
            404,
            "v11_office_v2_not_available",
            "Office v1.1 preview is not available in this release",
        )
    if isinstance(exc, OfficePreviewNotFoundError):
        return _error(
            404,
            "office_preview_not_found",
            "The Office preview resource was not found",
        )
    if isinstance(exc, OfficePreviewProvenanceError):
        return _error(
            409,
            "office_preview_provenance_mismatch",
            "The Office file is not bound to the requested session workspace",
        )
    if isinstance(exc, (OfficePreviewStaleError, StaleSourceError)):
        return _error(
            409,
            "office_preview_stale",
            "The Office file changed; request a new preview",
        )
    if isinstance(exc, OfficePreviewBusyError):
        return _error(
            429,
            "office_preview_busy",
            "The local Office renderer is busy",
        )
    if isinstance(exc, ProviderUnavailableError):
        return _error(
            503,
            "office_renderer_unavailable",
            "An approved local Office renderer is unavailable",
        )
    if isinstance(exc, RenderTimeoutError):
        return _error(
            504,
            "office_render_timeout",
            "The local Office renderer exceeded its time limit",
        )
    if isinstance(exc, RenderProcessError):
        return _error(
            502,
            "office_render_failed",
            "The local Office renderer failed safely",
        )
    if isinstance(exc, CacheWriteError):
        return _error(
            503,
            "office_cache_unavailable",
            "The private Office preview cache is unavailable",
        )
    if isinstance(exc, (CacheIntegrityError, PathEscapeError, RenderContractError)):
        return _error(
            409,
            "office_preview_integrity_failure",
            "The Office preview failed its integrity contract",
        )
    if isinstance(exc, OfficePreviewError):
        return _error(
            409,
            "office_preview_failed",
            "The Office preview failed safely",
        )
    raise exc


@router.get("/context")
async def office_preview_context(
    request: Request,
    session_id: str = Query(min_length=1, max_length=128),
) -> Any:
    """Return a path-free active workspace and renderer identity."""

    try:
        context = await _service(request).context(session_id=session_id)
    except Exception as exc:
        return _office_error(exc)
    return JSONResponse(
        content=context.to_dict(),
        headers={"Cache-Control": "no-store"},
    )


@router.post("/render")
async def render_office_preview(
    body: OfficeRenderBody,
    request: Request,
) -> Any:
    """Render current workspace bytes into a private immutable cache entry."""

    try:
        binding = await _service(request).render(
            session_id=body.session_id,
            workspace_instance_id=body.workspace_instance_id,
            relative_path=body.relative_path,
            expected_source_sha256=body.expected_source_sha256,
        )
    except Exception as exc:
        return _office_error(exc)
    return JSONResponse(
        content=binding.to_dict(),
        headers={"Cache-Control": "no-store"},
    )


@router.get("/validation")
async def office_validation_status(
    request: Request,
    session_id: str = Query(min_length=1, max_length=128),
    workspace_instance_id: str = Query(min_length=1, max_length=128),
    relative_path: str = Query(min_length=1, max_length=4096),
) -> Any:
    """Return path-free validation freshness after edits or rewind."""

    try:
        status = await _service(request).validation_status(
            session_id=session_id,
            workspace_instance_id=workspace_instance_id,
            relative_path=relative_path,
        )
    except Exception as exc:
        return _office_error(exc)
    return JSONResponse(
        content=status.to_dict(),
        headers={"Cache-Control": "no-store"},
    )


@router.get("/page")
async def office_preview_page(
    request: Request,
    session_id: str = Query(min_length=1, max_length=128),
    workspace_instance_id: str = Query(min_length=1, max_length=128),
    relative_path: str = Query(min_length=1, max_length=4096),
    cache_key: str = Query(pattern=r"^[0-9a-f]{64}$"),
    page_number: int = Query(ge=1, le=1000),
) -> Any:
    """Revalidate source identity before serving one validated PNG page."""

    try:
        page = await _service(request).page_path(
            session_id=session_id,
            workspace_instance_id=workspace_instance_id,
            relative_path=relative_path,
            cache_key=cache_key,
            page_number=page_number,
        )
    except Exception as exc:
        return _office_error(exc)
    return _private_png(page)


def _private_png(path: Path) -> FileResponse:
    return FileResponse(
        path=str(path),
        media_type="image/png",
        filename=f"office-preview-{path.name}",
        content_disposition_type="inline",
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )


__all__ = ["OfficeRenderBody", "router"]
