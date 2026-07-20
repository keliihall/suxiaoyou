"""Session processor — single LLM step execution.

SessionProcessor handles one LLM step:
  1. Stream from LLM with retry
  2. Accumulate text / reasoning / tool calls
  3. Execute tools (with permissions, doom-loop guard, timeout)
  4. Persist text parts + tool parts + step-finish part
  5. Return "continue" | "stop" | "compact"

The outer loop, setup, and post-loop work live in SessionPrompt (session/prompt.py).

Mirrors OpenCode's session/processor.ts.
"""

from __future__ import annotations

import asyncio
import copy
import datetime
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal, cast

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.agent import AgentRegistry
from app.agent.permission import (
    RejectedError,
    evaluate,
    serialize_permission_snapshot,
)
from app.i18n import Language, localize
from app.provider.registry import ProviderRegistry
from app.security.audit import AuditPersistenceError, record_security_event
from app.security.capabilities import (
    denied_invocation_capabilities,
    denied_tool_capabilities,
    describe_tool_source,
    primary_capability,
    tool_requires_durable_audit,
)
from app.schemas.agent import PermissionRule
from app.schemas.chat import PromptRequest
from app.schemas.message import StepFinishReason
from app.session.llm import stream_llm
from app.session.manager import (
    create_part,
    get_messages,
    update_part_data,
)
from app.session.middleware import ToolAction
from app.session.retry import (
    MAX_RETRIES,
    is_context_overflow,
    is_retryable,
    max_retries_for_error,
    retry_delay,
    sleep_with_abort,
)
from app.streaming.events import (
    AGENT_ERROR,
    DONE,
    INPUT_STARTED,
    MODEL_LOADING,
    GOAL_NEEDS_USER,
    PERMISSION_REQUEST,
    REASONING_DELTA,
    RETRY,
    STEP_FINISH,
    TEXT_DELTA,
    TOOL_ERROR,
    TOOL_RESULT,
    TOOL_START,
    SSEEvent,
)
from app.streaming.manager import GenerationJob
from app.tool.context import ToolContext
from app.tool.registry import ToolRegistry
from app.config import get_settings
from app.session.utils import (
    calculate_step_cost as _calculate_step_cost,
    compute_safe_max_tokens as _compute_safe_max_tokens,
    get_effective_context_window as _get_effective_context_window,
    llm_messages_have_image_content as _llm_messages_have_image_content,
    repair_tool_call_payload as _repair_tool_call_payload,
)
from app.utils.id import generate_ulid

if TYPE_CHECKING:
    from app.session.prompt import SessionPrompt

logger = logging.getLogger(__name__)


def _office_repair_admission_factory(prompt: SessionPrompt) -> object | None:
    """Bind repair inference to the same Goal execution and budget gate."""

    job = getattr(prompt, "job", None)
    session_factory = getattr(prompt, "session_factory", None)
    if job is None or session_factory is None:
        return None

    @asynccontextmanager
    async def admission(max_output_tokens: int):
        if job.goal_id is None:
            yield max_output_tokens
            return
        from app.office_validation.repair_agent import (
            OfficePrecommitRepairAgentError,
        )
        from app.session.goal_guard import (
            read_goal_budget_gate,
            read_goal_execution_gate,
        )

        async with job.execution_admission_lock:
            if not job.execution_admission_open:
                raise OfficePrecommitRepairAgentError(
                    "Office repair agent admission was rejected"
                )
            root_session_id = job.goal_session_id or job.session_id
            gate = await read_goal_execution_gate(
                session_factory,
                session_id=root_session_id,
                goal_id=job.goal_id,
                goal_run_id=job.goal_run_id,
            )
            if not gate.allowed:
                raise OfficePrecommitRepairAgentError(
                    "Office repair agent admission was rejected"
                )
            tokens_used, cost_used = job.goal_run_usage
            budget = await read_goal_budget_gate(
                session_factory,
                session_id=root_session_id,
                goal_id=job.goal_id,
                local_tokens_used=tokens_used,
                local_cost_microusd=cost_used,
                local_active_seconds=max(
                    0,
                    round(
                        time.monotonic()
                        - prompt._goal_active_started_monotonic
                        - (
                            job.goal_wait_seconds
                            - prompt._goal_wait_seconds_at_start
                        )
                    ),
                ),
                warning_ratio=_cfg().goal_budget_warning_ratio,
            )
            if not budget.allowed:
                raise OfficePrecommitRepairAgentError(
                    "Office repair agent budget is exhausted"
                )
            admitted = max_output_tokens
            if budget.token_remaining is not None:
                admitted = min(admitted, budget.token_remaining)
            if admitted < 1:
                raise OfficePrecommitRepairAgentError(
                    "Office repair agent budget is exhausted"
                )
            # The executor creates the stream and consumes its first chunk
            # inside this context, matching the main Provider admission race.
            yield admitted

    return admission


def _office_repair_execution_observer(prompt: SessionPrompt) -> object | None:
    """Persist path-free Repair usage and update the shared Goal ledger."""

    job = getattr(prompt, "job", None)
    session_factory = getattr(prompt, "session_factory", None)
    assistant_msg_id = getattr(prompt, "assistant_msg_id", None)
    if (
        job is None
        or session_factory is None
        or not isinstance(assistant_msg_id, str)
        or not assistant_msg_id
    ):
        return None
    lock = getattr(prompt, "_office_repair_accounting_lock", None)
    if not isinstance(lock, asyncio.Lock):
        lock = asyncio.Lock()
        setattr(prompt, "_office_repair_accounting_lock", lock)

    async def observe(receipt: object) -> None:
        from app.office_validation.repair_agent import (
            OfficeRepairExecutionReceipt,
        )

        if not isinstance(receipt, OfficeRepairExecutionReceipt):
            raise TypeError("Office repair accounting receipt is invalid")
        usage = {
            name: max(0, int(receipt.usage.get(name, 0) or 0))
            for name in (
                "input",
                "output",
                "reasoning",
                "cache_read",
                "cache_write",
                "total",
            )
        }
        tokens = sum(
            usage[name]
            for name in ("input", "output", "reasoning", "cache_read")
        )
        cost = _calculate_step_cost(usage, receipt.model_info)
        cost_microusd = max(0, round(cost * 1_000_000))
        async with lock:
            async with session_factory() as db:
                async with db.begin():
                    part = await create_part(
                        db,
                        message_id=assistant_msg_id,
                        session_id=job.session_id,
                        data={
                            "type": "office-repair-usage",
                            "goal_run_id": job.goal_run_id,
                            "execution_id": receipt.execution_id,
                            "provider_id": receipt.provider_id,
                            "model_id": receipt.model_id,
                            "outcome": receipt.outcome,
                            "tokens": usage,
                            "cost": cost,
                        },
                    )
                    if job.goal_id is not None:
                        if job.goal_run_id is None:
                            raise RuntimeError(
                                "Goal Office repair usage has no durable run identity"
                            )
                        from app.session.goal_manager import record_goal_run_usage

                        await record_goal_run_usage(
                            db,
                            goal_run_id=job.goal_run_id,
                            source_kind="office_repair",
                            source_key=f"office_repair:{part.id}",
                            tokens_used=tokens,
                            cost_used_microusd=cost_microusd,
                        )
            prompt.total_cost += cost
            for name in prompt.total_tokens_accumulated:
                prompt.total_tokens_accumulated[name] += usage.get(name, 0)
            if job.goal_id is not None:
                job.record_goal_usage(
                    tokens=tokens,
                    cost_microusd=cost_microusd,
                )
                prompt._goal_usage_recorded_tokens += tokens
                prompt._goal_usage_recorded_cost_microusd += cost_microusd

    return observe


def _office_precommit_repairer_for_prompt(
    prompt: SessionPrompt,
) -> object | None:
    """Bind the current exact provider/model to the private Office repair lane.

    This helper is deliberately dynamic: closing a source dependency or
    removing the authoritative coordinator revokes repair injection on the
    next tool call.  The cached instance is scoped to one SessionPrompt so
    concurrent Office calls in that session share the repairer's single-flight
    lock, while a provider/model switch creates a new binding.
    """

    from app.office_validation.precommit import (
        get_office_precommit_coordinator,
    )
    from app.release_readiness import v11_capability_released

    if (
        not v11_capability_released("office_authoring")
        or get_office_precommit_coordinator() is None
    ):
        setattr(prompt, "_office_repairer_binding", None)
        return None

    provider_id = getattr(getattr(prompt, "provider", None), "id", None)
    model_id = getattr(prompt, "model_id", None)
    capabilities = getattr(getattr(prompt, "model_info", None), "capabilities", None)
    if (
        not isinstance(provider_id, str)
        or not provider_id
        or not isinstance(model_id, str)
        or not model_id
        or not bool(getattr(capabilities, "json_output", False))
    ):
        setattr(prompt, "_office_repairer_binding", None)
        return None

    key = (id(prompt.provider_registry), provider_id, model_id)
    cached = getattr(prompt, "_office_repairer_binding", None)
    if (
        isinstance(cached, tuple)
        and len(cached) == 2
        and cached[0] == key
    ):
        return cached[1]

    try:
        from app.office_validation.repair_agent import (
            ProviderOfficePrecommitRepairer,
        )
        from app.office_validation.precommit_repair import (
            OfficePrecommitRepairError,
        )

        repairer = ProviderOfficePrecommitRepairer(
            provider_registry=prompt.provider_registry,
            provider_id=provider_id,
            model_id=model_id,
            admission_factory=_office_repair_admission_factory(prompt),
            observer=_office_repair_execution_observer(prompt),
        )
    except (OSError, TypeError, ValueError, OfficePrecommitRepairError):
        setattr(prompt, "_office_repairer_binding", None)
        return None
    setattr(prompt, "_office_repairer_binding", (key, repairer))
    return repairer


async def _audit_tool_event(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tool: Any,
    job: GenerationJob,
    call_id: str,
    decision: str,
    outcome: str,
    interactive: bool,
    extra_details: dict[str, Any] | None = None,
    required: bool = False,
) -> None:
    source_kind, source_id, capabilities = describe_tool_source(tool)
    details: dict[str, Any] = {
        "capabilities": ",".join(sorted(capabilities)),
        "interactive": interactive,
        "tool_id": tool.id,
    }
    connector_provenance = getattr(tool, "connector_provenance", None)
    if connector_provenance is not None:
        details["connector_provenance"] = str(connector_provenance)
    if extra_details:
        details.update(extra_details)
    await record_security_event(
        session_factory,
        source_kind=source_kind,
        source_id=source_id,
        invocation_source_kind=job.invocation_source,
        invocation_source_id=job.invocation_source_id,
        capability=primary_capability(tool),
        action="execute",
        decision=decision,
        outcome=outcome,
        session_id=job.session_id,
        call_id=call_id,
        details=details,
        required=required,
    )


async def _audit_provider_event(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    provider_id: str,
    job: GenerationJob,
    call_id: str,
    outcome: str,
    step: int,
    attempt: int,
) -> None:
    """Record inference lifecycle metadata without prompts, content, or credentials."""

    await record_security_event(
        session_factory,
        source_kind="provider",
        source_id=provider_id,
        invocation_source_kind=job.invocation_source,
        invocation_source_id=job.invocation_source_id,
        capability="model_inference",
        action="infer",
        decision="system",
        outcome=outcome,
        session_id=job.session_id,
        call_id=call_id,
        details={
            "step": step,
            "attempt": attempt,
            "invocation_source": job.invocation_source,
        },
        required=outcome == "started",
    )

# Loop detection: two-stage warn-then-stop (replaces old doom loop)
from app.session.loop_detection import (
    WEB_FETCH_CIRCUIT_OPEN_MSG,
    WEB_FETCH_CIRCUIT_OPEN_MSG_ZH,
    WEB_SEARCH_LIMIT_MSG,
    WEB_SEARCH_LIMIT_MSG_ZH,
    TOOL_FAILURE_CIRCUIT_OPEN_MSG,
    TOOL_FAILURE_CIRCUIT_OPEN_MSG_ZH,
    LoopCheckResult,
    loop_detector,
    web_fetch_circuit_scope,
)

# Tools that operate on file paths — used for two-dimensional permission check
_FILE_TOOLS = frozenset({"read", "write", "edit", "image_generate", "office"})

_PERMISSION_ARGUMENT_CHAR_LIMIT = 20_000
_SENSITIVE_ARG_KEY_RE = re.compile(
    r"(api[_-]?key|authorization|bearer|cookie|password|secret|token)",
    re.IGNORECASE,
)

# Agent limits — read from Settings (user-configurable via env vars).
# Accessed via _cfg() to avoid stale module-level reads.
def _cfg():
    return get_settings()


def _native_web_search_allowed(
    tool_registry: ToolRegistry,
    permissions: Any,
    *,
    quota_exhausted: bool,
    invocation_source: str = "desktop",
) -> bool:
    """Apply the same Security Center and permission gates to provider-native search."""

    return (
        not quota_exhausted
        and not denied_invocation_capabilities(
            invocation_source,
            ("network", "remote_data_read"),
        )
        and tool_registry.is_enabled("web_search")
        # Provider-native search cannot pause for an interactive permission
        # response.  Only an explicit allow may use it; ``ask`` stays on the
        # ordinary web_search tool path where the existing confirmation flow
        # can run.
        and evaluate("web_search", "*", permissions) == "allow"
    )


def _normalize_step_finish_reason(reason: str | None) -> StepFinishReason:
    """Normalize provider/internal finish reasons to the frontend contract."""
    if reason == "tool_calls":
        return "tool_use"
    if reason in {"stop", "tool_use", "length", "error"}:
        return cast(StepFinishReason, reason)
    logger.warning("Unexpected step finish reason %r; normalizing to 'error'", reason)
    return "error"


# --- Daily web_search usage tracking (single-user desktop app) ---

