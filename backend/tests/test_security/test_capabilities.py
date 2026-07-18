from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.mcp.tool_wrapper import McpToolWrapper
from app.security.capabilities import (
    denied_invocation_capabilities,
    denied_tool_capabilities,
    describe_tool_source,
    normalize_invocation_source,
    source_capability_profiles,
    tool_requires_durable_audit,
)


class _Tool:
    def __init__(self, tool_id: str) -> None:
        self.id = tool_id


def test_source_profiles_are_fail_closed_above_tool_permissions() -> None:
    assert denied_tool_capabilities("desktop", _Tool("write")) == ()
    assert denied_tool_capabilities("goal", _Tool("write")) == ()
    assert denied_tool_capabilities("scheduler", _Tool("read")) == ()
    assert denied_tool_capabilities("scheduler", _Tool("web_search")) == ()
    assert denied_tool_capabilities("scheduler", _Tool("write")) == (
        "filesystem_write",
    )
    assert denied_tool_capabilities("scheduler", _Tool("bash")) == (
        "filesystem_write",
        "process",
    )
    assert denied_tool_capabilities("validator", _Tool("read")) == ()
    assert denied_tool_capabilities("validator", _Tool("search")) == ()
    assert denied_tool_capabilities("validator", _Tool("write")) == (
        "filesystem_write",
    )
    assert denied_tool_capabilities("validator", _Tool("bash")) == (
        "filesystem_write",
        "network",
        "process",
    )
    assert denied_tool_capabilities("validator", _Tool("task")) == (
        "agent_control",
    )
    assert denied_tool_capabilities("validator", _Tool("question")) == (
        "agent_control",
    )
    assert denied_tool_capabilities("validator", _Tool("web_search")) == (
        "network",
        "remote_data_read",
    )
    assert denied_invocation_capabilities(
        "validator", ("model_inference",)
    ) == ()
    assert denied_invocation_capabilities("validator", ("network",)) == (
        "network",
    )
    assert denied_tool_capabilities("acp", _Tool("read")) == ()
    assert denied_tool_capabilities("acp", _Tool("write")) == ()
    assert denied_tool_capabilities("acp", _Tool("office")) == ()
    assert denied_tool_capabilities("acp", _Tool("restore_file_version")) == ()
    assert denied_tool_capabilities("acp", _Tool("bash")) == (
        "network",
        "process",
    )
    assert denied_tool_capabilities("acp", _Tool("web_search")) == (
        "network",
        "remote_data_read",
    )
    assert denied_tool_capabilities("channel", _Tool("read")) == (
        "filesystem_read",
    )
    assert denied_tool_capabilities("openai_compat", _Tool("task")) == (
        "agent_control",
    )

    profiles = {item["source"]: item for item in source_capability_profiles()}
    assert profiles["desktop"]["allowed_capabilities"] == ["*"]
    assert profiles["goal"]["allowed_capabilities"] == ["*"]
    assert profiles["unknown"]["allowed_capabilities"] == []
    assert profiles["channel"]["allowed_capabilities"] == ["model_inference"]
    assert profiles["validator"]["allowed_capabilities"] == [
        "filesystem_read",
        "model_inference",
    ]
    assert profiles["acp"]["allowed_capabilities"] == [
        "agent_control",
        "filesystem_read",
        "filesystem_write",
        "model_inference",
        "office_document",
        "recovery",
    ]
    assert profiles["unknown"]["deny_unknown"] is True


def test_validator_profile_is_dynamic_and_can_be_hidden_by_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.release_features.V11_VALIDATION_AGENT_RELEASED",
        False,
    )

    profiles = {item["source"]: item for item in source_capability_profiles()}
    assert "validator" not in profiles


def test_acp_profile_is_dynamic_and_can_be_hidden_by_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.release_features.V11_ACP_RELEASED", False)

    profiles = {item["source"]: item for item in source_capability_profiles()}
    assert "acp" not in profiles


def test_unknown_ingress_and_unknown_tools_fail_closed() -> None:
    with pytest.raises(ValueError, match="Unknown invocation source"):
        normalize_invocation_source("request-supplied")

    unknown_tool = _Tool("future_plugin_side_effect")
    assert describe_tool_source(unknown_tool)[2] == ["unknown_tool"]
    assert denied_tool_capabilities("desktop", unknown_tool) == ("unknown_tool",)
    assert denied_tool_capabilities("goal", unknown_tool) == ("unknown_tool",)
    assert denied_tool_capabilities("unknown", unknown_tool) == ("unknown_tool",)
    assert denied_tool_capabilities("scheduler", unknown_tool) == ("unknown_tool",)
    assert tool_requires_durable_audit(unknown_tool) is True


def test_mcp_reads_and_writes_are_distinct_but_non_desktop_credentials_block() -> None:
    class _Client:
        name = "test-connector"

        def __init__(self, mutating: bool, provenance: str) -> None:
            self.mutating = mutating
            self.connector_provenance = provenance

        def tool_id(self, name: str) -> str:
            return f"test-connector_{name}"

        def tool_requires_approval(self, _name: str) -> bool:
            return self.mutating

    mcp_tool = SimpleNamespace(
        name="operation",
        description="operation",
        inputSchema={"type": "object"},
    )
    read_tool = McpToolWrapper(_Client(False, "builtin"), mcp_tool)
    write_tool = McpToolWrapper(_Client(True, "builtin"), mcp_tool)
    custom_tool = McpToolWrapper(_Client(False, "custom"), mcp_tool)

    assert describe_tool_source(read_tool) == (
        "connector_builtin",
        "test-connector",
        [
            "network",
            "credential",
            "remote_data_read",
        ],
    )
    assert describe_tool_source(write_tool) == (
        "connector_builtin",
        "test-connector",
        [
            "network",
            "credential",
            "remote_data_write",
        ],
    )
    assert describe_tool_source(custom_tool) == (
        "connector_custom",
        "test-connector",
        [
            "network",
            "credential",
            "remote_data_write",
            "custom_connector",
        ],
    )
    assert "credential" in denied_tool_capabilities("scheduler", read_tool)
    assert denied_tool_capabilities("scheduler", custom_tool) == (
        "credential",
        "custom_connector",
        "remote_data_write",
    )
    assert custom_tool.requires_approval is True
    assert denied_tool_capabilities("desktop", write_tool) == ()
    assert denied_tool_capabilities("desktop", custom_tool) == ()


def test_native_remote_read_profile_uses_the_same_capability_ceiling() -> None:
    capabilities = ("network", "remote_data_read")
    assert denied_invocation_capabilities("desktop", capabilities) == ()
    assert denied_invocation_capabilities("scheduler", capabilities) == ()
    assert denied_invocation_capabilities("channel", capabilities) == capabilities
