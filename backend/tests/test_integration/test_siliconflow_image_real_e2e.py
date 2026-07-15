"""Explicitly opted-in, one-request SiliconFlow tool-closure E2E.

Normal CI skips this test.  The release gate runner performs a fail-closed
preflight before selecting this exact node, so a skipped test can never count
as release evidence.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import uuid
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from app.image_generation.siliconflow import (
    SILICONFLOW_IMAGE_ESTIMATED_COST_CNY,
    SILICONFLOW_IMAGE_PRICING_AS_OF,
    SILICONFLOW_IMAGE_PRICING_SOURCE_URL,
)
from app.schemas.agent import AgentInfo
from app.tool.builtin import image_generate as image_tool_module
from app.tool.builtin.image_generate import ImageGenerateTool
from app.tool.context import ToolContext


PAID_REQUEST_ACK = "I_UNDERSTAND_THIS_MAY_USE_PROVIDER_QUOTA_OR_INCUR_COST"


class _EnabledSiliconFlowRegistry:
    def get_provider(self, provider_id: str):
        return object() if provider_id == "siliconflow" else None


def _live_api_key() -> str:
    api_key = os.environ.get("SILICONFLOW_IMAGE_E2E_API_KEY", "").strip()
    if not api_key:
        pytest.skip(
            "Set SILICONFLOW_IMAGE_E2E_API_KEY and use the fail-closed "
            "v1 integration gate runner for a real provider request"
        )

    acknowledgement = os.environ.get(
        "SILICONFLOW_IMAGE_E2E_ALLOW_PAID_REQUEST",
        "",
    )
    if acknowledgement != PAID_REQUEST_ACK:
        pytest.fail(
            "SILICONFLOW_IMAGE_E2E_ALLOW_PAID_REQUEST must exactly match the "
            "documented quota/cost acknowledgement",
            pytrace=False,
        )
    if os.environ.get("SILICONFLOW_IMAGE_E2E_MAX_REQUESTS") != "1":
        pytest.fail(
            "SILICONFLOW_IMAGE_E2E_MAX_REQUESTS must exactly equal 1",
            pytrace=False,
        )
    budget_text = os.environ.get("SILICONFLOW_IMAGE_E2E_MAX_COST_CNY", "")
    if not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,4})?", budget_text):
        pytest.fail(
            "SILICONFLOW_IMAGE_E2E_MAX_COST_CNY must be a non-negative decimal "
            "with at most 4 places",
            pytrace=False,
        )
    try:
        budget = float(budget_text)
    except ValueError:
        pytest.fail(
            "SILICONFLOW_IMAGE_E2E_MAX_COST_CNY must be a non-negative number",
            pytrace=False,
        )
    if (
        not math.isfinite(budget)
        or budget < 0
        or budget > 10_000
        or SILICONFLOW_IMAGE_ESTIMATED_COST_CNY > budget
    ):
        pytest.fail(
            "the catalog image estimate exceeds SILICONFLOW_IMAGE_E2E_MAX_COST_CNY",
            pytrace=False,
        )
    try:
        pricing_date = date.fromisoformat(SILICONFLOW_IMAGE_PRICING_AS_OF)
    except ValueError:
        pytest.fail("SiliconFlow pricing date is invalid", pytrace=False)
    pricing_age_days = (date.today() - pricing_date).days
    if not -1 <= pricing_age_days <= 30:
        pytest.fail(
            "SiliconFlow catalog pricing must be reviewed within 30 days",
            pytrace=False,
        )
    if SILICONFLOW_IMAGE_PRICING_SOURCE_URL != "https://siliconflow.cn/pricing":
        pytest.fail(
            "SiliconFlow pricing source must use the reviewed official URL",
            pytrace=False,
        )
    return api_key


def _context(workspace: Path, call_id: str) -> ToolContext:
    return ToolContext(
        session_id="v1-image-real-e2e",
        message_id="v1-image-real-e2e-message",
        call_id=call_id,
        agent=AgentInfo(name="v1 integration gate", description="", mode="primary"),
        workspace=str(workspace),
        abort_event=asyncio.Event(),
    )


@pytest.mark.asyncio
async def test_optional_real_siliconflow_tool_closure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove credential, quota, download, save, ledger, and replay in one call."""

    api_key = _live_api_key()
    private = tmp_path / "private"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    call_id = f"v1-image-e2e-{uuid.uuid4().hex}"

    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    monkeypatch.setattr(
        image_tool_module,
        "get_provider_registry",
        lambda: _EnabledSiliconFlowRegistry(),
    )
    monkeypatch.setattr(
        image_tool_module,
        "get_settings",
        lambda: SimpleNamespace(siliconflow_api_key=api_key),
    )

    arguments = {
        "prompt": (
            "A minimal black circle centered on a plain white background, "
            "flat graphic, no text"
        ),
        "output_path": "v1-siliconflow-e2e.png",
        "image_size": "768x1024",
        "seed": 20260714,
        "num_inference_steps": 1,
    }
    context = _context(workspace, call_id)
    first = await ImageGenerateTool().execute(arguments, context)
    assert first.success, (
        "real image tool closure failed; do not auto-retry if billing_status is "
        f"uncertain: billing_status={first.metadata.get('billing_status')}, "
        f"error={first.error}"
    )
    assert first.metadata["billing_status"] == "completed"
    assert first.metadata["operation_id"] == call_id
    assert first.metadata["provider"] == "siliconflow"
    assert first.metadata["estimated_cost"] <= float(
        os.environ["SILICONFLOW_IMAGE_E2E_MAX_COST_CNY"]
    )

    output_path = Path(first.metadata["file_path"])
    content = output_path.read_bytes()
    assert output_path == workspace / "suxiaoyou_written" / "v1-siliconflow-e2e.png"
    assert hashlib.sha256(content).hexdigest() == first.metadata["content_hash"]
    with Image.open(output_path) as image:
        image.verify()
        assert image.format == "PNG"
    with Image.open(output_path) as image:
        image.load()
        assert image.width > 0 and image.height > 0

    ledger_path = private / "image-generation-ledger-v1.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    rows = [row for row in ledger["entries"] if row["call_id"] == call_id]
    assert len(rows) == 1
    assert rows[0]["status"] == "completed"
    assert rows[0]["content_hash"] == hashlib.sha256(content).hexdigest()
    assert rows[0]["bytes"] == len(content)

    # A second execution with the same call id must use the verified local
    # artifact.  No provider client is constructed on this path, so the live
    # gate is mechanically bounded to one quota-consuming request.
    original_client = image_tool_module.SiliconFlowImageClient

    class _ProviderReplayForbidden:
        def __init__(self, _api_key: str) -> None:
            raise AssertionError("completed operation attempted a second provider request")

    monkeypatch.setattr(
        image_tool_module,
        "SiliconFlowImageClient",
        _ProviderReplayForbidden,
    )
    try:
        replay = await ImageGenerateTool().execute(arguments, context)
    finally:
        monkeypatch.setattr(
            image_tool_module,
            "SiliconFlowImageClient",
            original_client,
        )

    assert replay.success
    assert replay.metadata["replayed"] is True
    assert replay.metadata["billing_status"] == "completed"
    assert Path(replay.metadata["file_path"]).read_bytes() == content
