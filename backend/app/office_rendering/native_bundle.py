"""Fail-closed native dependency closure verification for Office bundles.

The authoritative renderer is release-owned native code.  Hashing its files is
necessary but insufficient: an executable-looking text file, a binary for the
wrong architecture, or an unpinned private library could otherwise inherit the
signed renderer identity.  This module parses the native object formats
directly, compares their imported libraries with a canonical release manifest,
and rejects every native file that is not in that manifest.

No host inspection command is used.  Besides making verification reproducible
on all build hosts, this prevents PATH or locale from changing the evidence.
"""

from __future__ import annotations

import hashlib
import json
import mmap
import os
from pathlib import Path, PurePosixPath
import stat
import struct
import unicodedata
from dataclasses import dataclass
from typing import Any, Final, Mapping, Sequence


DEPENDENCY_MANIFEST_FILENAME: Final = "dependency-manifest.json"
DEPENDENCY_MANIFEST_SCHEMA_VERSION: Final = 1
MAX_DEPENDENCY_MANIFEST_BYTES: Final = 2 * 1024 * 1024
MAX_NATIVE_FILE_BYTES: Final = 1024 * 1024 * 1024
MAX_NATIVE_FILES: Final = 20_000
MAX_NATIVE_DEPENDENCIES: Final = 4_096

_TARGETS: Final = frozenset(
    {
        "darwin-arm64",
        "darwin-x64",
        "linux-arm64",
        "linux-x64",
        "windows-arm64",
        "windows-x64",
    }
)

# These are the only dependencies allowed to resolve outside the private
# renderer tree.  Everything else must have an exact manifest path.  Keep this
# list deliberately conservative; additions are reviewed application code.
_LINUX_SYSTEM_DEPENDENCIES: Final = frozenset(
    {
        "ld-linux-aarch64.so.1",
        "ld-linux-x86-64.so.2",
        "libc.so.6",
        "libdl.so.2",
        "libgcc_s.so.1",
        "libm.so.6",
        "libpthread.so.0",
        "libresolv.so.2",
        "librt.so.1",
        "libstdc++.so.6",
        "libutil.so.1",
    }
)
_DARWIN_SYSTEM_DEPENDENCIES: Final = frozenset(
    {
        "/usr/lib/libSystem.B.dylib",
        "/usr/lib/libbz2.1.0.dylib",
        "/usr/lib/libc++.1.dylib",
        "/usr/lib/libiconv.2.dylib",
        "/usr/lib/libobjc.A.dylib",
        "/usr/lib/libresolv.9.dylib",
        "/usr/lib/libxml2.2.dylib",
        "/usr/lib/libxslt.1.dylib",
        "/usr/lib/libz.1.dylib",
        "/System/Library/Frameworks/Accelerate.framework/Versions/A/Accelerate",
        "/System/Library/Frameworks/AppKit.framework/Versions/C/AppKit",
        "/System/Library/Frameworks/ApplicationServices.framework/Versions/A/ApplicationServices",
        "/System/Library/Frameworks/Carbon.framework/Versions/A/Carbon",
        "/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation",
        "/System/Library/Frameworks/CoreGraphics.framework/Versions/A/CoreGraphics",
        "/System/Library/Frameworks/CoreServices.framework/Versions/A/CoreServices",
        "/System/Library/Frameworks/CoreText.framework/Versions/A/CoreText",
        "/System/Library/Frameworks/Foundation.framework/Versions/C/Foundation",
        "/System/Library/Frameworks/IOKit.framework/Versions/A/IOKit",
        "/System/Library/Frameworks/ImageIO.framework/Versions/A/ImageIO",
        "/System/Library/Frameworks/Security.framework/Versions/A/Security",
        (
            "/System/Library/Frameworks/UniformTypeIdentifiers.framework/"
            "Versions/A/UniformTypeIdentifiers"
        ),
    }
)
_WINDOWS_SYSTEM_DEPENDENCIES: Final = frozenset(
    {
        "advapi32.dll",
        "bcrypt.dll",
        "cfgmgr32.dll",
        "combase.dll",
        "comctl32.dll",
        "crypt32.dll",
        "dwmapi.dll",
        "gdi32.dll",
        "imm32.dll",
        "iphlpapi.dll",
        "kernel32.dll",
        "mpr.dll",
        "msvcrt.dll",
        "ntdll.dll",
        "ole32.dll",
        "oleaut32.dll",
        "rpcrt4.dll",
        "secur32.dll",
        "setupapi.dll",
        "shell32.dll",
        "shlwapi.dll",
        "user32.dll",
        "userenv.dll",
        "version.dll",
        "winhttp.dll",
        "winmm.dll",
        "winspool.drv",
        "ws2_32.dll",
    }
)
_WINDOWS_SYSTEM_PREFIXES: Final = (
    "api-ms-win-",
    "ext-ms-win-",
)

_MACHO_64_MAGICS: Final = {
    b"\xcf\xfa\xed\xfe": "<",
    b"\xfe\xed\xfa\xcf": ">",
}
_OTHER_NATIVE_MAGICS: Final = frozenset(
    {
        b"\xca\xfe\xba\xbe",  # 32-bit universal Mach-O
        b"\xbe\xba\xfe\xca",
        b"\xca\xfe\xba\xbf",  # 64-bit universal Mach-O
        b"\xbf\xba\xfe\xca",
        b"\xce\xfa\xed\xfe",  # 32-bit thin Mach-O
        b"\xfe\xed\xfa\xce",
    }
)
_MACHO_LOAD_DYLIB_COMMANDS: Final = frozenset(
    {
        0x0000000C,  # LC_LOAD_DYLIB
        0x00000018 | 0x80000000,  # LC_LOAD_WEAK_DYLIB
        0x0000001F | 0x80000000,  # LC_REEXPORT_DYLIB
        0x00000020,  # LC_LAZY_LOAD_DYLIB
        0x00000023 | 0x80000000,  # LC_LOAD_UPWARD_DYLIB
    }
)


