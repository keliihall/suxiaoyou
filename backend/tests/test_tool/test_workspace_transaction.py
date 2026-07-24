from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import stat
import threading

import pytest

from app.schemas.agent import AgentInfo
from app.office_validation.draft import OfficeDraftSeal
from app.storage import file_versions as file_versions_module
from app.storage.file_versions import FileVersionStore
from app.storage.workspace_identity import ensure_workspace_identity
from app.tool.context import ToolContext
from app.tool import workspace_transaction as transaction_module
from app.tool.workspace_transaction import (
    WorkspaceMutationError,
    WorkspaceMutationTransaction,
    WorkspacePrecommitSealError,
    committed_checkpoint_journal_action,
    list_committed_checkpoint_journals,
    recover_pending_workspace_transactions,
    recover_pending_workspace_transactions_isolated,
)


def _context(workspace: Path) -> ToolContext:
    return ToolContext(
        session_id="session",
        message_id="message",
        agent=AgentInfo(name="test", description="", mode="primary"),
        call_id="call",
        workspace=str(workspace),
    )


def _transaction(
    workspace: Path,
    private: Path,
) -> WorkspaceMutationTransaction:
    return WorkspaceMutationTransaction(
        workspace,
        _context(workspace),
        operation="test.command",
        storage_root=private,
    )


def _single_pending_journal(private: Path) -> Path:
    journals = list(
        private.glob("execution-transactions/*/tx-*/journal-v1.json")
    )
    assert len(journals) == 1
    return journals[0]


