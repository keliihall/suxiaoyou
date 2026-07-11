"""Regression tests for the v0.7.3 -> v0.8.0 database boundary."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace

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


class _TrackedSQLiteConnection:
    """Proxy preserving sqlite3's non-closing context-manager semantics."""

    def __init__(self, inner: sqlite3.Connection) -> None:
        self.inner = inner
        self.closed = False

    def __enter__(self):
        self.inner.__enter__()
        return self

    def __exit__(self, *exc_info):
        return self.inner.__exit__(*exc_info)

    def close(self) -> None:
        self.closed = True
        self.inner.close()

    def __getattr__(self, name: str):
        return getattr(self.inner, name)


def _track_migration_sqlite_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[_TrackedSQLiteConnection], object]:
    real_connect = migrations.sqlite3.connect
    tracked: list[_TrackedSQLiteConnection] = []

    def tracking_connect(*args, **kwargs):
        connection = _TrackedSQLiteConnection(real_connect(*args, **kwargs))
        tracked.append(connection)
        return connection

    monkeypatch.setattr(migrations.sqlite3, "connect", tracking_connect)
    return tracked, real_connect


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


def test_new_database_closes_validation_handles_before_windows_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model Windows sharing semantics: replacing an open DB must fail."""

    database = tmp_path / "new.db"
    token = "windows-handle-order"
    staging = database.with_name(f".{database.name}.{token}.migrating")
    # Build a real valid staging DB before installing connection tracking.
    migrations._run_alembic(staging, stamp_revision=None)
    monkeypatch.setattr(migrations, "_migration_token", lambda: token)
    monkeypatch.setattr(migrations, "_run_alembic", lambda *_args, **_kwargs: None)
    tracked, real_connect = _track_migration_sqlite_connections(monkeypatch)
    real_replace = migrations.os.replace
    replace_checked = False

    def windows_replace(source, destination) -> None:
        nonlocal replace_checked
        if Path(source) == staging and Path(destination) == database:
            replace_checked = True
            if any(not connection.closed for connection in tracked):
                raise PermissionError(32, "file is being used by another process")
        real_replace(source, destination)

    monkeypatch.setattr(migrations.os, "replace", windows_replace)

    result = migrations._initialize_new_database(database)

    assert result.created is True
    assert replace_checked is True
    assert tracked and all(connection.closed for connection in tracked)
    # Avoid the test module's intentionally non-closing sqlite context helper
    # while the module-level connect function is patched.
    connection = real_connect(database)
    try:
        revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
    finally:
        connection.close()
    assert revision == (V080_HEAD_REVISION,)


def test_existing_database_closes_all_handles_before_windows_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise live, backup, and staging handles with Windows replace rules."""

    database = tmp_path / "suxiaoyou.db"
    fixture_connection = sqlite3.connect(database)
    try:
        fixture_connection.executescript(FIXTURE_SQL.read_text(encoding="utf-8"))
        fixture_connection.commit()
    finally:
        fixture_connection.close()

    token = "windows-existing-handle-order"
    staging = database.with_name(f".{database.name}.{token}.migrating")
    backup = database.with_name(f"{database.name}.pre-v0.8.0-{token}.bak")
    real_connect = migrations.sqlite3.connect
    tracked: list[tuple[Path, sqlite3.Connection]] = []

    def retaining_connect(database_path, *args, **kwargs):
        connection = real_connect(database_path, *args, **kwargs)
        tracked.append((Path(database_path).resolve(), connection))
        return connection

    def connection_is_open(connection: sqlite3.Connection) -> bool:
        try:
            connection.execute("SELECT 1")
        except sqlite3.ProgrammingError:
            return False
        return True

    monkeypatch.setattr(migrations, "_migration_token", lambda: token)
    monkeypatch.setattr(migrations.sqlite3, "connect", retaining_connect)
    real_replace = migrations.os.replace
    replace_checked = False
    tracked_paths_at_replace: set[Path] = set()

    def windows_replace(source, destination) -> None:
        nonlocal replace_checked, tracked_paths_at_replace
        if Path(source) == staging and Path(destination) == database:
            replace_checked = True
            tracked_paths_at_replace = {path for path, _connection in tracked}
            locked_paths = [
                path
                for path, connection in tracked
                if connection_is_open(connection)
            ]
            if locked_paths:
                raise PermissionError(
                    32,
                    "file is being used by another process: "
                    + ", ".join(str(path) for path in locked_paths),
                )
        real_replace(source, destination)

    monkeypatch.setattr(migrations.os, "replace", windows_replace)

    result = upgrade_sqlite_database(f"sqlite+aiosqlite:///{database}")

    assert result is not None and result.upgraded is True
    assert replace_checked is True
    assert database.resolve() in tracked_paths_at_replace
    assert backup.resolve() in tracked_paths_at_replace
    assert staging.resolve() in tracked_paths_at_replace
    assert tracked and all(not connection_is_open(connection) for _, connection in tracked)

    installed_connection = real_connect(database)
    try:
        revision = installed_connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
    finally:
        installed_connection.close()
    assert revision == (V080_HEAD_REVISION,)


