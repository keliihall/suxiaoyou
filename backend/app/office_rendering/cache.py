"""Atomic, content-addressed, private cache for Office render artifacts."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import stat
import struct
import tempfile
import uuid
from pathlib import Path
from typing import Final

from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader

from app.office_rendering.errors import (
    CacheIntegrityError,
    CacheWriteError,
    OfficeRenderingError,
    PathEscapeError,
    ProviderUnavailableError,
    RenderContractError,
    StaleSourceError,
)
from app.office_rendering.models import (
    PageArtifact,
    PdfArtifact,
    RenderManifest,
    RendererDescriptor,
    RenderRequest,
    build_cache_key,
    validate_sha256,
)
from app.office_rendering.provider import OfficeRenderProvider, ProviderAvailability


MANIFEST_FILENAME: Final = "manifest.json"
MANIFEST_DIGEST_FILENAME: Final = "manifest.sha256"
DEFAULT_MAX_SOURCE_BYTES: Final = 512 * 1024 * 1024
DEFAULT_MAX_ARTIFACT_BYTES: Final = 512 * 1024 * 1024
DEFAULT_MAX_PAGES: Final = 1_000
DEFAULT_MAX_PAGE_PIXELS: Final = 100_000_000
MAX_MANIFEST_BYTES: Final = 1024 * 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PDF_MAGIC = b"%PDF-"


class OfficeRenderCache:
    """Persist immutable render entries beneath one application-private root.

    Entries are staged and fully validated before a same-filesystem directory
    rename exposes them.  A cache hit revalidates the manifest sidecar, strict
    schema, request/provider identity, every page digest, and PNG dimensions.
    Corruption fails closed and is never silently treated as a cache miss.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        max_source_bytes: int = DEFAULT_MAX_SOURCE_BYTES,
        max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
        max_pages: int = DEFAULT_MAX_PAGES,
        max_page_pixels: int = DEFAULT_MAX_PAGE_PIXELS,
    ) -> None:
        raw_root = Path(root).expanduser()
        if not raw_root.is_absolute():
            raise RenderContractError("Office render cache root must be absolute")
        if raw_root.is_symlink():
            raise PathEscapeError("Office render cache root cannot be a symbolic link")
        _validate_positive_limit(max_source_bytes, "max_source_bytes")
        _validate_positive_limit(max_artifact_bytes, "max_artifact_bytes")
        _validate_positive_limit(max_pages, "max_pages")
        _validate_positive_limit(max_page_pixels, "max_page_pixels")

        try:
            raw_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise CacheWriteError("Could not create the Office render cache") from exc
        if raw_root.is_symlink() or not raw_root.is_dir():
            raise PathEscapeError("Office render cache root is redirected or invalid")

        self.root = raw_root.resolve(strict=True)
        self.max_source_bytes = max_source_bytes
        self.max_artifact_bytes = max_artifact_bytes
        self.max_pages = max_pages
        self.max_page_pixels = max_page_pixels
        _harden_directory(self.root)
        self._entries = self.root / "entries"
        self._staging = self.root / ".staging"
        self._trash = self.root / ".trash"
        for directory in (self._entries, self._staging, self._trash):
            _ensure_private_directory(directory, self.root)

    async def get_or_render(
        self,
        request: RenderRequest,
        provider: OfficeRenderProvider,
    ) -> RenderManifest:
        """Return a validated hit or atomically publish a new provider output."""

        descriptor = _provider_descriptor(provider)
        availability = _provider_availability(provider)
        if not availability.available:
            raise ProviderUnavailableError(
                availability.reason or "Office render provider is unavailable"
            )

        self._validate_source(request)
        cached = self.load(request, descriptor)
        if cached is not None:
            return cached

        staging_root = Path(
            tempfile.mkdtemp(prefix="render-", dir=self._staging)
        ).resolve(strict=True)
        _assert_within(self._staging, staging_root)
        _harden_directory(staging_root)
        payload_dir = staging_root / "entry"
        payload_dir.mkdir(mode=0o700)
        _harden_directory(payload_dir)

        try:
            manifest = await provider.render(request, payload_dir)
            if not isinstance(manifest, RenderManifest):
                raise RenderContractError(
                    "Office render provider must return a RenderManifest"
                )
            if _provider_descriptor(provider) != descriptor:
                raise RenderContractError(
                    "Office render provider identity changed during rendering"
                )
            self._validate_source(request)
            self._validate_manifest_identity(manifest, request, descriptor)
            self._validate_artifacts(
                payload_dir,
                manifest,
                published=False,
                prepare_for_commit=True,
            )
            self._write_manifest(payload_dir, manifest)
            _fsync_directory(payload_dir)

            installed = self._install_entry(payload_dir, manifest.cache_key)
            if not installed:
                # A concurrent renderer won the immutable content-addressed
                # destination.  Its entry must pass the same full validation.
                existing = self.load(request, descriptor)
                if existing is None:
                    raise CacheWriteError(
                        "Concurrent Office render cache entry disappeared"
                    )
                return existing

            installed_manifest = self.load(request, descriptor)
            if installed_manifest is None:
                raise CacheWriteError("Installed Office render cache entry is missing")
            return installed_manifest
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)

    def load(
        self,
        request: RenderRequest,
        descriptor: RendererDescriptor,
    ) -> RenderManifest | None:
        """Load and revalidate an entry; stale sources and corruption are errors."""

        if not isinstance(request, RenderRequest):
            raise RenderContractError("request must be a RenderRequest")
        if not isinstance(descriptor, RendererDescriptor):
            raise RenderContractError("descriptor must be a RendererDescriptor")
        self._validate_source(request)
        cache_key = build_cache_key(request, descriptor)
        entry = self._entry_path(cache_key)
        if not entry.exists() and not entry.is_symlink():
            return None
        return self._load_entry(entry, request, descriptor)

    def page_path(
        self,
        request: RenderRequest,
        descriptor: RendererDescriptor,
        page_number: int,
    ) -> Path | None:
        """Return a path only after the whole immutable entry has been verified."""

        if (
            not isinstance(page_number, int)
            or isinstance(page_number, bool)
            or page_number < 1
        ):
            raise RenderContractError("page_number must be a positive integer")
        manifest = self.load(request, descriptor)
        if manifest is None:
            return None
        page = next(
            (item for item in manifest.pages if item.page_number == page_number),
            None,
        )
        if page is None:
            return None
        path = self._entry_path(manifest.cache_key) / page.filename
        _assert_within(self._entry_path(manifest.cache_key), path)
        return path

    def pdf_path(
        self,
        request: RenderRequest,
        descriptor: RendererDescriptor,
    ) -> Path | None:
        """Return the retained PDF only after the complete entry revalidates."""

        manifest = self.load(request, descriptor)
        if manifest is None:
            return None
        entry = self._entry_path(manifest.cache_key)
        path = entry / manifest.pdf.filename
        _assert_within(entry, path)
        return path

    def entry_path(
        self,
        request: RenderRequest,
        descriptor: RendererDescriptor,
    ) -> Path | None:
        """Return a cache entry only after manifest and every page revalidate."""

        manifest = self.load(request, descriptor)
        if manifest is None:
            return None
        entry = self._entry_path(manifest.cache_key)
        _assert_within(self._entries, entry)
        return entry

    def invalidate(
        self,
        request: RenderRequest,
        descriptor: RendererDescriptor,
    ) -> bool:
        """Atomically hide and then remove the exact content-addressed entry.

        Source freshness is intentionally not checked: callers must be able to
        invalidate the old key after the source has changed.
        """

        if not isinstance(request, RenderRequest):
            raise RenderContractError("request must be a RenderRequest")
        if not isinstance(descriptor, RendererDescriptor):
            raise RenderContractError("descriptor must be a RendererDescriptor")
        return self.invalidate_key(build_cache_key(request, descriptor))

    def invalidate_key(self, cache_key: str) -> bool:
        """Atomically remove one validated-format cache key without path input."""

        key = validate_sha256(cache_key, "cache_key")
        entry = self._entry_path(key)
        if not entry.exists() and not entry.is_symlink():
            return False
        if entry.is_symlink() or not entry.is_dir():
            raise CacheIntegrityError(
                "Office render cache entry is redirected or not a directory"
            )
        _assert_within(self._entries, entry)

        quarantine = self._trash / f"{key}-{uuid.uuid4().hex}"
        _assert_within(self._trash, quarantine, strict=False)
        try:
            os.rename(entry, quarantine)
            _fsync_directory(entry.parent)
        except OSError as exc:
            raise CacheWriteError(
                "Could not atomically invalidate Office render cache entry"
            ) from exc
        try:
            shutil.rmtree(quarantine, ignore_errors=False)
        except OSError as exc:
            raise CacheWriteError(
                "Office render cache entry was hidden but trash cleanup failed"
            ) from exc
        _fsync_directory(self._trash)
        return True

    def _validate_source(self, request: RenderRequest) -> None:
        if not isinstance(request, RenderRequest):
            raise RenderContractError("request must be a RenderRequest")
        root = request.workspace_root
        source = request.source_path
        if root.is_symlink() or source.is_symlink():
            raise PathEscapeError("Office render source cannot use a symbolic link")
        try:
            resolved_root = root.resolve(strict=True)
            if not resolved_root.is_dir():
                raise PathEscapeError("Office render workspace root is not a directory")
            resolved_source = source.resolve(strict=True)
            resolved_source.relative_to(resolved_root)
        except PathEscapeError:
            raise
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise StaleSourceError("Office render source is missing") from exc
        except ValueError as exc:
            raise PathEscapeError(
                "Office render source resolves outside its workspace"
            ) from exc
        if not resolved_source.is_file():
            raise StaleSourceError("Office render source is not a regular file")
        digest, _size, _head = _hash_regular_file(
            resolved_source,
            boundary=resolved_root,
            max_bytes=self.max_source_bytes,
            error_type=StaleSourceError,
        )
        if digest != request.source_sha256:
            raise StaleSourceError(
                "Office render source SHA-256 no longer matches the request"
            )

    def _load_entry(
        self,
        entry: Path,
        request: RenderRequest,
        descriptor: RendererDescriptor,
    ) -> RenderManifest:
        if entry.is_symlink() or not entry.is_dir():
            raise CacheIntegrityError(
                "Office render cache entry is redirected or not a directory"
            )
        _assert_within(self._entries, entry)
        manifest_path = entry / MANIFEST_FILENAME
        digest_path = entry / MANIFEST_DIGEST_FILENAME
        manifest_bytes, manifest_size = _read_regular_file(
            manifest_path,
            boundary=entry,
            max_bytes=MAX_MANIFEST_BYTES,
        )
        if manifest_size == 0:
            raise CacheIntegrityError("Office render manifest is empty")
        digest_bytes, _digest_size = _read_regular_file(
            digest_path,
            boundary=entry,
            max_bytes=128,
        )
        try:
            recorded_digest = digest_bytes.decode("ascii").strip()
        except UnicodeDecodeError as exc:
            raise CacheIntegrityError(
                "Office render manifest digest is not ASCII"
            ) from exc
        try:
            validate_sha256(recorded_digest, "manifest digest")
        except RenderContractError as exc:
            raise CacheIntegrityError("Office render manifest digest is invalid") from exc
        actual_digest = hashlib.sha256(manifest_bytes).hexdigest()
        if actual_digest != recorded_digest:
            raise CacheIntegrityError("Office render manifest was modified")
        try:
            payload = json.loads(manifest_bytes.decode("utf-8"))
            manifest = RenderManifest.from_dict(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, RenderContractError) as exc:
            raise CacheIntegrityError("Office render manifest is corrupt") from exc
        if manifest.canonical_bytes() != manifest_bytes:
            raise CacheIntegrityError("Office render manifest is not canonical")
        self._validate_manifest_identity(manifest, request, descriptor)
        self._validate_artifacts(
            entry,
            manifest,
            published=True,
            prepare_for_commit=False,
        )
        return manifest

    def _validate_manifest_identity(
        self,
        manifest: RenderManifest,
        request: RenderRequest,
        descriptor: RendererDescriptor,
    ) -> None:
        expected = {
            "cache_key": build_cache_key(request, descriptor),
            "source_sha256": request.source_sha256,
            "document_format": request.document_format,
            "renderer_id": descriptor.renderer_id,
            "renderer_version": descriptor.renderer_version,
            "font_digest": descriptor.font_digest,
            "parameters_version": request.parameters_version,
            "parameters_sha256": request.parameters_sha256,
            "quality": descriptor.quality,
        }
        actual = {
            "cache_key": manifest.cache_key,
            "source_sha256": manifest.source_sha256,
            "document_format": manifest.document_format,
            "renderer_id": manifest.renderer_id,
            "renderer_version": manifest.renderer_version,
            "font_digest": manifest.font_digest,
            "parameters_version": manifest.parameters_version,
            "parameters_sha256": manifest.parameters_sha256,
            "quality": manifest.quality,
        }
        if actual != expected:
            raise CacheIntegrityError(
                "Office render manifest does not match the request and provider"
            )

    def _validate_artifacts(
        self,
        directory: Path,
        manifest: RenderManifest,
        *,
        published: bool,
        prepare_for_commit: bool,
    ) -> None:
        if directory.is_symlink() or not directory.is_dir():
            raise CacheIntegrityError("Office render artifact directory is invalid")
        if len(manifest.pages) > self.max_pages:
            raise CacheIntegrityError("Office render page count exceeds the cache budget")

        expected_names = {
            manifest.pdf.filename,
            *(page.filename for page in manifest.pages),
        }
        if published:
            expected_names.update({MANIFEST_FILENAME, MANIFEST_DIGEST_FILENAME})
        try:
            actual_entries = list(directory.iterdir())
        except OSError as exc:
            raise CacheIntegrityError(
                "Office render artifact directory cannot be listed"
            ) from exc
        actual_names = {path.name for path in actual_entries}
        if len(actual_names) != len(actual_entries) or actual_names != expected_names:
            raise CacheIntegrityError(
                "Office render artifact directory has missing or undeclared files"
            )
        if any(path.is_symlink() or not path.is_file() for path in actual_entries):
            raise CacheIntegrityError(
                "Office render artifact entries must be regular files"
            )

        total_bytes = 0
        pdf_path = directory / manifest.pdf.filename
        pdf_digest, pdf_size_bytes, pdf_head = _hash_regular_file(
            pdf_path,
            boundary=directory,
            max_bytes=self.max_artifact_bytes,
            error_type=CacheIntegrityError,
        )
        total_bytes += pdf_size_bytes
        if (
            pdf_digest != manifest.pdf.sha256
            or pdf_size_bytes != manifest.pdf.size_bytes
        ):
            raise CacheIntegrityError("Office render PDF digest or size changed")
        self._validate_pdf_artifact(pdf_path, manifest.pdf, pdf_head)
        if prepare_for_commit:
            _harden_file(pdf_path)
            _fsync_regular_file(pdf_path)

        for page in manifest.pages:
            path = directory / page.filename
            remaining = self.max_artifact_bytes - total_bytes
            if remaining < 1:
                raise CacheIntegrityError(
                    "Office render artifacts exceed the cache byte budget"
                )
            digest, size_bytes, head = _hash_regular_file(
                path,
                boundary=directory,
                max_bytes=remaining,
                error_type=CacheIntegrityError,
            )
            total_bytes += size_bytes
            if digest != page.sha256 or size_bytes != page.size_bytes:
                raise CacheIntegrityError(
                    f"Office render page {page.page_number} digest or size changed"
                )
            self._validate_png_header(page, head)
            if page.width_px * page.height_px > self.max_page_pixels:
                raise CacheIntegrityError(
                    f"Office render page {page.page_number} exceeds the pixel budget"
                )
            pixel_digest = _canonical_rgba_sha256(
                path,
                width=page.width_px,
                height=page.height_px,
                page_number=page.page_number,
            )
            if pixel_digest != page.pixel_sha256:
                raise CacheIntegrityError(
                    f"Office render page {page.page_number} decoded pixels changed"
                )
            if prepare_for_commit:
                _harden_file(path)
                _fsync_regular_file(path)

    def _validate_png_header(self, page: PageArtifact, head: bytes) -> None:
        if (
            len(head) < 24
            or not head.startswith(_PNG_SIGNATURE)
            or head[8:12] != b"\x00\x00\x00\r"
            or head[12:16] != b"IHDR"
        ):
            raise CacheIntegrityError(
                f"Office render page {page.page_number} is not a valid PNG container"
            )
        width, height = struct.unpack(">II", head[16:24])
        if (width, height) != (page.width_px, page.height_px):
            raise CacheIntegrityError(
                f"Office render page {page.page_number} dimensions changed"
            )

    def _validate_pdf_artifact(
        self,
        path: Path,
        artifact: PdfArtifact,
        head: bytes,
    ) -> None:
        if not head.startswith(_PDF_MAGIC):
            raise CacheIntegrityError("Office render PDF has an invalid container")
        flags = os.O_RDONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise CacheIntegrityError("Office render PDF cannot be opened") from exc
        try:
            with os.fdopen(os.dup(fd), "rb") as handle:
                try:
                    reader = PdfReader(handle, strict=True)
                    if reader.is_encrypted:
                        raise CacheIntegrityError("Office render PDF is encrypted")
                    if len(reader.pages) != artifact.page_count:
                        raise CacheIntegrityError(
                            "Office render PDF page count changed"
                        )
                except CacheIntegrityError:
                    raise
                except Exception as exc:
                    raise CacheIntegrityError(
                        "Office render PDF is structurally invalid"
                    ) from exc
        finally:
            os.close(fd)

    def _write_manifest(self, directory: Path, manifest: RenderManifest) -> None:
        manifest_bytes = manifest.canonical_bytes()
        digest_bytes = (hashlib.sha256(manifest_bytes).hexdigest() + "\n").encode("ascii")
        _write_private_file(directory / MANIFEST_FILENAME, manifest_bytes)
        _write_private_file(directory / MANIFEST_DIGEST_FILENAME, digest_bytes)

    def _install_entry(self, staged_entry: Path, cache_key: str) -> bool:
        destination = self._entry_path(cache_key)
        _ensure_private_directory(destination.parent, self._entries)
        if destination.is_symlink():
            raise CacheIntegrityError("Office render cache destination is redirected")
        try:
            os.rename(staged_entry, destination)
            _fsync_directory(destination.parent)
            return True
        except OSError as exc:
            if exc.errno in {errno.EEXIST, errno.ENOTEMPTY} or destination.exists():
                return False
            raise CacheWriteError(
                "Could not atomically install Office render cache entry"
            ) from exc

    def _entry_path(self, cache_key: str) -> Path:
        key = validate_sha256(cache_key, "cache_key")
        path = self._entries / key[:2] / key
        _assert_within(self._entries, path, strict=False)
        return path


