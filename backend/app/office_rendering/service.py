"""Session/workspace-bound Office preview service."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.checkpoint_change import CheckpointChange
from app.models.session import Session
from app.models.session_checkpoint import SessionCheckpoint
from app.models.workspace_instance import WorkspaceInstance
from app.office_rendering.cache import OfficeRenderCache
from app.office_rendering.models import RenderManifest, RendererDescriptor, RenderRequest
from app.office_rendering.provider import OfficeRenderProvider
from app.storage.checkpoints import inspect_workspace_identity


class OfficePreviewError(RuntimeError):
    code = "office_preview_error"


class OfficePreviewDisabledError(OfficePreviewError):
    code = "office_v2_disabled"


class OfficePreviewNotFoundError(OfficePreviewError):
    code = "office_preview_not_found"


class OfficePreviewProvenanceError(OfficePreviewError):
    code = "office_preview_provenance"


class OfficePreviewStaleError(OfficePreviewError):
    code = "office_preview_stale"


class OfficePreviewBusyError(OfficePreviewError):
    code = "office_preview_busy"


@dataclass(frozen=True, slots=True)
class OfficePreviewBinding:
    session_id: str
    workspace_instance_id: str
    relative_path: str
    source_sha256: str
    checkpoint_id: str | None
    root_turn_id: str | None
    manifest: RenderManifest

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "workspace_instance_id": self.workspace_instance_id,
            "relative_path": self.relative_path,
            "source_sha256": self.source_sha256,
            "checkpoint_id": self.checkpoint_id,
            "root_turn_id": self.root_turn_id,
            "manifest": self.manifest.to_dict(),
            "preview_quality": self.manifest.quality,
            "formula_values_recalculated": False,
        }


@dataclass(frozen=True, slots=True)
class OfficePreviewContext:
    """Public, path-free identity for the current session preview boundary."""

    session_id: str
    workspace_instance_id: str
    renderer_available: bool
    renderer_id: str
    renderer_version: str
    font_digest: str
    preview_quality: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "workspace_instance_id": self.workspace_instance_id,
            "renderer_available": self.renderer_available,
            "renderer_id": self.renderer_id,
            "renderer_version": self.renderer_version,
            "font_digest": self.font_digest,
            "preview_quality": self.preview_quality,
            "formula_values_recalculated": False,
        }


@dataclass(frozen=True, slots=True)
class OfficeValidationStatus:
    """Path-free freshness projection for checkpoint-owned Office evidence."""

    session_id: str
    workspace_instance_id: str
    relative_path: str
    source_sha256: str
    status: str
    stale_reason: str | None
    report: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "workspace_instance_id": self.workspace_instance_id,
            "relative_path": self.relative_path,
            "source_sha256": self.source_sha256,
            "status": self.status,
            "stale_reason": self.stale_reason,
            "report": self.report,
        }


@dataclass(frozen=True, slots=True)
class OfficePreviewValidationSnapshot:
    """Server-internal paths and identities for deterministic validation.

    This contract is intentionally not returned by the HTTP API.  Callers must
    obtain it from :class:`OfficePreviewService`, which revalidates the current
    workspace source and the complete private cache entry before exposing the
    paths to another trusted server component.
    """

    binding: OfficePreviewBinding
    source_path: Path
    entry_path: Path


@dataclass(frozen=True, slots=True)
class _ResolvedSource:
    session_id: str
    workspace_instance_id: str
    workspace_root: Path
    relative_path: str
    source_path: Path
    source_sha256: str
    document_format: str
    checkpoint_id: str | None
    root_turn_id: str | None


class OfficePreviewService:
    """Render only the current bytes owned by a durable workspace instance."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        cache: OfficeRenderCache,
        provider: OfficeRenderProvider,
        parameters_version: str,
        parameters: Mapping[str, Any],
        max_source_bytes: int = 512 * 1024 * 1024,
        max_concurrent_renders: int = 1,
        render_admission_timeout_seconds: float = 5.0,
        enabled: bool | None = None,
    ) -> None:
        if not isinstance(cache, OfficeRenderCache):
            raise TypeError("Office preview cache is invalid")
        if not isinstance(provider, OfficeRenderProvider):
            raise TypeError("Office preview provider is invalid")
        if not isinstance(parameters_version, str) or not parameters_version.strip():
            raise ValueError("Office preview parameters_version is required")
        if (
            not isinstance(max_source_bytes, int)
            or isinstance(max_source_bytes, bool)
            or max_source_bytes < 1
        ):
            raise ValueError("Office preview source budget must be positive")
        if (
            not isinstance(max_concurrent_renders, int)
            or isinstance(max_concurrent_renders, bool)
            or max_concurrent_renders < 1
            or max_concurrent_renders > 8
        ):
            raise ValueError("Office preview render concurrency is invalid")
        if (
            isinstance(render_admission_timeout_seconds, bool)
            or not isinstance(render_admission_timeout_seconds, (int, float))
            or not 0 < float(render_admission_timeout_seconds) <= 30
        ):
            raise ValueError("Office preview admission timeout is invalid")
        self.session_factory = session_factory
        self.cache = cache
        self.provider = provider
        self.parameters_version = parameters_version
        self.parameters = dict(parameters)
        self.max_source_bytes = max_source_bytes
        self._render_slots = asyncio.Semaphore(max_concurrent_renders)
        self._render_admission_timeout_seconds = float(
            render_admission_timeout_seconds
        )
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        if self._enabled is not None:
            return self._enabled
        from app import release_features

        return bool(release_features.V11_OFFICE_V2_RELEASED)

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise OfficePreviewDisabledError("Office v1.1 preview is not released")

    async def context(self, *, session_id: str) -> OfficePreviewContext:
        """Resolve the server-owned active workspace without exposing its path."""

        self._require_enabled()
        async with self.session_factory() as db:
            session = await db.get(Session, session_id)
            if session is None:
                raise OfficePreviewNotFoundError("Session was not found")
            raw_session_root = session.directory
            if not raw_session_root or raw_session_root == ".":
                raise OfficePreviewProvenanceError(
                    "Session has no selected workspace"
                )
            try:
                canonical_root, identity = inspect_workspace_identity(
                    raw_session_root
                )
            except Exception as exc:
                raise OfficePreviewProvenanceError(
                    "Session workspace identity is unavailable"
                ) from exc
            candidates = list(
                (
                    await db.execute(
                        select(WorkspaceInstance)
                        .where(
                            WorkspaceInstance.root_path == canonical_root,
                            WorkspaceInstance.identity_token == identity,
                            WorkspaceInstance.status == "active",
                        )
                        .order_by(WorkspaceInstance.time_created.desc())
                        .limit(8)
                    )
                ).scalars()
            )
        instance = next(
            (
                item
                for item in candidates
                if not (
                    session.project_id is not None
                    and item.project_id is not None
                    and session.project_id != item.project_id
                )
            ),
            None,
        )
        if instance is None:
            raise OfficePreviewNotFoundError(
                "Active workspace instance was not found"
            )
        descriptor = self._descriptor()
        try:
            availability = self.provider.availability()
            renderer_available = availability.available is True
        except Exception:
            renderer_available = False
        return OfficePreviewContext(
            session_id=session_id,
            workspace_instance_id=instance.id,
            renderer_available=renderer_available,
            renderer_id=descriptor.renderer_id,
            renderer_version=descriptor.renderer_version,
            font_digest=descriptor.font_digest,
            preview_quality=descriptor.quality,
        )

    async def render(
        self,
        *,
        session_id: str,
        workspace_instance_id: str,
        relative_path: str,
        expected_source_sha256: str | None = None,
    ) -> OfficePreviewBinding:
        self._require_enabled()
        source = await self._resolve_source(
            session_id=session_id,
            workspace_instance_id=workspace_instance_id,
            relative_path=relative_path,
        )
        if (
            expected_source_sha256 is not None
            and expected_source_sha256 != source.source_sha256
        ):
            raise OfficePreviewStaleError(
                "Office source changed before preview rendering"
            )
        request = self._request(source)
        try:
            await asyncio.wait_for(
                self._render_slots.acquire(),
                timeout=self._render_admission_timeout_seconds,
            )
        except TimeoutError as exc:
            raise OfficePreviewBusyError(
                "Office renderer is at its local concurrency limit"
            ) from exc
        try:
            manifest = await self.cache.get_or_render(request, self.provider)
        finally:
            self._render_slots.release()
        return OfficePreviewBinding(
            session_id=source.session_id,
            workspace_instance_id=source.workspace_instance_id,
            relative_path=source.relative_path,
            source_sha256=source.source_sha256,
            checkpoint_id=source.checkpoint_id,
            root_turn_id=source.root_turn_id,
            manifest=manifest,
        )

    async def validation_status(
        self,
        *,
        session_id: str,
        workspace_instance_id: str,
        relative_path: str,
    ) -> OfficeValidationStatus:
        """Return current/stale validation evidence without exposing cache paths."""

        self._require_enabled()
        source = await self._resolve_source(
            session_id=session_id,
            workspace_instance_id=workspace_instance_id,
            relative_path=relative_path,
        )
        async with self.session_factory() as db:
            rows = list(
                (
                    await db.execute(
                        select(SessionCheckpoint, CheckpointChange)
                        .join(
                            CheckpointChange,
                            CheckpointChange.checkpoint_id == SessionCheckpoint.id,
                        )
                        .where(
                            SessionCheckpoint.session_id == source.session_id,
                            SessionCheckpoint.workspace_instance_id
                            == source.workspace_instance_id,
                            CheckpointChange.relative_path == source.relative_path,
                        )
                        .order_by(
                            SessionCheckpoint.sequence.desc(),
                            CheckpointChange.sequence.desc(),
                        )
                        .limit(1_000)
                    )
                ).all()
            )
        newest_change_id = rows[0][1].id if rows else None
        evidence_row: tuple[SessionCheckpoint, CheckpointChange] | None = next(
            (
                (checkpoint, change)
                for checkpoint, change in rows
                if isinstance(
                    dict(change.details or {}).get("office_validation"),
                    dict,
                )
            ),
            None,
        )
        if evidence_row is None:
            return OfficeValidationStatus(
                session_id=source.session_id,
                workspace_instance_id=source.workspace_instance_id,
                relative_path=source.relative_path,
                source_sha256=source.source_sha256,
                status="unvalidated",
                stale_reason=None,
                report=None,
            )

        checkpoint, change = evidence_row
        raw_report = dict(change.details or {}).get("office_validation")
        from app.office_validation import (
            OfficeValidationContractError,
            OfficeValidationReport,
        )

        try:
            report = OfficeValidationReport.from_dict(raw_report)
        except OfficeValidationContractError:
            return OfficeValidationStatus(
                session_id=source.session_id,
                workspace_instance_id=source.workspace_instance_id,
                relative_path=source.relative_path,
                source_sha256=source.source_sha256,
                status="invalid",
                stale_reason="evidence_contract_invalid",
                report=None,
            )

        authoritative_checks = tuple(
            check
            for check in report.checks
            if check.code == "authoritative_quality"
        )
        if (
            report.verdict != "pass"
            or len(authoritative_checks) != 1
            or authoritative_checks[0].outcome != "pass"
            or change.node_kind != "file"
            or change.after_sha256 != report.candidate_sha256
            or report.document_format != source.document_format
        ):
            return OfficeValidationStatus(
                session_id=source.session_id,
                workspace_instance_id=source.workspace_instance_id,
                relative_path=source.relative_path,
                source_sha256=source.source_sha256,
                status="invalid",
                stale_reason="evidence_binding_invalid",
                report=None,
            )

        stale_reason: str | None = None
        if checkpoint.state != "finalized" or checkpoint.pin_state != "pinned":
            stale_reason = "checkpoint_not_current"
        elif change.id != newest_change_id:
            stale_reason = "newer_path_change"
        elif report.candidate_sha256 != source.source_sha256:
            stale_reason = "source_changed"
        elif (
            report.checkpoint_id != checkpoint.id
            or report.root_turn_id != checkpoint.root_turn_id
            or source.checkpoint_id != checkpoint.id
        ):
            stale_reason = "checkpoint_binding_changed"
        return OfficeValidationStatus(
            session_id=source.session_id,
            workspace_instance_id=source.workspace_instance_id,
            relative_path=source.relative_path,
            source_sha256=source.source_sha256,
            status="authoritative_pass" if stale_reason is None else "stale",
            stale_reason=stale_reason,
            report=report.to_dict(),
        )

    async def page_path(
        self,
        *,
        session_id: str,
        workspace_instance_id: str,
        relative_path: str,
        cache_key: str,
        page_number: int,
    ) -> Path:
        """Revalidate the current source before exposing a private cached page."""

        self._require_enabled()
        source = await self._resolve_source(
            session_id=session_id,
            workspace_instance_id=workspace_instance_id,
            relative_path=relative_path,
        )
        request = self._request(source)
        manifest = self.cache.load(request, self._descriptor())
        if manifest is None or manifest.cache_key != cache_key:
            raise OfficePreviewStaleError("Office preview is not current")
        page = self.cache.page_path(request, self._descriptor(), page_number)
        if page is None:
            raise OfficePreviewNotFoundError("Office preview page was not found")
        return page

    async def entry_path(
        self,
        *,
        session_id: str,
        workspace_instance_id: str,
        relative_path: str,
        cache_key: str,
    ) -> Path:
        """Return a fully revalidated entry for deterministic visual checking."""

        self._require_enabled()
        source = await self._resolve_source(
            session_id=session_id,
            workspace_instance_id=workspace_instance_id,
            relative_path=relative_path,
        )
        request = self._request(source)
        manifest = self.cache.load(request, self._descriptor())
        if manifest is None or manifest.cache_key != cache_key:
            raise OfficePreviewStaleError("Office preview is not current")
        entry = self.cache.entry_path(request, self._descriptor())
        if entry is None:
            raise OfficePreviewNotFoundError("Office preview entry was not found")
        return entry

    async def validation_snapshot(
        self,
        *,
        session_id: str,
        workspace_instance_id: str,
        relative_path: str,
        expected_source_sha256: str,
        expected_cache_key: str,
        expected_checkpoint_id: str | None,
        expected_root_turn_id: str | None,
    ) -> OfficePreviewValidationSnapshot:
        """Return a path-bearing snapshot only after exact identity rechecks.

        The expected values come from a prior server-owned ``render`` result.
        Any edit, rewind, cache replacement, renderer change, or checkpoint
        rebinding between the two calls fails closed.
        """

        self._require_enabled()
        source = await self._resolve_source(
            session_id=session_id,
            workspace_instance_id=workspace_instance_id,
            relative_path=relative_path,
        )
        if (
            source.source_sha256 != expected_source_sha256
            or source.checkpoint_id != expected_checkpoint_id
            or source.root_turn_id != expected_root_turn_id
        ):
            raise OfficePreviewStaleError(
                "Office source or checkpoint changed before validation"
            )
        request = self._request(source)
        descriptor = self._descriptor()
        manifest = self.cache.load(request, descriptor)
        if manifest is None or manifest.cache_key != expected_cache_key:
            raise OfficePreviewStaleError(
                "Office render cache changed before validation"
            )
        entry = self.cache.entry_path(request, descriptor)
        if entry is None:
            raise OfficePreviewNotFoundError(
                "Office preview entry was not found"
            )
        binding = OfficePreviewBinding(
            session_id=source.session_id,
            workspace_instance_id=source.workspace_instance_id,
            relative_path=source.relative_path,
            source_sha256=source.source_sha256,
            checkpoint_id=source.checkpoint_id,
            root_turn_id=source.root_turn_id,
            manifest=manifest,
        )
        return OfficePreviewValidationSnapshot(
            binding=binding,
            source_path=source.source_path,
            entry_path=entry,
        )

    def _descriptor(self) -> RendererDescriptor:
        descriptor = self.provider.descriptor
        if not isinstance(descriptor, RendererDescriptor):
            raise OfficePreviewProvenanceError(
                "Office renderer identity is unavailable"
            )
        return descriptor

    def _request(self, source: _ResolvedSource) -> RenderRequest:
        return RenderRequest(
            workspace_root=source.workspace_root,
            source_path=source.source_path,
            document_format=source.document_format,  # type: ignore[arg-type]
            source_sha256=source.source_sha256,
            parameters_version=self.parameters_version,
            parameters=self.parameters,
        )

    async def _resolve_source(
        self,
        *,
        session_id: str,
        workspace_instance_id: str,
        relative_path: str,
    ) -> _ResolvedSource:
        canonical_relative = _canonical_relative_path(relative_path)
        async with self.session_factory() as db:
            session = await db.get(Session, session_id)
            instance = await db.get(WorkspaceInstance, workspace_instance_id)
            if session is None or instance is None:
                raise OfficePreviewNotFoundError(
                    "Session or workspace instance was not found"
                )
            if instance.status != "active":
                raise OfficePreviewProvenanceError(
                    "Workspace instance is not active"
                )
            if (
                session.project_id is not None
                and instance.project_id is not None
                and session.project_id != instance.project_id
            ):
                raise OfficePreviewProvenanceError(
                    "Session and workspace project provenance differ"
                )
            raw_session_root = session.directory
            if not raw_session_root or raw_session_root == ".":
                raise OfficePreviewProvenanceError(
                    "Session has no selected workspace"
                )
            canonical_root, identity = inspect_workspace_identity(raw_session_root)
            if (
                canonical_root != instance.root_path
                or identity != instance.identity_token
            ):
                raise OfficePreviewProvenanceError(
                    "Session is bound to a different workspace instance"
                )
        workspace_root = Path(canonical_root)
        source_path = workspace_root.joinpath(*PurePosixPath(canonical_relative).parts)
        source_sha256 = _hash_current_source(
            workspace_root,
            source_path,
            max_bytes=self.max_source_bytes,
        )
        suffix = source_path.suffix.lower()
        if suffix not in {".docx", ".xlsx", ".pptx"}:
            raise OfficePreviewProvenanceError(
                "Office preview requires DOCX, XLSX, or PPTX"
            )
        # Bind a preview to a checkpoint only when that checkpoint's durable
        # after-digest is exactly the bytes just hashed.  A rewind without a
        # persisted per-path restored digest remains deliberately unbound
        # rather than pointing the UI at a stale historical version.
        async with self.session_factory() as db:
            checkpoint_id, root_turn_id = await _current_checkpoint_binding(
                db,
                session_id=session_id,
                workspace_instance_id=workspace_instance_id,
                relative_path=canonical_relative,
                source_sha256=source_sha256,
            )
        return _ResolvedSource(
            session_id=session_id,
            workspace_instance_id=workspace_instance_id,
            workspace_root=workspace_root,
            relative_path=canonical_relative,
            source_path=source_path,
            source_sha256=source_sha256,
            document_format=suffix[1:],
            checkpoint_id=checkpoint_id,
            root_turn_id=root_turn_id,
        )


