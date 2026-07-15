"""Regression tests for the v0.7.3 -> v0.8.0 database boundary."""

from __future__ import annotations

import logging
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.storage import migrations
from app.storage.migrations import (
    DatabaseMigrationError,
    DatabaseRestoreError,
    CURRENT_HEAD_REVISION,
    V080_HEAD_REVISION,
    database_lease,
    list_database_backups,
    restore_database_backup,
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
        try:
            row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        except sqlite3.OperationalError:
            return None
        return str(row[0]) if row else None


def _quick_check(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        return str(connection.execute("PRAGMA quick_check").fetchone()[0])


def _rewrite_manifest_for_backup(
    manifest_path: Path,
    backup_path: Path,
    *,
    source_revision: str | None,
) -> None:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["source_revision"] = source_revision
    payload["size"] = backup_path.stat().st_size
    payload["sha256"] = migrations._sha256_file(backup_path)
    manifest_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


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
        "session_goal",
        "goal_run",
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
    backup = database.with_name(f"{database.name}.pre-v1.0.0-{token}.bak")
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
        def begin(self) -> FakeConnection:
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


def test_rc1_database_adds_language_to_existing_queued_inputs(tmp_path: Path) -> None:
    database = tmp_path / "rc1.db"
    engine = migrations.create_sync_engine(f"sqlite:///{database}")
    try:
        with engine.connect() as connection:
            config = migrations._alembic_config(connection)
            try:
                migrations.command.upgrade(config, migrations.V080_IDEMPOTENCY_REVISION)
            finally:
                config.attributes.pop("connection", None)
    finally:
        engine.dispose()

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO session_input (
                id, session_id, client_request_id, mode, status, position,
                text, attachments, agent
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "queued-before-rc2",
                "session-before-rc2",
                "request-before-rc2",
                "queue",
                "queued",
                1,
                "continue",
                "[]",
                "build",
            ),
        )

    result = upgrade_sqlite_database(f"sqlite+aiosqlite:///{database}")

    assert result is not None
    assert result.previous_revision == migrations.V080_IDEMPOTENCY_REVISION
    assert result.current_revision == V080_HEAD_REVISION
    with sqlite3.connect(database) as connection:
        language = connection.execute(
            "SELECT language FROM session_input WHERE id = ?",
            ("queued-before-rc2",),
        ).fetchone()
    assert language == ("zh",)


def test_repeated_startup_is_idempotent_and_does_not_add_backups(
    tmp_path: Path,
) -> None:
    database = tmp_path / "suxiaoyou.db"
    _materialize_v073_database(database)

    first = upgrade_sqlite_database(f"sqlite+aiosqlite:///{database}")
    backups_after_first = sorted(tmp_path.glob("suxiaoyou.db.pre-v1.0.0-*.bak"))
    second = upgrade_sqlite_database(f"sqlite+aiosqlite:///{database}")

    assert first is not None and first.upgraded is True
    assert second is not None
    assert second.upgraded is False
    assert second.created is False
    assert second.previous_revision == V080_HEAD_REVISION
    assert second.backup_path is None
    assert sorted(tmp_path.glob("suxiaoyou.db.pre-v1.0.0-*.bak")) == backups_after_first
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

    def unsupported_fsync(_directory: Path) -> None:
        raise OSError("directory fsync unsupported")

    monkeypatch.setattr(migrations, "_fsync_directory", unsupported_fsync)

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


def test_v083_database_advances_to_formal_v090_boundary(tmp_path: Path) -> None:
    database = tmp_path / "v083.db"
    engine = migrations.create_sync_engine(f"sqlite:///{database}")
    try:
        with engine.begin() as connection:
            config = migrations._alembic_config(connection)
            try:
                migrations.command.upgrade(
                    config,
                    migrations.V083_SESSION_INPUT_LANGUAGE_REVISION,
                )
            finally:
                config.attributes.pop("connection", None)
    finally:
        engine.dispose()

    result = upgrade_sqlite_database(f"sqlite+aiosqlite:///{database}")

    assert result is not None
    assert result.previous_revision == migrations.V083_SESSION_INPUT_LANGUAGE_REVISION
    assert result.current_revision == CURRENT_HEAD_REVISION
    assert result.backup_metadata_path is not None
    assert _revision(database) == CURRENT_HEAD_REVISION


def test_v100_audit_database_adds_nullable_invocation_provenance(
    tmp_path: Path,
) -> None:
    database = tmp_path / "v100-audit.db"
    engine = migrations.create_sync_engine(f"sqlite:///{database}")
    try:
        with engine.begin() as connection:
            config = migrations._alembic_config(connection)
            try:
                migrations.command.upgrade(
                    config,
                    migrations.V100_SECURITY_AUDIT_REVISION,
                )
            finally:
                config.attributes.pop("connection", None)
            connection.exec_driver_sql(
                """
                INSERT INTO security_audit_event (
                    id, source_kind, source_id, capability, action,
                    decision, outcome, details
                ) VALUES (
                    'legacy-audit', 'builtin', 'suyo', 'filesystem_read',
                    'read', 'allow', 'success', '{}'
                )
                """
            )
    finally:
        engine.dispose()

    result = upgrade_sqlite_database(f"sqlite+aiosqlite:///{database}")

    assert result is not None
    assert result.previous_revision == migrations.V100_SECURITY_AUDIT_REVISION
    assert result.current_revision == CURRENT_HEAD_REVISION
    with sqlite3.connect(database) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(security_audit_event)"
            )
        }
        legacy = connection.execute(
            """
            SELECT invocation_source_kind, invocation_source_id
            FROM security_audit_event WHERE id = 'legacy-audit'
            """
        ).fetchone()
        indexes = {
            row[1]
            for row in connection.execute(
                "PRAGMA index_list(security_audit_event)"
            )
        }
    assert {
        "invocation_source_kind",
        "invocation_source_id",
    } <= columns
    assert legacy == (None, None)
    assert "ix_security_audit_invocation_source" in indexes


