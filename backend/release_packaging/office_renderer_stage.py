"""Fail-closed PyInstaller admission for an Office renderer profile.

The public repository intentionally contains no private renderer binaries. A
signed-authoritative v1.1+ build must receive one target-specific source bundle
through the release staging script; an explicit unsigned-degraded build must
receive none. This module revalidates the immutable lock and payload during
spec evaluation so a stale, edited, multi-platform, or ambient source tree
cannot silently enter either frozen-backend profile.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import ntpath
import os
from pathlib import Path
import platform
import posixpath
import re
import shutil
import stat
from typing import Any, Iterable, MutableSequence, Sequence
import unicodedata

from release_packaging.office_renderer_trust import (
    OfficeRendererTrustError,
    verify_office_renderer_attestation_signature,
)
from release_packaging.release_identity import ReleaseIdentityValues


LOCK_FILENAME = "office-renderer.lock.json"
STAGE_FILENAME = "office-renderer-stage.json"
SIGNED_AUTHORITATIVE_PROFILE = "signed-authoritative"
UNSIGNED_DEGRADED_PROFILE = "unsigned-degraded"
OFFICE_RENDERER_PROFILE_ENV = "SUXIAOYOU_OFFICE_RENDERER_PROFILE"
PAYLOAD_CONTRACT = "final-native-bytes-attested-after-signing-v1"
SCHEMA_VERSION = 1
SUPPORTED_TARGETS = frozenset(
    {
        "darwin-arm64",
        "darwin-x64",
        "linux-arm64",
        "linux-x64",
        "windows-x64",
    }
)
COMMON_REQUIRED_FILES = frozenset(
    {
        "dependency-manifest.json",
        "font-manifest.json",
        "license-manifest.json",
        "office-renderer-attestation.json",
        "probe/authoritative-renderer-probe.docx",
        "probe/authoritative-renderer-probe.json",
        "sandbox-manifest.json",
    }
)
SHA256_RE = re.compile(r"^(?!0{64}$)[0-9a-f]{64}$")
VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
MAX_LOCK_BYTES = 4 * 1024 * 1024
MAX_STAGE_BYTES = 64 * 1024
MAX_ATTESTATION_BYTES = 64 * 1024
MAX_FILE_BYTES = 1024 * 1024 * 1024
MAX_PAYLOAD_BYTES = 8 * 1024 * 1024 * 1024
MAX_FILES = 100_000
WINDOWS_RESERVED_DEVICE_RE = re.compile(
    r"^(?:aux|clock\$|com[1-9¹²³]|con|conin\$|conout\$|lpt[1-9¹²³]|nul|prn)$",
    re.IGNORECASE,
)
WINDOWS_FORBIDDEN_DESTINATION_CHARACTERS = frozenset('<>:"|?*')


def _executable_paths(target: str) -> dict[str, str]:
    if target.startswith("windows-"):
        return {"pdftoppm": "bin/pdftoppm.exe", "soffice": "bin/soffice.exe"}
    return {"pdftoppm": "bin/pdftoppm", "soffice": "bin/soffice"}


def _sandbox_contract(target: str) -> tuple[str, str, tuple[str, ...]]:
    family = target.split("-", 1)[0]
    if family == "darwin":
        return (
            "suxiaoyou.office-sandbox.macos-app-sandbox-xpc.v1",
            "bin/suxiaoyou-office-sandbox-launcher",
            (
                "app_sandbox",
                "host_filesystem_read_only",
                "network_denied",
                "private_input_read_only",
                "private_output_write_only",
                "process_tree_contained",
                "xpc_service",
            ),
        )
    if family == "windows":
        return (
            "suxiaoyou.office-sandbox.windows-appcontainer-restricted-token.v1",
            "bin/suxiaoyou-office-sandbox-launcher.exe",
            (
                "app_container",
                "host_filesystem_read_only",
                "kill_on_close_job",
                "network_denied",
                "private_input_read_only",
                "private_output_write_only",
                "process_tree_contained",
                "restricted_token",
            ),
        )
    return (
        "suxiaoyou.office-sandbox.linux-namespaces-seccomp-cgroup.v1",
        "bin/suxiaoyou-office-sandbox-launcher",
        (
            "cgroup",
            "host_filesystem_read_only",
            "mount_namespace",
            "network_denied",
            "network_namespace",
            "private_input_read_only",
            "private_output_write_only",
            "process_tree_contained",
            "seccomp",
            "user_namespace",
        ),
    )


def _sandbox_probe_path(target: str) -> str:
    if target.startswith("windows-"):
        return "bin/suxiaoyou-office-sandbox-probe.exe"
    return "bin/suxiaoyou-office-sandbox-probe"


class OfficeRendererPackagingError(RuntimeError):
    """The selected renderer staging tree is not a releasable build input."""


@dataclass(frozen=True, slots=True)
class OfficeRendererSnapshotDirectory:
    """One immutable directory identity in the private PyInstaller snapshot."""

    relative_path: str
    mode: int
    device: int
    inode: int
    link_count: int
    size: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True, slots=True)
class OfficeRendererSnapshotFile:
    """One digest-bound source file and its exact frozen-bundle destination."""

    relative_path: str
    source_path: str
    destination_path: str
    locked_mode: int
    snapshot_mode: int
    sha256: str
    size: int
    device: int
    inode: int
    link_count: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True, slots=True)
class OfficeRendererAssets:
    """The complete renderer source identity admitted to PyInstaller Analysis."""

    snapshot_root: str | None
    destination_root: str
    payload_tree_sha256: str | None
    directories: tuple[OfficeRendererSnapshotDirectory, ...]
    files: tuple[OfficeRendererSnapshotFile, ...]
    datas: tuple[tuple[str, str], ...]


def _fail(message: str) -> None:
    raise OfficeRendererPackagingError(message)


def _exact_keys(value: object, expected: tuple[str, ...], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or tuple(value) != expected:
        _fail(f"{label} fields and order must be exactly: {', '.join(expected)}")
    return value


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _read_regular(path: Path, *, label: str, max_bytes: int) -> tuple[bytes, int]:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise OfficeRendererPackagingError(f"{label} is unavailable") from exc
    digest_input = bytearray()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            _fail(f"{label} must be a bounded regular file")
        total = 0
        while chunk := os.read(descriptor, 1024 * 1024):
            total += len(chunk)
            if total > max_bytes:
                _fail(f"{label} exceeds its byte limit")
            digest_input.extend(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        visible = path.lstat()
    except OSError as exc:
        raise OfficeRendererPackagingError(f"{label} changed while reading") from exc
    identity = lambda item: (  # noqa: E731 - compact race identity used twice
        item.st_dev,
        item.st_ino,
        item.st_mode,
        item.st_size,
        item.st_mtime_ns,
        item.st_ctime_ns,
    )
    if total != before.st_size or identity(before) != identity(after) or identity(after) != identity(visible):
        _fail(f"{label} changed while reading")
    return bytes(digest_input), stat.S_IMODE(before.st_mode)


def _read_json(path: Path, *, label: str, max_bytes: int) -> tuple[dict[str, Any], bytes, int]:
    raw, mode = _read_regular(path, label=label, max_bytes=max_bytes)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise OfficeRendererPackagingError(f"{label} must be unambiguous UTF-8 JSON") from exc
    if not isinstance(value, dict):
        _fail(f"{label} must be a JSON object")
    return value, raw, mode


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path, *, expected_size: int) -> tuple[str, int]:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise OfficeRendererPackagingError("renderer payload file is unavailable") from exc
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
            _fail("renderer payload file is not a bounded regular file")
        total = 0
        while chunk := os.read(descriptor, 1024 * 1024):
            total += len(chunk)
            if total > MAX_FILE_BYTES:
                _fail("renderer payload file exceeds its byte limit")
            digest.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        visible = path.lstat()
    except OSError as exc:
        raise OfficeRendererPackagingError("renderer payload changed while reading") from exc
    identity = lambda item: (  # noqa: E731 - compact race identity used twice
        item.st_dev,
        item.st_ino,
        item.st_mode,
        item.st_size,
        item.st_mtime_ns,
        item.st_ctime_ns,
    )
    if (
        total != expected_size
        or total != before.st_size
        or identity(before) != identity(after)
        or identity(after) != identity(visible)
    ):
        _fail("renderer payload file changed or does not match its locked size")
    return digest.hexdigest(), stat.S_IMODE(before.st_mode)


def _safe_mode(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 0o777:
        _fail(f"{label} is not a valid permission mode")
    return value


def _relative_path(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or value != value.strip()
        or value.startswith("/")
        or value.endswith("/")
        or "\\" in value
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        _fail(f"{label} is not a canonical relative POSIX path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        _fail(f"{label} is not a canonical relative POSIX path")
    return value


def _tree_digest(files: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for item in files:
        canonical = {
            "mode": item["mode"],
            "path": item["path"],
            "sha256": item["sha256"],
            "size": item["size"],
        }
        digest.update(
            json.dumps(
                canonical,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _deployment_tree_digest(files: list[dict[str, Any]]) -> str:
    """Match the runtime bundle fingerprint, excluding its attestation."""

    digest = hashlib.sha256()
    for item in files:
        if item["path"] == "office-renderer-attestation.json":
            continue
        canonical = {
            "mode": item["mode"],
            "path": item["path"],
            "sha256": item["sha256"],
            "size": item["size"],
        }
        digest.update(
            json.dumps(
                canonical,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _validate_lock(value: dict[str, Any], target: str) -> dict[str, Any]:
    lock = _exact_keys(
        value,
        (
            "schema_version",
            "platform_target",
            "payload_contract",
            "payload_root_mode",
            "directories",
            "files",
            "payload_tree_sha256",
        ),
        "renderer lock",
    )
    if lock["schema_version"] != SCHEMA_VERSION or lock["platform_target"] != target:
        _fail("renderer lock schema or target does not match this native build")
    if lock["payload_contract"] != PAYLOAD_CONTRACT:
        _fail(
            "renderer lock does not bind final native bytes; macOS nested code signing "
            "must finish before attestation and lock creation"
        )
    root_mode = _safe_mode(lock["payload_root_mode"], "renderer payload root mode")
    raw_directories = lock["directories"]
    raw_files = lock["files"]
    if not isinstance(raw_directories, list):
        _fail("renderer lock directories must be an array")
    if not isinstance(raw_files, list) or not 1 <= len(raw_files) <= MAX_FILES:
        _fail("renderer lock file count is invalid")
    directories: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_directories):
        entry = _exact_keys(raw, ("mode", "path"), f"renderer directory {index}")
        directories.append(
            {
                "mode": _safe_mode(entry["mode"], f"renderer directory {index} mode"),
                "path": _relative_path(entry["path"], f"renderer directory {index} path"),
            }
        )
    files: list[dict[str, Any]] = []
    total_bytes = 0
    for index, raw in enumerate(raw_files):
        entry = _exact_keys(raw, ("mode", "path", "sha256", "size"), f"renderer file {index}")
        digest = entry["sha256"]
        size = entry["size"]
        if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
            _fail(f"renderer file {index} digest is invalid")
        if not isinstance(size, int) or isinstance(size, bool) or not 0 <= size <= MAX_FILE_BYTES:
            _fail(f"renderer file {index} size is invalid")
        total_bytes += size
        files.append(
            {
                "mode": _safe_mode(entry["mode"], f"renderer file {index} mode"),
                "path": _relative_path(entry["path"], f"renderer file {index} path"),
                "sha256": digest,
                "size": size,
            }
        )
    if total_bytes > MAX_PAYLOAD_BYTES:
        _fail("renderer lock payload exceeds its byte limit")
    for entries, label in ((directories, "directory"), (files, "file")):
        paths = [entry["path"] for entry in entries]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            _fail(f"renderer lock {label} paths must be unique and sorted")
    directory_paths = {entry["path"] for entry in directories}
    file_paths = {entry["path"] for entry in files}
    if directory_paths.intersection(file_paths):
        _fail("renderer lock aliases a file and directory")
    for path in directory_paths.union(file_paths):
        parts = path.split("/")
        for index in range(1, len(parts)):
            if "/".join(parts[:index]) not in directory_paths:
                _fail("renderer lock omits a parent directory")
    executable_paths = _executable_paths(target)
    _contract_id, launcher_path, _capabilities = _sandbox_contract(target)
    sandbox_probe_path = _sandbox_probe_path(target)
    required_files = COMMON_REQUIRED_FILES.union(
        executable_paths.values(),
        {launcher_path, sandbox_probe_path},
    )
    if not required_files.issubset(file_paths):
        _fail("renderer lock omits a required runtime component")
    if not any(re.fullmatch(r"fonts/.+\.(?:otf|ttc|ttf)", path, re.IGNORECASE) for path in file_paths):
        _fail("renderer lock contains no bundled Office font")
    locked_tree = lock["payload_tree_sha256"]
    if not isinstance(locked_tree, str) or SHA256_RE.fullmatch(locked_tree) is None:
        _fail("renderer lock payload tree digest is invalid")
    if _tree_digest(files) != locked_tree:
        _fail("renderer lock payload tree digest does not match its file identities")
    locked_by_path = {entry["path"]: entry for entry in files}
    required_executables = (
        *executable_paths.values(),
        launcher_path,
        sandbox_probe_path,
    )
    if any(locked_by_path[path]["size"] == 0 for path in required_executables):
        _fail("renderer lock executable components must not be empty")
    if not target.startswith("windows-"):
        payload_modes = [root_mode]
        payload_modes.extend(entry["mode"] for entry in directories)
        payload_modes.extend(entry["mode"] for entry in files)
        if any(mode & 0o022 for mode in payload_modes):
            _fail("renderer lock contains group- or world-writable payload modes")
        if any(
            locked_by_path[path]["mode"] & 0o111 == 0
            for path in required_executables
        ):
            _fail("renderer lock executable components are not executable")
        if (
            root_mode != 0o755
            or any(entry["mode"] != 0o755 for entry in directories)
            or any(entry["mode"] not in {0o644, 0o755} for entry in files)
        ):
            _fail(
                "renderer lock POSIX modes must be canonical 0755 directories "
                "and 0644/0755 files"
            )
    return {
        "directories": directories,
        "files": files,
        "payload_root_mode": root_mode,
        "payload_tree_sha256": locked_tree,
        "total_bytes": total_bytes,
    }


def _directory(path: Path, label: str) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise OfficeRendererPackagingError(f"{label} is unavailable") from exc
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
        _fail(f"{label} must be a real directory")
    return info


def _root_entries(path: Path, label: str) -> list[str]:
    _directory(path, label)
    try:
        return sorted(child.name for child in path.iterdir())
    except OSError as exc:
        raise OfficeRendererPackagingError(f"{label} cannot be listed") from exc


def _inventory_payload(root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    directories: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    pending = [(root, "")]
    while pending:
        directory, prefix = pending.pop()
        for name in _root_entries(directory, "renderer payload directory"):
            if not name or name in {".", ".."} or "/" in name or "\\" in name:
                _fail("renderer payload contains a non-canonical path")
            child = directory / name
            relative = f"{prefix}/{name}" if prefix else name
            info = child.lstat()
            if child.is_symlink():
                _fail("renderer payload contains a symbolic link")
            if stat.S_ISDIR(info.st_mode):
                directories.append({"mode": stat.S_IMODE(info.st_mode), "path": relative})
                pending.append((child, relative))
            elif stat.S_ISREG(info.st_mode):
                files.append(
                    {
                        "mode": stat.S_IMODE(info.st_mode),
                        "path": relative,
                        "size": info.st_size,
                    }
                )
            else:
                _fail("renderer payload contains a special file")
            if len(files) > MAX_FILES:
                _fail("renderer payload contains too many files")
    directories.sort(key=lambda item: item["path"])
    files.sort(key=lambda item: item["path"])
    return directories, files


def _native_target() -> str:
    system = {"darwin": "darwin", "linux": "linux", "win32": "windows"}.get(os.sys.platform)
    machine = {
        "amd64": "x64",
        "x86_64": "x64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(platform.machine().lower())
    if system is None or machine is None:
        _fail("this build host is not a supported Office renderer target")
    return f"{system}-{machine}"


def _application_version(repo_root: Path) -> str:
    package_path = repo_root / "package.json"
    value, _raw, _mode = _read_json(package_path, label="package.json", max_bytes=1024 * 1024)
    version = value.get("version")
    match = VERSION_RE.fullmatch(version) if isinstance(version, str) else None
    if match is None:
        _fail("package.json version is not an X.Y.Z release version")
    return version


def prepare_office_renderer_assets(
    *,
    app_dir: str,
    repo_root: str,
    work_root: str | None = None,
    release_identity: ReleaseIdentityValues | None = None,
) -> OfficeRendererAssets:
    """Return a digest-bound private renderer snapshot for this native build.

    The authenticated staging directory is an external build input and can be
    mutable even after its lock has been admitted.  PyInstaller consumes data
    entries after this function returns, so returning that directory directly
    would leave both a validation-to-copy race and an unreviewed directory
    expansion inside Analysis.  v1.1+ therefore copies every locked byte into
    a newly created private work directory, locks it read-only, and returns a
    complete per-file source/destination identity for later Analysis checks.
    """

    app_data_renderer = Path(app_dir) / "data" / "office-renderer"
    if os.path.lexists(app_data_renderer):
        _fail(
            "backend/app/data/office-renderer is forbidden; use the immutable external staging chain"
        )
    app_version = _application_version(Path(repo_root))
    version_parts = tuple(int(part) for part in app_version.split("."))
    required = version_parts >= (1, 1, 0)
    configured_required = os.environ.get("SUXIAOYOU_V11_OFFICE_RENDERER_REQUIRED")
    expected_required = "1" if required else "0"
    if configured_required is not None and configured_required != expected_required:
        _fail(
            f"SUXIAOYOU_V11_OFFICE_RENDERER_REQUIRED must be {expected_required} for this app version"
        )
    stage_value = os.environ.get("SUXIAOYOU_OFFICE_RENDERER_STAGE", "")
    target = os.environ.get("SUXIAOYOU_OFFICE_RENDERER_TARGET", "")
    expected_lock_sha256 = os.environ.get("SUXIAOYOU_OFFICE_RENDERER_LOCK_SHA256", "")
    if not required:
        configured_profile = os.environ.get(OFFICE_RENDERER_PROFILE_ENV)
        if configured_profile:
            _fail("pre-v1.1 builds must not select an Office renderer profile")
        if any((stage_value, target, expected_lock_sha256)):
            _fail("pre-v1.1 builds must not receive an Office renderer staging configuration")
        return OfficeRendererAssets(
            snapshot_root=None,
            destination_root="app/data/office-renderer",
            payload_tree_sha256=None,
            directories=(),
            files=(),
            datas=(),
        )
    profile = os.environ.get(
        OFFICE_RENDERER_PROFILE_ENV,
        SIGNED_AUTHORITATIVE_PROFILE,
    )
    if profile not in {SIGNED_AUTHORITATIVE_PROFILE, UNSIGNED_DEGRADED_PROFILE}:
        _fail(
            f"{OFFICE_RENDERER_PROFILE_ENV} must be "
            f"{SIGNED_AUTHORITATIVE_PROFILE} or {UNSIGNED_DEGRADED_PROFILE}"
        )
    if (
        not isinstance(release_identity, ReleaseIdentityValues)
        or release_identity.app_version != app_version
    ):
        _fail(
            "v1.1+ Office renderer packaging requires the frozen checkout "
            "release identity"
        )
    if profile == UNSIGNED_DEGRADED_PROFILE:
        if any((stage_value, target, expected_lock_sha256)):
            _fail(
                "unsigned-degraded Office packaging must not receive renderer "
                "stage, target, or lock inputs"
            )
        return OfficeRendererAssets(
            snapshot_root=None,
            destination_root="app/data/office-renderer",
            payload_tree_sha256=None,
            directories=(),
            files=(),
            datas=(),
        )
    if not stage_value or not target or not expected_lock_sha256:
        _fail(
            "v1.1+ requires SUXIAOYOU_OFFICE_RENDERER_STAGE, "
            "SUXIAOYOU_OFFICE_RENDERER_TARGET, and "
            "SUXIAOYOU_OFFICE_RENDERER_LOCK_SHA256 from a real native renderer source"
        )
    if target not in SUPPORTED_TARGETS or target != _native_target():
        _fail("Office renderer target does not match this native PyInstaller host")
    if SHA256_RE.fullmatch(expected_lock_sha256) is None:
        _fail("Office renderer lock digest is invalid")
    stage = Path(stage_value)
    if not stage.is_absolute():
        _fail("Office renderer staging path must be absolute")
    _directory(stage, "Office renderer staging root")
    if _root_entries(stage, "Office renderer staging root") != sorted(
        [LOCK_FILENAME, STAGE_FILENAME, "payload"]
    ):
        _fail("Office renderer staging root contains undeclared entries")
    lock_value, lock_raw, _lock_mode = _read_json(
        stage / LOCK_FILENAME,
        label="Office renderer lock",
        max_bytes=MAX_LOCK_BYTES,
    )
    if _sha256_bytes(lock_raw) != expected_lock_sha256:
        _fail("Office renderer staging lock does not match the release-configured digest")
    lock = _validate_lock(lock_value, target)
    stage_manifest, _manifest_raw, _manifest_mode = _read_json(
        stage / STAGE_FILENAME,
        label="Office renderer stage manifest",
        max_bytes=MAX_STAGE_BYTES,
    )
    _exact_keys(
        stage_manifest,
        (
            "schema_version",
            "platform_target",
            "payload_contract",
            "lock_sha256",
            "payload_tree_sha256",
            "payload_root_mode",
            "directory_count",
            "file_count",
            "total_bytes",
        ),
        "Office renderer stage manifest",
    )
    expected_manifest = {
        "schema_version": SCHEMA_VERSION,
        "platform_target": target,
        "payload_contract": PAYLOAD_CONTRACT,
        "lock_sha256": expected_lock_sha256,
        "payload_tree_sha256": lock["payload_tree_sha256"],
        "payload_root_mode": lock["payload_root_mode"],
        "directory_count": len(lock["directories"]),
        "file_count": len(lock["files"]),
        "total_bytes": lock["total_bytes"],
    }
    if stage_manifest != expected_manifest:
        _fail("Office renderer stage manifest does not match its immutable lock")
    payload_container = stage / "payload"
    if _root_entries(payload_container, "Office renderer payload container") != [target]:
        _fail("Office renderer staging must contain exactly one native target")
    payload = payload_container / target
    payload_info = _directory(payload, "Office renderer selected payload")
    if stat.S_IMODE(payload_info.st_mode) != lock["payload_root_mode"]:
        _fail("Office renderer payload root mode does not match its lock")
    actual_directories, actual_files = _inventory_payload(payload)
    if actual_directories != lock["directories"]:
        _fail("Office renderer directory set or modes do not match the lock")
    locked_files = {entry["path"]: entry for entry in lock["files"]}
    executable_paths = _executable_paths(target)
    if [entry["path"] for entry in actual_files] != list(locked_files):
        _fail("Office renderer file set does not match the lock")
    verified_files: list[dict[str, Any]] = []
    for actual in actual_files:
        locked = locked_files[actual["path"]]
        path = payload.joinpath(*actual["path"].split("/"))
        digest, read_mode = _sha256_file(path, expected_size=locked["size"])
        verified = {
            "mode": read_mode,
            "path": actual["path"],
            "sha256": digest,
            "size": actual["size"],
        }
        if verified != locked:
            _fail("Office renderer payload bytes or modes do not match the lock")
        verified_files.append(verified)
    if _tree_digest(verified_files) != lock["payload_tree_sha256"]:
        _fail("Office renderer verified payload tree does not match the lock")
    dependency_manifest, dependency_raw, _dependency_mode = _read_json(
        payload / "dependency-manifest.json",
        label="Office renderer dependency manifest",
        max_bytes=MAX_LOCK_BYTES,
    )
    _exact_keys(
        dependency_manifest,
        ("files", "platform_target", "schema_version"),
        "Office renderer dependency manifest",
    )
    canonical_dependency = (
        json.dumps(
            dependency_manifest,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        + b"\n"
    )
    if (
        dependency_raw != canonical_dependency
        or dependency_manifest["schema_version"] != 1
        or dependency_manifest["platform_target"] != target
        or not isinstance(dependency_manifest["files"], list)
    ):
        _fail("Office renderer dependency manifest schema, target, or encoding is invalid")
    dependency_files = dependency_manifest["files"]
    dependency_paths: list[str] = []
    for index, raw_record in enumerate(dependency_files):
        record = _exact_keys(
            raw_record,
            ("dependencies", "kind", "path", "sha256", "size"),
            f"Office renderer dependency file {index}",
        )
        path = _relative_path(
            record["path"],
            f"Office renderer dependency file {index} path",
        )
        if (
            record["kind"] not in {"executable", "library"}
            or not isinstance(record["dependencies"], list)
            or not isinstance(record["sha256"], str)
            or SHA256_RE.fullmatch(record["sha256"]) is None
            or not isinstance(record["size"], int)
            or isinstance(record["size"], bool)
            or record["size"] < 1
        ):
            _fail("Office renderer dependency file is invalid")
        dependency_paths.append(path)
    if dependency_paths != sorted(dependency_paths) or len(dependency_paths) != len(
        set(dependency_paths)
    ):
        _fail("Office renderer dependency file paths are not canonical")
    _contract_id, launcher_path, _capabilities = _sandbox_contract(target)
    sandbox_probe_path = _sandbox_probe_path(target)
    for path in (*executable_paths.values(), launcher_path, sandbox_probe_path):
        matches = [record for record in dependency_files if record["path"] == path]
        locked = locked_files[path]
        if (
            len(matches) != 1
            or matches[0]["kind"] != "executable"
            or matches[0]["sha256"] != locked["sha256"]
            or matches[0]["size"] != locked["size"]
        ):
            _fail(
                "Office renderer dependency manifest does not bind every "
                "required executable"
            )

    sandbox_manifest, sandbox_raw, _sandbox_mode = _read_json(
        payload / "sandbox-manifest.json",
        label="Office renderer sandbox manifest",
        max_bytes=MAX_STAGE_BYTES,
    )
    _exact_keys(
        sandbox_manifest,
        (
            "capabilities",
            "contract_id",
            "launcher_path",
            "platform_target",
            "schema_version",
        ),
        "Office renderer sandbox manifest",
    )
    contract_id, launcher_path, capabilities = _sandbox_contract(target)
    declared_capabilities = sandbox_manifest["capabilities"]
    canonical_sandbox = (
        json.dumps(
            sandbox_manifest,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        + b"\n"
    )
    if (
        sandbox_raw != canonical_sandbox
        or sandbox_manifest["schema_version"] != 1
        or sandbox_manifest["platform_target"] != target
        or sandbox_manifest["contract_id"] != contract_id
        or sandbox_manifest["launcher_path"] != launcher_path
        or not isinstance(declared_capabilities, dict)
        or tuple(declared_capabilities) != capabilities
        or any(value is not True for value in declared_capabilities.values())
    ):
        _fail("Office renderer sandbox manifest contract is invalid")

    probe_manifest, probe_raw, _probe_mode = _read_json(
        payload / "probe" / "authoritative-renderer-probe.json",
        label="Office renderer execution probe manifest",
        max_bytes=MAX_STAGE_BYTES,
    )
    _exact_keys(
        probe_manifest,
        ("dpi", "page_count", "pages", "schema_version", "source_sha256"),
        "Office renderer execution probe manifest",
    )
    canonical_probe = (
        json.dumps(
            probe_manifest,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        + b"\n"
    )
    pages = probe_manifest["pages"]
    if (
        probe_raw != canonical_probe
        or probe_manifest["schema_version"] != 1
        or probe_manifest["dpi"] != 144
        or not isinstance(probe_manifest["page_count"], int)
        or isinstance(probe_manifest["page_count"], bool)
        or not 1 <= probe_manifest["page_count"] <= 32
        or not isinstance(probe_manifest["source_sha256"], str)
        or SHA256_RE.fullmatch(probe_manifest["source_sha256"]) is None
        or not isinstance(pages, list)
        or not 1 <= len(pages) <= 32
        or probe_manifest["page_count"] != len(pages)
    ):
        _fail("Office renderer execution probe manifest contract is invalid")
    for index, raw_page in enumerate(pages, start=1):
        page = _exact_keys(
            raw_page,
            ("height_px", "page_number", "pixel_sha256", "width_px"),
            f"Office renderer execution probe page {index}",
        )
        if (
            page["page_number"] != index
            or not isinstance(page["width_px"], int)
            or isinstance(page["width_px"], bool)
            or not 1 <= page["width_px"] <= 100_000
            or not isinstance(page["height_px"], int)
            or isinstance(page["height_px"], bool)
            or not 1 <= page["height_px"] <= 100_000
            or page["width_px"] * page["height_px"] > 100_000_000
            or not isinstance(page["pixel_sha256"], str)
            or SHA256_RE.fullmatch(page["pixel_sha256"]) is None
        ):
            _fail("Office renderer execution probe page contract is invalid")
    probe_source = locked_files["probe/authoritative-renderer-probe.docx"]
    if (
        probe_source["size"] < 1
        or probe_source["sha256"] != probe_manifest["source_sha256"]
    ):
        _fail("Office renderer execution probe source does not match its manifest")
    attestation, _attestation_raw, _attestation_mode = _read_json(
        payload / "office-renderer-attestation.json",
        label="Office renderer attestation",
        max_bytes=MAX_ATTESTATION_BYTES,
    )
    expected_attestation_fields = {
        "app_version",
        "base_renderer_id",
        "base_renderer_version",
        "components",
        "font_digest",
        "platform_target",
        "release_commit",
        "schema_version",
        "signature",
    }
    if (
        set(attestation) != expected_attestation_fields
        or attestation.get("schema_version") != 2
        or attestation.get("app_version") != release_identity.app_version
        or attestation.get("release_commit") != release_identity.release_commit
        or attestation.get("platform_target") != target
        or not isinstance(attestation.get("app_version"), str)
        or VERSION_RE.fullmatch(attestation["app_version"]) is None
        or not isinstance(attestation.get("release_commit"), str)
        or re.fullmatch(r"(?!0{40}$)[0-9a-f]{40}", attestation["release_commit"]) is None
        or not isinstance(attestation.get("font_digest"), str)
        or SHA256_RE.fullmatch(attestation["font_digest"]) is None
    ):
        _fail("Office renderer attestation v2 release identity is invalid")
    components = attestation.get("components")
    expected_components = (
        "bundle-tree",
        "dependency-manifest",
        "font-manifest",
        "license-manifest",
        "pdftoppm",
        "sandbox-manifest",
        "soffice",
    )
    if (
        not isinstance(components, dict)
        or tuple(components) != expected_components
        or any(not isinstance(value, str) or SHA256_RE.fullmatch(value) is None for value in components.values())
    ):
        _fail("Office renderer attestation v2 component identity is invalid")
    component_paths = {
        "dependency-manifest": "dependency-manifest.json",
        "font-manifest": "font-manifest.json",
        "license-manifest": "license-manifest.json",
        "sandbox-manifest": "sandbox-manifest.json",
        **executable_paths,
    }
    if any(
        components[name] != locked_files[path]["sha256"]
        for name, path in component_paths.items()
    ) or components["dependency-manifest"] != _sha256_bytes(dependency_raw):
        _fail("Office renderer attestation v2 components do not match the lock")
    if components["bundle-tree"] != _deployment_tree_digest(lock["files"]):
        _fail(
            "Office renderer attestation bundle-tree does not match the locked payload"
        )
    signature = attestation.get("signature")
    try:
        decoded_signature = base64.b64decode(signature, validate=True)
    except (TypeError, ValueError) as exc:
        raise OfficeRendererPackagingError(
            "Office renderer attestation v2 signature encoding is invalid"
        ) from exc
    if len(decoded_signature) != 64:
        _fail("Office renderer attestation v2 signature encoding is invalid")
    try:
        verify_office_renderer_attestation_signature(attestation)
    except OfficeRendererTrustError as exc:
        raise OfficeRendererPackagingError(
            "Office renderer attestation v2 signature is not trusted"
        ) from exc
    return _materialize_verified_snapshot(
        payload=payload,
        lock=lock,
        target=target,
        work_root=work_root,
    )


def _materialize_verified_snapshot(
    *,
    payload: Path,
    lock: dict[str, Any],
    target: str,
    work_root: str | None,
) -> OfficeRendererAssets:
    """Copy locked bytes into a private tree and return its exact identity."""

    if not isinstance(work_root, str) or not work_root:
        _fail("v1.1+ Office renderer packaging requires a private work snapshot")
    container = Path(work_root)
    if not container.is_absolute():
        _fail("Office renderer work snapshot path must be absolute")
    if os.path.lexists(container):
        _fail("Office renderer work snapshot must not already exist")
    _directory(container.parent, "Office renderer work snapshot parent")
    try:
        container.mkdir(mode=0o700)
    except OSError as exc:
        raise OfficeRendererPackagingError(
            "Office renderer work snapshot cannot be created"
        ) from exc
    snapshot = container / target
    try:
        snapshot.mkdir(mode=lock["payload_root_mode"])
        if os.name != "nt":
            os.chmod(snapshot, lock["payload_root_mode"], follow_symlinks=False)
        for entry in lock["directories"]:
            destination = snapshot.joinpath(*entry["path"].split("/"))
            destination.mkdir(mode=entry["mode"])
            if os.name != "nt":
                os.chmod(destination, entry["mode"], follow_symlinks=False)
        for entry in lock["files"]:
            relative_parts = entry["path"].split("/")
            _copy_locked_file(
                source=payload.joinpath(*relative_parts),
                destination=snapshot.joinpath(*relative_parts),
                locked=entry,
            )

        root_info = _directory(snapshot, "Office renderer work snapshot")
        if stat.S_IMODE(root_info.st_mode) != lock["payload_root_mode"]:
            _fail("Office renderer work snapshot root mode changed")
        directories, files = _inventory_payload(snapshot)
        if directories != lock["directories"]:
            _fail("Office renderer work snapshot directory identity changed")
        if [entry["path"] for entry in files] != [
            entry["path"] for entry in lock["files"]
        ]:
            _fail("Office renderer work snapshot file identity changed")
        verified: list[dict[str, Any]] = []
        for actual, locked in zip(files, lock["files"], strict=True):
            digest, mode = _sha256_file(
                snapshot.joinpath(*actual["path"].split("/")),
                expected_size=locked["size"],
            )
            record = {
                "mode": mode,
                "path": actual["path"],
                "sha256": digest,
                "size": actual["size"],
            }
            if record != locked:
                _fail("Office renderer work snapshot bytes or modes changed")
            verified.append(record)
        if _tree_digest(verified) != lock["payload_tree_sha256"]:
            _fail("Office renderer work snapshot tree digest changed")
        _lock_snapshot_read_only(snapshot, lock)
        assets = _capture_snapshot_assets(
            snapshot=snapshot,
            lock=lock,
            target=target,
        )
        if os.name != "nt":
            os.chmod(container, 0o500, follow_symlinks=False)
        return assets
    except Exception:
        _remove_private_snapshot(container)
        raise


def _snapshot_mode(locked_mode: int) -> int:
    """Return a readable, non-writable mode that retains executability."""

    return 0o555 if locked_mode & 0o111 else 0o444


def _lock_snapshot_read_only(snapshot: Path, lock: dict[str, Any]) -> None:
    """Remove every POSIX write bit after the locked copy is complete."""

    if os.name == "nt":
        return
    try:
        for entry in lock["files"]:
            path = snapshot.joinpath(*entry["path"].split("/"))
            os.chmod(path, _snapshot_mode(entry["mode"]), follow_symlinks=False)
        for entry in reversed(lock["directories"]):
            path = snapshot.joinpath(*entry["path"].split("/"))
            os.chmod(path, 0o555, follow_symlinks=False)
        os.chmod(snapshot, 0o555, follow_symlinks=False)
    except OSError as exc:
        raise OfficeRendererPackagingError(
            "Office renderer work snapshot cannot be locked read-only"
        ) from exc


def _stat_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _snapshot_directory_record(
    path: Path,
    *,
    relative_path: str,
    expected_mode: int | None,
) -> OfficeRendererSnapshotDirectory:
    info = _directory(path, "Office renderer work snapshot directory")
    mode = stat.S_IMODE(info.st_mode)
    if expected_mode is not None and mode != expected_mode:
        _fail("Office renderer work snapshot directory mode changed")
    return OfficeRendererSnapshotDirectory(
        relative_path=relative_path,
        mode=mode,
        device=info.st_dev,
        inode=info.st_ino,
        link_count=info.st_nlink,
        size=info.st_size,
        modified_ns=info.st_mtime_ns,
        changed_ns=info.st_ctime_ns,
    )


def _capture_snapshot_assets(
    *,
    snapshot: Path,
    lock: dict[str, Any],
    target: str,
) -> OfficeRendererAssets:
    """Capture the post-lock inode and digest identity consumed by Analysis."""

    expected_directory_mode = None if os.name == "nt" else 0o555
    root = snapshot.resolve(strict=True)
    directories = [
        _snapshot_directory_record(
            root,
            relative_path="",
            expected_mode=expected_directory_mode,
        )
    ]
    directories.extend(
        _snapshot_directory_record(
            root.joinpath(*entry["path"].split("/")),
            relative_path=entry["path"],
            expected_mode=expected_directory_mode,
        )
        for entry in lock["directories"]
    )

    actual_directories, actual_files = _inventory_payload(root)
    if [entry["path"] for entry in actual_directories] != [
        entry["path"] for entry in lock["directories"]
    ]:
        _fail("Office renderer work snapshot directory set changed after locking")
    if [entry["path"] for entry in actual_files] != [
        entry["path"] for entry in lock["files"]
    ]:
        _fail("Office renderer work snapshot file set changed after locking")

    destination_root = os.path.join("app", "data", "office-renderer", target).replace(
        "\\", "/"
    )
    files: list[OfficeRendererSnapshotFile] = []
    datas: list[tuple[str, str]] = []
    verified: list[dict[str, Any]] = []
    for locked in lock["files"]:
        relative_path = locked["path"]
        source = root.joinpath(*relative_path.split("/"))
        digest, snapshot_mode = _sha256_file(
            source,
            expected_size=locked["size"],
        )
        info = source.lstat()
        expected_snapshot_mode = (
            snapshot_mode if os.name == "nt" else _snapshot_mode(locked["mode"])
        )
        if (
            digest != locked["sha256"]
            or snapshot_mode != expected_snapshot_mode
            or info.st_nlink != 1
        ):
            _fail("Office renderer work snapshot identity changed after locking")
        destination_path = f"{destination_root}/{relative_path}"
        destination_parent = destination_path.rsplit("/", 1)[0]
        files.append(
            OfficeRendererSnapshotFile(
                relative_path=relative_path,
                source_path=str(source),
                destination_path=destination_path,
                locked_mode=locked["mode"],
                snapshot_mode=snapshot_mode,
                sha256=digest,
                size=locked["size"],
                device=info.st_dev,
                inode=info.st_ino,
                link_count=info.st_nlink,
                modified_ns=info.st_mtime_ns,
                changed_ns=info.st_ctime_ns,
            )
        )
        datas.append(
            (
                str(source),
                os.path.join(*destination_parent.split("/")),
            )
        )
        verified.append(
            {
                "mode": locked["mode"],
                "path": relative_path,
                "sha256": digest,
                "size": locked["size"],
            }
        )
    if _tree_digest(verified) != lock["payload_tree_sha256"]:
        _fail("Office renderer work snapshot digest changed after locking")
    return OfficeRendererAssets(
        snapshot_root=str(root),
        destination_root=destination_root,
        payload_tree_sha256=lock["payload_tree_sha256"],
        directories=tuple(directories),
        files=tuple(files),
        datas=tuple(datas),
    )


def _remove_private_snapshot(container: Path) -> None:
    """Best-effort cleanup, including read-only snapshots on POSIX."""

    if not os.path.lexists(container):
        return
    if os.name != "nt":
        try:
            for directory, names, filenames in os.walk(
                container,
                topdown=False,
                followlinks=False,
            ):
                directory_path = Path(directory)
                for name in filenames:
                    path = directory_path / name
                    if not path.is_symlink():
                        os.chmod(path, 0o600, follow_symlinks=False)
                for name in names:
                    path = directory_path / name
                    if not path.is_symlink():
                        os.chmod(path, 0o700, follow_symlinks=False)
                os.chmod(directory_path, 0o700, follow_symlinks=False)
        except OSError:
            pass
    shutil.rmtree(container, ignore_errors=True)


def _copy_locked_file(
    *,
    source: Path,
    destination: Path,
    locked: dict[str, Any],
) -> None:
    """Copy one regular file while pinning and rechecking its source inode."""

    read_flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    write_flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        source_descriptor = os.open(source, read_flags)
    except OSError as exc:
        raise OfficeRendererPackagingError(
            "Office renderer snapshot source is unavailable"
        ) from exc
    destination_descriptor: int | None = None
    digest = hashlib.sha256()
    total = 0
    try:
        before = os.fstat(source_descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size != locked["size"]
            or stat.S_IMODE(before.st_mode) != locked["mode"]
        ):
            _fail("Office renderer snapshot source identity changed")
        try:
            destination_descriptor = os.open(
                destination,
                write_flags,
                locked["mode"],
            )
        except OSError as exc:
            raise OfficeRendererPackagingError(
                "Office renderer work snapshot file cannot be created"
            ) from exc
        if os.name != "nt":
            os.fchmod(destination_descriptor, locked["mode"])
        while chunk := os.read(source_descriptor, 1024 * 1024):
            total += len(chunk)
            if total > locked["size"]:
                _fail("Office renderer snapshot source size changed")
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination_descriptor, view)
                if written <= 0:
                    _fail("Office renderer work snapshot write failed")
                view = view[written:]
        os.fsync(destination_descriptor)
        after = os.fstat(source_descriptor)
        visible = source.lstat()
        identity = lambda item: (  # noqa: E731 - compact race identity
            item.st_dev,
            item.st_ino,
            item.st_mode,
            item.st_nlink,
            item.st_size,
            item.st_mtime_ns,
            item.st_ctime_ns,
        )
        if (
            total != locked["size"]
            or digest.hexdigest() != locked["sha256"]
            or identity(before) != identity(after)
            or identity(after) != identity(visible)
        ):
            _fail("Office renderer snapshot source changed while copying")
    except OSError as exc:
        raise OfficeRendererPackagingError(
            "Office renderer snapshot source changed while copying"
        ) from exc
    finally:
        if destination_descriptor is not None:
            os.close(destination_descriptor)
        os.close(source_descriptor)


def _assets_target(assets: OfficeRendererAssets) -> str | None:
    renderer_destination_root = "app/data/office-renderer"
    if assets.destination_root == renderer_destination_root:
        # Pre-v1.1 has no payload target suffix, but ambient renderer entries
        # still need the native host filesystem's alias semantics.
        return _native_target()
    prefix = f"{renderer_destination_root}/"
    if not assets.destination_root.startswith(prefix):
        _fail("Office renderer snapshot destination root is invalid")
    target = assets.destination_root.removeprefix(prefix)
    if target not in SUPPORTED_TARGETS:
        _fail("Office renderer snapshot destination target is invalid")
    return target


def _canonical_destination(value: object, *, target: str | None) -> str:
    if not isinstance(value, str) or not value:
        _fail("PyInstaller Analysis contains an invalid data destination")
    normalized = value.replace("\\", "/")
    if (
        normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or normalized.endswith("/")
    ):
        _fail("PyInstaller Analysis contains an invalid data destination")
    parts = normalized.split("/")
    if any(
        part in {"", ".", ".."}
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in part)
        for part in parts
    ):
        _fail("PyInstaller Analysis contains an invalid data destination")
    if target is not None and target.startswith("windows-"):
        for part in parts:
            basename = part.split(".", 1)[0]
            if (
                part.endswith((".", " "))
                or basename != basename.rstrip(". ")
                # NTFS may assign long names an 8.3 alias containing `~`.
                # PyInstaller sorts COLLECT's TOC, so an ambient short-name
                # destination could otherwise resolve into a protected long
                # directory that was created earlier in the same pass.
                or "~" in part
                or any(
                    character in WINDOWS_FORBIDDEN_DESTINATION_CHARACTERS
                    for character in part
                )
                or WINDOWS_RESERVED_DEVICE_RE.fullmatch(basename.rstrip(". "))
                is not None
            ):
                _fail(
                    "PyInstaller Analysis contains an invalid Windows data destination"
                )
    return "/".join(parts)


def _absolute_source(value: object) -> tuple[str, str]:
    try:
        source = os.fspath(value)
    except TypeError as exc:
        raise OfficeRendererPackagingError(
            "PyInstaller Analysis contains an invalid data source"
        ) from exc
    if not isinstance(source, str) or not os.path.isabs(source):
        _fail("PyInstaller Analysis contains an invalid data source")
    absolute = os.path.normcase(os.path.abspath(source))
    return absolute, os.path.normcase(os.path.realpath(source))


def _casefold_nfc(value: str) -> str:
    """Match APFS's default normalization- and case-insensitive identity."""

    return unicodedata.normalize(
        "NFC",
        unicodedata.normalize("NFC", value).casefold(),
    )