def _canonical_rgba_sha256(
    path: Path,
    *,
    width: int,
    height: int,
    page_number: int,
) -> str:
    """Decode one PNG and hash canonical row-major RGBA bytes."""

    try:
        with Image.open(path) as image:
            if image.format != "PNG" or getattr(image, "n_frames", 1) != 1:
                raise CacheIntegrityError(
                    f"Office render page {page_number} is not a single-frame PNG"
                )
            if image.size != (width, height):
                raise CacheIntegrityError(
                    f"Office render page {page_number} dimensions changed while decoding"
                )
            image.load()
            rgba = image.convert("RGBA")
            digest = hashlib.sha256()
            for top in range(0, height, 256):
                bottom = min(height, top + 256)
                digest.update(
                    rgba.crop((0, top, width, bottom)).tobytes("raw", "RGBA")
                )
            return digest.hexdigest()
    except CacheIntegrityError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise CacheIntegrityError(
            f"Office render page {page_number} cannot be decoded as PNG"
        ) from exc


def _provider_descriptor(provider: OfficeRenderProvider) -> RendererDescriptor:
    try:
        descriptor = provider.descriptor
    except Exception as exc:
        raise RenderContractError(
            "Office render provider descriptor is unavailable"
        ) from exc
    if not isinstance(descriptor, RendererDescriptor):
        raise RenderContractError(
            "Office render provider descriptor must be a RendererDescriptor"
        )
    return descriptor