def test_future_revision_is_rejected_without_backup_or_mutation(tmp_path: Path) -> None:
    database = tmp_path / "future.db"
    upgrade_sqlite_database(f"sqlite+aiosqlite:///{database}")
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE alembic_version SET version_num = '9999_future'")

    with pytest.raises(DatabaseMigrationError, match="newer build"):
        upgrade_sqlite_database(f"sqlite+aiosqlite:///{database}")

    assert _revision(database) == "9999_future"
    assert not list(tmp_path.glob("future.db.*.bak"))


def test_upgrade_backup_has_checksum_manifest_and_is_listed(tmp_path: Path) -> None:
    database = tmp_path / "suxiaoyou.db"
    _materialize_v073_database(database)

    result = upgrade_sqlite_database(f"sqlite+aiosqlite:///{database}")

    assert result is not None and result.backup_path is not None
    assert result.backup_metadata_path is not None
    manifest = json.loads(result.backup_metadata_path.read_text(encoding="utf-8"))
    assert manifest["app_version"] == "1.0.0"
    assert manifest["database_name"] == database.name
    assert manifest["database_path"] == str(database.resolve())
    assert manifest["backup_file"] == result.backup_path.name
    assert manifest["source_revision"] is None
    assert manifest["target_revision"] == CURRENT_HEAD_REVISION
    assert len(manifest["sha256"]) == 64
    assert manifest["size"] == result.backup_path.stat().st_size
    assert manifest["integrity_verified"] is True
    records = list_database_backups(f"sqlite+aiosqlite:///{database}")
    assert len(records) == 1
    assert records[0]["verified"] is True


def test_forged_known_revision_schema_is_not_verified_or_restored(
    tmp_path: Path,
) -> None:
    database = tmp_path / "forged.db"
    upgrade_sqlite_database(f"sqlite+aiosqlite:///{database}")
    backup = tmp_path / "forged.db.claimed-v073.bak"
    migrations._online_backup(database, backup)
    manifest = migrations._write_backup_manifest(
        database_path=database,
        backup_path=backup,
        source_revision=CURRENT_HEAD_REVISION,
        target_revision=CURRENT_HEAD_REVISION,
        reason="test",
        integrity_verified=True,
    )

    # Model a self-consistent but false manifest/revision claim: checksum,
    # size, and the revision row all agree, while the table shape is newer.
    with sqlite3.connect(backup) as connection:
        connection.execute(
            "UPDATE alembic_version SET version_num = ?",
            (migrations.V073_BASELINE_REVISION,),
        )
    with pytest.raises(RuntimeError, match="does not exactly match revision"):
        migrations._write_backup_manifest(
            database_path=database,
            backup_path=backup,
            source_revision=migrations.V073_BASELINE_REVISION,
            target_revision=CURRENT_HEAD_REVISION,
            reason="test-forged-claim",
            integrity_verified=True,
        )
    _rewrite_manifest_for_backup(
        manifest,
        backup,
        source_revision=migrations.V073_BASELINE_REVISION,
    )

    records = list_database_backups(f"sqlite+aiosqlite:///{database}")
    assert len(records) == 1
    assert records[0]["verified"] is False
    assert "schema does not match" in str(records[0]["error"])
    with pytest.raises(DatabaseRestoreError, match="schema does not match"):
        restore_database_backup(f"sqlite+aiosqlite:///{database}", manifest)
    assert _revision(database) == CURRENT_HEAD_REVISION


