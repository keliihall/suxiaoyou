"""Tool execution context."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from app.schemas.agent import AgentInfo
from app.i18n import Language, localize
from app.security.capabilities import InvocationSource


@dataclass
class ToolContext:
    """Context passed to every tool execution.

    Provides:
      - session/message identifiers
      - abort signaling
      - permission checking via ask()
      - metadata streaming to UI
      - full message history (read-only, mirrors OpenCode's Tool.Context.messages)
    """

    session_id: str
    message_id: str
    agent: AgentInfo
    call_id: str
    language: Language = "zh"
    abort_event: asyncio.Event = field(default_factory=asyncio.Event)
    workspace: str | None = None  # workspace directory restriction
    index_manager: Any | None = None  # FTS IndexManager; None when FTS disabled
    messages: list[dict[str, Any]] = field(default_factory=list)
    """Full LLM-formatted message history as of this tool call (read-only).

    Mirrors OpenCode's Tool.Context.messages field. Populated by SessionProcessor
    before each tool execution. Tools that need conversation context (e.g., task
    tool summarising prior work for a subagent) can read this field.
    """

    # Deferred-tools discovery state (shared reference with SessionPrompt)
    discovered_tools: set[str] | None = None

    # Immutable snapshot of the parent's effective permission rules.  Task
    # children append this snapshot after their own agent rules, so a child can
    # never gain a capability that the parent did not have.
    permission_rules: tuple[dict[str, Any], ...] = ()
    # Full server-owned compound snapshot. ``permission_rules`` remains as a
    # fail-closed compatibility fallback, while Goal subagents use this value
    # so intersection metadata cannot be washed away by delegation.
    permission_snapshot: dict[str, Any] | None = None

    # Canonical paths explicitly registered as file parts on this session.
    # Files inside ``workspace`` do not need to appear here; this narrow list
    # exists for read-only access to user-selected attachments that live
    # outside a project workspace.  Never populate it from model arguments.
    attachment_paths: frozenset[str] = frozenset()

    # Trusted root ingress, inherited by every child job.  Tools may read but
    # never derive this value from model arguments or PromptRequest JSON.
    invocation_source: InvocationSource = "unknown"
    invocation_source_id: str | None = None
    goal_id: str | None = None
    goal_run_id: str | None = None
    goal_session_id: str | None = None
    # Server-owned v1.1 persistence identities.  Tools may pass them through
    # to guarded journals but model arguments can never set them.
    root_turn_id: str | None = None
    turn_run_id: str | None = None
    checkpoint_id: str | None = None
    workspace_instance_id: str | None = None
    # Durable filesystem identity read from the server-owned WorkspaceInstance
    # row.  A checkpoint-aware mutation must match this token before it may
    # create a private stage or touch the visible workspace.
    workspace_identity_token: str | None = None

    # Callbacks set by the session processor
    _publish_fn: Callable[[str, dict[str, Any]], None] | None = None
    _ask_fn: Callable[[str, list[str]], Awaitable[bool]] | None = None
    _execution_guard_fn: Callable[[], Awaitable[tuple[bool, str | None]]] | None = None

    def publish_metadata(self, title: str | None = None, metadata: dict[str, Any] | None = None) -> None:
        """Stream metadata update to the UI (e.g., tool progress)."""
        if self._publish_fn:
            self._publish_fn("tool_metadata", {
                "call_id": self.call_id,
                "title": title,
                "metadata": metadata or {},
            })

    async def ask(self, permission: str, patterns: list[str] | None = None) -> bool:
        """Check permission. Raises RejectedError if denied.

        For 'allow' → returns True immediately.
        For 'ask' → publishes permission_request, waits for user.
        For 'deny' → raises RejectedError.
        """
        if self._ask_fn:
            return await self._ask_fn(permission, patterns or [])
        # Missing approval plumbing is never permission.  This is especially
        # important for headless tools and future plugins that call ctx.ask()
        # directly instead of going through SessionProcessor.
        return False

    async def execution_allowed(self) -> tuple[bool, str | None]:
        """Re-check server-owned admission immediately before a tool starts."""

        if self._execution_guard_fn is None:
            return True, None
        return await self._execution_guard_fn()

    async def set_goal_waiting_user(
        self,
        waiting: bool,
        *,
        reason: str,
        message: str,
    ) -> None:
        """Expose interactive tool waits as durable Goal state."""

        if self.goal_id is None:
            return
        app_state = getattr(self, "_app_state", None) or {}
        session_factory = app_state.get("session_factory")
        if session_factory is None:
            return
        job = getattr(self, "_job", None)
        from app.session.goal_guard import set_goal_waiting_user

        if job is not None and waiting:
            job.set_goal_waiting(True)
        try:
            await set_goal_waiting_user(
                session_factory,
                session_id=self.goal_session_id or self.session_id,
                goal_id=self.goal_id,
                goal_run_id=self.goal_run_id,
                waiting=waiting,
                blocker_code=reason if waiting else None,
                blocker_message=message if waiting else None,
            )
        except BaseException:
            if job is not None and waiting:
                job.set_goal_waiting(False)
            raise
        finally:
            if job is not None and not waiting:
                job.set_goal_waiting(False)
        if waiting and self._publish_fn is not None:
            self._publish_fn(
                "goal-needs-user",
                {
                    "goal_id": self.goal_id,
                    "goal_run_id": self.goal_run_id,
                    "reason": reason,
                    "call_id": self.call_id,
                },
            )

    async def block_goal(self, *, reason: str, message: str) -> None:
        """Persist a terminal blocker raised by an interactive tool."""

        if self.goal_id is None:
            return
        app_state = getattr(self, "_app_state", None) or {}
        session_factory = app_state.get("session_factory")
        if session_factory is None:
            return
        from app.session.goal_guard import block_goal_for_user_action

        await block_goal_for_user_action(
            session_factory,
            session_id=self.goal_session_id or self.session_id,
            goal_id=self.goal_id,
            goal_run_id=self.goal_run_id,
            blocker_code=reason,
            blocker_message=message,
        )

    @property
    def is_aborted(self) -> bool:
        return self.abort_event.is_set()

    def tr(self, zh: str, en: str) -> str:
        """Select request-localized dynamic tool text."""

        return localize(self.language, zh, en)
