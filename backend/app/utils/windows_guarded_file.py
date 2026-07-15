"""Win32 primitives for identity-guarded workspace file mutation.

The public Win32 API does not expose the POSIX ``RENAME_EXCHANGE`` operation,
but :func:`ReplaceFileW` provides the property the transaction protocol really
needs: one call installs the prepared file and gives the exact displaced file
a caller-selected backup name.  The backup can therefore be validated against
the staged baseline and used as the source of a second ``ReplaceFileW`` call if
the destination was changed by another writer.

Every name-based operation is performed while handles for the workspace and
the existing destination-parent chain are open *without* ``FILE_SHARE_DELETE``.
That prevents a same-user process from renaming a checked directory or swapping
it for a junction between validation and the Win32 call.  Reparse points are
opened themselves (``FILE_FLAG_OPEN_REPARSE_POINT``) and rejected.

This module imports on non-Windows hosts so the state machine can be unit
tested there.  Native entry points are resolved only when ``Win32Backend`` is
constructed.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import errno
import os
from pathlib import Path
from pathlib import PureWindowsPath
import stat
import sys
import unicodedata
from typing import Final, Protocol


FILE_ATTRIBUTE_REPARSE_POINT: Final = 0x00000400
FILE_ATTRIBUTE_DIRECTORY: Final = 0x00000010
FILE_READ_ATTRIBUTES: Final = 0x00000080
GENERIC_READ: Final = 0x80000000
FILE_SHARE_READ: Final = 0x00000001
FILE_SHARE_WRITE: Final = 0x00000002
FILE_SHARE_DELETE: Final = 0x00000004
OPEN_EXISTING: Final = 3
FILE_FLAG_BACKUP_SEMANTICS: Final = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT: Final = 0x00200000
FILE_FLAG_SEQUENTIAL_SCAN: Final = 0x08000000
MOVEFILE_WRITE_THROUGH: Final = 0x00000008
COPY_FILE_FAIL_IF_EXISTS: Final = 0x00000001
_MAX_STREAM_NAME_CHARS: Final = 296

# ReplaceFileW can report that it completed only part of its documented state
# transition.  Callers must inspect/preserve both names instead of assuming a
# generic failure means zero side effects.
ERROR_UNABLE_TO_REMOVE_REPLACED: Final = 1175
ERROR_UNABLE_TO_MOVE_REPLACEMENT: Final = 1176
ERROR_UNABLE_TO_MOVE_REPLACEMENT_2: Final = 1177
_REPLACE_PARTIAL_ERRORS: Final = frozenset(
    {
        ERROR_UNABLE_TO_REMOVE_REPLACED,
        ERROR_UNABLE_TO_MOVE_REPLACEMENT,
        ERROR_UNABLE_TO_MOVE_REPLACEMENT_2,
    }
)
_WINDOWS_RESERVED_BASENAMES: Final = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{value}" for value in range(1, 10)),
        *(f"LPT{value}" for value in range(1, 10)),
        "COM¹",
        "COM²",
        "COM³",
        "LPT¹",
        "LPT²",
        "LPT³",
    }
)


class WindowsGuardedFileError(OSError):
    """A native guarded operation failed or its postcondition is ambiguous."""

    def __init__(
        self,
        winerror: int,
        message: str,
        path: str | os.PathLike[str] | None = None,
        *,
        may_have_mutated: bool = False,
    ) -> None:
        super().__init__(winerror, message, os.fspath(path) if path is not None else None)
        self.winerror = winerror
        self.may_have_mutated = may_have_mutated


@dataclass(frozen=True, slots=True)
class WindowsFileIdentity:
    """Filesystem identity from an opened handle, including the ReFS-width ID."""

    volume_serial: int
    file_id: int

    def as_tuple(self) -> tuple[int, int]:
        return self.volume_serial, self.file_id


@dataclass(frozen=True, slots=True)
class WindowsHandleInfo:
    identity: WindowsFileIdentity
    attributes: int
    link_count: int
    size: int

    @property
    def is_directory(self) -> bool:
        return bool(self.attributes & FILE_ATTRIBUTE_DIRECTORY)

    @property
    def is_reparse_point(self) -> bool:
        return bool(self.attributes & FILE_ATTRIBUTE_REPARSE_POINT)


class WindowsMutationBackend(Protocol):
    """Injectable kernel surface used by the testable exchange state machine."""

    def replace_file(self, target: Path, replacement: Path, backup: Path) -> None: ...

    def move_noreplace(self, source: Path, destination: Path) -> None: ...


@dataclass(frozen=True, slots=True)
class GuardedExchange:
    """Three same-directory names participating in a Win32 replacement."""

    target: Path
    replacement: Path
    displaced: Path

    def __post_init__(self) -> None:
        parents = {
            os.path.normcase(os.path.abspath(value.parent))
            for value in (self.target, self.replacement, self.displaced)
        }
        if len(parents) != 1:
            raise ValueError("Guarded Windows exchange paths must share one parent")
        if len(
            {
                os.path.normcase(value.name)
                for value in (self.target, self.replacement, self.displaced)
            }
        ) != 3:
            raise ValueError("Guarded Windows exchange paths must be distinct")

    def install(self, backend: WindowsMutationBackend) -> None:
        """Install replacement and atomically retain the displaced object."""

        backend.replace_file(self.target, self.replacement, self.displaced)

    def rollback(self, backend: WindowsMutationBackend, conflict: Path) -> None:
        """Put the displaced object back and retain the failed output.

        The second ReplaceFileW call uses the current visible output as its
        replaced file, the first call's backup as replacement, and a fourth
        unique name for the failed output.  Thus rollback never has a target
        name gap and never overwrites an unrelated conflict sidecar.
        """

        if os.path.normcase(os.path.abspath(conflict.parent)) != os.path.normcase(
            os.path.abspath(self.target.parent)
        ):
            raise ValueError("Guarded Windows rollback conflict must share the parent")
        if os.path.normcase(conflict.name) in {
            os.path.normcase(self.target.name),
            os.path.normcase(self.replacement.name),
            os.path.normcase(self.displaced.name),
        }:
            raise ValueError("Guarded Windows rollback conflict path must be unique")
        backend.replace_file(self.target, self.displaced, conflict)


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", _FILETIME),
        ("ftLastAccessTime", _FILETIME),
        ("ftLastWriteTime", _FILETIME),
        ("dwVolumeSerialNumber", wintypes.DWORD),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("nNumberOfLinks", wintypes.DWORD),
        ("nFileIndexHigh", wintypes.DWORD),
        ("nFileIndexLow", wintypes.DWORD),
    ]


class _FILE_ID_128(ctypes.Structure):
    _fields_ = [("Identifier", ctypes.c_ubyte * 16)]


class _FILE_ID_INFO(ctypes.Structure):
    _fields_ = [("VolumeSerialNumber", ctypes.c_ulonglong), ("FileId", _FILE_ID_128)]


class _WIN32_FIND_STREAM_DATA(ctypes.Structure):
    _fields_ = [
        ("StreamSize", ctypes.c_longlong),
        ("cStreamName", wintypes.WCHAR * _MAX_STREAM_NAME_CHARS),
    ]


class Win32Backend:
    """Thin, strict ctypes wrapper around the required Kernel32 operations."""

    _FILE_ID_INFO_CLASS: Final = 18

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Win32 guarded file operations require native Windows")
        # WinDLL is intentionally looked up lazily: it is absent on some
        # non-Windows Python builds even though ctypes itself is importable.
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        self._configure_signatures()

    def _configure_signatures(self) -> None:
        kernel32 = self._kernel32
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.GetFileInformationByHandle.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION),
        ]
        kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
        kernel32.GetFileInformationByHandleEx.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
        kernel32.ReplaceFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.LPVOID,
        ]
        kernel32.ReplaceFileW.restype = wintypes.BOOL
        kernel32.MoveFileExW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
        ]
        kernel32.MoveFileExW.restype = wintypes.BOOL
        kernel32.CopyFileExW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPVOID,
            wintypes.LPVOID,
            ctypes.POINTER(wintypes.BOOL),
            wintypes.DWORD,
        ]
        kernel32.CopyFileExW.restype = wintypes.BOOL
        kernel32.FindFirstStreamW.argtypes = [
            wintypes.LPCWSTR,
            ctypes.c_int,
            ctypes.POINTER(_WIN32_FIND_STREAM_DATA),
            wintypes.DWORD,
        ]
        kernel32.FindFirstStreamW.restype = wintypes.HANDLE
        kernel32.FindNextStreamW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_WIN32_FIND_STREAM_DATA),
        ]
        kernel32.FindNextStreamW.restype = wintypes.BOOL
        kernel32.FindClose.argtypes = [wintypes.HANDLE]
        kernel32.FindClose.restype = wintypes.BOOL

    @staticmethod
    def _extended(path: Path) -> str:
        value = os.path.abspath(os.fspath(path))
        if value.startswith("\\\\?\\"):
            return value
        if value.startswith("\\\\"):
            return "\\\\?\\UNC\\" + value[2:]
        return "\\\\?\\" + value

    def _raise_last_error(
        self,
        operation: str,
        path: Path,
        *,
        may_have_mutated: bool = False,
    ) -> None:
        code = ctypes.get_last_error()
        message = ctypes.FormatError(code).strip() or f"Win32 error {code}"
        raise WindowsGuardedFileError(
            code,
            f"{operation}: {message}",
            path,
            may_have_mutated=may_have_mutated,
        )

    def open_handle(
        self,
        path: Path,
        *,
        directory: bool,
        readable: bool = False,
        block_writers: bool = False,
    ) -> int:
        access = FILE_READ_ATTRIBUTES | (GENERIC_READ if readable else 0)
        share = FILE_SHARE_READ
        if not block_writers:
            share |= FILE_SHARE_WRITE
        # Deliberately never share DELETE: the opened name cannot be renamed or
        # replaced until this lease is released.
        flags = FILE_FLAG_OPEN_REPARSE_POINT
        if directory:
            flags |= FILE_FLAG_BACKUP_SEMANTICS
        elif readable:
            flags |= FILE_FLAG_SEQUENTIAL_SCAN
        handle = self._kernel32.CreateFileW(
            self._extended(path),
            access,
            share,
            None,
            OPEN_EXISTING,
            flags,
            None,
        )
        invalid = ctypes.c_void_p(-1).value
        if handle in (None, invalid):
            self._raise_last_error("CreateFileW failed", path)
        info = self.handle_info(int(handle))
        if info.is_reparse_point:
            self.close_handle(int(handle))
            raise WindowsGuardedFileError(
                errno.ELOOP,
                "Refusing a Windows reparse point in guarded workspace path",
                path,
            )
        if directory != info.is_directory:
            self.close_handle(int(handle))
            kind = "directory" if directory else "regular file"
            raise WindowsGuardedFileError(
                errno.EINVAL,
                f"Guarded Windows path is not a {kind}",
                path,
            )
        return int(handle)

    def close_handle(self, handle: int) -> None:
        if handle and not self._kernel32.CloseHandle(wintypes.HANDLE(handle)):
            self._raise_last_error("CloseHandle failed", Path("<handle>"))

    def handle_info(self, handle: int) -> WindowsHandleInfo:
        basic = _BY_HANDLE_FILE_INFORMATION()
        if not self._kernel32.GetFileInformationByHandle(
            wintypes.HANDLE(handle), ctypes.byref(basic)
        ):
            self._raise_last_error("GetFileInformationByHandle failed", Path("<handle>"))
        wide = _FILE_ID_INFO()
        if self._kernel32.GetFileInformationByHandleEx(
            wintypes.HANDLE(handle),
            self._FILE_ID_INFO_CLASS,
            ctypes.byref(wide),
            ctypes.sizeof(wide),
        ):
            volume = int(wide.VolumeSerialNumber)
            file_id = int.from_bytes(bytes(wide.FileId.Identifier), "little")
        else:
            # The classic identity remains useful on filesystems/Windows builds
            # without FileIdInfo.  It is the documented NTFS identity tuple.
            volume = int(basic.dwVolumeSerialNumber)
            file_id = (int(basic.nFileIndexHigh) << 32) | int(basic.nFileIndexLow)
        return WindowsHandleInfo(
            identity=WindowsFileIdentity(volume, file_id),
            attributes=int(basic.dwFileAttributes),
            link_count=int(basic.nNumberOfLinks),
            size=(int(basic.nFileSizeHigh) << 32) | int(basic.nFileSizeLow),
        )

    def path_info(
        self,
        path: Path,
        *,
        directory: bool,
        readable: bool = False,
        block_writers: bool = False,
    ) -> WindowsHandleInfo:
        handle = self.open_handle(
            path,
            directory=directory,
            readable=readable,
            block_writers=block_writers,
        )
        try:
            return self.handle_info(handle)
        finally:
            self.close_handle(handle)

    def replace_file(self, target: Path, replacement: Path, backup: Path) -> None:
        exchange = GuardedExchange(target, replacement, backup)
        del exchange  # validation is the purpose of construction here
        if backup.exists() or backup.is_symlink():
            raise FileExistsError(errno.EEXIST, "Backup path already exists", str(backup))
        if not self._kernel32.ReplaceFileW(
            self._extended(target),
            self._extended(replacement),
            self._extended(backup),
            0,  # Never ignore ACL/metadata merge errors.
            None,
            None,
        ):
            code = ctypes.get_last_error()
            self._raise_last_error(
                "ReplaceFileW failed",
                target,
                may_have_mutated=code in _REPLACE_PARTIAL_ERRORS,
            )

    def move_noreplace(self, source: Path, destination: Path) -> None:
        if os.path.normcase(os.path.abspath(source.parent)) != os.path.normcase(
            os.path.abspath(destination.parent)
        ):
            raise ValueError("Guarded Windows no-replace move must stay in one parent")
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(errno.EEXIST, "Destination already exists", str(destination))
        # MOVEFILE_REPLACE_EXISTING and COPY_ALLOWED are deliberately absent.
        if not self._kernel32.MoveFileExW(
            self._extended(source),
            self._extended(destination),
            MOVEFILE_WRITE_THROUGH,
        ):
            code = ctypes.get_last_error()
            if code in {80, 183}:  # ERROR_FILE_EXISTS / ERROR_ALREADY_EXISTS
                raise FileExistsError(errno.EEXIST, "Destination already exists", str(destination))
            self._raise_last_error("MoveFileExW failed", source)

    def copy_file_full(self, source: Path, destination: Path) -> None:
        """Copy all Win32 file streams/security metadata to a new name."""

        if destination.exists() or destination.is_symlink():
            raise FileExistsError(errno.EEXIST, "Destination already exists", str(destination))
        cancel = wintypes.BOOL(False)
        if not self._kernel32.CopyFileExW(
            self._extended(source),
            self._extended(destination),
            None,
            None,
            ctypes.byref(cancel),
            COPY_FILE_FAIL_IF_EXISTS,
        ):
            code = ctypes.get_last_error()
            if code in {80, 183}:
                raise FileExistsError(errno.EEXIST, "Destination already exists", str(destination))
            self._raise_last_error("CopyFileExW failed", source)

    def stream_inventory(self, path: Path, *, max_streams: int = 4096) -> tuple[tuple[str, int], ...]:
        """Enumerate every NTFS/ReFS data stream without following reparse points."""

        data = _WIN32_FIND_STREAM_DATA()
        handle = self._kernel32.FindFirstStreamW(
            self._extended(path),
            0,  # FindStreamInfoStandard
            ctypes.byref(data),
            0,
        )
        invalid = ctypes.c_void_p(-1).value
        if handle in (None, invalid):
            self._raise_last_error("FindFirstStreamW failed", path)
        streams: list[tuple[str, int]] = []
        try:
            while True:
                name = str(data.cStreamName)
                size = int(data.StreamSize)
                if not name or size < 0 or not name.endswith(":$DATA"):
                    raise WindowsGuardedFileError(
                        errno.EINVAL,
                        "Windows file returned an invalid data stream",
                        path,
                    )
                streams.append((name, size))
                if len(streams) > max_streams:
                    raise WindowsGuardedFileError(
                        errno.E2BIG,
                        "Windows file has too many alternate data streams",
                        path,
                    )
                if not self._kernel32.FindNextStreamW(handle, ctypes.byref(data)):
                    code = ctypes.get_last_error()
                    if code == 38:  # ERROR_HANDLE_EOF
                        break
                    self._raise_last_error("FindNextStreamW failed", path)
        finally:
            self._kernel32.FindClose(handle)
        return tuple(streams)


@contextmanager
def locked_directory_chain(
    workspace: Path,
    relative_paths: Sequence[str],
    *,
    backend: Win32Backend | None = None,
    expected_workspace_identity: tuple[int, int] | None = None,
) -> Iterator[Win32Backend]:
    """Lock the workspace and every existing parent needed by ``relative_paths``.

    Handles omit ``FILE_SHARE_DELETE`` and are retained for the full context.
    Components are opened with reparse-point semantics and rejected if they are
    links/junctions.  Missing descendants are allowed because targeted writes
    can create their parent directories later; their deepest existing ancestor
    remains anchored.
    """

    api = backend or Win32Backend()
    handles: list[int] = []
    opened: set[str] = set()
    try:
        root = Path(os.path.abspath(workspace))
        root_handle = api.open_handle(root, directory=True)
        handles.append(root_handle)
        root_info = api.handle_info(root_handle)
        if (
            expected_workspace_identity is not None
            and root_info.identity.as_tuple() != expected_workspace_identity
        ):
            raise WindowsGuardedFileError(
                errno.ESTALE,
                "Workspace root identity changed before guarded Windows mutation",
                root,
            )
        opened.add(os.path.normcase(os.path.abspath(root)))
        for relative in sorted(set(relative_paths), key=lambda value: (value.count("/"), value)):
            candidate = root
            parts = Path(relative).parts
            if not parts or Path(relative).is_absolute() or ".." in parts:
                raise WindowsGuardedFileError(
                    errno.EINVAL,
                    "Unsafe relative path in guarded Windows mutation",
                    relative,
                )
            for component in parts[:-1]:
                candidate = candidate / component
                key = os.path.normcase(os.path.abspath(candidate))
                if key in opened:
                    continue
                try:
                    handle = api.open_handle(candidate, directory=True)
                except WindowsGuardedFileError as exc:
                    if exc.winerror in {2, 3}:  # FILE/PATH_NOT_FOUND
                        break
                    raise
                handles.append(handle)
                opened.add(key)
        yield api
        # Reopen the public root name while the original handle is still held;
        # this proves it still resolves to the same filesystem object.
        reopened = api.open_handle(root, directory=True)
        try:
            if api.handle_info(reopened).identity != root_info.identity:
                raise WindowsGuardedFileError(
                    errno.ESTALE,
                    "Workspace root moved during guarded Windows mutation",
                    root,
                )
        finally:
            api.close_handle(reopened)
    finally:
        first_error: BaseException | None = None
        for handle in reversed(handles):
            try:
                api.close_handle(handle)
            except BaseException as exc:  # pragma: no cover - native fault path
                first_error = first_error or exc
        if first_error is not None and sys.exc_info()[0] is None:
            raise first_error


@contextmanager
def open_regular_file_for_stable_read(
    path: Path,
    *,
    backend: Win32Backend | None = None,
) -> Iterator[tuple[int, WindowsHandleInfo]]:
    """Yield a CRT fd backed by a no-reparse handle that blocks writers/deletes."""

    if sys.platform != "win32":
        raise RuntimeError("Stable Win32 file reads require native Windows")
    api = backend or Win32Backend()
    handle = api.open_handle(path, directory=False, readable=True, block_writers=True)
    info = api.handle_info(handle)
    import msvcrt  # Windows-only standard-library module

    descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    # The CRT fd owns the HANDLE after open_osfhandle succeeds.
    try:
        yield descriptor, info
    finally:
        os.close(descriptor)


def windows_path_identity(path: Path, *, directory: bool) -> tuple[int, int]:
    return Win32Backend().path_info(path, directory=directory).identity.as_tuple()


def windows_relative_key(relative: str) -> str:
    return "/".join(
        unicodedata.normalize("NFC", component).casefold()
        for component in relative.replace("\\", "/").split("/")
    )


def validate_windows_relative_name(relative: str) -> None:
    """Reject ADS, device names, and Win32 normalization aliases."""

    if not relative or relative.startswith(("/", "\\")) or "\\" in relative:
        raise ValueError(f"Unsafe Windows path: {relative!r}")
    forbidden = frozenset('<>:"|?*')
    for component in relative.split("/"):
        if (
            not component
            or component in {".", ".."}
            or component.endswith((" ", "."))
            or any(character in forbidden or ord(character) < 32 for character in component)
        ):
            raise ValueError(f"Unsafe or ambiguous Win32 path component: {component!r}")
        if component.split(".", 1)[0].upper() in _WINDOWS_RESERVED_BASENAMES:
            raise ValueError(f"Reserved Win32 device name: {component!r}")


def validate_windows_declared_path(
    workspace: str | os.PathLike[str],
    value: str | os.PathLike[str],
) -> None:
    """Validate caller spelling before Path.resolve can erase Win32 aliases."""

    raw_text = os.fspath(value)
    if raw_text.startswith(("\\\\?\\", "\\\\.\\")):
        raise ValueError("Extended/device Win32 paths are not accepted")
    raw = PureWindowsPath(raw_text)
    workspace_raw = PureWindowsPath(os.fspath(workspace))
    if raw.is_absolute():
        # ``PureWindowsPath.parts`` keeps the drive/UNC share in the first
        # (anchor) component.  Validate every caller-controlled component even
        # when the path is ultimately outside the selected workspace.  The
        # workspace resolver will reject that boundary violation later, but it
        # must never see an ADS/device/normalization alias first.
        if not raw.anchor or raw.root != "\\":
            raise ValueError(f"Unsafe absolute Windows path: {raw_text!r}")
        raw_parts = raw.parts
        workspace_parts = workspace_raw.parts
        absolute_components = raw_parts[1:]
        if absolute_components:
            validate_windows_relative_name("/".join(absolute_components))
        if len(raw_parts) < len(workspace_parts) or any(
            left.casefold() != right.casefold()
            for left, right in zip(raw_parts[: len(workspace_parts)], workspace_parts)
        ):
            return
        relative_parts = raw_parts[len(workspace_parts) :]
    else:
        if raw.drive or raw.root:
            raise ValueError(f"Unsafe drive-relative Windows path: {raw_text!r}")
        relative_parts = raw.parts
    if relative_parts:
        validate_windows_relative_name("/".join(relative_parts))
        if sys.platform == "win32":
            current = Path(workspace)
            for component in relative_parts:
                current = current / component
                try:
                    info = current.lstat()
                except FileNotFoundError:
                    break
                if windows_lstat_is_reparse(info):
                    raise ValueError(
                        f"Windows path contains a reparse point: {current}"
                    )


def windows_has_alternate_streams(path: Path) -> bool:
    streams = Win32Backend().stream_inventory(path)
    return any(name != "::$DATA" for name, _size in streams)


def windows_lstat_is_reparse(info: os.stat_result) -> bool:
    return bool(
        getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
    ) or stat.S_ISLNK(info.st_mode)


__all__ = [
    "GuardedExchange",
    "Win32Backend",
    "WindowsFileIdentity",
    "WindowsGuardedFileError",
    "WindowsHandleInfo",
    "WindowsMutationBackend",
    "locked_directory_chain",
    "open_regular_file_for_stable_read",
    "validate_windows_declared_path",
    "validate_windows_relative_name",
    "windows_has_alternate_streams",
    "windows_lstat_is_reparse",
    "windows_path_identity",
    "windows_relative_key",
]
