"""Bounded SiliconFlow text-to-image client.

The v1 contract intentionally supports one provider and one model.  Generated
URLs expire quickly, so the client validates and downloads the image before it
returns.  It never exposes the provider credential or the temporary URL to the
model-facing tool result.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import socket
import struct
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from io import BytesIO
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from PIL import Image, UnidentifiedImageError


SILICONFLOW_IMAGE_ENDPOINT = "https://api.siliconflow.cn/v1/images/generations"
SILICONFLOW_IMAGE_MODEL = "Kwai-Kolors/Kolors"
# Keep the approval/result contract explicit even while the selected model is
# listed as free.  The provider does not return a per-request charge in the
# image response, so this is a catalog estimate rather than an actual bill.
# The UI always labels it as an estimate and warns that provider pricing can
# change before the next release.
SILICONFLOW_IMAGE_ESTIMATED_COST_CNY = 0.0
SILICONFLOW_IMAGE_PRICING_AS_OF = "2026-07-14"
SILICONFLOW_IMAGE_PRICING_SOURCE_URL = "https://siliconflow.cn/pricing"
SILICONFLOW_IMAGE_SIZES = frozenset(
    {"1024x1024", "960x1280", "768x1024", "720x1440", "720x1280"}
)
MAX_PROMPT_CHARS = 4_000
MAX_NEGATIVE_PROMPT_CHARS = 2_000
MAX_IMAGE_BYTES = 50 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
MAX_REDIRECTS = 3
PNG_PREFIX = b"\x89PNG\r\n\x1a\n"
DATA_URI_PREFIX = "data:image/png;base64,"


class ImageGenerationError(RuntimeError):
    """A safe, user-facing image generation failure."""


class ImageGenerationCancelled(ImageGenerationError):
    """The caller cancelled a generation or download."""


class ImageGenerationBillingUncertain(ImageGenerationError):
    """The provider may have accepted a paid request but no artifact is safe."""


@dataclass(frozen=True)
class SiliconFlowImageResult:
    content: bytes
    width: int
    height: int
    seed: int | None
    trace_id: str | None


UrlValidator = Callable[[str], Awaitable[None]]


async def _validate_public_https_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ImageGenerationError("Image provider returned a non-HTTPS download URL")
    if parsed.username or parsed.password:
        raise ImageGenerationError("Image provider returned a credential-bearing URL")

    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".local"):
        raise ImageGenerationError("Image provider returned a local download URL")

    try:
        literal = ipaddress.ip_address(host)
        addresses = [literal]
    except ValueError:
        try:
            infos = await asyncio.to_thread(
                socket.getaddrinfo,
                host,
                parsed.port or 443,
                type=socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise ImageGenerationError("Could not resolve image download host") from exc
        addresses = []
        for info in infos:
            try:
                addresses.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                continue

    if not addresses:
        raise ImageGenerationError("Image download host has no usable address")
    for address in addresses:
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise ImageGenerationError("Image provider returned a private download URL")


async def _await_or_cancel(
    awaitable: Awaitable[Any],
    abort_event: asyncio.Event | None,
) -> Any:
    request_task = asyncio.create_task(awaitable)
    if abort_event is None:
        return await request_task

    abort_task = asyncio.create_task(abort_event.wait())
    try:
        done, _pending = await asyncio.wait(
            {request_task, abort_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if abort_task in done and abort_event.is_set():
            request_task.cancel()
            await asyncio.gather(request_task, return_exceptions=True)
            raise ImageGenerationCancelled("Image generation was cancelled")
        return await request_task
    finally:
        abort_task.cancel()
        await asyncio.gather(abort_task, return_exceptions=True)


def _bounded_error(response: httpx.Response) -> str:
    # Provider-controlled error bodies can echo an Authorization value or a
    # signed URL.  Model-visible errors therefore expose only the status code;
    # the request trace remains available through provider-side diagnostics.
    return f"Image provider returned HTTP {response.status_code}"


def _parse_png(content: bytes) -> tuple[int, int]:
    if len(content) < 24 or not content.startswith(PNG_PREFIX):
        raise ImageGenerationError("Image provider response is not a valid PNG")
    if content[12:16] != b"IHDR":
        raise ImageGenerationError("Image provider response has an invalid PNG header")
    width, height = struct.unpack(">II", content[16:24])
    if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
        raise ImageGenerationError("Generated image dimensions exceed the safety budget")
    try:
        with Image.open(BytesIO(content)) as image:
            if image.format != "PNG" or image.size != (width, height):
                raise ImageGenerationError("Image provider response is not a valid PNG")
            image.verify()
        # ``verify`` validates the container; reopening and loading validates
        # the compressed pixel stream and rejects truncated/CRC-invalid files.
        with Image.open(BytesIO(content)) as image:
            if image.format != "PNG" or image.size != (width, height):
                raise ImageGenerationError("Image provider response is not a valid PNG")
            image.load()
    except ImageGenerationError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise ImageGenerationError("Image provider response is not a valid PNG") from exc
    return width, height


class SiliconFlowImageClient:
    """Generate one PNG through SiliconFlow and download it immediately."""

    def __init__(
        self,
        api_key: str,
        *,
        client: httpx.AsyncClient | None = None,
        url_validator: UrlValidator = _validate_public_https_url,
    ) -> None:
        if not api_key.strip():
            raise ValueError("SiliconFlow API key is required")
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(180.0, connect=30.0),
            follow_redirects=False,
        )
        self._owns_client = client is None
        self._validate_url = url_validator

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def generate(
        self,
        *,
        prompt: str,
        image_size: str = "1024x1024",
        negative_prompt: str | None = None,
        seed: int | None = None,
        num_inference_steps: int = 20,
        guidance_scale: float = 7.5,
        abort_event: asyncio.Event | None = None,
    ) -> SiliconFlowImageResult:
        prompt = prompt.strip()
        if not prompt or len(prompt) > MAX_PROMPT_CHARS:
            raise ImageGenerationError(
                f"Prompt must contain 1-{MAX_PROMPT_CHARS} characters"
            )
        if image_size not in SILICONFLOW_IMAGE_SIZES:
            raise ImageGenerationError("Unsupported image size")
        if negative_prompt is not None and len(negative_prompt) > MAX_NEGATIVE_PROMPT_CHARS:
            raise ImageGenerationError("Negative prompt is too long")
        if seed is not None and not 0 <= seed <= 9_999_999_999:
            raise ImageGenerationError("Seed must be between 0 and 9999999999")
        if not 1 <= num_inference_steps <= 100:
            raise ImageGenerationError("Inference steps must be between 1 and 100")
        if not 0 <= guidance_scale <= 20:
            raise ImageGenerationError("Guidance scale must be between 0 and 20")

        body: dict[str, Any] = {
            "model": SILICONFLOW_IMAGE_MODEL,
            "prompt": prompt,
            "image_size": image_size,
            "batch_size": 1,
            "num_inference_steps": num_inference_steps,
            "guidance_scale": guidance_scale,
        }
        if negative_prompt:
            body["negative_prompt"] = negative_prompt
        if seed is not None:
            body["seed"] = seed

        try:
            response = await _await_or_cancel(
                self._client.post(
                    SILICONFLOW_IMAGE_ENDPOINT,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                        "User-Agent": "suyo/1.0 image-generation",
                    },
                    json=body,
                ),
                abort_event,
            )
        except ImageGenerationCancelled:
            raise
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ImageGenerationBillingUncertain(
                "The paid image request may have been accepted; check provider billing before retrying"
            ) from exc
        if response.status_code != 200:
            raise ImageGenerationError(_bounded_error(response))

        try:
            payload = response.json()
        except Exception as exc:
            raise ImageGenerationBillingUncertain(
                "The provider accepted the request but returned an unusable response; check billing before retrying"
            ) from exc
        images = payload.get("images") if isinstance(payload, dict) else None
        image_url = images[0].get("url") if isinstance(images, list) and images and isinstance(images[0], dict) else None
        if not isinstance(image_url, str) or not image_url:
            raise ImageGenerationBillingUncertain(
                "The provider accepted the request but returned no downloadable image; check billing before retrying"
            )

        try:
            content = await self._download(image_url, abort_event=abort_event)
            width, height = _parse_png(content)
        except ImageGenerationCancelled:
            raise
        except ImageGenerationBillingUncertain:
            raise
        except (ImageGenerationError, httpx.HTTPError) as exc:
            raise ImageGenerationBillingUncertain(
                "The provider generated an image but local download or validation failed; check billing before retrying"
            ) from exc
        raw_seed = payload.get("seed") if isinstance(payload, dict) else None
        result_seed = raw_seed if isinstance(raw_seed, int) and not isinstance(raw_seed, bool) else seed
        raw_trace_id = response.headers.get("x-siliconcloud-trace-id", "").strip()
        trace_id = (
            raw_trace_id
            if raw_trace_id
            and len(raw_trace_id) <= 240
            and self._api_key not in raw_trace_id
            and all(32 <= ord(character) < 127 for character in raw_trace_id)
            else None
        )
        return SiliconFlowImageResult(
            content=content,
            width=width,
            height=height,
            seed=result_seed,
            trace_id=trace_id,
        )

    async def _download(
        self,
        image_url: str,
        *,
        abort_event: asyncio.Event | None,
    ) -> bytes:
        if image_url.startswith(DATA_URI_PREFIX):
            encoded = image_url[len(DATA_URI_PREFIX):]
            if len(encoded) > ((MAX_IMAGE_BYTES + 2) // 3) * 4 + 4:
                raise ImageGenerationError("Generated image exceeds the download limit")
            try:
                content = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ImageGenerationError("Image provider returned invalid PNG data") from exc
            if len(content) > MAX_IMAGE_BYTES:
                raise ImageGenerationError("Generated image exceeds the download limit")
            return content

        current = image_url
        for redirect_count in range(MAX_REDIRECTS + 1):
            await self._validate_url(current)
            request = self._client.build_request(
                "GET",
                current,
                headers={"User-Agent": "suyo/1.0 image-download"},
            )
            response = await _await_or_cancel(
                self._client.send(request, stream=True),
                abort_event,
            )
            try:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location or redirect_count >= MAX_REDIRECTS:
                        raise ImageGenerationError("Image download exceeded redirect limit")
                    current = urljoin(current, location)
                    continue
                if response.status_code != 200:
                    raise ImageGenerationError(_bounded_error(response))
                declared_length = response.headers.get("content-length")
                if declared_length:
                    try:
                        if int(declared_length) > MAX_IMAGE_BYTES:
                            raise ImageGenerationError(
                                "Generated image exceeds the download limit"
                            )
                    except ValueError:
                        pass

                chunks: list[bytes] = []
                size = 0
                iterator = response.aiter_bytes()
                while True:
                    if abort_event is not None and abort_event.is_set():
                        raise ImageGenerationCancelled("Image download was cancelled")
                    try:
                        chunk = await _await_or_cancel(iterator.__anext__(), abort_event)
                    except StopAsyncIteration:
                        break
                    size += len(chunk)
                    if size > MAX_IMAGE_BYTES:
                        raise ImageGenerationError("Generated image exceeds the download limit")
                    chunks.append(chunk)
                return b"".join(chunks)
            finally:
                await response.aclose()

        raise ImageGenerationError("Image download exceeded redirect limit")
