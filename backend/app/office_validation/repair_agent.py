"""Capability-free model repairer for failed Office precommit candidates.

This module deliberately is not part of the Office tool composition.  A release
assembly may construct :class:`ProviderOfficePrecommitRepairer` with one
explicitly selected provider/model and install it as the trusted
``office_precommit_repairer`` app-state value.  Until then it is inert.

The model sees only the immutable tokenized Office arguments and the redacted
location-only validation report defined in :mod:`precommit_repair`.  It never
receives a workspace, filesystem path, runtime identity, tool specification,
permission mechanism, policy, or commit authority.  Its response is treated as
untrusted JSON and is still subject to the Office tool's token unmasking and
identity checks before it can affect a candidate.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hashlib
import hmac
import json
import math
from pathlib import Path
import re
import secrets
import threading
from types import MappingProxyType
from typing import (
    Any,
    AsyncContextManager,
    Callable,
    Final,
    Literal,
    Protocol,
)
from weakref import WeakKeyDictionary

from app.office_validation.precommit_repair import (
    OfficePrecommitRepairError,
    OfficePrecommitRepairRequest,
    copy_replacement_args,
)
from app.provider.registry import ProviderRegistry
from app.schemas.provider import StreamChunk


_PROMPT_PATH: Final[Path] = (
    Path(__file__).resolve().parents[1] / "agent" / "prompts" / "office_repair.txt"
)
# Hash the canonical UTF-8 text (``Path.read_text`` normalizes checkout line
# endings) so the same source contract works on Windows, macOS, and Linux.
# Changing this server-owned policy is a reviewed source change: update the
# prompt, this digest, the bundle verifier, and the contract tests together.
OFFICE_PRECOMMIT_REPAIR_PROMPT_SHA256: Final[str] = (
    "7c9cd1613c47761539cd04fa22634e467881bac9917f5c88018454d5b91b5272"
)
_TOKEN_PREFIX: Final[str] = "sxy-office-repair:v1:"
_TOKEN_RE: Final[re.Pattern[str]] = re.compile(
    r"^sxy-office-repair:v1:(?:target:[A-Za-z0-9_-]{16,128}|"
    r"read:[A-Za-z0-9_-]{16,128}:[0-9]{1,6})$"
)
_CHECK_CODE_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_MAX_REQUEST_BYTES: Final[int] = 512 * 1024
_MAX_JSON_DEPTH: Final[int] = 48
_MAX_JSON_NODES: Final[int] = 100_000
_MAX_REPORT_CHECKS: Final[int] = 512


class OfficePrecommitRepairAgentError(OfficePrecommitRepairError):
    """A capability-free repair model did not return a safe replacement."""


@dataclass(frozen=True, slots=True)
class OfficeRepairAgentBudget:
    """Code-owned per-call bounds; no model argument can override them."""

    timeout_seconds: float = 45.0
    max_output_tokens: int = 4_096
    max_response_bytes: int = 512 * 1024
    admission_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if (
            not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or not math.isfinite(float(self.timeout_seconds))
            or not 1.0 <= float(self.timeout_seconds) <= 120.0
        ):
            raise ValueError("Office repair agent timeout is invalid")
        if (
            not isinstance(self.max_output_tokens, int)
            or isinstance(self.max_output_tokens, bool)
            or not 256 <= self.max_output_tokens <= 8_192
        ):
            raise ValueError("Office repair agent token budget is invalid")
        if (
            not isinstance(self.max_response_bytes, int)
            or isinstance(self.max_response_bytes, bool)
            or not 1_024 <= self.max_response_bytes <= 1_048_576
        ):
            raise ValueError("Office repair agent response budget is invalid")
        if (
            not isinstance(self.admission_timeout_seconds, (int, float))
            or isinstance(self.admission_timeout_seconds, bool)
            or not math.isfinite(float(self.admission_timeout_seconds))
            or not 0.1 <= float(self.admission_timeout_seconds) <= 30.0
        ):
            raise ValueError("Office repair agent admission timeout is invalid")


OfficeRepairExecutionOutcome = Literal[
    "success",
    "failed",
    "timeout",
    "cancelled",
]


@dataclass(frozen=True, slots=True)
class OfficeRepairExecutionReceipt:
    """Path-free accounting/audit record for exactly one model attempt."""

    execution_id: str
    provider_id: str
    model_id: str
    outcome: OfficeRepairExecutionOutcome
    usage: Mapping[str, int]
    model_info: Any = None


class OfficeRepairExecutionObserver(Protocol):
    async def __call__(self, receipt: OfficeRepairExecutionReceipt) -> None:
        ...


OfficeRepairAdmissionFactory = Callable[
    [int],
    AsyncContextManager[int],
]


@dataclass(slots=True)
class _OfficeRepairExecutionState:
    model_info: Any = None
    usage: dict[str, int] | None = None


class OfficeRepairModelExecutor:
    """Process-runtime admission for capability-free repair completions."""

    def __init__(self, *, max_concurrency: int = 1) -> None:
        if (
            not isinstance(max_concurrency, int)
            or isinstance(max_concurrency, bool)
            or not 1 <= max_concurrency <= 4
        ):
            raise ValueError("Office repair model concurrency is invalid")
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def execute(
        self,
        *,
        provider_registry: ProviderRegistry,
        provider_id: str,
        model_id: str,
        prompt: str,
        payload: str,
        budget: OfficeRepairAgentBudget,
        state: _OfficeRepairExecutionState,
        admission_factory: OfficeRepairAdmissionFactory,
    ) -> str:
        try:
            await asyncio.wait_for(
                self._semaphore.acquire(),
                timeout=budget.admission_timeout_seconds,
            )
        except TimeoutError:
            raise OfficePrecommitRepairAgentError(
                "Office repair agent is busy"
            ) from None
        try:
            resolved = provider_registry.resolve_model(model_id, provider_id)
            if resolved is None:
                raise OfficePrecommitRepairAgentError(
                    "Office repair model is unavailable"
                )
            provider, model = resolved
            if (
                provider.id != provider_id
                or model.id != model_id
                or model.provider_id != provider_id
                or not model.capabilities.json_output
            ):
                raise OfficePrecommitRepairAgentError(
                    "Office repair model is unavailable"
                )
            state.model_info = model
            stream: AsyncIterator[StreamChunk] | None = None
            first_chunk: StreamChunk | None = None
            try:
                async with admission_factory(budget.max_output_tokens) as max_tokens:
                    if (
                        not isinstance(max_tokens, int)
                        or isinstance(max_tokens, bool)
                        or max_tokens < 1
                        or max_tokens > budget.max_output_tokens
                    ):
                        raise OfficePrecommitRepairAgentError(
                            "Office repair agent admission was rejected"
                        )
                    stream = provider.stream_chat(
                        model_id,
                        [{"role": "user", "content": payload}],
                        tools=None,
                        system=prompt,
                        temperature=0.0,
                        max_tokens=max_tokens,
                        extra_body=None,
                        response_format=_REPAIR_RESPONSE_FORMAT,
                    )
                    try:
                        first_chunk = await anext(stream)
                    except StopAsyncIteration:
                        first_chunk = None
                return await _collect_json_response(
                    stream,
                    first_chunk=first_chunk,
                    max_response_bytes=budget.max_response_bytes,
                    state=state,
                )
            except BaseException:
                if stream is not None:
                    closer = getattr(stream, "aclose", None)
                    if callable(closer):
                        await closer()
                raise
        finally:
            self._semaphore.release()


_SHARED_EXECUTORS_LOCK: Final = threading.Lock()
_SHARED_EXECUTORS: Final[
    WeakKeyDictionary[ProviderRegistry, OfficeRepairModelExecutor]
] = WeakKeyDictionary()


def shared_office_repair_model_executor(
    provider_registry: ProviderRegistry,
) -> OfficeRepairModelExecutor:
    """Return the one repair admission lane for a production provider runtime."""

    if not isinstance(provider_registry, ProviderRegistry):
        raise TypeError("Office repair provider registry is invalid")
    with _SHARED_EXECUTORS_LOCK:
        executor = _SHARED_EXECUTORS.get(provider_registry)
        if executor is None:
            executor = OfficeRepairModelExecutor(max_concurrency=1)
            _SHARED_EXECUTORS[provider_registry] = executor
        return executor


@asynccontextmanager
async def _unrestricted_repair_admission(
    max_output_tokens: int,
) -> AsyncIterator[int]:
    yield max_output_tokens


def load_office_precommit_repair_prompt() -> str:
    """Load and authenticate the server-owned prompt selected by source code."""

    try:
        prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise OfficePrecommitRepairAgentError(
            "Office repair agent is unavailable"
        ) from None
    encoded = prompt.encode("utf-8")
    prompt_sha256 = hashlib.sha256(encoded).hexdigest()
    if (
        not prompt.strip()
        or len(encoded) > 32 * 1024
        or not hmac.compare_digest(
            prompt_sha256,
            OFFICE_PRECOMMIT_REPAIR_PROMPT_SHA256,
        )
    ):
        raise OfficePrecommitRepairAgentError("Office repair agent is unavailable")
    return prompt


class ProviderOfficePrecommitRepairer:
    """One-at-a-time, JSON-only completion boundary for Office repair.

    ``provider_id`` and ``model_id`` are required server configuration, rather
    than inferred from a user session or a model default.  Resolution happens
    for every call so changing the registry removes the repair surface instead
    of retaining a stale provider handle.
    """

    def __init__(
        self,
        *,
        provider_registry: ProviderRegistry,
        provider_id: str,
        model_id: str,
        budget: OfficeRepairAgentBudget | None = None,
        executor: OfficeRepairModelExecutor | None = None,
        admission_factory: OfficeRepairAdmissionFactory | None = None,
        observer: OfficeRepairExecutionObserver | None = None,
    ) -> None:
        if not isinstance(provider_registry, ProviderRegistry):
            raise TypeError("Office repair provider registry is invalid")
        self._provider_registry = provider_registry
        self._provider_id = _validate_selected_id(provider_id, "provider")
        self._model_id = _validate_selected_id(model_id, "model")
        self._budget = budget or OfficeRepairAgentBudget()
        if not isinstance(self._budget, OfficeRepairAgentBudget):
            raise TypeError("Office repair agent budget is invalid")
        self._prompt = load_office_precommit_repair_prompt()
        self._executor = executor or shared_office_repair_model_executor(
            provider_registry
        )
        if not isinstance(self._executor, OfficeRepairModelExecutor):
            raise TypeError("Office repair model executor is invalid")
        self._admission_factory = (
            admission_factory or _unrestricted_repair_admission
        )
        if not callable(self._admission_factory):
            raise TypeError("Office repair admission factory is invalid")
        self._observer = observer
        if observer is not None and not callable(observer):
            raise TypeError("Office repair execution observer is invalid")

    @property
    def provider_id(self) -> str:
        """The server-selected provider identity, for assembly diagnostics."""

        return self._provider_id

    @property
    def model_id(self) -> str:
        """The server-selected model identity, for assembly diagnostics."""

        return self._model_id

    async def repair(
        self,
        request: OfficePrecommitRepairRequest,
    ) -> Mapping[str, Any]:
        """Return a full untrusted replacement object or fail closed.

        Cancellation remains cancellation.  All other provider/parsing failures
        use deliberately content-free errors so neither Office data nor the
        server prompt can reach logs or a tool result.
        """

        _validate_repair_request(request)
        state = _OfficeRepairExecutionState()
        outcome: OfficeRepairExecutionOutcome = "failed"
        result: Mapping[str, Any] | None = None
        failure: BaseException | None = None
        try:
            result = await asyncio.wait_for(
                self._repair_once(request, state),
                timeout=self._budget.timeout_seconds,
            )
            outcome = "success"
        except asyncio.CancelledError as exc:
            outcome = "cancelled"
            failure = exc
        except TimeoutError:
            outcome = "timeout"
            failure = OfficePrecommitRepairAgentError(
                "Office repair agent timed out"
            )
        except OfficePrecommitRepairError as exc:
            failure = exc
        except Exception:
            failure = OfficePrecommitRepairAgentError(
                "Office repair agent request failed"
            )
        receipt = OfficeRepairExecutionReceipt(
            execution_id=secrets.token_hex(16),
            provider_id=self._provider_id,
            model_id=self._model_id,
            outcome=outcome,
            usage=MappingProxyType(dict(state.usage or {})),
            model_info=state.model_info,
        )
        if self._observer is not None:
            try:
                await _settle_execution_observer(self._observer, receipt)
            except asyncio.CancelledError as exc:
                failure = failure or exc
            except Exception:
                failure = failure or OfficePrecommitRepairAgentError(
                    "Office repair agent accounting failed"
                )
        if failure is not None:
            raise failure from None
        assert result is not None
        return result

    async def _repair_once(
        self,
        request: OfficePrecommitRepairRequest,
        state: _OfficeRepairExecutionState,
    ) -> dict[str, Any]:
        payload = _serialize_repair_request(request)
        response = await self._executor.execute(
            provider_registry=self._provider_registry,
            provider_id=self._provider_id,
            model_id=self._model_id,
            prompt=self._prompt,
            payload=payload,
            budget=self._budget,
            state=state,
            admission_factory=self._admission_factory,
        )
        return _parse_replacement_args(response)


_REPAIR_RESPONSE_FORMAT: Final[dict[str, Any]] = {
    "type": "json_schema",
    "json_schema": {
        "name": "office_precommit_repair_args_v1",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": True,
        },
    },
}


def _validate_selected_id(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 200
        or any(ord(character) < 33 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"Office repair {label} selection is invalid")
    return value


def _validate_repair_request(request: object) -> None:
    if not isinstance(request, OfficePrecommitRepairRequest):
        raise OfficePrecommitRepairAgentError("Office repair request is invalid")
    # The report is deliberately compact and redacted.  Re-validate it here
    # because these dataclasses intentionally stay serialization-light and a
    # release assembly could otherwise hand-construct one outside the normal
    # Office tool path.
    report = request.report
    if (
        report.document_format not in {"docx", "xlsx", "pptx"}
        or report.verdict not in {"pass", "fail", "needs_review"}
        or not isinstance(report.checks, tuple)
        or not report.checks
        or len(report.checks) > _MAX_REPORT_CHECKS
    ):
        raise OfficePrecommitRepairAgentError("Office repair request is invalid")
    for check in report.checks:
        if (
            not isinstance(check.code, str)
            or _CHECK_CODE_RE.fullmatch(check.code) is None
            or check.outcome not in {"pass", "fail", "needs_review"}
        ):
            raise OfficePrecommitRepairAgentError("Office repair request is invalid")
        box = check.box
        if box is not None and (
            any(
                not isinstance(value, int) or isinstance(value, bool)
                for value in (box.page_number, box.x, box.y, box.width, box.height)
            )
            or box.page_number < 1
            or box.x < 0
            or box.y < 0
            or box.width < 1
            or box.height < 1
        ):
            raise OfficePrecommitRepairAgentError("Office repair request is invalid")
    _validate_tokenized_paths(request.tokenized_args)


def _validate_tokenized_paths(value: Mapping[str, Any]) -> None:
    """Reject an accidental path leak before crossing the model boundary.

    This intentionally mirrors the small Office repair path surface rather
    than accepting arbitrary ``*_path`` fields.  The Office tool independently
    repeats the schema check when it unmasks model output.
    """

    seen: set[int] = set()
    nodes = 0
    target_count = 0

    def is_sequence(item: object) -> bool:
        return isinstance(item, Sequence) and not isinstance(
            item,
            (str, bytes, bytearray, memoryview),
        )

    def path_domain(location: tuple[str | int, ...]) -> str | None:
        if location == ("file_path",):
            return "target"
        if (
            len(location) == 4
            and location[0] == "document"
            and location[1] in {"images", "charts"}
            and isinstance(location[2], int)
            and location[3] == "path"
        ):
            return "read"
        if (
            len(location) == 6
            and location[0] == "presentation"
            and location[1] == "slides"
            and isinstance(location[2], int)
            and location[3] == "images"
            and isinstance(location[4], int)
            and location[5] == "path"
        ):
            return "read"
        return None

    def looks_like_path_key(key: str) -> bool:
        return key in {"path", "paths"} or key.endswith("_path") or key.endswith(
            "_paths"
        )

    def visit(item: object, location: tuple[str | int, ...], depth: int) -> None:
        nonlocal nodes, target_count
        nodes += 1
        if depth > _MAX_JSON_DEPTH or nodes > _MAX_JSON_NODES:
            raise OfficePrecommitRepairAgentError("Office repair request is invalid")
        if isinstance(item, Mapping):
            identity = id(item)
            if identity in seen:
                raise OfficePrecommitRepairAgentError("Office repair request is invalid")
            seen.add(identity)
            try:
                for key, child in item.items():
                    if not isinstance(key, str):
                        raise OfficePrecommitRepairAgentError(
                            "Office repair request is invalid"
                        )
                    child_location = location + (key,)
                    domain = path_domain(child_location)
                    if domain is not None:
                        if not isinstance(child, str) or not _TOKEN_RE.fullmatch(child):
                            raise OfficePrecommitRepairAgentError(
                                "Office repair request is invalid"
                            )
                        if domain == "target":
                            target_count += 1
                            if not child.startswith(_TOKEN_PREFIX + "target:"):
                                raise OfficePrecommitRepairAgentError(
                                    "Office repair request is invalid"
                                )
                        elif not child.startswith(_TOKEN_PREFIX + "read:"):
                            raise OfficePrecommitRepairAgentError(
                                "Office repair request is invalid"
                            )
                    elif looks_like_path_key(key) and (
                        len(child_location) == 1
                        or child_location[0]
                        in {"document", "workbook", "presentation"}
                    ):
                        raise OfficePrecommitRepairAgentError(
                            "Office repair request is invalid"
                        )
                    visit(child, child_location, depth + 1)
            finally:
                seen.remove(identity)
        elif is_sequence(item):
            identity = id(item)
            if identity in seen:
                raise OfficePrecommitRepairAgentError("Office repair request is invalid")
            seen.add(identity)
            try:
                for index, child in enumerate(item):
                    visit(child, location + (index,), depth + 1)
            finally:
                seen.remove(identity)

    visit(value, (), 0)
    if target_count != 1:
        raise OfficePrecommitRepairAgentError("Office repair request is invalid")


def _serialize_repair_request(request: OfficePrecommitRepairRequest) -> str:
    """Serialize only the two explicitly allowed redacted inputs."""

    payload = {
        "schema_version": 1,
        "attempt": request.attempt,
        "tokenized_args": copy_replacement_args(request.tokenized_args),
        "report": {
            "document_format": request.report.document_format,
            "verdict": request.report.verdict,
            "checks": [
                {
                    "code": check.code,
                    "outcome": check.outcome,
                    "box": (
                        None
                        if check.box is None
                        else {
                            "page_number": check.box.page_number,
                            "x": check.box.x,
                            "y": check.box.y,
                            "width": check.box.width,
                            "height": check.box.height,
                        }
                    ),
                }
                for check in request.report.checks
            ],
        },
    }
    try:
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        raise OfficePrecommitRepairAgentError("Office repair request is invalid") from None
    if len(serialized.encode("utf-8")) > _MAX_REQUEST_BYTES:
        raise OfficePrecommitRepairAgentError("Office repair request is invalid")
    return serialized


async def _collect_json_response(
    stream: AsyncIterator[StreamChunk],
    *,
    first_chunk: StreamChunk | None = None,
    max_response_bytes: int,
    state: _OfficeRepairExecutionState | None = None,
) -> str:
    """Collect only model text while rejecting tools and non-text channels."""

    parts: list[str] = []
    size = 0
    async def chunks() -> AsyncIterator[StreamChunk]:
        if first_chunk is not None:
            yield first_chunk
        async for item in stream:
            yield item

    try:
        async for chunk in chunks():
            if not isinstance(chunk, StreamChunk):
                raise OfficePrecommitRepairAgentError(
                    "Office repair agent returned an invalid response"
                )
            if chunk.type == "text-delta":
                text = chunk.data.get("text")
                if not isinstance(text, str):
                    raise OfficePrecommitRepairAgentError(
                        "Office repair agent returned an invalid response"
                    )
                size += len(text.encode("utf-8"))
                if size > max_response_bytes:
                    raise OfficePrecommitRepairAgentError(
                        "Office repair agent response exceeds its budget"
                    )
                parts.append(text)
            elif chunk.type == "usage":
                if state is not None:
                    state.usage = _canonical_repair_usage(chunk.data)
            elif chunk.type == "finish":
                continue
            else:
                raise OfficePrecommitRepairAgentError(
                    "Office repair agent returned an invalid response"
                )
    finally:
        closer = getattr(stream, "aclose", None)
        if callable(closer):
            await closer()
    if not parts:
        raise OfficePrecommitRepairAgentError(
            "Office repair agent returned an invalid response"
        )
    return "".join(parts)


def _canonical_repair_usage(value: object) -> dict[str, int]:
    """Normalize Provider usage without trusting a reported aggregate."""

    if not isinstance(value, Mapping):
        return {}

    def token(*names: str) -> int:
        for name in names:
            raw = value.get(name)
            if raw is None:
                continue
            try:
                parsed = int(raw)
            except (TypeError, ValueError, OverflowError):
                continue
            return max(0, parsed)
        return 0

    usage = {
        "input": token("input", "input_tokens", "prompt_tokens"),
        "output": token("output", "output_tokens", "completion_tokens"),
        "reasoning": token("reasoning", "reasoning_tokens"),
        "cache_read": token("cache_read", "cache_read_tokens"),
        "cache_write": token("cache_write", "cache_write_tokens"),
    }
    canonical_total = sum(
        usage[name] for name in ("input", "output", "reasoning", "cache_read")
    )
    reported_total = token("total", "total_tokens")
    if canonical_total == 0 and reported_total > 0:
        # Legacy Provider adapters sometimes report only a total. Attribute it
        # to output so hard budgets remain conservative and exact in aggregate.
        usage["output"] = reported_total
        canonical_total = reported_total
    usage["total"] = max(canonical_total, reported_total)
    return usage


async def _settle_execution_observer(
    observer: OfficeRepairExecutionObserver,
    receipt: OfficeRepairExecutionReceipt,
) -> None:
    """Finish durable accounting before propagating caller cancellation."""

    task = asyncio.create_task(observer(receipt))
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
    error = task.exception()
    if error is not None:
        raise error
    if cancellation is not None:
        raise cancellation


def _parse_replacement_args(raw: str) -> dict[str, Any]:
    """Accept exactly one finite JSON object, with no duplicate keys/prose."""

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate object key")
            result[key] = value
        return result

    def reject_nonfinite(_value: str) -> object:
        raise ValueError("non-finite number")

    try:
        decoded = json.loads(
            raw,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_nonfinite,
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        raise OfficePrecommitRepairAgentError(
            "Office repair agent returned invalid JSON"
        ) from None
    try:
        replacement = copy_replacement_args(decoded)
    except OfficePrecommitRepairError:
        raise OfficePrecommitRepairAgentError(
            "Office repair agent returned invalid replacement arguments"
        ) from None
    _validate_complete_replacement_args(replacement)
    return replacement


def _validate_complete_replacement_args(replacement: dict[str, Any]) -> None:
    """Reject a patch or an unmasked target before the Office tool sees it."""

    operation = replacement.get("operation")
    if (
        "file_path" not in replacement
        or not isinstance(operation, str)
        or not operation
        or len(operation) > 32
    ):
        raise OfficePrecommitRepairAgentError(
            "Office repair agent returned incomplete replacement arguments"
        )
    _validate_tokenized_paths(replacement)


__all__ = [
    "OFFICE_PRECOMMIT_REPAIR_PROMPT_SHA256",
    "OfficePrecommitRepairAgentError",
    "OfficeRepairExecutionObserver",
    "OfficeRepairExecutionReceipt",
    "OfficeRepairAgentBudget",
    "OfficeRepairModelExecutor",
    "ProviderOfficePrecommitRepairer",
    "load_office_precommit_repair_prompt",
    "shared_office_repair_model_executor",
]