def _rewrite_pending_journal(
    private: Path,
    transform: Callable[[dict[str, object]], None],
) -> dict[str, object]:
    journal = _single_pending_journal(private)
    payload = json.loads(journal.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    transform(payload)
    journal.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return payload


def _checkpoint_context(workspace: Path, *, checkpoint_id: str) -> ToolContext:
    identity = ensure_workspace_identity(workspace)
    return ToolContext(
        session_id="session",
        message_id="message",
        agent=AgentInfo(name="test", description="", mode="primary"),
        call_id="call",
        workspace=str(workspace),
        root_turn_id="root-turn",
        turn_run_id="turn-run",
        checkpoint_id=checkpoint_id,
        workspace_instance_id="workspace-instance",
        workspace_identity_token=identity.durable_token,
    )


@pytest.mark.workspace_identity_v2
def test_bound_transaction_rejects_replaced_workspace_before_staging(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    original = tmp_path / "original-workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    admitted = ensure_workspace_identity(workspace)
    workspace.rename(original)
    workspace.mkdir()
    target = workspace / "target.txt"
    target.write_text("replacement", encoding="utf-8")
    context = ToolContext(
        session_id="session",
        message_id="message",
        agent=AgentInfo(name="test", description="", mode="primary"),
        call_id="call",
        workspace=str(workspace),
        checkpoint_id="checkpoint",
        workspace_instance_id="workspace-instance",
        workspace_identity_token=admitted.durable_token,
    )
    transaction = WorkspaceMutationTransaction(
        workspace,
        context,
        operation="test.command",
        storage_root=private,
    )

    with pytest.raises(WorkspaceMutationError, match="durable identity"):
        transaction.prepare_paths([target])

    assert transaction.transaction_root is None
    assert target.read_text(encoding="utf-8") == "replacement"
    assert not (workspace / ".suxiaoyou" / "workspace-identity-v2").exists()
    assert not (private / "execution-transactions").exists()


def _office_seal(
    transaction: WorkspaceMutationTransaction,
    staged: Path,
    source: Path,
    *,
    bind: bool = True,
) -> OfficeDraftSeal:
    root_info = staged.stat(follow_symlinks=False)
    source_info = source.stat(follow_symlinks=False)
    content = source.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    validation_generation = None
    if bind:
        logical_target = transaction.workspace / source.relative_to(staged)
        validation_generation = transaction.arm_office_precommit_validation(
            logical_target
        ).validation_generation
    return OfficeDraftSeal(
        relative_path=source.relative_to(staged).as_posix(),
        source_sha256=digest,
        source_mode=stat.S_IMODE(source_info.st_mode),
        source_size=len(content),
        root_identity=(root_info.st_dev, root_info.st_ino),
        source_identity=(source_info.st_dev, source_info.st_ino),
        validation_generation=validation_generation,
        renderer_id="test-authoritative-renderer",
        renderer_version="1",
        font_digest="f" * 64,
        parameters_version="office-test-v1",
        parameters_sha256="a" * 64,
        quality="authoritative",
        cache_key="b" * 64,
    )


@pytest.mark.skipif(
    transaction_module.guarded_file_mutation_unavailable_reason() is not None,
    reason="guarded mutation primitive unavailable",
)
def test_authoritative_office_seal_commits_exact_hidden_replacement(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    target = workspace / "report.docx"
    target.write_bytes(b"before")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare_paths([target])
    staged_target = transaction.staged_path(target)
    staged_target.write_bytes(b"validated candidate")
    seal = _office_seal(transaction, staged, staged_target)

    result = transaction.commit_with_precommit_office_seal(seal)

    assert target.read_bytes() == b"validated candidate"
    assert result.written_files == (str(target),)
    assert "office_seal" not in result.metadata
    assert "precommit_seal" not in result.metadata


@pytest.mark.skipif(
    transaction_module.guarded_file_mutation_unavailable_reason() is not None,
    reason="guarded mutation primitive unavailable",
)
def test_unarmed_office_seal_cannot_match_an_absent_generation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    target = workspace / "report.docx"
    target.write_bytes(b"before")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare_paths([target])
    staged_target = transaction.staged_path(target)
    staged_target.write_bytes(b"candidate")
    unbound_seal = _office_seal(
        transaction,
        staged,
        staged_target,
        bind=False,
    )

    with pytest.raises(WorkspacePrecommitSealError, match="not armed"):
        transaction.commit_with_precommit_office_seal(unbound_seal)

    assert target.read_bytes() == b"before"


def test_office_reset_is_serialized_by_the_transaction_state_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    transaction = _transaction(workspace, private)
    worker_started = threading.Event()
    reset_body_entered = threading.Event()
    worker_finished = threading.Event()
    failures: list[BaseException] = []

    def guarded_reset_body(logical_target: object) -> object:
        del logical_target
        reset_body_entered.set()
        return object()

    monkeypatch.setattr(
        transaction,
        "_reset_office_precommit_target",
        guarded_reset_body,
    )

    def reset_worker() -> None:
        worker_started.set()
        try:
            transaction.reset_office_precommit_target(workspace / "report.docx")
        except BaseException as exc:  # pragma: no cover - assertion reports it
            failures.append(exc)
        finally:
            worker_finished.set()

    worker = threading.Thread(target=reset_worker)
    with transaction._office_state_lock:
        worker.start()
        assert worker_started.wait(timeout=1)
        assert not reset_body_entered.wait(timeout=0.1)
        assert not worker_finished.is_set()

    assert reset_body_entered.wait(timeout=1)
    worker.join(timeout=1)
    assert worker_finished.is_set()
    assert failures == []


@pytest.mark.skipif(
    transaction_module.guarded_file_mutation_unavailable_reason() is not None,
    reason="guarded mutation primitive unavailable",
)
def test_plain_commit_rechecks_armed_state_inside_transaction_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    target = workspace / "report.docx"
    target.write_bytes(b"before")
    transaction = _transaction(workspace, private)
    transaction.prepare_paths([target])
    staged_target = transaction.staged_path(target)
    staged_target.write_bytes(b"candidate")
    outer_check_passed = threading.Event()
    continue_commit = threading.Event()
    worker_finished = threading.Event()
    failures: list[BaseException] = []
    real_commit = transaction._commit

    def pause_after_plain_commit_outer_check(
        *,
        office_seal: OfficeDraftSeal | None,
    ) -> object:
        outer_check_passed.set()
        assert continue_commit.wait(timeout=1)
        return real_commit(office_seal=office_seal)

    monkeypatch.setattr(transaction, "_commit", pause_after_plain_commit_outer_check)

    def commit_worker() -> None:
        try:
            transaction.commit()
        except BaseException as exc:  # expected and asserted below
            failures.append(exc)
        finally:
            worker_finished.set()

    worker = threading.Thread(target=commit_worker)
    worker.start()
    assert outer_check_passed.wait(timeout=1)
    transaction.arm_office_precommit_validation(target)
    continue_commit.set()
    worker.join(timeout=1)

    assert worker_finished.is_set()
    assert len(failures) == 1
    assert isinstance(failures[0], WorkspacePrecommitSealError)
    assert "requires its precommit seal" in str(failures[0])
    assert target.read_bytes() == b"before"
    transaction.abort()


@pytest.mark.skipif(
    transaction_module.guarded_file_mutation_unavailable_reason() is not None,
    reason="guarded mutation primitive unavailable",
)
def test_office_sealed_create_builds_fresh_workspace_output_parent(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    target = workspace / "suxiaoyou_written" / "report.docx"
    transaction = _transaction(workspace, private)
    staged = transaction.prepare_paths([target])
    staged_target = transaction.staged_path(target)
    staged_target.parent.mkdir()
    staged_target.write_bytes(b"validated create")
    seal = _office_seal(transaction, staged, staged_target)

    result = transaction.commit_with_precommit_office_seal(seal)

    assert target.read_bytes() == b"validated create"
    assert result.written_files == (str(target),)
    assert tuple(
        path.name
        for path in workspace.iterdir()
        if path.name != ".suxiaoyou"
    ) == (
        "suxiaoyou_written",
    )


@pytest.mark.skipif(
    transaction_module.guarded_file_mutation_unavailable_reason() is not None,
    reason="guarded mutation primitive unavailable",
)
def test_office_sealed_create_builds_only_nested_missing_ancestor_chain(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    target = workspace / "deliverables" / "2026" / "q3" / "report.docx"
    transaction = _transaction(workspace, private)
    staged = transaction.prepare_paths([target])
    staged_target = transaction.staged_path(target)
    staged_target.parent.mkdir(parents=True)
    staged_target.write_bytes(b"nested validated create")
    seal = _office_seal(transaction, staged, staged_target)

    transaction.commit_with_precommit_office_seal(seal)

    assert target.read_bytes() == b"nested validated create"
    assert sorted(
        path.relative_to(workspace).as_posix()
        for path in workspace.rglob("*")
        if ".suxiaoyou" not in path.relative_to(workspace).parts
    ) == [
        "deliverables",
        "deliverables/2026",
        "deliverables/2026/q3",
        "deliverables/2026/q3/report.docx",
    ]


@pytest.mark.skipif(
    transaction_module.guarded_file_mutation_unavailable_reason() is not None,
    reason="guarded mutation primitive unavailable",
)
def test_office_sealed_create_rejects_unrelated_extra_directory(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    target = workspace / "reports" / "report.docx"
    transaction = _transaction(workspace, private)
    staged = transaction.prepare_paths([target])
    staged_target = transaction.staged_path(target)
    staged_target.parent.mkdir()
    staged_target.write_bytes(b"candidate")
    (staged / "unrelated").mkdir()
    seal = _office_seal(transaction, staged, staged_target)

    with pytest.raises(WorkspaceMutationError, match="undeclared directory"):
        transaction.commit_with_precommit_office_seal(seal)

    assert not (workspace / "reports").exists()
    assert not (workspace / "unrelated").exists()


@pytest.mark.skipif(
    transaction_module.guarded_file_mutation_unavailable_reason() is not None,
    reason="guarded mutation primitive unavailable",
)
def test_office_sealed_create_rejects_sibling_directory_below_new_parent(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    target = workspace / "reports" / "final" / "report.docx"
    transaction = _transaction(workspace, private)
    staged = transaction.prepare_paths([target])
    staged_target = transaction.staged_path(target)
    staged_target.parent.mkdir(parents=True)
    staged_target.write_bytes(b"candidate")
    (staged / "reports" / "sibling").mkdir()
    seal = _office_seal(transaction, staged, staged_target)

    with pytest.raises(WorkspaceMutationError, match="undeclared directory"):
        transaction.commit_with_precommit_office_seal(seal)

    assert not (workspace / "reports").exists()


@pytest.mark.skipif(
    os.name != "posix"
    or transaction_module.guarded_file_mutation_unavailable_reason() is not None,
    reason="directory mode mutation assertion requires POSIX",
)
def test_office_sealed_edit_rejects_existing_parent_metadata_mutation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    parent = workspace / "reports"
    parent.mkdir(parents=True, mode=0o750)
    parent.chmod(0o750)
    target = parent / "report.docx"
    target.write_bytes(b"before")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare_paths([target])
    staged_target = transaction.staged_path(target)
    staged_target.write_bytes(b"candidate")
    (staged / "reports").chmod(0o700)
    seal = _office_seal(transaction, staged, staged_target)

    with pytest.raises(WorkspaceMutationError, match="undeclared directory"):
        transaction.commit_with_precommit_office_seal(seal)

    assert target.read_bytes() == b"before"
    assert stat.S_IMODE(parent.stat().st_mode) == 0o750


@pytest.mark.skipif(
    transaction_module.guarded_file_mutation_unavailable_reason() is not None,
    reason="guarded mutation primitive unavailable",
)
def test_office_sealed_create_rolls_back_ancestors_after_precommit_fault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    target = workspace / "suxiaoyou_written" / "nested" / "report.docx"
    transaction = _transaction(workspace, private)
    staged = transaction.prepare_paths([target])
    staged_target = transaction.staged_path(target)
    staged_target.parent.mkdir(parents=True)
    staged_target.write_bytes(b"validated candidate")
    seal = _office_seal(transaction, staged, staged_target)
    real_prepare = transaction_module._prepare_regular_replacement_at
    exchange_calls = 0

    def prepare_then_tamper(
        workspace_fd: int,
        source: Path,
        relative: str,
        mode: int,
        *,
        temporary_name: str,
    ):
        prepared = real_prepare(
            workspace_fd,
            source,
            relative,
            mode,
            temporary_name=temporary_name,
        )
        hidden_name = prepared.replacement_name or prepared.temporary_name
        hidden = workspace / Path(relative).parent / hidden_name
        hidden.write_bytes(b"fault after hidden prepare")
        return prepared

    def record_exchange(workspace_fd: int, temporary: object) -> None:
        del workspace_fd, temporary
        nonlocal exchange_calls
        exchange_calls += 1

    monkeypatch.setattr(
        transaction_module,
        "_prepare_regular_replacement_at",
        prepare_then_tamper,
    )
    monkeypatch.setattr(transaction_module, "_link_prepared_new_at", record_exchange)

    with pytest.raises(WorkspacePrecommitSealError, match="no longer matches"):
        transaction.commit_with_precommit_office_seal(seal)

    assert exchange_calls == 0
    assert not (workspace / "suxiaoyou_written").exists()
    assert transaction.transaction_root is None


@pytest.mark.skipif(
    transaction_module.guarded_file_mutation_unavailable_reason() is not None,
    reason="guarded mutation primitive unavailable",
)
@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("source_sha256", "0" * 64),
        ("source_size", 999),
        ("source_mode", None),
        ("relative_path", "other.docx"),
        ("quality", "approximate"),
        ("root_identity", (0, 0)),
        ("source_identity", (0, 0)),
        ("validation_generation", "0" * 64),
    ),
)
def test_wrong_office_seal_aborts_without_changing_original(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    target = workspace / "report.docx"
    target.write_bytes(b"before")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare_paths([target])
    staged_target = transaction.staged_path(target)
    staged_target.write_bytes(b"validated candidate")
    seal = _office_seal(transaction, staged, staged_target)
    if field == "source_mode":
        value = seal.source_mode ^ stat.S_IXUSR
    seal = replace(seal, **{field: value})

    with pytest.raises(WorkspacePrecommitSealError):
        transaction.commit_with_precommit_office_seal(seal)

    assert target.read_bytes() == b"before"
    assert transaction.staged_workspace is None
    assert transaction.transaction_root is None


@pytest.mark.skipif(
    transaction_module.guarded_file_mutation_unavailable_reason() is not None,
    reason="guarded mutation primitive unavailable",
)
def test_office_seal_rejects_an_unsealed_second_write(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    first = workspace / "first.docx"
    second = workspace / "second.docx"
    first.write_bytes(b"first before")
    second.write_bytes(b"second before")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare_paths([first, second])
    staged_first = transaction.staged_path(first)
    staged_second = transaction.staged_path(second)
    staged_first.write_bytes(b"first candidate")
    staged_second.write_bytes(b"second unsealed candidate")
    seal = _office_seal(transaction, staged, staged_first, bind=False)

    with pytest.raises(WorkspacePrecommitSealError, match="exact file write"):
        transaction.commit_with_precommit_office_seal(seal)

    assert first.read_bytes() == b"first before"
    assert second.read_bytes() == b"second before"
    assert transaction.staged_workspace is None


@pytest.mark.skipif(
    transaction_module.guarded_file_mutation_unavailable_reason() is not None,
    reason="guarded mutation primitive unavailable",
)
def test_office_seal_rejects_staged_tamper_after_validation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    target = workspace / "report.docx"
    target.write_bytes(b"before")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare_paths([target])
    staged_target = transaction.staged_path(target)
    staged_target.write_bytes(b"validated candidate")
    seal = _office_seal(transaction, staged, staged_target)
    staged_target.write_bytes(b"tampered after validation")

    with pytest.raises(WorkspacePrecommitSealError, match="changed after validation"):
        transaction.commit_with_precommit_office_seal(seal)

    assert target.read_bytes() == b"before"
    assert transaction.staged_workspace is None


@pytest.mark.skipif(
    transaction_module.guarded_file_mutation_unavailable_reason() is not None,
    reason="guarded mutation primitive unavailable",
)
def test_office_seal_preserves_concurrent_visible_edit(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    target = workspace / "report.docx"
    target.write_bytes(b"before")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare_paths([target])
    staged_target = transaction.staged_path(target)
    staged_target.write_bytes(b"validated candidate")
    seal = _office_seal(transaction, staged, staged_target)
    target.write_bytes(b"concurrent user edit")

    with pytest.raises(WorkspaceMutationError, match="changed outside"):
        transaction.commit_with_precommit_office_seal(seal)

    assert target.read_bytes() == b"concurrent user edit"
    assert transaction.staged_workspace is None


@pytest.mark.skipif(
    transaction_module.guarded_file_mutation_unavailable_reason() is not None,
    reason="guarded mutation primitive unavailable",
)
def test_office_seal_detects_hidden_replacement_tamper_before_exchange(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    target = workspace / "report.docx"
    target.write_bytes(b"before")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare_paths([target])
    staged_target = transaction.staged_path(target)
    staged_target.write_bytes(b"validated candidate")
    seal = _office_seal(transaction, staged, staged_target)
    real_prepare = transaction_module._prepare_regular_replacement_at
    exchange_calls = 0

    def prepare_then_tamper(
        workspace_fd: int,
        source: Path,
        relative: str,
        mode: int,
        *,
        temporary_name: str,
    ):
        prepared = real_prepare(
            workspace_fd,
            source,
            relative,
            mode,
            temporary_name=temporary_name,
        )
        hidden_name = prepared.replacement_name or prepared.temporary_name
        hidden = workspace / Path(relative).parent / hidden_name
        hidden.write_bytes(b"hidden replacement tamper")
        return prepared

    def record_exchange(workspace_fd: int, temporary: object) -> None:
        del workspace_fd, temporary
        nonlocal exchange_calls
        exchange_calls += 1

    monkeypatch.setattr(
        transaction_module,
        "_prepare_regular_replacement_at",
        prepare_then_tamper,
    )
    monkeypatch.setattr(transaction_module, "_exchange_prepared_at", record_exchange)

    with pytest.raises(WorkspacePrecommitSealError, match="no longer matches"):
        transaction.commit_with_precommit_office_seal(seal)

    assert exchange_calls == 0
    assert target.read_bytes() == b"before"
    assert transaction.staged_workspace is None


@pytest.mark.skipif(
    transaction_module.guarded_file_mutation_unavailable_reason() is not None,
    reason="guarded mutation primitive unavailable",
)
def test_office_seal_detects_source_copy_toctou_before_exchange(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    target = workspace / "report.docx"
    target.write_bytes(b"before")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare_paths([target])
    staged_target = transaction.staged_path(target)
    staged_target.write_bytes(b"validated candidate")
    seal = _office_seal(transaction, staged, staged_target)
    real_prepare = transaction_module._prepare_regular_replacement_at
    exchange_calls = 0

    def race_source_during_prepare(
        workspace_fd: int,
        source: Path,
        relative: str,
        mode: int,
        *,
        temporary_name: str,
    ):
        source.write_bytes(b"raced while copying")
        return real_prepare(
            workspace_fd,
            source,
            relative,
            mode,
            temporary_name=temporary_name,
        )

    def record_exchange(workspace_fd: int, temporary: object) -> None:
        del workspace_fd, temporary
        nonlocal exchange_calls
        exchange_calls += 1

    monkeypatch.setattr(
        transaction_module,
        "_prepare_regular_replacement_at",
        race_source_during_prepare,
    )
    monkeypatch.setattr(transaction_module, "_exchange_prepared_at", record_exchange)

    with pytest.raises(WorkspacePrecommitSealError, match="changed after validation"):
        transaction.commit_with_precommit_office_seal(seal)

    assert exchange_calls == 0
    assert target.read_bytes() == b"before"
    assert transaction.staged_workspace is None


@pytest.mark.skipif(
    transaction_module.guarded_file_mutation_unavailable_reason() is not None,
    reason="guarded mutation primitive unavailable",
)
def test_checkpoint_journal_records_explicit_turn_commit_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    target = workspace / "target.txt"
    target.write_text("before", encoding="utf-8")
    transaction = WorkspaceMutationTransaction(
        workspace,
        _checkpoint_context(workspace, checkpoint_id="checkpoint"),
        operation="test.command",
        storage_root=private,
    )
    staged = transaction.prepare_paths([target])
    (staged / "target.txt").write_text("after", encoding="utf-8")

    result = transaction.commit()
    journals = list_committed_checkpoint_journals(storage_root=private)

    assert result.checkpoint_journal_token is not None
    assert len(journals) == 1
    payload = journals[0][1]
    identity = transaction_module.inspect_workspace_identity(workspace)
    assert payload["schema_version"] == 5
    assert payload["workspace_identity"] == {
        "token": identity.durable_token,
        "dev": identity.volatile_identity[0],
        "ino": identity.volatile_identity[1],
    }
    assert committed_checkpoint_journal_action(payload) == (
        "turn_commit",
        (),
    )


@pytest.mark.skipif(
    transaction_module.guarded_file_mutation_unavailable_reason() is not None,
    reason="guarded mutation primitive unavailable",
)
def test_checkpoint_journal_records_explicit_rewind_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    target = workspace / "target.txt"
    target.write_text("after", encoding="utf-8")
    transaction = WorkspaceMutationTransaction(
        workspace,
        _checkpoint_context(workspace, checkpoint_id="checkpoint-target"),
        operation="runtime.rewind",
        storage_root=private,
        checkpoint_action="rewind",
        rewind_checkpoint_ids=("checkpoint-target", "checkpoint-later"),
    )
    staged = transaction.prepare_paths([target])
    (staged / "target.txt").write_text("before", encoding="utf-8")

    result = transaction.commit()
    journals = list_committed_checkpoint_journals(storage_root=private)

    assert result.checkpoint_journal_token is not None
    assert len(journals) == 1
    assert committed_checkpoint_journal_action(journals[0][1]) == (
        "rewind",
        ("checkpoint-target", "checkpoint-later"),
    )


def test_checkpoint_journal_action_is_schema_bound() -> None:
    legacy = {
        "schema_version": 3,
        "state": "committed",
        "runtime_checkpoint": {"checkpoint_id": "checkpoint"},
    }
    assert committed_checkpoint_journal_action(legacy) == ("turn_commit", ())

    legacy["runtime_checkpoint"]["action"] = "rewind"
    with pytest.raises(WorkspaceMutationError, match="Legacy checkpoint journal"):
        committed_checkpoint_journal_action(legacy)

    incomplete_v4 = {
        "schema_version": 4,
        "state": "committed",
        "runtime_checkpoint": {
            "checkpoint_id": "checkpoint",
            "action": "turn_commit",
        },
    }
    with pytest.raises(WorkspaceMutationError, match="action metadata is incomplete"):
        committed_checkpoint_journal_action(incomplete_v4)


@pytest.mark.skipif(
    os.name != "posix"
    or transaction_module.guarded_file_mutation_unavailable_reason() is not None,
    reason="rewind symlink deletion requires POSIX guarded mutation support",
)
def test_only_rewind_can_delete_an_existing_symlink_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    (workspace / "target.txt").write_text("target", encoding="utf-8")
    link = workspace / "latest"
    os.symlink("target.txt", link)

    ordinary = WorkspaceMutationTransaction(
        workspace,
        _checkpoint_context(workspace, checkpoint_id="ordinary"),
        operation="test.command",
        storage_root=private,
    )
    ordinary_stage = ordinary.prepare()
    (ordinary_stage / "latest").unlink()
    with pytest.raises(WorkspaceMutationError, match="Only rewind"):
        ordinary.commit()
    ordinary.abort()
    assert link.is_symlink() and os.readlink(link) == "target.txt"

    rewind = WorkspaceMutationTransaction(
        workspace,
        _checkpoint_context(workspace, checkpoint_id="rewind-target"),
        operation="runtime.rewind",
        storage_root=private,
        checkpoint_action="rewind",
        rewind_checkpoint_ids=("rewind-target",),
    )
    rewind_stage = rewind.prepare()
    (rewind_stage / "latest").unlink()
    result = rewind.commit()

    assert not link.exists() and not link.is_symlink()
    assert result.checkpoint_journal_token is not None
    assert len(result.mutations) == 1
    assert result.mutations[0].node_kind == "symlink"
    assert result.mutations[0].operation == "deleted"
    journals = list_committed_checkpoint_journals(storage_root=private)
    assert committed_checkpoint_journal_action(journals[0][1]) == (
        "rewind",
        ("rewind-target",),
    )
    assert journals[0][1]["existing_symlinks"]["latest"]["before"][
        "link_target"
    ] == "target.txt"


@pytest.mark.skipif(
    os.name != "posix"
    or transaction_module.guarded_file_mutation_unavailable_reason() is not None,
    reason="rewind symlink deletion requires POSIX guarded mutation support",
)
def test_rewind_symlink_delete_rolls_back_on_commit_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    (workspace / "target.txt").write_text("target", encoding="utf-8")
    link = workspace / "latest"
    os.symlink("target.txt", link)
    transaction = WorkspaceMutationTransaction(
        workspace,
        _checkpoint_context(workspace, checkpoint_id="rewind-target"),
        operation="runtime.rewind",
        storage_root=private,
        checkpoint_action="rewind",
        rewind_checkpoint_ids=("rewind-target",),
    )
    stage = transaction.prepare()
    (stage / "latest").unlink()
    real_write_state = transaction._write_journal_state

    def fail_final_marker(state: str) -> None:
        if state == "committed":
            raise OSError("simulated journal failure")
        real_write_state(state)

    monkeypatch.setattr(transaction, "_write_journal_state", fail_final_marker)
    with pytest.raises(WorkspaceMutationError, match="rolled back"):
        transaction.commit()
    transaction.abort()

    assert link.is_symlink()
    assert os.readlink(link) == "target.txt"


@pytest.mark.skipif(
    os.name != "posix"
    or transaction_module.guarded_file_mutation_unavailable_reason() is not None,
    reason="rewind symlink deletion requires POSIX guarded mutation support",
)
def test_startup_recovery_restores_interrupted_rewind_symlink_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    (workspace / "target.txt").write_text("target", encoding="utf-8")
    link = workspace / "latest"
    os.symlink("target.txt", link)
    transaction = WorkspaceMutationTransaction(
        workspace,
        _checkpoint_context(workspace, checkpoint_id="rewind-target"),
        operation="runtime.rewind",
        storage_root=private,
        checkpoint_action="rewind",
        rewind_checkpoint_ids=("rewind-target",),
    )
    stage = transaction.prepare()
    (stage / "latest").unlink()
    real_write_state = transaction._write_journal_state

    def crash_before_final_marker(state: str) -> None:
        if state == "committed":
            raise KeyboardInterrupt("simulated process death")
        real_write_state(state)

    monkeypatch.setattr(
        transaction,
        "_write_journal_state",
        crash_before_final_marker,
    )
    with pytest.raises(KeyboardInterrupt, match="simulated process death"):
        transaction.commit()
    assert not link.exists() and not link.is_symlink()

    recovered = recover_pending_workspace_transactions(storage_root=private)

    assert len(recovered) == 1
    assert link.is_symlink()
    assert os.readlink(link) == "target.txt"


@pytest.mark.skipif(
    os.name != "posix"
    or transaction_module.guarded_file_mutation_unavailable_reason() is not None,
    reason="byte-preserving symlink targets require POSIX",
)
def test_rewind_journal_preserves_non_utf8_symlink_target_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    link = workspace / "opaque-link"
    os.symlink(b"opaque-\xff-target", os.fsencode(link))
    transaction = WorkspaceMutationTransaction(
        workspace,
        _checkpoint_context(workspace, checkpoint_id="rewind-target"),
        operation="runtime.rewind",
        storage_root=private,
        checkpoint_action="rewind",
        rewind_checkpoint_ids=("rewind-target",),
    )
    stage = transaction.prepare()
    (stage / link.name).unlink()

    transaction.commit()
    payload = list_committed_checkpoint_journals(storage_root=private)[0][1]

    raw_target = payload["existing_symlinks"]["opaque-link"]["before"][
        "link_target"
    ]
    assert os.fsencode(raw_target) == b"opaque-\xff-target"


@pytest.mark.skipif(
    transaction_module.guarded_file_mutation_unavailable_reason() is not None,
    reason="guarded mutation primitive unavailable",
)
def test_commit_exposes_ordered_checkpoint_mutation_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    changed = workspace / "changed.txt"
    removed = workspace / "removed.txt"
    empty = workspace / "empty"
    changed.write_text("before", encoding="utf-8")
    removed.write_text("remove", encoding="utf-8")
    empty.mkdir()

    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / "changed.txt").write_text("after", encoding="utf-8")
    (staged / "removed.txt").unlink()
    (staged / "created").mkdir()
    (staged / "created" / "new.txt").write_text("new", encoding="utf-8")
    (staged / "empty").rmdir()

    commit = transaction.commit()
    mutations = [item.metadata for item in commit.mutations]

    assert [item["relative_path"] for item in mutations] == [
        "created",
        "changed.txt",
        "created/new.txt",
        "removed.txt",
        "empty",
    ]
    assert [item["operation"] for item in mutations] == [
        "created",
        "modified",
        "created",
        "deleted",
        "deleted",
    ]
    assert mutations[0]["node_kind"] == "directory"
    assert mutations[1]["before_version_id"] in commit.previous_version_ids
    assert mutations[1]["before_sha256"]
    assert mutations[1]["after_sha256"]
    assert mutations[2]["before_version_id"] is None
    assert mutations[3]["before_version_id"] in commit.previous_version_ids
    assert mutations[3]["after_sha256"] is None
    assert mutations[4]["node_kind"] == "directory"
    assert mutations[4]["before_mode"] is not None
    assert commit.metadata["workspace_mutations"] == mutations


@pytest.mark.skipif(
    os.name == "nt"
    or transaction_module.guarded_file_mutation_unavailable_reason() is not None,
    reason="symbolic links require POSIX guarded mutation support",
)
def test_created_symlink_evidence_uses_byte_stable_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    (workspace / "target.txt").write_text("target", encoding="utf-8")

    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    os.symlink("target.txt", staged / "alias.txt")

    commit = transaction.commit()
    mutation = next(
        item for item in commit.mutations if item.relative_path == "alias.txt"
    )

    assert mutation.operation == "created"
    assert mutation.node_kind == "symlink"
    assert mutation.link_target_b64 == "dGFyZ2V0LnR4dA=="
    assert mutation.after_sha256
    assert mutation.after_size == len(b"target.txt")


@pytest.mark.skipif(
    transaction_module.guarded_file_mutation_unavailable_reason() is not None,
    reason="guarded mutation primitive unavailable",
)
def test_targeted_staging_ignores_unrelated_large_and_special_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    oversized = workspace / "unrelated-large.bin"
    oversized.touch()
    os.truncate(
        oversized,
        transaction_module.MAX_STAGED_WORKSPACE_BYTES + 1,
    )
    fifo = workspace / "unrelated-pipe"
    if hasattr(os, "mkfifo"):
        os.mkfifo(fifo)

    target = workspace / "nested" / "result.txt"
    transaction = _transaction(workspace, private)
    staged = transaction.prepare_paths([target])

    assert not (staged / oversized.name).exists()
    assert not (staged / fifo.name).exists()
    staged_target = transaction.staged_path(target)
    staged_target.parent.mkdir(parents=True)
    staged_target.write_text("done", encoding="utf-8")
    transaction.commit()

    assert target.read_text(encoding="utf-8") == "done"
    assert oversized.stat().st_size == transaction_module.MAX_STAGED_WORKSPACE_BYTES + 1
    if hasattr(os, "mkfifo"):
        assert fifo.exists()


@pytest.mark.skipif(
    transaction_module.guarded_file_mutation_unavailable_reason() is not None,
    reason="guarded mutation primitive unavailable",
)
def test_targeted_read_dependency_is_staged_but_never_publishable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    image = workspace / "image.png"
    image.write_bytes(b"original")
    target = workspace / "output.docx"
    transaction = _transaction(workspace, private)
    staged = transaction.prepare_paths([target], read_paths=[image])

    assert (staged / "image.png").read_bytes() == b"original"
    (staged / "image.png").write_bytes(b"mutated")
    (staged / "output.docx").write_bytes(b"output")

    with pytest.raises(WorkspaceMutationError, match="undeclared path"):
        transaction.commit()
    assert image.read_bytes() == b"original"
    assert not target.exists()


@pytest.mark.skipif(
    transaction_module.guarded_file_mutation_unavailable_reason() is not None,
    reason="guarded mutation primitive unavailable",
)
def test_targeted_read_dependency_change_conflicts_before_output_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    image = workspace / "image.png"
    image.write_bytes(b"original")
    target = workspace / "output.docx"
    transaction = _transaction(workspace, private)
    staged = transaction.prepare_paths([target], read_paths=[image])
    (staged / "output.docx").write_bytes(b"output")
    image.write_bytes(b"concurrent edit")

    with pytest.raises(WorkspaceMutationError, match="declared path changed"):
        transaction.commit()
    assert not target.exists()
    assert image.read_bytes() == b"concurrent edit"


@pytest.mark.skipif(
    transaction_module.guarded_file_mutation_unavailable_reason() is not None,
    reason="guarded mutation primitive unavailable",
)
def test_targeted_multi_file_commit_rolls_back_first_install_on_second_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    first = workspace / "a.txt"
    second = workspace / "b.txt"
    first.write_text("a-before", encoding="utf-8")
    second.write_text("b-before", encoding="utf-8")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare_paths([first, second])
    (staged / "a.txt").write_text("a-after", encoding="utf-8")
    (staged / "b.txt").write_text("b-after", encoding="utf-8")
    real_install = transaction_module._exchange_prepared_at
    calls = 0

    def fail_second(workspace_fd: int, temporary: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated second install failure")
        real_install(workspace_fd, temporary)

    monkeypatch.setattr(transaction_module, "_exchange_prepared_at", fail_second)

    with pytest.raises(WorkspaceMutationError, match="rolled back"):
        transaction.commit()
    transaction.abort()

    assert first.read_text(encoding="utf-8") == "a-before"
    assert second.read_text(encoding="utf-8") == "b-before"


@pytest.mark.skipif(
    transaction_module.guarded_file_mutation_unavailable_reason() is not None,
    reason="guarded mutation primitive unavailable",
)
def test_targeted_read_dependency_is_rechecked_after_output_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    image = workspace / "image.png"
    image.write_bytes(b"original")
    target = workspace / "output.docx"
    transaction = _transaction(workspace, private)
    staged = transaction.prepare_paths([target], read_paths=[image])
    (staged / "output.docx").write_bytes(b"derived output")
    real_install = transaction_module._link_prepared_new_at

    def install_then_change_dependency(workspace_fd: int, temporary: object) -> None:
        real_install(workspace_fd, temporary)
        image.write_bytes(b"concurrent edit")

    monkeypatch.setattr(
        transaction_module,
        "_link_prepared_new_at",
        install_then_change_dependency,
    )

    with pytest.raises(WorkspaceMutationError, match="rolled back"):
        transaction.commit()
    transaction.abort()

    assert not target.exists()
    assert image.read_bytes() == b"concurrent edit"


def test_stages_then_versions_and_commits_create_modify_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    existing = workspace / "existing.txt"
    removed = workspace / "removed.txt"
    existing.write_text("before", encoding="utf-8")
    removed.write_text("remove me", encoding="utf-8")
    transaction = _transaction(workspace, private)

    staged = transaction.prepare()
    (staged / "existing.txt").write_text("after", encoding="utf-8")
    (staged / "removed.txt").unlink()
    (staged / "nested").mkdir()
    (staged / "nested" / "created.txt").write_text("new", encoding="utf-8")

    assert existing.read_text(encoding="utf-8") == "before"
    assert removed.exists()
    assert not (workspace / "nested").exists()

    result = transaction.commit()

    assert existing.read_text(encoding="utf-8") == "after"
    assert not removed.exists()
    assert (workspace / "nested" / "created.txt").read_text(encoding="utf-8") == "new"
    assert set(result.written_files) == {
        str(existing),
        str(workspace / "nested" / "created.txt"),
    }
    assert result.deleted_files == (str(removed),)
    versions = FileVersionStore(workspace).list_versions(limit=10)
    assert {version.relative_path for version in versions} == {
        "existing.txt",
        "removed.txt",
    }
    assert len(result.previous_version_ids) == 2
    assert result.metadata["recovery_files"] == list(result.recovery_sidecars)
    assert {
        Path(path).read_text(encoding="utf-8") for path in result.recovery_sidecars
    } == {"before", "remove me"}


def test_commit_keeps_old_open_fd_writes_reachable_in_recovery_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    target = workspace / "target.txt"
    target.write_bytes(b"before")
    descriptor = os.open(target, os.O_RDWR)
    try:
        transaction = _transaction(workspace, private)
        staged = transaction.prepare()
        (staged / target.name).write_bytes(b"command")

        result = transaction.commit()

        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(descriptor, b"late-fd-write")
        os.ftruncate(descriptor, len(b"late-fd-write"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

    assert target.read_bytes() == b"command"
    assert result.recovery_sidecars == tuple(result.metadata["recovery_sidecars"])
    assert any(Path(path).read_bytes() == b"late-fd-write" for path in result.recovery_sidecars)


def test_abort_discards_every_staged_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    target = workspace / "target.txt"
    target.write_text("before", encoding="utf-8")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / "target.txt").write_text("uncommitted", encoding="utf-8")
    (staged / "new.txt").write_text("uncommitted", encoding="utf-8")

    transaction.abort()

    assert target.read_text(encoding="utf-8") == "before"
    assert not (workspace / "new.txt").exists()
    assert FileVersionStore(workspace).list_versions() == []


def test_external_change_conflict_leaves_external_contents_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    target = workspace / "target.txt"
    target.write_text("before", encoding="utf-8")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / "target.txt").write_text("command", encoding="utf-8")
    target.write_text("external", encoding="utf-8")

    with pytest.raises(WorkspaceMutationError, match="outside the command transaction"):
        transaction.commit()

    transaction.abort()
    assert target.read_text(encoding="utf-8") == "external"
    assert FileVersionStore(workspace).list_versions() == []


def test_concurrently_created_output_is_never_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / "new.txt").write_text("command", encoding="utf-8")
    (workspace / "new.txt").write_text("external", encoding="utf-8")

    with pytest.raises(WorkspaceMutationError, match="created outside"):
        transaction.commit()

    transaction.abort()
    assert (workspace / "new.txt").read_text(encoding="utf-8") == "external"


def test_parent_symlink_swap_cannot_redirect_commit_outside_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    folder = workspace / "folder"
    folder.mkdir()
    target = folder / "target.txt"
    target.write_text("before", encoding="utf-8")
    outside_target = outside / "target.txt"
    outside_target.write_text("outside", encoding="utf-8")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / "folder" / "target.txt").write_text("command", encoding="utf-8")

    saved = workspace / "saved-folder"
    folder.rename(saved)
    try:
        folder.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("filesystem does not support directory symlinks")

    with pytest.raises(WorkspaceMutationError, match="redirected|changed"):
        transaction.commit()
    transaction.abort()

    assert outside_target.read_text(encoding="utf-8") == "outside"
    assert (saved / "target.txt").read_text(encoding="utf-8") == "before"


def test_workspace_root_swap_during_version_capture_fails_before_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    target = workspace / "target.txt"
    target.write_text("before", encoding="utf-8")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / target.name).write_text("command", encoding="utf-8")
    moved = tmp_path / "moved-workspace"
    real_capture = FileVersionStore.capture_batch_before_mutation

    def swap_root_then_capture(store: FileVersionStore, *args, **kwargs):
        workspace.rename(moved)
        workspace.mkdir()
        (workspace / "target.txt").write_text("replacement root", encoding="utf-8")
        return real_capture(store, *args, **kwargs)

    monkeypatch.setattr(
        FileVersionStore,
        "capture_batch_before_mutation",
        swap_root_then_capture,
    )

    with pytest.raises(WorkspaceMutationError, match="Workspace root changed"):
        transaction.commit()
    transaction.abort()
    assert (moved / "target.txt").read_text(encoding="utf-8") == "before"
    assert (workspace / "target.txt").read_text(encoding="utf-8") == "replacement root"


def test_incomplete_capture_batch_never_reaches_journal_or_workspace_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    target = workspace / "target.txt"
    target.write_text("before", encoding="utf-8")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / target.name).write_text("command", encoding="utf-8")
    real_capture = FileVersionStore.capture_batch_before_mutation

    def omit_capture_result(store: FileVersionStore, *args, **kwargs):
        real_capture(store, *args, **kwargs)
        return []

    monkeypatch.setattr(
        FileVersionStore,
        "capture_batch_before_mutation",
        omit_capture_result,
    )

    with pytest.raises(WorkspaceMutationError, match="complete command mutation batch"):
        transaction.commit()
    transaction.abort()
    assert target.read_text(encoding="utf-8") == "before"
    assert not list(private.glob("execution-transactions/*/*/journal-v1.json"))


def test_new_output_fault_after_atomic_install_is_rolled_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / "new.txt").write_text("command", encoding="utf-8")

    def fail_after_install(_workspace_fd: int, _relative: str) -> None:
        raise OSError("simulated directory fsync failure")

    monkeypatch.setattr(transaction_module, "_fsync_parent_at", fail_after_install)

    with pytest.raises(WorkspaceMutationError, match="rolled back"):
        transaction.commit()
    transaction.abort()
    assert not (workspace / "new.txt").exists()
    assert any(
        path.read_text(encoding="utf-8") == "command"
        for path in workspace.glob(".new.txt.suyo-tx-*")
    )


def test_recursive_nonempty_directory_delete_is_rejected_before_versioning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    folder = workspace / "folder"
    folder.mkdir()
    (folder / "data.txt").write_text("keep", encoding="utf-8")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / "folder" / "data.txt").unlink()
    (staged / "folder").rmdir()

    with pytest.raises(WorkspaceMutationError, match="non-empty baseline directory"):
        transaction.commit()
    transaction.abort()
    assert (folder / "data.txt").read_text(encoding="utf-8") == "keep"
    assert FileVersionStore(workspace).list_versions() == []


def test_empty_directory_rollback_never_chmods_a_user_recreation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX directory modes are required")
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    folder = workspace / "empty"
    folder.mkdir(mode=0o700)
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / folder.name).rmdir()
    real_remove = transaction_module._remove_directory_at
    injected = False

    def remove_then_recreate(workspace_fd: int, relative: str) -> None:
        nonlocal injected
        real_remove(workspace_fd, relative)
        if not injected and relative == folder.name:
            injected = True
            folder.mkdir(mode=0o755)
            folder.chmod(0o755)

    monkeypatch.setattr(transaction_module, "_remove_directory_at", remove_then_recreate)

    with pytest.raises(WorkspaceMutationError, match="rollback failed"):
        transaction.commit()

    assert folder.is_dir()
    assert folder.stat().st_mode & 0o777 == 0o755


def test_edit_after_atomic_exchange_is_preserved_when_rollback_refuses_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    target = workspace / "target.txt"
    target.write_text("before", encoding="utf-8")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / target.name).write_text("command", encoding="utf-8")
    real_exchange = transaction_module._exchange_prepared_at
    calls = 0

    def exchange_then_edit(workspace_fd: int, temporary: object) -> None:
        nonlocal calls
        calls += 1
        real_exchange(workspace_fd, temporary)
        if calls == 1:
            target.write_text("later user edit", encoding="utf-8")

    monkeypatch.setattr(transaction_module, "_exchange_prepared_at", exchange_then_edit)

    with pytest.raises(WorkspaceMutationError, match="rollback failed"):
        transaction.commit()
    assert calls == 2
    assert target.read_text(encoding="utf-8") == "before"
    conflict_values = {
        path.read_text(encoding="utf-8")
        for path in workspace.glob(".target.txt.suyo-tx-*")
    }
    assert "later user edit" in conflict_values


def test_new_external_hardlink_at_exchange_refuses_automatic_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    target = workspace / "target.txt"
    target.write_text("before", encoding="utf-8")
    outside_link = tmp_path / "outside-link.txt"
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / target.name).write_text("command", encoding="utf-8")
    real_exchange = transaction_module._exchange_prepared_at
    injected = False

    def link_then_exchange(workspace_fd: int, temporary: object) -> None:
        nonlocal injected
        if not injected:
            injected = True
            os.link(target, outside_link)
        real_exchange(workspace_fd, temporary)

    monkeypatch.setattr(transaction_module, "_exchange_prepared_at", link_then_exchange)

    with pytest.raises(WorkspaceMutationError, match="rollback failed"):
        transaction.commit()
    assert target.read_text(encoding="utf-8") == "command"
    assert outside_link.read_text(encoding="utf-8") == "before"
    conflict_values = {
        path.read_text(encoding="utf-8")
        for path in workspace.glob(".target.txt.suyo-tx-*")
    }
    assert "before" in conflict_values


def test_parent_moved_after_exchange_never_mutates_replacement_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    folder = workspace / "folder"
    folder.mkdir()
    target = folder / "target.txt"
    target.write_text("before", encoding="utf-8")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / "folder" / "target.txt").write_text("command", encoding="utf-8")
    moved = workspace / "moved-folder"
    real_rename = transaction_module._renameat_with_flags
    injected = False

    def exchange_then_move_parent(*args, **kwargs) -> None:
        nonlocal injected
        real_rename(*args, **kwargs)
        if kwargs.get("exchange") and not injected:
            injected = True
            folder.rename(moved)
            folder.mkdir()
            (folder / "target.txt").write_text("replacement parent", encoding="utf-8")

    monkeypatch.setattr(
        transaction_module,
        "_renameat_with_flags",
        exchange_then_move_parent,
    )

    with pytest.raises(WorkspaceMutationError, match="rollback failed"):
        transaction.commit()
    assert (folder / "target.txt").read_text(encoding="utf-8") == "replacement parent"
    assert (moved / "target.txt").read_text(encoding="utf-8") == "command"


