from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from app.models import Base
from app.storage import migrations
from app.storage.migrations import CURRENT_HEAD_REVISION, upgrade_sqlite_database


def test_v100_database_adds_goal_control_plane_and_preserves_todos(
    tmp_path: Path,
) -> None:
    database = tmp_path / "pre-goal.db"
    migrations._run_alembic_revision(
        database,
        stamp_revision=None,
        target_revision=migrations.V100_INVOCATION_SOURCE_REVISION,
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO session (
                id, slug, directory, title, version, time_created, time_updated
            ) VALUES (
                'goal-session', '', '.', 'Existing conversation', '1.0.0',
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            INSERT INTO todo (
                id, session_id, content, status, active_form, position,
                time_created, time_updated
            ) VALUES (
                'legacy-todo', 'goal-session', 'Keep me', 'pending', 'Keeping', 1,
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            """
        )

    result = upgrade_sqlite_database(f"sqlite+aiosqlite:///{database}")

    assert result is not None
    assert result.previous_revision == migrations.V100_INVOCATION_SOURCE_REVISION
    assert result.current_revision == CURRENT_HEAD_REVISION
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        todo_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(todo)")
        }
        todo_foreign_keys = {
            (row[3], row[2], row[4], row[6])
            for row in connection.execute("PRAGMA foreign_key_list(todo)")
        }
        todo_indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(todo)")
        }
        usage_indexes = {
            row[1]
            for row in connection.execute("PRAGMA index_list(goal_usage_record)")
        }
        usage_foreign_keys = {
            (row[3], row[2], row[4], row[6])
            for row in connection.execute(
                "PRAGMA foreign_key_list(goal_usage_record)"
            )
        }
        legacy_todo = connection.execute(
            "SELECT content, goal_id FROM todo WHERE id = 'legacy-todo'"
        ).fetchone()
        connection.execute(
            """
            INSERT INTO session_goal (
                id, session_id, objective, token_budget, cost_budget_microusd,
                time_budget_seconds, max_continuations
            ) VALUES ('goal', 'goal-session', 'Test constraints', 100, 100, 100, 10)
            """
        )
        connection.execute(
            """
            INSERT INTO goal_run (
                id, goal_id, ordinal, goal_revision, idempotency_key, trigger
            ) VALUES ('run-1', 'goal', 1, 1, 'migration-run-1', 'initial')
            """
        )
        connection.execute(
            """
            INSERT INTO session (
                id, slug, directory, title, version, time_created, time_updated
            ) VALUES (
                'other-session', '', '.', 'Other conversation', '1.0.0',
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO todo (
                    id, session_id, goal_id, content, status, active_form,
                    position, time_created, time_updated
                ) VALUES (
                    'cross-session-todo', 'other-session', 'goal', 'Invalid',
                    'pending', '', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE session_goal SET no_progress_count = -1 WHERE id = 'goal'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE session_goal SET status = 'complete' WHERE id = 'goal'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO goal_run (
                    id, goal_id, ordinal, goal_revision, idempotency_key, trigger
                ) VALUES ('run-2', 'goal', 2, 1, 'migration-run-2', 'auto')
                """
            )
        connection.execute(
            """
            INSERT INTO goal_usage_record (
                id, goal_run_id, source_kind, source_key, tokens_used,
                cost_used_microusd
            ) VALUES ('usage-1', 'run-1', 'provider', 'provider:message-1', 7, 11)
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO goal_usage_record (
                    id, goal_run_id, source_kind, source_key, tokens_used,
                    cost_used_microusd
                ) VALUES (
                    'usage-duplicate', 'run-1', 'provider',
                    'provider:message-1', 7, 11
                )
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO goal_usage_record (
                    id, goal_run_id, source_kind, source_key, tokens_used,
                    cost_used_microusd
                ) VALUES (
                    'usage-negative', 'run-1', 'provider',
                    'provider:message-2', -1, 0
                )
                """
            )

    assert {"session_goal", "goal_run", "goal_usage_record"} <= tables
    assert "goal_id" in todo_columns
    assert ("goal_id", "session_goal", "id", "CASCADE") in todo_foreign_keys
    assert (
        "session_id",
        "session_goal",
        "session_id",
        "CASCADE",
    ) in todo_foreign_keys
    assert "ix_todo_goal" in todo_indexes
    assert "ix_goal_usage_run" in usage_indexes
    assert ("goal_run_id", "goal_run", "id", "CASCADE") in usage_foreign_keys
    assert legacy_todo == ("Keep me", None)


def test_goal_migration_downgrade_restores_pre_goal_shape(tmp_path: Path) -> None:
    database = tmp_path / "goal-downgrade.db"
    migrations._run_alembic_revision(
        database,
        stamp_revision=None,
        target_revision=CURRENT_HEAD_REVISION,
    )
    engine = migrations.create_sync_engine(f"sqlite:///{database}")
    try:
        with engine.begin() as connection:
            config = migrations._alembic_config(connection)
            try:
                migrations.command.downgrade(
                    config,
                    migrations.V100_INVOCATION_SOURCE_REVISION,
                )
            finally:
                config.attributes.pop("connection", None)
    finally:
        engine.dispose()

    with sqlite3.connect(database) as connection:
        revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        todo_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(todo)")
        }
    assert revision == (migrations.V100_INVOCATION_SOURCE_REVISION,)
    assert "session_goal" not in tables
    assert "goal_run" not in tables
    assert "goal_usage_record" not in tables
    assert "goal_id" not in todo_columns


def test_goal_migration_head_matches_orm_metadata(tmp_path: Path) -> None:
    database = tmp_path / "goal-metadata-parity.db"
    migrations._run_alembic_revision(
        database,
        stamp_revision=None,
        target_revision=CURRENT_HEAD_REVISION,
    )
    engine = migrations.create_sync_engine(f"sqlite:///{database}")
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            differences = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()

    assert differences == []
