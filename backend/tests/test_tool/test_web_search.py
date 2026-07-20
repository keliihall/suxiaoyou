"""Tests for app.tool.builtin.web_search — result parsing and formatting."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.tool.builtin import web_search as web_search_module
from app.tool.builtin.web_search import (
    WebSearchTool,
    _parse_bing_rss_results,
    _parse_ddg_results,
    _system_search_proxy,
)


class TestParseDdgResults:
    def test_extracts_results(self):
        html = '''
        <a class="result__a" href="https://example.com">Example <b>Title</b></a>
        <span class="result__snippet">A snippet</span>
        '''
        results = _parse_ddg_results(html, 10)
        assert len(results) == 1
        assert results[0]["title"] == "Example Title"
        assert "snippet" in results[0]["snippet"].lower()

    def test_respects_max_results(self):
        html = '''
        <a class="result__a" href="https://a.com">A</a>
        <span class="result__snippet">S1</span>
        <a class="result__a" href="https://b.com">B</a>
        <span class="result__snippet">S2</span>
        <a class="result__a" href="https://c.com">C</a>
        <span class="result__snippet">S3</span>
        '''
        results = _parse_ddg_results(html, 2)
        assert len(results) == 2

    def test_uddg_redirect(self):
        html = '''
        <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Freal.site.com%2Fpage&rut=abc">Title</a>
        <span class="result__snippet">Snippet</span>
        '''
        results = _parse_ddg_results(html, 10)
        assert len(results) == 1
        assert results[0]["url"] == "https://real.site.com/page"

    def test_empty_html(self):
        assert _parse_ddg_results("", 10) == []

    def test_strips_html_from_title(self):
        html = '''
        <a class="result__a" href="https://x.com"><b>Bold</b> Title</a>
        <span class="result__snippet">Snip</span>
        '''
        results = _parse_ddg_results(html, 10)
        assert results[0]["title"] == "Bold Title"


class TestParseBingRssResults:
    def test_extracts_and_cleans_results(self):
        xml = """
        <rss><channel><item>
          <title>Example &amp; Result</title>
          <link>https://example.com/article</link>
          <description>&lt;b&gt;Useful&lt;/b&gt; snippet</description>
        </item></channel></rss>
        """
        results = _parse_bing_rss_results(xml, 10)
        assert results == [{
            "title": "Example & Result",
            "url": "https://example.com/article",
            "snippet": "Useful snippet",
        }]

    def test_respects_max_results(self):
        xml = """
        <rss><channel>
          <item><title>One</title><link>https://one.example</link></item>
          <item><title>Two</title><link>https://two.example</link></item>
        </channel></rss>
        """
        assert len(_parse_bing_rss_results(xml, 1)) == 1


def test_search_uses_valid_macos_system_https_proxy(monkeypatch):
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(web_search_module, "proxy_bypass", lambda _host: False)
    monkeypatch.setattr(
        web_search_module,
        "getproxies",
        lambda: {"https": "127.0.0.1:7897"},
    )

    assert _system_search_proxy("https://html.duckduckgo.com") == (
        "http://127.0.0.1:7897"
    )


def test_search_rejects_unsupported_system_proxy_scheme(monkeypatch):
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(web_search_module, "proxy_bypass", lambda _host: False)
    monkeypatch.setattr(
        web_search_module,
        "getproxies",
        lambda: {"https": "socks5://127.0.0.1:7897"},
    )

    assert _system_search_proxy("https://html.duckduckgo.com") is None


class _FakeSearchResponse:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


class _FakeSearchClient:
    def __init__(self, *, bing_fails: bool = False, **_kwargs) -> None:
        self._bing_fails = bing_fails

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def get(self, url: str, **_kwargs):
        if "duckduckgo" in url:
            raise httpx.ConnectTimeout("duckduckgo timeout")
        if self._bing_fails:
            raise httpx.ConnectError("bing failed")
        return _FakeSearchResponse("""
            <rss><channel><item>
              <title>Fallback result</title>
              <link>https://fallback.example</link>
              <description>Available through Bing RSS</description>
            </item></channel></rss>
        """)


@pytest.mark.asyncio
async def test_ddg_timeout_falls_back_to_bing_rss(monkeypatch):
    monkeypatch.setattr(web_search_module.httpx, "AsyncClient", _FakeSearchClient)
    ctx = SimpleNamespace(language="zh", tr=lambda zh, _en: zh)

    result = await WebSearchTool()._search_ddg("test query", 10, ctx)

    assert result.success
    assert "Fallback result" in result.output
    assert result.metadata["provider"] == "bing_rss"
    assert result.metadata["fallback_from"] == "duckduckgo"


@pytest.mark.asyncio
async def test_all_public_backends_report_actionable_failure(monkeypatch):
    class FailingSearchClient(_FakeSearchClient):
        def __init__(self, **kwargs) -> None:
            super().__init__(bing_fails=True, **kwargs)

    monkeypatch.setattr(web_search_module.httpx, "AsyncClient", FailingSearchClient)
    ctx = SimpleNamespace(language="zh", tr=lambda zh, _en: zh)

    result = await WebSearchTool()._search_ddg("test query", 10, ctx)

    assert not result.success
    assert "DuckDuckGo 连接超时" in result.error
    assert "Bing 连接失败" in result.error
    assert result.metadata["error_code"] == "search_backends_unavailable"


class TestFormatSerperResults:
    def test_organic_results(self):
        data = {"organic": [
            {"title": "Result 1", "link": "https://a.com", "snippet": "Snip 1"},
            {"title": "Result 2", "link": "https://b.com", "snippet": "Snip 2"},
        ]}
        usage = {"hosted_search_used": True, "daily_searches_used": 5, "daily_search_limit": 100}
        result = WebSearchTool._format_serper_results("test", 10, data, usage)
        assert result.success
        assert "Result 1" in result.output
        assert "Result 2" in result.output
        assert result.metadata["count"] == 2

    def test_knowledge_graph(self):
        data = {
            "knowledgeGraph": {"title": "Python", "type": "Language", "description": "A programming language"},
            "organic": [{"title": "R1", "link": "https://a.com", "snippet": "S1"}],
        }
        usage = {}
        result = WebSearchTool._format_serper_results("test", 10, data, usage)
        assert "[Knowledge Graph] Python" in result.output

    def test_no_results(self):
        data = {"organic": []}
        usage = {"hosted_search_used": False, "daily_searches_used": 0, "daily_search_limit": 100}
        result = WebSearchTool._format_serper_results("test", 10, data, usage)
        assert result.output == "未找到结果。"
        assert "hosted_search_used" in result.metadata

    def test_hosted_search_usage_meta(self):
        data = {"organic": [{"title": "R1", "link": "https://a.com", "snippet": "S1"}]}
        usage = {"hosted_search_used": True, "daily_searches_used": 10, "daily_search_limit": 50}
        result = WebSearchTool._format_serper_results("test", 10, data, usage)
        assert result.metadata["hosted_search_used"] is True
        assert result.metadata["daily_searches_used"] == 10

    def test_respects_max_results_cap(self):
        data = {"organic": [
            {"title": f"R{i}", "link": f"https://{i}.com", "snippet": f"S{i}"}
            for i in range(20)
        ]}
        usage = {}
        result = WebSearchTool._format_serper_results("test", 3, data, usage)
        assert result.metadata["count"] == 3
