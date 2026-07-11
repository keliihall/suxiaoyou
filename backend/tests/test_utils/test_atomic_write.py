from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from app.utils import atomic_write


def test_atomic_write_replaces_content_and_preserves_mode(tmp_path: Path) -> None:
    target = tmp_path / "report.txt"
    target.write_text("old", encoding="utf-8")
    target.chmod(0o640)

    atomic_write.atomic_write_text(target, "new\ncontent\n")

    assert target.read_bytes() == b"new\ncontent\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert list(tmp_path.glob(".report.txt.*.tmp")) == []


def test_replace_failure_keeps_existing_file_and_removes_temporary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "important.txt"
    original = b"the original must survive"
    target.write_bytes(original)

    def fail_replace(source, destination) -> None:
        del source, destination
        raise OSError("simulated rename failure")

    monkeypatch.setattr(atomic_write.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated rename failure"):
        atomic_write.atomic_write_text(target, "replacement")

    assert target.read_bytes() == original
    assert list(tmp_path.glob(".important.txt.*.tmp")) == []


def test_flush_failure_does_not_create_or_truncate_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = tmp_path / "existing.txt"
    existing.write_bytes(b"keep me")
    new_file = tmp_path / "new.txt"

    def fail_fsync(_fd: int) -> None:
        raise OSError("simulated disk failure")

    monkeypatch.setattr(atomic_write.os, "fsync", fail_fsync)

    with pytest.raises(OSError, match="simulated disk failure"):
        atomic_write.atomic_write_text(existing, "destroy me")
    with pytest.raises(OSError, match="simulated disk failure"):
        atomic_write.atomic_write_text(new_file, "partial")

    assert existing.read_bytes() == b"keep me"
    assert not new_file.exists()
    assert list(tmp_path.glob(".*.tmp")) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode semantics")
def test_new_file_respects_process_umask(tmp_path: Path) -> None:
    target = tmp_path / "new.txt"
    current_umask = os.umask(0)
    os.umask(current_umask)

    atomic_write.atomic_write_text(target, "content")

    assert stat.S_IMODE(target.stat().st_mode) == 0o666 & ~current_umask
