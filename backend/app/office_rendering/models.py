"""Versioned, renderer-neutral Office rendering contracts.

These models deliberately do not identify any concrete renderer as high
fidelity.  ``quality`` is supplied by a provider descriptor, persisted in the
manifest, and checked again by the cache boundary.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, TypeAlias, cast

from app.office_rendering.errors import RenderContractError


RENDER_MANIFEST_SCHEMA_VERSION = 2
AUTHORITATIVE_QUALITY = "authoritative"
APPROXIMATE_QUALITY = "approximate"
RenderQuality: TypeAlias = Literal["authoritative", "approximate"]
OfficeDocumentFormat: TypeAlias = Literal["docx", "xlsx", "pptx"]
JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ARTIFACT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_TEXT_LENGTH = 256


def validate_sha256(value: object, field_name: str) -> str:
    """Return a canonical SHA-256 or reject the containing contract."""

    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise RenderContractError(f"{field_name} must be a lowercase SHA-256")
    return value


def _bounded_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise RenderContractError(f"{field_name} must be a string")
    text = value.strip()
    if (
        not text
        or len(text) > _MAX_TEXT_LENGTH
        or any(ord(character) < 32 for character in text)
    ):
        raise RenderContractError(f"{field_name} is invalid")
    return text


def _strict_positive_int(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise RenderContractError(f"{field_name} must be a positive integer")
    return value


def _strict_non_negative_int(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RenderContractError(f"{field_name} must be a non-negative integer")
    return value


def _freeze_json(value: Any, *, depth: int = 0) -> JsonValue:
    if depth > 20:
        raise RenderContractError("render parameters exceed the nesting limit")
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RenderContractError("render parameters contain a non-finite number")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > _MAX_TEXT_LENGTH:
                raise RenderContractError("render parameter keys must be bounded strings")
            if key in frozen:
                raise RenderContractError("render parameter keys must be unique")
            frozen[key] = _freeze_json(item, depth=depth + 1)
        return cast(JsonValue, MappingProxyType(frozen))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_freeze_json(item, depth=depth + 1) for item in value)
    raise RenderContractError("render parameters must be JSON-compatible")


def _thaw_json(value: JsonValue) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def canonical_json_bytes(value: Any) -> bytes:
    """Encode JSON deterministically after enforcing the bounded JSON subset."""

    frozen = _freeze_json(value)
    return json.dumps(
        _thaw_json(frozen),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class RenderRequest:
    """One render request pinned to a source content digest.

    ``workspace_root`` is part of the trust boundary but intentionally not the
    cache key: identical content and parameters may share a local cache entry.
    The cache validates that ``source_path`` resolves inside it and matches
    ``source_sha256`` before every read and both before and after rendering.
    """

    workspace_root: Path
    source_path: Path
    document_format: OfficeDocumentFormat
    source_sha256: str
    parameters_version: str
    parameters: Mapping[str, JsonValue] = field(default_factory=dict, repr=False)
    _parameters_json: bytes = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        root = Path(self.workspace_root).expanduser()
        source = Path(self.source_path).expanduser()
        if not root.is_absolute() or not source.is_absolute():
            raise RenderContractError("render source paths must be absolute")
        object.__setattr__(self, "workspace_root", root)
        object.__setattr__(self, "source_path", source)
        if self.document_format not in {"docx", "xlsx", "pptx"}:
            raise RenderContractError("document_format must be docx, xlsx, or pptx")
        if source.suffix.lower() != f".{self.document_format}":
            raise RenderContractError(
                "source extension must match the explicit document_format"
            )
        object.__setattr__(
            self,
            "source_sha256",
            validate_sha256(self.source_sha256, "source_sha256"),
        )
        object.__setattr__(
            self,
            "parameters_version",
            _bounded_text(self.parameters_version, "parameters_version"),
        )
        frozen = _freeze_json(self.parameters)
        if not isinstance(frozen, Mapping):
            raise RenderContractError("render parameters must be a mapping")
        object.__setattr__(self, "parameters", frozen)
        object.__setattr__(self, "_parameters_json", canonical_json_bytes(frozen))

    @property
    def parameters_json(self) -> bytes:
        return self._parameters_json

    @property
    def parameters_sha256(self) -> str:
        return hashlib.sha256(self._parameters_json).hexdigest()

    def parameters_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], _thaw_json(cast(JsonValue, self.parameters)))


@dataclass(frozen=True, slots=True)
class RendererDescriptor:
    """Immutable identity and claimed quality of one renderer build."""

    renderer_id: str
    renderer_version: str
    font_digest: str
    quality: RenderQuality

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "renderer_id", _bounded_text(self.renderer_id, "renderer_id")
        )
        object.__setattr__(
            self,
            "renderer_version",
            _bounded_text(self.renderer_version, "renderer_version"),
        )
        object.__setattr__(
            self,
            "font_digest",
            validate_sha256(self.font_digest, "font_digest"),
        )
        if self.quality not in {AUTHORITATIVE_QUALITY, APPROXIMATE_QUALITY}:
            raise RenderContractError(
                "quality must be 'authoritative' or 'approximate'"
            )


@dataclass(frozen=True, slots=True)
class PageArtifact:
    """One validated, one-indexed PNG page or slide.

    ``sha256`` binds the encoded PNG container while ``pixel_sha256`` binds
    the decoded, row-major RGBA bytes.  Keeping both prevents container-only
    evidence from being mistaken for visual evidence.
    """

    page_number: int
    filename: str
    sha256: str
    pixel_sha256: str
    size_bytes: int
    width_px: int
    height_px: int
    mime_type: str = "image/png"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "page_number",
            _strict_positive_int(self.page_number, "page_number"),
        )
        if (
            not isinstance(self.filename, str)
            or _ARTIFACT_NAME_PATTERN.fullmatch(self.filename) is None
            or not self.filename.lower().endswith(".png")
        ):
            raise RenderContractError("page filename must be a safe PNG basename")
        object.__setattr__(
            self, "sha256", validate_sha256(self.sha256, "page sha256")
        )
        object.__setattr__(
            self,
            "pixel_sha256",
            validate_sha256(self.pixel_sha256, "page pixel_sha256"),
        )
        object.__setattr__(
            self,
            "size_bytes",
            _strict_non_negative_int(self.size_bytes, "page size_bytes"),
        )
        object.__setattr__(
            self, "width_px", _strict_positive_int(self.width_px, "page width_px")
        )
        object.__setattr__(
            self,
            "height_px",
            _strict_positive_int(self.height_px, "page height_px"),
        )
        if self.mime_type != "image/png":
            raise RenderContractError("page mime_type must be image/png")

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_number": self.page_number,
            "filename": self.filename,
            "sha256": self.sha256,
            "pixel_sha256": self.pixel_sha256,
            "size_bytes": self.size_bytes,
            "width_px": self.width_px,
            "height_px": self.height_px,
            "mime_type": self.mime_type,
        }

    @classmethod
    def from_dict(cls, value: object) -> PageArtifact:
        expected = {
            "page_number",
            "filename",
            "sha256",
            "pixel_sha256",
            "size_bytes",
            "width_px",
            "height_px",
            "mime_type",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise RenderContractError("page artifact fields do not match the schema")
        return cls(
            page_number=_strict_positive_int(value["page_number"], "page_number"),
            filename=cast(str, value["filename"]),
            sha256=cast(str, value["sha256"]),
            pixel_sha256=cast(str, value["pixel_sha256"]),
            size_bytes=_strict_non_negative_int(value["size_bytes"], "size_bytes"),
            width_px=_strict_positive_int(value["width_px"], "width_px"),
            height_px=_strict_positive_int(value["height_px"], "height_px"),
            mime_type=cast(str, value["mime_type"]),
        )


@dataclass(frozen=True, slots=True)
class PdfArtifact:
    """The validated PDF from which all declared PNG pages were rasterized."""

    filename: str
    sha256: str
    size_bytes: int
    page_count: int
    mime_type: str = "application/pdf"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.filename, str)
            or _ARTIFACT_NAME_PATTERN.fullmatch(self.filename) is None
            or not self.filename.lower().endswith(".pdf")
        ):
            raise RenderContractError("PDF filename must be a safe PDF basename")
        object.__setattr__(
            self, "sha256", validate_sha256(self.sha256, "PDF sha256")
        )
        object.__setattr__(
            self,
            "size_bytes",
            _strict_non_negative_int(self.size_bytes, "PDF size_bytes"),
        )
        object.__setattr__(
            self,
            "page_count",
            _strict_positive_int(self.page_count, "PDF page_count"),
        )
        if self.mime_type != "application/pdf":
            raise RenderContractError("PDF mime_type must be application/pdf")

    def to_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "page_count": self.page_count,
            "mime_type": self.mime_type,
        }

    @classmethod
    def from_dict(cls, value: object) -> PdfArtifact:
        expected = {
            "filename",
            "sha256",
            "size_bytes",
            "page_count",
            "mime_type",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise RenderContractError("PDF artifact fields do not match the schema")
        return cls(
            filename=cast(str, value["filename"]),
            sha256=cast(str, value["sha256"]),
            size_bytes=_strict_non_negative_int(value["size_bytes"], "size_bytes"),
            page_count=_strict_positive_int(value["page_count"], "page_count"),
            mime_type=cast(str, value["mime_type"]),
        )


@dataclass(frozen=True, slots=True)
class RenderManifest:
    """Content-addressed description of a complete renderer output."""

    cache_key: str
    source_sha256: str
    document_format: OfficeDocumentFormat
    renderer_id: str
    renderer_version: str
    font_digest: str
    parameters_version: str
    parameters_sha256: str
    quality: RenderQuality
    pdf: PdfArtifact
    pages: tuple[PageArtifact, ...]
    schema_version: int = RENDER_MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != RENDER_MANIFEST_SCHEMA_VERSION:
            raise RenderContractError("unsupported render manifest schema version")
        object.__setattr__(
            self, "cache_key", validate_sha256(self.cache_key, "cache_key")
        )
        object.__setattr__(
            self,
            "source_sha256",
            validate_sha256(self.source_sha256, "source_sha256"),
        )
        if self.document_format not in {"docx", "xlsx", "pptx"}:
            raise RenderContractError("document_format must be docx, xlsx, or pptx")
        object.__setattr__(
            self, "renderer_id", _bounded_text(self.renderer_id, "renderer_id")
        )
        object.__setattr__(
            self,
            "renderer_version",
            _bounded_text(self.renderer_version, "renderer_version"),
        )
        object.__setattr__(
            self,
            "font_digest",
            validate_sha256(self.font_digest, "font_digest"),
        )
        object.__setattr__(
            self,
            "parameters_version",
            _bounded_text(self.parameters_version, "parameters_version"),
        )
        object.__setattr__(
            self,
            "parameters_sha256",
            validate_sha256(self.parameters_sha256, "parameters_sha256"),
        )
        if self.quality not in {AUTHORITATIVE_QUALITY, APPROXIMATE_QUALITY}:
            raise RenderContractError(
                "quality must be 'authoritative' or 'approximate'"
            )
        if not isinstance(self.pdf, PdfArtifact):
            raise RenderContractError("render manifest must contain a PDF artifact")
        pages = tuple(self.pages)
        if not pages or any(not isinstance(page, PageArtifact) for page in pages):
            raise RenderContractError("render manifest must contain page artifacts")
        expected_numbers = list(range(1, len(pages) + 1))
        if [page.page_number for page in pages] != expected_numbers:
            raise RenderContractError("render pages must be ordered and contiguous")
        filenames = [page.filename for page in pages]
        if len(filenames) != len(set(filenames)):
            raise RenderContractError("render page filenames must be unique")
        if self.pdf.filename in filenames:
            raise RenderContractError("render artifact filenames must be unique")
        if self.pdf.page_count != len(pages):
            raise RenderContractError(
                "render PDF page count must match the declared PNG pages"
            )
        object.__setattr__(self, "pages", pages)

    @classmethod
    def for_request(
        cls,
        request: RenderRequest,
        descriptor: RendererDescriptor,
        pages: Sequence[PageArtifact],
        *,
        pdf: PdfArtifact,
    ) -> RenderManifest:
        return cls(
            cache_key=build_cache_key(request, descriptor),
            source_sha256=request.source_sha256,
            document_format=request.document_format,
            renderer_id=descriptor.renderer_id,
            renderer_version=descriptor.renderer_version,
            font_digest=descriptor.font_digest,
            parameters_version=request.parameters_version,
            parameters_sha256=request.parameters_sha256,
            quality=descriptor.quality,
            pdf=pdf,
            pages=tuple(pages),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "cache_key": self.cache_key,
            "source_sha256": self.source_sha256,
            "document_format": self.document_format,
            "renderer_id": self.renderer_id,
            "renderer_version": self.renderer_version,
            "font_digest": self.font_digest,
            "parameters_version": self.parameters_version,
            "parameters_sha256": self.parameters_sha256,
            "quality": self.quality,
            "pdf": self.pdf.to_dict(),
            "pages": [page.to_dict() for page in self.pages],
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict()) + b"\n"

    @classmethod
    def from_dict(cls, value: object) -> RenderManifest:
        expected = {
            "schema_version",
            "cache_key",
            "source_sha256",
            "document_format",
            "renderer_id",
            "renderer_version",
            "font_digest",
            "parameters_version",
            "parameters_sha256",
            "quality",
            "pdf",
            "pages",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise RenderContractError("render manifest fields do not match the schema")
        schema_version = value["schema_version"]
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise RenderContractError("render manifest schema_version must be an integer")
        raw_pages = value["pages"]
        if not isinstance(raw_pages, list):
            raise RenderContractError("render manifest pages must be a list")
        return cls(
            schema_version=schema_version,
            cache_key=cast(str, value["cache_key"]),
            source_sha256=cast(str, value["source_sha256"]),
            document_format=cast(OfficeDocumentFormat, value["document_format"]),
            renderer_id=cast(str, value["renderer_id"]),
            renderer_version=cast(str, value["renderer_version"]),
            font_digest=cast(str, value["font_digest"]),
            parameters_version=cast(str, value["parameters_version"]),
            parameters_sha256=cast(str, value["parameters_sha256"]),
            quality=cast(RenderQuality, value["quality"]),
            pdf=PdfArtifact.from_dict(value["pdf"]),
            pages=tuple(PageArtifact.from_dict(page) for page in raw_pages),
        )


def build_cache_key(
    request: RenderRequest,
    descriptor: RendererDescriptor,
) -> str:
    """Build the cache key from every renderer reproducibility input."""

    payload = {
        "schema_version": RENDER_MANIFEST_SCHEMA_VERSION,
        "source_sha256": request.source_sha256,
        "document_format": request.document_format,
        "renderer_id": descriptor.renderer_id,
        "renderer_version": descriptor.renderer_version,
        "font_digest": descriptor.font_digest,
        "quality": descriptor.quality,
        "parameters_version": request.parameters_version,
        "parameters_sha256": request.parameters_sha256,
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
