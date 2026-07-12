"""File API — upload, browse (native dialog), and attach-by-path."""

from __future__ import annotations

import asyncio
import base64
import errno
import hashlib
import logging
import mimetypes
import os
import platform
import stat
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Literal

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select

from app.dependencies import IndexManagerDep, SessionFactoryDep
from app.models.session import Session
from app.session.managed_workspace import managed_workspace_for_session
from app.utils.id import generate_ulid

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Telemetry — ADR-0010
# ---------------------------------------------------------------------------

def _log_browse_telemetry(
    event: str,
    request: Request | None,
    outcome: str,
    *,
    paths_count: int | None = None,
    error: str | None = None,
) -> None:
    """Emit one structured log line per /files/browse* hit.

    Per ADR-0010 the backend native-dialog code paths may be vestigial —
    the frontend (`upload.ts`) prefers Tauri's plugin-dialog and only falls
    back to these endpoints on import failure. After one release of this
    signal we decide between extraction and deletion based on hit rate.
    """
    ua = request.headers.get("user-agent", "") if request is not None else ""
    caller = "tauri" if "tauri" in ua.lower() else "browser"
    fields = [
        f"event={event}",
        f"outcome={outcome}",
        f"caller={caller}",
        f"server={platform.system()}",
    ]
    if paths_count is not None:
        fields.append(f"paths={paths_count}")
    if error:
        fields.append(f"error={error[:120]!r}")
    logger.info("telemetry.files_browse %s", " ".join(fields))

# Upload destination — relative to backend working directory
UPLOAD_DIR = Path("data/uploads")

# Browser uploads are streamed to disk.  The limit is deliberately above the
# size of long recordings while still bounding disk exhaustion from a single
# request.  Managed-workspace snapshots enforce the same ceiling.
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 1024 * 1024
MAX_TEXT_PREVIEW_BYTES = 10 * 1024 * 1024
MAX_BINARY_PREVIEW_BYTES = 50 * 1024 * 1024
NATIVE_FILE_STREAM_CHUNK_BYTES = 1024 * 1024
MAX_DIALOG_TITLE_CHARS = 200
MAX_UPLOAD_FILENAME_BYTES = 180

# In-memory hash → path index for deduplication of uploaded files
_hash_index: dict[str, Path] = {}
_hash_index_lock = threading.RLock()


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class BrowseRequest(BaseModel):
    multiple: bool = True
    title: str = "Select files"


class BrowseDirectoryRequest(BaseModel):
    title: str = "Select workspace directory"


class AttachRequest(BaseModel):
    paths: list[str]


class FileMetadata(BaseModel):
    file_id: str
    name: str
    path: str
    size: int
    mime_type: str
    source: Literal["referenced", "uploaded"] = "uploaded"
    content_hash: str | None = None


# ---------------------------------------------------------------------------
# Hash-index management
# ---------------------------------------------------------------------------

