"""Agent schemas."""

from __future__ import annotations

import fnmatch
from typing import Any, Literal

from pydantic import BaseModel, PrivateAttr


class PermissionRule(BaseModel):
    """Two-dimensional permission rule matching OpenCode's design.

    - permission: tool/capability name pattern (e.g., "read", "bash", "*")
    - pattern: resource pattern (e.g., "*", "*.env", "/tmp/*")
    - action: what to do when both match
    """

    action: Literal["allow", "deny", "ask"]
    permission: str = "*"  # tool name pattern
    pattern: str = "*"     # resource pattern (file path, etc.)


class Ruleset(BaseModel):
    """Ordered list of permission rules (last match wins)."""

    rules: list[PermissionRule] = []
    # A server-created intersection is a hard policy boundary, not another
    # last-rule-wins layer.  Keep the component policies private so public
    # Agent/Prompt JSON cannot opt into (or forge) this authority mode.  The
    # permission module owns the versioned persistence format for this value.
    _intersection: tuple["Ruleset", ...] = PrivateAttr(default=())
    # Pair of (policy at Goal authorization, current policy). Evaluation
    # exposes only decisions that became stricter, preserving unchanged user
    # overrides while enforcing allow->ask/deny and ask->deny transitions.
    _tightening_sources: "tuple[Ruleset, Ruleset] | None" = PrivateAttr(
        default=None
    )

    def evaluate(self, permission: str, pattern: str = "*") -> str:
        """Evaluate permission + resource pattern. Returns 'allow', 'deny', or 'ask'."""
        if self._tightening_sources is not None:
            previous, current = self._tightening_sources
            previous_action = previous.evaluate(permission, pattern)
            current_action = current.evaluate(permission, pattern)
            strictness = {"allow": 0, "ask": 1, "deny": 2}
            return (
                current_action
                if strictness[current_action] > strictness[previous_action]
                else "allow"
            )
        if self._intersection:
            actions = {
                policy.evaluate(permission, pattern)
                for policy in self._intersection
            }
            if "deny" in actions:
                return "deny"
            if "ask" in actions:
                return "ask"
            # An intersection permits an operation only when every component
            # policy independently permits it.
            return "allow" if actions == {"allow"} else "deny"

        result = "deny"  # default if no rules match
        for rule in self.rules:
            if _glob_match(permission, rule.permission) and _glob_match(pattern, rule.pattern):
                result = rule.action
        return result


class AgentModel(BaseModel):
    """Per-agent model override."""

    model_id: str
    provider_id: str | None = None


class AgentInfo(BaseModel):
    """Public agent information."""

    name: str
    description: str
    mode: str  # "primary" | "subagent" | "hidden"
    tools: list[str] = []  # tool IDs this agent can access
    permissions: Ruleset = Ruleset()
    system_prompt: str | None = None
    temperature: float | None = None
    model: AgentModel | None = None  # per-agent model override
    metadata: dict[str, Any] = {}


def _glob_match(value: str, pattern: str) -> bool:
    """Glob matching for permission patterns.

    Supports:
      - "*" matches everything
      - "read" matches "read" exactly
      - "read.*" matches "read.file", "read.dir"
      - "*.env" matches "config.env", "secrets.env"
    """
    if pattern == "*":
        return True
    if "*" not in pattern and "?" not in pattern and "[" not in pattern:
        return value == pattern
    return fnmatch.fnmatch(value, pattern)
