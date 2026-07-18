"""Contract tests for the capability-free Office precommit repair agent."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import hashlib
from types import MappingProxyType
from typing import Any

import pytest

import app.office_validation.repair_agent as repair_agent_module
from app.office_validation.precommit_repair import (
    OfficePrecommitRepairRequest,
    RedactedOfficeEvidenceBox,
    RedactedOfficeValidationCheck,
    RedactedOfficeValidationReport,
)
from app.office_validation.repair_agent import (
    OFFICE_PRECOMMIT_REPAIR_PROMPT_SHA256,
    OfficePrecommitRepairAgentError,
    OfficeRepairAgentBudget,
    ProviderOfficePrecommitRepairer,
    load_office_precommit_repair_prompt,
)
from app.provider.base import BaseProvider
from app.provider.registry import ProviderRegistry
from app.schemas.provider import (
    ModelCapabilities,
    ModelInfo,
    ProviderStatus,
    StreamChunk,
)


_NONCE = "A" * 32
_TARGET = f"sxy-office-repair:v1:target:{_NONCE}"
_READ = f"sxy-office-repair:v1:read:{_NONCE}:0"


class _Provider(BaseProvider):
    def __init__(self, chunks: list[object]) -> None:
        self.chunks = chunks
        self.calls: list[dict[str, Any]] = []
        self.active = 0
        self.max_active = 0

    @property
    def id(self) -> str:
        return "repair-test"

    async def list_models(self) -> list[ModelInfo]:
        return []

    async def stream_chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        self.calls.append({"model": model, "messages": messages, **kwargs})
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            for item in self.chunks:
                if isinstance(item, BaseException):
                    raise item
                if callable(item):
                    await item()
                    continue
                assert isinstance(item, StreamChunk)
                yield item
        finally:
            self.active -= 1

    async def health_check(self) -> ProviderStatus:
        return ProviderStatus(status="connected")


def _registry(provider: _Provider, *, json_output: bool = True) -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(provider)
    model = ModelInfo(
        id="repair-model",
        name="Repair model",
        provider_id=provider.id,
        capabilities=ModelCapabilities(json_output=json_output),
    )
    registry._provider_models[provider.id] = [model]  # noqa: SLF001
    registry._rebuild_indexes()  # noqa: SLF001
    return registry


def _request() -> OfficePrecommitRepairRequest:
    return OfficePrecommitRepairRequest(
        tokenized_args=MappingProxyType(
            {
                "operation": "create",
                "file_path": _TARGET,
                "overwrite": False,
                "document": {
                    "title": "Quarterly report",
                    "images": [{"path": _READ, "caption": "Revenue"}],
                },
            }
        ),
        report=RedactedOfficeValidationReport(
            document_format="docx",
            verdict="fail",
            checks=(
                RedactedOfficeValidationCheck(
                    code="layout_overflow",
                    outcome="fail",
                    box=RedactedOfficeEvidenceBox(
                        page_number=1,
                        x=10,
                        y=20,
                        width=30,
                        height=40,
                    ),
                ),
            ),
        ),
        attempt=1,
    )


def _response(text: str) -> list[object]:
    return [
        StreamChunk(type="text-delta", data={"text": text}),
        StreamChunk(type="finish", data={"reason": "stop"}),
        StreamChunk(type="usage", data={"total_tokens": 12}),
    ]


def test_repair_prompt_is_hash_locked_and_fails_closed_on_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    prompt = load_office_precommit_repair_prompt()
    assert hashlib.sha256(prompt.encode("utf-8")).hexdigest() == (
        OFFICE_PRECOMMIT_REPAIR_PROMPT_SHA256
    )

    changed_prompt = tmp_path / "office_repair.txt"
    changed_prompt.write_text(f"{prompt}\nchanged\n", encoding="utf-8")
    monkeypatch.setattr(repair_agent_module, "_PROMPT_PATH", changed_prompt)

    with pytest.raises(OfficePrecommitRepairAgentError, match="unavailable"):
        load_office_precommit_repair_prompt()


@pytest.mark.asyncio
async def test_repair_uses_explicit_json_capable_provider_without_tools_or_context() -> None:
    provider = _Provider(
        _response(
            '{"operation":"create","file_path":"%s","overwrite":false,'
            '"document":{"title":"Repaired","images":[{"path":"%s",'
            '"caption":"Revenue"}]}}' % (_TARGET, _READ)
        )
    )
    repairer = ProviderOfficePrecommitRepairer(
        provider_registry=_registry(provider),
        provider_id=provider.id,
        model_id="repair-model",
    )

    replacement = await repairer.repair(_request())

    assert replacement["file_path"] == _TARGET
    assert replacement["document"]["title"] == "Repaired"
    assert len(provider.calls) == 1
    call = provider.calls[0]
    assert call["model"] == "repair-model"
    assert call["tools"] is None
    assert call["temperature"] == 0.0
    assert call["max_tokens"] == 4_096
    assert call["extra_body"] is None
    assert call["response_format"]["type"] == "json_schema"
    payload = call["messages"][0]["content"]
    assert "Quarterly report" in payload
    assert _TARGET in payload and _READ in payload
    assert "/private/workspace" not in payload
    assert "runtime-instance" not in payload
    assert "private-renderer" not in payload
    assert call["system"] == load_office_precommit_repair_prompt()


@pytest.mark.asyncio
async def test_repair_reports_path_free_usage_and_outcome_to_observer() -> None:
    receipts = []

    async def observe(receipt) -> None:
        receipts.append(receipt)

    provider = _Provider(
        _response('{"operation":"create","file_path":"%s"}' % _TARGET)
    )
    repairer = ProviderOfficePrecommitRepairer(
        provider_registry=_registry(provider),
        provider_id=provider.id,
        model_id="repair-model",
        observer=observe,
    )

    await repairer.repair(_request())

    assert len(receipts) == 1
    assert receipts[0].outcome == "success"
    assert receipts[0].usage["output"] == 12
    assert receipts[0].usage["total"] == 12
    serialized = repr(receipts[0])
    assert "/private" not in serialized
    assert "Quarterly report" not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        "```json\n{}\n```",
        "before {}",
        '{"operation":"create","operation":"edit"}',
        '{"value":NaN}',
        "[1,2,3]",
        "{" * 50 + "}" * 50,
    ],
)
async def test_repair_rejects_noncanonical_or_unbounded_json(raw: str) -> None:
    provider = _Provider(_response(raw))
    repairer = ProviderOfficePrecommitRepairer(
        provider_registry=_registry(provider),
        provider_id=provider.id,
        model_id="repair-model",
    )

    with pytest.raises(OfficePrecommitRepairAgentError, match="invalid"):
        await repairer.repair(_request())


@pytest.mark.asyncio
async def test_repair_rejects_tool_or_reasoning_channels() -> None:
    provider = _Provider(
        [StreamChunk(type="tool-call", data={"name": "read", "arguments": {}})]
    )
    repairer = ProviderOfficePrecommitRepairer(
        provider_registry=_registry(provider),
        provider_id=provider.id,
        model_id="repair-model",
    )

    with pytest.raises(OfficePrecommitRepairAgentError, match="invalid response"):
        await repairer.repair(_request())


@pytest.mark.asyncio
async def test_repair_rejects_path_leak_before_provider_call() -> None:
    provider = _Provider(_response("{}"))
    repairer = ProviderOfficePrecommitRepairer(
        provider_registry=_registry(provider),
        provider_id=provider.id,
        model_id="repair-model",
    )
    leaked = OfficePrecommitRepairRequest(
        tokenized_args=MappingProxyType(
            {"operation": "create", "file_path": "/private/workspace/report.docx"}
        ),
        report=_request().report,
        attempt=1,
    )

    with pytest.raises(OfficePrecommitRepairAgentError, match="request is invalid"):
        await repairer.repair(leaked)
    assert provider.calls == []


@pytest.mark.asyncio
async def test_repair_hides_provider_exception_content_and_prompt() -> None:
    secret = "private Office bytes and server prompt must not leak"
    provider = _Provider([RuntimeError(secret)])
    repairer = ProviderOfficePrecommitRepairer(
        provider_registry=_registry(provider),
        provider_id=provider.id,
        model_id="repair-model",
    )

    with pytest.raises(OfficePrecommitRepairAgentError) as exc_info:
        await repairer.repair(_request())
    assert str(exc_info.value) == "Office repair agent request failed"
    assert secret not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


@pytest.mark.asyncio
async def test_repair_requires_the_explicit_selected_json_model() -> None:
    provider = _Provider(_response("{}"))
    repairer = ProviderOfficePrecommitRepairer(
        provider_registry=_registry(provider, json_output=False),
        provider_id=provider.id,
        model_id="repair-model",
    )

    with pytest.raises(OfficePrecommitRepairAgentError, match="unavailable"):
        await repairer.repair(_request())
    assert provider.calls == []


@pytest.mark.asyncio
async def test_repair_rejects_an_incomplete_or_oversized_replacement() -> None:
    provider = _Provider(_response("{}"))
    repairer = ProviderOfficePrecommitRepairer(
        provider_registry=_registry(provider),
        provider_id=provider.id,
        model_id="repair-model",
    )
    with pytest.raises(OfficePrecommitRepairAgentError, match="incomplete"):
        await repairer.repair(_request())

    oversized = _Provider(
        _response(
            '{"operation":"create","file_path":"%s","document":{"title":"%s"}}'
            % (_TARGET, "x" * 1_100)
        )
    )
    bounded = ProviderOfficePrecommitRepairer(
        provider_registry=_registry(oversized),
        provider_id=oversized.id,
        model_id="repair-model",
        budget=OfficeRepairAgentBudget(max_response_bytes=1_024),
    )
    with pytest.raises(OfficePrecommitRepairAgentError, match="exceeds its budget"):
        await bounded.repair(_request())


@pytest.mark.asyncio
async def test_repair_serializes_concurrent_calls() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def wait_once() -> None:
        started.set()
        await release.wait()

    provider = _Provider(
        [
            wait_once,
            StreamChunk(
                type="text-delta",
                data={"text": '{"operation":"create","file_path":"%s"}' % _TARGET},
            ),
            StreamChunk(type="finish", data={}),
        ]
    )
    repairer = ProviderOfficePrecommitRepairer(
        provider_registry=_registry(provider),
        provider_id=provider.id,
        model_id="repair-model",
    )

    first = asyncio.create_task(repairer.repair(_request()))
    await started.wait()
    second = asyncio.create_task(repairer.repair(_request()))
    await asyncio.sleep(0)
    assert provider.max_active == 1
    release.set()
    assert (await first)["file_path"] == _TARGET
    assert (await second)["file_path"] == _TARGET
    assert provider.max_active == 1


@pytest.mark.asyncio
async def test_repair_admission_is_shared_across_distinct_repairers() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def wait_once() -> None:
        started.set()
        await release.wait()

    provider = _Provider(
        [
            wait_once,
            StreamChunk(
                type="text-delta",
                data={"text": '{"operation":"create","file_path":"%s"}' % _TARGET},
            ),
        ]
    )
    registry = _registry(provider)
    first_repairer = ProviderOfficePrecommitRepairer(
        provider_registry=registry,
        provider_id=provider.id,
        model_id="repair-model",
    )
    second_repairer = ProviderOfficePrecommitRepairer(
        provider_registry=registry,
        provider_id=provider.id,
        model_id="repair-model",
    )

    first = asyncio.create_task(first_repairer.repair(_request()))
    await started.wait()
    second = asyncio.create_task(second_repairer.repair(_request()))
    await asyncio.sleep(0)
    assert provider.max_active == 1
    release.set()
    await first
    await second
    assert provider.max_active == 1


@pytest.mark.asyncio
async def test_repair_admission_can_clamp_server_output_budget() -> None:
    @asynccontextmanager
    async def admit(_requested: int):
        yield 17

    provider = _Provider(
        _response('{"operation":"create","file_path":"%s"}' % _TARGET)
    )
    repairer = ProviderOfficePrecommitRepairer(
        provider_registry=_registry(provider),
        provider_id=provider.id,
        model_id="repair-model",
        admission_factory=admit,
    )

    await repairer.repair(_request())

    assert provider.calls[0]["max_tokens"] == 17


@pytest.mark.asyncio
async def test_repair_enforces_its_server_owned_timeout() -> None:
    started = asyncio.Event()

    async def never_finishes() -> None:
        started.set()
        await asyncio.Event().wait()

    provider = _Provider([never_finishes])
    repairer = ProviderOfficePrecommitRepairer(
        provider_registry=_registry(provider),
        provider_id=provider.id,
        model_id="repair-model",
        budget=OfficeRepairAgentBudget(timeout_seconds=1.0),
    )
    with pytest.raises(OfficePrecommitRepairAgentError, match="timed out"):
        await repairer.repair(_request())
    assert started.is_set()
    assert provider.active == 0


@pytest.mark.asyncio
async def test_repair_preserves_cancellation() -> None:
    started = asyncio.Event()

    async def never_finishes() -> None:
        started.set()
        await asyncio.Event().wait()

    provider = _Provider([never_finishes])
    receipts = []

    async def observe(receipt) -> None:
        receipts.append(receipt)

    repairer = ProviderOfficePrecommitRepairer(
        provider_registry=_registry(provider),
        provider_id=provider.id,
        model_id="repair-model",
        observer=observe,
    )
    task = asyncio.create_task(repairer.repair(_request()))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.active == 0
    assert len(receipts) == 1
    assert receipts[0].outcome == "cancelled"


def test_repair_budget_has_fixed_safe_bounds() -> None:
    with pytest.raises(ValueError):
        OfficeRepairAgentBudget(timeout_seconds=0.5)
    with pytest.raises(ValueError):
        OfficeRepairAgentBudget(max_output_tokens=8_193)
    with pytest.raises(ValueError):
        OfficeRepairAgentBudget(max_response_bytes=1_048_577)
