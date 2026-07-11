"""Non-destructive predecessor data migration tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.legacy_data import migrate_legacy_data


LEGACY_NAME = "open" + "yak"


def _create_database(path: Path, rows: list[tuple[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE session (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                new_optional_column TEXT DEFAULT NULL
            );
            CREATE TABLE message (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                content TEXT NOT NULL
            );
            """
        )
        connection.executemany(
            "INSERT INTO session (id, title) VALUES (?, ?)",
            rows,
        )


def test_merges_database_environment_and_user_files_without_overwrite(tmp_path: Path):
    source = tmp_path / "legacy" / "data"
    target = tmp_path / "current" / "data"
    source.mkdir(parents=True)
    target.mkdir(parents=True)

    legacy_prefix = LEGACY_NAME.upper() + "_"
    source.joinpath(".env").write_text(
        f"{legacy_prefix}DEEPSEEK_API_KEY=legacy-key\n"
        f"{legacy_prefix}OPENAI_OAUTH_ACCESS_TOKEN=legacy-token\n",
        encoding="utf-8",
    )
    target.joinpath(".env").write_text(
        "SUXIAOYOU_DEEPSEEK_API_KEY=current-key\n",
        encoding="utf-8",
    )

    legacy_db = source / "data" / f"{LEGACY_NAME}.db"
    current_db = target / "data" / "suxiaoyou.db"
    _create_database(legacy_db, [("shared", "legacy title"), ("legacy", "old chat")])
    _create_database(current_db, [("shared", "current title"), ("current", "new chat")])

    source.joinpath("report.md").write_text("legacy report", encoding="utf-8")
    target.joinpath("report.md").write_text("current report", encoding="utf-8")
    source.joinpath("generated.txt").write_text("generated", encoding="utf-8")
    source.joinpath("session_token.json").write_text("secret", encoding="utf-8")
    legacy_settings = source / ("." + LEGACY_NAME)
    legacy_settings.mkdir()
    legacy_settings.joinpath("plugins.enabled.json").write_text("{}", encoding="utf-8")

    report = migrate_legacy_data(source, target)

    assert report["status"] == "complete"
    env = target.joinpath(".env").read_text(encoding="utf-8")
    assert "SUXIAOYOU_DEEPSEEK_API_KEY=current-key" in env
    assert "SUXIAOYOU_DEEPSEEK_API_KEY=legacy-key" not in env
    assert "SUXIAOYOU_OPENAI_OAUTH_ACCESS_TOKEN=legacy-token" in env

    with sqlite3.connect(current_db) as connection:
        rows = dict(connection.execute("SELECT id, title FROM session"))
        assert rows == {
            "shared": "current title",
            "current": "new chat",
            "legacy": "old chat",
        }
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)

    assert current_db.with_name(
        "suxiaoyou.db.pre-legacy-migration-v1.bak"
    ).is_file()
    assert target.joinpath("report.md").read_text(encoding="utf-8") == "current report"
    assert target.joinpath("generated.txt").read_text(encoding="utf-8") == "generated"
    assert not target.joinpath("session_token.json").exists()
    assert target.joinpath(".suxiaoyou/plugins.enabled.json").is_file()

    second = migrate_legacy_data(source, target)
    assert second["status"] == "already_complete"


def test_copies_database_when_current_database_does_not_exist(tmp_path: Path):
    source = tmp_path / "legacy"
    target = tmp_path / "current"
    legacy_db = source / "data" / f"{LEGACY_NAME}.db"
    _create_database(legacy_db, [("legacy", "old chat")])

    report = migrate_legacy_data(source, target)

    assert report["database"]["status"] == "copied"
    with sqlite3.connect(target / "data" / "suxiaoyou.db") as connection:
        assert connection.execute("SELECT title FROM session").fetchone() == ("old chat",)


def test_missing_source_is_a_noop(tmp_path: Path):
    report = migrate_legacy_data(tmp_path / "missing", tmp_path / "target")
    assert report == {"status": "no_source"}