def rebuild_hash_index(*, cancel_event: threading.Event | None = None) -> None:
    """Scan uploads and atomically rebuild the dedup hash index.

    The optional event lets the lifespan-owned worker stop promptly during
    shutdown.  Building into a private dict prevents requests from observing a
    half-populated index now that startup scanning runs in the background.
    """
    if not UPLOAD_DIR.exists():
        with _hash_index_lock:
            # Preserve a file concurrently uploaded after the existence check.
            current = {
                digest: path
                for digest, path in _hash_index.items()
                if path.exists()
            }
            _hash_index.clear()
            _hash_index.update(current)
        return
    rebuilt: dict[str, Path] = {}
    for f in UPLOAD_DIR.iterdir():
        if cancel_event is not None and cancel_event.is_set():
            return
        if f.name.startswith(".") and f.name.endswith(".uploading"):
            # Crash/cancellation staging files are explicitly incomplete. They
            # remain eligible for age-based GC but must never become a dedup
            # target returned to a later upload.
            continue
        if f.is_file():
            try:
                digest_builder = hashlib.sha256()
                with f.open("rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        if cancel_event is not None and cancel_event.is_set():
                            return
                        digest_builder.update(chunk)
                rebuilt[digest_builder.hexdigest()] = f
            except OSError:
                pass
    with _hash_index_lock:
        # Do not erase entries added by an upload while the scan was running.
        for digest, path in _hash_index.items():
            if path.exists():
                rebuilt.setdefault(digest, path)
        _hash_index.clear()
        _hash_index.update(rebuilt)
        count = len(_hash_index)
    logger.info("Upload hash index rebuilt: %d entries", count)


def remove_from_hash_index(content_hash: str | None) -> None:
    """Remove a hash entry (called when an uploaded file is deleted)."""
    if content_hash:
        with _hash_index_lock:
            _hash_index.pop(content_hash, None)


def _truncate_utf8(value: str, max_bytes: int) -> str:
    """Truncate text on a UTF-8 code-point boundary."""

    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _bounded_upload_filename(name: str) -> str:
    """Leave byte-safe room for the stored ULID prefix on common filesystems."""

    if len(name.encode("utf-8")) <= MAX_UPLOAD_FILENAME_BYTES:
        return name
    path = Path(name)
    suffix = _truncate_utf8(path.suffix, 24)
    stem_budget = max(1, MAX_UPLOAD_FILENAME_BYTES - len(suffix.encode("utf-8")))
    stem = _truncate_utf8(path.stem, stem_budget)
    return f"{stem or 'f'}{suffix}"


# ---------------------------------------------------------------------------
# Native file dialog (platform-specific)
# ---------------------------------------------------------------------------

def _normalize_dialog_title(title: str) -> str:
    """Bound native-dialog labels and remove control characters."""

    normalized = " ".join(str(title).split()).strip()
    return (normalized or "Select files")[:MAX_DIALOG_TITLE_CHARS]


def _powershell_single_quoted(value: str) -> str:
    """Encode a literal for a PowerShell single-quoted string."""

    return value.replace("'", "''")


def _applescript_double_quoted(value: str) -> str:
    """Encode a literal for an AppleScript double-quoted string."""

    return value.replace("\\", "\\\\").replace('"', '\\"')


async def _open_native_file_dialog(
    multiple: bool = True,
    title: str = "Select files",
    *,
    request: Request | None = None,
) -> list[str]:
    """Open an OS-native file dialog and return selected paths.

    Uses platform-specific subprocess calls:
    - Windows: PowerShell + System.Windows.Forms.OpenFileDialog
    - macOS: osascript (AppleScript)
    - Linux: zenity
    """
    system = platform.system()
    title = _normalize_dialog_title(title)

    try:
        if system == "Windows":
            return await _dialog_windows(multiple, title)
        elif system == "Darwin":
            return await _dialog_macos(multiple, title)
        else:
            return await _dialog_linux(multiple, title)
    except Exception as e:
        logger.warning("Native file dialog failed: %s", e)
        _log_browse_telemetry("files_browse", request, "error", error=str(e))
        return []


async def _dialog_windows(multiple: bool, title: str) -> list[str]:
    """Windows: PowerShell + WinForms OpenFileDialog.

    Uses -STA for WinForms compatibility and a TopMost owner form
    so the dialog appears in front of the browser window.
    Uses subprocess.run in a thread because Windows SelectorEventLoop
    does not support asyncio.create_subprocess_exec.
    """
    multiselect = "$true" if multiple else "$false"
    title_literal = _powershell_single_quoted(_normalize_dialog_title(title))
    script = (
        # Force UTF-8 output so non-ASCII paths survive decoding
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8;"
        # Enable DPI awareness BEFORE loading WinForms — prevents blurry dialog on high-DPI screens
        "Add-Type -TypeDefinition '"
        "using System.Runtime.InteropServices; "
        "public class DpiHelper { "
        "[DllImport(\"user32.dll\")] "
        "public static extern bool SetProcessDPIAware(); "
        "}';"
        "[void][DpiHelper]::SetProcessDPIAware();"
        "Add-Type -AssemblyName System.Windows.Forms;"
        "[System.Windows.Forms.Application]::EnableVisualStyles();"
        "$f = New-Object System.Windows.Forms.Form;"
        "$f.TopMost = $true;"
        "$d = New-Object System.Windows.Forms.OpenFileDialog;"
        f"$d.Title = '{title_literal}';"
        f"$d.Multiselect = {multiselect};"
        "$d.Filter = 'All files (*.*)|*.*';"
        "if ($d.ShowDialog($f) -eq 'OK') {"
        "  $d.FileNames -join '|'"
        "}"
        "$f.Dispose()"
    )

    def _run() -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=300,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

    result = await asyncio.to_thread(_run)
    raw = result.stdout.decode("utf-8", errors="replace").strip()
    stderr = result.stderr.decode("utf-8", errors="replace").strip()
    if stderr:
        logger.warning("PowerShell dialog stderr: %s", stderr)
    if result.returncode != 0:
        logger.warning("PowerShell dialog exited with code %d", result.returncode)
    logger.info("PowerShell dialog raw stdout: %r", raw[:500])
    if not raw:
        return []
    paths = [p for p in raw.split("|") if p.strip()]
    # Log each path for debugging
    for p in paths:
        fp = Path(p)
        logger.info("Browse path: %r exists=%s is_file=%s", p, fp.exists(), fp.is_file())
    return paths


async def _dialog_macos(multiple: bool, title: str) -> list[str]:
    multi_clause = " with multiple selections allowed" if multiple else ""
    title_literal = _applescript_double_quoted(_normalize_dialog_title(title))
    script = f'choose file with prompt "{title_literal}"{multi_clause}'

    def _run() -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["osascript", "-e", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=300,
        )

    result = await asyncio.to_thread(_run)
    raw = result.stdout.decode("utf-8", errors="replace").strip()
    if not raw:
        return []
    # osascript returns "alias Macintosh HD:Users:..." format
    paths = []
    for item in raw.split(", "):
        item = item.strip()
        if item.startswith("alias "):
            item = item[6:]
        # Convert colon-separated path to POSIX
        parts = item.split(":")
        if len(parts) > 1:
            paths.append("/" + "/".join(parts[1:]))
        else:
            paths.append(item)
    return paths


async def _dialog_linux(multiple: bool, title: str) -> list[str]:
    args = ["zenity", "--file-selection", f"--title={title}"]
    if multiple:
        args.append("--multiple")
        args.append("--separator=|")

    def _run() -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=300,
        )

    result = await asyncio.to_thread(_run)
    stdout = result.stdout
    raw = stdout.decode("utf-8", errors="replace").strip()
    if not raw:
        return []
    return [p for p in raw.split("|") if p.strip()]


# ---------------------------------------------------------------------------
# Native directory dialog (platform-specific)
# ---------------------------------------------------------------------------

async def _open_native_directory_dialog(
    title: str = "Select directory",
    *,
    request: Request | None = None,
) -> str | None:
    """Open an OS-native folder picker and return the selected path."""
    system = platform.system()
    title = _normalize_dialog_title(title)
    try:
        if system == "Windows":
            return await _dir_dialog_windows(title)
        elif system == "Darwin":
            return await _dir_dialog_macos(title)
        else:
            return await _dir_dialog_linux(title)
    except Exception as e:
        logger.warning("Native directory dialog failed: %s", e)
        _log_browse_telemetry("files_browse_directory", request, "error", error=str(e))
        return None