class NativeBundleVerificationError(RuntimeError):
    """The renderer's native binary closure is not release-safe."""


@dataclass(frozen=True, slots=True)
class NativeBundleClosure:
    """Path-free identity returned after complete closure verification."""

    platform_target: str
    manifest_sha256: str
    closure_sha256: str
    native_file_count: int
    dependency_count: int


@dataclass(frozen=True, slots=True)
class _Dependency:
    name: str
    scope: str
    path: PurePosixPath | None


@dataclass(frozen=True, slots=True)
class _NativeFile:
    path: PurePosixPath
    kind: str
    size: int
    sha256: str
    dependencies: tuple[_Dependency, ...]


@dataclass(frozen=True, slots=True)
class _ParsedNative:
    dependencies: tuple[str, ...]
    search_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, order=True)
class _InventoryEntry:
    path: str
    kind: str
    mode: int
    size: int
    device: int
    inode: int
    modified_ns: int
    changed_ns: int


def verify_native_bundle(
    root: Path,
    *,
    platform_target: str,
    executable_paths: Sequence[PurePosixPath],
) -> NativeBundleClosure:
    """Verify and fingerprint the exact native closure below ``root``.

    ``executable_paths`` is supplied by the trusted deployment layout, never by
    the manifest.  Both renderer entry points must therefore be native 64-bit
    executables for the requested target even if a signed manifest attempts to
    classify them differently.
    """

    if platform_target not in _TARGETS:
        raise NativeBundleVerificationError("native platform target is unsupported")
    private_root = _private_root(Path(root), platform_target=platform_target)
    expected_executables = tuple(_relative_path(path) for path in executable_paths)
    if len(expected_executables) != 2 or len(set(expected_executables)) != 2:
        raise NativeBundleVerificationError("native executable set is invalid")

    raw_manifest = _read_bounded_file(
        private_root,
        PurePosixPath(DEPENDENCY_MANIFEST_FILENAME),
        max_bytes=MAX_DEPENDENCY_MANIFEST_BYTES,
        platform_target=platform_target,
    )
    manifest_sha256 = hashlib.sha256(raw_manifest).hexdigest()
    decoded = _decode_canonical_manifest(raw_manifest)
    files = _parse_manifest(decoded, platform_target=platform_target)
    by_path = {item.path: item for item in files}
    if len(by_path) != len(files):
        raise NativeBundleVerificationError("native manifest paths are duplicated")
    for path in expected_executables:
        item = by_path.get(path)
        if item is None or item.kind != "executable":
            raise NativeBundleVerificationError("native entry point is not declared")

    actual_native_paths, inventory_before = _scan_native_files(
        private_root,
        platform_target=platform_target,
    )
    if actual_native_paths != frozenset(by_path):
        raise NativeBundleVerificationError("native bundle inventory does not match manifest")

    parsed_dependencies: dict[PurePosixPath, tuple[str, ...]] = {}
    for item in files:
        payload, actual_sha256, actual_size, actual_mode = _read_native_file(
            private_root,
            item.path,
            platform_target=platform_target,
        )
        if actual_size != item.size or actual_sha256 != item.sha256:
            payload.close()
            raise NativeBundleVerificationError("native file identity does not match manifest")
        try:
            _validate_native_mode(
                actual_mode,
                kind=item.kind,
                platform_target=platform_target,
            )
        except Exception:
            payload.close()
            raise
        parsed = _parse_native_metadata(payload, platform_target, kind=item.kind)
        actual_imports = parsed.dependencies
        _validate_search_paths(
            importer=item.path,
            search_paths=parsed.search_paths,
            platform_target=platform_target,
        )
        declared_names = tuple(dependency.name for dependency in item.dependencies)
        if actual_imports != declared_names:
            raise NativeBundleVerificationError("native imports do not match manifest")
        parsed_dependencies[item.path] = actual_imports

        for dependency in item.dependencies:
            if dependency.scope == "system":
                if not _allowed_system_dependency(dependency.name, platform_target):
                    raise NativeBundleVerificationError(
                        "native system dependency is not allowlisted"
                    )
                continue
            assert dependency.path is not None
            target = by_path.get(dependency.path)
            if target is None or dependency.path == item.path:
                raise NativeBundleVerificationError(
                    "native private dependency is not recursively declared"
                )
            _validate_private_resolution(
                importer=item.path,
                dependency=dependency,
                manifest_paths=frozenset(by_path),
                search_paths=parsed.search_paths,
                platform_target=platform_target,
            )

    final_native_paths, inventory_after = _scan_native_files(
        private_root,
        platform_target=platform_target,
    )
    final_manifest = _read_bounded_file(
        private_root,
        PurePosixPath(DEPENDENCY_MANIFEST_FILENAME),
        max_bytes=MAX_DEPENDENCY_MANIFEST_BYTES,
        platform_target=platform_target,
    )
    if (
        final_native_paths != actual_native_paths
        or inventory_after != inventory_before
        or final_manifest != raw_manifest
    ):
        raise NativeBundleVerificationError("native bundle changed during verification")

    evidence = {
        "domain": "suxiaoyou-office-native-closure-v1",
        "manifest_sha256": manifest_sha256,
        "platform_target": platform_target,
        "files": [
            {
                "dependencies": list(parsed_dependencies[item.path]),
                "kind": item.kind,
                "path": item.path.as_posix(),
                "sha256": item.sha256,
                "size": item.size,
            }
            for item in files
        ],
    }
    evidence_bytes = _canonical_json_bytes(evidence)
    return NativeBundleClosure(
        platform_target=platform_target,
        manifest_sha256=manifest_sha256,
        closure_sha256=hashlib.sha256(evidence_bytes).hexdigest(),
        native_file_count=len(files),
        dependency_count=sum(len(item.dependencies) for item in files),
    )


