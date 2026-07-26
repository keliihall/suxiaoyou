"""v1.1 root-turn, checkpoint ledger, pin, and migration contracts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.checkpoint_change import CheckpointChange
from app.models.session import Session
from app.storage import migrations
from app.storage.checkpoints import (
    CheckpointConflictError,
    CheckpointValidationError,
    create_child_turn,
    create_root_turn,
    finish_turn,
    list_checkpoint_changes,
    list_root_turn_checkpoints,
    prepare_checkpoint,
    reconcile_workspace_checkpoint_pins,
    record_checkpoint_change,
    record_irreversible_side_effect,
    register_workspace_instance,
    release_checkpoint_pin,
    release_workspace_instance,
    transition_checkpoint,
)
from app.storage.file_versions import FileVersionLimits, FileVersionStore
from app.utils.id import generate_ulid


async def _session(db: AsyncSession, session_id: str, directory: Path) -> Session:
    session = Session(
        id=session_id,
        directory=str(directory),
        title="Checkpoint test",
        version="1.1.0",
    )
    db.add(session)
    await db.flush()
    return session


@pytest.mark.asyncio
async def test_workspace_instance_supplied_identity_and_release_reservation(
    db: AsyncSession,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "managed-worktree"
    other = tmp_path / "other-worktree"
    workspace.mkdir()
    other.mkdir()
    await _session(db, "session", workspace)
    instance = await register_workspace_instance(
        db,
        workspace,
        instance_id="worktree-instance",
        kind="git_worktree",
        created_by_session_id="session",
    )
    assert instance.id == "worktree-instance"
    replay = await register_workspace_instance(
        db,
        workspace,
        instance_id="worktree-instance",
        kind="git_worktree",
        created_by_session_id="session",
    )
    assert replay.id == instance.id

    with pytest.raises(CheckpointConflictError, match="different provenance"):
        await register_workspace_instance(
            db,
            workspace,
            instance_id="different-id",
            kind="git_worktree",
            created_by_session_id="session",
        )
    with pytest.raises(CheckpointConflictError, match="already registered"):
        await register_workspace_instance(
            db,
            other,
            instance_id="worktree-instance",
            kind="git_worktree",
            created_by_session_id="session",
        )

    instance.details = {"release_intent": {"token": "reserved"}}
    await db.flush()
    with pytest.raises(CheckpointConflictError, match="not accepting new turns"):
        await create_root_turn(
            db,
            session_id="session",
            workspace_instance_id=instance.id,
        )

    instance.details = {}
    await db.flush()
    turn = await create_root_turn(
        db,
        session_id="session",
        workspace_instance_id=instance.id,
    )
    assert turn.workspace_instance_id == instance.id


@pytest.mark.asyncio
async def test_root_child_checkpoint_ownership_mutations_and_side_effects(
    db: AsyncSession,
    tmp_path: Path,
) -> None:
    root_workspace = tmp_path / "主工作区"
    child_workspace = tmp_path / "子工作区"
    root_workspace.mkdir()
    child_workspace.mkdir()
    await _session(db, "root-session", root_workspace)
    await _session(db, "child-session", child_workspace)
    root_instance = await register_workspace_instance(
        db,
        root_workspace,
        created_by_session_id="root-session",
    )
    child_instance = await register_workspace_instance(
        db,
        child_workspace,
        kind="git_worktree",
        parent_instance_id=root_instance.id,
        created_by_session_id="child-session",
    )

    root = await create_root_turn(
        db,
        session_id="root-session",
        workspace_instance_id=root_instance.id,
        turn_id="job-root-turn",
        idempotency_key="prompt-1",
        request_message_id="message-before",
        stream_id="stream-root",
    )
    replay = await create_root_turn(
        db,
        session_id="root-session",
        workspace_instance_id=root_instance.id,
        turn_id="job-root-turn",
        idempotency_key="prompt-1",
        request_message_id="message-before",
        stream_id="stream-root",
    )
    assert replay.id == root.id == root.root_turn_id
    child = await create_child_turn(
        db,
        parent_turn_id=root.id,
        session_id="child-session",
        workspace_instance_id=child_instance.id,
        turn_id="job-child-turn",
        idempotency_key="subagent-1",
        stream_id="stream-child",
    )
    assert child.parent_turn_id == root.id
    assert child.root_turn_id == root.id
    assert child.workspace_instance_id == child_instance.id
    with pytest.raises(CheckpointValidationError, match="must not be blank"):
        await create_child_turn(
            db,
            parent_turn_id=root.id,
            session_id="child-session",
            workspace_instance_id=child_instance.id,
            turn_id="   ",
        )

    root_checkpoint = await prepare_checkpoint(
        db,
        turn_run_id=root.id,
        anchor_message_id="message-before",
        goal_run_id="goal-run-1",
        todo_snapshot=[{"id": "todo-1", "status": "pending"}],
    )
    child_checkpoint = await prepare_checkpoint(
        db,
        turn_run_id=child.id,
        anchor_message_id="child-message-before",
    )
    assert root_checkpoint.root_turn_id == child_checkpoint.root_turn_id == root.id
    assert root_checkpoint.turn_run_id == root.id
    assert child_checkpoint.turn_run_id == child.id
    assert root_checkpoint.id != child_checkpoint.id
    assert [
        checkpoint.id
        for checkpoint in await list_root_turn_checkpoints(db, root.id)
    ] == [root_checkpoint.id, child_checkpoint.id]

    root_store = FileVersionStore(
        root_workspace,
        storage_root=tmp_path / "private" / "root-versions",
    )
    report = root_workspace / "report.txt"
    report.write_text("before", encoding="utf-8")
    report_before = root_store.capture_before_mutation(report, operation="edit")
    assert report_before is not None
    removed = root_workspace / "removed.txt"
    removed.write_text("remove me", encoding="utf-8")
    removed_before = root_store.capture_before_mutation(removed, operation="delete")
    assert removed_before is not None

    await transition_checkpoint(
        db, root_checkpoint.id, target_state="committing"
    )
    modified = await record_checkpoint_change(
        db,
        checkpoint_id=root_checkpoint.id,
        turn_run_id=root.id,
        operation="modified",
        node_kind="file",
        relative_path="report.txt",
        before_version_id=report_before.id,
        before_sha256=report_before.sha256,
        before_mode=report_before.original_mode,
        after_sha256=hashlib.sha256(b"after").hexdigest(),
        after_mode=0o640,
        after_size=5,
        call_id="call-edit",
        version_store=root_store,
    )
    created = await record_checkpoint_change(
        db,
        checkpoint_id=root_checkpoint.id,
        turn_run_id=root.id,
        operation="created",
        node_kind="file",
        relative_path="new.txt",
        after_sha256=hashlib.sha256(b"new").hexdigest(),
        after_mode=0o600,
        after_size=3,
        version_store=root_store,
    )
    deleted = await record_checkpoint_change(
        db,
        checkpoint_id=root_checkpoint.id,
        turn_run_id=root.id,
        operation="deleted",
        node_kind="file",
        relative_path="removed.txt",
        before_version_id=removed_before.id,
        before_sha256=removed_before.sha256,
        before_mode=removed_before.original_mode,
        version_store=root_store,
    )
    directory = await record_checkpoint_change(
        db,
        checkpoint_id=root_checkpoint.id,
        turn_run_id=root.id,
        operation="created",
        node_kind="directory",
        relative_path="exports",
        after_mode=0o755,
        version_store=root_store,
    )
    link_target = "report.txt"
    symlink = await record_checkpoint_change(
        db,
        checkpoint_id=root_checkpoint.id,
        turn_run_id=root.id,
        operation="created",
        node_kind="symlink",
        relative_path="latest-report",
        after_sha256=hashlib.sha256(link_target.encode("utf-8")).hexdigest(),
        after_mode=0o777,
        details={"link_target": link_target},
        version_store=root_store,
    )
    assert [
        (item.sequence, item.operation, item.node_kind)
        for item in await list_checkpoint_changes(db, root_checkpoint.id)
    ] == [
        (1, "modified", "file"),
        (2, "created", "file"),
        (3, "deleted", "file"),
        (4, "created", "directory"),
        (5, "created", "symlink"),
    ]
    assert modified.before_exists is True and modified.after_exists is True
    assert created.before_exists is False and created.before_version_id is None
    assert deleted.after_exists is False and deleted.after_sha256 is None
    assert directory.before_exists is False
    assert symlink.after_size == len(link_target.encode("utf-8"))
    assert symlink.details == {"link_target": link_target}
    assert root_store.list_pins()[f"checkpoint:{root_checkpoint.id}"] == {
        report_before.id,
        removed_before.id,
    }
    root_store.unpin_versions(f"checkpoint:{root_checkpoint.id}")
    assert await reconcile_workspace_checkpoint_pins(
        db,
        root_instance.id,
        version_store=root_store,
    ) == 1
    root_store.pin_versions("checkpoint:orphan", [report_before.id])
    assert await reconcile_workspace_checkpoint_pins(
        db,
        root_instance.id,
        version_store=root_store,
    ) == 1
    assert "checkpoint:orphan" not in root_store.list_pins()

    with pytest.raises(CheckpointConflictError, match="does not own"):
        await record_checkpoint_change(
            db,
            checkpoint_id=root_checkpoint.id,
            turn_run_id=child.id,
            operation="created",
            node_kind="file",
            relative_path="wrong-owner.txt",
            after_sha256=hashlib.sha256(b"wrong").hexdigest(),
            version_store=root_store,
        )

    await transition_checkpoint(
        db, root_checkpoint.id, target_state="finalized"
    )
    assert root_checkpoint.child_turn_ids == [child.id]
    await record_irreversible_side_effect(
        db,
        checkpoint_id=child_checkpoint.id,
        turn_run_id=child.id,
        source="mcp",
        operation="send_email",
        audit_id="audit-1",
    )
    assert child_checkpoint.has_irreversible_side_effects is True
    assert child.has_irreversible_side_effects is True
    assert root.has_irreversible_side_effects is True
    assert root.external_side_effects == [
        {"source": "mcp", "operation": "send_email", "audit_id": "audit-1"}
    ]

    await transition_checkpoint(
        db, child_checkpoint.id, target_state="committing"
    )
    await transition_checkpoint(
        db, child_checkpoint.id, target_state="finalized"
    )
    with pytest.raises(CheckpointConflictError, match="rewindable checkpoint"):
        await release_workspace_instance(db, child_instance.id)
    child_store = FileVersionStore(
        child_workspace,
        storage_root=tmp_path / "private" / "child-versions",
    )
    await release_checkpoint_pin(
        db, child_checkpoint.id, version_store=child_store
    )
    await finish_turn(db, child.id, status="completed")
    released_child = await release_workspace_instance(db, child_instance.id)
    assert released_child.status == "released"

    await transition_checkpoint(
        db, root_checkpoint.id, target_state="rewinding"
    )
    finalized_at = root_checkpoint.time_finalized
    await transition_checkpoint(db, root_checkpoint.id, target_state="finalized")
    assert root_checkpoint.time_finalized == finalized_at
    await transition_checkpoint(
        db, root_checkpoint.id, target_state="rewinding"
    )
    await transition_checkpoint(db, root_checkpoint.id, target_state="rewound")
    await release_checkpoint_pin(
        db, root_checkpoint.id, version_store=root_store
    )
    assert f"checkpoint:{root_checkpoint.id}" not in root_store.list_pins()


@pytest.mark.asyncio
async def test_change_validation_and_database_require_before_sha256(
    db: AsyncSession,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    await _session(db, "session", workspace)
    instance = await register_workspace_instance(db, workspace)
    turn = await create_root_turn(
        db,
        session_id="session",
        workspace_instance_id=instance.id,
    )
    checkpoint = await prepare_checkpoint(
        db,
        turn_run_id=turn.id,
        anchor_message_id=None,
    )
    await transition_checkpoint(db, checkpoint.id, target_state="committing")
    store = FileVersionStore(
        workspace,
        storage_root=tmp_path / "private" / "versions",
    )
    target = workspace / "safe.txt"
    target.write_text("before", encoding="utf-8")
    version = store.capture_before_mutation(target, operation="edit")
    assert version is not None

    with pytest.raises(CheckpointValidationError, match="before_sha256"):
        await record_checkpoint_change(
            db,
            checkpoint_id=checkpoint.id,
            turn_run_id=turn.id,
            operation="modified",
            node_kind="file",
            relative_path="safe.txt",
            before_version_id=version.id,
            after_sha256=hashlib.sha256(b"after").hexdigest(),
            version_store=store,
        )
    with pytest.raises(CheckpointValidationError, match="does not match"):
        await record_checkpoint_change(
            db,
            checkpoint_id=checkpoint.id,
            turn_run_id=turn.id,
            operation="deleted",
            node_kind="file",
            relative_path="safe.txt",
            before_version_id=version.id,
            before_sha256="0" * 64,
            version_store=store,
        )
    with pytest.raises(CheckpointValidationError, match="workspace-relative"):
        await record_checkpoint_change(
            db,
            checkpoint_id=checkpoint.id,
            turn_run_id=turn.id,
            operation="created",
            node_kind="file",
            relative_path="../escape.txt",
            after_sha256="1" * 64,
            version_store=store,
        )
    with pytest.raises(CheckpointValidationError, match="newly created"):
        await record_checkpoint_change(
            db,
            checkpoint_id=checkpoint.id,
            turn_run_id=turn.id,
            operation="modified",
            node_kind="symlink",
            relative_path="link",
            before_sha256="1" * 64,
            after_sha256="2" * 64,
            details={"link_target": "safe.txt"},
            version_store=store,
        )
    with pytest.raises(CheckpointValidationError, match="outside the workspace"):
        outside_target = "../outside.txt"
        await record_checkpoint_change(
            db,
            checkpoint_id=checkpoint.id,
            turn_run_id=turn.id,
            operation="created",
            node_kind="symlink",
            relative_path="link",
            after_sha256=hashlib.sha256(
                outside_target.encode("utf-8")
            ).hexdigest(),
            details={"link_target": outside_target},
            version_store=store,
        )

    invalid = CheckpointChange(
        id=generate_ulid(),
        checkpoint_id=checkpoint.id,
        turn_run_id=turn.id,
        sequence=1,
        operation="modified",
        node_kind="file",
        relative_path="safe.txt",
        before_exists=True,
        before_version_id=version.id,
        before_sha256=None,
        after_exists=True,
        after_sha256="2" * 64,
        details={},
    )
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            db.add(invalid)
            await db.flush()

    invalid_symlink = CheckpointChange(
        id=generate_ulid(),
        checkpoint_id=checkpoint.id,
        turn_run_id=turn.id,
        sequence=1,
        operation="modified",
        node_kind="symlink",
        relative_path="link",
        before_exists=True,
        before_sha256="1" * 64,
        after_exists=True,
        after_sha256="2" * 64,
        details={"link_target": "safe.txt"},
    )
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            db.add(invalid_symlink)
            await db.flush()


@pytest.mark.workspace_identity_v2
def test_durable_owner_pins_survive_retention_and_old_manifest_shape(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = FileVersionStore(
        workspace,
        storage_root=tmp_path / "private" / "versions",
        limits=FileVersionLimits(
            max_file_bytes=16,
            max_workspace_bytes=32,
            max_versions_per_file=2,
            max_total_versions=2,
        ),
    )
    target = workspace / "timeline.txt"
    target.write_text("A", encoding="utf-8")
    version_a = store.capture_before_mutation(target, operation="edit")
    assert version_a is not None
    assert store.pin_versions("checkpoint:one", [version_a.id]) == {version_a.id}
    assert store.pin_versions("checkpoint:one", [version_a.id]) == frozenset()
    assert json.loads(store.manifest_path.read_text(encoding="utf-8"))[
        "schema_version"
    ] == 3

    target.write_text("B", encoding="utf-8")
    version_b = store.capture_before_mutation(target, operation="edit")
    assert version_b is not None
    target.write_text("C", encoding="utf-8")
    version_c = store.capture_before_mutation(target, operation="edit")
    assert version_c is not None
    assert {item.id for item in store.list_versions()} == {
        version_a.id,
        version_c.id,
    }

    assert store.unpin_versions("checkpoint:one") == {version_a.id}
    target.write_text("D", encoding="utf-8")
    version_d = store.capture_before_mutation(target, operation="edit")
    assert version_d is not None
    assert {item.id for item in store.list_versions()} == {
        version_c.id,
        version_d.id,
    }

    # v1 manifests written before checkpoint pins remain readable.
    manifest = json.loads(store.manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = 1
    manifest["workspace_identity"] = {
        "dev": store._workspace_identity[0],
        "ino": store._workspace_identity[1],
    }
    manifest.pop("pins")
    store.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert store.list_pins() == {}
    target.write_text("E", encoding="utf-8")
    assert store.capture_before_mutation(target, operation="edit") is not None
    assert json.loads(store.manifest_path.read_text(encoding="utf-8"))[
        "schema_version"
    ] == 3


def test_v100_head_migrates_to_current_v110_schema(tmp_path: Path) -> None:
    database = tmp_path / "v100.db"
    engine = migrations.create_sync_engine(f"sqlite:///{database}")
    try:
        with engine.begin() as connection:
            config = migrations._alembic_config(connection)
            try:
                migrations.command.upgrade(
                    config,
                    migrations.V100_GOAL_USAGE_LEDGER_REVISION,
                )
            finally:
                config.attributes.pop("connection", None)
    finally:
        engine.dispose()

    result = migrations.upgrade_sqlite_database(
        f"sqlite+aiosqlite:///{database}"
    )

    assert result is not None
    assert result.previous_revision == migrations.V100_GOAL_USAGE_LEDGER_REVISION
    assert result.current_revision == migrations.CURRENT_HEAD_REVISION
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        checkpoint_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(session_checkpoint)")
        }
    assert {
        "workspace_instance",
        "turn_run",
        "session_checkpoint",
        "checkpoint_change",
        "office_user_template",
    } <= tables
    assert {
        "turn_run_id",
        "root_turn_id",
        "pin_state",
        "todo_snapshot",
        "child_turn_ids",
    } <= checkpoint_columns

    engine = migrations.create_sync_engine(f"sqlite:///{database}")
    try:
        with engine.begin() as connection:
            config = migrations._alembic_config(connection)
            try:
                migrations.command.downgrade(
                    config,
                    migrations.V100_GOAL_USAGE_LEDGER_REVISION,
                )
            finally:
                config.attributes.pop("connection", None)
    finally:
        engine.dispose()
    with sqlite3.connect(database) as connection:
        downgraded_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {
        "workspace_instance",
        "turn_run",
        "session_checkpoint",
        "checkpoint_change",
        "office_user_template",
    }.isdisjoint(downgraded_tables)
