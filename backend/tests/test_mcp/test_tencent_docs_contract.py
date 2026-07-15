"""Tencent Docs connector contract and security-boundary tests.

The live vendor server is intentionally optional.  The default suite pins the
official endpoint/auth contract and exercises discovery through a strict mock
so CI never needs a personal credential.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, call

import pytest

pytest.importorskip("mcp")

from app.connector.registry import ConnectorRegistry
from app.mcp.client import McpClient
from app.mcp.manager import McpManager
from app.mcp.token_store import McpTokenStore
from app.mcp.tool_wrapper import McpToolWrapper
from mcp.types import CallToolResult, TextContent


CATALOG_PATH = Path(__file__).parents[2] / "app" / "data" / "connectors.json"
PRODUCTIVITY_MCP_PATH = (
    Path(__file__).parents[2]
    / "app"
    / "data"
    / "plugins"
    / "productivity"
    / ".mcp.json"
)

READ_ONLY_TOOLS = {
    "query_space_node",
    "search_space_file",
    "get_content",
    "smartcanvas.get_element_info",
    "smartcanvas.get_page_info",
    "smartcanvas.get_top_level_pages",
    "smartsheet.list_tables",
    "smartsheet.list_views",
    "smartsheet.list_fields",
    "smartsheet.list_records",
}
HIDDEN_DELETE_TOOLS = {
    "delete_space_node",
    "smartcanvas.delete_element",
    "smartsheet.delete_view",
    "smartsheet.delete_fields",
    "smartsheet.delete_records",
}

WRITE_E2E_ACK = "I_UNDERSTAND_THIS_MODIFIES_A_DEDICATED_TEST_DOCUMENT"
WRITE_E2E_MARKER_PLACEHOLDER = "{{SUYO_TENCENT_DOCS_E2E_MARKER}}"
WRITE_E2E_BASELINE_PLACEHOLDER = "{{SUYO_TENCENT_DOCS_E2E_BASELINE}}"


@dataclass(frozen=True)
class _ReversibleWriteConfig:
    document_id: str
    baseline_text: str
    read_tool: str
    read_args: dict[str, Any]
    write_tool: str
    write_args: dict[str, Any]
    restore_tool: str
    restore_args: dict[str, Any]


def _catalog_entry() -> dict:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))["tencent-docs"]


def _client_config(**overrides) -> dict:
    entry = _catalog_entry()
    config = {
        "type": "remote",
        "url": entry["url"],
        "connector_provenance": "builtin",
        "auth_mode": entry["auth_mode"],
        "allowed_tool_patterns": entry["allowed_tool_patterns"],
        "approval_required_tool_patterns": entry[
            "approval_required_tool_patterns"
        ],
        "timeout": 10,
    }
    config.update(overrides)
    return config


def _tool(name: str):
    return SimpleNamespace(
        name=name,
        description=f"Mock contract tool: {name}",
        inputSchema={"type": "object", "properties": {}},
    )


def _json_object_from_env(environ: Mapping[str, str], name: str) -> dict[str, Any]:
    try:
        value = json.loads(environ[name])
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must contain valid JSON: {exc.msg}") from None
    if not isinstance(value, dict):
        raise ValueError(f"{name} must contain a JSON object")
    return value


def _nested_string_contains(value: Any, expected: str) -> bool:
    if isinstance(value, str):
        return expected in value
    if isinstance(value, dict):
        return any(_nested_string_contains(item, expected) for item in value.values())
    if isinstance(value, list):
        return any(_nested_string_contains(item, expected) for item in value)
    return False


def _replace_placeholders(value: Any, *, marker: str, baseline: str) -> Any:
    if isinstance(value, str):
        return value.replace(WRITE_E2E_MARKER_PLACEHOLDER, marker).replace(
            WRITE_E2E_BASELINE_PLACEHOLDER,
            baseline,
        )
    if isinstance(value, dict):
        return {
            key: _replace_placeholders(item, marker=marker, baseline=baseline)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _replace_placeholders(item, marker=marker, baseline=baseline)
            for item in value
        ]
    return value


def _load_reversible_write_config(
    environ: Mapping[str, str],
) -> _ReversibleWriteConfig | None:
    """Load the destructive opt-in without ever reading a token into fixtures.

    No acknowledgement means the optional write cycle is disabled.  Once an
    operator sets the acknowledgement variable, incomplete or malformed
    configuration is a hard failure rather than a misleading skip.
    """

    acknowledgement = environ.get("TENCENT_DOCS_E2E_ALLOW_WRITE", "")
    if not acknowledgement:
        return None
    if acknowledgement != WRITE_E2E_ACK:
        raise ValueError(
            "TENCENT_DOCS_E2E_ALLOW_WRITE must exactly match the documented "
            "dedicated-test-document acknowledgement"
        )

    required = (
        "TENCENT_DOCS_E2E_TOKEN",
        "TENCENT_DOCS_E2E_TEST_DOCUMENT_ID",
        "TENCENT_DOCS_E2E_BASELINE_TEXT",
        "TENCENT_DOCS_E2E_READ_ARGS_JSON",
        "TENCENT_DOCS_E2E_WRITE_TOOL",
        "TENCENT_DOCS_E2E_WRITE_ARGS_JSON",
        "TENCENT_DOCS_E2E_RESTORE_TOOL",
        "TENCENT_DOCS_E2E_RESTORE_ARGS_JSON",
    )
    missing = [name for name in required if not environ.get(name, "").strip()]
    if missing:
        raise ValueError(
            "write E2E acknowledgement was set but required variables are missing: "
            + ", ".join(missing)
        )

    document_id = environ["TENCENT_DOCS_E2E_TEST_DOCUMENT_ID"].strip()
    baseline_text = environ["TENCENT_DOCS_E2E_BASELINE_TEXT"]
    read_args = _json_object_from_env(environ, "TENCENT_DOCS_E2E_READ_ARGS_JSON")
    write_args = _json_object_from_env(environ, "TENCENT_DOCS_E2E_WRITE_ARGS_JSON")
    restore_args = _json_object_from_env(
        environ,
        "TENCENT_DOCS_E2E_RESTORE_ARGS_JSON",
    )

    for name, arguments in (
        ("TENCENT_DOCS_E2E_READ_ARGS_JSON", read_args),
        ("TENCENT_DOCS_E2E_WRITE_ARGS_JSON", write_args),
        ("TENCENT_DOCS_E2E_RESTORE_ARGS_JSON", restore_args),
    ):
        if not _nested_string_contains(arguments, document_id):
            raise ValueError(f"{name} must reference TENCENT_DOCS_E2E_TEST_DOCUMENT_ID")

    if not _nested_string_contains(write_args, WRITE_E2E_MARKER_PLACEHOLDER):
        raise ValueError(
            "TENCENT_DOCS_E2E_WRITE_ARGS_JSON must contain "
            f"{WRITE_E2E_MARKER_PLACEHOLDER}"
        )
    if not _nested_string_contains(restore_args, WRITE_E2E_BASELINE_PLACEHOLDER):
        raise ValueError(
            "TENCENT_DOCS_E2E_RESTORE_ARGS_JSON must contain "
            f"{WRITE_E2E_BASELINE_PLACEHOLDER}"
        )

    return _ReversibleWriteConfig(
        document_id=document_id,
        baseline_text=baseline_text,
        read_tool=environ.get("TENCENT_DOCS_E2E_READ_TOOL", "get_content").strip(),
        read_args=read_args,
        write_tool=environ["TENCENT_DOCS_E2E_WRITE_TOOL"].strip(),
        write_args=write_args,
        restore_tool=environ["TENCENT_DOCS_E2E_RESTORE_TOOL"].strip(),
        restore_args=restore_args,
    )


def _tool_result_text(result: CallToolResult) -> str:
    return json.dumps(
        result.model_dump(mode="json", exclude_none=True),
        ensure_ascii=False,
        sort_keys=True,
    )


async def _call_successfully(
    client: McpClient,
    tool_name: str,
    arguments: dict[str, Any],
) -> str:
    result = await client.call_tool(tool_name, arguments)
    text = client.scrub_sensitive_text(_tool_result_text(result))
    if result.isError:
        raise AssertionError(f"Tencent Docs tool {tool_name!r} failed: {text}")
    return text


async def _run_reversible_write_cycle(
    client: McpClient,
    config: _ReversibleWriteConfig,
    *,
    marker: str,
) -> None:
    write_args = _replace_placeholders(
        config.write_args,
        marker=marker,
        baseline=config.baseline_text,
    )
    restore_args = _replace_placeholders(
        config.restore_args,
        marker=marker,
        baseline=config.baseline_text,
    )

    before = await _call_successfully(client, config.read_tool, config.read_args)
    assert config.baseline_text in before, "dedicated test document baseline is absent"
    assert marker not in before, "fresh E2E marker unexpectedly exists before mutation"

    # Set this before the remote call: a transport failure can happen after the
    # server has already committed the mutation, so cleanup must still run.
    mutation_may_have_started = False
    try:
        mutation_may_have_started = True
        await _call_successfully(client, config.write_tool, write_args)
        after_write = await _call_successfully(
            client,
            config.read_tool,
            config.read_args,
        )
        assert marker in after_write, "write marker was not visible on read-back"
    finally:
        if mutation_may_have_started:
            await _call_successfully(client, config.restore_tool, restore_args)
            after_restore = await _call_successfully(
                client,
                config.read_tool,
                config.read_args,
            )
            assert marker not in after_restore, "write marker remains after restore"
            assert config.baseline_text in after_restore, (
                "dedicated test document baseline was not restored"
            )


def test_builtin_definition_pins_official_endpoint_and_raw_auth() -> None:
    entry = _catalog_entry()
    plugin_config = json.loads(PRODUCTIVITY_MCP_PATH.read_text(encoding="utf-8"))

    assert entry["url"] == "https://docs.qq.com/openapi/mcp"
    assert entry["auth_mode"] == "raw_authorization"
    assert entry["credential_url"].startswith("https://docs.qq.com/")
    assert plugin_config["mcpServers"]["tencent-docs"] == {
        "type": "http",
        "url": "https://docs.qq.com/openapi/mcp",
    }


def test_registry_carries_trusted_auth_and_tool_policy_to_manager(tmp_path: Path) -> None:
    registry = ConnectorRegistry(project_dir=str(tmp_path))
    registry.register_from_plugin(
        "productivity",
        {
            "tencent-docs": {
                "type": "remote",
                "url": "https://docs.qq.com/openapi/mcp",
            }
        },
        source="builtin",
    )
    connector = registry.get("tencent-docs")
    assert connector is not None
    assert connector.auth_mode == "raw_authorization"
    assert "delete_space_node" not in connector.allowed_tool_patterns
    assert "create_space_node" in connector.allowed_tool_patterns
    assert "create_*" in connector.approval_required_tool_patterns

    registry.prepare()
    assert registry.mcp_manager is not None
    runtime = registry.mcp_manager._config["tencent-docs"]
    assert runtime["auth_mode"] == "raw_authorization"
    assert runtime["allowed_tool_patterns"] == connector.allowed_tool_patterns
    assert runtime["connector_provenance"] == "builtin"


def test_only_fixed_read_tools_bypass_approval_and_deletes_are_hidden() -> None:
    entry = _catalog_entry()
    allowed = set(entry["allowed_tool_patterns"])
    client = McpClient("tencent-docs", _client_config())

    assert READ_ONLY_TOOLS <= allowed
    assert HIDDEN_DELETE_TOOLS.isdisjoint(allowed)
    assert all("delete" not in name.casefold() for name in allowed)

    for name in allowed:
        requires_approval = client.tool_requires_approval(name)
        assert requires_approval is (name not in READ_ONLY_TOOLS), name
        if name not in READ_ONLY_TOOLS:
            assert any(verb in name for verb in ("create", "append", "add", "update"))


def test_raw_authorization_value_has_no_bearer_prefix() -> None:
    client = McpClient("tencent-docs", _client_config())
    client.set_oauth_token("personal-token-value")

    assert client._request_headers() == {
        "Authorization": "personal-token-value",
    }


def test_arbitrary_remote_headers_are_rejected() -> None:
    client = McpClient(
        "tencent-docs",
        _client_config(headers={"X-Injected": "not-allowed"}),
    )
    client.set_oauth_token("personal-token-value")

    with pytest.raises(ValueError, match="Arbitrary remote MCP headers"):
        client._request_headers()


@pytest.mark.asyncio
async def test_mock_discovery_filters_tools_outside_official_allowlist() -> None:
    client = McpClient("tencent-docs", _client_config())
    read_only = _tool("get_content")
    write = _tool("create_smartcanvas_by_markdown")
    destructive = _tool("smartcanvas.delete_element")
    untrusted = _tool("export_all_credentials")

    session = SimpleNamespace(
        list_tools=AsyncMock(
            return_value=SimpleNamespace(
                tools=[read_only, write, destructive, untrusted]
            )
        )
    )

    async def connect_mock() -> None:
        client._session = session

    client._connect_remote = connect_mock  # type: ignore[method-assign]
    await client.connect()

    assert client.status == "connected"
    assert [tool.name for tool in client.list_tools()] == [
        "get_content",
        "create_smartcanvas_by_markdown",
    ]
    assert client.tool_requires_approval("get_content") is False
    assert client.tool_requires_approval("create_smartcanvas_by_markdown") is True
    assert client.is_tool_allowed("smartcanvas.delete_element") is False


@pytest.mark.asyncio
async def test_direct_call_cannot_bypass_tool_allowlist() -> None:
    client = McpClient("tencent-docs", _client_config())
    session = SimpleNamespace(call_tool=AsyncMock())
    client._session = session

    with pytest.raises(PermissionError, match="outside the connector allowlist"):
        await client.call_tool("delete_space_node", {})
    session.call_tool.assert_not_awaited()


def test_mutating_wrapper_requires_interactive_approval() -> None:
    client = McpClient("tencent-docs", _client_config())
    wrapper = McpToolWrapper(client, _tool("create_space_node"))
    read_wrapper = McpToolWrapper(client, _tool("get_content"))

    assert wrapper.requires_approval is True
    assert read_wrapper.requires_approval is False


@pytest.mark.asyncio
async def test_tool_call_exception_scrubs_token_before_tool_result() -> None:
    token = "secret-personal-token"
    client = McpClient("tencent-docs", _client_config())
    client.set_oauth_token(token)
    client._session = SimpleNamespace(
        call_tool=AsyncMock(side_effect=RuntimeError(f"upstream echoed {token}"))
    )
    wrapper = McpToolWrapper(client, _tool("get_content"))

    with pytest.raises(RuntimeError) as caught:
        await client.call_tool("get_content", {})
    assert str(caught.value) == "upstream echoed [redacted]"
    assert token not in str(caught.value)

    result = await wrapper.execute({}, None)  # type: ignore[arg-type]

    assert not result.success
    assert result.error == "MCP tool call failed: upstream echoed [redacted]"
    assert token not in (result.error or "")


@pytest.mark.asyncio
async def test_is_error_content_scrubs_token_before_tool_result() -> None:
    token = "secret-personal-token"
    client = McpClient("tencent-docs", _client_config())
    client.set_oauth_token(token)
    client._session = SimpleNamespace(
        call_tool=AsyncMock(
            return_value=CallToolResult(
                content=[
                    TextContent(type="text", text=f"vendor rejected {token}")
                ],
                isError=True,
            )
        )
    )
    wrapper = McpToolWrapper(client, _tool("get_content"))

    result = await wrapper.execute({}, None)  # type: ignore[arg-type]

    assert not result.success
    assert result.error == "vendor rejected [redacted]"
    assert token not in (result.error or "")


@pytest.mark.asyncio
async def test_connection_error_redacts_personal_token() -> None:
    client = McpClient("tencent-docs", _client_config())
    client.set_oauth_token("secret-personal-token")
    client._connect_remote = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("upstream echoed secret-personal-token")
    )

    await client.connect()

    assert client.status == "failed"
    assert client.error == "upstream echoed [redacted]"


def test_personal_token_uses_existing_protected_store(tmp_path: Path) -> None:
    manager = McpManager(
        {"tencent-docs": _client_config()},
        project_dir=str(tmp_path),
    )
    manager._token_store = McpTokenStore(
        project_dir=str(tmp_path),
        storage_root=tmp_path / "private-credentials",
    )

    assert manager.set_static_token("tencent-docs", "personal-token-value") is True
    stored = manager._token_store.get("tencent-docs")
    assert stored is not None
    assert stored.access_token == "personal-token-value"
    assert stored.token_type == "RawAuthorization"
    assert "personal-token-value" not in manager._token_store.path.read_text(
        encoding="utf-8"
    )


def test_write_e2e_requires_exact_acknowledgement() -> None:
    assert _load_reversible_write_config({}) is None

    with pytest.raises(ValueError, match="must exactly match"):
        _load_reversible_write_config(
            {"TENCENT_DOCS_E2E_ALLOW_WRITE": "yes"}
        )


def test_write_e2e_rejects_incomplete_configuration_before_connecting() -> None:
    with pytest.raises(ValueError, match="TENCENT_DOCS_E2E_TOKEN"):
        _load_reversible_write_config(
            {"TENCENT_DOCS_E2E_ALLOW_WRITE": WRITE_E2E_ACK}
        )


def test_write_e2e_config_is_bound_to_document_and_restore_placeholders() -> None:
    environ = {
        "TENCENT_DOCS_E2E_ALLOW_WRITE": WRITE_E2E_ACK,
        "TENCENT_DOCS_E2E_TOKEN": "not-used-by-this-unit-test",
        "TENCENT_DOCS_E2E_TEST_DOCUMENT_ID": "dedicated-doc-id",
        "TENCENT_DOCS_E2E_BASELINE_TEXT": "SUYO_E2E_READY",
        "TENCENT_DOCS_E2E_READ_ARGS_JSON": '{"doc_id":"dedicated-doc-id"}',
        "TENCENT_DOCS_E2E_WRITE_TOOL": "batch_update_sheet_range",
        "TENCENT_DOCS_E2E_WRITE_ARGS_JSON": (
            '{"doc_id":"dedicated-doc-id","value":"'
            + WRITE_E2E_MARKER_PLACEHOLDER
            + '"}'
        ),
        "TENCENT_DOCS_E2E_RESTORE_TOOL": "batch_update_sheet_range",
        "TENCENT_DOCS_E2E_RESTORE_ARGS_JSON": (
            '{"doc_id":"dedicated-doc-id","value":"'
            + WRITE_E2E_BASELINE_PLACEHOLDER
            + '"}'
        ),
    }

    config = _load_reversible_write_config(environ)

    assert config is not None
    assert config.document_id == "dedicated-doc-id"
    assert config.read_tool == "get_content"

    environ["TENCENT_DOCS_E2E_WRITE_ARGS_JSON"] = (
        '{"doc_id":"another-document","value":"'
        + WRITE_E2E_MARKER_PLACEHOLDER
        + '"}'
    )
    with pytest.raises(ValueError, match="must reference"):
        _load_reversible_write_config(environ)


@pytest.mark.asyncio
async def test_reversible_write_cycle_restores_after_success() -> None:
    marker = "SUYO_TDOC_E2E_test-marker"
    config = _ReversibleWriteConfig(
        document_id="dedicated-doc-id",
        baseline_text="SUYO_E2E_READY",
        read_tool="get_content",
        read_args={"doc_id": "dedicated-doc-id"},
        write_tool="batch_update_sheet_range",
        write_args={"value": WRITE_E2E_MARKER_PLACEHOLDER},
        restore_tool="batch_update_sheet_range",
        restore_args={"value": WRITE_E2E_BASELINE_PLACEHOLDER},
    )
    client = McpClient("tencent-docs", _client_config())
    client.call_tool = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            CallToolResult(content=[TextContent(type="text", text="SUYO_E2E_READY")]),
            CallToolResult(content=[TextContent(type="text", text="updated")]),
            CallToolResult(content=[TextContent(type="text", text=marker)]),
            CallToolResult(content=[TextContent(type="text", text="restored")]),
            CallToolResult(content=[TextContent(type="text", text="SUYO_E2E_READY")]),
        ]
    )

    await _run_reversible_write_cycle(client, config, marker=marker)

    assert client.call_tool.await_args_list == [
        call("get_content", {"doc_id": "dedicated-doc-id"}),
        call("batch_update_sheet_range", {"value": marker}),
        call("get_content", {"doc_id": "dedicated-doc-id"}),
        call("batch_update_sheet_range", {"value": "SUYO_E2E_READY"}),
        call("get_content", {"doc_id": "dedicated-doc-id"}),
    ]


@pytest.mark.asyncio
async def test_reversible_write_cycle_attempts_restore_after_ambiguous_write_failure() -> None:
    marker = "SUYO_TDOC_E2E_test-marker"
    config = _ReversibleWriteConfig(
        document_id="dedicated-doc-id",
        baseline_text="SUYO_E2E_READY",
        read_tool="get_content",
        read_args={"doc_id": "dedicated-doc-id"},
        write_tool="batch_update_sheet_range",
        write_args={"value": WRITE_E2E_MARKER_PLACEHOLDER},
        restore_tool="batch_update_sheet_range",
        restore_args={"value": WRITE_E2E_BASELINE_PLACEHOLDER},
    )
    client = McpClient("tencent-docs", _client_config())
    client.call_tool = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            CallToolResult(content=[TextContent(type="text", text="SUYO_E2E_READY")]),
            RuntimeError("ambiguous transport failure"),
            CallToolResult(content=[TextContent(type="text", text="restored")]),
            CallToolResult(content=[TextContent(type="text", text="SUYO_E2E_READY")]),
        ]
    )

    with pytest.raises(RuntimeError, match="ambiguous transport failure"):
        await _run_reversible_write_cycle(client, config, marker=marker)

    assert client.call_tool.await_args_list[-2:] == [
        call("batch_update_sheet_range", {"value": "SUYO_E2E_READY"}),
        call("get_content", {"doc_id": "dedicated-doc-id"}),
    ]


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("TENCENT_DOCS_E2E_TOKEN"),
    reason="Set TENCENT_DOCS_E2E_TOKEN to run the optional real-server contract",
)
async def test_optional_real_server_lists_allowlisted_tools() -> None:
    client = McpClient("tencent-docs", _client_config(timeout=30))
    client.set_oauth_token(os.environ["TENCENT_DOCS_E2E_TOKEN"])
    try:
        await client.connect()
        assert client.status == "connected", client.error
        names = {tool.name for tool in client.list_tools()}
        assert "get_content" in names
        assert all(client.is_tool_allowed(name) for name in names)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_optional_real_server_reversible_write_cycle() -> None:
    try:
        config = _load_reversible_write_config(os.environ)
    except ValueError as exc:
        pytest.fail(str(exc), pytrace=False)
    if config is None:
        pytest.skip(
            "Set TENCENT_DOCS_E2E_ALLOW_WRITE to the documented exact value "
            "and provide a dedicated reversible fixture to run the write cycle"
        )

    client = McpClient("tencent-docs", _client_config(timeout=30))
    client.set_oauth_token(os.environ["TENCENT_DOCS_E2E_TOKEN"])
    marker = f"SUYO_TDOC_E2E_{uuid.uuid4().hex}"
    try:
        await client.connect()
        assert client.status == "connected", client.error

        discovered = {tool.name for tool in client.list_tools()}
        required_tools = {config.read_tool, config.write_tool, config.restore_tool}
        assert required_tools <= discovered, (
            f"configured tools were not discovered: {sorted(required_tools - discovered)}"
        )
        assert client.tool_requires_approval(config.read_tool) is False
        assert client.tool_requires_approval(config.write_tool) is True
        assert client.tool_requires_approval(config.restore_tool) is True
        assert all(client.is_tool_allowed(name) for name in required_tools)

        await _run_reversible_write_cycle(client, config, marker=marker)
    finally:
        await client.close()