def _provider_availability(provider: OfficeRenderProvider) -> ProviderAvailability:
    try:
        availability = provider.availability()
    except Exception as exc:
        raise ProviderUnavailableError(
            "Office render provider availability check failed"
        ) from exc
    if not isinstance(availability, ProviderAvailability):
        raise RenderContractError(
            "Office render provider must return ProviderAvailability"
        )
    return availability


def _validate_positive_limit(value: object, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise RenderContractError(f"{name} must be a positive integer")


def _ensure_private_directory(directory: Path, boundary: Path) -> None:
    if directory.is_symlink():
        raise PathEscapeError("Office render cache directory cannot be a symbolic link")
    _assert_within(boundary, directory, strict=False)
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise CacheWriteError("Could not create Office render cache directory") from exc
    if directory.is_symlink() or not directory.is_dir():
        raise PathEscapeError("Office render cache directory is redirected or invalid")
    _assert_within(boundary, directory)
    _harden_directory(directory)


def _assert_within(boundary: Path, candidate: Path, *, strict: bool = True) -> Path:
    try:
        resolved_boundary = boundary.resolve(strict=True)
        resolved_candidate = candidate.resolve(strict=strict)
        resolved_candidate.relative_to(resolved_boundary)
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        raise PathEscapeError("Office render path escapes its declared boundary") from exc
    return resolved_candidate


def _read_regular_file(
    path: Path,
    *,
    boundary: Path,
    max_bytes: int,
) -> tuple[bytes, int]:
    _assert_within(boundary, path)
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise CacheIntegrityError("Office render cache file cannot be opened") from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > max_bytes:
            raise CacheIntegrityError(
                "Office render cache file is not regular or exceeds its budget"
            )
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > max_bytes or len(content) != metadata.st_size:
            raise CacheIntegrityError("Office render cache file size changed while reading")
        return content, len(content)
    finally:
        os.close(fd)


def _hash_regular_file(
    path: Path,
    *,
    boundary: Path,
    max_bytes: int,
    error_type: type[OfficeRenderingError],
) -> tuple[str, int, bytes]:
    _assert_within(boundary, path)
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise error_type("Office render file cannot be opened safely") from exc
    digest = hashlib.sha256()
    size_bytes = 0
    head = bytearray()
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > max_bytes:
            raise error_type(
                "Office render file is not regular or exceeds its byte budget"
            )
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            size_bytes += len(chunk)
            if size_bytes > max_bytes:
                raise error_type("Office render file exceeds its byte budget")
            digest.update(chunk)
            if len(head) < 24:
                head.extend(chunk[: 24 - len(head)])
        if size_bytes != metadata.st_size:
            raise error_type("Office render file size changed while reading")
    finally:
        os.close(fd)
    return digest.hexdigest(), size_bytes, bytes(head)


def _write_private_file(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise CacheWriteError("Could not create Office render cache metadata") from exc
    try:
        view = memoryview(content)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise CacheWriteError("Could not write Office render cache metadata")
            view = view[written:]
        os.fsync(fd)
    except Exception as exc:
        try:
            path.unlink()
        except OSError:
            pass
        if isinstance(exc, OfficeRenderingError):
            raise
        raise CacheWriteError("Could not persist Office render cache metadata") from exc
    finally:
        os.close(fd)
    _harden_file(path)


def _harden_directory(path: Path) -> None:
    try:
        os.chmod(path, 0o700)
    except OSError:
        # Windows inherits the per-user app-data ACL; POSIX mode is best effort.
        pass


def _harden_file(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _fsync_regular_file(path: Path) -> None:
    flags = os.O_RDWR
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise CacheWriteError("Could not reopen Office render page for fsync") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise CacheWriteError("Office render page is not a regular file")
        try:
            os.fsync(fd)
        except OSError as exc:
            raise CacheWriteError("Could not fsync Office render page") from exc
    finally:
        os.close(fd)


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        # Directory fsync is unavailable on Windows and some filesystems.
        pass
    finally:
        os.close(fd)
