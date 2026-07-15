"""Tests for app.tool.builtin.web_fetch HTML extraction."""

from __future__ import annotations

import asyncio

import httpx
import pytest

import app.tool.builtin.web_fetch as web_fetch
from app.schemas.agent import AgentInfo
from app.tool.builtin.web_fetch import (
    WebFetchTool,
    _CachingPublicResolver,
    _PinnedNetworkBackend,
    _build_pinned_client,
    _strip_html,
    extract_readable_content,
)
from app.tool.context import ToolContext


PUBLIC_IP = "93.184.216.34"


def _ctx() -> ToolContext:
    return ToolContext(
        session_id="web-fetch-session",
        message_id="web-fetch-message",
        agent=AgentInfo(name="test", description="", mode="primary"),
        call_id="web-fetch-call",
    )


async def _public_resolver(_host: str, _port: int):
    return [PUBLIC_IP]


async def _run_tool(
    handler,
    *,
    url: str = "https://example.com/article",
    resolver=_public_resolver,
    max_length: int = 50_000,
):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        tool = WebFetchTool(client=client, resolver=resolver)
        return await tool.execute(
            {"url": url, "max_length": max_length},
            _ctx(),
        )


def test_extracts_article_as_markdown_without_regex_fallback(monkeypatch) -> None:
    html = """
    <!doctype html>
    <html>
      <head><title>Packaging contracts</title></head>
      <body>
        <nav>Navigation noise that must not be returned.</nav>
        <main>
          <article>
            <h1>Reliable releases</h1>
            <p>A sufficiently detailed article paragraph explains why the
               packaged application must include its runtime dependencies.</p>
            <p>It also preserves <a href="https://example.com/source">source links</a>
               when converted to readable Markdown for the assistant.</p>
          </article>
        </main>
        <script>window.tracking = true;</script>
      </body>
    </html>
    """

    def fail_if_regex_fallback_runs(_html: str) -> str:
        raise AssertionError("regex fallback was used")

    monkeypatch.setattr(web_fetch, "_strip_html", fail_if_regex_fallback_runs)

    result = extract_readable_content(html, "https://example.com/article")

    assert result.startswith("# Packaging contracts")
    assert "Reliable releases" in result
    assert "[source links](https://example.com/source)" in result
    assert "Navigation noise" not in result
    assert "window.tracking" not in result


class TestStripHtml:
    def test_removes_script_tags(self):
        html = '<p>hello</p><script>alert("xss")</script><p>world</p>'
        result = _strip_html(html)
        assert "alert" not in result
        assert "hello" in result
        assert "world" in result

    def test_removes_style_tags(self):
        html = "<style>body{color:red}</style><p>text</p>"
        result = _strip_html(html)
        assert "color" not in result
        assert "text" in result

    def test_removes_html_tags(self):
        html = "<div><p>hello</p></div>"
        result = _strip_html(html)
        assert "hello" in result
        assert "<" not in result

    def test_collapses_whitespace(self):
        html = "<p>hello</p>   \n\n   <p>world</p>"
        result = _strip_html(html)
        # Multiple spaces should be collapsed
        assert "  " not in result

    def test_nested_tags(self):
        html = "<div><span><b>nested</b></span></div>"
        result = _strip_html(html)
        assert "nested" in result
        assert "<" not in result


