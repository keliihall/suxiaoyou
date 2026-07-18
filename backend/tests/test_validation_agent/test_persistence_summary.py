from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import json

import pytest

from app.models.session_checkpoint import SessionCheckpoint
from app.validation_agent.contracts import (
    ValidationBudgetReport,
    ValidationSource,
    ValidationVerdictRecord,
)
from app.validation_agent.persistence import (
    POST_CHECKPOINT_VALIDATIONS_KEY,
    parse_public_checkpoint_validation_summary,
)


def _checkpoint() -> SessionCheckpoint:
    return SessionCheckpoint(
        id="checkpoint-public-summary",
        session_id="session-public-summary",
        workspace_instance_id="workspace-public-summary",
        root_turn_id="turn-public-summary",
        turn_run_id="turn-public-summary",
        sequence=4,
        state="finalized",
        pin_state="pinned",
        details={},
        time_finalized=datetime.now(UTC),
    )


def _record(
    checkpoint: SessionCheckpoint,
    *,
    verdict: str,
    reason_code: str = "model_verdict",
) -> ValidationVerdictRecord:
    rounds = 0 if reason_code == "cancelled" else 1
    return ValidationVerdictRecord.model_validate(
        {
            "schema_version": 1,
            "validation_id": f"private-validation-{verdict}",
            "verdict": verdict,
            "reason_code": reason_code,
            "source": ValidationSource(
                session_id=checkpoint.session_id,
                root_turn_id=checkpoint.root_turn_id,
                checkpoint_id=checkpoint.id,
                workspace_instance_id=checkpoint.workspace_instance_id,
            ),
            "round": rounds,
            "budget": ValidationBudgetReport(
                max_rounds=2,
                max_tokens=8_000,
                timeout_ms=60_000,
                rounds_used=rounds,
                tokens_used=12,
                elapsed_ms=25,
            ),
            "summary": "Private validator summary must never cross the API.",
            "evidence": (),
            "validator_session_ids": (
                ("private-validator-session",) if rounds else ()
            ),
        }
    )


def _entry(
    checkpoint: SessionCheckpoint,
    *,
    request_id: str,
    status: str,
    record: ValidationVerdictRecord | None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "request_id": request_id,
        "policy_id": "private.server.policy",
        "status": status,
        "generation_job": {
            "session_id": checkpoint.session_id,
            "root_turn_id": checkpoint.root_turn_id,
            "turn_run_id": checkpoint.turn_run_id,
            "checkpoint_id": checkpoint.id,
            "workspace_instance_id": checkpoint.workspace_instance_id,
        },
        "record": record.model_dump(mode="json") if record is not None else None,
    }


def _set_entries(
    checkpoint: SessionCheckpoint,
    entries: list[dict[str, object]],
) -> None:
    checkpoint.details = {POST_CHECKPOINT_VALIDATIONS_KEY: entries}


@pytest.mark.parametrize(
    ("entry_status", "verdict", "reason_code", "overall_status"),
    [
        ("completed", "pass", "model_verdict", "pass"),
        ("completed", "fail", "model_verdict", "fail"),
        ("completed", "needs_review", "model_verdict", "needs_review"),
        ("failed_closed", None, None, "failed_closed"),
        ("cancelled", "needs_review", "cancelled", "cancelled"),
    ],
)
def test_public_summary_exposes_only_strict_aggregate_status(
    entry_status: str,
    verdict: str | None,
    reason_code: str | None,
    overall_status: str,
) -> None:
    checkpoint = _checkpoint()
    record = (
        _record(checkpoint, verdict=verdict, reason_code=reason_code or "")
        if verdict is not None
        else None
    )
    _set_entries(
        checkpoint,
        [
            _entry(
                checkpoint,
                request_id="private-request-id",
                status=entry_status,
                record=record,
            )
        ],
    )

    summary = parse_public_checkpoint_validation_summary(checkpoint)

    assert summary["overall_status"] == overall_status
    assert summary["count"] == 1
    assert summary["completed_count"] == (entry_status == "completed")
    assert summary["failed_count"] == (entry_status == "failed_closed")
    assert summary["cancelled_count"] == (entry_status == "cancelled")
    assert summary["verdict_counts"] == {
        "pass": int(entry_status == "completed" and verdict == "pass"),
        "fail": int(entry_status == "completed" and verdict == "fail"),
        "needs_review": int(
            entry_status == "completed" and verdict == "needs_review"
        ),
    }
    assert set(summary) == {
        "overall_status",
        "count",
        "completed_count",
        "failed_count",
        "cancelled_count",
        "verdict_counts",
    }
    serialized = json.dumps(summary, ensure_ascii=False)
    for secret in (
        "private-request-id",
        "private.server.policy",
        "private-validator-session",
        "Private validator summary",
        checkpoint.id,
        checkpoint.session_id,
        checkpoint.workspace_instance_id,
    ):
        assert secret not in serialized