def test_incompatible_unversioned_schema_is_not_verified_or_restored(
    tmp_path: Path,
) -> None:
    database = tmp_path / "unversioned.db"
    upgrade_sqlite_database(f"sqlite+aiosqlite:///{database}")
    backup = tmp_path / "unversioned.db.incompatible.bak"
    migrations._online_backup(database, backup)
    manifest = migrations._write_backup_manifest(
        database_path=database,
        backup_path=backup,
        source_revision=CURRENT_HEAD_REVISION,
        target_revision=CURRENT_HEAD_REVISION,
        reason="test",
        integrity_verified=True,
    )

    # A current session_input without its idempotency table is not one of the
    # explicitly supported historical unversioned snapshots.
    with sqlite3.connect(backup) as connection:
        connection.execute("DROP TABLE alembic_version")
        connection.execute("DROP TABLE idempotency_record")
    with pytest.raises(RuntimeError, match="explicitly compatible legacy snapshot"):
        migrations._write_backup_manifest(
            database_path=database,
            backup_path=backup,
            source_revision=None,
            target_revision=CURRENT_HEAD_REVISION,
            reason="test-incompatible-legacy",
            integrity_verified=True,
        )
    _rewrite_manifest_for_backup(manifest, backup, source_revision=None)

    records = list_database_backups(f"sqlite+aiosqlite:///{database}")
    assert len(records) == 1
    assert records[0]["verified"] is False
    assert "explicitly compatible legacy snapshot" in str(records[0]["error"])
    with pytest.raises(
        DatabaseRestoreError,
        match="explicitly compatible legacy snapshot",
    ):
        restore_database_backup(f"sqlite+aiosqlite:///{database}", manifest)
    assert _revision(database) == CURRENT_HEAD_REVISION


def test_revision_schema_rejects_text_only_idempotency_table_with_duplicates(
    tmp_path: Path,
) -> None:
    database = tmp_path / "constraints.db"
    upgrade_sqlite_database(f"sqlite+aiosqlite:///{database}")
    backup = tmp_path / "constraints.db.malformed.bak"
    migrations._online_backup(database, backup)
    manifest = migrations._write_backup_manifest(
        database_path=database,
        backup_path=backup,
        source_revision=CURRENT_HEAD_REVISION,
        target_revision=CURRENT_HEAD_REVISION,
        reason="test",
        integrity_verified=True,
    )

    with sqlite3.connect(backup) as connection:
        connection.execute(
            "ALTER TABLE idempotency_record RENAME TO idempotency_record_valid"
        )
        connection.execute(
            """
            CREATE TABLE idempotency_record (
                id TEXT,
                scope TEXT,
                request_key TEXT,
                request_hash TEXT,
                status TEXT,
                response TEXT,
                error_message TEXT,
                time_created TEXT,
                time_updated TEXT
            )
            """
        )
        duplicate = (
            "duplicate-id",
            "session",
            "same-key",
            "hash",
            "pending",
            "{}",
            None,
            "2026-07-14",
            "2026-07-14",
        )
        connection.execute(
            "INSERT INTO idempotency_record VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            duplicate,
        )
        connection.execute(
            "INSERT INTO idempotency_record VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            duplicate,
        )
        connection.execute("DROP TABLE idempotency_record_valid")
    _rewrite_manifest_for_backup(
        manifest,
        backup,
        source_revision=CURRENT_HEAD_REVISION,
    )

    with sqlite3.connect(backup) as connection:
        assert connection.execute(
            "SELECT count(*) FROM idempotency_record "
            "WHERE scope = 'session' AND request_key = 'same-key'"
        ).fetchone() == (2,)
    records = list_database_backups(f"sqlite+aiosqlite:///{database}")
    assert records[0]["verified"] is False
    assert "column type/not-null/default/primary-key" in str(records[0]["error"])
    assert "unique/index definition differs" in str(records[0]["error"])
    with pytest.raises(DatabaseRestoreError, match="schema does not match"):
        restore_database_backup(f"sqlite+aiosqlite:///{database}", manifest)
    assert _revision(database) == CURRENT_HEAD_REVISION


