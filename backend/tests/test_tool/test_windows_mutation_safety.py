"""Win32 guarded mutation contracts and native integration coverage."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from app.schemas.agent import AgentInfo
from app.storage.file_versions import FileVersionError, FileVersionStore
from app.tool import workspace_transaction as transaction_module
from app.tool.builtin.apply_patch import ApplyPatchTool
from app.tool.builtin.edit import EditTool
from app.tool.builtin.office import OfficeTool
from app.tool.builtin.write import WriteTool
from app.tool.context import ToolContext
from app.tool.file_metadata import (
    UnsupportedFileMetadataError,
    ensure_mutation_metadata_supported,
)
from app.tool.workspace import APP_PRIVATE_DIR_ENV
from app.tool.workspace_transaction import (
    WorkspaceMutationError,
    WorkspaceMutationTransaction,
    recover_pending_workspace_transactions,
)
from app.utils.guarded_file_mutation import (
    guarded_file_mutation_supported,
    guarded_file_mutation_unavailable_reason,
)
from app.utils.windows_guarded_file import GuardedExchange
from app.utils.windows_guarded_file import (
    WindowsFileIdentity,
    WindowsGuardedFileError,
    WindowsHandleInfo,
    validate_windows_declared_path,
    validate_windows_relative_name,
    windows_relative_key,
)


WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32",
    reason="requires native Windows guarded-mutation primitives",
)


def _context(workspace: Path) -> ToolContext:
    return ToolContext(
        session_id="windows-safety-session",
        message_id="windows-safety-message",
        agent=AgentInfo(name="test", description="", mode="primary"),
        call_id="windows-safety-call",
        workspace=str(workspace),
        language="en",
    )


class _FakeWindowsBackend:
    """In-memory ReplaceFileW/MoveFileExW name transition simulator."""

    def __init__(self, values: dict[Path, str]) -> None:
        self.values = dict(values)

    def replace_file(self, target: Path, replacement: Path, backup: Path) -> None:
        if backup in self.values:
            raise FileExistsError(str(backup))
        if target not in self.values or replacement not in self.values:
            raise FileNotFoundError(str(target))
        displaced = self.values.pop(target)
        installed = self.values.pop(replacement)
        self.values[target] = installed
        self.values[backup] = displaced

    def move_noreplace(self, source: Path, destination: Path) -> None:
        if destination in self.values:
            raise FileExistsError(str(destination))
        self.values[destination] = self.values.pop(source)


class _FilesystemWindowsBackend:
    """Non-atomic host-filesystem simulator used only for state fault tests."""

    def __init__(self, *, rollback_error: int | None = None) -> None:
        self.rollback_error = rollback_error

    def path_info(self, path: Path, *, directory: bool = False) -> WindowsHandleInfo:
        info = path.stat()
        return WindowsHandleInfo(
            identity=WindowsFileIdentity(info.st_dev, info.st_ino),
            attributes=0x10 if directory else 0,
            link_count=info.st_nlink,
            size=info.st_size,
        )

    def replace_file(self, target: Path, replacement: Path, backup: Path) -> None:
        if self.rollback_error is not None:
            # Simulate rollback's documented ERROR_UNABLE_TO_MOVE_REPLACEMENT_2:
            # current target moves to conflict, replacement remains named, and
            # the visible target has a temporary name gap.
            os.replace(target, backup)
            raise WindowsGuardedFileError(
                self.rollback_error,
                "simulated partial ReplaceFileW rollback",
                target,
                may_have_mutated=True,
            )
        os.replace(target, backup)
        os.replace(replacement, target)

    def move_noreplace(self, source: Path, destination: Path) -> None:
        if destination.exists():
            raise FileExistsError(str(destination))
        source.rename(destination)


def test_guarded_mutation_capability_contract_enables_win32() -> None:
    assert guarded_file_mutation_supported("linux")
    assert guarded_file_mutation_supported("darwin")
    assert guarded_file_mutation_supported("win32")
    assert guarded_file_mutation_unavailable_reason("win32") is None


def test_replacefile_exchange_state_machine_preserves_both_objects() -> None:
    parent = Path("C:/workspace")
    target = parent / "report.docx"
    replacement = parent / ".report.new"
    displaced = parent / ".report.backup"
    conflict = parent / ".report.failed-output"
    backend = _FakeWindowsBackend({target: "user-before", replacement: "agent-after"})
    exchange = GuardedExchange(target, replacement, displaced)

    exchange.install(backend)
    assert backend.values[target] == "agent-after"
    assert backend.values[displaced] == "user-before"

    exchange.rollback(backend, conflict)
    assert backend.values[target] == "user-before"
    assert backend.values[conflict] == "agent-after"
    assert displaced not in backend.values


@pytest.mark.parametrize(
    "relative",
    [
        "file.txt:secret",
        "NUL.txt",
        "con",
        "folder/name. ",
        "folder/name.",
        "folder\\name.txt",
        "folder/COM1.log",
        "folder/bad?.txt",
    ],
)
def test_windows_relative_validator_rejects_aliases_and_devices(relative: str) -> None:
    with pytest.raises(ValueError):
        validate_windows_relative_name(relative)


def test_windows_declared_validator_checks_raw_spelling_before_resolution() -> None:
    workspace = r"C:\Users\tester\workspace"
    for value in ("foo.", "foo ", "file.txt:ads", "NUL.txt"):
        with pytest.raises(ValueError):
            validate_windows_declared_path(workspace, value)


def test_windows_declared_validator_checks_unsafe_absolute_path_outside_workspace() -> None:
    workspace = r"C:\Users\tester\workspace"
    for value in (r"D:\outside\NUL.txt", r"D:\outside\file.txt:ads"):
        with pytest.raises(ValueError):
            validate_windows_declared_path(workspace, value)


def test_windows_alias_key_and_prefix_checks_are_case_insensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert windows_relative_key("Dir/Foo.txt") == windows_relative_key("dir/foo.TXT")
    with pytest.raises(WorkspaceMutationError, match="alias"):
        transaction_module._reject_windows_aliases(("Foo.txt", "foo.TXT"))
    monkeypatch.setattr(transaction_module.sys, "platform", "win32")
    with pytest.raises(WorkspaceMutationError, match="contain one another"):
        transaction_module._reject_targeted_prefix_conflicts(("Dir", "dir/file.txt"))


@pytest.mark.parametrize("error_code", [1175, 1176])
def test_documented_replacefile_no_move_errors_leave_visible_target_unchanged(
    tmp_path: Path,
    error_code: int,
) -> None:
    target = tmp_path / "target.txt"
    replacement = tmp_path / ".replacement"
    displaced = tmp_path / ".backup"
    target.write_text("before", encoding="utf-8")
    replacement.write_text("after", encoding="utf-8")
    temporary = transaction_module._PreparedPath(
        relative="target.txt",
        temporary_name=displaced.name,
        replacement_name=replacement.name,
    )
    backend = _FilesystemWindowsBackend()
    anchor = transaction_module._WindowsWorkspaceAnchor(tmp_path, backend, (0, 1), [])
    exchange = GuardedExchange(target, replacement, displaced)

    # 1175/1176 with a backup specified retain the two original names.
    transaction_module._recover_partial_windows_exchange(
        anchor, exchange, temporary
    )
    assert error_code in {1175, 1176}
    assert target.read_text(encoding="utf-8") == "before"
    assert replacement.read_text(encoding="utf-8") == "after"


def test_partial_1177_install_restores_exact_backup_and_reports_replacement(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.txt"
    replacement = tmp_path / ".replacement"
    displaced = tmp_path / ".backup"
    replacement.write_text("after", encoding="utf-8")
    displaced.write_text("before", encoding="utf-8")
    temporary = transaction_module._PreparedPath(
        relative="target.txt",
        temporary_name=displaced.name,
        replacement_name=replacement.name,
    )
    backend = _FilesystemWindowsBackend()
    anchor = transaction_module._WindowsWorkspaceAnchor(tmp_path, backend, (0, 1), [])

    transaction_module._recover_partial_windows_exchange(
        anchor,
        GuardedExchange(target, replacement, displaced),
        temporary,
    )

    assert target.read_text(encoding="utf-8") == "before"
    assert temporary.current_sidecar_name == replacement.name
    assert replacement.read_text(encoding="utf-8") == "after"


def test_partial_1177_during_reverse_exchange_restores_backup_without_name_gap(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.txt"
    replacement = tmp_path / ".replacement"
    displaced = tmp_path / ".backup"
    target.write_text("after", encoding="utf-8")
    displaced.write_text("before", encoding="utf-8")
    temporary = transaction_module._PreparedPath(
        relative="target.txt",
        temporary_name=displaced.name,
        replacement_name=replacement.name,
    )
    backend = _FilesystemWindowsBackend(rollback_error=1177)
    anchor = transaction_module._WindowsWorkspaceAnchor(tmp_path, backend, (0, 1), [])

    transaction_module._recover_partial_windows_exchange(
        anchor,
        GuardedExchange(target, replacement, displaced),
        temporary,
    )

    assert target.read_text(encoding="utf-8") == "before"
    assert temporary.current_sidecar_name is not None
    conflict = tmp_path / temporary.current_sidecar_name
    assert conflict.read_text(encoding="utf-8") == "after"


@WINDOWS_ONLY
def test_windows_production_gate_allows_targeted_staging_only(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    full_transaction = WorkspaceMutationTransaction(
        workspace,
        _context(workspace),
        operation="windows-full-command",
        storage_root=private,
    )

    with pytest.raises(WorkspaceMutationError, match="Full-workspace command staging"):
        full_transaction.prepare()

    assert full_transaction.transaction_root is None
    assert not private.exists()

    target = workspace / "new.txt"
    targeted_transaction = WorkspaceMutationTransaction(
        workspace,
        _context(workspace),
        operation="windows-targeted-write",
        storage_root=private,
    )
    targeted_transaction.prepare_paths([target])
    targeted_transaction.staged_path(target).write_text("agent output\n", encoding="utf-8")
    commit = targeted_transaction.commit()

    assert target.read_text(encoding="utf-8") == "agent output\n"
    assert commit.written_files == (str(target),)
    assert commit.previous_version_ids == ()
    assert targeted_transaction.transaction_root is None


@WINDOWS_ONLY
@pytest.mark.asyncio
@pytest.mark.parametrize("tool_kind", ["write", "edit", "apply_patch"])
async def test_windows_production_tools_use_guarded_targeted_transactions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_kind: str,
) -> None:
    workspace = tmp_path / f"workspace-{tool_kind}"
    private = tmp_path / f"private-{tool_kind}"
    workspace.mkdir()
    existing = workspace / "existing.txt"
    existing.write_text("before\n", encoding="utf-8")
    monkeypatch.setenv(APP_PRIVATE_DIR_ENV, str(private))

    if tool_kind == "write":
        target = workspace / "new.txt"
        result = await WriteTool().execute(
            {"file_path": str(target), "content": "agent output\n"},
            _context(workspace),
        )
    elif tool_kind == "edit":
        target = existing
        result = await EditTool().execute(
            {
                "file_path": str(existing),
                "old_string": "before",
                "new_string": "after",
            },
            _context(workspace),
        )
    else:
        target = workspace / "new.txt"
        result = await ApplyPatchTool().execute(
            {
                "patch_text": (
                    "*** Begin Patch\n"
                    f"*** Add File: {target.as_posix()}\n"
                    "+agent output\n"
                    "*** End Patch"
                )
            },
            _context(workspace),
        )

    assert result.success, result.error
    assert result.metadata["workspace_transaction"] is True
    assert result.metadata["atomic_file_install"] is True
    if tool_kind == "edit":
        assert existing.read_text(encoding="utf-8") == "after\n"
        assert result.metadata["previous_version_id"]
    else:
        assert target.read_text(encoding="utf-8") == "agent output\n"


@WINDOWS_ONLY
@pytest.mark.asyncio
async def test_windows_production_office_create_uses_guarded_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace-office"
    private = tmp_path / "private-office"
    workspace.mkdir()
    target = workspace / "report.docx"
    monkeypatch.setenv(APP_PRIVATE_DIR_ENV, str(private))

    result = await OfficeTool().execute(
        {
            "file_path": str(target),
            "operation": "create",
            "document": {
                "title": "Windows report",
                "paragraphs": [{"text": "Created through guarded mutation"}],
            },
        },
        _context(workspace),
    )

    assert result.success, result.error
    assert target.is_file()
    assert result.metadata["workspace_transaction"] is True
    assert result.metadata["reopened_and_validated"] is True


@WINDOWS_ONLY
def test_windows_production_file_version_restore_round_trip(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace-restore"
    private = tmp_path / "private-restore"
    workspace.mkdir()
    target = workspace / "target.txt"
    target.write_text("before", encoding="utf-8")
    store = FileVersionStore(workspace, storage_root=private)
    version = store.capture_before_mutation(target, operation="windows-production")
    assert version is not None
    target.write_text("after", encoding="utf-8")

    restored, recovery, restored_target = store.restore(version.id)

    assert restored.id == version.id
    assert recovery is not None
    assert restored_target == target
    assert target.read_text(encoding="utf-8") == "before"
    assert sorted(path.name for path in workspace.iterdir()) == ["target.txt"]


@WINDOWS_ONLY
def test_windows_prepare_rejects_missing_or_reparse_parent_before_staging(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    missing_target = workspace / "missing" / "result.txt"
    transaction = WorkspaceMutationTransaction(
        workspace,
        _context(workspace),
        operation="windows-preflight",
        storage_root=private,
    )
    with pytest.raises(WorkspaceMutationError, match="parent must already exist"):
        transaction.prepare_paths([missing_target])
    assert not private.exists()

    real_parent = workspace / "real-parent"
    junction = workspace / "junction-parent"
    real_parent.mkdir()
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(real_parent)],
        check=True,
        capture_output=True,
        text=True,
    )
    redirected = WorkspaceMutationTransaction(
        workspace,
        _context(workspace),
        operation="windows-junction",
        storage_root=private,
    )
    with pytest.raises(WorkspaceMutationError, match="reparse point"):
        redirected.prepare_paths([junction / "result.txt"])
    assert not private.exists()


@WINDOWS_ONLY
@pytest.mark.asyncio
async def test_windows_edit_rejects_hardlink_without_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv(APP_PRIVATE_DIR_ENV, str(private))
    target = workspace / "target.txt"
    alias = workspace / "alias.txt"
    target.write_text("before", encoding="utf-8")
    os.link(target, alias)

    result = await EditTool().execute(
        {"file_path": str(target), "old_string": "before", "new_string": "after"},
        _context(workspace),
    )

    assert not result.success
    assert "hard-linked" in (result.error or "")
    assert target.read_text(encoding="utf-8") == "before"
    assert alias.read_text(encoding="utf-8") == "before"


@WINDOWS_ONLY
@pytest.mark.asyncio
async def test_windows_new_file_install_is_no_replace_under_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv(APP_PRIVATE_DIR_ENV, str(private))
    target = workspace / "new.txt"
    real_move = transaction_module.Win32Backend.move_noreplace
    injected = False

    def race_move(self, source: Path, destination: Path) -> None:
        nonlocal injected
        if destination == target and not injected:
            injected = True
            target.write_text("concurrent user", encoding="utf-8")
        return real_move(self, source, destination)

    monkeypatch.setattr(transaction_module.Win32Backend, "move_noreplace", race_move)
    result = await WriteTool().execute(
        {"file_path": str(target), "content": "agent"},
        _context(workspace),
    )

    assert not result.success
    assert target.read_text(encoding="utf-8") == "concurrent user"


@WINDOWS_ONLY
@pytest.mark.asyncio
async def test_windows_multi_file_commit_fails_before_visible_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv(APP_PRIVATE_DIR_ENV, str(private))
    first = workspace / "first.txt"
    second = workspace / "second.txt"
    result = await ApplyPatchTool().execute(
        {
            "patch_text": (
                "*** Begin Patch\n"
                f"*** Add File: {first.as_posix()}\n"
                "+one\n"
                f"*** Add File: {second.as_posix()}\n"
                "+two\n"
                "*** End Patch"
            )
        },
        _context(workspace),
    )

    assert not result.success
    assert "exactly one file" in (result.error or "")
    assert not first.exists()
    assert not second.exists()


@WINDOWS_ONLY
@pytest.mark.asyncio
async def test_windows_office_create_and_edit_use_guarded_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv(APP_PRIVATE_DIR_ENV, str(private))
    target = workspace / "report.docx"
    tool = OfficeTool()
    created = await tool.execute(
        {
            "file_path": str(target),
            "operation": "create",
            "document": {"title": "Windows report", "paragraphs": [{"text": "Before"}]},
        },
        _context(workspace),
    )
    assert created.success, created.error

    edited = await tool.execute(
        {
            "file_path": str(target),
            "operation": "edit",
            "replacements": [{"old_text": "Before", "new_text": "After"}],
        },
        _context(workspace),
    )
    assert edited.success, edited.error
    assert edited.metadata["previous_version_id"]
    assert edited.metadata["reopened_and_validated"] is True


@WINDOWS_ONLY
def test_windows_metadata_allows_acl_preserving_file_but_rejects_ads(
    tmp_path: Path,
) -> None:
    ordinary = tmp_path / "ordinary.txt"
    ordinary.write_text("ordinary", encoding="utf-8")
    ensure_mutation_metadata_supported(ordinary)

    with_ads = tmp_path / "with-ads.txt"
    with_ads.write_text("default", encoding="utf-8")
    Path(f"{with_ads}:user-metadata").write_text("hidden", encoding="utf-8")
    with pytest.raises(UnsupportedFileMetadataError, match="alternate data streams"):
        ensure_mutation_metadata_supported(with_ads)
    assert with_ads.read_text(encoding="utf-8") == "default"


@WINDOWS_ONLY
def test_windows_file_version_capture_and_restore_round_trip(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "target.txt"
    target.write_text("before", encoding="utf-8")
    store = FileVersionStore(workspace, storage_root=tmp_path / "versions")
    version = store.capture_before_mutation(target, operation="windows-test")
    assert version is not None
    assert version.object_name and version.object_name.endswith(".win32-full.blob")

    target.write_text("after", encoding="utf-8")
    restored, recovery, restored_target = store.restore(version.id)

    assert restored.id == version.id
    assert recovery is not None
    assert restored_target == target
    assert target.read_text(encoding="utf-8") == "before"


@WINDOWS_ONLY
def test_windows_file_version_restore_conflict_puts_later_edit_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "target.txt"
    target.write_text("version", encoding="utf-8")
    store = FileVersionStore(workspace, storage_root=tmp_path / "versions")
    version = store.capture_before_mutation(target, operation="capture")
    assert version is not None
    target.write_text("expected-current", encoding="utf-8")
    real_install = GuardedExchange.install
    injected = False

    def inject_later_edit(exchange: GuardedExchange, backend: object) -> None:
        nonlocal injected
        if exchange.target == target and not injected:
            injected = True
            target.write_text("later-user-edit", encoding="utf-8")
        return real_install(exchange, backend)  # type: ignore[arg-type]

    monkeypatch.setattr(GuardedExchange, "install", inject_later_edit)
    with pytest.raises(FileVersionError, match="conflicted"):
        store.restore(version.id)
    assert target.read_text(encoding="utf-8") == "later-user-edit"


@WINDOWS_ONLY
def test_windows_startup_recovery_rolls_back_interrupted_targeted_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv(APP_PRIVATE_DIR_ENV, str(private))
    target = workspace / "target.txt"
    target.write_text("before", encoding="utf-8")
    transaction = WorkspaceMutationTransaction(
        workspace,
        _context(workspace),
        operation="windows-crash",
        storage_root=private,
    )
    staged = transaction.prepare_paths([target])
    (staged / target.name).write_text("command", encoding="utf-8")
    real_exchange = transaction_module._exchange_prepared_at

    def crash_after_exchange(workspace_fd: object, temporary: object) -> None:
        real_exchange(workspace_fd, temporary)  # type: ignore[arg-type]
        raise KeyboardInterrupt("simulated Windows process death")

    monkeypatch.setattr(transaction_module, "_exchange_prepared_at", crash_after_exchange)
    with pytest.raises(KeyboardInterrupt, match="simulated Windows process death"):
        transaction.commit()
    assert target.read_text(encoding="utf-8") == "command"

    monkeypatch.setattr(transaction_module, "_exchange_prepared_at", real_exchange)
    recovered = recover_pending_workspace_transactions(storage_root=private)

    assert len(recovered) == 1
    assert target.read_text(encoding="utf-8") == "before"
    assert not (private / "execution-transactions").exists()
