"""Web search via the 苏小有 Proxy or keyless public-search fallbacks.

When proxy mode is active, searches go through the deployed proxy which
holds the Serper API key and handles hosted-search limits. Without proxy, falls
back to DuckDuckGo with the desktop system HTTP(S) proxy when available, then
to Bing RSS if the first endpoint cannot provide results.
"""

from __future__ import annotations

import html
import os
import re
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import quote_plus, urlparse
from urllib.request import getproxies, proxy_bypass

import httpx

from app.i18n import localize
from app.tool.base import ToolDefinition, ToolResult
from app.tool.context import ToolContext


class WebSearchTool(ToolDefinition):

    @property
    def id(self) -> str:
        return "web_search"

    @property
    def is_concurrency_safe(self) -> bool:
        return True

    @property
    def description(self) -> str:
        return (
            "Search the web for information. Returns search results with titles and URLs. "
            "For time-sensitive queries, include the current year in the search query "
            "to get recent results."
        )

    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results (default: 10)",
                    "default": 10,
                },
            },
            "required": ["query"],
        }

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        query = args["query"]
        max_results = args.get("max_results", 10)

        from app.config import get_settings
        settings = get_settings()

        if settings.proxy_url and settings.proxy_token:
            from app.auth.credential_store import resolve_env_value

            proxy_token = resolve_env_value(
                "SUXIAOYOU_PROXY_TOKEN",
                settings.proxy_token,
            )
            return await self._search_proxy(
                query, max_results,
                settings.proxy_url, proxy_token,
                ctx,
            )
        return await self._search_ddg(query, max_results, ctx)

    # ------------------------------------------------------------------ #
    # Proxy search (Serper via deployed proxy)
    # ------------------------------------------------------------------ #

    async def _search_proxy(
        self, query: str, max_results: int,
        proxy_url: str, proxy_token: str,
        ctx: ToolContext | None = None,
    ) -> ToolResult:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{proxy_url.rstrip('/')}/api/search",
                    json={"q": query, "num": max_results},
                    headers={"Authorization": f"Bearer {proxy_token}"},
                )

            if resp.status_code == 429:
                return ToolResult(
                    error=_tr(
                        ctx,
                        "已达到每日网络搜索上限。",
                        "The daily web-search limit has been reached.",
                    ),
                    metadata={"error_code": "search_daily_limit"},
                )
            if resp.status_code == 402:
                return ToolResult(
                    error=_tr(
                        ctx,
                        "托管网络搜索当前不可用。",
                        "Hosted web search is currently unavailable.",
                    ),
                    metadata={"error_code": "hosted_search_unavailable"},
                )
            if resp.status_code != 200:
                return ToolResult(
                    error=_tr(
                        ctx,
                        f"托管网络搜索返回 HTTP {resp.status_code}。",
                        f"Hosted web search returned HTTP {resp.status_code}.",
                    ),
                    metadata={
                        "error_code": "search_http_error",
                        "http_status": resp.status_code,
                    },
                )

            data = resp.json()
            serper_data = data.get("results", {})
            proxy_usage = data.get("usage", {})

            # Store hosted-search usage info in metadata for processor to read.
            usage_meta = {
                "hosted_search_used": proxy_usage.get("charged", False),
                "daily_searches_used": proxy_usage.get("daily_searches_used", 0),
                "daily_search_limit": proxy_usage.get("daily_search_limit", 0),
            }

            return self._format_serper_results(query, max_results, serper_data, usage_meta, ctx)

        except Exception as exc:
            return ToolResult(
                error=_tr(
                    ctx,
                    f"托管网络搜索失败：{_search_failure_reason('搜索服务', exc, ctx)}。",
                    f"Hosted web search failed: "
                    f"{_search_failure_reason('search service', exc, ctx)}.",
                ),
                metadata={
                    "error_code": _search_error_code(exc),
                    "provider": "hosted_proxy",
                },
            )

    @staticmethod
    def _format_serper_results(
        query: str, max_results: int,
        data: dict[str, Any], usage_meta: dict[str, Any],
        ctx: ToolContext | None = None,
    ) -> ToolResult:
        output_lines: list[str] = []
        results_data: list[dict[str, str]] = []

        # Knowledge Graph
        kg = data.get("knowledgeGraph")
        if kg:
            title = kg.get("title", "")
            kg_type = kg.get("type", "")
            desc = kg.get("description", "")
            output_lines.append(f"[Knowledge Graph] {title}")
            if kg_type:
                output_lines.append(f"   Type: {kg_type}")
            if desc:
                output_lines.append(f"   {desc}")
            attrs = kg.get("attributes", {})
            for k, v in list(attrs.items())[:5]:
                output_lines.append(f"   {k}: {v}")
            output_lines.append("")

        # Organic results
        organic = data.get("organic", [])
        for i, r in enumerate(organic[:max_results], 1):
            title = r.get("title", "")
            url = r.get("link", "")
            snippet = r.get("snippet", "")
            output_lines.append(f"{i}. {title}")
            output_lines.append(f"   {url}")
            if snippet:
                output_lines.append(f"   {snippet}")
            output_lines.append("")
            results_data.append({"url": url, "title": title, "snippet": snippet})

        if not organic:
            return ToolResult(
                output=(ctx.tr if ctx else lambda zh, en: localize("zh", zh, en))("未找到结果。", "No results found."),
                title=(ctx.tr if ctx else lambda zh, en: localize("zh", zh, en))(f"搜索：{query[:50]}", f"Search: {query[:50]}"),
                metadata=usage_meta,
            )

        count = min(len(organic), max_results)
        return ToolResult(
            output="\n".join(output_lines),
            title=(ctx.tr if ctx else lambda zh, en: localize("zh", zh, en))(
                f"搜索：{query[:50]}（{count} 条结果）",
                f"Search: {query[:50]} ({count} results)",
            ),
            metadata={
                "query": query,
                "count": count,
                "results": results_data,
                **usage_meta,
            },
        )

    # ------------------------------------------------------------------ #
    # DuckDuckGo fallback (no API key needed)
    # ------------------------------------------------------------------ #

    async def _search_ddg(
        self,
        query: str,
        max_results: int,
        ctx: ToolContext,
    ) -> ToolResult:
        failures: list[str] = []
        ddg_had_no_results = False
        try:
            async with _search_client(
                "https://html.duckduckgo.com",
            ) as client:
                resp = await client.get(
                    f"https://html.duckduckgo.com/html/?q={quote_plus(query)}",
                    headers={"User-Agent": "suyo/0.1"},
                )
                resp.raise_for_status()

            results = _parse_ddg_results(resp.text, max_results)
            if results:
                return _format_public_results(
                    query,
                    results,
                    ctx,
                    provider="duckduckgo",
                )
            ddg_had_no_results = True
        except Exception as exc:
            failures.append(_search_failure_reason("DuckDuckGo", exc, ctx))

        # DuckDuckGo's HTML endpoint is unavailable on some otherwise healthy
        # networks.  Bing RSS is a keyless, structured fallback and avoids
        # treating one blocked host as a global loss of connectivity.
        try:
            async with _search_client("https://www.bing.com") as client:
                resp = await client.get(
                    f"https://www.bing.com/search?q={quote_plus(query)}&format=rss",
                    headers={"User-Agent": "suyo/0.1"},
                    follow_redirects=True,
                )
                resp.raise_for_status()
            results = _parse_bing_rss_results(resp.text, max_results)
            if results:
                return _format_public_results(
                    query,
                    results,
                    ctx,
                    provider="bing_rss",
                    fallback_from="duckduckgo",
                )
            return _no_search_results(
                query,
                ctx,
                provider="bing_rss",
                fallback_from="duckduckgo",
            )
        except Exception as exc:
            failures.append(_search_failure_reason("Bing", exc, ctx))

        if ddg_had_no_results:
            return _no_search_results(
                query,
                ctx,
                provider="duckduckgo",
            )
        details = "；".join(failures) if ctx.language == "zh" else "; ".join(failures)
        return ToolResult(
            error=ctx.tr(
                f"网络搜索暂时不可用：{details}。请检查网络或代理设置后重试。",
                f"Web search is temporarily unavailable: {details}. "
                "Check the network or proxy settings and try again.",
            ),
            metadata={
                "error_code": "search_backends_unavailable",
                "providers": ["duckduckgo", "bing_rss"],
            },
        )


