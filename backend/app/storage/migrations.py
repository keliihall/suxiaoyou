"""Versioned, failure-safe database upgrades for the desktop SQLite store.

The desktop app has historically created tables directly from SQLAlchemy
metadata.  v0.8.0 is the first release with an explicit Alembic history.  An
unversioned v0.7.3 database is therefore validated, backed up with SQLite's
online-backup API, stamped at the v0.7.3 baseline *in a staging copy*, and then
upgraded.  Only a fully migrated, integrity-checked staging database replaces
the live file.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Final

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy.engine import URL, make_url

from app.utils.atomic_write import atomic_write_text
from app.version import APP_VERSION

logger = logging.getLogger(__name__)

V073_BASELINE_REVISION: Final = "0001_v073_baseline"
V080_SESSION_INPUT_REVISION: Final = "0002_v080_session_input"
V080_IDEMPOTENCY_REVISION: Final = "0003_v080_idempotency_record"
V083_SESSION_INPUT_LANGUAGE_REVISION: Final = "0004_v083_session_input_language"
V090_RELEASE_BOUNDARY_REVISION: Final = "0005_v090_release_boundary"
V100_SECURITY_AUDIT_REVISION: Final = "0006_v100_security_audit"
V100_INVOCATION_SOURCE_REVISION: Final = "0007_v100_invocation_source"
V100_SESSION_GOAL_REVISION: Final = "0008_v100_session_goal"
V100_GOAL_USAGE_LEDGER_REVISION: Final = "0009_v100_goal_usage_ledger"
V110_CHECKPOINT_LEDGER_REVISION: Final = "0010_v110_checkpoint_ledger"
V110_USER_OFFICE_TEMPLATES_REVISION: Final = "0011_v110_user_office_templates"
CURRENT_HEAD_REVISION: Final = V110_USER_OFFICE_TEMPLATES_REVISION
# Backward-compatible import for older callers/tests. New code must use the
# release-neutral CURRENT_HEAD_REVISION name.
V080_HEAD_REVISION: Final = CURRENT_HEAD_REVISION
SUPPORTED_REVISIONS: Final[frozenset[str]] = frozenset(
    {
        V073_BASELINE_REVISION,
        V080_SESSION_INPUT_REVISION,
        V080_IDEMPOTENCY_REVISION,
        V083_SESSION_INPUT_LANGUAGE_REVISION,
        V090_RELEASE_BOUNDARY_REVISION,
        V100_SECURITY_AUDIT_REVISION,
        V100_INVOCATION_SOURCE_REVISION,
        V100_SESSION_GOAL_REVISION,
        V100_GOAL_USAGE_LEDGER_REVISION,
        V110_CHECKPOINT_LEDGER_REVISION,
        CURRENT_HEAD_REVISION,
    }
)
BACKUP_MANIFEST_VERSION: Final = 1
BACKUP_APP_VERSION: Final = APP_VERSION
SUPPORTED_BACKUP_APP_VERSIONS: Final[frozenset[str]] = frozenset(
    {"1.0.0", BACKUP_APP_VERSION}
)

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
        "language",
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

V080_SESSION_INPUT_COLUMNS_BEFORE_LANGUAGE: Final[frozenset[str]] = (
    V080_SESSION_INPUT_COLUMNS - {"language"}
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
    backup_metadata_path: Path | None
    upgraded: bool
    created: bool


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """Result of atomically replacing the live DB from a verified backup."""

    database_path: Path
    restored_backup_path: Path
    restored_revision: str | None
    safety_backup_path: Path | None
    safety_backup_metadata_path: Path | None


@dataclass(frozen=True, slots=True)
class _ColumnSchema:
    name: str
    data_type: str
    not_null: bool
    default: str | None
    primary_key_position: int


@dataclass(frozen=True, slots=True)
class _ForeignKeySchema:
    identifier: int
    sequence: int
    referenced_table: str
    source_column: str
    referenced_column: str
    on_update: str
    on_delete: str
    match: str


@dataclass(frozen=True, slots=True)
class _IndexSchema:
    # SQLite-generated PK/UNIQUE names are ordinal implementation details;
    # explicit critical index names remain part of the contract.
    name: str | None
    unique: bool
    origin: str
    partial: bool
    columns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _TableSchema:
    columns: tuple[_ColumnSchema, ...]
    foreign_keys: frozenset[_ForeignKeySchema]
    indexes: frozenset[_IndexSchema]


_CANONICAL_SCHEMA_CACHE: dict[str, dict[str, _TableSchema]] = {}


class DatabaseMigrationError(RuntimeError):
    """Raised when a safe database upgrade could not be completed."""

    def __init__(
        self,
        database_path: Path,
        message: str,
        *,
        backup_path: Path | None = None,
        backup_metadata_path: Path | None = None,
        failed_copy_path: Path | None = None,
    ) -> None:
        details = [f"Database upgrade failed for {database_path}.", message]
        if database_path.exists():
            details.append(
                "The original database was left untouched and remains the active database."
            )
        else:
            details.append("No incomplete database was installed.")
        if backup_path is not None:
            details.append(f"Pre-upgrade backup: {backup_path}")
        if backup_metadata_path is not None:
            details.append(f"Verified backup manifest: {backup_metadata_path}")
            details.append(
                "Offline restore command: "
                f"suxiaoyou-backend --database-url "
                f"'sqlite+aiosqlite:///{database_path}' --restore-backup "
                f"'{backup_metadata_path}'"
            )
        if failed_copy_path is not None:
            details.append(f"Failed staging copy retained for diagnostics: {failed_copy_path}")
        details.append("Resolve the reported problem and restart; the upgrade is safe to retry.")
        super().__init__(" ".join(details))
        self.database_path = database_path
        self.backup_path = backup_path
        self.backup_metadata_path = backup_metadata_path
        self.failed_copy_path = failed_copy_path


class DatabaseRestoreError(RuntimeError):
    """Raised when an offline restore cannot be completed safely."""


class DatabaseLeaseError(RuntimeError):
    """Raised when another cooperating app process owns the database."""


@dataclass(slots=True)
class DatabaseLease:
    """Cross-process exclusive lease held for a database lifecycle."""

    database_path: Path
    lock_path: Path
    _handle: BinaryIO | None

    @property
    def active(self) -> bool:
        return self._handle is not None

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _acquire_database_lease(database_path: Path) -> DatabaseLease:
    database_path = database_path.resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = database_path.with_name(f".{database_path.name}.lease")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    os.set_inheritable(descriptor, False)
    handle = os.fdopen(descriptor, "r+b", buffering=0)
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError) as exc:
        handle.close()
        raise DatabaseLeaseError(
            f"Database is already in use by another app or recovery process: "
            f"{database_path}. Close it and retry."
        ) from exc

    return DatabaseLease(
        database_path=database_path,
        lock_path=lock_path,
        _handle=handle,
    )


@contextmanager
def database_lease(database_url: str) -> Iterator[DatabaseLease | None]:
    """Acquire the exclusive database lease, or yield ``None`` for non-files."""

    database_path = sqlite_file_path(database_url)
    if database_path is None:
        yield None
        return
    lease = _acquire_database_lease(database_path)
    try:
        yield lease
    finally:
        lease.release()


def _require_database_lease(
    database_path: Path,
    lease: DatabaseLease | None,
) -> None:
    if (
        lease is None
        or not lease.active
        or lease.database_path != database_path.resolve()
    ):
        raise DatabaseLeaseError(
            f"An active exclusive lease is required for database {database_path}"
        )


def list_database_backups(
    database_url: str,
    *,
    lease: DatabaseLease | None = None,
) -> list[dict[str, object]]:
    """List backup manifests and report their current verification status."""

    database_path = sqlite_file_path(database_url)
    if database_path is None:
        raise DatabaseRestoreError("Backup listing requires a file-backed SQLite URL")
    if lease is None:
        with database_lease(database_url) as owned_lease:
            return _list_database_backups_locked(database_path, owned_lease)
    return _list_database_backups_locked(database_path, lease)


def _list_database_backups_locked(
    database_path: Path,
    lease: DatabaseLease | None,
) -> list[dict[str, object]]:
    _require_database_lease(database_path, lease)

    records: list[dict[str, object]] = []
    pattern = f"{database_path.name}.*.bak.json"
    for manifest_path in sorted(database_path.parent.glob(pattern), reverse=True):
        record: dict[str, object] = {"manifest_path": str(manifest_path)}
        try:
            metadata, backup_path = _load_and_verify_backup_manifest(manifest_path)
            if metadata["database_name"] != database_path.name:
                raise DatabaseRestoreError("manifest belongs to a different database")
            record.update(metadata)
            record["backup_path"] = str(backup_path)
            record["verified"] = True
        except Exception as exc:
            record["verified"] = False
            record["error"] = str(exc)
        records.append(record)
    return records


def restore_database_backup(
    database_url: str,
    backup_or_manifest: str | Path,
    *,
    lease: DatabaseLease | None = None,
) -> RestoreResult:
    """Atomically restore a checksum-verified backup with a safety snapshot.

    This function has no dependency on application startup or ORM model
    initialization, so the backend CLI can run it even when normal migration
    fails. The selected backup is copied to a staging database and validated;
    the live database is never migrated or downgraded in place.
    """

    database_path = sqlite_file_path(database_url)
    if database_path is None:
        raise DatabaseRestoreError("Restore requires a file-backed SQLite URL")
    if lease is None:
        with database_lease(database_url) as owned_lease:
            return _restore_database_backup_locked(
                database_path,
                backup_or_manifest,
                owned_lease,
            )
    return _restore_database_backup_locked(database_path, backup_or_manifest, lease)


def _restore_database_backup_locked(
    database_path: Path,
    backup_or_manifest: str | Path,
    lease: DatabaseLease | None,
) -> RestoreResult:
    _require_database_lease(database_path, lease)
    source = Path(backup_or_manifest).expanduser().resolve()
    manifest_path = source if source.name.endswith(".json") else Path(f"{source}.json")
    metadata, backup_path = _load_and_verify_backup_manifest(manifest_path)
    if metadata["database_name"] != database_path.name:
        raise DatabaseRestoreError(
            f"Backup is for {metadata['database_name']!r}, not {database_path.name!r}"
        )

    restored_revision = metadata.get("source_revision")
    if restored_revision is not None and restored_revision not in SUPPORTED_REVISIONS:
        raise DatabaseRestoreError(
            f"Backup revision {restored_revision!r} is not supported by this build"
        )

    database_path.parent.mkdir(parents=True, exist_ok=True)
    token = _migration_token()
    staging_path = database_path.with_name(f".{database_path.name}.{token}.restoring")
    safety_backup_path: Path | None = None
    safety_manifest_path: Path | None = None
    safety_snapshot_is_sqlite_backup = False
    live_file_state: tuple[tuple[str, int, str], ...] | None = None

    if database_path.is_file():
        safety_backup_path = database_path.with_name(
            f"{database_path.name}.pre-restore-{token}.bak"
        )
        safety_revision: str | None
        integrity_verified = True
        try:
            safety_revision = _read_revision(database_path)
        except Exception:
            safety_revision = None
        try:
            _online_backup(database_path, safety_backup_path)
            safety_snapshot_is_sqlite_backup = True
            live_file_state = _capture_sqlite_file_state(database_path)
            try:
                _validate_revision_schema(safety_backup_path, safety_revision)
            except Exception as exc:
                # Recovery must remain possible when the live file is readable
                # but has an unsupported shape. Retain it, but never advertise
                # that safety copy as an automatically restorable backup.
                logger.warning(
                    "Live database safety snapshot has an unverified schema: %s",
                    exc,
                )
                integrity_verified = False
        except Exception as exc:
            # A corrupt live DB is exactly when offline recovery matters most.
            # Preserve its bytes for forensic/manual recovery, but mark that
            # snapshot unverified so it can never be selected automatically.
            logger.warning(
                "Live database could not be integrity-backed-up before restore; "
                "retaining an unverified byte snapshot: %s",
                exc,
            )
            _remove_sqlite_files(safety_backup_path)
            live_file_state_before_copy = _capture_sqlite_file_state(database_path)
            shutil.copy2(database_path, safety_backup_path)
            live_file_state = _capture_sqlite_file_state(database_path)
            if live_file_state != live_file_state_before_copy:
                raise DatabaseRestoreError(
                    "Live database files changed while the recovery snapshot was copied; "
                    "close other app instances and retry"
                )
            integrity_verified = False
        safety_manifest_path = _write_backup_manifest(
            database_path=database_path,
            backup_path=safety_backup_path,
            source_revision=safety_revision,
            target_revision=(
                restored_revision if isinstance(restored_revision, str) else None
            ),
            reason="pre-restore",
            integrity_verified=integrity_verified,
        )

    try:
        _copy_manifest_backup_to_staging(
            backup_path,
            staging_path,
            expected_size=int(metadata["size"]),
            expected_sha256=str(metadata["sha256"]),
        )
        _prepare_staging_journal(staging_path)
        _validate_revision_schema(staging_path, restored_revision)
        if safety_backup_path is not None:
            if live_file_state is None:
                raise RuntimeError("Live database safety state was not recorded")
            _assert_sqlite_file_state(database_path, live_file_state)
            if safety_snapshot_is_sqlite_backup:
                _assert_live_matches_snapshot(database_path, safety_backup_path)
        else:
            _assert_sqlite_files_absent(database_path)
        _assert_sqlite_sidecars_absent(database_path)
        _atomic_install(staging_path, database_path)
    except Exception as exc:
        try:
            _remove_sqlite_files(staging_path)
        except OSError:
            pass
        raise DatabaseRestoreError(
            f"Restore failed before the verified staging database was installed: {exc}. "
            f"Live database: {database_path}. Safety backup: {safety_backup_path}."
        ) from exc

    return RestoreResult(
        database_path=database_path,
        restored_backup_path=backup_path,
        restored_revision=(restored_revision if isinstance(restored_revision, str) else None),
        safety_backup_path=safety_backup_path,
        safety_backup_metadata_path=safety_manifest_path,
    )


def _write_backup_manifest(
    *,
    database_path: Path,
    backup_path: Path,
    source_revision: str | None,
    target_revision: str | None,
    reason: str,
    integrity_verified: bool,
) -> Path:
    if integrity_verified:
        _validate_revision_schema(backup_path, source_revision)
    manifest_path = Path(f"{backup_path}.json")
    payload = {
        "version": BACKUP_MANIFEST_VERSION,
        "app_version": BACKUP_APP_VERSION,
        "database_name": database_path.name,
        "database_path": str(database_path.resolve()),
        "backup_file": backup_path.name,
        "source_revision": source_revision,
        "target_revision": target_revision,
        "reason": reason,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "size": backup_path.stat().st_size,
        "sha256": _sha256_file(backup_path),
        "integrity_verified": integrity_verified,
    }
    atomic_write_text(
        manifest_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        mode=0o600,
    )
    return manifest_path


def _load_and_verify_backup_manifest(
    manifest_path: Path,
) -> tuple[dict[str, object], Path]:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DatabaseRestoreError(f"Cannot read backup manifest {manifest_path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("version") != BACKUP_MANIFEST_VERSION:
        raise DatabaseRestoreError(f"Unsupported backup manifest format: {manifest_path}")
    required_strings = (
        "app_version",
        "database_name",
        "database_path",
        "backup_file",
        "reason",
        "created_at",
        "sha256",
    )
    if any(not isinstance(payload.get(key), str) or not payload[key] for key in required_strings):
        raise DatabaseRestoreError(f"Incomplete backup manifest: {manifest_path}")
    if payload.get("app_version") not in SUPPORTED_BACKUP_APP_VERSIONS:
        raise DatabaseRestoreError(
            f"Backup manifest was created by unsupported app version "
            f"{payload.get('app_version')!r}"
        )
    backup_name = str(payload["backup_file"])
    if Path(backup_name).name != backup_name:
        raise DatabaseRestoreError("Backup manifest contains an unsafe backup path")
    backup_path = manifest_path.parent / backup_name
    if not backup_path.is_file():
        raise DatabaseRestoreError(f"Backup file is missing: {backup_path}")
    if payload.get("integrity_verified") is not True:
        raise DatabaseRestoreError("Backup was not integrity-verified when it was created")
    if payload.get("size") != backup_path.stat().st_size:
        raise DatabaseRestoreError(f"Backup size mismatch: {backup_path}")
    actual_checksum = _sha256_file(backup_path)
    if payload.get("sha256") != actual_checksum:
        raise DatabaseRestoreError(f"Backup checksum mismatch: {backup_path}")
    source_revision = payload.get("source_revision")
    if source_revision is not None and not isinstance(source_revision, str):
        raise DatabaseRestoreError("Backup source revision has an invalid type")
    try:
        _validate_revision_schema(backup_path, source_revision)
    except Exception as exc:
        raise DatabaseRestoreError(
            f"Backup schema does not match its manifest revision: {exc}"
        ) from exc
    return dict(payload), backup_path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_manifest_backup_to_staging(
    backup_path: Path,
    staging_path: Path,
    *,
    expected_size: int,
    expected_sha256: str,
) -> None:
    """Build staging from one verified byte stream of the selected backup.

    The bytes written to staging are the same bytes fed to the manifest hash.
    A change before or during this read fails closed; changes after the read
    cannot affect the private staging file that may later be installed.
    """

    _remove_sqlite_files(staging_path)
    digest = hashlib.sha256()
    copied_size = 0
    try:
        with backup_path.open("rb") as source, staging_path.open("xb") as destination:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                destination.write(chunk)
                digest.update(chunk)
                copied_size += len(chunk)
            destination.flush()
            os.fsync(destination.fileno())
        if copied_size != expected_size:
            raise DatabaseRestoreError(
                f"Backup size changed before staging: {backup_path}"
            )
        if digest.hexdigest() != expected_sha256:
            raise DatabaseRestoreError(
                f"Backup checksum changed before staging: {backup_path}"
            )
        _quick_check(staging_path)
    except Exception:
        _remove_sqlite_files(staging_path)
        raise


def _capture_sqlite_file_state(
    database_path: Path,
) -> tuple[tuple[str, int, str], ...]:
    """Hash the live SQLite file set, rejecting a file that changes mid-read."""

    state: list[tuple[str, int, str]] = []
    for suffix in ("", "-wal", "-shm", "-journal"):
        path = database_path.with_name(database_path.name + suffix)
        try:
            before = path.stat()
        except FileNotFoundError:
            continue
        try:
            checksum = _sha256_file(path)
            after = path.stat()
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Live database file changed while it was verified: {path}"
            ) from exc
        stable_fields_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        stable_fields_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if stable_fields_before != stable_fields_after:
            raise RuntimeError(
                f"Live database file changed while it was verified: {path}"
            )
        state.append((suffix, after.st_size, checksum))
    return tuple(state)


def _assert_sqlite_file_state(
    database_path: Path,
    expected: tuple[tuple[str, int, str], ...],
) -> None:
    if _capture_sqlite_file_state(database_path) != expected:
        raise RuntimeError(
            "Live database files changed after the safety snapshot; "
            "close other app instances and retry"
        )


def _assert_sqlite_files_absent(database_path: Path) -> None:
    if _capture_sqlite_file_state(database_path):
        raise RuntimeError(
            "A live database was created while staging was prepared; "
            "close other app instances and retry"
        )


def _assert_sqlite_sidecars_absent(database_path: Path) -> None:
    existing = [
        database_path.with_name(database_path.name + suffix)
        for suffix in ("-wal", "-shm", "-journal")
        if database_path.with_name(database_path.name + suffix).exists()
    ]
    if existing:
        raise RuntimeError(
            "Live database sidecars are still active before replacement; "
            "close other app instances and retry"
        )


def _assert_live_matches_snapshot(database_path: Path, snapshot_path: Path) -> None:
    """Compare two SQLite-consistent snapshots immediately before replace."""

    verification_path = database_path.with_name(
        f".{database_path.name}.{_migration_token()}.live-check"
    )
    try:
        _online_backup(database_path, verification_path)
        if (
            verification_path.stat().st_size != snapshot_path.stat().st_size
            or _sha256_file(verification_path) != _sha256_file(snapshot_path)
        ):
            raise RuntimeError(
                "Live database changed after the safety snapshot; "
                "close other app instances and retry"
            )
    finally:
        _remove_sqlite_files(verification_path)


def sqlite_file_path(database_url: str) -> Path | None:
    """Return the resolved SQLite file path, or ``None`` for non-file URLs."""

    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        return None
    database = url.database
    if not database or database == ":memory:":
        return None
    return Path(database).expanduser().resolve()


def upgrade_sqlite_database(
    database_url: str,
    *,
    lease: DatabaseLease | None = None,
) -> MigrationResult | None:
    """Prepare a file-backed SQLite database and return its migration result.

    ``None`` means the URL is an in-memory or non-SQLite development database;
    callers may initialise those transient databases from ORM metadata.  Every
    production desktop database is file-backed SQLite and follows the
    versioned path below.
    """

    database_path = sqlite_file_path(database_url)
    if database_path is None:
        return None
    if lease is None:
        with database_lease(database_url) as owned_lease:
            return _upgrade_sqlite_database_locked(database_path, owned_lease)
    return _upgrade_sqlite_database_locked(database_path, lease)


def _upgrade_sqlite_database_locked(
    database_path: Path,
    lease: DatabaseLease | None,
) -> MigrationResult:
    _require_database_lease(database_path, lease)

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
        if previous_revision == CURRENT_HEAD_REVISION:
            try:
                _validate_head_database(database_path)
            except Exception as exc:
                raise DatabaseMigrationError(
                    database_path,
                    f"Database claims the current revision but its schema is invalid: {exc}",
                ) from exc
            return MigrationResult(
                database_path=database_path,
                previous_revision=previous_revision,
                current_revision=CURRENT_HEAD_REVISION,
                backup_path=None,
                backup_metadata_path=None,
                upgraded=False,
                created=False,
            )
        if previous_revision is not None and previous_revision not in SUPPORTED_REVISIONS:
            raise DatabaseMigrationError(
                database_path,
                "The database revision "
                f"{previous_revision!r} is unsupported or was created by a newer build. "
                "This build will not modify or downgrade it.",
            )
        return _upgrade_existing_database(database_path, previous_revision)

    return _initialize_new_database(database_path)


def _initialize_new_database(database_path: Path) -> MigrationResult:
    token = _migration_token()
    staging_path = database_path.with_name(f".{database_path.name}.{token}.migrating")
    try:
        _run_alembic(staging_path, stamp_revision=None)
        _validate_head_database(staging_path)
        _assert_sqlite_files_absent(database_path)
        _atomic_install(staging_path, database_path)
    except Exception as exc:
        cleanup_failure: OSError | None = None
        retained_failed_path: Path | None = None
        try:
            _remove_sqlite_files(staging_path)
        except OSError as cleanup_exc:
            cleanup_failure = cleanup_exc
            if staging_path.exists():
                retained_failed_path = staging_path
            logger.warning(
                "Could not remove failed new-database staging files at %s: %s",
                staging_path,
                cleanup_exc,
                exc_info=True,
            )
        failure_message = f"A new database could not be initialized: {exc}"
        if cleanup_failure is not None:
            failure_message += (
                " Cleanup of the failed staging database also failed: "
                f"{cleanup_failure}."
            )
        raise DatabaseMigrationError(
            database_path,
            failure_message,
            failed_copy_path=retained_failed_path,
        ) from exc

    logger.info("Initialized database at Alembic revision %s", CURRENT_HEAD_REVISION)
    return MigrationResult(
        database_path=database_path,
        previous_revision=None,
        current_revision=CURRENT_HEAD_REVISION,
        backup_path=None,
        backup_metadata_path=None,
        upgraded=True,
        created=True,
    )


def _upgrade_existing_database(
    database_path: Path,
    previous_revision: str | None,
) -> MigrationResult:
    token = _migration_token()
    backup_path = database_path.with_name(
        f"{database_path.name}.pre-v{BACKUP_APP_VERSION}-{token}.bak"
    )
    backup_metadata_path: Path | None = None
    staging_path = database_path.with_name(f".{database_path.name}.{token}.migrating")
    failed_path = database_path.with_name(f"{database_path.name}.{token}.migration-failed")

    try:
        _online_backup(database_path, backup_path)
        live_file_state = _capture_sqlite_file_state(database_path)
        backup_metadata_path = _write_backup_manifest(
            database_path=database_path,
            backup_path=backup_path,
            source_revision=previous_revision,
            target_revision=CURRENT_HEAD_REVISION,
            reason="pre-upgrade",
            integrity_verified=True,
        )
        backup_metadata, verified_backup_path = _load_and_verify_backup_manifest(
            backup_metadata_path
        )
        _copy_manifest_backup_to_staging(
            verified_backup_path,
            staging_path,
            expected_size=int(backup_metadata["size"]),
            expected_sha256=str(backup_metadata["sha256"]),
        )
        _prepare_staging_journal(staging_path)

        stamp_revision = _validate_revision_schema(staging_path, previous_revision)

        _run_alembic(staging_path, stamp_revision=stamp_revision)
        _validate_head_database(staging_path)
        _assert_sqlite_file_state(database_path, live_file_state)
        _assert_live_matches_snapshot(database_path, backup_path)
        _assert_sqlite_sidecars_absent(database_path)
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
            backup_metadata_path=(
                backup_metadata_path
                if backup_metadata_path is not None and backup_metadata_path.exists()
                else None
            ),
            failed_copy_path=retained_failed_path,
        ) from exc

    logger.info(
        "Upgraded database from %s to %s; backup retained at %s",
        previous_revision or "unversioned-v0.7.3",
        CURRENT_HEAD_REVISION,
        backup_path,
    )
    return MigrationResult(
        database_path=database_path,
        previous_revision=previous_revision,
        current_revision=CURRENT_HEAD_REVISION,
        backup_path=backup_path,
        backup_metadata_path=backup_metadata_path,
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
    _run_alembic_revision(
        database_path,
        stamp_revision=stamp_revision,
        target_revision="head",
    )


def _run_alembic_revision(
    database_path: Path,
    *,
    stamp_revision: str | None,
    target_revision: str,
) -> None:
    # Staging databases use DELETE journaling so all committed migration data
    # lives in the main file that will be atomically installed.
    engine = create_sync_engine(URL.create("sqlite", database=str(database_path)))
    try:
        # One explicit outer transaction ensures Alembic's revision-table
        # update is committed even when a SQLite migration consists only of
        # ALTER TABLE. DDL may persist independently while the version row is
        # otherwise rolled back as the connection closes.
        with engine.begin() as connection:
            config = _alembic_config(connection)
            try:
                if stamp_revision is not None:
                    command.stamp(config, stamp_revision)
                command.upgrade(config, target_revision)
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


def _database_schema(
    connection: sqlite3.Connection,
) -> dict[str, _TableSchema]:
    schema: dict[str, _TableSchema] = {}
    for table in _table_names(connection):
        columns = tuple(
            _ColumnSchema(
                name=str(row[1]),
                data_type=" ".join(str(row[2]).upper().split()),
                not_null=bool(row[3]),
                default=(str(row[4]).strip() if row[4] is not None else None),
                primary_key_position=int(row[5]),
            )
            for row in connection.execute(f'PRAGMA table_info("{table}")')
        )
        foreign_keys = frozenset(
            _ForeignKeySchema(
                identifier=int(row[0]),
                sequence=int(row[1]),
                referenced_table=str(row[2]),
                source_column=str(row[3]),
                referenced_column=str(row[4] or ""),
                on_update=str(row[5]),
                on_delete=str(row[6]),
                match=str(row[7]),
            )
            for row in connection.execute(f'PRAGMA foreign_key_list("{table}")')
        )
        indexes: set[_IndexSchema] = set()
        for row in connection.execute(f'PRAGMA index_list("{table}")').fetchall():
            index_name = str(row[1])
            origin = str(row[3])
            index_columns = tuple(
                str(column_row[2] or "<expression>")
                for column_row in connection.execute(
                    f'PRAGMA index_info("{index_name}")'
                )
            )
            indexes.add(
                _IndexSchema(
                    name=index_name if origin == "c" else None,
                    unique=bool(row[2]),
                    origin=origin,
                    partial=bool(row[4]),
                    columns=index_columns,
                )
            )
        schema[table] = _TableSchema(
            columns=columns,
            foreign_keys=foreign_keys,
            indexes=frozenset(indexes),
        )
    return schema


def _canonical_schema(revision: str) -> dict[str, _TableSchema]:
    cached = _CANONICAL_SCHEMA_CACHE.get(revision)
    if cached is not None:
        return cached
    with tempfile.TemporaryDirectory(prefix="suxiaoyou-schema-") as directory:
        database_path = Path(directory) / "canonical.db"
        _run_alembic_revision(
            database_path,
            stamp_revision=None,
            target_revision=revision,
        )
        with _sqlite_connection(database_path) as connection:
            canonical = _database_schema(connection)
    _CANONICAL_SCHEMA_CACHE[revision] = canonical
    return canonical


def _unversioned_schema(revision: str) -> dict[str, _TableSchema]:
    return {
        table: schema
        for table, schema in _canonical_schema(revision).items()
        if table != "alembic_version"
    }


def _schema_mismatch_details(
    actual: dict[str, _TableSchema],
    expected: dict[str, _TableSchema],
) -> str:
    problems: list[str] = []
    missing_tables = sorted(set(expected) - set(actual))
    unexpected_tables = sorted(set(actual) - set(expected))
    if missing_tables:
        problems.append("missing tables: " + ", ".join(missing_tables))
    if unexpected_tables:
        problems.append("unexpected tables: " + ", ".join(unexpected_tables))
    for table in sorted(set(actual) & set(expected)):
        actual_table = actual[table]
        expected_table = expected[table]
        if actual_table.columns != expected_table.columns:
            problems.append(
                f"{table} column type/not-null/default/primary-key definition differs"
            )
        if actual_table.foreign_keys != expected_table.foreign_keys:
            problems.append(f"{table} foreign-key definition differs")
        if actual_table.indexes != expected_table.indexes:
            problems.append(f"{table} unique/index definition differs")
    return "; ".join(problems) or "unknown schema mismatch"


def _validate_revision_schema(
    database_path: Path,
    claimed_revision: str | None,
) -> str:
    """Validate a revision claim against one exact, supported schema shape.

    The returned revision is the safe Alembic stamp for a compatible legacy
    unversioned database. Known revisions are returned unchanged. This is the
    only place where an unversioned shape is classified, so backup creation,
    backup listing, restore, and upgrade cannot drift into separate guesses.
    """

    if claimed_revision is not None and claimed_revision not in SUPPORTED_REVISIONS:
        raise RuntimeError(f"Unsupported database revision {claimed_revision!r}")

    with _sqlite_connection(database_path) as connection:
        connection.execute("PRAGMA busy_timeout=30000")
        _quick_check_connection(connection, database_path)
        actual = _database_schema(connection)
        if "alembic_version" in actual:
            revision_rows = connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchall()
            actual_revision = (
                str(revision_rows[0][0])
                if len(revision_rows) == 1 and revision_rows[0][0]
                else None
            )
        else:
            revision_rows = []
            actual_revision = None

    if actual_revision != claimed_revision or (
        claimed_revision is not None and len(revision_rows) != 1
    ):
        raise RuntimeError(
            f"Database has unexpected revision {actual_revision!r}; "
            f"expected {claimed_revision!r}"
        )

    if claimed_revision is not None:
        expected = _canonical_schema(claimed_revision)
        if actual != expected:
            raise RuntimeError(
                f"Database schema does not exactly match revision "
                f"{claimed_revision!r} ({_schema_mismatch_details(actual, expected)})"
            )
        return claimed_revision

    baseline = _unversioned_schema(V073_BASELINE_REVISION)
    legacy_shapes: tuple[tuple[dict[str, _TableSchema], str], ...] = (
        (baseline, V073_BASELINE_REVISION),
        (
            _unversioned_schema(V080_SESSION_INPUT_REVISION),
            V080_SESSION_INPUT_REVISION,
        ),
        (
            _unversioned_schema(V080_IDEMPOTENCY_REVISION),
            V080_IDEMPOTENCY_REVISION,
        ),
        (
            _unversioned_schema(CURRENT_HEAD_REVISION),
            CURRENT_HEAD_REVISION,
        ),
    )
    for expected, safe_stamp_revision in legacy_shapes:
        if actual == expected:
            return safe_stamp_revision

    raise RuntimeError(
        "Unversioned database does not match the supported v0.7.3 baseline "
        "or an explicitly compatible legacy snapshot "
        f"({_schema_mismatch_details(actual, baseline)}). "
        "No guessed schema changes were applied."
    )


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


def _session_input_columns(database_path: Path) -> frozenset[str] | None:
    with _sqlite_connection(database_path) as connection:
        if "session_input" not in _table_names(connection):
            return None
        return frozenset(
            str(row[1])
            for row in connection.execute('PRAGMA table_info("session_input")')
        )


def _has_valid_v080_session_input(database_path: Path) -> bool:
    columns = _session_input_columns(database_path)
    if columns is None:
        return False
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
    _validate_revision_schema(database_path, CURRENT_HEAD_REVISION)


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
        _fsync_directory(database_path.parent)
    except OSError:
        # The replacement has already succeeded and cannot be rolled back by
        # reporting an error here. Some Windows/network filesystems do not
        # support directory fsync.
        logger.warning(
            "Directory fsync is unavailable after installing database %s",
            database_path,
        )


def _fsync_directory(directory: Path) -> None:
    try:
        directory_fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
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
