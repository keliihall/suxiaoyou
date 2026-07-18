"""Server-owned contract for bounded repairs of private Office candidates.

The repair boundary receives declarative input only.  It never receives a
workspace, staging, cache, golden, policy, threshold, or commit-seal path/value.
Every value passed to a repairer is immutable and every returned replacement is
copied back into plain JSON containers before the Office tool evaluates it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math
from types import MappingProxyType
from typing import Any, Literal, Protocol, runtime_checkable

from app.office_validation.errors import OfficeValidationContractError
from app.office_validation.models import OfficeValidationReport


_MAX_JSON_DEPTH = 48
_MAX_JSON_NODES = 100_000


class OfficePrecommitRepairError(OfficeValidationContractError):
    """A repair request or complete replacement argument set is invalid."""


@dataclass(frozen=True, slots=True)
class RedactedOfficeEvidenceBox:
    """Location-only visual evidence safe to cross the repair boundary."""

    page_number: int
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class RedactedOfficeValidationCheck:
    """A check without messages, metrics, hashes, renderer, or policy data."""

    code: str
    outcome: Literal["pass", "fail", "needs_review"]
    box: RedactedOfficeEvidenceBox | None


@dataclass(frozen=True, slots=True)
class RedactedOfficeValidationReport:
    """The only validation evidence visible to a declarative repairer."""

    document_format: Literal["docx", "xlsx", "pptx"]
    verdict: Literal["pass", "fail", "needs_review"]
    checks: tuple[RedactedOfficeValidationCheck, ...]


@dataclass(frozen=True, slots=True)
class OfficePrecommitRepairRequest:
    """Immutable request for repair attempt one or two.

    ``tokenized_args`` is a recursively frozen declarative payload whose target
    and local-read paths have been replaced by per-call opaque tokens.  A
    repairer must return a complete replacement payload; it cannot
    incrementally patch server state.
    """

    tokenized_args: Mapping[str, Any]
    report: RedactedOfficeValidationReport
    attempt: Literal[1, 2]

    def __post_init__(self) -> None:
        if not isinstance(self.tokenized_args, MappingProxyType):
            raise OfficePrecommitRepairError(
                "Office repair tokenized args must be a server-frozen mapping"
            )
        if not isinstance(self.report, RedactedOfficeValidationReport):
            raise OfficePrecommitRepairError("Office repair report is invalid")
        if self.attempt not in {1, 2} or isinstance(self.attempt, bool):
            raise OfficePrecommitRepairError("Office repair attempt must be one or two")


@runtime_checkable
class OfficePrecommitRepairer(Protocol):
    """Server-injected producer of complete declarative replacement args."""

    async def repair(
        self,
        request: OfficePrecommitRepairRequest,
    ) -> Mapping[str, Any]:
        ...


def redact_office_validation_report(
    report: OfficeValidationReport,
) -> RedactedOfficeValidationReport:
    """Drop all paths, identities, hashes, policy metrics, and free text."""

    if not isinstance(report, OfficeValidationReport):
        raise OfficePrecommitRepairError("Office repair report source is invalid")
    checks = tuple(
        RedactedOfficeValidationCheck(
            code=check.code,
            outcome=check.outcome,
            box=(
                RedactedOfficeEvidenceBox(
                    page_number=check.box.page_number,
                    x=check.box.x,
                    y=check.box.y,
                    width=check.box.width,
                    height=check.box.height,
                )
                if check.box is not None
                else None
            ),
        )
        for check in report.checks
    )
    return RedactedOfficeValidationReport(
        document_format=report.document_format,
        verdict=report.verdict,
        checks=checks,
    )


def build_precommit_repair_request(
    *,
    tokenized_args: Mapping[str, Any],
    report: OfficeValidationReport,
    attempt: Literal[1, 2],
) -> OfficePrecommitRepairRequest:
    """Construct the nominal request from server-owned values only."""

    frozen = _copy_json(tokenized_args, frozen=True)
    if not isinstance(frozen, MappingProxyType):
        raise OfficePrecommitRepairError("Office repair args must be an object")
    return OfficePrecommitRepairRequest(
        tokenized_args=frozen,
        report=redact_office_validation_report(report),
        attempt=attempt,
    )


def copy_replacement_args(value: object) -> dict[str, Any]:
    """Copy untrusted repair output into bounded, acyclic JSON containers."""

    copied = _copy_json(value, frozen=False)
    if not isinstance(copied, dict):
        raise OfficePrecommitRepairError(
            "Office repairer must return a complete argument object"
        )
    return copied


def _copy_json(value: object, *, frozen: bool) -> object:
    seen: set[int] = set()
    nodes = 0

    def copy_value(item: object, depth: int) -> object:
        nonlocal nodes
        nodes += 1
        if depth > _MAX_JSON_DEPTH or nodes > _MAX_JSON_NODES:
            raise OfficePrecommitRepairError("Office repair arguments exceed bounds")
        if item is None or isinstance(item, (str, bool, int)):
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise OfficePrecommitRepairError(
                    "Office repair arguments contain a non-finite number"
                )
            return item
        if isinstance(item, Mapping):
            identity = id(item)
            if identity in seen:
                raise OfficePrecommitRepairError(
                    "Office repair arguments contain a reference cycle"
                )
            seen.add(identity)
            try:
                copied: dict[str, object] = {}
                for key, child in item.items():
                    if not isinstance(key, str):
                        raise OfficePrecommitRepairError(
                            "Office repair argument keys must be strings"
                        )
                    copied[key] = copy_value(child, depth + 1)
            finally:
                seen.remove(identity)
            return MappingProxyType(copied) if frozen else copied
        if isinstance(item, Sequence) and not isinstance(
            item,
            (str, bytes, bytearray, memoryview),
        ):
            identity = id(item)
            if identity in seen:
                raise OfficePrecommitRepairError(
                    "Office repair arguments contain a reference cycle"
                )
            seen.add(identity)
            try:
                copied_items = tuple(copy_value(child, depth + 1) for child in item)
            finally:
                seen.remove(identity)
            return copied_items if frozen else list(copied_items)
        raise OfficePrecommitRepairError(
            "Office repair arguments must contain JSON values only"
        )

    return copy_value(value, 0)


__all__ = [
    "OfficePrecommitRepairError",
    "OfficePrecommitRepairRequest",
    "OfficePrecommitRepairer",
    "RedactedOfficeEvidenceBox",
    "RedactedOfficeValidationCheck",
    "RedactedOfficeValidationReport",
    "build_precommit_repair_request",
    "copy_replacement_args",
    "redact_office_validation_report",
]