def test_delete_conflict_restores_exact_atomically_captured_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    target = workspace / "target.txt"
    target.write_text("before", encoding="utf-8")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / target.name).unlink()
    real_read = transaction_module._read_prepared_entry
    injected = False

    def replace_captured_object(workspace_fd: int, temporary: object):
        nonlocal injected
        if not injected:
            injected = True
            (workspace / temporary.temporary_name).write_text(
                "concurrent object",
                encoding="utf-8",
            )
        return real_read(workspace_fd, temporary)

    monkeypatch.setattr(
        transaction_module,
        "_read_prepared_entry",
        replace_captured_object,
    )

    with pytest.raises(WorkspaceMutationError, match="rolled back"):
        transaction.commit()
    transaction.abort()
    assert target.read_text(encoding="utf-8") == "concurrent object"


def test_commit_failure_rolls_back_already_installed_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    first = workspace / "a.txt"
    second = workspace / "b.txt"
    first.write_text("a-before", encoding="utf-8")
    second.write_text("b-before", encoding="utf-8")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / "a.txt").write_text("a-after", encoding="utf-8")
    (staged / "b.txt").write_text("b-after", encoding="utf-8")
    real_install = transaction_module._exchange_prepared_at
    calls = 0

    def fail_second(workspace_fd: int, temporary: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated second install failure")
        real_install(workspace_fd, temporary)

    monkeypatch.setattr(transaction_module, "_exchange_prepared_at", fail_second)

    with pytest.raises(WorkspaceMutationError, match="rolled back"):
        transaction.commit()

    transaction.abort()
    assert first.read_text(encoding="utf-8") == "a-before"
    assert second.read_text(encoding="utf-8") == "b-before"
    assert any(
        path.read_text(encoding="utf-8") == "a-after"
        for path in workspace.glob(".a.txt.suyo-tx-*")
    )


def test_new_symlink_must_resolve_inside_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / "escaped").symlink_to(tmp_path / "outside")

    with pytest.raises(WorkspaceMutationError, match="outside the workspace"):
        transaction.collect_changes()

    transaction.abort()


