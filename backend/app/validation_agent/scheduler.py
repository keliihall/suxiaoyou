"""Post-checkpoint scheduling for explicit server-owned validation requests.

The model cannot enqueue these requests: no tool or request schema exposes this
module.  A trusted product component supplies only an intent.  The scheduler
selects the actual finalized checkpoint at the SessionPrompt boundary and owns
the validation budget, so neither checkpoint identity nor resource limits can
come from model output.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal, Protocol
from weakref import WeakKeyDictionary

from app.streaming.manager import GenerationJob
from app.utils.id import generate_ulid
from app.validation_agent.contracts import (
    DeterministicValidationFailure,
    ValidationBudgetLimits,
    ValidationTask,
    ValidationVerdictRecord,
)
from app.validation_agent.service import validation_agent_enabled


class PostCheckpointValidator(Protocol):
    async def validate(
        self,
        *,
        parent_job: GenerationJob,
        checkpoint_id: str,
        task: ValidationTask,
    ) -> ValidationVerdictRecord: ...


@dataclass(frozen=True, slots=True)
class ServerValidationIntent:
    """Trusted task intent with deliberately no checkpoint or budget fields."""

    policy_id: str
    objective: str
    deterministic_failures: tuple[DeterministicValidationFailure, ...] = ()

    def __post_init__(self) -> None:
        policy_id = str(self.policy_id).strip()
        objective = str(self.objective).strip()
        failures = tuple(self.deterministic_failures)
        if (
            not policy_id
            or len(policy_id) > 120
            or any(ord(character) < 32 for character in policy_id)
        ):
            raise ValueError("validation policy_id is invalid")
        if (
            not objective
            or len(objective) > 16_000
            or any(
                ord(character) < 32 and character not in "\t\n\r"
                for character in objective
            )
        ):
            raise ValueError("validation objective is invalid")
        if len(failures) > 32 or any(
            not isinstance(item, DeterministicValidationFailure)
            for item in failures
        ):
            raise ValueError("deterministic validation failures are invalid")
        object.__setattr__(self, "policy_id", policy_id)
        object.__setattr__(self, "objective", objective)
        object.__setattr__(self, "deterministic_failures", failures)


@dataclass(frozen=True, slots=True)
class PostCheckpointValidationOutcome:
    request_id: str
    policy_id: str
    status: Literal["completed", "failed_closed"]
    record: ValidationVerdictRecord | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not isinstance(
            self.policy_id, str
        ):
            raise ValueError("validation outcome identifiers must be strings")
        request_id = self.request_id.strip()
        policy_id = self.policy_id.strip()
        if (
            not request_id
            or len(request_id) > 128
            or any(ord(character) < 32 for character in request_id)
        ):
            raise ValueError("validation request_id is invalid")
        if (
            not policy_id
            or len(policy_id) > 120
            or any(ord(character) < 32 for character in policy_id)
        ):
            raise ValueError("validation policy_id is invalid")
        if not isinstance(self.status, str) or self.status not in {
            "completed",
            "failed_closed",
        }:
            raise ValueError("validation outcome status is invalid")
        if self.status == "completed" and not isinstance(
            self.record, ValidationVerdictRecord
        ):
            raise ValueError("completed validation outcome requires a record")
        if self.status == "failed_closed" and self.record is not None:
            raise ValueError("failed-closed validation outcome cannot carry a record")
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "policy_id", policy_id)

    @property
    def passed(self) -> bool:
        return self.status == "completed" and self.record is not None and (
            self.record.verdict == "pass"
        )


@dataclass(frozen=True, slots=True)
class _PendingValidation:
    request_id: str
    session_id: str
    root_turn_id: str
    turn_run_id: str
    workspace_instance_id: str
    intent: ServerValidationIntent


class PostCheckpointValidationScheduler:
    """Consume explicit intents after the owning checkpoint is finalized."""

    def __init__(
        self,
        validator: PostCheckpointValidator,
        *,
        budget: ValidationBudgetLimits | None = None,
        max_pending_per_turn: int = 16,
    ) -> None:
        if not hasattr(validator, "validate"):
            raise TypeError("post-checkpoint validator is invalid")
        resolved_budget = budget or ValidationBudgetLimits()
        if not isinstance(resolved_budget, ValidationBudgetLimits):
            raise TypeError("post-checkpoint validation budget is invalid")
        if (
            not isinstance(max_pending_per_turn, int)
            or isinstance(max_pending_per_turn, bool)
            or not 1 <= max_pending_per_turn <= 64
        ):
            raise ValueError("max_pending_per_turn must be between 1 and 64")
        self._validator = validator
        self._budget = resolved_budget
        self._max_pending_per_turn = max_pending_per_turn
        self._lock = asyncio.Lock()
        self._pending: WeakKeyDictionary[
            GenerationJob,
            dict[str, list[_PendingValidation]],
        ] = WeakKeyDictionary()

    @property
    def enabled(self) -> bool:
        return validation_agent_enabled()

    async def request_validation(
        self,
        *,
        parent_job: GenerationJob,
        intent: ServerValidationIntent,
    ) -> str | None:
        """Queue one task without accepting a checkpoint or budget from caller."""

        if not self.enabled:
            return None
        if not isinstance(parent_job, GenerationJob):
            raise TypeError("parent_job must be a GenerationJob")
        if not isinstance(intent, ServerValidationIntent):
            raise TypeError("validation intent must be server-owned")
        if parent_job.invocation_source == "validator":
            raise ValueError("a validator child cannot request validation")
        if parent_job.completed:
            raise ValueError("validation cannot be requested after root completion")
        workspace_instance_id = parent_job.workspace_instance_id
        if not workspace_instance_id:
            raise ValueError("validation request has no workspace instance")

        pending = _PendingValidation(
            request_id=generate_ulid(),
            session_id=parent_job.session_id,
            root_turn_id=parent_job.root_turn_id,
            turn_run_id=parent_job.turn_run_id,
            workspace_instance_id=workspace_instance_id,
            intent=intent,
        )
        async with self._lock:
            by_turn = self._pending.setdefault(parent_job, {})
            requests = by_turn.setdefault(parent_job.root_turn_id, [])
            if len(requests) >= self._max_pending_per_turn:
                raise RuntimeError("post-checkpoint validation request limit reached")
            requests.append(pending)
        parent_job.publish_lifecycle(
            "validation.requested",
            {
                "request_id": pending.request_id,
                "policy_id": intent.policy_id,
            },
        )
        return pending.request_id

    async def run_pending(
        self,
        *,
        parent_job: GenerationJob,
        checkpoint_id: str,
    ) -> tuple[PostCheckpointValidationOutcome, ...]:
        """Validate queued tasks against this server-selected checkpoint once."""

        if not isinstance(parent_job, GenerationJob):
            raise TypeError("parent_job must be a GenerationJob")
        normalized_checkpoint = str(checkpoint_id).strip()
        if not normalized_checkpoint:
            raise ValueError("checkpoint_id cannot be blank")
        requests = await self._pop_requests(parent_job, parent_job.root_turn_id)
        if not requests or not self.enabled:
            return ()

        outcomes: list[PostCheckpointValidationOutcome] = []
        for pending in requests:
            if not self._matches_job(pending, parent_job):
                outcomes.append(
                    self._failed_closed(parent_job, pending, "runtime_provenance_changed")
                )
                continue
            task = ValidationTask(
                objective=pending.intent.objective,
                deterministic_failures=pending.intent.deterministic_failures,
                budget=self._budget,
            )
            try:
                record = await self._validator.validate(
                    parent_job=parent_job,
                    checkpoint_id=normalized_checkpoint,
                    task=task,
                )
                if not isinstance(record, ValidationVerdictRecord):
                    raise TypeError("validator returned an invalid record")
            except asyncio.CancelledError:
                parent_job.publish_lifecycle(
                    "validation.dispatch.cancelled",
                    {
                        "request_id": pending.request_id,
                        "policy_id": pending.intent.policy_id,
                    },
                    checkpoint_id=normalized_checkpoint,
                )
                raise
            except Exception:
                outcomes.append(
                    self._failed_closed(
                        parent_job,
                        pending,
                        "validator_unavailable",
                        checkpoint_id=normalized_checkpoint,
                    )
                )
                continue
            outcomes.append(
                PostCheckpointValidationOutcome(
                    request_id=pending.request_id,
                    policy_id=pending.intent.policy_id,
                    status="completed",
                    record=record,
                )
            )
        return tuple(outcomes)

    async def _pop_requests(
        self,
        parent_job: GenerationJob,
        root_turn_id: str,
    ) -> tuple[_PendingValidation, ...]:
        async with self._lock:
            by_turn = self._pending.get(parent_job)
            if not by_turn:
                return ()
            requests = tuple(by_turn.pop(root_turn_id, ()))
            if not by_turn:
                self._pending.pop(parent_job, None)
            return requests

    @staticmethod
    def _matches_job(
        pending: _PendingValidation,
        parent_job: GenerationJob,
    ) -> bool:
        return (
            pending.session_id == parent_job.session_id
            and pending.root_turn_id == parent_job.root_turn_id
            and pending.turn_run_id == parent_job.turn_run_id
            and pending.workspace_instance_id == parent_job.workspace_instance_id
            and parent_job.invocation_source != "validator"
        )

    @staticmethod
    def _failed_closed(
        parent_job: GenerationJob,
        pending: _PendingValidation,
        reason: str,
        *,
        checkpoint_id: str | None = None,
    ) -> PostCheckpointValidationOutcome:
        parent_job.publish_lifecycle(
            "validation.dispatch.failed",
            {
                "request_id": pending.request_id,
                "policy_id": pending.intent.policy_id,
                "reason": reason,
            },
            checkpoint_id=checkpoint_id,
        )
        return PostCheckpointValidationOutcome(
            request_id=pending.request_id,
            policy_id=pending.intent.policy_id,
            status="failed_closed",
            record=None,
        )


__all__ = [
    "PostCheckpointValidationOutcome",
    "PostCheckpointValidationScheduler",
    "PostCheckpointValidator",
    "ServerValidationIntent",
]
