"""Crash-safe atomic helpers for user-facing text files.

Text is written and flushed to a temporary file in the destination directory
before a single ``os.replace`` makes it visible.  A failed write therefore
leaves an existing destination byte-for-byte intact instead of truncating it.
"""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path


def atomic_write_text(
    path: str | os.PathLike[str],
    content: str,
    *,
    encoding: str = "utf-8",
    newline: str | None = "\n",
) -> None:
    """Atomically replace *path* with *content*.

    The caller remains responsible for creating the parent directory.  When an
    existing regular file is replaced, its permission bits are preserved.  The
    temporary file is always created on the same filesystem as the target so
    ``os.replace`` cannot degrade into a copy-and-delete operation.
    """

    target = Path(path)
    parent = target.parent
    existing_mode: int | None = None
    try:
        existing_mode = stat.S_IMODE(target.stat().st_mode)
    except FileNotFoundError:
        pass

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY

    fd = -1
    temporary: Path | None = None
    for _ in range(100):
        candidate = parent / f".{target.name}.{secrets.token_hex(8)}.tmp"
        try:
            fd = os.open(candidate, flags, 0o666)
        except FileExistsError:
            continue
        temporary = candidate
        break
    if temporary is None:
        raise FileExistsError(f"Could not allocate a temporary file beside {target}")

    try:
        if existing_mode is not None:
            try:
                os.fchmod(fd, existing_mode)
            except (AttributeError, OSError):
                # Windows ACLs and some virtual filesystems do not expose a
                # useful POSIX mode through fchmod.  Atomicity does not depend
                # on the best-effort mode preservation.
                pass

        with os.fdopen(fd, "w", encoding=encoding, newline=newline) as handle:
            fd = -1  # fdopen owns it from this point onward.
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temporary, target)
        temporary = None
        _fsync_directory(parent)
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _fsync_directory(directory: Path) -> None:
    """Best-effort durability for the rename metadata on supporting systems."""

    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        directory_fd = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    except OSError:
        # Directory fsync is unsupported on Windows and some filesystems.  The
        # file contents were already fsynced and atomically installed.
        pass
    finally:
        os.close(directory_fd)
