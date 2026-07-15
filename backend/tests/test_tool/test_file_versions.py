"""Built-in file mutation integration with persistent recovery versions."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from app.schemas.agent import AgentInfo
from app.storage.file_versions import FileVersionLimits, FileVersionStore
from app.tool.builtin.apply_patch import ApplyPatchTool
from app.tool.builtin.edit import EditTool
from app.tool.builtin.file_versions import FileVersionsTool, RestoreFileVersionTool
from app.tool.builtin.write import WriteTool
from app.tool.context import ToolContext
from app.tool.workspace import APP_PRIVATE_DIR_ENV


def _ctx(workspace: Path, *, language: str = "en") -> ToolContext:
    return ToolContext(
        session_id="session-file-versions",
        message_id="message-file-versions",
        agent=AgentInfo(name="test", description="", mode="primary"),
        call_id="call-file-versions",
        workspace=str(workspace),
        language=language,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_write_overwrite_captures_version_and_can_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    private = tmp_path / "private"
    monkeypatch.setenv(APP_PRIVATE_DIR_ENV, str(private))
    target = workspace / "draft.txt"
    target.write_text("before", encoding="utf-8")
    ctx = _ctx(workspace)

    result = await WriteTool().execute(
        {"file_path": str(target), "content": "after"},
        ctx,
    )
    assert result.success
    assert target.read_text(encoding="utf-8") == "after"
    version_id = result.metadata["previous_version_id"]

    listed = await FileVersionsTool().execute({"file_path": str(target)}, ctx)
    assert listed.success
    assert listed.metadata["versions"][0]["id"] == version_id

    restored = await RestoreFileVersionTool().execute({"version_id": version_id}, ctx)
    assert restored.success
    assert target.read_text(encoding="utf-8") == "before"
    assert restored.metadata["recovery_version"]["sha256"]


@pytest.mark.asyncio
async def test_edit_captures_exact_pre_edit_checksum(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv(APP_PRIVATE_DIR_ENV, str(tmp_path / "private"))
    target = workspace / "code.py"
    target.write_text("value = 'old'\n", encoding="utf-8")

    result = await EditTool().execute(
        {
            "file_path": str(target),
            "old_string": "'old'",
            "new_string": "'new'",
        },
        _ctx(workspace),
    )
    assert result.success
    version = FileVersionStore(workspace).list_versions(file_path=target)[0]
    assert version.id == result.metadata["previous_version_id"]
    restored, _, _ = FileVersionStore(workspace).restore(version.id)
    assert restored.id == version.id
    assert target.read_text(encoding="utf-8") == "value = 'old'\n"


@pytest.mark.asyncio
async def test_apply_patch_update_and_delete_are_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv(APP_PRIVATE_DIR_ENV, str(tmp_path / "private"))
    update_target = workspace / "update.txt"
    delete_target = workspace / "delete.txt"
    update_target.write_text("old\n", encoding="utf-8")
    delete_target.write_text("keep a copy\n", encoding="utf-8")
    patch = f"""*** Begin Patch
*** Update File: {update_target}
-old
+new
*** Delete File: {delete_target}
*** End Patch"""

    result = await ApplyPatchTool().execute({"patch_text": patch}, _ctx(workspace))
    assert result.success
    assert update_target.read_text(encoding="utf-8") == "new\n"
    assert not delete_target.exists()
    assert len(result.metadata["previous_versions"]) == 2

    versions = FileVersionStore(workspace).list_versions()
    delete_version = next(v for v in versions if v.relative_path == "delete.txt")
    FileVersionStore(workspace).restore(delete_version.id)
    assert delete_target.read_text(encoding="utf-8") == "keep a copy\n"


@pytest.mark.asyncio
async def test_apply_patch_late_hunk_failure_leaves_no_visible_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv(APP_PRIVATE_DIR_ENV, str(tmp_path / "private"))
    added = workspace / "added.txt"
    missing = workspace / "missing.txt"
    patch = f"""*** Begin Patch