def test_existing_symlink_mutation_is_discarded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    (workspace / "target.txt").write_text("target", encoding="utf-8")
    link = workspace / "link"
    link.symlink_to("target.txt")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / "link").unlink()

    with pytest.raises(WorkspaceMutationError, match="existing symbolic link"):
        transaction.collect_changes()

    transaction.abort()
    assert link.is_symlink()


def test_changed_hardlinked_files_are_rejected_without_breaking_link_topology(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    first = workspace / "first.txt"
    second = workspace / "second.txt"
    first.write_text("before", encoding="utf-8")
    try:
        os.link(first, second)
    except OSError:
        pytest.skip("filesystem does not support hard links")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    assert (staged / "first.txt").stat().st_ino == (staged / "second.txt").stat().st_ino

    (staged / "first.txt").write_text("after", encoding="utf-8")
    with pytest.raises(WorkspaceMutationError, match="hard-linked path"):
        transaction.commit()
    transaction.abort()

    assert first.read_text(encoding="utf-8") == "before"
    assert second.read_text(encoding="utf-8") == "before"
    assert first.stat().st_ino == second.stat().st_ino
    assert FileVersionStore(workspace).list_versions() == []


def test_startup_recovery_rolls_back_a_process_crash_mid_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    existing = workspace / "a-existing.txt"
    created = workspace / "b-created.txt"
    existing.write_text("before", encoding="utf-8")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / existing.name).write_text("after", encoding="utf-8")
    (staged / created.name).write_text("new", encoding="utf-8")
    real_install_new = transaction_module._link_prepared_new_at

    def crash_after_new_file(workspace_fd: int, temporary: object) -> None:
        real_install_new(workspace_fd, temporary)
        raise KeyboardInterrupt("simulated process death")

    monkeypatch.setattr(
        transaction_module,
        "_link_prepared_new_at",
        crash_after_new_file,
    )

    with pytest.raises(KeyboardInterrupt, match="simulated process death"):
        transaction.commit()

    assert existing.read_text(encoding="utf-8") == "after"
    assert created.read_text(encoding="utf-8") == "new"
    recovered = recover_pending_workspace_transactions(storage_root=private)

    assert len(recovered) == 1
    assert existing.read_text(encoding="utf-8") == "before"
    assert not created.exists()
    sidecar_values = {
        path.read_text(encoding="utf-8")
        for path in workspace.iterdir()
        if path.is_file() and path.name.startswith(".")
    }
    assert {"after", "new"} <= sidecar_values
    assert not (private / "execution-transactions").exists()


