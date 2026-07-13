"""Backend-generated display text follows the request language."""

from __future__ import annotations

import inspect
import io
import re

import pytest
from pypdf import PdfReader

from app.agent.agent import BUILTIN_AGENTS
from app.api.callback_html import render_callback
from app.api.openai_auth import _error_html, _success_html
from app.api.pdf import html_to_pdf
from app.i18n import Language, normalize_language, product_name
from app.mcp.oauth import register_client
from app.provider.catalog import PROVIDER_CATALOG
from app.provider.openrouter import OpenRouterProvider
from app.streaming.manager import GenerationJob
from app.tool.builtin.invalid import InvalidTool
from app.tool.builtin.plan import PlanTool
from app.tool.builtin.todo import TodoTool
from app.tool.builtin.web_search import WebSearchTool
from app.tool.builtin.write import WriteTool
from app.tool.context import ToolContext


_CJK = re.compile(r"[\u3400-\u9fff]")


def _ctx(language: Language = "zh", *, workspace: str | None = None) -> ToolContext:
    return ToolContext(
        session_id="session",
        message_id="message",
        agent=BUILTIN_AGENTS["build"],
        call_id="call",
        language=language,
        workspace=workspace,
    )


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, "zh"),
        ("en-US,en;q=0.9,zh;q=0.8", "en"),
        ("zh-CN;q=0.5,en;q=0.9", "en"),
        ("en;q=0,zh-CN;q=0.7", "zh"),
        ("fr-FR, en-GB;q=0.4", "en"),
        ("fr-FR", "zh"),
    ],
)
def test_accept_language_normalization(header: str | None, expected: str) -> None:
    assert normalize_language(header) == expected


def test_localized_product_and_provider_names() -> None:
    assert product_name("zh") == "苏小有"
    assert product_name("en") == "suyo"
    assert PROVIDER_CATALOG["qwen"].display_name("zh") == "通义千问"
    assert PROVIDER_CATALOG["qwen"].display_name("en") == "Qwen"
    assert not _CJK.search(PROVIDER_CATALOG["xiaomi"].display_name("en"))


def test_generation_job_has_typed_language_state() -> None:
    assert GenerationJob("stream", "session").language == "zh"
    assert GenerationJob("stream", "session", language="en").language == "en"


def test_english_todo_and_web_search_activity_has_no_cjk() -> None:
    ctx = _ctx("en")
    todo = TodoTool._build_result(
        [
            {"content": "One", "status": "completed"},
            {"content": "Two", "status": "in_progress"},
        ],
        ctx,
    )
    search = WebSearchTool._format_serper_results(
        "example",
        10,
        {"organic": []},
        {},
        ctx,
    )
    assert todo.title == "Todo list"
    assert "1/2 completed" in todo.output
    assert search.title == "Search: example"
    assert search.output == "No results found."
    assert not _CJK.search(f"{todo.title}{todo.output}{search.title}{search.output}")


@pytest.mark.asyncio
async def test_english_write_plan_and_error_activity_has_no_cjk(tmp_path) -> None:
    ctx = _ctx("en", workspace=str(tmp_path))
    written = await WriteTool().execute(
        {"file_path": "result.txt", "content": "hello\n"},
        ctx,
    )
    plan = await PlanTool().execute({"command": "enter"}, ctx)
    invalid = await InvalidTool().execute({"name": "missing"}, ctx)
    combined = f"{written.title}{written.output}{plan.output}{invalid.error}"
    assert written.title == "Created result.txt"
    assert "Switched to plan mode" in plan.output
    assert 'Tool "missing" is unavailable' in invalid.error
    assert not _CJK.search(combined)


@pytest.mark.asyncio
async def test_provider_endpoint_uses_accept_language(app_client) -> None:
    english = await app_client.get(
        "/api/config/providers",
        headers={"Accept-Language": "en-US,en;q=0.9"},
    )
    chinese = await app_client.get(
        "/api/config/providers",
        headers={"Accept-Language": "zh-CN"},
    )
    assert english.status_code == 200
    assert chinese.status_code == 200
    en_by_id = {item["id"]: item["name"] for item in english.json()}
    zh_by_id = {item["id"]: item["name"] for item in chinese.json()}
    assert en_by_id["qwen"] == "Qwen"
    assert zh_by_id["qwen"] == "通义千问"
    assert not any(_CJK.search(name) for name in en_by_id.values())


def test_oauth_callback_uses_english_brand() -> None:
    page = render_callback(True)
    assert "<title>suyo — Connected</title>" in page
    assert "苏小有" not in page


def test_external_metadata_uses_neutral_english_brand() -> None:
    provider = OpenRouterProvider("test-key")
    assert provider._client.default_headers["X-Title"] == "suyo"
    assert inspect.signature(register_client).parameters["client_name"].default == "suyo"
    assert "苏小有" not in _success_html("user@example.com")
    assert "苏小有" not in _error_html("failed")

    metadata = PdfReader(io.BytesIO(html_to_pdf("<p>Hello</p>"))).metadata
    assert metadata.title == "suyo export"
    assert metadata.author == "suyo"
