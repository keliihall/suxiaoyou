from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.image_generation.ledger import (
    ImageGenerationLedger,
    ImageGenerationLedgerError,
)


def _entry(call_id: str = "call-1") -> dict[str, object]:
    return {
        "call_id": call_id,
        "model": "Kwai-Kolors/Kolors",
        "prompt_sha256": "a" * 64,
        "parameters_sha256": "c" * 64,
        "output_path": "/workspace/result.png",
        "image_size": "1024x1024",
    }


def test_ledger_prevents_replay_and_persists_only_prompt_hash(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    ledger = ImageGenerationLedger(path)

    submitted, created = ledger.begin(_entry())
    replayed, replay_created = ledger.begin(_entry())
    completed = ledger.mark(
        "call-1",
        "completed",
        content_hash="b" * 64,
        width=1,
        height=1,
        bytes=68,
    )

    assert created is True
    assert replay_created is False
    assert submitted["status"] == "submitted"
    assert replayed["call_id"] == "call-1"
    assert completed["status"] == "completed"
    raw = path.read_text(encoding="utf-8")
    assert "prompt_sha256" in raw
    assert '"parameters_sha256": "' + "c" * 64 + '"' in raw
    assert "prompt text" not in raw
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_corrupt_or_redirected_ledger_fails_closed(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("not-json", encoding="utf-8")
    with pytest.raises(ImageGenerationLedgerError, match="corrupt"):
        ImageGenerationLedger(corrupt).begin(_entry())

    target = tmp_path / "target.json"
    target.write_text(json.dumps({"schema_version": 1, "entries": []}), encoding="utf-8")
    redirected = tmp_path / "redirected.json"
    try:
        redirected.symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    with pytest.raises(ImageGenerationLedgerError, match="redirected"):
        ImageGenerationLedger(redirected).begin(_entry("call-2"))
