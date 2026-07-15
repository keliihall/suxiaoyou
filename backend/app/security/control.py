"""Persistent runtime controls for Security Center Lite."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.utils.atomic_write import atomic_write_text


_TOOL_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
STATE_VERSION = 1
TOGGLEABLE_BUILTIN_TOOLS = frozenset({"web_fetch", "web_search", "image_generate"})


class SecurityControl:
    """Own the persisted emergency-stop and external-tool switches."""

    def __init__(self, state_path: str | Path) -> None:
        self.state_path = Path(state_path)
        self._lock = asyncio.Lock()
        # Runtime start/stop operations must remain ordered with the persisted
        # state transition.  Keeping this separate from the short state-write
        # lock lets the API serialize the complete transition without
        # deadlocking the setters below.
        self._transition_lock = asyncio.Lock()
        self._emergency_stop = False
        self._disabled_tools: set[str] = set()
        self._updated_at: str | None = None
        self._degraded_reason: str | None = None
        self._load()

    @property
    def emergency_stop(self) -> bool:
        return self._emergency_stop

    @property
    def disabled_tools(self) -> frozenset[str]:
        return frozenset(self._disabled_tools)

    @property
    def updated_at(self) -> str | None:
        return self._updated_at

    @property
    def degraded_reason(self) -> str | None:
        return self._degraded_reason

    @property
    def transition_lock(self) -> asyncio.Lock:
        """Serialize a persisted emergency transition with runtime teardown/startup."""

        return self._transition_lock

    def is_tool_enabled(self, tool_id: str) -> bool:
        return tool_id not in self._disabled_tools

    async def set_tool_enabled(self, tool_id: str, enabled: bool) -> bool:
        if not _TOOL_ID_RE.fullmatch(tool_id):
            raise ValueError("Invalid tool identifier")
        async with self._lock:
            next_disabled = set(self._disabled_tools)
            if enabled:
                next_disabled.discard(tool_id)
            else:
                next_disabled.add(tool_id)
            changed = next_disabled != self._disabled_tools
            if changed or self._degraded_reason is not None:
                updated_at = self._persist_state(
                    emergency_stop=self._emergency_stop,
                    disabled_tools=next_disabled,
                )
                self._disabled_tools = next_disabled
                self._updated_at = updated_at
                self._degraded_reason = None
            return changed

    async def set_emergency_stop(self, active: bool) -> bool:
        async with self._lock:
            changed = self._emergency_stop is not active
            if not changed and self._degraded_reason is None:
                return False
            try:
                updated_at = self._persist_state(
                    emergency_stop=active,
                    disabled_tools=self._disabled_tools,
                )
            except OSError:
                if active:
                    # An urgent stop must fail closed for the lifetime of this
                    # process even when the disk is unavailable.  The API still
                    # performs runtime teardown and reports the persistence
                    # warning to the user.
                    self._emergency_stop = True
                    self._degraded_reason = "security_state_not_persisted"
                raise
            self._emergency_stop = active
            self._updated_at = updated_at
            self._degraded_reason = None
            return changed

    def snapshot(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "emergency_stop": self._emergency_stop,
            "disabled_tools": sorted(self._disabled_tools),
            "updated_at": self._updated_at,
            "degraded_reason": self._degraded_reason,
        }

    def _load(self) -> None:
        # A genuinely absent file is the first-run state.  Any present but
        # unreadable, redirected, malformed, or unknown state is ambiguous and
        # must therefore start with the emergency stop active.
        if not self.state_path.exists() and not self.state_path.is_symlink():
            return
        try:
            if self.state_path.is_symlink() or not self.state_path.is_file():
                raise OSError("Security state is not a regular file")
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
                raise ValueError("Unsupported security-state schema")
            if not isinstance(payload.get("emergency_stop"), bool):
                raise ValueError("Invalid emergency-stop state")
            disabled = payload.get("disabled_tools")
            if not isinstance(disabled, list) or any(
                not isinstance(item, str) or not _TOOL_ID_RE.fullmatch(item)
                for item in disabled
            ):
                raise ValueError("Invalid disabled-tools state")
        except (OSError, ValueError, json.JSONDecodeError):
            self._emergency_stop = True
            self._degraded_reason = "security_state_unreadable"
            return
        self._emergency_stop = payload["emergency_stop"]
        self._disabled_tools = set(disabled)
        updated_at = payload.get("updated_at")
        self._updated_at = updated_at if isinstance(updated_at, str) else None

    def _persist_state(
        self,
        *,
        emergency_stop: bool,
        disabled_tools: set[str] | frozenset[str],
    ) -> str:
        """Write a proposed state before committing it to in-memory fields."""

        updated_at = datetime.now(timezone.utc).isoformat()
        payload = {
            "version": STATE_VERSION,
            "emergency_stop": emergency_stop,
            "disabled_tools": sorted(disabled_tools),
            "updated_at": updated_at,
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        if self.state_path.exists() and self.state_path.is_symlink():
            raise OSError("Refusing to replace a symbolic-link security state")
        atomic_write_text(
            self.state_path,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            mode=0o600,
        )
        return updated_at


_security_control: SecurityControl | None = None


def set_security_control(control: SecurityControl) -> None:
    global _security_control
    _security_control = control


def get_security_control() -> SecurityControl:
    if _security_control is None:
        raise RuntimeError("Security control is not initialized")
    return _security_control