async def _dir_dialog_windows(title: str) -> str | None:
    # COM interop class for IFileOpenDialog with FOS_PICKFOLDERS.
    # This produces the modern Explorer-style folder picker (breadcrumb
    # nav, search, favorites) instead of the legacy XP tree-view dialog.
    # All IFileDialog vtable slots must be declared in order even if unused.
    csharp = (
        "using System; "
        "using System.Runtime.InteropServices; "
        "[Flags] public enum FOS : uint { "
        "  PICKFOLDERS = 0x20, FORCEFILESYSTEM = 0x40, PATHMUSTEXIST = 0x800 "
        "} "
        "[ComImport, Guid(\"43826D1E-E718-42EE-BC55-A1E261C37BFE\"), "
        " InterfaceType(ComInterfaceType.InterfaceIsIUnknown)] "
        "public interface IShellItem { "
        "  void BindToHandler(IntPtr pbc, ref Guid bhid, ref Guid riid, out IntPtr ppv); "
        "  void GetParent(out IShellItem ppsi); "
        "  void GetDisplayName(uint sigdnName, "
        "    [MarshalAs(UnmanagedType.LPWStr)] out string ppszName); "
        "  void GetAttributes(uint sfgaoMask, out uint psfgaoAttribs); "
        "  void Compare(IShellItem psi, uint hint, out int piOrder); "
        "} "
        "[ComImport, Guid(\"42f85136-db7e-439c-85f1-e4075d135fc8\"), "
        " InterfaceType(ComInterfaceType.InterfaceIsIUnknown)] "
        "public interface IFileDialog { "
        "  [PreserveSig] int Show(IntPtr parent); "
        "  void SetFileTypes(uint cFileTypes, IntPtr rgFilterSpec); "
        "  void SetFileTypeIndex(uint iFileType); "
        "  void GetFileTypeIndex(out uint piFileType); "
        "  void Advise(IntPtr pfde, out uint pdwCookie); "
        "  void Unadvise(uint dwCookie); "
        "  void SetOptions(FOS fos); "
        "  void GetOptions(out FOS pfos); "
        "  void SetDefaultFolder(IShellItem psi); "
        "  void SetFolder(IShellItem psi); "
        "  void GetFolder(out IShellItem ppsi); "
        "  void GetCurrentSelection(out IShellItem ppsi); "
        "  void SetFileName([MarshalAs(UnmanagedType.LPWStr)] string pszName); "
        "  void GetFileName([MarshalAs(UnmanagedType.LPWStr)] out string pszName); "
        "  void SetTitle([MarshalAs(UnmanagedType.LPWStr)] string pszTitle); "
        "  void SetOkButtonLabel([MarshalAs(UnmanagedType.LPWStr)] string pszText); "
        "  void SetFileNameLabel([MarshalAs(UnmanagedType.LPWStr)] string pszLabel); "
        "  void GetResult(out IShellItem ppsi); "
        "  void AddPlace(IShellItem psi, int fdap); "
        "  void SetDefaultExtension([MarshalAs(UnmanagedType.LPWStr)] string pszDefaultExtension); "
        "  void Close(int hr); "
        "  void SetClientGuid(ref Guid guid); "
        "  void ClearClientData(); "
        "  void SetFilter(IntPtr pFilter); "
        "} "
        "[ComImport, Guid(\"DC1C5A9C-E88A-4dde-A5A1-60F82A20AEF7\")] "
        "public class FileOpenDialog {} "
        "public class FolderPicker { "
        "  public static string Show(string title, IntPtr hwnd) { "
        "    IFileDialog dlg = (IFileDialog)new FileOpenDialog(); "
        "    try { "
        "      dlg.SetOptions(FOS.PICKFOLDERS | FOS.FORCEFILESYSTEM | FOS.PATHMUSTEXIST); "
        "      dlg.SetTitle(title); "
        "      if (dlg.Show(hwnd) != 0) return null; "
        "      IShellItem item; dlg.GetResult(out item); "
        "      string path; item.GetDisplayName(0x80058000, out path); "
        "      return path; "
        "    } catch { return null; } "
        "    finally { Marshal.ReleaseComObject(dlg); } "
        "  } "
        "} "
    )
    title_literal = _powershell_single_quoted(_normalize_dialog_title(title))
    script = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8;"
        "Add-Type -TypeDefinition '"
        "using System.Runtime.InteropServices; "
        "public class DpiHelper { "
        "[DllImport(\"user32.dll\")] "
        "public static extern bool SetProcessDPIAware(); "
        "}';"
        "[void][DpiHelper]::SetProcessDPIAware();"
        "Add-Type -AssemblyName System.Windows.Forms;"
        "[System.Windows.Forms.Application]::EnableVisualStyles();"
        f"Add-Type -TypeDefinition '{csharp}';"
        "$f = New-Object System.Windows.Forms.Form;"
        "$f.TopMost = $true;"
        f"$result = [FolderPicker]::Show('{title_literal}', $f.Handle);"
        "if ($result) { $result }"
        "$f.Dispose()"
    )

    def _run() -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=300,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

    result = await asyncio.to_thread(_run)
    raw = result.stdout.decode("utf-8", errors="replace").strip()
    return raw if raw else None


async def _dir_dialog_macos(title: str) -> str | None:
    title_literal = _applescript_double_quoted(_normalize_dialog_title(title))
    script = f'choose folder with prompt "{title_literal}"'

    def _run() -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["osascript", "-e", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=300,
        )

    result = await asyncio.to_thread(_run)
    raw = result.stdout.decode("utf-8", errors="replace").strip()
    if not raw:
        return None
    if raw.startswith("alias "):
        raw = raw[6:]
    parts = raw.split(":")
    if len(parts) > 1:
        return "/" + "/".join(parts[1:])
    return raw


