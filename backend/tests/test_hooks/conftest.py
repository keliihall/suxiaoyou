from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.hooks.models import HookEvent, HookEventName


@pytest.fixture
def hook_event() -> HookEvent:
    return HookEvent(
        event_id="evt-1",
        event=HookEventName.PRE_TOOL_USE,
        sequence=7,
        occurred_at=datetime.now(timezone.utc),
        session_id="session-1",
        root_turn_id="turn-1",
        call_id="call-1",
        workspace_instance_id="workspace-1",
        payload={
            "tool_name": "write",
            "tool_args": {"file_path": "report.docx"},
            "permission_decision": "ask",
        },
    )


@pytest.fixture
def executable_hook(tmp_path: Path):
    def create(name: str, body: str) -> Path:
        path = tmp_path / name
        path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
        path.chmod(0o700)
        return path

    return create
