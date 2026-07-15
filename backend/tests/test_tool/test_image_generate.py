from __future__ import annotations

import asyncio
import base64
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.image_generation import ImageGenerationBillingUncertain
from app.image_generation.siliconflow import SiliconFlowImageResult
from app.schemas.agent import AgentInfo
from app.tool.builtin import image_generate as image_tool_module
from app.tool.builtin.image_generate import ImageGenerateTool
from app.tool.context import ToolContext


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class _Registry:
    def get_provider(self, provider_id: str):
        return object() if provider_id == "siliconflow" else None


class _FakeClient:
    instances: list["_FakeClient"] = []

    def __init__(self, api_key: str) -> None:
        assert api_key == "secret"
        self.closed = False
        self.kwargs = None
        self.instances.append(self)

    async def generate(self, **kwargs):
        self.kwargs = kwargs
        return SiliconFlowImageResult(
            content=PNG_1X1,
            width=1,
            height=1,
            seed=123,
            trace_id="trace",
        )

    async def aclose(self) -> None:
        self.closed = True


class _UncertainClient(_FakeClient):
    async def generate(self, **kwargs):
        self.kwargs = kwargs
        raise ImageGenerationBillingUncertain(
            "The paid image request may have been accepted; check provider billing before retrying"
        )


def _ctx(workspace: Path) -> ToolContext:
    return ToolContext(
        session_id="session",
        message_id="message",
        call_id="call-12345678",
        agent=AgentInfo(name="test", description="", mode="primary"),
        workspace=str(workspace),
        abort_event=asyncio.Event(),
    )


@pytest.mark.asyncio
async def test_tool_saves_new_png_and_returns_preview_attachment(tmp_path: Path, monkeypatch) -> None:
    _FakeClient.instances.clear()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(tmp_path / "private"))
    monkeypatch.setattr(image_tool_module, "get_provider_registry", lambda: _Registry())
    monkeypatch.setattr(
        image_tool_module,
        "get_settings",
        lambda: SimpleNamespace(siliconflow_api_key="secret"),
    )
    monkeypatch.setattr(image_tool_module, "SiliconFlowImageClient", _FakeClient)

    result = await ImageGenerateTool().execute(
        {"prompt": "cat", "output_path": "cat.png", "seed": 123},
        _ctx(workspace),
    )

    expected = workspace / "suxiaoyou_written" / "cat.png"
    assert result.success
    assert expected.read_bytes() == PNG_1X1
    assert result.metadata["file_path"] == str(expected)
    assert result.metadata["prompt_sha256"] == hashlib.sha256(b"cat").hexdigest()
    assert result.metadata["provider"] == "siliconflow"
    assert result.metadata["provider_name"] == "SiliconFlow"
    assert result.metadata["model"] == "Kwai-Kolors/Kolors"
    assert result.metadata["estimated_cost"] == 0.0
    assert result.metadata["currency"] == "CNY"
    assert result.metadata["pricing_unit"] == "image"
    assert result.metadata["pricing_basis"] == "official_catalog"
    assert result.metadata["pricing_as_of"] == "2026-07-14"
    assert result.metadata["pricing_source_url"] == "https://siliconflow.cn/pricing"
    assert result.metadata["approval_mode"] == "per_call"
    assert result.metadata["external_billing"] is True
    assert result.metadata["workspace_transaction"] is True
    assert result.metadata["atomic_file_install"] is True
    assert "供应商账单" in result.metadata["cost_notice"]
    assert result.attachments == [
        {
            "file_id": result.attachments[0]["file_id"],
            "name": "cat.png",
            "path": str(expected),
            "size": len(PNG_1X1),
            "mime_type": "image/png",
            "source": "referenced",
            "content_hash": hashlib.sha256(PNG_1X1).hexdigest(),
        }
    ]
    assert _FakeClient.instances[0].closed is True
    assert _FakeClient.instances[0].kwargs["abort_event"] is not None


@pytest.mark.asyncio
async def test_tool_refuses_to_overwrite_before_charging(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "suxiaoyou_written" / "cat.png"
    target.parent.mkdir()
    target.write_bytes(b"original")
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(tmp_path / "private"))

    monkeypatch.setattr(image_tool_module, "get_provider_registry", lambda: _Registry())
    monkeypatch.setattr(
        image_tool_module,
        "get_settings",
        lambda: SimpleNamespace(siliconflow_api_key="secret"),
    )
    monkeypatch.setattr(image_tool_module, "SiliconFlowImageClient", _FakeClient)

    result = await ImageGenerateTool().execute(
        {"prompt": "cat", "output_path": "cat.png"},
        _ctx(workspace),
    )
    assert not result.success
    assert "overwrite" in (result.error or "") or "覆盖" in (result.error or "")
    assert target.read_bytes() == b"original"


