from __future__ import annotations

import asyncio
import base64
import json
import os

import httpx
import pytest

from app.image_generation.siliconflow import (
    DATA_URI_PREFIX,
    ImageGenerationBillingUncertain,
    ImageGenerationCancelled,
    ImageGenerationError,
    SILICONFLOW_IMAGE_ENDPOINT,
    SILICONFLOW_IMAGE_MODEL,
    SiliconFlowImageClient,
)


# Complete, valid 1x1 transparent PNG.
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


async def _allow_url(_url: str) -> None:
    return None


@pytest.mark.asyncio
async def test_generate_posts_bounded_contract_and_downloads_png() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if str(request.url) == SILICONFLOW_IMAGE_ENDPOINT:
            payload = json.loads(request.content)
            assert payload == {
                "model": SILICONFLOW_IMAGE_MODEL,
                "prompt": "一只白猫",
                "image_size": "1024x1024",
                "batch_size": 1,
                "num_inference_steps": 20,
                "guidance_scale": 7.5,
                "negative_prompt": "文字",
                "seed": 42,
            }
            assert request.headers["authorization"] == "Bearer secret-key"
            return httpx.Response(
                200,
                json={"images": [{"url": "https://cdn.example.test/image.png"}], "seed": 42},
                headers={"x-siliconcloud-trace-id": "trace-1"},
            )
        assert str(request.url) == "https://cdn.example.test/image.png"
        return httpx.Response(200, content=PNG_1X1, headers={"content-type": "image/png"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, follow_redirects=False) as http:
        client = SiliconFlowImageClient("secret-key", client=http, url_validator=_allow_url)
        result = await client.generate(
            prompt="一只白猫",
            negative_prompt="文字",
            seed=42,
        )

    assert result.content == PNG_1X1
    assert (result.width, result.height) == (1, 1)
    assert result.seed == 42
    assert result.trace_id == "trace-1"
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_generate_accepts_bounded_png_data_uri() -> None:
    encoded = base64.b64encode(PNG_1X1).decode("ascii")

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"images": [{"url": DATA_URI_PREFIX + encoded}], "seed": 7},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = SiliconFlowImageClient("secret-key", client=http)
        result = await client.generate(prompt="cat")
    assert result.content == PNG_1X1
    assert result.seed == 7


@pytest.mark.asyncio
async def test_generate_rejects_non_https_download_without_fetching_it() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"images": [{"url": "http://127.0.0.1/x.png"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = SiliconFlowImageClient("secret-key", client=http)
        with pytest.raises(ImageGenerationBillingUncertain, match="check billing"):
            await client.generate(prompt="cat")
    assert calls == 1


@pytest.mark.asyncio
async def test_provider_error_body_cannot_echo_api_key() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"message": "bad authorization secret-key-do-not-echo"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = SiliconFlowImageClient("secret-key-do-not-echo", client=http)
        with pytest.raises(ImageGenerationError) as raised:
            await client.generate(prompt="cat")
    assert "secret-key-do-not-echo" not in str(raised.value)
    assert str(raised.value) == "Image provider returned HTTP 401"


@pytest.mark.asyncio
async def test_transport_failure_is_billing_uncertain_without_leaking_url() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(
            "timeout at https://signed.example/image?token=do-not-echo",
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = SiliconFlowImageClient("secret", client=http)
        with pytest.raises(ImageGenerationBillingUncertain) as raised:
            await client.generate(prompt="cat")
    assert "do-not-echo" not in str(raised.value)
    assert "check provider billing" in str(raised.value)


@pytest.mark.asyncio
async def test_truncated_png_header_is_not_accepted_as_an_artifact() -> None:
    truncated = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\x0dIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01"
    )
    encoded = base64.b64encode(truncated).decode("ascii")

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"images": [{"url": DATA_URI_PREFIX + encoded}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = SiliconFlowImageClient("secret", client=http)
        with pytest.raises(ImageGenerationBillingUncertain):
            await client.generate(prompt="cat")


@pytest.mark.asyncio
async def test_generation_can_be_cancelled_while_provider_is_pending() -> None:
    started = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.sleep(60)
        return httpx.Response(500)

    abort = asyncio.Event()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = SiliconFlowImageClient("secret-key", client=http)
        task = asyncio.create_task(client.generate(prompt="cat", abort_event=abort))
        await started.wait()
        abort.set()
        with pytest.raises(ImageGenerationCancelled):
            await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_rejects_unbounded_parameters_before_network() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("network should not be reached")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = SiliconFlowImageClient("secret-key", client=http)
        with pytest.raises(ImageGenerationError, match="Unsupported image size"):
            await client.generate(prompt="cat", image_size="4096x4096")
        with pytest.raises(ImageGenerationError, match="Prompt"):
            await client.generate(prompt="")


@pytest.mark.asyncio
async def test_optional_real_siliconflow_image_contract() -> None:
    """Run one explicitly opted-in paid request before promoting image GA."""

    api_key = os.environ.get("SILICONFLOW_IMAGE_E2E_API_KEY", "").strip()
    if not api_key:
        pytest.skip(
            "Set SILICONFLOW_IMAGE_E2E_API_KEY to run the optional paid real-server contract"
        )

    client = SiliconFlowImageClient(api_key)
    try:
        result = await client.generate(
            prompt=(
                "A minimal black circle centered on a plain white background, "
                "flat graphic, no text"
            ),
            image_size="768x1024",
            seed=20260714,
            num_inference_steps=1,
        )
    finally:
        await client.aclose()

    assert result.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert result.width > 0
    assert result.height > 0
