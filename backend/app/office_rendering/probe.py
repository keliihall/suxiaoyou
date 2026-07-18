"""Real-execution release probe for the authoritative Office renderer.

Metadata and signatures establish *which* private renderer bundle was loaded;
this contract additionally proves that the provider executes the bundle's
reviewed DOCX probe and still produces the reviewed 144-DPI pixels.  Both probe
inputs live inside the signed bundle tree.  Reports contain hashes and counts
only -- never filesystem paths, filenames, document text, or font names.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
from typing import Any, Final, Mapping, cast
from xml.etree import ElementTree
import zipfile

from PIL import Image, UnidentifiedImageError
from fontTools.ttLib import TTFont, TTLibError
from pypdf import PdfReader

from app.office_rendering.deployment import (
    AttestedOfficeRendererDeployment,
    OfficeRendererDeploymentError,
    bind_authoritative_renderer_probe,
    fingerprint_office_renderer_bundle,
)
from app.office_rendering.errors import OfficeRenderingError, RenderContractError
from app.office_rendering.libreoffice import LIBREOFFICE_PARAMETERS_VERSION
from app.office_rendering.models import (
    AUTHORITATIVE_QUALITY,
    RenderManifest,
    RenderRequest,
    RendererDescriptor,
    build_cache_key,
    canonical_json_bytes,
    validate_sha256,
)
from app.office_rendering.provider import OfficeRenderProvider, ProviderAvailability


PROBE_SCHEMA_VERSION: Final = 1
PROBE_DPI: Final = 144
# The authoritative deployment delegates to LibreOfficeRenderProvider, which
# rejects every other request contract.  The golden is still independently
# versioned by PROBE_SCHEMA_VERSION and pinned to exactly 144 DPI.
PROBE_PARAMETERS_VERSION: Final = LIBREOFFICE_PARAMETERS_VERSION
PROBE_DIRECTORY: Final = PurePosixPath("probe")
PROBE_MANIFEST_FILENAME: Final = "authoritative-renderer-probe.json"
PROBE_SOURCE_FILENAME: Final = "authoritative-renderer-probe.docx"
MAX_PROBE_MANIFEST_BYTES: Final = 64 * 1024
MAX_PROBE_SOURCE_BYTES: Final = 32 * 1024 * 1024
MAX_PROBE_OUTPUT_BYTES: Final = 256 * 1024 * 1024
MAX_PROBE_PAGES: Final = 32
MAX_PROBE_PAGE_PIXELS: Final = 100_000_000
MAX_DOCX_ENTRIES: Final = 1_024
MAX_DOCX_UNCOMPRESSED_BYTES: Final = 64 * 1024 * 1024
MAX_EMBEDDED_FONT_BYTES: Final = 128 * 1024 * 1024
_PDF_MAGIC: Final = b"%PDF-"
_PNG_MAGIC: Final = b"\x89PNG\r\n\x1a\n"
_FONT_FILE_KEYS: Final = ("/FontFile", "/FontFile2", "/FontFile3")
_REQUIRED_DOCX_PARTS: Final = frozenset(
    {"[Content_Types].xml", "_rels/.rels", "word/document.xml"}
)
_GENERIC_FAILURE: Final = "Authoritative Office renderer execution probe failed"


class AuthoritativeRendererProbeError(OfficeRenderingError):
    """The signed real-render probe cannot be accepted as release evidence."""


def _positive_int(value: object, field: str, *, maximum: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= maximum
    ):
        raise AuthoritativeRendererProbeError(f"renderer probe {field} is invalid")
    return value


@dataclass(frozen=True, slots=True)
class AuthoritativeRendererProbePage:
    page_number: int
    width_px: int
    height_px: int
    pixel_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "page_number",
            _positive_int(self.page_number, "page_number", maximum=MAX_PROBE_PAGES),
        )
        object.__setattr__(
            self,
            "width_px",
            _positive_int(self.width_px, "width_px", maximum=100_000),
        )
        object.__setattr__(
            self,
            "height_px",
            _positive_int(self.height_px, "height_px", maximum=100_000),
        )
        if self.width_px * self.height_px > MAX_PROBE_PAGE_PIXELS:
            raise AuthoritativeRendererProbeError(
                "renderer probe page dimensions exceed the pixel budget"
            )
        try:
            digest = validate_sha256(self.pixel_sha256, "probe pixel_sha256")
        except RenderContractError as exc:
            raise AuthoritativeRendererProbeError(
                "renderer probe pixel identity is invalid"
            ) from exc
        object.__setattr__(self, "pixel_sha256", digest)

    def to_dict(self) -> dict[str, object]:
        return {
            "height_px": self.height_px,
            "page_number": self.page_number,
            "pixel_sha256": self.pixel_sha256,
            "width_px": self.width_px,
        }

    @classmethod
    def from_dict(cls, value: object) -> AuthoritativeRendererProbePage:
        if not isinstance(value, dict) or set(value) != {
            "height_px",
            "page_number",
            "pixel_sha256",
            "width_px",
        }:
            raise AuthoritativeRendererProbeError(
                "renderer probe page fields are invalid"
            )
        return cls(
            page_number=cast(int, value["page_number"]),
            width_px=cast(int, value["width_px"]),
            height_px=cast(int, value["height_px"]),
            pixel_sha256=cast(str, value["pixel_sha256"]),
        )


@dataclass(frozen=True, slots=True)
class AuthoritativeRendererProbeManifest:
    source_sha256: str
    pages: tuple[AuthoritativeRendererProbePage, ...]
    dpi: int = PROBE_DPI
    schema_version: int = PROBE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != PROBE_SCHEMA_VERSION or self.dpi != PROBE_DPI:
            raise AuthoritativeRendererProbeError(
                "renderer probe schema or DPI is unsupported"
            )
        try:
            source_digest = validate_sha256(
                self.source_sha256,
                "probe source_sha256",
            )
        except RenderContractError as exc:
            raise AuthoritativeRendererProbeError(
                "renderer probe source identity is invalid"
            ) from exc
        object.__setattr__(self, "source_sha256", source_digest)
        pages = tuple(self.pages)
        if (
            not pages
            or len(pages) > MAX_PROBE_PAGES
            or any(not isinstance(page, AuthoritativeRendererProbePage) for page in pages)
            or [page.page_number for page in pages]
            != list(range(1, len(pages) + 1))
        ):
            raise AuthoritativeRendererProbeError(
                "renderer probe page sequence is invalid"
            )
        object.__setattr__(self, "pages", pages)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def to_dict(self) -> dict[str, object]:
        return {
            "dpi": self.dpi,
            "page_count": self.page_count,
            "pages": [page.to_dict() for page in self.pages],
            "schema_version": self.schema_version,
            "source_sha256": self.source_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict()) + b"\n"

    @classmethod
    def from_dict(cls, value: object) -> AuthoritativeRendererProbeManifest:
        if not isinstance(value, dict) or set(value) != {
            "dpi",
            "page_count",
            "pages",
            "schema_version",
            "source_sha256",
        }:
            raise AuthoritativeRendererProbeError(
                "renderer probe manifest fields are invalid"
            )
        raw_pages = value["pages"]
        if not isinstance(raw_pages, list):
            raise AuthoritativeRendererProbeError(
                "renderer probe manifest pages are invalid"
            )
        manifest = cls(
            schema_version=cast(int, value["schema_version"]),
            dpi=cast(int, value["dpi"]),
            source_sha256=cast(str, value["source_sha256"]),
            pages=tuple(
                AuthoritativeRendererProbePage.from_dict(page) for page in raw_pages
            ),
        )
        expected_page_count = _positive_int(
            value["page_count"],
            "page_count",
            maximum=MAX_PROBE_PAGES,
        )
        if manifest.page_count != expected_page_count:
            raise AuthoritativeRendererProbeError(
                "renderer probe manifest page count is invalid"
            )
        return manifest


@dataclass(frozen=True, slots=True)
class AuthoritativeRendererProbeReport:
    bundle_tree_sha256: str
    probe_manifest_sha256: str
    probe_source_sha256: str
    render_manifest_sha256: str
    pdf_sha256: str
    page_count: int
    embedded_font_count: int
    pages: tuple[AuthoritativeRendererProbePage, ...]
    schema_version: int = PROBE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != PROBE_SCHEMA_VERSION:
            raise AuthoritativeRendererProbeError(
                "renderer probe report schema is invalid"
            )
        for field in (
            "bundle_tree_sha256",
            "probe_manifest_sha256",
            "probe_source_sha256",
            "render_manifest_sha256",
            "pdf_sha256",
        ):
            try:
                digest = validate_sha256(getattr(self, field), field)
            except RenderContractError as exc:
                raise AuthoritativeRendererProbeError(
                    "renderer probe report identity is invalid"
                ) from exc
            object.__setattr__(self, field, digest)
        pages = tuple(self.pages)
        page_count = _positive_int(
            self.page_count,
            "report page_count",
            maximum=MAX_PROBE_PAGES,
        )
        embedded_font_count = _positive_int(
            self.embedded_font_count,
            "report embedded_font_count",
            maximum=10_000,
        )
        if len(pages) != page_count:
            raise AuthoritativeRendererProbeError(
                "renderer probe report page count is invalid"
            )
        if (
            any(not isinstance(page, AuthoritativeRendererProbePage) for page in pages)
            or [page.page_number for page in pages]
            != list(range(1, page_count + 1))
        ):
            raise AuthoritativeRendererProbeError(
                "renderer probe report page sequence is invalid"
            )
        object.__setattr__(self, "page_count", page_count)
        object.__setattr__(self, "embedded_font_count", embedded_font_count)
        object.__setattr__(self, "pages", pages)

    def to_dict(self) -> dict[str, object]:
        return {
            "bundle_tree_sha256": self.bundle_tree_sha256,
            "embedded_font_count": self.embedded_font_count,
            "page_count": self.page_count,
            "pages": [page.to_dict() for page in self.pages],
            "pdf_sha256": self.pdf_sha256,
            "probe_manifest_sha256": self.probe_manifest_sha256,
            "probe_source_sha256": self.probe_source_sha256,
            "render_manifest_sha256": self.render_manifest_sha256,
            "schema_version": self.schema_version,
        }


async def execute_authoritative_office_renderer_probe(
    provider: OfficeRenderProvider,
    *,
    bundle_root: Path,
    expected_bundle_tree_sha256: str,
) -> AuthoritativeRendererProbeReport:
    """Execute and verify the signed golden DOCX using ``provider.render``.

    ``expected_bundle_tree_sha256`` must come from the release attestation.  A
    lower-level injectable entry point is intentional: tests can exercise the
    complete verifier with a provider that really writes artifacts, while
    production uses :func:`run_attested_authoritative_office_renderer_probe`.
    """

    try:
        return await _execute_probe(
            provider,
            bundle_root=Path(bundle_root),
            expected_bundle_tree_sha256=expected_bundle_tree_sha256,
        )
    except asyncio.CancelledError:
        raise
    except AuthoritativeRendererProbeError:
        raise
    except Exception as exc:
        raise AuthoritativeRendererProbeError(_GENERIC_FAILURE) from exc


async def run_attested_authoritative_office_renderer_probe(
    provider: OfficeRenderProvider,
    *,
    deployment: AttestedOfficeRendererDeployment | None = None,
) -> AuthoritativeRendererProbeReport:
    """Execute the probe using only an attestation-derived deployment binding."""

    try:
        binding = bind_authoritative_renderer_probe(
            provider,
            deployment=deployment,
        )
    except OfficeRendererDeploymentError as exc:
        raise AuthoritativeRendererProbeError(_GENERIC_FAILURE) from exc
    return await execute_authoritative_office_renderer_probe(
        provider,
        bundle_root=binding.bundle_root,
        expected_bundle_tree_sha256=binding.bundle_tree_sha256,
    )


async def _execute_probe(
    provider: OfficeRenderProvider,
    *,
    bundle_root: Path,
    expected_bundle_tree_sha256: str,
) -> AuthoritativeRendererProbeReport:
    if not isinstance(provider, OfficeRenderProvider):
        raise AuthoritativeRendererProbeError("renderer probe provider is invalid")
    try:
        expected_tree = validate_sha256(
            expected_bundle_tree_sha256,
            "renderer probe bundle tree",
        )
    except RenderContractError as exc:
        raise AuthoritativeRendererProbeError(
            "renderer probe bundle identity is invalid"
        ) from exc
    if fingerprint_office_renderer_bundle(bundle_root) != expected_tree:
        raise AuthoritativeRendererProbeError(
            "renderer probe bundle tree does not match the attestation"
        )
    resolved_root = _validated_bundle_root(bundle_root)
    manifest_bytes = _read_bound_file(
        resolved_root,
        PROBE_DIRECTORY / PROBE_MANIFEST_FILENAME,
        max_bytes=MAX_PROBE_MANIFEST_BYTES,
    )
    manifest = _parse_probe_manifest(manifest_bytes)
    source_bytes = _read_bound_file(
        resolved_root,
        PROBE_DIRECTORY / PROBE_SOURCE_FILENAME,
        max_bytes=MAX_PROBE_SOURCE_BYTES,
    )
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    if source_sha256 != manifest.source_sha256:
        raise AuthoritativeRendererProbeError(
            "renderer probe DOCX does not match its canonical manifest"
        )
    _validate_docx_source(source_bytes)

    descriptor = provider.descriptor
    availability = provider.availability()
    if (
        not isinstance(descriptor, RendererDescriptor)
        or descriptor.quality != AUTHORITATIVE_QUALITY
        or not isinstance(availability, ProviderAvailability)
        or not availability.available
    ):
        raise AuthoritativeRendererProbeError(
            "renderer probe provider is not authoritative and available"
        )
    source_path = resolved_root.joinpath(
        *PROBE_DIRECTORY.parts,
        PROBE_SOURCE_FILENAME,
    )
    request = RenderRequest(
        workspace_root=resolved_root,
        source_path=source_path,
        document_format="docx",
        source_sha256=source_sha256,
        parameters_version=PROBE_PARAMETERS_VERSION,
        parameters={"dpi": PROBE_DPI},
    )

    with tempfile.TemporaryDirectory(prefix="suxiaoyou-office-probe-") as temp:
        output_dir = Path(temp).resolve(strict=True) / "output"
        output_dir.mkdir(mode=0o700)
        rendered = await provider.render(request, output_dir)
        if provider.descriptor != descriptor:
            raise AuthoritativeRendererProbeError(
                "renderer probe provider identity changed during rendering"
            )
        after_availability = provider.availability()
        if (
            not isinstance(after_availability, ProviderAvailability)
            or not after_availability.available
        ):
            raise AuthoritativeRendererProbeError(
                "renderer probe provider became unavailable during rendering"
            )
        report = _verify_rendered_output(
            output_dir,
            rendered,
            request=request,
            descriptor=descriptor,
            golden=manifest,
            bundle_tree_sha256=expected_tree,
            probe_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        )

    if fingerprint_office_renderer_bundle(resolved_root) != expected_tree:
        raise AuthoritativeRendererProbeError(
            "renderer probe bundle changed during execution"
        )
    return report


def _parse_probe_manifest(raw: bytes) -> AuthoritativeRendererProbeManifest:
    try:
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_no_duplicate_object,
        )
        manifest = AuthoritativeRendererProbeManifest.from_dict(decoded)
    except AuthoritativeRendererProbeError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise AuthoritativeRendererProbeError(
            "renderer probe manifest is invalid"
        ) from exc
    if raw != manifest.canonical_bytes():
        raise AuthoritativeRendererProbeError(
            "renderer probe manifest is not canonical"
        )
    return manifest


def _validated_bundle_root(root: Path) -> Path:
    try:
        info = root.lstat()
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise AuthoritativeRendererProbeError(
            "renderer probe bundle is unavailable"
        ) from exc
    if root.is_symlink() or not stat.S_ISDIR(info.st_mode):
        raise AuthoritativeRendererProbeError("renderer probe bundle is invalid")
    return resolved


def _read_bound_file(root: Path, relative: PurePosixPath, *, max_bytes: int) -> bytes:
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise AuthoritativeRendererProbeError("renderer probe file is invalid")
    candidate = root.joinpath(*relative.parts)
    try:
        parent = candidate.parent.resolve(strict=True)
        parent.relative_to(root)
        parent_info = candidate.parent.lstat()
        if candidate.parent.is_symlink() or not stat.S_ISDIR(parent_info.st_mode):
            raise AuthoritativeRendererProbeError("renderer probe file is invalid")
        visible_before = candidate.lstat()
    except (OSError, ValueError) as exc:
        raise AuthoritativeRendererProbeError(
            "renderer probe file is unavailable"
        ) from exc
    if (
        candidate.is_symlink()
        or not stat.S_ISREG(visible_before.st_mode)
        or visible_before.st_size < 1
        or visible_before.st_size > max_bytes
    ):
        raise AuthoritativeRendererProbeError("renderer probe file is invalid")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise AuthoritativeRendererProbeError(
            "renderer probe file is unavailable"
        ) from exc
    chunks: list[bytes] = []
    total = 0
    try:
        before = os.fstat(descriptor)
        while chunk := os.read(descriptor, 1024 * 1024):
            total += len(chunk)
            if total > max_bytes:
                raise AuthoritativeRendererProbeError(
                    "renderer probe file exceeds its byte budget"
                )
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        visible_after = candidate.lstat()
    except OSError as exc:
        raise AuthoritativeRendererProbeError("renderer probe file changed") from exc
    if (
        total != before.st_size
        or _stat_identity(before) != _stat_identity(after)
        or _stat_identity(after) != _stat_identity(visible_after)
    ):
        raise AuthoritativeRendererProbeError("renderer probe file changed")
    return b"".join(chunks)


def _stat_identity(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        stat.S_IMODE(info.st_mode),
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _validate_docx_source(content: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            infos = archive.infolist()
            if not 1 <= len(infos) <= MAX_DOCX_ENTRIES:
                raise AuthoritativeRendererProbeError(
                    "renderer probe DOCX entry count is invalid"
                )
            names: set[str] = set()
            total = 0
            for info in infos:
                name = info.filename
                path = PurePosixPath(name)
                if (
                    not name
                    or "\\" in name
                    or path.is_absolute()
                    or any(part in {"", ".", ".."} for part in path.parts)
                    or name in names
                    or info.flag_bits & 0x1
                ):
                    raise AuthoritativeRendererProbeError(
                        "renderer probe DOCX structure is invalid"
                    )
                names.add(name)
                if info.is_dir():
                    continue
                unix_mode = (info.external_attr >> 16) & 0o170000
                if unix_mode == stat.S_IFLNK:
                    raise AuthoritativeRendererProbeError(
                        "renderer probe DOCX structure is invalid"
                    )
                total += info.file_size
                if total > MAX_DOCX_UNCOMPRESSED_BYTES:
                    raise AuthoritativeRendererProbeError(
                        "renderer probe DOCX exceeds its byte budget"
                    )
                if (
                    info.file_size > 0
                    and (
                        info.compress_size < 1
                        or info.file_size / info.compress_size > 250
                    )
                ):
                    raise AuthoritativeRendererProbeError(
                        "renderer probe DOCX compression is invalid"
                    )
            if not _REQUIRED_DOCX_PARTS.issubset(names):
                raise AuthoritativeRendererProbeError(
                    "renderer probe DOCX parts are incomplete"
                )
            _validate_docx_xml(archive)
    except AuthoritativeRendererProbeError:
        raise
    except (OSError, zipfile.BadZipFile, ElementTree.ParseError, KeyError) as exc:
        raise AuthoritativeRendererProbeError(
            "renderer probe DOCX is invalid"
        ) from exc


def _validate_docx_xml(archive: zipfile.ZipFile) -> None:
    document = ElementTree.fromstring(archive.read("word/document.xml"))
    if not document.tag.endswith("}document"):
        raise AuthoritativeRendererProbeError(
            "renderer probe DOCX main document is invalid"
        )
    content_types = ElementTree.fromstring(archive.read("[Content_Types].xml"))
    if not any(
        child.attrib.get("PartName") == "/word/document.xml"
        and "wordprocessingml.document.main+xml"
        in child.attrib.get("ContentType", "")
        for child in content_types
    ):
        raise AuthoritativeRendererProbeError(
            "renderer probe DOCX content type is invalid"
        )
    for info in archive.infolist():
        if not info.filename.endswith(".rels") or info.is_dir():
            continue
        relationships = ElementTree.fromstring(archive.read(info))
        if any(
            child.attrib.get("TargetMode", "").lower() == "external"
            for child in relationships
        ):
            raise AuthoritativeRendererProbeError(
                "renderer probe DOCX has an external relationship"
            )


def _verify_rendered_output(
    output_dir: Path,
    rendered: object,
    *,
    request: RenderRequest,
    descriptor: RendererDescriptor,
    golden: AuthoritativeRendererProbeManifest,
    bundle_tree_sha256: str,
    probe_manifest_sha256: str,
) -> AuthoritativeRendererProbeReport:
    if not isinstance(rendered, RenderManifest):
        raise AuthoritativeRendererProbeError(
            "renderer probe provider returned no render manifest"
        )
    expected_identity = (
        build_cache_key(request, descriptor),
        request.source_sha256,
        request.document_format,
        descriptor.renderer_id,
        descriptor.renderer_version,
        descriptor.font_digest,
        request.parameters_version,
        request.parameters_sha256,
        descriptor.quality,
    )
    actual_identity = (
        rendered.cache_key,
        rendered.source_sha256,
        rendered.document_format,
        rendered.renderer_id,
        rendered.renderer_version,
        rendered.font_digest,
        rendered.parameters_version,
        rendered.parameters_sha256,
        rendered.quality,
    )
    if actual_identity != expected_identity:
        raise AuthoritativeRendererProbeError(
            "renderer probe manifest identity is invalid"
        )
    if len(rendered.pages) != len(golden.pages):
        raise AuthoritativeRendererProbeError(
            "renderer probe page count does not match the golden manifest"
        )
    _validate_output_directory(output_dir, rendered)

    pdf_bytes = _read_output_file(
        output_dir,
        rendered.pdf.filename,
        max_bytes=MAX_PROBE_OUTPUT_BYTES,
    )
    if (
        len(pdf_bytes) != rendered.pdf.size_bytes
        or hashlib.sha256(pdf_bytes).hexdigest() != rendered.pdf.sha256
        or not pdf_bytes.startswith(_PDF_MAGIC)
    ):
        raise AuthoritativeRendererProbeError(
            "renderer probe PDF identity is invalid"
        )
    pdf_page_count, embedded_font_count = _inspect_pdf(
        pdf_bytes,
        expected_pages=golden.pages,
    )
    if (
        pdf_page_count != len(golden.pages)
        or rendered.pdf.page_count != pdf_page_count
    ):
        raise AuthoritativeRendererProbeError(
            "renderer probe PDF page count does not match"
        )
    if embedded_font_count < 1:
        raise AuthoritativeRendererProbeError(
            "renderer probe PDF contains no embedded font"
        )

    actual_pages: list[AuthoritativeRendererProbePage] = []
    total_bytes = len(pdf_bytes)
    for artifact, expected in zip(rendered.pages, golden.pages, strict=True):
        remaining = MAX_PROBE_OUTPUT_BYTES - total_bytes
        if remaining < 1:
            raise AuthoritativeRendererProbeError(
                "renderer probe output exceeds its byte budget"
            )
        content = _read_output_file(
            output_dir,
            artifact.filename,
            max_bytes=remaining,
        )
        total_bytes += len(content)
        if (
            len(content) != artifact.size_bytes
            or hashlib.sha256(content).hexdigest() != artifact.sha256
            or not content.startswith(_PNG_MAGIC)
        ):
            raise AuthoritativeRendererProbeError(
                "renderer probe page identity is invalid"
            )
        width, height, pixel_sha256 = _decode_rgba_page(content)
        if (
            (artifact.page_number, artifact.width_px, artifact.height_px)
            != (expected.page_number, expected.width_px, expected.height_px)
            or (width, height) != (expected.width_px, expected.height_px)
            or pixel_sha256 != artifact.pixel_sha256
            or pixel_sha256 != expected.pixel_sha256
        ):
            raise AuthoritativeRendererProbeError(
                "renderer probe decoded pixels do not match the golden manifest"
            )
        actual_pages.append(expected)

    return AuthoritativeRendererProbeReport(
        bundle_tree_sha256=bundle_tree_sha256,
        probe_manifest_sha256=probe_manifest_sha256,
        probe_source_sha256=request.source_sha256,
        render_manifest_sha256=hashlib.sha256(rendered.canonical_bytes()).hexdigest(),
        pdf_sha256=rendered.pdf.sha256,
        page_count=len(actual_pages),
        embedded_font_count=embedded_font_count,
        pages=tuple(actual_pages),
    )


def _validate_output_directory(directory: Path, manifest: RenderManifest) -> None:
    try:
        info = directory.lstat()
        resolved = directory.resolve(strict=True)
        entries = list(directory.iterdir())
    except OSError as exc:
        raise AuthoritativeRendererProbeError(
            "renderer probe output is unavailable"
        ) from exc
    if directory.is_symlink() or not stat.S_ISDIR(info.st_mode) or resolved != directory:
        raise AuthoritativeRendererProbeError("renderer probe output is invalid")
    expected = {
        manifest.pdf.filename,
        *(page.filename for page in manifest.pages),
    }
    if (
        len(entries) != len(expected)
        or {entry.name for entry in entries} != expected
        or any(entry.is_symlink() or not entry.is_file() for entry in entries)
    ):
        raise AuthoritativeRendererProbeError(
            "renderer probe output artifact set is invalid"
        )


def _read_output_file(directory: Path, filename: str, *, max_bytes: int) -> bytes:
    return _read_bound_file(directory, PurePosixPath(filename), max_bytes=max_bytes)


def _decode_rgba_page(content: bytes) -> tuple[int, int, str]:
    try:
        with Image.open(io.BytesIO(content)) as image:
            image.load()
            if image.format != "PNG" or getattr(image, "n_frames", 1) != 1:
                raise AuthoritativeRendererProbeError(
                    "renderer probe page container is invalid"
                )
            width, height = image.size
            if width < 1 or height < 1 or width * height > MAX_PROBE_PAGE_PIXELS:
                raise AuthoritativeRendererProbeError(
                    "renderer probe page dimensions are invalid"
                )
            digest = hashlib.sha256(
                image.convert("RGBA").tobytes("raw", "RGBA")
            ).hexdigest()
            return width, height, digest
    except AuthoritativeRendererProbeError:
        raise
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        raise AuthoritativeRendererProbeError(
            "renderer probe page is invalid"
        ) from exc


def _inspect_pdf(
    content: bytes,
    *,
    expected_pages: tuple[AuthoritativeRendererProbePage, ...],
) -> tuple[int, int]:
    try:
        reader = PdfReader(io.BytesIO(content), strict=True)
        if reader.is_encrypted:
            raise AuthoritativeRendererProbeError(
                "renderer probe PDF is encrypted"
            )
        pages = len(reader.pages)
        if not 1 <= pages <= MAX_PROBE_PAGES:
            raise AuthoritativeRendererProbeError(
                "renderer probe PDF page count is invalid"
            )
        if pages != len(expected_pages):
            raise AuthoritativeRendererProbeError(
                "renderer probe PDF page count does not match"
            )
        for page, expected in zip(reader.pages, expected_pages, strict=True):
            width_points = float(page.mediabox.width)
            height_points = float(page.mediabox.height)
            rotation = int(page.get("/Rotate", 0) or 0) % 360
            if rotation in {90, 270}:
                width_points, height_points = height_points, width_points
            if (
                not math.isfinite(width_points)
                or not math.isfinite(height_points)
                or width_points <= 0
                or height_points <= 0
                or math.ceil(width_points * PROBE_DPI / 72.0)
                != expected.width_px
                or math.ceil(height_points * PROBE_DPI / 72.0)
                != expected.height_px
            ):
                raise AuthoritativeRendererProbeError(
                    "renderer probe PDF page dimensions do not match 144-DPI output"
                )
        embedded = _embedded_font_digests(reader)
        return pages, len(embedded)
    except AuthoritativeRendererProbeError:
        raise
    except Exception as exc:
        raise AuthoritativeRendererProbeError(
            "renderer probe PDF is structurally invalid"
        ) from exc


def _embedded_font_digests(reader: PdfReader) -> frozenset[str]:
    digests: set[str] = set()
    visited_resources: set[int] = set()
    for page in reader.pages:
        resources = _pdf_object(page.get("/Resources"))
        _collect_resource_fonts(
            resources,
            digests=digests,
            visited_resources=visited_resources,
            depth=0,
        )
    return frozenset(digests)


def _collect_resource_fonts(
    resources: object,
    *,
    digests: set[str],
    visited_resources: set[int],
    depth: int,
) -> None:
    if depth > 16 or not isinstance(resources, Mapping):
        return
    marker = id(resources)
    if marker in visited_resources or len(visited_resources) > 10_000:
        return
    visited_resources.add(marker)
    fonts = _pdf_object(resources.get("/Font"))
    if isinstance(fonts, Mapping):
        for raw_font in fonts.values():
            _collect_font_streams(_pdf_object(raw_font), digests)
    xobjects = _pdf_object(resources.get("/XObject"))
    if isinstance(xobjects, Mapping):
        for raw_xobject in xobjects.values():
            xobject = _pdf_object(raw_xobject)
            if isinstance(xobject, Mapping):
                _collect_resource_fonts(
                    _pdf_object(xobject.get("/Resources")),
                    digests=digests,
                    visited_resources=visited_resources,
                    depth=depth + 1,
                )


def _collect_font_streams(font: object, digests: set[str]) -> None:
    if not isinstance(font, Mapping):
        return
    candidates: list[Mapping[Any, Any]] = [font]
    descendants = _pdf_object(font.get("/DescendantFonts"))
    if isinstance(descendants, (list, tuple)):
        candidates.extend(
            resolved
            for item in descendants
            if isinstance((resolved := _pdf_object(item)), Mapping)
        )
    for candidate in candidates:
        descriptor = _pdf_object(candidate.get("/FontDescriptor"))
        if not isinstance(descriptor, Mapping):
            continue
        for key in _FONT_FILE_KEYS:
            stream = _pdf_object(descriptor.get(key))
            getter = getattr(stream, "get_data", None)
            if not callable(getter):
                continue
            content = getter()
            if not isinstance(content, bytes) or not content:
                continue
            if len(content) > MAX_EMBEDDED_FONT_BYTES:
                raise AuthoritativeRendererProbeError(
                    "renderer probe embedded font exceeds its byte budget"
                )
            if _is_valid_sfnt_font(content):
                digests.add(hashlib.sha256(content).hexdigest())


def _is_valid_sfnt_font(content: bytes) -> bool:
    """Reject a dummy FontFile stream that merely occupies a PDF key."""

    font: TTFont | None = None
    try:
        font = TTFont(
            io.BytesIO(content),
            lazy=False,
            recalcBBoxes=False,
            recalcTimestamp=False,
        )
        return bool(font.keys())
    except (TTLibError, OSError, ValueError):
        return False
    finally:
        if font is not None:
            font.close()


def _pdf_object(value: object) -> object:
    getter = getattr(value, "get_object", None)
    if callable(getter):
        return getter()
    return value


def _no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


__all__ = [
    "AuthoritativeRendererProbeError",
    "AuthoritativeRendererProbeManifest",
    "AuthoritativeRendererProbePage",
    "AuthoritativeRendererProbeReport",
    "PROBE_DIRECTORY",
    "PROBE_DPI",
    "PROBE_MANIFEST_FILENAME",
    "PROBE_PARAMETERS_VERSION",
    "PROBE_SCHEMA_VERSION",
    "PROBE_SOURCE_FILENAME",
    "execute_authoritative_office_renderer_probe",
    "run_attested_authoritative_office_renderer_probe",
]