class SearchQuotaTracker:
    """Tracks daily web_search usage with automatic UTC-day reset.

    Encapsulates mutable quota state behind a lock for thread safety.
    """

    def __init__(self) -> None:
        self._date: str = ""
        self._count: int = 0
        self._credits_mode: bool = False  # Sticky: True once hosted proxy confirms paid search.
        self._lock = asyncio.Lock()

    def _reset_if_new_day(self) -> None:
        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        if self._date != today:
            self._date = today
            self._count = 0

    async def get_quota(self) -> tuple[int, bool]:
        """Return (count_today, is_credits_mode), resetting if UTC day changed."""
        async with self._lock:
            self._reset_if_new_day()
            return self._count, self._credits_mode

    async def increment(self, *, charged: bool = False) -> None:
        async with self._lock:
            self._reset_if_new_day()
            self._count += 1
            if charged:
                self._credits_mode = True


_search_quota = SearchQuotaTracker()


async def _track_session_file(
    session_factory: Any,
    session_id: str,
    file_path: str,
    tool_id: str,
) -> None:
    """Persist a file record for the workspace panel (deduplicated by path)."""
    import os
    from sqlalchemy import select
    from app.models.session_file import SessionFile
    from app.utils.id import generate_ulid

    file_name = os.path.basename(file_path)
    try:
        async with session_factory() as db:
            async with db.begin():
                # Deduplicate: skip if this exact path is already tracked
                existing = await db.execute(
                    select(SessionFile.id).where(
                        SessionFile.session_id == session_id,
                        SessionFile.file_path == file_path,
                    ).limit(1)
                )
                if existing.scalar_one_or_none() is not None:
                    return
                db.add(SessionFile(
                    id=generate_ulid(),
                    session_id=session_id,
                    file_path=file_path,
                    file_name=file_name,
                    tool_id=tool_id,
                    file_type="generated",
                ))
    except Exception:
        logger.debug("Failed to track session file: %s", file_path, exc_info=True)


_PRESENTABLE_DELIVERABLE_EXTS = {
    ".aac",
    ".avi",
    ".csv",
    ".docx",
    ".flac",
    ".gif",
    ".html",
    ".jpeg",
    ".jpg",
    ".json",
    ".m4a",
    ".md",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".ogg",
    ".opus",
    ".pdf",
    ".png",
    ".ppt",
    ".pptx",
    ".svg",
    ".txt",
    ".wav",
    ".webm",
    ".webp",
    ".xls",
    ".xlsx",
    ".zip",
}

_NON_PRESENTABLE_OUTPUT_HINTS = {
    "helper",
    "scratch",
    "temp",
    "tmp",
}

_NON_PRESENTABLE_PATH_SEGMENTS = {
    ".git",
    ".suxiaoyou",
    ".venv",
    "__pycache__",
    "node_modules",
}

_PRIMARY_OUTPUT_FILE_TOOLS = {
    "edit",
    "image_generate",
    "office",
    "write",
}


def _artifact_delivery_paths(
    tool_id: str,
    metadata: dict[str, Any] | None,
) -> list[str]:
    """Extract generated-file paths from the shared tool metadata contract.

    ``written_files`` is intentionally tool-agnostic so shell runners, code
    execution, plugins, and future generators receive the same tracking and
    deterministic file-card behavior.  ``artifact_files`` is the forward
    contract for tools that want to declare delivery without also claiming a
    workspace mutation.  Existing single-output tools retain ``file_path``.
    """
    if not metadata:
        return []

    files: list[str] = []
    for key in ("artifact_files", "written_files"):
        values = metadata.get(key)
        if not isinstance(values, (list, tuple)):
            continue
        for value in values:
            if isinstance(value, str) and value:
                files.append(value)
            elif isinstance(value, dict):
                path = value.get("path") or value.get("file_path")
                if isinstance(path, str) and path:
                    files.append(path)

    primary = metadata.get("file_path")
    if (
        isinstance(primary, str)
        and primary
        and (
            tool_id in _PRIMARY_OUTPUT_FILE_TOOLS
            or metadata.get("artifact_delivery") is True
        )
    ):
        files.append(primary)

    return list(dict.fromkeys(files))


def _presentation_reminder(
    tool_id: str,
    metadata: dict[str, Any] | None,
    *,
    language: Language | str = "en",
) -> str:
    """Return an LLM-only reminder when a tool produced likely deliverables."""
    # image_generate returns a persisted attachment and the frontend renders
    # its file card directly from this tool result. Asking the model to call
    # present_file would add a redundant, non-deterministic second tool call.
    if tool_id == "image_generate" or not metadata:
        return ""

    candidates: list[str] = []
    for file_path in _artifact_delivery_paths(tool_id, metadata):
        path = Path(file_path)
        suffix = path.suffix.lower()
        name = path.name.lower()
        if suffix not in _PRESENTABLE_DELIVERABLE_EXTS:
            continue
        if any(part.lower() in _NON_PRESENTABLE_PATH_SEGMENTS for part in path.parts):
            continue
        if any(hint in name for hint in _NON_PRESENTABLE_OUTPUT_HINTS):
            continue
        candidates.append(file_path)

    if not candidates:
        return ""

    joined = ", ".join(candidates[:5])
    return "\n\n" + localize(
        language,
        (
            "<reminder>检测到可能的最终交付文件："
            f"{joined}。若这些是用户要求的最终文件，请在最终答复前逐个调用 "
            "present_file 展示面向用户的交付物。除非用户要求打开或分享，否则请将"
            "支撑数据文件单独说明。不要展示临时脚本、草稿文件、日志、辅助文件或"
            "中间产物。</reminder>"
        ),
        (
            "<reminder>Potential final deliverable file(s) were created: "
            f"{joined}. If these are final files the user asked for, call "
            "present_file for each user-facing deliverable before your final "
            "response. Mention supporting data files separately unless the user "
            "asked to open or share them. Do not present temporary scripts, "
            "scratch files, logs, helper files, or intermediate outputs.</reminder>"
        ),
    )


# ---------------------------------------------------------------------------
# run_generation — thin shim (preserves existing call sites in api/chat.py and task.py)
# ---------------------------------------------------------------------------


async def run_generation(
    job: GenerationJob,
    request: PromptRequest,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    provider_registry: ProviderRegistry,
    agent_registry: AgentRegistry,
    tool_registry: ToolRegistry,
    index_manager: Any | None = None,
    skip_user_message: bool = False,
    idempotency_record_id: str | None = None,
) -> None:
    """Run the full agent generation loop.

    Delegates to SessionPrompt which owns setup + the while-loop,
    and creates a SessionProcessor per step for LLM streaming + tool execution.
    """
    from app.session.prompt import SessionPrompt

    from app.session.input_queue import (
        block_unstarted_inputs_for_stream,
        claim_next_generation_input,
        finish_session_input,
    )

    # Keep directly invoked/headless generations aligned with API-created
    # jobs. A later queued input may update this field through its request.
    job.language = request.language
    current_request = request
    current_input_id: str | None = None
    current_skip_user_message = skip_user_message
    prompt: SessionPrompt | None = None
    chain_total_cost = 0.0
    record_status = "accepted"
    record_error: str | None = None
    saw_generation_error = False

    async def close_and_block_remaining_inputs(error_message: str) -> None:
        async with job.session_input_lock:
            job.close_session_input_admission()
            try:
                async with session_factory() as db:
                    async with db.begin():
                        await block_unstarted_inputs_for_stream(
                            db,
                            session_id=job.session_id,
                            stream_id=job.stream_id,
                            error_message=error_message,
                        )
            except Exception:
                logger.warning(
                    "Failed to block remaining inputs for stream %s",
                    job.stream_id,
                    exc_info=True,
                )

    try:
        if idempotency_record_id is not None:
            from app.session.idempotency import mark_idempotency_status

            async with session_factory() as db:
                async with db.begin():
                    await mark_idempotency_status(
                        db,
                        idempotency_record_id,
                        status="running",
                    )
            record_status = "running"

        while True:
            job.language = current_request.language
            # The replay list is capped and trims from the front.  A list index
            # captured here becomes invalid once a long task crosses 5,000
            # events, so use the monotonic event id as the generation boundary.
            event_start_id = job._event_counter
            prompt = SessionPrompt(
                job,
                current_request,
                session_factory=session_factory,
                provider_registry=provider_registry,
                agent_registry=agent_registry,
                tool_registry=tool_registry,
                index_manager=index_manager,
                skip_user_message=current_skip_user_message,
            )
            await prompt.run(publish_done=False)
            chain_total_cost += prompt.total_cost

            generation_error = next(
                (
                    event
                    for event in job.events
                    if event.event == AGENT_ERROR
                    and event.id is not None
                    and event.id > event_start_id
                ),
                None,
            )
            if generation_error is not None:
                saw_generation_error = True
            if current_input_id is not None:
                async with session_factory() as db:
                    async with db.begin():
                        if job.abort_event.is_set():
                            await finish_session_input(
                                db,
                                current_input_id,
                                status="blocked",
                                applied_stream_id=job.stream_id,
                                error_message="Task stopped before this queued input completed",
                            )
                        elif generation_error is not None:
                            await finish_session_input(
                                db,
                                current_input_id,
                                status="failed",
                                applied_stream_id=job.stream_id,
                                error_message=str(
                                    generation_error.data.get("error_message")
                                    or "Queued input failed"
                                ),
                            )
                        else:
                            await finish_session_input(
                                db,
                                current_input_id,
                                status="consumed",
                                applied_stream_id=job.stream_id,
                            )

            if job.abort_event.is_set() or generation_error is not None:
                await close_and_block_remaining_inputs(
                    "Task was stopped before this queued input started"
                    if job.abort_event.is_set()
                    else "The owning task failed before this queued input started"
                )
                break

            # Admission and the final empty check must be atomic with respect
            # to POST /chat/inputs, otherwise a just-accepted follow-up can be
            # stranded as this stream exits.
            async with job.session_input_lock:
                if job.abort_event.is_set():
                    next_input = None
                else:
                    async with session_factory() as db:
                        async with db.begin():
                            next_input = await claim_next_generation_input(
                                db,
                                job.session_id,
                                target_stream_id=job.stream_id,
                            )
                if next_input is None:
                    job.close_session_input_admission()
            if next_input is None:
                break

            current_input_id = next_input.id
            current_skip_user_message = False
            # The SSE transport remains open for queued input chaining, but a
            # queued prompt is a distinct user root turn with its own checkpoint.
            job.begin_root_turn(next_input.id)
            current_request = PromptRequest(
                session_id=job.session_id,
                text=next_input.text,
                model=next_input.model_id,
                provider_id=next_input.provider_id,
                agent=next_input.agent,
                attachments=next_input.attachments or [],
                permission_presets=next_input.permission_presets,
                permission_rules=next_input.permission_rules,
                reasoning=next_input.reasoning,
                workspace=next_input.workspace,
                language=next_input.language,
            )
            job.publish(
                SSEEvent(
                    INPUT_STARTED,
                    {
                        "input_id": next_input.id,
                        "mode": next_input.mode,
                        "position": next_input.position,
                        "session_id": job.session_id,
                    },
                )
            )

        if prompt is not None:
            prompt.total_cost = chain_total_cost
            prompt.publish_done()
        if job.abort_event.is_set():
            record_status = "stopped"
        elif saw_generation_error:
            record_status = "failed"
            record_error = "Generation reported an agent error"
        else:
            record_status = "completed"
    except IntegrityError:
        # Session was deleted while generation was in-flight — notify frontend
        # so it can exit the generating state, then stop.
        logger.info(
            "Session %s deleted during generation, stopping stream %s",
            job.session_id,
            job.stream_id,
        )
        job.publish(SSEEvent(DONE, {
            "session_id": job.session_id,
            "finish_reason": "aborted",
        }))
        record_status = "interrupted"
        record_error = "Session was deleted while generation was running"
    except asyncio.CancelledError:
        record_status = "interrupted"
        record_error = "Generation was cancelled before completion"
        if current_input_id is not None:
            try:
                async with session_factory() as db:
                    async with db.begin():
                        await finish_session_input(
                            db,
                            current_input_id,
                            status="blocked",
                            applied_stream_id=job.stream_id,
                            error_message=(
                                "Task was cancelled before this queued input completed"
                            ),
                        )
            except Exception:
                logger.warning(
                    "Failed to block cancelled queued input %s",
                    current_input_id,
                    exc_info=True,
                )
        await close_and_block_remaining_inputs(
            "Task was cancelled before this queued input started"
        )
        raise
    except Exception:
        logger.exception("Generation error for stream %s", job.stream_id)
        if current_input_id is not None:
            try:
                async with session_factory() as db:
                    async with db.begin():
                        await finish_session_input(
                            db,
                            current_input_id,
                            status="failed",
                            applied_stream_id=job.stream_id,
                            error_message="Queued input failed with an internal error",
                        )
            except Exception:
                logger.warning(
                    "Failed to persist queued input failure %s",
                    current_input_id,
                    exc_info=True,
                )
        await close_and_block_remaining_inputs(
            "The owning task failed before this queued input started"
        )
        job.publish(SSEEvent(AGENT_ERROR, {"error_message": "An internal error occurred. Please try again."}))
        record_status = "failed"
        record_error = "Generation failed with an internal error"
    finally:
        async with job.session_input_lock:
            job.close_session_input_admission()
        if idempotency_record_id is not None:
            try:
                from app.session.idempotency import mark_idempotency_status

                async with session_factory() as db:
                    async with db.begin():
                        await mark_idempotency_status(
                            db,
                            idempotency_record_id,
                            status=record_status,
                            error_message=record_error,
                        )
            except Exception:
                logger.warning(
                    "Failed to finalize idempotency record %s",
                    idempotency_record_id,
                    exc_info=True,
                )
        job.complete()


