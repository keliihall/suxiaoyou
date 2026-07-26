from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
import shutil
import subprocess
from typing import Any

from fastapi import HTTPException
import pytest
from sqlalchemy import event, select

from app import release_features
from app.auth.local import require_local_session
from app.models.project import Project
from app.models.security_audit_event import SecurityAuditEvent
from app.models.session import Session
from app.models.session_checkpoint import SessionCheckpoint
from app.models.turn_run import TurnRun
from app.models.workspace_instance import WorkspaceInstance
from app.runtime.rewind import (
    RewindBusyError,
    RewindCheckpointItem,
    RewindConflict,
    RewindConflictError,
    RewindPath,
    RewindPreview,
    RewindProvenanceError,
    RewindResult,
)
from app.security.audit import AuditPersistenceError
from app.session.managed_workspace import managed_workspace_for_session
from app.storage.workspace_identity import ensure_workspace_identity
from app.tool.workspace import APP_PRIVATE_DIR_ENV
from app.validation_agent.contracts import (
    ValidationBudgetReport,
    ValidationSource,
    ValidationVerdictRecord,
)
from app.validation_agent.persistence import POST_CHECKPOINT_VALIDATIONS_KEY
from app.worktree import WorktreeActiveError


pytestmark = pytest.mark.asyncio


@pytest.fixture
def released_runtime_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(release_features, "V11_CHECKPOINTS_RELEASED", True)
    monkeypatch.setattr(release_features, "V11_REWIND_RELEASED", True)
    monkeypatch.setattr(release_features, "V11_WORKTREES_RELEASED", True)
    # Most runtime-control tests exercise rewind independently of validator
    # persistence; the dedicated summary test opts the released validator in.
    monkeypatch.setattr(release_features, "V11_VALIDATION_AGENT_RELEASED", False)