async def _dir_dialog_linux(title: str) -> str | None:
    def _run() -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["zenity", "--file-selection", "--directory", f"--title={title}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=300,
        )

    result = await asyncio.to_thread(_run)
    raw = result.stdout.decode("utf-8", errors="replace").strip()
    return raw if raw else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _file_metadata(path: Path, *, source: str, content_hash: str | None = None) -> FileMetadata:
    """Build FileMetadata from a resolved file path."""
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileMetadata(
        file_id=generate_ulid(),
        name=path.name,
        path=str(path.resolve()),
        size=path.stat().st_size,
        mime_type=mime_type,
        source=source,
        content_hash=content_hash,
    )


def _path_metadata(path: Path, *, source: str, content_hash: str | None = None) -> FileMetadata:
    """Build attachment metadata for a file or directory path."""
    if path.is_dir():
        resolved = path.resolve()
        return FileMetadata(
            file_id=generate_ulid(),
            name=resolved.name or str(resolved),
            path=str(resolved),
            size=0,
            mime_type="inode/directory",
            source=source,
            content_hash=content_hash,
        )
    return _file_metadata(path, source=source, content_hash=content_hash)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

class FileContentRequest(BaseModel):
    path: str
    workspace: str | None = None  # Resolve relative paths against this directory


class NativeFileActionRequest(BaseModel):
    """A source file claimed by one persisted conversation.

    Native file actions deliberately do not trust a workspace path supplied by
    the WebView.  ``session_id`` is resolved through the database and its
    persisted workspace becomes the authorization boundary.
    """

    path: str
    session_id: str


def _resolve_requested_file_path(path: str, workspace: str | None = None) -> Path:
    """Resolve a requested file path for preview/open operations.

    Relative paths are resolved against the active workspace when present.
    In unrestricted sessions, they fall back to the backend process cwd so
    references like ``backend/app/main.py`` remain openable.
    """
    file_path = Path(path)
    if file_path.is_absolute():
        return file_path

    if workspace:
        return (Path(workspace) / file_path).resolve()

    return (Path.cwd() / file_path).resolve()


@dataclass(frozen=True)
class _NativeSourceIdentity:
    device: int
    inode: int
    file_type: int
    size: int
    modified_ns: int


@dataclass
class _OpenedNativeSource:
    """An authorized source whose identity is pinned by an open handle."""

    path: Path
    workspace: Path
    relative_path: Path
    handle: BinaryIO
    opened_stat: os.stat_result

    @property
    def identity(self) -> _NativeSourceIdentity:
        return _native_source_identity(self.opened_stat)

    @property
    def identity_token(self) -> str:
        """Opaque equality token for a trusted desktop re-authorization."""

        identity = self.identity
        return (
            f"v1:{identity.device:x}:{identity.inode:x}:{identity.file_type:x}:"
            f"{identity.size:x}:{identity.modified_ns:x}"
        )


def _native_source_identity(result: os.stat_result) -> _NativeSourceIdentity:
    return _NativeSourceIdentity(
        device=result.st_dev,
        inode=result.st_ino,
        file_type=stat.S_IFMT(result.st_mode),
        size=result.st_size,
        modified_ns=result.st_mtime_ns,
    )


def _is_link_or_reparse_point(result: os.stat_result) -> bool:
    """Treat Windows junctions and other reparse points like symlinks."""

    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(result.st_mode) or bool(
        getattr(result, "st_file_attributes", 0) & reparse_attribute
    )


def _native_source_error(error: OSError) -> HTTPException:
    if isinstance(error, FileNotFoundError) or error.errno == errno.ENOENT:
        return HTTPException(status_code=404, detail="Source file no longer exists")
    if error.errno in {errno.EISDIR}:
        return HTTPException(status_code=400, detail="Source path is not a regular file")
    return HTTPException(status_code=403, detail="File source could not be authorized")


