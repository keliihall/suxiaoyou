from __future__ import annotations

from datetime import datetime, timezone
import math

import pytest
from pydantic import ValidationError

from app.hooks.models import (
    HookCommandDeclaration,
    HookDecision,
    HookDecisionKind,
    HookEvent,
    HookEventName,
    HookProtocolError,
    combine_pre_tool_decisions,
)


def _event_payload(**overrides):
    payload = {
        "version": 1,
        "event_id": "event-1",
        "event": "PreToolUse",
        "sequence": 1,
        "occurred_at": datetime.now(timezone.utc),
        "payload": {"tool_name": "read", "tool_args": {}},
    }
    payload.update(overrides)
    return payload


def test_event_contract_accepts_every_v1_event_name() -> None:
    for event_name in HookEventName:
        event = HookEvent.model_validate(_event_payload(event=event_name.value))
        assert event.version == 1
        assert event.event is event_name
        assert HookEvent.model_validate_json(event.to_wire_bytes()) == event


@pytest.mark.parametrize(
    "override",
    [
        {"version": 2},
        {"event": "FutureEvent"},
        {"occurred_at": datetime.now()},
        {"unexpected": True},
    ],
)
def test_unknown_or_malformed_event_contract_fails_closed(override) -> None:
    with pytest.raises(ValidationError):
        HookEvent.model_validate(_event_payload(**override))


@pytest.mark.parametrize("decision", ["allow", "deny", "ask"])
def test_pre_tool_wire_decisions_are_exactly_allow_deny_ask(decision) -> None:
    parsed = HookDecision.from_wire_bytes(
        f'{{"version":1,"decision":"{decision}"}}'.encode(),
        event=HookEventName.PRE_TOOL_USE,
    )
    assert parsed.decision.value == decision


def test_pre_tool_response_cannot_rewrite_tool_args_or_permissions() -> None:
    response = (
        b'{"version":1,"decision":"allow",'
        b'"tool_name":"read","tool_args":{},"permission":"allow"}'
    )
    with pytest.raises(HookProtocolError):
        HookDecision.from_wire_bytes(
            response,
            event=HookEventName.PRE_TOOL_USE,
        )


def test_response_duplicate_keys_and_non_standard_event_numbers_are_rejected() -> None:
    with pytest.raises(HookProtocolError, match="duplicate"):
        HookDecision.from_wire_bytes(
            b'{"version":1,"decision":"deny","decision":"allow"}',
            event=HookEventName.PRE_TOOL_USE,
        )
    with pytest.raises(ValidationError):
        HookEvent.model_validate(_event_payload(payload={"score": math.nan}))


def test_non_pre_tool_hook_cannot_return_authority_decision() -> None:
    with pytest.raises(HookProtocolError):
        HookDecision.from_wire_bytes(
            b'{"version":1,"decision":"allow"}',
            event=HookEventName.POST_TOOL_USE,
        )
    decision = HookDecision.from_wire_bytes(
        b'{"version":1,"decision":"continue","annotation":"observed"}',
        event=HookEventName.POST_TOOL_USE,
    )
    assert decision.annotation == "observed"


def test_permission_combination_can_only_narrow_authority() -> None:
    assert combine_pre_tool_decisions("deny", "allow") is HookDecisionKind.DENY
    assert combine_pre_tool_decisions("ask", "allow") is HookDecisionKind.ASK
    assert combine_pre_tool_decisions("allow", "ask") is HookDecisionKind.ASK
    with pytest.raises(ValueError):
        combine_pre_tool_decisions("continue", "allow")


def test_command_declaration_requires_explicit_failure_policy_and_timeout_cap() -> None:
    base = {
        "hook_id": "policy",
        "event": "PreToolUse",
        "command": ["hook"],
    }
    with pytest.raises(ValidationError):
        HookCommandDeclaration.model_validate(base)
    with pytest.raises(ValidationError):
        HookCommandDeclaration.model_validate({
            **base,
            "failure_policy": "required",
            "timeout_seconds": 30.01,
        })
    parsed = HookCommandDeclaration.model_validate({
        **base,
        "failure_policy": "required",
    })
    assert parsed.timeout_seconds == 5
