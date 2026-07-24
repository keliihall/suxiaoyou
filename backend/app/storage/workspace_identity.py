"""Durable, replacement-aware identities for local workspaces.

POSIX ``st_dev`` values are allocated by the running kernel and can change
after a reboot.  They are consequently useful as a short-lived guard, but not
as a durable database identity.  This module keeps those two concerns
separate:

* POSIX workspaces get a random, app-owned identity stored atomically on the
  root directory.  A directory extended attribute is preferred so Git
  workspaces stay clean; filesystems without xattr support use a crash-safe
  marker below ``.suxiaoyou``.  The durable identity is independent of the
  root's device/inode tuple, which remains an in-flight operation guard only.
* Windows workspaces use the native volume serial and file ID.  That tuple is
  stable and is also the best operation-time guard available on Windows.

All POSIX name lookups after opening the root are relative to directory file
descriptors and reject symbolic links.  A final binding check makes a rename
or replacement during inspection fail closed.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import errno
import os
from pathlib import Path
import secrets
import stat
import sys
from typing import Final

from app.utils.windows_guarded_file import windows_path_identity


_APP_DIRECTORY: Final = ".suxiaoyou"
_MARKER_NAME: Final = "workspace-identity-v2"
_TEMP_MARKER_PREFIX: Final = ".workspace-identity-v2."
_TEMP_MARKER_SUFFIX: Final = ".tmp"
_MARKER_PREFIX: Final = "marker-v2:"
_MARKER_HEX_LENGTH: Final = 64
_MARKER_SIZE: Final = len(_MARKER_PREFIX) + _MARKER_HEX_LENGTH + 1
_LEGACY_PREFIX: Final = "stat-v1:"
_XATTR_NAME: Final = (
    "com.suxiaoyou.workspace-identity-v2"
    if sys.platform == "darwin"
    else "user.com.suxiaoyou.workspace-identity-v2"
)
_XATTR_MISSING_ERRNOS: Final = frozenset(
    {
        errno.ENODATA,
        getattr(errno, "ENOATTR", errno.ENODATA),
    }
)
_XATTR_UNSUPPORTED_ERRNOS: Final = frozenset(
    {
        errno.ENOTSUP,
        getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
        getattr(errno, "ENOSYS", errno.ENOTSUP),
    }
)


@dataclass(frozen=True, slots=True)
class WorkspaceIdentityState:
    """The durable identity and operation-time guard for one workspace."""

    canonical_path: Path
    durable_token: str
    volatile_identity: tuple[int, int]


class WorkspaceIdentityError(RuntimeError):
    """A workspace identity is absent, unsafe, corrupt, or changed."""


class _WorkspaceIdentityMissing(WorkspaceIdentityError):
    """No durable POSIX identity exists in a supported representation."""


def parse_legacy_stat_token(value: object) -> tuple[int, int] | None:
    """Parse a legacy ``stat-v1:<device>:<inode>`` token.

    Invalid and non-canonical inputs return ``None`` so database migration can
    distinguish old identity rows without accepting signs, whitespace, or
    non-ASCII decimal lookalikes.
    """

    if not isinstance(value, str) or not value.startswith(_LEGACY_PREFIX):
        return None
    fields = value[len(_LEGACY_PREFIX) :].split(":")
    if len(fields) != 2 or any(
        not field
        or len(field) > 64
        or any(character < "0" or character > "9" for character in field)
        for field in fields
    ):
        return None
    try:
        return int(fields[0]), int(fields[1])
    except ValueError:
        return None


def inspect_workspace_identity(
    path: str | os.PathLike[str],
) -> WorkspaceIdentityState:
    """Strictly inspect an existing workspace identity without creating it."""

    canonical = _canonical_directory(path)
    if sys.platform == "win32":
        return _inspect_windows(canonical)
    return _inspect_posix(canonical, create=False)


def ensure_workspace_identity(
    path: str | os.PathLike[str],
) -> WorkspaceIdentityState:
    """Return a workspace identity, securely creating its POSIX token.

    Existing entries are only inspected.  Attribute creation is create-only;
    fallback marker creation uses exclusive, no-follow opens and synchronizes
    the marker and containing directory before success is reported.  No entry
    other than the dedicated app directory and marker is removed or
    overwritten.
    """

    canonical = _canonical_directory(path)
    if sys.platform == "win32":
        return _inspect_windows(canonical)
    return _inspect_posix(canonical, create=True)


def workspace_identity_uses_file_fallback(
    path: str | os.PathLike[str],
) -> bool:
    """Establish an identity and report whether POSIX had to create a file.

    Managed Git worktrees use this capability probe on their empty, app-owned
    parent before checkout creation.  They can then reject a filesystem where
    the fallback would make every checkout permanently dirty.
    """

    identity = ensure_workspace_identity(path)
    if sys.platform == "win32":
        return False
    return _inspect_posix_xattr(identity.canonical_path, create=False) is None


def _canonical_directory(path: str | os.PathLike[str]) -> Path:
    try:
        canonical = Path(path).expanduser().resolve(strict=True)
        visible = canonical.stat(follow_symlinks=False)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise WorkspaceIdentityError(
            f"Cannot resolve workspace directory: {path!r}"
        ) from exc
    if not stat.S_ISDIR(visible.st_mode):
        raise WorkspaceIdentityError(f"Workspace is not a directory: {canonical}")
    return canonical


def _inspect_windows(canonical: Path) -> WorkspaceIdentityState:
    try:
        first = tuple(
            int(part) for part in windows_path_identity(canonical, directory=True)
        )
        second = tuple(
            int(part) for part in windows_path_identity(canonical, directory=True)
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise WorkspaceIdentityError(
            f"Cannot inspect native Windows workspace identity: {canonical}"
        ) from exc
    if (
        len(first) != 2
        or len(second) != 2
        or first != second
        or any(part < 0 for part in first)
    ):
        raise WorkspaceIdentityError(
            f"Workspace root changed during identity inspection: {canonical}"
        )
    identity = first[0], first[1]
    return WorkspaceIdentityState(
        canonical_path=canonical,
        durable_token=f"winfile-v2:{identity[0]}:{identity[1]}",
        volatile_identity=identity,
    )


def _inspect_posix(canonical: Path, *, create: bool) -> WorkspaceIdentityState:
    # Never create a preferred xattr before looking for an existing fallback
    # marker.  Filesystem capabilities can change across mounts or app
    # versions; an existing identity must always win over representation
    # preference or durable history would be silently orphaned.
    xattr_identity = _inspect_posix_xattr(canonical, create=False)
    marker_missing: _WorkspaceIdentityMissing | None = None
    try:
        marker_identity = _inspect_posix_marker_file(canonical, create=False)
    except _WorkspaceIdentityMissing as exc:
        marker_identity = None
        marker_missing = exc

    if xattr_identity is not None and marker_identity is not None:
        if xattr_identity.durable_token != marker_identity.durable_token:
            raise WorkspaceIdentityError(
                "Workspace identity representations conflict"
            )
        return xattr_identity
    if xattr_identity is not None:
        return xattr_identity
    if marker_identity is not None:
        return marker_identity
    if not create:
        assert marker_missing is not None
        raise marker_missing

    xattr_identity = _inspect_posix_xattr(canonical, create=True)
    if xattr_identity is not None:
        # Reinspect both public representations so a cross-capability create
        # race can never silently publish two different durable identities.
        return _inspect_posix(canonical, create=False)
    _inspect_posix_marker_file(canonical, create=True)
    return _inspect_posix(canonical, create=False)


def _inspect_posix_xattr(
    canonical: Path,
    *,
    create: bool,
) -> WorkspaceIdentityState | None:
    """Use one atomic directory xattr when the filesystem supports it.

    This is the preferred POSIX representation: it survives remounts without
    adding an untracked file to Git workspaces.  Unsupported filesystems fall
    back to the crash-safe marker-file protocol below.
    """

    if sys.platform != "darwin" and not all(
        hasattr(os, name) for name in ("getxattr", "setxattr")
    ):
        return None
    root_fd = -1
    try:
        root_fd = os.open(canonical, _directory_open_flags())
        root_info = os.fstat(root_fd)
        root_identity = _stat_identity(root_info)
        _verify_root_binding(root_fd, canonical, root_identity)
        try:
            payload = _get_workspace_xattr(root_fd)
        except OSError as exc:
            if exc.errno in _XATTR_UNSUPPORTED_ERRNOS:
                return None
            if exc.errno not in _XATTR_MISSING_ERRNOS:
                raise WorkspaceIdentityError(
                    "Workspace identity attribute is unreadable"
                ) from exc
            if not create:
                # A legacy/fallback file marker may still identify this root.
                return None
            token = _MARKER_PREFIX + secrets.token_hex(_MARKER_HEX_LENGTH // 2)
            payload = (token + "\n").encode("ascii")
            try:
                _set_workspace_xattr_create(root_fd, payload)
            except OSError as create_exc:
                if create_exc.errno in _XATTR_UNSUPPORTED_ERRNOS:
                    return None
                if create_exc.errno in {errno.EEXIST, getattr(errno, "EALREADY", errno.EEXIST)}:
                    payload = _get_workspace_xattr(root_fd)
                else:
                    raise WorkspaceIdentityError(
                        "Cannot create workspace identity attribute"
                    ) from create_exc
            os.fsync(root_fd)
        token = _parse_marker_payload(payload, source="attribute")
        after = os.fstat(root_fd)
        if _stat_identity(after) != root_identity:
            raise WorkspaceIdentityError(
                "Workspace root changed during identity inspection"
            )
        _verify_root_binding(root_fd, canonical, root_identity)
        if _get_workspace_xattr(root_fd) != payload:
            raise WorkspaceIdentityError(
                "Workspace identity attribute changed during inspection"
            )
        return WorkspaceIdentityState(
            canonical_path=canonical,
            durable_token=token,
            volatile_identity=root_identity,
        )
    except WorkspaceIdentityError:
        raise
    except OSError as exc:
        if exc.errno in _XATTR_UNSUPPORTED_ERRNOS:
            return None
        raise WorkspaceIdentityError(
            f"Cannot inspect workspace identity attribute: {canonical}"
        ) from exc
    finally:
        if root_fd >= 0:
            os.close(root_fd)


def _inspect_posix_marker_file(
    canonical: Path,
    *,
    create: bool,
) -> WorkspaceIdentityState:
    root_fd = -1
    app_fd = -1
    try:
        root_fd = os.open(canonical, _directory_open_flags())
        root_info = os.fstat(root_fd)
        if not stat.S_ISDIR(root_info.st_mode):
            raise WorkspaceIdentityError(f"Workspace is not a directory: {canonical}")
        root_identity = _stat_identity(root_info)
        _verify_root_binding(root_fd, canonical, root_identity)

        try:
            app_fd = _open_app_directory(root_fd)
        except FileNotFoundError:
            if not create:
                raise _WorkspaceIdentityMissing(
                    f"Workspace identity directory is missing: {canonical}"
                ) from None
            app_fd = _create_or_open_app_directory(root_fd)

        app_identity = _stat_identity(os.fstat(app_fd))
        _verify_app_directory_binding(root_fd, app_fd, app_identity)

        try:
            token = _read_marker(app_fd)
        except FileNotFoundError:
            if not create:
                raise _WorkspaceIdentityMissing(
                    f"Workspace identity marker is missing: {canonical}"
                ) from None
            token = _create_marker(app_fd)

        _verify_app_directory_binding(root_fd, app_fd, app_identity)
        _verify_root_binding(root_fd, canonical, root_identity)
        # Reopen and parse the public name after all persistence and binding
        # checks.  This detects replacement of a just-created marker too.
        if _read_marker(app_fd) != token:
            raise WorkspaceIdentityError(
                f"Workspace identity marker changed during inspection: {canonical}"
            )
        return WorkspaceIdentityState(
            canonical_path=canonical,
            durable_token=token,
            volatile_identity=root_identity,
        )
    except WorkspaceIdentityError:
        raise
    except OSError as exc:
        raise WorkspaceIdentityError(
            f"Cannot inspect workspace identity: {canonical}"
        ) from exc
    finally:
        if app_fd >= 0:
            os.close(app_fd)
        if root_fd >= 0:
            os.close(root_fd)


def _get_workspace_xattr(descriptor: int) -> bytes:
    if sys.platform != "darwin":
        return os.getxattr(descriptor, _XATTR_NAME)
    libc = ctypes.CDLL(None, use_errno=True)
    function = libc.fgetxattr
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_uint32,
        ctypes.c_int,
    ]
    function.restype = ctypes.c_ssize_t
    buffer = ctypes.create_string_buffer(_MARKER_SIZE + 1)
    size = function(
        descriptor,
        _XATTR_NAME.encode("ascii"),
        buffer,
        len(buffer),
        0,
        0,
    )
    if size < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return bytes(buffer.raw[:size])


def _set_workspace_xattr_create(descriptor: int, payload: bytes) -> None:
    if sys.platform != "darwin":
        os.setxattr(
            descriptor,
            _XATTR_NAME,
            payload,
            flags=getattr(os, "XATTR_CREATE", 1),
        )
        return
    libc = ctypes.CDLL(None, use_errno=True)
    function = libc.fsetxattr
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_uint32,
        ctypes.c_int,
    ]
    function.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(payload)
    if (
        function(
            descriptor,
            _XATTR_NAME.encode("ascii"),
            buffer,
            len(payload),
            0,
            0x0002,  # XATTR_CREATE
        )
        != 0
    ):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _directory_open_flags() -> int:
    required = ("O_DIRECTORY", "O_NOFOLLOW")
    if any(not hasattr(os, name) for name in required):
        raise WorkspaceIdentityError(
            "Secure POSIX workspace identity inspection is unavailable"
        )
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )


def _marker_open_flags(*, write: bool = False, create: bool = False) -> int:
    if not hasattr(os, "O_NOFOLLOW"):
        raise WorkspaceIdentityError(
            "Secure POSIX workspace identity inspection is unavailable"
        )
    flags = (os.O_WRONLY if write else os.O_RDONLY) | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    return flags


def _open_app_directory(root_fd: int) -> int:
    try:
        descriptor = os.open(
            _APP_DIRECTORY,
            _directory_open_flags(),
            dir_fd=root_fd,
        )
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise WorkspaceIdentityError(
            "Workspace .suxiaoyou entry is not a safe directory"
        ) from exc
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode):
        os.close(descriptor)
        raise WorkspaceIdentityError(
            "Workspace .suxiaoyou entry is not a directory"
        )
    return descriptor


def _create_or_open_app_directory(root_fd: int) -> int:
    try:
        os.mkdir(_APP_DIRECTORY, 0o700, dir_fd=root_fd)
    except FileExistsError:
        # Another process won the create race.  It must still pass the same
        # strict no-follow inspection as a pre-existing directory.
        pass
    except OSError as exc:
        raise WorkspaceIdentityError(
            "Cannot create workspace identity directory"
        ) from exc
    else:
        os.fsync(root_fd)
    return _open_app_directory(root_fd)


def _create_marker(app_fd: int) -> str:
    token = _MARKER_PREFIX + secrets.token_hex(_MARKER_HEX_LENGTH // 2)
    payload = (token + "\n").encode("ascii")
    temporary_name, descriptor = _open_temporary_marker(app_fd)
    adopted_token: str | None = None
    try:
        identity = _stat_identity(os.fstat(descriptor))
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        _validate_marker_info(os.fstat(descriptor))
        _verify_temporary_marker_binding(app_fd, temporary_name, identity)
        if not _publish_marker_noreplace(app_fd, temporary_name):
            # A concurrent successful publisher is idempotently adopted.  A
            # corrupt or attacker-controlled final entry fails inspection.
            adopted_token = _read_marker(app_fd)
    finally:
        os.close(descriptor)

        # A no-replace rename consumes our temporary name on success.  If a
        # concurrent publisher wins, or publication fails, retain the random
        # temporary entry: a check-then-unlink cleanup could delete an
        # attacker replacement.  Future inspections ignore these names.

    os.fsync(app_fd)
    if adopted_token is not None:
        return adopted_token
    visible_token = _read_marker(app_fd)
    if visible_token != token:
        raise WorkspaceIdentityError("New workspace identity marker changed")
    return token


def _publish_marker_noreplace(app_fd: int, temporary_name: str) -> bool:
    """Atomically publish a complete marker without replacing any entry."""

    try:
        _rename_noreplace(
            app_fd,
            temporary_name,
            app_fd,
            _MARKER_NAME,
        )
        return True
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            return False
        if exc.errno not in _XATTR_UNSUPPORTED_ERRNOS | {
            errno.EINVAL,
            errno.EPERM,
        }:
            raise

    # Older POSIX platforms may not expose a no-replace rename.  A hard-link
    # publication is still atomic when supported; the random source link is
    # deliberately retained because it cannot be safely unlinked by name.
    try:
        os.link(
            temporary_name,
            _MARKER_NAME,
            src_dir_fd=app_fd,
            dst_dir_fd=app_fd,
            follow_symlinks=False,
        )
        return True
    except FileExistsError:
        return False
    except OSError as exc:
        raise WorkspaceIdentityError(
            "Filesystem cannot atomically publish a workspace identity marker"
        ) from exc


def _rename_noreplace(
    source_dir_fd: int,
    source_name: str,
    destination_dir_fd: int,
    destination_name: str,
) -> None:
    """Use the native exclusive-rename primitive on Darwin or Linux."""

    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        function = libc.renameatx_np
        exclusive_flag = 0x00000004  # RENAME_EXCL
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        function = libc.renameat2
        exclusive_flag = 0x00000001  # RENAME_NOREPLACE
    else:
        raise OSError(errno.ENOTSUP, "Exclusive rename is unavailable")
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    if (
        function(
            source_dir_fd,
            os.fsencode(source_name),
            destination_dir_fd,
            os.fsencode(destination_name),
            exclusive_flag,
        )
        != 0
    ):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _open_temporary_marker(app_fd: int) -> tuple[str, int]:
    for _attempt in range(16):
        name = _TEMP_MARKER_PREFIX + secrets.token_hex(16) + _TEMP_MARKER_SUFFIX
        try:
            descriptor = os.open(
                name,
                _marker_open_flags(write=True, create=True),
                0o600,
                dir_fd=app_fd,
            )
        except FileExistsError:
            continue
        except OSError as exc:
            raise WorkspaceIdentityError(
                "Cannot create temporary workspace identity marker"
            ) from exc
        return name, descriptor
    raise WorkspaceIdentityError(
        "Cannot allocate a unique temporary workspace identity name"
    )


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError(errno.EIO, "Short write while creating identity marker")
        view = view[written:]


def _read_marker(app_fd: int) -> str:
    descriptor = -1
    try:
        descriptor = os.open(
            _MARKER_NAME,
            _marker_open_flags(),
            dir_fd=app_fd,
        )
        before = os.fstat(descriptor)
        _validate_marker_info(before)
        chunks: list[bytes] = []
        remaining = _MARKER_SIZE + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if _stable_file_signature(before) != _stable_file_signature(after):
            raise WorkspaceIdentityError(
                "Workspace identity marker changed while it was read"
            )
        visible = os.stat(_MARKER_NAME, dir_fd=app_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(visible.st_mode)
            or _stat_identity(visible) != _stat_identity(after)
        ):
            raise WorkspaceIdentityError(
                "Workspace identity marker name was replaced"
            )
    except FileNotFoundError:
        raise
    except WorkspaceIdentityError:
        raise
    except OSError as exc:
        raise WorkspaceIdentityError(
            "Workspace identity marker is unsafe or unreadable"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    return _parse_marker_payload(b"".join(chunks), source="marker")


def _parse_marker_payload(payload: bytes, *, source: str) -> str:
    if len(payload) != _MARKER_SIZE or not payload.endswith(b"\n"):
        raise WorkspaceIdentityError(f"Workspace identity {source} is corrupt")
    raw = payload[:-1]
    prefix = _MARKER_PREFIX.encode("ascii")
    if not raw.startswith(prefix):
        raise WorkspaceIdentityError(f"Workspace identity {source} is corrupt")
    digest = raw[len(prefix) :]
    if len(digest) != _MARKER_HEX_LENGTH or any(
        byte not in b"0123456789abcdef" for byte in digest
    ):
        raise WorkspaceIdentityError(f"Workspace identity {source} is corrupt")
    return raw.decode("ascii")


def _validate_marker_info(info: os.stat_result) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_size != _MARKER_SIZE
    ):
        raise WorkspaceIdentityError(
            "Workspace identity marker is not a private regular file"
        )


def _stable_file_signature(info: os.stat_result) -> tuple[int, ...]:
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_mode),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(info.st_ctime_ns),
    )


def _verify_temporary_marker_binding(
    app_fd: int,
    name: str,
    expected_identity: tuple[int, int],
) -> None:
    try:
        visible = os.stat(name, dir_fd=app_fd, follow_symlinks=False)
    except OSError as exc:
        raise WorkspaceIdentityError(
            "Temporary workspace identity marker was removed or replaced"
        ) from exc
    if (
        not stat.S_ISREG(visible.st_mode)
        or _stat_identity(visible) != expected_identity
    ):
        raise WorkspaceIdentityError(
            "Temporary workspace identity marker was removed or replaced"
        )


def _verify_app_directory_binding(
    root_fd: int,
    app_fd: int,
    expected_identity: tuple[int, int],
) -> None:
    opened = os.fstat(app_fd)
    if (
        not stat.S_ISDIR(opened.st_mode)
        or _stat_identity(opened) != expected_identity
    ):
        raise WorkspaceIdentityError("Workspace identity directory changed")
    try:
        visible = os.stat(_APP_DIRECTORY, dir_fd=root_fd, follow_symlinks=False)
    except OSError as exc:
        raise WorkspaceIdentityError(
            "Workspace identity directory was removed or replaced"
        ) from exc
    if (
        not stat.S_ISDIR(visible.st_mode)
        or _stat_identity(visible) != expected_identity
    ):
        raise WorkspaceIdentityError(
            "Workspace identity directory was removed or replaced"
        )


def _verify_root_binding(
    root_fd: int,
    canonical: Path,
    expected_identity: tuple[int, int],
) -> None:
    opened = os.fstat(root_fd)
    if not stat.S_ISDIR(opened.st_mode) or _stat_identity(opened) != expected_identity:
        raise WorkspaceIdentityError("Opened workspace root changed")
    try:
        visible = canonical.stat(follow_symlinks=False)
    except OSError as exc:
        raise WorkspaceIdentityError(
            f"Workspace root was removed or replaced: {canonical}"
        ) from exc
    if (
        not stat.S_ISDIR(visible.st_mode)
        or _stat_identity(visible) != expected_identity
    ):
        raise WorkspaceIdentityError(
            f"Workspace root was removed or replaced: {canonical}"
        )


def _stat_identity(info: os.stat_result) -> tuple[int, int]:
    return int(info.st_dev), int(info.st_ino)


__all__ = [
    "WorkspaceIdentityError",
    "WorkspaceIdentityState",
    "ensure_workspace_identity",
    "inspect_workspace_identity",
    "parse_legacy_stat_token",
    "workspace_identity_uses_file_fallback",
]