def _no_search_results(
    query: str,
    ctx: ToolContext,
    *,
    provider: str,
    fallback_from: str | None = None,
) -> ToolResult:
    metadata: dict[str, Any] = {
        "query": query,
        "count": 0,
        "results": [],
        "provider": provider,
    }
    if fallback_from:
        metadata["fallback_from"] = fallback_from
    return ToolResult(
        output=ctx.tr("未找到结果。", "No results found."),
        title=ctx.tr(f"搜索：{query[:50]}", f"Search: {query[:50]}"),
        metadata=metadata,
    )


def _search_client(endpoint: str) -> httpx.AsyncClient:
    proxy = _system_search_proxy(endpoint)
    kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(10.0, connect=5.0),
    }
    if proxy is not None:
        kwargs["proxy"] = proxy
    return httpx.AsyncClient(**kwargs)


def _system_search_proxy(endpoint: str) -> str | None:
    """Bridge macOS system HTTP(S) proxy settings into httpx search calls.

    httpx already honors explicit proxy environment variables.  The desktop
    process often has none even when macOS has a system proxy, so only that
    missing-env case needs an explicit value.  Search endpoints are fixed by
    this module; web_fetch deliberately keeps its separate SSRF-safe client.
    """

    proxy_env_keys = (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    )
    if any(os.environ.get(key) for key in proxy_env_keys):
        return None
    parsed_endpoint = urlparse(endpoint)
    hostname = parsed_endpoint.hostname
    if not hostname:
        return None
    try:
        if proxy_bypass(hostname):
            return None
        configured = getproxies()
    except (OSError, RuntimeError, ValueError):
        return None
    candidate = configured.get(parsed_endpoint.scheme) or configured.get("https")
    if not candidate:
        return None
    normalized = candidate if "://" in candidate else f"http://{candidate}"
    parsed_proxy = urlparse(normalized)
    if parsed_proxy.scheme not in {"http", "https"} or not parsed_proxy.hostname:
        return None
    return normalized


