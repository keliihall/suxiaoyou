"""Two-phase context compaction.

Phase 1 (prune): Mark old tool outputs as truncated
  - Skip last 2 turns
  - Protect first 40K tokens of tool output
  - Mark rest as compacted → "[truncated]"

Phase 2 (summarize): LLM generates structured summary
  Goal → Instructions → Discoveries → Accomplished → Relevant files

Auto-continue: Append a process-language-aware continuation instruction
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.agent import AgentRegistry
from app.i18n import localize, synthetic_process_instruction
from app.models.message import Message, Part
from app.provider.registry import ProviderRegistry
from app.session.manager import create_message, create_part
from app.streaming.events import (
    COMPACTED,
    COMPACTION_ERROR,
    COMPACTION_PHASE,
    COMPACTION_PROGRESS,
    COMPACTION_START,
    SSEEvent,
)
from app.streaming.manager import GenerationJob
from app.utils.token import estimate_tokens

# Re-use cost/budget helpers from session utils
from app.session.utils import calculate_step_cost as _calculate_step_cost
from app.session.utils import compute_usable_context_window

logger = logging.getLogger(__name__)

# Config
PROTECTED_TOKEN_BUDGET = 40_000  # Protect this many tokens of tool output
SKIP_RECENT_TURNS = 2  # Don't compact the last N assistant messages
PROTECTED_TOOLS = frozenset({"skill"})  # Never prune these tool outputs
AUTO_COMPACT_CONTEXT_RATIO = 0.85  # Proactively compact before the hard context edge


@dataclass
class CompactionResult:
    pruned_parts: int = 0
    summary: str | None = None
    summary_visible: bool = False


async def run_compaction(
    session_id: str,
    *,
    job: GenerationJob,
    session_factory: async_sessionmaker[AsyncSession],
    provider_registry: ProviderRegistry,
    agent_registry: AgentRegistry,
    model_id: str | None = None,
    visible_summary: bool = False,
) -> CompactionResult:
    """Run two-phase compaction on a session's history."""
    logger.info("Running compaction on session %s", session_id)
    result = CompactionResult(summary_visible=visible_summary)

    if job.abort_event.is_set():
        logger.info("Compaction aborted before start for session %s", session_id)
        return result
    if job.goal_id is not None:
        from app.session.goal_guard import goal_job_execution_allowed

        allowed, reason = await goal_job_execution_allowed(session_factory, job)
        if not allowed:
            logger.info(
                "Goal compaction denied before start for session %s: %s",
                session_id,
                reason,
            )
            return result

    # Signal compaction start
    job.publish(SSEEvent(COMPACTION_START, {
        "session_id": session_id,
        "phases": ["prune", "summarize"],
    }))

    # Phase 1: Prune old tool outputs
    job.publish(SSEEvent(COMPACTION_PHASE, {
        "session_id": session_id, "phase": "prune", "status": "started",
    }))
    result.pruned_parts = await _phase1_prune(session_id, session_factory=session_factory)
    job.publish(SSEEvent(COMPACTION_PHASE, {
        "session_id": session_id, "phase": "prune", "status": "completed",
    }))

    if job.abort_event.is_set():
        logger.info("Compaction aborted after prune for session %s", session_id)
        return result

    # Phase 2: Generate summary
    job.publish(SSEEvent(COMPACTION_PHASE, {
        "session_id": session_id, "phase": "summarize", "status": "started",
    }))
    result.summary = await _phase2_summarize(
        session_id,
        job=job,
        session_factory=session_factory,
        provider_registry=provider_registry,
        agent_registry=agent_registry,
        model_id=model_id,
    )
    job.publish(SSEEvent(COMPACTION_PHASE, {
        "session_id": session_id, "phase": "summarize", "status": "completed",
    }))

    if job.abort_event.is_set():
        logger.info("Compaction aborted during summarize for session %s", session_id)
        return result

    if result.summary:
        summary_header = localize(job.language, "[上下文摘要]", "[Context Summary]")
        continuation = synthetic_process_instruction(
            job.language,
            "如有后续步骤，请继续执行。",
            "Continue if you have next steps.",
        )
        # Auto compaction keeps the injected summary invisible so it doesn't
        # interrupt the normal assistant flow. Manual compaction should surface
        # the summary so the user can see what the AI actually compressed.
        async with session_factory() as db:
            async with db.begin():
                msg = await create_message(
                    db,
                    session_id=session_id,
                    data={
                        "role": "assistant" if visible_summary else "user",
                        "agent": "compaction",
                        "system": True,
                        **({"summary": True} if visible_summary else {}),
                    },
                )
                await create_part(
                    db,
                    message_id=msg.id,
                    session_id=session_id,
                    data={
                        "type": "text",
                        "text": (
                            f"{summary_header}\n\n{result.summary}"
                            if visible_summary
                            else (
                                f"{summary_header}\n\n{result.summary}\n\n"
                                f"{continuation}"
                            )
                        ),
                        "synthetic": True,
                    },
                )
                await create_part(
                    db,
                    message_id=msg.id,
                    session_id=session_id,
                    data={"type": "compaction", "auto": True},
                )

    job.publish(SSEEvent(COMPACTED, {
        "session_id": session_id,
        "summary_created": bool(result.summary),
        "pruned_parts": result.pruned_parts,
        "visible_summary": visible_summary,
    }))
    logger.info("Compaction complete for session %s", session_id)
    return result


