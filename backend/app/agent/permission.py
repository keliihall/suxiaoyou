"""Permission evaluation engine.

Implements OpenCode's 4-layer permission model:
  Global defaults → Agent-level → User config → Session-level

Each layer is a Ruleset (ordered list of PermissionRules).
Rules are evaluated in order; last match wins.

Two-dimensional matching (matches OpenCode's permission/next.ts):
  - permission dimension: matches tool/capability name
  - pattern dimension: matches resource (file path, etc.)
"""

from __future__ import annotations

from typing import Any

from app.schemas.agent import PermissionRule, Ruleset


PERMISSION_SNAPSHOT_VERSION = 1
_INTERSECTION_KEY = "all_of"
_TIGHTENING_KEY = "tightening"
_POLICY_BASELINE_KEY = "policy_baseline"


class RejectedError(Exception):
    """Raised when a tool call is denied by the permission engine."""

    def __init__(self, permission: str, pattern: str = "*"):
        self.permission = permission
        self.pattern = pattern
        super().__init__(f"Permission denied: {permission} (pattern: {pattern})")


def evaluate(permission: str, pattern: str, ruleset: Ruleset) -> str:
    """Evaluate a permission against a ruleset with two-dimensional matching.

    Args:
        permission: tool/capability name (e.g., "read", "bash")
        pattern: resource being accessed (e.g., file path, "*" for generic)
        ruleset: merged permission ruleset

    Returns 'allow', 'deny', or 'ask'. Last matching rule wins.
    """
    return ruleset.evaluate(permission, pattern)


def merge_rulesets(*rulesets: Ruleset) -> Ruleset:
    """Merge multiple rulesets in priority order (last wins).

    Layers: defaults → agent → user → session
    """
    merged_rules: list[PermissionRule] = []
    hard_constraints: list[Ruleset] = []
    for rs in rulesets:
        intersection = getattr(rs, "_intersection", ())
        if intersection:
            # A compound policy is a hard ceiling. Its public ``rules`` are a
            # deliberately fail-closed compatibility fallback and must not be
            # treated as an ordinary last-wins layer.
            hard_constraints.extend(intersection)
        else:
            merged_rules.extend(rs.rules)

    merged = Ruleset(rules=merged_rules)
    if not hard_constraints:
        return merged
    if not merged_rules:
        merged = Ruleset(rules=[
            PermissionRule(action="allow", permission="*", pattern="*"),
        ])
    return intersect_permission_rulesets(merged, *hard_constraints)


def intersect_permission_rulesets(*rulesets: Ruleset) -> Ruleset:
    """Return the least-privilege intersection of ordered policies.

    Concatenating two policies cannot express an intersection because this
    engine is last-match-wins: whichever policy is appended last can widen the
    other one.  A compound ruleset instead evaluates every component policy
    independently and takes the most restrictive decision (deny > ask >
    allow).  Existing intersections are flattened so repeated Goal slices do
    not build an unbounded recursive policy tree.

    ``rules`` contains a deny-all compatibility fallback. Exact component
    policies are server-owned private state and are included explicitly by the
    versioned snapshot serializer below. A legacy consumer that drops the
    compound metadata therefore fails closed rather than widening authority.
    """

    components: list[Ruleset] = []
    for ruleset in rulesets:
        intersection = getattr(ruleset, "_intersection", ())
        if intersection:
            components.extend(
                component.model_copy(deep=True)
                for component in intersection
            )
        else:
            components.append(ruleset.model_copy(deep=True))

    if not components:
        components.append(Ruleset())

    result = Ruleset(rules=[
        PermissionRule(action="deny", permission="*", pattern="*"),
    ])
    result._intersection = tuple(components)
    return result


def tightening_permission_ceiling(
    previous: Ruleset,
    current: Ruleset,
) -> Ruleset:
    """Return a ceiling containing only decisions that became stricter.

    The effective Goal snapshot already includes user overrides that were
    authorized at creation. Re-intersecting it with an unchanged built-in
    Agent policy (whose mutation defaults are ``ask``) would incorrectly
    revoke those grants on the first continuation. Comparing the persisted
    baseline layer to the current layer enforces real tightening transitions:
    allow->ask/deny and ask->deny. Equal or looser decisions evaluate allow in
    this ceiling, so they can never expand the historical Goal snapshot.
    """

    result = Ruleset(rules=[
        PermissionRule(action="deny", permission="*", pattern="*"),
    ])
    result._tightening_sources = (
        previous.model_copy(deep=True),
        current.model_copy(deep=True),
    )
    return result