def _destination_key(assets: OfficeRendererAssets, destination: str) -> str:
    """Return the native target's frozen-bundle destination identity."""

    target = _assets_target(assets)
    canonical = _canonical_destination(destination, target=target)
    if target is not None and target.startswith("windows-"):
        return canonical.casefold()
    if target is not None and target.startswith("darwin-"):
        return _casefold_nfc(canonical)
    return canonical


def _source_path_key(target: str | None, value: str) -> str:
    """Normalize source containment according to the native build filesystem."""

    if target is not None and target.startswith("windows-"):
        normalized = ntpath.normpath(value).replace("\\", "/")
        stripped = "/".join(
            part.rstrip(". ").casefold() for part in normalized.split("/")
        )
        return ntpath.normcase(stripped)
    if target is not None and target.startswith("darwin-"):
        return _casefold_nfc(value)
    return value


def _path_is_within(path: str, root: str, *, target: str | None) -> bool:
    path_module = (
        ntpath
        if target is not None and target.startswith("windows-")
        else posixpath
    )
    try:
        return path_module.commonpath((path, root)) == path_module.normpath(root)
    except ValueError:
        return False


def _verify_snapshot_assets(assets: OfficeRendererAssets) -> None:
    if not isinstance(assets, OfficeRendererAssets):
        _fail("Office renderer snapshot evidence is invalid")
    renderer_destination_root = "app/data/office-renderer"
    if assets.snapshot_root is None:
        if any(
            (
                assets.payload_tree_sha256,
                assets.directories,
                assets.files,
                assets.datas,
            )
        ) or assets.destination_root != renderer_destination_root:
            _fail("renderer-free Office snapshot evidence is inconsistent")
        return
    target = assets.destination_root.removeprefix(f"{renderer_destination_root}/")
    if (
        not os.path.isabs(assets.snapshot_root)
        or target not in SUPPORTED_TARGETS
        or assets.payload_tree_sha256 is None
        or SHA256_RE.fullmatch(assets.payload_tree_sha256) is None
        or not assets.directories
        or assets.directories[0].relative_path != ""
        or any(not record.relative_path for record in assets.directories[1:])
        or not assets.files
        or len(assets.files) != len(assets.datas)
    ):
        _fail("Office renderer snapshot evidence is incomplete")

    root = Path(assets.snapshot_root)
    try:
        if str(root.resolve(strict=True)) != assets.snapshot_root:
            _fail("Office renderer snapshot root identity changed")
    except OSError as exc:
        raise OfficeRendererPackagingError(
            "Office renderer snapshot root identity changed"
        ) from exc

    actual_directories, actual_files = _inventory_payload(root)
    expected_directory_paths = [
        record.relative_path for record in assets.directories if record.relative_path
    ]
    if [record["path"] for record in actual_directories] != expected_directory_paths:
        _fail("Office renderer snapshot directory inventory changed")
    if [record["path"] for record in actual_files] != [
        record.relative_path for record in assets.files
    ]:
        _fail("Office renderer snapshot file inventory changed")

    for record in assets.directories:
        path = (
            root
            if not record.relative_path
            else root.joinpath(*record.relative_path.split("/"))
        )
        info = _directory(path, "Office renderer snapshot directory")
        if _stat_identity(info) != (
            record.device,
            record.inode,
            stat.S_IFMT(info.st_mode) | record.mode,
            record.link_count,
            record.size,
            record.modified_ns,
            record.changed_ns,
        ):
            _fail("Office renderer snapshot directory identity changed")

    verified: list[dict[str, Any]] = []
    expected_datas: list[tuple[str, str]] = []
    for record in assets.files:
        expected_source = root.joinpath(*record.relative_path.split("/"))
        expected_destination = f"{assets.destination_root}/{record.relative_path}"
        if (
            record.source_path != str(expected_source)
            or record.destination_path != expected_destination
        ):
            _fail("Office renderer snapshot file mapping changed")
        digest, mode = _sha256_file(expected_source, expected_size=record.size)
        info = expected_source.lstat()
        if (
            digest != record.sha256
            or mode != record.snapshot_mode
            or _stat_identity(info)
            != (
                record.device,
                record.inode,
                stat.S_IFMT(info.st_mode) | record.snapshot_mode,
                record.link_count,
                record.size,
                record.modified_ns,
                record.changed_ns,
            )
        ):
            _fail("Office renderer snapshot file identity or digest changed")
        verified.append(
            {
                "mode": record.locked_mode,
                "path": record.relative_path,
                "sha256": digest,
                "size": record.size,
            }
        )
        expected_datas.append(
            (
                record.source_path,
                os.path.join(
                    *record.destination_path.rsplit("/", 1)[0].split("/")
                ),
            )
        )
    if tuple(expected_datas) != assets.datas:
        _fail("Office renderer snapshot PyInstaller data mapping changed")
    if _tree_digest(verified) != assets.payload_tree_sha256:
        _fail("Office renderer snapshot payload digest changed")


