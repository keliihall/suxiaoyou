"""Strict, versioned contracts for the read-only validation Agent.

The model produces :class:`ValidationModelVerdict`; the server wraps it in a
:class:`ValidationVerdictRecord` whose provenance, round counter, and budget
accounting cannot be supplied or changed by model output.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


VALIDATION_CONTRACT_VERSION = 1
MAX_MODEL_OUTPUT_BYTES = 64 * 1024
MAX_EVIDENCE_ITEMS = 32

ValidationVerdict = Literal["pass", "fail", "needs_review"]
ValidationReasonCode = Literal[
    "model_verdict",
    "deterministic_failure",
    "malformed_output",
    "timeout",
    "cancelled",
    "budget_exhausted",
    "unverified_evidence",
    "runner_error",
]


class ValidationContractError(ValueError):
    """Raised when untrusted model output violates the v1 contract."""


class _StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def _validate_text(value: str, *, field: str, limit: int) -> str:
    if not value.strip():
        raise ValueError(f"{field} cannot be blank")
    if len(value) > limit:
        raise ValueError(f"{field} exceeds {limit} characters")
    if any(ord(char) < 32 and char not in "\t\n\r" for char in value):
        raise ValueError(f"{field} contains control characters")
    return value


class ValidationBudgetLimits(_StrictContract):
    """Server-selected hard limits for one validation run."""

    max_rounds: int = Field(default=2, ge=1, le=2)
    max_tokens: int = Field(default=8_000, ge=1, le=100_000)
    timeout_ms: int = Field(default=60_000, ge=50, le=300_000)
    max_output_bytes: int = Field(
        default=MAX_MODEL_OUTPUT_BYTES,
        ge=256,
        le=MAX_MODEL_OUTPUT_BYTES,
    )


class ValidationBudgetReport(_StrictContract):
    """Server-measured limits and consumption attached to every verdict."""

    max_rounds: int = Field(ge=1, le=2)
    max_tokens: int = Field(ge=1, le=100_000)
    timeout_ms: int = Field(ge=50, le=300_000)
    rounds_used: int = Field(ge=0, le=2)
    tokens_used: int = Field(ge=0)
    elapsed_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def _rounds_fit_limit(self) -> "ValidationBudgetReport":
        if self.rounds_used > self.max_rounds:
            raise ValueError("rounds_used exceeds max_rounds")
        return self


class ValidationSource(_StrictContract):
    """Immutable database provenance selected from the parent GenerationJob."""

    kind: Literal["turn_checkpoint"] = "turn_checkpoint"
    session_id: str = Field(min_length=1, max_length=128)
    root_turn_id: str = Field(min_length=1, max_length=128)
    checkpoint_id: str = Field(min_length=1, max_length=128)
    workspace_instance_id: str = Field(min_length=1, max_length=128)

    @field_validator(
        "session_id", "root_turn_id", "checkpoint_id", "workspace_instance_id"
    )
    @classmethod
    def _bounded_identifiers(cls, value: str, info: Any) -> str:
        return _validate_text(value, field=info.field_name, limit=128)


class DeterministicValidationFailure(_StrictContract):
    """A non-model failure that the Agent is never allowed to downgrade."""

    code: str = Field(min_length=1, max_length=120)
    source: str = Field(min_length=1, max_length=1024)
    summary: str = Field(min_length=1, max_length=4_000)

    @field_validator("code")
    @classmethod
    def _code_text(cls, value: str) -> str:
        return _validate_text(value, field="code", limit=120)

    @field_validator("source")
    @classmethod
    def _source_text(cls, value: str) -> str:
        return _validate_text(value, field="source", limit=1024)

    @field_validator("summary")
    @classmethod
    def _summary_text(cls, value: str) -> str:
        return _validate_text(value, field="summary", limit=4_000)


class ValidationTask(_StrictContract):
    """Server input. It intentionally has no workspace or permission fields."""

    objective: str = Field(min_length=1, max_length=16_000)
    deterministic_failures: tuple[DeterministicValidationFailure, ...] = Field(
        default=(),
        max_length=MAX_EVIDENCE_ITEMS,
    )
    budget: ValidationBudgetLimits = Field(default_factory=ValidationBudgetLimits)

    @field_validator("objective")
    @classmethod
    def _objective_text(cls, value: str) -> str:
        return _validate_text(value, field="objective", limit=16_000)


class ValidationModelEvidence(_StrictContract):
    """One bounded observation supplied by the untrusted model."""

    kind: Literal["file", "search", "observation"]
    source: str = Field(min_length=1, max_length=1024)
    summary: str = Field(min_length=1, max_length=4_000)

    @field_validator("source")
    @classmethod
    def _source_text(cls, value: str) -> str:
        return _validate_text(value, field="source", limit=1024)

    @field_validator("summary")
    @classmethod
    def _summary_text(cls, value: str) -> str:
        return _validate_text(value, field="summary", limit=4_000)


class ValidationModelVerdict(_StrictContract):
    """The only JSON shape accepted from the model."""

    schema_version: Literal[1]
    verdict: ValidationVerdict
    summary: str = Field(min_length=1, max_length=8_000)
    evidence: tuple[ValidationModelEvidence, ...] = Field(
        default=(),
        max_length=MAX_EVIDENCE_ITEMS,
    )

    @field_validator("summary")
    @classmethod
    def _summary_text(cls, value: str) -> str:
        return _validate_text(value, field="summary", limit=8_000)

    @model_validator(mode="after")
    def _decisive_verdict_has_evidence(self) -> "ValidationModelVerdict":
        if self.verdict in {"pass", "fail"} and not self.evidence:
            raise ValueError("pass/fail verdicts require at least one evidence item")
        return self


class ValidationEvidence(_StrictContract):
    """Evidence in the final server-owned verdict record."""

    evidence_id: str = Field(min_length=1, max_length=128)
    origin: Literal["validator", "deterministic", "runtime"]
    kind: Literal[
        "file",
        "search",
        "observation",
        "deterministic_failure",
        "runtime",
    ]
    source: str = Field(min_length=1, max_length=1024)
    summary: str = Field(min_length=1, max_length=4_000)

    @field_validator("evidence_id")
    @classmethod
    def _id_text(cls, value: str) -> str:
        return _validate_text(value, field="evidence_id", limit=128)

    @field_validator("source")
    @classmethod
    def _source_text(cls, value: str) -> str:
        return _validate_text(value, field="source", limit=1024)

    @field_validator("summary")
    @classmethod
    def _summary_text(cls, value: str) -> str:
        return _validate_text(value, field="summary", limit=4_000)


class ValidationVerdictRecord(_StrictContract):
    """Authoritative server result bound to one turn/checkpoint and <=2 rounds."""

    schema_version: Literal[1]
    validation_id: str = Field(min_length=1, max_length=128)
    verdict: ValidationVerdict
    reason_code: ValidationReasonCode
    source: ValidationSource
    round: int = Field(ge=0, le=2)
    budget: ValidationBudgetReport
    summary: str = Field(min_length=1, max_length=8_000)
    evidence: tuple[ValidationEvidence, ...] = Field(
        default=(),
        max_length=MAX_EVIDENCE_ITEMS,
    )
    validator_session_ids: tuple[str, ...] = Field(default=(), max_length=2)

    @field_validator("validation_id")
    @classmethod
    def _id_text(cls, value: str) -> str:
        return _validate_text(value, field="validation_id", limit=128)

    @field_validator("summary")
    @classmethod
    def _summary_text(cls, value: str) -> str:
        return _validate_text(value, field="summary", limit=8_000)

    @field_validator("validator_session_ids")
    @classmethod
    def _session_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            _validate_text(value, field="validator_session_id", limit=128)
        return values

    @model_validator(mode="after")
    def _server_accounting_matches(self) -> "ValidationVerdictRecord":
        if self.round != self.budget.rounds_used:
            raise ValueError("round must equal budget.rounds_used")
        if len(self.validator_session_ids) != self.round:
            raise ValueError("validator session count must equal round")
        if self.reason_code == "deterministic_failure" and self.verdict != "fail":
            raise ValueError("deterministic failures require a fail verdict")
        if self.verdict == "pass" and self.reason_code != "model_verdict":
            raise ValueError("only a valid model verdict can produce pass")
        return self


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValidationContractError(f"non-finite JSON value is forbidden: {value}")


def parse_validation_model_verdict(
    raw_output: str,
    *,
    max_bytes: int = MAX_MODEL_OUTPUT_BYTES,
) -> ValidationModelVerdict:
    """Parse one plain JSON object with duplicate/extra/type checks enabled."""

    if not isinstance(raw_output, str):
        raise ValidationContractError("model output must be text")
    encoded = raw_output.encode("utf-8", errors="strict")
    if not encoded:
        raise ValidationContractError("model output is empty")
    if len(encoded) > max_bytes:
        raise ValidationContractError("model output exceeds byte budget")
    try:
        payload = json.loads(
            raw_output,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except ValidationContractError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValidationContractError("model output is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValidationContractError("model output must be one JSON object")
    try:
        # Use Pydantic's JSON path for the schema pass.  In strict mode it
        # still preserves JSON's canonical array representation for tuple
        # fields, whereas validating the already-decoded Python list would
        # incorrectly reject every valid JSON evidence array.
        return ValidationModelVerdict.model_validate_json(
            raw_output,
            strict=True,
        )
    except ValueError as exc:
        raise ValidationContractError("model output violates verdict schema v1") from exc


__all__ = [
    "VALIDATION_CONTRACT_VERSION",
    "MAX_MODEL_OUTPUT_BYTES",
    "MAX_EVIDENCE_ITEMS",
    "ValidationBudgetLimits",
    "ValidationBudgetReport",
    "ValidationContractError",
    "ValidationEvidence",
    "ValidationModelEvidence",
    "ValidationModelVerdict",
    "ValidationSource",
    "ValidationTask",
    "ValidationVerdictRecord",
    "DeterministicValidationFailure",
    "parse_validation_model_verdict",
]
