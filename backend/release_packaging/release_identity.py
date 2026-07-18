"""Build-time generation of the frozen application release identity.

The release identity is deliberately derived from version-controlled inputs
and Git itself.  Environment variables are not consulted.  PyInstaller specs
can append :attr:`FrozenReleaseIdentityBuild.datas` to their data files and
use :attr:`FrozenReleaseIdentityBuild.identity` when checking other signed
release inputs.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tomllib
from typing import Any


RELEASE_IDENTITY_FILENAME = "release-identity.json"
RELEASE_IDENTITY_SCHEMA_VERSION = 1
RELEASE_IDENTITY_BINDING_MODULE = "_suxiaoyou_frozen_release_identity"
_VERSION = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$"
)
_COMMIT = re.compile(r"^(?!0{40}$)[0-9a-f]{40}$")
_MAX_PROJECT_METADATA_BYTES = 1024 * 1024


class ReleaseIdentityPackagingError(RuntimeError):
    """The checkout cannot supply a trustworthy frozen release identity."""


@dataclass(frozen=True, slots=True)
class ReleaseIdentityValues:
    """Version and immutable Git commit bound into the frozen application."""

    app_version: str
    release_commit: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.app_version, str)
            or _VERSION.fullmatch(self.app_version) is None
        ):
            raise ReleaseIdentityPackagingError("application version is not X.Y.Z")
        if (
            not isinstance(self.release_commit, str)
            or _COMMIT.fullmatch(self.release_commit) is None
        ):
            raise ReleaseIdentityPackagingError("release commit is not canonical")


@dataclass(frozen=True, slots=True)
class FrozenReleaseIdentityBuild:
    """Result consumable directly by a PyInstaller spec.

    Versions older than 1.1 intentionally return ``identity=None`` and no
    data files.  Package and Python project versions are still checked before
    that compatibility decision is made.
    """

    app_version: str
    identity: ReleaseIdentityValues | None
    source_path: Path | None
    binding_module_path: Path | None
    binding_module_root: Path | None
    hiddenimports: tuple[str, ...]
    datas: tuple[tuple[str, str], ...]


def prepare_frozen_release_identity(
    *,
    repository_root: str | os.PathLike[str],
    work_root: str | os.PathLike[str],
) -> FrozenReleaseIdentityBuild:
    """Generate the canonical identity resource below an absolute work root.

    For v1.1 and later, the tracked checkout must be clean immediately before
    and after generation and ``HEAD`` must remain the same commit.  Untracked
    files are intentionally ignored: build output commonly lives in a
    repository-local, ignored directory and cannot affect the committed
    release identity.
    """

    repository = _private_absolute_directory(
        Path(repository_root), label="repository root", create=False
    )
    package_version = _package_version(repository / "package.json")
    python_version = _python_project_version(repository / "backend" / "pyproject.toml")
    if python_version != package_version:
        _fail("package.json and backend/pyproject.toml versions do not match")

    if _version_tuple(package_version) < (1, 1, 0):
        return FrozenReleaseIdentityBuild(
            app_version=package_version,
            identity=None,
            source_path=None,
            binding_module_path=None,
            binding_module_root=None,
            hiddenimports=(),
            datas=(),
        )

    _require_reserved_binding_name_absent(repository)

    output_root = _private_absolute_directory(
        Path(work_root), label="release identity work root", create=True
    )
    destination = output_root / RELEASE_IDENTITY_FILENAME
    binding_destination = output_root / f"{RELEASE_IDENTITY_BINDING_MODULE}.py"
    if any(
        path.exists() or path.is_symlink()
        for path in (destination, binding_destination)
    ):
        _fail("release identity output already exists")

    git_root = Path(_git_line(repository, "rev-parse", "--show-toplevel"))
    try:
        git_root_matches = git_root.resolve(strict=True) == repository.resolve(strict=True)
    except OSError as exc:
        raise ReleaseIdentityPackagingError("Git repository root is unavailable") from exc
    if not git_root_matches:
        _fail("repository root is not the Git worktree root")

    commit = _git_commit(repository)
    _require_clean_tracked_checkout(repository)
    identity = ReleaseIdentityValues(
        app_version=package_version,
        release_commit=commit,
    )
    raw = canonical_release_identity_bytes(identity)
    binding_raw = _binding_module_bytes(raw)

    created: list[Path] = []
    try:
        _write_new_private_file(destination, raw)
        created.append(destination)
        _write_new_private_file(binding_destination, binding_raw)
        created.append(binding_destination)
        if _git_commit(repository) != commit:
            _fail("Git HEAD changed while generating the release identity")
        _require_clean_tracked_checkout(repository)
        if _read_bounded_regular(
            destination,
            label="generated release identity",
            max_bytes=4096,
        ) != raw:
            _fail("generated release identity changed after writing")
        if _read_bounded_regular(
            binding_destination,
            label="generated release identity binding",
            max_bytes=4096,
        ) != binding_raw:
            _fail("generated release identity binding changed after writing")
    except Exception:
        for created_path in reversed(created):
            try:
                created_path.unlink()
            except OSError:
                pass
        raise

    return FrozenReleaseIdentityBuild(
        app_version=package_version,
        identity=identity,
        source_path=destination,
        binding_module_path=binding_destination,
        binding_module_root=output_root,
        hiddenimports=(RELEASE_IDENTITY_BINDING_MODULE,),
        datas=((str(destination), os.path.join("app", "data")),),
    )


def canonical_release_identity_bytes(identity: ReleaseIdentityValues) -> bytes:
    """Return the only accepted on-disk representation of an identity."""

    payload = {
        "app_version": identity.app_version,
        "release_commit": identity.release_commit,
        "schema_version": RELEASE_IDENTITY_SCHEMA_VERSION,
    }
    return (
        json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        + b"\n"
    )


def _binding_module_bytes(identity_raw: bytes) -> bytes:
    """Generate the digest module embedded in PyInstaller's signed PYZ/PKG."""

    digest = hashlib.sha256(identity_raw).hexdigest()
    return (
        "# Generated by release_packaging.release_identity; do not edit.\n"
        f'RELEASE_IDENTITY_SHA256 = "{digest}"\n'
    ).encode("ascii")