def _related_analysis_entries(
    assets: OfficeRendererAssets,
    inventory: Iterable[Sequence[object]],
    *,
    label: str,
) -> list[tuple[str, object, object]]:
    target = _assets_target(assets)
    snapshot_root = (
        _source_path_key(
            target,
            os.path.normcase(os.path.abspath(assets.snapshot_root)),
        )
        if assets.snapshot_root is not None
        else None
    )
    snapshot_root_real = (
        _source_path_key(
            target,
            os.path.normcase(os.path.realpath(assets.snapshot_root)),
        )
        if assets.snapshot_root is not None
        else None
    )
    try:
        entries = list(inventory)
    except TypeError as exc:
        raise OfficeRendererPackagingError(
            f"PyInstaller Analysis {label} inventory is unavailable"
        ) from exc
    related: list[tuple[str, object, object]] = []
    destination_root_key = _destination_key(assets, assets.destination_root)
    for raw_entry in entries:
        if not isinstance(raw_entry, (tuple, list)) or len(raw_entry) != 3:
            _fail(f"PyInstaller Analysis contains an invalid {label} entry")
        destination = _canonical_destination(raw_entry[0], target=target)
        source_value = raw_entry[1]
        typecode = raw_entry[2]
        destination_key = _destination_key(assets, destination)
        destination_related = destination_key == destination_root_key or destination_key.startswith(
            f"{destination_root_key}/"
        )
        source_related = False
        source_absolute: str | None = None
        source_real: str | None = None
        if isinstance(source_value, (str, os.PathLike)):
            source_text = os.fspath(source_value)
            if isinstance(source_text, str) and os.path.isabs(source_text):
                source_absolute, source_real = _absolute_source(source_text)
                source_absolute = _source_path_key(target, source_absolute)
                source_real = _source_path_key(target, source_real)
                source_related = bool(
                    snapshot_root is not None
                    and snapshot_root_real is not None
                    and (
                        _path_is_within(
                            source_absolute,
                            snapshot_root,
                            target=target,
                        )
                        or _path_is_within(
                            source_real,
                            snapshot_root_real,
                            target=target,
                        )
                    )
                )
        if not destination_related and not source_related:
            continue
        related.append((destination, source_value, typecode))
    return related


