"""Fail-closed checks for file metadata the v1 mutation layer cannot preserve.

Replacing a pathname with a new inode can silently discard extended attributes,
ACLs, resource forks, or hard-link topology even when the file bytes are
correct.  Until the cross-platform version store captures and restores those
properties, declarative tools must refuse such mutations rather than claim a
safe, recoverable edit.
"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import stat
import sys

from app.utils.guarded_file_mutation import guarded_file_mutation_supported
from app.utils.windows_guarded_file import (
    WindowsGuardedFileError,
    windows_has_alternate_streams,
    windows_lstat_is_reparse,
)


class UnsupportedFileMetadataError(RuntimeError):
    """A mutation would discard metadata that is not versioned in v1."""


# macOS attaches this kernel-managed provenance marker to ordinary files
# created by the application runtime itself, including every transaction
# temporary.  It is not a user-authored/resource-fork attribute and is
# recreated on the installed inode; treating it as unsupported would make all
# normal macOS edits fail closed.  Quarantine, Finder metadata, resource forks,
# and every other xattr remain blockers.
_RECREATED_SYSTEM_XATTRS = frozenset({"com.apple.provenance"})


def ensure_mutation_metadata_supported(path: str | os.PathLike[str]) -> None:
    """Reject an existing regular file whose metadata cannot be preserved."""

    target = Path(path)
    try:
        info = target.lstat()
    except FileNotFoundError:
        return
    if os.name == "nt" and windows_lstat_is_reparse(info):
        raise UnsupportedFileMetadataError(
            f"Refusing to modify a Windows reparse point: {target}"
        )
    if not stat.S_ISREG(info.st_mode):
        return
    if info.st_nlink != 1:
        raise UnsupportedFileMetadataError(
            f"Refusing to modify hard-linked file until link topology can be recovered: {target}"
        )

    if os.name == "nt":
        if not guarded_file_mutation_supported("win32"):
            raise UnsupportedFileMetadataError(
                "Refusing to modify an existing Windows file because guarded "
                f"metadata-preserving replacement is unavailable: {target}"
            )
        try:
            if windows_has_alternate_streams(target):
                raise UnsupportedFileMetadataError(
                    "Refusing to modify a Windows file with alternate data "
                    f"streams until stream sizes participate in recovery quotas: {target}"
                )
        except WindowsGuardedFileError as exc:
            raise UnsupportedFileMetadataError(
                f"Refusing to modify file because Windows streams cannot be audited: {target}"
            ) from exc
        # ReplaceFileW is invoked without either IGNORE_* flag.  Windows merges
        # the displaced file's DACL, object ID, compression/encryption state,
        # security resource attributes into the replacement.  A metadata merge
        # failure aborts the commit; ADS are rejected above until their storage
        # accounting is represented in the manifest.
        return

    attributes = [
        value
        for value in _list_extended_attributes(target)
        if value not in _RECREATED_SYSTEM_XATTRS
    ]
    if attributes:
        raise UnsupportedFileMetadataError(
            "Refusing to modify file with unsupported extended attributes "
            f"({', '.join(sorted(attributes))}): {target}"
        )

    if sys.platform == "darwin" and _darwin_has_extended_acl(target):
        raise UnsupportedFileMetadataError(
            f"Refusing to modify file with an unsupported macOS ACL: {target}"
        )


def _list_extended_attributes(path: Path) -> list[str]:
    if hasattr(os, "listxattr"):
        try:
            return list(os.listxattr(path, follow_symlinks=False))
        except (NotImplementedError, OSError):
            raise UnsupportedFileMetadataError(
                f"Refusing to modify file because extended attributes cannot be audited: {path}"
            ) from None
    if sys.platform != "darwin":
        return []

    libc = ctypes.CDLL(None, use_errno=True)
    listxattr = getattr(libc, "listxattr", None)
    if listxattr is None:
        raise UnsupportedFileMetadataError(
            f"Refusing to modify file because macOS extended attributes cannot be audited: {path}"
        )
    listxattr.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_size_t,
        ctypes.c_int,
    ]
    listxattr.restype = ctypes.c_ssize_t
    encoded = os.fsencode(path)
    size = listxattr(encoded, None, 0, 0x0001)  # XATTR_NOFOLLOW
    if size < 0:
        raise UnsupportedFileMetadataError(
            f"Refusing to modify file because macOS extended attributes cannot be read: {path}"
        )
    if size == 0:
        return []
    buffer = ctypes.create_string_buffer(size)
    copied = listxattr(encoded, buffer, size, 0x0001)
    if copied < 0 or copied > size:
        raise UnsupportedFileMetadataError(
            "Refusing to modify file because macOS extended attributes changed "
            f"while being read: {path}"
        )
    return [
        os.fsdecode(value)
        for value in bytes(buffer.raw[:copied]).split(b"\0")
        if value
    ]


def _darwin_has_extended_acl(path: Path) -> bool:
    """Return whether *path* has at least one macOS extended ACL entry."""

    libc = ctypes.CDLL(None, use_errno=True)
    acl_get_file = getattr(libc, "acl_get_file", None)
    acl_get_entry = getattr(libc, "acl_get_entry", None)
    acl_free = getattr(libc, "acl_free", None)
    if acl_get_file is None or acl_get_entry is None or acl_free is None:
        raise UnsupportedFileMetadataError(
            f"Refusing to modify file because macOS ACLs cannot be audited: {path}"
        )

    acl_get_file.argtypes = [ctypes.c_char_p, ctypes.c_int]
    acl_get_file.restype = ctypes.c_void_p
    acl_get_entry.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
    acl_get_entry.restype = ctypes.c_int
    acl_free.argtypes = [ctypes.c_void_p]
    acl_free.restype = ctypes.c_int

    # Darwin's ACL_TYPE_EXTENDED and ACL_FIRST_ENTRY constants.
    acl = acl_get_file(os.fsencode(path), 0x00000100)
    if not acl:
        error = ctypes.get_errno()
        # ENOENT means the path disappeared and must be handled as a conflict
        # by the transaction.  Other failures mean we cannot make a safe
        # metadata-preservation claim.
        if error == 2:
            return False
        raise UnsupportedFileMetadataError(
            f"Refusing to modify file because its macOS ACL cannot be read: {path}"
        )
    try:
        entry = ctypes.c_void_p()
        result = acl_get_entry(acl, 0, ctypes.byref(entry))
        if result < 0:
            raise UnsupportedFileMetadataError(
                f"Refusing to modify file because its macOS ACL cannot be enumerated: {path}"
            )
        return result == 1
    finally:
        acl_free(acl)
