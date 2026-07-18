"""Durable, path-free checkpoint records for post-checkpoint validation.

Only the typed scheduler outcome crosses this boundary.  Task prompts,
workspace roots, exception strings, and caller-selected checkpoint or budget
values are deliberately absent from the durable shape.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Final, Literal, TypedDict

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.session_checkpoint import SessionCheckpoint
from app.streaming.manager import GenerationJob
from app.validation_agent.contracts import ValidationVerdictRecord
from app.validation_agent.scheduler import PostCheckpointValidationOutcome


POST_CHECKPOINT_VALIDATIONS_KEY: Final = "post_checkpoint_validations"
PERSISTED_VALIDATION_SCHEMA_VERSION: Final = 1
MAX_PERSISTED_VALIDATIONS: Final = 16
_MAX_CAS_ATTEMPTS: Final = 3
_ENTRY_KEYS: Final = frozenset(
    {
        "schema_version",
        "request_id",
        "policy_id",
        "status",
        "generation_job",
        "record",
    }
)
_BINDING_KEYS: Final = frozenset(
    {
        "session_id",
        "root_turn_id",
        "turn_run_id",
        "checkpoint_id",
        "workspace_instance_id",
    }
)
# Conservative absolute/private filesystem path markers. Workspace-relative
# evidence such as ``reports/q3.docx:1`` remains valid; host paths do not.
_PRIVATE_PATH = re.compile(
    r"(?:^|[\s\"'(<\[{:;,=])(?:"
    r"file://|~[\\/]|/(?!/)[^\s\"'<>\])}]+|"
    r"[A-Za-z]:[\\/]|\\\\[^\\\s]+[\\/])",
    re.IGNORECASE,
)

PersistenceReason = Literal[
    "checkpoint_missing",
    "checkpoint_not_finalized",
    "checkpoint_not_pinned",
    "generation_binding_mismatch",
    "invalid_outcome",
    "unsafe_record",
    "request_id_conflict",
    "capacity_exceeded",
    "concurrent_update",
]
PersistedValidationStatus = Literal["completed", "cancelled", "failed_closed"]
PublicValidationOverallStatus = Literal[
    "not_requested",
    "pass",
    "fail",
    "needs_review",
    "failed_closed",
    "cancelled",
    "invalid",
]


PublicValidationVerdictCounts = TypedDict(
    "PublicValidationVerdictCounts",
    {"pass": int, "fail": int, "needs_review": int},
)


class PublicCheckpointValidationSummary(TypedDict):
    """Path-free public projection of one checkpoint's validator records."""

    overall_status: PublicValidationOverallStatus
    count: int
    completed_count: int
    failed_count: int
    cancelled_count: int
    verdict_counts: PublicValidationVerdictCounts


class PostCheckpointValidationPersistenceError(RuntimeError):
    """Stable, path-free failure at the checkpoint persistence boundary."""

    def __init__(self, reason_code: PersistenceReason) -> None:
        self.reason_code = reason_code
        super().__init__(f"post-checkpoint validation persistence: {reason_code}")


class PostCheckpointValidationConflict(
    PostCheckpointValidationPersistenceError
):
    """A request ID or durable payload was reused with different provenance."""


@dataclass(frozen=True, slots=True)
class PostCheckpointValidationPersistenceReport:
    written_request_ids: tuple[str, ...] = ()
    replayed_request_ids: tuple[str, ...] = ()


def _identifier(value: object, *, maximum: int = 200) -> str:
    if not isinstance(value, str):
        raise PostCheckpointValidationPersistenceError("invalid_outcome")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > maximum
        or any(ord(character) < 32 for character in normalized)
    ):
        raise PostCheckpointValidationPersistenceError("invalid_outcome")
    return normalized


def _generation_binding(
    parent_job: GenerationJob,
    checkpoint_id: str,
) -> dict[str, str]:
    if not isinstance(parent_job, GenerationJob):
        raise PostCheckpointValidationPersistenceError(
            "generation_binding_mismatch"
        )
    if parent_job.invocation_source == "validator":
        raise PostCheckpointValidationPersistenceError(
            "generation_binding_mismatch"
        )
    workspace_instance_id = parent_job.workspace_instance_id
    if workspace_instance_id is None:
        raise PostCheckpointValidationPersistenceError(
            "generation_binding_mismatch"
        )
    return {
        "session_id": _identifier(parent_job.session_id),
        "root_turn_id": _identifier(parent_job.root_turn_id),
        "turn_run_id": _identifier(parent_job.turn_run_id),
        "checkpoint_id": _identifier(checkpoint_id),
        "workspace_instance_id": _identifier(workspace_instance_id),
    }