def test_tool_always_requires_per_call_approval() -> None:
    assert ImageGenerateTool().requires_approval is True


@pytest.mark.asyncio
async def test_uncertain_paid_call_is_persisted_and_never_silently_replayed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _UncertainClient.instances.clear()
    private = tmp_path / "private"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    monkeypatch.setattr(image_tool_module, "get_provider_registry", lambda: _Registry())
    monkeypatch.setattr(
        image_tool_module,
        "get_settings",
        lambda: SimpleNamespace(siliconflow_api_key="secret"),
    )
    monkeypatch.setattr(image_tool_module, "SiliconFlowImageClient", _UncertainClient)

    first = await ImageGenerateTool().execute(
        {"prompt": "private prompt phrase", "output_path": "cat.png"},
        _ctx(workspace),
    )
    second = await ImageGenerateTool().execute(
        {"prompt": "private prompt phrase", "output_path": "cat.png"},
        _ctx(workspace),
    )

    assert first.metadata["billing_status"] == "uncertain"
    assert second.metadata["billing_status"] == "uncertain"
    assert second.metadata["replayed"] is True
    assert len(_UncertainClient.instances) == 1
    ledger_text = (private / "image-generation-ledger-v1.json").read_text(encoding="utf-8")
    assert "private prompt phrase" not in ledger_text
    assert "secret" not in ledger_text


@pytest.mark.asyncio
async def test_completed_call_reuses_verified_local_artifact_without_provider_replay(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _FakeClient.instances.clear()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(tmp_path / "private"))
    monkeypatch.setattr(image_tool_module, "get_provider_registry", lambda: _Registry())
    monkeypatch.setattr(
        image_tool_module,
        "get_settings",
        lambda: SimpleNamespace(siliconflow_api_key="secret"),
    )
    monkeypatch.setattr(image_tool_module, "SiliconFlowImageClient", _FakeClient)

    first = await ImageGenerateTool().execute(
        {"prompt": "cat", "output_path": "cat.png"},
        _ctx(workspace),
    )
    second = await ImageGenerateTool().execute(
        {"prompt": "cat", "output_path": "cat.png"},
        _ctx(workspace),
    )

    assert first.success and second.success
    assert second.metadata["replayed"] is True
    assert second.metadata["provider_name"] == "SiliconFlow"
    assert second.metadata["model"] == "Kwai-Kolors/Kolors"
    assert second.metadata["estimated_cost"] == 0.0
    assert len(_FakeClient.instances) == 1


@pytest.mark.asyncio
async def test_paid_result_does_not_follow_parent_swapped_to_symlink(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    private = tmp_path / "private"
    output_parent = workspace / "suxiaoyou_written"

    class _SwapParentClient(_FakeClient):
        async def generate(self, **kwargs):
            result = await super().generate(**kwargs)
            try:
                output_parent.symlink_to(outside, target_is_directory=True)
            except OSError:
                pytest.skip("symbolic links are unavailable")
            return result

    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    monkeypatch.setattr(image_tool_module, "get_provider_registry", lambda: _Registry())
    monkeypatch.setattr(
        image_tool_module,
        "get_settings",
        lambda: SimpleNamespace(siliconflow_api_key="secret"),
    )
    monkeypatch.setattr(image_tool_module, "SiliconFlowImageClient", _SwapParentClient)

    result = await ImageGenerateTool().execute(
        {"prompt": "cat", "output_path": "cat.png"},
        _ctx(workspace),
    )

    assert not result.success
    assert result.metadata["billing_status"] == "output_failed"
    assert not (outside / "cat.png").exists()
    ledger = (private / "image-generation-ledger-v1.json").read_text(encoding="utf-8")
    assert '"status": "output_failed"' in ledger


@pytest.mark.asyncio
async def test_tool_requires_enabled_siliconflow_provider(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        image_tool_module,
        "get_provider_registry",
        lambda: SimpleNamespace(get_provider=lambda _provider_id: None),
    )
    result = await ImageGenerateTool().execute({"prompt": "cat"}, _ctx(tmp_path))
    assert not result.success
    assert "SiliconFlow" in (result.error or "") or "硅基流动" in (result.error or "")