def _open_native_source_with_dir_fd(
    workspace: Path,
    relative_path: Path,
) -> _OpenedNativeSource:
    """Open every path component relative to a trusted directory handle.

    ``O_NOFOLLOW`` on every hop closes the resolution/open race: replacing
    either a parent directory or the final file with a symlink cannot redirect
    the handle outside the conversation workspace.
    """

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    file_flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    directory_fd = -1
    file_fd = -1
    try:
        directory_fd = os.open(workspace, directory_flags)
        if not stat.S_ISDIR(os.fstat(directory_fd).st_mode):
            raise PermissionError(errno.EACCES, "Workspace is not a directory")

        for component in relative_path.parts[:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            if not stat.S_ISDIR(os.fstat(next_fd).st_mode):
                os.close(next_fd)
                raise PermissionError(errno.EACCES, "Path component is not a directory")
            os.close(directory_fd)
            directory_fd = next_fd

        file_fd = os.open(relative_path.name, file_flags, dir_fd=directory_fd)
        opened_stat = os.fstat(file_fd)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise IsADirectoryError(errno.EISDIR, "Source path is not a regular file")
        handle = open(file_fd, "rb", closefd=True)
        file_fd = -1
        return _OpenedNativeSource(
            path=workspace / relative_path,
            workspace=workspace,
            relative_path=relative_path,
            handle=handle,
            opened_stat=opened_stat,
        )
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        if directory_fd >= 0:
            os.close(directory_fd)


def _assert_no_link_components(workspace: Path, relative_path: Path) -> os.stat_result:
    """Windows fallback: lstat every workspace-relative path component."""

    current = workspace
    workspace_stat = os.lstat(current)
    if _is_link_or_reparse_point(workspace_stat):
        raise PermissionError(errno.ELOOP, "Workspace is a link or reparse point")
    if not stat.S_ISDIR(workspace_stat.st_mode):
        raise PermissionError(errno.ENOTDIR, "Workspace is not a directory")

    result = workspace_stat
    for component in relative_path.parts:
        current = current / component
        result = os.lstat(current)
        if _is_link_or_reparse_point(result):
            raise PermissionError(errno.ELOOP, "Source path contains a link or reparse point")
    return result


def _windows_path_from_handle(file_descriptor: int) -> Path | None:
    """Return the kernel-resolved DOS path for a Windows file handle."""

    if os.name != "nt":
        return None

    import ctypes
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_final_path = kernel32.GetFinalPathNameByHandleW
    get_final_path.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
    ]
    get_final_path.restype = ctypes.c_uint32
    native_handle = msvcrt.get_osfhandle(file_descriptor)
    required = get_final_path(native_handle, None, 0, 0)
    if required == 0:
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_unicode_buffer(required + 1)
    written = get_final_path(native_handle, buffer, len(buffer), 0)
    if written == 0 or written >= len(buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


def _path_is_within(child: Path, parent: Path) -> bool:
    try:
        child_key = os.path.normcase(os.path.abspath(child))
        parent_key = os.path.normcase(os.path.abspath(parent))
        return os.path.commonpath([child_key, parent_key]) == parent_key
    except ValueError:
        return False


def _open_native_source_fallback(
    workspace: Path,
    relative_path: Path,
) -> _OpenedNativeSource:
    """Open safely where component-relative ``open`` is unavailable.

    Windows is additionally validated against the kernel-resolved path for the
    opened handle.  That check prevents a raced junction from supplying bytes
    outside the authorized workspace even if the path is restored immediately.
    """

    target = workspace / relative_path
    before = _assert_no_link_components(workspace, relative_path)
    if not stat.S_ISREG(before.st_mode):
        raise IsADirectoryError(errno.EISDIR, "Source path is not a regular file")

    file_descriptor = os.open(
        target,
        os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0),
    )
    try:
        opened_stat = os.fstat(file_descriptor)
        after = _assert_no_link_components(workspace, relative_path)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise IsADirectoryError(errno.EISDIR, "Source path is not a regular file")
        if _native_source_identity(after) != _native_source_identity(opened_stat):
            raise PermissionError(errno.EACCES, "Source identity changed while opening")

        resolved_handle_path = _windows_path_from_handle(file_descriptor)
        if resolved_handle_path is not None and not _path_is_within(
            resolved_handle_path,
            workspace,
        ):
            raise PermissionError(errno.EACCES, "Opened source escaped its workspace")

        handle = open(file_descriptor, "rb", closefd=True)
        file_descriptor = -1
        return _OpenedNativeSource(
            path=target,
            workspace=workspace,
            relative_path=relative_path,
            handle=handle,
            opened_stat=opened_stat,
        )
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)


def _securely_open_native_source(
    workspace: Path,
    relative_path: Path,
) -> _OpenedNativeSource:
    try:
        supports_dir_fd = os.open in os.supports_dir_fd
        if supports_dir_fd and hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW"):
            return _open_native_source_with_dir_fd(workspace, relative_path)
        return _open_native_source_fallback(workspace, relative_path)
    except HTTPException:
        raise
    except OSError as error:
        raise _native_source_error(error)


async def _authorize_native_file_source(
    body: NativeFileActionRequest,
    session_factory: SessionFactoryDep,
) -> _OpenedNativeSource:
    """Authorize and pin a regular source without holding a DB session open."""

    # Do only the database lookup in this short context.  In particular, the
    # StreamingResponse iterator never captures a yield-based get_db session.
    async with session_factory() as db:
        session = (
            await db.execute(select(Session).where(Session.id == body.session_id))
        ).scalar_one_or_none()
        if session is None:
            raise HTTPException(
                status_code=403,
                detail="File is not authorized for this conversation",
            )
        directory = session.directory

    try:
        workspace_claim = (
            managed_workspace_for_session(body.session_id, create=False)
            if directory == "."
            else Path(directory).expanduser()
        )
        workspace_claim_stat = os.lstat(workspace_claim)
        if _is_link_or_reparse_point(workspace_claim_stat):
            raise HTTPException(
                status_code=403,
                detail="Conversation workspace cannot be a symbolic link",
            )
        workspace = workspace_claim.resolve(strict=True)
        if not workspace.is_dir():
            raise HTTPException(
                status_code=403,
                detail="Conversation workspace is unavailable",
            )

        requested = Path(body.path).expanduser()
        lexical = requested if requested.is_absolute() else workspace / requested
        normalized = Path(os.path.abspath(lexical))
        try:
            relative_path = normalized.relative_to(workspace)
        except ValueError:
            raise HTTPException(
                status_code=403,
                detail="File is outside the conversation workspace",
            )
        if not relative_path.parts:
            raise HTTPException(status_code=400, detail="Source path is not a regular file")
    except HTTPException:
        raise
    except FileNotFoundError:
        raise HTTPException(status_code=403, detail="Conversation workspace is unavailable")
    except (OSError, RuntimeError, ValueError):
        raise HTTPException(status_code=403, detail="File source could not be authorized")

    return await asyncio.to_thread(
        _securely_open_native_source,
        workspace,
        relative_path,
    )


def _invoke_native_path_action(
    path: Path,
    action: Literal["open", "reveal"],
    system: str,
) -> None:
    """Invoke one platform path launcher without performing authorization."""

    if action == "open":
        if system == "Windows":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif system == "Darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return

    if system == "Windows":
        subprocess.Popen(["explorer.exe", "/select,", str(path)])
    elif system == "Darwin":
        subprocess.Popen(["open", "-R", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path.parent)])


