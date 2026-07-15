"""Durable billing-operation ledger for paid image generation.

The provider does not expose a documented idempotency key.  The application
therefore records every submitted tool call before network I/O and never
silently replays the same call id.  An interrupted or ambiguous request remains
``uncertain`` until the user explicitly decides whether to make a new paid
request.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from app.tool.workspace import APP_PRIVATE_DIR_ENV
from app.utils.atomic_write import atomic_write_text


LEDGER_SCHEMA_VERSION: Final = 1
MAX_LEDGER_ENTRIES: Final = 2_000
RETAIN_TERMINAL_ENTRIES: Final = 1_000
_TERMINAL_STATUSES = frozenset({"completed", "rejected", "output_failed"})
_LOCK = threading.RLock()


class ImageGenerationLedgerError(RuntimeError):
    """The paid-operation ledger cannot be used safely."""


def default_image_generation_ledger_path() -> Path:
    configured = os.environ.get(APP_PRIVATE_DIR_ENV, "").strip()
    root = Path(configured).expanduser() if configured else Path.cwd() / "data"
    return root.resolve() / "image-generation-ledger-v1.json"


class ImageGenerationLedger:
    def __init__(self, path: str | Path | None = None) -> None:
        raw = Path(path) if path is not None else default_image_generation_ledger_path()
        self.path = Path(os.path.abspath(raw.expanduser()))

    def begin(self, entry: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Persist a submitted call, returning an existing row on replay."""

        call_id = _bounded_required(entry.get("call_id"), 160, "call_id")
        now = _now()
        with _LOCK:
            entries = self._load()
            existing = next((item for item in entries if item["call_id"] == call_id), None)
            if existing is not None:
                return dict(existing), False
            if len(entries) >= MAX_LEDGER_ENTRIES:
                entries = _prune(entries)
            if len(entries) >= MAX_LEDGER_ENTRIES:
                raise ImageGenerationLedgerError(
                    "Too many unresolved image-generation billing operations"
                )
            row = {
                "call_id": call_id,
                "provider": "siliconflow",
                "model": _bounded_required(entry.get("model"), 160, "model"),
                "prompt_sha256": _sha256(entry.get("prompt_sha256")),
                "parameters_sha256": _sha256(entry.get("parameters_sha256")),
                "output_path": _bounded_required(entry.get("output_path"), 2_000, "output_path"),
                "image_size": _bounded_required(entry.get("image_size"), 32, "image_size"),
                "status": "submitted",
                "created_at": now,
                "updated_at": now,
            }
            entries.append(row)
            self._write(entries)
            return dict(row), True

    def mark(self, call_id: str, status: str, **metadata: Any) -> dict[str, Any]:
        if status not in {
            "completed",
            "rejected",
            "uncertain",
            "output_failed",
        }:
            raise ImageGenerationLedgerError("Invalid image-generation billing status")
        normalized_call_id = _bounded_required(call_id, 160, "call_id")
        with _LOCK:
            entries = self._load()
            row = next(
                (item for item in entries if item["call_id"] == normalized_call_id),
                None,
            )
            if row is None:
                raise ImageGenerationLedgerError("Image-generation operation is not recorded")
            row["status"] = status
            row["updated_at"] = _now()
            for key in ("content_hash", "width", "height", "bytes", "seed", "trace_id"):
                if key not in metadata:
                    continue
                value = metadata[key]
                if key == "content_hash":
                    row[key] = _sha256(value)
                elif key in {"width", "height", "bytes", "seed"}:
                    if value is None:
                        row[key] = None
                    elif isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                        row[key] = value
                elif key == "trace_id" and value is not None:
                    row[key] = str(value)[:240]
            entries = _prune(entries)
            self._write(entries)
            return dict(row)

    def _load(self) -> list[dict[str, Any]]:
        if not self.path.exists() and not self.path.is_symlink():
            return []
        if self.path.is_symlink() or not self.path.is_file():
            raise ImageGenerationLedgerError(
                "Image-generation billing ledger is redirected or unavailable"
            )
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ImageGenerationLedgerError(
                "Image-generation billing ledger is unreadable or corrupt"
            ) from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != LEDGER_SCHEMA_VERSION:
            raise ImageGenerationLedgerError("Unsupported image-generation billing ledger")
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, list) or len(raw_entries) > MAX_LEDGER_ENTRIES:
            raise ImageGenerationLedgerError("Invalid image-generation billing ledger entries")
        entries: list[dict[str, Any]] = []
        for raw in raw_entries:
            if not isinstance(raw, dict):
                raise ImageGenerationLedgerError("Invalid image-generation billing ledger row")
            call_id = _bounded_required(raw.get("call_id"), 160, "call_id")
            status = raw.get("status")
            if status not in {"submitted", "completed", "rejected", "uncertain", "output_failed"}:
                raise ImageGenerationLedgerError("Invalid image-generation billing status")
            if any(item["call_id"] == call_id for item in entries):
                raise ImageGenerationLedgerError("Duplicate image-generation billing call id")
            row = dict(raw)
            row["call_id"] = call_id
            entries.append(row)
        return entries

    def _write(self, entries: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ImageGenerationLedgerError(
                "Image-generation billing ledger cannot be a symbolic link"
            )
        payload = {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "entries": entries,
        }
        try:
            atomic_write_text(
                self.path,
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                mode=0o600,
            )
        except OSError as exc:
            raise ImageGenerationLedgerError(
                "Could not persist image-generation billing state"
            ) from exc


def _prune(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(entries) <= RETAIN_TERMINAL_ENTRIES:
        return entries
    unresolved = [item for item in entries if item.get("status") not in _TERMINAL_STATUSES]
    terminal = [item for item in entries if item.get("status") in _TERMINAL_STATUSES]
    terminal.sort(key=lambda item: str(item.get("updated_at", "")), reverse=True)
    keep_terminal = max(0, RETAIN_TERMINAL_ENTRIES - len(unresolved))
    retained = [*unresolved, *terminal[:keep_terminal]]
    retained.sort(key=lambda item: str(item.get("created_at", "")))
    return retained


def _bounded_required(value: Any, limit: int, field: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > limit:
        raise ImageGenerationLedgerError(f"Invalid image-generation {field}")
    return text


def _sha256(value: Any) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ImageGenerationLedgerError("Invalid image-generation SHA-256")
    return text


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