def _require_reserved_binding_name_absent(repository: Path) -> None:
    """Refuse import-path entries that could shadow the generated module."""

    backend = repository / "backend"
    try:
        entries = tuple(backend.iterdir())
    except OSError as exc:
        raise ReleaseIdentityPackagingError(
            "backend source root is unavailable"
        ) from exc
    if any(
        entry.name == RELEASE_IDENTITY_BINDING_MODULE
        or entry.name.startswith(f"{RELEASE_IDENTITY_BINDING_MODULE}.")
        for entry in entries
    ):
        _fail("reserved release identity binding module name is occupied")


def _version_tuple(value: str) -> tuple[int, int, int]:
    if _VERSION.fullmatch(value) is None:
        _fail("application version is not X.Y.Z")
    major, minor, patch = value.split(".")
    return int(major), int(minor), int(patch)


def _package_version(path: Path) -> str:
    raw = _read_bounded_regular(
        path,
        label="package.json",
        max_bytes=_MAX_PROJECT_METADATA_BYTES,
    )
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ReleaseIdentityPackagingError("package.json is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        _fail("package.json is not an object")
    version = value.get("version")
    if not isinstance(version, str) or _VERSION.fullmatch(version) is None:
        _fail("package.json version is not X.Y.Z")
    return version


def _python_project_version(path: Path) -> str:
    raw = _read_bounded_regular(
        path,
        label="backend/pyproject.toml",
        max_bytes=_MAX_PROJECT_METADATA_BYTES,
    )
    try:
        value = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseIdentityPackagingError("backend/pyproject.toml is invalid") from exc
    project = value.get("project")
    version = project.get("version") if isinstance(project, dict) else None
    if not isinstance(version, str) or _VERSION.fullmatch(version) is None:
        _fail("backend/pyproject.toml version is not X.Y.Z")
    return version


def _private_absolute_directory(path: Path, *, label: str, create: bool) -> Path:
    if not path.is_absolute():
        _fail(f"{label} must be absolute")
    if create:
        try:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise ReleaseIdentityPackagingError(f"{label} is unavailable") from exc
    try:
        info = path.lstat()
    except OSError as exc:
        raise ReleaseIdentityPackagingError(f"{label} is unavailable") from exc
    if path.is_symlink() or not stat.S_ISDIR(info.st_mode):
        _fail(f"{label} must be a real directory")
    if os.name != "nt" and info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        _fail(f"{label} permissions are unsafe")
    return path


def _read_bounded_regular(path: Path, *, label: str, max_bytes: int) -> bytes:
    try:
        visible_before = path.lstat()
    except OSError as exc:
        raise ReleaseIdentityPackagingError(f"{label} is unavailable") from exc
    if path.is_symlink() or not stat.S_ISREG(visible_before.st_mode):
        _fail(f"{label} must be a regular file")
    if not 0 <= visible_before.st_size <= max_bytes:
        _fail(f"{label} exceeds its byte limit")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ReleaseIdentityPackagingError(f"{label} is unavailable") from exc
    chunks: list[bytes] = []
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            _fail(f"{label} must be a bounded regular file")
        total = 0
        while chunk := os.read(descriptor, 8192):
            total += len(chunk)
            if total > max_bytes:
                _fail(f"{label} exceeds its byte limit")
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        visible_after = path.lstat()
    except OSError as exc:
        raise ReleaseIdentityPackagingError(f"{label} changed while reading") from exc
    if total != before.st_size or not (
        _file_identity(visible_before)
        == _file_identity(before)
        == _file_identity(after)
        == _file_identity(visible_after)
    ):
        _fail(f"{label} changed while reading")
    return b"".join(chunks)


def _file_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _write_new_private_file(path: Path, raw: bytes) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ReleaseIdentityPackagingError("release identity output is unavailable") from exc
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                _fail("release identity output write failed")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _git_commit(repository: Path) -> str:
    commit = _git_line(repository, "rev-parse", "--verify", "HEAD^{commit}")
    if _COMMIT.fullmatch(commit) is None:
        _fail("Git HEAD is not a canonical nonzero full commit")
    return commit


def _require_clean_tracked_checkout(repository: Path) -> None:
    tracked = _run_git(repository, "ls-files", "-v", "-z")
    entries = tracked.split("\x00")
    if entries[-1] != "" or any(
        len(entry) < 3 or entry[0] != "H" or entry[1] != " "
        for entry in entries[:-1]
    ):
        _fail("tracked Git checkout has unsafe index flags")
    status = _run_git(
        repository,
        "status",
        "--porcelain=v1",
        "--untracked-files=no",
        "--ignore-submodules=none",
    )
    if status:
        _fail("tracked Git checkout is dirty")


def _git_line(repository: Path, *arguments: str) -> str:
    output = _run_git(repository, *arguments)
    lines = output.splitlines()
    if len(lines) != 1 or not lines[0] or output not in {lines[0], f"{lines[0]}\n"}:
        _fail("Git returned a non-canonical response")
    return lines[0]


def _run_git(repository: Path, *arguments: str) -> str:
    git_environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_")
    }
    git_environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "LC_ALL": "C",
        }
    )
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=repository,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            env=git_environment,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise ReleaseIdentityPackagingError("Git identity check failed") from exc
    if completed.returncode != 0 or completed.stderr:
        _fail("Git identity check failed")
    return completed.stdout


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON number: {value}")


def _fail(message: str) -> None:
    raise ReleaseIdentityPackagingError(message)


__all__ = [
    "FrozenReleaseIdentityBuild",
    "RELEASE_IDENTITY_BINDING_MODULE",
    "RELEASE_IDENTITY_FILENAME",
    "RELEASE_IDENTITY_SCHEMA_VERSION",
    "ReleaseIdentityPackagingError",
    "ReleaseIdentityValues",
    "canonical_release_identity_bytes",
    "prepare_frozen_release_identity",
]
