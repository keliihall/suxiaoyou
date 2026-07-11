"""One-time, non-destructive import from the predecessor desktop data dir."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


_LEGACY_NAME = "open" + "yak"
_LEGACY_ENV_PREFIX = _LEGACY_NAME.upper() + "_"
_CURRENT_ENV_PREFIX = "SUXIAOYOU_"
_LEGACY_DATABASE = _LEGACY_NAME + ".db"
_CURRENT_DATABASE = "suxiaoyou.db"
_MARKER = ".legacy-data-migration-v1.json"
_SKIP_TOP_LEVEL = {
    ".DS_Store",
    ".env",
    "session_token.json",
    "ollama",
    "ollama-models",
}


def migrate_legacy_data(source_dir: Path, target_dir: Path) -> dict[str, Any]:
    """Merge predecessor data into ``target_dir`` without deleting the source.

    The operation is idempotent. A completion marker is written only after the
    environment, SQLite database, and user-file phases all succeed, so an
    interrupted migration is retried safely on the next launch.
    """
    source_dir = source_dir.expanduser().resolve()
    target_dir = target_dir.expanduser().resolve()
    marker = target_dir / _MARKER

    if marker.is_file():
        return {"status": "already_complete", "marker": str(marker)}
    if not source_dir.is_dir() or source_dir == target_dir:
        return {"status": "no_source"}

    target_dir.mkdir(parents=True, exist_ok=True)
    env_keys = _merge_environment(source_dir / ".env", target_dir / ".env")
    database = _merge_database(
        source_dir / "data" / _LEGACY_DATABASE,
        target_dir / "data" / _CURRENT_DATABASE,
    )
    copied_files = _copy_user_files(source_dir, target_dir)

    report: dict[str, Any] = {
        "status": "complete",
        "completed_at": datetime.now(UTC).isoformat(),
        "environment_keys_added": env_keys,
        "database": database,
        "user_files_copied": copied_files,
    }
    _atomic_write_text(marker, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def _merge_environment(source: Path, target: Path) -> list[str]:
    if not source.is_file():
        return []

    source_values: dict[str, str] = {}
    for line in source.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key.startswith(_LEGACY_ENV_PREFIX):
            source_values[_CURRENT_ENV_PREFIX + key[len(_LEGACY_ENV_PREFIX) :]] = value

    existing = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
    existing_keys = {
        line.split("=", 1)[0].strip()
        for line in existing.splitlines()
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
    }
    additions = [key for key in source_values if key not in existing_keys]
    if not additions:
        return []

    content = existing
    if content and not content.endswith("\n"):
        content += "\n"
    if content:
        content += "\n# Imported from the predecessor desktop application\n"
    content += "".join(f"{key}={source_values[key]}\n" for key in additions)
    _atomic_write_text(target, content, mode=0o600)
    return additions


def _merge_database(source: Path, target: Path) -> dict[str, Any]:
    if not source.is_file():
        return {"status": "no_source"}

    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists() or target.stat().st_size == 0:
        _sqlite_backup(source, target)
        return {"status": "copied", "rows_added": {}}

    backup = target.with_name(f"{target.name}.pre-legacy-migration-v1.bak")
    if not backup.exists():
        _sqlite_backup(target, backup)

    rows_added: dict[str, int] = {}
    connection = sqlite3.connect(target, timeout=15)
    try:
        connection.execute("PRAGMA busy_timeout = 15000")
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("ATTACH DATABASE ? AS legacy_source", (str(source),))
        try:
            current_tables = _database_tables(connection, "main")
            legacy_tables = _database_tables(connection, "legacy_source")
            connection.execute("BEGIN IMMEDIATE")
            for table in sorted(current_tables & legacy_tables):
                current_columns = _table_columns(connection, "main", table)
                legacy_columns = _table_columns(connection, "legacy_source", table)
                columns = [name for name in current_columns if name in legacy_columns]
                if not columns:
                    continue
                quoted_table = _quote_identifier(table)
                quoted_columns = ", ".join(_quote_identifier(name) for name in columns)
                before = connection.execute(
                    f"SELECT COUNT(*) FROM {quoted_table}"
                ).fetchone()[0]
                connection.execute(
                    f"INSERT OR IGNORE INTO {quoted_table} ({quoted_columns}) "
                    f"SELECT {quoted_columns} FROM legacy_source.{quoted_table}"
                )
                after = connection.execute(
                    f"SELECT COUNT(*) FROM {quoted_table}"
                ).fetchone()[0]
                if after > before:
                    rows_added[table] = after - before
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.execute("DETACH DATABASE legacy_source")

        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise RuntimeError(f"Merged database failed integrity_check: {integrity!r}")
    finally:
        connection.close()

    return {
        "status": "merged",
        "backup": backup.name,
        "rows_added": rows_added,
    }


def _database_tables(connection: sqlite3.Connection, schema: str) -> set[str]:
    rows = connection.execute(
        f"SELECT name, sql FROM {schema}.sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {
        name
        for name, sql in rows
        if sql is not None and not sql.lstrip().upper().startswith("CREATE VIRTUAL TABLE")
    }


def _table_columns(
    connection: sqlite3.Connection,
    schema: str,
    table: str,
) -> list[str]:
    quoted_table = _quote_identifier(table)
    return [
        row[1]
        for row in connection.execute(
            f"PRAGMA {schema}.table_info({quoted_table})"
        ).fetchall()
    ]


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _sqlite_backup(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with sqlite3.connect(source, timeout=15) as source_db:
            source_db.execute("PRAGMA busy_timeout = 15000")
            with sqlite3.connect(temporary) as target_db:
                source_db.backup(target_db)
                integrity = target_db.execute("PRAGMA integrity_check").fetchone()
                if not integrity or integrity[0] != "ok":
                    raise RuntimeError(f"Database backup failed integrity_check: {integrity!r}")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _copy_user_files(source_dir: Path, target_dir: Path) -> int:
    copied = 0
    for source in source_dir.iterdir():
        if source.name in _SKIP_TOP_LEVEL or source.is_symlink():
            continue
        destination_name = ".suxiaoyou" if source.name == "." + _LEGACY_NAME else source.name
        destination = target_dir / destination_name
        copied += _copy_missing(source, destination)
    return copied


def _copy_missing(source: Path, destination: Path) -> int:
    if source.is_symlink():
        return 0
    if source.is_dir():
        if source.name in {"ollama", "ollama-models"}:
            return 0
        destination.mkdir(parents=True, exist_ok=True)
        return sum(
            _copy_missing(child, destination / child.name)
            for child in source.iterdir()
            if not _is_runtime_database_file(child)
        )
    if not source.is_file() or destination.exists() or _is_runtime_database_file(source):
        return 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return 1


def _is_runtime_database_file(path: Path) -> bool:
    name = path.name
    return name == _LEGACY_DATABASE or name.startswith(_LEGACY_DATABASE + "-")


def _atomic_write_text(path: Path, content: str, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
