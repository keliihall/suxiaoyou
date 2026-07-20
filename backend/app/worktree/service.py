"""Conservative Git-worktree lifecycle service for the v1.1 Beta.

The service owns only directories it created below an application-private
management root.  Checkout removal is delegated to ``git worktree remove``;
Python never recursively deletes a checkout.  Database-backed liveness is
deliberately represented as a protocol so the service can land before the
SessionPrompt/checkpoint integration without weakening that future boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Final, Iterator, Protocol, Sequence

from app.release_features import V11_WORKTREES_RELEASED
from app.tool.workspace import APP_PRIVATE_DIR_ENV

from .errors import (
    GitCommandError,
    GitCommandTimeout,
    GitUnavailableError,
    RepositoryValidationError,
    WorktreeActiveError,
    WorktreeConflictError,
    WorktreeDirtyError,
    WorktreeFeatureDisabled,
    WorktreeNotFoundError,
    WorktreeOwnershipError,
    WorktreePathError,
)

_MANIFEST_SCHEMA_VERSION: Final = 1
_MANIFEST_SERVICE: Final = "suxiaoyou.git-worktree"
_MANIFEST_NAME: Final = "ownership-v1.json"
_CHECKOUT_NAME: Final = "checkout"
_MAX_MANIFEST_BYTES: Final = 64 * 1024
_INSTANCE_ID_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_OBJECT_ID_RE: Final = re.compile(r"^[0-9a-f]{40,64}$")
_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}


class WorktreeState(StrEnum):
    CREATING = "creating"
    CREATED = "created"
    BOUND = "bound"
    DETACHED = "detached"
    CREATE_FAILED = "create_failed"
    REMOVED = "removed"


@dataclass(frozen=True, slots=True)
class FilesystemIdentity:
    device: int
    inode: int

    @classmethod
    def for_directory(cls, path: Path) -> "FilesystemIdentity":
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode):
            raise WorktreeOwnershipError(f"Expected an owned directory: {path}")
        return cls(device=int(info.st_dev), inode=int(info.st_ino))


@dataclass(frozen=True, slots=True)
class WorktreeReferences:
    """Live references that must block detach/removal/garbage collection."""

    workspace_instance_ids: tuple[str, ...] = ()
    turn_ids: tuple[str, ...] = ()
    checkpoint_ids: tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        return bool(
            self.workspace_instance_ids or self.turn_ids or self.checkpoint_ids
        )

    def describe(self) -> str:
        values: list[str] = []
        if self.workspace_instance_ids:
            values.append("workspace=" + ",".join(self.workspace_instance_ids))
        if self.turn_ids:
            values.append("turn=" + ",".join(self.turn_ids))
        if self.checkpoint_ids:
            values.append("checkpoint=" + ",".join(self.checkpoint_ids))
        return "; ".join(values) or "none"


class WorktreeReferenceGuard(Protocol):
    """Adapter point for persistent workspace/turn/checkpoint liveness."""

    def blockers_for(
        self,
        *,
        workspace_instance_id: str,
        checkout_path: Path,
    ) -> WorktreeReferences: ...


class NoWorktreeReferences:
    """Explicit adapter for callers that have proven there are no references."""

    def blockers_for(
        self,
        *,
        workspace_instance_id: str,
        checkout_path: Path,
    ) -> WorktreeReferences:
        del workspace_instance_id, checkout_path
        return WorktreeReferences()


class UnconfiguredWorktreeReferenceGuard:
    """Fail closed until persistent liveness storage is connected."""

    def blockers_for(
        self,
        *,
        workspace_instance_id: str,
        checkout_path: Path,
    ) -> WorktreeReferences:
        del workspace_instance_id, checkout_path
        raise WorktreeActiveError(
            "Persistent workspace/turn/checkpoint reference guard is not configured"
        )


@dataclass(frozen=True, slots=True)
class WorktreeRecord:
    schema_version: int
    service: str
    ownership_token: str
    workspace_instance_id: str
    repository_root: str
    git_common_dir: str
    checkout_path: str
    source_head: str
    requested_ref: str
    branch: str | None
    state: WorktreeState
    instance_identity: FilesystemIdentity
    repository_identity: FilesystemIdentity
    common_dir_identity: FilesystemIdentity
    checkout_identity: FilesystemIdentity | None
    created_at: str
    updated_at: str

    @property
    def path(self) -> Path:
        return Path(self.checkout_path)

    @classmethod
    def from_json(cls, value: object) -> "WorktreeRecord":
        try:
            if not isinstance(value, dict):
                raise TypeError("manifest root is not an object")
            record = cls(
                schema_version=int(value["schema_version"]),
                service=str(value["service"]),
                ownership_token=str(value["ownership_token"]),
                workspace_instance_id=str(value["workspace_instance_id"]),
                repository_root=str(value["repository_root"]),
                git_common_dir=str(value["git_common_dir"]),
                checkout_path=str(value["checkout_path"]),
                source_head=str(value["source_head"]),
                requested_ref=str(value["requested_ref"]),
                branch=(
                    str(value["branch"]) if value.get("branch") is not None else None
                ),
                state=WorktreeState(str(value["state"])),
                instance_identity=FilesystemIdentity(
                    device=int(value["instance_identity"]["device"]),
                    inode=int(value["instance_identity"]["inode"]),
                ),
                repository_identity=FilesystemIdentity(
                    device=int(value["repository_identity"]["device"]),
                    inode=int(value["repository_identity"]["inode"]),
                ),
                common_dir_identity=FilesystemIdentity(
                    device=int(value["common_dir_identity"]["device"]),
                    inode=int(value["common_dir_identity"]["inode"]),
                ),
                checkout_identity=(
                    FilesystemIdentity(
                        device=int(value["checkout_identity"]["device"]),
                        inode=int(value["checkout_identity"]["inode"]),
                    )
                    if value.get("checkout_identity") is not None
                    else None
                ),
                created_at=str(value["created_at"]),
                updated_at=str(value["updated_at"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise WorktreeOwnershipError("Owned worktree manifest is invalid") from exc
        if record.schema_version != _MANIFEST_SCHEMA_VERSION:
            raise WorktreeOwnershipError("Unsupported worktree manifest version")
        if record.service != _MANIFEST_SERVICE:
            raise WorktreeOwnershipError("Manifest was not created by this service")
        if not re.fullmatch(r"[0-9a-f]{64}", record.ownership_token):
            raise WorktreeOwnershipError("Manifest ownership token is invalid")
        _validate_instance_id(record.workspace_instance_id)
        if not _OBJECT_ID_RE.fullmatch(record.source_head):
            raise WorktreeOwnershipError("Manifest source HEAD is invalid")
        return record

    def to_json(self) -> dict[str, object]:
        value = asdict(self)
        value["state"] = self.state.value
        return value


@dataclass(frozen=True, slots=True)
class WorktreeInspection:
    record: WorktreeRecord
    head: str
    branch: str | None
    clean: bool
    registered: bool


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    healthy: tuple[str, ...] = ()
    repaired: tuple[str, ...] = ()
    removed_pending_gc: tuple[str, ...] = ()
    blocked: tuple[str, ...] = ()
    foreign: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GcReport:
    collected: tuple[str, ...] = ()
    blocked: tuple[str, ...] = ()
    foreign: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _GitResult:
    returncode: int
    stdout: bytes
    stderr: bytes

    def stdout_text(self) -> str:
        return os.fsdecode(self.stdout).rstrip("\r\n\0")

    def stderr_text(self) -> str:
        return self.stderr.decode("utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class _RepositoryInfo:
    root: Path
    common_dir: Path
    common_identity: FilesystemIdentity
    head: str


class WorktreeService:
    """Manage app-owned Git worktrees without exposing raw Git operations."""

    def __init__(
        self,
        *,
        managed_root: str | os.PathLike[str] | None = None,
        git_executable: str | os.PathLike[str] | None = None,
        timeout_seconds: float = 20.0,
        reference_guard: WorktreeReferenceGuard | None = None,
        enabled: bool | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if managed_root is None:
            private = os.environ.get(APP_PRIVATE_DIR_ENV, "").strip()
            trusted_base = (
                Path(private).expanduser() if private else Path.cwd() / "data"
            ).resolve()
            raw_root = trusted_base / "git-worktrees-v1"
        else:
            requested = Path(managed_root).expanduser()
            if requested.exists() and _is_link_or_junction(requested):
                raise WorktreePathError("Managed root must not be a symbolic link")
            raw_root = requested.parent.resolve() / requested.name
        self.managed_root = Path(os.path.abspath(raw_root))
        self._git_executable_input = (
            os.fspath(git_executable) if git_executable is not None else None
        )
        self._git_executable: str | None = None
        self.timeout_seconds = float(timeout_seconds)
        self.reference_guard = reference_guard or UnconfiguredWorktreeReferenceGuard()
        self.enabled = V11_WORKTREES_RELEASED if enabled is None else bool(enabled)

    def create(
        self,
        repository: str | os.PathLike[str],
        *,
        workspace_instance_id: str,
        ref: str = "HEAD",
        branch: str | None = None,
    ) -> WorktreeRecord:
        """Create an owned checkout, detached unless an existing branch is explicit."""

        self._prepare()
        instance_id = _validate_instance_id(workspace_instance_id)
        requested_ref = _validate_ref(ref)
        repository_info = self._validate_repository(repository, require_clean=True)
        resolved_branch = self._validate_branch(repository_info, branch)
        instance_dir, checkout, manifest = self._owned_paths(instance_id)
        lock_keys = [
            f"common:{_path_key(repository_info.common_dir)}",
            f"path:{_path_key(instance_dir)}",
        ]
        if resolved_branch:
            lock_keys.append(
                f"branch:{_path_key(repository_info.common_dir)}:{resolved_branch}"
            )
        with self._operation_locks(lock_keys):
            # Repeat mutable validation under the common-dir/path lock.
            repository_info = self._validate_repository(
                repository_info.root, require_clean=True
            )
            if instance_dir.exists() or _is_link_or_junction(instance_dir):
                raise WorktreeConflictError(
                    f"Managed instance path already exists: {instance_dir}"
                )
            if resolved_branch and self._branch_is_occupied(
                repository_info, resolved_branch
            ):
                raise WorktreeConflictError(
                    f"Branch is already checked out: {resolved_branch}"
                )
            commit = self._resolve_commit(
                repository_info.root,
                f"refs/heads/{resolved_branch}" if resolved_branch else requested_ref,
            )
            instance_dir.mkdir(mode=0o700)
            _fsync_directory(self.managed_root)
            instance_identity = FilesystemIdentity.for_directory(instance_dir)
            now = _utc_now()
            record = WorktreeRecord(
                schema_version=_MANIFEST_SCHEMA_VERSION,
                service=_MANIFEST_SERVICE,
                ownership_token=secrets.token_hex(32),
                workspace_instance_id=instance_id,
                repository_root=os.fspath(repository_info.root),
                git_common_dir=os.fspath(repository_info.common_dir),
                checkout_path=os.fspath(checkout),
                source_head=commit,
                requested_ref=requested_ref,
                branch=resolved_branch,
                state=WorktreeState.CREATING,
                instance_identity=instance_identity,
                repository_identity=FilesystemIdentity.for_directory(
                    repository_info.root
                ),
                common_dir_identity=repository_info.common_identity,
                checkout_identity=None,
                created_at=now,
                updated_at=now,
            )
            self._write_manifest(manifest, record)
            args = ["-C", os.fspath(repository_info.root), "worktree", "add"]
            if resolved_branch is None:
                args.append("--detach")
            target_ref = commit if resolved_branch is None else resolved_branch
            args.extend(["--", os.fspath(checkout), target_ref])
            try:
                self._run_git("worktree add", args)
                checkout_identity = FilesystemIdentity.for_directory(checkout)
                record = replace(
                    record,
                    state=WorktreeState.CREATED,
                    checkout_identity=checkout_identity,
                    updated_at=_utc_now(),
                )
                self._write_manifest(manifest, record)
                self._inspect_record(record, require_clean=True)
                return record
            except Exception:
                failed = replace(
                    record,
                    state=WorktreeState.CREATE_FAILED,
                    checkout_identity=(
                        FilesystemIdentity.for_directory(checkout)
                        if checkout.exists() and not _is_link_or_junction(checkout)
                        else None
                    ),
                    updated_at=_utc_now(),
                )
                try:
                    self._write_manifest(manifest, failed)
                except Exception:
                    pass
                raise

    def validate_source(self, repository: str | os.PathLike[str]) -> None:
        """Check whether a source can safely create a managed worktree.

        This is the path-free, read-only preflight used by the local runtime
        controls.  It intentionally applies the same clean-repository policy
        as ``create`` so the UI never advertises an operation that the service
        will immediately refuse.
        """

        self._prepare()
        self._validate_repository(repository, require_clean=True)

    def bind(
        self,
        workspace_instance_id: str,
        *,
        expected_repository: str | os.PathLike[str] | None = None,
    ) -> WorktreeRecord:
        """Validate and immutably bind a created instance for runtime use."""

        self._prepare()
        record, manifest = self._locked_record(workspace_instance_id)
        with self._operation_locks(self._record_lock_keys(record)):
            record = self._read_owned_record(workspace_instance_id)
            if expected_repository is not None and not _same_path(
                Path(record.repository_root),
                _canonical_directory(expected_repository, label="repository"),
            ):
                raise WorktreeConflictError("Worktree belongs to another repository")
            if record.state is WorktreeState.BOUND:
                self._inspect_record(record, require_clean=True)
                return record
            if record.state is not WorktreeState.CREATED:
                raise WorktreeConflictError(
                    f"Cannot bind worktree in state {record.state.value}"
                )
            self._inspect_record(record, require_clean=True)
            updated = replace(record, state=WorktreeState.BOUND, updated_at=_utc_now())
            self._write_manifest(manifest, updated)
            return updated

    def inspect(self, workspace_instance_id: str) -> WorktreeInspection:
        """Return verified ownership, Git registration, HEAD, branch, and cleanliness."""

        self._prepare()
        record, _ = self._locked_record(workspace_instance_id)
        with self._operation_locks(self._record_lock_keys(record)):
            record = self._read_owned_record(workspace_instance_id)
            return self._inspect_record(record, require_clean=False)

    def detach(self, workspace_instance_id: str) -> WorktreeRecord:
        """Release a runtime binding after the persistent guard reports no users."""

        self._prepare()
        record, manifest = self._locked_record(workspace_instance_id)
        with self._operation_locks(self._record_lock_keys(record)):
            record = self._read_owned_record(workspace_instance_id)
            if record.state is WorktreeState.REMOVED:
                self._assert_unreferenced(record)
                return record
            if record.state is WorktreeState.DETACHED:
                return record
            if record.state not in {WorktreeState.CREATED, WorktreeState.BOUND}:
                raise WorktreeConflictError(
                    f"Cannot detach worktree in state {record.state.value}"
                )
            self._assert_unreferenced(record)
            self._inspect_record(record, require_clean=False)
            updated = replace(
                record, state=WorktreeState.DETACHED, updated_at=_utc_now()
            )
            self._write_manifest(manifest, updated)
            return updated

    def remove(self, workspace_instance_id: str) -> WorktreeRecord:
        """Remove one clean, detached checkout through Git without force."""

        self._prepare()
        record, manifest = self._locked_record(workspace_instance_id)
        with self._operation_locks(self._record_lock_keys(record)):
            record = self._read_owned_record(workspace_instance_id)
            if record.state is WorktreeState.REMOVED:
                self._assert_unreferenced(record)
                return record
            if record.state is not WorktreeState.DETACHED:
                raise WorktreeConflictError(
                    f"Worktree must be detached before removal, not {record.state.value}"
                )
            self._assert_unreferenced(record)
            self._inspect_record(record, require_clean=True)
            self._run_git(
                "worktree remove",
                [
                    "-C",
                    record.repository_root,
                    "worktree",
                    "remove",
                    "--",
                    record.checkout_path,
                ],
            )
            if Path(record.checkout_path).exists() or _is_link_or_junction(
                Path(record.checkout_path)
            ):
                raise WorktreeConflictError(
                    "Git reported success but the managed checkout still exists"
                )
            if self._is_registered(record):
                raise WorktreeConflictError(
                    "Git reported success but the worktree remains registered"
                )
            updated = replace(record, state=WorktreeState.REMOVED, updated_at=_utc_now())
            self._write_manifest(manifest, updated)
            return updated

    def gc(self, workspace_instance_id: str | None = None) -> GcReport:
        """Collect only removed ownership manifests and empty instance shells."""

        self._prepare()
        ids = (
            (_validate_instance_id(workspace_instance_id),)
            if workspace_instance_id is not None
            else self._instance_directory_names()
        )
        collected: list[str] = []
        blocked: list[str] = []
        foreign: list[str] = []
        errors: list[str] = []
        for instance_id in ids:
            try:
                record, manifest = self._locked_record(instance_id)
                with self._operation_locks(self._record_lock_keys(record)):
                    record = self._read_owned_record(instance_id)
                    if record.state is not WorktreeState.REMOVED:
                        continue
                    self._assert_unreferenced(record)
                    checkout = Path(record.checkout_path)
                    if checkout.exists() or _is_link_or_junction(checkout):
                        raise WorktreeOwnershipError(
                            "Removed manifest still has a checkout path"
                        )
                    if self._is_registered(record):
                        raise WorktreeConflictError(
                            "Removed manifest is still registered by Git"
                        )
                    instance_dir = manifest.parent
                    self._validate_instance_identity(record, instance_dir)
                    entries = list(instance_dir.iterdir())
                    if entries != [manifest]:
                        foreign.append(instance_id)
                        continue
                    manifest.unlink()
                    _fsync_directory(instance_dir)
                    instance_dir.rmdir()
                    _fsync_directory(self.managed_root)
                    collected.append(instance_id)
            except WorktreeActiveError:
                blocked.append(instance_id)
            except WorktreeOwnershipError:
                foreign.append(instance_id)
            except Exception as exc:
                errors.append(f"{instance_id}: {exc}")
        return GcReport(
            collected=tuple(collected),
            blocked=tuple(blocked),
            foreign=tuple(foreign),
            errors=tuple(errors),
        )

    def reconcile(self) -> ReconcileReport:
        """Conservatively reconcile crash states; never delete a checkout."""

        self._prepare()
        healthy: list[str] = []
        repaired: list[str] = []
        removed_pending_gc: list[str] = []
        blocked: list[str] = []
        foreign: list[str] = []
        errors: list[str] = []
        for entry in sorted(self.managed_root.iterdir(), key=lambda item: item.name):
            if entry.name.startswith("_"):
                continue
            instance_id = entry.name
            try:
                _validate_instance_id(instance_id)
                if _is_link_or_junction(entry) or not entry.is_dir():
                    foreign.append(instance_id)
                    continue
                manifest = entry / _MANIFEST_NAME
                if not manifest.exists() or _is_link_or_junction(manifest):
                    foreign.append(instance_id)
                    continue
                record = self._read_owned_record(instance_id)
                with self._operation_locks(self._record_lock_keys(record)):
                    record = self._read_owned_record(instance_id)
                    checkout = Path(record.checkout_path)
                    exists = checkout.exists() and not _is_link_or_junction(checkout)
                    registered = self._is_registered(record)
                    if record.state is WorktreeState.REMOVED:
                        if exists or registered:
                            errors.append(
                                f"{instance_id}: removed ownership conflicts with Git/path"
                            )
                        else:
                            removed_pending_gc.append(instance_id)
                        continue
                    if exists and registered:
                        inspection = self._inspect_record(record, require_clean=False)
                        if not inspection.clean:
                            errors.append(
                                f"{instance_id}: managed worktree is dirty; retained"
                            )
                            continue
                        if record.state in {
                            WorktreeState.CREATING,
                            WorktreeState.CREATE_FAILED,
                        }:
                            updated = replace(
                                record,
                                state=WorktreeState.CREATED,
                                checkout_identity=FilesystemIdentity.for_directory(
                                    checkout
                                ),
                                updated_at=_utc_now(),
                            )
                            self._write_manifest(manifest, updated)
                            repaired.append(instance_id)
                        else:
                            healthy.append(instance_id)
                        continue
                    if not exists and not registered and record.state in {
                        WorktreeState.CREATING,
                        WorktreeState.DETACHED,
                        WorktreeState.CREATE_FAILED,
                    }:
                        try:
                            self._assert_unreferenced(record)
                        except WorktreeActiveError:
                            blocked.append(instance_id)
                            continue
                        updated = replace(
                            record,
                            state=WorktreeState.REMOVED,
                            updated_at=_utc_now(),
                        )
                        self._write_manifest(manifest, updated)
                        repaired.append(instance_id)
                        removed_pending_gc.append(instance_id)
                        continue
                    errors.append(
                        f"{instance_id}: checkout existence ({exists}) and Git registration "
                        f"({registered}) disagree or state {record.state.value} is not recoverable"
                    )
            except WorktreeActiveError:
                blocked.append(instance_id)
            except WorktreeOwnershipError:
                foreign.append(instance_id)
            except Exception as exc:
                errors.append(f"{instance_id}: {exc}")
        return ReconcileReport(
            healthy=tuple(healthy),
            repaired=tuple(repaired),
            removed_pending_gc=tuple(removed_pending_gc),
            blocked=tuple(blocked),
            foreign=tuple(foreign),
            errors=tuple(errors),
        )

    def _prepare(self) -> None:
        if not self.enabled:
            raise WorktreeFeatureDisabled(
                "Git worktree Beta is disabled by the v1.1 release gate"
            )
        self._ensure_managed_root()
        if self._git_executable is None:
            candidate = self._git_executable_input or shutil.which("git")
            if not candidate:
                raise GitUnavailableError("Git executable was not found")
            candidate_path = Path(candidate).expanduser().resolve(strict=True)
            if not candidate_path.is_file():
                raise GitUnavailableError("Git executable is not a regular file")
            self._git_executable = os.fspath(candidate_path)

    def _ensure_managed_root(self) -> None:
        root = self.managed_root
        with _acquire_locks([f"root:{_path_key(root)}"]):
            if root.exists() or _is_link_or_junction(root):
                if _is_link_or_junction(root) or not root.is_dir():
                    raise WorktreePathError(
                        "Managed worktree root must be a real directory, not a link"
                    )
            else:
                root.mkdir(parents=True, mode=0o700)
            info = root.stat(follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                raise WorktreePathError("Managed worktree root is not a directory")
            hooks = root / "_empty-hooks"
            home = root / "_git-home"
            locks = root / "_locks"
            for private_dir in (hooks, home, locks):
                if private_dir.exists() or _is_link_or_junction(private_dir):
                    if _is_link_or_junction(private_dir) or not private_dir.is_dir():
                        raise WorktreePathError(
                            f"Private Git directory is unsafe: {private_dir}"
                        )
                else:
                    private_dir.mkdir(mode=0o700)

    def _validate_repository(
        self,
        repository: str | os.PathLike[str],
        *,
        require_clean: bool,
    ) -> _RepositoryInfo:
        root = _canonical_directory(repository, label="repository")
        if _is_link_or_junction(Path(repository).expanduser()):
            raise RepositoryValidationError("Repository root must not be a symlink")
        inside_result = self._run_git(
            "rev-parse worktree",
            ["-C", os.fspath(root), "rev-parse", "--is-inside-work-tree"],
            # A normal directory is a valid preflight input, not a broken Git
            # process. Git reports that case with 128 (and some versions use
            # 1), which must become a repository eligibility failure rather
            # than a misleading supervised-command 502.
            allowed_returncodes=(0, 1, 128),
        )
        inside = inside_result.stdout_text()
        if inside_result.returncode != 0 or inside != "true":
            raise RepositoryValidationError("Repository is not a Git worktree")
        top = self._run_git(
            "rev-parse top-level",
            ["-C", os.fspath(root), "rev-parse", "--show-toplevel"],
        ).stdout_text()
        top_path = Path(top).resolve(strict=True)
        if not _same_path(root, top_path):
            raise RepositoryValidationError(
                "Repository path must be the canonical Git worktree root"
            )
        common_text = self._run_git(
            "rev-parse common-dir",
            ["-C", os.fspath(root), "rev-parse", "--git-common-dir"],
        ).stdout_text()
        common_raw = Path(common_text)
        common = (
            common_raw if common_raw.is_absolute() else root / common_raw
        ).resolve(strict=True)
        if not common.is_dir():
            raise RepositoryValidationError("Git common-dir is not a directory")
        head = self._resolve_commit(root, "HEAD")
        self._reject_external_git_drivers(root)
        if require_clean:
            self._assert_clean(root, source=True)
        return _RepositoryInfo(
            root=root,
            common_dir=common,
            common_identity=FilesystemIdentity.for_directory(common),
            head=head,
        )

    def _reject_external_git_drivers(self, repository: Path) -> None:
        result = self._run_git(
            "config safety check",
            [
                "-C",
                os.fspath(repository),
                "config",
                "--name-only",
                "--get-regexp",
                r"^(core\.fsmonitor|filter\..*\.(clean|smudge|process))$",
            ],
            allowed_returncodes=(0, 1),
        )
        if result.returncode == 0 and result.stdout.strip():
            names = ", ".join(
                line for line in result.stdout_text().splitlines() if line
            )
            raise RepositoryValidationError(
                "Repository config can launch external checkout/status helpers: "
                + names
            )

    def _validate_branch(
        self, repository: _RepositoryInfo, branch: str | None
    ) -> str | None:
        if branch is None:
            return None
        value = branch.strip()
        if not value or value.startswith("-") or "\0" in value:
            raise RepositoryValidationError("Branch name is invalid")
        checked = self._run_git(
            "check branch",
            ["-C", os.fspath(repository.root), "check-ref-format", "--branch", value],
            allowed_returncodes=(0, 1, 128),
        )
        if checked.returncode != 0:
            raise RepositoryValidationError("Branch name is invalid")
        self._resolve_commit(repository.root, f"refs/heads/{value}")
        return value

    def _resolve_commit(self, repository: Path, ref: str) -> str:
        result = self._run_git(
            "resolve commit",
            [
                "-C",
                os.fspath(repository),
                "rev-parse",
                "--verify",
                "--end-of-options",
                f"{ref}^{{commit}}",
            ],
            allowed_returncodes=(0, 1, 128),
        )
        commit = result.stdout_text().strip()
        if result.returncode != 0 or not _OBJECT_ID_RE.fullmatch(commit):
            raise RepositoryValidationError(f"Git reference does not resolve: {ref}")
        return commit

    def _assert_clean(self, worktree: Path, *, source: bool = False) -> None:
        status_result = self._run_git(
            "status",
            [
                "-C",
                os.fspath(worktree),
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ],
        )
        if status_result.stdout:
            location = "source repository" if source else "managed worktree"
            raise WorktreeDirtyError(f"Refusing operation on dirty {location}")

    def _inspect_record(
        self, record: WorktreeRecord, *, require_clean: bool
    ) -> WorktreeInspection:
        self._validate_owned_record_paths(record)
        checkout = Path(record.checkout_path)
        if record.state is WorktreeState.REMOVED:
            raise WorktreeConflictError("Removed worktree cannot be inspected as active")
        if _is_link_or_junction(checkout) or not checkout.is_dir():
            raise WorktreeOwnershipError("Owned checkout is missing or is a symlink")
        if record.checkout_identity is None:
            if record.state not in {
                WorktreeState.CREATING,
                WorktreeState.CREATE_FAILED,
            }:
                raise WorktreeOwnershipError("Checkout identity is missing")
        elif FilesystemIdentity.for_directory(checkout) != record.checkout_identity:
            raise WorktreeOwnershipError("Checkout filesystem identity changed")
        common = Path(record.git_common_dir)
        if (
            not common.is_dir()
            or _is_link_or_junction(common)
            or FilesystemIdentity.for_directory(common) != record.common_dir_identity
        ):
            raise WorktreeOwnershipError("Git common-dir identity changed")
        self._validate_repository_identity(record)
        top = self._run_git(
            "inspect top-level",
            ["-C", record.checkout_path, "rev-parse", "--show-toplevel"],
        ).stdout_text()
        if not _same_path(Path(top).resolve(strict=True), checkout):
            raise WorktreeOwnershipError("Checkout top-level escaped its owned path")
        actual_common_text = self._run_git(
            "inspect common-dir",
            ["-C", record.checkout_path, "rev-parse", "--git-common-dir"],
        ).stdout_text()
        actual_common_raw = Path(actual_common_text)
        actual_common = (
            actual_common_raw
            if actual_common_raw.is_absolute()
            else checkout / actual_common_raw
        ).resolve(strict=True)
        if not _same_path(actual_common, common):
            raise WorktreeOwnershipError("Checkout belongs to another Git common-dir")
        head = self._resolve_commit(checkout, "HEAD")
        symbolic = self._run_git(
            "symbolic-ref",
            ["-C", record.checkout_path, "symbolic-ref", "-q", "--short", "HEAD"],
            allowed_returncodes=(0, 1),
        )
        branch = symbolic.stdout_text() if symbolic.returncode == 0 else None
        if branch != record.branch:
            raise WorktreeConflictError(
                f"Checkout branch changed from {record.branch!r} to {branch!r}"
            )
        registered = self._is_registered(record)
        if not registered:
            raise WorktreeConflictError("Checkout is not registered in Git worktree metadata")
        clean = self._is_clean(checkout)
        if require_clean and not clean:
            raise WorktreeDirtyError("Refusing operation on dirty managed worktree")
        return WorktreeInspection(
            record=record,
            head=head,
            branch=branch,
            clean=clean,
            registered=registered,
        )

    def _is_clean(self, checkout: Path) -> bool:
        result = self._run_git(
            "status",
            [
                "-C",
                os.fspath(checkout),
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ],
        )
        return not result.stdout

    def _branch_is_occupied(
        self, repository: _RepositoryInfo, branch: str
    ) -> bool:
        branch_ref = f"refs/heads/{branch}"
        return any(
            entry.get("branch") == branch_ref
            for entry in self._list_git_worktrees(repository.root)
        )

    def _is_registered(self, record: WorktreeRecord) -> bool:
        repository = Path(record.repository_root)
        self._validate_repository_identity(record)
        target_key = _path_key(Path(record.checkout_path))
        return any(
            _path_key(Path(entry["worktree"])) == target_key
            for entry in self._list_git_worktrees(repository)
            if "worktree" in entry
        )

    def _list_git_worktrees(self, repository: Path) -> tuple[dict[str, str], ...]:
        result = self._run_git(
            "worktree list",
            [
                "-C",
                os.fspath(repository),
                "worktree",
                "list",
                "--porcelain",
                "-z",
            ],
        )
        records: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for raw in result.stdout.split(b"\0"):
            if not raw:
                if current:
                    records.append(current)
                    current = {}
                continue
            field, separator, value = raw.partition(b" ")
            key = field.decode("ascii", errors="replace")
            current[key] = os.fsdecode(value) if separator else "true"
        if current:
            records.append(current)
        return tuple(records)

    def _assert_unreferenced(self, record: WorktreeRecord) -> None:
        references = self.reference_guard.blockers_for(
            workspace_instance_id=record.workspace_instance_id,
            checkout_path=Path(record.checkout_path),
        )
        if not isinstance(references, WorktreeReferences):
            raise WorktreeActiveError(
                "Worktree reference guard returned an invalid result; failing closed"
            )
        if references.blocked:
            raise WorktreeActiveError(
                "Worktree still has active persistent references: "
                + references.describe()
            )

    def _locked_record(self, instance_id: str) -> tuple[WorktreeRecord, Path]:
        instance_id = _validate_instance_id(instance_id)
        with self._operation_locks(
            [f"path:{_path_key(self._owned_paths(instance_id)[0])}"]
        ):
            record = self._read_owned_record(instance_id)
            return record, self._owned_paths(instance_id)[2]

    def _read_owned_record(self, instance_id: str) -> WorktreeRecord:
        instance_id = _validate_instance_id(instance_id)
        instance_dir, _, manifest = self._owned_paths(instance_id)
        if not instance_dir.exists() and not _is_link_or_junction(instance_dir):
            raise WorktreeNotFoundError(f"Unknown worktree instance: {instance_id}")
        if _is_link_or_junction(instance_dir) or not instance_dir.is_dir():
            raise WorktreeOwnershipError("Managed instance path is not an owned directory")
        if not manifest.exists() and not _is_link_or_junction(manifest):
            raise WorktreeOwnershipError("Managed instance has no ownership manifest")
        if _is_link_or_junction(manifest) or not manifest.is_file():
            raise WorktreeOwnershipError("Ownership manifest is not a regular file")
        manifest_identity = manifest.stat(follow_symlinks=False)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(manifest, flags)
        except OSError as exc:
            raise WorktreeOwnershipError("Could not safely open ownership manifest") from exc
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_MANIFEST_BYTES:
                raise WorktreeOwnershipError("Ownership manifest has an unsafe type/size")
            if (int(info.st_dev), int(info.st_ino)) != (
                int(manifest_identity.st_dev),
                int(manifest_identity.st_ino),
            ):
                raise WorktreeOwnershipError(
                    "Ownership manifest changed while it was being opened"
                )
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                descriptor = -1
                value = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise WorktreeOwnershipError("Ownership manifest could not be decoded") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        record = WorktreeRecord.from_json(value)
        if record.workspace_instance_id != instance_id:
            raise WorktreeOwnershipError("Manifest instance ID does not match its directory")
        self._validate_owned_record_paths(record)
        self._validate_instance_identity(record, instance_dir)
        return record

    def _validate_owned_record_paths(self, record: WorktreeRecord) -> None:
        instance_dir, checkout, _ = self._owned_paths(record.workspace_instance_id)
        if not Path(record.checkout_path).is_absolute():
            raise WorktreeOwnershipError("Manifest checkout path must be absolute")
        if not _same_path(Path(record.checkout_path), checkout):
            raise WorktreeOwnershipError("Manifest checkout path escaped managed ownership")
        if not _is_direct_child(instance_dir, self.managed_root):
            raise WorktreeOwnershipError("Managed instance escaped its ownership root")
        repository = Path(record.repository_root)
        common = Path(record.git_common_dir)
        if not repository.is_absolute() or not common.is_absolute():
            raise WorktreeOwnershipError("Manifest Git paths must be absolute")

    @staticmethod
    def _validate_instance_identity(
        record: WorktreeRecord, instance_dir: Path
    ) -> None:
        if _is_link_or_junction(instance_dir):
            raise WorktreeOwnershipError("Managed instance directory became a symlink")
        if FilesystemIdentity.for_directory(instance_dir) != record.instance_identity:
            raise WorktreeOwnershipError("Managed instance filesystem identity changed")

    @staticmethod
    def _validate_repository_identity(record: WorktreeRecord) -> None:
        repository = Path(record.repository_root)
        if _is_link_or_junction(repository) or not repository.is_dir():
            raise WorktreeOwnershipError("Source repository no longer exists safely")
        if FilesystemIdentity.for_directory(repository) != record.repository_identity:
            raise WorktreeOwnershipError("Source repository filesystem identity changed")

    def _write_manifest(self, manifest: Path, record: WorktreeRecord) -> None:
        instance_dir = manifest.parent
        self._validate_instance_identity(record, instance_dir)
        if manifest.exists() and _is_link_or_junction(manifest):
            raise WorktreeOwnershipError("Refusing to replace a symlink manifest")
        payload = json.dumps(
            record.to_json(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ) + "\n"
        encoded = payload.encode("utf-8")
        if len(encoded) > _MAX_MANIFEST_BYTES:
            raise WorktreeOwnershipError("Ownership manifest exceeds its size limit")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".ownership-", suffix=".tmp", dir=instance_dir
        )
        temporary = Path(temporary_name)
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            if manifest.exists() and _is_link_or_junction(manifest):
                raise WorktreeOwnershipError("Manifest became a symlink during update")
            os.replace(temporary, manifest)
            _fsync_directory(instance_dir)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _owned_paths(self, instance_id: str) -> tuple[Path, Path, Path]:
        instance_id = _validate_instance_id(instance_id)
        instance_dir = self.managed_root / instance_id
        checkout = instance_dir / _CHECKOUT_NAME
        manifest = instance_dir / _MANIFEST_NAME
        if not _is_direct_child(instance_dir, self.managed_root):
            raise WorktreePathError("Instance path escaped the managed root")
        return instance_dir, checkout, manifest

    def _record_lock_keys(self, record: WorktreeRecord) -> list[str]:
        keys = [
            f"common:{_path_key(Path(record.git_common_dir))}",
            f"path:{_path_key(Path(record.checkout_path).parent)}",
        ]
        if record.branch:
            keys.append(
                f"branch:{_path_key(Path(record.git_common_dir))}:{record.branch}"
            )
        return keys

    def _instance_directory_names(self) -> tuple[str, ...]:
        names: list[str] = []
        for entry in self.managed_root.iterdir():
            if not entry.name.startswith("_"):
                names.append(entry.name)
        return tuple(sorted(names))

    @contextmanager
    def _operation_locks(self, keys: Sequence[str]) -> Iterator[None]:
        """Serialize branch/path mutations in-process and across backend processes."""

        normalized = tuple(sorted(set(keys)))
        with _acquire_locks(normalized):
            with ExitStack() as stack:
                for key in normalized:
                    stack.enter_context(self._cross_process_lock(key))
                yield

    @contextmanager
    def _cross_process_lock(self, key: str) -> Iterator[None]:
        lock_dir = self.managed_root / "_locks"
        if _is_link_or_junction(lock_dir) or not lock_dir.is_dir():
            raise WorktreePathError("Cross-process lock directory is unsafe")
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        lock_path = lock_dir / f"{digest}.lock"
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"0")
                os.fsync(descriptor)
            deadline = time.monotonic() + self.timeout_seconds
            while True:
                try:
                    _try_lock_descriptor(descriptor)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise WorktreeConflictError(
                            "Timed out waiting for a concurrent worktree lock"
                        ) from None
                    time.sleep(0.025)
            try:
                yield
            finally:
                _unlock_descriptor(descriptor)
        finally:
            os.close(descriptor)

    def _run_git(
        self,
        operation: str,
        arguments: Sequence[str],
        *,
        allowed_returncodes: Sequence[int] = (0,),
    ) -> _GitResult:
        executable = self._git_executable
        if executable is None:
            raise GitUnavailableError("Git service was not prepared")
        hooks = self.managed_root / "_empty-hooks"
        command = [
            executable,
            "--no-pager",
            "-c",
            f"core.hooksPath={hooks}",
            "-c",
            "credential.interactive=never",
            "-c",
            "protocol.allow=never",
            "-c",
            "core.quotepath=false",
            *arguments,
        ]
        environment = self._minimal_git_environment()
        kwargs: dict[str, object] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "env": environment,
            "shell": False,
        }
        if os.name == "nt":
            kwargs["creationflags"] = int(
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        else:
            kwargs["start_new_session"] = True
        try:
            process = subprocess.Popen(command, **kwargs)
        except OSError as exc:
            raise GitUnavailableError(f"Could not start Git: {exc}") from exc
        try:
            stdout, stderr = process.communicate(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            self._terminate_process_tree(process)
            raise GitCommandTimeout(
                operation, timeout_seconds=self.timeout_seconds
            ) from None
        result = _GitResult(
            returncode=int(process.returncode), stdout=stdout, stderr=stderr
        )
        if result.returncode not in allowed_returncodes:
            raise GitCommandError(
                operation,
                returncode=result.returncode,
                stderr=result.stderr_text(),
            )
        return result

    def _minimal_git_environment(self) -> dict[str, str]:
        environment = {
            "HOME": os.fspath(self.managed_root / "_git-home"),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
            "GIT_LFS_SKIP_SMUDGE": "1",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
        for name in (
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "PATHEXT",
            "TEMP",
            "TMP",
            "TMPDIR",
        ):
            if value := os.environ.get(name):
                environment[name] = value
        return environment

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=3,
                    check=False,
                    shell=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
            try:
                process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.communicate(timeout=1)
                except subprocess.TimeoutExpired:
                    _close_process_pipes(process)
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        pass
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=1)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            _close_process_pipes(process)
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass


def _validate_instance_id(value: str) -> str:
    if not isinstance(value, str) or not _INSTANCE_ID_RE.fullmatch(value):
        raise WorktreePathError(
            "workspace_instance_id must be 1-128 ASCII letters, digits, '.', '_' or '-'"
        )
    if value in {".", ".."} or value.startswith("_"):
        raise WorktreePathError("workspace_instance_id uses a reserved value")
    return value


def _validate_ref(value: str) -> str:
    if not isinstance(value, str):
        raise RepositoryValidationError("Git reference must be text")
    result = value.strip()
    if not result or result.startswith("-") or "\0" in result or "\n" in result:
        raise RepositoryValidationError("Git reference is invalid")
    return result


def _canonical_directory(
    value: str | os.PathLike[str], *, label: str
) -> Path:
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise WorktreePathError(f"{label.capitalize()} path does not exist safely") from exc
    if not path.is_dir():
        raise WorktreePathError(f"{label.capitalize()} path is not a directory")
    return path


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _same_path(left: Path, right: Path) -> bool:
    return _path_key(left) == _path_key(right)


def _is_direct_child(child: Path, parent: Path) -> bool:
    return _path_key(child.parent) == _path_key(parent)


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    return bool(os.name == "nt" and path.is_junction())


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lock_for(key: str) -> threading.RLock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _acquire_locks(keys: Sequence[str]) -> Iterator[None]:
    with ExitStack() as stack:
        for key in sorted(set(keys)):
            lock = _lock_for(key)
            lock.acquire()
            stack.callback(lock.release)
        yield


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _close_process_pipes(process: subprocess.Popen[bytes]) -> None:
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def _try_lock_descriptor(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise BlockingIOError from exc
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_descriptor(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)
