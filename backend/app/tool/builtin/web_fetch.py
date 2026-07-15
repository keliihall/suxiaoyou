"""Web fetch tool — fetch URL content and convert to readable markdown."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from collections.abc import Awaitable, Callable, Sequence
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from app.tool.base import ToolDefinition, ToolResult
from app.tool.context import ToolContext

logger = logging.getLogger(__name__)

MAX_URL_LENGTH = 4_096
MAX_RETURN_CHARS = 200_000
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 5
DNS_TIMEOUT_SECONDS = 5.0
TOTAL_TIMEOUT_SECONDS = 45.0
REQUEST_TIMEOUT = httpx.Timeout(
    30.0,
    connect=10.0,
    read=20.0,
    write=10.0,
    pool=5.0,
)
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
HostResolver = Callable[
    [str, int],
    Awaitable[Sequence[str | IPAddress]],
]


class WebFetchSafetyError(RuntimeError):
    """A bounded, user-facing rejection before unsafe content is consumed."""


def _is_public_address(address: IPAddress) -> bool:
    """Accept only ordinary globally routable addresses.

    The explicit predicates document the v1 security contract.  ``is_global``
    additionally excludes shared/documentation/special-purpose ranges that are
    inappropriate for an internet-reading tool.  IPv6 transition mechanisms
    are rejected because their embedded IPv4 endpoint could otherwise bypass
    the corresponding IPv4 decision.
    """

    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or not address.is_global
    ):
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return False
        if address.sixtofour is not None or address.teredo is not None:
            return False
    return True


def _normalize_host(host: str) -> str:
    host = host.rstrip(".").lower()
    if not host or "%" in host:
        raise WebFetchSafetyError("URL must include a valid public host")
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise WebFetchSafetyError("URL must include a valid public host") from exc


async def _system_resolve(host: str, port: int) -> Sequence[str | IPAddress]:
    try:
        infos = await asyncio.wait_for(
            asyncio.to_thread(
                socket.getaddrinfo,
                host,
                port,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            ),
            timeout=DNS_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        raise WebFetchSafetyError("URL host DNS lookup timed out") from exc
    except OSError as exc:
        raise WebFetchSafetyError("Could not resolve URL host") from exc
    return [info[4][0] for info in infos]


class _CachingPublicResolver:
    """Resolve once per host/port and retain only validated public addresses."""

    def __init__(self, resolver: HostResolver | None = None) -> None:
        self._resolver = resolver or _system_resolve
        self._cache: dict[tuple[str, int], tuple[IPAddress, ...]] = {}

    async def resolve(self, host: str, port: int) -> tuple[IPAddress, ...]:
        normalized_host = _normalize_host(host)
        key = (normalized_host, port)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        try:
            literal = ipaddress.ip_address(normalized_host)
            raw_addresses: Sequence[str | IPAddress] = [literal]
        except ValueError:
            raw_addresses = await self._resolver(normalized_host, port)

        addresses: list[IPAddress] = []
        for value in raw_addresses:
            try:
                address = (
                    value
                    if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address))
                    else ipaddress.ip_address(value)
                )
            except ValueError as exc:
                raise WebFetchSafetyError(
                    "URL host returned an invalid DNS address"
                ) from exc
            if not _is_public_address(address):
                raise WebFetchSafetyError(
                    "URL host resolves to a non-public network address"
                )
            if address not in addresses:
                addresses.append(address)

        if not addresses:
            raise WebFetchSafetyError("URL host has no usable public address")
        result = tuple(addresses)
        self._cache[key] = result
        return result


class _PinnedNetworkBackend:
    """Make the actual socket use the addresses that passed SSRF validation."""

    def __init__(self, resolver: _CachingPublicResolver, backend: Any) -> None:
        self._resolver = resolver
        self._backend = backend

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> Any:
        host_text = host.decode("ascii") if isinstance(host, bytes) else host
        addresses = await self._resolver.resolve(host_text, port)
        last_error: Exception | None = None
        for address in addresses:
            try:
                # httpcore performs TLS afterwards and preserves the original
                # hostname for SNI/certificate validation.  Only TCP routing is
                # pinned to the approved IP here.
                return await self._backend.connect_tcp(
                    str(address),
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except Exception as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    async def connect_unix_socket(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Unix sockets are not allowed for web_fetch")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


def _build_pinned_client(resolver: _CachingPublicResolver) -> httpx.AsyncClient:
    transport = httpx.AsyncHTTPTransport(
        trust_env=False,
        http2=False,
        limits=httpx.Limits(
            max_connections=4,
            max_keepalive_connections=0,
        ),
    )
    # httpx does not currently expose httpcore's network_backend constructor
    # argument.  Keep this one compatibility seam local and covered by tests;
    # it prevents DNS rebinding between validation and socket creation.
    transport._pool._network_backend = _PinnedNetworkBackend(  # type: ignore[attr-defined]
        resolver,
        transport._pool._network_backend,  # type: ignore[attr-defined]
    )
    return httpx.AsyncClient(
        transport=transport,
        follow_redirects=False,
        timeout=REQUEST_TIMEOUT,
        trust_env=False,
    )


async def _validate_public_url(
    url: str,
    resolver: _CachingPublicResolver,
) -> str:
    if not isinstance(url, str) or not url or len(url) > MAX_URL_LENGTH:
        raise WebFetchSafetyError("URL is empty or too long")
    if url != url.strip() or any(ord(char) < 32 or ord(char) == 127 for char in url):
        raise WebFetchSafetyError("URL contains whitespace or control characters")
    if "\\" in url:
        raise WebFetchSafetyError("URL contains an unsafe path separator")

    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise WebFetchSafetyError("URL is malformed") from exc

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise WebFetchSafetyError("URL must use http:// or https://")
    if not parsed.netloc or parsed.hostname is None:
        raise WebFetchSafetyError("URL must include a valid host")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise WebFetchSafetyError("Credential-bearing URLs are not allowed")
    if "%" in parsed.netloc or parsed.netloc.endswith(":"):
        raise WebFetchSafetyError("URL contains an unsafe authority form")
    if port == 0:
        raise WebFetchSafetyError("URL contains an invalid port")

    host = _normalize_host(parsed.hostname)
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise WebFetchSafetyError("Local network hosts are not allowed")
    effective_port = port or (443 if scheme == "https" else 80)
    await resolver.resolve(host, effective_port)

    # Fragments are client-side only and should not participate in redirect or
    # audit metadata.  Preserve the path/query exactly as supplied.
    return urlunsplit((scheme, parsed.netloc, parsed.path, parsed.query, ""))


def _normalized_content_type(response: httpx.Response) -> str:
    return response.headers.get("content-type", "").split(";", 1)[0].strip().lower()


def _is_supported_content_type(content_type: str) -> bool:
    if not content_type:
        # RFC 9110 permits omission.  The strict decoded-size budget still
        # prevents binary/memory abuse, and UTF-8 replacement keeps output text.
        return True
    if content_type.startswith("text/"):
        return True
    if content_type in {
        "application/json",
        "application/ld+json",
        "application/problem+json",
        "application/x-ndjson",
        "application/xml",
        "application/xhtml+xml",
        "application/rss+xml",
        "application/atom+xml",
        "application/javascript",
    }:
        return True
    return content_type.endswith("+json") or content_type.endswith("+xml")


def _declared_content_length(response: httpx.Response) -> int | None:
    value = response.headers.get("content-length")
    if value is None:
        return None
    if not value.isascii() or not value.isdecimal():
        raise WebFetchSafetyError("Response has an invalid Content-Length header")
    return int(value)


def _looks_like_utf8_text(data: bytes) -> bool:
    """Conservatively classify a body when the server omitted Content-Type."""

    if not data:
        return True
    control_count = sum(
        byte < 32 and byte not in {9, 10, 13}
        for byte in data
    )
    if control_count > max(1, len(data) // 100):
        return False
    try:
        data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return False
    return True


async def _read_bounded_response(
    response: httpx.Response,
    *,
    ctx: ToolContext,
) -> tuple[bytes, str]:
    content_type = _normalized_content_type(response)
    if not _is_supported_content_type(content_type):
        raise WebFetchSafetyError(
            f"Unsupported response content type: {content_type or 'unknown'}"
        )
    declared = _declared_content_length(response)
    if declared is not None and declared > MAX_RESPONSE_BYTES:
        raise WebFetchSafetyError(
            f"Response exceeds the {MAX_RESPONSE_BYTES}-byte download limit"
        )

    chunks: list[bytes] = []
    received = 0
    async for chunk in response.aiter_bytes():
        if ctx.is_aborted:
            raise WebFetchSafetyError("Web fetch was cancelled")
        received += len(chunk)
        if received > MAX_RESPONSE_BYTES:
            raise WebFetchSafetyError(
                f"Response exceeds the {MAX_RESPONSE_BYTES}-byte download limit"
            )
        chunks.append(chunk)
    body = b"".join(chunks)
    if not content_type and not _looks_like_utf8_text(body):
        raise WebFetchSafetyError("Response without Content-Type is not UTF-8 text")
    return body, content_type


class WebFetchTool(ToolDefinition):

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        resolver: HostResolver | None = None,
    ) -> None:
        # An injected client/resolver keeps security tests deterministic and
        # avoids real network traffic.  Production calls create a fresh pinned
        # client per execution so concurrent tool calls cannot share DNS state.
        self._client = client
        self._resolver = resolver

    @property
    def id(self) -> str:
        return "web_fetch"

    @property
    def is_concurrency_safe(self) -> bool:
        return True

    @property
    def description(self) -> str:
        return (
            "Fetch content from a URL and return it as readable markdown. "
            "Useful for reading documentation, API responses, and web pages."
        )

    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to fetch",
                },
                "max_length": {
                    "type": "integer",
                    "description": "Maximum content length to return (default: 50000)",
                    "default": 50000,
                    "minimum": 1,
                    "maximum": MAX_RETURN_CHARS,
                },
            },
            "required": ["url"],
        }

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        requested_url = args["url"]
        max_length = args.get("max_length", 50000)
        if (
            isinstance(max_length, bool)
            or not isinstance(max_length, int)
            or not 1 <= max_length <= MAX_RETURN_CHARS
        ):
            return ToolResult(
                error=f"max_length must be between 1 and {MAX_RETURN_CHARS}"
            )

        resolver = _CachingPublicResolver(self._resolver)
        client = self._client or _build_pinned_client(resolver)
        owns_client = self._client is None

        try:
            async with asyncio.timeout(TOTAL_TIMEOUT_SECONDS):
                current_url = await _validate_public_url(requested_url, resolver)
                redirects = 0
                body = b""
                content_type = ""
                status_code = 0

                while True:
                    if ctx.is_aborted:
                        raise WebFetchSafetyError("Web fetch was cancelled")
                    async with client.stream(
                        "GET",
                        current_url,
                        headers={
                            "User-Agent": "suyo/1.0 (tool; web_fetch)",
                            "Accept": (
                                "text/html, text/plain, application/json, "
                                "application/xml;q=0.9, */*;q=0.1"
                            ),
                        },
                        follow_redirects=False,
                    ) as response:
                        status_code = response.status_code
                        if status_code in REDIRECT_STATUSES:
                            location = response.headers.get("location")
                            if not location:
                                raise WebFetchSafetyError(
                                    "Redirect response is missing a Location header"
                                )
                            if redirects >= MAX_REDIRECTS:
                                raise WebFetchSafetyError(
                                    f"Too many redirects (maximum {MAX_REDIRECTS})"
                                )
                            next_url = urljoin(current_url, location)
                            current_url = await _validate_public_url(
                                next_url,
                                resolver,
                            )
                            redirects += 1
                            continue

                        response.raise_for_status()
                        body, content_type = await _read_bounded_response(
                            response,
                            ctx=ctx,
                        )
                        encoding = response.encoding or "utf-8"
                        try:
                            text = body.decode(encoding, errors="replace")
                        except LookupError:
                            text = body.decode("utf-8", errors="replace")
                        break

                # Parsing untrusted HTML is part of the same total budget as
                # DNS, redirects, and download. Run the synchronous parser off
                # the event loop so the timeout can still interrupt this tool.
                if content_type in {"text/html", "application/xhtml+xml"}:
                    text = await asyncio.to_thread(
                        extract_readable_content,
                        text,
                        current_url,
                    )

                if len(text) > max_length:
                    text = text[:max_length] + f"\n\n... [truncated at {max_length} chars]"

            return ToolResult(
                output=text,
                title=ctx.tr(
                    f"已获取 {current_url[:60]}",
                    f"Fetched {current_url[:60]}",
                ),
                metadata={
                    "url": current_url,
                    "requested_url": requested_url,
                    "status_code": status_code,
                    "length": len(text),
                    "bytes_received": len(body),
                    "content_type": content_type or None,
                    "redirects": redirects,
                },
            )

        except WebFetchSafetyError as exc:
            return ToolResult(error=f"Web fetch blocked: {exc}")
        except TimeoutError:
            return ToolResult(
                error=f"Request timed out after {TOTAL_TIMEOUT_SECONDS:g} seconds"
            )
        except httpx.HTTPStatusError as e:
            return ToolResult(error=f"HTTP {e.response.status_code}: {requested_url}")
        except httpx.HTTPError as e:
            return ToolResult(error=f"Request failed: {e}")
        finally:
            if owns_client:
                await client.aclose()


# ---------------------------------------------------------------------------
# HTML → readable markdown extraction
# ---------------------------------------------------------------------------

def extract_readable_content(html: str, url: str = "") -> str:
    """Extract main article content from HTML and convert to markdown.

    Uses readabilipy (Readability algorithm) + markdownify for high-quality
    extraction.  Falls back to regex stripping if the libraries fail.
    """
    try:
        return _readability_extract(html, url)
    except Exception as e:
        logger.debug("Readability extraction failed (%s), falling back to regex", e)
        return _strip_html(html)


def _readability_extract(html: str, url: str = "") -> str:
    """Readability-based extraction → markdown."""
    from readabilipy import simple_json_from_html_string
    from markdownify import markdownify as md

    try:
        article = simple_json_from_html_string(html, use_readability=True)
    except Exception:
        # Readability.js unavailable — fall back to pure-Python extraction
        article = simple_json_from_html_string(html, use_readability=False)

    title = (article.get("title") or "").strip() or None
    html_content = article.get("content") or ""

    if not html_content.strip():
        # Readability couldn't find an article body — fall back
        raise ValueError("Readability extracted empty content")

    # Convert HTML fragment → markdown
    markdown = md(html_content, strip=["img"]).strip()

    # Collapse excessive blank lines (3+ → 2)
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)

    if title:
        markdown = f"# {title}\n\n{markdown}"

    return markdown


def _strip_html(html: str) -> str:
    """Regex fallback — basic HTML tag removal."""
    # Remove script and style blocks
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # Remove tags
    text = re.sub(r"<[^>]+>", " ", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text