@pytest.mark.workspace_identity_v2
def test_schema_v5_recovery_uses_marker_not_persisted_device_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    target = workspace / "target.txt"
    target.write_text("before", encoding="utf-8")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / target.name).write_text("command", encoding="utf-8")
    real_exchange = transaction_module._exchange_prepared_at

    def crash_after_exchange(workspace_fd: int, temporary: object) -> None:
        real_exchange(workspace_fd, temporary)
        raise KeyboardInterrupt("simulated process death")

    monkeypatch.setattr(
        transaction_module,
        "_exchange_prepared_at",
        crash_after_exchange,
    )
    with pytest.raises(KeyboardInterrupt, match="simulated process death"):
        transaction.commit()
    assert target.read_text(encoding="utf-8") == "command"

    def rewrite_device_evidence(payload: dict[str, object]) -> None:
        assert payload["schema_version"] == 5
        raw_identity = payload["workspace_identity"]
        assert isinstance(raw_identity, dict)
        assert isinstance(raw_identity.get("token"), str)
        raw_identity["dev"] = int(raw_identity["dev"]) + 1

    _rewrite_pending_journal(private, rewrite_device_evidence)

    recovered = recover_pending_workspace_transactions(storage_root=private)

    assert len(recovered) == 1
    assert target.read_text(encoding="utf-8") == "before"
    assert not (private / "execution-transactions").exists()


