"""Fail-closed loader for the identity embedded in a frozen application."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any

from app.office_rendering.attested import AuthoritativeRendererReleaseIdentity
from app.office_rendering.errors import RenderContractError


RELEASE_IDENTITY_FILENAME = "release-identity.json"
RELEASE_IDENTITY_SCHEMA_VERSION = 1
RELEASE_IDENTITY_BINDING_MODULE = "_suxiaoyou_frozen_release_identity"
MAX_RELEASE_IDENTITY_BYTES = 4096


class FrozenReleaseIdentityError(RuntimeError):
    """The frozen application identity is absent, unsafe, or ambiguous."""


def load_frozen_renderer_release_identity() -> AuthoritativeRendererReleaseIdentity:
    """Load the authoritative identity from ``sys._MEIPASS`` only.

    There is intentionally no source-tree or environment-variable fallback.
    Every directory below the bootloader-provided root and the identity file
    itself must be non-symlinked and non-writable by group or world.
    """

    root = _frozen_root()
    directories = (root, root / "app", root / "app" / "data")
    snapshots = tuple(_private_directory_snapshot(path) for path in directories)
    path = directories[-1] / RELEASE_IDENTITY_FILENAME
    raw = _read_private_identity_file(path)
    if tuple(_private_directory_snapshot(path) for path in directories) != snapshots:
        _fail("frozen release identity path changed while reading")
    _require_executable_bound_identity(raw)

    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise FrozenReleaseIdentityError("frozen release identity is invalid") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "app_version",
        "release_commit",
        "schema_version",
    }:
        _fail("frozen release identity fields are invalid")
    if (
        not isinstance(payload["schema_version"], int)
        or isinstance(payload["schema_version"], bool)
        or payload["schema_version"] != RELEASE_IDENTITY_SCHEMA_VERSION
    ):
        _fail("frozen release identity schema is invalid")
    try:
        identity = AuthoritativeRendererReleaseIdentity(
            app_version=payload["app_version"],
            release_commit=payload["release_commit"],
        )
    except (TypeError, RenderContractError) as exc:
        raise FrozenReleaseIdentityError("frozen release identity values are invalid") from exc
    if raw != canonical_release_identity_bytes(identity):
        _fail("frozen release identity is not canonical")
    return identity


def _require_executable_bound_identity(raw: bytes) -> None:
    """Bind the replaceable JSON resource to the frozen executable archive."""

    try:
        import pyimod02_importers

        frozen_loader_type = pyimod02_importers.PyiFrozenLoader
        module = importlib.import_module(RELEASE_IDENTITY_BINDING_MODULE)
    except (AttributeError, ImportError, OSError) as exc:
        raise FrozenReleaseIdentityError(
            "frozen release identity executable binding is unavailable"
        ) from exc
    module_spec = getattr(module, "__spec__", None)
    module_loader = getattr(module_spec, "loader", None)
    module_origin = getattr(module_spec, "origin", None)
    expected_origin = str(
        _frozen_root() / f"{RELEASE_IDENTITY_BINDING_MODULE}.py"
    )
    if (
        not isinstance(frozen_loader_type, type)
        or not isinstance(module_loader, frozen_loader_type)
        or module_origin != expected_origin
    ):
        _fail("frozen release identity executable binding origin is invalid")
    expected = getattr(module, "RELEASE_IDENTITY_SHA256", None)
    actual = hashlib.sha256(raw).hexdigest()
    if (
        type(expected) is not str
        or len(expected) != 64
        or any(character not in "0123456789abcdef" for character in expected)
        or expected == "0" * 64
        or expected != actual
    ):
        _fail("frozen release identity executable binding does not match")


def canonical_release_identity_bytes(
    identity: AuthoritativeRendererReleaseIdentity,
) -> bytes:
    """Return the exact representation accepted from a frozen resource."""

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


def _frozen_root() -> Path:
    if getattr(sys, "frozen", False) is not True:
        _fail("release identity is available only in a frozen application")
    value = getattr(sys, "_MEIPASS", None)
    if not isinstance(value, str) or not value or "\x00" in value:
        _fail("frozen application root is unavailable")
    root = Path(value)
    if not root.is_absolute():
        _fail("frozen application root is invalid")
    return root


def _private_directory_snapshot(path: Path) -> tuple[int, ...]:
    try:
        info = path.lstat()
    except OSError as exc:
        raise FrozenReleaseIdentityError("frozen release identity path is unavailable") from exc
    if path.is_symlink() or not stat.S_ISDIR(info.st_mode):
        _fail("frozen release identity path is invalid")
    _reject_unsafe_mode(info, directory=True)
    return _identity(info)


def _read_private_identity_file(path: Path) -> bytes:
    try:
        visible_before = path.lstat()
    except OSError as exc:
        raise FrozenReleaseIdentityError("frozen release identity is unavailable") from exc
    if path.is_symlink() or not stat.S_ISREG(visible_before.st_mode):
        _fail("frozen release identity must be a regular file")
    if visible_before.st_nlink != 1:
        _fail("frozen release identity must not be hard-linked")
    _reject_unsafe_mode(visible_before, directory=False)
    if not 1 <= visible_before.st_size <= MAX_RELEASE_IDENTITY_BYTES:
        _fail("frozen release identity size is invalid")

    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise FrozenReleaseIdentityError("frozen release identity is unavailable") from exc
    chunks: list[bytes] = []
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not 1 <= before.st_size <= MAX_RELEASE_IDENTITY_BYTES
        ):
            _fail("frozen release identity must be a bounded private file")
        _reject_unsafe_mode(before, directory=False)
        total = 0
        while chunk := os.read(descriptor, 1024):
            total += len(chunk)
            if total > MAX_RELEASE_IDENTITY_BYTES:
                _fail("frozen release identity exceeds its byte limit")
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        visible_after = path.lstat()
    except OSError as exc:
        raise FrozenReleaseIdentityError("frozen release identity changed") from exc
    if total != before.st_size or not (
        _identity(visible_before)
        == _identity(before)
        == _identity(after)
        == _identity(visible_after)
    ):
        _fail("frozen release identity changed while reading")
    return b"".join(chunks)


def _reject_unsafe_mode(info: os.stat_result, *, directory: bool) -> None:
    if os.name == "nt":
        return
    mode = stat.S_IMODE(info.st_mode)
    if mode & (
        stat.S_IWGRP
        | stat.S_IWOTH
        | stat.S_ISUID
        | stat.S_ISGID
        | stat.S_ISVTX
    ):
        _fail("frozen release identity permissions are unsafe")
    if directory:
        if mode & stat.S_IXUSR == 0:
            _fail("frozen release identity directory is inaccessible")
    elif (
        mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        or mode & stat.S_IRUSR == 0
    ):
        _fail("frozen release identity file mode is unsafe")


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


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
    raise FrozenReleaseIdentityError(message)


__all__ = [
    "FrozenReleaseIdentityError",
    "MAX_RELEASE_IDENTITY_BYTES",
    "RELEASE_IDENTITY_BINDING_MODULE",
    "RELEASE_IDENTITY_FILENAME",
    "RELEASE_IDENTITY_SCHEMA_VERSION",
    "canonical_release_identity_bytes",
    "load_frozen_renderer_release_identity",
]