def _validate_checkpoint_binding(
    checkpoint: SessionCheckpoint | None,
    binding: dict[str, str],
) -> SessionCheckpoint:
    if checkpoint is None:
        raise PostCheckpointValidationPersistenceError("checkpoint_missing")
    if checkpoint.state != "finalized" or checkpoint.time_finalized is None:
        raise PostCheckpointValidationPersistenceError(
            "checkpoint_not_finalized"
        )
    if checkpoint.pin_state != "pinned":
        raise PostCheckpointValidationPersistenceError("checkpoint_not_pinned")
    if (
        checkpoint.id != binding["checkpoint_id"]
        or checkpoint.session_id != binding["session_id"]
        or checkpoint.root_turn_id != binding["root_turn_id"]
        or checkpoint.turn_run_id != binding["turn_run_id"]
        or checkpoint.workspace_instance_id
        != binding["workspace_instance_id"]
    ):
        raise PostCheckpointValidationPersistenceError(
            "generation_binding_mismatch"
        )
    return checkpoint


def _assert_path_free(value: object) -> None:
    if isinstance(value, str):
        if _PRIVATE_PATH.search(value):
            raise PostCheckpointValidationPersistenceError("unsafe_record")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise PostCheckpointValidationPersistenceError("unsafe_record")
            _assert_path_free(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _assert_path_free(item)


def _validate_record_source(
    record: ValidationVerdictRecord,
    binding: dict[str, str],
) -> None:
    source = record.source
    if (
        source.session_id != binding["session_id"]
        or source.root_turn_id != binding["root_turn_id"]
        or source.checkpoint_id != binding["checkpoint_id"]
        or source.workspace_instance_id != binding["workspace_instance_id"]
    ):
        raise PostCheckpointValidationPersistenceError(
            "generation_binding_mismatch"
        )
    # Cancellation and runtime failure records are allowed to be durable, but
    # the strict verdict contract and this explicit check prevent them from
    # ever being represented as a successful pass.
    if record.reason_code in {"cancelled", "runner_error", "timeout"} and (
        record.verdict == "pass"
    ):
        raise PostCheckpointValidationPersistenceError("invalid_outcome")


def _canonical_entry(
    outcome: PostCheckpointValidationOutcome,
    binding: dict[str, str],
) -> dict[str, Any]:
    if not isinstance(outcome, PostCheckpointValidationOutcome):
        raise PostCheckpointValidationPersistenceError("invalid_outcome")
    request_id = _identifier(outcome.request_id, maximum=128)
    policy_id = _identifier(outcome.policy_id, maximum=120)
    record_json: dict[str, Any] | None
    if outcome.status == "completed":
        if not isinstance(outcome.record, ValidationVerdictRecord):
            raise PostCheckpointValidationPersistenceError("invalid_outcome")
        _validate_record_source(outcome.record, binding)
        record_json = outcome.record.model_dump(mode="json")
        _assert_path_free(record_json)
    elif outcome.status == "failed_closed":
        if outcome.record is not None:
            raise PostCheckpointValidationPersistenceError("invalid_outcome")
        record_json = None
    else:
        raise PostCheckpointValidationPersistenceError("invalid_outcome")
    durable_status: PersistedValidationStatus
    if outcome.status == "failed_closed":
        durable_status = "failed_closed"
    elif record_json is not None and outcome.record is not None:
        if outcome.record.reason_code == "cancelled":
            durable_status = "cancelled"
        elif outcome.record.reason_code in {
            "runner_error",
            "timeout",
            "budget_exhausted",
        }:
            durable_status = "failed_closed"
        else:
            durable_status = "completed"
    else:  # pragma: no cover - guarded by the status/record checks above
        raise PostCheckpointValidationPersistenceError("invalid_outcome")
    if durable_status != "completed" and outcome.passed:
        raise PostCheckpointValidationPersistenceError("invalid_outcome")
    entry = {
        "schema_version": PERSISTED_VALIDATION_SCHEMA_VERSION,
        "request_id": request_id,
        "policy_id": policy_id,
        "status": durable_status,
        "generation_job": dict(binding),
        "record": record_json,
    }
    _assert_path_free(entry)
    return entry


def _parse_existing_entry(
    raw: object,
    binding: dict[str, str],
) -> dict[str, Any]:
    if not isinstance(raw, dict) or frozenset(raw) != _ENTRY_KEYS:
        raise PostCheckpointValidationConflict("request_id_conflict")
    # ``bool`` compares equal to integer 1 in Python.  Check the concrete type
    # before the final canonical equality so persisted schema versions remain
    # genuinely strict when read back from an untrusted JSON column.
    if (
        type(raw.get("schema_version")) is not int
        or raw.get("schema_version") != PERSISTED_VALIDATION_SCHEMA_VERSION
    ):
        raise PostCheckpointValidationConflict("request_id_conflict")
    raw_binding = raw.get("generation_job")
    if (
        not isinstance(raw_binding, dict)
        or frozenset(raw_binding) != _BINDING_KEYS
        or raw_binding != binding
    ):
        raise PostCheckpointValidationConflict("request_id_conflict")
    raw_record = raw.get("record")
    record: ValidationVerdictRecord | None
    if raw_record is None:
        record = None
    elif isinstance(raw_record, dict):
        try:
            record = ValidationVerdictRecord.model_validate_json(
                json.dumps(raw_record, ensure_ascii=True)
            )
        except Exception as exc:
            raise PostCheckpointValidationConflict(
                "request_id_conflict"
            ) from exc
    else:
        raise PostCheckpointValidationConflict("request_id_conflict")
    raw_status = raw.get("status")
    if raw_status not in {"completed", "cancelled", "failed_closed"}:
        raise PostCheckpointValidationConflict("request_id_conflict")
    if record is None and raw_status != "failed_closed":
        raise PostCheckpointValidationConflict("request_id_conflict")
    try:
        outcome = PostCheckpointValidationOutcome(
            request_id=raw.get("request_id"),
            policy_id=raw.get("policy_id"),
            status="completed" if record is not None else "failed_closed",
            record=record,
        )
        canonical = _canonical_entry(outcome, binding)
    except PostCheckpointValidationPersistenceError:
        raise
    except Exception as exc:
        raise PostCheckpointValidationConflict("request_id_conflict") from exc
    if raw != canonical:
        raise PostCheckpointValidationConflict("request_id_conflict")
    return canonical


def _public_summary(
    overall_status: PublicValidationOverallStatus,
    *,
    count: int = 0,
    completed_count: int = 0,
    failed_count: int = 0,
    cancelled_count: int = 0,
    pass_count: int = 0,
    fail_count: int = 0,
    needs_review_count: int = 0,
) -> PublicCheckpointValidationSummary:
    """Create a fresh public-only dictionary with no durable identifiers."""

    return {
        "overall_status": overall_status,
        "count": count,
        "completed_count": completed_count,
        "failed_count": failed_count,
        "cancelled_count": cancelled_count,
        "verdict_counts": {
            "pass": pass_count,
            "fail": fail_count,
            "needs_review": needs_review_count,
        },
    }


def not_requested_validation_summary() -> PublicCheckpointValidationSummary:
    """Return the public shape used for a closed gate or an empty request set."""

    return _public_summary("not_requested")


def invalid_validation_summary(
    *, count: int = 0
) -> PublicCheckpointValidationSummary:
    """Return a fail-closed public shape without partially trusted counters."""

    return _public_summary("invalid", count=max(0, count))


def parse_public_checkpoint_validation_summary(
    checkpoint: SessionCheckpoint | None,
) -> PublicCheckpointValidationSummary:
    """Strictly revalidate durable records into a path-free public summary.

    Every entry is reconstructed through the same canonical parser used for
    idempotent persistence.  The durable generation binding and the strict
    :class:`ValidationVerdictRecord` source must match the supplied database
    checkpoint exactly.  One malformed, replayed, unsafe, or foreign entry
    invalidates the entire public projection; no prompt, evidence, summary,
    path, session identifier, request identifier, or policy identifier is
    copied to the result.
    """

    if not isinstance(checkpoint, SessionCheckpoint):
        return invalid_validation_summary()
    details = checkpoint.details
    if not isinstance(details, dict):
        return invalid_validation_summary()
    raw_entries = details.get(POST_CHECKPOINT_VALIDATIONS_KEY)
    if raw_entries is None:
        return not_requested_validation_summary()
    if not isinstance(raw_entries, list):
        return invalid_validation_summary()
    count = len(raw_entries)
    if count == 0:
        return not_requested_validation_summary()
    if count > MAX_PERSISTED_VALIDATIONS:
        return invalid_validation_summary(count=count)

    try:
        checkpoint_values = {
            "session_id": checkpoint.session_id,
            "root_turn_id": checkpoint.root_turn_id,
            "turn_run_id": checkpoint.turn_run_id,
            "checkpoint_id": checkpoint.id,
            "workspace_instance_id": checkpoint.workspace_instance_id,
        }
        binding = {
            key: _identifier(value)
            for key, value in checkpoint_values.items()
        }
        if any(binding[key] != value for key, value in checkpoint_values.items()):
            raise PostCheckpointValidationConflict("request_id_conflict")
        _validate_checkpoint_binding(checkpoint, binding)
        completed_count = 0
        failed_count = 0
        cancelled_count = 0
        verdict_counts = {"pass": 0, "fail": 0, "needs_review": 0}
        request_ids: set[str] = set()

        for raw_entry in raw_entries:
            entry = _parse_existing_entry(raw_entry, binding)
            request_id = entry["request_id"]
            if request_id in request_ids:
                raise PostCheckpointValidationConflict("request_id_conflict")
            request_ids.add(request_id)

            status = entry["status"]
            if status == "failed_closed":
                failed_count += 1
                continue
            if status == "cancelled":
                cancelled_count += 1
                continue
            if status != "completed" or not isinstance(entry["record"], dict):
                raise PostCheckpointValidationConflict("request_id_conflict")

            record = ValidationVerdictRecord.model_validate_json(
                json.dumps(entry["record"], ensure_ascii=True)
            )
            _validate_record_source(record, binding)
            if record.verdict == "pass" and record.reason_code != "model_verdict":
                raise PostCheckpointValidationConflict("request_id_conflict")
            completed_count += 1
            verdict_counts[record.verdict] += 1

        if completed_count + failed_count + cancelled_count != count:
            raise PostCheckpointValidationConflict("request_id_conflict")
        if sum(verdict_counts.values()) != completed_count:
            raise PostCheckpointValidationConflict("request_id_conflict")

        if failed_count:
            overall_status: PublicValidationOverallStatus = "failed_closed"
        elif cancelled_count:
            overall_status = "cancelled"
        elif verdict_counts["fail"]:
            overall_status = "fail"
        elif verdict_counts["needs_review"]:
            overall_status = "needs_review"
        elif (
            completed_count > 0
            and verdict_counts["pass"] == completed_count
        ):
            overall_status = "pass"
        else:
            raise PostCheckpointValidationConflict("request_id_conflict")

        return _public_summary(
            overall_status,
            count=count,
            completed_count=completed_count,
            failed_count=failed_count,
            cancelled_count=cancelled_count,
            pass_count=verdict_counts["pass"],
            fail_count=verdict_counts["fail"],
            needs_review_count=verdict_counts["needs_review"],
        )
    except Exception:
        # The public boundary is deliberately fail-closed and never reflects
        # exception text because it may contain private validation material.
        return invalid_validation_summary(count=count)


def _merge_entries(
    existing_raw: object,
    candidates: tuple[dict[str, Any], ...],
    binding: dict[str, str],
) -> tuple[list[dict[str, Any]], tuple[str, ...], tuple[str, ...]]:
    if existing_raw is None:
        existing_items: list[object] = []
    elif isinstance(existing_raw, list):
        existing_items = list(existing_raw)
    else:
        raise PostCheckpointValidationConflict("request_id_conflict")
    if len(existing_items) > MAX_PERSISTED_VALIDATIONS:
        raise PostCheckpointValidationConflict("capacity_exceeded")

    merged: list[dict[str, Any]] = []
    by_request: dict[str, dict[str, Any]] = {}
    for raw in existing_items:
        entry = _parse_existing_entry(raw, binding)
        request_id = entry["request_id"]
        if request_id in by_request:
            raise PostCheckpointValidationConflict("request_id_conflict")
        by_request[request_id] = entry
        merged.append(entry)

    written: list[str] = []
    replayed: list[str] = []
    for candidate in candidates:
        request_id = candidate["request_id"]
        previous = by_request.get(request_id)
        if previous is not None:
            if previous != candidate:
                raise PostCheckpointValidationConflict("request_id_conflict")
            if request_id not in replayed:
                replayed.append(request_id)
            continue
        if len(merged) >= MAX_PERSISTED_VALIDATIONS:
            raise PostCheckpointValidationConflict("capacity_exceeded")
        by_request[request_id] = candidate
        merged.append(candidate)
        written.append(request_id)
    return merged, tuple(written), tuple(replayed)


async def persist_post_checkpoint_validation_outcomes(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    parent_job: GenerationJob,
    checkpoint_id: str,
    outcomes: tuple[PostCheckpointValidationOutcome, ...],
) -> PostCheckpointValidationPersistenceReport:
    """Atomically append or replay typed results on one finalized checkpoint."""

    if not isinstance(outcomes, tuple) or any(
        not isinstance(item, PostCheckpointValidationOutcome) for item in outcomes
    ):
        raise PostCheckpointValidationPersistenceError("invalid_outcome")
    if not outcomes:
        return PostCheckpointValidationPersistenceReport()
    binding = _generation_binding(parent_job, checkpoint_id)
    candidates = tuple(_canonical_entry(item, binding) for item in outcomes)

    # Reject conflicting duplicates in the submitted batch before touching DB.
    submitted: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        request_id = candidate["request_id"]
        previous = submitted.get(request_id)
        if previous is not None and previous != candidate:
            raise PostCheckpointValidationConflict("request_id_conflict")
        submitted[request_id] = candidate

    for _attempt in range(_MAX_CAS_ATTEMPTS):
        async with session_factory() as db:
            async with db.begin():
                checkpoint = await db.get(
                    SessionCheckpoint,
                    binding["checkpoint_id"],
                )
                checkpoint = _validate_checkpoint_binding(checkpoint, binding)
                if not isinstance(checkpoint.details, dict):
                    raise PostCheckpointValidationConflict(
                        "request_id_conflict"
                    )
                original_details = dict(checkpoint.details)
                merged, written, replayed = _merge_entries(
                    original_details.get(POST_CHECKPOINT_VALIDATIONS_KEY),
                    candidates,
                    binding,
                )
                if not written:
                    return PostCheckpointValidationPersistenceReport(
                        replayed_request_ids=replayed
                    )
                updated_details = dict(original_details)
                updated_details[POST_CHECKPOINT_VALIDATIONS_KEY] = merged
                result = await db.execute(
                    update(SessionCheckpoint)
                    .where(
                        SessionCheckpoint.id == binding["checkpoint_id"],
                        SessionCheckpoint.session_id == binding["session_id"],
                        SessionCheckpoint.root_turn_id
                        == binding["root_turn_id"],
                        SessionCheckpoint.turn_run_id
                        == binding["turn_run_id"],
                        SessionCheckpoint.workspace_instance_id
                        == binding["workspace_instance_id"],
                        SessionCheckpoint.state == "finalized",
                        SessionCheckpoint.pin_state == "pinned",
                        SessionCheckpoint.details == original_details,
                    )
                    .values(details=updated_details)
                )
                if result.rowcount == 1:
                    return PostCheckpointValidationPersistenceReport(
                        written_request_ids=written,
                        replayed_request_ids=replayed,
                    )
    raise PostCheckpointValidationConflict("concurrent_update")


__all__ = [
    "MAX_PERSISTED_VALIDATIONS",
    "PERSISTED_VALIDATION_SCHEMA_VERSION",
    "POST_CHECKPOINT_VALIDATIONS_KEY",
    "PostCheckpointValidationConflict",
    "PostCheckpointValidationPersistenceError",
    "PostCheckpointValidationPersistenceReport",
    "PublicCheckpointValidationSummary",
    "PublicValidationOverallStatus",
    "invalid_validation_summary",
    "not_requested_validation_summary",
    "parse_public_checkpoint_validation_summary",
    "persist_post_checkpoint_validation_outcomes",
]