def _launch_authorized_native_path(
    source: _OpenedNativeSource,
    action: Literal["open", "reveal"],
) -> None:
    """Perform the final identity check at the synchronous launcher entry.

    The supported OS launch APIs accept a text path, not an already-authorized
    file handle.  Keeping this re-open, identity comparison, and launcher call
    in one synchronous function removes event-loop and ``await`` gaps and keeps
    both identity-pinned handles alive until the launcher returns.

    Threat boundary: this rejects untrusted WebView/remote path escapes,
    symlinks/reparse points, and replacements observed before launcher entry.
    No portable path launcher can prevent a process with the same user's
    filesystem permissions from renaming the path in the final instructions
    between this check and the OS consuming it.  A snapshot or fd path would
    break edit-in-place and reveal-original semantics, so we do not claim that
    stronger guarantee for open/reveal.  Byte streaming does retain it because
    it reads only from the pinned handle.
    """

    system = platform.system()
    revalidated = _securely_open_native_source(
        source.workspace,
        source.relative_path,
    )
    try:
        if revalidated.identity != source.identity:
            raise HTTPException(
                status_code=403,
                detail="Source file changed before the native action",
            )
        try:
            _invoke_native_path_action(source.path, action, system)
        except Exception as error:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to {action} file: {error}",
            )
    finally:
        revalidated.handle.close()


@router.post("/files/content")
async def get_file_content(body: FileContentRequest) -> dict[str, Any]:
    """Read a file from disk and return its content for artifact preview."""
    from fastapi import HTTPException

    file_path = _resolve_requested_file_path(body.path, body.workspace)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {body.path}")
    if not file_path.is_file():
        raise HTTPException(status_code=400, detail=f"Not a file: {body.path}")

    size = file_path.stat().st_size
    if size > MAX_TEXT_PREVIEW_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"File too large to preview ({size} bytes, "
                f"max {MAX_TEXT_PREVIEW_BYTES})"
            ),
        )

    try:
        content = await asyncio.to_thread(file_path.read_text, encoding="utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail=f"Binary file cannot be previewed: {body.path}")

    mime_type = mimetypes.guess_type(file_path.name)[0] or "text/plain"
    return {
        "content": content,
        "path": str(file_path.resolve()),
        "name": file_path.name,
        "mime_type": mime_type,
        "size": size,
    }


@router.post("/files/content-binary")
async def get_file_content_binary(body: FileContentRequest) -> dict[str, Any]:
    """Read a binary file from disk and return base64-encoded content.

    Used for .docx, .xlsx, and other binary formats that need
    client-side rendering (e.g. docx-preview, SheetJS).
    """
    from fastapi import HTTPException

    file_path = _resolve_requested_file_path(body.path, body.workspace)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {body.path}")
    if not file_path.is_file():
        raise HTTPException(status_code=400, detail=f"Not a file: {body.path}")

    size = file_path.stat().st_size
    if size > MAX_BINARY_PREVIEW_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"File too large to preview ({size} bytes, "
                f"max {MAX_BINARY_PREVIEW_BYTES})"
            ),
        )

    raw = await asyncio.to_thread(file_path.read_bytes)
    content_b64 = (await asyncio.to_thread(base64.b64encode, raw)).decode("ascii")
    mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    return {
        "content_base64": content_b64,
        "name": file_path.name,
        "path": str(file_path.resolve()),
        "mime_type": mime_type,
        "size": size,
    }


@router.post("/files/native-source-info")
async def get_native_source_info(
    request: Request,
    body: NativeFileActionRequest,
    session_factory: SessionFactoryDep,
) -> dict[str, str | int]:
    """Return a canonical source path to the trusted desktop process only."""

    if getattr(request.state, "source", "remote") != "local":
        raise HTTPException(
            status_code=403,
            detail="Native file actions are only available locally",
        )

    source = await _authorize_native_file_source(body, session_factory)
    try:
        return {
            "path": str(source.path),
            "name": source.path.name,
            "size": source.opened_stat.st_size,
            "identity": source.identity_token,
        }
    finally:
        source.handle.close()


@router.post("/files/native-source-content")
async def stream_native_source_content(
    request: Request,
    body: NativeFileActionRequest,
    session_factory: SessionFactoryDep,
) -> StreamingResponse:
    """Stream an authorized source without a base64 or WebView memory hop.

    The file handle is opened before the response is returned and kept alive
    for the duration of the stream.  Tauri writes each bounded chunk to a
    destination-side temporary file and atomically installs it only after a
    successful flush/fsync.
    """

    if getattr(request.state, "source", "remote") != "local":
        raise HTTPException(
            status_code=403,
            detail="Native file actions are only available locally",
        )

    source = await _authorize_native_file_source(body, session_factory)

    async def chunks():
        try:
            while True:
                chunk = await asyncio.to_thread(
                    source.handle.read,
                    NATIVE_FILE_STREAM_CHUNK_BYTES,
                )
                if not chunk:
                    break
                yield chunk
        finally:
            source.handle.close()

    mime_type = mimetypes.guess_type(source.path.name)[0] or "application/octet-stream"
    return StreamingResponse(
        chunks(),
        media_type=mime_type,
        headers={
            "Content-Length": str(source.opened_stat.st_size),
            "Cache-Control": "no-store",
        },
    )


@router.post("/files/open-system")
async def open_with_system(
    request: Request,
    body: FileContentRequest,
) -> dict[str, str]:
    """Open a local path with the OS default application.

    This endpoint launches a process on the machine running 苏小有. Remote
    clients must never be able to invoke it, even if they know a valid path.
    """
    if getattr(request.state, "source", "remote") != "local":
        raise HTTPException(status_code=403, detail="System open is only available locally")

    file_path = _resolve_requested_file_path(body.path, body.workspace)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {body.path}")

    system = platform.system()
    try:
        if system == "Windows":
            os.startfile(str(file_path))  # type: ignore[attr-defined]
        elif system == "Darwin":
            subprocess.Popen(["open", str(file_path)])
        else:
            subprocess.Popen(["xdg-open", str(file_path)])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to open file: {e}")

    return {"status": "ok"}