async def _seed_workspace(
    session_factory,
    root: Path,
    *,
    session_id: str = "session",
    workspace_instance_id: str = "workspace",
    project_id: str | None = None,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    identity = ensure_workspace_identity(root).durable_token
    async with session_factory() as db:
        async with db.begin():
            db.add(
                Session(
                    id=session_id,
                    project_id=project_id,
                    directory=str(root.resolve()),
                    title=session_id,
                    version="1.1.0",
                )
            )
            db.add(
                WorkspaceInstance(
                    id=workspace_instance_id,
                    project_id=project_id,
                    created_by_session_id=session_id,
                    kind="direct",
                    root_path=str(root.resolve()),
                    identity_token=identity,
                    status="active",
                    details={"managed": False},
                )
            )


class _FakeRewindService:
    def __init__(self) -> None:
        self.execute_count = 0
        self.execute_error: Exception | None = None
        self.preview_error: Exception | None = None
        self.items: tuple[RewindCheckpointItem, ...] | None = None

    async def list(self, **_kwargs: Any):
        if self.items is not None:
            return self.items
        return (
            RewindCheckpointItem(
                checkpoint_id="checkpoint",
                sequence=7,
                state="finalized",
                pin_state="pinned",
                anchor_message_id="anchor",
                turn_run_id="turn",
                has_irreversible_side_effects=True,
                external_side_effects=(
                    {
                        "source": "mcp",
                        "operation": "send_email",
                        "audit_id": "audit-1",
                        "raw_payload": "must-not-cross-api",
                    },
                ),
            ),
        )

    async def preview(self, **kwargs: Any):
        if self.preview_error is not None:
            raise self.preview_error
        return RewindPreview(
            session_id=kwargs["session_id"],
            workspace_instance_id=kwargs["workspace_instance_id"],
            target_checkpoint_id=kwargs["checkpoint_id"],
            affected_checkpoint_ids=(kwargs["checkpoint_id"],),
            paths=(
                RewindPath(
                    relative_path="report.docx",
                    action="restore_file",
                    current_kind="file",
                    desired_kind="file",
                    source_version_id="private-version-id",
                ),
            ),
            conflicts=(),
            blockers=(),
            external_side_effects=(
                {
                    "checkpoint_id": kwargs["checkpoint_id"],
                    "source": "mcp",
                    "operation": "send_email",
                    "audit_id": "audit-1",
                    "credential": "must-not-cross-api",
                },
            ),
        )

    async def execute(self, **kwargs: Any):
        if self.execute_error is not None:
            raise self.execute_error
        self.execute_count += 1
        replay = self.execute_count > 1
        return RewindResult(
            session_id=kwargs["session_id"],
            workspace_instance_id=kwargs["workspace_instance_id"],
            target_checkpoint_id=kwargs["checkpoint_id"],
            affected_checkpoint_ids=(kwargs["checkpoint_id"],),
            changed_paths=("report.docx",),
            messages_removed=2,
            todos_restored=1,
            external_side_effects=(
                {
                    "checkpoint_id": kwargs["checkpoint_id"],
                    "source": "mcp",
                    "operation": "send_email",
                    "audit_id": "audit-1",
                    "raw_payload": "must-not-cross-api",
                },
            ),
            already_rewound=replay,
        )


async def test_runtime_routes_are_dynamically_hidden_while_gates_are_closed(
    app_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release_features, "V11_CHECKPOINTS_RELEASED", False)
    monkeypatch.setattr(release_features, "V11_REWIND_RELEASED", False)
    monkeypatch.setattr(release_features, "V11_WORKTREES_RELEASED", False)
    context = await app_client.get(
        "/api/runtime/context",
        params={"session_id": "session"},
    )
    rewind = await app_client.get(
        "/api/runtime/checkpoints",
        params={"session_id": "session", "workspace_instance_id": "workspace"},
    )
    worktree = await app_client.post(
        "/api/runtime/worktrees/create-bind",
        json={"session_id": "session"},
    )
    assert rewind.status_code == 404
    assert context.status_code == 404
    assert context.json()["code"] == "v11_runtime_not_available"
    assert rewind.json()["code"] == "v11_rewind_not_available"
    assert worktree.status_code == 404
    assert worktree.json()["code"] == "v11_worktree_not_available"


async def test_runtime_context_resolves_only_server_owned_workspace_identity(
    app_client,
    session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    released_runtime_gates: None,
) -> None:
    monkeypatch.setenv(APP_PRIVATE_DIR_ENV, str(tmp_path / "private"))
    workspace = tmp_path / "workspace"
    await _seed_workspace(
        session_factory,
        workspace,
        session_id="context-session",
        workspace_instance_id="context-workspace",
    )

    response = await app_client.get(
        "/api/runtime/context",
        params={"session_id": "context-session"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "session_id": "context-session",
        "workspace_instance_id": "context-workspace",
        "workspace_kind": "direct",
        "checkpoint_rewind_released": True,
        "managed_worktrees_released": True,
        "worktree_creation_available": False,
        "worktree_creation_reason": "repository_not_supported",
        "external_side_effects_reverted": False,
    }
    assert response.headers["cache-control"] == "no-store"

    # Keep the original marker with the original directory.  A replacement at
    # the same canonical path must not inherit the registered durable identity.
    workspace.rename(tmp_path / "retired-workspace")
    workspace.mkdir()
    replaced = await app_client.get(
        "/api/runtime/context",
        params={"session_id": "context-session"},
    )
    assert replaced.status_code == 409
    assert replaced.json()["code"] == "runtime_workspace_provenance_mismatch"


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required")
async def test_runtime_context_reports_clean_git_creation_eligibility(
    app_client,
    session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    released_runtime_gates: None,
) -> None:
    monkeypatch.setenv(APP_PRIVATE_DIR_ENV, str(tmp_path / "private"))
    workspace = tmp_path / "git-workspace"
    await _seed_workspace(
        session_factory,
        workspace,
        session_id="git-context-session",
        workspace_instance_id="git-context-workspace",
    )
    _git(workspace, "init")
    _git(workspace, "config", "user.email", "runtime@example.invalid")
    _git(workspace, "config", "user.name", "Runtime API")
    (workspace / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(workspace, "add", "tracked.txt")
    _git(workspace, "commit", "-m", "base")

    response = await app_client.get(
        "/api/runtime/context",
        params={"session_id": "git-context-session"},
    )

    assert response.status_code == 200
    assert response.json()["worktree_creation_available"] is True
    assert response.json()["worktree_creation_reason"] is None


async def test_non_git_worktree_create_is_a_clear_eligibility_failure(
    app_client,
    session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    released_runtime_gates: None,
) -> None:
    monkeypatch.setenv(APP_PRIVATE_DIR_ENV, str(tmp_path / "private"))
    workspace = tmp_path / "ordinary-folder"
    await _seed_workspace(
        session_factory,
        workspace,
        session_id="ordinary-session",
        workspace_instance_id="ordinary-workspace",
    )

    response = await app_client.post(
        "/api/runtime/worktrees/create-bind",
        json={"session_id": "ordinary-session"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "worktree_repository_invalid"
    assert "supervised Git" not in response.json()["detail"]


async def test_runtime_context_resolves_folderless_managed_workspace(
    app_client,
    session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    released_runtime_gates: None,
) -> None:
    monkeypatch.setenv("SUXIAOYOU_MANAGED_WORKSPACE_ROOT", str(tmp_path / "managed"))
    root = managed_workspace_for_session("managed-session")
    identity = ensure_workspace_identity(root).durable_token
    async with session_factory() as db:
        async with db.begin():
            db.add(
                Session(
                    id="managed-session",
                    directory=".",
                    title="managed-session",
                    version="1.1.0",
                )
            )
            db.add(
                WorkspaceInstance(
                    id="managed-workspace",
                    created_by_session_id="managed-session",
                    kind="managed",
                    root_path=str(root.resolve()),
                    identity_token=identity,
                    status="active",
                    details={"managed": True},
                )
            )

    response = await app_client.get(
        "/api/runtime/context",
        params={"session_id": "managed-session"},
    )

    assert response.status_code == 200
    assert response.json()["workspace_instance_id"] == "managed-workspace"
    assert response.json()["workspace_kind"] == "managed"
    assert response.json()["worktree_creation_available"] is False
    assert response.json()["worktree_creation_reason"] == "workspace_not_supported"


async def test_runtime_context_hides_uninitialized_folderless_workspace(
    app_client,
    session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    released_runtime_gates: None,
) -> None:
    monkeypatch.setenv("SUXIAOYOU_MANAGED_WORKSPACE_ROOT", str(tmp_path / "managed"))
    async with session_factory() as db:
        async with db.begin():
            db.add(
                Session(
                    id="empty-session",
                    directory=".",
                    title="empty-session",
                    version="1.1.0",
                )
            )

    response = await app_client.get(
        "/api/runtime/context",
        params={"session_id": "empty-session"},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "runtime_workspace_not_found"


async def test_runtime_context_rejects_foreign_managed_workspace(
    app_client,
    session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    released_runtime_gates: None,
) -> None:
    monkeypatch.setenv("SUXIAOYOU_MANAGED_WORKSPACE_ROOT", str(tmp_path / "managed"))
    root = managed_workspace_for_session("managed-session")
    async with session_factory() as db:
        async with db.begin():
            db.add_all([
                Session(
                    id="managed-session",
                    directory=".",
                    title="managed-session",
                    version="1.1.0",
                ),
                Session(
                    id="foreign-session",
                    directory=".",
                    title="foreign-session",
                    version="1.1.0",
                ),
            ])
            await db.flush()
            db.add(
                WorkspaceInstance(
                    id="foreign-workspace",
                    created_by_session_id="foreign-session",
                    kind="managed",
                    root_path=str(root.resolve()),
                    identity_token=ensure_workspace_identity(root).durable_token,
                    status="active",
                    details={"managed": True},
                )
            )

    response = await app_client.get(
        "/api/runtime/context",
        params={"session_id": "managed-session"},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "runtime_workspace_not_found"


async def test_runtime_routes_require_local_session_and_trusted_origin(
    app_client,
    released_runtime_gates: None,
) -> None:
    def reject_non_local_request() -> None:
        raise HTTPException(status_code=403, detail="local desktop required")

    app_client.app.dependency_overrides[require_local_session] = reject_non_local_request
    try:
        denied = await app_client.get(
            "/api/runtime/checkpoints",
            params={"session_id": "session", "workspace_instance_id": "workspace"},
        )
    finally:
        app_client.app.dependency_overrides.pop(require_local_session, None)
    assert denied.status_code == 403

    cross_origin = await app_client.post(
        "/api/runtime/rewind/execute",
        headers={"Origin": "https://attacker.invalid"},
        json={
            "session_id": "session",
            "workspace_instance_id": "workspace",
            "checkpoint_id": "checkpoint",
        },
    )
    assert cross_origin.status_code == 403


async def test_runtime_request_schemas_never_accept_workspace_or_repository_paths(
    app_client,
    released_runtime_gates: None,
) -> None:
    rewind = await app_client.post(
        "/api/runtime/rewind/preview",
        json={
            "session_id": "session",
            "workspace_instance_id": "workspace",
            "checkpoint_id": "checkpoint",
            "workspace_path": "/caller/chosen/path",
        },
    )
    worktree = await app_client.post(
        "/api/runtime/worktrees/create-bind",
        json={
            "session_id": "session",
            "repository": "/caller/chosen/repository",
        },
    )
    assert rewind.status_code == 422
    assert worktree.status_code == 422


async def test_rewind_list_preview_execute_disclose_irreversible_effects_and_replay(
    app_client,
    session_factory,
    tmp_path: Path,
    released_runtime_gates: None,
) -> None:
    await _seed_workspace(session_factory, tmp_path / "workspace")
    service = _FakeRewindService()
    app_client.app.state.v11_rewind_service = service

    listed = await app_client.get(
        "/api/runtime/checkpoints",
        params={"session_id": "session", "workspace_instance_id": "workspace"},
    )
    assert listed.status_code == 200
    listed_payload = listed.json()
    assert listed_payload["external_side_effects_are_reverted"] is False
    assert listed_payload["checkpoints"][0]["external_side_effects"] == [
        {
            "source": "mcp",
            "operation": "send_email",
            "audit_id": "audit-1",
        }
    ]
    assert listed_payload["checkpoints"][0]["validation"] == {
        "overall_status": "not_requested",
        "count": 0,
        "completed_count": 0,
        "failed_count": 0,
        "cancelled_count": 0,
        "verdict_counts": {"pass": 0, "fail": 0, "needs_review": 0},
    }

    request = {
        "session_id": "session",
        "workspace_instance_id": "workspace",
        "checkpoint_id": "checkpoint",
    }
    preview = await app_client.post("/api/runtime/rewind/preview", json=request)
    assert preview.status_code == 200
    assert preview.json()["can_execute"] is True
    assert preview.json()["external_side_effects_will_be_reverted"] is False
    assert preview.json()["paths"] == [
        {
            "relative_path": "report.docx",
            "action": "restore_file",
            "current_kind": "file",
            "desired_kind": "file",
        }
    ]
    assert "private-version-id" not in preview.text
    assert "must-not-cross-api" not in preview.text

    first = await app_client.post("/api/runtime/rewind/execute", json=request)
    replay = await app_client.post("/api/runtime/rewind/execute", json=request)
    assert first.status_code == replay.status_code == 200
    assert first.json()["status"] == "rewound"
    assert first.json()["external_side_effects_were_reverted"] is False
    assert replay.json()["status"] == "already_rewound"
    assert replay.json()["already_rewound"] is True
    assert "must-not-cross-api" not in first.text

    async with session_factory() as db:
        events = list(
            (
                await db.execute(
                    select(SecurityAuditEvent).where(
                        SecurityAuditEvent.capability == "checkpoint_rewind"
                    )
                )
            ).scalars()
        )
    assert [event.outcome for event in events] == [
        "started",
        "success",
        "started",
        "success",
    ]
    assert all("path" not in str(event.details).lower() for event in events)


def _public_validation_entry(
    *,
    checkpoint_id: str,
    turn_run_id: str,
    request_id: str,
) -> dict[str, Any]:
    record = ValidationVerdictRecord(
        schema_version=1,
        validation_id=f"private-validation-{request_id}",
        verdict="pass",
        reason_code="model_verdict",
        source=ValidationSource(
            session_id="session",
            root_turn_id=turn_run_id,
            checkpoint_id=checkpoint_id,
            workspace_instance_id="workspace",
        ),
        round=1,
        budget=ValidationBudgetReport(
            max_rounds=2,
            max_tokens=8_000,
            timeout_ms=60_000,
            rounds_used=1,
            tokens_used=7,
            elapsed_ms=12,
        ),
        summary="Private validation narrative must remain server-side.",
        validator_session_ids=("private-validator-session",),
    )
    return {
        "schema_version": 1,
        "request_id": request_id,
        "policy_id": "private.runtime.policy",
        "status": "completed",
        "generation_job": {
            "session_id": "session",
            "root_turn_id": turn_run_id,
            "turn_run_id": turn_run_id,
            "checkpoint_id": checkpoint_id,
            "workspace_instance_id": "workspace",
        },
        "record": record.model_dump(mode="json"),
    }


async def test_checkpoint_validation_summaries_are_strict_redacted_and_bulk_loaded(
    app_client,
    session_factory,
    db_engine,
    tmp_path: Path,
    released_runtime_gates: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        release_features,
        "V11_VALIDATION_AGENT_RELEASED",
        True,
    )
    await _seed_workspace(session_factory, tmp_path / "workspace")
    now = datetime.now(UTC)
    checkpoints = (
        ("checkpoint-validation-pass", "turn-validation-pass", 8),
        ("checkpoint-validation-invalid", "turn-validation-invalid", 7),
    )
    async with session_factory() as db:
        async with db.begin():
            for checkpoint_id, turn_id, sequence in checkpoints:
                db.add(
                    TurnRun(
                        id=turn_id,
                        session_id="session",
                        workspace_instance_id="workspace",
                        root_turn_id=turn_id,
                        parent_turn_id=None,
                        depth=0,
                        source_kind="interactive",
                        status="completed",
                        time_started=now,
                        time_finished=now,
                    )
                )
                await db.flush()
                entry = _public_validation_entry(
                    checkpoint_id=checkpoint_id,
                    turn_run_id=turn_id,
                    request_id=f"private-request-{sequence}",
                )
                if checkpoint_id.endswith("invalid"):
                    entry["schema_version"] = True
                db.add(
                    SessionCheckpoint(
                        id=checkpoint_id,
                        session_id="session",
                        workspace_instance_id="workspace",
                        root_turn_id=turn_id,
                        turn_run_id=turn_id,
                        sequence=sequence,
                        state="finalized",
                        pin_state="pinned",
                        details={POST_CHECKPOINT_VALIDATIONS_KEY: [entry]},
                        time_finalized=now,
                    )
                )

    service = _FakeRewindService()
    service.items = tuple(
        RewindCheckpointItem(
            checkpoint_id=checkpoint_id,
            sequence=sequence,
            state="finalized",
            pin_state="pinned",
            anchor_message_id=None,
            turn_run_id=turn_id,
            has_irreversible_side_effects=False,
            external_side_effects=(),
        )
        for checkpoint_id, turn_id, sequence in checkpoints
    )
    app_client.app.state.v11_rewind_service = service

    statements: list[str] = []

    def capture_statement(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(
        db_engine.sync_engine,
        "before_cursor_execute",
        capture_statement,
    )
    try:
        response = await app_client.get(
            "/api/runtime/checkpoints",
            params={
                "session_id": "session",
                "workspace_instance_id": "workspace",
            },
        )
    finally:
        event.remove(
            db_engine.sync_engine,
            "before_cursor_execute",
            capture_statement,
        )

    assert response.status_code == 200
    payload = response.json()
    assert [item["validation"]["overall_status"] for item in payload["checkpoints"]] == [
        "pass",
        "invalid",
    ]
    assert payload["checkpoints"][0]["validation"] == {
        "overall_status": "pass",
        "count": 1,
        "completed_count": 1,
        "failed_count": 0,
        "cancelled_count": 0,
        "verdict_counts": {"pass": 1, "fail": 0, "needs_review": 0},
    }
    serialized = response.text
    for secret in (
        "private-request-8",
        "private.runtime.policy",
        "private-validator-session",
        "Private validation narrative",
    ):
        assert secret not in serialized
    checkpoint_selects = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT")
        and "FROM session_checkpoint" in statement
    ]
    assert len(checkpoint_selects) == 1
    assert " IN (" in checkpoint_selects[0]


async def test_rewind_conflict_busy_and_cross_workspace_have_stable_codes(
    app_client,
    session_factory,
    tmp_path: Path,
    released_runtime_gates: None,
) -> None:
    await _seed_workspace(session_factory, tmp_path / "one")
    await _seed_workspace(
        session_factory,
        tmp_path / "two",
        session_id="other-session",
        workspace_instance_id="other-workspace",
    )
    service = _FakeRewindService()
    app_client.app.state.v11_rewind_service = service
    request = {
        "session_id": "session",
        "workspace_instance_id": "workspace",
        "checkpoint_id": "checkpoint",
    }

    service.execute_error = RewindBusyError("session has an active generation job")
    busy = await app_client.post("/api/runtime/rewind/execute", json=request)
    assert busy.status_code == 409
    assert busy.json()["code"] == "rewind_busy"

    service.execute_error = RewindConflictError(
        "workspace conflicts with ledger",
        conflicts=(RewindConflict("report.docx", "content changed"),),
    )
    conflict = await app_client.post("/api/runtime/rewind/execute", json=request)
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "rewind_conflict"
    assert conflict.json()["conflicts"] == [
        {"relative_path": "report.docx", "reason": "content changed"}
    ]

    cross_workspace = await app_client.post(
        "/api/runtime/rewind/preview",
        json={**request, "workspace_instance_id": "other-workspace"},
    )
    assert cross_workspace.status_code == 409
    assert cross_workspace.json()["code"] == "rewind_provenance_mismatch"

    service.preview_error = RewindProvenanceError("foreign checkpoint")
    cross_checkpoint = await app_client.post(
        "/api/runtime/rewind/preview",
        json={**request, "checkpoint_id": "foreign-checkpoint"},
    )
    assert cross_checkpoint.status_code == 409
    assert cross_checkpoint.json()["code"] == "rewind_provenance_mismatch"


async def test_required_rewind_audit_failure_prevents_service_execution(
    app_client,
    session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    released_runtime_gates: None,
) -> None:
    await _seed_workspace(session_factory, tmp_path / "workspace")
    service = _FakeRewindService()
    app_client.app.state.v11_rewind_service = service

    async def unavailable_audit(*_args: Any, **kwargs: Any) -> None:
        if kwargs.get("required"):
            raise AuditPersistenceError("audit unavailable")

    monkeypatch.setattr(
        "app.api.runtime_control.record_security_event",
        unavailable_audit,
    )
    response = await app_client.post(
        "/api/runtime/rewind/execute",
        json={
            "session_id": "session",
            "workspace_instance_id": "workspace",
            "checkpoint_id": "checkpoint",
        },
    )
    assert response.status_code == 503
    assert response.json()["code"] == "runtime_audit_unavailable"
    assert service.execute_count == 0


class _ActiveWorktreeRuntime:
    async def release_session(self, **_kwargs: Any) -> None:
        raise WorktreeActiveError("persistent checkpoint references remain")


async def test_worktree_reference_failure_is_fail_closed(
    app_client,
    session_factory,
    tmp_path: Path,
    released_runtime_gates: None,
) -> None:
    root = tmp_path / "managed"
    root.mkdir()
    identity = ensure_workspace_identity(root).durable_token
    async with session_factory() as db:
        async with db.begin():
            db.add(
                Session(
                    id="session",
                    directory=str(root),
                    title="session",
                    version="1.1.0",
                )
            )
            db.add(
                WorkspaceInstance(
                    id="managed-workspace",
                    created_by_session_id="session",
                    kind="git_worktree",
                    root_path=str(root),
                    identity_token=identity,
                    status="active",
                    details={"session_id": "session", "worktree_state": "bound"},
                )
            )
    app_client.app.state.v11_worktree_runtime = _ActiveWorktreeRuntime()
    response = await app_client.post(
        "/api/runtime/worktrees/release",
        json={
            "session_id": "session",
            "workspace_instance_id": "managed-workspace",
        },
    )
    assert response.status_code == 409
    assert response.json() == {
        "detail": "persistent checkpoint references remain",
        "code": "worktree_active",
    }


def _git(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required")
async def test_worktree_api_derives_repository_and_handles_dirty_retry_gc_idempotently(
    app_client,
    session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    released_runtime_gates: None,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "runtime@example.invalid")
    _git(repository, "config", "user.name", "Runtime API")
    (repository / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "-m", "base")
    private = tmp_path / "private"
    monkeypatch.setenv(APP_PRIVATE_DIR_ENV, str(private))

    async with session_factory() as db:
        async with db.begin():
            db.add(
                Project(
                    id="project",
                    name="repository",
                    worktree=str(repository.resolve()),
                )
            )
            db.add(
                Session(
                    id="session",
                    project_id="project",
                    directory=str(repository.resolve()),
                    title="session",
                    version="1.1.0",
                )
            )

    created = await app_client.post(
        "/api/runtime/worktrees/create-bind",
        json={"session_id": "session"},
    )
    assert created.status_code == 200, created.text
    created_payload = created.json()
    workspace_instance_id = created_payload["workspace_instance_id"]
    assert created_payload["status"] == "bound"
    assert not {
        "repository",
        "checkout_path",
        "ownership_token",
        "command",
    }.intersection(created_payload)

    inspected = await app_client.get(
        "/api/runtime/worktrees/inspect",
        params={
            "session_id": "session",
            "workspace_instance_id": workspace_instance_id,
        },
    )
    assert inspected.status_code == 200
    assert inspected.json()["available"] is True
    assert inspected.json()["clean"] is True
    assert inspected.json()["registered"] is True

    async with session_factory() as db:
        session = await db.get(Session, "session")
        instance = await db.get(WorkspaceInstance, workspace_instance_id)
        assert session is not None and instance is not None
        checkout = Path(session.directory)
    (checkout / "dirty.txt").write_text("dirty\n", encoding="utf-8")

    request = {
        "session_id": "session",
        "workspace_instance_id": workspace_instance_id,
    }
    dirty = await app_client.post("/api/runtime/worktrees/release", json=request)
    assert dirty.status_code == 409
    assert dirty.json()["code"] == "worktree_dirty"
    async with session_factory() as db:
        session = await db.get(Session, "session")
        instance = await db.get(WorkspaceInstance, workspace_instance_id)
        assert session is not None and instance is not None
        assert Path(session.directory).resolve() == repository.resolve()
        assert instance.status == "active"
        assert instance.details["worktree_state"] == "releasing"

    (checkout / "dirty.txt").unlink()
    released = await app_client.post("/api/runtime/worktrees/release", json=request)
    replay = await app_client.post("/api/runtime/worktrees/release", json=request)
    collected = await app_client.post("/api/runtime/worktrees/gc", json=request)
    assert released.status_code == replay.status_code == collected.status_code == 200
    assert released.json()["status"] == "released"
    assert replay.json()["status"] == "already_released"
    assert replay.json()["already_released"] is True
    assert collected.json()["status"] == "complete"
    assert not checkout.exists()

    async with session_factory() as db:
        instance = await db.get(WorkspaceInstance, workspace_instance_id)
        events = list(
            (
                await db.execute(
                    select(SecurityAuditEvent).where(
                        SecurityAuditEvent.capability == "managed_worktree"
                    )
                )
            ).scalars()
        )
    assert instance is not None and instance.status == "released"
    assert any(event.action == "release" and event.outcome == "blocked" for event in events)
    assert all(str(repository) not in str(event.details) for event in events)
    assert all("command" not in str(event.details).lower() for event in events)
