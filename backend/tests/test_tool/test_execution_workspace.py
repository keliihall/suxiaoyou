from __future__ import annotations

from pathlib import Path

import pytest

from app.schemas.agent import AgentInfo
from app.tool.context import ToolContext
from app.tool.execution_workspace import ExecutionWorkspace
from app.tool.sandbox import SandboxUnavailable


def _context(workspace: Path, *, session_id: str = "session") -> ToolContext:
    return ToolContext(
        session_id=session_id,
        message_id="message",
        agent=AgentInfo(name="test", description="", mode="primary"),
        call_id="call",
        workspace=str(workspace),
    )


def test_windows_direct_workspace_reports_written_and_deleted_artifacts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    changed = workspace / "changed.txt"
    deleted = workspace / "deleted.txt"
    changed.write_text("before", encoding="utf-8")
    deleted.write_text("remove", encoding="utf-8")
    monkeypatch.setattr("app.tool.execution_workspace.sys.platform", "win32")

    execution = ExecutionWorkspace(workspace, _context(workspace), operation="bash")
    assert execution.prepare() == workspace
    changed.write_text("after with a different size", encoding="utf-8")
    deleted.unlink()
    created = workspace / "report.mp3"
    created.write_bytes(b"audio")

    assert execution.commit() is None
    metadata = execution.success_metadata(None)

    assert set(metadata["written_files"]) == {str(changed), str(created)}
    assert metadata["deleted_files"] == [str(deleted)]
    assert metadata["artifact_tracking_complete"] is True


def test_windows_direct_workspace_ignores_dependency_and_private_trees(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr("app.tool.execution_workspace.sys.platform", "win32")

    execution = ExecutionWorkspace(workspace, _context(workspace), operation="bash")
    execution.prepare()
    (workspace / "node_modules" / "package").mkdir(parents=True)
    (workspace / "node_modules" / "package" / "index.js").write_text(
        "dependency",
        encoding="utf-8",
    )
    (workspace / ".venv" / "bin").mkdir(parents=True)
    (workspace / ".venv" / "bin" / "python").write_bytes(b"runtime")
    result = workspace / "result.wav"
    result.write_bytes(b"audio")

    execution.commit()
    assert execution.success_metadata(None)["written_files"] == [str(result)]


def test_windows_direct_scratch_rejects_redirected_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    (workspace / ".suxiaoyou").mkdir(parents=True)
    external.mkdir()
    (workspace / ".suxiaoyou" / "sandbox").symlink_to(
        external,
        target_is_directory=True,
    )
    monkeypatch.setattr("app.tool.execution_workspace.sys.platform", "win32")
    execution = ExecutionWorkspace(workspace, _context(workspace), operation="bash")
    execution.prepare()

    with pytest.raises(SandboxUnavailable, match="symlink"):
        execution.create_scratch(prefix="probe-")

    assert list(external.iterdir()) == []


def test_persistent_environment_is_stable_per_session_and_isolated_between_sessions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr("app.tool.execution_workspace.sys.platform", "win32")

    first = ExecutionWorkspace(
        workspace,
        _context(workspace, session_id="session-a"),
        operation="bash",
    )
    first.prepare()
    first_home = first.create_persistent_environment()
    (first_home / "home" / "marker.txt").write_text("kept", encoding="utf-8")
    first.abort()

    second = ExecutionWorkspace(
        workspace,
        _context(workspace, session_id="session-a"),
        operation="bash",
    )
    second.prepare()
    assert second.create_persistent_environment() == first_home
    assert (first_home / "home" / "marker.txt").read_text(encoding="utf-8") == "kept"

    other = ExecutionWorkspace(
        workspace,
        _context(workspace, session_id="session-b"),
        operation="bash",
    )
    other.prepare()
    assert other.create_persistent_environment() != first_home


def test_persistent_environment_rejects_redirected_private_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / ".suxiaoyou").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr("app.tool.execution_workspace.sys.platform", "win32")
    execution = ExecutionWorkspace(workspace, _context(workspace), operation="bash")
    execution.prepare()

    with pytest.raises(SandboxUnavailable, match="redirected"):
        execution.create_persistent_environment()


def test_output_redaction_maps_stage_and_hides_call_scratch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr("app.tool.execution_workspace.sys.platform", "linux")
    execution = ExecutionWorkspace(workspace, _context(workspace), operation="bash")
    staged = execution.prepare()
    scratch, _ = execution.create_scratch(prefix="bash-call-")

    output = execution.redact_output(f"cwd={staged}\nhome={scratch / 'home'}")

    assert f"cwd={workspace}" in output
    assert "execution-transactions" not in output
    assert "tx-" not in output
    assert str(scratch) not in output
    assert "<temporary-execution-directory>/home" in output
    execution.abort()


def test_output_redaction_inserts_windows_workspace_path_literally(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    execution = ExecutionWorkspace(workspace, _context(workspace), operation="bash")
    windows_workspace = Path(r"C:\Users\runneradmin\selected workspace")
    execution.workspace = windows_workspace
    stale_workspace = (
        r"C:\Users\runneradmin\AppData\Local\suxiaoyou\execution-transactions"
        r"\workspace-key\tx-candidate\workspace"
    )

    output = execution.redact_output(f"cwd={stale_workspace}\\result.txt")

    assert output == f"cwd={windows_workspace}\\result.txt"