def test_public_summary_is_not_requested_without_a_durable_request() -> None:
    checkpoint = _checkpoint()

    assert parse_public_checkpoint_validation_summary(checkpoint) == {
        "overall_status": "not_requested",
        "count": 0,
        "completed_count": 0,
        "failed_count": 0,
        "cancelled_count": 0,
        "verdict_counts": {"pass": 0, "fail": 0, "needs_review": 0},
    }


def test_any_corrupt_schema_or_binding_invalidates_the_whole_summary() -> None:
    checkpoint = _checkpoint()
    valid = _entry(
        checkpoint,
        request_id="request-valid",
        status="completed",
        record=_record(checkpoint, verdict="pass"),
    )
    corrupt_entries: list[list[dict[str, object]]] = []

    wrong_schema = deepcopy(valid)
    wrong_schema["schema_version"] = True
    corrupt_entries.append([wrong_schema])

    extra_key = deepcopy(valid)
    extra_key["private_path"] = "/Users/private/report.docx"
    corrupt_entries.append([extra_key])

    foreign_generation = deepcopy(valid)
    generation = foreign_generation["generation_job"]
    assert isinstance(generation, dict)
    generation["checkpoint_id"] = "foreign-checkpoint"
    corrupt_entries.append([foreign_generation])

    foreign_source = deepcopy(valid)
    record = foreign_source["record"]
    assert isinstance(record, dict)
    source = record["source"]
    assert isinstance(source, dict)
    source["workspace_instance_id"] = "foreign-workspace"
    corrupt_entries.append([foreign_source])

    forged_pass = deepcopy(valid)
    forged_record = forged_pass["record"]
    assert isinstance(forged_record, dict)
    forged_record["reason_code"] = "runner_error"
    corrupt_entries.append([forged_pass])

    corrupt_entries.append([deepcopy(valid), deepcopy(valid)])

    for entries in corrupt_entries:
        _set_entries(checkpoint, entries)
        summary = parse_public_checkpoint_validation_summary(checkpoint)
        assert summary["overall_status"] == "invalid"
        assert summary["count"] == len(entries)
        assert summary["completed_count"] == 0
        assert summary["failed_count"] == 0
        assert summary["cancelled_count"] == 0
        assert summary["verdict_counts"] == {
            "pass": 0,
            "fail": 0,
            "needs_review": 0,
        }


def test_failed_closed_and_cancelled_prevent_a_mixed_set_from_passing() -> None:
    checkpoint = _checkpoint()
    passed = _entry(
        checkpoint,
        request_id="request-pass",
        status="completed",
        record=_record(checkpoint, verdict="pass"),
    )
    cancelled = _entry(
        checkpoint,
        request_id="request-cancelled",
        status="cancelled",
        record=_record(
            checkpoint,
            verdict="needs_review",
            reason_code="cancelled",
        ),
    )
    failed = _entry(
        checkpoint,
        request_id="request-failed",
        status="failed_closed",
        record=None,
    )
    _set_entries(checkpoint, [passed, cancelled, failed])

    summary = parse_public_checkpoint_validation_summary(checkpoint)

    assert summary == {
        "overall_status": "failed_closed",
        "count": 3,
        "completed_count": 1,
        "failed_count": 1,
        "cancelled_count": 1,
        "verdict_counts": {"pass": 1, "fail": 0, "needs_review": 0},
    }


def test_non_finalized_or_released_checkpoint_cannot_retain_a_public_pass() -> None:
    checkpoint = _checkpoint()
    _set_entries(
        checkpoint,
        [
            _entry(
                checkpoint,
                request_id="request-before-rewind",
                status="completed",
                record=_record(checkpoint, verdict="pass"),
            )
        ],
    )

    checkpoint.state = "rewound"
    checkpoint.pin_state = "released"
    summary = parse_public_checkpoint_validation_summary(checkpoint)

    assert summary["overall_status"] == "invalid"
    assert summary["verdict_counts"]["pass"] == 0