def _format_public_results(
    query: str,
    results: list[dict[str, str]],
    ctx: ToolContext,
    *,
    provider: str,
    fallback_from: str | None = None,
) -> ToolResult:
    output_lines: list[str] = []
    for index, result in enumerate(results, 1):
        output_lines.append(f"{index}. {result['title']}")
        output_lines.append(f"   {result['url']}")
        if result.get("snippet"):
            output_lines.append(f"   {result['snippet']}")
        output_lines.append("")
    metadata: dict[str, Any] = {
        "query": query,
        "count": len(results),
        "results": results,
        "provider": provider,
    }
    if fallback_from:
        metadata["fallback_from"] = fallback_from
    return ToolResult(
        output="\n".join(output_lines),
        title=ctx.tr(
            f"搜索：{query[:50]}（{len(results)} 条结果）",
            f"Search: {query[:50]} ({len(results)} results)",
        ),
        metadata=metadata,
    )


def _search_failure_reason(
    provider: str,
    exc: Exception,
    ctx: ToolContext | None,
) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return _tr(ctx, f"{provider} 连接超时", f"{provider} timed out")
    if isinstance(exc, httpx.ConnectError):
        return _tr(ctx, f"{provider} 连接失败", f"{provider} connection failed")
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return _tr(
            ctx,
            f"{provider} 返回 HTTP {status}",
            f"{provider} returned HTTP {status}",
        )
    return _tr(
        ctx,
        f"{provider} 返回无效响应（{type(exc).__name__}）",
        f"{provider} returned an invalid response ({type(exc).__name__})",
    )


def _search_error_code(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "search_timeout"
    if isinstance(exc, httpx.ConnectError):
        return "search_connect_failed"
    if isinstance(exc, httpx.HTTPStatusError):
        return "search_http_error"
    return "search_invalid_response"


def _tr(ctx: ToolContext | None, zh: str, en: str) -> str:
    return ctx.tr(zh, en) if ctx is not None else localize("zh", zh, en)


def _parse_ddg_results(html: str, max_results: int) -> list[dict[str, str]]:
    """Parse DuckDuckGo HTML search results."""
    results = []

    link_pattern = re.compile(
        r'class="result__a"[^>]*href="([^"]*)"[^>]*>(.+?)</a>', re.DOTALL
    )
    snippet_pattern = re.compile(
        r'class="result__snippet"[^>]*>(.+?)</(?:a|span|div)', re.DOTALL
    )

    links = link_pattern.findall(html)
    snippets = snippet_pattern.findall(html)

    for i, (url, title) in enumerate(links[:max_results]):
        title = re.sub(r"<[^>]+>", "", title).strip()
        snippet = ""
        if i < len(snippets):
            snippet = re.sub(r"<[^>]+>", "", snippets[i]).strip()

        if "uddg=" in url:
            from urllib.parse import parse_qs, urlparse
            parsed = urlparse(url)
            qs = parse_qs(parsed.query)
            if "uddg" in qs:
                url = qs["uddg"][0]

        results.append({"url": url, "title": title, "snippet": snippet})

    return results


def _parse_bing_rss_results(xml: str, max_results: int) -> list[dict[str, str]]:
    """Parse Bing's keyless RSS search response."""

    root = ET.fromstring(xml)
    results: list[dict[str, str]] = []
    for item in root.findall(".//item")[:max_results]:
        title = _plain_text(item.findtext("title") or "")
        url = (item.findtext("link") or "").strip()
        snippet = _plain_text(item.findtext("description") or "")
        if title and url:
            results.append({"url": url, "title": title, "snippet": snippet})
    return results


def _plain_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html.unescape(value))).strip()