async def _phase1_prune(
    session_id: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """Mark old tool outputs as truncated to reduce context size."""
    pruned_parts = 0
    async with session_factory() as db:
        async with db.begin():
            # Get all messages ordered by time
            stmt = (
                select(Message)
                .where(Message.session_id == session_id)
                .order_by(Message.time_created.asc())
            )
            result = await db.execute(stmt)
            messages = list(result.scalars().all())

            if len(messages) <= SKIP_RECENT_TURNS * 2:
                return 0  # Not enough history to prune

            # Skip the last N turns (each turn = user + assistant)
            cutoff = len(messages) - (SKIP_RECENT_TURNS * 2)
            messages_to_prune = messages[:cutoff]

            token_budget = PROTECTED_TOKEN_BUDGET

            for msg in messages_to_prune:
                # Get tool parts for this message
                part_stmt = (
                    select(Part)
                    .where(Part.message_id == msg.id)
                    .order_by(Part.time_created.asc())
                )
                part_result = await db.execute(part_stmt)
                parts = list(part_result.scalars().all())

                for part in parts:
                    if not part.data or part.data.get("type") != "tool":
                        continue

                    # Never prune protected tool outputs (e.g. skill)
                    tool_name = part.data.get("tool", "")
                    if tool_name in PROTECTED_TOOLS:
                        continue

                    state = part.data.get("state", {})
                    output = state.get("output", "")
                    if not output or state.get("time_compacted"):
                        continue

                    output_tokens = estimate_tokens(output)

                    if token_budget > 0:
                        token_budget -= output_tokens
                        continue  # Protected

                    # Mark as compacted
                    updated_data = dict(part.data)
                    updated_state = dict(state)
                    updated_state["output"] = "[truncated]"
                    updated_state["time_compacted"] = "auto"
                    updated_data["state"] = updated_state
                    part.data = updated_data
                    pruned_parts += 1

            await db.flush()
    return pruned_parts


async def _phase2_summarize(
    session_id: str,
    *,
    job: GenerationJob,
    session_factory: async_sessionmaker[AsyncSession],
    provider_registry: ProviderRegistry,
    agent_registry: AgentRegistry,
    model_id: str | None = None,
) -> str | None:
    """Generate a structured summary of the conversation."""
    compaction_agent = agent_registry.get("compaction")
    if not compaction_agent or not compaction_agent.system_prompt:
        return None

    # Find a model
    if not model_id:
        models = provider_registry.all_models()
        if not models:
            return None
        model_id = models[0].id

    resolved = provider_registry.resolve_model(model_id)
    if not resolved:
        return None

    provider, model_info = resolved

    # Load conversation for summarization
    from app.session.manager import get_message_history_for_llm

    async with session_factory() as db:
        async with db.begin():
            llm_messages = await get_message_history_for_llm(
                db,
                session_id,
                provider_id=provider.id,
                model_id=model_id,
                process_language=job.language,
            )

    if not llm_messages:
        return None

    max_tokens = 4096
    if job.goal_id is not None:
        from app.config import get_settings
        from app.session.goal_guard import (
            read_goal_budget_gate,
            read_goal_execution_gate,
        )

        root_session_id = job.goal_session_id or job.session_id
        gate = await read_goal_execution_gate(
            session_factory,
            session_id=root_session_id,
            goal_id=job.goal_id,
            goal_run_id=job.goal_run_id,
        )
        if not gate.allowed:
            logger.info(
                "Goal compaction Provider call denied for session %s: %s",
                session_id,
                gate.reason,
            )
            return None
        tokens_used, cost_used = job.goal_run_usage
        budget = await read_goal_budget_gate(
            session_factory,
            session_id=root_session_id,
            goal_id=job.goal_id,
            local_tokens_used=tokens_used,
            local_cost_microusd=cost_used,
            warning_ratio=get_settings().goal_budget_warning_ratio,
        )
        if not budget.allowed:
            logger.info(
                "Goal compaction Provider call denied by %s for session %s",
                budget.reason_code,
                session_id,
            )
            return None
        if budget.token_remaining is not None:
            max_tokens = max(1, min(max_tokens, budget.token_remaining))

    # Ask compaction agent to summarize
    try:
        summary_prompt = synthetic_process_instruction(
            job.language,
            (
                "总结上方对话并遵循 system prompt 指定的格式。为了后续交接，摘要语言"
                "必须保持为最近一条真实用户消息的语言。"
            ),
            (
                "Summarize the conversation above and follow the format in your "
                "system prompt. Preserve the language of the latest genuine "
                "user-authored message for continuation handoff."
            ),
        )
        messages = llm_messages + [{"role": "user", "content": summary_prompt}]

        summary = ""
        usage_data: dict[str, Any] = {}
        last_reported = 0
        first_chunk = None
        if job.goal_id is not None:
            # Pair the final durable checks and the Provider's first iterator
            # step with the same runtime lock used by pause/edit/archive. A
            # control operation therefore wins before this call, or observes
            # that the compaction request has already started.
            async with job.execution_admission_lock:
                if not job.execution_admission_open:
                    return None
                root_session_id = job.goal_session_id or job.session_id
                gate = await read_goal_execution_gate(
                    session_factory,
                    session_id=root_session_id,
                    goal_id=job.goal_id,
                    goal_run_id=job.goal_run_id,
                )
                if not gate.allowed:
                    return None
                tokens_used, cost_used = job.goal_run_usage
                budget = await read_goal_budget_gate(
                    session_factory,
                    session_id=root_session_id,
                    goal_id=job.goal_id,
                    local_tokens_used=tokens_used,
                    local_cost_microusd=cost_used,
                    warning_ratio=get_settings().goal_budget_warning_ratio,
                )
                if not budget.allowed:
                    return None
                if budget.token_remaining is not None:
                    max_tokens = max(1, min(max_tokens, budget.token_remaining))
                provider_stream = provider.stream_chat(
                    model_id,
                    messages,
                    system=compaction_agent.system_prompt,
                    max_tokens=max_tokens,
                )
                try:
                    first_chunk = await anext(provider_stream)
                except StopAsyncIteration:
                    first_chunk = None

            async def admitted_chunks():
                if first_chunk is not None:
                    yield first_chunk
                async for remaining_chunk in provider_stream:
                    yield remaining_chunk

            chunks = admitted_chunks()
        else:
            chunks = provider.stream_chat(
                model_id,
                messages,
                system=compaction_agent.system_prompt,
                max_tokens=max_tokens,
            )

        async for chunk in chunks:
            if job.abort_event.is_set():
                logger.info("Compaction summarize stream aborted for session %s", session_id)
                return None
            if chunk.type == "text-delta":
                summary += chunk.data.get("text", "")
                # Emit progress every ~200 chars to avoid flooding
                if len(summary) - last_reported >= 200:
                    job.publish(SSEEvent(COMPACTION_PROGRESS, {
                        "session_id": session_id,
                        "phase": "summarize",
                        "chars": len(summary),
                    }))
                    last_reported = len(summary)
            elif chunk.type == "usage":
                usage_data = chunk.data

        # Persist usage as a synthetic assistant message so the usage API picks it up
        if usage_data:
            cost = _calculate_step_cost(usage_data, model_info)
            goal_tokens = sum(
                max(0, int(usage_data.get(key, 0) or 0))
                for key in ("input", "output", "reasoning", "cache_read")
            )
            goal_cost_microusd = max(0, round(cost * 1_000_000))
            async with session_factory() as db:
                async with db.begin():
                    usage_message = await create_message(
                        db,
                        session_id=session_id,
                        data={
                            "role": "assistant",
                            "agent": "compaction",
                            "system": True,
                            "goal_run_id": job.goal_run_id,
                            "cost": cost,
                            "tokens": usage_data,
                            "model_id": model_id,
                            "provider_id": provider.id,
                        },
                    )
                    if job.goal_id is not None:
                        if job.goal_run_id is None:
                            raise RuntimeError(
                                "Goal compaction usage has no durable run identity"
                            )
                        from app.session.goal_manager import record_goal_run_usage

                        await record_goal_run_usage(
                            db,
                            goal_run_id=job.goal_run_id,
                            source_kind="compaction",
                            source_key=f"compaction:{usage_message.id}",
                            tokens_used=goal_tokens,
                            cost_used_microusd=goal_cost_microusd,
                        )
            if job.goal_id is not None:
                job.record_goal_usage(
                    tokens=goal_tokens,
                    cost_microusd=goal_cost_microusd,
                )
            logger.info(
                "Compaction usage: %s tokens, $%.6f (session %s)",
                usage_data.get("total", 0), cost, session_id,
            )

        return summary.strip() if summary.strip() else None

    except Exception as e:
        logger.warning("Failed to generate compaction summary: %s", e)
        job.publish(SSEEvent(COMPACTION_ERROR, {
            "session_id": session_id,
            "message": "Context compression failed. Consider starting a new chat.",
        }))
        return None


def should_compact(
    usage: dict[str, Any],
    model_max_context: int,
    *,
    model_max_output: int | None = None,
    reserved: int | None = None,
    threshold_ratio: float = AUTO_COMPACT_CONTEXT_RATIO,
) -> bool:
    """Check if context usage warrants compaction.

    Mirrors OpenCode ``SessionCompaction.isOverflow()`` budget shape:
      - reserved defaults to ``min(20_000, model_max_output)``
      - usable = model_max_context - output_budget - reserved

    Also applies a proactive threshold so compaction starts around 85% of the
    provider-reported context window instead of waiting for the hard edge.
    """
    total_tokens = usage.get("total", 0)
    if not total_tokens:
        total_tokens = (
            usage.get("input", 0)
            + usage.get("output", 0)
            + usage.get("reasoning", 0)
            + usage.get("cache_read", 0)
        )
    usable = compute_usable_context_window(
        model_max_context,
        model_max_output=model_max_output,
        reserved=reserved,
    )
    threshold = min(usable, int(model_max_context * threshold_ratio))
    return total_tokens >= threshold and threshold > 0