def test_database_lease_blocks_second_app_and_offline_recovery_processes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "leased.db"
    _materialize_v073_database(database)
    migration = upgrade_sqlite_database(f"sqlite+aiosqlite:///{database}")
    assert migration is not None and migration.backup_metadata_path is not None
    database_url = f"sqlite+aiosqlite:///{database}"
    run_py = Path(__file__).resolve().parents[2] / "run.py"
    environment = dict(os.environ)
    backend_root = str(Path(__file__).resolve().parents[2])
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, (backend_root, environment.get("PYTHONPATH", "")))
    )
    startup_probe = (
        "import sys; "
        "from app.storage.migrations import upgrade_sqlite_database; "
        "upgrade_sqlite_database(sys.argv[1])"
    )

    with database_lease(database_url):
        attempted_start = subprocess.run(
            [sys.executable, "-c", startup_probe, database_url],
            capture_output=True,
            text=True,
            timeout=30,
            env=environment,
        )
        attempted_list = subprocess.run(
            [
                sys.executable,
                str(run_py),
                "--database-url",
                database_url,
                "--list-backups",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env=environment,
        )
        attempted_restore = subprocess.run(
            [
                sys.executable,
                str(run_py),
                "--database-url",
                database_url,
                "--restore-backup",
                str(migration.backup_metadata_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env=environment,
        )

    for attempt in (attempted_start, attempted_list, attempted_restore):
        assert attempt.returncode != 0
        assert "already in use by another app or recovery process" in attempt.stderr
    assert _revision(database) == CURRENT_HEAD_REVISION


def test_upgrade_refuses_to_replace_live_database_after_late_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "late-upgrade.db"
    _materialize_v073_database(database)
    real_run_alembic = migrations._run_alembic

    def migrate_then_write_live(
        staging_path: Path,
        *,
        stamp_revision: str | None,
    ) -> None:
        real_run_alembic(staging_path, stamp_revision=stamp_revision)
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE session SET title = 'late-write' WHERE id = 'session-v073'"
            )

    monkeypatch.setattr(migrations, "_run_alembic", migrate_then_write_live)

    with pytest.raises(DatabaseMigrationError, match="changed after the safety snapshot") as exc:
        upgrade_sqlite_database(f"sqlite+aiosqlite:///{database}")

    assert exc.value.backup_path is not None and exc.value.backup_path.is_file()
    assert _revision(database) is None
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT title FROM session WHERE id = 'session-v073'"
        ).fetchone() == ("late-write",)
    with sqlite3.connect(exc.value.backup_path) as connection:
        assert connection.execute(
            "SELECT title FROM session WHERE id = 'session-v073'"
        ).fetchone() == ("需要保留的 v0.7.3 对话",)


def test_restore_refuses_to_replace_live_database_after_late_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "late-restore.db"
    _materialize_v073_database(database)
    migration = upgrade_sqlite_database(f"sqlite+aiosqlite:///{database}")
    assert migration is not None and migration.backup_metadata_path is not None
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE session SET title = 'before-restore' WHERE id = 'session-v073'"
        )
    real_prepare_staging_journal = migrations._prepare_staging_journal

    def prepare_then_write_live(staging_path: Path) -> None:
        real_prepare_staging_journal(staging_path)
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE session SET title = 'late-write' WHERE id = 'session-v073'"
            )

    monkeypatch.setattr(
        migrations,
        "_prepare_staging_journal",
        prepare_then_write_live,
    )

    with pytest.raises(DatabaseRestoreError, match="changed after the safety snapshot"):
        restore_database_backup(
            f"sqlite+aiosqlite:///{database}",
            migration.backup_metadata_path,
        )

    assert _revision(database) == CURRENT_HEAD_REVISION
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT title FROM session WHERE id = 'session-v073'"
        ).fetchone() == ("late-write",)
    safety_backups = list(tmp_path.glob("late-restore.db.pre-restore-*.bak"))
    assert len(safety_backups) == 1
    with sqlite3.connect(safety_backups[0]) as connection:
        assert connection.execute(
            "SELECT title FROM session WHERE id = 'session-v073'"
        ).fetchone() == ("before-restore",)


def test_restore_is_atomic_and_keeps_verified_safety_backup(tmp_path: Path) -> None:
    database = tmp_path / "suxiaoyou.db"
    _materialize_v073_database(database)
    migration = upgrade_sqlite_database(f"sqlite+aiosqlite:///{database}")
    assert migration is not None and migration.backup_metadata_path is not None
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE session SET title = 'changed-after-upgrade' WHERE id = 'session-v073'"
        )

    restored = restore_database_backup(
        f"sqlite+aiosqlite:///{database}",
        migration.backup_metadata_path,
    )

    assert restored.restored_revision is None
    assert restored.safety_backup_path is not None
    assert restored.safety_backup_metadata_path is not None
    assert restored.safety_backup_path.is_file()
    assert restored.safety_backup_metadata_path.is_file()
    assert _revision(restored.safety_backup_path) == CURRENT_HEAD_REVISION
    assert _revision(database) is None
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT title FROM session WHERE id = 'session-v073'"
        ).fetchone() == ("需要保留的 v0.7.3 对话",)