@router.post("/files/open-file-default")
async def open_authorized_file_default(
    request: Request,
    body: NativeFileActionRequest,
    session_factory: SessionFactoryDep,
) -> dict[str, str]:
    """Open a session-authorized regular file with its default application."""

    if getattr(request.state, "source", "remote") != "local":
        raise HTTPException(status_code=403, detail="System open is only available locally")

    source = await _authorize_native_file_source(body, session_factory)
    try:
        await asyncio.to_thread(_launch_authorized_native_path, source, "open")
    finally:
        source.handle.close()
    return {"status": "ok"}


@router.post("/files/reveal-system")
async def reveal_with_system(
    request: Request,
    body: FileContentRequest,
) -> dict[str, str]:
    """Reveal a local path in Finder, Explorer, or the Linux file manager.

    Like ``open-system``, this is a host-side action rather than a file API.
    Remote clients are rejected before path resolution/existence checks so the
    endpoint cannot also be used as a local-path existence oracle.
    """
    if getattr(request.state, "source", "remote") != "local":
        raise HTTPException(status_code=403, detail="System reveal is only available locally")

    file_path = _resolve_requested_file_path(body.path, body.workspace)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {body.path}")
    file_path = file_path.resolve()

    system = platform.system()
    try:
        if system == "Windows":
            if file_path.is_dir():
                subprocess.Popen(["explorer.exe", str(file_path)])
            else:
                subprocess.Popen(["explorer.exe", "/select,", str(file_path)])
        elif system == "Darwin":
            subprocess.Popen(["open", "-R", str(file_path)])
        else:
            target = file_path if file_path.is_dir() else file_path.parent
            subprocess.Popen(["xdg-open", str(target)])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to reveal file: {e}")

    return {"status": "ok"}


@router.post("/files/reveal-file-system")
async def reveal_authorized_file(
    request: Request,
    body: NativeFileActionRequest,
    session_factory: SessionFactoryDep,
) -> dict[str, str]:
    """Reveal a session-authorized regular file in the platform file manager."""

    if getattr(request.state, "source", "remote") != "local":
        raise HTTPException(status_code=403, detail="System reveal is only available locally")

    source = await _authorize_native_file_source(body, session_factory)
    try:
        await asyncio.to_thread(_launch_authorized_native_path, source, "reveal")
    finally:
        source.handle.close()
    return {"status": "ok"}


@router.post("/files/browse-directory")
async def browse_directory(
    request: Request,
    body: BrowseDirectoryRequest | None = None,
) -> dict[str, str | None]:
    """Open native directory picker dialog. Returns selected path or null."""
    from fastapi import HTTPException

    if getattr(request.state, "source", "remote") != "local":
        raise HTTPException(
            status_code=403,
            detail="Native directory browsing is only available locally",
        )
    req = body or BrowseDirectoryRequest()
    path = await _open_native_directory_dialog(title=req.title, request=request)
    _log_browse_telemetry(
        "files_browse_directory",
        request,
        "success" if path else "cancel",
        paths_count=1 if path else 0,
    )
    return {"path": path}


class ListDirectoryRequest(BaseModel):
    path: str | None = None  # None = user home directory


@router.post("/files/list-directory")
async def list_directory(body: ListDirectoryRequest | None = None) -> dict[str, Any]:
    """List subdirectories of a given path for remote directory browsing.

    Returns the resolved parent path and a list of child directories.
    Hidden directories (starting with .) are excluded by default.
    """
    req = body or ListDirectoryRequest()
    target = Path(req.path) if req.path else Path.home()
    target = target.resolve()

    if not target.is_dir():
        return {"path": str(target), "parent": str(target.parent), "dirs": []}

    dirs: list[dict[str, str]] = []
    try:
        with os.scandir(target) as entries:
            for entry in entries:
                try:
                    if entry.is_dir() and not entry.name.startswith("."):
                        dirs.append({"name": entry.name, "path": entry.path})
                except (PermissionError, OSError):
                    continue
    except (PermissionError, OSError):
        pass

    dirs.sort(key=lambda d: d["name"].lower())

    return {
        "path": str(target),
        "parent": str(target.parent) if target.parent != target else None,
        "dirs": dirs,
    }


@router.post("/files/browse")
async def browse_files(
    request: Request,
    body: BrowseRequest | None = None,
) -> list[dict[str, Any]]:
    """Open native file dialog and return metadata for selected files.

    No files are copied — paths reference the originals.
    """
    from fastapi import HTTPException

    if getattr(request.state, "source", "remote") != "local":
        raise HTTPException(
            status_code=403,
            detail="Native file browsing is only available locally",
        )
    req = body or BrowseRequest()
    paths = await _open_native_file_dialog(
        multiple=req.multiple, title=req.title, request=request
    )

    results = []
    for p in paths:
        fp = Path(p)
        if fp.is_file():
            results.append(_file_metadata(fp, source="referenced").model_dump())

    _log_browse_telemetry(
        "files_browse",
        request,
        "success" if results else "cancel",
        paths_count=len(results),
    )
    return results


@router.post("/files/attach")
async def attach_by_path(body: AttachRequest) -> list[dict[str, Any]]:
    """Attach files or directories by explicit paths. No copying.

    Validates that each path exists and references it in-place.
    """
    results = []
    for p in body.paths:
        fp = Path(p)
        if fp.exists() and (fp.is_file() or fp.is_dir()):
            results.append(_path_metadata(fp, source="referenced").model_dump())
        else:
            logger.warning("Attach path not found or not attachable: %s", p)
    return results


class IngestRequest(BaseModel):
    """Ingest files into FTS index for a session."""
    session_id: str
    workspace: str
    paths: list[str]


