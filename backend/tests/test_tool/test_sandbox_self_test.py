"""Native acceptance coverage for the packaged execution self-test."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.tool.sandbox_self_test import _has_python_import_smoke_output, _run


@pytest.mark.parametrize(
    "output",
    [
        "6\nFalse\n",
        "6\r\nFalse\r\n",
        "diagnostic\r\n6\r\nFalse\r\ncomplete\r\n",
    ],
)
def test_python_import_smoke_output_accepts_native_line_endings(output: str) -> None:
    assert _has_python_import_smoke_output(output)


@pytest.mark.parametrize("output", ["6\nTrue\n", "16\nFalse\n", "6 False\n"])
def test_python_import_smoke_output_rejects_changed_probe_values(output: str) -> None:
    assert not _has_python_import_smoke_output(output)


@pytest.mark.asyncio
@pytest.mark.skipif(
    not (
        sys.platform.startswith("linux")
        or sys.platform in {"darwin", "win32"}
    ),
    reason="supported desktop execution platforms",
)
async def test_native_execution_acceptance_contract(tmp_path: Path) -> None:
    report = await _run(tmp_path / "workspace")

    assert report["status"] == "ok"
    assert report["platform"] == sys.platform
    assert report["environment_sanitized"] is True
    assert report["descendant_terminated"] is True
    assert report["process_tree_reaped"] is True
    assert report["persistent_home"] is True
    assert report["pipeline_failure_propagated"] is True
    assert report["private_paths_redacted"] is True
    assert report["macos_system_python"] is True
    if sys.platform == "win32":
        assert report["sandbox"] == "windows-job-object"
        assert report["workspace_execution"] == "direct-approved"
        assert report["filesystem_isolated"] is False
        assert report["network_isolated"] is False
    else:
        assert report["workspace_execution"] == "transactional"
        assert report["filesystem_isolated"] is True
        assert report["network_isolated"] is True