class TestWebFetchNetworkBoundary:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "ftp://example.com/file",
            "http://user:secret@example.com/",
            "http://127.0.0.1/",
            "http://[::1]/",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.8/",
            "http://224.0.0.1/",
            "http://0.0.0.0/",
            "http://example.com:0/",
            "http://example.com\\@127.0.0.1/",
        ],
    )
    async def test_rejects_unsafe_url_forms_before_request(self, url: str):
        calls = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, text="must not run")

        result = await _run_tool(handler, url=url)

        assert not result.success
        assert "blocked" in result.error.lower()
        assert calls == 0

    @pytest.mark.asyncio
    async def test_rejects_hostname_when_any_dns_answer_is_private(self):
        async def mixed_resolver(_host: str, _port: int):
            return [PUBLIC_IP, "10.1.2.3"]

        def handler(_request: httpx.Request) -> httpx.Response:
            raise AssertionError("request must not be attempted")

        result = await _run_tool(handler, resolver=mixed_resolver)

        assert not result.success
        assert "non-public" in result.error

    @pytest.mark.asyncio
    async def test_redirect_target_is_revalidated_before_second_request(self):
        requests: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(str(request.url))
            return httpx.Response(
                302,
                headers={"location": "http://127.0.0.1/admin"},
            )

        result = await _run_tool(handler)

        assert not result.success
        assert "non-public" in result.error
        assert requests == ["https://example.com/article"]

    @pytest.mark.asyncio
    async def test_follows_bounded_public_redirect_and_reports_final_url(self):
        requests: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(str(request.url))
            if request.url.host == "example.com":
                return httpx.Response(
                    302,
                    headers={"location": "https://docs.example.org/page"},
                )
            return httpx.Response(
                200,
                headers={"content-type": "text/plain; charset=utf-8"},
                text="safe documentation",
            )

        result = await _run_tool(handler)

        assert result.success
        assert result.output == "safe documentation"
        assert result.metadata["url"] == "https://docs.example.org/page"
        assert result.metadata["redirects"] == 1
        assert requests == [
            "https://example.com/article",
            "https://docs.example.org/page",
        ]

    @pytest.mark.asyncio
    async def test_rejects_unsupported_binary_content_type(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "image/png"},
                content=b"\x89PNG\r\n\x1a\n",
            )

        result = await _run_tool(handler)

        assert not result.success
        assert "Unsupported response content type" in result.error

    @pytest.mark.asyncio
    async def test_missing_content_type_must_still_look_like_text(self):
        class BinaryStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"\x00\x01\x02\xffbinary"

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=BinaryStream())

        result = await _run_tool(handler)

        assert not result.success
        assert "not UTF-8 text" in result.error

    @pytest.mark.asyncio
    async def test_missing_content_type_checks_the_entire_bounded_body(self):
        class LateBinaryStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"a" * 65_536
                yield b"\xfflate-binary"

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=LateBinaryStream())

        result = await _run_tool(handler)

        assert not result.success
        assert "not UTF-8 text" in result.error

    @pytest.mark.asyncio
    async def test_rejects_declared_oversized_response(self, monkeypatch):
        monkeypatch.setattr(web_fetch, "MAX_RESPONSE_BYTES", 8)

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={
                    "content-type": "text/plain",
                    "content-length": "9",
                },
            )

        result = await _run_tool(handler)

        assert not result.success
        assert "8-byte download limit" in result.error

    @pytest.mark.asyncio
    async def test_streaming_limit_stops_chunked_response(self, monkeypatch):
        monkeypatch.setattr(web_fetch, "MAX_RESPONSE_BYTES", 8)

        class ChunkStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"123456"
                yield b"789"

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/plain"},
                stream=ChunkStream(),
            )

        result = await _run_tool(handler)

        assert not result.success
        assert "8-byte download limit" in result.error

    @pytest.mark.asyncio
    async def test_output_length_is_bounded_after_download(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/plain"},
                text="abcdefghij",
            )

        result = await _run_tool(handler, max_length=5)

        assert result.success
        assert result.output.startswith("abcde\n\n... [truncated at 5 chars]")

    @pytest.mark.asyncio
    async def test_whole_redirect_chain_has_a_total_timeout(self, monkeypatch):
        monkeypatch.setattr(web_fetch, "TOTAL_TIMEOUT_SECONDS", 0.001)

        async def handler(_request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0.05)
            return httpx.Response(200, text="late")

        result = await _run_tool(handler)

        assert not result.success
        assert "timed out" in result.error

    @pytest.mark.asyncio
    async def test_html_extraction_is_inside_the_total_timeout(self, monkeypatch):
        monkeypatch.setattr(web_fetch, "TOTAL_TIMEOUT_SECONDS", 0.001)

        def slow_extract(_html: str, _url: str) -> str:
            import time

            time.sleep(0.05)
            return "late"

        monkeypatch.setattr(web_fetch, "extract_readable_content", slow_extract)

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text="<main>bounded</main>",
            )

        result = await _run_tool(handler)

        assert not result.success
        assert "timed out" in result.error


@pytest.mark.asyncio
async def test_pinned_backend_connects_to_the_validated_ip_not_hostname():
    resolver_calls: list[tuple[str, int]] = []

    async def resolver(host: str, port: int):
        resolver_calls.append((host, port))
        return [PUBLIC_IP]

    class Backend:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int]] = []

        async def connect_tcp(self, host: str, port: int, **_kwargs):
            self.calls.append((host, port))
            return object()

        async def sleep(self, _seconds: float) -> None:
            return None

    cache = _CachingPublicResolver(resolver)
    backend = Backend()
    pinned = _PinnedNetworkBackend(cache, backend)

    await cache.resolve("example.com", 443)
    await pinned.connect_tcp("example.com", 443)

    assert backend.calls == [(PUBLIC_IP, 443)]
    assert resolver_calls == [("example.com", 443)]


@pytest.mark.asyncio
async def test_default_client_installs_pinned_network_backend():
    resolver = _CachingPublicResolver(_public_resolver)
    client = _build_pinned_client(resolver)
    try:
        assert isinstance(
            client._transport._pool._network_backend,  # type: ignore[attr-defined]
            _PinnedNetworkBackend,
        )
    finally:
        await client.aclose()
