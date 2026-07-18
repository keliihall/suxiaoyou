"""Private execution and bundled-font boundary for Office rendering.

This module deliberately provides a process/environment sandbox, not an
operating-system security boundary.  It pins executable and font paths to one
bundle, creates a one-render Fontconfig configuration containing only those
fonts, and rejects redirected or writable bundle content.  Network denial and
read-only host filesystem enforcement still require platform-native packaging
(macOS sandbox entitlements, a Windows AppContainer, or Linux namespaces and
seccomp) and must remain release blockers until such evidence exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import stat
from types import MappingProxyType
from typing import Final, Mapping, Sequence
from xml.sax.saxutils import escape

from app.office_rendering.errors import RenderContractError


_FONT_SUFFIXES: Final = frozenset({".otf", ".ttf", ".ttc"})
_MAX_FONT_FILES: Final = 8_192
_MAX_FONT_BYTES: Final = 4 * 1024 * 1024 * 1024
_FONTCONFIG_FILENAME: Final = "fonts.conf"


@dataclass(frozen=True, slots=True)
class BundledOfficeRendererSandbox:
    """One immutable renderer bundle and its exclusive font roots."""

    bundle_root: Path
    executable_paths: tuple[Path, ...]
    font_roots: tuple[Path, ...]
    font_tree_sha256: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        root = _private_directory(Path(self.bundle_root), "renderer bundle")
        if (
            not isinstance(self.executable_paths, tuple)
            or len(self.executable_paths) != 2
            or not isinstance(self.font_roots, tuple)
            or not self.font_roots
        ):
            raise RenderContractError("bundled renderer sandbox layout is invalid")

        executables = tuple(
            _private_regular_file(
                Path(path),
                root,
                "renderer executable",
                require_executable=True,
            )
            for path in self.executable_paths
        )
        if len(set(executables)) != len(executables):
            raise RenderContractError("bundled renderer executables are invalid")
        expected_bin = root / "bin"
        if any(path.parent != expected_bin for path in executables):
            raise RenderContractError("bundled renderer executable escaped bin")

        font_roots = tuple(
            _private_directory_under(Path(path), root, "renderer font root")
            for path in self.font_roots
        )
        if len(set(font_roots)) != len(font_roots):
            raise RenderContractError("bundled renderer font roots are invalid")
        font_tree_sha256 = _font_tree_sha256(font_roots)

        object.__setattr__(self, "bundle_root", root)
        object.__setattr__(self, "executable_paths", executables)
        object.__setattr__(self, "font_roots", font_roots)
        object.__setattr__(self, "font_tree_sha256", font_tree_sha256)

    def validate(self) -> None:
        """Revalidate redirect, permission, executable, and font invariants."""

        root = _private_directory(self.bundle_root, "renderer bundle")
        if root != self.bundle_root:
            raise RenderContractError("bundled renderer root changed")
        for executable in self.executable_paths:
            current = _private_regular_file(
                executable,
                root,
                "renderer executable",
                require_executable=True,
            )
            if current != executable or current.parent != root / "bin":
                raise RenderContractError("bundled renderer executable changed")
        current_fonts = tuple(
            _private_directory_under(path, root, "renderer font root")
            for path in self.font_roots
        )
        if current_fonts != self.font_roots:
            raise RenderContractError("bundled renderer font root changed")
        if _font_tree_sha256(current_fonts) != self.font_tree_sha256:
            raise RenderContractError("bundled renderer font content changed")

    def prepare(self, work_dir: Path) -> OfficeRendererSandboxInvocation:
        """Create a private Fontconfig file and cache for one render."""

        self.validate()
        work = _private_directory(Path(work_dir), "renderer work directory")
        if _contains(self.bundle_root, work) or _contains(work, self.bundle_root):
            raise RenderContractError("renderer work directory overlaps its bundle")

        config_dir = _new_private_directory(work, "fontconfig")
        cache_dir = _new_private_directory(work, "font-cache")
        config_path = config_dir / _FONTCONFIG_FILENAME
        content = _fontconfig_bytes(self.font_roots, cache_dir)
        _write_private_file(config_path, content)
        invocation = OfficeRendererSandboxInvocation(
            sandbox=self,
            work_dir=work,
            fontconfig_file=config_path,
            fontconfig_bytes=content,
            environment=MappingProxyType(
                {
                    "FONTCONFIG_FILE": str(config_path),
                    "FONTCONFIG_PATH": str(config_dir),
                }
            ),
        )
        invocation.validate()
        return invocation


@dataclass(frozen=True, slots=True)
class OfficeRendererSandboxInvocation:
    """Tamper-evident per-render sandbox material."""

    sandbox: BundledOfficeRendererSandbox
    work_dir: Path
    fontconfig_file: Path
    fontconfig_bytes: bytes = field(repr=False)
    environment: Mapping[str, str]

    def __post_init__(self) -> None:
        if not isinstance(self.sandbox, BundledOfficeRendererSandbox):
            raise RenderContractError("renderer sandbox invocation is invalid")
        if not isinstance(self.fontconfig_bytes, bytes) or not self.fontconfig_bytes:
            raise RenderContractError("renderer Fontconfig identity is invalid")
        try:
            environment = dict(self.environment)
        except (TypeError, ValueError) as exc:
            raise RenderContractError("renderer sandbox environment is invalid") from exc
        expected = {
            "FONTCONFIG_FILE": str(self.fontconfig_file),
            "FONTCONFIG_PATH": str(self.fontconfig_file.parent),
        }
        if environment != expected:
            raise RenderContractError("renderer sandbox environment is invalid")
        object.__setattr__(self, "environment", MappingProxyType(environment))

    def validate(self) -> None:
        self.sandbox.validate()
        work = _private_directory(self.work_dir, "renderer work directory")
        try:
            self.fontconfig_file.relative_to(work)
        except ValueError as exc:
            raise RenderContractError("renderer Fontconfig escaped work directory") from exc
        actual = _read_private_file(
            self.fontconfig_file,
            work,
            "renderer Fontconfig",
        )
        if actual != self.fontconfig_bytes:
            raise RenderContractError("renderer Fontconfig changed")


def discover_bundled_office_renderer_sandbox(
    executable_paths: Sequence[Path],
) -> BundledOfficeRendererSandbox | None:
    """Recognize the signed bundle layout without upgrading arbitrary hosts.

    Only two executables sharing an immediate ``<root>/bin`` directory and an
    existing ``<root>/fonts`` directory are considered a private bundle.  A
    normal host installation remains an approximate, non-isolated provider.
    Once the layout is recognized, malformed content raises instead of silently
    falling back to host fonts.
    """

    try:
        paths = tuple(Path(path) for path in executable_paths)
    except TypeError as exc:
        raise RenderContractError("bundled renderer executable paths are invalid") from exc
    if len(paths) != 2 or any(not path.is_absolute() for path in paths):
        return None
    parents = {path.parent for path in paths}
    if len(parents) != 1:
        return None
    bin_dir = next(iter(parents))
    if bin_dir.name.casefold() != "bin":
        return None
    bundle_root = bin_dir.parent
    font_root = bundle_root / "fonts"
    try:
        font_info = font_root.lstat()
    except OSError:
        return None
    if not stat.S_ISDIR(font_info.st_mode) or stat.S_ISLNK(font_info.st_mode):
        raise RenderContractError("bundled renderer font root is invalid")
    return BundledOfficeRendererSandbox(
        bundle_root=bundle_root,
        executable_paths=paths,
        font_roots=(font_root,),
    )


def _private_directory(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise RenderContractError(f"{label} path is invalid")
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise RenderContractError(f"{label} is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RenderContractError(f"{label} is invalid")
    _reject_unsafe_permissions(info, label)
    return resolved


def _private_directory_under(path: Path, root: Path, label: str) -> Path:
    resolved = _private_directory(path, label)
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise RenderContractError(f"{label} escaped renderer bundle") from exc
    current = root
    for part in relative.parts:
        current /= part
        try:
            info = current.lstat()
        except OSError as exc:
            raise RenderContractError(f"{label} is unavailable") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RenderContractError(f"{label} is redirected")
        _reject_unsafe_permissions(info, label)
    return resolved


def _private_regular_file(
    path: Path,
    root: Path,
    label: str,
    *,
    require_executable: bool = False,
) -> Path:
    if not path.is_absolute():
        raise RenderContractError(f"{label} path is invalid")
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise RenderContractError(f"{label} is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RenderContractError(f"{label} is invalid")
    _reject_unsafe_permissions(info, label)
    if require_executable and os.name != "nt" and not os.access(resolved, os.X_OK):
        raise RenderContractError(f"{label} is not executable")
    return resolved


def _font_tree_sha256(font_roots: tuple[Path, ...]) -> str:
    files = 0
    total_bytes = 0
    digest = hashlib.sha256(b"suxiaoyou-bundled-font-tree-v1\0")
    for root_index, root in enumerate(font_roots):
        for directory, names, filenames in os.walk(root, followlinks=False):
            directory_path = Path(directory)
            directory_info = directory_path.lstat()
            if stat.S_ISLNK(directory_info.st_mode) or not stat.S_ISDIR(
                directory_info.st_mode
            ):
                raise RenderContractError("bundled renderer font directory is invalid")
            _reject_unsafe_permissions(directory_info, "renderer font directory")
            names[:] = sorted(names)
            for name in names:
                child = directory_path / name
                info = child.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                    raise RenderContractError(
                        "bundled renderer font directory is redirected"
                    )
                _reject_unsafe_permissions(info, "renderer font directory")
            for name in sorted(filenames):
                child = directory_path / name
                info = child.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    raise RenderContractError("bundled renderer font file is invalid")
                _reject_unsafe_permissions(info, "renderer font file")
                if child.suffix.casefold() not in _FONT_SUFFIXES:
                    continue
                files += 1
                total_bytes += info.st_size
                if files > _MAX_FONT_FILES or total_bytes > _MAX_FONT_BYTES:
                    raise RenderContractError("bundled renderer font budget exceeded")
                relative = child.relative_to(root).as_posix()
                digest.update(str(root_index).encode("ascii"))
                digest.update(b"\0")
                digest.update(relative.encode("utf-8"))
                digest.update(b"\0")
                digest.update(_stable_file_sha256(child).encode("ascii"))
                digest.update(b"\n")
    if files == 0:
        raise RenderContractError("bundled renderer contains no fonts")
    return digest.hexdigest()


def _stable_file_sha256(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RenderContractError("bundled renderer font is unavailable") from exc
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        visible = path.lstat()
    except OSError as exc:
        raise RenderContractError("bundled renderer font changed") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(visible.st_mode)
        or _stat_identity(before) != _stat_identity(after)
        or _stat_identity(after) != _stat_identity(visible)
    ):
        raise RenderContractError("bundled renderer font changed")
    return digest.hexdigest()


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _new_private_directory(root: Path, name: str) -> Path:
    path = root / name
    try:
        path.relative_to(root)
        path.mkdir(mode=0o700)
        os.chmod(path, 0o700)
    except (OSError, ValueError) as exc:
        raise RenderContractError("renderer sandbox directory is unavailable") from exc
    return path


def _write_private_file(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        try:
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short renderer sandbox write")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.chmod(path, 0o600)
    except OSError as exc:
        raise RenderContractError("renderer Fontconfig could not be created") from exc


def _read_private_file(path: Path, root: Path, label: str) -> bytes:
    secured = _private_regular_file(path, root, label)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(secured, flags)
    except OSError as exc:
        raise RenderContractError(f"{label} is unavailable") from exc
    chunks: list[bytes] = []
    total = 0
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > 64 * 1024:
            raise RenderContractError(f"{label} is invalid")
        while chunk := os.read(descriptor, 8192):
            total += len(chunk)
            if total > 64 * 1024:
                raise RenderContractError(f"{label} is invalid")
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        visible = path.lstat()
    except OSError as exc:
        raise RenderContractError(f"{label} changed") from exc
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    visible_identity = (
        visible.st_dev,
        visible.st_ino,
        visible.st_size,
        visible.st_mtime_ns,
        visible.st_ctime_ns,
    )
    if (
        total != before.st_size
        or before_identity != after_identity
        or visible_identity != after_identity
        or stat.S_ISLNK(visible.st_mode)
    ):
        raise RenderContractError(f"{label} changed")
    return b"".join(chunks)


def _fontconfig_bytes(font_roots: tuple[Path, ...], cache_dir: Path) -> bytes:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<fontconfig>",
    ]
    lines.extend(f"  <dir>{escape(str(root))}</dir>" for root in font_roots)
    lines.extend(
        (
            f"  <cachedir>{escape(str(cache_dir))}</cachedir>",
            "  <config><rescan><int>0</int></rescan></config>",
            "</fontconfig>",
            "",
        )
    )
    return "\n".join(lines).encode("utf-8")


def _reject_unsafe_permissions(info: os.stat_result, label: str) -> None:
    if os.name != "nt" and info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise RenderContractError(f"{label} permissions are unsafe")


def _contains(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


__all__ = [
    "BundledOfficeRendererSandbox",
    "OfficeRendererSandboxInvocation",
    "discover_bundled_office_renderer_sandbox",
]