def _decode_canonical_manifest(raw: bytes) -> dict[str, Any]:
    try:
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_no_duplicate_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"invalid JSON constant: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise NativeBundleVerificationError("native dependency manifest is invalid") from exc
    if not isinstance(decoded, dict) or raw != _canonical_json_bytes(decoded):
        raise NativeBundleVerificationError("native dependency manifest is not canonical")
    return decoded


def _parse_manifest(
    decoded: Mapping[str, Any],
    *,
    platform_target: str,
) -> tuple[_NativeFile, ...]:
    if set(decoded) != {"files", "platform_target", "schema_version"}:
        raise NativeBundleVerificationError("native dependency manifest schema is invalid")
    if decoded.get("schema_version") != DEPENDENCY_MANIFEST_SCHEMA_VERSION:
        raise NativeBundleVerificationError("native dependency manifest schema is invalid")
    if decoded.get("platform_target") != platform_target:
        raise NativeBundleVerificationError("native dependency target does not match")
    raw_files = decoded.get("files")
    if (
        not isinstance(raw_files, list)
        or not raw_files
        or len(raw_files) > MAX_NATIVE_FILES
    ):
        raise NativeBundleVerificationError("native dependency file list is invalid")

    files: list[_NativeFile] = []
    for raw_file in raw_files:
        if not isinstance(raw_file, dict) or set(raw_file) != {
            "dependencies",
            "kind",
            "path",
            "sha256",
            "size",
        }:
            raise NativeBundleVerificationError("native dependency file is invalid")
        path = _relative_path(raw_file.get("path"))
        kind = raw_file.get("kind")
        size = raw_file.get("size")
        sha256 = raw_file.get("sha256")
        raw_dependencies = raw_file.get("dependencies")
        if (
            kind not in {"executable", "library"}
            or not isinstance(size, int)
            or isinstance(size, bool)
            or not 1 <= size <= MAX_NATIVE_FILE_BYTES
            or not _is_sha256(sha256)
            or not isinstance(raw_dependencies, list)
            or len(raw_dependencies) > MAX_NATIVE_DEPENDENCIES
        ):
            raise NativeBundleVerificationError("native dependency file is invalid")
        dependencies = tuple(
            _parse_dependency(value, platform_target=platform_target)
            for value in raw_dependencies
        )
        dependency_keys = tuple(
            (
                dependency.name,
                dependency.scope,
                dependency.path.as_posix() if dependency.path else "",
            )
            for dependency in dependencies
        )
        if dependency_keys != tuple(sorted(dependency_keys)) or len(
            {dependency.name for dependency in dependencies}
        ) != len(dependencies):
            raise NativeBundleVerificationError("native dependency list is not canonical")
        files.append(
            _NativeFile(
                path=path,
                kind=kind,
                size=size,
                sha256=sha256,
                dependencies=dependencies,
            )
        )
    if tuple(item.path.as_posix() for item in files) != tuple(
        sorted(item.path.as_posix() for item in files)
    ):
        raise NativeBundleVerificationError("native dependency files are not canonical")
    if platform_target.startswith(("darwin-", "windows-")) and len(
        {item.path.as_posix().casefold() for item in files}
    ) != len(files):
        raise NativeBundleVerificationError("native dependency paths collide on target")
    return tuple(files)


def _parse_dependency(value: object, *, platform_target: str) -> _Dependency:
    if not isinstance(value, dict):
        raise NativeBundleVerificationError("native dependency is invalid")
    scope = value.get("scope")
    expected_keys = {"name", "scope", "path"} if scope == "private" else {"name", "scope"}
    if set(value) != expected_keys or scope not in {"private", "system"}:
        raise NativeBundleVerificationError("native dependency is invalid")
    name = value.get("name")
    if (
        not isinstance(name, str)
        or not name
        or len(name.encode("utf-8")) > 1024
        or "\x00" in name
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
        or unicodedata.normalize("NFC", name) != name
        or name != _normalize_dependency_name(name, platform_target)
    ):
        raise NativeBundleVerificationError("native dependency name is invalid")
    path = _relative_path(value.get("path")) if scope == "private" else None
    return _Dependency(name=name, scope=scope, path=path)


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        + b"\n"
    )