@pytest.mark.workspace_identity_v2
def test_schema_v5_recovery_rejects_a_different_durable_marker(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    current = transaction_module.ensure_workspace_identity(workspace)
    foreign_token = (
        f"marker-v2:{'0' * 64}"
        if current.durable_token != f"marker-v2:{'0' * 64}"
        else f"marker-v2:{'1' * 64}"
    )
    payload = {
        "schema_version": 5,
        "workspace_identity": {
            "token": foreign_token,
            "dev": current.volatile_identity[0],
            "ino": current.volatile_identity[1],
        },
    }

    with pytest.raises(WorkspaceMutationError, match="Workspace root changed"):
        transaction_module._journal_recovery_workspace_identity(
            payload,
            current.canonical_path,
        )


@pytest.mark.workspace_identity_v2
@pytest.mark.skipif(
    transaction_module.sys.platform == "win32",
    reason="exercises the legacy macOS device-renumbering path",
)
def test_legacy_recovery_blocks_ambiguous_macos_device_only_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    target = workspace / "target.txt"
    target.write_text("before", encoding="utf-8")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / target.name).write_text("command", encoding="utf-8")
    real_exchange = transaction_module._exchange_prepared_at

    def crash_after_exchange(workspace_fd: int, temporary: object) -> None:
        real_exchange(workspace_fd, temporary)
        raise KeyboardInterrupt("simulated process death")

    monkeypatch.setattr(
        transaction_module,
        "_exchange_prepared_at",
        crash_after_exchange,
    )
    with pytest.raises(KeyboardInterrupt, match="simulated process death"):
        transaction.commit()

    def downgrade_with_device_drift(payload: dict[str, object]) -> None:
        payload["schema_version"] = 4
        raw_identity = payload["workspace_identity"]
        assert isinstance(raw_identity, dict)
        raw_identity.pop("token")
        raw_identity["dev"] = int(raw_identity["dev"]) + 1

    _rewrite_pending_journal(private, downgrade_with_device_drift)
    monkeypatch.setattr(transaction_module.sys, "platform", "darwin")

    with pytest.raises(WorkspaceMutationError, match="Workspace root changed"):
        recover_pending_workspace_transactions(storage_root=private)

    report = recover_pending_workspace_transactions_isolated(storage_root=private)

    assert report.recovered == ()
    assert len(report.blocked) == 1
    assert target.read_text(encoding="utf-8") == "command"
    assert (private / "execution-transactions").exists()