*** Add File: {added}
+must never become visible
*** Update File: {missing}
-old
+new
*** End Patch"""

    result = await ApplyPatchTool().execute({"patch_text": patch}, _ctx(workspace))

    assert not result.success
    assert "not found" in (result.error or "")
    assert not added.exists()
    assert not FileVersionStore(workspace).list_versions()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_kind", ["write", "edit"])
async def test_text_mutation_conflict_preserves_concurrent_user_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_kind: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv(APP_PRIVATE_DIR_ENV, str(tmp_path / "private"))
    target = workspace / "draft.txt"
    target.write_text("before", encoding="utf-8")
    original_capture = FileVersionStore.capture_batch_before_mutation

    def capture_then_race(self, *args, **kwargs):
        versions = original_capture(self, *args, **kwargs)
        target.write_text("later-user-edit", encoding="utf-8")
        return versions

    monkeypatch.setattr(
        FileVersionStore,
        "capture_batch_before_mutation",
        capture_then_race,
    )
    if tool_kind == "write":
        result = await WriteTool().execute(
            {"file_path": str(target), "content": "agent-output"},
            _ctx(workspace),
        )
    else:
        result = await EditTool().execute(
            {
                "file_path": str(target),
                "old_string": "before",
                "new_string": "agent-output",
            },
            _ctx(workspace),
        )

    assert not result.success
    assert target.read_text(encoding="utf-8") == "later-user-edit"
    versions = FileVersionStore(workspace).list_versions(file_path=target)
    assert versions and versions[0].sha256


@pytest.mark.asyncio
async def test_write_ancestor_symlink_race_cannot_reach_outside_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    nested = workspace / "nested"
    nested.mkdir(parents=True)
    monkeypatch.setenv(APP_PRIVATE_DIR_ENV, str(tmp_path / "private"))
    target = nested / "draft.txt"
    target.write_text("before", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_target = outside / "draft.txt"
    outside_target.write_text("outside-safe", encoding="utf-8")
    original_capture = FileVersionStore.capture_batch_before_mutation

    def capture_then_redirect(self, *args, **kwargs):
        versions = original_capture(self, *args, **kwargs)
        nested.rename(workspace / "nested-held")
        nested.symlink_to(outside, target_is_directory=True)
        return versions

    monkeypatch.setattr(
        FileVersionStore,
        "capture_batch_before_mutation",
        capture_then_redirect,
    )

    result = await WriteTool().execute(
        {"file_path": str(target), "content": "must-not-escape"},
        _ctx(workspace),
    )

    assert not result.success
    assert outside_target.read_text(encoding="utf-8") == "outside-safe"
    assert (workspace / "nested-held" / "draft.txt").read_text(encoding="utf-8") == "before"


@pytest.mark.asyncio
async def test_write_refuses_file_with_unversioned_extended_attribute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv(APP_PRIVATE_DIR_ENV, str(tmp_path / "private"))
    target = workspace / "metadata.txt"
    target.write_text("before", encoding="utf-8")
    attribute = "com.suyo.test" if sys.platform == "darwin" else "user.suyo_test"
    try:
        if hasattr(os, "setxattr"):
            os.setxattr(target, attribute, b"preserve-me")
        elif sys.platform == "darwin":
            subprocess.run(
                ["/usr/bin/xattr", "-w", attribute, "preserve-me", str(target)],
                check=True,
                capture_output=True,
            )
        else:
            pytest.skip("extended attributes are not exposed on this platform")
    except OSError as exc:
        pytest.skip(f"extended attributes unavailable: {exc}")

    result = await WriteTool().execute(
        {"file_path": str(target), "content": "after"},
        _ctx(workspace),
    )

    assert not result.success
    assert "extended attributes" in (result.error or "")
    assert target.read_text(encoding="utf-8") == "before"
    if hasattr(os, "getxattr"):
        assert os.getxattr(target, attribute) == b"preserve-me"
    else:
        value = subprocess.run(
            ["/usr/bin/xattr", "-p", attribute, str(target)],
            check=True,
            capture_output=True,
        ).stdout.strip()
        assert value == b"preserve-me"


@pytest.mark.asyncio
async def test_write_refuses_hard_linked_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv(APP_PRIVATE_DIR_ENV, str(tmp_path / "private"))
    target = workspace / "linked.txt"
    alias = workspace / "alias.txt"
    target.write_text("before", encoding="utf-8")
    os.link(target, alias)

    result = await WriteTool().execute(
        {"file_path": str(target), "content": "after"},
        _ctx(workspace),
    )

    assert not result.success
    assert "hard-linked" in (result.error or "")
    assert target.read_text(encoding="utf-8") == "before"
    assert alias.read_text(encoding="utf-8") == "before"


@pytest.mark.asyncio
async def test_failed_snapshot_blocks_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv(APP_PRIVATE_DIR_ENV, str(tmp_path / "private"))
    target = workspace / "blocked.txt"
    original = "safe!"
    target.write_text(original, encoding="utf-8")
    monkeypatch.setattr(
        "app.tool.workspace_transaction.FileVersionStore",
        lambda selected, **_kwargs: FileVersionStore(
            selected,
            limits=FileVersionLimits(
                max_file_bytes=4,
                max_workspace_bytes=8,
                max_versions_per_file=5,
                max_total_versions=10,
            ),
        ),
    )

    result = await WriteTool().execute(
        {"file_path": str(target), "content": "must not replace"},
        _ctx(workspace),
    )
    assert not result.success
    assert "recovery limit" in (result.error or "")
    assert target.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_manifest_commit_failure_blocks_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv(APP_PRIVATE_DIR_ENV, str(tmp_path / "private"))
    target = workspace / "protected.txt"
    target.write_text("original", encoding="utf-8")

    def fail_manifest(*_args, **_kwargs):
        raise OSError("simulated manifest failure")

    monkeypatch.setattr(
        "app.storage.file_versions.atomic_write_text",
        fail_manifest,
    )
    result = await WriteTool().execute(
        {"file_path": str(target), "content": "replacement"},
        _ctx(workspace),
    )
    assert not result.success
    assert "simulated manifest failure" in (result.error or "")
    assert target.read_text(encoding="utf-8") == "original"
    store = FileVersionStore(workspace)
    assert not store.objects_dir.exists() or not list(store.objects_dir.glob("*.blob"))


@pytest.mark.asyncio
async def test_version_tools_require_workspace() -> None:
    ctx = ToolContext(
        session_id="s",
        message_id="m",
        agent=AgentInfo(name="test", description="", mode="primary"),
        call_id="c",
    )
    listed = await FileVersionsTool().execute({}, ctx)
    restored = await RestoreFileVersionTool().execute({"version_id": "missing"}, ctx)
    assert not listed.success
    assert not restored.success
