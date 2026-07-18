"""Production construction for the local, explicitly approximate renderer.

An authoritative provider still requires a signed deployment attestation.  The
factory here never upgrades a host LibreOffice installation to authoritative;
it fingerprints the actual executable and font environment so cache entries
invalidate when that approximate environment changes.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import sys
from typing import Final

from app.office_rendering.libreoffice import (
    LibreOfficeRenderProvider,
    discover_libreoffice_toolchain,
)
from app.office_rendering.provider import (
    OfficeRenderProvider,
    UnavailableOfficeRenderProvider,
)


_FONT_SUFFIXES: Final = frozenset({".otf", ".ttf", ".ttc"})
_MAX_FONT_FILES: Final = 8_192
_MAX_FONT_BYTES: Final = 4 * 1024 * 1024 * 1024
_READ_CHUNK_BYTES: Final = 1024 * 1024


class FontFingerprintError(RuntimeError):
    """The host font environment could not be pinned safely."""


def build_local_office_render_provider(
    *,
    platform_name: str | None = None,
    font_roots: tuple[Path, ...] | None = None,
) -> OfficeRenderProvider:
    """Build a content-pinned approximate provider or an unavailable sentinel."""

    selected_platform = (platform_name or sys.platform).lower()
    toolchain = discover_libreoffice_toolchain(platform_name=selected_platform)
    if not toolchain.available:
        return UnavailableOfficeRenderProvider(
            "The local LibreOffice rendering toolchain is incomplete"
        )
    try:
        font_digest = fingerprint_font_environment(
            roots=font_roots or _platform_font_roots(selected_platform)
        )
    except FontFingerprintError:
        return UnavailableOfficeRenderProvider(
            "The local Office font environment could not be fingerprinted"
        )
    return LibreOfficeRenderProvider(
        font_digest=font_digest,
        toolchain=toolchain,
        platform_name=selected_platform,
    )


def fingerprint_font_environment(
    *,
    roots: tuple[Path, ...],
    max_files: int = _MAX_FONT_FILES,
    max_bytes: int = _MAX_FONT_BYTES,
) -> str:
    """Hash every regular font byte under a fixed, no-symlink root set.

    Paths are included only as local identity input and never exposed by the
    preview API.  Before/after stat checks make a concurrent replacement fail
    closed instead of publishing a digest for mixed font generations.
    """

    if not roots or max_files < 1 or max_bytes < 1:
        raise FontFingerprintError("Invalid font fingerprint budget")
    files: list[Path] = []
    normalized_roots: list[Path] = []
    for raw_root in roots:
        root = Path(raw_root).expanduser()
        try:
            root_info = root.lstat()
            resolved = root.resolve(strict=True)
        except OSError:
            continue
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
            continue
        normalized_roots.append(resolved)
        for directory, names, filenames in os.walk(resolved, followlinks=False):
            directory_path = Path(directory)
            names[:] = sorted(
                name
                for name in names
                if not (directory_path / name).is_symlink()
            )
            for filename in sorted(filenames):
                path = directory_path / filename
                if path.suffix.lower() not in _FONT_SUFFIXES:
                    continue
                try:
                    info = path.lstat()
                except OSError as exc:
                    raise FontFingerprintError(
                        "Font inventory changed during discovery"
                    ) from exc
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    continue
                files.append(path)
                if len(files) > max_files:
                    raise FontFingerprintError("Font file budget exceeded")
    if not files:
        raise FontFingerprintError("No regular fonts were discovered")

    digest = hashlib.sha256(b"suxiaoyou-font-environment-v1\0")
    consumed = 0
    root_labels = {
        root: f"root-{index}"
        for index, root in enumerate(sorted(set(normalized_roots), key=str))
    }
    for path in sorted(set(files), key=str):
        root = next(
            (candidate for candidate in root_labels if path.is_relative_to(candidate)),
            None,
        )
        if root is None:
            raise FontFingerprintError("Font escaped its declared root")
        relative = path.relative_to(root).as_posix()
        label = root_labels[root]
        try:
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
                raise FontFingerprintError("Font is not a regular file")
            if consumed + before.st_size > max_bytes:
                raise FontFingerprintError("Font byte budget exceeded")
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            try:
                opened = os.fstat(descriptor)
                if (opened.st_dev, opened.st_ino) != (
                    before.st_dev,
                    before.st_ino,
                ):
                    raise FontFingerprintError("Font changed while opening")
                digest.update(label.encode("ascii"))
                digest.update(b"\0")
                digest.update(relative.encode("utf-8"))
                digest.update(b"\0")
                read_size = 0
                while chunk := os.read(descriptor, _READ_CHUNK_BYTES):
                    read_size += len(chunk)
                    consumed += len(chunk)
                    if consumed > max_bytes:
                        raise FontFingerprintError("Font byte budget exceeded")
                    digest.update(chunk)
                after = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            visible = path.lstat()
        except FontFingerprintError:
            raise
        except OSError as exc:
            raise FontFingerprintError(
                "Font inventory changed while hashing"
            ) from exc
        if (
            read_size != opened.st_size
            or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
            or (visible.st_dev, visible.st_ino)
            != (after.st_dev, after.st_ino)
        ):
            raise FontFingerprintError("Font changed while hashing")
    return digest.hexdigest()


def _platform_font_roots(platform_name: str) -> tuple[Path, ...]:
    home = Path.home()
    if platform_name.startswith("win"):
        windows = Path(os.environ.get("WINDIR", r"C:\Windows"))
        return (windows / "Fonts",)
    if platform_name == "darwin":
        return (
            Path("/System/Library/Fonts"),
            Path("/Library/Fonts"),
            home / "Library/Fonts",
        )
    return (
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
        home / ".local/share/fonts",
        home / ".fonts",
    )


__all__ = [
    "FontFingerprintError",
    "build_local_office_render_provider",
    "fingerprint_font_environment",
]
