"""Strict manifest and result models for immutable Office templates."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, TypeAlias, cast

from app.office_templates.errors import TemplateContractError


TEMPLATE_MANIFEST_SCHEMA_VERSION: Final = 1
OFFICE_TEMPLATES_DEFAULT_ENABLED: Final = False
OfficeTemplateFormat: TypeAlias = Literal["docx", "xlsx", "pptx"]

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_TEMPLATE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_PLACEHOLDER_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_REFERENCE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_FORMAT_EXTENSION = {"docx": ".docx", "xlsx": ".xlsx", "pptx": ".pptx"}


def validate_sha256(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise TemplateContractError(f"{field_name} must be a lowercase SHA-256")
    return value


def validate_reference_id(value: object) -> str:
    if not isinstance(value, str) or _REFERENCE_PATTERN.fullmatch(value) is None:
        raise TemplateContractError("template reference id is invalid")
    return value


def validate_template_id(value: object) -> str:
    if not isinstance(value, str) or _TEMPLATE_ID_PATTERN.fullmatch(value) is None:
        raise TemplateContractError("template_id is invalid")
    return value


def validate_template_version(value: object) -> str:
    if not isinstance(value, str) or _VERSION_PATTERN.fullmatch(value) is None:
        raise TemplateContractError("template_version is invalid")
    return value


def _bounded_text(value: object, field_name: str, *, limit: int = 512) -> str:
    if not isinstance(value, str):
        raise TemplateContractError(f"{field_name} must be a string")
    text = value.strip()
    if not text or len(text) > limit or any(ord(item) < 32 for item in text):
        raise TemplateContractError(f"{field_name} is invalid")
    return text


@dataclass(frozen=True, slots=True)
class AllowedOutputRules:
    """Manifest-owned constraints for caller-provided staging outputs."""

    extensions: tuple[str, ...]
    max_output_bytes: int
    allow_overwrite: bool = False

    def __post_init__(self) -> None:
        try:
            extensions = tuple(self.extensions)
        except TypeError as exc:
            raise TemplateContractError(
                "allowed output extensions are invalid"
            ) from exc
        if (
            not extensions
            or len(extensions) > 8
            or any(
                not isinstance(item, str)
                or re.fullmatch(r"\.[a-z0-9]{1,12}", item) is None
                for item in extensions
            )
            or len(set(extensions)) != len(extensions)
        ):
            raise TemplateContractError("allowed output extensions are invalid")
        if (
            not isinstance(self.max_output_bytes, int)
            or isinstance(self.max_output_bytes, bool)
            or not 1 <= self.max_output_bytes <= 1024 * 1024 * 1024
        ):
            raise TemplateContractError("allowed output byte limit is invalid")
        if self.allow_overwrite is not False:
            raise TemplateContractError("Office template output overwrite is not allowed")
        object.__setattr__(self, "extensions", extensions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "extensions": list(self.extensions),
            "max_output_bytes": self.max_output_bytes,
            "allow_overwrite": self.allow_overwrite,
        }

    @classmethod
    def from_dict(cls, value: object) -> AllowedOutputRules:
        expected = {"extensions", "max_output_bytes", "allow_overwrite"}
        if not isinstance(value, dict) or set(value) != expected:
            raise TemplateContractError("allowed output rule fields are invalid")
        extensions = value["extensions"]
        if not isinstance(extensions, list):
            raise TemplateContractError("allowed output extensions must be a list")
        return cls(
            extensions=tuple(cast(list[str], extensions)),
            max_output_bytes=cast(int, value["max_output_bytes"]),
            allow_overwrite=cast(bool, value["allow_overwrite"]),
        )


@dataclass(frozen=True, slots=True)
class TemplatePackageManifest:
    """Versioned metadata bound to one immutable OOXML source package."""

    template_id: str
    template_version: str
    format: OfficeTemplateFormat
    source_sha256: str
    license: str
    provenance: str
    required_placeholders: tuple[str, ...]
    allowed_output_rules: AllowedOutputRules
    schema_version: int = TEMPLATE_MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != TEMPLATE_MANIFEST_SCHEMA_VERSION
        ):
            raise TemplateContractError("unsupported Office template manifest schema")
        validate_template_id(self.template_id)
        validate_template_version(self.template_version)
        if not isinstance(self.format, str) or self.format not in _FORMAT_EXTENSION:
            raise TemplateContractError("template format must be docx, xlsx, or pptx")
        object.__setattr__(
            self,
            "source_sha256",
            validate_sha256(self.source_sha256, "template source_sha256"),
        )
        object.__setattr__(self, "license", _bounded_text(self.license, "license"))
        object.__setattr__(
            self,
            "provenance",
            _bounded_text(self.provenance, "provenance", limit=1024),
        )
        try:
            placeholders = tuple(self.required_placeholders)
        except TypeError as exc:
            raise TemplateContractError("required placeholders are invalid") from exc
        if (
            len(placeholders) > 256
            or len(placeholders) != len(set(placeholders))
            or any(
                not isinstance(item, str)
                or _PLACEHOLDER_PATTERN.fullmatch(item) is None
                for item in placeholders
            )
        ):
            raise TemplateContractError("required placeholders are invalid")
        if tuple(sorted(placeholders)) != placeholders:
            raise TemplateContractError("required placeholders must be sorted")
        object.__setattr__(self, "required_placeholders", placeholders)
        if not isinstance(self.allowed_output_rules, AllowedOutputRules):
            raise TemplateContractError(
                "allowed_output_rules must be AllowedOutputRules"
            )
        expected_extension = _FORMAT_EXTENSION[self.format]
        if tuple(self.allowed_output_rules.extensions) != (expected_extension,):
            raise TemplateContractError(
                "allowed output extension must exactly match the template format"
            )

    @property
    def immutable_key(self) -> tuple[str, str]:
        return self.template_id, self.template_version

    @property
    def template_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "template_id": self.template_id,
            "template_version": self.template_version,
            "format": self.format,
            "source_sha256": self.source_sha256,
            "license": self.license,
            "provenance": self.provenance,
            "required_placeholders": list(self.required_placeholders),
            "allowed_output_rules": self.allowed_output_rules.to_dict(),
        }

    def canonical_bytes(self) -> bytes:
        return (
            json.dumps(
                self.to_dict(),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )

    @classmethod
    def from_dict(cls, value: object) -> TemplatePackageManifest:
        expected = {
            "schema_version",
            "template_id",
            "template_version",
            "format",
            "source_sha256",
            "license",
            "provenance",
            "required_placeholders",
            "allowed_output_rules",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise TemplateContractError("template manifest fields are invalid")
        placeholders = value["required_placeholders"]
        if not isinstance(placeholders, list):
            raise TemplateContractError("required_placeholders must be a list")
        return cls(
            schema_version=cast(int, value["schema_version"]),
            template_id=cast(str, value["template_id"]),
            template_version=cast(str, value["template_version"]),
            format=cast(OfficeTemplateFormat, value["format"]),
            source_sha256=cast(str, value["source_sha256"]),
            license=cast(str, value["license"]),
            provenance=cast(str, value["provenance"]),
            required_placeholders=tuple(cast(list[str], placeholders)),
            allowed_output_rules=AllowedOutputRules.from_dict(
                value["allowed_output_rules"]
            ),
        )


@dataclass(frozen=True, slots=True)
class TemplateRecord:
    manifest: TemplatePackageManifest
    content_path: Path
    reference_count: int
    reference_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, TemplatePackageManifest):
            raise TemplateContractError("record manifest is invalid")
        if not isinstance(self.content_path, Path) or not self.content_path.is_absolute():
            raise TemplateContractError("record content path must be absolute")
        try:
            references = tuple(self.reference_ids)
        except TypeError as exc:
            raise TemplateContractError("record reference ids are invalid") from exc
        if (
            not isinstance(self.reference_count, int)
            or isinstance(self.reference_count, bool)
            or self.reference_count < 0
            or self.reference_count != len(references)
        ):
            raise TemplateContractError("record reference count is inconsistent")
        if (
            len(references) != len(set(references))
            or tuple(sorted(references)) != references
        ):
            raise TemplateContractError("record reference ids must be sorted")
        for reference_id in references:
            validate_reference_id(reference_id)
        object.__setattr__(self, "reference_ids", references)


@dataclass(frozen=True, slots=True)
class TemplateChange:
    part_name: str
    placeholder: str
    occurrences: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.part_name, str)
            or not self.part_name
            or self.part_name.startswith("/")
            or ".." in self.part_name
        ):
            raise TemplateContractError("template change part name is invalid")
        if (
            not isinstance(self.placeholder, str)
            or _PLACEHOLDER_PATTERN.fullmatch(self.placeholder) is None
        ):
            raise TemplateContractError("template change placeholder is invalid")
        if (
            not isinstance(self.occurrences, int)
            or isinstance(self.occurrences, bool)
            or self.occurrences < 1
        ):
            raise TemplateContractError("template change count must be positive")


@dataclass(frozen=True, slots=True)
class TemplateInstantiationResult:
    template_id: str
    template_version: str
    source_sha256: str
    template_sha256: str
    output_sha256: str
    output_path: Path
    changes: tuple[TemplateChange, ...]

    def __post_init__(self) -> None:
        validate_template_id(self.template_id)
        validate_template_version(self.template_version)
        validate_sha256(self.source_sha256, "result source_sha256")
        validate_sha256(self.template_sha256, "result template_sha256")
        validate_sha256(self.output_sha256, "result output_sha256")
        if not isinstance(self.output_path, Path) or not self.output_path.is_absolute():
            raise TemplateContractError("result output path must be absolute")
        try:
            changes = tuple(self.changes)
        except TypeError as exc:
            raise TemplateContractError("result change list is invalid") from exc
        if any(not isinstance(change, TemplateChange) for change in changes):
            raise TemplateContractError("result change list is invalid")
        object.__setattr__(self, "changes", changes)


def expected_extension(format_name: OfficeTemplateFormat) -> str:
    try:
        return _FORMAT_EXTENSION[format_name]
    except (KeyError, TypeError) as exc:
        raise TemplateContractError("Office template format is invalid") from exc