@pytest.mark.workspace_identity_v2
@pytest.mark.parametrize("blocked_kind", ["corrupt", "missing", "replaced"])
def test_isolated_startup_recovery_preserves_bad_journal_and_recovers_sibling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    blocked_kind: str,
) -> None:
    private = tmp_path / "private"
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    workspaces = {
        name: tmp_path / name for name in ("blocked-workspace", "healthy-workspace")
    }
    for workspace in workspaces.values():
        workspace.mkdir()
        (workspace / "target.txt").write_text("before", encoding="utf-8")

    real_exchange = transaction_module._exchange_prepared_at

    def crash_after_exchange(workspace_fd: int, temporary: object) -> None:
        real_exchange(workspace_fd, temporary)
        raise KeyboardInterrupt("simulated process death")

    monkeypatch.setattr(
        transaction_module,
        "_exchange_prepared_at",
        crash_after_exchange,
    )
    for workspace in workspaces.values():
        transaction = _transaction(workspace, private)
        staged = transaction.prepare()
        (staged / "target.txt").write_text("command", encoding="utf-8")
        with pytest.raises(KeyboardInterrupt, match="simulated process death"):
            transaction.commit()

    blocked_workspace = workspaces["blocked-workspace"]
    blocked_key = hashlib.sha256(
        os.fsencode(str(blocked_workspace.resolve()))
    ).hexdigest()
    blocked_root = private / "execution-transactions" / blocked_key
    blocked_journals = list(blocked_root.glob("tx-*/journal-v1.json"))
    assert len(blocked_journals) == 1
    blocked_journal = blocked_journals[0]
    moved_workspace = tmp_path / "moved-blocked-workspace"
    if blocked_kind == "corrupt":
        blocked_journal.write_text("{not-json", encoding="utf-8")
    elif blocked_kind == "missing":
        blocked_journal.unlink()
    else:
        blocked_workspace.rename(moved_workspace)
        blocked_workspace.mkdir()
        (blocked_workspace / "target.txt").write_text(
            "replacement root",
            encoding="utf-8",
        )

    report = recover_pending_workspace_transactions_isolated(storage_root=private)

    assert len(report.recovered) == 1
    assert len(report.blocked) == 1
    assert blocked_root.exists()
    assert (
        workspaces["healthy-workspace"] / "target.txt"
    ).read_text(encoding="utf-8") == "before"
    if blocked_kind == "replaced":
        assert (blocked_workspace / "target.txt").read_text(encoding="utf-8") == (
            "replacement root"
        )
        assert (moved_workspace / "target.txt").read_text(encoding="utf-8") == (
            "command"
        )
    else:
        assert (blocked_workspace / "target.txt").read_text(encoding="utf-8") == (
            "command"
        )


def test_startup_recovery_leaves_matching_undeleted_directory_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX directory modes are required")
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    folder = workspace / "empty"
    folder.mkdir(mode=0o710)
    folder.chmod(0o710)
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / folder.name).rmdir()

    def crash_before_remove(_workspace_fd: int, _relative: str) -> None:
        raise KeyboardInterrupt("simulated process death")

    monkeypatch.setattr(transaction_module, "_remove_directory_at", crash_before_remove)
    with pytest.raises(KeyboardInterrupt, match="simulated process death"):
        transaction.commit()

    monkeypatch.setattr(transaction_module, "_remove_directory_at", lambda *_args: None)

    def unexpected_install(*_args, **_kwargs) -> None:
        raise AssertionError("matching existing directory must not be replaced or chmodded")

    monkeypatch.setattr(
        transaction_module,
        "_install_empty_directory_noreplace_at",
        unexpected_install,
    )
    recovered = recover_pending_workspace_transactions(storage_root=private)

    assert len(recovered) == 1
    assert folder.is_dir()
    assert folder.stat().st_mode & 0o777 == 0o710


