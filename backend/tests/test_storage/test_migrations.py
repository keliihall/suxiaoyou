"""Regression tests for the v0.7.3 -> v0.8.0 database boundary."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

from app.storage import migrations
from app.storage.migrations import (
    DatabaseMigrationError,
    V080_HEAD_REVISION,
    upgrade_sqlite_database,
)


FIXTURE_SQL = (
    Path(__file__).resolve().parents[1] / "fixtures" / "v0_7_3_database.sql"
)


def _materialize_v073_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(FIXTURE_SQL.read_text(encoding="utf-8"))


def _table_names(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }


def _revision(path: Path) -> str | None:
    with sqlite3.connect(path) as connection:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        return str(row[0]) if row else None


def _quick_check(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        return str(connection.execute("PRAGMA quick_check").fetchone()[0])


def test_new_database_is_initialized_at_v080_head(tmp_path: Path) -> None:
    database = tmp_path / "new.db"

    result = upgrade_sqlite_database(f"sqlite+aiosqlite:///{database}")

    assert result is not None
    assert result.created is True
    assert result.upgraded is True
    assert result.backup_path is None
    assert _revision(database) == V080_HEAD_REVISION
    assert _quick_check(database) == "ok"
    assert {
        "project",
        "session",
        "message",
        "part",
        "todo",
        "session_file",
        "scheduled_task",
        "task_run",
        "workspace_memory",
        "session_input",
        "idempotency_record",
        "alembic_version",
    } <= _table_names(database)


def test_embedded_migration_does_not_disable_application_loggers(tmp_path: Path) -> None:
    database = tmp_path / "logging.db"
    application_logger = logging.getLogger("app.api.files")
    application_logger.disabled = False

    upgrade_sqlite_database(f"sqlite+aiosqlite:///{database}")

    assert application_logger.disabled is False


def test_real_v073_fixture_upgrades_without_losing_data(tmp_path: Path) -> None:
    database = tmp_path / "suxiaoyou.db"
    _materialize_v073_database(database)

    result = upgrade_sqlite_database(f"sqlite+aiosqlite:///{database}")

    assert result is not None
    assert result.created is False
    assert result.upgraded is True
    assert result.previous_revision is None
    assert result.backup_path is not None and result.backup_path.is_file()
    assert _quick_check(result.backup_path) == "ok"
    assert "alembic_version" not in _table_names(result.backup_path)
    assert "session_input" not in _table_names(result.backup_path)
    assert "idempotency_record" not in _table_names(result.backup_path)

    assert _revision(database) == V080_HEAD_REVISION
    assert _quick_check(database) == "ok"
    assert "session_input" in _table_names(database)
    assert "idempotency_record" in _table_names(database)
    with sqlite3.connect(database) as connection:
        session = connection.execute(
            "SELECT title, model_id, provider_id FROM session WHERE id = ?",
            ("session-v073",),
        ).fetchone()
        part = connection.execute(
            "SELECT json_extract(data, '$.text') FROM part WHERE id = ?",
            ("part-v073",),
        ).fetchone()
        task = connection.execute(
            "SELECT name, run_count FROM scheduled_task WHERE id = ?",
            ("task-v073",),
        ).fetchone()
        indexes = {
            str(row[1])
            for row in connection.execute('PRAGMA index_list("session_input")')
        }
        columns = {
            str(row[1])
            for row in connection.execute('PRAGMA table_info("session_input")')
        }

    assert session == ("需要保留的 v0.7.3 对话", "deepseek-chat", "deepseek")
    assert part == ("这条历史消息不能在升级时丢失",)
    assert task == ("每日简报", 7)
    assert columns == migrations.V080_SESSION_INPUT_COLUMNS
    assert "ix_session_input_dispatch" in indexes
    assert "sqlite_autoindex_session_input_2" in indexes


def test_repeated_startup_is_idempotent_and_does_not_add_backups(
    tmp_path: Path,
) -> None:
    database = tmp_path / "suxiaoyou.db"
    _materialize_v073_database(database)

    first = upgrade_sqlite_database(f"sqlite+aiosqlite:///{database}")
    backups_after_first = sorted(tmp_path.glob("suxiaoyou.db.pre-v0.8.0-*.bak"))
    second = upgrade_sqlite_database(f"sqlite+aiosqlite:///{database}")

    assert first is not None and first.upgraded is True
    assert second is not None
    assert second.upgraded is False
    assert second.created is False
    assert second.previous_revision == V080_HEAD_REVISION
    assert second.backup_path is None
    assert sorted(tmp_path.glob("suxiaoyou.db.pre-v0.8.0-*.bak")) == backups_after_first
    assert len(backups_after_first) == 1
    assert _revision(database) == V080_HEAD_REVISION
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM session").fetchone() == (1,)


def test_unsupported_directory_fsync_does_not_report_false_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "suxiaoyou.db"
    _materialize_v073_database(database)

    def unsupported_fsync(_fd: int) -> None:
        raise OSError("directory fsync unsupported")

    monkeypatch.setattr(migrations.os, "fsync", unsupported_fsync)

    result = upgrade_sqlite_database(f"sqlite+aiosqlite:///{database}")

    assert result is not None and result.upgraded is True
    assert _revision(database) == V080_HEAD_REVISION
    assert _quick_check(database) == "ok"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT title FROM session WHERE id = 'session-v073'"
        ).fetchone() == ("需要保留的 v0.7.3 对话",)


def test_unversioned_complete_v080_snapshot_is_stamped_without_recreating_tables(
    tmp_path: Path,
) -> None:
    database = tmp_path / "development-snapshot.db"
    upgrade_sqlite_database(f"sqlite+aiosqlite:///{database}")
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE alembic_version")

    result = upgrade_sqlite_database(f"sqlite+aiosqlite:///{database}")

    assert result is not None and result.upgraded is True
    assert _revision(database) == V080_HEAD_REVISION
    assert "session_input" in _table_names(database)
    assert "idempotency_record" in _table_names(database)


def test_failed_upgrade_leaves_original_readable_and_retains_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "suxiaoyou.db"
    _materialize_v073_database(database)

    def fail_after_mutating_staging(
        staging_path: Path,
        *,
        stamp_revision: str | None,
    ) -> None:
        del stamp_revision
        with sqlite3.connect(staging_path) as connection:
            connection.execute("CREATE TABLE partial_migration (value TEXT)")
        raise RuntimeError("forced revision failure")

    monkeypatch.setattr(migrations, "_run_alembic", fail_after_mutating_staging)

    with pytest.raises(DatabaseMigrationError) as exc_info:
        upgrade_sqlite_database(f"sqlite+aiosqlite:///{database}")

    error = exc_info.value
    assert "forced revision failure" in str(error)
    assert "original database was left untouched" in str(error)
    assert error.backup_path is not None and error.backup_path.is_file()
    assert error.failed_copy_path is not None and error.failed_copy_path.is_file()

    assert _quick_check(database) == "ok"
    assert _quick_check(error.backup_path) == "ok"
    assert "alembic_version" not in _table_names(database)
    assert "session_input" not in _table_names(database)
    assert "partial_migration" not in _table_names(database)
    assert "partial_migration" in _table_names(error.failed_copy_path)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT title FROM session WHERE id = 'session-v073'"
        ).fetchone() == ("需要保留的 v0.7.3 对话",)
    with sqlite3.connect(error.backup_path) as connection:
        assert connection.execute(
            "SELECT title FROM session WHERE id = 'session-v073'"
        ).fetchone() == ("需要保留的 v0.7.3 对话",)


def test_unversioned_partial_schema_is_rejected_without_guessing(
    tmp_path: Path,
) -> None:
    database = tmp_path / "partial.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE session (id VARCHAR PRIMARY KEY)")
        connection.execute("INSERT INTO session (id) VALUES ('keep-me')")

    with pytest.raises(DatabaseMigrationError) as exc_info:
        upgrade_sqlite_database(f"sqlite+aiosqlite:///{database}")

    assert "does not match the supported v0.7.3 baseline" in str(exc_info.value)
    assert "No guessed schema changes were applied" in str(exc_info.value)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT id FROM session").fetchone() == ("keep-me",)
        assert "alembic_version" not in _table_names(database)
