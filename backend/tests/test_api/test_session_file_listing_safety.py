"""Workspace ownership and legacy file-recovery safety contracts."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api import sessions as sessions_api
from app.api.sessions import (
    _absolute_path_flavour,
    _extract_absolute_file_path_strings,
    _extract_file_paths_from_messages,
)
from app.models.session_file import SessionFile


pytestmark = pytest.mark.asyncio


def _tool_message(
    output: str,
    *,
    tool: str = "code_execute",
    status: str | None = "completed",
    metadata: dict | None = None,
):
    state = {"output": output, "metadata": metadata or {}}
    if status is not None:
        state["status"] = status
    return SimpleNamespace(
        data={"role": "assistant"},
        parts=[
            SimpleNamespace(
                data={"type": "tool", "tool": tool, "state": state}
            )
        ],
    )


async def _create_workspace_session(app_client, workspace: Path) -> str:
    response = await app_client.post(
        "/api/sessions",
        json={"title": "Files", "directory": str(workspace)},
    )
    assert response.status_code == 201
    return response.json()["id"]


async def test_session_files_reject_tracked_and_output_paths_outside_workspace(
    app_client,
    session_factory,
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    output_dir = workspace / "suxiaoyou_written"
    outside = tmp_path / "workspace-evil"
    output_dir.mkdir(parents=True)
    outside.mkdir()
    tracked_inside = workspace / "inside.txt"
    output_inside = output_dir / "output.txt"
    outside_file = outside / "private.txt"
    tracked_inside.write_text("inside", encoding="utf-8")
    output_inside.write_text("output", encoding="utf-8")
    outside_file.write_text("private", encoding="utf-8")
    session_id = await _create_workspace_session(app_client, workspace)

    async with session_factory() as db:
        async with db.begin():
            db.add_all(
                [
                    SessionFile(
                        session_id=session_id,
                        file_path=str(tracked_inside),
                        file_name="spoofed-name.txt",
                        tool_id="write",
                        file_type="generated",
                    ),
                    SessionFile(
                        session_id=session_id,
                        file_path=str(outside_file),
                        file_name=outside_file.name,
                        tool_id="write",
                        file_type="generated",
                    ),
                ]
            )

    response = await app_client.get(f"/api/sessions/{session_id}/files")

    assert response.status_code == 200
    files = response.json()["files"]
    assert {item["path"] for item in files} == {
        str(tracked_inside.resolve()),
        str(output_inside.resolve()),
    }
    assert next(item for item in files if item["path"] == str(tracked_inside))["name"] == "inside.txt"
    assert str(outside_file) not in {item["path"] for item in files}


async def test_session_files_reject_final_intermediate_and_broken_symlinks(
    app_client,
    session_factory,
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    output_dir = workspace / "suxiaoyou_written"
    real_dir = workspace / "real"
    outside = tmp_path / "outside"
    output_dir.mkdir(parents=True)
    real_dir.mkdir()
    outside.mkdir()
    real_file = real_dir / "real.txt"
    external = outside / "external.txt"
    real_file.write_text("real", encoding="utf-8")
    external.write_text("external", encoding="utf-8")
    final_link = output_dir / "final-link.txt"
    escape_link = output_dir / "escape-link.txt"
    broken_link = output_dir / "broken-link.txt"
    intermediate = workspace / "linked-dir"
    try:
        final_link.symlink_to(real_file)
        escape_link.symlink_to(external)
        broken_link.symlink_to(workspace / "missing.txt")
        intermediate.symlink_to(real_dir, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symbolic links unavailable: {exc}")

    session_id = await _create_workspace_session(app_client, workspace)
    async with session_factory() as db:
        async with db.begin():
            db.add(
                SessionFile(
                    session_id=session_id,
                    file_path=str(intermediate / real_file.name),
                    file_name=real_file.name,
                    tool_id="write",
                    file_type="generated",
                )
            )

    response = await app_client.get(f"/api/sessions/{session_id}/files")

    assert response.status_code == 200
    assert response.json()["files"] == []


async def test_session_files_reject_symlinked_output_directory(
    app_client,
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    outside_output = tmp_path / "outside-output"
    workspace.mkdir()
    outside_output.mkdir()
    secret = outside_output / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    try:
        (workspace / "suxiaoyou_written").symlink_to(
            outside_output,
            target_is_directory=True,
        )
    except OSError as exc:
        pytest.skip(f"symbolic links unavailable: {exc}")
    session_id = await _create_workspace_session(app_client, workspace)

    response = await app_client.get(f"/api/sessions/{session_id}/files")

    assert response.status_code == 200
    assert response.json()["files"] == []


async def test_one_output_stat_failure_is_skipped_without_failing_listing(
    app_client,
    tmp_path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    output_dir = workspace / "suxiaoyou_written"
    output_dir.mkdir(parents=True)
    stable = output_dir / "stable.txt"
    racing = output_dir / "racing.txt"
    stable.write_text("stable", encoding="utf-8")
    racing.write_text("racing", encoding="utf-8")
    session_id = await _create_workspace_session(app_client, workspace)
    original_lstat = Path.lstat

    def lstat_with_concurrent_delete(path: Path):
        if os.path.normcase(str(path)) == os.path.normcase(str(racing)):
            raise FileNotFoundError(str(path))
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", lstat_with_concurrent_delete)
    response = await app_client.get(f"/api/sessions/{session_id}/files")

    assert response.status_code == 200
    assert response.json()["files"] == [
        {
            "name": stable.name,
            "path": str(stable.resolve()),
            "type": "generated",
            "tool": "artifact",
        }
    ]


async def test_output_directory_enumeration_error_keeps_safe_tracked_files(
    app_client,
    session_factory,
    tmp_path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    output_dir = workspace / "suxiaoyou_written"
    output_dir.mkdir(parents=True)
    tracked = workspace / "tracked.txt"
    tracked.write_text("tracked", encoding="utf-8")
    session_id = await _create_workspace_session(app_client, workspace)
    async with session_factory() as db:
        async with db.begin():
            db.add(
                SessionFile(
                    session_id=session_id,
                    file_path=str(tracked),
                    file_name=tracked.name,
                    tool_id="write",
                    file_type="generated",
                )
            )
    original_iterdir = Path.iterdir

    def inaccessible_output(path: Path):
        if os.path.normcase(str(path)) == os.path.normcase(str(output_dir)):
            raise PermissionError(str(path))
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", inaccessible_output)
    response = await app_client.get(f"/api/sessions/{session_id}/files")

    assert response.status_code == 200
    assert [item["path"] for item in response.json()["files"]] == [
        str(tracked.resolve())
    ]


async def test_legacy_recovery_supports_chinese_and_quoted_space_paths(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    chinese = workspace / "会议纪要.md"
    spaced = workspace / "最终 报告.docx"
    metadata_file = workspace / "结构化 输出.xlsx"
    for path in (chinese, spaced, metadata_file):
        path.write_text("result", encoding="utf-8")
    message = _tool_message(
        f"已生成 {chinese}\nCreated file: `{spaced}`",
        metadata={"written_files": [str(metadata_file)]},
    )

    recovered = _extract_file_paths_from_messages([message], workspace)

    assert recovered == [
        str(metadata_file.resolve()),
        str(chinese.resolve()),
        str(spaced.resolve()),
    ]


async def test_legacy_created_in_supports_quoted_directory_and_unicode_leaf(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    result_dir = workspace / "结果 目录"
    result_dir.mkdir(parents=True)
    result = result_dir / "最终 报告.xlsx"
    result.write_text("result", encoding="utf-8")
    message = _tool_message(
        f"created in `{result_dir}`\n- `最终 报告.xlsx`"
    )

    assert _extract_file_paths_from_messages([message], workspace) == [
        str(result.resolve())
    ]


async def test_legacy_recovery_ignores_natural_language_failed_tools_and_links(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    input_file = workspace / "input.pdf"
    input_file.write_text("input", encoding="utf-8")
    link = workspace / "linked.pdf"
    try:
        link.symlink_to(input_file)
    except OSError as exc:
        pytest.skip(f"symbolic links unavailable: {exc}")
    assistant_text = SimpleNamespace(
        data={"role": "assistant"},
        parts=[
            SimpleNamespace(
                data={
                    "type": "text",
                    "text": f"I read `{input_file}` and generated a summary.",
                }
            )
        ],
    )
    user_text = SimpleNamespace(
        data={"role": "user"},
        parts=[
            SimpleNamespace(
                data={"type": "text", "text": f"Created file: `{input_file}`"}
            )
        ],
    )
    failed_tool = _tool_message(
        f"Created file: `{input_file}`",
        status="error",
    )
    linked_tool = _tool_message(f"Created file: `{link}`")

    assert _extract_file_paths_from_messages(
        [assistant_text, user_text, failed_tool, linked_tool],
        workspace,
    ) == []


async def test_windows_drive_and_unc_tokens_are_parsed_without_host_coercion() -> None:
    drive = r"C:\Users\Alice\最终 报告.docx"
    unc = r"\\server\share\会议记录.xlsx"

    assert _absolute_path_flavour(drive, require_file=True) == "windows"
    assert _absolute_path_flavour(unc, require_file=True) == "windows"
    assert _extract_absolute_file_path_strings(
        f"Created file: `{drive}`\nSaved output to \"{unc}\""
    ) == [drive, unc]
    assert _absolute_path_flavour(r"C:relative.txt", require_file=True) is None
    assert _absolute_path_flavour(r"\rooted-only.txt", require_file=True) is None
    assert _absolute_path_flavour("relative/report.txt", require_file=True) is None
