"""Signed, read-only first-party Office template delivery.

The application package contains a canonical catalog, a detached Ed25519
signature, and immutable OOXML assets.  The private release key is never part
of the source tree.  Every catalog read revalidates the signature, every asset
is rebound to its SHA-256 and placeholder contract, and instantiation imports a
private copy into the existing content-addressed registry before publishing a
new output.  Packaged assets are never edited in place.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Final, Literal, TypeAlias, cast

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.office_templates.errors import (
    TemplateContractError,
    TemplateFeatureDisabledError,
    TemplateIntegrityError,
    TemplateNotFoundError,
)
from app.office_templates.instantiation import OfficeTemplateInstantiator
from app.office_templates.models import (
    AllowedOutputRules,
    OfficeTemplateFormat,
    TemplateInstantiationResult,
    TemplatePackageManifest,
    expected_extension,
    validate_sha256,
    validate_template_id,
    validate_template_version,
)
from app.office_templates.registry import OfficeTemplateRegistry
from app.office_templates.validation import TemplateSafetyLimits, inspect_ooxml_package


BUNDLED_CATALOG_SCHEMA_VERSION: Final = 1
BUNDLED_SIGNATURE_SCHEMA_VERSION: Final = 1
BUNDLED_CATALOG_ID: Final = "suxiaoyou-office-templates"
MAX_BUNDLED_CATALOG_BYTES: Final = 1024 * 1024
MAX_BUNDLED_SIGNATURE_BYTES: Final = 16 * 1024

# The matching private release key is intentionally not checked in.  The value
# is replaced only through a reviewed template-release ceremony.
_DEFAULT_TRUST_ROOTS_B64: Final[dict[str, str]] = {
    "suxiaoyou-office-templates-2026-01": (
        "ObLvOHnt5NdrfXC52+zcMb9OppotBEljBYgV8m3jZPE="
    ),
}

_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_PLACEHOLDER = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_ASSET_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

PlaceholderValueType: TypeAlias = Literal["text"]
RenderUnitKind: TypeAlias = Literal["pages", "worksheets", "slides"]


def _bounded_text(value: object, field: str, *, limit: int) -> str:
    if not isinstance(value, str):
        raise TemplateContractError(f"bundled template {field} must be text")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > limit
        or any(ord(character) < 32 for character in normalized)
    ):
        raise TemplateContractError(f"bundled template {field} is invalid")
    return normalized


@dataclass(frozen=True, slots=True)
class BundledPlaceholderSchema:
    """Typed, bounded contract for one text-only placeholder."""

    name: str
    value_type: PlaceholderValueType
    required: bool
    min_chars: int
    max_chars: int
    description: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _PLACEHOLDER.fullmatch(self.name) is None:
            raise TemplateContractError("bundled placeholder name is invalid")
        if self.value_type != "text":
            raise TemplateContractError("bundled placeholder type must be text")
        if self.required is not True:
            raise TemplateContractError("bundled placeholders must be required")
        if (
            not isinstance(self.min_chars, int)
            or isinstance(self.min_chars, bool)
            or not isinstance(self.max_chars, int)
            or isinstance(self.max_chars, bool)
            or not 0 <= self.min_chars <= self.max_chars <= 100_000
        ):
            raise TemplateContractError("bundled placeholder text bounds are invalid")
        object.__setattr__(
            self,
            "description",
            _bounded_text(self.description, "placeholder description", limit=256),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.value_type,
            "required": self.required,
            "min_chars": self.min_chars,
            "max_chars": self.max_chars,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, value: object) -> BundledPlaceholderSchema:
        expected = {
            "name",
            "type",
            "required",
            "min_chars",
            "max_chars",
            "description",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise TemplateContractError("bundled placeholder fields are invalid")
        return cls(
            name=cast(str, value["name"]),
            value_type=cast(PlaceholderValueType, value["type"]),
            required=cast(bool, value["required"]),
            min_chars=cast(int, value["min_chars"]),
            max_chars=cast(int, value["max_chars"]),
            description=cast(str, value["description"]),
        )


@dataclass(frozen=True, slots=True)
class BundledRenderBaseline:
    """Release-reviewed structural expectation for later visual validation."""

    baseline_id: str
    unit_kind: RenderUnitKind
    min_units: int
    max_units: int

    def __post_init__(self) -> None:
        if not isinstance(self.baseline_id, str) or _LABEL.fullmatch(self.baseline_id) is None:
            raise TemplateContractError("bundled render baseline id is invalid")
        if self.unit_kind not in {"pages", "worksheets", "slides"}:
            raise TemplateContractError("bundled render baseline unit kind is invalid")
        if (
            not isinstance(self.min_units, int)
            or isinstance(self.min_units, bool)
            or not isinstance(self.max_units, int)
            or isinstance(self.max_units, bool)
            or not 1 <= self.min_units <= self.max_units <= 10_000
        ):
            raise TemplateContractError("bundled render baseline bounds are invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_id": self.baseline_id,
            "unit_kind": self.unit_kind,
            "min_units": self.min_units,
            "max_units": self.max_units,
        }

    @classmethod
    def from_dict(cls, value: object) -> BundledRenderBaseline:
        expected = {"baseline_id", "unit_kind", "min_units", "max_units"}
        if not isinstance(value, dict) or set(value) != expected:
            raise TemplateContractError("bundled render baseline fields are invalid")
        return cls(
            baseline_id=cast(str, value["baseline_id"]),
            unit_kind=cast(RenderUnitKind, value["unit_kind"]),
            min_units=cast(int, value["min_units"]),
            max_units=cast(int, value["max_units"]),
        )


@dataclass(frozen=True, slots=True)
class BundledTemplateDescriptor:
    """Safe metadata bound by the signed first-party catalog."""

    manifest: TemplatePackageManifest
    title: str
    description: str
    asset_path: str
    placeholders: tuple[BundledPlaceholderSchema, ...]
    allowed_operations: tuple[str, ...]
    expected_render_baseline: BundledRenderBaseline

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, TemplatePackageManifest):
            raise TemplateContractError("bundled template manifest is invalid")
        object.__setattr__(self, "title", _bounded_text(self.title, "title", limit=160))
        object.__setattr__(
            self,
            "description",
            _bounded_text(self.description, "description", limit=512),
        )
        asset_path = _validate_resource_path(self.asset_path)
        if PurePosixPath(asset_path).suffix.casefold() != expected_extension(
            self.manifest.format
        ):
            raise TemplateContractError("bundled template asset extension is invalid")
        object.__setattr__(self, "asset_path", asset_path)
        try:
            placeholders = tuple(self.placeholders)
        except TypeError as exc:
            raise TemplateContractError("bundled placeholder schema is invalid") from exc
        if (
            not placeholders
            or len(placeholders) > 256
            or any(not isinstance(item, BundledPlaceholderSchema) for item in placeholders)
            or tuple(item.name for item in placeholders)
            != tuple(sorted(item.name for item in placeholders))
            or len({item.name for item in placeholders}) != len(placeholders)
        ):
            raise TemplateContractError(
                "bundled placeholder schema must be sorted and unique"
            )
        if tuple(item.name for item in placeholders) != self.manifest.required_placeholders:
            raise TemplateContractError(
                "bundled placeholder schema does not match the OOXML manifest"
            )
        object.__setattr__(self, "placeholders", placeholders)
        try:
            operations = tuple(self.allowed_operations)
        except TypeError as exc:
            raise TemplateContractError("bundled allowed operations are invalid") from exc
        if operations != ("instantiate_text",):
            raise TemplateContractError(
                "bundled templates may only allow text instantiation"
            )
        object.__setattr__(self, "allowed_operations", operations)
        if not isinstance(self.expected_render_baseline, BundledRenderBaseline):
            raise TemplateContractError("bundled render baseline is invalid")
        expected_unit = {
            "docx": "pages",
            "xlsx": "worksheets",
            "pptx": "slides",
        }[self.manifest.format]
        if self.expected_render_baseline.unit_kind != expected_unit:
            raise TemplateContractError(
                "bundled render baseline does not match the template format"
            )

    @property
    def immutable_key(self) -> tuple[str, str]:
        return self.manifest.immutable_key

    def to_dict(self) -> dict[str, Any]:
        return {
            "template_id": self.manifest.template_id,
            "template_version": self.manifest.template_version,
            "format": self.manifest.format,
            "title": self.title,
            "description": self.description,
            "asset_path": self.asset_path,
            "source_sha256": self.manifest.source_sha256,
            "license": self.manifest.license,
            "provenance": self.manifest.provenance,
            "placeholders": [item.to_dict() for item in self.placeholders],
            "allowed_operations": list(self.allowed_operations),
            "allowed_output_rules": self.manifest.allowed_output_rules.to_dict(),
            "expected_render_baseline": self.expected_render_baseline.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: object) -> BundledTemplateDescriptor:
        expected = {
            "template_id",
            "template_version",
            "format",
            "title",
            "description",
            "asset_path",
            "source_sha256",
            "license",
            "provenance",
            "placeholders",
            "allowed_operations",
            "allowed_output_rules",
            "expected_render_baseline",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise TemplateContractError("bundled template catalog fields are invalid")
        placeholders = value["placeholders"]
        operations = value["allowed_operations"]
        if not isinstance(placeholders, list) or not isinstance(operations, list):
            raise TemplateContractError("bundled template catalog lists are invalid")
        parsed_placeholders = tuple(
            BundledPlaceholderSchema.from_dict(item) for item in placeholders
        )
        manifest = TemplatePackageManifest(
            template_id=cast(str, value["template_id"]),
            template_version=cast(str, value["template_version"]),
            format=cast(OfficeTemplateFormat, value["format"]),
            source_sha256=cast(str, value["source_sha256"]),
            license=cast(str, value["license"]),
            provenance=cast(str, value["provenance"]),
            required_placeholders=tuple(item.name for item in parsed_placeholders),
            allowed_output_rules=AllowedOutputRules.from_dict(
                value["allowed_output_rules"]
            ),
        )
        return cls(
            manifest=manifest,
            title=cast(str, value["title"]),
            description=cast(str, value["description"]),
            asset_path=cast(str, value["asset_path"]),
            placeholders=parsed_placeholders,
            allowed_operations=tuple(cast(list[str], operations)),
            expected_render_baseline=BundledRenderBaseline.from_dict(
                value["expected_render_baseline"]
            ),
        )


@dataclass(frozen=True, slots=True)
class _CatalogSnapshot:
    descriptors: tuple[BundledTemplateDescriptor, ...]
    contents: Mapping[tuple[str, str], bytes]


def _default_trust_roots() -> MappingProxyType[str, bytes]:
    roots: dict[str, bytes] = {}
    for key_id, encoded in _DEFAULT_TRUST_ROOTS_B64.items():
        try:
            public_key = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:  # pragma: no cover - release invariant
            raise TemplateIntegrityError("bundled template trust root is invalid") from exc
        roots[key_id] = public_key
    return _validate_trust_roots(roots)


class BundledOfficeTemplateCatalog:
    """Load and verify the signed template resources shipped with the app."""

    def __init__(
        self,
        asset_root: Path | Traversable | None = None,
        *,
        trusted_public_keys: Mapping[str, bytes] | None = None,
        limits: TemplateSafetyLimits | None = None,
    ) -> None:
        self.asset_root = asset_root or resources.files("app.office_templates").joinpath(
            "assets"
        )
        self.trusted_public_keys = (
            _default_trust_roots()
            if trusted_public_keys is None
            else _validate_trust_roots(trusted_public_keys)
        )
        self.limits = limits or TemplateSafetyLimits()
        if not isinstance(self.limits, TemplateSafetyLimits):
            raise TemplateContractError("bundled template safety limits are invalid")

    def list_templates(self) -> tuple[BundledTemplateDescriptor, ...]:
        return self._load().descriptors

    def read_template(
        self,
        template_id: str,
        template_version: str,
    ) -> tuple[BundledTemplateDescriptor, bytes]:
        key = (
            validate_template_id(template_id),
            validate_template_version(template_version),
        )
        snapshot = self._load()
        for descriptor in snapshot.descriptors:
            if descriptor.immutable_key == key:
                return descriptor, snapshot.contents[key]
        raise TemplateNotFoundError(
            f"bundled Office template {template_id}@{template_version} was not found"
        )

    def _load(self) -> _CatalogSnapshot:
        catalog_bytes = _read_resource(
            self.asset_root,
            "catalog.json",
            max_bytes=MAX_BUNDLED_CATALOG_BYTES,
        )
        signature_bytes = _read_resource(
            self.asset_root,
            "catalog.sig.json",
            max_bytes=MAX_BUNDLED_SIGNATURE_BYTES,
        )
        catalog_value = _strict_json(catalog_bytes, "catalog")
        signature_value = _strict_json(signature_bytes, "signature")
        if catalog_bytes != _canonical_json(catalog_value):
            raise TemplateIntegrityError("bundled template catalog is not canonical")
        if signature_bytes != _canonical_json(signature_value):
            raise TemplateIntegrityError(
                "bundled template signature envelope is not canonical"
            )
        _verify_signature(
            catalog_bytes,
            signature_value,
            self.trusted_public_keys,
        )
        descriptors = _parse_catalog(catalog_value)
        contents: dict[tuple[str, str], bytes] = {}
        seen_assets: set[str] = set()
        for descriptor in descriptors:
            if descriptor.asset_path in seen_assets:
                raise TemplateIntegrityError(
                    "bundled template catalog reuses an asset path"
                )
            seen_assets.add(descriptor.asset_path)
            content = _read_resource(
                self.asset_root,
                descriptor.asset_path,
                max_bytes=self.limits.max_package_bytes,
            )
            if hashlib.sha256(content).hexdigest() != descriptor.manifest.source_sha256:
                raise TemplateIntegrityError(
                    "bundled Office template asset digest does not match"
                )
            try:
                inspect_ooxml_package(
                    content,
                    descriptor.manifest.format,
                    expected_placeholders=descriptor.manifest.required_placeholders,
                    limits=self.limits,
                )
            except Exception as exc:
                raise TemplateIntegrityError(
                    "bundled Office template asset failed safety validation"
                ) from exc
            contents[descriptor.immutable_key] = content
        return _CatalogSnapshot(
            descriptors=descriptors,
            contents=MappingProxyType(contents),
        )


class BundledOfficeTemplateService:
    """Dynamically gated first-party list/instantiate service."""

    def __init__(
        self,
        registry_root: str | Path,
        *,
        catalog: BundledOfficeTemplateCatalog | None = None,
        limits: TemplateSafetyLimits | None = None,
    ) -> None:
        self.catalog = catalog or BundledOfficeTemplateCatalog(limits=limits)
        if not isinstance(self.catalog, BundledOfficeTemplateCatalog):
            raise TemplateContractError("bundled template catalog is invalid")
        self.registry = OfficeTemplateRegistry(
            registry_root,
            limits=limits or self.catalog.limits,
        )
        self._instantiator = OfficeTemplateInstantiator(self.registry)

    def list_templates(self) -> tuple[BundledTemplateDescriptor, ...]:
        _require_feature_enabled()
        return self.catalog.list_templates()

    def instantiate(
        self,
        template_id: str,
        template_version: str,
        values: Mapping[str, object],
        *,
        staging_root: str | Path,
        output_path: str | Path,
    ) -> TemplateInstantiationResult:
        _require_feature_enabled()
        descriptor, content = self.catalog.read_template(
            template_id,
            template_version,
        )
        _validate_schema_values(descriptor.placeholders, values)
        with tempfile.TemporaryDirectory(
            prefix="suxiaoyou-bundled-template-"
        ) as temporary:
            temporary_root = Path(temporary).resolve(strict=True)
            if os.name != "nt":
                os.chmod(temporary_root, 0o700)
            source_path = temporary_root / (
                "source" + expected_extension(descriptor.manifest.format)
            )
            _write_private_copy(source_path, content)
            self.registry.import_template(descriptor.manifest, source_path)
        return self._instantiator.instantiate(
            template_id,
            template_version,
            values,
            staging_root=staging_root,
            output_path=output_path,
        )


def _require_feature_enabled() -> None:
    from app import release_features

    if not bool(release_features.V11_OFFICE_V2_RELEASED):
        raise TemplateFeatureDisabledError(
            "first-party Office templates are not released"
        )


def _validate_schema_values(
    schema: tuple[BundledPlaceholderSchema, ...],
    values: Mapping[str, object],
) -> None:
    if not isinstance(values, Mapping):
        raise TemplateContractError("placeholder values must be a mapping")
    expected = {item.name for item in schema}
    provided = set(values)
    if any(not isinstance(name, str) for name in values):
        raise TemplateContractError("placeholder names must be strings")
    missing = sorted(expected - provided)
    unknown = sorted(provided - expected)
    if missing:
        raise TemplateContractError(
            "missing required placeholders: " + ", ".join(missing)
        )
    if unknown:
        raise TemplateContractError("unknown placeholders: " + ", ".join(unknown))
    for field in schema:
        value = values[field.name]
        if not isinstance(value, str):
            raise TemplateContractError(
                f"placeholder {field.name} must match type text"
            )
        if not field.min_chars <= len(value) <= field.max_chars:
            raise TemplateContractError(
                f"placeholder {field.name} violates its text length contract"
            )


def _parse_catalog(value: object) -> tuple[BundledTemplateDescriptor, ...]:
    expected = {"schema_version", "catalog_id", "catalog_version", "templates"}
    if not isinstance(value, dict) or set(value) != expected:
        raise TemplateIntegrityError("bundled template catalog fields are invalid")
    if (
        not isinstance(value["schema_version"], int)
        or isinstance(value["schema_version"], bool)
        or value["schema_version"] != BUNDLED_CATALOG_SCHEMA_VERSION
    ):
        raise TemplateIntegrityError("bundled template catalog schema is unsupported")
    if value["catalog_id"] != BUNDLED_CATALOG_ID:
        raise TemplateIntegrityError("bundled template catalog id is invalid")
    version = value["catalog_version"]
    if not isinstance(version, str) or _LABEL.fullmatch(version) is None:
        raise TemplateIntegrityError("bundled template catalog version is invalid")
    raw_templates = value["templates"]
    if not isinstance(raw_templates, list) or not 1 <= len(raw_templates) <= 64:
        raise TemplateIntegrityError("bundled template catalog is empty or oversized")
    try:
        descriptors = tuple(
            BundledTemplateDescriptor.from_dict(item) for item in raw_templates
        )
    except TemplateContractError as exc:
        raise TemplateIntegrityError(
            "bundled template catalog contract is invalid"
        ) from exc
    keys = tuple(item.immutable_key for item in descriptors)
    if keys != tuple(sorted(keys)) or len(set(keys)) != len(keys):
        raise TemplateIntegrityError(
            "bundled template catalog keys must be sorted and unique"
        )
    return descriptors


def _verify_signature(
    catalog_bytes: bytes,
    value: object,
    trust_roots: Mapping[str, bytes],
) -> None:
    expected = {
        "schema_version",
        "algorithm",
        "key_id",
        "catalog_sha256",
        "signature",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise TemplateIntegrityError("bundled template signature fields are invalid")
    if (
        not isinstance(value["schema_version"], int)
        or isinstance(value["schema_version"], bool)
        or value["schema_version"] != BUNDLED_SIGNATURE_SCHEMA_VERSION
    ):
        raise TemplateIntegrityError("bundled template signature schema is unsupported")
    if value["algorithm"] != "Ed25519":
        raise TemplateIntegrityError("bundled template signature algorithm is invalid")
    key_id = value["key_id"]
    if not isinstance(key_id, str) or _LABEL.fullmatch(key_id) is None:
        raise TemplateIntegrityError("bundled template signature key id is invalid")
    try:
        expected_digest = validate_sha256(
            value["catalog_sha256"],
            "bundled catalog SHA-256",
        )
    except TemplateContractError as exc:
        raise TemplateIntegrityError("bundled template signature digest is invalid") from exc
    if hashlib.sha256(catalog_bytes).hexdigest() != expected_digest:
        raise TemplateIntegrityError("bundled template catalog digest does not match")
    signature_text = value["signature"]
    if not isinstance(signature_text, str) or len(signature_text) > 256:
        raise TemplateIntegrityError("bundled template signature is invalid")
    try:
        signature = base64.b64decode(signature_text, validate=True)
    except (ValueError, TypeError) as exc:
        raise TemplateIntegrityError("bundled template signature is invalid") from exc
    public_key = trust_roots.get(key_id)
    if public_key is None:
        raise TemplateIntegrityError("bundled template signature key is not trusted")
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            signature,
            catalog_bytes,
        )
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise TemplateIntegrityError(
            "bundled template signature is not trusted"
        ) from exc


def _validate_trust_roots(
    value: Mapping[str, bytes],
) -> MappingProxyType[str, bytes]:
    try:
        roots = dict(value)
    except (TypeError, ValueError) as exc:
        raise TemplateContractError("bundled template trust roots are invalid") from exc
    if (
        not 1 <= len(roots) <= 8
        or any(
            not isinstance(key_id, str)
            or _LABEL.fullmatch(key_id) is None
            or not isinstance(public_key, bytes)
            or len(public_key) != 32
            for key_id, public_key in roots.items()
        )
    ):
        raise TemplateContractError("bundled template trust roots are invalid")
    return MappingProxyType(
        {key_id: bytes(public_key) for key_id, public_key in roots.items()}
    )


def _validate_resource_path(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise TemplateContractError("bundled template asset path is invalid")
    if (
        "\\" in value
        or "%" in value
        or ":" in value
        or value.startswith("/")
        or any(
            segment in {"", ".", ".."}
            or _ASSET_SEGMENT.fullmatch(segment) is None
            for segment in value.split("/")
        )
    ):
        raise TemplateContractError("bundled template asset path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute():
        raise TemplateContractError("bundled template asset path is invalid")
    return value


def _read_resource(root: Path | Traversable, relative: str, *, max_bytes: int) -> bytes:
    try:
        validated = _validate_resource_path(relative)
    except TemplateContractError as exc:
        raise TemplateIntegrityError("bundled template resource path is invalid") from exc
    if isinstance(root, Path):
        return _read_path_resource(root, validated, max_bytes=max_bytes)
    try:
        candidate = root.joinpath(*validated.split("/"))
        if not candidate.is_file():
            raise TemplateIntegrityError("bundled template resource is missing")
        payload = candidate.read_bytes()
    except TemplateIntegrityError:
        raise
    except (OSError, RuntimeError, TypeError) as exc:
        raise TemplateIntegrityError("bundled template resource cannot be read") from exc
    if not 1 <= len(payload) <= max_bytes:
        raise TemplateIntegrityError("bundled template resource exceeds its byte budget")
    return payload


def _read_path_resource(root: Path, relative: str, *, max_bytes: int) -> bytes:
    try:
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise TemplateIntegrityError("bundled template resource root is invalid")
        resolved_root = root.resolve(strict=True)
        candidate = root.joinpath(*relative.split("/"))
        current = root
        for segment in relative.split("/"):
            current = current / segment
            if current.is_symlink():
                raise TemplateIntegrityError(
                    "bundled template resource cannot be a symbolic link"
                )
        candidate.resolve(strict=True).relative_to(resolved_root)
    except TemplateIntegrityError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise TemplateIntegrityError("bundled template resource is missing") from exc
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise TemplateIntegrityError("bundled template resource cannot be opened") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 1 <= before.st_size <= max_bytes:
            raise TemplateIntegrityError("bundled template resource is invalid")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise TemplateIntegrityError(
                    "bundled template resource exceeds its byte budget"
                )
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or total != after.st_size
        ):
            raise TemplateIntegrityError(
                "bundled template resource changed while reading"
            )
        return b"".join(chunks)
    except OSError as exc:
        raise TemplateIntegrityError("bundled template resource cannot be read") from exc
    finally:
        os.close(descriptor)


def _write_private_copy(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        if os.name != "nt":
            os.chmod(path, 0o600, follow_symlinks=False)
    except OSError as exc:
        raise TemplateIntegrityError(
            "bundled template private copy cannot be created"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _strict_json(payload: bytes, label: str) -> object:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key: {key}")
            result[key] = value
        return result

    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise TemplateIntegrityError(
            f"bundled template {label} is not strict JSON"
        ) from exc


def _canonical_json(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError) as exc:
        raise TemplateIntegrityError(
            "bundled template metadata cannot be canonicalized"
        ) from exc


__all__ = [
    "BUNDLED_CATALOG_ID",
    "BUNDLED_CATALOG_SCHEMA_VERSION",
    "BUNDLED_SIGNATURE_SCHEMA_VERSION",
    "BundledOfficeTemplateCatalog",
    "BundledOfficeTemplateService",
    "BundledPlaceholderSchema",
    "BundledRenderBaseline",
    "BundledTemplateDescriptor",
]
