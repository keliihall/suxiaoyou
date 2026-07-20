"""Question tool — ask the user a question and wait for response.

Actually blocks until the user responds via POST /api/chat/respond,
matching OpenCode's behavior. Degrades gracefully in headless/test mode.

Supports two modes:
- Legacy: single ``question`` + ``options`` (strings).
- Multi-question: ``questions`` array with tabs, radio/checkbox, preview.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.tool.base import ToolDefinition, ToolResult
from app.tool.context import ToolContext

logger = logging.getLogger(__name__)


class QuestionTool(ToolDefinition):

    @property
    def id(self) -> str:
        return "question"

    @property
    def description(self) -> str:
        return (
            "Ask the user a question and wait for their response. "
            "Use this when you need clarification or user input to proceed. "
            "Supports single-question mode (question + options) or "
            "multi-question mode (questions array with tabs, radio/checkbox, and preview)."
        )

    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                # Legacy single-question mode
                "question": {
                    "type": "string",
                    "minLength": 1,
                    "description": "The question to ask the user (single-question mode)",
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of choices for single-question mode",
                },
                # Multi-question mode
                "questions": {
                    "type": "array",
                    "description": "Array of 1-4 questions (multi-question mode with tab UI)",
                    "minItems": 1,
                    "maxItems": 4,
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "minLength": 1,
                                "description": "The question text",
                            },
                            "header": {
                                "type": "string",
                                "description": "Tab label (max 12 chars)",
                                "minLength": 1,
                                "maxLength": 12,
                            },
                            "options": {
                                "type": "array",
                                "description": "2-4 selectable options",
                                "minItems": 2,
                                "maxItems": 4,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {
                                            "type": "string",
                                            "description": "Display text for this option",
                                        },
                                        "description": {
                                            "type": "string",
                                            "description": "Explanation of what this option means",
                                        },
                                        "preview": {
                                            "type": "string",
                                            "description": "Optional preview content (markdown) shown in side panel",
                                        },
                                    },
                                    "required": ["label"],
                                },
                            },
                            "multiSelect": {
                                "type": "boolean",
                                "description": "true = checkboxes (multiple answers), false = radio (single answer)",
                                "default": False,
                            },
                        },
                        "required": ["question", "header"],
                    },
                },
            },
            "anyOf": [
                {"required": ["question"]},
                {"required": ["questions"]},
            ],
        }

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raw_questions = args.get("questions")
        questions: list[dict[str, Any]] = []
        invalid_multi = False
        if isinstance(raw_questions, list) and raw_questions:
            for item in raw_questions:
                if not isinstance(item, dict):
                    invalid_multi = True
                    continue
                question_text = item.get("question")
                header_text = item.get("header")
                if not isinstance(question_text, str) or not question_text.strip():
                    invalid_multi = True
                    continue
                if not isinstance(header_text, str) or not header_text.strip():
                    invalid_multi = True
                    continue
                questions.append(
                    {
                        **item,
                        "question": question_text.strip(),
                        "header": header_text.strip(),
                    }
                )
        is_multi = bool(questions) and not invalid_multi

        # Legacy fields
        raw_question = args.get("question", "")
        question = raw_question.strip() if isinstance(raw_question, str) else ""
        options = args.get("options", [])

        if invalid_multi or (not is_multi and not question):
            return ToolResult(
                error=ctx.tr(
                    "提问内容为空或格式不完整，请明确问题后重新提问。",
                    "The question was empty or incomplete. Ask again with explicit question text.",
                ),
                title=ctx.tr("提问内容无效", "Invalid question"),
                metadata={"code": "invalid_question_payload"},
            )

        # Access the GenerationJob for wait_for_response
        job = getattr(ctx, "_job", None)
        should_wait = job is not None and job.interactive

        if should_wait:
            job.register_response_request(
                ctx.call_id,
                prompt_type="question",
                timeout=300.0,
                tool_call_id=ctx.call_id,
                tool=self.id,
            )

        # Publish question event to SSE stream
        if ctx._publish_fn:
            payload: dict[str, Any] = {
                "call_id": ctx.call_id,
                "session_id": ctx.session_id,
            }
            if is_multi:
                payload["questions"] = questions
                payload["arguments"] = {"questions": questions}
            else:
                payload["question"] = question
                payload["options"] = options
                payload["arguments"] = {
                    "question": question,
                    "options": options,
                }
            ctx._publish_fn("question", payload)

        # If no job context or not interactive — degrade gracefully
        summary = (
            ctx.tr(f"[多问题] {len(questions)} 个问题", f"[Multiple questions] {len(questions)} questions")
            if is_multi
            else ctx.tr(f"已提问：{question}", f"Asked: {question}")
        )
        if not should_wait:
            return ToolResult(
                output=ctx.tr(f"[没有用户连接] {summary}", f"[No user connected] {summary}"),
                title=ctx.tr("提问（无监听）", "Question (no listener)"),
                metadata={"questions": questions} if is_multi else {"question": question, "options": options},
            )

        # Block until user responds via POST /api/chat/respond
        await ctx.set_goal_waiting_user(
            True,
            reason="question_required",
            message="The Goal is waiting for the user's answer",
        )
        try:
            response = await job.wait_for_response(ctx.call_id, timeout=300.0)

            if is_multi:
                # Multi-question response is JSON: Record<str, str>
                try:
                    answers = json.loads(str(response))
                    formatted = "\n".join(
                        f"Q: {q}\nA: {a}" for q, a in answers.items()
                    )
                except (json.JSONDecodeError, AttributeError):
                    answers = response
                    formatted = str(response)
                return ToolResult(
                    output=formatted,
                    title=ctx.tr(
                        f"用户回答了 {len(answers) if isinstance(answers, dict) else 1} 个问题",
                        f"User answered {len(answers) if isinstance(answers, dict) else 1} questions",
                    ),
                    metadata={"questions": questions, "answers": answers},
                )
            else:
                return ToolResult(
                    output=str(response),
                    title=ctx.tr(f"用户回答：{str(response)[:100]}", f"User answered: {str(response)[:100]}"),
                    metadata={"question": question, "answer": response},
                )
        except TimeoutError:
            await ctx.block_goal(
                reason="question_timeout",
                message="The user did not answer the Goal question before it expired",
            )
            return ToolResult(
                output=ctx.tr("（用户在 5 分钟内没有回复）", "(The user did not respond within 5 minutes)"),
                error=ctx.tr("提问超时：用户未回复", "Question timed out: the user did not respond"),
            )
        finally:
            await ctx.set_goal_waiting_user(
                False,
                reason="question_required",
                message="",
            )