def _no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _relative_path(value: object) -> PurePosixPath:
    if not isinstance(value, (str, PurePosixPath)):
        raise NativeBundleVerificationError("native dependency path is invalid")
    if isinstance(value, str) and (
        not value
        or "\\" in value
        or unicodedata.normalize("NFC", value) != value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise NativeBundleVerificationError("native dependency path is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or len(path.as_posix().encode("utf-8")) > 1024
        or (isinstance(value, str) and path.as_posix() != value)
    ):
        raise NativeBundleVerificationError("native dependency path is invalid")
    return path


def _private_root(root: Path, *, platform_target: str) -> Path:
    try:
        info = root.lstat()
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise NativeBundleVerificationError("native bundle is unavailable") from exc
    if root.is_symlink() or not stat.S_ISDIR(info.st_mode):
        raise NativeBundleVerificationError("native bundle root is invalid")
    _validate_safe_mode(info.st_mode, platform_target=platform_target, directory=True)
    return resolved


def _secure_path(
    root: Path,
    relative: PurePosixPath,
    *,
    platform_target: str,
) -> tuple[Path, os.stat_result]:
    current = root
    try:
        for part in relative.parts[:-1]:
            current = current / part
            info = current.lstat()
            if current.is_symlink() or not stat.S_ISDIR(info.st_mode):
                raise NativeBundleVerificationError("native bundle directory is invalid")
            _validate_safe_mode(info.st_mode, platform_target=platform_target, directory=True)
        candidate = current / relative.parts[-1]
        info = candidate.lstat()
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise NativeBundleVerificationError("native bundle file is unavailable") from exc
    if candidate.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise NativeBundleVerificationError("native bundle file is invalid")
    _validate_safe_mode(info.st_mode, platform_target=platform_target, directory=False)
    return resolved, info


def _read_bounded_file(
    root: Path,
    relative: PurePosixPath,
    *,
    max_bytes: int,
    platform_target: str,
) -> bytes:
    path, visible_before = _secure_path(
        root,
        relative,
        platform_target=platform_target,
    )
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise NativeBundleVerificationError("native bundle file is unavailable") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 0 <= before.st_size <= max_bytes:
            raise NativeBundleVerificationError("native bundle file is too large")
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(descriptor, 1024 * 1024):
            total += len(chunk)
            if total > max_bytes:
                raise NativeBundleVerificationError("native bundle file is too large")
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if total != before.st_size or not _same_file_state(before, after) or not _same_file_state(
        visible_before, after
    ):
        raise NativeBundleVerificationError("native bundle file changed")
    try:
        visible_after = path.lstat()
    except OSError as exc:
        raise NativeBundleVerificationError("native bundle file changed") from exc
    if not _same_file_state(after, visible_after):
        raise NativeBundleVerificationError("native bundle file changed")
    return b"".join(chunks)


def _read_native_file(
    root: Path,
    relative: PurePosixPath,
    *,
    platform_target: str,
) -> tuple[mmap.mmap, str, int, int]:
    path, visible_before = _secure_path(root, relative, platform_target=platform_target)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise NativeBundleVerificationError("native file is unavailable") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or not 1 <= before.st_size <= MAX_NATIVE_FILE_BYTES
        ):
            raise NativeBundleVerificationError("native file size is invalid")
        digest = hashlib.sha256()
        total = 0
        while chunk := os.read(descriptor, 1024 * 1024):
            total += len(chunk)
            if total > MAX_NATIVE_FILE_BYTES:
                raise NativeBundleVerificationError("native file size is invalid")
            digest.update(chunk)
        os.lseek(descriptor, 0, os.SEEK_SET)
        payload = mmap.mmap(descriptor, 0, access=mmap.ACCESS_READ)
        after = os.fstat(descriptor)
    except Exception:
        os.close(descriptor)
        raise
    os.close(descriptor)
    if total != before.st_size or not _same_file_state(before, after) or not _same_file_state(
        visible_before, after
    ):
        payload.close()
        raise NativeBundleVerificationError("native file changed")
    try:
        visible_after = path.lstat()
    except OSError as exc:
        payload.close()
        raise NativeBundleVerificationError("native file changed") from exc
    if not _same_file_state(after, visible_after):
        payload.close()
        raise NativeBundleVerificationError("native file changed")
    return payload, digest.hexdigest(), total, before.st_mode


def _scan_native_files(
    root: Path,
    *,
    platform_target: str,
) -> tuple[frozenset[PurePosixPath], tuple[_InventoryEntry, ...]]:
    result: set[PurePosixPath] = set()
    inventory: list[_InventoryEntry] = []
    directories: list[tuple[Path, PurePosixPath]] = [(root, PurePosixPath())]
    visited_files = 0
    try:
        while directories:
            directory, relative_directory = directories.pop()
            info = directory.lstat()
            if directory.is_symlink() or not stat.S_ISDIR(info.st_mode):
                raise NativeBundleVerificationError("native bundle directory is invalid")
            _validate_safe_mode(info.st_mode, platform_target=platform_target, directory=True)
            inventory.append(
                _inventory_entry(
                    relative_directory.as_posix(),
                    "directory",
                    info,
                )
            )
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                relative = relative_directory / entry.name
                if stat.S_ISLNK(info.st_mode):
                    raise NativeBundleVerificationError("native bundle symlink is invalid")
                if stat.S_ISDIR(info.st_mode):
                    _validate_safe_mode(
                        info.st_mode,
                        platform_target=platform_target,
                        directory=True,
                    )
                    directories.append((Path(entry.path), relative))
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise NativeBundleVerificationError("native bundle file is invalid")
                _validate_safe_mode(
                    info.st_mode,
                    platform_target=platform_target,
                    directory=False,
                )
                visited_files += 1
                if visited_files > 100_000:
                    raise NativeBundleVerificationError("native bundle has too many files")
                prefix = _read_stable_prefix(Path(entry.path), info)
                inventory.append(_inventory_entry(relative.as_posix(), "file", info))
                if _looks_native(prefix):
                    result.add(relative)
                    if len(result) > MAX_NATIVE_FILES:
                        raise NativeBundleVerificationError("native bundle has too many binaries")
    except OSError as exc:
        raise NativeBundleVerificationError("native bundle inventory is unavailable") from exc
    inventory.sort()
    return frozenset(result), tuple(inventory)


