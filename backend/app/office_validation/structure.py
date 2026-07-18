"""Deterministic OOXML part preservation reports."""

from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import hashlib
import os
from pathlib import Path
import stat
from types import MappingProxyType
from typing import Literal, Mapping

from app.office_templates.validation import TemplateSafetyLimits, inspect_ooxml_package
from app.office_validation.errors import (
    OfficeValidationContractError,
    OfficeValidationSecurityError,
)


@dataclass(frozen=True, slots=True)
class OOXMLPartManifest:
    document_format: Literal["docx", "xlsx", "pptx"]
    package_sha256: str
    parts: Mapping[str, str]

    def __post_init__(self) -> None:
        if self.document_format not in {"docx", "xlsx", "pptx"}:
            raise OfficeValidationContractError("OOXML manifest format is invalid")
        if (
            not isinstance(self.package_sha256, str)
            or len(self.package_sha256) != 64
        ):
            raise OfficeValidationContractError("OOXML package digest is invalid")
        copied = dict(self.parts)
        if not copied or tuple(sorted(copied)) != tuple(copied):
            raise OfficeValidationContractError("OOXML part manifest must be sorted")
        if any(
            not isinstance(name, str)
            or not name
            or not isinstance(digest, str)
            or len(digest) != 64
            for name, digest in copied.items()
        ):
            raise OfficeValidationContractError("OOXML part manifest is invalid")
        object.__setattr__(self, "parts", MappingProxyType(copied))


@dataclass(frozen=True, slots=True)
class OOXMLPartChange:
    part_name: str
    operation: Literal["added", "modified", "deleted"]
    allowed: bool


@dataclass(frozen=True, slots=True)
class StructuralDeltaReport:
    baseline: OOXMLPartManifest
    candidate: OOXMLPartManifest
    changes: tuple[OOXMLPartChange, ...]

    @property
    def passed(self) -> bool:
        return all(item.allowed for item in self.changes)

    @property
    def rejected_parts(self) -> tuple[str, ...]:
        return tuple(item.part_name for item in self.changes if not item.allowed)


def inspect_ooxml_path(
    path: Path,
    document_format: Literal["docx", "xlsx", "pptx"],
    *,
    limits: TemplateSafetyLimits | None = None,
) -> OOXMLPartManifest:
    """Read one regular file without following a final symlink and hash every part."""

    selected = limits or TemplateSafetyLimits()
    source = Path(path)
    if not source.is_absolute():
        raise OfficeValidationContractError("OOXML validation path must be absolute")
    try:
        before = source.lstat()
    except OSError as exc:
        raise OfficeValidationSecurityError("OOXML source cannot be inspected") from exc
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise OfficeValidationSecurityError("OOXML source must be a regular file")
    if before.st_size < 1 or before.st_size > selected.max_package_bytes:
        raise OfficeValidationSecurityError("OOXML source exceeds its byte budget")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(source, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise OfficeValidationSecurityError("OOXML source changed while opening")
            content = handle.read(selected.max_package_bytes + 1)
            after = os.fstat(handle.fileno())
    except OfficeValidationSecurityError:
        raise
    except OSError as exc:
        raise OfficeValidationSecurityError("OOXML source cannot be read safely") from exc
    visible = source.lstat()
    if (
        len(content) != opened.st_size
        or len(content) > selected.max_package_bytes
        or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        or (visible.st_dev, visible.st_ino) != (after.st_dev, after.st_ino)
    ):
        raise OfficeValidationSecurityError("OOXML source changed while reading")

    try:
        inspection = inspect_ooxml_package(
            content,
            document_format,
            expected_placeholders=None,
            limits=selected,
        )
    except Exception as exc:
        raise OfficeValidationSecurityError("OOXML package safety validation failed") from exc
    parts = {
        name: hashlib.sha256(payload).hexdigest()
        for name, payload in sorted(inspection.entries.items())
    }
    return OOXMLPartManifest(
        document_format=document_format,
        package_sha256=hashlib.sha256(content).hexdigest(),
        parts=parts,
    )


def compare_ooxml_parts(
    baseline: OOXMLPartManifest,
    candidate: OOXMLPartManifest,
    *,
    allowed_changed_parts: tuple[str, ...],
    max_changed_parts: int = 500,
) -> StructuralDeltaReport:
    """Reject every changed, added, or deleted part outside the explicit allow-list."""

    if baseline.document_format != candidate.document_format:
        raise OfficeValidationContractError("OOXML formats cannot be compared")
    if (
        not isinstance(max_changed_parts, int)
        or isinstance(max_changed_parts, bool)
        or max_changed_parts < 1
    ):
        raise OfficeValidationContractError("max_changed_parts must be positive")
    patterns = tuple(allowed_changed_parts)
    if (
        len(patterns) > 256
        or len(patterns) != len(set(patterns))
        or any(
            not isinstance(pattern, str)
            or not pattern
            or pattern.startswith("/")
            or "\\" in pattern
            or ".." in pattern.split("/")
            for pattern in patterns
        )
    ):
        raise OfficeValidationContractError("allowed OOXML part patterns are invalid")

    changes: list[OOXMLPartChange] = []
    names = sorted(set(baseline.parts) | set(candidate.parts))
    for name in names:
        before = baseline.parts.get(name)
        after = candidate.parts.get(name)
        if before == after:
            continue
        if before is None:
            operation: Literal["added", "modified", "deleted"] = "added"
        elif after is None:
            operation = "deleted"
        else:
            operation = "modified"
        changes.append(
            OOXMLPartChange(
                part_name=name,
                operation=operation,
                allowed=any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns),
            )
        )
        if len(changes) > max_changed_parts:
            raise OfficeValidationSecurityError("OOXML delta exceeds its part budget")
    return StructuralDeltaReport(
        baseline=baseline,
        candidate=candidate,
        changes=tuple(changes),
    )
