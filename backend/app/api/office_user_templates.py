"""Local, path-free API for workspace-scoped user Office template Beta."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
import re
from typing import Any, NoReturn

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.local import require_local_session
from app.dependencies import SessionFactoryDep
from app.models.office_user_template import OfficeUserTemplate
from app.models.session import Session
from app.models.workspace_instance import WorkspaceInstance
from app.office_rendering import OfficePreviewService
from app.office_rendering.models import canonical_json_bytes
from app.office_templates.errors import (
    OfficeTemplateError,
    TemplateContractError,
    TemplateIntegrityError,
    TemplateSecurityError,
)
from app.office_templates.user import (
    USER_TEMPLATE_RECONCILIATION_MAX_OWNERS,
    USER_TEMPLATE_RECONCILIATION_MAX_RECORDS,
    UserOfficeTemplateService,
    UserTemplateEvidenceError,
    UserTemplateFeatureDisabledError,
    UserTemplateImportCandidate,
    UserTemplatePlaceholder,
    UserTemplateReopenError,
    decode_user_template_placeholder_schema,
    set_user_office_template_service,
    validate_user_template_ref,
)
from app.office_validation import OfficeValidationError
from app.office_validation.draft import OfficeDraftValidationService
from app.office_validation.precommit import OfficePrecommitCoordinator
from app.release_readiness import v11_capability_released
from app.runtime.v11_readiness import v11_runtime_readiness
from app.security.audit import AuditPersistenceError, record_security_event
from app.storage.checkpoints import inspect_workspace_identity
from app.storage.file_versions import default_file_version_storage_root


logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/office-v2/user-templates",
    dependencies=[Depends(require_local_session)],
)

_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_PLACEHOLDER_REQUIRED_FIELDS = {
    "name",
    "type",
    "required",
    "min_chars",
    "max_chars",
}
_PLACEHOLDER_OPTIONAL_FIELDS = {"description"}


class _StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UserTemplateApprovalBody(_StrictBody):
    session_id: str = Field(min_length=1, max_length=128)
    workspace_instance_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1)
    expected_state_version: int = Field(ge=1)
    expected_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_render_cache_key: str = Field(pattern=r"^[0-9a-f]{64}$")


class UserTemplateDeleteBody(_StrictBody):
    session_id: str = Field(min_length=1, max_length=128)
    workspace_instance_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1)
    expected_state_version: int = Field(ge=1)


class _UserTemplateRuntimeUnavailable(RuntimeError):
    pass


class _UserTemplateNotFound(RuntimeError):
    pass


class _UserTemplateProvenance(RuntimeError):
    pass


class _UserTemplateConflict(RuntimeError):
    pass


def _lifecycle_lock(request: Request) -> asyncio.Lock:
    lock = getattr(
        request.app.state,
        "office_user_template_lifecycle_lock",
        None,
    )
    if lock is None:
        lock = asyncio.Lock()
        request.app.state.office_user_template_lifecycle_lock = lock
    if not isinstance(lock, asyncio.Lock):
        raise _UserTemplateRuntimeUnavailable
    return lock


def _runtime_can_instantiate(
    request: Request,
    record: OfficeUserTemplate | None = None,
) -> bool:
    try:
        readiness = v11_runtime_readiness(request.app.state)
        if readiness["user_office_templates"]["runtime_ready"] is not True:
            return False
        service = getattr(
            request.app.state,
            "office_user_template_service",
            None,
        )
        coordinator = getattr(
            request.app.state,
            "office_precommit_coordinator",
            None,
        )
        preview = getattr(request.app.state, "office_preview_service", None)
        provider = getattr(preview, "provider", None)
        descriptor = getattr(provider, "descriptor", None)
        availability = provider.availability()
        draft = getattr(service, "_draft", None)
        ready = (
            isinstance(service, UserOfficeTemplateService)
            and isinstance(coordinator, OfficePrecommitCoordinator)
            and isinstance(draft, OfficeDraftValidationService)
            and getattr(draft, "_provider", None) is provider
            and getattr(coordinator, "_service", None) is draft
            and getattr(descriptor, "quality", None) == "authoritative"
            and getattr(availability, "available", None) is True
            and getattr(request.app.state, "validation_agent_service", None)
            is not None
        )
        if not ready or record is None:
            return ready
        parameters = getattr(draft, "_parameters", None)
        parameters_sha256 = hashlib.sha256(
            canonical_json_bytes(parameters)
        ).hexdigest()
        return (
            record.render_quality == "authoritative"
            and record.renderer_id == descriptor.renderer_id
            and record.renderer_version == descriptor.renderer_version
            and record.font_digest == descriptor.font_digest
            and record.render_parameters_version
            == getattr(draft, "_parameters_version", None)
            and record.render_parameters_sha256 == parameters_sha256
        )
    except Exception:
        return False


async def _can_instantiate(
    request: Request,
    record: OfficeUserTemplate,
) -> bool:
    """Require runtime readiness and the record's current immutable source.

    The Office tool repeats this check before generation and commit.  Doing a
    read-only copy here keeps the desktop availability claim truthful when a
    DB owner survives local registry loss or corruption.
    """

    if (
        record.status != "approved"
        or not _runtime_can_instantiate(request, record)
    ):
        return False
    service = getattr(request.app.state, "office_user_template_service", None)
    if not isinstance(service, UserOfficeTemplateService):
        return False
    try:
        await asyncio.to_thread(
            service.verify_registry_contract,
            template_ref=record.template_ref,
            revision=record.revision,
            source_sha256=record.source_sha256,
            manifest_sha256=record.manifest_sha256,
            format_name=record.format,
            placeholder_schema=decode_user_template_placeholder_schema(
                record.placeholder_schema
            ),
            placeholder_parts=tuple(record.placeholder_parts),
        )
    except Exception:
        return False
    return True


def _audit_details(
    *,
    workspace_instance_id: str,
    template_ref: str | None = None,
    revision: int | None = None,
    state_version: int | None = None,
    format_name: str | None = None,
    idempotent: bool | None = None,
) -> dict[str, Any]:
    """Return the fixed, path-free audit projection for this API."""

    details: dict[str, Any] = {
        "workspace_instance_id": workspace_instance_id,
    }
    if template_ref is not None:
        try:
            details["template_ref"] = validate_user_template_ref(template_ref)
        except TemplateContractError:
            pass
    if isinstance(revision, int) and not isinstance(revision, bool) and revision >= 1:
        details["revision"] = revision
    if (
        isinstance(state_version, int)
        and not isinstance(state_version, bool)
        and state_version >= 1
    ):
        details["state_version"] = state_version
    if format_name in {"docx", "xlsx", "pptx"}:
        details["format"] = format_name
    if idempotent is not None:
        details["idempotent"] = bool(idempotent)
    return details


async def _record_template_audit(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    action: str,
    outcome: str,
    session_id: str,
    details: dict[str, Any],
    required: bool = False,
) -> None:
    try:
        await record_security_event(
            session_factory,
            source_kind="office_user_template",
            source_id="desktop",
            invocation_source_kind="desktop",
            capability="user_office_templates",
            action=action,
            decision="system",
            outcome=outcome,
            session_id=session_id,
            details=details,
            required=required,
        )
    except Exception as exc:
        if required:
            if isinstance(exc, AuditPersistenceError):
                raise
            raise AuditPersistenceError(
                "Required user-template audit event could not be persisted"
            ) from exc
        logger.warning("Could not persist user-template outcome audit")


def _audit_failure_outcome(exc: BaseException) -> str:
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled"
    if isinstance(
        exc,
        (
            UserTemplateFeatureDisabledError,
            _UserTemplateNotFound,
            _UserTemplateProvenance,
            _UserTemplateConflict,
            TemplateContractError,
            TemplateSecurityError,
            UserTemplateEvidenceError,
            TemplateIntegrityError,
            OfficeTemplateError,
            OfficeValidationError,
        ),
    ):
        return "blocked"
    return "error"


def _error(status_code: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"code": code, "detail": detail},
        headers={"Cache-Control": "no-store"},
    )


def _require_feature() -> None:
    if not v11_capability_released("user_office_templates"):
        raise UserTemplateFeatureDisabledError(
            "user Office templates are not released"
        )


async def _load_global_template_owners(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[tuple[str, int, str], ...]:
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(
                    OfficeUserTemplate.template_ref,
                    OfficeUserTemplate.revision,
                    OfficeUserTemplate.status,
                )
                .order_by(
                    OfficeUserTemplate.template_ref,
                    OfficeUserTemplate.revision,
                )
                .limit(USER_TEMPLATE_RECONCILIATION_MAX_OWNERS + 1)
            )
        ).all()
    return tuple((item[0], item[1], item[2]) for item in rows)


async def _service_locked(
    request: Request,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    reconcile: bool,
) -> UserOfficeTemplateService:
    """Resolve a service and optionally reconcile it under the lifecycle lock."""

    _require_feature()
    service = getattr(request.app.state, "office_user_template_service", None)
    if not isinstance(service, UserOfficeTemplateService):
        # GET/list/preview must never create private directories or run an
        # orphan-deleting reconciliation as an unaudited side effect.
        if not reconcile:
            raise _UserTemplateRuntimeUnavailable
        preview = getattr(request.app.state, "office_preview_service", None)
        if not isinstance(preview, OfficePreviewService):
            raise _UserTemplateRuntimeUnavailable
        try:
            draft = OfficeDraftValidationService(
                cache=preview.cache,
                provider=preview.provider,
                parameters_version=preview.parameters_version,
                parameters=preview.parameters,
            )
            service = UserOfficeTemplateService(
                (
                    default_file_version_storage_root().parent
                    / "v1.1"
                    / "office-user-templates"
                ).absolute(),
                draft_validation=draft,
            )
        except (
            OfficeTemplateError,
            OfficeValidationError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            raise _UserTemplateRuntimeUnavailable from exc

    reconciled = getattr(
        request.app.state,
        "office_user_template_reconciled_service",
        None,
    )
    if reconcile and reconciled is not service:
        try:
            await service.reconcile_registry_orphans_once(
                lambda: _load_global_template_owners(session_factory)
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _UserTemplateRuntimeUnavailable from exc
        request.app.state.office_user_template_reconciled_service = service

    request.app.state.office_user_template_service = service
    set_user_office_template_service(service)
    return service


async def _service(
    request: Request,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    reconcile: bool = False,
) -> UserOfficeTemplateService:
    async with _lifecycle_lock(request):
        return await _service_locked(
            request,
            session_factory,
            reconcile=reconcile,
        )


def _raise_json_constant(value: str) -> NoReturn:
    raise ValueError(f"unsupported JSON constant: {value}")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _placeholder_schema(value: str) -> tuple[UserTemplatePlaceholder, ...]:
    if not isinstance(value, str) or not 1 <= len(value) <= 512 * 1024:
        raise TemplateContractError("placeholder schema is invalid")
    try:
        raw = json.loads(
            value,
            object_pairs_hook=_unique_json_object,
            parse_constant=_raise_json_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TemplateContractError("placeholder schema is invalid") from exc
    if not isinstance(raw, list):
        raise TemplateContractError("placeholder schema must be a list")
    fields: list[UserTemplatePlaceholder] = []
    for item in raw:
        if not isinstance(item, dict):
            raise TemplateContractError("placeholder schema entry is invalid")
        keys = set(item)
        if (
            not _PLACEHOLDER_REQUIRED_FIELDS.issubset(keys)
            or not keys.issubset(
                _PLACEHOLDER_REQUIRED_FIELDS | _PLACEHOLDER_OPTIONAL_FIELDS
            )
        ):
            raise TemplateContractError("placeholder schema fields are invalid")
        fields.append(
            UserTemplatePlaceholder(
                name=item["name"],
                value_type=item["type"],
                required=item["required"],
                min_chars=item["min_chars"],
                max_chars=item["max_chars"],
                description=item.get("description", ""),
            )
        )
    return tuple(fields)


async def _require_workspace_binding(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    session_id: str,
    workspace_instance_id: str,
) -> None:
    """Require the current session directory and durable instance to coincide."""

    async with session_factory() as db:
        session = await db.get(Session, session_id)
        instance = await db.get(WorkspaceInstance, workspace_instance_id)
        if session is None or instance is None:
            raise _UserTemplateNotFound
        directory = session.directory
        instance_root = instance.root_path
        identity_token = instance.identity_token
        if (
            session.time_archived is not None
            or not directory
            or directory == "."
            or instance.status != "active"
            or dict(instance.details or {}).get("release_intent") is not None
            or session.project_id != instance.project_id
        ):
            raise _UserTemplateProvenance
    try:
        canonical, identity = await asyncio.to_thread(
            inspect_workspace_identity,
            directory,
        )
    except Exception as exc:
        raise _UserTemplateProvenance from exc
    if canonical != instance_root or identity != identity_token:
        raise _UserTemplateProvenance


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _public_record(
    record: OfficeUserTemplate,
    *,
    runtime_ready: bool,
) -> dict[str, Any]:
    """Return only opaque identifiers, immutable evidence and safe state."""

    return {
        "template_ref": record.template_ref,
        "revision": record.revision,
        "state_version": record.state_version,
        "display_name": record.display_name,
        "format": record.format,
        "source": {
            "sha256": record.source_sha256,
            "size_bytes": record.source_size_bytes,
            "manifest_sha256": record.manifest_sha256,
        },
        "placeholder_schema": list(record.placeholder_schema),
        "allowed_operations": list(record.allowed_operations),
        "status": record.status,
        "can_approve": (
            record.status == "needs_confirmation"
            and record.render_quality == "authoritative"
        ),
        "can_instantiate": record.status == "approved" and runtime_ready,
        "render_evidence": {
            "quality": record.render_quality,
            "renderer_id": record.renderer_id,
            "renderer_version": record.renderer_version,
            "font_digest": record.font_digest,
            "parameters_version": record.render_parameters_version,
            "parameters_sha256": record.render_parameters_sha256,
            "cache_key": record.render_cache_key,
            "manifest_sha256": record.render_manifest_sha256,
            "page_count": record.render_page_count,
        },
        "validation_report": dict(record.validation_report),
        "time_created": _iso(record.time_created),
        "time_updated": _iso(record.time_updated),
        "time_approved": _iso(record.time_approved),
        "time_tombstoned": _iso(record.time_tombstoned),
        "beta": True,
    }


def _record_from_candidate(
    candidate: UserTemplateImportCandidate,
    *,
    session_id: str,
    workspace_instance_id: str,
    idempotency_key: str,
) -> OfficeUserTemplate:
    manifest = candidate.render_manifest
    return OfficeUserTemplate(
        template_ref=candidate.template_ref,
        revision=candidate.revision,
        state_version=1,
        workspace_instance_id=workspace_instance_id,
        created_by_session_id=session_id,
        import_idempotency_key=idempotency_key,
        import_request_sha256=candidate.import_request_sha256,
        display_name=candidate.display_name,
        format=candidate.format,
        source_sha256=candidate.source_sha256,
        source_size_bytes=candidate.source_size_bytes,
        manifest_sha256=candidate.manifest_sha256,
        placeholder_schema=[field.to_dict() for field in candidate.placeholder_schema],
        placeholder_parts=list(candidate.placeholder_parts),
        allowed_operations=list(candidate.allowed_operations),
        status=candidate.status,
        render_quality=manifest.quality,
        renderer_id=manifest.renderer_id,
        renderer_version=manifest.renderer_version,
        font_digest=manifest.font_digest,
        render_parameters_version=manifest.parameters_version,
        render_parameters_sha256=manifest.parameters_sha256,
        render_cache_key=manifest.cache_key,
        render_manifest_sha256=candidate.render_manifest_sha256,
        render_page_count=len(manifest.pages),
        validation_report=dict(candidate.validation_report),
    )


async def _idempotent_existing(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    workspace_instance_id: str,
    idempotency_key: str,
    request_sha256: str,
) -> OfficeUserTemplate | None:
    async with session_factory() as db:
        existing = (
            await db.execute(
                select(OfficeUserTemplate).where(
                    OfficeUserTemplate.workspace_instance_id
                    == workspace_instance_id,
                    OfficeUserTemplate.import_idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            return None
        if existing.import_request_sha256 != request_sha256:
            raise _UserTemplateConflict
        if existing.status == "tombstoned":
            raise _UserTemplateConflict
        return existing


async def _discard_candidate(
    service: UserOfficeTemplateService,
    candidate: UserTemplateImportCandidate,
) -> None:
    try:
        await asyncio.shield(
            service.discard_orphan(candidate.template_ref, candidate.revision)
        )
    except OfficeTemplateError:
        # Cleanup failure must never replace the API's safe, path-free result.
        pass


def _api_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, AuditPersistenceError):
        return _error(
            503,
            "user_office_template_audit_unavailable",
            "The required pre-action audit record could not be persisted",
        )
    if isinstance(exc, UserTemplateFeatureDisabledError):
        return _error(
            404,
            "v11_user_office_templates_not_available",
            "User Office templates are not available in this release",
        )
    if isinstance(exc, _UserTemplateRuntimeUnavailable):
        return _error(
            503,
            "user_office_template_runtime_unavailable",
            "The approved local Office template runtime is unavailable",
        )
    if isinstance(exc, _UserTemplateNotFound):
        return _error(
            404,
            "user_office_template_not_found",
            "The user Office template resource was not found",
        )
    if isinstance(exc, _UserTemplateProvenance):
        return _error(
            409,
            "user_office_template_provenance_mismatch",
            "The request is not bound to the current verified workspace",
        )
    if isinstance(exc, _UserTemplateConflict):
        return _error(
            409,
            "user_office_template_conflict",
            "The user Office template state or request identity changed",
        )
    if isinstance(exc, (TemplateContractError, UserTemplateReopenError)):
        return _error(
            422,
            "user_office_template_invalid",
            "The Office template does not satisfy the required safe contract",
        )
    if isinstance(exc, TemplateSecurityError):
        return _error(
            422,
            "user_office_template_unsafe",
            "The Office template contains unsupported or unsafe package content",
        )
    if isinstance(
        exc,
        (UserTemplateEvidenceError, TemplateIntegrityError, OfficeValidationError),
    ):
        return _error(
            409,
            "user_office_template_evidence_invalid",
            "The Office template evidence could not be verified",
        )
    if isinstance(exc, OfficeTemplateError):
        return _error(
            409,
            "user_office_template_failed",
            "The Office template operation failed safely",
        )
    logger.exception("User Office template operation failed unexpectedly")
    return _error(
        500,
        "user_office_template_internal_error",
        "The Office template operation failed safely",
    )


@router.get("")
async def list_user_office_templates(
    request: Request,
    session_factory: SessionFactoryDep,
    session_id: str = Query(min_length=1, max_length=128),
    workspace_instance_id: str = Query(min_length=1, max_length=128),
) -> Any:
    try:
        _require_feature()
        await _require_workspace_binding(
            session_factory,
            session_id=session_id,
            workspace_instance_id=workspace_instance_id,
        )
        await _service(request, session_factory)
        async with session_factory() as db:
            records = list(
                (
                    await db.execute(
                        select(OfficeUserTemplate)
                        .where(
                            OfficeUserTemplate.workspace_instance_id
                            == workspace_instance_id,
                            OfficeUserTemplate.status != "tombstoned",
                        )
                        .order_by(
                            OfficeUserTemplate.time_created.desc(),
                            OfficeUserTemplate.template_ref,
                        )
                        .limit(USER_TEMPLATE_RECONCILIATION_MAX_RECORDS + 1)
                    )
                ).scalars()
            )
            if len(records) > USER_TEMPLATE_RECONCILIATION_MAX_RECORDS:
                raise UserTemplateEvidenceError(
                    "user template list exceeds its response budget"
                )
            templates: list[dict[str, Any]] = []
            for record in records:
                templates.append(
                    _public_record(
                        record,
                        runtime_ready=await _can_instantiate(request, record),
                    )
                )
            content = {
                "templates": templates,
                "beta": True,
            }
    except Exception as exc:
        return _api_error(exc)
    return JSONResponse(content=content, headers={"Cache-Control": "no-store"})


@router.post("/import")
async def import_user_office_template(
    request: Request,
    session_factory: SessionFactoryDep,
    file: UploadFile = File(...),
    session_id: str = Form(min_length=1, max_length=128),
    workspace_instance_id: str = Form(min_length=1, max_length=128),
    client_request_id: str = Form(min_length=1, max_length=160),
    display_name: str = Form(min_length=1, max_length=160),
    placeholder_schema: str = Form(min_length=1, max_length=512 * 1024),
) -> Any:
    candidate: UserTemplateImportCandidate | None = None
    service: UserOfficeTemplateService | None = None
    publication_started = False
    audit = _audit_details(
        workspace_instance_id=workspace_instance_id,
        revision=1,
    )
    try:
        _require_feature()
        if _IDEMPOTENCY_KEY.fullmatch(client_request_id) is None:
            raise TemplateContractError("client request id is invalid")
        schema = _placeholder_schema(placeholder_schema)
        await _require_workspace_binding(
            session_factory,
            session_id=session_id,
            workspace_instance_id=workspace_instance_id,
        )
        # Reconciliation and the complete registry -> DB publication window
        # share one app-owned lock.  No orphan sweep can observe a candidate
        # between immutable registry publication and durable DB ownership.
        async with _lifecycle_lock(request):
            await _record_template_audit(
                session_factory,
                action="import",
                outcome="started",
                session_id=session_id,
                details=audit,
                required=True,
            )
            service = await _service_locked(
                request,
                session_factory,
                reconcile=True,
            )
            candidate = await service.validate_and_register(
                file.file,
                filename=file.filename or "",
                display_name=display_name,
                placeholders=schema,
            )

            existing = await _idempotent_existing(
                session_factory,
                workspace_instance_id=workspace_instance_id,
                idempotency_key=client_request_id,
                request_sha256=candidate.import_request_sha256,
            )
            if existing is not None:
                await _discard_candidate(service, candidate)
                candidate = None
                success_audit = _audit_details(
                    workspace_instance_id=workspace_instance_id,
                    template_ref=existing.template_ref,
                    revision=existing.revision,
                    state_version=existing.state_version,
                    format_name=existing.format,
                    idempotent=True,
                )
                await _record_template_audit(
                    session_factory,
                    action="import",
                    outcome="success",
                    session_id=session_id,
                    details=success_audit,
                )
                return JSONResponse(
                    content={
                        "template": _public_record(
                            existing,
                            runtime_ready=await _can_instantiate(request, existing),
                        ),
                        "idempotent": True,
                    },
                    headers={"Cache-Control": "no-store"},
                )

            await _require_workspace_binding(
                session_factory,
                session_id=session_id,
                workspace_instance_id=workspace_instance_id,
            )
            record = _record_from_candidate(
                candidate,
                session_id=session_id,
                workspace_instance_id=workspace_instance_id,
                idempotency_key=client_request_id,
            )
            try:
                # Once commit starts, cancellation has an ambiguous outcome.
                # Retaining an orphan is safer than deleting bytes which may
                # already have a durable owner after cancellation.
                publication_started = True
                async with session_factory() as db:
                    async with db.begin():
                        db.add(record)
                        await db.flush()
                    content = _public_record(
                        record,
                        runtime_ready=await _can_instantiate(request, record),
                    )
            except IntegrityError:
                request_sha256 = candidate.import_request_sha256
                await _discard_candidate(service, candidate)
                candidate = None
                existing = await _idempotent_existing(
                    session_factory,
                    workspace_instance_id=workspace_instance_id,
                    idempotency_key=client_request_id,
                    request_sha256=request_sha256,
                )
                if existing is None:
                    raise _UserTemplateConflict
                success_audit = _audit_details(
                    workspace_instance_id=workspace_instance_id,
                    template_ref=existing.template_ref,
                    revision=existing.revision,
                    state_version=existing.state_version,
                    format_name=existing.format,
                    idempotent=True,
                )
                await _record_template_audit(
                    session_factory,
                    action="import",
                    outcome="success",
                    session_id=session_id,
                    details=success_audit,
                )
                return JSONResponse(
                    content={
                        "template": _public_record(
                            existing,
                            runtime_ready=await _can_instantiate(request, existing),
                        ),
                        "idempotent": True,
                    },
                    headers={"Cache-Control": "no-store"},
                )
            candidate = None
            await _record_template_audit(
                session_factory,
                action="import",
                outcome="success",
                session_id=session_id,
                details=_audit_details(
                    workspace_instance_id=workspace_instance_id,
                    template_ref=record.template_ref,
                    revision=record.revision,
                    state_version=record.state_version,
                    format_name=record.format,
                    idempotent=False,
                ),
            )
            return JSONResponse(
                status_code=201,
                content={"template": content, "idempotent": False},
                headers={"Cache-Control": "no-store"},
            )
    except BaseException as exc:
        if (
            candidate is not None
            and service is not None
            and not publication_started
        ):
            await _discard_candidate(service, candidate)
        await _record_template_audit(
            session_factory,
            action="import",
            outcome=_audit_failure_outcome(exc),
            session_id=session_id,
            details=audit,
        )
        if isinstance(exc, Exception):
            return _api_error(exc)
        raise
    finally:
        await file.close()


async def _scoped_record(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    workspace_instance_id: str,
    template_ref: str,
    revision: int,
) -> OfficeUserTemplate:
    validate_user_template_ref(template_ref)
    async with session_factory() as db:
        record = (
            await db.execute(
                select(OfficeUserTemplate).where(
                    OfficeUserTemplate.workspace_instance_id
                    == workspace_instance_id,
                    OfficeUserTemplate.template_ref == template_ref,
                    OfficeUserTemplate.revision == revision,
                    OfficeUserTemplate.status != "tombstoned",
                )
            )
        ).scalar_one_or_none()
        if record is None:
            raise _UserTemplateNotFound
        return record


def _preview_record_identity(record: OfficeUserTemplate) -> tuple[object, ...]:
    """Bind a preview to every DB field that authorizes its private evidence."""

    return (
        record.id,
        record.workspace_instance_id,
        record.template_ref,
        record.revision,
        record.state_version,
        record.status,
        record.format,
        record.source_sha256,
        record.manifest_sha256,
        json.dumps(
            record.placeholder_schema,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        json.dumps(
            record.placeholder_parts,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ),
        record.render_quality,
        record.renderer_id,
        record.renderer_version,
        record.font_digest,
        record.render_parameters_version,
        record.render_parameters_sha256,
        record.render_cache_key,
        record.render_manifest_sha256,
        record.render_page_count,
    )


@router.get("/{template_ref}/page")
async def user_office_template_page(
    template_ref: str,
    request: Request,
    session_factory: SessionFactoryDep,
    session_id: str = Query(min_length=1, max_length=128),
    workspace_instance_id: str = Query(min_length=1, max_length=128),
    revision: int = Query(ge=1),
    expected_state_version: int = Query(ge=1),
    page_number: int = Query(ge=1, le=1000),
) -> Any:
    """Serve one fully revalidated private render page without exposing paths."""

    try:
        _require_feature()
        await _require_workspace_binding(
            session_factory,
            session_id=session_id,
            workspace_instance_id=workspace_instance_id,
        )
        record = await _scoped_record(
            session_factory,
            workspace_instance_id=workspace_instance_id,
            template_ref=template_ref,
            revision=revision,
        )
        if (
            record.state_version != expected_state_version
            or record.render_quality not in {"authoritative", "approximate"}
        ):
            raise _UserTemplateConflict
        if page_number > record.render_page_count:
            raise _UserTemplateNotFound
        identity = _preview_record_identity(record)
        service = await _service(request, session_factory)
        page = await service.preview_page_bytes(
            template_ref=record.template_ref,
            revision=record.revision,
            source_sha256=record.source_sha256,
            manifest_sha256=record.manifest_sha256,
            format_name=record.format,  # type: ignore[arg-type]
            placeholder_schema=decode_user_template_placeholder_schema(
                record.placeholder_schema
            ),
            placeholder_parts=tuple(record.placeholder_parts),
            render_manifest_sha256=record.render_manifest_sha256,
            render_cache_key=record.render_cache_key,
            renderer_id=record.renderer_id,
            renderer_version=record.renderer_version,
            font_digest=record.font_digest,
            render_parameters_version=record.render_parameters_version,
            render_parameters_sha256=record.render_parameters_sha256,
            render_quality=record.render_quality,
            render_page_count=record.render_page_count,
            page_number=page_number,
        )
        await _require_workspace_binding(
            session_factory,
            session_id=session_id,
            workspace_instance_id=workspace_instance_id,
        )
        current = await _scoped_record(
            session_factory,
            workspace_instance_id=workspace_instance_id,
            template_ref=template_ref,
            revision=revision,
        )
        if (
            current.state_version != expected_state_version
            or _preview_record_identity(current) != identity
        ):
            raise _UserTemplateConflict
    except Exception as exc:
        return _api_error(exc)
    return _private_template_png(page, page_number=page_number)


def _private_template_png(payload: bytes, *, page_number: int) -> Response:
    return Response(
        content=payload,
        media_type="image/png",
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Content-Disposition": (
                f'inline; filename="user-template-page-{page_number}.png"'
            ),
        },
    )


@router.post("/{template_ref}/approve")
async def approve_user_office_template(
    template_ref: str,
    body: UserTemplateApprovalBody,
    request: Request,
    session_factory: SessionFactoryDep,
) -> Any:
    audit = _audit_details(
        workspace_instance_id=body.workspace_instance_id,
        template_ref=template_ref,
        revision=body.revision,
        state_version=body.expected_state_version,
    )
    try:
        _require_feature()
        await _require_workspace_binding(
            session_factory,
            session_id=body.session_id,
            workspace_instance_id=body.workspace_instance_id,
        )
        record = await _scoped_record(
            session_factory,
            workspace_instance_id=body.workspace_instance_id,
            template_ref=template_ref,
            revision=body.revision,
        )
        if (
            record.state_version != body.expected_state_version
            or record.source_sha256 != body.expected_source_sha256
            or record.render_cache_key != body.expected_render_cache_key
        ):
            raise _UserTemplateConflict
        audit = _audit_details(
            workspace_instance_id=body.workspace_instance_id,
            template_ref=record.template_ref,
            revision=record.revision,
            state_version=record.state_version,
            format_name=record.format,
        )
        await _record_template_audit(
            session_factory,
            action="approve",
            outcome="started",
            session_id=body.session_id,
            details=audit,
            required=True,
        )
        if record.status == "approved":
            await _record_template_audit(
                session_factory,
                action="approve",
                outcome="success",
                session_id=body.session_id,
                details={**audit, "idempotent": True},
            )
            return JSONResponse(
                content={
                    "template": _public_record(
                        record,
                        runtime_ready=await _can_instantiate(request, record),
                    ),
                    "idempotent": True,
                },
                headers={"Cache-Control": "no-store"},
            )
        if (
            record.status != "needs_confirmation"
            or record.render_quality != "authoritative"
        ):
            raise _UserTemplateConflict

        service = await _service(
            request,
            session_factory,
            reconcile=True,
        )
        manifest = await service.verify_approval_evidence(
            template_ref=record.template_ref,
            revision=record.revision,
            source_sha256=record.source_sha256,
            manifest_sha256=record.manifest_sha256,
            render_manifest_sha256=record.render_manifest_sha256,
            format_name=record.format,  # type: ignore[arg-type]
            placeholder_schema=decode_user_template_placeholder_schema(
                record.placeholder_schema
            ),
            placeholder_parts=tuple(record.placeholder_parts),
            render_cache_key=record.render_cache_key,
            renderer_id=record.renderer_id,
            renderer_version=record.renderer_version,
            font_digest=record.font_digest,
            render_parameters_version=record.render_parameters_version,
            render_parameters_sha256=record.render_parameters_sha256,
        )
        if (
            manifest.cache_key != record.render_cache_key
            or manifest.renderer_id != record.renderer_id
            or manifest.renderer_version != record.renderer_version
            or manifest.font_digest != record.font_digest
            or manifest.parameters_version != record.render_parameters_version
            or manifest.parameters_sha256 != record.render_parameters_sha256
        ):
            raise UserTemplateEvidenceError(
                "user template approval evidence no longer matches"
            )

        await _require_workspace_binding(
            session_factory,
            session_id=body.session_id,
            workspace_instance_id=body.workspace_instance_id,
        )
        now = datetime.now(timezone.utc)
        async with session_factory() as db:
            async with db.begin():
                result = await db.execute(
                    update(OfficeUserTemplate)
                    .where(
                        OfficeUserTemplate.id == record.id,
                        OfficeUserTemplate.workspace_instance_id
                        == body.workspace_instance_id,
                        OfficeUserTemplate.revision == body.revision,
                        OfficeUserTemplate.state_version
                        == body.expected_state_version,
                        OfficeUserTemplate.status == "needs_confirmation",
                        OfficeUserTemplate.render_quality == "authoritative",
                        OfficeUserTemplate.source_sha256
                        == body.expected_source_sha256,
                        OfficeUserTemplate.render_cache_key
                        == body.expected_render_cache_key,
                    )
                    .values(
                        status="approved",
                        state_version=OfficeUserTemplate.state_version + 1,
                        time_approved=now,
                        time_updated=now,
                    )
                )
                if result.rowcount != 1:
                    raise _UserTemplateConflict
            approved = await db.get(OfficeUserTemplate, record.id)
            if approved is None:
                raise _UserTemplateConflict
            content = _public_record(
                approved,
                runtime_ready=await _can_instantiate(request, approved),
            )
        await _record_template_audit(
            session_factory,
            action="approve",
            outcome="success",
            session_id=body.session_id,
            details=_audit_details(
                workspace_instance_id=body.workspace_instance_id,
                template_ref=approved.template_ref,
                revision=approved.revision,
                state_version=approved.state_version,
                format_name=approved.format,
                idempotent=False,
            ),
        )
        return JSONResponse(
            content={"template": content, "idempotent": False},
            headers={"Cache-Control": "no-store"},
        )
    except BaseException as exc:
        await _record_template_audit(
            session_factory,
            action="approve",
            outcome=_audit_failure_outcome(exc),
            session_id=body.session_id,
            details=audit,
        )
        if isinstance(exc, Exception):
            return _api_error(exc)
        raise


@router.delete("/{template_ref}")
async def delete_user_office_template(
    template_ref: str,
    body: UserTemplateDeleteBody,
    request: Request,
    session_factory: SessionFactoryDep,
) -> Any:
    audit = _audit_details(
        workspace_instance_id=body.workspace_instance_id,
        template_ref=template_ref,
        revision=body.revision,
        state_version=body.expected_state_version,
    )
    try:
        _require_feature()
        await _require_workspace_binding(
            session_factory,
            session_id=body.session_id,
            workspace_instance_id=body.workspace_instance_id,
        )
        record = await _scoped_record(
            session_factory,
            workspace_instance_id=body.workspace_instance_id,
            template_ref=template_ref,
            revision=body.revision,
        )
        if record.state_version != body.expected_state_version:
            raise _UserTemplateConflict
        audit = _audit_details(
            workspace_instance_id=body.workspace_instance_id,
            template_ref=record.template_ref,
            revision=record.revision,
            state_version=record.state_version,
            format_name=record.format,
        )
        await _record_template_audit(
            session_factory,
            action="delete",
            outcome="started",
            session_id=body.session_id,
            details=audit,
            required=True,
        )
        now = datetime.now(timezone.utc)
        async with session_factory() as db:
            async with db.begin():
                result = await db.execute(
                    update(OfficeUserTemplate)
                    .where(
                        OfficeUserTemplate.id == record.id,
                        OfficeUserTemplate.workspace_instance_id
                        == body.workspace_instance_id,
                        OfficeUserTemplate.revision == body.revision,
                        OfficeUserTemplate.state_version
                        == body.expected_state_version,
                        OfficeUserTemplate.status != "tombstoned",
                    )
                    .values(
                        status="tombstoned",
                        state_version=OfficeUserTemplate.state_version + 1,
                        time_tombstoned=now,
                        time_updated=now,
                    )
                )
                if result.rowcount != 1:
                    raise _UserTemplateConflict
            deleted = await db.get(OfficeUserTemplate, record.id)
            if deleted is None:
                raise _UserTemplateConflict
            content = _public_record(
                deleted,
                runtime_ready=await _can_instantiate(request, deleted),
            )
        await _record_template_audit(
            session_factory,
            action="delete",
            outcome="success",
            session_id=body.session_id,
            details=_audit_details(
                workspace_instance_id=body.workspace_instance_id,
                template_ref=deleted.template_ref,
                revision=deleted.revision,
                state_version=deleted.state_version,
                format_name=deleted.format,
                idempotent=False,
            ),
        )
        return JSONResponse(
            content={"template": content},
            headers={"Cache-Control": "no-store"},
        )
    except BaseException as exc:
        await _record_template_audit(
            session_factory,
            action="delete",
            outcome=_audit_failure_outcome(exc),
            session_id=body.session_id,
            details=audit,
        )
        if isinstance(exc, Exception):
            return _api_error(exc)
        raise


__all__ = [
    "UserTemplateApprovalBody",
    "UserTemplateDeleteBody",
    "router",
]