def _inventory_entry(path: str, kind: str, info: os.stat_result) -> _InventoryEntry:
    return _InventoryEntry(
        path=path,
        kind=kind,
        mode=stat.S_IMODE(info.st_mode),
        size=info.st_size,
        device=info.st_dev,
        inode=info.st_ino,
        modified_ns=info.st_mtime_ns,
        changed_ns=info.st_ctime_ns,
    )


def _read_stable_prefix(path: Path, visible_before: os.stat_result) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise NativeBundleVerificationError("native bundle file is unavailable") from exc
    try:
        before = os.fstat(descriptor)
        prefix = os.read(descriptor, 64)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        visible_after = path.lstat()
    except OSError as exc:
        raise NativeBundleVerificationError("native bundle file changed") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or not _same_file_state(visible_before, before)
        or not _same_file_state(before, after)
        or not _same_file_state(after, visible_after)
    ):
        raise NativeBundleVerificationError("native bundle file changed")
    return prefix


def _validate_safe_mode(mode: int, *, platform_target: str, directory: bool) -> None:
    if platform_target.startswith("windows-"):
        return
    if mode & (stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
        raise NativeBundleVerificationError("native bundle permissions are unsafe")
    if directory and not mode & stat.S_IXUSR:
        raise NativeBundleVerificationError("native bundle directory is not searchable")


def _validate_native_mode(mode: int, *, kind: str, platform_target: str) -> None:
    _validate_safe_mode(mode, platform_target=platform_target, directory=False)
    if not platform_target.startswith("windows-") and kind == "executable":
        if not mode & stat.S_IXUSR:
            raise NativeBundleVerificationError("native entry point is not executable")


def _same_file_state(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


def _looks_native(prefix: bytes) -> bool:
    return (
        prefix.startswith(b"\x7fELF")
        or prefix.startswith(b"MZ")
        or prefix[:4] in _MACHO_64_MAGICS
        or prefix[:4] in _OTHER_NATIVE_MAGICS
        or prefix.startswith(b"!<arch>\n")
    )


def _parse_native_metadata(
    payload: mmap.mmap,
    platform_target: str,
    *,
    kind: str,
) -> _ParsedNative:
    try:
        if platform_target.startswith("linux-"):
            dependencies, search_paths = _parse_elf64(
                payload,
                platform_target,
                kind=kind,
            )
        elif platform_target.startswith("darwin-"):
            dependencies, search_paths = _parse_macho64(
                payload,
                platform_target,
                kind=kind,
            )
        else:
            dependencies = _parse_pe64(payload, platform_target, kind=kind)
            search_paths = ()
        normalized = tuple(
            sorted(_normalize_dependency_name(value, platform_target) for value in dependencies)
        )
        if len(normalized) != len(set(normalized)):
            raise NativeBundleVerificationError("native imports are duplicated")
        if len(search_paths) != len(set(search_paths)):
            raise NativeBundleVerificationError("native search paths are duplicated")
        return _ParsedNative(dependencies=normalized, search_paths=search_paths)
    except (IndexError, OverflowError, struct.error, UnicodeDecodeError) as exc:
        raise NativeBundleVerificationError("native binary metadata is malformed") from exc
    finally:
        payload.close()


def _parse_elf64(
    data: mmap.mmap,
    platform_target: str,
    *,
    kind: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if len(data) < 64 or data[:4] != b"\x7fELF" or data[4] != 2:
        raise NativeBundleVerificationError("native ELF binary is not 64-bit")
    if data[5] != 1 or data[6] != 1:
        raise NativeBundleVerificationError("native ELF byte order is invalid")
    endian = "<"
    file_type = struct.unpack_from(endian + "H", data, 16)[0]
    if (kind == "executable" and file_type not in {2, 3}) or (
        kind == "library" and file_type != 3
    ):
        raise NativeBundleVerificationError("native ELF file type is invalid")
    machine = struct.unpack_from(endian + "H", data, 18)[0]
    expected_machine = 183 if platform_target.endswith("-arm64") else 62
    if machine != expected_machine:
        raise NativeBundleVerificationError("native ELF architecture does not match")
    phoff = struct.unpack_from(endian + "Q", data, 32)[0]
    phentsize, phnum = struct.unpack_from(endian + "HH", data, 54)
    if phentsize < 56 or not 1 <= phnum <= 4096 or phoff + phentsize * phnum > len(data):
        raise NativeBundleVerificationError("native ELF program headers are invalid")
    load_segments: list[tuple[int, int, int, int]] = []
    dynamic: tuple[int, int] | None = None
    for index in range(phnum):
        offset = phoff + index * phentsize
        p_type = struct.unpack_from(endian + "I", data, offset)[0]
        p_offset, p_vaddr, _p_paddr, p_filesz, p_memsz = struct.unpack_from(
            endian + "QQQQQ", data, offset + 8
        )
        if p_offset + p_filesz > len(data):
            raise NativeBundleVerificationError("native ELF segment is invalid")
        if p_type == 1:
            load_segments.append((p_vaddr, p_memsz, p_offset, p_filesz))
        elif p_type == 2:
            if dynamic is not None:
                raise NativeBundleVerificationError("native ELF dynamic table is ambiguous")
            dynamic = (p_offset, p_filesz)
    if dynamic is None:
        return (), ()
    dyn_offset, dyn_size = dynamic
    if dyn_size % 16 != 0 or dyn_size // 16 > 65536:
        raise NativeBundleVerificationError("native ELF dynamic table is invalid")
    needed_offsets: list[int] = []
    strtab_vaddr: int | None = None
    strtab_size: int | None = None
    rpath_offsets: list[int] = []
    runpath_offsets: list[int] = []
    terminated = False
    for offset in range(dyn_offset, dyn_offset + dyn_size, 16):
        tag, value = struct.unpack_from(endian + "qQ", data, offset)
        if tag == 0:
            terminated = True
            break
        if tag == 1:
            needed_offsets.append(value)
        elif tag == 5:
            strtab_vaddr = value
        elif tag == 10:
            strtab_size = value
        elif tag == 15:
            rpath_offsets.append(value)
        elif tag == 29:
            runpath_offsets.append(value)
    if not terminated or strtab_vaddr is None or strtab_size is None or strtab_size > len(data):
        raise NativeBundleVerificationError("native ELF string table is invalid")
    strtab_offset = _elf_vaddr_to_offset(strtab_vaddr, strtab_size, load_segments)
    all_offsets = [*needed_offsets, *rpath_offsets, *runpath_offsets]
    if any(value >= strtab_size for value in all_offsets):
        raise NativeBundleVerificationError("native ELF dependency offset is invalid")
    dependencies = tuple(
        _bounded_c_string(data, strtab_offset + needed, strtab_offset + strtab_size)
        for needed in needed_offsets
    )
    selected_search_offsets = runpath_offsets or rpath_offsets
    if len(selected_search_offsets) > 1:
        raise NativeBundleVerificationError("native ELF search path is ambiguous")
    search_paths: tuple[str, ...] = ()
    if selected_search_offsets:
        raw_search_path = _bounded_c_string(
            data,
            strtab_offset + selected_search_offsets[0],
            strtab_offset + strtab_size,
        )
        search_paths = tuple(raw_search_path.split(":"))
        if not search_paths or any(not value for value in search_paths):
            raise NativeBundleVerificationError("native ELF search path is invalid")
    return dependencies, search_paths


def _elf_vaddr_to_offset(
    address: int,
    size: int,
    segments: Sequence[tuple[int, int, int, int]],
) -> int:
    matches: list[int] = []
    for virtual, memory_size, file_offset, file_size in segments:
        if virtual <= address and address + size <= virtual + memory_size:
            relative = address - virtual
            if relative + size <= file_size:
                matches.append(file_offset + relative)
    if len(matches) != 1:
        raise NativeBundleVerificationError("native ELF string table is unresolved")
    return matches[0]


def _parse_macho64(
    data: mmap.mmap,
    platform_target: str,
    *,
    kind: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if len(data) < 32:
        raise NativeBundleVerificationError("native Mach-O binary is truncated")
    endian = _MACHO_64_MAGICS.get(bytes(data[:4]))
    if endian != "<":
        raise NativeBundleVerificationError("native Mach-O binary is not thin 64-bit")
    cpu_type, _cpu_subtype, file_type, command_count, command_bytes = struct.unpack_from(
        endian + "iiIII", data, 4
    )
    expected_cpu = 0x0100000C if platform_target.endswith("-arm64") else 0x01000007
    if cpu_type != expected_cpu:
        raise NativeBundleVerificationError("native Mach-O architecture does not match")
    if (kind == "executable" and file_type != 2) or (
        kind == "library" and file_type not in {6, 8}
    ):
        raise NativeBundleVerificationError("native Mach-O file type is invalid")
    if command_count > 65536 or 32 + command_bytes > len(data):
        raise NativeBundleVerificationError("native Mach-O load commands are invalid")
    dependencies: list[str] = []
    search_paths: list[str] = []
    offset = 32
    end = 32 + command_bytes
    for _index in range(command_count):
        if offset + 8 > end:
            raise NativeBundleVerificationError("native Mach-O load command is truncated")
        command, command_size = struct.unpack_from(endian + "II", data, offset)
        if command_size < 8 or offset + command_size > end:
            raise NativeBundleVerificationError("native Mach-O load command is invalid")
        if command in _MACHO_LOAD_DYLIB_COMMANDS:
            if command_size < 24:
                raise NativeBundleVerificationError("native Mach-O dylib command is invalid")
            name_offset = struct.unpack_from(endian + "I", data, offset + 8)[0]
            if name_offset < 24 or name_offset >= command_size:
                raise NativeBundleVerificationError("native Mach-O dylib name is invalid")
            dependencies.append(
                _bounded_c_string(data, offset + name_offset, offset + command_size)
            )
        elif command == (0x0000001C | 0x80000000):  # LC_RPATH
            if command_size < 12:
                raise NativeBundleVerificationError("native Mach-O rpath is invalid")
            path_offset = struct.unpack_from(endian + "I", data, offset + 8)[0]
            if path_offset < 12 or path_offset >= command_size:
                raise NativeBundleVerificationError("native Mach-O rpath is invalid")
            search_paths.append(
                _bounded_c_string(data, offset + path_offset, offset + command_size)
            )
        offset += command_size
    if offset != end:
        raise NativeBundleVerificationError("native Mach-O command size does not match")
    return tuple(dependencies), tuple(search_paths)


def _parse_pe64(
    data: mmap.mmap,
    platform_target: str,
    *,
    kind: str,
) -> tuple[str, ...]:
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise NativeBundleVerificationError("native PE binary has no DOS header")
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if pe_offset + 24 > len(data) or data[pe_offset : pe_offset + 4] != b"PE\x00\x00":
        raise NativeBundleVerificationError("native PE signature is invalid")
    machine, section_count = struct.unpack_from("<HH", data, pe_offset + 4)
    expected_machine = 0xAA64 if platform_target.endswith("-arm64") else 0x8664
    if machine != expected_machine or not 1 <= section_count <= 4096:
        raise NativeBundleVerificationError("native PE architecture does not match")
    characteristics = struct.unpack_from("<H", data, pe_offset + 22)[0]
    is_library = bool(characteristics & 0x2000)
    if is_library != (kind == "library"):
        raise NativeBundleVerificationError("native PE file type is invalid")
    optional_size = struct.unpack_from("<H", data, pe_offset + 20)[0]
    optional_offset = pe_offset + 24
    if optional_size < 112 or optional_offset + optional_size > len(data):
        raise NativeBundleVerificationError("native PE optional header is invalid")
    if struct.unpack_from("<H", data, optional_offset)[0] != 0x20B:
        raise NativeBundleVerificationError("native PE binary is not PE32+")
    directory_count = struct.unpack_from("<I", data, optional_offset + 108)[0]
    section_offset = optional_offset + optional_size
    if section_offset + section_count * 40 > len(data):
        raise NativeBundleVerificationError("native PE section table is invalid")
    sections: list[tuple[int, int, int, int]] = []
    for index in range(section_count):
        offset = section_offset + index * 40
        virtual_size, virtual_address, raw_size, raw_offset = struct.unpack_from(
            "<IIII", data, offset + 8
        )
        if raw_offset + raw_size > len(data):
            raise NativeBundleVerificationError("native PE section is invalid")
        sections.append((virtual_address, virtual_size, raw_offset, raw_size))

    dependencies: list[str] = []
    if directory_count > 1 and optional_size >= 128:
        import_rva, import_size = struct.unpack_from("<II", data, optional_offset + 120)
        if import_rva:
            dependencies.extend(
                _parse_pe_import_directory(data, import_rva, import_size, sections)
            )
    if directory_count > 13 and optional_size >= 224:
        delay_rva, delay_size = struct.unpack_from("<II", data, optional_offset + 216)
        if delay_rva:
            dependencies.extend(
                _parse_pe_delay_directory(data, delay_rva, delay_size, sections)
            )
    return tuple(dependencies)


def _parse_pe_import_directory(
    data: mmap.mmap,
    rva: int,
    size: int,
    sections: Sequence[tuple[int, int, int, int]],
) -> tuple[str, ...]:
    if not 20 <= size <= 20 * 65536:
        raise NativeBundleVerificationError("native PE import directory is invalid")
    offset = _pe_rva_to_offset(rva, min(size, 20), sections)
    end = offset + size
    if end > len(data):
        raise NativeBundleVerificationError("native PE import directory is invalid")
    result: list[str] = []
    terminated = False
    while offset + 20 <= end:
        descriptor = struct.unpack_from("<IIIII", data, offset)
        if descriptor == (0, 0, 0, 0, 0):
            terminated = True
            break
        name_rva = descriptor[3]
        name_offset = _pe_rva_to_offset(name_rva, 1, sections)
        result.append(_bounded_c_string(data, name_offset, len(data)))
        offset += 20
    if not terminated:
        raise NativeBundleVerificationError("native PE import directory is unterminated")
    return tuple(result)


def _parse_pe_delay_directory(
    data: mmap.mmap,
    rva: int,
    size: int,
    sections: Sequence[tuple[int, int, int, int]],
) -> tuple[str, ...]:
    if not 32 <= size <= 32 * 65536:
        raise NativeBundleVerificationError("native PE delay imports are invalid")
    offset = _pe_rva_to_offset(rva, min(size, 32), sections)
    end = offset + size
    if end > len(data):
        raise NativeBundleVerificationError("native PE delay imports are invalid")
    result: list[str] = []
    terminated = False
    while offset + 32 <= end:
        descriptor = struct.unpack_from("<IIIIIIII", data, offset)
        if descriptor == (0, 0, 0, 0, 0, 0, 0, 0):
            terminated = True
            break
        attributes, name_value = descriptor[:2]
        if attributes & 1 == 0:
            raise NativeBundleVerificationError("native PE delay import is not RVA based")
        name_offset = _pe_rva_to_offset(name_value, 1, sections)
        result.append(_bounded_c_string(data, name_offset, len(data)))
        offset += 32
    if not terminated:
        raise NativeBundleVerificationError("native PE delay imports are unterminated")
    return tuple(result)


def _pe_rva_to_offset(
    rva: int,
    size: int,
    sections: Sequence[tuple[int, int, int, int]],
) -> int:
    matches: list[int] = []
    for virtual, virtual_size, raw, raw_size in sections:
        span = max(virtual_size, raw_size)
        if virtual <= rva and rva + size <= virtual + span:
            relative = rva - virtual
            if relative + size <= raw_size:
                matches.append(raw + relative)
    if len(matches) != 1:
        raise NativeBundleVerificationError("native PE RVA is unresolved")
    return matches[0]


def _bounded_c_string(data: mmap.mmap, offset: int, end: int) -> str:
    if offset < 0 or offset >= end or end > len(data):
        raise NativeBundleVerificationError("native dependency string is invalid")
    terminator = data.find(b"\x00", offset, min(end, offset + 1025))
    if terminator < 0 or terminator == offset:
        raise NativeBundleVerificationError("native dependency string is invalid")
    try:
        return bytes(data[offset:terminator]).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NativeBundleVerificationError("native dependency string is invalid") from exc


def _validate_private_resolution(
    *,
    importer: PurePosixPath,
    dependency: _Dependency,
    manifest_paths: frozenset[PurePosixPath],
    search_paths: tuple[str, ...],
    platform_target: str,
) -> None:
    assert dependency.path is not None
    if platform_target.startswith("windows-"):
        if (
            "/" in dependency.name
            or "\\" in dependency.name
            or dependency.path.name.casefold() != dependency.name
            or dependency.path.parent != importer.parent
        ):
            raise NativeBundleVerificationError(
                "native private dependency resolution does not match"
            )
        return

    if platform_target.startswith("linux-"):
        if "/" in dependency.name or "\\" in dependency.name:
            raise NativeBundleVerificationError(
                "native private dependency resolution does not match"
            )
        directories = tuple(
            _expand_origin_search_path(importer.parent, value, token="$ORIGIN")
            for value in search_paths
        )
        candidates = tuple(
            directory / dependency.name
            for directory in directories
            if directory / dependency.name in manifest_paths
        )
    else:
        # Every LC_RPATH is constrained even when a particular import uses
        # @loader_path directly.  This prevents an unused host rpath from later
        # becoming an unreviewed resolution path after a binary update.
        directories = tuple(
            _expand_origin_search_path(importer.parent, value, token="@loader_path")
            for value in search_paths
        )
        loader_prefix = "@loader_path/"
        rpath_prefix = "@rpath/"
        if dependency.name.startswith(loader_prefix):
            candidates = (
                _join_normalized(
                    importer.parent,
                    dependency.name[len(loader_prefix) :],
                ),
            )
        elif dependency.name.startswith(rpath_prefix):
            suffix = dependency.name[len(rpath_prefix) :]
            candidates = tuple(
                _join_normalized(directory, suffix)
                for directory in directories
                if _join_normalized(directory, suffix) in manifest_paths
            )
        else:
            raise NativeBundleVerificationError(
                "native private dependency resolution does not match"
            )

    if candidates != (dependency.path,):
        raise NativeBundleVerificationError(
            "native private dependency resolution does not match"
        )


def _validate_search_paths(
    *,
    importer: PurePosixPath,
    search_paths: tuple[str, ...],
    platform_target: str,
) -> None:
    if platform_target.startswith("windows-"):
        if search_paths:
            raise NativeBundleVerificationError("native search path is invalid")
        return
    token = "$ORIGIN" if platform_target.startswith("linux-") else "@loader_path"
    for value in search_paths:
        _expand_origin_search_path(importer.parent, value, token=token)


def _expand_origin_search_path(
    importer_directory: PurePosixPath,
    value: str,
    *,
    token: str,
) -> PurePosixPath:
    alternatives = (token, "${ORIGIN}") if token == "$ORIGIN" else (token,)
    for prefix in alternatives:
        if value == prefix:
            return importer_directory
        if value.startswith(prefix + "/"):
            return _join_normalized(importer_directory, value[len(prefix) + 1 :])
    raise NativeBundleVerificationError("native search path leaves the private bundle")


def _join_normalized(base: PurePosixPath, suffix: str) -> PurePosixPath:
    if not suffix or "\\" in suffix or suffix.startswith("/"):
        raise NativeBundleVerificationError("native search path is invalid")
    parts = list(base.parts)
    for part in suffix.split("/"):
        if part in {"", "."}:
            if part == "":
                raise NativeBundleVerificationError("native search path is invalid")
            continue
        if part == "..":
            if not parts:
                raise NativeBundleVerificationError(
                    "native search path leaves the private bundle"
                )
            parts.pop()
            continue
        if "\x00" in part:
            raise NativeBundleVerificationError("native search path is invalid")
        parts.append(part)
    return PurePosixPath(*parts)


def _normalize_dependency_name(name: str, platform_target: str) -> str:
    return name.casefold() if platform_target.startswith("windows-") else name


def _allowed_system_dependency(name: str, platform_target: str) -> bool:
    if platform_target.startswith("linux-"):
        return name in _LINUX_SYSTEM_DEPENDENCIES
    if platform_target.startswith("darwin-"):
        return name in _DARWIN_SYSTEM_DEPENDENCIES
    return name in _WINDOWS_SYSTEM_DEPENDENCIES or name.startswith(
        _WINDOWS_SYSTEM_PREFIXES
    )


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


__all__ = [
    "DEPENDENCY_MANIFEST_FILENAME",
    "DEPENDENCY_MANIFEST_SCHEMA_VERSION",
    "NativeBundleClosure",
    "NativeBundleVerificationError",
    "verify_native_bundle",
]
