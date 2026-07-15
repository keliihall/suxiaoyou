"""Stable source and capability labels shared by policy, UI, and audit."""

from __future__ import annotations

from typing import Any, Literal, cast

from app.connector.model import CONNECTOR_PROVENANCE_BUILTIN


InvocationSource = Literal[
    "unknown", "desktop", "goal", "scheduler", "channel", "openai_compat"
]

INVOCATION_SOURCES: frozenset[str] = frozenset(
    {"unknown", "desktop", "goal", "scheduler", "channel", "openai_compat"}
)

# Source policy is an independent ceiling above the ordinary per-tool
# permission rules.  Desktop requests may use the complete registered tool
# surface (subject to those existing rules).  Scheduled work may perform
# unattended reads, but cannot mutate the workspace, spawn processes, spend
# money, or change remote data.  External/headless ingress receives no host
# tools in v1; model inference itself remains available so those adapters can
# still answer without reaching into the user's machine.
_SOURCE_CAPABILITY_ALLOWLISTS: dict[InvocationSource, frozenset[str] | None] = {
    "unknown": frozenset(),
    "desktop": None,
    # A Goal is created or resumed by the interactive desktop user and keeps
    # the server-owned permission snapshot captured at that boundary.  The
    # source ceiling therefore exposes the same capability vocabulary as a
    # desktop turn, while the immutable snapshot (and ordinary tool policy)
    # prevents a later autonomous continuation from gaining new permissions.
    "goal": None,
    "scheduler": frozenset(
        {
            "agent_control",
            "filesystem_read",
            "model_inference",
            "network",
            "recovery",
            "remote_data_read",
        }
    ),
    "channel": frozenset({"model_inference"}),
    "openai_compat": frozenset({"model_inference"}),
}

HIGH_PRIVILEGE_CAPABILITIES: frozenset[str] = frozenset(
    {
        "credential",
        "custom_connector",
        "desktop_ui",
        "filesystem_write",
        "network",
        "paid",
        "process",
        "remote_data_read",
        "remote_data_write",
        "unknown_tool",
    }
)


TOOL_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "artifact": ("agent_control",),
    "invalid": ("agent_control",),
    "plan": ("agent_control",),
    "question": ("agent_control",),
    "skill": ("agent_control",),
    "submit_plan": ("agent_control",),
    "task": ("agent_control",),
    "todo": ("agent_control",),
    "get_goal": ("agent_control",),
    "update_goal": ("agent_control",),
    "tool_search": ("agent_control",),
    "read": ("filesystem_read",),
    "glob": ("filesystem_read",),
    "grep": ("filesystem_read",),
    "search": ("filesystem_read",),
    "write": ("filesystem_write",),
    "edit": ("filesystem_write",),
    "apply_patch": ("filesystem_write",),
    "office": ("filesystem_write", "office_document"),
    "file_versions": ("filesystem_read", "recovery"),
    "restore_file_version": ("filesystem_write", "recovery"),
    "present_file": ("filesystem_read", "desktop_ui"),
    # A terminal command may legitimately install a dependency or call a
    # network-backed CLI (for example edge-tts).  Keep that access behind the
    # same explicit high-risk permission/audit path as other network tools.
    "bash": ("filesystem_read", "filesystem_write", "network", "process"),
    # Windows currently provides a Job/process boundary but not AppContainer
    # network isolation. Declare network conservatively for the cross-platform
    # tool so autonomous Goals cannot inherit undeclared outbound access.
    "code_execute": ("filesystem_read", "filesystem_write", "network", "process"),
    "web_fetch": ("network", "remote_data_read"),
    "web_search": ("network", "remote_data_read"),
    "image_generate": ("network", "credential", "paid", "filesystem_write"),
}


def describe_tool_source(tool: Any) -> tuple[str, str, list[str]]:
    """Return source kind/id and a conservative capability profile."""

    from app.mcp.tool_wrapper import McpToolWrapper

    if isinstance(tool, McpToolWrapper):
        client = getattr(tool, "_client", None)
        source_id = str(getattr(client, "name", "mcp"))
        provenance = tool.connector_provenance
        remote_capability = (
            "remote_data_write"
            if bool(getattr(tool, "requires_approval", False))
            else "remote_data_read"
        )
        capabilities = ["network", "credential", remote_capability]
        if provenance != CONNECTOR_PROVENANCE_BUILTIN:
            capabilities.append("custom_connector")
        return f"connector_{provenance}", source_id, capabilities
    return (
        "builtin",
        "suyo",
        list(TOOL_CAPABILITIES.get(getattr(tool, "id", ""), ("unknown_tool",))),
    )


def primary_capability(tool: Any) -> str:
    capabilities = describe_tool_source(tool)[2]
    for preferred in (
        "paid",
        "process",
        "filesystem_write",
        "remote_data_write",
        "network",
        "remote_data_read",
        "filesystem_read",
    ):
        if preferred in capabilities:
            return preferred
    return capabilities[0] if capabilities else "agent_control"


def normalize_invocation_source(value: str) -> InvocationSource:
    """Validate a server-owned invocation source; unknown ingress fails closed."""

    if value not in INVOCATION_SOURCES:
        raise ValueError(f"Unknown invocation source: {value!r}")
    return cast(InvocationSource, value)


def denied_invocation_capabilities(
    invocation_source: str,
    capabilities: list[str] | tuple[str, ...],
) -> tuple[str, ...]:
    """Return capabilities forbidden by the source-level hard ceiling."""

    source = normalize_invocation_source(invocation_source)
    allowlist = _SOURCE_CAPABILITY_ALLOWLISTS[source]
    # Unknown tools never inherit a wildcard.  This keeps newly registered or
    # misspelled built-ins fail-closed even for an interactive desktop request.
    denied = {
        capability
        for capability in capabilities
        if capability == "unknown_tool"
    }
    if allowlist is not None:
        denied.update(
            capability for capability in capabilities if capability not in allowlist
        )
    return tuple(sorted(denied))


def denied_tool_capabilities(invocation_source: str, tool: Any) -> tuple[str, ...]:
    return denied_invocation_capabilities(
        invocation_source,
        describe_tool_source(tool)[2],
    )


def tool_requires_durable_audit(tool: Any) -> bool:
    """Whether a tool must have a durable pre-execution audit record."""

    return bool(
        HIGH_PRIVILEGE_CAPABILITIES.intersection(describe_tool_source(tool)[2])
    )


def source_capability_profiles() -> list[dict[str, Any]]:
    """Stable, credential-free policy summary for Security Center."""

    profiles: list[dict[str, Any]] = []
    for source in (
        "unknown",
        "desktop",
        "goal",
        "scheduler",
        "channel",
        "openai_compat",
    ):
        allowlist = _SOURCE_CAPABILITY_ALLOWLISTS[source]
        profiles.append(
            {
                "source": source,
                "allowed_capabilities": (
                    ["*"] if allowlist is None else sorted(allowlist)
                ),
                "deny_unknown": True,
            }
        )
    return profiles