@router.post("/files/ingest")
async def ingest_files(body: IngestRequest, manager: IndexManagerDep) -> dict[str, Any]:
    """Ingest attached files into the FTS index for an existing session.

    Called by the frontend immediately after attaching files to a session
    that already exists, so they are indexed without waiting for the next
    message to be sent.
    """
    if manager is None:
        return {"ingested": 0, "message": "FTS not enabled"}

    if not body.workspace or not body.session_id:
        return {"ingested": 0, "message": "workspace and session_id required"}

    try:
        await manager.ensure_index(body.workspace, body.session_id)

        ingested = 0
        for p in body.paths:
            fp = Path(p)
            if fp.is_file():
                try:
                    await manager.ingest_file(body.workspace, p)
                    ingested += 1
                except Exception as e:
                    logger.warning("FTS ingest failed for %s: %s", p, e)

        return {"ingested": ingested, "message": f"Ingested {ingested} file(s)"}
    except Exception as e:
        logger.error("FTS ingest error: %s", e)
        return {"ingested": 0, "message": str(e)}


@router.post("/files/upload")
async def upload_file(file: UploadFile) -> dict:
    """Upload a file (for browser drag-drop where path is unavailable).

    Includes SHA-256 deduplication: if the same content was already
    uploaded, the existing file is reused.
    """
    from fastapi import HTTPException

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    original_name = (file.filename or "untitled").replace("\\", "/")
    safe_name = Path(original_name).name.replace("\x00", "").strip() or "untitled"
    # Keep enough *bytes* (not characters) free for the ULID prefix on
    # filesystems with a 255-byte component limit.  CJK characters take three
    # UTF-8 bytes, so character slicing alone is not a safe bound.
    safe_name = _bounded_upload_filename(safe_name)

    upload_id = generate_ulid()
    temp_path = UPLOAD_DIR / f".{upload_id}.uploading"
    digest_builder = hashlib.sha256()
    size = 0

    try:
        with temp_path.open("xb") as handle:
            while chunk := await file.read(UPLOAD_CHUNK_BYTES):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File too large (max {MAX_UPLOAD_BYTES} bytes)",
                    )
                digest_builder.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())

        digest = digest_builder.hexdigest()
        with _hash_index_lock:
            existing = _hash_index.get(digest)
            if existing is not None and not existing.is_file():
                _hash_index.pop(digest, None)
                existing = None

            if existing is None:
                dest = UPLOAD_DIR / f"{upload_id}_{safe_name}"
                # Atomic installation means a cancelled request or backend
                # crash can leave only a hidden staging file, never a
                # partially-readable attachment at its final path.
                os.replace(temp_path, dest)
                _hash_index[digest] = dest
                existing = dest

        if temp_path.exists():
            temp_path.unlink()

        stored_prefix, separator, _stored_name = existing.name.partition("_")
        stored_file_id = (
            stored_prefix
            if separator and len(stored_prefix) == 26 and stored_prefix.isalnum()
            else upload_id
        )
        mime_type = (
            file.content_type
            or mimetypes.guess_type(safe_name)[0]
            or "application/octet-stream"
        )
        return FileMetadata(
            file_id=stored_file_id,
            name=safe_name,
            path=str(existing.resolve()),
            size=size,
            mime_type=mime_type,
            source="uploaded",
            content_hash=digest,
        ).model_dump()
    finally:
        # This covers validation errors, disconnect cancellation and disk-full
        # failures.  Orphan GC handles committed final files separately.
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Failed to remove incomplete upload: %s", temp_path)


# ---------------------------------------------------------------------------
# File search (for @mention autocomplete)
# ---------------------------------------------------------------------------

# Directories to skip when walking workspace for file search
_IGNORED_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".next", "dist", "build",
    ".venv", "venv", ".tox", ".mypy_cache", "target", ".turbo",
    ".cache", ".parcel-cache", ".svelte-kit", ".nuxt", ".output",
    "coverage", ".pytest_cache", ".ruff_cache",
})


class FileSearchRequest(BaseModel):
    directory: str
    query: str = ""
    limit: int = 50


class FileSearchResultItem(BaseModel):
    name: str
    relative_path: str
    absolute_path: str


def _walk_files(root: Path, query_lower: str, limit: int) -> list[FileSearchResultItem]:
    """Recursively walk *root*, returning files whose relative path contains *query_lower*."""
    results: list[FileSearchResultItem] = []

    def _scan(directory: Path, rel_prefix: str) -> None:
        if len(results) >= limit * 3:  # collect extra for sorting, cap for perf
            return
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name.lower())
        except (PermissionError, OSError):
            return
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                if entry.name in _IGNORED_DIRS or entry.name.startswith("."):
                    continue
                _scan(entry.path, f"{rel_prefix}{entry.name}/")
            elif entry.is_file(follow_symlinks=False):
                rel_path = f"{rel_prefix}{entry.name}"
                if not query_lower or query_lower in rel_path.lower():
                    results.append(FileSearchResultItem(
                        name=entry.name,
                        relative_path=rel_path,
                        absolute_path=str(Path(entry.path).resolve()),
                    ))

    _scan(root, "")

    if not query_lower:
        # No query — return shortest paths first
        results.sort(key=lambda r: (len(r.relative_path), r.relative_path.lower()))
    else:
        # Sort: exact filename match first, then shortest path
        def _sort_key(r: FileSearchResultItem) -> tuple[int, int, str]:
            name_lower = r.name.lower()
            exact = 0 if name_lower == query_lower else (1 if query_lower in name_lower else 2)
            return (exact, len(r.relative_path), r.relative_path.lower())
        results.sort(key=_sort_key)

    return results[:limit]


@router.post("/files/search")
async def search_files(body: FileSearchRequest) -> list[dict[str, str]]:
    """Search for files in a workspace directory. Used for @mention autocomplete."""
    root = Path(body.directory)
    if not root.is_dir():
        return []

    query_lower = body.query.strip().lower()
    limit = min(body.limit, 100)  # hard cap

    results = await asyncio.to_thread(_walk_files, root, query_lower, limit)
    return [r.model_dump() for r in results]
