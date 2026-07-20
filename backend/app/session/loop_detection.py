"""Two-stage loop detection for agent tool calls.

Replaces the simple "block after N identical calls" approach with a
warn-then-stop strategy inspired by DeerFlow's LoopDetectionMiddleware:

  1. Hash each set of tool calls (name + args, order-independent).
  2. Track recent hashes in a sliding window per session.
  3. After ``warn_threshold`` identical hashes → inject a warning message
     into the tool output so the LLM knows it's repeating itself.
  4. After ``hard_limit`` identical hashes → block the tool call entirely
     and force the agent to produce a final text answer.

This is strictly better than the old binary block/allow: the model gets a
chance to self-correct before being hard-stopped.  A separate response-scoped
failure circuit also removes a tool after a bounded number of failed
executions, preventing argument churn from evading the identical-call hash.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import OrderedDict, defaultdict
from dataclasses import dataclass

from app.i18n import Language, localize

logger = logging.getLogger(__name__)

# Defaults — read from Settings when the singleton is created (see bottom of file)
_DEFAULT_WINDOW_SIZE = 20
_DEFAULT_MAX_SESSIONS = 200

WARNING_MSG = (
    "[LOOP DETECTED] You are repeating the same tool calls with identical arguments. "
    "Stop calling tools, use the results already collected, and state any remaining "
    "blocker."
)
WARNING_MSG_ZH = (
    "[检测到循环] 正在使用相同参数重复调用同一工具。请停止调用工具，"
    "基于已有结果作答，并明确说明仍未解决的问题。"
)

HARD_STOP_MSG = (
    "[SAFETY STOP] Repeated tool calls exceeded the safety limit. Further execution "
    "was stopped; results produced earlier were preserved for review."
)
HARD_STOP_MSG_ZH = (
    "[安全停止] 重复工具调用已超过安全上限，系统已停止继续执行；"
    "此前产生的结果已保留，等待确认。"
)

WEB_FETCH_NON_PUBLIC_ERROR = "URL host resolves to a non-public network address"
WEB_FETCH_NON_PUBLIC_LIMIT = 3
WEB_FETCH_CIRCUIT_OPEN_MSG = (
    "Web fetch skipped: this response has already received 3 consecutive "
    "non-public-address policy blocks from web_fetch. Do not retry web_fetch "
    "in this response. Continue with web_search result summaries, another "
    "source that is already known to be accessible, or clearly explain that "
    "the source could not be verified."
)
WEB_FETCH_CIRCUIT_OPEN_MSG_ZH = (
    "网页抓取已跳过：本次回复中的 web_fetch 已连续 3 次因非公网地址策略被阻止。"
    "本次回复中不要再次调用 web_fetch；请改用 web_search 的结果摘要、已知可访问的"
    "其他来源，或明确说明该来源暂时无法核验。"
)
WEB_SEARCH_STREAM_LIMIT = 5
WEB_SEARCH_LIMIT_MSG = (
    "Web search skipped: this response has already submitted 5 custom "
    "web_search calls. Do not call web_search again in this response. "
    "Synthesize the results already collected and answer from them; if the "
    "available evidence is insufficient, clearly state the remaining gap."
)
WEB_SEARCH_LIMIT_MSG_ZH = (
    "网页搜索已跳过：本次回复已提交 5 次自定义 web_search 调用。请勿再次调用 "
    "web_search；请综合已有结果作答，证据不足时明确说明剩余缺口。"
)
TOOL_FAILURE_LIMIT = 3
WEB_SEARCH_FAILURE_LIMIT = 2
TOOL_FAILURE_CIRCUIT_OPEN_MSG = (
    "This tool failed repeatedly in this response and is now disabled for the "
    "remainder of the response. Do not retry it with different arguments. "
    "Use a different available tool or report the concrete blocker."
)
TOOL_FAILURE_CIRCUIT_OPEN_MSG_ZH = (
    "该工具在本次回复中已连续失败，现已停用至本次回复结束。请勿更换参数后重试；"
    "请改用其他可用工具，或报告具体阻碍。"
)


def web_fetch_circuit_scope(session_id: str, stream_id: str) -> str:
    """Return the per-generation key used by the web-fetch policy circuit.

    A conversation can span many real user turns.  Including the stream keeps
    repeated failures bounded inside one response (or one Goal stream) without
    poisoning a later turn in the same session.
    """

    return f"{session_id}:{stream_id}"


_TRANSIENT_TRANSACTION_RE = re.compile(
    r"(?:[A-Za-z]:[/\\]|/)[^\s\"']*execution-transactions[/\\][^\s\"']+"
    r"[/\\]tx-[^/\\\s\"']+[/\\]workspace",
)
_TRANSIENT_SCRATCH_RE = re.compile(
    r"(?:[A-Za-z]:[/\\]|/)[^\s\"']*\.suxiaoyou[/\\]sandbox"
    r"[/\\][^/\\\s\"']+",
)


def _normalise_tool_args(name: str, args: dict) -> dict:
    """Collapse per-call paths/timeouts that disguise the same command loop."""

    if name not in {"bash", "code_execute"}:
        return args
    normalised = dict(args)
    normalised.pop("timeout", None)
    text_key = "command" if name == "bash" else "code"
    value = normalised.get(text_key)
    if isinstance(value, str):
        value = _TRANSIENT_TRANSACTION_RE.sub("<workspace>", value)
        value = _TRANSIENT_SCRATCH_RE.sub("<temporary-execution-directory>", value)
        normalised[text_key] = value.strip()
    return normalised


def _hash_tool_call(name: str, args: dict) -> str:
    """Deterministic hash of a single tool call (name + args)."""
    blob = json.dumps(
        {"name": name, "args": _normalise_tool_args(name, args)},
        sort_keys=True,
        default=str,
    )
    return hashlib.md5(blob.encode()).hexdigest()[:12]


@dataclass
class LoopCheckResult:
    """Result of a loop detection check."""

    action: str  # "allow" | "warn" | "block"
    message: str | None = None  # Warning/block message to inject


class LoopDetector:
    """Per-session sliding-window loop detector with two-stage response.

    Usage::

        detector = LoopDetector()

        # In the tool execution loop:
        result = detector.check(session_id, tool_name, tool_args)
        if result.action == "block":
            # hard-stop: do not execute, force final answer
            ...
        elif result.action == "warn":
            # append result.message to tool output so LLM sees it
            ...
        else:
            # normal execution
            ...
    """

    def __init__(
        self,
        warn_threshold: int | None = None,
        hard_limit: int | None = None,
        window_size: int = _DEFAULT_WINDOW_SIZE,
        max_sessions: int = _DEFAULT_MAX_SESSIONS,
    ) -> None:
        from app.config import get_settings as _get_settings
        _s = _get_settings()
        if warn_threshold is None:
            warn_threshold = _s.loop_warn_threshold
        if hard_limit is None:
            hard_limit = _s.loop_hard_limit
        self.warn_threshold = warn_threshold
        self.hard_limit = hard_limit
        self.window_size = window_size
        self.max_sessions = max_sessions
        # Per-session tracking: OrderedDict for LRU eviction
        self._history: OrderedDict[str, list[str]] = OrderedDict()
        self._warned: dict[str, set[str]] = defaultdict(set)
        self._web_fetch_non_public_failures: dict[str, int] = defaultdict(int)
        self._web_fetch_circuit_open: set[str] = set()
        self._custom_web_search_submissions: dict[str, int] = defaultdict(int)
        self._tool_failures: dict[tuple[str, str], int] = defaultdict(int)
        self._tool_failure_circuit_open: set[tuple[str, str]] = set()

    def check(
        self,
        session_id: str,
        tool_name: str,
        tool_args: dict,
        *,
        language: Language | str = "en",
    ) -> LoopCheckResult:
        """Check a tool call for loop patterns.

        Returns a LoopCheckResult indicating whether to allow, warn, or block.
        """
        call_hash = _hash_tool_call(tool_name, tool_args)

        # Touch / create entry (move to end for LRU)
        if session_id in self._history:
            self._history.move_to_end(session_id)
        else:
            self._history[session_id] = []
            self._evict_if_needed()

        history = self._history[session_id]
        history.append(call_hash)
        if len(history) > self.window_size:
            history[:] = history[-self.window_size:]

        count = history.count(call_hash)

        if count >= self.hard_limit:
            logger.error(
                "Loop hard limit reached for session %s: %s called %d times",
                session_id, tool_name, count,
            )
            return LoopCheckResult(
                action="block",
                message=localize(language, HARD_STOP_MSG_ZH, HARD_STOP_MSG),
            )

        if count >= self.warn_threshold:
            warned = self._warned[session_id]
            if call_hash not in warned:
                warned.add(call_hash)
                logger.warning(
                    "Repetitive tool calls detected for session %s: %s (%d times)",
                    session_id, tool_name, count,
                )
                return LoopCheckResult(
                    action="warn",
                    message=localize(language, WARNING_MSG_ZH, WARNING_MSG),
                )

        return LoopCheckResult(action="allow")

    def is_web_fetch_circuit_open(self, scope_id: str) -> bool:
        """Return whether policy-blocked ``web_fetch`` calls should be skipped.

        Calls already submitted in the same concurrent batch are allowed to
        finish naturally.  The processor consults this state before accepting
        a later call, so opening this circuit never hard-stops the surrounding
        model response.
        """

        return scope_id in self._web_fetch_circuit_open

    def record_tool_result(
        self,
        scope_id: str,
        tool_name: str,
        *,
        success: bool,
        error: str | None = None,
    ) -> None:
        """Update response-scoped failure circuits from a completed result.

        The generic circuit prevents a model from cycling through cosmetically
        different arguments after a capability has repeatedly failed.  The
        narrower web-fetch circuit retains its SSRF-specific behavior.
        """

        normalized_tool = tool_name.casefold()
        tool_key = (scope_id, normalized_tool)
        if success:
            self._tool_failures.pop(tool_key, None)
            self._tool_failure_circuit_open.discard(tool_key)
        else:
            failures = self._tool_failures[tool_key] + 1
            self._tool_failures[tool_key] = failures
            failure_limit = (
                WEB_SEARCH_FAILURE_LIMIT
                if normalized_tool == "web_search"
                else TOOL_FAILURE_LIMIT
            )
            if failures >= failure_limit:
                self._tool_failure_circuit_open.add(tool_key)
                logger.warning(
                    "Opening tool failure circuit for response %s tool %s "
                    "after %d failures",
                    scope_id,
                    normalized_tool,
                    failures,
                )

        if normalized_tool != "web_fetch":
            return

        if success or WEB_FETCH_NON_PUBLIC_ERROR not in (error or ""):
            self._web_fetch_non_public_failures.pop(scope_id, None)
            self._web_fetch_circuit_open.discard(scope_id)
            return

        failures = self._web_fetch_non_public_failures[scope_id] + 1
        self._web_fetch_non_public_failures[scope_id] = failures
        if failures >= WEB_FETCH_NON_PUBLIC_LIMIT:
            self._web_fetch_circuit_open.add(scope_id)
            logger.warning(
                "Opening web_fetch non-public-address circuit for response %s "
                "after %d consecutive policy blocks",
                scope_id,
                failures,
            )

    def is_tool_failure_circuit_open(
        self,
        scope_id: str,
        tool_name: str,
    ) -> bool:
        """Return whether a repeatedly failing tool is disabled this response."""

        return (scope_id, tool_name.casefold()) in self._tool_failure_circuit_open

    def blocked_tools(self, scope_id: str) -> set[str]:
        """Return tool IDs hidden from the next provider step."""

        return {
            tool_name
            for candidate_scope, tool_name in self._tool_failure_circuit_open
            if candidate_scope == scope_id
        }

    def admit_custom_web_search(self, scope_id: str) -> bool:
        """Reserve one custom ``web_search`` slot for this response.

        This method is intentionally synchronous and called before the first
        await in tool handling.  Stream chunks are consumed serially, so a
        parallel batch cannot observe stale capacity and exceed the limit.
        Provider-native search is not counted here: it is initiated inside the
        model provider and retains its existing per-step display cap.
        """

        submitted = self._custom_web_search_submissions[scope_id]
        if submitted >= WEB_SEARCH_STREAM_LIMIT:
            return False
        self._custom_web_search_submissions[scope_id] = submitted + 1
        return True

    def reset(self, session_id: str | None = None) -> None:
        """Clear tracking state. If session_id given, clear only that session."""
        if session_id:
            self._history.pop(session_id, None)
            self._warned.pop(session_id, None)
            # Accept either an exact stream scope or a conversation session ID.
            # The latter clears all response-scoped circuits for that session.
            scope_prefix = f"{session_id}:"
            matching_scopes = {
                scope
                for scope in (
                    set(self._web_fetch_non_public_failures)
                    | self._web_fetch_circuit_open
                    | set(self._custom_web_search_submissions)
                )
                if scope == session_id or scope.startswith(scope_prefix)
            }
            for scope in matching_scopes:
                self._web_fetch_non_public_failures.pop(scope, None)
                self._web_fetch_circuit_open.discard(scope)
                self._custom_web_search_submissions.pop(scope, None)
            matching_tool_keys = {
                key
                for key in set(self._tool_failures) | self._tool_failure_circuit_open
                if key[0] == session_id or key[0].startswith(scope_prefix)
            }
            for key in matching_tool_keys:
                self._tool_failures.pop(key, None)
                self._tool_failure_circuit_open.discard(key)
        else:
            self._history.clear()
            self._warned.clear()
            self._web_fetch_non_public_failures.clear()
            self._web_fetch_circuit_open.clear()
            self._custom_web_search_submissions.clear()
            self._tool_failures.clear()
            self._tool_failure_circuit_open.clear()

    def _evict_if_needed(self) -> None:
        """Evict least recently used sessions if over the limit."""
        while len(self._history) > self.max_sessions:
            evicted_id, _ = self._history.popitem(last=False)
            self._warned.pop(evicted_id, None)
            scope_prefix = f"{evicted_id}:"
            matching_scopes = {
                scope
                for scope in (
                    set(self._web_fetch_non_public_failures)
                    | self._web_fetch_circuit_open
                    | set(self._custom_web_search_submissions)
                )
                if scope == evicted_id or scope.startswith(scope_prefix)
            }
            for scope in matching_scopes:
                self._web_fetch_non_public_failures.pop(scope, None)
                self._web_fetch_circuit_open.discard(scope)
                self._custom_web_search_submissions.pop(scope, None)
            matching_tool_keys = {
                key
                for key in set(self._tool_failures) | self._tool_failure_circuit_open
                if key[0] == evicted_id or key[0].startswith(scope_prefix)
            }
            for key in matching_tool_keys:
                self._tool_failures.pop(key, None)
                self._tool_failure_circuit_open.discard(key)


# Module-level singleton — shared across all generations
loop_detector = LoopDetector()
