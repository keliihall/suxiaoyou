"""Versioned, failure-safe database upgrades for the desktop SQLite store.

The desktop app has historically created tables directly from SQLAlchemy
metadata.  v0.8.0 is the first release with an explicit Alembic history.  An
unversioned v0.7.3 database is therefore validated, backed up with SQLite's
online-backup API, stamped at the v0.7.3 baseline *in a staging copy*, and then
upgraded.  Only a fully migrated, integrity-checked staging database replaces
the live file.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy.engine import URL, make_url

logger = logging.getLogger(__name__)

V073_BASELINE_REVISION: Final = "0001_v073_baseline"
V080_SESSION_INPUT_REVISION: Final = "0002_v080_session_input"
V080_HEAD_REVISION: Final = "0003_v080_idempotency_record"

# This is the schema shipped by v0.7.3.  It is deliberately independent from
# current ORM metadata: using the current models here would silently bless an
# older or partially upgraded database and recreate the unsafe guess-based
# migration behaviour this module replaces.
V073_REQUIRED_COLUMNS: Final[dict[str, frozenset[str]]] = {
    "project": frozenset(
        {"id", "name", "worktree", "time_created", "time_updated"}
    ),
    "session": frozenset(
        {
            "id",
            "project_id",
            "parent_id",
            "slug",
            "directory",
            "title",
            "version",
            "model_id",
            "provider_id",
            "summary_additions",
            "summary_deletions",
            "summary_files",
            "summary_diffs",
            "is_pinned",
            "permission",
            "time_compacting",
            "time_archived",
            "time_created",
            "time_updated",
        }
    ),
    "message": frozenset(
        {"id", "session_id", "data", "time_created", "time_updated"}
    ),
    "part": frozenset(
        {
            "id",
            "message_id",
            "session_id",
            "data",
            "time_created",
            "time_updated",
        }
    ),
    "todo": frozenset(
        {
            "id",
            "session_id",
            "content",
            "status",
            "active_form",
            "position",
            "time_created",
            "time_updated",
        }
    ),
    "session_file": frozenset(
        {
            "id",
            "session_id",
            "file_path",
            "file_name",
            "tool_id",
            "file_type",
            "time_created",
            "time_updated",
        }
    ),
    "scheduled_task": frozenset(
        {
            "id",
            "name",
            "description",
            "prompt",
            "schedule_config",
            "agent",
            "model",
            "workspace",
            "enabled",
            "template_id",
            "last_run_at",
            "last_run_status",
            "last_session_id",
            "next_run_at",
            "run_count",
            "timeout_seconds",
            "loop_max_iterations",
            "loop_preset",
            "loop_stop_marker",
            "time_created",
            "time_updated",
        }
    ),
    "task_run": frozenset(
        {
            "id",
            "task_id",
            "session_id",
            "status",
            "error_message",
            "started_at",
            "finished_at",
            "triggered_by",
            "time_created",
            "time_updated",
        }
    ),
    "workspace_memory": frozenset(
        {"id", "workspace_path", "content", "time_created", "time_updated"}
    ),
}

V080_SESSION_INPUT_COLUMNS: Final[frozenset[str]] = frozenset(
    {
        "id",
        "session_id",
        "client_request_id",
        "mode",
        "status",
        "position",
        "text",
        "attachments",
        "model_id",
        "provider_id",
        "agent",
        "workspace",
        "reasoning",
        "permission_presets",
        "permission_rules",
        "target_stream_id",
        "applied_stream_id",
        "error_message",
        "time_applied",
        "time_created",
        "time_updated",
    }
)

V080_IDEMPOTENCY_RECORD_COLUMNS: Final[frozenset[str]] = frozenset(
    {
        "id",
        "scope",
        "request_key",
        "request_hash",
        "status",
        "response",
        "error_message",
        "time_created",
        "time_updated",
    }
)


@dataclass(frozen=True, slots=True)
class MigrationResult:
    """Observable result of preparing a file-backed SQLite database."""

    database_path: Path
    previous_revision: str | None
    current_revision: str
    backup_path: Path | None
    upgraded: bool
    created: bool


class DatabaseMigrationError(RuntimeError):
    """Raised when a safe database upgrade could not be completed."""

    def __init__(
        self,
        database_path: Path,
        message: str,
        *,
        backup_path: Path | None = None,
        failed_copy_path: Path | None = None,
    ) -> None:
        details = [f"Database upgrade to v0.8.0 failed for {database_path}.", message]
        if database_path.exists():
            details.append(
                "The original database was left untouched and remains the active database."
            )
        else:
            details.append("No incomplete database was installed.")
        if backup_path is not None:
            details.append(f"Pre-upgrade backup: {backup_path}")
        if failed_copy_path is not None:
            details.append(f"Failed staging copy retained for diagnostics: {failed_copy_path}")
        details.append("Resolve the reported problem and restart; the upgrade is safe to retry.")
        super().__init__(" ".join(details))
        self.database_path = database_path
        self.backup_path = backup_path
        self.failed_copy_path = failed_copy_path


def sqlite_file_path(database_url: str) -> Path | None:
    """Return the resolved SQLite file path, or ``None`` for non-file URLs."""

    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        return None
    database = url.database
    if not database or database == ":memory:":
        return None
    return Path(database).expanduser().resolve()


def upgrade_sqlite_database(database_url: str) -> MigrationResult | None:
    """Prepare a file-backed SQLite database and return its migration result.

    ``None`` means the URL is an in-memory or non-SQLite development database;
    callers may initialise those transient databases from ORM metadata.  Every
    production desktop database is file-backed SQLite and follows the
    versioned path below.
    """

    database_path = sqlite_file_path(database_url)
    if database_path is None:
        return None

    database_path.parent.mkdir(parents=True, exist_ok=True)
    existed = database_path.is_file() and database_path.stat().st_size > 0

    if existed:
        try:
            _quick_check(database_path)
            previous_revision = _read_revision(database_path)
        except Exception as exc:
            raise DatabaseMigrationError(
                database_path,
                f"Pre-upgrade integrity check failed: {exc}",
            ) from exc
        if previous_revision == V080_HEAD_REVISION:
            try:
                _validate_head_database(database_path)
            except Exception as exc:
                raise DatabaseMigrationError(
                    database_path,
                    f"Database claims the v0.8.0 revision but its schema is invalid: {exc}",
                ) from exc
            return MigrationResult(
                database_path=database_path,
                previous_revision=previous_revision,
                current_revision=V080_HEAD_REVISION,
                backup_path=None,
                upgraded=False,
                created=False,
            )
        return _upgrade_existing_database(database_path, previous_revision)

    return _initialize_new_database(database_path)


def _initialize_new_database(database_path: Path) -> MigrationResult:
    token = _migration_token()
    staging_path = database_path.with_name(f".{database_path.name}.{token}.migrating")
    try:
        _run_alembic(staging_path, stamp_revision=None)
        _validate_head_database(staging_path)
        _atomic_install(staging_path, database_path)
    except Exception as exc:
        _remove_sqlite_files(staging_path)
        raise DatabaseMigrationError(
            database_path,
            f"A new database could not be initialized: {exc}",
        ) from exc

    logger.info("Initialized database at Alembic revision %s", V080_HEAD_REVISION)
    return MigrationResult(
        database_path=database_path,
        previous_revision=None,
        current_revision=V080_HEAD_REVISION,
        backup_path=None,
        upgraded=True,
        created=True,
    )


def _upgrade_existing_database(
    database_path: Path,
    previous_revision: str | None,
) -> MigrationResult:
    token = _migration_token()
    backup_path = database_path.with_name(
        f"{database_path.name}.pre-v0.8.0-{token}.bak"
    )
    staging_path = database_path.with_name(f".{database_path.name}.{token}.migrating")
    failed_path = database_path.with_name(f"{database_path.name}.{token}.migration-failed")

    try:
        _online_backup(database_path, backup_path)
        _online_backup(backup_path, staging_path, checkpoint_source=False)
        _prepare_staging_journal(staging_path)

        stamp_revision = previous_revision
        if previous_revision is None:
            _validate_v073_schema(staging_path)
            if _has_valid_v080_session_input(staging_path):
                # Development snapshots briefly created this table with
                # create_all before v0.8.0 acquired a formal migration chain.
                # Validate complete shapes and stamp the newest schema that is
                # actually present; never guess or add individual columns.
                stamp_revision = (
                    V080_HEAD_REVISION
                    if _has_valid_v080_idempotency_record(staging_path)
                    else V080_SESSION_INPUT_REVISION
                )
            else:
                stamp_revision = V073_BASELINE_REVISION

        _run_alembic(staging_path, stamp_revision=stamp_revision)
        _validate_head_database(staging_path)
        _remove_sqlite_sidecars(database_path)
        _atomic_install(staging_path, database_path)
    except Exception as exc:
        retained_failed_path: Path | None = None
        if staging_path.exists():
            try:
                os.replace(staging_path, failed_path)
                for suffix in ("-wal", "-shm", "-journal"):
                    _move_sidecar_if_present(staging_path, failed_path, suffix)
                retained_failed_path = failed_path
            except OSError:
                retained_failed_path = staging_path
        backup = backup_path if backup_path.exists() else None
        raise DatabaseMigrationError(
            database_path,
            str(exc),
            backup_path=backup,
            failed_copy_path=retained_failed_path,
        ) from exc

    logger.info(
        "Upgraded database from %s to %s; backup retained at %s",
        previous_revision or "unversioned-v0.7.3",
        V080_HEAD_REVISION,
        backup_path,
    )
    return MigrationResult(
        database_path=database_path,
        previous_revision=previous_revision,
        current_revision=V080_HEAD_REVISION,
        backup_path=backup_path,
        upgraded=True,
        created=False,
    )


def _resource_root() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root)
    return Path(__file__).resolve().parents[2]


def _alembic_config(connection) -> Config:
    root = _resource_root()
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.attributes["connection"] = connection
    return config


def _run_alembic(database_path: Path, *, stamp_revision: str | None) -> None:
    # Staging databases use DELETE journaling so all committed migration data
    # lives in the main file that will be atomically installed.
    engine = create_sync_engine(URL.create("sqlite", database=str(database_path)))
    try:
        with engine.connect() as connection:
            config = _alembic_config(connection)
            try:
                if stamp_revision is not None:
                    command.stamp(config, stamp_revision)
                command.upgrade(config, "head")
            finally:
                # Alembic's Config is mutable and otherwise retains a strong
                # reference to the supplied SQLAlchemy Connection.  Drop it
                # before the context closes the connection and the engine is
                # disposed, making the handle-release order explicit.
                config.attributes.pop("connection", None)
    finally:
        engine.dispose()


@contextmanager
def _sqlite_connection(
    database_path: Path,
    *,
    timeout: float = 30,
) -> Iterator[sqlite3.Connection]:
    """Yield a transactional SQLite connection and always close its handle.

    ``sqlite3.Connection``'s own context manager commits or rolls back but
    deliberately does *not* close the connection.  Relying on refcounting to
    release that file handle happened to work with Unix rename semantics, but
    fails with ``WinError 32`` when the migrated staging file is replaced.
    """

    connection = sqlite3.connect(database_path, timeout=timeout)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _quick_check(database_path: Path) -> None:
    try:
        with _sqlite_connection(database_path) as connection:
            connection.execute("PRAGMA busy_timeout=30000")
            rows = connection.execute("PRAGMA quick_check").fetchall()
    except sqlite3.Error as exc:
        raise RuntimeError(f"SQLite could not read {database_path}: {exc}") from exc
    results = [str(row[0]) for row in rows]
    if results != ["ok"]:
        raise RuntimeError(
            f"SQLite quick_check failed for {database_path}: {'; '.join(results)}"
        )


def _online_backup(
    source_path: Path,
    destination_path: Path,
    *,
    checkpoint_source: bool = True,
) -> None:
    _remove_sqlite_files(destination_path)
    try:
        with _sqlite_connection(source_path) as source:
            source.execute("PRAGMA busy_timeout=30000")
            if checkpoint_source:
                journal_mode = str(source.execute("PRAGMA journal_mode").fetchone()[0])
                if journal_mode.casefold() == "wal":
                    busy, _log_frames, _checkpointed = source.execute(
                        "PRAGMA wal_checkpoint(TRUNCATE)"
                    ).fetchone()
                    if busy:
                        raise RuntimeError(
                            "SQLite database is busy; close other app instances and retry"
                        )
            _quick_check_connection(source, source_path)
            with _sqlite_connection(destination_path) as destination:
                source.backup(destination)
                destination.commit()
        _quick_check(destination_path)
    except Exception:
        _remove_sqlite_files(destination_path)
        raise


def _quick_check_connection(connection: sqlite3.Connection, path: Path) -> None:
    results = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]
    if results != ["ok"]:
        raise RuntimeError(f"SQLite quick_check failed for {path}: {'; '.join(results)}")


def _prepare_staging_journal(database_path: Path) -> None:
    with _sqlite_connection(database_path) as connection:
        mode = str(connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0])
        if mode.casefold() != "delete":
            raise RuntimeError(
                f"Could not put staging database in DELETE journal mode (got {mode})"
            )


def _read_revision(database_path: Path) -> str | None:
    with _sqlite_connection(database_path) as connection:
        tables = _table_names(connection)
        if "alembic_version" not in tables:
            return None
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        return str(row[0]) if row and row[0] else None


def _validate_v073_schema(database_path: Path) -> None:
    with _sqlite_connection(database_path) as connection:
        tables = _table_names(connection)
        missing_tables = sorted(set(V073_REQUIRED_COLUMNS) - tables)
        missing_columns: list[str] = []
        for table, required in V073_REQUIRED_COLUMNS.items():
            if table not in tables:
                continue
            actual = {
                str(row[1])
                for row in connection.execute(f'PRAGMA table_info("{table}")')
            }
            for column in sorted(required - actual):
                missing_columns.append(f"{table}.{column}")
    if missing_tables or missing_columns:
        problems: list[str] = []
        if missing_tables:
            problems.append("missing tables: " + ", ".join(missing_tables))
        if missing_columns:
            problems.append("missing columns: " + ", ".join(missing_columns))
        raise RuntimeError(
            "Unversioned database does not match the supported v0.7.3 baseline ("
            + "; ".join(problems)
            + "). No guessed schema changes were applied."
        )


def _has_valid_v080_session_input(database_path: Path) -> bool:
    with _sqlite_connection(database_path) as connection:
        if "session_input" not in _table_names(connection):
            return False
        columns = {
            str(row[1])
            for row in connection.execute('PRAGMA table_info("session_input")')
        }
    if columns != V080_SESSION_INPUT_COLUMNS:
        missing = sorted(V080_SESSION_INPUT_COLUMNS - columns)
        extra = sorted(columns - V080_SESSION_INPUT_COLUMNS)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if extra:
            details.append("unexpected " + ", ".join(extra))
        raise RuntimeError(
            "Existing session_input table does not match v0.8.0 ("
            + "; ".join(details)
            + ")"
        )
    return True


def _has_valid_v080_idempotency_record(database_path: Path) -> bool:
    with _sqlite_connection(database_path) as connection:
        if "idempotency_record" not in _table_names(connection):
            return False
        columns = {
            str(row[1])
            for row in connection.execute(
                'PRAGMA table_info("idempotency_record")'
            )
        }
    if columns != V080_IDEMPOTENCY_RECORD_COLUMNS:
        raise RuntimeError("Existing idempotency_record table does not match v0.8.0")
    return True


def _validate_head_database(database_path: Path) -> None:
    _quick_check(database_path)
    revision = _read_revision(database_path)
    if revision != V080_HEAD_REVISION:
        raise RuntimeError(
            f"Migration ended at unexpected revision {revision!r}; "
            f"expected {V080_HEAD_REVISION!r}"
        )
    _validate_v073_schema(database_path)
    if not _has_valid_v080_session_input(database_path):
        raise RuntimeError("Migration did not create the session_input table")
    if not _has_valid_v080_idempotency_record(database_path):
        raise RuntimeError("Migration did not create the idempotency_record table")


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _atomic_install(staging_path: Path, database_path: Path) -> None:
    _remove_sqlite_sidecars(staging_path)
    os.replace(staging_path, database_path)
    try:
        directory_fd = os.open(database_path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    except OSError:
        # The replacement has already succeeded and cannot be rolled back by
        # reporting an error here.  Some Windows/network filesystems do not
        # support directory fsync; treat that durability enhancement as best
        # effort rather than falsely claiming the live database was untouched.
        logger.warning(
            "Directory fsync is unavailable after installing migrated database %s",
            database_path,
        )
    finally:
        os.close(directory_fd)


def _remove_sqlite_files(database_path: Path) -> None:
    for path in (
        database_path,
        database_path.with_name(database_path.name + "-wal"),
        database_path.with_name(database_path.name + "-shm"),
        database_path.with_name(database_path.name + "-journal"),
    ):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _remove_sqlite_sidecars(database_path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            database_path.with_name(database_path.name + suffix).unlink()
        except FileNotFoundError:
            pass


def _move_sidecar_if_present(source: Path, destination: Path, suffix: str) -> None:
    source_sidecar = source.with_name(source.name + suffix)
    if source_sidecar.exists():
        os.replace(
            source_sidecar,
            destination.with_name(destination.name + suffix),
        )


def _migration_token() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
