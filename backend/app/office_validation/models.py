"""Versioned, serialization-safe contracts for Office validation evidence."""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Any, Literal, TypeAlias, cast

from app.office_validation.errors import OfficeValidationContractError


OFFICE_VALIDATION_SCHEMA_VERSION = 1
ValidationVerdict: TypeAlias = Literal["pass", "fail", "needs_review"]
CheckOutcome: TypeAlias = Literal["pass", "fail", "needs_review"]

_CODE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _bounded_text(value: object, field: str, *, limit: int = 1000) -> str:
    if not isinstance(value, str):
        raise OfficeValidationContractError(f"{field} must be a string")
    text = value.strip()
    if not text or len(text) > limit or any(ord(char) < 32 for char in text):
        raise OfficeValidationContractError(f"{field} is invalid")
    return text


def _sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise OfficeValidationContractError(f"{field} must be a lowercase SHA-256")
    return value


@dataclass(frozen=True, slots=True)
class EvidenceBox:
    page_number: int
    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        values = (self.page_number, self.x, self.y, self.width, self.height)
        if any(not isinstance(value, int) or isinstance(value, bool) for value in values):
            raise OfficeValidationContractError("evidence box values must be integers")
        if self.page_number < 1 or self.x < 0 or self.y < 0:
            raise OfficeValidationContractError("evidence box origin is invalid")
        if self.width < 1 or self.height < 1:
            raise OfficeValidationContractError("evidence box size is invalid")

    def to_dict(self) -> dict[str, int]:
        return {
            "page_number": self.page_number,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
        }

    @classmethod
    def from_dict(cls, value: object) -> "EvidenceBox":
        expected = {"page_number", "x", "y", "width", "height"}
        if not isinstance(value, dict) or set(value) != expected:
            raise OfficeValidationContractError("evidence box fields are invalid")
        return cls(
            page_number=cast(int, value["page_number"]),
            x=cast(int, value["x"]),
            y=cast(int, value["y"]),
            width=cast(int, value["width"]),
            height=cast(int, value["height"]),
        )