def test_restore_rejects_backup_changed_after_manifest_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "restore-toctou-reject.db"
    _materialize_v073_database(database)
    migration = upgrade_sqlite_database(f"sqlite+aiosqlite:///{database}")
    assert migration is not None and migration.backup_path is not None
    assert migration.backup_metadata_path is not None
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE session SET title = 'live-must-survive' WHERE id = 'session-v073'"
        )
    real_copy = migrations._copy_manifest_backup_to_staging

    def tamper_then_copy(*args, **kwargs) -> None:
        with sqlite3.connect(migration.backup_path) as connection:
            connection.execute(
                "UPDATE session SET title = 'changed-after-validation' "
                "WHERE id = 'session-v073'"
            )
        real_copy(*args, **kwargs)

    monkeypatch.setattr(
        migrations,
        "_copy_manifest_backup_to_staging",
        tamper_then_copy,
    )

    with pytest.raises(DatabaseRestoreError, match="checksum changed before staging"):
        restore_database_backup(
            f"sqlite+aiosqlite:///{database}",
            migration.backup_metadata_path,
        )

    assert _revision(database) == CURRENT_HEAD_REVISION
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT title FROM session WHERE id = 'session-v073'"
        ).fetchone() == ("live-must-survive",)
    assert list(tmp_path.glob("restore-toctou-reject.db.pre-restore-*.bak"))


def test_restore_staging_is_immutable_after_verified_backup_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "restore-toctou-isolated.db"
    _materialize_v073_database(database)
    migration = upgrade_sqlite_database(f"sqlite+aiosqlite:///{database}")
    assert migration is not None and migration.backup_path is not None
    assert migration.backup_metadata_path is not None
    real_copy = migrations._copy_manifest_backup_to_staging

    def copy_then_tamper_source(*args, **kwargs) -> None:
        real_copy(*args, **kwargs)
        with sqlite3.connect(migration.backup_path) as connection:
            connection.execute(
                "UPDATE session SET title = 'source-changed-after-copy' "
                "WHERE id = 'session-v073'"
            )

    monkeypatch.setattr(
        migrations,
        "_copy_manifest_backup_to_staging",
        copy_then_tamper_source,
    )

    restore_database_backup(
        f"sqlite+aiosqlite:///{database}",
        migration.backup_metadata_path,
    )

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT title FROM session WHERE id = 'session-v073'"
        ).fetchone() == ("需要保留的 v0.7.3 对话",)
    with sqlite3.connect(migration.backup_path) as connection:
        assert connection.execute(
            "SELECT title FROM session WHERE id = 'session-v073'"
        ).fetchone() == ("source-changed-after-copy",)


def test_checksum_mismatch_refuses_restore_and_preserves_live_db(tmp_path: Path) -> None:
    database = tmp_path / "suxiaoyou.db"
    _materialize_v073_database(database)
    migration = upgrade_sqlite_database(f"sqlite+aiosqlite:///{database}")
    assert migration is not None and migration.backup_path is not None
    assert migration.backup_metadata_path is not None
    with migration.backup_path.open("ab") as handle:
        handle.write(b"tamper")

    with pytest.raises(DatabaseRestoreError, match="size mismatch|checksum mismatch"):
        restore_database_backup(
            f"sqlite+aiosqlite:///{database}",
            migration.backup_metadata_path,
        )

    assert _revision(database) == CURRENT_HEAD_REVISION
    assert not list(tmp_path.glob("suxiaoyou.db.pre-restore-*.bak"))


def test_offline_recovery_cli_lists_and_restores_without_app_startup(
    tmp_path: Path,
) -> None:
    database = tmp_path / "suxiaoyou.db"
    _materialize_v073_database(database)
    migration = upgrade_sqlite_database(f"sqlite+aiosqlite:///{database}")
    assert migration is not None and migration.backup_metadata_path is not None
    database_url = f"sqlite+aiosqlite:///{database}"
    run_py = Path(__file__).resolve().parents[2] / "run.py"

    listed = subprocess.run(
        [
            sys.executable,
            str(run_py),
            "--database-url",
            database_url,
            "--list-backups",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert json.loads(listed.stdout)["backups"][0]["verified"] is True

    restored = subprocess.run(
        [
            sys.executable,
            str(run_py),
            "--database-url",
            database_url,
            "--restore-backup",
            str(migration.backup_metadata_path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert json.loads(restored.stdout)["status"] == "restored"
    assert _revision(database) is None