def disabled_tools(tool_names: list[str], ruleset: Ruleset) -> set[str]:
    """Return set of tool names that are denied by the ruleset.

    Checks with pattern="*" (generic resource).
    """
    denied = set()
    for name in tool_names:
        action = evaluate(name, "*", ruleset)
        if action == "deny":
            denied.add(name)
    return denied


def parse_session_permissions(permission_data: list[dict] | None) -> Ruleset:
    """Parse session-level permission JSON into a Ruleset.

    Session.permission stores:
      [{"action": "allow", "permission": "bash", "pattern": "*"}, ...]
    """
    if not permission_data:
        return Ruleset()
    rules = []
    for item in permission_data:
        if not isinstance(item, dict):
            continue
        try:
            rules.append(PermissionRule(
                action=item.get("action", "deny"),
                permission=item.get("permission", "*"),
                pattern=item.get("pattern", "*"),
            ))
        except (ValueError, KeyError):
            continue  # Skip malformed rules
    return Ruleset(rules=rules)


def _serialize_ruleset_payload(ruleset: Ruleset) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "rules": [rule.model_dump(mode="json") for rule in ruleset.rules],
    }
    intersection = getattr(ruleset, "_intersection", ())
    if intersection:
        payload[_INTERSECTION_KEY] = [
            _serialize_ruleset_payload(component)
            for component in intersection
        ]
    tightening = getattr(ruleset, "_tightening_sources", None)
    if tightening is not None:
        previous, current = tightening
        payload[_TIGHTENING_KEY] = {
            "previous": _serialize_ruleset_payload(previous),
            "current": _serialize_ruleset_payload(current),
        }
    return payload


def serialize_permission_snapshot(
    ruleset: Ruleset,
    *,
    global_permissions: Ruleset | None = None,
    agent_permissions: Ruleset | None = None,
) -> dict[str, Any]:
    """Serialize a server-derived effective ruleset for child inheritance."""

    snapshot = {
        "version": PERMISSION_SNAPSHOT_VERSION,
        "kind": "effective_permission_snapshot",
        **_serialize_ruleset_payload(ruleset),
    }
    if global_permissions is not None and agent_permissions is not None:
        snapshot[_POLICY_BASELINE_KEY] = {
            "global": _serialize_ruleset_payload(global_permissions),
            "agent": _serialize_ruleset_payload(agent_permissions),
        }
    return snapshot


def _parse_ruleset_payload(value: Any, *, depth: int = 0) -> Ruleset | None:
    if not isinstance(value, dict) or depth > 8:
        return None
    rules = value.get("rules")
    if not isinstance(rules, list):
        return None
    parsed = parse_session_permissions(rules)
    # Versioned snapshots are authority objects, so malformed entries reject
    # the entire payload instead of being skipped beside a broader allow.
    if len(parsed.rules) != len(rules):
        return None
    special_keys = sum(
        key in value
        for key in (_TIGHTENING_KEY, _INTERSECTION_KEY)
    )
    if special_keys > 1:
        return None
    if _TIGHTENING_KEY in value:
        sources = value.get(_TIGHTENING_KEY)
        if not isinstance(sources, dict):
            return None
        previous = _parse_ruleset_payload(
            sources.get("previous"),
            depth=depth + 1,
        )
        current = _parse_ruleset_payload(
            sources.get("current"),
            depth=depth + 1,
        )
        if previous is None or current is None:
            return None
        return tightening_permission_ceiling(previous, current)
    if _INTERSECTION_KEY not in value:
        return parsed

    components = value.get(_INTERSECTION_KEY)
    if not isinstance(components, list) or not components:
        return None
    parsed_components: list[Ruleset] = []
    for component in components:
        candidate = _parse_ruleset_payload(component, depth=depth + 1)
        if candidate is None:
            return None
        parsed_components.append(candidate)
    return intersect_permission_rulesets(*parsed_components)