def verify_office_renderer_analysis_assets(
    assets: OfficeRendererAssets,
    analysis_datas: Iterable[Sequence[object]],
    analysis_binaries: Iterable[Sequence[object]] = (),
) -> None:
    """Verify the exact post-Analysis DATA injection and reject binary handling."""

    _verify_snapshot_assets(assets)
    expected = {
        _destination_key(assets, record.destination_path): record
        for record in assets.files
    }
    if len(expected) != len(assets.files):
        _fail("Office renderer snapshot destinations are not unique")
    binary_entries = _related_analysis_entries(
        assets,
        analysis_binaries,
        label="binary",
    )
    if binary_entries:
        _fail(
            "PyInstaller Analysis classified an Office renderer source as a "
            "binary subject to rewriting"
        )
    observed: dict[str, str] = {}
    for destination, source_value, typecode in _related_analysis_entries(
        assets,
        analysis_datas,
        label="data",
    ):
        destination_key = _destination_key(assets, destination)
        if typecode != "DATA" or destination_key not in expected:
            _fail("PyInstaller Analysis contains an extra Office renderer data entry")
        if destination_key in observed:
            _fail("PyInstaller Analysis contains a duplicate Office renderer data entry")
        source_absolute, source_real = _absolute_source(source_value)
        expected_record = expected[destination_key]
        if destination != expected_record.destination_path:
            _fail("PyInstaller Analysis changed an Office renderer destination spelling")
        expected_absolute, expected_real = _absolute_source(
            expected_record.source_path
        )
        if (
            source_absolute != expected_absolute
            or source_real != expected_real
        ):
            _fail("PyInstaller Analysis substituted an Office renderer data source")
        observed[destination_key] = expected_record.source_path
    if observed != {
        key: record.source_path for key, record in expected.items()
    }:
        _fail("PyInstaller Analysis omitted an Office renderer data entry")
    _verify_snapshot_assets(assets)