# ---------------------------------------------------------------------------
# SessionProcessor — handles a single LLM step
# ---------------------------------------------------------------------------


class SessionProcessor:
    """Handles one LLM step: stream → parse → execute tools.

    Created fresh per loop iteration by SessionPrompt._loop().
    Reads mutable state from session_prompt and writes back on agent switch.

    Mirrors OpenCode's SessionProcessor / processor.ts.
    """

    def __init__(
        self,
        session_prompt: SessionPrompt,
        llm_messages: list[dict[str, Any]],
        assistant_msg_id: str,
        middleware_ctx: Any | None = None,
    ) -> None:
        self._sp = session_prompt
        self._llm_messages = llm_messages
        self._assistant_msg_id = assistant_msg_id
        self._mw_ctx = middleware_ctx  # MiddlewareContext from prompt.py

        # Step-local results exposed for SessionPrompt to accumulate
        self.usage_data: dict[str, Any] = {}
        self.finish_reason: str = "stop"
        self.step_cost: float = 0.0
        self.has_text: bool = False  # True if this step produced non-empty text

    async def process(self) -> Literal["continue", "stop", "compact"]:
        """Execute one LLM step and return the loop continuation signal.

        Returns:
          "continue" — tool calls were made; loop again so LLM sees results
          "stop"     — no tool calls; model finished this turn
          "compact"  — context overflow detected; run compaction then continue
        """
        self._init_step_state()
        await self._persist_step_start()

        # Phase 1: stream from LLM (with retry); may early-return "stop".
        early = await self._stream_llm_with_retry()
        if early is not None:
            return early

        # Phase 2: non-retryable / retries-exhausted stream error → "compact" or "stop".
        if self._stream_error is not None:
            return await self._handle_stream_error()

        # Phase 3: empty output after retries → continue the loop.
        if await self._handle_empty_output_after_retries():
            return "continue"

        # Phase 4: persist text + reasoning parts.
        await self._persist_text_and_reasoning()

        # Phase 5: account Provider usage before dispatching tools. A Task tool
        # can start a child Agent immediately; recording here makes the child's
        # first Provider gate observe the parent step's spend.
        self._compute_step_cost()
        await self._sp._record_goal_step_usage_before_tools(
            self.usage_data,
            self.step_cost,
        )

        # Phase 6: dispatch concurrent tool calls.
        early = await self._dispatch_tool_calls()
        if early is not None:
            return early

        # If this step produced tool calls, the agent loop is not actually
        # finished yet, even if the provider reported a generic "stop".
        # Surface that as "tool_use" so the frontend keeps streaming UI alive
        # until the follow-up step completes.
        if self._has_tool_calls and self.finish_reason != "tool_use":
            self.finish_reason = "tool_use"

        # Phase 7: step finish (SSE + DB step-finish part).
        await self._persist_step_finish()

        # Phase 8: reactive compaction on usage-based overflow.
        if self._check_context_overflow():
            return "compact"

        # Phase 9: middleware on_step_complete.
        if self._mw_ctx is not None:
            await self._sp.middleware_chain.run_on_step_complete(self._mw_ctx)

        # Phase 10: determine continuation.
        if not self._has_tool_calls:
            return "stop"
        return "continue"

    # ------------------------------------------------------------------
    # process() phases — initialization
    # ------------------------------------------------------------------

    def _init_step_state(self) -> None:
        """Initialize per-step accumulator state shared across phases."""
        from app.session.tool_executor import StreamingToolExecutor

        self._accumulated_text: str = ""
        self._accumulated_reasoning: str = ""
        self._reasoning_persisted_chars: int = 0
        self._tool_calls_in_step: list[dict[str, Any]] = []
        self._has_tool_calls: bool = False
        self._native_search_ids: set[str] = set()
        self._native_search_completed: set[str] = set()
        self._native_search_count: int = 0
        self._ws_part_ids: dict[str, str] = {}  # web_search call_id → part_id
        self._stream_error: Exception | None = None

        # Streaming tool executor: starts concurrent-safe tools during streaming
        self._streaming_executor = StreamingToolExecutor(
            self._sp.job.abort_event,
            task_tracker=self._sp.job.track_tool_task,
        )
        self._exec_metadata: dict[int, dict[str, Any]] = {}
        self._exec_index: int = 0
        self._exec_blocked: bool = False  # Set True if loop detection blocks
        middleware_chain = getattr(self._sp, "middleware_chain", None)
        self._defer_after_llm_response = bool(
            self._mw_ctx is not None
            and middleware_chain is not None
            and getattr(
                middleware_chain,
                "has_after_llm_response_hooks",
                False,
            )
        )

    async def _persist_step_start(self) -> None:
        """Persist the StepStart part (mirrors OpenCode's StepStartPart)."""
        sp = self._sp
        async with sp.session_factory() as db:
            async with db.begin():
                await create_part(
                    db,
                    message_id=self._assistant_msg_id,
                    session_id=sp.job.session_id,
                    data={"type": "step-start", "step": sp.step},
                )

    # ------------------------------------------------------------------
    # process() phases — LLM streaming with retry
    # ------------------------------------------------------------------

    async def _stream_llm_with_retry(self) -> Literal["stop"] | None:
        """Stream from the LLM with retry. Mutates self._accumulated_*, self._stream_error.

        Returns "stop" early only for fatal in-stream conditions (non-vision model
        received images, or the stream chunk reported an explicit error).
        """
        sp = self._sp
        job = sp.job

        for attempt in range(MAX_RETRIES + 1):
            if job.abort_event.is_set():
                break

            provider_call_id = f"{self._assistant_msg_id}:{sp.step}:{attempt + 1}"
            provider_started = False
            provider_finished = False

            try:
                provider_denied = denied_invocation_capabilities(
                    job.invocation_source,
                    ("model_inference",),
                )
                if provider_denied:
                    await record_security_event(
                        sp.session_factory,
                        source_kind="provider",
                        source_id=sp.provider.id,
                        invocation_source_kind=job.invocation_source,
                        invocation_source_id=job.invocation_source_id,
                        capability="model_inference",
                        action="infer",
                        decision="deny",
                        outcome="blocked",
                        session_id=job.session_id,
                        call_id=provider_call_id,
                        details={"invocation_source": job.invocation_source},
                    )
                    raise PermissionError(
                        f"Invocation source {job.invocation_source!r} is not "
                        "allowed to use model inference"
                    )

                stream_args = await self._build_stream_args()
                if stream_args is None:
                    return "stop"
                (
                    reasoning_extra,
                    safe_max_tokens,
                    exclude_tools,
                    native_web_search_enabled,
                ) = stream_args

                logger.info(
                    "Starting LLM stream for model=%s, messages=%d, max_tokens=%d",
                    sp.model_id,
                    len(self._llm_messages),
                    safe_max_tokens,
                )

                # Notify frontend that the model may need loading (Ollama cold start)
                if sp.provider.id == "ollama":
                    job.publish(SSEEvent(MODEL_LOADING, {"model": sp.model_id, "status": "loading"}))

                blocked = await self._check_vision_blocked()
                if blocked is not None:
                    return blocked

                llm_stream = stream_llm(
                    sp.provider,
                    sp.model_id,
                    self._llm_messages,
                    system_prompt=sp.system_prompt,
                    agent=sp.agent,
                    tool_registry=sp.tool_registry,
                    extra_body=reasoning_extra,
                    max_tokens=safe_max_tokens,
                    exclude_tools=exclude_tools,
                    discovered_tools=sp.discovered_tools,
                    response_format=sp.request.format,
                    native_web_search_enabled=native_web_search_enabled,
                )

                first_chunk = None
                async with job.execution_admission_lock:
                    if not job.execution_admission_open:
                        self.finish_reason = "paused" if job.goal_id else "stop"
                        return "stop"
                    # The control plane may change while message/tool specs are
                    # prepared. Re-check and start the Provider iterator under
                    # the same admission lock used by Goal pause/edit/archive.
                    # Pause therefore linearizes either before this call (deny)
                    # or after the request has actually begun.
                    if not await self._goal_provider_admission_allowed():
                        return "stop"
                    await _audit_provider_event(
                        sp.session_factory,
                        provider_id=sp.provider.id,
                        job=job,
                        call_id=provider_call_id,
                        outcome="started",
                        step=sp.step,
                        attempt=attempt + 1,
                    )
                    provider_started = True
                    try:
                        first_chunk = await anext(llm_stream)
                    except StopAsyncIteration:
                        first_chunk = None

                async def admitted_chunks():
                    if first_chunk is not None:
                        yield first_chunk
                    async for remaining_chunk in llm_stream:
                        yield remaining_chunk

                async for chunk in admitted_chunks():
                    if job.abort_event.is_set():
                        break

                    logger.debug("LLM chunk: type=%s", chunk.type)
                    match chunk.type:
                        case "text-delta":
                            text = chunk.data.get("text", "")
                            self._accumulated_text += text
                            if not self._defer_after_llm_response:
                                job.publish(SSEEvent(TEXT_DELTA, {
                                    "session_id": job.session_id,
                                    "message_id": self._assistant_msg_id,
                                    "text": text,
                                }))

                        case "reasoning-delta":
                            text = chunk.data.get("text", "")
                            self._accumulated_reasoning += text
                            job.publish(SSEEvent(REASONING_DELTA, {"text": text}))

                        case "tool-call":
                            self._has_tool_calls = True
                            self._tool_calls_in_step.append(chunk.data)
                            if (
                                not self._defer_after_llm_response
                                and not self._exec_blocked
                            ):
                                await self._handle_tool_call_chunk(chunk)

                        case "web-search-start":
                            await self._handle_web_search_start_chunk(chunk)

                        case "web-search-result":
                            await self._handle_web_search_result_chunk(chunk)

                        case "usage":
                            self.usage_data = chunk.data

                        case "finish":
                            self.finish_reason = _normalize_step_finish_reason(
                                chunk.data.get("reason", "stop")
                            )

                        case "error":
                            await _audit_provider_event(
                                sp.session_factory,
                                provider_id=sp.provider.id,
                                job=job,
                                call_id=provider_call_id,
                                outcome="error",
                                step=sp.step,
                                attempt=attempt + 1,
                            )
                            provider_finished = True
                            return await self._handle_stream_error_chunk(chunk)

                await _audit_provider_event(
                    sp.session_factory,
                    provider_id=sp.provider.id,
                    job=job,
                    call_id=provider_call_id,
                    outcome="cancelled" if job.abort_event.is_set() else "success",
                    step=sp.step,
                    attempt=attempt + 1,
                )
                provider_finished = True

                self._stream_error = None
                if not job.abort_event.is_set():
                    await self._apply_after_llm_response_middleware()
                logger.info(
                    "LLM stream completed: text=%d chars, reasoning=%d chars, "
                    "tool_calls=%d, finish=%s",
                    len(self._accumulated_text),
                    len(self._accumulated_reasoning),
                    len(self._tool_calls_in_step),
                    self.finish_reason,
                )

                # --- Empty response guard: retry if LLM produced nothing ---
                if (
                    not self._accumulated_text.strip()
                    and not self._has_tool_calls
                    and not self._accumulated_reasoning
                    and not job.abort_event.is_set()
                    and attempt < 2
                ):
                    logger.warning(
                        "Empty LLM response (attempt %d/%d), retrying",
                        attempt + 1,
                        MAX_RETRIES + 1,
                    )
                    self._reset_stream_accumulators()
                    continue

                break

            except Exception as e:
                if provider_started and not provider_finished:
                    await _audit_provider_event(
                        sp.session_factory,
                        provider_id=sp.provider.id,
                        job=job,
                        call_id=provider_call_id,
                        outcome="error",
                        step=sp.step,
                        attempt=attempt + 1,
                    )
                self._stream_error = e
                retry_reason = is_retryable(e)
                effective_max = max_retries_for_error(e)

                if retry_reason and attempt < effective_max:
                    delay = retry_delay(attempt, e)
                    logger.warning(
                        "LLM stream error (attempt %d/%d, %s), retrying in %.1fs: %s",
                        attempt + 1,
                        effective_max,
                        retry_reason,
                        delay,
                        e,
                    )
                    job.publish(SSEEvent(RETRY, {
                        "attempt": attempt + 1,
                        "max_retries": MAX_RETRIES,
                        "delay": delay,
                        "reason": retry_reason,
                        "message": str(e),
                    }))
                    self._reset_stream_accumulators()
                    aborted = await sleep_with_abort(delay, job.abort_event)
                    if aborted:
                        break
                    continue
                else:
                    break

        return None

    async def _apply_after_llm_response_middleware(self) -> None:
        """Apply the full-response middleware without corrupting live streams.

        Tool calls normally begin while the provider is still streaming.  A
        middleware that overrides ``after_llm_response`` may rewrite or remove
        those calls, so its presence is detected before streaming starts and
        both visible text and tool dispatch are deferred until this method.
        Chains containing only the base no-op keep the existing low-latency
        streaming path; any attempted late mutation on that path fails closed.
        """

        if self._mw_ctx is None:
            return
        middleware_chain = getattr(self._sp, "middleware_chain", None)
        if middleware_chain is None:
            return

        original_text = self._accumulated_text
        original_tool_calls = copy.deepcopy(self._tool_calls_in_step)
        try:
            transformed_text, transformed_tool_calls = (
                await middleware_chain.run_after_llm_response(
                    original_text,
                    copy.deepcopy(original_tool_calls),
                    self._mw_ctx,
                )
            )
            if not isinstance(transformed_text, str):
                raise TypeError(
                    "after_llm_response middleware must return text as a string"
                )
            if not isinstance(transformed_tool_calls, list) or any(
                not isinstance(tool_call, dict)
                for tool_call in transformed_tool_calls
            ):
                raise TypeError(
                    "after_llm_response middleware must return tool_calls as a list of objects"
                )
        except Exception:
            if self._defer_after_llm_response:
                # Deferred provider output has never been made visible. Discard
                # it atomically so the generic stream-error path cannot persist
                # unapproved raw text or later dispatch unapproved tool calls.
                self._accumulated_text = ""
                self._tool_calls_in_step = []
                self._has_tool_calls = False
            raise

        if not self._defer_after_llm_response and (
            transformed_text != original_text
            or transformed_tool_calls != original_tool_calls
        ):
            # Text may already be visible and concurrency-safe tools may already
            # be running. Silently accepting a late rewrite would make the UI,
            # persisted history, and actual side effects disagree.
            raise RuntimeError(
                "after_llm_response attempted to mutate an already-streamed response"
            )

        self._accumulated_text = transformed_text
        self._tool_calls_in_step = copy.deepcopy(transformed_tool_calls)
        self._has_tool_calls = bool(self._tool_calls_in_step)

        if not self._defer_after_llm_response:
            return

        job = self._sp.job
        if transformed_text:
            job.publish(SSEEvent(TEXT_DELTA, {
                "session_id": job.session_id,
                "message_id": self._assistant_msg_id,
                "text": transformed_text,
            }))

        for tool_call in self._tool_calls_in_step:
            if job.abort_event.is_set() or self._exec_blocked:
                break
            await self._handle_tool_call_chunk(
                SimpleNamespace(data=copy.deepcopy(tool_call))
            )

    async def _build_stream_args(
        self,
    ) -> tuple[Any, int, set[str] | None, bool] | None:
        """Compute provider and native-search gates for one stream attempt."""
        sp = self._sp

        reasoning_extra = None
        if sp.request.reasoning is False:
            reasoning_extra = {"reasoning": {"enabled": False}}

        safe_max_tokens = _compute_safe_max_tokens(
            self._llm_messages,
            model_max_context=(
                _get_effective_context_window(sp.model_info) if sp.model_info else 8192
            ),
            model_max_output=(
                sp.model_info.capabilities.max_output if sp.model_info else None
            ),
        )
        server_output_ceiling = getattr(
            sp.request,
            "_max_output_tokens_ceiling",
            None,
        )
        if server_output_ceiling is not None:
            # This value lives in a Pydantic PrivateAttr and is assigned only
            # by a server-owned runtime. It intentionally may be lower than
            # the interactive request's normal minimum output allocation.
            # Subtract already-accounted steps so a tool loop cannot reset the
            # ceiling on every Provider request within the same specialist run.
            already_used = sum(
                max(0, int(value or 0))
                for value in sp.total_tokens_accumulated.values()
            )
            safe_max_tokens = max(
                1,
                min(
                    safe_max_tokens,
                    int(server_output_ceiling) - already_used,
                ),
            )
        if sp.job.goal_id is not None:
            from app.session.goal_guard import (
                read_goal_budget_gate,
                read_goal_execution_gate,
            )

            root_session_id = sp.job.goal_session_id or sp.job.session_id
            gate = await read_goal_execution_gate(
                sp.session_factory,
                session_id=root_session_id,
                goal_id=sp.job.goal_id,
                goal_run_id=sp.job.goal_run_id,
            )
            if not gate.allowed:
                self.finish_reason = self._goal_gate_finish_reason(
                    gate.status,
                    gate.run_state,
                )
                return None

            counted_tokens, counted_cost = sp.job.goal_run_usage
            budget = await read_goal_budget_gate(
                sp.session_factory,
                session_id=root_session_id,
                goal_id=sp.job.goal_id,
                local_tokens_used=counted_tokens,
                local_cost_microusd=counted_cost,
                local_active_seconds=max(
                    0,
                    round(
                        time.monotonic()
                        - sp._goal_active_started_monotonic
                        - (
                            sp.job.goal_wait_seconds
                            - sp._goal_wait_seconds_at_start
                        )
                    ),
                ),
                warning_ratio=_cfg().goal_budget_warning_ratio,
            )
            if not budget.allowed:
                self.finish_reason = "budget_limited"
                return None
            # Provider input accounting is only known after the call.  Clamp
            # the controllable output portion to the remaining hard budget;
            # at most this already-admitted provider step can overshoot due to
            # input-token estimation differences.
            if budget.token_remaining is not None:
                safe_max_tokens = max(
                    1,
                    min(safe_max_tokens, budget.token_remaining),
                )

        exclude_tools: set[str] | None = None
        all_tools = getattr(sp.tool_registry, "all_tools", None)
        if callable(all_tools):
            source_denied = {
                tool.id
                for tool in all_tools()
                if denied_tool_capabilities(sp.job.invocation_source, tool)
            }
            if source_denied:
                exclude_tools = source_denied
        response_scope = web_fetch_circuit_scope(
            sp.job.session_id,
            sp.job.stream_id,
        )
        failure_blocked_tools = loop_detector.blocked_tools(response_scope)
        if failure_blocked_tools:
            exclude_tools = exclude_tools or set()
            exclude_tools.update(failure_blocked_tools)
        sq_count, sq_credits = await _search_quota.get_quota()
        quota_exhausted = not sq_credits and sq_count >= get_settings().daily_search_limit
        if quota_exhausted:
            exclude_tools = exclude_tools or set()
            exclude_tools.add("web_search")

        native_web_search_enabled = _native_web_search_allowed(
            sp.tool_registry,
            sp.merged_permissions,
            quota_exhausted=quota_exhausted,
            invocation_source=sp.job.invocation_source,
        )
        web_search_permission = evaluate("web_search", "*", sp.merged_permissions)

        # Provider-native search begins inside the model request, before an
        # individual query has a tool-admission boundary. While Hooks are
        # active, keep web_search on the ordinary tool path so PreToolUse runs
        # after permission and before the first network side effect.
        hooks_active = getattr(sp, "hooks_runtime_active", None)
        if callable(hooks_active) and hooks_active():
            native_web_search_enabled = False

        # An explicit allow uses provider-native search.  An explicit deny is
        # hidden entirely, while ``ask`` deliberately keeps the custom tool so
        # the normal interactive confirmation path can run.
        if sp.provider.id == "openai-subscription" and (
            native_web_search_enabled or web_search_permission == "deny"
        ):
            exclude_tools = exclude_tools or set()
            exclude_tools.add("web_search")

        return reasoning_extra, safe_max_tokens, exclude_tools, native_web_search_enabled

    @staticmethod
    def _goal_gate_finish_reason(status: str, run_state: str) -> str:
        if status != "active":
            return status
        if run_state == "pausing":
            return "paused"
        if run_state == "interrupted":
            return "interrupted"
        return "blocked"

    async def _goal_provider_admission_allowed(self) -> bool:
        """Fail closed at the last await boundary before Provider inference."""

        sp = self._sp
        job = sp.job
        if job.goal_id is None:
            return True

        if not job.execution_admission_open:
            self.finish_reason = "paused"
            return False

        from app.session.goal_guard import (
            read_goal_budget_gate,
            read_goal_execution_gate,
        )

        root_session_id = job.goal_session_id or job.session_id
        gate = await read_goal_execution_gate(
            sp.session_factory,
            session_id=root_session_id,
            goal_id=job.goal_id,
            goal_run_id=job.goal_run_id,
        )
        if not gate.allowed:
            self.finish_reason = self._goal_gate_finish_reason(
                gate.status,
                gate.run_state,
            )
            return False

        tokens_used, cost_used = job.goal_run_usage
        budget = await read_goal_budget_gate(
            sp.session_factory,
            session_id=root_session_id,
            goal_id=job.goal_id,
            local_tokens_used=tokens_used,
            local_cost_microusd=cost_used,
            local_active_seconds=max(
                0,
                round(
                    time.monotonic()
                    - sp._goal_active_started_monotonic
                    - (job.goal_wait_seconds - sp._goal_wait_seconds_at_start)
                ),
            ),
            warning_ratio=_cfg().goal_budget_warning_ratio,
        )
        if not budget.allowed:
            self.finish_reason = "budget_limited"
            return False
        return True

    async def _check_vision_blocked(self) -> Literal["stop"] | None:
        """If a non-vision model received image content, persist an error + return 'stop'."""
        sp = self._sp
        job = sp.job

        if not (
            sp.model_info
            and not sp.model_info.capabilities.vision
            and _llm_messages_have_image_content(self._llm_messages)
        ):
            return None

        message = localize(
            sp.request.language,
            "当前所选模型不支持图片，请选择支持视觉的模型后重试。",
            (
                "The selected model does not support images. "
                "Choose a vision model and try again."
            ),
        )
        logger.info(
            "Blocked image content for non-vision model=%s session=%s",
            sp.model_id,
            job.session_id,
        )
        job.publish(SSEEvent(
            AGENT_ERROR,
            {
                "error_type": "MODEL_DOES_NOT_SUPPORT_IMAGES",
                "error_message": message,
            },
        ))
        async with sp.session_factory() as db:
            async with db.begin():
                await create_part(
                    db,
                    message_id=self._assistant_msg_id,
                    session_id=job.session_id,
                    data={"type": "text", "text": message},
                )
                await create_part(
                    db,
                    message_id=self._assistant_msg_id,
                    session_id=job.session_id,
                    data={
                        "type": "step-finish",
                        "goal_run_id": job.goal_run_id,
                        "reason": "error",
                        "tokens": {},
                        "cost": 0.0,
                    },
                )
        self.finish_reason = "error"
        return "stop"

    def _reset_stream_accumulators(self) -> None:
        """Reset per-attempt accumulators between retries (mirrors original local reset)."""
        self._accumulated_text = ""
        self._accumulated_reasoning = ""
        self._reasoning_persisted_chars = 0
        self._tool_calls_in_step = []
        self._has_tool_calls = False

    # ------------------------------------------------------------------
    # process() phases — chunk handlers
    # ------------------------------------------------------------------

    async def _handle_tool_call_chunk(self, chunk: Any) -> None:
        """Submit one streamed tool call to the executor (with loop/permission checks)."""
        from app.session.tool_executor import ToolCallInfo

        sp = self._sp
        job = sp.job
        session_factory = sp.session_factory

        tc = chunk.data
        tn = tc.get("name", "")
        ta = tc.get("arguments", {})
        ci = tc.get("id", generate_ulid())
        tn, ta = _repair_tool_call_payload(tn, ta)
        # Tool parts are persisted while the provider is still streaming,
        # whereas reasoning used to be persisted only after the stream ended.
        # Flush the reasoning prefix first so a history reload preserves the
        # same reasoning -> tool boundary the live SSE UI displayed.
        await self._persist_pending_reasoning()
        response_scope = web_fetch_circuit_scope(
            job.session_id,
            job.stream_id,
        )
        web_fetch_circuit_msg = localize(
            job.language,
            WEB_FETCH_CIRCUIT_OPEN_MSG_ZH,
            WEB_FETCH_CIRCUIT_OPEN_MSG,
        )
        tool_failure_circuit_msg = localize(
            job.language,
            TOOL_FAILURE_CIRCUIT_OPEN_MSG_ZH,
            TOOL_FAILURE_CIRCUIT_OPEN_MSG,
        )
        web_search_limit_msg = localize(
            job.language,
            WEB_SEARCH_LIMIT_MSG_ZH,
            WEB_SEARCH_LIMIT_MSG,
        )
        hook_gate = getattr(sp, "hooks_runtime_active", None)
        hooks_active = bool(callable(hook_gate) and hook_gate())

        # A run of different URLs can evade the generic identical-call loop
        # detector while repeatedly hitting the same SSRF policy boundary.
        # Skip later web_fetch calls with a model-readable tool error, but do
        # not set _exec_blocked: the model must remain free to continue with
        # web_search summaries or produce a bounded final answer.
        if (
            tn.lower() == "web_fetch"
            and loop_detector.is_web_fetch_circuit_open(response_scope)
        ):
            job.publish(SSEEvent(
                TOOL_ERROR,
                {
                    "call_id": ci,
                    "error": web_fetch_circuit_msg,
                    "tool": "web_fetch",
                },
            ))
            await _persist_tool_error(
                session_factory,
                self._assistant_msg_id,
                job.session_id,
                "web_fetch",
                ci,
                ta,
                web_fetch_circuit_msg,
            )
            return

        # Repeated failures with different arguments are not caught by the
        # identical-call hash.  Once a tool's response-scoped circuit opens,
        # keep the model free to switch approaches without running it again.
        # The narrower web_fetch policy above retains its more specific error.
        if loop_detector.is_tool_failure_circuit_open(response_scope, tn):
            job.publish(SSEEvent(
                TOOL_ERROR,
                {
                    "call_id": ci,
                    "error": tool_failure_circuit_msg,
                    "tool": tn,
                },
            ))
            await _persist_tool_error(
                session_factory,
                self._assistant_msg_id,
                job.session_id,
                tn,
                ci,
                ta,
                tool_failure_circuit_msg,
            )
            return

        # Custom web_search is bounded across the entire model response, not
        # merely per step. Reserve the slot synchronously so a batch of
        # different concurrent queries cannot bypass the limit. Provider-
        # native search follows its separate per-step provider path.
        if (
            tn.lower() == "web_search"
            and not hooks_active
            and not loop_detector.admit_custom_web_search(response_scope)
        ):
            job.publish(SSEEvent(
                TOOL_ERROR,
                {
                    "call_id": ci,
                    "error": web_search_limit_msg,
                    "tool": "web_search",
                },
            ))
            await _persist_tool_error(
                session_factory,
                self._assistant_msg_id,
                job.session_id,
                "web_search",
                ci,
                ta,
                web_search_limit_msg,
            )
            return

        # Direct unit/embed callers may intentionally omit SessionPrompt's
        # middleware context. Preserve loop protection for that compatibility
        # path; production calls use the wired LoopDetectionMiddleware below.
        lr = LoopCheckResult(action="allow")
        if not hooks_active and (
            self._mw_ctx is None
            or getattr(sp, "middleware_chain", None) is None
        ):
            lr = loop_detector.check(
                job.session_id,
                tn,
                ta,
                language=job.language,
            )
            if lr.action == "block":
                job.publish(SSEEvent(AGENT_ERROR, {
                    "error_type": "loop_detected",
                    "error_message": lr.message,
                    "tool": tn,
                }))
                await _persist_tool_error(
                    session_factory, self._assistant_msg_id,
                    job.session_id, tn, ci, ta,
                    lr.message or "Loop detected — hard stop",
                )
                self._exec_blocked = True
                return

        # Resolve tool
        tool = sp.tool_registry.get(tn)
        if tool is None:
            tool = sp.tool_registry.get(tn.lower())
        if tool is None:
            get_registered = getattr(sp.tool_registry, "get_registered", None)
            is_enabled = getattr(sp.tool_registry, "is_enabled", None)
            disabled_tool = (
                get_registered(tn) or get_registered(tn.lower())
                if callable(get_registered)
                else None
            )
            if (
                disabled_tool is not None
                and callable(is_enabled)
                and not is_enabled(disabled_tool.id)
            ):
                error = f"Tool disabled by Security Center: {disabled_tool.id}"
                await _audit_tool_event(
                    session_factory,
                    tool=disabled_tool,
                    job=job,
                    call_id=ci,
                    decision="deny",
                    outcome="blocked",
                    interactive=job.interactive,
                )
                job.publish(SSEEvent(TOOL_ERROR, {"call_id": ci, "error": error}))
                await _persist_tool_error(
                    session_factory,
                    self._assistant_msg_id,
                    job.session_id,
                    disabled_tool.id,
                    ci,
                    ta,
                    error,
                )
                return
        if tool is None:
            tool = sp.tool_registry.get("invalid")
            if tool:
                ta = {"name": tn}
        if tool is None:
            job.publish(SSEEvent(TOOL_ERROR, {"call_id": ci, "error": f"Tool not found: {tn}"}))
            return

        # The invocation-source profile is a hard ceiling above user/session
        # permission rules.  It evaluates every capability, so a multi-purpose
        # tool cannot hide a write/process/network side effect behind a benign
        # primary label.
        blocked_capabilities = denied_tool_capabilities(
            job.invocation_source,
            tool,
        )
        if blocked_capabilities:
            blocked_summary = ",".join(blocked_capabilities)
            error = (
                f"Invocation source {job.invocation_source!r} is not allowed "
                f"to use {tool.id}: {blocked_summary}"
            )
            await _audit_tool_event(
                session_factory,
                tool=tool,
                job=job,
                call_id=ci,
                decision="deny",
                outcome="blocked",
                interactive=job.interactive,
                extra_details={"blocked_capabilities": blocked_summary},
            )
            job.publish(SSEEvent(TOOL_ERROR, {"call_id": ci, "error": error}))
            job.publish(SSEEvent(AGENT_ERROR, {
                "error_type": "invocation_source_denied",
                "error_message": error,
                "tool": tool.id,
                "call_id": ci,
            }))
            self._exec_blocked = True
            self.finish_reason = "error"
            await _persist_tool_error(
                session_factory,
                self._assistant_msg_id,
                job.session_id,
                tool.id,
                ci,
                ta,
                error,
            )
            return

        # Middleware may only preserve or narrow authority. Run it before any
        # permission prompt, durable tool-start record, ToolPart, or executor
        # task; the ordinary permission engine still runs afterwards and an
        # ``allow`` decision here can never override ask/deny.
        if not hooks_active and self._mw_ctx is not None:
            middleware_chain = getattr(sp, "middleware_chain", None)
            if middleware_chain is not None:
                try:
                    middleware_action = await middleware_chain.run_before_tool_exec(
                        tool.id,
                        copy.deepcopy(ta),
                        self._mw_ctx,
                    )
                except Exception:
                    logger.exception("Pre-tool middleware failed for %s", tool.id)
                    middleware_action = ToolAction(
                        action="block",
                        message="Pre-tool middleware failed; tool execution was blocked",
                        code="middleware_error",
                    )

                if middleware_action.action == "block":
                    error = middleware_action.message or (
                        f"Tool blocked by middleware: {tool.id}"
                    )
                    job.publish(SSEEvent(AGENT_ERROR, {
                        "error_type": middleware_action.code or "middleware_blocked",
                        "error_message": error,
                        "tool": tool.id,
                        "call_id": ci,
                    }))
                    await _persist_tool_error(
                        session_factory,
                        self._assistant_msg_id,
                        job.session_id,
                        tool.id,
                        ci,
                        ta,
                        error,
                    )
                    self._exec_blocked = True
                    return
                if middleware_action.action == "warn":
                    lr = LoopCheckResult(
                        action="warn",
                        message=middleware_action.message,
                    )

        # Permission check
        rp = "*"
        if tool.id in _FILE_TOOLS:
            rp = ta.get("file_path") or ta.get("output_path") or "*"
        action = evaluate(tool.id, rp, sp.merged_permissions)
        if action == "allow" and getattr(tool, "requires_approval", False):
            action = "ask"

        if action == "deny":
            await _audit_tool_event(
                session_factory,
                tool=tool,
                job=job,
                call_id=ci,
                decision="deny",
                outcome="denied",
                interactive=job.interactive,
            )
            job.publish(SSEEvent(TOOL_ERROR, {"call_id": ci, "error": f"Permission denied for tool: {tool.id}"}))
            await _persist_tool_error(
                session_factory, self._assistant_msg_id,
                job.session_id, tool.id, ci, ta, "Permission denied",
            )
            return

        if action == "ask":
            if not job.interactive:
                error = (
                    f"Permission approval required for {tool.id}; "
                    "non-interactive tasks cannot grant permissions"
                )
                # Headless callers must observe an explicit terminal failure,
                # not a successful task whose requested tool was silently
                # auto-approved or skipped.
                job.publish(SSEEvent(TOOL_ERROR, {"call_id": ci, "error": error}))
                job.publish(SSEEvent(AGENT_ERROR, {
                    "error_type": "permission_required",
                    "error_message": error,
                    "tool": tool.id,
                    "call_id": ci,
                }))
                self._exec_blocked = True
                self.finish_reason = "error"
                await _audit_tool_event(
                    session_factory,
                    tool=tool,
                    job=job,
                    call_id=ci,
                    decision="ask",
                    outcome="blocked",
                    interactive=False,
                )
                await _persist_tool_error(
                    session_factory,
                    self._assistant_msg_id,
                    job.session_id,
                    tool.id,
                    ci,
                    ta,
                    error,
                )
                return

            if job.goal_id is not None:
                from app.session.goal_guard import set_goal_waiting_user

                job.set_goal_waiting(True)
                await set_goal_waiting_user(
                    session_factory,
                    session_id=job.goal_session_id or job.session_id,
                    goal_id=job.goal_id,
                    goal_run_id=job.goal_run_id,
                    waiting=True,
                    blocker_code="permission_required",
                    blocker_message=f"Permission required for {tool.id}",
                )
                job.publish(
                    SSEEvent(
                        GOAL_NEEDS_USER,
                        {
                            "goal_id": job.goal_id,
                            "goal_run_id": job.goal_run_id,
                            "reason": "permission_required",
                            "tool": tool.id,
                            "call_id": ci,
                        },
                    )
                )
            try:
                decision = await _ask_permission(
                    job,
                    call_id=ci,
                    tool_name=tool.id,
                    tool_args=ta,
                    resource_pattern=rp,
                    language=sp.request.language,
                )
            finally:
                if job.goal_id is not None:
                    from app.session.goal_guard import set_goal_waiting_user

                    try:
                        await set_goal_waiting_user(
                            session_factory,
                            session_id=job.goal_session_id or job.session_id,
                            goal_id=job.goal_id,
                            goal_run_id=job.goal_run_id,
                            waiting=False,
                        )
                    finally:
                        job.set_goal_waiting(False)
            if decision.get("remember"):
                await _remember_permission_rule(
                    session_factory,
                    job.session_id,
                    sp,
                    permission=tool.id,
                    pattern=rp,
                    allow=bool(decision.get("allowed")),
                )
            if not decision.get("allowed"):
                if job.goal_id is not None:
                    from app.session.goal_guard import block_goal_for_user_action
                    from app.session.goal_guard import read_goal_execution_gate

                    goal_gate = await read_goal_execution_gate(
                        session_factory,
                        session_id=job.goal_session_id or job.session_id,
                        goal_id=job.goal_id,
                        goal_run_id=job.goal_run_id,
                    )
                    # A safe pause wakes permission waits with a denial. Keep
                    # the durable pausing state so the outer Goal boundary can
                    # finish the current slice as paused instead of turning it
                    # into a user-denied blocker.
                    if goal_gate.run_state != "pausing":
                        await block_goal_for_user_action(
                            session_factory,
                            session_id=job.goal_session_id or job.session_id,
                            goal_id=job.goal_id,
                            goal_run_id=job.goal_run_id,
                            blocker_code=(
                                "permission_timeout"
                                if decision.get("timed_out")
                                else "permission_denied"
                            ),
                            blocker_message=(
                                f"Permission request expired for {tool.id}"
                                if decision.get("timed_out")
                                else f"Permission denied for {tool.id}"
                            ),
                        )
                await _audit_tool_event(
                    session_factory,
                    tool=tool,
                    job=job,
                    call_id=ci,
                    decision="ask",
                    outcome="denied",
                    interactive=True,
                )
                job.publish(SSEEvent(TOOL_ERROR, {"call_id": ci, "error": f"User denied permission for: {tool.id}"}))
                await _persist_tool_error(
                    session_factory, self._assistant_msg_id,
                    job.session_id, tool.id, ci, ta, "Permission denied by user",
                )
                return

        # Ordinary permission is now fully resolved. Hook policy runs at the
        # last boundary before durable start audit, ToolPart creation,
        # TOOL_START, context construction, or executor submission. Passing
        # ``allow`` reflects the effective post-confirmation authority; the
        # Hook result can only narrow it to ask/deny.
        if not await self._admit_pre_tool_hook(
            tool=tool,
            tool_args=ta,
            call_id=ci,
            resource_pattern=rp,
        ):
            return
        if hooks_active:
            guarded = await self._run_post_hook_tool_guards(
                tool=tool,
                tool_args=ta,
                call_id=ci,
                response_scope=response_scope,
            )
            if guarded is None:
                return
            lr = guarded

        # Privileged tools may only enter the executor after their pre-action
        # audit record commits.  An unavailable audit store is a hard stop;
        # outcome records after execution remain best effort because a generic
        # tool side effect cannot be rolled back here.
        try:
            await _audit_tool_event(
                session_factory,
                tool=tool,
                job=job,
                call_id=ci,
                decision=action,
                outcome="started",
                interactive=job.interactive,
                required=tool_requires_durable_audit(tool),
            )
        except AuditPersistenceError:
            error = (
                f"Security audit unavailable; privileged tool blocked: {tool.id}"
            )
            job.publish(SSEEvent(TOOL_ERROR, {"call_id": ci, "error": error}))
            job.publish(SSEEvent(AGENT_ERROR, {
                "error_type": "security_audit_unavailable",
                "error_message": error,
                "tool": tool.id,
                "call_id": ci,
            }))
            self._exec_blocked = True
            self.finish_reason = "error"
            await _persist_tool_error(
                session_factory,
                self._assistant_msg_id,
                job.session_id,
                tool.id,
                ci,
                ta,
                error,
            )
            return

        # Persist "running" state
        tool_part_id = generate_ulid()
        async with session_factory() as db:
            async with db.begin():
                if job.goal_run_id is not None:
                    from app.models.goal_run import GoalRun

                    run = await db.get(GoalRun, job.goal_run_id)
                    if run is not None:
                        # Commit this conservative marker before the executor
                        # can begin an external side effect. Recovery will
                        # require review rather than replaying the run.
                        run.side_effects_started = True
                await create_part(
                    db, message_id=self._assistant_msg_id,
                    session_id=job.session_id, part_id=tool_part_id,
                    data={"type": "tool", "tool": tool.id, "call_id": ci,
                          "state": {"status": "running", "input": ta}},
                )
        job.publish(SSEEvent(TOOL_START, {
            "tool": tool.id, "call_id": ci,
            "arguments": ta, "session_id": job.session_id,
        }))
        # Build context
        ctx = ToolContext(
            session_id=job.session_id,
            message_id=self._assistant_msg_id,
            agent=sp.agent, call_id=ci,
            language=sp.request.language,
            abort_event=job.abort_event,
            workspace=sp.workspace,
            index_manager=getattr(sp, "index_manager", None),
            messages=self._llm_messages,
            discovered_tools=sp.discovered_tools,
            permission_rules=tuple(
                rule.model_dump(mode="json")
                for rule in sp.merged_permissions.rules
            ),
            permission_snapshot=serialize_permission_snapshot(
                sp.merged_permissions,
                global_permissions=(
                    sp.request._goal_permission_baseline[0]
                    if sp.request._goal_permission_baseline is not None
                    else None
                ),
                agent_permissions=(
                    sp.request._goal_permission_baseline[1]
                    if sp.request._goal_permission_baseline is not None
                    else None
                ),
            ),
            attachment_paths=frozenset(getattr(sp, "attachment_paths", ())),
            invocation_source=job.invocation_source,
            invocation_source_id=job.invocation_source_id,
            goal_id=job.goal_id,
            goal_run_id=job.goal_run_id,
            goal_session_id=job.goal_session_id,
            root_turn_id=job.root_turn_id,
            turn_run_id=job.turn_run_id,
            checkpoint_id=(
                getattr(sp, "checkpoint_binding", None).checkpoint_id
                if getattr(sp, "checkpoint_binding", None) is not None
                else None
            ),
            workspace_instance_id=job.workspace_instance_id,
            _publish_fn=lambda et, d: job.publish(SSEEvent(et, d)),
        )
        if job.goal_id is not None:
            from app.session.goal_guard import goal_job_execution_allowed

            ctx._execution_guard_fn = lambda: goal_job_execution_allowed(
                session_factory,
                job,
            )
        ctx._app_state = {  # type: ignore[attr-defined]
            "session_factory": session_factory,
            "provider_registry": sp.provider_registry,
            "agent_registry": sp.agent_registry,
            "tool_registry": sp.tool_registry,
            # Trusted first-party tools can submit a code-owned validation
            # intent through this object.  No tool schema or PromptRequest
            # field exposes checkpoint selection or validation budgets.
            "post_checkpoint_validation_scheduler": (
                job.post_checkpoint_validation_scheduler
            ),
        }
        office_repairer = (
            _office_precommit_repairer_for_prompt(sp)
            if tool.id == "office"
            else None
        )
        if office_repairer is not None:
            # The repairer is server-owned and never appears in a tool schema.
            # Its request contract contains tokenized declarative args only.
            ctx._app_state["office_precommit_repairer"] = office_repairer
        ctx._model_id = sp.model_id  # type: ignore[attr-defined]
        ctx._job = job  # type: ignore[attr-defined]
        ctx._depth = job._depth  # type: ignore[attr-defined]

        # Submit to streaming executor (concurrent tools start NOW)
        self._streaming_executor.submit(ToolCallInfo(
            index=self._exec_index, tool=tool,
            tool_name=tool.id, tool_args=ta,
            call_id=ci, ctx=ctx,
            timeout=_cfg().tool_timeout,
        ))
        self._exec_metadata[self._exec_index] = {
            "tool_part_id": tool_part_id,
            "loop_result": lr,
            "tool": tool,
            "tool_args": ta,
            "call_id": ci,
            "permission_decision": action,
        }
        self._exec_index += 1

    async def _run_post_hook_tool_guards(
        self,
        *,
        tool: Any,
        tool_args: dict[str, Any],
        call_id: str,
        response_scope: str,
    ) -> LoopCheckResult | None:
        """Run stateful loop/search middleware only after an allowing Hook."""

        sp = self._sp
        job = sp.job
        web_search_limit_msg = localize(
            job.language,
            WEB_SEARCH_LIMIT_MSG_ZH,
            WEB_SEARCH_LIMIT_MSG,
        )
        if (
            tool.id.lower() == "web_search"
            and not loop_detector.admit_custom_web_search(response_scope)
        ):
            job.publish(
                SSEEvent(
                    TOOL_ERROR,
                    {
                        "call_id": call_id,
                        "error": web_search_limit_msg,
                        "tool": "web_search",
                    },
                )
            )
            await _persist_tool_error(
                sp.session_factory,
                self._assistant_msg_id,
                job.session_id,
                "web_search",
                call_id,
                tool_args,
                web_search_limit_msg,
            )
            return None

        result = LoopCheckResult(action="allow")
        middleware_chain = getattr(sp, "middleware_chain", None)
        if self._mw_ctx is None or middleware_chain is None:
            result = loop_detector.check(
                job.session_id,
                tool.id,
                tool_args,
                language=job.language,
            )
            if result.action == "block":
                job.publish(
                    SSEEvent(
                        AGENT_ERROR,
                        {
                            "error_type": "loop_detected",
                            "error_message": result.message,
                            "tool": tool.id,
                        },
                    )
                )
                await _persist_tool_error(
                    sp.session_factory,
                    self._assistant_msg_id,
                    job.session_id,
                    tool.id,
                    call_id,
                    tool_args,
                    result.message or "Loop detected — hard stop",
                )
                self._exec_blocked = True
                return None
            return result

        try:
            middleware_action = await middleware_chain.run_before_tool_exec(
                tool.id,
                copy.deepcopy(tool_args),
                self._mw_ctx,
            )
        except Exception:
            logger.exception("Pre-tool middleware failed for %s", tool.id)
            middleware_action = ToolAction(
                action="block",
                message="Pre-tool middleware failed; tool execution was blocked",
                code="middleware_error",
            )
        if middleware_action.action == "block":
            error = middleware_action.message or (
                f"Tool blocked by middleware: {tool.id}"
            )
            job.publish(
                SSEEvent(
                    AGENT_ERROR,
                    {
                        "error_type": (
                            middleware_action.code or "middleware_blocked"
                        ),
                        "error_message": error,
                        "tool": tool.id,
                        "call_id": call_id,
                    },
                )
            )
            await _persist_tool_error(
                sp.session_factory,
                self._assistant_msg_id,
                job.session_id,
                tool.id,
                call_id,
                tool_args,
                error,
            )
            self._exec_blocked = True
            return None
        if middleware_action.action == "warn":
            return LoopCheckResult(
                action="warn",
                message=middleware_action.message,
            )
        return result

    async def _admit_pre_tool_hook(
        self,
        *,
        tool: Any,
        tool_args: dict[str, Any],
        call_id: str,
        resource_pattern: str,
    ) -> bool:
        """Run PreToolUse after permission and fail closed before execution."""

        sp = self._sp
        active = getattr(sp, "hooks_runtime_active", None)
        if not callable(active) or not active():
            return True

        fatal = False
        try:
            result = await sp.dispatch_hook_event(
                "PreToolUse",
                {
                    "tool_name": tool.id,
                    "tool_args": copy.deepcopy(tool_args),
                    "resource_pattern": resource_pattern,
                },
                permission_decision="allow",
                message_id=self._assistant_msg_id,
                call_id=call_id,
                checkpoint_id=(
                    sp.checkpoint_binding.checkpoint_id
                    if getattr(sp, "checkpoint_binding", None) is not None
                    else None
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("PreToolUse Hook admission failed closed")
            result = None
            fatal = True

        # The code-owned release switch is intentionally dynamic. If it is
        # closed while a dispatch or confirmation is in flight, restore the
        # pre-Hooks admission path instead of turning the kill switch itself
        # into a tool denial.
        if not active():
            return True

        decision = None
        state = None
        if result is not None:
            state = getattr(result.state, "value", str(result.state))
            decision_value = result.pre_tool_decision
            decision = (
                getattr(decision_value, "value", str(decision_value))
                if decision_value is not None
                else None
            )
            if state == "completed" and decision == "allow":
                return True
            if state == "completed" and decision == "ask":
                if await sp.confirm_pre_tool_hook(
                    event_id=result.hook_event.event_id,
                    tool_name=tool.id,
                    call_id=call_id,
                ):
                    return True
            fatal = state in {"failed_closed", "cancelled"}

        if self._sp.job.abort_event.is_set():
            return False
        error = (
            f"Tool blocked because required Hook policy failed: {tool.id}"
            if fatal
            else f"Tool blocked by project Hook policy: {tool.id}"
        )
        self._sp.job.publish(
            SSEEvent(
                TOOL_ERROR,
                {"call_id": call_id, "tool": tool.id, "error": error},
            )
        )
        await _persist_tool_error(
            self._sp.session_factory,
            self._assistant_msg_id,
            self._sp.job.session_id,
            tool.id,
            call_id,
            tool_args,
            error,
        )
        if fatal:
            self._exec_blocked = True
            self.finish_reason = "error"
            self._sp.job.publish(
                SSEEvent(
                    AGENT_ERROR,
                    {
                        "error_type": "hook_policy_failed",
                        "error_message": "Required Hook policy failed closed.",
                        "tool": tool.id,
                        "call_id": call_id,
                    },
                )
            )
        return False

    async def _dispatch_post_tool_hook(
        self,
        *,
        tool: Any,
        tool_args: dict[str, Any],
        call_id: str,
        outcome: str,
        result: Any | None = None,
    ) -> None:
        """Publish one output-free PostToolUse projection at finalization."""

        sp = self._sp
        active = getattr(sp, "hooks_runtime_active", None)
        if not callable(active) or not active():
            return
        metadata = getattr(result, "metadata", None) if result is not None else None
        output = getattr(result, "output", None) if result is not None else None
        attachments = (
            getattr(result, "attachments", None) if result is not None else None
        )
        await sp._dispatch_required_hook(
            "PostToolUse",
            {
                "tool_name": tool.id,
                "tool_args": copy.deepcopy(tool_args),
                "outcome": outcome,
                "success": (
                    bool(getattr(result, "success", False))
                    if result is not None
                    else False
                ),
                "output_length": len(output) if isinstance(output, str) else 0,
                "attachment_count": len(attachments or ()),
                "metadata_keys": (
                    sorted(str(key) for key in metadata)[:128]
                    if isinstance(metadata, dict)
                    else []
                ),
            },
            message_id=self._assistant_msg_id,
            call_id=call_id,
            checkpoint_id=(
                sp.checkpoint_binding.checkpoint_id
                if getattr(sp, "checkpoint_binding", None) is not None
                else None
            ),
        )

    async def _dispatch_post_tool_hook_once(
        self,
        meta: dict[str, Any],
        *,
        outcome: str,
        result: Any | None = None,
    ) -> None:
        """Dispatch the terminal Hook at most once for this admitted call."""

        if meta.get("_post_tool_hook_dispatched", False):
            return
        # Mark before awaiting: if a required Hook fails or this task is
        # cancelled while dispatching, the finalization fallback must not emit
        # the same semantic boundary again.
        meta["_post_tool_hook_dispatched"] = True
        await self._dispatch_post_tool_hook(
            tool=meta["tool"],
            tool_args=meta["tool_args"],
            call_id=meta["call_id"],
            outcome=outcome,
            result=result,
        )

    async def _handle_web_search_start_chunk(self, chunk: Any) -> None:
        """Persist a 'running' tool part for an OpenAI-native web search start."""
        sp = self._sp
        job = sp.job

        ws_call_id = chunk.data.get("id", "")
        ws_query = chunk.data.get("query", "")
        self._native_search_ids.add(ws_call_id)
        self._native_search_count += 1

        await record_security_event(
            sp.session_factory,
            source_kind="provider",
            source_id=sp.provider.id,
            invocation_source_kind=job.invocation_source,
            invocation_source_id=job.invocation_source_id,
            capability="web_search",
            action="search",
            decision="allow",
            outcome="started",
            session_id=job.session_id,
            call_id=ws_call_id,
            details={
                "native": True,
                "step": sp.step,
                "invocation_source": job.invocation_source,
            },
            required=True,
        )

        # Drop excess searches beyond the per-step cap
        if self._native_search_count > get_settings().max_native_searches_per_step:
            return

        self._ws_part_ids[ws_call_id] = generate_ulid()
        async with sp.session_factory() as db:
            async with db.begin():
                if job.goal_run_id is not None:
                    from app.models.goal_run import GoalRun

                    run = await db.get(GoalRun, job.goal_run_id)
                    if run is not None:
                        run.side_effects_started = True
                await create_part(
                    db,
                    message_id=self._assistant_msg_id,
                    session_id=job.session_id,
                    part_id=self._ws_part_ids[ws_call_id],
                    data={
                        "type": "tool",
                        "tool": "web_search",
                        "call_id": ws_call_id,
                        "state": {"status": "running", "input": {"query": ws_query}},
                    },
                )

        # Emit TOOL_START so frontend shows searching state
        job.publish(SSEEvent(
            TOOL_START,
            {
                "tool": "web_search",
                "call_id": ws_call_id,
                "arguments": {"query": ws_query},
                "session_id": job.session_id,
            },
        ))

    async def _handle_web_search_result_chunk(self, chunk: Any) -> None:
        """Format & persist completion of an OpenAI-native web search."""
        sp = self._sp
        job = sp.job

        ws_call_id = chunk.data.get("id", "")
        ws_query = chunk.data.get("query", "")
        ws_results = chunk.data.get("results", [])

        # Account and audit every provider-native search exactly once, even if
        # its UI part was dropped by the per-step display cap.  Audit metadata
        # deliberately excludes both the query and returned sources.
        if (
            ws_call_id in self._native_search_ids
            and ws_call_id not in self._native_search_completed
        ):
            self._native_search_completed.add(ws_call_id)
            await _search_quota.increment(charged=False)
            await record_security_event(
                sp.session_factory,
                source_kind="provider",
                source_id=sp.provider.id,
                capability="web_search",
                action="search",
                decision="allow",
                outcome="success",
                session_id=job.session_id,
                call_id=ws_call_id,
                invocation_source_kind=job.invocation_source,
                invocation_source_id=job.invocation_source_id,
                details={
                    "native": True,
                    "step": sp.step,
                    "invocation_source": job.invocation_source,
                },
            )

        # Skip results for searches that exceeded the per-step cap
        if ws_call_id not in self._ws_part_ids:
            return

        # Format results like the custom web_search tool
        output_lines: list[str] = []
        results_data: list[dict[str, str]] = []
        for i, r in enumerate(ws_results, 1):
            title = r.get("title", "")
            url = r.get("url", "")
            snippet = r.get("snippet", "")
            output_lines.append(f"{i}. {title}")
            output_lines.append(f"   {url}")
            if snippet:
                output_lines.append(f"   {snippet}")
            output_lines.append("")
            results_data.append({"url": url, "title": title, "snippet": snippet})

        count = len(results_data)
        from app.i18n import localize

        output_text = "\n".join(output_lines) if output_lines else localize(
            sp.request.language, "未找到结果。", "No results found."
        )
        ws_title = localize(
            sp.request.language,
            f"搜索：{ws_query[:50]}（{count} 条结果）",
            f"Search: {ws_query[:50]} ({count} results)",
        )
        ws_metadata = {
            "query": ws_query,
            "count": count,
            "results": results_data,
            "_native": True,
        }

        # Update tool part to completed
        ws_part_id = self._ws_part_ids.pop(ws_call_id, None)
        if ws_part_id:
            async with sp.session_factory() as db:
                async with db.begin():
                    await update_part_data(
                        db,
                        part_id=ws_part_id,
                        data={
                            "type": "tool",
                            "tool": "web_search",
                            "call_id": ws_call_id,
                            "state": {
                                "status": "completed",
                                "input": {"query": ws_query},
                                "output": output_text,
                                "title": ws_title,
                                "metadata": ws_metadata,
                            },
                        },
                    )

        # Emit TOOL_RESULT so frontend updates to completed
        job.publish(SSEEvent(
            TOOL_RESULT,
            {
                "call_id": ws_call_id,
                "tool": "web_search",
                "output": output_text[:500],
                "title": ws_title,
                "metadata": ws_metadata,
            },
        ))

    async def _handle_stream_error_chunk(self, chunk: Any) -> Literal["stop"]:
        """Mid-stream 'error' chunk: persist any accumulated text + publish error + clean up."""
        sp = self._sp
        job = sp.job
        self.finish_reason = "error"
        self.has_text = bool(self._accumulated_text.strip())
        await self._persist_pending_reasoning()

        if self._accumulated_text:
            async with sp.session_factory() as db:
                async with db.begin():
                    await create_part(
                        db,
                        message_id=self._assistant_msg_id,
                        session_id=job.session_id,
                        data={"type": "text", "text": self._accumulated_text},
                    )
        job.publish(SSEEvent(
            AGENT_ERROR,
            {"error_message": chunk.data.get("message", "LLM error")},
        ))
        await _delete_empty_assistant_messages(sp.session_factory, job.session_id)
        return "stop"

    # ------------------------------------------------------------------
    # process() phases — post-stream
    # ------------------------------------------------------------------

    async def _handle_stream_error(self) -> Literal["compact", "stop"]:
        """Handle a retries-exhausted or non-retryable stream exception."""
        sp = self._sp
        job = sp.job

        # --- Reactive compact: recover from context overflow via compaction ---
        # Inspired by Claude Code's reactive compact pattern.
        if is_context_overflow(self._stream_error):
            logger.info(
                "Context overflow detected, triggering reactive compaction for session %s",
                job.session_id,
            )
            await _delete_empty_assistant_messages(sp.session_factory, job.session_id)
            return "compact"

        logger.exception("LLM stream error (not retryable or retries exhausted)")
        self.has_text = bool(self._accumulated_text.strip())
        self.finish_reason = "error"
        await self._persist_pending_reasoning()
        if self._accumulated_text or self._accumulated_reasoning:
            async with sp.session_factory() as db:
                async with db.begin():
                    if self._accumulated_text:
                        await create_part(
                            db,
                            message_id=self._assistant_msg_id,
                            session_id=job.session_id,
                            data={"type": "text", "text": self._accumulated_text},
                        )
                    await create_part(
                        db,
                        message_id=self._assistant_msg_id,
                        session_id=job.session_id,
                        data={
                            "type": "step-finish",
                            "goal_run_id": job.goal_run_id,
                            "reason": self.finish_reason,
                            "tokens": self.usage_data,
                            "cost": self.step_cost,
                        },
                    )
            job.publish(SSEEvent(
                STEP_FINISH,
                {
                    "tokens": self.usage_data,
                    "cost": self.step_cost,
                    "total_cost": sp.total_cost + self.step_cost,
                    "reason": self.finish_reason,
                },
            ))
        await _delete_empty_assistant_messages(sp.session_factory, job.session_id)
        job.publish(SSEEvent(
            AGENT_ERROR,
            {"error_message": f"LLM stream error: {self._stream_error}"},
        ))
        return "stop"

    async def _handle_empty_output_after_retries(self) -> bool:
        """Return True if the step produced nothing after retries (caller continues the loop).

        The model produced nothing (no text, no tools, no reasoning) even after retries.
        Rather than surfacing an error, delete the empty assistant message shell and let
        the outer loop re-invoke the LLM with the full conversation context intact.
        The hard step cap (50) prevents infinite looping.
        """
        sp = self._sp
        job = sp.job

        if not (
            not self._accumulated_text.strip()
            and not self._has_tool_calls
            and not self._accumulated_reasoning
            and self._stream_error is None
            and not job.abort_event.is_set()
        ):
            return False

        logger.warning(
            "LLM produced no output after retries for session %s, continuing loop",
            job.session_id,
        )
        # Publish a paired non-terminal STEP_FINISH so the frontend step tracker
        # stays consistent without seeing an undeclared terminal reason.
        job.publish(SSEEvent(
            STEP_FINISH,
            {
                "tokens": None,
                "cost": 0.0,
                "total_cost": sp.total_cost,
                "reason": "tool_use",
            },
        ))
        await _delete_empty_assistant_messages(sp.session_factory, job.session_id)
        return True

    async def _persist_text_and_reasoning(self) -> None:
        """Persist accumulated text + reasoning as parts on the assistant message."""
        sp = self._sp
        self.has_text = bool(self._accumulated_text.strip())
        await self._persist_pending_reasoning()
        async with sp.session_factory() as db:
            async with db.begin():
                if self._accumulated_text.strip():
                    await create_part(
                        db,
                        message_id=self._assistant_msg_id,
                        session_id=sp.job.session_id,
                        data={"type": "text", "text": self._accumulated_text},
                    )

    async def _persist_pending_reasoning(self) -> None:
        """Persist only the reasoning suffix not already flushed before tools."""

        pending = self._accumulated_reasoning[self._reasoning_persisted_chars :]
        if not pending:
            return
        sp = self._sp
        async with sp.session_factory() as db:
            async with db.begin():
                await create_part(
                    db,
                    message_id=self._assistant_msg_id,
                    session_id=sp.job.session_id,
                    data={"type": "reasoning", "text": pending},
                )
        self._reasoning_persisted_chars = len(self._accumulated_reasoning)

    # ------------------------------------------------------------------
    # process() phases — tool dispatch
    # ------------------------------------------------------------------

    async def _dispatch_tool_calls(self) -> Literal["stop"] | None:
        """Collect concurrent tool results, finalize each. Returns 'stop' if loop-blocked."""
        # Filter out native web search calls (already persisted during streaming)
        if self._native_search_ids:
            self._tool_calls_in_step = [
                tc for tc in self._tool_calls_in_step
                if tc.get("id") not in self._native_search_ids
            ]
            if not self._tool_calls_in_step:
                self._has_tool_calls = False

        if self._exec_blocked:
            return "stop"

        if not (self._has_tool_calls and self._streaming_executor.has_submissions):
            return None

        # === Collect results — concurrent tools already running, exclusive run now ===
        exec_results = await self._streaming_executor.collect()

        # === Finalize — persist results, emit SSE, handle side effects ===
        if exec_results:
            for exec_result in exec_results:
                meta = self._exec_metadata.get(exec_result.index)
                if meta is None:
                    continue
                try:
                    await self._finalize_one_tool_result(meta, exec_result)
                except BaseException:
                    # Even an unexpected persistence/middleware failure is a
                    # real terminal boundary for the already-executed tool.
                    # The per-call guard prevents a required PostToolUse Hook
                    # failure from being dispatched a second time here.
                    await self._dispatch_post_tool_hook_once(
                        meta,
                        outcome="finalization_error",
                        result=getattr(exec_result, "result", None),
                    )
                    raise

        return None

    async def _finalize_one_tool_result(
        self, meta: dict[str, Any], exec_result: Any,
    ) -> None:
        """Persist one tool result: timeouts/errors, SSE, side effects, agent switching."""
        sp = self._sp
        job = sp.job
        session_factory = sp.session_factory

        tool_part_id = meta["tool_part_id"]
        loop_result = meta["loop_result"]
        tool = meta["tool"]
        tool_args = meta["tool_args"]
        call_id = meta["call_id"]
        permission_decision = meta.get("permission_decision", "system")

        # Handle timeout
        if exec_result.timed_out:
            timeout_msg = f"Tool timed out after {_cfg().tool_timeout}s: {tool.id}"
            logger.warning(timeout_msg)
            job.publish(SSEEvent(TOOL_ERROR, {"call_id": call_id, "error": timeout_msg}))
            await _update_tool_part_error(
                session_factory, tool_part_id, tool.id, call_id, tool_args, timeout_msg,
            )
            await _audit_tool_event(
                session_factory,
                tool=tool,
                job=job,
                call_id=call_id,
                decision=permission_decision,
                outcome="timeout",
                interactive=job.interactive,
            )
            await self._dispatch_post_tool_hook_once(
                meta,
                outcome="timeout",
            )
            return

        # Handle execution error
        if exec_result.error is not None:
            if isinstance(exec_result.error, RejectedError):
                err_msg = f"Permission denied: {exec_result.error.permission}"
            elif isinstance(exec_result.error, asyncio.CancelledError):
                err_msg = str(exec_result.error) or "Tool execution cancelled"
            else:
                err_msg = str(exec_result.error)
                logger.exception("Tool execution error: %s", tool.id)
            job.publish(SSEEvent(TOOL_ERROR, {"call_id": call_id, "error": err_msg}))
            await _update_tool_part_error(
                session_factory, tool_part_id, tool.id, call_id, tool_args, err_msg,
            )
            await _audit_tool_event(
                session_factory,
                tool=tool,
                job=job,
                call_id=call_id,
                decision=permission_decision,
                outcome=(
                    "denied"
                    if isinstance(exec_result.error, RejectedError)
                    else "cancelled"
                    if isinstance(exec_result.error, asyncio.CancelledError)
                    else "error"
                ),
                interactive=job.interactive,
            )
            await self._dispatch_post_tool_hook_once(
                meta,
                outcome=(
                    "denied"
                    if isinstance(exec_result.error, RejectedError)
                    else "cancelled"
                    if isinstance(exec_result.error, asyncio.CancelledError)
                    else "error"
                ),
            )
            return

        result = exec_result.result
        if result is None:
            await self._dispatch_post_tool_hook_once(
                meta,
                outcome="missing_result",
            )
            return

        loop_detector.record_tool_result(
            web_fetch_circuit_scope(job.session_id, job.stream_id),
            tool.id,
            success=result.success,
            error=result.error,
        )

        # A guarded workspace transaction is not user-visible success until
        # its exact mutation evidence and version pins are durable.  This runs
        # before audit-success, session-file refresh, TOOL_RESULT and the
        # completed ToolPart transition.
        if result.success:
            try:
                await sp._record_tool_checkpoint_effects(
                    tool_id=tool.id,
                    call_id=call_id,
                    metadata=result.metadata,
                )
            except Exception as exc:
                sp._checkpoint_ledger_failed = True
                error_message = (
                    "Workspace changes were applied, but the rewind ledger "
                    "could not be finalized. The turn was stopped for recovery."
                )
                job.publish(
                    SSEEvent(
                        TOOL_ERROR,
                        {"call_id": call_id, "tool": tool.id, "error": error_message},
                    )
                )
                await _update_tool_part_error(
                    session_factory,
                    tool_part_id,
                    tool.id,
                    call_id,
                    tool_args,
                    error_message,
                )
                await _audit_tool_event(
                    session_factory,
                    tool=tool,
                    job=job,
                    call_id=call_id,
                    decision=permission_decision,
                    outcome="error",
                    interactive=job.interactive,
                )
                await self._dispatch_post_tool_hook_once(
                    meta,
                    outcome="checkpoint_failed",
                    result=result,
                )
                raise RuntimeError(error_message) from exc

        await _audit_tool_event(
            session_factory,
            tool=tool,
            job=job,
            call_id=call_id,
            decision=permission_decision,
            outcome="success" if result.success else "error",
            interactive=job.interactive,
        )

        # Persist generated-file/todo side effects before announcing the result.
        # The client refreshes the workspace file list as soon as TOOL_RESULT
        # arrives, so emitting first creates a real race where a successfully
        # generated artifact is absent from that refresh.
        await self._apply_tool_side_effects(tool, result)

        # Emit SSE result
        if result.error:
            job.publish(SSEEvent(
                TOOL_ERROR,
                {"call_id": call_id, "error": result.error, "tool": tool.id},
            ))
        else:
            job.publish(SSEEvent(
                TOOL_RESULT,
                {
                    "call_id": call_id,
                    "tool": tool.id,
                    "output": result.output[:500] if result.output else "",
                    "title": result.title,
                    "metadata": result.metadata,
                },
            ))

        persist_output = await self._build_tool_persist_output(
            tool, tool_args, result, loop_result,
        )

        # Update tool part to "completed" / "error"
        async with session_factory() as db:
            async with db.begin():
                await update_part_data(
                    db,
                    tool_part_id,
                    {
                        "type": "tool",
                        "tool": tool.id,
                        "call_id": call_id,
                        "state": {
                            "status": "completed" if result.success else "error",
                            "input": tool_args,
                            "output": persist_output,
                            "title": result.title,
                            "metadata": result.metadata,
                        },
                    },
                )

        # Persist file attachments returned by the tool as FileParts
        if result.attachments:
            async with session_factory() as db:
                async with db.begin():
                    for att in result.attachments:
                        await create_part(
                            db,
                            message_id=self._assistant_msg_id,
                            session_id=job.session_id,
                            data={"type": "file", **att},
                        )

        self._maybe_switch_agent(result)
        await self._dispatch_post_tool_hook_once(
            meta,
            outcome="success" if result.success else "error",
            result=result,
        )

    async def _apply_tool_side_effects(self, tool: Any, result: Any) -> None:
        """Update session files, todos, and web-search quota from a tool result.

        Visual artifacts remain message-backed preview state.  They must not be
        silently materialized into the user's workspace: ``artifact`` is an
        agent-control capability, so doing so would bypass the source policy,
        file-write approval, versioning, and guarded workspace transaction.
        Users can request an explicit ``write`` when they want an artifact on
        disk.
        """
        sp = self._sp
        job = sp.job
        session_factory = sp.session_factory

        # Web search usage tracking
        if tool.id == "web_search" and result.success:
            charged = bool(result.metadata and result.metadata.get("charged"))
            await _search_quota.increment(charged=charged)

        # Track every declared generated file through one metadata contract.
        # This includes shell-created outputs (for example edge-tts MP3 files),
        # plugin artifacts, code execution, and existing single-file tools.
        if result.success:
            for file_path in _artifact_delivery_paths(tool.id, result.metadata):
                await _track_session_file(
                    session_factory,
                    session_id=job.session_id,
                    file_path=file_path,
                    tool_id=tool.id,
                )

        # Track todos from todo tool results
        if tool.id == "todo" and result.metadata and "todos" in result.metadata:
            sp.current_todos = list(result.metadata["todos"])

    async def _build_tool_persist_output(
        self,
        tool: Any,
        tool_args: dict[str, Any],
        result: Any,
        loop_result: Any,
    ) -> str:
        """Assemble the output text persisted to the tool part (+ reminders, middleware)."""
        sp = self._sp

        persist_output = result.output or result.error or ""
        if result.success:
            persist_output += _presentation_reminder(
                tool.id,
                result.metadata,
                language=sp.job.language,
            )

        # Inject loop warning into output so LLM sees it
        if loop_result.action == "warn" and loop_result.message:
            persist_output += f"\n\n{loop_result.message}"

        response_scope = web_fetch_circuit_scope(
            sp.job.session_id,
            sp.job.stream_id,
        )
        if (
            not result.success
            and loop_detector.is_tool_failure_circuit_open(response_scope, tool.id)
        ):
            persist_output += "\n\n" + localize(
                sp.job.language,
                TOOL_FAILURE_CIRCUIT_OPEN_MSG_ZH,
                TOOL_FAILURE_CIRCUIT_OPEN_MSG,
            )

        # Run middleware after_tool_exec hooks
        if self._mw_ctx is not None:
            persist_output = await sp.middleware_chain.run_after_tool_exec(
                tool.id, tool_args, persist_output, self._mw_ctx,
            )

        return persist_output

    def _maybe_switch_agent(self, result: Any) -> None:
        """Apply agent switching if the tool result requested it (plan tool enter/exit)."""
        sp = self._sp
        if not (result.metadata and result.metadata.get("switch_agent")):
            return

        new_agent_name = result.metadata["switch_agent"]
        new_agent = sp.agent_registry.get(new_agent_name)
        if not new_agent:
            return

        sp.agent = new_agent
        if sp.agent.model:
            new_resolved = sp.provider_registry.resolve_model(sp.agent.model.model_id)
            if new_resolved:
                sp.provider, sp.model_info = new_resolved
                sp.model_id = sp.agent.model.model_id
        sp.rebuild_permissions_and_prompt()
        logger.info("Agent switched to: %s", sp.agent.name)

    # ------------------------------------------------------------------
    # process() phases — cost / finish / overflow
    # ------------------------------------------------------------------

    def _compute_step_cost(self) -> None:
        """Compute self.step_cost from self.usage_data + model pricing; log usage."""
        sp = self._sp
        if self.usage_data and sp.model_info:
            if sp.model_info.pricing and (
                sp.model_info.pricing.prompt > 0 or sp.model_info.pricing.completion > 0
            ):
                self.step_cost = _calculate_step_cost(self.usage_data, sp.model_info)
            elif sp.model_info.provider_id == "openai-subscription":
                self.step_cost = 0.0
            else:
                logger.warning(
                    "Pricing unavailable for model %s, cost will be $0.00 "
                    "(tokens: %d input, %d output)",
                    sp.model_info.id,
                    self.usage_data.get("input", 0),
                    self.usage_data.get("output", 0),
                )

        if self.usage_data:
            logger.info(
                "Step usage [%s]: input=%d, output=%d, reasoning=%d, "
                "cache_read=%d, cache_write=%d",
                sp.model_info.id if sp.model_info else "unknown",
                self.usage_data.get("input", 0),
                self.usage_data.get("output", 0),
                self.usage_data.get("reasoning", 0),
                self.usage_data.get("cache_read", 0),
                self.usage_data.get("cache_write", 0),
            )

    async def _persist_step_finish(self) -> None:
        """Persist the step boundary, then publish it to the live stream.

        Database-first ordering guarantees that a client reacting to the SSE
        event can immediately reconcile the same terminal boundary from the
        messages API.  Publishing first creates a race where recovery sees an
        active-looking final message and leaves the UI in "finalizing".
        """
        sp = self._sp
        job = sp.job

        async with sp.session_factory() as db:
            async with db.begin():
                await create_part(
                    db,
                    message_id=self._assistant_msg_id,
                    session_id=job.session_id,
                    data={
                        "type": "step-finish",
                        "goal_run_id": job.goal_run_id,
                        "reason": self.finish_reason,
                        "tokens": self.usage_data,
                        "cost": self.step_cost,
                    },
                )
        job.publish(SSEEvent(
            STEP_FINISH,
            {
                "tokens": self.usage_data,
                "cost": self.step_cost,
                "total_cost": sp.total_cost + self.step_cost,
                "reason": self.finish_reason,
            },
        ))

    def _check_context_overflow(self) -> bool:
        """Return True if usage_data exceeds the safe compaction threshold."""
        sp = self._sp
        if not (self.usage_data and sp.model_info):
            return False

        from app.session.compaction import should_compact

        max_ctx = (
            _get_effective_context_window(sp.model_info)
            or sp.model_info.capabilities.max_context
        )
        max_out = sp.model_info.capabilities.max_output
        if should_compact(self.usage_data, max_ctx, model_max_output=max_out):
            logger.info("Context overflow detected, running compaction")
            return True
        return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _ask_permission(
    job: GenerationJob,
    call_id: str,
    tool_name: str,
    tool_args: dict[str, Any],
    resource_pattern: str = "*",
    language: str = "en",
) -> dict[str, bool]:
    """Ask user for permission via SSE and wait for response."""
    permission_call_id = generate_ulid()
    arguments, truncated = _permission_arguments_for_event(tool_args)
    message = _permission_message(tool_name, arguments, truncated, language=language)
    metadata = _permission_metadata(tool_name, arguments)
    job.register_response_request(
        permission_call_id,
        prompt_type="permission",
        timeout=300.0,
        tool_call_id=call_id,
        tool=tool_name,
    )
    job.publish(
        SSEEvent(
            PERMISSION_REQUEST,
            {
                "call_id": permission_call_id,
                "tool_call_id": call_id,
                "tool": tool_name,
                "permission": tool_name,
                "patterns": [resource_pattern] if resource_pattern else [],
                "arguments": arguments,
                "metadata": metadata,
                "message": message,
                "arguments_truncated": truncated,
            },
        )
    )

    try:
        response = await job.wait_for_response(permission_call_id, timeout=300.0)
        return _permission_decision_from_response(response)
    except TimeoutError:
        logger.warning("Permission request timed out for %s", tool_name)
        return {"allowed": False, "remember": False, "timed_out": True}


def _permission_decision_from_response(response: Any) -> dict[str, bool]:
    if isinstance(response, dict):
        return {
            "allowed": bool(response.get("allowed")),
            "remember": bool(response.get("remember")),
        }
    allowed = str(response).lower() in ("allow", "yes", "true", "1")
    return {"allowed": allowed, "remember": False}


async def _remember_permission_rule(
    _session_factory: async_sessionmaker[AsyncSession],
    session_id: str,
    sp: SessionPrompt,
    *,
    permission: str,
    pattern: str,
    allow: bool,
) -> None:
    del _session_factory, session_id
    action: Literal["allow", "deny"] = "allow" if allow else "deny"
    rule = PermissionRule(action=action, permission=permission, pattern=pattern or "*")

    sp.session_permissions.rules = [
        existing
        for existing in sp.session_permissions.rules
        if not (existing.permission == rule.permission and existing.pattern == rule.pattern)
    ]
    sp.session_permissions.rules.append(rule)
    sp.merged_permissions.rules.append(rule)


def _permission_arguments_for_event(
    value: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Return permission arguments suitable for SSE display.

    The permission prompt must show the action being approved, but it should not
    turn one huge file write into an unbounded SSE event. Key names that are
    obviously secret-like are redacted; long string values are truncated with a
    clear marker so the UI can still show the relevant target and command.
    """
    sanitized = _sanitize_permission_value(value)
    encoded = json.dumps(sanitized, default=str, ensure_ascii=False)
    if len(encoded) <= _PERMISSION_ARGUMENT_CHAR_LIMIT:
        return cast(dict[str, Any], sanitized), False

    clipped = _clip_permission_value(sanitized)
    return cast(dict[str, Any], clipped), True


def _sanitize_permission_value(value: Any, key: str | None = None) -> Any:
    if key and _SENSITIVE_ARG_KEY_RE.search(key):
        return "[redacted]"
    if isinstance(value, dict):
        return {
            str(k): _sanitize_permission_value(v, str(k))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_permission_value(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_permission_value(item) for item in value]
    return value


def _clip_permission_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _clip_permission_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip_permission_value(item) for item in value[:50]]
    if isinstance(value, str) and len(value) > 12_000:
        return value[:12_000] + "\n\n[permission preview truncated]"
    return value


def _permission_message(
    tool_name: str,
    arguments: dict[str, Any],
    truncated: bool,
    *,
    language: str = "en",
) -> str:
    if tool_name == "image_generate":
        if language == "zh":
            return (
                "是否允许通过 SiliconFlow 发起一次图片生成？图片描述会发送给外部供应商；"
                "目录估价可能变化，最终费用以供应商账单为准。本次确认仅授权一次生成。"
            )
        return (
            "Allow one image generation request through SiliconFlow? The prompt is sent "
            "to an external provider; catalog pricing may change and the provider bill "
            "is authoritative. This confirmation authorizes one generation only."
        )

    if tool_name == "bash":
        command = arguments.get("command")
        if isinstance(command, str) and command.strip():
            return f"Allow running this shell command?\n\n{command}"
        return "Allow running a shell command?"

    if tool_name in {"write", "edit", "read"}:
        file_path = arguments.get("file_path")
        if isinstance(file_path, str) and file_path.strip():
            suffix = " The preview was truncated." if truncated else ""
            return f"Allow {tool_name} on {file_path}?{suffix}"

    suffix = " Preview was truncated." if truncated else ""
    return f"Allow tool '{tool_name}' with the shown arguments?{suffix}"


def _permission_metadata(
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Return additive context used by richer permission prompts."""

    if tool_name != "image_generate":
        return {}

    from app.image_generation.siliconflow import (
        SILICONFLOW_IMAGE_ESTIMATED_COST_CNY,
        SILICONFLOW_IMAGE_MODEL,
        SILICONFLOW_IMAGE_PRICING_AS_OF,
        SILICONFLOW_IMAGE_PRICING_SOURCE_URL,
    )

    return {
        "provider": "siliconflow",
        "provider_name": "SiliconFlow",
        "model": SILICONFLOW_IMAGE_MODEL,
        "image_size": arguments.get("image_size", "1024x1024"),
        "estimated_cost": SILICONFLOW_IMAGE_ESTIMATED_COST_CNY,
        "currency": "CNY",
        "pricing_unit": "image",
        "pricing_basis": "official_catalog",
        "pricing_as_of": SILICONFLOW_IMAGE_PRICING_AS_OF,
        "pricing_source_url": SILICONFLOW_IMAGE_PRICING_SOURCE_URL,
        "approval_mode": "per_call",
        "external_billing": True,
    }


async def _delete_empty_assistant_messages(
    session_factory: async_sessionmaker[AsyncSession],
    session_id: str,
    *,
    _retried: bool = False,
) -> None:
    """Remove assistant message shells that ended with zero persisted parts."""
    try:
        async with session_factory() as db:
            async with db.begin():
                messages = await get_messages(db, session_id)
                for msg in messages:
                    payload = dict(msg.data) if msg.data else {}
                    if payload.get("role") == "assistant" and not msg.parts:
                        await db.delete(msg)
    except Exception:
        if not _retried:
            logger.warning("Retrying empty assistant cleanup for session %s", session_id)
            await _delete_empty_assistant_messages(
                session_factory, session_id, _retried=True
            )
        else:
            logger.error(
                "Failed to clean empty assistant messages for session %s after retry",
                session_id,
            )


async def _persist_tool_error(
    session_factory: async_sessionmaker[AsyncSession],
    assistant_msg_id: str,
    session_id: str,
    tool_name: str,
    call_id: str,
    tool_args: dict[str, Any],
    error_msg: str,
) -> None:
    """Persist a tool error part to the database."""
    async with session_factory() as db:
        async with db.begin():
            await create_part(
                db,
                message_id=assistant_msg_id,
                session_id=session_id,
                data={
                    "type": "tool",
                    "tool": tool_name,
                    "call_id": call_id,
                    "state": {
                        "status": "error",
                        "input": tool_args,
                        "output": error_msg,
                    },
                },
            )


async def _update_tool_part_error(
    session_factory: async_sessionmaker[AsyncSession],
    part_id: str,
    tool_name: str,
    call_id: str,
    tool_args: dict[str, Any],
    error_msg: str,
) -> None:
    """Update an existing tool part to error state. Logs warning on failure."""
    try:
        async with session_factory() as db:
            async with db.begin():
                await update_part_data(
                    db,
                    part_id,
                    {
                        "type": "tool",
                        "tool": tool_name,
                        "call_id": call_id,
                        "state": {"status": "error", "input": tool_args, "output": error_msg},
                    },
                )
    except Exception:
        logger.warning("Failed to persist error state for tool %s", tool_name)