@dataclass(frozen=True, slots=True)
class ValidationCheck:
    code: str
    outcome: CheckOutcome
    message: str
    box: EvidenceBox | None = None
    metrics: tuple[tuple[str, float | int], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or _CODE.fullmatch(self.code) is None:
            raise OfficeValidationContractError("validation check code is invalid")
        if self.outcome not in {"pass", "fail", "needs_review"}:
            raise OfficeValidationContractError("validation check outcome is invalid")
        _bounded_text(self.message, "validation check message")
        if self.box is not None and not isinstance(self.box, EvidenceBox):
            raise OfficeValidationContractError("validation check box is invalid")
        try:
            metrics = tuple(self.metrics)
        except TypeError as exc:
            raise OfficeValidationContractError("validation metrics are invalid") from exc
        names: set[str] = set()
        for name, value in metrics:
            if (
                not isinstance(name, str)
                or _CODE.fullmatch(name) is None
                or name in names
                or isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise OfficeValidationContractError("validation metrics are invalid")
            names.add(name)
        if tuple(sorted(metrics)) != metrics:
            raise OfficeValidationContractError("validation metrics must be sorted")
        object.__setattr__(self, "metrics", metrics)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "outcome": self.outcome,
            "message": self.message,
            "box": self.box.to_dict() if self.box is not None else None,
            "metrics": {name: value for name, value in self.metrics},
        }

    @classmethod
    def from_dict(cls, value: object) -> "ValidationCheck":
        expected = {"code", "outcome", "message", "box", "metrics"}
        if not isinstance(value, dict) or set(value) != expected:
            raise OfficeValidationContractError(
                "validation check fields are invalid"
            )
        raw_metrics = value["metrics"]
        if not isinstance(raw_metrics, dict) or any(
            not isinstance(name, str) for name in raw_metrics
        ):
            raise OfficeValidationContractError("validation metrics are invalid")
        raw_box = value["box"]
        return cls(
            code=cast(str, value["code"]),
            outcome=cast(CheckOutcome, value["outcome"]),
            message=cast(str, value["message"]),
            box=(EvidenceBox.from_dict(raw_box) if raw_box is not None else None),
            metrics=tuple(sorted(cast(dict[str, float | int], raw_metrics).items())),
        )


@dataclass(frozen=True, slots=True)
class OfficeValidationReport:
    document_format: Literal["docx", "xlsx", "pptx"]
    baseline_sha256: str
    candidate_sha256: str
    renderer_id: str
    renderer_version: str
    font_digest: str
    verdict: ValidationVerdict
    checks: tuple[ValidationCheck, ...]
    checkpoint_id: str | None = None
    root_turn_id: str | None = None
    schema_version: int = OFFICE_VALIDATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != OFFICE_VALIDATION_SCHEMA_VERSION:
            raise OfficeValidationContractError("unsupported validation report schema")
        if self.document_format not in {"docx", "xlsx", "pptx"}:
            raise OfficeValidationContractError("validation document format is invalid")
        _sha256(self.baseline_sha256, "baseline_sha256")
        _sha256(self.candidate_sha256, "candidate_sha256")
        _sha256(self.font_digest, "font_digest")
        _bounded_text(self.renderer_id, "renderer_id", limit=256)
        _bounded_text(self.renderer_version, "renderer_version", limit=256)
        if self.verdict not in {"pass", "fail", "needs_review"}:
            raise OfficeValidationContractError("validation verdict is invalid")
        try:
            checks = tuple(self.checks)
        except TypeError as exc:
            raise OfficeValidationContractError("validation checks are invalid") from exc
        if not checks or any(not isinstance(item, ValidationCheck) for item in checks):
            raise OfficeValidationContractError("validation checks are invalid")
        derived = derive_verdict(checks)
        if self.verdict != derived:
            raise OfficeValidationContractError("validation verdict contradicts checks")
        for field, value in (
            ("checkpoint_id", self.checkpoint_id),
            ("root_turn_id", self.root_turn_id),
        ):
            if value is not None:
                _bounded_text(value, field, limit=200)
        object.__setattr__(self, "checks", checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "document_format": self.document_format,
            "baseline_sha256": self.baseline_sha256,
            "candidate_sha256": self.candidate_sha256,
            "renderer_id": self.renderer_id,
            "renderer_version": self.renderer_version,
            "font_digest": self.font_digest,
            "verdict": self.verdict,
            "checkpoint_id": self.checkpoint_id,
            "root_turn_id": self.root_turn_id,
            "checks": [item.to_dict() for item in self.checks],
        }

    @classmethod
    def from_dict(cls, value: object) -> "OfficeValidationReport":
        expected = {
            "schema_version",
            "document_format",
            "baseline_sha256",
            "candidate_sha256",
            "renderer_id",
            "renderer_version",
            "font_digest",
            "verdict",
            "checkpoint_id",
            "root_turn_id",
            "checks",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise OfficeValidationContractError(
                "validation report fields are invalid"
            )
        raw_checks = value["checks"]
        if not isinstance(raw_checks, list):
            raise OfficeValidationContractError("validation checks are invalid")
        return cls(
            schema_version=cast(int, value["schema_version"]),
            document_format=cast(
                Literal["docx", "xlsx", "pptx"],
                value["document_format"],
            ),
            baseline_sha256=cast(str, value["baseline_sha256"]),
            candidate_sha256=cast(str, value["candidate_sha256"]),
            renderer_id=cast(str, value["renderer_id"]),
            renderer_version=cast(str, value["renderer_version"]),
            font_digest=cast(str, value["font_digest"]),
            verdict=cast(ValidationVerdict, value["verdict"]),
            checkpoint_id=cast(str | None, value["checkpoint_id"]),
            root_turn_id=cast(str | None, value["root_turn_id"]),
            checks=tuple(ValidationCheck.from_dict(item) for item in raw_checks),
        )


def derive_verdict(checks: tuple[ValidationCheck, ...]) -> ValidationVerdict:
    outcomes = {item.outcome for item in checks}
    if "fail" in outcomes:
        return "fail"
    if "needs_review" in outcomes:
        return "needs_review"
    return "pass"