def bind_office_renderer_analysis_assets(
    assets: OfficeRendererAssets,
    analysis_datas: MutableSequence[Sequence[object]],
    analysis_binaries: Iterable[Sequence[object]],
) -> None:
    """Inject verified native bytes only after PyInstaller's binary processing.

    Analysis automatically reclassifies ELF, PE, and Mach-O files supplied via
    its input ``datas`` as ``BINARY``. That would run dependency processing,
    UPX, Mach-O load-command repair, or re-signing over already attested bytes.
    The private renderer therefore stays out of Analysis inputs. Once Analysis
    has fully finished, this function rejects any ambient renderer entry in
    either TOC and injects the exact snapshot sources as trusted ``DATA``.
    COLLECT consequently performs a plain byte copy rather than binary repair.
    """

    _verify_snapshot_assets(assets)
    if _related_analysis_entries(assets, analysis_datas, label="data"):
        _fail("PyInstaller Analysis already contains an ambient Office renderer data entry")
    if _related_analysis_entries(assets, analysis_binaries, label="binary"):
        _fail(
            "PyInstaller Analysis already contains an ambient Office renderer binary entry"
        )
    try:
        analysis_datas.extend(
            (record.destination_path, record.source_path, "DATA")
            for record in assets.files
        )
    except (AttributeError, TypeError) as exc:
        raise OfficeRendererPackagingError(
            "PyInstaller Analysis data inventory cannot receive the Office renderer"
        ) from exc
    verify_office_renderer_analysis_assets(
        assets,
        analysis_datas,
        analysis_binaries,
    )


def office_renderer_datas(
    *,
    app_dir: str,
    repo_root: str,
    work_root: str | None = None,
    release_identity: ReleaseIdentityValues | None = None,
) -> list[tuple[str, str]]:
    """Compatibility wrapper for callers that only need PyInstaller datas."""

    return list(
        prepare_office_renderer_assets(
            app_dir=app_dir,
            repo_root=repo_root,
            work_root=work_root,
            release_identity=release_identity,
        ).datas
    )


__all__ = [
    "OFFICE_RENDERER_PROFILE_ENV",
    "OfficeRendererAssets",
    "OfficeRendererPackagingError",
    "OfficeRendererSnapshotDirectory",
    "OfficeRendererSnapshotFile",
    "SIGNED_AUTHORITATIVE_PROFILE",
    "UNSIGNED_DEGRADED_PROFILE",
    "bind_office_renderer_analysis_assets",
    "office_renderer_datas",
    "prepare_office_renderer_assets",
    "verify_office_renderer_analysis_assets",
]
