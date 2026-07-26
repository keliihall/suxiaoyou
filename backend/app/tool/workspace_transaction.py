"""Transactional workspace staging for approved Linux command execution.

Untrusted shell/Python processes never receive a writable bind of the real
workspace.  They run against an application-private copy mounted at the same
logical path.  A successful process is diffed, every displaced regular file is
versioned as one retention-pinned batch, and each result is installed through a
same-directory temporary file plus ``os.replace``.  Failed, timed-out, or
cancelled processes simply discard the staging tree.

The transaction deliberately rejects special files and mutations of existing
symlinks.  That keeps commit and recovery semantics explicit instead of
silently following redirected paths.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import contextmanager
import base64
import ctypes
import errno
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import stat
import sys
import tempfile
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

from app.storage.file_versions import (
    FileVersion,
    FileVersionError,
    FileVersionStore,
    default_file_version_storage_root,
)
from app.storage.workspace_identity import (
    WorkspaceIdentityError,
    WorkspaceIdentityState,
    ensure_workspace_identity,
    inspect_workspace_identity,
)
from app.tool.context import ToolContext
from app.tool.sandbox import validate_workspace_private_boundary
from app.utils.atomic_write import atomic_write_text
from app.utils.guarded_file_mutation import (
    guarded_file_mutation_unavailable_reason,
)
from app.utils.windows_guarded_file import (
    GuardedExchange,
    Win32Backend,
    WindowsGuardedFileError,
    locked_directory_chain,
    open_regular_file_for_stable_read,
    validate_windows_declared_path,
    validate_windows_relative_name,
    windows_lstat_is_reparse,
    windows_path_identity,
    windows_relative_key,
)

if TYPE_CHECKING:
    from app.office_validation.draft import OfficeDraftSeal


logger = logging.getLogger(__name__)


MAX_STAGED_FILE_BYTES: Final = 100 * 1024 * 1024
MAX_STAGED_WORKSPACE_BYTES: Final = 512 * 1024 * 1024
MAX_STAGED_ENTRIES: Final = 50_000
_COPY_CHUNK_BYTES: Final = 1024 * 1024
_INTERNAL_ROOT: Final = ".suxiaoyou"
_JOURNAL_NAME: Final = "journal-v1.json"
_JOURNAL_SCHEMA_VERSION: Final = 5
_SUPPORTED_JOURNAL_SCHEMA_VERSIONS: Final = frozenset({1, 2, 3, 4, 5})


class WorkspaceMutationError(RuntimeError):
    """A staged command could not be prepared or safely committed."""


class WorkspacePrecommitSealError(WorkspaceMutationError):
    """Server-owned precommit evidence no longer matches staged output."""


class _WorkspaceParentMovedAfterOperation(WorkspaceMutationError):
    """An fd-anchored operation completed but its parent left the workspace."""


class _WindowsExchangeRecovered(WorkspaceMutationError):
    """ReplaceFileW failed, visible state was restored, sidecars may remain."""


class _WindowsExchangeAmbiguous(WorkspaceMutationError):
    """ReplaceFileW failed and exact visible state could not be proven."""


@dataclass(frozen=True, slots=True)
class WorkspaceEntry:
    kind: Literal["file", "directory", "symlink"]
    mode: int
    size: int = 0
    sha256: str | None = None
    link_target: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceOfficePrecommitView:
    """Transaction-derived identities for one armed Office validation run.

    Callers cannot choose staging or baseline paths independently.  The view is
    produced only after ``prepare_paths()`` has pinned the declared target and
    is intentionally free of any commit credential.
    """

    workspace_root: Path
    visible_target: Path
    staged_root: Path
    staged_target: Path
    relative_path: str
    workspace_identity: tuple[int, int]
    staged_root_identity: tuple[int, int]
    validation_generation: str
    baseline: WorkspaceEntry | None
    baseline_identity: tuple[int, int] | None
    operation: str
    session_id: str
    message_id: str
    call_id: str
    root_turn_id: str | None
    turn_run_id: str | None
    checkpoint_id: str | None
    workspace_instance_id: str | None


@dataclass(slots=True)
class _PreparedPath:
    relative: str
    temporary_name: str
    # ReplaceFileW needs distinct replacement and displaced-backup names.
    # POSIX exchange uses ``temporary_name`` for both roles and leaves this
    # unset.  ``current_sidecar_name`` changes only after a rollback, when the
    # failed output is preserved under a fourth conflict name.
    replacement_name: str | None = None
    current_sidecar_name: str | None = None


@dataclass(frozen=True, slots=True)
class _OfficePrecommitGuard:
    """Transaction-bound form of an authoritative Office draft seal."""

    relative: str
    expected: WorkspaceEntry
    workspace_identity: tuple[int, int]
    baseline: WorkspaceEntry | None
    created_directories: tuple[tuple[str, WorkspaceEntry], ...]
    staged_root_identity: tuple[int, int]
    staged_source_identity: tuple[int, int]
    validation_generation: str


@dataclass(slots=True)
class _WindowsWorkspaceAnchor:
    root: Path
    api: Win32Backend
    identity: tuple[int, int]
    held_directory_handles: list[int]


@dataclass(frozen=True, slots=True)
class _JournalRecoveryIdentity:
    """Root identity selected after validating a persisted journal owner."""

    volatile_identity: tuple[int, int]
    durable_token: str | None
    legacy_identity: tuple[int, int] | None


@dataclass(frozen=True, slots=True)
class WorkspaceRecoveryBlocker:
    """One journal that startup preserved because recovery was not provable.

    The owner fields are deliberately best-effort and are used only to defer
    cleanup.  They never authorize a filesystem or database mutation.  When
    ``provenance_unknown`` is true, checkpoint startup must conservatively
    avoid cleanup whose owner could be this journal.
    """

    token: str
    checkpoint_ids: tuple[str, ...]
    turn_run_ids: tuple[str, ...]
    workspace_instance_ids: tuple[str, ...]
    provenance_unknown: bool
    reason: str


@dataclass(frozen=True, slots=True)
class WorkspaceTransactionRecoveryReport:
    """Outcome of the non-fatal startup transaction recovery pass."""

    recovered: tuple[str, ...]
    blocked: tuple[WorkspaceRecoveryBlocker, ...]


@dataclass(frozen=True, slots=True)
class CommittedCheckpointJournalScan:
    """Valid committed journals plus entries unsafe to interpret."""

    journals: tuple[tuple[str, dict[str, object]], ...]
    blocked: tuple[WorkspaceRecoveryBlocker, ...]


_LOCKS_GUARD = threading.Lock()
_WORKSPACE_COMMIT_LOCKS: dict[str, threading.RLock] = {}


def _require_office_precommit_seal(value: object) -> OfficeDraftSeal:
    """Accept only the nominal server-internal Office validation evidence."""

    # Local import keeps the generic transaction module out of the Office
    # validation import graph.  In particular, this value is never parsed from
    # JSON or reconstructed from tool/model arguments.
    from app.office_validation.draft import OfficeDraftSeal

    if not isinstance(value, OfficeDraftSeal):
        raise WorkspacePrecommitSealError(
            "A server-owned Office precommit seal is required"
        )
    return value


def _office_sealed_create_ancestors(
    relative: str,
    *,
    baseline: dict[str, WorkspaceEntry],
) -> tuple[str, ...]:
    """Return the exact missing ancestor chain allowed for one sealed create."""

    if baseline.get(relative) is not None:
        return ()
    parents = tuple(
        parent.as_posix()
        for parent in reversed(Path(relative).parents)
        if parent != Path(".")
    )
    missing: list[str] = []
    missing_parent = False
    for parent in parents:
        entry = baseline.get(parent)
        if entry is None:
            missing_parent = True
            missing.append(parent)
            continue
        if entry.kind != "directory" or missing_parent:
            raise WorkspacePrecommitSealError(
                "Office precommit create ancestor baseline is inconsistent"
            )
    return tuple(missing)


@dataclass(frozen=True, slots=True)
class WorkspaceChangeSet:
    writes: tuple[str, ...]
    deletes: tuple[str, ...]
    created_directories: tuple[str, ...]
    deleted_directories: tuple[str, ...]

    @property
    def changed_paths(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                (*self.writes, *self.deletes, *self.created_directories, *self.deleted_directories)
            )
        )


@dataclass(frozen=True, slots=True)
class WorkspaceMutationRecord:
    """Canonical post-commit evidence consumed by the v1.1 checkpoint ledger.

    Absolute delivery paths remain in :class:`WorkspaceCommitResult` for v1.0
    clients.  Rewind uses only canonical workspace-relative paths plus exact
    before/after evidence, so a moved or replaced workspace root cannot be
    confused with the instance that originally committed the mutation.
    """

    relative_path: str
    operation: Literal["created", "modified", "deleted"]
    node_kind: Literal["file", "directory", "symlink"]
    before_version_id: str | None = None
    before_sha256: str | None = None
    before_mode: int | None = None
    after_sha256: str | None = None
    after_mode: int | None = None
    after_size: int | None = None
    link_target: str | None = None
    # POSIX link targets are byte strings.  Base64 preserves a target that was
    # decoded with surrogateescape without putting invalid Unicode in JSON.
    link_target_b64: str | None = None

    @property
    def metadata(self) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "operation": self.operation,
            "node_kind": self.node_kind,
            "before_version_id": self.before_version_id,
            "before_sha256": self.before_sha256,
            "before_mode": self.before_mode,
            "after_sha256": self.after_sha256,
            "after_mode": self.after_mode,
            "after_size": self.after_size,
            "link_target": self.link_target,
            "link_target_b64": self.link_target_b64,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceCommitResult:
    written_files: tuple[str, ...]
    deleted_files: tuple[str, ...]
    previous_version_ids: tuple[str, ...]
    recovery_sidecars: tuple[str, ...] = ()
    mutations: tuple[WorkspaceMutationRecord, ...] = ()
    checkpoint_journal_token: str | None = None

    @property
    def metadata(self) -> dict[str, object]:
        metadata: dict[str, object] = {
            "workspace_transaction": True,
            "atomic_file_install": True,
            "written_files": list(self.written_files),
            "deleted_files": list(self.deleted_files),
            "previous_version_ids": list(self.previous_version_ids),
            "recovery_sidecars": list(self.recovery_sidecars),
            # Generic name for tool/API consumers; values are the same hidden
            # workspace files described more precisely as sidecars above.
            "recovery_files": list(self.recovery_sidecars),
            # Versioned, workspace-relative mutation evidence.  The session
            # processor persists this before publishing TOOL_RESULT when the
            # v1.1 checkpoint gate is enabled.
            "workspace_mutations": [item.metadata for item in self.mutations],
        }
        if self.checkpoint_journal_token is not None:
            # Internal hand-off removed by SessionProcessor before the result
            # reaches SSE or message persistence.
            metadata["_checkpoint_journal"] = self.checkpoint_journal_token
        return metadata


class WorkspaceMutationTransaction:
    """One isolated command view and its eventual workspace commit."""

    def __init__(
        self,
        workspace: str | os.PathLike[str],
        ctx: ToolContext,
        *,
        operation: str,
        storage_root: str | os.PathLike[str] | None = None,
        checkpoint_action: Literal["turn_commit", "rewind"] = "turn_commit",
        rewind_checkpoint_ids: Iterable[str] = (),
    ) -> None:
        self.workspace = validate_workspace_private_boundary(workspace)
        if not self.workspace.is_dir():
            raise WorkspaceMutationError(f"Workspace does not exist: {self.workspace}")
        self.ctx = ctx
        self.operation = operation
        if checkpoint_action not in {"turn_commit", "rewind"}:
            raise WorkspaceMutationError("Unsupported checkpoint journal action")
        normalized_rewind_ids = tuple(str(value).strip() for value in rewind_checkpoint_ids)
        if (
            len(normalized_rewind_ids) > 500
            or any(not value or len(value) > 200 for value in normalized_rewind_ids)
            or len(set(normalized_rewind_ids)) != len(normalized_rewind_ids)
        ):
            raise WorkspaceMutationError("Rewind checkpoint identity list is invalid")
        if checkpoint_action == "rewind":
            if (
                not normalized_rewind_ids
                or ctx.checkpoint_id is None
                or ctx.checkpoint_id not in normalized_rewind_ids
            ):
                raise WorkspaceMutationError(
                    "Rewind journal requires its target and affected checkpoint IDs"
                )
        elif normalized_rewind_ids:
            raise WorkspaceMutationError(
                "Turn-commit journal cannot carry rewind checkpoint IDs"
            )
        self.checkpoint_action = checkpoint_action
        self.rewind_checkpoint_ids = normalized_rewind_ids
        private_base = Path(
            storage_root
            if storage_root is not None
            else default_file_version_storage_root().parent
        ).expanduser()
        self.storage_root = Path(os.path.abspath(private_base)) / "execution-transactions"
        self._workspace_key = hashlib.sha256(os.fsencode(str(self.workspace))).hexdigest()
        self._commit_lock = _workspace_commit_lock(self._workspace_key)
        self._office_state_lock = threading.RLock()
        self.transaction_root: Path | None = None
        self.staged_workspace: Path | None = None
        self._baseline: dict[str, WorkspaceEntry] | None = None
        self._baseline_hardlinks: dict[str, tuple[str, ...]] | None = None
        self._baseline_linked_paths: frozenset[str] | None = None
        self._workspace_identity: tuple[int, int] | None = None
        self._workspace_durable_token: str | None = None
        self._targeted_scope_paths: tuple[str, ...] | None = None
        self._targeted_mutation_paths: frozenset[str] | None = None
        self._targeted_read_paths: frozenset[str] | None = None
        self._targeted_declared_baseline: dict[str, WorkspaceEntry | None] | None = None
        self._targeted_source_identities: dict[str, tuple[int, int] | None] | None = None
        self._office_precommit_relative: str | None = None
        self._office_precommit_root_identity: tuple[int, int] | None = None
        self._office_precommit_generation: str | None = None
        self._finished = False
        self._preserve_for_recovery = False
        self._journal_payload: dict[str, object] | None = None

    def prepare(self) -> Path:
        """Create a byte-bounded private copy and return its host path."""

        _require_guarded_workspace_mutation_support()
        if sys.platform == "win32":
            raise WorkspaceMutationError(
                "Full-workspace command staging is unavailable on Windows; "
                "declarative file and Office tools must use prepare_paths()."
            )
        if self.transaction_root is not None:
            raise WorkspaceMutationError("Workspace transaction is already prepared")
        _validate_internal_sandbox_path(self.workspace)
        root_info = self.workspace.stat(follow_symlinks=False)
        if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
            raise WorkspaceMutationError("Workspace root is redirected")
        identity = self._ensure_workspace_identity()
        if identity.volatile_identity != _path_identity(
            self.workspace,
            directory=True,
        ):
            raise WorkspaceMutationError(
                "Workspace root changed while its durable identity was prepared"
            )
        baseline = _scan_workspace(self.workspace)
        baseline_hardlinks = _scan_hardlink_groups(self.workspace)
        baseline_linked_paths = _scan_multiply_linked_paths(self.workspace)
        workspace_root = self.storage_root / self._workspace_key
        _ensure_private_directory(self.storage_root)
        _ensure_private_directory(workspace_root)
        transaction_root = Path(
            tempfile.mkdtemp(prefix="tx-", dir=workspace_root)
        )
        os.chmod(transaction_root, 0o700)
        # Publish the root before the potentially long copy so coroutine
        # cancellation can remove it while the worker thread winds down.
        self.transaction_root = transaction_root
        staged = transaction_root / "workspace"
        try:
            copy_function = _hardlink_preserving_copy()
            shutil.copytree(
                self.workspace,
                staged,
                symlinks=True,
                copy_function=copy_function,
                ignore=lambda directory, _names: (
                    {_INTERNAL_ROOT}
                    if Path(directory) == self.workspace
                    else set()
                ),
            )
            copied = _scan_workspace(staged)
            if copied != baseline:
                raise WorkspaceMutationError(
                    "Workspace changed while its isolated command view was being prepared"
                )
            if _scan_hardlink_groups(staged) != baseline_hardlinks:
                raise WorkspaceMutationError(
                    "Workspace hard-link topology changed while its isolated view was prepared"
                )
            self._assert_durable_workspace_identity()
        except Exception:
            shutil.rmtree(transaction_root, ignore_errors=True)
            if self.transaction_root == transaction_root:
                self.transaction_root = None
            self._workspace_identity = None
            self._workspace_durable_token = None
            raise

        if self._finished:
            shutil.rmtree(transaction_root, ignore_errors=True)
            self._workspace_identity = None
            self._workspace_durable_token = None
            raise WorkspaceMutationError("Workspace transaction was cancelled during preparation")
        self.staged_workspace = staged
        self._baseline = baseline
        self._baseline_hardlinks = baseline_hardlinks
        self._baseline_linked_paths = baseline_linked_paths
        self._targeted_scope_paths = None
        self._targeted_mutation_paths = None
        self._targeted_read_paths = None
        self._targeted_declared_baseline = None
        self._targeted_source_identities = None
        return staged

    def prepare_paths(
        self,
        mutation_paths: Iterable[str | os.PathLike[str]],
        *,
        read_paths: Iterable[str | os.PathLike[str]] = (),
    ) -> Path:
        """Stage only declared file mutations and read-only inputs.

        Declarative tools know their complete path set before execution.  Copying
        the entire workspace for a one-file edit is both wasteful and makes an
        unrelated large or special file block the operation.  This mode builds a
        sparse private tree through fd-anchored, no-follow reads.  Only mutation
        paths and their necessary ancestors participate in the baseline and diff;
        read-only inputs are copied for deterministic processing but can never be
        published by ``commit``.

        ``prepare`` remains the full-workspace mode for sandboxed Bash/Python,
        whose output paths cannot be known in advance.
        """

        _require_guarded_workspace_mutation_support()
        if self.transaction_root is not None:
            raise WorkspaceMutationError("Workspace transaction is already prepared")
        mutation_relatives = _normalize_targeted_paths(self.workspace, mutation_paths)
        if not mutation_relatives:
            raise WorkspaceMutationError(
                "Targeted workspace transaction requires at least one mutation path"
            )
        read_relatives = _normalize_targeted_paths(self.workspace, read_paths)
        if sys.platform == "win32":
            _reject_windows_aliases((*mutation_relatives, *read_relatives))
        mutation_set = frozenset(mutation_relatives)
        read_set = frozenset(read_relatives) - mutation_set
        mutation_scope = _targeted_scope_paths(mutation_set)
        copy_scope = _targeted_scope_paths(mutation_set | read_set)
        _reject_targeted_prefix_conflicts(mutation_set | read_set)
        if len(copy_scope) > MAX_STAGED_ENTRIES:
            raise WorkspaceMutationError(
                f"Targeted workspace transaction exceeds {MAX_STAGED_ENTRIES} entries"
            )

        _validate_internal_sandbox_path(self.workspace)
        root_info = self.workspace.stat(follow_symlinks=False)
        if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
            raise WorkspaceMutationError("Workspace root is redirected")
        identity = self._ensure_workspace_identity()
        if identity.volatile_identity != _path_identity(
            self.workspace,
            directory=True,
        ):
            raise WorkspaceMutationError(
                "Workspace root changed while its durable identity was prepared"
            )
        if sys.platform == "win32":
            _preflight_windows_targeted_parents(
                self.workspace,
                mutation_set | read_set,
                expected_workspace_identity=self._workspace_identity,
            )

        workspace_root = self.storage_root / self._workspace_key
        _ensure_private_directory(self.storage_root)
        _ensure_private_directory(workspace_root)
        transaction_root = Path(tempfile.mkdtemp(prefix="tx-", dir=workspace_root))
        os.chmod(transaction_root, 0o700)
        self.transaction_root = transaction_root
        staged = transaction_root / "workspace"
        # Build with owner-write permission first.  Restoring a selected 0555
        # directory before its descendants are copied would make construction
        # of the private sparse view fail even though the snapshot is readable.
        staged.mkdir(mode=0o700)

        source_entries: dict[str, WorkspaceEntry] = {}
        linked_mutation_paths: set[str] = set()
        source_identities: dict[str, tuple[int, int] | None] = {}
        total_bytes = 0
        terminals = mutation_set | read_set
        directory_modes: dict[str, int] = {}
        try:
            with _open_workspace_root_fd(
                self.workspace,
                expected_identity=self._workspace_identity,
            ) as workspace_fd:
                for relative in copy_scope:
                    entry = _read_entry_at_relative(workspace_fd, relative)
                    source_identities[relative] = _read_identity_at_relative(
                        workspace_fd,
                        relative,
                    )
                    if entry is None:
                        continue
                    source_entries[relative] = entry
                    destination = staged / relative
                    if entry.kind == "directory":
                        if relative in terminals:
                            raise WorkspaceMutationError(
                                f"Targeted workspace terminal is a directory: {relative}"
                            )
                        destination.mkdir(mode=0o700, exist_ok=True)
                        directory_modes[relative] = entry.mode
                        continue
                    if relative not in terminals:
                        raise WorkspaceMutationError(
                            f"Targeted workspace parent is not a directory: {relative}"
                        )
                    if entry.kind == "symlink":
                        raise WorkspaceMutationError(
                            f"Targeted workspace path is a symbolic link: {relative}"
                        )
                    if entry.size > MAX_STAGED_FILE_BYTES:
                        raise WorkspaceMutationError(
                            f"Workspace file exceeds the transaction limit: {relative}"
                        )
                    copied_entry, link_count = _copy_regular_file_at_relative(
                        workspace_fd,
                        relative,
                        destination,
                    )
                    if copied_entry != entry:
                        raise WorkspaceMutationError(
                            f"Workspace path changed while targeted staging was prepared: {relative}"
                        )
                    total_bytes += copied_entry.size
                    if total_bytes > MAX_STAGED_WORKSPACE_BYTES:
                        raise WorkspaceMutationError(
                            "Targeted workspace inputs exceed the 512 MiB transaction limit"
                        )
                    if relative in mutation_set and link_count > 1:
                        linked_mutation_paths.add(relative)
                # Validate the exact logical snapshot after all reads and inode
                # captures, then restore directory modes only after construction.
                for relative in copy_scope:
                    if (
                        _read_entry_at_relative(workspace_fd, relative)
                        != source_entries.get(relative)
                        or _read_identity_at_relative(workspace_fd, relative)
                        != source_identities[relative]
                    ):
                        raise WorkspaceMutationError(
                            "Workspace changed while its targeted isolated view "
                            f"was being prepared: {relative}"
                        )
                for relative, mode in sorted(
                    directory_modes.items(),
                    key=lambda item: (-item[0].count("/"), item[0]),
                ):
                    os.chmod(staged / relative, mode)
                os.chmod(staged, stat.S_IMODE(root_info.st_mode))
                self._assert_durable_workspace_identity()
        except Exception:
            shutil.rmtree(transaction_root, ignore_errors=True)
            if self.transaction_root == transaction_root:
                self.transaction_root = None
            self._workspace_identity = None
            self._workspace_durable_token = None
            raise

        if self._finished:
            shutil.rmtree(transaction_root, ignore_errors=True)
            self._workspace_identity = None
            self._workspace_durable_token = None
            raise WorkspaceMutationError("Workspace transaction was cancelled during preparation")
        self.staged_workspace = staged
        # The baseline is the complete sparse view, not a recursive view of the
        # real workspace.  Thus read inputs are recognized as unchanged while
        # unrelated real siblings are never inferred as deletions.
        self._baseline = source_entries
        self._baseline_hardlinks = {}
        self._baseline_linked_paths = frozenset(linked_mutation_paths)
        self._targeted_scope_paths = mutation_scope
        self._targeted_mutation_paths = mutation_set
        self._targeted_read_paths = read_set
        self._targeted_declared_baseline = {
            relative: source_entries.get(relative) for relative in copy_scope
        }
        self._targeted_source_identities = source_identities
        return staged

    def create_scratch(self, *, prefix: str) -> tuple[Path, Path]:
        """Create scratch in staging and return ``(host, logical)`` paths."""

        staged = self._require_stage()
        sandbox_root = staged / _INTERNAL_ROOT / "sandbox"
        sandbox_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        if sandbox_root.is_symlink() or not sandbox_root.is_dir():
            raise WorkspaceMutationError("Staged sandbox scratch path is redirected")
        name = f"{_safe_prefix(prefix)}{secrets.token_hex(12)}"
        host_path = sandbox_root / name
        host_path.mkdir(mode=0o700)
        logical_path = self.workspace / host_path.relative_to(staged)
        return host_path, logical_path

    def staged_path(self, logical_path: str | os.PathLike[str]) -> Path:
        """Map one canonical logical workspace path into the private copy."""

        staged = self._require_stage()
        logical = Path(logical_path)
        if not logical.is_absolute():
            logical = self.workspace / logical
        try:
            relative = logical.relative_to(self.workspace)
        except ValueError as exc:
            raise WorkspaceMutationError(
                f"Execution path is outside the workspace: {logical}"
            ) from exc
        return staged / relative

    def arm_office_precommit_validation(
        self,
        logical_target: str | os.PathLike[str],
    ) -> WorkspaceOfficePrecommitView:
        """Serialize arming with reset and commit for this workspace."""

        with self._office_state_lock:
            return self._arm_office_precommit_validation(logical_target)

    def _arm_office_precommit_validation(
        self,
        logical_target: str | os.PathLike[str],
    ) -> WorkspaceOfficePrecommitView:
        """Bind the prepared single-file target to the sealed commit path.

        Once armed, ``commit()`` is mechanically unavailable.  The transaction
        can only be published through ``commit_with_precommit_office_seal()`` or
        discarded with ``abort()``.
        """

        if self._finished:
            raise WorkspacePrecommitSealError(
                "Workspace transaction is already finished"
            )
        staged = self._require_stage()
        baseline = self._require_baseline()
        if self._targeted_mutation_paths is None:
            raise WorkspacePrecommitSealError(
                "Office precommit validation requires prepare_paths()"
            )
        relative = _normalize_targeted_paths(
            self.workspace,
            (logical_target,),
        )[0]
        if self._targeted_mutation_paths != frozenset({relative}):
            raise WorkspacePrecommitSealError(
                "Office precommit validation requires exactly one declared write"
            )
        if self._office_precommit_relative is not None:
            raise WorkspacePrecommitSealError(
                "Office precommit validation is already armed"
            )
        workspace_identity = self._workspace_identity
        identities = self._targeted_source_identities
        if workspace_identity is None or identities is None or relative not in identities:
            raise WorkspacePrecommitSealError(
                "Office precommit transaction identities are unavailable"
            )
        staged_target = staged / relative
        staged_root_identity = _path_identity(staged, directory=True)
        validation_generation = secrets.token_hex(32)
        self._office_precommit_relative = relative
        self._office_precommit_root_identity = staged_root_identity
        self._office_precommit_generation = validation_generation
        return WorkspaceOfficePrecommitView(
            workspace_root=self.workspace,
            visible_target=self.workspace / relative,
            staged_root=staged,
            staged_target=staged_target,
            relative_path=relative,
            workspace_identity=workspace_identity,
            staged_root_identity=staged_root_identity,
            validation_generation=validation_generation,
            baseline=baseline.get(relative),
            baseline_identity=identities[relative],
            operation=self.operation,
            session_id=self.ctx.session_id,
            message_id=self.ctx.message_id,
            call_id=self.ctx.call_id,
            root_turn_id=self.ctx.root_turn_id,
            turn_run_id=self.ctx.turn_run_id,
            checkpoint_id=self.ctx.checkpoint_id,
            workspace_instance_id=self.ctx.workspace_instance_id,
        )

    def reset_office_precommit_target(
        self,
        logical_target: str | os.PathLike[str],
    ) -> WorkspaceOfficePrecommitView:
        """Serialize candidate reset with arming and visible commit."""

        with self._office_state_lock:
            return self._reset_office_precommit_target(logical_target)

    def _reset_office_precommit_target(
        self,
        logical_target: str | os.PathLike[str],
    ) -> WorkspaceOfficePrecommitView:
        """Reset one candidate and return its newly authorized validation view.

        Create candidates are removed.  Edit candidates are replaced with a new
        private copy only while the visible baseline still has the exact entry,
        inode identity, and SHA-256 captured by ``prepare_paths()``.  The armed
        relative path, staged root, and staged-root identity are preserved.  A
        fresh transaction-owned validation generation is returned, so neither
        inode reuse nor identical candidate bytes can revive an earlier seal.
        """

        if self._finished:
            raise WorkspacePrecommitSealError(
                "Workspace transaction is already finished"
            )
        staged = self._require_stage()
        baseline = self._require_baseline()
        relative = _normalize_targeted_paths(
            self.workspace,
            (logical_target,),
        )[0]
        expected_root_identity = self._office_precommit_root_identity
        if (
            self._office_precommit_relative != relative
            or expected_root_identity is None
            or self._office_precommit_generation is None
        ):
            raise WorkspacePrecommitSealError(
                "Office precommit target is not armed for reset"
            )
        if _path_identity(staged, directory=True) != expected_root_identity:
            raise WorkspacePrecommitSealError(
                "Office precommit staged root changed before reset"
            )
        if self.operation not in {"office.create", "office.edit"}:
            raise WorkspacePrecommitSealError(
                "Office precommit reset operation is invalid"
            )

        # Reserve the next capability before changing the candidate, then
        # publish it only after the reset has completed.  This prevents an
        # old seal from becoming current again if a filesystem immediately
        # reuses the removed candidate's inode for identical replacement
        # bytes (a normal possibility on Linux).
        next_validation_generation = secrets.token_hex(32)
        self._office_precommit_generation = None

        visible_identity = self._workspace_identity
        source_identities = self._targeted_source_identities
        declared_baseline = self._targeted_declared_baseline
        if (
            visible_identity is None
            or source_identities is None
            or declared_baseline is None
            or relative not in source_identities
            or relative not in declared_baseline
        ):
            raise WorkspacePrecommitSealError(
                "Office precommit reset identities are unavailable"
            )

        expected_visible = declared_baseline[relative]
        expected_visible_identity = source_identities[relative]
        if self.operation == "office.edit":
            if (
                expected_visible is None
                or expected_visible.kind != "file"
                or expected_visible.sha256 is None
                or expected_visible_identity is None
                or baseline.get(relative) != expected_visible
            ):
                raise WorkspacePrecommitSealError(
                    "Office edit baseline is unavailable for reset"
                )
            with _open_workspace_root_fd(
                self.workspace,
                expected_identity=visible_identity,
            ) as workspace_fd:
                self._assert_office_reset_visible_baseline(
                    workspace_fd,
                    relative=relative,
                    expected=expected_visible,
                    expected_identity=expected_visible_identity,
                )
                self._remove_office_staged_candidate(
                    staged,
                    relative=relative,
                    expected_root_identity=expected_root_identity,
                )
                copied, link_count = _copy_regular_file_at_relative(
                    workspace_fd,
                    relative,
                    staged / relative,
                )
                if copied != expected_visible or link_count > 1:
                    raise WorkspacePrecommitSealError(
                        "Office edit baseline changed while it was reset"
                    )
                self._assert_office_reset_visible_baseline(
                    workspace_fd,
                    relative=relative,
                    expected=expected_visible,
                    expected_identity=expected_visible_identity,
                )
        else:
            self._remove_office_staged_candidate(
                staged,
                relative=relative,
                expected_root_identity=expected_root_identity,
            )

        if (
            self._office_precommit_relative != relative
            or self._office_precommit_root_identity != expected_root_identity
            or _path_identity(staged, directory=True) != expected_root_identity
        ):
            raise WorkspacePrecommitSealError(
                "Office precommit transaction identity changed during reset"
            )

        self._office_precommit_generation = next_validation_generation
        return WorkspaceOfficePrecommitView(
            workspace_root=self.workspace,
            visible_target=self.workspace / relative,
            staged_root=staged,
            staged_target=staged / relative,
            relative_path=relative,
            workspace_identity=visible_identity,
            staged_root_identity=expected_root_identity,
            validation_generation=next_validation_generation,
            baseline=baseline.get(relative),
            baseline_identity=source_identities[relative],
            operation=self.operation,
            session_id=self.ctx.session_id,
            message_id=self.ctx.message_id,
            call_id=self.ctx.call_id,
            root_turn_id=self.ctx.root_turn_id,
            turn_run_id=self.ctx.turn_run_id,
            checkpoint_id=self.ctx.checkpoint_id,
            workspace_instance_id=self.ctx.workspace_instance_id,
        )

    def _assert_office_reset_visible_baseline(
        self,
        workspace_fd: int,
        *,
        relative: str,
        expected: WorkspaceEntry,
        expected_identity: tuple[int, int],
    ) -> None:
        if (
            _read_entry_at_relative(workspace_fd, relative) != expected
            or _read_identity_at_relative(workspace_fd, relative)
            != expected_identity
        ):
            raise WorkspacePrecommitSealError(
                "Office edit baseline changed before candidate reset"
            )

    def _remove_office_staged_candidate(
        self,
        staged: Path,
        *,
        relative: str,
        expected_root_identity: tuple[int, int],
    ) -> None:
        with _open_workspace_root_fd(
            staged,
            expected_identity=expected_root_identity,
        ) as staged_fd:
            current = _read_entry_at_relative(staged_fd, relative)
            if current is None or current.kind != "file":
                raise WorkspacePrecommitSealError(
                    "Office staged candidate is unavailable for reset"
                )
            captured = _rename_to_backup_at(staged_fd, relative)
            try:
                _remove_owned_temporary_if_matches_at(
                    staged_fd,
                    captured,
                    acceptable={current},
                )
            except WorkspaceMutationError as exc:
                raise WorkspacePrecommitSealError(
                    "Office staged candidate changed during reset"
                ) from exc

    def collect_changes(self) -> WorkspaceChangeSet:
        staged = self._require_stage()
        baseline = self._require_baseline()
        current = _scan_workspace(staged)
        baseline_hardlinks = self._baseline_hardlinks or {}
        current_hardlinks = _scan_hardlink_groups(staged)
        if current_hardlinks != baseline_hardlinks:
            raise WorkspaceMutationError(
                "Command changed hard-link topology; the transaction was discarded"
            )
        writes: list[str] = []
        deletes: list[str] = []
        created_directories: list[str] = []
        deleted_directories: list[str] = []
        baseline_nonempty_directories: set[str] = set()
        for baseline_relative in baseline:
            for parent in Path(baseline_relative).parents:
                if parent == Path("."):
                    break
                baseline_nonempty_directories.add(parent.as_posix())

        for relative in sorted(set(baseline) | set(current)):
            before = baseline.get(relative)
            after = current.get(relative)
            if before == after:
                continue
            if self._targeted_mutation_paths is not None:
                if after is not None and after.kind == "directory":
                    allowed_ancestor = any(
                        mutation.startswith(relative + "/")
                        for mutation in self._targeted_mutation_paths
                    )
                    if before is None and allowed_ancestor:
                        created_directories.append(relative)
                        continue
                    raise WorkspaceMutationError(
                        "Targeted transaction changed an undeclared directory: "
                        f"{relative}"
                    )
                if before is not None and before.kind == "directory":
                    raise WorkspaceMutationError(
                        "Targeted transaction changed a selected ancestor directory: "
                        f"{relative}"
                    )
                if relative not in self._targeted_mutation_paths:
                    raise WorkspaceMutationError(
                        f"Targeted transaction changed undeclared path: {relative}"
                    )
                if after is not None and after.kind == "symlink":
                    raise WorkspaceMutationError(
                        f"Targeted transaction created a symbolic link: {relative}"
                    )
            if relative in (self._baseline_linked_paths or frozenset()):
                raise WorkspaceMutationError(
                    f"Command changed hard-linked path {relative}; the transaction was discarded"
                )
            if before is not None and after is not None and before.kind != after.kind:
                raise WorkspaceMutationError(
                    f"Command changed the filesystem type of {relative}; the transaction was discarded"
                )
            if before is not None and before.kind == "symlink":
                if not (
                    self.checkpoint_action == "rewind"
                    and after is None
                    and os.name == "posix"
                ):
                    raise WorkspaceMutationError(
                        "Only rewind may delete an existing symbolic link; "
                        f"the transaction was discarded: {relative}"
                    )
            if after is not None and after.kind == "directory":
                if before is None:
                    created_directories.append(relative)
                else:
                    raise WorkspaceMutationError(
                        f"Command changed directory metadata at {relative}; the transaction was discarded"
                    )
                continue
            if before is not None and before.kind == "directory" and after is None:
                if relative in baseline_nonempty_directories:
                    raise WorkspaceMutationError(
                        "Command deleted a non-empty baseline directory; v1 safely "
                        f"rejects recursive directory deletion: {relative}"
                    )
                deleted_directories.append(relative)
                continue
            if after is None:
                deletes.append(relative)
                continue
            if after.kind == "symlink":
                _validate_new_symlink(self.workspace, relative, after)
            writes.append(relative)

        return WorkspaceChangeSet(
            writes=tuple(writes),
            deletes=tuple(deletes),
            created_directories=tuple(
                sorted(created_directories, key=lambda value: (value.count("/"), value))
            ),
            deleted_directories=tuple(
                sorted(deleted_directories, key=lambda value: (-value.count("/"), value))
            ),
        )

    def commit(self) -> WorkspaceCommitResult:
        """Version displaced files and atomically install the staged changes."""

        if self._office_precommit_relative is not None:
            raise WorkspacePrecommitSealError(
                "An armed Office transaction requires its precommit seal"
            )
        return self._commit(office_seal=None)

    def commit_with_precommit_office_seal(
        self,
        seal: object,
    ) -> WorkspaceCommitResult:
        """Commit exactly one authoritative, server-validated Office output.

        ``seal`` is an in-process capability returned by the Office draft
        validator.  It is intentionally not accepted by tool schemas and is
        never copied into public commit metadata.  Any failure discards the
        private transaction unless existing recovery rules require retaining
        sidecars after an ambiguous platform operation.
        """

        try:
            office_seal = _require_office_precommit_seal(seal)
            if (
                self._office_precommit_relative is not None
                and office_seal.relative_path != self._office_precommit_relative
            ):
                raise WorkspacePrecommitSealError(
                    "Office precommit seal targets a different armed path"
                )
            return self._commit(office_seal=office_seal)
        except Exception:
            if not self._finished:
                self.abort()
            raise

    def _commit(
        self,
        *,
        office_seal: OfficeDraftSeal | None,
    ) -> WorkspaceCommitResult:
        _require_guarded_workspace_mutation_support()
        with self._office_state_lock:
            # ``commit()`` performs the same check as an early fast-fail, but
            # arming may race between that observation and acquisition of this
            # transaction-local lock.  Recheck at the serialized boundary so
            # a plain commit can never publish an armed Office candidate.
            if office_seal is None and self._office_precommit_relative is not None:
                raise WorkspacePrecommitSealError(
                    "An armed Office transaction requires its precommit seal"
                )
            with self._commit_lock:
                with _open_workspace_root_fd(
                    self.workspace,
                    expected_identity=self._workspace_identity,
                ) as workspace_fd:
                    self._assert_durable_workspace_identity()
                    return self._commit_with_fd(
                        workspace_fd,
                        office_seal=office_seal,
                    )

    def _commit_with_fd(
        self,
        workspace_fd: int,
        *,
        office_seal: OfficeDraftSeal | None,
    ) -> WorkspaceCommitResult:
        """Commit while holding the process lock and an immutable root handle."""

        if self._finished:
            raise WorkspaceMutationError("Workspace transaction is already finished")
        staged = self._require_stage()
        baseline = self._require_baseline()
        changes = self.collect_changes()
        office_guard = (
            self._bind_office_precommit_guard(
                seal=office_seal,
                staged=staged,
                baseline=baseline,
                changes=changes,
            )
            if office_seal is not None
            else None
        )
        if sys.platform == "win32" and (
            changes.created_directories or changes.deleted_directories
        ):
            raise WorkspaceMutationError(
                "Windows declarative transactions require existing parent "
                "directories and do not create or delete directories; file "
                "writes and file deletions remain supported."
            )
        if sys.platform == "win32" and len(
            (*changes.writes, *changes.deletes)
        ) > 1:
            raise WorkspaceMutationError(
                "Windows declarative transactions currently commit exactly one "
                "file at a time; multi-file commit remains unavailable until all "
                "destination-parent handles can be pinned for the full transaction."
            )
        self._assert_targeted_declared_snapshot(workspace_fd)
        touched_existing = [
            relative
            for relative in (*changes.writes, *changes.deletes)
            if relative in baseline
        ]
        versioned_existing = [
            relative
            for relative in touched_existing
            if baseline[relative].kind == "file"
        ]
        rewind_symlink_deletes = [
            relative
            for relative in changes.deletes
            if relative in baseline and baseline[relative].kind == "symlink"
        ]
        if any(
            baseline[relative].kind not in {"file", "symlink"}
            for relative in touched_existing
        ):
            raise WorkspaceMutationError(
                "Transaction touched an unsupported existing filesystem type"
            )
        if rewind_symlink_deletes and (
            self.checkpoint_action != "rewind" or os.name != "posix"
        ):
            raise WorkspaceMutationError(
                "Existing symbolic-link deletion is restricted to POSIX rewind"
            )
        for relative in (*touched_existing, *changes.deleted_directories):
            current = _read_entry_at_relative(workspace_fd, relative)
            if current != baseline.get(relative):
                raise WorkspaceMutationError(
                    f"Workspace path changed outside the command transaction: {relative}"
                )
        for relative in (
            value for value in changes.writes if value not in baseline
        ):
            if _read_entry_at_relative(workspace_fd, relative) is not None:
                raise WorkspaceMutationError(
                    f"Workspace path was created outside the command transaction: {relative}"
                )

        _assert_workspace_path_identity(self.workspace, self._workspace_identity)
        store = FileVersionStore(
            self.workspace,
            expected_workspace_identity=self._workspace_identity,
            expected_durable_workspace_identity=self._workspace_durable_token,
        )
        try:
            versions = store.capture_batch_before_mutation(
                [str(self.workspace / relative) for relative in versioned_existing],
                operation=self.operation,
                session_id=self.ctx.session_id,
                message_id=self.ctx.message_id,
                call_id=self.ctx.call_id,
            )
        except FileVersionError as exc:
            raise WorkspaceMutationError(str(exc)) from exc
        _assert_workspace_path_identity(self.workspace, self._workspace_identity)
        self._assert_durable_workspace_identity()
        version_by_path = {version.relative_path: version for version in versions}
        if (
            len(versions) != len(versioned_existing)
            or len(version_by_path) != len(versioned_existing)
            or set(version_by_path) != set(versioned_existing)
        ):
            raise WorkspaceMutationError(
                "File-version capture did not return the complete command mutation batch"
            )
        for relative in versioned_existing:
            entry = baseline[relative]
            version = version_by_path[relative]
            if (
                entry.kind != "file"
                or version.relative_path != relative
                or version.sha256 != entry.sha256
                or version.size != entry.size
                or version.original_mode != entry.mode
            ):
                raise WorkspaceMutationError(
                    f"File-version snapshot does not match transaction baseline: {relative}"
                )
        for relative in rewind_symlink_deletes:
            if _relative_nlink(workspace_fd, relative) != 1:
                raise WorkspaceMutationError(
                    "Rewind symbolic-link target gained a hard link: "
                    f"{relative}"
                )

        created_directories: dict[str, tuple[int, int]] = {}
        prepared: dict[str, _PreparedPath] = {}
        prepared_cleanup_entries: dict[str, WorkspaceEntry] = {}
        deleted_backups: dict[str, _PreparedPath] = {}
        applied_writes: list[str] = []
        attempted_writes: set[str] = set()
        applied_deletes: list[str] = []
        removed_directories: list[str] = []
        recovery_sidecars: list[_PreparedPath] = []
        preinstall_cleanup_error: WorkspaceMutationError | None = None
        final_entries: dict[str, WorkspaceEntry] = {}
        temporary_names = {
            relative: _temporary_name(Path(relative).name)
            for relative in (*changes.writes, *changes.deletes)
        }
        try:
            final_entries = _scan_workspace(staged)
            if office_guard is not None:
                self._assert_office_precommit_layout(
                    guard=office_guard,
                    changes=changes,
                    observed=final_entries,
                )
                self._assert_office_precommit_source(
                    staged=staged,
                    guard=office_guard,
                    observed=final_entries.get(office_guard.relative),
                )
            if changes.changed_paths:
                self._write_prepared_journal(
                    changes=changes,
                    versions=versions,
                    final_entries=final_entries,
                    temporary_names=temporary_names,
                )
            for relative in changes.created_directories:
                previously_created = set(created_directories)
                if self._targeted_mutation_paths is None:
                    _create_directory_at(
                        workspace_fd,
                        relative,
                        baseline_mode=final_entries[relative].mode,
                        created=created_directories,
                    )
                else:
                    _create_targeted_directory_at(
                        workspace_fd,
                        relative,
                        mode=final_entries[relative].mode,
                        created=created_directories,
                    )
                newly_created = {
                    path: created_directories[path]
                    for path in created_directories.keys() - previously_created
                }
                if newly_created:
                    self._write_created_directory_proofs(
                        workspace_fd=workspace_fd,
                        identities=newly_created,
                        final_entries=final_entries,
                    )

            for relative in changes.writes:
                entry = final_entries[relative]
                if entry.kind == "file":
                    prepared[relative] = _prepare_regular_replacement_at(
                        workspace_fd,
                        staged / relative,
                        relative,
                        entry.mode,
                        temporary_name=temporary_names[relative],
                    )
                elif entry.kind == "symlink":
                    prepared[relative] = _prepare_symlink_replacement_at(
                        workspace_fd,
                        relative,
                        entry.link_target or "",
                        temporary_name=temporary_names[relative],
                    )
                else:  # pragma: no cover - guarded by collect_changes
                    raise WorkspaceMutationError(f"Unsupported staged entry: {relative}")

                observed_prepared = _read_prepared_entry(
                    workspace_fd,
                    prepared[relative],
                )
                if observed_prepared is None:
                    raise WorkspaceMutationError(
                        f"Prepared transaction output disappeared: {relative}"
                    )
                prepared_cleanup_entries[relative] = observed_prepared

            if office_guard is not None:
                # Revalidate the private source after the copy, then validate
                # the exact hidden replacement that will be exchanged.  This
                # is deliberately the final step before any visible install.
                self._assert_office_precommit_source(
                    staged=staged,
                    guard=office_guard,
                )
                temporary = prepared[office_guard.relative]
                observed_prepared = _read_prepared_entry(workspace_fd, temporary)
                if (
                    observed_prepared != office_guard.expected
                    or _prepared_nlink(workspace_fd, temporary) != 1
                ):
                    if observed_prepared is not None:
                        prepared_cleanup_entries[office_guard.relative] = (
                            observed_prepared
                        )
                    raise WorkspacePrecommitSealError(
                        "Prepared Office output no longer matches its precommit "
                        f"seal: {office_guard.relative}"
                    )

            for relative in changes.writes:
                temporary = prepared[relative]
                # Once an install has been attempted, an exception cannot in
                # general prove whether the rename linearized.  Its temporary
                # must therefore be treated as potentially published.
                attempted_writes.add(relative)
                if relative in baseline:
                    try:
                        _exchange_prepared_at(workspace_fd, temporary)
                    except _WindowsExchangeAmbiguous:
                        self._preserve_for_recovery = True
                        if temporary.current_sidecar_name is not None:
                            recovery_sidecars.append(temporary)
                        raise
                    except _WindowsExchangeRecovered:
                        if temporary.current_sidecar_name is not None:
                            recovery_sidecars.append(temporary)
                        raise
                    except _WorkspaceParentMovedAfterOperation:
                        applied_writes.append(relative)
                        recovery_sidecars.append(temporary)
                        raise
                    # The exchange captures the exact object that occupied the
                    # destination at the linearization point. Validate that
                    # displaced object rather than trusting an earlier stat.
                    applied_writes.append(relative)
                    recovery_sidecars.append(temporary)
                    displaced = _read_prepared_entry(workspace_fd, temporary)
                    if (
                        displaced != baseline[relative]
                        or _prepared_nlink(workspace_fd, temporary) != 1
                    ):
                        raise WorkspaceMutationError(
                            "Workspace path changed or gained a hard link during "
                            f"command commit: {relative}"
                        )
                else:
                    try:
                        _link_prepared_new_at(workspace_fd, temporary)
                    except _WorkspaceParentMovedAfterOperation:
                        applied_writes.append(relative)
                        raise
                    applied_writes.append(relative)
                _fsync_parent_at(workspace_fd, relative)

            for relative in changes.deletes:
                try:
                    backup = _rename_to_backup_at(
                        workspace_fd,
                        relative,
                        temporary_name=temporary_names[relative],
                    )
                except _WorkspaceParentMovedAfterOperation:
                    backup = _PreparedPath(
                        relative=relative,
                        temporary_name=temporary_names[relative],
                    )
                    deleted_backups[relative] = backup
                    applied_deletes.append(relative)
                    recovery_sidecars.append(backup)
                    raise
                deleted_backups[relative] = backup
                applied_deletes.append(relative)
                recovery_sidecars.append(backup)
                if (
                    _read_prepared_entry(workspace_fd, backup) != baseline[relative]
                    or _prepared_nlink(workspace_fd, backup) != 1
                ):
                    raise WorkspaceMutationError(
                        "Workspace path changed or gained a hard link during "
                        f"command commit: {relative}"
                    )
                _fsync_parent_at(workspace_fd, relative)

            for relative in changes.deleted_directories:
                try:
                    _remove_directory_at(workspace_fd, relative)
                except _WorkspaceParentMovedAfterOperation:
                    removed_directories.append(relative)
                    raise
                _fsync_parent_at(workspace_fd, relative)
                removed_directories.append(relative)
            if changes.changed_paths:
                for relative in changes.writes:
                    if (
                        _read_entry_at_relative(workspace_fd, relative)
                        != final_entries[relative]
                        or _relative_nlink(workspace_fd, relative) != 1
                    ):
                        raise WorkspaceMutationError(
                            "Workspace output changed or gained a hard link during "
                            f"command commit: {relative}"
                        )
                for relative in changes.deletes:
                    if _read_entry_at_relative(workspace_fd, relative) is not None:
                        raise WorkspaceMutationError(
                            f"Deleted workspace output was recreated during commit: {relative}"
                        )
                for relative in changes.deleted_directories:
                    if _read_entry_at_relative(workspace_fd, relative) is not None:
                        raise WorkspaceMutationError(
                            "Deleted workspace directory was recreated during commit: "
                            f"{relative}"
                        )
                self._assert_targeted_read_dependencies(workspace_fd)
                _assert_workspace_path_identity(self.workspace, self._workspace_identity)
                self._assert_durable_workspace_identity()
                self._write_journal_state("committed")
                _assert_workspace_path_identity(self.workspace, self._workspace_identity)
                self._assert_durable_workspace_identity()
        except Exception as exc:
            if not attempted_writes and prepared and not self._preserve_for_recovery:
                # A sealed create may have provisionally created missing
                # ancestors so the hidden same-directory replacement can be
                # prepared.  Remove every known-never-installed replacement
                # before rollback tries to remove those directories.
                try:
                    for relative, temporary in tuple(prepared.items()):
                        expected = final_entries[relative]
                        _remove_owned_temporary_if_matches_at(
                            workspace_fd,
                            temporary,
                            acceptable={
                                expected,
                                prepared_cleanup_entries.get(relative, expected),
                            },
                        )
                    prepared.clear()
                except WorkspaceMutationError as cleanup_exc:
                    preinstall_cleanup_error = cleanup_exc
                    self._preserve_for_recovery = True
            rollback_error = _rollback_commit(
                workspace=self.workspace,
                workspace_fd=workspace_fd,
                store=store,
                version_by_path=version_by_path,
                prepared=prepared,
                deleted_backups=deleted_backups,
                applied_writes=applied_writes,
                applied_deletes=applied_deletes,
                removed_directories=removed_directories,
                created_directories=created_directories,
                baseline=baseline,
                final_entries=final_entries,
                recovery_sidecars=recovery_sidecars,
            )
            sidecar_suffix = _recovery_sidecar_error_suffix(
                self.workspace,
                recovery_sidecars,
            )
            if rollback_error is not None:
                self._preserve_for_recovery = True
                raise WorkspaceMutationError(
                    "Workspace commit failed "
                    f"({exc}) and rollback failed ({rollback_error}){sidecar_suffix}"
                ) from exc
            if preinstall_cleanup_error is not None:
                raise WorkspaceMutationError(
                    "Workspace precommit cleanup failed; recovery journal and "
                    f"hidden output were preserved: {preinstall_cleanup_error}"
                ) from exc
            if self._preserve_for_recovery:
                raise WorkspaceMutationError(
                    "Workspace commit entered an ambiguous Windows replacement "
                    f"state; recovery journal and sidecars were preserved: {exc}"
                    f"{sidecar_suffix}"
                ) from exc
            if isinstance(exc, WorkspacePrecommitSealError):
                # No visible install has been attempted on the sealed path.
                # Preserve the specific fail-closed reason so the server-only
                # entry point can atomically discard its private transaction.
                raise exc
            raise WorkspaceMutationError(
                "Workspace commit failed; all applied file changes were rolled back: "
                f"{exc}{sidecar_suffix}"
            ) from exc
        finally:
            for relative, temporary in prepared.items():
                # Only a temporary whose install was never attempted is known
                # never to have occupied the visible target name.  Published
                # or possibly-published inodes remain addressable sidecars so
                # writes through an older open descriptor cannot be lost.
                if relative in attempted_writes:
                    continue
                if not self._preserve_for_recovery:
                    try:
                        _remove_owned_temporary_if_matches_at(
                            workspace_fd,
                            temporary,
                            acceptable={
                                final_entries[temporary.relative],
                                prepared_cleanup_entries.get(
                                    temporary.relative,
                                    final_entries[temporary.relative],
                                ),
                            },
                        )
                    except WorkspaceMutationError:
                        self._preserve_for_recovery = True
                        raise

        self._finished = True
        mutations = _workspace_mutation_records(
            changes=changes,
            baseline=baseline,
            final_entries=final_entries,
            version_by_path=version_by_path,
        )
        checkpoint_journal_token = (
            self._checkpoint_journal_token()
            if mutations and self.ctx.checkpoint_id is not None
            else None
        )
        result = WorkspaceCommitResult(
            written_files=tuple(str(self.workspace / value) for value in changes.writes),
            deleted_files=tuple(str(self.workspace / value) for value in changes.deletes),
            previous_version_ids=tuple(version.id for version in versions),
            recovery_sidecars=tuple(
                dict.fromkeys(
                    _recovery_sidecar_path(self.workspace, value)
                    for value in recovery_sidecars
                )
            ),
            mutations=mutations,
            checkpoint_journal_token=checkpoint_journal_token,
        )
        if checkpoint_journal_token is not None:
            self._retain_committed_checkpoint_journal()
        else:
            self.abort()
        return result

    def _bind_office_precommit_guard(
        self,
        *,
        seal: OfficeDraftSeal,
        staged: Path,
        baseline: dict[str, WorkspaceEntry],
        changes: WorkspaceChangeSet,
    ) -> _OfficePrecommitGuard:
        """Bind validation evidence to this exact targeted transaction."""

        if self._targeted_mutation_paths is None:
            raise WorkspacePrecommitSealError(
                "Office precommit seals require targeted transaction staging"
            )
        if seal.quality != "authoritative":
            raise WorkspacePrecommitSealError(
                "Office precommit seal is not authoritative"
            )
        if (
            changes.writes != (seal.relative_path,)
            or changes.deletes
            or changes.deleted_directories
        ):
            raise WorkspacePrecommitSealError(
                "Office precommit seal must cover the transaction's exact file write"
            )
        if seal.relative_path not in self._targeted_mutation_paths:
            raise WorkspacePrecommitSealError(
                "Office precommit seal is outside the declared mutation set"
            )
        armed_relative = self._office_precommit_relative
        armed_root_identity = self._office_precommit_root_identity
        validation_generation = self._office_precommit_generation
        if (
            armed_relative is None
            or armed_root_identity is None
        ):
            raise WorkspacePrecommitSealError(
                "Office precommit transaction is not armed"
            )
        if validation_generation is None:
            raise WorkspacePrecommitSealError(
                "Office precommit validation generation is retired"
            )
        if (
            seal.relative_path != armed_relative
            or seal.root_identity != armed_root_identity
        ):
            raise WorkspacePrecommitSealError(
                "Office precommit seal targets a different armed view"
            )
        if (
            seal.validation_generation != validation_generation
        ):
            raise WorkspacePrecommitSealError(
                "Office precommit seal validation generation is stale"
            )
        if self._workspace_identity is None:
            raise WorkspacePrecommitSealError(
                "Office precommit transaction identity is unavailable"
            )
        expected_created = _office_sealed_create_ancestors(
            seal.relative_path,
            baseline=baseline,
        )
        if changes.created_directories != expected_created:
            raise WorkspacePrecommitSealError(
                "Office precommit seal includes an unrelated directory change"
            )
        staged_entries = _scan_workspace(staged)
        created_entries: list[tuple[str, WorkspaceEntry]] = []
        for relative in expected_created:
            entry = staged_entries.get(relative)
            if entry is None or entry.kind != "directory":
                raise WorkspacePrecommitSealError(
                    "Office precommit create ancestor is not a directory"
                )
            created_entries.append((relative, entry))
        guard = _OfficePrecommitGuard(
            relative=seal.relative_path,
            expected=WorkspaceEntry(
                kind="file",
                mode=seal.source_mode,
                size=seal.source_size,
                sha256=seal.source_sha256,
            ),
            workspace_identity=self._workspace_identity,
            baseline=baseline.get(seal.relative_path),
            created_directories=tuple(created_entries),
            staged_root_identity=seal.root_identity,
            staged_source_identity=seal.source_identity,
            validation_generation=validation_generation,
        )
        self._assert_office_precommit_source(staged=staged, guard=guard)
        return guard

    def _assert_office_precommit_layout(
        self,
        *,
        guard: _OfficePrecommitGuard,
        changes: WorkspaceChangeSet,
        observed: dict[str, WorkspaceEntry],
    ) -> None:
        """Keep the exact sealed write/ancestor layout stable until journal."""

        if self.collect_changes() != changes:
            raise WorkspacePrecommitSealError(
                "Office precommit staged layout changed after validation"
            )
        for relative, expected in guard.created_directories:
            if observed.get(relative) != expected:
                raise WorkspacePrecommitSealError(
                    "Office precommit create ancestor metadata changed"
                )

    def _assert_office_precommit_source(
        self,
        *,
        staged: Path,
        guard: _OfficePrecommitGuard,
        observed: WorkspaceEntry | None = None,
    ) -> None:
        """Revalidate the sealed source through an immutable staged-root handle."""

        if (
            self._workspace_identity != guard.workspace_identity
            or self._office_precommit_generation != guard.validation_generation
            or self._require_baseline().get(guard.relative) != guard.baseline
        ):
            raise WorkspacePrecommitSealError(
                "Office precommit transaction identity changed"
            )
        try:
            with _open_workspace_root_fd(
                staged,
                expected_identity=guard.staged_root_identity,
            ) as staged_fd:
                current = _read_entry_at_relative(staged_fd, guard.relative)
                source_identity = _read_identity_at_relative(
                    staged_fd,
                    guard.relative,
                )
        except WorkspaceMutationError as exc:
            raise WorkspacePrecommitSealError(
                "Office precommit staged source is unavailable"
            ) from exc
        if (
            current != guard.expected
            or source_identity != guard.staged_source_identity
            or (observed is not None and observed != guard.expected)
        ):
            raise WorkspacePrecommitSealError(
                "Office precommit staged source changed after validation"
            )

    def abort(self) -> None:
        """Discard the private view without mutating the selected workspace."""

        if self.transaction_root is not None and not self._preserve_for_recovery:
            shutil.rmtree(self.transaction_root, ignore_errors=True)
        if not self._preserve_for_recovery:
            self.transaction_root = None
            self.staged_workspace = None
            self._baseline = None
            self._baseline_hardlinks = None
            self._baseline_linked_paths = None
            self._targeted_scope_paths = None
            self._targeted_mutation_paths = None
            self._targeted_read_paths = None
            self._targeted_declared_baseline = None
            self._targeted_source_identities = None
            self._office_precommit_relative = None
            self._office_precommit_root_identity = None
            self._office_precommit_generation = None
            self._journal_payload = None
            self._workspace_identity = None
            self._workspace_durable_token = None
        self._finished = True

    def _checkpoint_journal_token(self) -> str:
        root = self.transaction_root
        if root is None or root.parent.name != self._workspace_key:
            raise WorkspaceMutationError("Checkpoint journal has an invalid owner path")
        journal = root / _JOURNAL_NAME
        if not journal.is_file() or (self._journal_payload or {}).get("state") != "committed":
            raise WorkspaceMutationError("Checkpoint journal is not durably committed")
        return f"{self._workspace_key}/{root.name}"

    def _retain_committed_checkpoint_journal(self) -> None:
        """Drop the large stage but retain the minimal crash-bridge journal."""

        root = self.transaction_root
        if root is None:
            raise WorkspaceMutationError("Checkpoint journal root is unavailable")
        journal = root / _JOURNAL_NAME
        for child in root.iterdir():
            if child == journal:
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        self.transaction_root = None
        self.staged_workspace = None
        self._baseline = None
        self._baseline_hardlinks = None
        self._baseline_linked_paths = None
        self._targeted_scope_paths = None
        self._targeted_mutation_paths = None
        self._targeted_read_paths = None
        self._targeted_declared_baseline = None
        self._targeted_source_identities = None
        self._office_precommit_relative = None
        self._office_precommit_root_identity = None
        self._office_precommit_generation = None
        self._journal_payload = None
        self._workspace_identity = None
        self._workspace_durable_token = None
        self._finished = True

    def _write_prepared_journal(
        self,
        *,
        changes: WorkspaceChangeSet,
        versions: list[FileVersion],
        final_entries: dict[str, WorkspaceEntry],
        temporary_names: dict[str, str],
    ) -> None:
        baseline = self._require_baseline()
        root = self.transaction_root
        if root is None:
            raise WorkspaceMutationError("Workspace transaction journal has no root")
        if (
            self._workspace_identity is None
            or self._workspace_durable_token is None
        ):
            raise WorkspaceMutationError(
                "Workspace transaction durable identity is unavailable"
            )
        version_by_path = {version.relative_path: version.id for version in versions}
        existing: dict[str, object] = {}
        existing_symlinks: dict[str, object] = {}
        for relative in (*changes.writes, *changes.deletes):
            before = baseline.get(relative)
            if before is None:
                continue
            if before.kind == "symlink":
                if (
                    self.checkpoint_action != "rewind"
                    or relative not in changes.deletes
                    or relative in changes.writes
                ):
                    raise WorkspaceMutationError(
                        "Existing symbolic links are journaled only for rewind deletion"
                    )
                existing_symlinks[relative] = {
                    "before": asdict(before),
                    "after": None,
                }
                continue
            version_id = version_by_path.get(relative)
            if version_id is None:
                raise WorkspaceMutationError(
                    f"Recovery version is missing for command output: {relative}"
                )
            existing[relative] = {
                "version_id": version_id,
                "before": asdict(before),
                "after": (
                    asdict(final_entries[relative])
                    if relative in final_entries
                    else None
                ),
            }
        new_paths = {
            relative: asdict(final_entries[relative])
            for relative in changes.writes
            if relative not in baseline
        }
        payload: dict[str, object] = {
            "schema_version": _JOURNAL_SCHEMA_VERSION,
            "state": "prepared",
            "workspace": str(self.workspace),
            "workspace_identity": {
                "token": self._workspace_durable_token,
                "dev": self._workspace_identity[0],
                "ino": self._workspace_identity[1],
            },
            "operation": self.operation,
            "existing": existing,
            "existing_symlinks": existing_symlinks,
            "new_paths": new_paths,
            "created_directories": {
                relative: {
                    "mode": final_entries[relative].mode,
                    # Filled only after mkdir returned a descriptor-verified
                    # identity and that proof was durably persisted.  A crash
                    # before then must preserve any same-name directory.
                    "identity": None,
                }
                for relative in changes.created_directories
            },
            "deleted_directories": {
                relative: baseline[relative].mode
                for relative in changes.deleted_directories
            },
            "temporary_paths": temporary_names,
        }
        if self.ctx.checkpoint_id is not None:
            runtime_values = {
                "session_id": self.ctx.session_id,
                "message_id": self.ctx.message_id,
                "call_id": self.ctx.call_id,
                "root_turn_id": self.ctx.root_turn_id,
                "turn_run_id": self.ctx.turn_run_id,
                "checkpoint_id": self.ctx.checkpoint_id,
                "workspace_instance_id": self.ctx.workspace_instance_id,
                "tool_operation": self.operation,
            }
            if not all(
                isinstance(value, str) and bool(value.strip())
                for value in runtime_values.values()
            ):
                raise WorkspaceMutationError(
                    "Checkpoint-aware commit is missing trusted runtime identity"
                )
            payload["runtime_checkpoint"] = {
                **runtime_values,
                "action": self.checkpoint_action,
                "rewind_checkpoint_ids": list(self.rewind_checkpoint_ids),
            }
        self._journal_payload = payload
        _persist_journal(root / _JOURNAL_NAME, payload)

    def _write_created_directory_proofs(
        self,
        *,
        workspace_fd: int,
        identities: dict[str, tuple[int, int]],
        final_entries: dict[str, WorkspaceEntry],
    ) -> None:
        root = self.transaction_root
        payload = self._journal_payload
        if root is None or payload is None:
            raise WorkspaceMutationError("Workspace transaction journal is unavailable")
        raw_created = payload.get("created_directories")
        if not isinstance(raw_created, dict):
            raise WorkspaceMutationError(
                "Workspace transaction created-directory journal is invalid"
            )
        updated = dict(raw_created)
        for relative, identity in identities.items():
            current_identity = _relative_inode_identity(workspace_fd, relative)
            current_entry = _read_entry_at_relative(workspace_fd, relative)
            expected_entry = final_entries.get(relative)
            if (
                current_identity != identity
                or expected_entry is None
                or expected_entry.kind != "directory"
                or current_entry != expected_entry
            ):
                raise WorkspaceMutationError(
                    "Created workspace directory changed before its recovery "
                    f"ownership proof was persisted: {relative}"
                )
            updated[relative] = {
                "mode": expected_entry.mode,
                "identity": {"dev": identity[0], "ino": identity[1]},
            }
        next_payload = {**payload, "created_directories": updated}
        _persist_journal(root / _JOURNAL_NAME, next_payload)
        self._journal_payload = next_payload

    def _write_journal_state(self, state: str) -> None:
        root = self.transaction_root
        payload = self._journal_payload
        if root is None or payload is None:
            raise WorkspaceMutationError("Workspace transaction journal is unavailable")
        payload = {**payload, "state": state}
        _persist_journal(root / _JOURNAL_NAME, payload)
        self._journal_payload = payload

    def _ensure_workspace_identity(self) -> WorkspaceIdentityState:
        """Read the admitted durable identity before creating any stage.

        A checkpoint-aware context is already bound to a database-owned
        workspace instance.  It must inspect rather than ensure here: ensuring
        an unmarked replacement directory would mint a new identity for an
        attacker-controlled root before discovering the mismatch.
        """

        try:
            expected_token = self.ctx.workspace_identity_token
            if self.ctx.workspace_instance_id is not None:
                if not isinstance(expected_token, str) or not expected_token.strip():
                    raise WorkspaceMutationError(
                        "Workspace transaction is missing its admitted durable identity"
                    )
                identity = inspect_workspace_identity(self.workspace)
                if identity.durable_token != expected_token:
                    raise WorkspaceMutationError(
                        "Workspace no longer matches its admitted durable identity"
                    )
            else:
                identity = ensure_workspace_identity(self.workspace)
        except WorkspaceIdentityError as exc:
            raise WorkspaceMutationError(
                "Workspace durable identity is unavailable or changed"
            ) from exc
        if identity.canonical_path != self.workspace:
            raise WorkspaceMutationError(
                "Workspace durable identity resolved to a different path"
            )
        self._workspace_identity = identity.volatile_identity
        self._workspace_durable_token = identity.durable_token
        return identity

    def _assert_durable_workspace_identity(self) -> None:
        """Reject marker or native-root replacement during a live operation."""

        volatile_identity = self._workspace_identity
        durable_token = self._workspace_durable_token
        if volatile_identity is None or durable_token is None:
            raise WorkspaceMutationError(
                "Workspace transaction durable identity is unavailable"
            )
        try:
            identity = inspect_workspace_identity(self.workspace)
        except WorkspaceIdentityError as exc:
            raise WorkspaceMutationError(
                "Workspace durable identity changed during transaction"
            ) from exc
        if (
            identity.canonical_path != self.workspace
            or identity.volatile_identity != volatile_identity
            or identity.durable_token != durable_token
        ):
            raise WorkspaceMutationError(
                "Workspace durable identity changed during transaction"
            )

    def _require_stage(self) -> Path:
        if self.staged_workspace is None:
            raise WorkspaceMutationError("Workspace transaction is not prepared")
        return self.staged_workspace

    def _require_baseline(self) -> dict[str, WorkspaceEntry]:
        if self._baseline is None:
            raise WorkspaceMutationError("Workspace transaction is not prepared")
        return self._baseline

    def _assert_targeted_declared_snapshot(self, workspace_fd: int) -> None:
        baseline = self._targeted_declared_baseline
        identities = self._targeted_source_identities
        if baseline is None or identities is None:
            return
        for relative, expected in baseline.items():
            if (
                _read_entry_at_relative(workspace_fd, relative) != expected
                or _read_identity_at_relative(workspace_fd, relative)
                != identities[relative]
            ):
                raise WorkspaceMutationError(
                    "Workspace declared path changed outside the targeted "
                    f"transaction: {relative}"
                )

    def _assert_targeted_read_dependencies(self, workspace_fd: int) -> None:
        baseline = self._targeted_declared_baseline
        identities = self._targeted_source_identities
        if baseline is None or identities is None:
            return
        for relative in self._targeted_read_paths or frozenset():
            if (
                _read_entry_at_relative(workspace_fd, relative) != baseline[relative]
                or _read_identity_at_relative(workspace_fd, relative)
                != identities[relative]
            ):
                raise WorkspaceMutationError(
                    "Workspace read dependency changed during the targeted "
                    f"transaction: {relative}"
                )


def _scan_workspace(root: Path) -> dict[str, WorkspaceEntry]:
    entries: dict[str, WorkspaceEntry] = {}
    total_bytes = 0

    def visit(directory: Path, relative_parent: Path) -> None:
        nonlocal total_bytes
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise WorkspaceMutationError(f"Could not scan workspace directory: {directory}") from exc
        for child in children:
            if not relative_parent.parts and child.name == _INTERNAL_ROOT:
                continue
            relative_path = relative_parent / child.name
            relative = relative_path.as_posix()
            try:
                info = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise WorkspaceMutationError(f"Could not inspect workspace path: {relative}") from exc
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISLNK(info.st_mode):
                entries[relative] = WorkspaceEntry(
                    kind="symlink",
                    mode=mode,
                    link_target=os.readlink(child.path),
                )
            elif stat.S_ISDIR(info.st_mode):
                entries[relative] = WorkspaceEntry(kind="directory", mode=mode)
                visit(Path(child.path), relative_path)
            elif stat.S_ISREG(info.st_mode):
                if info.st_size > MAX_STAGED_FILE_BYTES:
                    raise WorkspaceMutationError(
                        f"Workspace file exceeds the transaction limit: {relative}"
                    )
                digest, size = _hash_regular_file(Path(child.path))
                total_bytes += size
                if total_bytes > MAX_STAGED_WORKSPACE_BYTES:
                    raise WorkspaceMutationError(
                        "Workspace exceeds the 512 MiB command transaction limit"
                    )
                entries[relative] = WorkspaceEntry(
                    kind="file",
                    mode=mode,
                    size=size,
                    sha256=digest,
                )
            else:
                raise WorkspaceMutationError(
                    f"Workspace contains an unsupported special file: {relative}"
                )
            if len(entries) > MAX_STAGED_ENTRIES:
                raise WorkspaceMutationError(
                    f"Workspace exceeds the {MAX_STAGED_ENTRIES}-entry command transaction limit"
                )

    visit(root, Path())
    return entries


def _scan_hardlink_groups(root: Path) -> dict[str, tuple[str, ...]]:
    """Return normalized in-workspace hard-link groups.

    Device/inode values differ between the real and staged trees, so topology is
    represented by the sorted relative names in each group.
    """

    groups: dict[tuple[int, int], list[str]] = {}

    def visit(directory: Path, relative_parent: Path) -> None:
        for child in os.scandir(directory):
            if not relative_parent.parts and child.name == _INTERNAL_ROOT:
                continue
            relative_path = relative_parent / child.name
            info = child.stat(follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                visit(Path(child.path), relative_path)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                groups.setdefault((info.st_dev, info.st_ino), []).append(
                    relative_path.as_posix()
                )

    visit(root, Path())
    normalized: dict[str, tuple[str, ...]] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        group = tuple(sorted(members))
        for relative in group:
            normalized[relative] = group
    return normalized


def _scan_multiply_linked_paths(root: Path) -> frozenset[str]:
    """Return regular paths whose inode has another link, even outside root."""

    linked: set[str] = set()

    def visit(directory: Path, relative_parent: Path) -> None:
        for child in os.scandir(directory):
            if not relative_parent.parts and child.name == _INTERNAL_ROOT:
                continue
            relative_path = relative_parent / child.name
            info = child.stat(follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                visit(Path(child.path), relative_path)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                linked.add(relative_path.as_posix())

    visit(root, Path())
    return frozenset(linked)


def _workspace_commit_lock(workspace_key: str) -> threading.RLock:
    with _LOCKS_GUARD:
        return _WORKSPACE_COMMIT_LOCKS.setdefault(workspace_key, threading.RLock())


def _require_guarded_workspace_mutation_support() -> None:
    reason = guarded_file_mutation_unavailable_reason()
    if reason is not None:
        raise WorkspaceMutationError(reason)


def _assert_workspace_path_identity(
    workspace: Path,
    expected_identity: tuple[int, int] | None,
) -> None:
    if expected_identity is None:
        raise WorkspaceMutationError("Workspace identity was not captured")
    try:
        info = workspace.stat(follow_symlinks=False)
        identity = _path_identity(workspace, directory=True)
    except OSError as exc:
        raise WorkspaceMutationError("Workspace root changed during command commit") from exc
    if (
        identity != expected_identity
        or not stat.S_ISDIR(info.st_mode)
        or (sys.platform == "win32" and windows_lstat_is_reparse(info))
    ):
        raise WorkspaceMutationError("Workspace root changed during command commit")


def _path_identity(path: Path, *, directory: bool) -> tuple[int, int]:
    if sys.platform == "win32":
        try:
            return windows_path_identity(path, directory=directory)
        except WindowsGuardedFileError as exc:
            raise WorkspaceMutationError(
                f"Workspace path is unavailable or redirected: {path}"
            ) from exc
    info = path.stat(follow_symlinks=False)
    return info.st_dev, info.st_ino


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


@contextmanager
def _open_workspace_root_fd(
    workspace: Path,
    *,
    expected_identity: tuple[int, int] | None,
):
    if sys.platform == "win32":
        try:
            with locked_directory_chain(
                workspace,
                (),
                expected_workspace_identity=expected_identity,
            ) as api:
                identity = api.path_info(workspace, directory=True).identity.as_tuple()
                anchor = _WindowsWorkspaceAnchor(workspace, api, identity, [])
                try:
                    yield anchor
                    _assert_workspace_path_identity(workspace, expected_identity)
                finally:
                    for held in reversed(anchor.held_directory_handles):
                        try:
                            api.close_handle(held)
                        except OSError:
                            logger.exception(
                                "Could not close held Windows workspace directory handle"
                            )
            return
        except WindowsGuardedFileError as exc:
            raise WorkspaceMutationError(
                "Workspace root is unavailable, redirected, or changed"
            ) from exc
    try:
        descriptor = os.open(workspace, _directory_open_flags())
    except OSError as exc:
        raise WorkspaceMutationError("Workspace root is unavailable or redirected") from exc
    try:
        info = os.fstat(descriptor)
        identity = (info.st_dev, info.st_ino)
        if not stat.S_ISDIR(info.st_mode):
            raise WorkspaceMutationError("Workspace root is not a directory")
        if expected_identity is not None and identity != expected_identity:
            raise WorkspaceMutationError("Workspace root changed during command execution")
        yield descriptor
        _assert_workspace_path_identity(workspace, expected_identity)
    finally:
        os.close(descriptor)


def _relative_parts(relative: str) -> tuple[str, ...]:
    path = Path(relative)
    if not relative or path.is_absolute() or ".." in path.parts or path.as_posix() != relative:
        raise WorkspaceMutationError(f"Unsafe workspace transaction path: {relative!r}")
    return path.parts


def _require_windows_anchor(workspace_fd: object) -> _WindowsWorkspaceAnchor:
    if not isinstance(workspace_fd, _WindowsWorkspaceAnchor):
        raise WorkspaceMutationError("Windows workspace anchor is unavailable")
    return workspace_fd


@contextmanager
def _lock_windows_relative(
    workspace_fd: object,
    relative: str,
):
    anchor = _require_windows_anchor(workspace_fd)
    try:
        with locked_directory_chain(
            anchor.root,
            (relative,),
            backend=anchor.api,
            expected_workspace_identity=anchor.identity,
        ):
            yield anchor, anchor.root / Path(*_relative_parts(relative))
    except WindowsGuardedFileError as exc:
        raise WorkspaceMutationError(
            f"Workspace parent changed or is redirected: {relative}"
        ) from exc


def _windows_read_entry_path(
    anchor: _WindowsWorkspaceAnchor,
    path: Path,
) -> WorkspaceEntry | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if windows_lstat_is_reparse(info):
        raise WorkspaceMutationError(f"Workspace path is a Windows reparse point: {path}")
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISDIR(info.st_mode):
        native = anchor.api.path_info(path, directory=True)
        if native.is_reparse_point:
            raise WorkspaceMutationError(f"Workspace path is redirected: {path}")
        return WorkspaceEntry(kind="directory", mode=mode)
    if not stat.S_ISREG(info.st_mode):
        raise WorkspaceMutationError(f"Workspace path became a special file: {path}")
    try:
        with open_regular_file_for_stable_read(path, backend=anchor.api) as (
            descriptor,
            native,
        ):
            opened = os.fstat(descriptor)
            digest, size = _hash_regular_fd(descriptor)
            after = os.fstat(descriptor)
            current_native = anchor.api.path_info(path, directory=False)
            if (
                native.size != size
                or native.identity != current_native.identity
                or opened.st_size != size
                or after.st_size != size
                or opened.st_mtime_ns != after.st_mtime_ns
            ):
                raise WorkspaceMutationError(
                    f"Workspace path changed while being read: {path}"
                )
            return WorkspaceEntry(
                kind="file",
                mode=stat.S_IMODE(after.st_mode),
                size=size,
                sha256=digest,
            )
    except WindowsGuardedFileError as exc:
        raise WorkspaceMutationError(f"Could not safely read workspace file: {path}") from exc


def _normalize_targeted_paths(
    workspace: Path,
    paths: Iterable[str | os.PathLike[str]],
) -> tuple[str, ...]:
    """Return unique lexical workspace-relative file declarations."""

    if isinstance(paths, (str, os.PathLike)):
        values: Iterable[str | os.PathLike[str]] = (paths,)
    else:
        values = paths
    normalized: dict[str, str] = {}
    for value in values:
        if sys.platform == "win32":
            _validate_windows_declared_lexical_path(workspace, value)
        logical = Path(value)
        if not logical.is_absolute():
            logical = workspace / logical
        # Match the canonical workspace root (notably /var -> /private/var on
        # macOS) and the public workspace path resolvers used by callers.
        logical = logical.expanduser().resolve(strict=False)
        try:
            relative_path = logical.relative_to(workspace)
        except ValueError as exc:
            raise WorkspaceMutationError(
                f"Targeted transaction path is outside the workspace: {logical}"
            ) from exc
        relative = relative_path.as_posix()
        if relative in ("", "."):
            raise WorkspaceMutationError(
                "Workspace root cannot be a targeted transaction path"
            )
        parts = _relative_parts(relative)
        if sys.platform == "win32":
            _validate_windows_relative_path(relative)
        if parts[0].casefold() == _INTERNAL_ROOT.casefold():
            raise WorkspaceMutationError(
                "Application-private workspace paths cannot be targeted"
            )
        key = _windows_relative_key(relative) if sys.platform == "win32" else relative
        previous = normalized.get(key)
        if previous is not None and previous != relative:
            raise WorkspaceMutationError(
                "Targeted Windows paths alias the same filesystem name: "
                f"{previous!r}, {relative!r}"
            )
        normalized[key] = relative
    return tuple(sorted(normalized.values()))


def _windows_relative_key(relative: str) -> str:
    return windows_relative_key(relative)


def _validate_windows_declared_lexical_path(
    workspace: Path,
    value: str | os.PathLike[str],
) -> None:
    """Validate caller spelling before Win32 canonicalization can erase aliases."""

    try:
        validate_windows_declared_path(workspace, value)
    except ValueError as exc:
        raise WorkspaceMutationError(str(exc)) from exc


def _validate_windows_relative_path(relative: str) -> None:
    """Reject Win32 aliases, ADS, devices, and normalization-ambiguous names."""

    try:
        validate_windows_relative_name(relative)
    except ValueError as exc:
        raise WorkspaceMutationError(str(exc)) from exc


def _reject_windows_aliases(relatives: Iterable[str]) -> None:
    seen: dict[str, str] = {}
    for relative in relatives:
        _validate_windows_relative_path(relative)
        key = _windows_relative_key(relative)
        previous = seen.get(key)
        if previous is not None:
            if previous == relative:
                continue
            raise WorkspaceMutationError(
                "Targeted Windows declarations alias the same path: "
                f"{previous!r}, {relative!r}"
            )
        seen[key] = relative


def _preflight_windows_targeted_parents(
    workspace: Path,
    relatives: Iterable[str],
    *,
    expected_workspace_identity: tuple[int, int],
) -> None:
    values = tuple(relatives)
    try:
        with locked_directory_chain(
            workspace,
            values,
            expected_workspace_identity=expected_workspace_identity,
        ) as api:
            for relative in values:
                parent = workspace / Path(relative).parent
                try:
                    info = parent.lstat()
                except FileNotFoundError as exc:
                    raise WorkspaceMutationError(
                        "Windows output parent must already exist before the "
                        f"operation starts: {parent}"
                    ) from exc
                if windows_lstat_is_reparse(info) or not stat.S_ISDIR(info.st_mode):
                    raise WorkspaceMutationError(
                        f"Windows output parent is redirected or not a directory: {parent}"
                    )
                api.path_info(parent, directory=True)
    except WindowsGuardedFileError as exc:
        raise WorkspaceMutationError(
            "Windows targeted path parent is unavailable or redirected"
        ) from exc


def _targeted_scope_paths(terminals: Iterable[str]) -> tuple[str, ...]:
    scope: set[str] = set()
    for terminal in terminals:
        parts = _relative_parts(terminal)
        for end in range(1, len(parts) + 1):
            scope.add(Path(*parts[:end]).as_posix())
    return tuple(sorted(scope, key=lambda value: (value.count("/"), value)))


def _reject_targeted_prefix_conflicts(terminals: Iterable[str]) -> None:
    values = set(terminals)
    if sys.platform == "win32":
        _reject_windows_aliases(values)
        key_by_value = {value: _windows_relative_key(value) for value in values}
    else:
        key_by_value = {value: value for value in values}
    ordered = sorted(values, key=lambda value: (value.count("/"), key_by_value[value]))
    for index, candidate in enumerate(ordered):
        prefix = key_by_value[candidate] + "/"
        if any(
            key_by_value[other].startswith(prefix)
            for other in ordered[index + 1 :]
        ):
            raise WorkspaceMutationError(
                "Targeted transaction paths cannot contain one another: "
                f"{candidate}"
            )


def _read_identity_at_relative(
    workspace_fd: int,
    relative: str,
) -> tuple[int, int] | None:
    """Read an inode identity without treating a missing ancestor as an error."""

    if sys.platform == "win32":
        with _lock_windows_relative(workspace_fd, relative) as (anchor, path):
            try:
                info = path.lstat()
            except FileNotFoundError:
                return None
            if windows_lstat_is_reparse(info):
                raise WorkspaceMutationError(
                    f"Workspace parent changed or is redirected: {relative}"
                )
            return anchor.api.path_info(
                path,
                directory=stat.S_ISDIR(info.st_mode),
            ).identity.as_tuple()

    parts = _relative_parts(relative)
    descriptor = os.dup(workspace_fd)
    try:
        for component in parts[:-1]:
            try:
                child = os.open(component, _directory_open_flags(), dir_fd=descriptor)
            except FileNotFoundError:
                return None
            os.close(descriptor)
            descriptor = child
        try:
            info = os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return None
        return info.st_dev, info.st_ino
    except OSError as exc:
        raise WorkspaceMutationError(
            f"Workspace parent changed or is redirected: {relative}"
        ) from exc
    finally:
        os.close(descriptor)


def _copy_regular_file_at_relative(
    workspace_fd: int,
    relative: str,
    destination: Path,
) -> tuple[WorkspaceEntry, int]:
    """Copy one selected regular file through an anchored no-follow handle."""

    if sys.platform == "win32":
        with _lock_windows_relative(workspace_fd, relative) as (anchor, source):
            try:
                source_info = source.lstat()
            except FileNotFoundError as exc:
                raise WorkspaceMutationError(
                    f"Workspace path disappeared during targeted staging: {relative}"
                ) from exc
            if windows_lstat_is_reparse(source_info) or not stat.S_ISREG(
                source_info.st_mode
            ):
                raise WorkspaceMutationError(
                    f"Targeted workspace path is not a regular file: {relative}"
                )
            destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            destination_fd = -1
            try:
                with open_regular_file_for_stable_read(
                    source, backend=anchor.api
                ) as (source_fd, native):
                    current_native = anchor.api.path_info(source, directory=False)
                    if current_native.identity != native.identity:
                        raise WorkspaceMutationError(
                            f"Workspace path changed while being copied: {relative}"
                        )
                    if native.size > MAX_STAGED_FILE_BYTES:
                        raise WorkspaceMutationError(
                            f"Workspace file exceeds the transaction limit: {relative}"
                        )
                    destination_fd = os.open(
                        destination,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                        0o600,
                    )
                    digest = hashlib.sha256()
                    copied = 0
                    while True:
                        chunk = os.read(source_fd, _COPY_CHUNK_BYTES)
                        if not chunk:
                            break
                        copied += len(chunk)
                        digest.update(chunk)
                        view = memoryview(chunk)
                        while view:
                            written = os.write(destination_fd, view)
                            if written <= 0:
                                raise WorkspaceMutationError(
                                    f"Short write while staging workspace path: {relative}"
                                )
                            view = view[written:]
                    if copied != native.size:
                        raise WorkspaceMutationError(
                            f"Workspace path changed while being copied: {relative}"
                        )
                    os.fsync(destination_fd)
                    return (
                        WorkspaceEntry(
                            kind="file",
                            mode=stat.S_IMODE(source_info.st_mode),
                            size=copied,
                            sha256=digest.hexdigest(),
                        ),
                        native.link_count,
                    )
            except Exception:
                try:
                    destination.unlink()
                except OSError:
                    pass
                raise
            finally:
                if destination_fd >= 0:
                    os.close(destination_fd)

    with _open_parent_fd(workspace_fd, relative) as (parent_fd, name):
        try:
            initial = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise WorkspaceMutationError(
                f"Workspace path disappeared during targeted staging: {relative}"
            ) from exc
        if not stat.S_ISREG(initial.st_mode):
            raise WorkspaceMutationError(
                f"Targeted workspace path is not a regular file: {relative}"
            )
        if initial.st_size > MAX_STAGED_FILE_BYTES:
            raise WorkspaceMutationError(
                f"Workspace file exceeds the transaction limit: {relative}"
            )
        source_fd = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        destination_fd = -1
        try:
            opened = os.fstat(source_fd)
            if (opened.st_dev, opened.st_ino) != (initial.st_dev, initial.st_ino):
                raise WorkspaceMutationError(
                    f"Workspace path changed while being copied: {relative}"
                )
            destination_fd = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            digest = hashlib.sha256()
            copied = 0
            while True:
                chunk = os.read(source_fd, _COPY_CHUNK_BYTES)
                if not chunk:
                    break
                copied += len(chunk)
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_fd, view)
                    if written <= 0:
                        raise WorkspaceMutationError(
                            f"Short write while staging workspace path: {relative}"
                        )
                    view = view[written:]
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            after = os.fstat(source_fd)
            if (
                (current.st_dev, current.st_ino) != (after.st_dev, after.st_ino)
                or (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)
                or stat.S_IMODE(current.st_mode) != stat.S_IMODE(after.st_mode)
                or stat.S_IMODE(opened.st_mode) != stat.S_IMODE(after.st_mode)
                or current.st_size != copied
                or after.st_size != copied
                or current.st_mtime_ns != after.st_mtime_ns
                or opened.st_mtime_ns != after.st_mtime_ns
            ):
                raise WorkspaceMutationError(
                    f"Workspace path changed while being copied: {relative}"
                )
            os.fchmod(destination_fd, stat.S_IMODE(after.st_mode))
            os.fsync(destination_fd)
            return (
                WorkspaceEntry(
                    kind="file",
                    mode=stat.S_IMODE(after.st_mode),
                    size=copied,
                    sha256=digest.hexdigest(),
                ),
                after.st_nlink,
            )
        except Exception:
            try:
                destination.unlink()
            except OSError:
                pass
            raise
        finally:
            if destination_fd >= 0:
                os.close(destination_fd)
            os.close(source_fd)


@contextmanager
def _open_parent_fd(workspace_fd: int, relative: str):
    parts = _relative_parts(relative)
    descriptor = os.dup(workspace_fd)
    try:
        for component in parts[:-1]:
            child = os.open(component, _directory_open_flags(), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor, parts[-1]
        try:
            _verify_parent_fd_reachable(workspace_fd, relative, descriptor)
        except WorkspaceMutationError as exc:
            raise _WorkspaceParentMovedAfterOperation(str(exc)) from exc
    except OSError as exc:
        raise WorkspaceMutationError(
            f"Workspace parent changed or is redirected: {relative}"
        ) from exc
    finally:
        os.close(descriptor)


def _verify_parent_fd_reachable(
    workspace_fd: int,
    relative: str,
    held_parent_fd: int,
) -> None:
    parts = _relative_parts(relative)
    descriptor = os.dup(workspace_fd)
    try:
        for component in parts[:-1]:
            child = os.open(component, _directory_open_flags(), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        held = os.fstat(held_parent_fd)
        current = os.fstat(descriptor)
        if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
            raise WorkspaceMutationError(
                f"Workspace parent moved during transaction: {relative}"
            )
    except OSError as exc:
        raise WorkspaceMutationError(
            f"Workspace parent moved during transaction: {relative}"
        ) from exc
    finally:
        os.close(descriptor)


def _hash_regular_fd(descriptor: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    copied = 0
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        chunk = os.read(descriptor, _COPY_CHUNK_BYTES)
        if not chunk:
            break
        copied += len(chunk)
        digest.update(chunk)
    return digest.hexdigest(), copied


def _read_entry_in_dir(parent_fd: int, name: str) -> WorkspaceEntry | None:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISDIR(info.st_mode):
        return WorkspaceEntry(kind="directory", mode=mode)
    if stat.S_ISLNK(info.st_mode):
        return WorkspaceEntry(
            kind="symlink",
            mode=mode,
            link_target=os.readlink(name, dir_fd=parent_fd),
        )
    if not stat.S_ISREG(info.st_mode):
        raise WorkspaceMutationError(f"Workspace path became a special file: {name}")
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )
    try:
        opened = os.fstat(descriptor)
        if opened.st_dev != info.st_dev or opened.st_ino != info.st_ino:
            raise WorkspaceMutationError(f"Workspace path changed while being read: {name}")
        digest, size = _hash_regular_fd(descriptor)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        after = os.fstat(descriptor)
        if (
            current.st_dev != after.st_dev
            or current.st_ino != after.st_ino
            or stat.S_IMODE(current.st_mode) != stat.S_IMODE(after.st_mode)
            or stat.S_IMODE(opened.st_mode) != stat.S_IMODE(after.st_mode)
            or after.st_size != size
            or current.st_size != size
            or after.st_mtime_ns != opened.st_mtime_ns
            or current.st_mtime_ns != after.st_mtime_ns
        ):
            raise WorkspaceMutationError(f"Workspace path changed while being read: {name}")
        return WorkspaceEntry(
            kind="file",
            mode=stat.S_IMODE(after.st_mode),
            size=size,
            sha256=digest,
        )
    finally:
        os.close(descriptor)


def _read_entry_at_relative(workspace_fd: int, relative: str) -> WorkspaceEntry | None:
    if sys.platform == "win32":
        with _lock_windows_relative(workspace_fd, relative) as (anchor, path):
            return _windows_read_entry_path(anchor, path)
    parts = _relative_parts(relative)
    descriptor = os.dup(workspace_fd)
    try:
        for component in parts[:-1]:
            try:
                child = os.open(component, _directory_open_flags(), dir_fd=descriptor)
            except FileNotFoundError:
                return None
            os.close(descriptor)
            descriptor = child
        return _read_entry_in_dir(descriptor, parts[-1])
    except OSError as exc:
        raise WorkspaceMutationError(
            f"Workspace parent changed or is redirected: {relative}"
        ) from exc
    finally:
        os.close(descriptor)


def _temporary_name(target_name: str) -> str:
    return f".{target_name}.suyo-tx-{secrets.token_hex(12)}"


def _windows_name_exists_unredirected(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if windows_lstat_is_reparse(info):
        raise WorkspaceMutationError(f"Guarded Windows name is redirected: {path}")
    return True


def _prepared_observable_name(temporary: _PreparedPath, parent: Path) -> str:
    if temporary.current_sidecar_name is not None:
        return temporary.current_sidecar_name
    displaced = parent / temporary.temporary_name
    if _windows_name_exists_unredirected(displaced):
        return temporary.temporary_name
    return temporary.replacement_name or temporary.temporary_name


def _recover_partial_windows_exchange(
    anchor: _WindowsWorkspaceAnchor,
    exchange: GuardedExchange,
    temporary: _PreparedPath,
    *,
    known_conflict: Path | None = None,
) -> None:
    """Restore the exact displaced name after a partial ReplaceFileW error.

    ReplaceFileW documents three errors for which one or more renames may have
    completed.  Its backup, when present, is still the exact object removed
    from the target.  Put that object back without a name gap and retain any
    object currently visible at the target under a unique conflict name.
    """

    displaced_exists = _windows_name_exists_unredirected(exchange.displaced)
    if not displaced_exists:
        return
    if _windows_name_exists_unredirected(exchange.target):
        conflict = exchange.target.parent / (
            f"{temporary.temporary_name}.partial-{secrets.token_hex(8)}"
        )
        displaced_identity = anchor.api.path_info(
            exchange.displaced, directory=False
        ).identity
        try:
            exchange.rollback(anchor.api, conflict)
        except WindowsGuardedFileError as exc:
            if exc.may_have_mutated:
                target_exists = _windows_name_exists_unredirected(exchange.target)
                if target_exists and anchor.api.path_info(
                    exchange.target, directory=False
                ).identity == displaced_identity:
                    if _windows_name_exists_unredirected(conflict):
                        temporary.current_sidecar_name = conflict.name
                    return
                if (
                    not target_exists
                    and _windows_name_exists_unredirected(exchange.displaced)
                ):
                    try:
                        anchor.api.move_noreplace(
                            exchange.displaced,
                            exchange.target,
                        )
                        if anchor.api.path_info(
                            exchange.target, directory=False
                        ).identity != displaced_identity:
                            raise WorkspaceMutationError(
                                "Partial rollback restored a different Windows object"
                            )
                        if _windows_name_exists_unredirected(conflict):
                            temporary.current_sidecar_name = conflict.name
                        return
                    except (WindowsGuardedFileError, FileExistsError) as move_exc:
                        raise WorkspaceMutationError(
                            "Partial Windows rollback left the target empty; exact "
                            f"objects were preserved near {exchange.target}"
                        ) from move_exc
            temporary.current_sidecar_name = exchange.displaced.name
            raise WorkspaceMutationError(
                "Partial ReplaceFileW state could not be rolled back; all names "
                f"were preserved: {exchange.target}"
            ) from exc
        except FileExistsError as exc:
            temporary.current_sidecar_name = exchange.displaced.name
            raise WorkspaceMutationError(
                "Partial ReplaceFileW state could not reserve a conflict sidecar; "
                f"all names were preserved: {exchange.target}"
            ) from exc
        temporary.current_sidecar_name = conflict.name
        return
    displaced_identity = anchor.api.path_info(
        exchange.displaced, directory=False
    ).identity
    try:
        anchor.api.move_noreplace(exchange.displaced, exchange.target)
        if anchor.api.path_info(
            exchange.target, directory=False
        ).identity != displaced_identity:
            raise WorkspaceMutationError(
                "Partial ReplaceFileW recovery restored a different object"
            )
        if _windows_name_exists_unredirected(exchange.replacement):
            temporary.current_sidecar_name = exchange.replacement.name
        elif known_conflict is not None and _windows_name_exists_unredirected(
            known_conflict
        ):
            temporary.current_sidecar_name = known_conflict.name
    except (WindowsGuardedFileError, FileExistsError) as exc:
        temporary.current_sidecar_name = exchange.displaced.name
        raise WorkspaceMutationError(
            "Partial ReplaceFileW state left the target name empty and its exact "
            f"displaced object was preserved: {exchange.displaced}"
        ) from exc


def _prepare_regular_replacement_at(
    workspace_fd: int,
    source: Path,
    relative: str,
    mode: int,
    *,
    temporary_name: str,
) -> _PreparedPath:
    if sys.platform == "win32":
        with _lock_windows_relative(workspace_fd, relative) as (_anchor, target):
            replacement_name = f"{temporary_name}.replacement"
            replacement = target.parent / replacement_name
            descriptor = -1
            source_descriptor = -1
            try:
                descriptor = os.open(
                    replacement,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_BINARY", 0),
                    0o600,
                )
                source_descriptor = os.open(
                    source,
                    os.O_RDONLY | getattr(os, "O_BINARY", 0),
                )
                while True:
                    chunk = os.read(source_descriptor, _COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    view = memoryview(chunk)
                    while view:
                        written = os.write(descriptor, view)
                        if written <= 0:
                            raise WorkspaceMutationError(
                                f"Short write while preparing replacement: {relative}"
                            )
                        view = view[written:]
                try:
                    os.chmod(replacement, mode)
                except OSError:
                    pass
                os.fsync(descriptor)
                return _PreparedPath(
                    relative=relative,
                    temporary_name=temporary_name,
                    replacement_name=replacement_name,
                )
            except FileExistsError as exc:
                raise WorkspaceMutationError(
                    f"Could not prepare replacement: {relative}"
                ) from exc
            except Exception:
                replacement.unlink(missing_ok=True)
                raise
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                if source_descriptor >= 0:
                    os.close(source_descriptor)
    with _open_parent_fd(workspace_fd, relative) as (parent_fd, target_name):
        descriptor = -1
        try:
            descriptor = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent_fd,
            )
        except FileExistsError as exc:
            raise WorkspaceMutationError(f"Could not prepare replacement: {relative}") from exc
        source_descriptor = -1
        try:
            source_descriptor = os.open(
                source,
                os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            while True:
                chunk = os.read(source_descriptor, _COPY_CHUNK_BYTES)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise WorkspaceMutationError(
                            f"Short write while preparing replacement: {relative}"
                        )
                    view = view[written:]
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
            return _PreparedPath(relative=relative, temporary_name=temporary_name)
        except Exception:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except OSError:
                pass
            raise
        finally:
            os.close(descriptor)
            if source_descriptor >= 0:
                os.close(source_descriptor)


def _prepare_symlink_replacement_at(
    workspace_fd: int,
    relative: str,
    link_target: str,
    *,
    temporary_name: str,
) -> _PreparedPath:
    if sys.platform == "win32":
        raise WorkspaceMutationError(
            "Targeted Windows transactions do not create symbolic links or junctions"
        )
    with _open_parent_fd(workspace_fd, relative) as (parent_fd, target_name):
        del target_name
        try:
            os.symlink(link_target, temporary_name, dir_fd=parent_fd)
        except FileExistsError as exc:
            raise WorkspaceMutationError(f"Could not prepare symbolic link: {relative}") from exc
        return _PreparedPath(relative=relative, temporary_name=temporary_name)


def _renameat_with_flags(
    source_fd: int,
    source_name: str,
    destination_fd: int,
    destination_name: str,
    *,
    exchange: bool,
) -> None:
    _require_guarded_workspace_mutation_support()
    libc = ctypes.CDLL(None, use_errno=True)
    source = os.fsencode(source_name)
    destination = os.fsencode(destination_name)
    if sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        function = libc.renameat2
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        flags = 2 if exchange else 1  # RENAME_EXCHANGE / RENAME_NOREPLACE
    elif sys.platform == "darwin" and hasattr(libc, "renameatx_np"):
        function = libc.renameatx_np
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        flags = 2 if exchange else 4  # RENAME_SWAP / RENAME_EXCL
    else:
        raise WorkspaceMutationError(
            "Atomic guarded workspace replacement is unavailable on this platform"
        )
    if function(source_fd, source, destination_fd, destination, flags) != 0:
        error = ctypes.get_errno()
        if not exchange and error == errno.EEXIST:
            raise FileExistsError(error, os.strerror(error), destination_name)
        raise OSError(error, os.strerror(error), destination_name)


def _exchange_prepared_at(workspace_fd: int, temporary: _PreparedPath) -> None:
    if sys.platform == "win32":
        with _lock_windows_relative(workspace_fd, temporary.relative) as (anchor, target):
            parent = target.parent
            replacement = parent / (temporary.replacement_name or temporary.temporary_name)
            displaced = parent / temporary.temporary_name
            if temporary.replacement_name is None:
                raise WorkspaceMutationError(
                    f"Windows replacement source is unavailable: {temporary.relative}"
                )
            # First call: prepared replacement exists, displaced backup does not.
            # Rollback call: displaced backup exists, prepared name was consumed.
            replacement_exists = _windows_name_exists_unredirected(replacement)
            displaced_exists = _windows_name_exists_unredirected(displaced)
            if replacement_exists and not displaced_exists:
                exchange = GuardedExchange(target, replacement, displaced)
                try:
                    exchange.install(anchor.api)
                except WindowsGuardedFileError as exc:
                    try:
                        _recover_partial_windows_exchange(
                            anchor,
                            exchange,
                            temporary,
                        )
                    except WorkspaceMutationError as recovery_exc:
                        raise _WindowsExchangeAmbiguous(str(recovery_exc)) from exc
                    if _windows_name_exists_unredirected(replacement):
                        temporary.current_sidecar_name = replacement.name
                    sidecar = (
                        f"; recovery sidecar: {parent / temporary.current_sidecar_name}"
                        if temporary.current_sidecar_name is not None
                        else ""
                    )
                    raise _WindowsExchangeRecovered(
                        "Guarded Windows replacement failed after restoring the "
                        f"visible object: {temporary.relative}: {exc}{sidecar}"
                    ) from exc
                return
            if displaced_exists and not replacement_exists:
                conflict = parent / f"{temporary.temporary_name}.rollback-{secrets.token_hex(8)}"
                exchange = GuardedExchange(target, replacement, displaced)
                try:
                    exchange.rollback(anchor.api, conflict)
                except WindowsGuardedFileError as exc:
                    if not exc.may_have_mutated:
                        raise WorkspaceMutationError(
                            "Guarded Windows rollback could not restore the displaced "
                            f"object: {temporary.relative}: {exc}"
                        ) from exc
                    try:
                        _recover_partial_windows_exchange(
                            anchor,
                            exchange,
                            temporary,
                            known_conflict=conflict,
                        )
                    except WorkspaceMutationError as recovery_exc:
                        raise _WindowsExchangeAmbiguous(str(recovery_exc)) from exc
                    if _windows_name_exists_unredirected(conflict):
                        temporary.current_sidecar_name = conflict.name
                    return
                temporary.current_sidecar_name = conflict.name
                return
            raise WorkspaceMutationError(
                "Guarded Windows replacement state is ambiguous; all observable "
                f"objects were preserved: {temporary.relative}"
            )
    with _open_parent_fd(workspace_fd, temporary.relative) as (parent_fd, target_name):
        _renameat_with_flags(
            parent_fd,
            temporary.temporary_name,
            parent_fd,
            target_name,
            exchange=True,
        )


def _link_prepared_new_at(workspace_fd: int, temporary: _PreparedPath) -> None:
    if sys.platform == "win32":
        with _lock_windows_relative(workspace_fd, temporary.relative) as (anchor, target):
            source = target.parent / (
                temporary.replacement_name or temporary.temporary_name
            )
            try:
                anchor.api.move_noreplace(source, target)
            except FileExistsError as exc:
                raise WorkspaceMutationError(
                    f"Workspace path was created by another writer: {temporary.relative}"
                ) from exc
            except WindowsGuardedFileError as exc:
                raise WorkspaceMutationError(
                    f"Could not install Windows workspace file: {temporary.relative}"
                ) from exc
            return
    with _open_parent_fd(workspace_fd, temporary.relative) as (parent_fd, target_name):
        _renameat_with_flags(
            parent_fd,
            temporary.temporary_name,
            parent_fd,
            target_name,
            exchange=False,
        )


def _read_prepared_entry(workspace_fd: int, temporary: _PreparedPath) -> WorkspaceEntry | None:
    if sys.platform == "win32":
        with _lock_windows_relative(workspace_fd, temporary.relative) as (anchor, target):
            path = target.parent / _prepared_observable_name(temporary, target.parent)
            return _windows_read_entry_path(anchor, path)
    with _open_parent_fd(workspace_fd, temporary.relative) as (parent_fd, _target_name):
        return _read_entry_in_dir(parent_fd, temporary.temporary_name)


def _prepared_nlink(workspace_fd: int, temporary: _PreparedPath) -> int:
    if sys.platform == "win32":
        with _lock_windows_relative(workspace_fd, temporary.relative) as (anchor, target):
            path = target.parent / _prepared_observable_name(temporary, target.parent)
            try:
                info = path.lstat()
            except FileNotFoundError:
                return 0
            if windows_lstat_is_reparse(info):
                raise WorkspaceMutationError(
                    f"Transaction sidecar is redirected: {temporary.relative}"
                )
            return anchor.api.path_info(
                path, directory=stat.S_ISDIR(info.st_mode)
            ).link_count
    with _open_parent_fd(workspace_fd, temporary.relative) as (parent_fd, _target_name):
        try:
            info = os.stat(
                temporary.temporary_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return 0
        return info.st_nlink


def _relative_nlink(workspace_fd: int, relative: str) -> int:
    if sys.platform == "win32":
        with _lock_windows_relative(workspace_fd, relative) as (anchor, path):
            try:
                info = path.lstat()
            except FileNotFoundError:
                return 0
            if windows_lstat_is_reparse(info):
                raise WorkspaceMutationError(f"Workspace path is redirected: {relative}")
            return anchor.api.path_info(
                path, directory=stat.S_ISDIR(info.st_mode)
            ).link_count
    with _open_parent_fd(workspace_fd, relative) as (parent_fd, target_name):
        try:
            info = os.stat(target_name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return 0
        return info.st_nlink


def _relative_inode_identity(
    workspace_fd: int,
    relative: str,
) -> tuple[int, int] | None:
    if sys.platform == "win32":
        return _read_identity_at_relative(workspace_fd, relative)
    with _open_parent_fd(workspace_fd, relative) as (parent_fd, target_name):
        try:
            info = os.stat(target_name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        return info.st_dev, info.st_ino


def _rename_to_backup_at(
    workspace_fd: int,
    relative: str,
    *,
    temporary_name: str | None = None,
) -> _PreparedPath:
    if sys.platform == "win32":
        with _lock_windows_relative(workspace_fd, relative) as (anchor, target):
            backup = _PreparedPath(
                relative=relative,
                temporary_name=temporary_name or _temporary_name(target.name),
            )
            backup_path = target.parent / backup.temporary_name
            try:
                anchor.api.move_noreplace(target, backup_path)
            except FileExistsError as exc:
                raise WorkspaceMutationError(
                    f"Could not reserve Windows delete backup: {relative}"
                ) from exc
            except WindowsGuardedFileError as exc:
                raise WorkspaceMutationError(
                    f"Could not guard Windows workspace deletion: {relative}"
                ) from exc
            return backup
    with _open_parent_fd(workspace_fd, relative) as (parent_fd, target_name):
        backup = _PreparedPath(
            relative=relative,
            temporary_name=temporary_name or _temporary_name(target_name),
        )
        _renameat_with_flags(
            parent_fd,
            target_name,
            parent_fd,
            backup.temporary_name,
            exchange=False,
        )
        return backup


def _restore_backup_noreplace_at(workspace_fd: int, backup: _PreparedPath) -> None:
    if sys.platform == "win32":
        with _lock_windows_relative(workspace_fd, backup.relative) as (anchor, target):
            source = target.parent / _prepared_observable_name(backup, target.parent)
            try:
                anchor.api.move_noreplace(source, target)
            except FileExistsError as exc:
                raise WorkspaceMutationError(
                    f"Rollback target was recreated by another writer: {backup.relative}"
                ) from exc
            except WindowsGuardedFileError as exc:
                raise WorkspaceMutationError(
                    f"Could not restore Windows delete backup: {backup.relative}"
                ) from exc
            return
    with _open_parent_fd(workspace_fd, backup.relative) as (parent_fd, target_name):
        _renameat_with_flags(
            parent_fd,
            backup.temporary_name,
            parent_fd,
            target_name,
            exchange=False,
        )


def _remove_relative_if_matches_at(
    workspace_fd: int,
    relative: str,
    *,
    expected: WorkspaceEntry,
) -> _PreparedPath:
    quarantine = _rename_to_backup_at(workspace_fd, relative)
    actual = _read_prepared_entry(workspace_fd, quarantine)
    if actual != expected:
        try:
            _restore_backup_noreplace_at(workspace_fd, quarantine)
        except Exception as exc:
            raise WorkspaceMutationError(
                "Rollback captured a later edit and could not put it back; "
                f"the object was preserved: {relative} ({quarantine.temporary_name})"
            ) from exc
        raise WorkspaceMutationError(f"Rollback output conflicts with a later edit: {relative}")
    # This inode occupied the visible target name and may still be referenced
    # by an open descriptor.  Unlinking it would make later writes through that
    # descriptor unreachable, so the hidden quarantine is a recovery sidecar.
    return quarantine


def _recovery_sidecar_path(workspace: Path, sidecar: _PreparedPath) -> str:
    return str(
        workspace
        / Path(sidecar.relative).parent
        / (sidecar.current_sidecar_name or sidecar.temporary_name)
    )


def _recovery_sidecar_error_suffix(
    workspace: Path,
    sidecars: list[_PreparedPath],
) -> str:
    paths = tuple(
        dict.fromkeys(_recovery_sidecar_path(workspace, item) for item in sidecars)
    )
    if not paths:
        return ""
    return f"; recovery sidecars: {', '.join(paths)}"


def _remove_owned_temporary_if_matches_at(
    workspace_fd: int,
    temporary: _PreparedPath,
    *,
    acceptable: set[WorkspaceEntry],
) -> None:
    if sys.platform == "win32":
        with _lock_windows_relative(workspace_fd, temporary.relative) as (anchor, target):
            source = target.parent / _prepared_observable_name(temporary, target.parent)
            if not _windows_name_exists_unredirected(source):
                return
            quarantine = target.parent / f"{_temporary_name(target.name)}.cleanup"
            try:
                anchor.api.move_noreplace(source, quarantine)
            except WindowsGuardedFileError as exc:
                raise WorkspaceMutationError(
                    f"Could not quarantine transaction temporary: {temporary.relative}"
                ) from exc
            captured = _windows_read_entry_path(anchor, quarantine)
            captured_info = anchor.api.path_info(quarantine, directory=False)
            if captured not in acceptable or captured_info.link_count != 1:
                raise WorkspaceMutationError(
                    "Transaction temporary changed or gained a hard link; captured "
                    f"object was preserved: {temporary.relative} ({quarantine.name})"
                )
            quarantine.unlink()
            return
    with _open_parent_fd(workspace_fd, temporary.relative) as (parent_fd, target_name):
        quarantine_name = f"{_temporary_name(target_name)}.cleanup"
        try:
            _renameat_with_flags(
                parent_fd,
                temporary.temporary_name,
                parent_fd,
                quarantine_name,
                exchange=False,
            )
        except FileNotFoundError:
            return
        captured = _read_entry_in_dir(parent_fd, quarantine_name)
        captured_nlink = os.stat(
            quarantine_name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        ).st_nlink
        if captured not in acceptable or captured_nlink != 1:
            raise WorkspaceMutationError(
                "Transaction temporary changed or gained a hard link; captured object "
                f"was preserved: {temporary.relative} ({quarantine_name})"
            )
        os.unlink(quarantine_name, dir_fd=parent_fd)


def _create_directory_at(
    workspace_fd: int,
    relative: str,
    *,
    baseline_mode: int,
    created: dict[str, tuple[int, int]],
) -> None:
    if sys.platform == "win32":
        # Full-workspace staging is not exposed on Windows, but recovery may
        # still call this helper for a journal produced by a targeted writer.
        parts = _relative_parts(relative)
        for index in range(1, len(parts) + 1):
            current = Path(*parts[:index]).as_posix()
            if _read_entry_at_relative(workspace_fd, current) is not None:
                continue
            _create_targeted_directory_at(
                workspace_fd,
                current,
                mode=baseline_mode if index == len(parts) else 0o755,
                created=created,
            )
        return
    parts = _relative_parts(relative)
    descriptor = os.dup(workspace_fd)
    current_parts: list[str] = []
    try:
        for index, component in enumerate(parts):
            current_parts.append(component)
            try:
                child = os.open(component, _directory_open_flags(), dir_fd=descriptor)
            except FileNotFoundError:
                installed_mode = baseline_mode if index == len(parts) - 1 else 0o755
                os.mkdir(
                    component,
                    mode=installed_mode,
                    dir_fd=descriptor,
                )
                child = os.open(component, _directory_open_flags(), dir_fd=descriptor)
                os.fchmod(child, installed_mode)
                child_info = os.fstat(child)
                created["/".join(current_parts)] = (
                    child_info.st_dev,
                    child_info.st_ino,
                )
            os.close(descriptor)
            descriptor = child
        _verify_directory_fd_reachable(workspace_fd, relative, descriptor)
    except OSError as exc:
        raise WorkspaceMutationError(f"Directory path changed or is redirected: {relative}") from exc
    finally:
        os.close(descriptor)


def _create_targeted_directory_at(
    workspace_fd: int,
    relative: str,
    *,
    mode: int,
    created: dict[str, tuple[int, int]],
) -> None:
    """Create one declared missing ancestor without merging a concurrent tree."""

    if sys.platform == "win32":
        with _lock_windows_relative(workspace_fd, relative) as (anchor, path):
            temporary = path.parent / f"{_temporary_name(path.name)}.directory"
            owns_visible_path = False
            try:
                os.mkdir(temporary, mode=0o700)
            except FileExistsError as exc:
                raise WorkspaceMutationError(
                    "Could not reserve a targeted output directory: "
                    f"{relative}"
                ) from exc
            try:
                installed_identity = anchor.api.path_info(
                    temporary, directory=True
                ).identity.as_tuple()
                anchor.api.move_noreplace(temporary, path)
                handle = anchor.api.open_handle(path, directory=True)
                installed = anchor.api.handle_info(handle)
                if installed.identity.as_tuple() != installed_identity:
                    anchor.api.close_handle(handle)
                    raise WorkspaceMutationError(
                        f"Created Windows workspace directory changed: {relative}"
                    )
                owns_visible_path = True
                # Keep the new directory name non-renamable through the rest of
                # the commit, including file install and journal persistence.
                anchor.held_directory_handles.append(handle)
                info = path.lstat()
                if windows_lstat_is_reparse(info) or not stat.S_ISDIR(info.st_mode):
                    raise WorkspaceMutationError(
                        f"Created Windows workspace directory is redirected: {relative}"
                    )
                try:
                    os.chmod(path, mode)
                except OSError:
                    pass
                created[relative] = installed_identity
            except Exception:
                while anchor.held_directory_handles:
                    anchor.api.close_handle(anchor.held_directory_handles.pop())
                if owns_visible_path:
                    try:
                        current = anchor.api.path_info(path, directory=True)
                        if current.identity.as_tuple() == installed_identity:
                            os.rmdir(path)
                    except OSError:
                        pass
                try:
                    os.rmdir(temporary)
                except OSError:
                    pass
                raise
            return

    with _open_parent_fd(workspace_fd, relative) as (parent_fd, name):
        try:
            os.mkdir(name, mode=mode, dir_fd=parent_fd)
        except FileExistsError as exc:
            raise WorkspaceMutationError(
                "A targeted output directory was created by another writer: "
                f"{relative}"
            ) from exc
        descriptor = -1
        try:
            descriptor = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
            info = os.fstat(descriptor)
            created[relative] = (info.st_dev, info.st_ino)
            _verify_directory_fd_reachable(workspace_fd, relative, descriptor)
            try:
                os.fsync(parent_fd)
            except OSError:
                pass
        finally:
            if descriptor >= 0:
                os.close(descriptor)


def _install_empty_directory_noreplace_at(
    workspace_fd: int,
    relative: str,
    *,
    mode: int,
) -> None:
    """Install one empty directory without touching an existing same-name entry."""

    if sys.platform == "win32":
        with _lock_windows_relative(workspace_fd, relative) as (anchor, target):
            temporary = target.parent / f"{_temporary_name(target.name)}.directory"
            try:
                os.mkdir(temporary, mode=0o700)
                try:
                    os.chmod(temporary, mode)
                except OSError:
                    pass
                anchor.api.move_noreplace(temporary, target)
            except FileExistsError as exc:
                raise WorkspaceMutationError(
                    "Deleted workspace directory was recreated by another writer; "
                    f"it was not modified: {relative}"
                ) from exc
            except WindowsGuardedFileError as exc:
                raise WorkspaceMutationError(
                    f"Could not restore Windows workspace directory: {relative}"
                ) from exc
            finally:
                try:
                    os.rmdir(temporary)
                except FileNotFoundError:
                    pass
            return

    with _open_parent_fd(workspace_fd, relative) as (parent_fd, target_name):
        temporary_name = f"{_temporary_name(target_name)}.directory"
        temporary_fd = -1
        try:
            os.mkdir(temporary_name, mode=0o700, dir_fd=parent_fd)
            temporary_fd = os.open(
                temporary_name,
                _directory_open_flags(),
                dir_fd=parent_fd,
            )
            os.fchmod(temporary_fd, mode)
            os.fsync(temporary_fd)
            os.close(temporary_fd)
            temporary_fd = -1
            try:
                _renameat_with_flags(
                    parent_fd,
                    temporary_name,
                    parent_fd,
                    target_name,
                    exchange=False,
                )
                os.fsync(parent_fd)
            except FileExistsError as exc:
                raise WorkspaceMutationError(
                    "Deleted workspace directory was recreated by another writer; "
                    f"it was not modified: {relative}"
                ) from exc
        finally:
            if temporary_fd >= 0:
                os.close(temporary_fd)
            try:
                # The temporary was never published when it still has this
                # name, so it is the one kind of transaction object safe to
                # remove automatically.
                os.rmdir(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass


def _verify_directory_fd_reachable(
    workspace_fd: int,
    relative: str,
    held_directory_fd: int,
) -> None:
    descriptor = os.dup(workspace_fd)
    try:
        for component in _relative_parts(relative):
            child = os.open(component, _directory_open_flags(), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        held = os.fstat(held_directory_fd)
        current = os.fstat(descriptor)
        if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
            raise WorkspaceMutationError(
                f"Workspace directory moved during transaction: {relative}"
            )
    except OSError as exc:
        raise WorkspaceMutationError(
            f"Workspace directory moved during transaction: {relative}"
        ) from exc
    finally:
        os.close(descriptor)


def _remove_directory_at(workspace_fd: int, relative: str) -> None:
    if sys.platform == "win32":
        anchor = _require_windows_anchor(workspace_fd)
        while anchor.held_directory_handles:
            anchor.api.close_handle(anchor.held_directory_handles.pop())
        with _lock_windows_relative(workspace_fd, relative) as (_anchor, path):
            os.rmdir(path)
            return
    with _open_parent_fd(workspace_fd, relative) as (parent_fd, name):
        os.rmdir(name, dir_fd=parent_fd)


def _fsync_parent_at(workspace_fd: int, relative: str) -> None:
    if sys.platform == "win32":
        # Prepared files are fsynced and MoveFileExW uses WRITE_THROUGH.  The
        # ReplaceFileW WRITE_THROUGH flag is explicitly unsupported by Win32.
        return
    with _open_parent_fd(workspace_fd, relative) as (parent_fd, _name):
        try:
            os.fsync(parent_fd)
        except OSError:
            pass


def _hash_regular_file(path: Path) -> tuple[str, int]:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise WorkspaceMutationError(f"Could not safely read workspace file: {path}") from exc
    digest = hashlib.sha256()
    copied = 0
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise WorkspaceMutationError(f"Workspace path is not a regular file: {path}")
        while True:
            chunk = os.read(descriptor, _COPY_CHUNK_BYTES)
            if not chunk:
                break
            copied += len(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or copied != after.st_size
        ):
            raise WorkspaceMutationError(f"Workspace file changed while being read: {path}")
        return digest.hexdigest(), copied
    finally:
        os.close(descriptor)


def _hardlink_preserving_copy() -> Callable[[str, str], str]:
    copied_inodes: dict[tuple[int, int], str] = {}

    def copy_regular_file(source: str, destination: str) -> str:
        # Preserve hard-link groups inside the private view so code that opens
        # either name observes normal POSIX semantics. ``copy2`` uses the
        # platform's efficient kernel copy path for the first member.
        info = os.stat(source, follow_symlinks=False)
        key = (info.st_dev, info.st_ino)
        existing = copied_inodes.get(key) if info.st_nlink > 1 else None
        if existing is not None:
            os.link(existing, destination)
            shutil.copystat(source, destination, follow_symlinks=False)
            return destination
        result = shutil.copy2(source, destination)
        if info.st_nlink > 1:
            copied_inodes[key] = destination
        return result

    return copy_regular_file


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if _path_is_redirected(path) or not path.is_dir():
        raise WorkspaceMutationError(f"Transaction storage is redirected: {path}")
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _validate_internal_sandbox_path(workspace: Path) -> None:
    internal = workspace / _INTERNAL_ROOT
    sandbox = internal / "sandbox"
    for path in (internal, sandbox):
        if not path.exists() and not path.is_symlink():
            continue
        if _path_is_redirected(path) or not path.is_dir():
            raise WorkspaceMutationError(
                "Sandbox scratch path contains a symlink or non-directory"
            )


def _path_is_redirected(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return path.is_symlink() or (
        sys.platform == "win32" and windows_lstat_is_reparse(info)
    )


def _safe_prefix(value: str) -> str:
    safe = "".join(character for character in value if character.isalnum() or character in "-_")
    return (safe or "command-")[:80]


def _validate_new_symlink(
    workspace: Path,
    relative: str,
    entry: WorkspaceEntry,
) -> None:
    target_value = entry.link_target or ""
    target = Path(target_value)
    logical_link = workspace / relative
    resolved = (
        target.resolve(strict=False)
        if target.is_absolute()
        else (logical_link.parent / target).resolve(strict=False)
    )
    try:
        resolved.relative_to(workspace)
    except ValueError as exc:
        raise WorkspaceMutationError(
            f"Command created a symbolic link outside the workspace: {relative}"
        ) from exc


def _workspace_mutation_records(
    *,
    changes: WorkspaceChangeSet,
    baseline: dict[str, WorkspaceEntry],
    final_entries: dict[str, WorkspaceEntry],
    version_by_path: dict[str, FileVersion],
) -> tuple[WorkspaceMutationRecord, ...]:
    """Build ordered evidence only after the guarded commit is durable."""

    records: list[WorkspaceMutationRecord] = []

    for relative in changes.created_directories:
        after = final_entries[relative]
        records.append(
            WorkspaceMutationRecord(
                relative_path=relative,
                operation="created",
                node_kind="directory",
                after_mode=after.mode,
            )
        )

    for relative in changes.writes:
        before = baseline.get(relative)
        after = final_entries[relative]
        version = version_by_path.get(relative)
        if before is not None and version is None:
            raise WorkspaceMutationError(
                f"Committed file has no retained before version: {relative}"
            )
        link_bytes = (
            os.fsencode(after.link_target or "")
            if after.kind == "symlink"
            else None
        )
        records.append(
            WorkspaceMutationRecord(
                relative_path=relative,
                operation="modified" if before is not None else "created",
                node_kind=after.kind,
                before_version_id=version.id if version is not None else None,
                before_sha256=before.sha256 if before is not None else None,
                before_mode=before.mode if before is not None else None,
                after_sha256=(
                    hashlib.sha256(link_bytes).hexdigest()
                    if link_bytes is not None
                    else after.sha256
                ),
                after_mode=after.mode,
                after_size=(len(link_bytes) if link_bytes is not None else after.size),
                link_target=(after.link_target if link_bytes is not None else None),
                link_target_b64=(
                    base64.b64encode(link_bytes).decode("ascii")
                    if link_bytes is not None
                    else None
                ),
            )
        )

    for relative in changes.deletes:
        before = baseline[relative]
        version = version_by_path.get(relative)
        if before.kind == "symlink":
            link_bytes = os.fsencode(before.link_target or "")
            records.append(
                WorkspaceMutationRecord(
                    relative_path=relative,
                    operation="deleted",
                    node_kind="symlink",
                    before_sha256=hashlib.sha256(link_bytes).hexdigest(),
                    before_mode=before.mode,
                    after_size=None,
                    link_target=before.link_target,
                    link_target_b64=base64.b64encode(link_bytes).decode("ascii"),
                )
            )
            continue
        if version is None:
            raise WorkspaceMutationError(
                f"Committed deletion has no retained before version: {relative}"
            )
        records.append(
            WorkspaceMutationRecord(
                relative_path=relative,
                operation="deleted",
                node_kind="file",
                before_version_id=version.id,
                before_sha256=before.sha256,
                before_mode=before.mode,
            )
        )

    for relative in changes.deleted_directories:
        before = baseline[relative]
        records.append(
            WorkspaceMutationRecord(
                relative_path=relative,
                operation="deleted",
                node_kind="directory",
                before_mode=before.mode,
            )
        )

    return tuple(records)


def _rollback_commit(
    *,
    workspace: Path,
    workspace_fd: int,
    store: FileVersionStore,
    version_by_path: dict[str, FileVersion],
    prepared: dict[str, _PreparedPath],
    deleted_backups: dict[str, _PreparedPath],
    applied_writes: list[str],
    applied_deletes: list[str],
    removed_directories: list[str],
    created_directories: dict[str, tuple[int, int]],
    baseline: dict[str, WorkspaceEntry],
    final_entries: dict[str, WorkspaceEntry],
    recovery_sidecars: list[_PreparedPath],
) -> Exception | None:
    del workspace, store, version_by_path  # durable versions are for process-crash recovery
    try:
        for relative in reversed(removed_directories):
            _install_empty_directory_noreplace_at(
                workspace_fd,
                relative,
                mode=baseline[relative].mode,
            )

        for relative in reversed(applied_writes):
            temporary = prepared[relative]
            if relative in baseline:
                if _prepared_nlink(workspace_fd, temporary) != 1:
                    raise WorkspaceMutationError(
                        "Rollback source gained a hard link; both objects were "
                        f"preserved: {relative} ({temporary.temporary_name})"
                    )
                _exchange_prepared_at(workspace_fd, temporary)
                displaced_after = _read_prepared_entry(workspace_fd, temporary)
                if (
                    displaced_after != final_entries[relative]
                    or _prepared_nlink(workspace_fd, temporary) != 1
                ):
                    raise WorkspaceMutationError(
                        "Rollback output conflicts with a later edit or hard link; "
                        f"both objects were preserved: {relative} "
                        f"({temporary.temporary_name})"
                    )
            else:
                recovery_sidecars.append(
                    _remove_relative_if_matches_at(
                        workspace_fd,
                        relative,
                        expected=final_entries[relative],
                    )
                )

        for relative in reversed(applied_deletes):
            backup = deleted_backups[relative]
            # The backup is the exact object atomically removed from the target.
            # Even when it proves to be a concurrent edit rather than baseline,
            # put that object back if the target name is still free.
            if _prepared_nlink(workspace_fd, backup) != 1:
                raise WorkspaceMutationError(
                    "Deleted rollback source gained a hard link; captured object "
                    f"was preserved: {relative} ({backup.temporary_name})"
                )
            try:
                _restore_backup_noreplace_at(workspace_fd, backup)
            except _WorkspaceParentMovedAfterOperation:
                # The no-replace rename completed before reachability failed;
                # the inode is visible again rather than a hidden sidecar.
                recovery_sidecars.remove(backup)
                raise
            recovery_sidecars.remove(backup)

        for relative in sorted(
            created_directories,
            key=lambda value: (-value.count("/"), value),
        ):
            if (
                _relative_inode_identity(workspace_fd, relative)
                != created_directories[relative]
            ):
                # A same-name directory installed by another writer is not ours
                # to remove, even when it is currently empty.
                continue
            try:
                _remove_directory_at(workspace_fd, relative)
            except (OSError, WorkspaceMutationError):
                pass
        return None
    except Exception as exc:  # pragma: no cover - fault-injection coverage owns this path
        return exc


def _persist_journal(path: Path, payload: dict[str, object]) -> None:
    atomic_write_text(
        path,
        # ASCII escaping is required for POSIX link targets decoded with
        # surrogateescape.  Loading the JSON reconstructs the exact surrogate
        # code points, which ``os.fsencode`` maps back to the original bytes.
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        mode=0o600,
    )


def recover_pending_workspace_transactions(
    *,
    storage_root: str | os.PathLike[str] | None = None,
    preserve_committed_checkpoint_journals: bool = False,
) -> list[str]:
    """Recover prepared command commits left by an interrupted backend.

    Call once during startup, before any Agent writer is registered.  A stage
    with no journal never touched the real workspace and is simply removed.  A
    committed journal keeps its outputs; a prepared journal rolls every
    displaced file back and removes only new paths whose bytes/link target still
    match the staged command result.
    """

    private_base = Path(
        storage_root
        if storage_root is not None
        else default_file_version_storage_root().parent
    ).expanduser()
    root = Path(os.path.abspath(private_base)) / "execution-transactions"
    if not root.exists():
        return []
    _require_guarded_workspace_mutation_support()
    if _path_is_redirected(root) or not root.is_dir():
        raise WorkspaceMutationError(f"Transaction recovery root is redirected: {root}")

    recovered: list[str] = []
    for workspace_root in sorted(root.iterdir()):
        if _path_is_redirected(workspace_root) or not workspace_root.is_dir():
            raise WorkspaceMutationError(
                f"Transaction recovery workspace root is redirected: {workspace_root}"
            )
        for transaction_root in sorted(workspace_root.iterdir()):
            if _path_is_redirected(transaction_root) or not transaction_root.is_dir():
                raise WorkspaceMutationError(
                    f"Transaction recovery entry is redirected: {transaction_root}"
                )
            journal_path = transaction_root / _JOURNAL_NAME
            if not journal_path.exists():
                shutil.rmtree(transaction_root)
                continue
            payload = _load_journal(journal_path)
            state = payload.get("state")
            if state == "committed":
                _cleanup_committed_journal_temporaries(
                    payload,
                    expected_workspace_key=workspace_root.name,
                )
                if (
                    preserve_committed_checkpoint_journals
                    and isinstance(payload.get("runtime_checkpoint"), dict)
                ):
                    recovered.append(transaction_root.name)
                    continue
                shutil.rmtree(transaction_root)
                recovered.append(transaction_root.name)
                continue
            if state == "rolled_back":
                shutil.rmtree(transaction_root)
                recovered.append(transaction_root.name)
                continue
            if state != "prepared":
                raise WorkspaceMutationError(
                    f"Transaction journal has an invalid state: {journal_path}"
                )
            _recover_prepared_journal(
                payload,
                expected_workspace_key=workspace_root.name,
            )
            shutil.rmtree(transaction_root)
            recovered.append(transaction_root.name)
        try:
            workspace_root.rmdir()
        except OSError:
            pass
    try:
        root.rmdir()
    except OSError:
        pass
    return recovered


def describe_workspace_recovery_blocker(
    token: str,
    payload: dict[str, object] | None,
    error: BaseException | str,
    *,
    trusted_provenance: bool = False,
) -> WorkspaceRecoveryBlocker:
    """Extract only conservative cleanup exclusions from an unsafe journal.

    A malformed value can increase the amount of deferred cleanup, but it can
    never grant authority to replay a journal.  Complete provenance requires
    all database owner fields used by checkpoint/rewind recovery, and the
    caller must separately have validated those fields against the ledger.
    Syntactically plausible values from a blocked journal are not proof.
    """

    checkpoint_ids: list[str] = []
    turn_run_ids: list[str] = []
    workspace_instance_ids: list[str] = []
    complete = False
    if isinstance(payload, dict):
        runtime = payload.get("runtime_checkpoint")
        if isinstance(runtime, dict):
            required = (
                "session_id",
                "checkpoint_id",
                "workspace_instance_id",
                "root_turn_id",
                "turn_run_id",
            )
            complete = all(
                isinstance(runtime.get(key), str) and bool(runtime[key])
                for key in required
            )
            checkpoint_id = runtime.get("checkpoint_id")
            if isinstance(checkpoint_id, str) and checkpoint_id:
                checkpoint_ids.append(checkpoint_id)
            turn_run_id = runtime.get("turn_run_id")
            if isinstance(turn_run_id, str) and turn_run_id:
                turn_run_ids.append(turn_run_id)
            workspace_instance_id = runtime.get("workspace_instance_id")
            if isinstance(workspace_instance_id, str) and workspace_instance_id:
                workspace_instance_ids.append(workspace_instance_id)

            action = runtime.get("action")
            raw_rewind_ids = runtime.get("rewind_checkpoint_ids")
            schema_version = payload.get("schema_version")
            if isinstance(schema_version, int) and schema_version < 4:
                action = "turn_commit"
                raw_rewind_ids = []
            if action not in {"turn_commit", "rewind"}:
                complete = False
            if not isinstance(raw_rewind_ids, list) or any(
                not isinstance(value, str) or not value
                for value in raw_rewind_ids
            ):
                complete = False
            else:
                checkpoint_ids.extend(str(value) for value in raw_rewind_ids)
                if action == "rewind" and (
                    not raw_rewind_ids or checkpoint_id not in raw_rewind_ids
                ):
                    complete = False
                if action == "turn_commit" and raw_rewind_ids:
                    complete = False

    reason = str(error)
    if not isinstance(error, str):
        reason = f"{type(error).__name__}: {error}"
    return WorkspaceRecoveryBlocker(
        token=token,
        checkpoint_ids=tuple(dict.fromkeys(checkpoint_ids)),
        turn_run_ids=tuple(dict.fromkeys(turn_run_ids)),
        workspace_instance_ids=tuple(dict.fromkeys(workspace_instance_ids)),
        provenance_unknown=not (complete and trusted_provenance),
        reason=reason,
    )


def recover_pending_workspace_transactions_isolated(
    *,
    storage_root: str | os.PathLike[str] | None = None,
    preserve_committed_checkpoint_journals: bool = False,
) -> WorkspaceTransactionRecoveryReport:
    """Recover independent transactions without letting one block startup.

    Unlike the strict API, every entry that cannot be proven safe is preserved
    byte-for-byte for a later retry or diagnosis.  This is the only variant the
    application startup path should call.
    """

    private_base = Path(
        storage_root
        if storage_root is not None
        else default_file_version_storage_root().parent
    ).expanduser()
    root = Path(os.path.abspath(private_base)) / "execution-transactions"
    if not root.exists():
        return WorkspaceTransactionRecoveryReport((), ())

    recovered: list[str] = []
    blocked: list[WorkspaceRecoveryBlocker] = []
    try:
        _require_guarded_workspace_mutation_support()
        if _path_is_redirected(root) or not root.is_dir():
            raise WorkspaceMutationError(
                f"Transaction recovery root is redirected: {root}"
            )
        workspace_roots = sorted(root.iterdir())
    except Exception as exc:
        blocker = describe_workspace_recovery_blocker("<recovery-root>", None, exc)
        logger.error("Workspace transaction recovery root was preserved: %s", exc)
        return WorkspaceTransactionRecoveryReport((), (blocker,))

    for workspace_root in workspace_roots:
        workspace_token = workspace_root.name
        try:
            if _path_is_redirected(workspace_root) or not workspace_root.is_dir():
                raise WorkspaceMutationError(
                    "Transaction recovery workspace root is redirected: "
                    f"{workspace_root}"
                )
            transaction_roots = sorted(workspace_root.iterdir())
        except Exception as exc:
            blocked.append(
                describe_workspace_recovery_blocker(workspace_token, None, exc)
            )
            logger.error(
                "Workspace transaction owner was preserved during startup: %s",
                exc,
            )
            continue

        for transaction_root in transaction_roots:
            token = f"{workspace_token}/{transaction_root.name}"
            payload: dict[str, object] | None = None
            try:
                if _path_is_redirected(transaction_root) or not transaction_root.is_dir():
                    raise WorkspaceMutationError(
                        "Transaction recovery entry is redirected: "
                        f"{transaction_root}"
                    )
                journal_path = transaction_root / _JOURNAL_NAME
                if not journal_path.exists():
                    raise WorkspaceMutationError(
                        f"Transaction recovery entry has no journal: {transaction_root}"
                    )
                payload = _load_journal(journal_path)
                state = payload.get("state")
                if state == "committed":
                    _cleanup_committed_journal_temporaries(
                        payload,
                        expected_workspace_key=workspace_token,
                    )
                    if (
                        preserve_committed_checkpoint_journals
                        and isinstance(payload.get("runtime_checkpoint"), dict)
                    ):
                        recovered.append(transaction_root.name)
                        continue
                    shutil.rmtree(transaction_root)
                    recovered.append(transaction_root.name)
                    continue
                if state == "rolled_back":
                    shutil.rmtree(transaction_root)
                    recovered.append(transaction_root.name)
                    continue
                if state != "prepared":
                    raise WorkspaceMutationError(
                        f"Transaction journal has an invalid state: {journal_path}"
                    )
                _recover_prepared_journal(
                    payload,
                    expected_workspace_key=workspace_token,
                )
                shutil.rmtree(transaction_root)
                recovered.append(transaction_root.name)
            except Exception as exc:
                blocked.append(
                    describe_workspace_recovery_blocker(token, payload, exc)
                )
                logger.error(
                    "Workspace transaction journal %s was preserved during startup: %s",
                    token,
                    exc,
                )

        try:
            workspace_root.rmdir()
        except OSError:
            pass
    try:
        root.rmdir()
    except OSError:
        pass
    return WorkspaceTransactionRecoveryReport(
        tuple(recovered),
        tuple(blocked),
    )


def _load_journal(path: Path) -> dict[str, object]:
    if _path_is_redirected(path) or not path.is_file():
        raise WorkspaceMutationError(f"Transaction journal is redirected: {path}")
    if path.stat().st_size > 20 * 1024 * 1024:
        raise WorkspaceMutationError(f"Transaction journal is too large: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkspaceMutationError(f"Transaction journal is unreadable: {path}") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") not in _SUPPORTED_JOURNAL_SCHEMA_VERSIONS
    ):
        raise WorkspaceMutationError(f"Transaction journal schema is invalid: {path}")
    return value


def list_committed_checkpoint_journals(
    *,
    storage_root: str | os.PathLike[str] | None = None,
) -> list[tuple[str, dict[str, object]]]:
    """Return retained committed journals for database-ledger recovery."""

    private_base = Path(
        storage_root
        if storage_root is not None
        else default_file_version_storage_root().parent
    ).expanduser()
    root = Path(os.path.abspath(private_base)) / "execution-transactions"
    if not root.exists():
        return []
    if _path_is_redirected(root) or not root.is_dir():
        raise WorkspaceMutationError(
            f"Transaction recovery root is redirected: {root}"
        )
    journals: list[tuple[str, dict[str, object]]] = []
    for workspace_root in sorted(root.iterdir()):
        if (
            not re.fullmatch(r"[0-9a-f]{64}", workspace_root.name)
            or _path_is_redirected(workspace_root)
            or not workspace_root.is_dir()
        ):
            raise WorkspaceMutationError(
                f"Checkpoint journal workspace owner is invalid: {workspace_root}"
            )
        for transaction_root in sorted(workspace_root.iterdir()):
            if (
                not re.fullmatch(r"tx-[A-Za-z0-9._-]+", transaction_root.name)
                or _path_is_redirected(transaction_root)
                or not transaction_root.is_dir()
            ):
                raise WorkspaceMutationError(
                    f"Checkpoint journal entry is invalid: {transaction_root}"
                )
            payload = _load_journal(transaction_root / _JOURNAL_NAME)
            if payload.get("state") != "committed":
                continue
            if not isinstance(payload.get("runtime_checkpoint"), dict):
                continue
            journals.append(
                (f"{workspace_root.name}/{transaction_root.name}", payload)
            )
    return journals


def scan_committed_checkpoint_journals_isolated(
    *,
    storage_root: str | os.PathLike[str] | None = None,
) -> CommittedCheckpointJournalScan:
    """List checkpoint journals while preserving and reporting bad siblings."""

    private_base = Path(
        storage_root
        if storage_root is not None
        else default_file_version_storage_root().parent
    ).expanduser()
    root = Path(os.path.abspath(private_base)) / "execution-transactions"
    if not root.exists():
        return CommittedCheckpointJournalScan((), ())

    journals: list[tuple[str, dict[str, object]]] = []
    blocked: list[WorkspaceRecoveryBlocker] = []
    try:
        if _path_is_redirected(root) or not root.is_dir():
            raise WorkspaceMutationError(
                f"Transaction recovery root is redirected: {root}"
            )
        workspace_roots = sorted(root.iterdir())
    except Exception as exc:
        blocker = describe_workspace_recovery_blocker("<recovery-root>", None, exc)
        logger.error("Checkpoint journal recovery root is blocked: %s", exc)
        return CommittedCheckpointJournalScan((), (blocker,))

    for workspace_root in workspace_roots:
        workspace_token = workspace_root.name
        try:
            if (
                not re.fullmatch(r"[0-9a-f]{64}", workspace_token)
                or _path_is_redirected(workspace_root)
                or not workspace_root.is_dir()
            ):
                raise WorkspaceMutationError(
                    f"Checkpoint journal workspace owner is invalid: {workspace_root}"
                )
            transaction_roots = sorted(workspace_root.iterdir())
        except Exception as exc:
            blocked.append(
                describe_workspace_recovery_blocker(workspace_token, None, exc)
            )
            logger.error("Checkpoint journal owner is blocked: %s", exc)
            continue

        for transaction_root in transaction_roots:
            token = f"{workspace_token}/{transaction_root.name}"
            payload: dict[str, object] | None = None
            try:
                if (
                    not re.fullmatch(r"tx-[A-Za-z0-9._-]+", transaction_root.name)
                    or _path_is_redirected(transaction_root)
                    or not transaction_root.is_dir()
                ):
                    raise WorkspaceMutationError(
                        f"Checkpoint journal entry is invalid: {transaction_root}"
                    )
                journal_path = transaction_root / _JOURNAL_NAME
                if not journal_path.exists():
                    raise WorkspaceMutationError(
                        f"Checkpoint journal entry has no journal: {transaction_root}"
                    )
                payload = _load_journal(journal_path)
                if (
                    payload.get("state") == "committed"
                    and isinstance(payload.get("runtime_checkpoint"), dict)
                ):
                    journals.append((token, payload))
                    continue
                raise WorkspaceMutationError(
                    "Retained transaction journal is not a committed checkpoint "
                    f"bridge: {journal_path}"
                )
            except Exception as exc:
                blocked.append(
                    describe_workspace_recovery_blocker(token, payload, exc)
                )
                logger.error("Checkpoint journal %s is blocked: %s", token, exc)

    return CommittedCheckpointJournalScan(
        tuple(journals),
        tuple(blocked),
    )


def committed_checkpoint_journal_metadata(
    payload: dict[str, object],
) -> tuple[dict[str, str], dict[str, object]]:
    """Reconstruct the same canonical mutation evidence returned at commit."""

    if payload.get("state") != "committed":
        raise WorkspaceMutationError("Checkpoint journal is not committed")
    action, _rewind_checkpoint_ids = committed_checkpoint_journal_action(payload)
    if action != "turn_commit":
        raise WorkspaceMutationError(
            "Rewind checkpoint journal cannot be replayed as a forward tool commit"
        )
    raw_runtime = payload.get("runtime_checkpoint")
    required_runtime = (
        "session_id",
        "message_id",
        "call_id",
        "root_turn_id",
        "turn_run_id",
        "checkpoint_id",
        "workspace_instance_id",
        "tool_operation",
    )
    if not isinstance(raw_runtime, dict) or not all(
        isinstance(raw_runtime.get(key), str) and bool(raw_runtime[key].strip())
        for key in required_runtime
    ):
        raise WorkspaceMutationError("Checkpoint journal runtime identity is invalid")
    runtime = {key: str(raw_runtime[key]) for key in required_runtime}

    existing = _journal_mapping(payload, "existing")
    existing_symlinks = _journal_mapping(payload, "existing_symlinks")
    if existing_symlinks:
        raise WorkspaceMutationError(
            "Forward checkpoint journal contains rewind-only symlink evidence"
        )
    new_paths = _journal_mapping(payload, "new_paths")
    created_directories = _journal_mapping(payload, "created_directories")
    deleted_directories = _journal_mapping(payload, "deleted_directories")
    records: list[WorkspaceMutationRecord] = []

    for relative in sorted(
        created_directories,
        key=lambda value: (value.count("/"), value),
    ):
        _validate_journal_relative(relative)
        raw = created_directories[relative]
        if not isinstance(raw, dict):
            raise WorkspaceMutationError(
                "Checkpoint journal created-directory proof is invalid"
            )
        mode = _validate_journal_mode(raw.get("mode"))
        records.append(
            WorkspaceMutationRecord(
                relative_path=relative,
                operation="created",
                node_kind="directory",
                after_mode=mode,
            )
        )

    writes: list[WorkspaceMutationRecord] = []
    deletes: list[WorkspaceMutationRecord] = []
    for relative, raw in existing.items():
        _validate_journal_relative(relative)
        if not isinstance(raw, dict):
            raise WorkspaceMutationError("Checkpoint journal existing entry is invalid")
        version_id = raw.get("version_id")
        if not isinstance(version_id, str) or not version_id:
            raise WorkspaceMutationError("Checkpoint journal version ID is invalid")
        before = _entry_from_journal(raw.get("before"))
        if before.kind != "file" or before.sha256 is None:
            raise WorkspaceMutationError(
                "Checkpoint journal before entry is not a regular file"
            )
        raw_after = raw.get("after")
        if raw_after is None:
            deletes.append(
                WorkspaceMutationRecord(
                    relative_path=relative,
                    operation="deleted",
                    node_kind="file",
                    before_version_id=version_id,
                    before_sha256=before.sha256,
                    before_mode=before.mode,
                )
            )
            continue
        after = _entry_from_journal(raw_after)
        if after.kind != "file" or after.sha256 is None:
            raise WorkspaceMutationError(
                "Checkpoint journal after entry is not a regular file"
            )
        writes.append(
            WorkspaceMutationRecord(
                relative_path=relative,
                operation="modified",
                node_kind="file",
                before_version_id=version_id,
                before_sha256=before.sha256,
                before_mode=before.mode,
                after_sha256=after.sha256,
                after_mode=after.mode,
                after_size=after.size,
            )
        )

    for relative, raw in new_paths.items():
        _validate_journal_relative(relative)
        after = _entry_from_journal(raw)
        if after.kind not in {"file", "symlink"}:
            raise WorkspaceMutationError(
                "Checkpoint journal new path has an unsupported type"
            )
        link_bytes = (
            os.fsencode(after.link_target or "")
            if after.kind == "symlink"
            else None
        )
        writes.append(
            WorkspaceMutationRecord(
                relative_path=relative,
                operation="created",
                node_kind=after.kind,
                after_sha256=(
                    hashlib.sha256(link_bytes).hexdigest()
                    if link_bytes is not None
                    else after.sha256
                ),
                after_mode=after.mode,
                after_size=len(link_bytes) if link_bytes is not None else after.size,
                link_target=after.link_target if link_bytes is not None else None,
                link_target_b64=(
                    base64.b64encode(link_bytes).decode("ascii")
                    if link_bytes is not None
                    else None
                ),
            )
        )
    records.extend(sorted(writes, key=lambda item: item.relative_path))
    records.extend(sorted(deletes, key=lambda item: item.relative_path))

    for relative in sorted(
        deleted_directories,
        key=lambda value: (-value.count("/"), value),
    ):
        _validate_journal_relative(relative)
        records.append(
            WorkspaceMutationRecord(
                relative_path=relative,
                operation="deleted",
                node_kind="directory",
                before_mode=_validate_journal_mode(deleted_directories[relative]),
            )
        )

    return runtime, {
        "workspace_transaction": True,
        "workspace_changes_committed": True,
        "workspace_mutations": [item.metadata for item in records],
    }


def committed_checkpoint_journal_action(
    payload: dict[str, object],
) -> tuple[Literal["turn_commit", "rewind"], tuple[str, ...]]:
    """Validate and return the durable database action owned by a journal.

    Schema 1-3 journals predate rewind and are unambiguously ordinary tool
    commits.  Schema 4 and later record the action explicitly so startup
    recovery can never append reverse mutations to the forward checkpoint
    ledger.
    """

    if payload.get("state") != "committed":
        raise WorkspaceMutationError("Checkpoint journal is not committed")
    schema_version = payload.get("schema_version")
    if (
        type(schema_version) is not int
        or schema_version not in _SUPPORTED_JOURNAL_SCHEMA_VERSIONS
    ):
        raise WorkspaceMutationError("Checkpoint journal schema is invalid")
    raw_runtime = payload.get("runtime_checkpoint")
    if not isinstance(raw_runtime, dict):
        raise WorkspaceMutationError("Checkpoint journal runtime identity is invalid")
    if schema_version < 4:
        if "action" in raw_runtime or "rewind_checkpoint_ids" in raw_runtime:
            raise WorkspaceMutationError(
                "Legacy checkpoint journal contains unsupported action metadata"
            )
        action: object = "turn_commit"
        raw_ids: object = []
    else:
        if "action" not in raw_runtime or "rewind_checkpoint_ids" not in raw_runtime:
            raise WorkspaceMutationError(
                "Checkpoint journal action metadata is incomplete"
            )
        action = raw_runtime["action"]
        raw_ids = raw_runtime["rewind_checkpoint_ids"]
    if action not in {"turn_commit", "rewind"}:
        raise WorkspaceMutationError("Checkpoint journal action is invalid")
    if not isinstance(raw_ids, list) or any(
        not isinstance(value, str) or not value.strip() or len(value) > 200
        for value in raw_ids
    ):
        raise WorkspaceMutationError("Checkpoint journal rewind identity list is invalid")
    checkpoint_ids = tuple(str(value) for value in raw_ids)
    if len(checkpoint_ids) > 500 or len(set(checkpoint_ids)) != len(checkpoint_ids):
        raise WorkspaceMutationError("Checkpoint journal rewind identity list is invalid")
    target_checkpoint_id = raw_runtime.get("checkpoint_id")
    if action == "rewind":
        if (
            not checkpoint_ids
            or not isinstance(target_checkpoint_id, str)
            or target_checkpoint_id not in checkpoint_ids
        ):
            raise WorkspaceMutationError(
                "Rewind checkpoint journal does not include its target"
            )
    elif checkpoint_ids:
        raise WorkspaceMutationError(
            "Turn-commit checkpoint journal unexpectedly contains rewind identities"
        )
    return action, checkpoint_ids


def cleanup_committed_checkpoint_journal(
    token: str,
    *,
    expected_checkpoint_id: str,
    storage_root: str | os.PathLike[str] | None = None,
) -> None:
    """Delete a crash bridge only after its database ledger is committed."""

    parts = str(token).split("/")
    if (
        len(parts) != 2
        or not re.fullmatch(r"[0-9a-f]{64}", parts[0])
        or not re.fullmatch(r"tx-[A-Za-z0-9._-]+", parts[1])
    ):
        raise WorkspaceMutationError("Checkpoint journal token is invalid")
    private_base = Path(
        storage_root
        if storage_root is not None
        else default_file_version_storage_root().parent
    ).expanduser()
    recovery_root = Path(os.path.abspath(private_base)) / "execution-transactions"
    workspace_root = recovery_root / parts[0]
    transaction_root = workspace_root / parts[1]
    if _path_is_redirected(transaction_root) or not transaction_root.is_dir():
        raise WorkspaceMutationError("Checkpoint journal is unavailable")
    payload = _load_journal(transaction_root / _JOURNAL_NAME)
    runtime = payload.get("runtime_checkpoint")
    if (
        payload.get("state") != "committed"
        or not isinstance(runtime, dict)
        or runtime.get("checkpoint_id") != expected_checkpoint_id
    ):
        raise WorkspaceMutationError("Checkpoint journal ownership does not match")
    shutil.rmtree(transaction_root)
    try:
        workspace_root.rmdir()
    except OSError:
        pass
    try:
        recovery_root.rmdir()
    except OSError:
        pass


def _recover_prepared_journal(
    payload: dict[str, object],
    *,
    expected_workspace_key: str,
) -> None:
    raw_workspace = payload.get("workspace")
    if not isinstance(raw_workspace, str) or not raw_workspace:
        raise WorkspaceMutationError("Transaction journal has no workspace")
    try:
        workspace = validate_workspace_private_boundary(raw_workspace)
    except (OSError, RuntimeError) as exc:
        raise WorkspaceMutationError(
            f"Transaction recovery workspace is unavailable: {raw_workspace}"
        ) from exc
    if not workspace.is_dir():
        raise WorkspaceMutationError(
            f"Transaction recovery workspace does not exist: {workspace}"
        )
    actual_workspace_key = hashlib.sha256(os.fsencode(str(workspace))).hexdigest()
    if actual_workspace_key != expected_workspace_key:
        raise WorkspaceMutationError(
            "Transaction journal workspace does not match its private storage scope"
        )

    with _workspace_commit_lock(actual_workspace_key):
        recovery_identity = _journal_recovery_workspace_identity(payload, workspace)
        with _open_workspace_root_fd(
            workspace,
            expected_identity=recovery_identity.volatile_identity,
        ) as workspace_fd:
            if (
                _journal_recovery_workspace_identity(payload, workspace)
                != recovery_identity
            ):
                raise WorkspaceMutationError(
                    "Workspace root changed during transaction recovery"
                )
            _recover_prepared_journal_with_fd(
                payload,
                workspace,
                workspace_fd,
                recovery_identity=recovery_identity,
            )
            _assert_workspace_path_identity(
                workspace,
                recovery_identity.volatile_identity,
            )
            current_identity = _journal_recovery_workspace_identity(
                payload,
                workspace,
            )
            if current_identity != recovery_identity:
                raise WorkspaceMutationError(
                    "Workspace root changed during transaction recovery"
                )


def _cleanup_committed_journal_temporaries(
    payload: dict[str, object],
    *,
    expected_workspace_key: str,
) -> None:
    raw_workspace = payload.get("workspace")
    if not isinstance(raw_workspace, str) or not raw_workspace:
        raise WorkspaceMutationError("Transaction journal has no workspace")
    workspace = validate_workspace_private_boundary(raw_workspace)
    actual_workspace_key = hashlib.sha256(os.fsencode(str(workspace))).hexdigest()
    if actual_workspace_key != expected_workspace_key:
        raise WorkspaceMutationError(
            "Transaction journal workspace does not match its private storage scope"
        )
    existing = _journal_mapping(payload, "existing")
    existing_symlinks = _journal_mapping(payload, "existing_symlinks")
    new_paths = _journal_mapping(payload, "new_paths")
    temporary_paths = _journal_mapping(payload, "temporary_paths")
    if not temporary_paths:
        with _workspace_commit_lock(actual_workspace_key):
            _journal_recovery_workspace_identity(payload, workspace)
        return
    acceptable: dict[str, set[WorkspaceEntry]] = {}
    for relative, raw in existing.items():
        _validate_journal_relative(relative)
        if not isinstance(raw, dict):
            raise WorkspaceMutationError("Transaction journal existing entry is invalid")
        before = _entry_from_journal(raw.get("before"))
        after_raw = raw.get("after")
        after = _entry_from_journal(after_raw) if after_raw is not None else None
        acceptable[relative] = {entry for entry in (before, after) if entry is not None}
    for relative, raw in existing_symlinks.items():
        _validate_journal_relative(relative)
        if not isinstance(raw, dict):
            raise WorkspaceMutationError(
                "Transaction journal existing symlink entry is invalid"
            )
        before = _entry_from_journal(raw.get("before"))
        if before.kind != "symlink" or raw.get("after") is not None:
            raise WorkspaceMutationError(
                "Transaction journal existing symlink entry is invalid"
            )
        acceptable[relative] = {before}
    for relative, raw in new_paths.items():
        _validate_journal_relative(relative)
        acceptable[relative] = {_entry_from_journal(raw)}
    if set(temporary_paths) != set(acceptable):
        raise WorkspaceMutationError("Transaction journal temporary-path map is incomplete")
    with _workspace_commit_lock(actual_workspace_key):
        recovery_identity = _journal_recovery_workspace_identity(payload, workspace)
        with _open_workspace_root_fd(
            workspace,
            expected_identity=recovery_identity.volatile_identity,
        ) as workspace_fd:
            if (
                _journal_recovery_workspace_identity(payload, workspace)
                != recovery_identity
            ):
                raise WorkspaceMutationError(
                    "Workspace root changed during transaction recovery"
                )
            for relative, raw_name in temporary_paths.items():
                _validate_journal_temporary_name(raw_name)
                sidecar = _PreparedPath(relative=relative, temporary_name=raw_name)
                if _read_prepared_entry(workspace_fd, sidecar) is not None:
                    logger.warning(
                        "Preserving committed transaction recovery sidecar: %s",
                        _recovery_sidecar_path(workspace, sidecar),
                    )
            current_identity = _journal_recovery_workspace_identity(
                payload,
                workspace,
            )
            if current_identity != recovery_identity:
                raise WorkspaceMutationError(
                    "Workspace root changed during transaction recovery"
                )


def _recover_prepared_journal_with_fd(
    payload: dict[str, object],
    workspace: Path,
    workspace_fd: int,
    *,
    recovery_identity: _JournalRecoveryIdentity,
) -> None:

    existing = _journal_mapping(payload, "existing")
    existing_symlinks = _journal_mapping(payload, "existing_symlinks")
    new_paths = _journal_mapping(payload, "new_paths")
    created_directories = _journal_mapping(payload, "created_directories")
    deleted_directories = _journal_mapping(payload, "deleted_directories")
    temporary_paths = _journal_mapping(payload, "temporary_paths")
    version_ids: list[str] = []
    expected_current: dict[str, dict[str, object] | None] = {}
    acceptable_temporaries: dict[str, set[WorkspaceEntry]] = {}
    expected_replacements: dict[str, WorkspaceEntry] = {}

    if sys.platform == "win32":
        _recover_windows_prepared_replace_gaps(
            workspace_fd,
            existing=existing,
            existing_symlinks=existing_symlinks,
            new_paths=new_paths,
            temporary_paths=temporary_paths,
        )

    # Validate every observable path before the first recovery mutation.  A
    # third-party edit after the crash is never overwritten by automation.
    for relative, raw in existing.items():
        _validate_journal_relative(relative)
        if not isinstance(raw, dict):
            raise WorkspaceMutationError("Transaction journal existing entry is invalid")
        version_id = raw.get("version_id")
        if not isinstance(version_id, str) or not version_id:
            raise WorkspaceMutationError("Transaction journal version ID is invalid")
        before = _entry_from_journal(raw.get("before"))
        after_raw = raw.get("after")
        after = _entry_from_journal(after_raw) if after_raw is not None else None
        current = _read_entry_at_relative(workspace_fd, relative)
        if current != before and current != after:
            raise WorkspaceMutationError(
                f"Interrupted command output conflicts with a later edit: {relative}"
            )
        if current != before:
            version_ids.append(version_id)
            expected_current[relative] = asdict(after) if after is not None else None
        acceptable_temporaries[relative] = {
            entry for entry in (before, after) if entry is not None
        }
        if after is not None:
            expected_replacements[relative] = after

    symlinks_to_restore: dict[str, tuple[WorkspaceEntry, _PreparedPath]] = {}
    for relative, raw in existing_symlinks.items():
        _validate_journal_relative(relative)
        if not isinstance(raw, dict):
            raise WorkspaceMutationError(
                "Transaction journal existing symlink entry is invalid"
            )
        before = _entry_from_journal(raw.get("before"))
        if before.kind != "symlink" or raw.get("after") is not None:
            raise WorkspaceMutationError(
                "Transaction journal existing symlink entry is invalid"
            )
        current = _read_entry_at_relative(workspace_fd, relative)
        if current is not None and current != before:
            raise WorkspaceMutationError(
                "Interrupted rewind symlink conflicts with a later edit: "
                f"{relative}"
            )
        temporary_name = _validate_journal_temporary_name(
            temporary_paths.get(relative)
        )
        backup = _PreparedPath(
            relative=relative,
            temporary_name=temporary_name,
        )
        if current is None:
            if (
                _read_prepared_entry(workspace_fd, backup) != before
                or _prepared_nlink(workspace_fd, backup) != 1
            ):
                raise WorkspaceMutationError(
                    "Interrupted rewind symlink has no exact recovery backup: "
                    f"{relative}"
                )
            symlinks_to_restore[relative] = (before, backup)
        acceptable_temporaries[relative] = {before}

    parsed_new: dict[str, WorkspaceEntry] = {}
    for relative, raw in new_paths.items():
        _validate_journal_relative(relative)
        expected = _entry_from_journal(raw)
        current = _read_entry_at_relative(workspace_fd, relative)
        if current is not None and current != expected:
            raise WorkspaceMutationError(
                f"Interrupted new command output conflicts with a later edit: {relative}"
            )
        parsed_new[relative] = expected
        acceptable_temporaries[relative] = {expected}
        expected_replacements[relative] = expected

    parsed_created_directories: dict[
        str,
        tuple[int, tuple[int, int] | None],
    ] = {}
    for relative, raw_proof in created_directories.items():
        _validate_journal_relative(relative)
        parsed_created_directories[relative] = _created_directory_proof_from_journal(
            raw_proof
        )
    parsed_deleted_directories: dict[str, int] = {}
    for relative, raw_mode in deleted_directories.items():
        _validate_journal_relative(relative)
        parsed_deleted_directories[relative] = _validate_journal_mode(raw_mode)

    # Recreate removed empty directories with a no-replace install.  A same-name
    # directory that survived the crash is accepted only when its full entry
    # already matches; recovery never chmods an existing user object.
    for relative, raw_mode in parsed_deleted_directories.items():
        expected_directory = WorkspaceEntry(kind="directory", mode=raw_mode)
        current = _read_entry_at_relative(workspace_fd, relative)
        if current is None:
            _install_empty_directory_noreplace_at(
                workspace_fd,
                relative,
                mode=raw_mode,
            )
        elif current != expected_directory:
            raise WorkspaceMutationError(
                "Interrupted deleted directory conflicts with a later recreation; "
                f"it was not modified: {relative}"
            )

    store = FileVersionStore(
        workspace,
        expected_workspace_identity=recovery_identity.volatile_identity,
        expected_durable_workspace_identity=recovery_identity.durable_token,
        legacy_workspace_identity=recovery_identity.legacy_identity,
    )
    if version_ids:
        try:
            store.restore_failed_mutation_batch(
                version_ids,
                expected_current=expected_current,
            )
        except FileVersionError as exc:
            raise WorkspaceMutationError(str(exc)) from exc

    for relative, (before, backup) in symlinks_to_restore.items():
        if _read_entry_at_relative(workspace_fd, relative) is not None:
            raise WorkspaceMutationError(
                f"Interrupted rewind symlink was recreated: {relative}"
            )
        _restore_backup_noreplace_at(workspace_fd, backup)
        if _read_entry_at_relative(workspace_fd, relative) != before:
            raise WorkspaceMutationError(
                f"Interrupted rewind restored a different symlink: {relative}"
            )
        _fsync_parent_at(workspace_fd, relative)

    for relative, expected in parsed_new.items():
        current = _read_entry_at_relative(workspace_fd, relative)
        if current is None:
            continue
        if current != expected:
            raise WorkspaceMutationError(
                f"Interrupted new command output conflicts with a later edit: {relative}"
            )
        quarantine = _remove_relative_if_matches_at(
            workspace_fd,
            relative,
            expected=expected,
        )
        logger.warning(
            "Preserving interrupted-new-file recovery sidecar: %s",
            _recovery_sidecar_path(workspace, quarantine),
        )
        _fsync_parent_at(workspace_fd, relative)

    if temporary_paths and set(temporary_paths) != set(acceptable_temporaries):
        raise WorkspaceMutationError("Transaction journal temporary-path map is incomplete")
    for relative, raw_name in temporary_paths.items():
        _validate_journal_temporary_name(raw_name)
        sidecar = _PreparedPath(relative=relative, temporary_name=raw_name)
        if _read_prepared_entry(workspace_fd, sidecar) is not None:
            # A prepared journal cannot prove whether this inode was only
            # staged or once occupied the visible destination. Preserve it.
            logger.warning(
                "Preserving prepared transaction recovery sidecar: %s",
                _recovery_sidecar_path(workspace, sidecar),
            )
        if sys.platform == "win32":
            replacement = _PreparedPath(
                relative=relative,
                temporary_name=f"{raw_name}.replacement",
            )
            replacement_entry = _read_prepared_entry(workspace_fd, replacement)
            if replacement_entry is None:
                continue
            expected_replacement = expected_replacements.get(relative)
            # A still-named replacement was never installed: ReplaceFileW and
            # MoveFileExW consume that name on success.  A crashed process has
            # no live descriptor left, so exact output bytes with one link can
            # be quarantined and removed.  Anything else is preserved.
            if expected_replacement is None:
                logger.warning(
                    "Preserving unexpected Windows transaction replacement: %s",
                    _recovery_sidecar_path(workspace, replacement),
                )
                continue
            try:
                _remove_owned_temporary_if_matches_at(
                    workspace_fd,
                    replacement,
                    acceptable={expected_replacement},
                )
            except WorkspaceMutationError:
                logger.warning(
                    "Preserving changed Windows transaction replacement: %s",
                    _recovery_sidecar_path(workspace, replacement),
                )
                raise

    for relative, (expected_mode, expected_identity) in sorted(
        parsed_created_directories.items(),
        key=lambda item: (-item[0].count("/"), item[0]),
    ):
        if expected_identity is None:
            logger.warning(
                "Preserving unproven interrupted-transaction directory: %s",
                workspace / relative,
            )
            continue
        current_identity = _relative_inode_identity(workspace_fd, relative)
        current_entry = _read_entry_at_relative(workspace_fd, relative)
        if (
            current_identity != expected_identity
            or current_entry != WorkspaceEntry(kind="directory", mode=expected_mode)
        ):
            if current_entry is not None:
                logger.warning(
                    "Preserving replaced or modified interrupted-transaction "
                    "directory: %s",
                    workspace / relative,
                )
            continue
        try:
            _remove_directory_at(workspace_fd, relative)
            _fsync_parent_at(workspace_fd, relative)
        except FileNotFoundError:
            pass
        except OSError:
            # A user-created file that was not part of the interrupted command
            # keeps the directory non-empty; preserving it is the safe result.
            pass


def _recover_windows_prepared_replace_gaps(
    workspace_fd: int,
    *,
    existing: dict[str, object],
    existing_symlinks: dict[str, object],
    new_paths: dict[str, object],
    temporary_paths: dict[str, object],
) -> None:
    """Close a documented ReplaceFileW 1177 target-name gap after a crash.

    With a backup requested, error 1177 leaves the old target at the backup
    name and the prepared output at its original replacement name.  Requiring
    both exact journal entries distinguishes that kernel state from a user who
    deleted an already-installed command output after the crash.
    """

    if existing_symlinks:
        raise WorkspaceMutationError(
            "Windows recovery does not accept POSIX rewind symlink journals"
        )
    expected_paths = set(existing) | set(new_paths)
    if set(temporary_paths) != expected_paths:
        raise WorkspaceMutationError(
            "Transaction journal temporary-path map is incomplete"
        )

    # Validate the entire name-bearing payload before the first recovery move.
    parsed_existing: dict[str, tuple[WorkspaceEntry, WorkspaceEntry | None, str]] = {}
    for relative, raw in existing.items():
        _validate_journal_relative(relative)
        if not isinstance(raw, dict):
            raise WorkspaceMutationError(
                "Transaction journal existing entry is invalid"
            )
        before = _entry_from_journal(raw.get("before"))
        after_raw = raw.get("after")
        after = _entry_from_journal(after_raw) if after_raw is not None else None
        temporary_name = _validate_journal_temporary_name(
            temporary_paths.get(relative)
        )
        parsed_existing[relative] = (before, after, temporary_name)
    for relative, raw in new_paths.items():
        _validate_journal_relative(relative)
        _entry_from_journal(raw)
        _validate_journal_temporary_name(temporary_paths.get(relative))

    for relative, (before, after, temporary_name) in parsed_existing.items():
        if after is None or _read_entry_at_relative(workspace_fd, relative) is not None:
            continue
        backup = _PreparedPath(relative=relative, temporary_name=temporary_name)
        replacement = _PreparedPath(
            relative=relative,
            temporary_name=f"{temporary_name}.replacement",
        )
        if (
            _read_prepared_entry(workspace_fd, backup) != before
            or _prepared_nlink(workspace_fd, backup) != 1
            or _read_prepared_entry(workspace_fd, replacement) != after
            or _prepared_nlink(workspace_fd, replacement) != 1
        ):
            # The ordinary conflict check below will fail closed while leaving
            # every observable object untouched for manual recovery.
            continue
        _restore_backup_noreplace_at(workspace_fd, backup)
        if _read_entry_at_relative(workspace_fd, relative) != before:
            raise WorkspaceMutationError(
                "Interrupted Windows replacement recovered a different target: "
                f"{relative}"
            )
        _fsync_parent_at(workspace_fd, relative)
        logger.warning(
            "Recovered interrupted Windows ReplaceFileW target-name gap: %s",
            workspace_fd.root / relative
            if isinstance(workspace_fd, _WindowsWorkspaceAnchor)
            else relative,
        )

def _journal_mapping(
    payload: dict[str, object],
    key: str,
) -> dict[str, object]:
    value = payload.get(key, {})
    if not isinstance(value, dict):
        raise WorkspaceMutationError(f"Transaction journal field is invalid: {key}")
    if len(value) > MAX_STAGED_ENTRIES:
        raise WorkspaceMutationError(f"Transaction journal field is too large: {key}")
    return {str(item_key): item_value for item_key, item_value in value.items()}


def _entry_from_journal(value: object) -> WorkspaceEntry:
    if not isinstance(value, dict):
        raise WorkspaceMutationError("Transaction journal file entry is invalid")
    try:
        entry = WorkspaceEntry(
            kind=value["kind"],
            mode=int(value["mode"]),
            size=int(value.get("size", 0)),
            sha256=value.get("sha256"),
            link_target=value.get("link_target"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkspaceMutationError("Transaction journal file entry is invalid") from exc
    if entry.kind not in {"file", "directory", "symlink"}:
        raise WorkspaceMutationError("Transaction journal entry kind is invalid")
    _validate_journal_mode(entry.mode)
    if entry.size < 0 or entry.size > MAX_STAGED_FILE_BYTES:
        raise WorkspaceMutationError("Transaction journal entry size is invalid")
    if entry.kind == "file":
        if (
            not isinstance(entry.sha256, str)
            or len(entry.sha256) != 64
            or any(character not in "0123456789abcdef" for character in entry.sha256)
        ):
            raise WorkspaceMutationError("Transaction journal checksum is invalid")
    elif entry.sha256 is not None:
        raise WorkspaceMutationError("Transaction journal non-file checksum is invalid")
    if entry.kind == "symlink" and not isinstance(entry.link_target, str):
        raise WorkspaceMutationError("Transaction journal symlink target is invalid")
    return entry


def _validate_journal_relative(value: str) -> None:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise WorkspaceMutationError(f"Transaction journal path is unsafe: {value!r}")
    if sys.platform == "win32":
        try:
            validate_windows_relative_name(value)
        except ValueError as exc:
            raise WorkspaceMutationError(
                f"Transaction journal Windows path is unsafe: {value!r}"
            ) from exc


def _validate_journal_temporary_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or Path(value).name != value
        or "/" in value
        or "\\" in value
    ):
        raise WorkspaceMutationError("Transaction journal temporary path is invalid")
    if sys.platform == "win32":
        try:
            validate_windows_relative_name(value)
        except ValueError as exc:
            raise WorkspaceMutationError(
                "Transaction journal Windows temporary path is invalid"
            ) from exc
    return value


def _validate_journal_mode(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 0o7777:
        raise WorkspaceMutationError("Transaction journal filesystem mode is invalid")
    return value


def _created_directory_proof_from_journal(
    value: object,
) -> tuple[int, tuple[int, int] | None]:
    # Schema v1 stored only a mode.  That is not proof that a same-name
    # directory observed after a crash was created by this transaction, so
    # legacy entries are deliberately preserved.
    if isinstance(value, int) and not isinstance(value, bool):
        return _validate_journal_mode(value), None
    if not isinstance(value, dict):
        raise WorkspaceMutationError(
            "Transaction journal created-directory proof is invalid"
        )
    mode = _validate_journal_mode(value.get("mode"))
    raw_identity = value.get("identity")
    if raw_identity is None:
        return mode, None
    if not isinstance(raw_identity, dict):
        raise WorkspaceMutationError(
            "Transaction journal created-directory identity is invalid"
        )
    dev = raw_identity.get("dev")
    ino = raw_identity.get("ino")
    if (
        not isinstance(dev, int)
        or isinstance(dev, bool)
        or dev < 0
        or not isinstance(ino, int)
        or isinstance(ino, bool)
        or ino <= 0
    ):
        raise WorkspaceMutationError(
            "Transaction journal created-directory identity is invalid"
        )
    return mode, (dev, ino)


def _journal_workspace_identity(payload: dict[str, object]) -> tuple[int, int]:
    value = payload.get("workspace_identity")
    if not isinstance(value, dict):
        raise WorkspaceMutationError("Transaction journal workspace identity is invalid")
    dev = value.get("dev")
    ino = value.get("ino")
    if (
        not isinstance(dev, int)
        or isinstance(dev, bool)
        or dev < 0
        or not isinstance(ino, int)
        or isinstance(ino, bool)
        or ino <= 0
    ):
        raise WorkspaceMutationError("Transaction journal workspace identity is invalid")
    return dev, ino


def _journal_durable_workspace_token(payload: dict[str, object]) -> str:
    value = payload.get("workspace_identity")
    if not isinstance(value, dict):
        raise WorkspaceMutationError(
            "Transaction journal workspace identity is invalid"
        )
    token = value.get("token")
    if not isinstance(token, str) or not (
        re.fullmatch(r"marker-v2:[0-9a-f]{64}", token)
        or re.fullmatch(r"winfile-v2:[0-9]+:[0-9]+", token)
    ):
        raise WorkspaceMutationError(
            "Transaction journal durable workspace identity is invalid"
        )
    return token


def _journal_recovery_workspace_identity(
    payload: dict[str, object],
    workspace: Path,
) -> _JournalRecoveryIdentity:
    """Validate journal ownership and return a live native root identity.

    Schema 5 journals are owned by the app marker.  Their persisted native
    tuple documents the operation that wrote the journal, but is deliberately
    not compared during startup: APFS can assign a different device number
    after a valid reboot/remount.  The marker is checked first and the current
    native tuple is then pinned for the recovery operation.

    Schemas 1-4 have no durable marker.  Their native tuple must match exactly.
    An inode by itself is not durable proof of ownership across a reboot,
    remount, restore, or volume replacement, so ambiguous legacy journals are
    preserved for diagnosis instead of being replayed automatically.
    """

    schema_version = payload.get("schema_version")
    if (
        type(schema_version) is not int
        or schema_version not in _SUPPORTED_JOURNAL_SCHEMA_VERSIONS
    ):
        raise WorkspaceMutationError("Transaction journal schema is invalid")
    journal_identity = _journal_workspace_identity(payload)
    if schema_version == 5:
        durable_token = _journal_durable_workspace_token(payload)
        try:
            current = inspect_workspace_identity(workspace)
        except WorkspaceIdentityError as exc:
            raise WorkspaceMutationError(
                "Workspace root changed before transaction recovery"
            ) from exc
        if (
            current.canonical_path != workspace
            or current.durable_token != durable_token
        ):
            raise WorkspaceMutationError(
                "Workspace root changed before transaction recovery"
            )
        return _JournalRecoveryIdentity(
            volatile_identity=current.volatile_identity,
            durable_token=durable_token,
            legacy_identity=None,
        )

    current_identity = _path_identity(workspace, directory=True)
    if current_identity != journal_identity:
        raise WorkspaceMutationError(
            "Workspace root changed before transaction recovery"
        )
    return _JournalRecoveryIdentity(
        volatile_identity=current_identity,
        durable_token=None,
        legacy_identity=journal_identity,
    )


__all__ = [
    "CommittedCheckpointJournalScan",
    "WorkspaceRecoveryBlocker",
    "WorkspaceTransactionRecoveryReport",
    "cleanup_committed_checkpoint_journal",
    "committed_checkpoint_journal_action",
    "committed_checkpoint_journal_metadata",
    "describe_workspace_recovery_blocker",
    "list_committed_checkpoint_journals",
    "scan_committed_checkpoint_journals_isolated",
    "WorkspaceChangeSet",
    "WorkspaceCommitResult",
    "WorkspaceMutationRecord",
    "WorkspaceMutationError",
    "WorkspacePrecommitSealError",
    "WorkspaceMutationTransaction",
    "recover_pending_workspace_transactions",
    "recover_pending_workspace_transactions_isolated",
]
