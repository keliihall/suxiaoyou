"""Restricted model tools for inspecting and reporting a persistent Goal."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.i18n import Language, localize
from app.models.goal_run import GoalRun
from app.models.message import Part
from app.models.session_input import SessionInput
from app.models.todo import Todo
from app.schemas.goal import GoalResponse
from app.session.goal_manager import (
    GoalControlError,
    GoalNotFoundError,
    GoalRevisionConflictError,
    get_session_goal,
    transition_goal_status,
)
from app.tool.base import ToolDefinition, ToolResult
from app.tool.context import ToolContext
from app.tool.workspace import WorkspaceViolation, resolve_and_validate


def _factory(ctx: ToolContext):
    app_state = getattr(ctx, "_app_state", None) or {}
    return app_state.get("session_factory")


def _public_goal(goal: Any) -> dict[str, Any]:
    return GoalResponse.model_validate(goal).model_dump(mode="json")


def _status_title(status: str, language: Language) -> str:
    """Render a human-readable Goal status without translating protocol data."""

    labels = {
        "active": ("执行中", "active"),
        "paused": ("已暂停", "paused"),
        "blocked": ("已阻塞", "blocked"),
        "usage_limited": ("用量受限", "usage limited"),
        "budget_limited": ("预算受限", "budget limited"),
        "complete": ("已完成", "complete"),
    }
    zh, en = labels.get(status, (status, status))
    return localize(language, f"目标：{zh}", f"Goal: {en}")


def _goal_control_error(exc: GoalControlError, ctx: ToolContext) -> str:
    """Keep manager exception text from leaking the wrong process language."""

    if isinstance(exc, GoalRevisionConflictError):
        return ctx.tr(
            (
                "目标修订号已变更（预期 "
                f"{exc.expected_revision}，当前 {exc.current_revision}）；"
                "请重新读取目标后再试。"
            ),
            (
                "Goal revision changed "
                f"(expected {exc.expected_revision}, current {exc.current_revision}); "
                "read the Goal again before retrying."
            ),
        )
    if isinstance(exc, GoalNotFoundError):
        return ctx.tr(
            "当前目标已不存在。",
            "The active Goal no longer exists.",
        )
    return ctx.tr(
        "目标状态更新未通过服务器校验；请重新读取当前目标后再试。",
        f"Goal status update failed server validation: {exc}",
    )


class GetGoalTool(ToolDefinition):
    @property
    def id(self) -> str:
        return "get_goal"

    @property
    def description(self) -> str:
        return (
            "Read the current persistent Goal, its exact revision, budgets, "
            "status, blockers, and completion contract. This tool never changes it."
        )

    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        del args
        session_factory = _factory(ctx)
        if session_factory is None:
            return ToolResult(
                error=ctx.tr("目标存储不可用。", "Goal storage is unavailable.")
            )
        async with session_factory() as db:
            goal = await get_session_goal(
                db,
                ctx.goal_session_id or ctx.session_id,
            )
        if goal is None:
            return ToolResult(
                output=ctx.tr(
                    "当前会话尚未设置目标。",
                    "No Goal is set for this session.",
                ),
                title=ctx.tr("无目标", "No Goal"),
            )
        snapshot = _public_goal(goal)
        return ToolResult(
            output=json.dumps(snapshot, ensure_ascii=False, indent=2),
            title=_status_title(goal.status, ctx.language),
            metadata={"goal": snapshot},
        )


class UpdateGoalTool(ToolDefinition):
    @property
    def id(self) -> str:
        return "update_goal"

    @property
    def description(self) -> str:
        return (
            "Report that the current persistent Goal is complete or genuinely "
            "blocked. Completion requires current-revision evidence and passes "
            "server validation. You cannot edit, pause, resume, clear, or raise "
            "the Goal budget with this tool."
        )

    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["complete", "blocked"],
                },
                "expected_revision": {"type": "integer", "minimum": 1},
                "summary": {"type": "string"},
                "evidence": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "criterion": {"type": "string"},
                            "evidence": {"type": "string"},
                            "path": {"type": "string"},
                            "call_id": {"type": "string"},
                        },
                        "required": ["criterion", "evidence"],
                    },
                },
                "blocker_code": {"type": "string"},
                "blocker_message": {"type": "string"},
            },
            "required": ["status", "expected_revision", "summary", "evidence"],
        }

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session_factory = _factory(ctx)
        if session_factory is None:
            return ToolResult(
                error=ctx.tr("目标存储不可用。", "Goal storage is unavailable.")
            )
        if ctx.invocation_source != "goal" or ctx.goal_id is None:
            return ToolResult(
                error=ctx.tr(
                    "只有当前正在执行目标的主运行器才能上报目标状态。",
                    "Only the active Goal runner may report Goal status.",
                )
            )
        if ctx.goal_session_id not in {None, ctx.session_id}:
            return ToolResult(
                error=ctx.tr(
                    "目标子代理可以收集证据，但只有主运行器才能上报最终状态。",
                    (
                        "A Goal sub-agent may gather evidence, but only the root "
                        "runner may report final status."
                    ),
                )
            )

        status = str(args["status"])
        summary = str(args.get("summary") or "").strip()
        evidence = args.get("evidence")
        if not summary:
            return ToolResult(
                error=ctx.tr(
                    "必须提供非空的目标摘要。",
                    "A non-empty Goal summary is required.",
                )
            )
        if not isinstance(evidence, list) or not evidence:
            return ToolResult(
                error=ctx.tr(
                    "至少需要一条结构化证据。",
                    "At least one structured evidence item is required.",
                )
            )

        job = getattr(ctx, "_job", None)
        if job is None or not hasattr(job, "session_input_lock"):
            return ToolResult(
                error=ctx.tr(
                    "缺少主运行器的输入接收锁，无法确定目标最终状态。",
                    (
                        "Goal status cannot be finalized without the root input "
                        "admission lock."
                    ),
                )
            )

        try:
            # Serialize the final queued-input observation with POST
            # /chat/inputs. Any input committed first must be processed before
            # the model may declare completion/blocking; once the terminal
            # transition commits, close admission before releasing the lock.
            async with job.session_input_lock:
                async with session_factory() as db:
                    async with db.begin():
                        goal_session_id = ctx.goal_session_id or ctx.session_id
                        goal = await get_session_goal(db, goal_session_id)
                        if goal is None or goal.id != ctx.goal_id:
                            return ToolResult(
                                error=ctx.tr(
                                    "当前目标已不存在。",
                                    "The active Goal no longer exists.",
                                )
                            )
                        if goal.status != "active":
                            return ToolResult(
                                error=ctx.tr(
                                    f"目标已处于 {goal.status} 状态。",
                                    f"Goal status is already {goal.status}.",
                                )
                            )

                        pending_input = (
                            await db.execute(
                                select(SessionInput.id)
                                .where(
                                    SessionInput.session_id == goal_session_id,
                                    SessionInput.status == "queued",
                                )
                                .limit(1)
                            )
                        ).scalar_one_or_none()
                        if pending_input is not None:
                            return ToolResult(
                                error=ctx.tr(
                                    (
                                        "已有真实用户输入排队，必须先处理该输入，"
                                        "才能确定目标最终状态。"
                                    ),
                                    (
                                        "A real user input is queued and must be "
                                        "processed before the Goal can be finalized."
                                    ),
                                )
                            )

                        if status == "complete":
                            validation_error = await _validate_completion(
                                db,
                                goal_id=goal.id,
                                run_id=ctx.goal_run_id,
                                session_id=goal_session_id,
                                current_call_id=ctx.call_id,
                                evidence=evidence,
                                workspace=ctx.workspace,
                                completion_contract=(
                                    goal.definition_of_done or goal.objective
                                ),
                                language=ctx.language,
                            )
                            if validation_error:
                                return ToolResult(error=validation_error)
                            goal = await transition_goal_status(
                                db,
                                goal_id=goal.id,
                                expected_revision=int(args["expected_revision"]),
                                target_status="complete",
                                completion_summary=summary,
                                completion_evidence=evidence,
                            )
                        else:
                            blocker_code = str(
                                args.get("blocker_code") or "model_reported_blocker"
                            ).strip()[:80]
                            blocker_message = str(
                                args.get("blocker_message") or summary
                            ).strip()
                            if not blocker_message:
                                return ToolResult(
                                    error=ctx.tr(
                                        "必须提供具体的阻碍说明。",
                                        "A concrete blocker is required.",
                                    )
                                )
                            goal = await transition_goal_status(
                                db,
                                goal_id=goal.id,
                                expected_revision=int(args["expected_revision"]),
                                target_status="blocked",
                                blocker_code=blocker_code,
                                blocker_message=blocker_message,
                                completion_evidence=evidence,
                            )
                job.close_session_input_admission()
                job.close_execution_admission()
        except GoalControlError as exc:
            return ToolResult(error=_goal_control_error(exc, ctx))

        snapshot = _public_goal(goal)
        return ToolResult(
            output=(
                ctx.tr("目标完成状态已接受。", "Goal completion accepted.")
                if status == "complete"
                else ctx.tr(
                    "目标阻碍已记录，自主续跑将停止。",
                    "Goal blocker recorded; autonomous continuation will stop.",
                )
            ),
            title=(
                ctx.tr("目标已完成", "Goal complete")
                if status == "complete"
                else ctx.tr("目标已阻塞", "Goal blocked")
            ),
            metadata={"goal": snapshot, "goal_status_updated": True},
        )


async def _validate_completion(
    db,
    *,
    goal_id: str,
    run_id: str | None,
    session_id: str,
    current_call_id: str,
    evidence: list[Any],
    workspace: str | None,
    completion_contract: str,
    language: Language,
) -> str | None:
    incomplete = list(
        (
            await db.execute(
                select(Todo).where(
                    Todo.goal_id == goal_id,
                    Todo.status.in_(("pending", "in_progress")),
                )
            )
        ).scalars().all()
    )
    if incomplete:
        return localize(
            language,
            "目标待办中仍有未完成项，当前无法完成目标。",
            "Goal cannot complete while Goal Todo items remain unfinished.",
        )

    if run_id is None:
        return localize(
            language,
            "完成目标需要一个正在执行的 GoalRun。",
            "Goal completion requires an active GoalRun.",
        )
    run = await db.get(GoalRun, run_id)
    if run is None or run.goal_id != goal_id or run.status not in {
        "running",
        "waiting_user",
    }:
        return localize(
            language,
            "完成请求来自过期或未激活的 GoalRun。",
            "Goal completion came from a stale or inactive GoalRun.",
        )

    # A still-running tool means the completion claim raced ahead of a side
    # effect. The runner must reach a clean safe boundary first.
    part_statement = select(Part).where(Part.session_id == session_id)
    if run.time_started is not None:
        part_statement = part_statement.where(Part.time_created >= run.time_started)
    parts = list(
        (
            await db.execute(part_statement.order_by(Part.time_created.asc()))
        ).scalars().all()
    )
    if any(
        (part.data or {}).get("type") == "tool"
        and (part.data or {}).get("call_id") != current_call_id
        and ((part.data or {}).get("state") or {}).get("status") == "running"
        for part in parts
    ):
        return localize(
            language,
            "仍有工具调用在运行，当前无法完成目标。",
            "Goal cannot complete while a tool call is still running.",
        )

    step_finishes = [
        part
        for part in parts
        if (part.data or {}).get("type") == "step-finish"
    ]
    if step_finishes and str((step_finishes[-1].data or {}).get("reason")) == "error":
        return localize(
            language,
            "最新步骤存在未处理错误，当前无法完成目标。",
            "Goal cannot complete with an unhandled error in the latest step.",
        )

    verified_criteria: list[str] = []
    successful_tools = {
        str((part.data or {}).get("call_id")): part
        for part in parts
        if (part.data or {}).get("type") == "tool"
        and ((part.data or {}).get("state") or {}).get("status") == "completed"
        and (part.data or {}).get("call_id")
        and (part.data or {}).get("call_id") != current_call_id
    }

    for item in evidence:
        if not isinstance(item, dict):
            return localize(
                language,
                "每条证据都必须是结构化对象。",
                "Each evidence item must be an object.",
            )
        criterion = str(item.get("criterion") or "").strip()
        proof = str(item.get("evidence") or "").strip()
        if not criterion or not proof:
            return localize(
                language,
                "每条证据都必须包含完成条件和证据说明。",
                "Each evidence item needs criterion and evidence text.",
            )
        verified_criteria.append(criterion)
        path_value = item.get("path")
        call_id = str(item.get("call_id") or "").strip()
        if not path_value and not call_id:
            return localize(
                language,
                (
                    "完成证据必须引用已验证的工作区文件路径，"
                    "或成功工具调用的 call_id。"
                ),
                (
                    "Completion evidence must reference either a verified workspace "
                    "file path or a successful tool call_id."
                ),
            )
        if path_value:
            if not workspace:
                return localize(
                    language,
                    "文件证据需要当前存在可信工作区。",
                    "File evidence requires an active trusted workspace.",
                )
            try:
                resolved = resolve_and_validate(str(path_value), workspace)
                path = Path(resolved).expanduser().resolve(strict=True)
            except WorkspaceViolation:
                return localize(
                    language,
                    f"证据路径位于当前工作区之外：{path_value}",
                    f"Evidence path is outside the active workspace: {path_value}",
                )
            except (OSError, RuntimeError):
                return localize(
                    language,
                    f"证据路径不存在：{path_value}",
                    f"Evidence path does not exist: {path_value}",
                )
            if not path.is_file():
                return localize(
                    language,
                    f"证据路径不是文件：{path_value}",
                    f"Evidence path is not a file: {path_value}",
                )
            try:
                file_info = path.stat()
            except OSError:
                return localize(
                    language,
                    f"无法读取证据文件：{path_value}",
                    f"Evidence file could not be read: {path_value}",
                )
            if file_info.st_size > 512 * 1024 * 1024:
                return localize(
                    language,
                    f"证据文件过大，无法验证：{path_value}",
                    f"Evidence file is too large to verify: {path_value}",
                )
            digest = hashlib.sha256()
            try:
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
            except OSError:
                return localize(
                    language,
                    f"无法读取证据文件：{path_value}",
                    f"Evidence file could not be read: {path_value}",
                )
            # Replace any model-supplied verification fields with facts
            # computed by the server at the completion boundary.
            item["path"] = str(path)
            item["verification"] = "server-file-sha256"
            item["sha256"] = digest.hexdigest()
            item["size_bytes"] = file_info.st_size
        if call_id:
            tool_part = successful_tools.get(call_id)
            if tool_part is None:
                return localize(
                    language,
                    (
                        "证据 call_id 未指向当前目标运行中的"
                        f"成功工具调用：{call_id}"
                    ),
                    (
                        "Evidence call_id does not reference a successful tool call "
                        f"from the active Goal run: {call_id}"
                    ),
                )
            data = tool_part.data or {}
            item["call_id"] = call_id
            item["tool"] = str(data.get("tool") or "unknown")
            item["verification"] = "server-successful-tool-call"

    contract_lines = [
        _normalize_contract_line(line)
        for line in re.split(r"[\n;；]+", completion_contract)
        if _normalize_contract_line(line)
    ]
    normalized_criteria = [_normalize_contract_line(value) for value in verified_criteria]
    for contract_line in contract_lines:
        if not any(
            criterion in contract_line or contract_line in criterion
            for criterion in normalized_criteria
            if criterion
        ):
            return localize(
                language,
                f"完成证据未覆盖当前完成条件：{contract_line}",
                (
                    "Completion evidence does not cover the current completion "
                    f"criterion: {contract_line}"
                ),
            )
    return None


def _normalize_contract_line(value: str) -> str:
    text = value.strip().casefold()
    while text[:1] in {"-", "*", "•", "·"}:
        text = text[1:].strip()
    return "".join(text.split())