async def _current_checkpoint_binding(
    db: AsyncSession,
    *,
    session_id: str,
    workspace_instance_id: str,
    relative_path: str,
    source_sha256: str,
) -> tuple[str | None, str | None]:
    row = (
        await db.execute(
            select(SessionCheckpoint)
            .join(
                CheckpointChange,
                CheckpointChange.checkpoint_id == SessionCheckpoint.id,
            )
            .where(
                SessionCheckpoint.session_id == session_id,
                SessionCheckpoint.workspace_instance_id == workspace_instance_id,
                SessionCheckpoint.state == "finalized",
                CheckpointChange.relative_path == relative_path,
                CheckpointChange.after_sha256 == source_sha256,
            )
            .order_by(SessionCheckpoint.sequence.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is not None:
        return row.id, row.root_turn_id

    # A rewind restores the state *before* its target turn, so no forward
    # CheckpointChange.after_sha256 represents that version.  The rewind
    # transaction persists its exact desired per-path digest on the target
    # checkpoint; bind only when that digest still equals the current source.
    rewound = list(
        (
            await db.execute(
                select(SessionCheckpoint)
                .where(
                    SessionCheckpoint.session_id == session_id,
                    SessionCheckpoint.workspace_instance_id == workspace_instance_id,
                    SessionCheckpoint.state == "rewound",
                )
                .order_by(SessionCheckpoint.sequence.desc())
                .limit(100)
            )
        ).scalars()
    )
    for checkpoint in rewound:
        raw_result = dict(checkpoint.details or {}).get("rewind_result")
        if not isinstance(raw_result, dict):
            continue
        restored = raw_result.get("restored_paths")
        if not isinstance(restored, list) or len(restored) > 50_000:
            continue
        for item in restored:
            if (
                isinstance(item, dict)
                and item.get("relative_path") == relative_path
                and item.get("exists") is True
                and item.get("node_kind") == "file"
                and item.get("sha256") == source_sha256
            ):
                return checkpoint.id, checkpoint.root_turn_id
    return None, None


def _canonical_relative_path(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or "\\" in value
    ):
        raise OfficePreviewProvenanceError("Office relative path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise OfficePreviewProvenanceError("Office relative path escapes workspace")
    return path.as_posix()


def _hash_current_source(root: Path, source: Path, *, max_bytes: int) -> str:
    try:
        resolved_root = root.resolve(strict=True)
        resolved_source = source.resolve(strict=True)
        resolved_source.relative_to(resolved_root)
        before = source.lstat()
    except (OSError, ValueError) as exc:
        raise OfficePreviewNotFoundError("Office source was not found") from exc
    if (
        resolved_source != source
        or not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
    ):
        raise OfficePreviewProvenanceError(
            "Office source cannot traverse symbolic links"
        )
    if before.st_size < 1 or before.st_size > max_bytes:
        raise OfficePreviewProvenanceError("Office source exceeds its byte budget")
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise OfficePreviewProvenanceError(
            "Office source cannot be opened safely"
        ) from exc
    digest = hashlib.sha256()
    size = 0
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise OfficePreviewProvenanceError(
                "Office source changed while opening"
            )
        while chunk := os.read(descriptor, min(1024 * 1024, max_bytes - size + 1)):
            size += len(chunk)
            if size > max_bytes:
                raise OfficePreviewProvenanceError(
                    "Office source exceeds its byte budget"
                )
            digest.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        visible = source.lstat()
    except OSError as exc:
        raise OfficePreviewStaleError(
            "Office source changed while hashing"
        ) from exc
    if (
        size != opened.st_size
        or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        or (visible.st_dev, visible.st_ino) != (after.st_dev, after.st_ino)
    ):
        raise OfficePreviewStaleError("Office source changed while hashing")
    return digest.hexdigest()


__all__ = [
    "OfficePreviewBinding",
    "OfficePreviewBusyError",
    "OfficePreviewContext",
    "OfficePreviewDisabledError",
    "OfficePreviewError",
    "OfficePreviewNotFoundError",
    "OfficePreviewProvenanceError",
    "OfficePreviewService",
    "OfficePreviewStaleError",
    "OfficePreviewValidationSnapshot",
]
