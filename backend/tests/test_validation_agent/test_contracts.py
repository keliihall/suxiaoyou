from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.schemas.chat import PromptRequest
from app.validation_agent.contracts import (
    DeterministicValidationFailure,
    ValidationBudgetLimits,
    ValidationBudgetReport,
    ValidationContractError,
    ValidationSource,
    ValidationTask,
    ValidationVerdictRecord,
    parse_validation_model_verdict,
)


def _valid_model_payload() -> dict:
    return {
        "schema_version": 1,
        "verdict": "pass",
        "summary": "The required file contains the expected invariant.",
        "evidence": [
            {
                "kind": "file",
                "source": "src/example.py:12",
                "summary": "The guarded branch is present.",
            }
        ],
    }


def test_model_verdict_parser_accepts_only_plain_strict_v1_json() -> None:
    verdict = parse_validation_model_verdict(json.dumps(_valid_model_payload()))

    assert verdict.schema_version == 1
    assert verdict.verdict == "pass"
    assert verdict.evidence[0].source == "src/example.py:12"

    with pytest.raises(ValidationContractError, match="valid JSON"):
        parse_validation_model_verdict("```json\n{}\n```")
    with pytest.raises(ValidationContractError, match="duplicate JSON key"):
        parse_validation_model_verdict(
            '{"schema_version":1,"verdict":"pass","verdict":"fail",'
            '"summary":"x","evidence":[]}'
        )
    with pytest.raises(ValidationContractError, match="verdict schema"):
        payload = _valid_model_payload()
        payload["workspace"] = "/model/chosen"
        parse_validation_model_verdict(json.dumps(payload))
    with pytest.raises(ValidationContractError, match="verdict schema"):
        payload = _valid_model_payload()
        payload["schema_version"] = 2
        parse_validation_model_verdict(json.dumps(payload))
    with pytest.raises(ValidationContractError, match="verdict schema"):
        payload = _valid_model_payload()
        payload["evidence"] = []
        parse_validation_model_verdict(json.dumps(payload))
    with pytest.raises(ValidationContractError, match="byte budget"):
        parse_validation_model_verdict(json.dumps(_valid_model_payload()), max_bytes=8)


def test_contracts_reject_coercion_extra_fields_and_round_three() -> None:
    with pytest.raises(ValidationError):
        ValidationBudgetLimits(max_rounds="2")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ValidationBudgetLimits(max_rounds=3)
    with pytest.raises(ValidationError):
        ValidationTask(objective="check", workspace="/tmp")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        ValidationTask(objective="\x00")

    source = ValidationSource(
        session_id="session",
        root_turn_id="turn",
        checkpoint_id="checkpoint",
        workspace_instance_id="workspace",
    )
    with pytest.raises(ValidationError, match="less than or equal to 2"):
        ValidationVerdictRecord(
            schema_version=1,
            validation_id="validation",
            verdict="needs_review",
            reason_code="timeout",
            source=source,
            round=3,
            budget=ValidationBudgetReport(
                max_rounds=2,
                max_tokens=10,
                timeout_ms=100,
                rounds_used=2,
                tokens_used=0,
                elapsed_ms=1,
            ),
            summary="timeout",
            validator_session_ids=("one", "two"),
        )


def test_deterministic_failure_and_budget_invariants_are_server_shaped() -> None:
    failure = DeterministicValidationFailure(
        code="visual_diff",
        source="preview/page-1.png",
        summary="Pixel difference exceeds the release threshold.",
    )
    task = ValidationTask(
        objective="Verify the rendered document.",
        deterministic_failures=(failure,),
        budget=ValidationBudgetLimits(
            max_rounds=2,
            max_tokens=500,
            timeout_ms=2_000,
        ),
    )

    assert task.deterministic_failures == (failure,)
    with pytest.raises(ValidationError, match="rounds_used exceeds"):
        ValidationBudgetReport(
            max_rounds=1,
            max_tokens=500,
            timeout_ms=2_000,
            rounds_used=2,
            tokens_used=1,
            elapsed_ms=1,
        )


def test_external_prompt_json_cannot_forge_server_output_ceiling() -> None:
    request = PromptRequest.model_validate(
        {
            "text": "hello",
            "_max_output_tokens_ceiling": 999_999,
        }
    )

    assert request._max_output_tokens_ceiling is None
    assert "_max_output_tokens_ceiling" not in request.model_dump()