def test_startup_recovery_preserves_unproven_same_name_directory_created_after_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / "later-empty").mkdir()
    real_create = transaction_module._create_directory_at

    def crash_before_create(*_args, **_kwargs) -> None:
        raise KeyboardInterrupt("simulated process death")

    monkeypatch.setattr(
        transaction_module,
        "_create_directory_at",
        crash_before_create,
    )
    with pytest.raises(KeyboardInterrupt, match="simulated process death"):
        transaction.commit()
    assert not (workspace / "later-empty").exists()

    # This directory was created after the crash, not by the interrupted
    # transaction.  A prepared journal without an inode proof must preserve it.
    later = workspace / "later-empty"
    later.mkdir()
    monkeypatch.setattr(transaction_module, "_create_directory_at", real_create)

    recovered = recover_pending_workspace_transactions(storage_root=private)

    assert len(recovered) == 1
    assert later.is_dir()


def test_startup_recovery_removes_directory_with_durable_creation_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    created = staged / "transaction-empty"
    created.mkdir()

    def crash_before_commit_marker(_state: str) -> None:
        raise KeyboardInterrupt("simulated process death")

    monkeypatch.setattr(transaction, "_write_journal_state", crash_before_commit_marker)
    with pytest.raises(KeyboardInterrupt, match="simulated process death"):
        transaction.commit()
    visible = workspace / created.name
    assert visible.is_dir()

    recovered = recover_pending_workspace_transactions(storage_root=private)

    assert len(recovered) == 1
    assert not visible.exists()


def test_startup_recovery_installs_an_actually_removed_empty_directory_noreplace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX directory modes are required")
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    folder = workspace / "empty"
    folder.mkdir(mode=0o710)
    folder.chmod(0o710)
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / folder.name).rmdir()
    real_remove = transaction_module._remove_directory_at

    def crash_after_remove(workspace_fd: int, relative: str) -> None:
        real_remove(workspace_fd, relative)
        raise KeyboardInterrupt("simulated process death")

    monkeypatch.setattr(transaction_module, "_remove_directory_at", crash_after_remove)
    with pytest.raises(KeyboardInterrupt, match="simulated process death"):
        transaction.commit()
    assert not folder.exists()

    monkeypatch.setattr(transaction_module, "_remove_directory_at", real_remove)
    recovered = recover_pending_workspace_transactions(storage_root=private)

    assert len(recovered) == 1
    assert folder.is_dir()
    assert folder.stat().st_mode & 0o777 == 0o710


def test_startup_recovery_never_chmods_a_recreated_deleted_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX directory modes are required")
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    folder = workspace / "empty"
    folder.mkdir(mode=0o710)
    folder.chmod(0o710)
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / folder.name).rmdir()
    real_remove = transaction_module._remove_directory_at

    def crash_after_remove(workspace_fd: int, relative: str) -> None:
        real_remove(workspace_fd, relative)
        raise KeyboardInterrupt("simulated process death")

    monkeypatch.setattr(transaction_module, "_remove_directory_at", crash_after_remove)
    with pytest.raises(KeyboardInterrupt, match="simulated process death"):
        transaction.commit()
    folder.mkdir(mode=0o755)
    folder.chmod(0o755)

    monkeypatch.setattr(transaction_module, "_remove_directory_at", real_remove)
    with pytest.raises(WorkspaceMutationError, match="later recreation"):
        recover_pending_workspace_transactions(storage_root=private)

    assert folder.is_dir()
    assert folder.stat().st_mode & 0o777 == 0o755
    assert (private / "execution-transactions").exists()


def test_startup_recovery_keeps_a_fully_committed_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    target = workspace / "target.txt"
    target.write_text("before", encoding="utf-8")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / target.name).write_text("after", encoding="utf-8")
    monkeypatch.setattr(transaction, "abort", lambda: None)

    result = transaction.commit()

    assert result.written_files == (str(target),)
    assert target.read_text(encoding="utf-8") == "after"
    assert len(result.recovery_sidecars) == 1
    sidecar = Path(result.recovery_sidecars[0])
    assert sidecar.read_text(encoding="utf-8") == "before"
    recovered = recover_pending_workspace_transactions(storage_root=private)
    assert len(recovered) == 1
    assert target.read_text(encoding="utf-8") == "after"
    assert sidecar.read_text(encoding="utf-8") == "before"
    assert not (private / "execution-transactions").exists()


@pytest.mark.parametrize("legacy_schema", [False, True], ids=["v5", "legacy-v4"])
def test_startup_recovery_rejects_workspace_root_identity_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy_schema: bool,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    target = workspace / "target.txt"
    target.write_text("before", encoding="utf-8")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / target.name).write_text("command", encoding="utf-8")
    real_exchange = transaction_module._exchange_prepared_at

    def crash_after_exchange(workspace_fd: int, temporary: object) -> None:
        real_exchange(workspace_fd, temporary)
        raise KeyboardInterrupt("simulated process death")

    monkeypatch.setattr(transaction_module, "_exchange_prepared_at", crash_after_exchange)
    with pytest.raises(KeyboardInterrupt):
        transaction.commit()
    if legacy_schema:

        def downgrade(payload: dict[str, object]) -> None:
            payload["schema_version"] = 4
            raw_identity = payload["workspace_identity"]
            assert isinstance(raw_identity, dict)
            raw_identity.pop("token")

        _rewrite_pending_journal(private, downgrade)
    moved = tmp_path / "moved-workspace"
    workspace.rename(moved)
    workspace.mkdir()
    (workspace / "target.txt").write_text("replacement root", encoding="utf-8")

    with pytest.raises(WorkspaceMutationError, match="Workspace root changed"):
        recover_pending_workspace_transactions(storage_root=private)
    assert (workspace / "target.txt").read_text(encoding="utf-8") == "replacement root"
    assert (moved / "target.txt").read_text(encoding="utf-8") == "command"
    assert (private / "execution-transactions").exists()


def test_startup_recovery_never_overwrites_a_later_external_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    target = workspace / "target.txt"
    target.write_text("before", encoding="utf-8")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / target.name).write_text("command", encoding="utf-8")
    real_install = transaction_module._exchange_prepared_at

    def crash_after_install(workspace_fd: int, temporary: object) -> None:
        real_install(workspace_fd, temporary)
        raise KeyboardInterrupt("simulated process death")

    monkeypatch.setattr(
        transaction_module,
        "_exchange_prepared_at",
        crash_after_install,
    )
    with pytest.raises(KeyboardInterrupt):
        transaction.commit()
    target.write_text("later user edit", encoding="utf-8")

    with pytest.raises(WorkspaceMutationError, match="conflicts with a later edit"):
        recover_pending_workspace_transactions(storage_root=private)

    assert target.read_text(encoding="utf-8") == "later user edit"
    assert (private / "execution-transactions").exists()


def test_startup_recovery_rechecks_after_preflight_before_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(private))
    target = workspace / "target.txt"
    target.write_text("before", encoding="utf-8")
    transaction = _transaction(workspace, private)
    staged = transaction.prepare()
    (staged / target.name).write_text("command", encoding="utf-8")
    real_exchange = transaction_module._exchange_prepared_at

    def crash_after_exchange(workspace_fd: int, temporary: object) -> None:
        real_exchange(workspace_fd, temporary)
        raise KeyboardInterrupt("simulated process death")

    monkeypatch.setattr(
        transaction_module,
        "_exchange_prepared_at",
        crash_after_exchange,
    )
    with pytest.raises(KeyboardInterrupt):
        transaction.commit()
    monkeypatch.setattr(transaction_module, "_exchange_prepared_at", real_exchange)
    assert target.read_text(encoding="utf-8") == "command"

    real_restore = FileVersionStore.restore_failed_mutation_batch

    def edit_between_preflight_and_restore(
        store: FileVersionStore,
        version_ids: list[str],
        *,
        expected_current: dict[str, dict[str, object] | None] | None = None,
    ):
        target.write_text("later user edit", encoding="utf-8")
        return real_restore(
            store,
            version_ids,
            expected_current=expected_current,
        )

    monkeypatch.setattr(
        FileVersionStore,
        "restore_failed_mutation_batch",
        edit_between_preflight_and_restore,
    )
    real_atomic_rename = file_versions_module._version_atomic_rename
    atomic_exchange_calls = 0

    def count_atomic_exchange(*args, **kwargs):
        nonlocal atomic_exchange_calls
        if kwargs.get("exchange"):
            atomic_exchange_calls += 1
        return real_atomic_rename(*args, **kwargs)

    monkeypatch.setattr(
        file_versions_module,
        "_version_atomic_rename",
        count_atomic_exchange,
    )

    with pytest.raises(WorkspaceMutationError, match="later edit"):
        recover_pending_workspace_transactions(storage_root=private)
    assert atomic_exchange_calls == 2
    assert target.read_text(encoding="utf-8") == "later user edit"
    conflict_values = {
        path.read_text(encoding="utf-8")
        for path in workspace.glob(".target.txt.*.rollback.tmp")
    }
    assert "before" in conflict_values
    assert "later user edit" not in conflict_values
    assert (private / "execution-transactions").exists()