def test_failed_new_database_validation_closes_handles_before_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "invalid.db"
    token = "windows-failed-cleanup"
    staging = database.with_name(f".{database.name}.{token}.migrating")
    real_connect = migrations.sqlite3.connect

    def create_invalid_staging(path: Path, *, stamp_revision: str | None) -> None:
        del stamp_revision
        connection = real_connect(path)
        try:
            connection.execute("CREATE TABLE incomplete (value TEXT)")
            connection.commit()
        finally:
            connection.close()

    monkeypatch.setattr(migrations, "_migration_token", lambda: token)
    monkeypatch.setattr(migrations, "_run_alembic", create_invalid_staging)
    tracked, _ = _track_migration_sqlite_connections(monkeypatch)
    real_remove = migrations._remove_sqlite_files
    cleanup_checked = False

    def windows_remove(path: Path) -> None:
        nonlocal cleanup_checked
        cleanup_checked = True
        if any(not connection.closed for connection in tracked):
            raise PermissionError(32, "file is being used by another process")
        real_remove(path)

    monkeypatch.setattr(migrations, "_remove_sqlite_files", windows_remove)

    with pytest.raises(DatabaseMigrationError) as exc_info:
        migrations._initialize_new_database(database)

    assert "unexpected revision" in str(exc_info.value)
    assert cleanup_checked is True
    assert tracked and all(connection.closed for connection in tracked)
    assert not staging.exists()


def test_failed_new_database_cleanup_error_preserves_migration_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database = tmp_path / "cleanup-locked.db"
    token = "windows-cleanup-locked"
    staging = database.with_name(f".{database.name}.{token}.migrating")

    def fail_after_creating_staging(
        path: Path,
        *,
        stamp_revision: str | None,
    ) -> None:
        del stamp_revision
        connection = sqlite3.connect(path)
        try:
            connection.execute("CREATE TABLE incomplete (value TEXT)")
            connection.commit()
        finally:
            connection.close()
        raise RuntimeError("original migration failure")

    def windows_locked_cleanup(path: Path) -> None:
        assert path == staging
        raise PermissionError(32, "staging file is being used by another process")

    monkeypatch.setattr(migrations, "_migration_token", lambda: token)
    monkeypatch.setattr(migrations, "_run_alembic", fail_after_creating_staging)
    monkeypatch.setattr(migrations, "_remove_sqlite_files", windows_locked_cleanup)

    with caplog.at_level(logging.WARNING, logger=migrations.__name__):
        with pytest.raises(DatabaseMigrationError) as exc_info:
            migrations._initialize_new_database(database)

    error = exc_info.value
    assert "original migration failure" in str(error)
    assert "Cleanup of the failed staging database also failed" in str(error)
    assert "staging file is being used by another process" in str(error)
    assert isinstance(error.__cause__, RuntimeError)
    assert str(error.__cause__) == "original migration failure"
    assert error.failed_copy_path == staging
    assert staging.is_file()
    assert "Could not remove failed new-database staging files" in caplog.text


def test_alembic_connection_reference_is_dropped_before_engine_dispose_on_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FakeConnection:
        def __enter__(self):
            events.append("connection-open")
            return self

        def __exit__(self, *_exc_info) -> None:
            events.append("connection-close")

    connection = FakeConnection()

    class FakeEngine:
        def connect(self) -> FakeConnection:
            return connection

        def dispose(self) -> None:
            events.append("engine-dispose")

    engine = FakeEngine()
    config = SimpleNamespace(attributes={"connection": connection})

    def fail_upgrade(received_config, revision: str) -> None:
        assert received_config.attributes["connection"] is connection
        assert revision == "head"
        events.append("upgrade")
        raise RuntimeError("forced Alembic failure")

    monkeypatch.setattr(migrations, "create_sync_engine", lambda *_args: engine)
    monkeypatch.setattr(migrations, "_alembic_config", lambda _connection: config)
    monkeypatch.setattr(migrations.command, "upgrade", fail_upgrade)

    with pytest.raises(RuntimeError, match="forced Alembic failure"):
        migrations._run_alembic(tmp_path / "staging.db", stamp_revision=None)

    assert "connection" not in config.attributes
    assert events == [
        "connection-open",
        "upgrade",
        "connection-close",
        "engine-dispose",
    ]


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
