"""Persistent follow-up queue and safe-boundary steer API."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.dependencies import SessionFactoryDep, StreamManagerDep
from app.schemas.chat import SessionInputRequest, SessionInputResponse
from app.models.session_input import SessionInput
from app.session.input_queue import (
    cancel_session_input,
    enqueue_session_input,
    list_session_inputs,
)
from app.session.idempotency import canonical_request_hash
from app.session.managed_workspace import snapshot_attachments
from app.session.manager import get_session
from app.streaming.events import INPUT_QUEUED, SSEEvent


router = APIRouter()


def _response(item) -> SessionInputResponse:
    return SessionInputResponse(
        id=item.id,
        session_id=item.session_id,
        client_request_id=item.client_request_id,
        mode=item.mode,
        status=item.status,
        position=item.position,
        text=item.text,
        attachments=item.attachments or [],
        target_stream_id=item.target_stream_id,
        error_message=item.error_message,
    )


def _logical_attachments(
    attachments: list[dict],
) -> list[dict]:
    """Remove managed-snapshot transport details from replay comparison."""

    logical: list[dict] = []
    for attachment in attachments:
        normalized = dict(attachment)
        original_path = normalized.pop("original_path", None)
        if original_path:
            normalized["path"] = original_path
        # ``managed`` is an internal storage source written after admission;
        # source does not change what bytes/instruction the user submitted.
        normalized.pop("source", None)
        logical.append(normalized)
    return logical


def _request_fingerprint(
    *,
    session_id: str,
    mode: str,
    text: str,
    attachments: list[dict],
    model_id: str | None,
    provider_id: str | None,
    agent: str,
    workspace: str | None,
    reasoning: bool | None,
    permission_presets: dict | None,
    permission_rules: list[dict] | None,
) -> str:
    return canonical_request_hash(
        {
            "session_id": session_id,
            "mode": mode,
            "text": text.strip(),
            "attachments": _logical_attachments(attachments),
            "model": model_id,
            "provider_id": provider_id,
            "agent": agent,
            "workspace": workspace,
            "reasoning": reasoning,
            "permission_presets": permission_presets,
            "permission_rules": permission_rules,
        }
    )


def _validate_replay(item: SessionInput, body: SessionInputRequest, *, workspace: str | None) -> None:
    stored = _request_fingerprint(
        session_id=item.session_id,
        mode=item.mode,
        text=item.text,
        attachments=item.attachments or [],
        model_id=item.model_id,
        provider_id=item.provider_id,
        agent=item.agent,
        workspace=item.workspace,
        reasoning=item.reasoning,
        permission_presets=item.permission_presets,
        permission_rules=item.permission_rules,
    )
    incoming = _request_fingerprint(
        session_id=body.session_id,
        mode=body.mode,
        text=body.text,
        attachments=body.attachments,
        model_id=body.model,
        provider_id=body.provider_id,
        agent=body.agent,
        workspace=workspace,
        reasoning=body.reasoning,
        permission_presets=body.permission_presets,
        permission_rules=body.permission_rules,
    )
    if stored != incoming:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "idempotency_conflict",
                "message": "The request key was already used with a different queued input.",
            },
        )


@router.post("/chat/inputs", response_model=SessionInputResponse)
async def create_session_input(
    body: SessionInputRequest,
    stream_manager: StreamManagerDep,
    session_factory: SessionFactoryDep,
) -> SessionInputResponse:
    async with session_factory() as db:
        session = await get_session(db, body.session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        session_directory = session.directory
        existing = (
            await db.execute(
                select(SessionInput).where(
                    SessionInput.session_id == body.session_id,
                    SessionInput.client_request_id == body.client_request_id,
                )
            )
        ).scalar_one_or_none()
    # Idempotency must outlive the in-memory generation job. A client retry can
    # arrive after the original follow-up was already consumed and the stream
    # completed, especially when the first HTTP response was lost.
    if existing is not None:
        _validate_replay(
            existing,
            body,
            workspace=None if session_directory == "." else body.workspace,
        )
        return _response(existing)
    if not body.text.strip() and not body.attachments:
        raise HTTPException(status_code=422, detail="Text or attachments are required")

    active = stream_manager.active_job_for_session(body.session_id)
    if active is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "session_idle",
                "message": "The current task already finished; send this as a normal message.",
            },
        )

    attachments = body.attachments
    if session_directory == "." and attachments:
        try:
            attachments = await asyncio.to_thread(
                snapshot_attachments,
                body.session_id,
                attachments,
            )
        except (OSError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Serialize durable admission with the generation's final empty-queue
    # observation. This closes the otherwise tiny but real lost-wakeup window
    # between "no queued input" and stream completion.
    async with active.session_input_lock:
        if (
            not active.accepting_session_inputs
            or active.abort_event.is_set()
            or stream_manager.active_job_for_session(body.session_id) is not active
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "session_idle",
                    "message": "The current task already finished; send this as a normal message.",
                },
            )
        try:
            async with session_factory() as db:
                async with db.begin():
                    item, _created = await enqueue_session_input(
                        db,
                        session_id=body.session_id,
                        client_request_id=body.client_request_id,
                        mode=body.mode,
                        text=body.text.strip(),
                        attachments=attachments,
                        model_id=body.model,
                        provider_id=body.provider_id,
                        agent=body.agent,
                        # The persisted session directory is authoritative. A
                        # stale global workspace selection must not redirect a
                        # folderless conversation's queued outputs.
                        workspace=None if session_directory == "." else body.workspace,
                        reasoning=body.reasoning,
                        permission_presets=body.permission_presets,
                        permission_rules=body.permission_rules,
                        target_stream_id=(
                            active.stream_id if body.mode == "steer" else None
                        ),
                    )
        except IntegrityError:
            # Concurrent desktop/mobile submissions with the same idempotency
            # key converge on the already committed item.
            async with session_factory() as db:
                item = (
                    await db.execute(
                        select(SessionInput).where(
                            SessionInput.session_id == body.session_id,
                            SessionInput.client_request_id == body.client_request_id,
                        )
                    )
                ).scalar_one_or_none()
            if item is None:
                raise
            _created = False

            _validate_replay(
                item,
                body,
                workspace=None if session_directory == "." else body.workspace,
            )

    if _created:
        active.publish(
            SSEEvent(
                INPUT_QUEUED,
                {
                    "input_id": item.id,
                    "mode": item.mode,
                    "position": item.position,
                    "session_id": item.session_id,
                },
            )
        )
    return _response(item)


@router.get("/chat/inputs/{session_id}", response_model=list[SessionInputResponse])
async def get_session_inputs(
    session_id: str,
    session_factory: SessionFactoryDep,
) -> list[SessionInputResponse]:
    async with session_factory() as db:
        return [
            _response(item)
            for item in await list_session_inputs(db, session_id)
        ]


@router.delete("/chat/inputs/{session_id}/{item_id}")
async def delete_session_input(
    session_id: str,
    item_id: str,
    session_factory: SessionFactoryDep,
) -> dict[str, str]:
    async with session_factory() as db:
        async with db.begin():
            cancelled = await cancel_session_input(db, session_id, item_id)
    if not cancelled:
        raise HTTPException(
            status_code=409,
            detail="Only queued or blocked inputs can be cancelled",
        )
    return {"status": "cancelled", "input_id": item_id}