def parse_permission_snapshot(value: Any) -> Ruleset | None:
    """Parse only the versioned snapshot shape written by the server.

    Legacy ``Session.permission`` lists were externally writable and therefore
    cannot be treated as an authority boundary for non-interactive children.
    """

    if not isinstance(value, dict):
        return None
    if value.get("version") != PERMISSION_SNAPSHOT_VERSION:
        return None
    if value.get("kind") != "effective_permission_snapshot":
        return None
    return _parse_ruleset_payload(value)


def parse_permission_policy_baseline(
    value: Any,
) -> tuple[Ruleset, Ruleset] | None:
    """Parse the trusted global/agent layers captured with a snapshot."""

    if not isinstance(value, dict):
        return None
    if value.get("version") != PERMISSION_SNAPSHOT_VERSION:
        return None
    if value.get("kind") != "effective_permission_snapshot":
        return None
    baseline = value.get(_POLICY_BASELINE_KEY)
    if not isinstance(baseline, dict):
        return None
    global_permissions = _parse_ruleset_payload(baseline.get("global"))
    agent_permissions = _parse_ruleset_payload(baseline.get("agent"))
    if global_permissions is None or agent_permissions is None:
        return None
    return global_permissions, agent_permissions


def tighten_permission_snapshot(
    parent: Ruleset,
    requested_rules: list[dict[str, Any]] | None,
) -> Ruleset:
    """Return a child ceiling that can only be narrower than ``parent``.

    Task-batch fields are public request data.  An ``allow`` (including an
    allow generated by a permission preset) must never widen the effective
    parent snapshot.  Explicit denies are safe to append because this engine
    is last-match-wins and deny is the most restrictive action.
    """

    restrictions = [
        rule
        for rule in parse_session_permissions(requested_rules).rules
        if rule.action == "deny"
    ]
    if not restrictions:
        return parent.model_copy(deep=True)

    intersection = getattr(parent, "_intersection", ())
    if not intersection:
        return Ruleset(rules=[*parent.rules, *restrictions])

    requested_ceiling = Ruleset(rules=[
        PermissionRule(action="allow", permission="*", pattern="*"),
        *restrictions,
    ])
    return intersect_permission_rulesets(parent, requested_ceiling)


def presets_to_ruleset(presets: dict[str, bool] | None) -> Ruleset:
    """Convert frontend permission presets into a Ruleset.

    Preset keys:
      - file_changes  → allow workspace file mutators, including Office
      - run_commands   → allow bash

    Only True values generate allow rules; False values are ignored so the
    GLOBAL_DEFAULTS "ask" behaviour is preserved.
    """
    if not presets:
        return Ruleset()
    rules: list[PermissionRule] = []
    if presets.get("file_changes"):
        rules.append(PermissionRule(action="allow", permission="write"))
        rules.append(PermissionRule(action="allow", permission="edit"))
        rules.append(PermissionRule(action="allow", permission="apply_patch"))
        rules.append(PermissionRule(action="allow", permission="office"))
        rules.append(PermissionRule(action="allow", permission="restore_file_version"))
    if presets.get("run_commands"):
        rules.append(PermissionRule(action="allow", permission="bash"))
        rules.append(PermissionRule(action="allow", permission="code_execute"))
    return Ruleset(rules=rules)


# --- Default rulesets ---

GLOBAL_DEFAULTS = Ruleset(rules=[
    PermissionRule(action="allow", permission="*"),
    PermissionRule(action="ask", permission="bash"),
    PermissionRule(action="ask", permission="code_execute"),
    PermissionRule(action="ask", permission="write"),
    PermissionRule(action="ask", permission="edit"),
    PermissionRule(action="ask", permission="apply_patch"),
    PermissionRule(action="ask", permission="image_generate"),
    PermissionRule(action="ask", permission="office"),
    PermissionRule(action="ask", permission="restore_file_version"),
    PermissionRule(action="deny", permission="question"),
    PermissionRule(action="deny", permission="plan"),
    # .env file protection (two-dimensional: tool + resource pattern)
    PermissionRule(action="ask", permission="read", pattern="*.env"),
    PermissionRule(action="ask", permission="read", pattern="*.env.*"),
    PermissionRule(action="allow", permission="read", pattern="*.env.example"),
])
