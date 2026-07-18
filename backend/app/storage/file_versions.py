"""Durable, checksum-verified versions for Agent file mutations.

Version blobs live in the application-private data directory instead of the
selected workspace.  This keeps an approved ``bash``/``code_execute`` call
from rewriting its own recovery history while preserving the local-first
storage boundary.  Manifests contain only workspace-relative paths; callers
must still present the exact canonical workspace to list or restore them.
"""

from __future__ import annotations

import hashlib
import ctypes
import errno
import json
import logging
import os
import re
import stat
import sys
import tempfile
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from app.tool.workspace import (
    APP_PRIVATE_DIR_ENV,
    WorkspaceViolation,
    resolve_and_validate,
    resolve_for_write,
)
from app.utils.atomic_write import atomic_write_text
from app.utils.guarded_file_mutation import (
    guarded_file_mutation_unavailable_reason,
)
from app.utils.id import generate_ulid
from app.utils.windows_guarded_file import (
    GuardedExchange,
    Win32Backend,
    WindowsGuardedFileError,
    locked_directory_chain,
    open_regular_file_for_stable_read,
    validate_windows_relative_name,
    windows_lstat_is_reparse,
    windows_path_identity,
)

logger = logging.getLogger(__name__)

MANIFEST_SCHEMA_VERSION: Final = 2
SUPPORTED_MANIFEST_SCHEMA_VERSIONS: Final = frozenset({1, MANIFEST_SCHEMA_VERSION})
DEFAULT_MAX_FILE_BYTES: Final = 100 * 1024 * 1024
DEFAULT_MAX_WORKSPACE_BYTES: Final = 512 * 1024 * 1024
DEFAULT_MAX_VERSIONS_PER_FILE: Final = 50
DEFAULT_MAX_TOTAL_VERSIONS: Final = 2_000
_COPY_CHUNK_BYTES: Final = 1024 * 1024
_SHA256_PATTERN: Final = re.compile(r"^[0-9a-f]{64}$")


class FileVersionError(RuntimeError):
    """A version could not be captured or restored safely."""


class FileVersionNotFound(FileVersionError):
    """The requested version does not exist in this workspace."""


@dataclass(frozen=True, slots=True)
class FileVersion:
    """One immutable pre-mutation snapshot."""

    id: str
    relative_path: str
    sha256: str
    size: int
    created_at: str
    created_at_ns: int
    operation: str
    session_id: str | None = None
    message_id: str | None = None
    call_id: str | None = None
    original_mode: int | None = None
    # Windows snapshots are unique full-fidelity CopyFileW objects because two
    # files with identical default-stream bytes can have different ACLs/ADS.
    # POSIX versions remain content-addressed when this field is absent.
    object_name: str | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "FileVersion":
        try:
            result = cls(
                id=str(value["id"]),
                relative_path=str(value["relative_path"]),
                sha256=str(value["sha256"]),
                size=int(value["size"]),
                created_at=str(value["created_at"]),
                created_at_ns=int(value["created_at_ns"]),
                operation=str(value["operation"]),
                session_id=_optional_string(value.get("session_id")),
                message_id=_optional_string(value.get("message_id")),
                call_id=_optional_string(value.get("call_id")),
                original_mode=(
                    int(value["original_mode"])
                    if value.get("original_mode") is not None
                    else None
                ),
                object_name=_optional_string(value.get("object_name")),
            )
            if not result.id or not _SHA256_PATTERN.fullmatch(result.sha256):
                raise ValueError("invalid identifier/checksum")
            if result.size < 0:
                raise ValueError("negative size")
            if result.object_name is not None and (
                Path(result.object_name).name != result.object_name
                or not result.object_name.endswith(".blob")
            ):
                raise ValueError("unsafe object name")
            return result
        except (KeyError, TypeError, ValueError) as exc:
            raise FileVersionError("File-version manifest contains an invalid entry") from exc

    def public_dict(self) -> dict[str, Any]:
        """Return stable JSON data suitable for tool/API responses."""

        value = asdict(self)
        value.pop("object_name", None)
        return value


@dataclass(frozen=True, slots=True)
class FileVersionLimits:
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_workspace_bytes: int = DEFAULT_MAX_WORKSPACE_BYTES
    max_versions_per_file: int = DEFAULT_MAX_VERSIONS_PER_FILE
    max_total_versions: int = DEFAULT_MAX_TOTAL_VERSIONS

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.max_file_bytes > self.max_workspace_bytes:
            raise ValueError("max_file_bytes cannot exceed max_workspace_bytes")


_LOCKS_GUARD = threading.Lock()
_WORKSPACE_LOCKS: dict[str, threading.RLock] = {}


def default_file_version_storage_root() -> Path:
    """Return the app-private root used for durable version blobs."""

    private_root = os.environ.get(APP_PRIVATE_DIR_ENV, "").strip()
    base = Path(private_root).expanduser() if private_root else Path.cwd() / "data"
    # Canonicalize the trusted app-data parent, but deliberately do not resolve
    # the final managed component.  _ensure_store must be able to detect and
    # reject a pre-existing ``file-versions`` symlink instead of following it.
    return base.resolve() / "file-versions"


class FileVersionStore:
    """Workspace-scoped persistent file history.

    The service is synchronous because the built-in file tools already perform
    local filesystem work synchronously.  Operations are serialized per
    canonical workspace so concurrent Agent turns cannot lose manifest rows.
    """

    def __init__(
        self,
        workspace: str | os.PathLike[str],
        *,
        storage_root: str | os.PathLike[str] | None = None,
        limits: FileVersionLimits | None = None,
        expected_workspace_identity: tuple[int, int] | None = None,
    ) -> None:
        workspace_path = Path(workspace).expanduser().resolve(strict=True)
        if not workspace_path.is_dir():
            raise FileVersionError(f"Workspace is not a directory: {workspace_path}")
        self.workspace = workspace_path
        workspace_info = self.workspace.stat(follow_symlinks=False)
        if sys.platform == "win32":
            if windows_lstat_is_reparse(workspace_info):
                raise FileVersionError("Workspace root is a Windows reparse point")
            self._workspace_identity = windows_path_identity(
                self.workspace, directory=True
            )
        else:
            self._workspace_identity = (workspace_info.st_dev, workspace_info.st_ino)
        if (
            expected_workspace_identity is not None
            and self._workspace_identity != expected_workspace_identity
        ):
            raise FileVersionError("Workspace root changed before file-version operation")
        self.limits = limits or FileVersionLimits()
        raw_storage_root = Path(
            storage_root if storage_root is not None else default_file_version_storage_root()
        ).expanduser()
        self.storage_root = Path(os.path.abspath(raw_storage_root))
        # A pathname is not a workspace identity.  The directory at a selected
        # path can be removed and recreated while an older history tree still
        # exists.  Scope private history by both the canonical path and the
        # filesystem object so a replacement directory cannot inherit or restore
        # the previous directory's contents.
        workspace_key = _workspace_storage_key(
            self.workspace,
            self._workspace_identity,
        )
        self.root = self.storage_root / workspace_key
        self.objects_dir = self.root / "objects"
        self.manifest_path = self.root / "manifest-v1.json"
        self._lock = _lock_for_workspace(workspace_key)

    def capture_before_mutation(
        self,
        file_path: str | os.PathLike[str],
        *,
        operation: str,
        session_id: str | None = None,
        message_id: str | None = None,
        call_id: str | None = None,
    ) -> FileVersion | None:
        try:
            return self._capture_before_mutation(
                file_path,
                operation=operation,
                session_id=session_id,
                message_id=message_id,
                call_id=call_id,
                pinned_version_ids=frozenset(),
            )
        except FileVersionError:
            raise
        except OSError as exc:
            raise FileVersionError(
                f"Could not create a recovery snapshot for {file_path}: {exc}"
            ) from exc

    def capture_batch_before_mutation(
        self,
        file_paths: list[str | os.PathLike[str]],
        *,
        operation: str,
        session_id: str | None = None,
        message_id: str | None = None,
        call_id: str | None = None,
    ) -> list[FileVersion]:
        """Capture one recoverable, retention-pinned batch before a mutation.

        Multi-file writers must not loop over :meth:`capture_before_mutation`:
        ordinary retention could evict an earlier row before the whole write is
        ready to commit.  This method pins every snapshot created by the batch
        while later rows are installed, and fails before the caller mutates any
        workspace path if the configured limits cannot retain all of them.
        """

        captured: list[FileVersion] = []
        pinned: set[str] = set()
        try:
            with self._lock:
                for file_path in file_paths:
                    version = self._capture_before_mutation(
                        file_path,
                        operation=operation,
                        session_id=session_id,
                        message_id=message_id,
                        call_id=call_id,
                        pinned_version_ids=frozenset(pinned),
                    )
                    if version is not None:
                        captured.append(version)
                        pinned.add(version.id)
                self._assert_workspace_identity()
            return captured
        except FileVersionError:
            raise
        except OSError as exc:
            raise FileVersionError(
                f"Could not create the recovery snapshot batch: {exc}"
            ) from exc

    def _capture_before_mutation(
        self,
        file_path: str | os.PathLike[str],
        *,
        operation: str,
        session_id: str | None = None,
        message_id: str | None = None,
        call_id: str | None = None,
        pinned_version_ids: frozenset[str] = frozenset(),
    ) -> FileVersion | None:
        """Persist the existing regular file, returning ``None`` for new paths.

        Capture fails closed for oversized, redirected, changing, or otherwise
        unreadable files.  Callers must not mutate the destination after an
        exception.
        """

        self._assert_workspace_identity()
        target = self.resolve_target(file_path, for_write=True)
        if not target.exists():
            return None
        if target.is_dir():
            raise FileVersionError(f"Cannot version a directory: {target}")

        with self._lock:
            self._ensure_store()
            version_id = generate_ulid()
            temporary, digest, size, mode = self._copy_target_to_temporary(target)
            object_name = (
                f"{version_id}.win32-full.blob" if sys.platform == "win32" else None
            )
            object_path = self.objects_dir / (object_name or f"{digest}.blob")
            object_was_present = object_path.exists()
            manifest_committed = False
            try:
                object_path = self._install_object(
                    temporary,
                    digest,
                    size,
                    object_name=object_name,
                )
                temporary = None
                now = datetime.now(timezone.utc)
                version = FileVersion(
                    id=version_id,
                    relative_path=target.relative_to(self.workspace).as_posix(),
                    sha256=digest,
                    size=size,
                    created_at=now.isoformat(),
                    created_at_ns=int(now.timestamp() * 1_000_000_000),
                    operation=_safe_operation(operation),
                    session_id=_optional_string(session_id),
                    message_id=_optional_string(message_id),
                    call_id=_optional_string(call_id),
                    original_mode=mode,
                    object_name=object_name,
                )
                manifest = self._load_manifest()
                versions = [*manifest["versions"], version]
                durable_pins = _manifest_pinned_version_ids(manifest["pins"])
                retained = self._apply_retention(
                    versions,
                    newest=version,
                    pinned_version_ids=frozenset(
                        {*pinned_version_ids, *durable_pins}
                    ),
                )
                self._write_manifest(retained, pins=manifest["pins"])
                manifest_committed = True
                self._delete_unreferenced_objects(retained)
                # Make the postcondition explicit: a successful capture always
                # leaves a verified object referenced by the committed manifest.
                self._verify_object(object_path, digest=digest, size=size)
                self._assert_workspace_identity()
                return version
            except Exception:
                # A blob installed before a failed manifest commit is not a
                # retained version. Remove that new orphan so failed writes do
                # not bypass the workspace storage cap.
                if not manifest_committed and not object_was_present:
                    try:
                        object_path.unlink(missing_ok=True)
                    except OSError:
                        logger.warning(
                            "Could not clean uncommitted file-version object %s",
                            object_path,
                        )
                raise
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

    def list_versions(
        self,
        *,
        file_path: str | os.PathLike[str] | None = None,
        limit: int = 100,
    ) -> list[FileVersion]:
        """List newest-first versions, optionally filtered to one target."""

        if limit < 1 or limit > 500:
            raise FileVersionError("limit must be between 1 and 500")
        with self._lock:
            manifest = self._load_manifest(create=False)
            versions = manifest["versions"]
            relative_path: str | None = None
            if file_path is not None:
                raw_path = os.fspath(file_path)
                normalized = Path(raw_path).as_posix()
                known_paths = {version.relative_path for version in versions}
                if not Path(raw_path).is_absolute() and normalized in known_paths:
                    # ``relative_path`` values returned by this service are
                    # workspace-relative, while new Agent writes default to
                    # ``suxiaoyou_written``.  Prefer an exact manifest match so
                    # callers can feed listed paths back into the filter even
                    # after the target was deleted.
                    relative_path = normalized
                else:
                    target = self.resolve_target(raw_path, for_write=True)
                    relative_path = target.relative_to(self.workspace).as_posix()
            if relative_path is not None:
                versions = [v for v in versions if v.relative_path == relative_path]
            return sorted(versions, key=_version_sort_key, reverse=True)[:limit]

    def list_pins(self) -> dict[str, frozenset[str]]:
        """Return durable retention owners and their immutable version IDs."""

        with self._lock:
            manifest = self._load_manifest(create=False)
            return {
                owner: frozenset(version_ids)
                for owner, version_ids in manifest["pins"].items()
            }

    def get_version(self, version_id: str) -> FileVersion:
        """Return one immutable version by ID or fail closed."""

        requested = str(version_id).strip()
        if not requested:
            raise FileVersionNotFound("version_id is required")
        with self._lock:
            manifest = self._load_manifest(create=False)
            version = next(
                (item for item in manifest["versions"] if item.id == requested),
                None,
            )
            if version is None:
                raise FileVersionNotFound(
                    f"File version not found in this workspace: {requested}"
                )
            return version

    def materialize_version_in_transaction(
        self,
        version_id: str,
        staged_workspace: str | os.PathLike[str],
        *,
        expected_relative_path: str,
    ) -> tuple[FileVersion, Path]:
        """Copy a verified version into a private workspace transaction stage.

        This is intentionally narrower than a generic export API.  Rewind must
        assemble its complete desired state in ``WorkspaceMutationTransaction``
        before the first visible write, but the immutable version objects live
        outside that stage.  The destination therefore has to match the
        transaction service's private ``execution-transactions/<workspace-key>/
        tx-*/workspace`` layout and the version's own canonical relative path.
        It can never point at the selected workspace itself.
        """

        requested = str(version_id).strip()
        relative = str(expected_relative_path).strip()
        if not requested:
            raise FileVersionNotFound("version_id is required")
        if not relative or "\\" in relative:
            raise FileVersionError("Expected version path is not canonical")
        relative_path = Path(relative)
        if relative_path.is_absolute() or any(
            part in {"", ".", ".."} for part in relative_path.parts
        ):
            raise FileVersionError("Expected version path is not canonical")

        stage = Path(staged_workspace)
        if not stage.is_absolute():
            raise FileVersionError("Transaction stage must be an absolute path")
        stage = Path(os.path.abspath(stage))
        transaction_root = stage.parent
        workspace_owner = transaction_root.parent
        transaction_service = workspace_owner.parent
        expected_owner = hashlib.sha256(os.fsencode(str(self.workspace))).hexdigest()
        if (
            stage.name != "workspace"
            or not transaction_root.name.startswith("tx-")
            or workspace_owner.name != expected_owner
            or transaction_service.name != "execution-transactions"
        ):
            raise FileVersionError(
                "Version materialization is restricted to an owned workspace transaction"
            )

        try:
            stage_info = stage.lstat()
        except OSError as exc:
            raise FileVersionError("Transaction stage is unavailable") from exc
        if (
            stat.S_ISLNK(stage_info.st_mode)
            or (sys.platform == "win32" and windows_lstat_is_reparse(stage_info))
            or not stat.S_ISDIR(stage_info.st_mode)
        ):
            raise FileVersionError("Transaction stage is redirected or invalid")
        stage_identity = (int(stage_info.st_dev), int(stage_info.st_ino))

        with self._lock:
            manifest = self._load_manifest(create=False)
            version = next(
                (item for item in manifest["versions"] if item.id == requested),
                None,
            )
            if version is None:
                raise FileVersionNotFound(
                    f"File version not found in this workspace: {requested}"
                )
            if version.relative_path != relative_path.as_posix():
                raise FileVersionError("Version belongs to a different workspace path")
            source = self._object_path(version)
            self._verify_object(
                source,
                digest=version.sha256,
                size=version.size,
            )

            parent = stage
            for component in relative_path.parts[:-1]:
                parent = parent / component
                try:
                    parent_info = parent.lstat()
                except OSError as exc:
                    raise FileVersionError(
                        "Version destination parent is unavailable in the transaction stage"
                    ) from exc
                if (
                    stat.S_ISLNK(parent_info.st_mode)
                    or (
                        sys.platform == "win32"
                        and windows_lstat_is_reparse(parent_info)
                    )
                    or not stat.S_ISDIR(parent_info.st_mode)
                ):
                    raise FileVersionError(
                        "Version destination parent is redirected or not a directory"
                    )

            target = stage.joinpath(*relative_path.parts)
            try:
                target_info = target.lstat()
            except FileNotFoundError:
                target_info = None
            except OSError as exc:
                raise FileVersionError("Version destination cannot be inspected") from exc
            if target_info is not None and (
                stat.S_ISLNK(target_info.st_mode)
                or (
                    sys.platform == "win32"
                    and windows_lstat_is_reparse(target_info)
                )
                or not stat.S_ISREG(target_info.st_mode)
            ):
                raise FileVersionError(
                    "Version destination is redirected or not a regular file"
                )

            temporary_fd, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.",
                suffix=".rewind.tmp",
                dir=target.parent,
            )
            temporary = Path(temporary_name)
            source_fd = -1
            digest = hashlib.sha256()
            copied = 0
            try:
                source_fd = os.open(
                    source,
                    os.O_RDONLY
                    | getattr(os, "O_BINARY", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                )
                opened_source = os.fstat(source_fd)
                if not stat.S_ISREG(opened_source.st_mode):
                    raise FileVersionError("Recovery object is not a regular file")
                with os.fdopen(source_fd, "rb") as src, os.fdopen(
                    temporary_fd, "wb"
                ) as dst:
                    source_fd = -1
                    temporary_fd = -1
                    while chunk := src.read(_COPY_CHUNK_BYTES):
                        digest.update(chunk)
                        copied += len(chunk)
                        dst.write(chunk)
                    dst.flush()
                    os.fsync(dst.fileno())
                if copied != version.size or digest.hexdigest() != version.sha256:
                    raise FileVersionError("Recovery object changed during materialization")
                if version.original_mode is not None:
                    os.chmod(temporary, version.original_mode)
                current_stage = stage.lstat()
                if (int(current_stage.st_dev), int(current_stage.st_ino)) != stage_identity:
                    raise FileVersionError("Transaction stage changed during materialization")
                os.replace(temporary, target)
                _fsync_directory(target.parent)
                installed_digest, installed_size = _sha256_regular_file(target)
                if (
                    installed_digest != version.sha256
                    or installed_size != version.size
                ):
                    raise FileVersionError("Materialized version failed verification")
                return version, target
            except FileVersionError:
                raise
            except OSError as exc:
                raise FileVersionError(
                    f"Could not materialize version into transaction: {exc}"
                ) from exc
            finally:
                if temporary_fd >= 0:
                    os.close(temporary_fd)
                if source_fd >= 0:
                    os.close(source_fd)
                temporary.unlink(missing_ok=True)

    def pin_versions(
        self,
        owner_id: str,
        version_ids: list[str] | tuple[str, ...] | set[str] | frozenset[str],
    ) -> frozenset[str]:
        """Persistently protect versions for one checkpoint/retention owner.

        The return value contains only IDs newly added for this owner.  Callers
        can use it for narrow compensation if their database flush fails.
        """

        owner = _safe_pin_owner(owner_id)
        requested = _safe_version_ids(version_ids)
        if not requested:
            return frozenset()
        with self._lock:
            manifest = self._load_manifest(create=False)
            available = {version.id for version in manifest["versions"]}
            missing = requested - available
            if missing:
                raise FileVersionNotFound(
                    "Pinned file version is missing from this workspace: "
                    + ", ".join(sorted(missing))
                )
            existing = set(manifest["pins"].get(owner, ()))
            added = requested - existing
            if not added:
                return frozenset()
            pins = dict(manifest["pins"])
            pins[owner] = sorted(existing | requested)
            self._write_manifest(manifest["versions"], pins=pins)
            return frozenset(added)

    def replace_pinned_versions(
        self,
        owner_id: str,
        version_ids: list[str] | tuple[str, ...] | set[str] | frozenset[str],
    ) -> None:
        """Reconcile one owner's exact pins from the database source of truth."""

        owner = _safe_pin_owner(owner_id)
        requested = _safe_version_ids(version_ids)
        with self._lock:
            manifest = self._load_manifest(create=False)
            available = {version.id for version in manifest["versions"]}
            missing = requested - available
            if missing:
                raise FileVersionNotFound(
                    "Pinned file version is missing from this workspace: "
                    + ", ".join(sorted(missing))
                )
            pins = dict(manifest["pins"])
            if requested:
                pins[owner] = sorted(requested)
            else:
                pins.pop(owner, None)
            if pins != manifest["pins"]:
                self._write_manifest(manifest["versions"], pins=pins)

    def unpin_versions(
        self,
        owner_id: str,
        version_ids: (
            list[str] | tuple[str, ...] | set[str] | frozenset[str] | None
        ) = None,
    ) -> frozenset[str]:
        """Release some or all versions held by one retention owner.

        Unpinning does not eagerly prune blobs.  The next capture applies the
        ordinary retention policy, which avoids deleting a just-released
        recovery source in the middle of a larger checkpoint transaction.
        """

        owner = _safe_pin_owner(owner_id)
        requested = None if version_ids is None else _safe_version_ids(version_ids)
        with self._lock:
            manifest = self._load_manifest(create=False)
            existing = set(manifest["pins"].get(owner, ()))
            if not existing:
                return frozenset()
            removed = existing if requested is None else existing & requested
            if not removed:
                return frozenset()
            retained = existing - removed
            pins = dict(manifest["pins"])
            if retained:
                pins[owner] = sorted(retained)
            else:
                pins.pop(owner, None)
            self._write_manifest(manifest["versions"], pins=pins)
            return frozenset(removed)

    def restore(
        self,
        version_id: str,
        *,
        session_id: str | None = None,
        message_id: str | None = None,
        call_id: str | None = None,
    ) -> tuple[FileVersion, FileVersion | None, Path]:
        """Atomically restore one version and snapshot the displaced file.

        Returns ``(restored_version, recovery_version, target_path)``.  The
        recovery version is ``None`` only when the target no longer exists.
        """

        requested_id = str(version_id).strip()
        if not requested_id:
            raise FileVersionNotFound("version_id is required")
        self._require_guarded_restore_support()

        with self._lock:
            manifest = self._load_manifest(create=False)
            version = next(
                (item for item in manifest["versions"] if item.id == requested_id),
                None,
            )
            if version is None:
                raise FileVersionNotFound(
                    f"File version not found in this workspace: {requested_id}"
                )
            target = self._target_from_relative(version.relative_path)
            blob = self._object_path(version)
            self._verify_object(blob, digest=version.sha256, size=version.size)

            try:
                # Keep the selected source version referenced while committing
                # the recovery snapshot. Without this pin, a full retention
                # window could evict and delete ``blob`` before the atomic
                # restore reads it (for example A/B retained, current C, then
                # restore A with a two-version limit).
                recovery = self._capture_before_mutation(
                    target,
                    operation="restore",
                    session_id=session_id,
                    message_id=message_id,
                    call_id=call_id,
                    pinned_version_ids=frozenset({version.id}),
                )
                expected_current = (
                    {
                        "kind": "file",
                        "mode": recovery.original_mode,
                        "size": recovery.size,
                        "sha256": recovery.sha256,
                        "link_target": None,
                    }
                    if recovery is not None
                    else None
                )
                self._copy_object_guarded(
                    blob,
                    relative_path=version.relative_path,
                    expected_current=expected_current,
                    expected_digest=version.sha256,
                    expected_size=version.size,
                    mode=version.original_mode,
                )
            except FileVersionError:
                raise
            except OSError as exc:
                raise FileVersionError(
                    f"Could not atomically restore {version.relative_path}: {exc}"
                ) from exc
            return version, recovery, target

    def restore_failed_mutation_batch(
        self,
        version_ids: list[str],
        *,
        expected_current: dict[str, dict[str, object] | None] | None = None,
    ) -> list[Path]:
        """Restore a failed multi-file commit without retaining failed outputs.

        This recovery path is reserved for a writer whose commit failed after
        some atomic replacements became visible.  All source blobs are checked
        before the first rollback write, and no recovery snapshots of the
        uncommitted output are added (which could otherwise evict another
        source from a full retention window).
        """

        requested = [str(value).strip() for value in version_ids]
        self._require_guarded_restore_support()
        if any(not value for value in requested):
            raise FileVersionNotFound("version_id is required")
        if len(set(requested)) != len(requested):
            raise FileVersionError("Rollback version IDs must be unique")
        with self._lock:
            manifest = self._load_manifest(create=False)
            by_id = {version.id: version for version in manifest["versions"]}
            try:
                versions = [by_id[value] for value in requested]
            except KeyError as exc:
                raise FileVersionNotFound(
                    f"File version not found in this workspace: {exc.args[0]}"
                ) from exc
            prepared: list[tuple[FileVersion, Path, Path]] = []
            for version in versions:
                target = self._target_from_relative(version.relative_path)
                blob = self._object_path(version)
                self._verify_object(blob, digest=version.sha256, size=version.size)
                prepared.append((version, target, blob))
            restored: list[Path] = []
            try:
                for version, target, blob in prepared:
                    if expected_current is None:
                        if sys.platform == "win32":
                            # Windows rollback is declarative-only: parent
                            # directories were preflighted by the transaction
                            # and must not be created outside its journal.
                            try:
                                parent_info = target.parent.lstat()
                            except FileNotFoundError as exc:
                                raise FileVersionError(
                                    "Windows rollback parent must already exist: "
                                    f"{target.parent}"
                                ) from exc
                            if windows_lstat_is_reparse(parent_info) or not stat.S_ISDIR(
                                parent_info.st_mode
                            ):
                                raise FileVersionError(
                                    "Windows rollback parent is redirected or not a "
                                    f"directory: {target.parent}"
                                )
                        else:
                            target.parent.mkdir(parents=True, exist_ok=True)
                        if self._target_from_relative(version.relative_path) != target:
                            raise FileVersionError("Rollback target changed during recovery")
                        self._copy_object_atomically(
                            blob,
                            target,
                            expected_digest=version.sha256,
                            expected_size=version.size,
                            mode=version.original_mode,
                        )
                    else:
                        self._assert_workspace_identity()
                        if version.relative_path not in expected_current:
                            raise FileVersionError(
                                "Rollback expected-current map is incomplete"
                            )
                        self._copy_object_guarded(
                            blob,
                            relative_path=version.relative_path,
                            expected_current=expected_current[version.relative_path],
                            expected_digest=version.sha256,
                            expected_size=version.size,
                            mode=version.original_mode,
                        )
                    restored.append(target)
            except FileVersionError:
                raise
            except OSError as exc:
                raise FileVersionError(
                    f"Could not roll back the failed mutation batch: {exc}"
                ) from exc
            return restored

    def _copy_object_guarded(
        self,
        source: Path,
        *,
        relative_path: str,
        expected_current: dict[str, object] | None,
        expected_digest: str,
        expected_size: int,
        mode: int | None,
    ) -> None:
        """Restore only if the atomically displaced object is the expected output."""

        self._require_guarded_restore_support()
        if sys.platform == "win32":
            self._copy_object_guarded_windows(
                source,
                relative_path=relative_path,
                expected_current=expected_current,
                expected_digest=expected_digest,
                expected_size=expected_size,
                mode=mode,
            )
            return
        parent_fd, target_name = _ensure_version_parent_fd(
            self.workspace,
            relative_path,
            expected_identity=self._workspace_identity,
        )
        temporary_name = f".{target_name}.{generate_ulid()}.rollback.tmp"
        temporary_fd = -1
        source_fd = -1
        preserve_temporary = False
        try:
            temporary_fd = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent_fd,
            )
            source_fd = os.open(
                source,
                os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            digest = hashlib.sha256()
            copied = 0
            while True:
                chunk = os.read(source_fd, _COPY_CHUNK_BYTES)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    written = os.write(temporary_fd, view)
                    if written <= 0:
                        raise FileVersionError("Short write during guarded restore")
                    view = view[written:]
                copied += len(chunk)
                digest.update(chunk)
            if copied != expected_size or digest.hexdigest() != expected_digest:
                raise FileVersionError("Recovery object changed during guarded restore")
            if mode is not None:
                os.fchmod(temporary_fd, mode)
            os.fsync(temporary_fd)
            os.close(temporary_fd)
            temporary_fd = -1

            prepared_identity = _version_inode_identity_at(
                parent_fd,
                temporary_name,
            )
            if prepared_identity is None:
                raise FileVersionError("Prepared recovery object disappeared")

            if expected_current is None:
                _version_atomic_rename(
                    parent_fd,
                    temporary_name,
                    parent_fd,
                    target_name,
                    exchange=False,
                )
            else:
                _version_atomic_rename(
                    parent_fd,
                    temporary_name,
                    parent_fd,
                    target_name,
                    exchange=True,
                )
                # The temporary now names the inode that was visible at the
                # destination.  It may still have an open descriptor, so it is
                # a durable recovery sidecar even after a successful restore.
                preserve_temporary = True
                displaced = _version_entry_at(parent_fd, temporary_name)
                displaced_identity = _version_inode_identity_at(
                    parent_fd,
                    temporary_name,
                )
                displaced_nlink = _version_nlink_at(parent_fd, temporary_name)
                restored_entry = _version_entry_at(parent_fd, target_name)
                expected_restored = {
                    "kind": "file",
                    "mode": mode if mode is not None else 0o600,
                    "size": expected_size,
                    "sha256": expected_digest,
                    "link_target": None,
                }
                target_identity = _version_inode_identity_at(parent_fd, target_name)
                restored_is_ours = (
                    restored_entry == expected_restored
                    and target_identity == prepared_identity
                    and _version_nlink_at(parent_fd, target_name) == 1
                )
                displaced_conflicted = (
                    displaced != expected_current
                    or displaced_identity is None
                    or displaced_nlink != 1
                )
                if displaced_conflicted and restored_is_ours:
                    # The exchange linearized after another writer changed or
                    # hard-linked the destination.  Put that exact inode back at
                    # the visible name before reporting the conflict.  The
                    # requested restore remains under the hidden temporary name;
                    # retaining it also keeps any descriptor opened during the
                    # short exchange window reachable.
                    _version_atomic_rename(
                        parent_fd,
                        temporary_name,
                        parent_fd,
                        target_name,
                        exchange=True,
                    )
                    if (
                        _version_inode_identity_at(parent_fd, target_name)
                        != displaced_identity
                        or _version_inode_identity_at(parent_fd, temporary_name)
                        != prepared_identity
                    ):
                        raise FileVersionError(
                            "Restore conflict could not be rolled back safely; "
                            f"both objects were preserved: {relative_path} "
                            f"({temporary_name})"
                        )
                    _verify_version_parent_fd_reachable(
                        self.workspace,
                        relative_path,
                        parent_fd,
                        expected_identity=self._workspace_identity,
                    )
                    _fsync_fd(parent_fd)
                    raise FileVersionError(
                        "Restore conflicted with a later edit or hard link; "
                        f"the original visible object was restored: {relative_path} "
                        f"({temporary_name})"
                    )
                if displaced_conflicted:
                    raise FileVersionError(
                        "Restore conflicted with a later edit or hard link and the "
                        "prepared restore also changed; both objects were preserved: "
                        f"{relative_path} ({temporary_name})"
                    )
                if restored_entry != expected_restored:
                    preserve_temporary = True
                    raise FileVersionError(
                        "Rollback target changed after guarded restore; conflict temporary "
                        f"was preserved: {relative_path} ({temporary_name})"
                    )
            try:
                _verify_version_parent_fd_reachable(
                    self.workspace,
                    relative_path,
                    parent_fd,
                    expected_identity=self._workspace_identity,
                )
            except FileVersionError:
                preserve_temporary = True
                raise
            if preserve_temporary:
                logger.warning(
                    "Preserving file-version recovery sidecar: %s",
                    self.workspace / Path(relative_path).parent / temporary_name,
                )
            else:
                try:
                    os.unlink(temporary_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
            _fsync_fd(parent_fd)
        except FileVersionError as exc:
            if preserve_temporary:
                sidecar = self.workspace / Path(relative_path).parent / temporary_name
                if str(sidecar) not in str(exc):
                    raise FileVersionError(
                        f"{exc}; recovery sidecar: {sidecar}"
                    ) from exc
            raise
        except FileExistsError as exc:
            raise FileVersionError(
                f"Rollback target was created by another writer: {relative_path}"
            ) from exc
        except OSError as exc:
            if preserve_temporary:
                sidecar = self.workspace / Path(relative_path).parent / temporary_name
                raise FileVersionError(
                    "Guarded restore failed after displacing the current target; "
                    f"recovery sidecar: {sidecar}: {exc}"
                ) from exc
            raise
        finally:
            if temporary_fd >= 0:
                os.close(temporary_fd)
            if source_fd >= 0:
                os.close(source_fd)
            if not preserve_temporary:
                try:
                    os.unlink(temporary_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
            os.close(parent_fd)

    def _copy_object_guarded_windows(
        self,
        source: Path,
        *,
        relative_path: str,
        expected_current: dict[str, object] | None,
        expected_digest: str,
        expected_size: int,
        mode: int | None,
    ) -> None:
        """Restore with ReplaceFileW backup validation and no path-name gap."""

        del mode  # ReplaceFileW preserves/merges the target's native metadata.
        target = self.workspace / Path(relative_path)
        temporary = target.parent / f".{target.name}.{generate_ulid()}.rollback.tmp"
        backup = target.parent / f".{target.name}.{generate_ulid()}.rollback-backup"
        conflict = target.parent / f".{target.name}.{generate_ulid()}.rollback-conflict"
        preserve: set[Path] = set()
        try:
            with locked_directory_chain(
                self.workspace,
                (relative_path,),
                expected_workspace_identity=self._workspace_identity,
            ) as api:
                if not target.parent.is_dir():
                    raise FileVersionError(
                        f"Restore parent does not exist or is redirected: {target.parent}"
                    )
                with open_regular_file_for_stable_read(source, backend=api) as (
                    source_fd,
                    source_info,
                ):
                    source_streams = api.stream_inventory(source)
                    if (
                        any(name != "::$DATA" for name, _size in source_streams)
                        or sum(size for _name, size in source_streams)
                        != expected_size
                    ):
                        raise FileVersionError(
                            "Recovery object has unaccounted Windows data streams"
                        )
                    if source_info.link_count != 1:
                        raise FileVersionError("Recovery object gained a hard link")
                    digest, size = _hash_regular_descriptor(source_fd)
                    if digest != expected_digest or size != expected_size:
                        raise FileVersionError(
                            "Recovery object changed during guarded Windows restore"
                        )
                    api.copy_file_full(source, temporary)
                    if api.stream_inventory(temporary) != source_streams:
                        raise FileVersionError(
                            "Prepared Windows restore stream inventory changed during copy"
                        )
                prepared_info = api.path_info(temporary, directory=False)
                if prepared_info.link_count != 1:
                    raise FileVersionError("Prepared Windows restore gained a hard link")

                if expected_current is None:
                    try:
                        api.move_noreplace(temporary, target)
                    except FileExistsError as exc:
                        raise FileVersionError(
                            f"Rollback target was created by another writer: {relative_path}"
                        ) from exc
                    restored = _windows_version_entry(api, target)
                    if (
                        restored is None
                        or restored["sha256"] != expected_digest
                        or restored["size"] != expected_size
                        or api.path_info(target, directory=False).identity
                        != prepared_info.identity
                    ):
                        preserve.add(target)
                        raise FileVersionError(
                            "New Windows restore target changed after its guarded install"
                        )
                    return

                exchange = GuardedExchange(target, temporary, backup)
                try:
                    exchange.install(api)
                except WindowsGuardedFileError as exc:
                    if exc.may_have_mutated:
                        try:
                            recovered_sidecars = _recover_partial_windows_restore(
                                api,
                                exchange,
                                conflict=conflict,
                            )
                            preserve.update(recovered_sidecars)
                        except FileVersionError:
                            for candidate in (temporary, backup, conflict):
                                if _windows_version_name_exists(candidate):
                                    preserve.add(candidate)
                            raise
                    raise FileVersionError(
                        "ReplaceFileW failed during guarded restore; the exact "
                        f"displaced object was restored: {exc}"
                    ) from exc
                preserve.add(backup)
                displaced = _windows_version_entry(api, backup)
                displaced_info = api.path_info(backup, directory=False)
                restored = _windows_version_entry(api, target)
                restored_info = api.path_info(target, directory=False)
                displaced_conflicted = (
                    displaced != expected_current or displaced_info.link_count != 1
                )
                restored_is_ours = (
                    restored is not None
                    and restored.get("sha256") == expected_digest
                    and restored.get("size") == expected_size
                    and restored_info.identity == prepared_info.identity
                    and restored_info.link_count == 1
                )
                if displaced_conflicted or not restored_is_ours:
                    if restored_is_ours:
                        try:
                            exchange.rollback(api, conflict)
                            preserve.discard(backup)
                            preserve.add(conflict)
                        except (WindowsGuardedFileError, FileExistsError) as exc:
                            raise FileVersionError(
                                "Windows restore conflict could not be rolled back; "
                                f"objects preserved at {backup} and {target}"
                            ) from exc
                        visible = api.path_info(target, directory=False)
                        if visible.identity != displaced_info.identity:
                            raise FileVersionError(
                                "Windows restore rollback did not recover displaced identity"
                            )
                    raise FileVersionError(
                        "Restore conflicted with a later edit or hard link; the "
                        f"original visible object was restored; sidecar: {conflict}"
                    )
                logger.warning(
                    "Preserving file-version recovery sidecar: %s",
                    backup,
                )
        except FileVersionError:
            raise
        except (OSError, WindowsGuardedFileError) as exc:
            raise FileVersionError(
                f"Guarded Windows restore failed for {relative_path}: {exc}"
            ) from exc
        finally:
            for candidate in (temporary, backup, conflict):
                if candidate in preserve:
                    continue
                try:
                    candidate.unlink()
                except FileNotFoundError:
                    pass

    def resolve_target(
        self,
        file_path: str | os.PathLike[str],
        *,
        for_write: bool,
    ) -> Path:
        """Canonicalize a target and enforce the selected workspace boundary."""

        raw = os.fspath(file_path)
        try:
            if for_write:
                resolved = resolve_for_write(raw, str(self.workspace))
            else:
                resolved = resolve_and_validate(raw, str(self.workspace))
        except WorkspaceViolation as exc:
            raise FileVersionError(str(exc)) from exc
        except OSError as exc:
            raise FileVersionError(f"Cannot resolve version target {raw}: {exc}") from exc
        target = Path(resolved)
        try:
            target.relative_to(self.workspace)
        except ValueError as exc:  # Defense in depth around resolver changes.
            raise FileVersionError(f"Target escaped workspace: {target}") from exc
        return target

    def _assert_workspace_identity(self) -> None:
        try:
            info = self.workspace.stat(follow_symlinks=False)
        except OSError as exc:
            raise FileVersionError("Workspace root changed during file-version operation") from exc
        identity = (
            windows_path_identity(self.workspace, directory=True)
            if sys.platform == "win32"
            else (info.st_dev, info.st_ino)
        )
        if (
            identity != self._workspace_identity
            or not stat.S_ISDIR(info.st_mode)
            or (sys.platform == "win32" and windows_lstat_is_reparse(info))
        ):
            raise FileVersionError("Workspace root changed during file-version operation")

    @staticmethod
    def _require_guarded_restore_support() -> None:
        reason = guarded_file_mutation_unavailable_reason()
        if reason is not None:
            raise FileVersionError(reason)

    def _target_from_relative(self, relative_path: str) -> Path:
        if sys.platform == "win32":
            try:
                validate_windows_relative_name(relative_path)
            except ValueError as exc:
                raise FileVersionError(
                    "File-version manifest contains an unsafe Windows path"
                ) from exc
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise FileVersionError("File-version manifest contains an unsafe path")
        lexical = self.workspace / relative
        current = self.workspace
        for component in relative.parts:
            current = current / component
            try:
                current_info = current.lstat()
                if current.is_symlink() or (
                    sys.platform == "win32" and windows_lstat_is_reparse(current_info)
                ):
                    raise FileVersionError(
                        f"Refusing to restore through a symbolic link: {current}"
                    )
            except FileNotFoundError:
                break
            except OSError as exc:
                raise FileVersionError(f"Cannot inspect restore path: {current}") from exc
        candidate = lexical if sys.platform == "win32" else lexical.resolve()
        try:
            candidate.relative_to(self.workspace)
        except ValueError as exc:
            raise FileVersionError("Restored path escaped the workspace") from exc
        return candidate

    def _ensure_store(self) -> None:
        for directory in (self.storage_root, self.root, self.objects_dir):
            try:
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            except OSError as exc:
                raise FileVersionError(
                    f"Cannot create private file-version storage: {directory}"
                ) from exc
            try:
                directory_info = directory.lstat()
            except OSError as exc:
                raise FileVersionError(
                    f"Cannot inspect private file-version storage: {directory}"
                ) from exc
            if (
                directory.is_symlink()
                or (sys.platform == "win32" and windows_lstat_is_reparse(directory_info))
                or not directory.is_dir()
            ):
                raise FileVersionError(
                    f"File-version storage contains a redirected/non-directory path: {directory}"
                )
            try:
                os.chmod(directory, 0o700)
            except OSError:
                # Windows applies the per-user ACL inherited from app data.
                pass

    def _copy_target_to_temporary(
        self,
        target: Path,
    ) -> tuple[Path, str, int, int | None]:
        if sys.platform == "win32":
            return self._copy_windows_target_to_temporary(target)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        relative_path = target.relative_to(self.workspace).as_posix()
        parent_fd = -1
        try:
            parent_fd, target_name = _open_version_parent_fd(
                self.workspace,
                relative_path,
                expected_identity=self._workspace_identity,
            )
            source_fd = os.open(target_name, flags, dir_fd=parent_fd)
        except OSError as exc:
            if parent_fd >= 0:
                os.close(parent_fd)
            raise FileVersionError(f"Cannot safely open file for versioning: {target}") from exc

        temporary_fd = -1
        temporary_path: Path | None = None
        try:
            before = os.fstat(source_fd)
            if not stat.S_ISREG(before.st_mode):
                raise FileVersionError(f"Only regular files can be versioned: {target}")
            if before.st_size > self.limits.max_file_bytes:
                raise FileVersionError(
                    f"Refusing to modify {target}: existing file is {before.st_size} bytes, "
                    f"above the {self.limits.max_file_bytes}-byte recovery limit"
                )
            temporary_fd, temporary_name = tempfile.mkstemp(
                prefix=".snapshot-",
                suffix=".tmp",
                dir=self.objects_dir,
            )
            temporary_path = Path(temporary_name)
            digest = hashlib.sha256()
            copied = 0
            with os.fdopen(temporary_fd, "wb") as destination:
                temporary_fd = -1
                while True:
                    chunk = os.read(source_fd, _COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > self.limits.max_file_bytes:
                        raise FileVersionError(
                            f"File grew beyond the recovery limit while being snapshotted: {target}"
                        )
                    destination.write(chunk)
                    digest.update(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            after = os.fstat(source_fd)
            try:
                current_path = os.stat(
                    target_name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise FileVersionError(
                    f"File changed while its recovery snapshot was being created: {target}"
                ) from exc
            if (
                before.st_dev != after.st_dev
                or before.st_ino != after.st_ino
                or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or copied != after.st_size
                or current_path.st_dev != after.st_dev
                or current_path.st_ino != after.st_ino
                or not stat.S_ISREG(current_path.st_mode)
            ):
                raise FileVersionError(
                    f"File changed while its recovery snapshot was being created: {target}"
                )
            try:
                os.chmod(temporary_path, 0o600)
            except OSError:
                pass
            _verify_version_parent_fd_reachable(
                self.workspace,
                relative_path,
                parent_fd,
                expected_identity=self._workspace_identity,
            )
            return (
                temporary_path,
                digest.hexdigest(),
                copied,
                stat.S_IMODE(before.st_mode),
            )
        except Exception:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise
        finally:
            os.close(source_fd)
            if parent_fd >= 0:
                os.close(parent_fd)
            if temporary_fd >= 0:
                os.close(temporary_fd)

    def _copy_windows_target_to_temporary(
        self,
        target: Path,
    ) -> tuple[Path, str, int, int | None]:
        """Capture bytes plus ACLs/attributes/named streams through CopyFileW."""

        relative = target.relative_to(self.workspace).as_posix()
        temporary = self.objects_dir / f".snapshot-{generate_ulid()}.tmp"
        try:
            with locked_directory_chain(
                self.workspace,
                (relative,),
                expected_workspace_identity=self._workspace_identity,
            ) as api:
                try:
                    path_info = target.lstat()
                except FileNotFoundError as exc:
                    raise FileVersionError(
                        f"File changed before its recovery snapshot was created: {target}"
                    ) from exc
                if windows_lstat_is_reparse(path_info) or not stat.S_ISREG(
                    path_info.st_mode
                ):
                    raise FileVersionError(
                        f"Only non-reparse regular files can be versioned: {target}"
                    )
                with open_regular_file_for_stable_read(target, backend=api) as (
                    source_fd,
                    native,
                ):
                    streams = api.stream_inventory(target)
                    if any(name != "::$DATA" for name, _size in streams):
                        raise FileVersionError(
                            "Windows recovery snapshots with alternate data streams "
                            "are unavailable until stream sizes participate in quotas"
                        )
                    if sum(size for _name, size in streams) != native.size:
                        raise FileVersionError(
                            "Windows stream inventory does not match the default file size"
                        )
                    if native.link_count != 1:
                        raise FileVersionError(
                            f"Refusing to version multiply-linked Windows file: {target}"
                        )
                    if native.size > self.limits.max_file_bytes:
                        raise FileVersionError(
                            f"Refusing to modify {target}: existing file is "
                            f"{native.size} bytes, above the "
                            f"{self.limits.max_file_bytes}-byte recovery limit"
                        )
                    digest, size = _hash_regular_descriptor(source_fd)
                    if size != native.size:
                        raise FileVersionError(
                            f"File changed while its recovery snapshot was created: {target}"
                        )
                    api.copy_file_full(target, temporary)
                    copied_streams = api.stream_inventory(temporary)
                    if copied_streams != streams:
                        raise FileVersionError(
                            "Windows recovery snapshot stream inventory changed during copy"
                        )
                    copied_digest, copied_size = _sha256_regular_file(temporary)
                    if copied_digest != digest or copied_size != size:
                        raise FileVersionError(
                            f"Windows recovery snapshot does not match source: {target}"
                        )
                    after = os.fstat(source_fd)
                    if after.st_size != size:
                        raise FileVersionError(
                            f"File changed while its recovery snapshot was created: {target}"
                        )
                    return temporary, digest, size, stat.S_IMODE(after.st_mode)
        except FileVersionError:
            temporary.unlink(missing_ok=True)
            raise
        except (OSError, WindowsGuardedFileError) as exc:
            temporary.unlink(missing_ok=True)
            raise FileVersionError(
                f"Could not capture full Windows recovery snapshot: {target}: {exc}"
            ) from exc

    def _install_object(
        self,
        temporary: Path,
        digest: str,
        size: int,
        *,
        object_name: str | None = None,
    ) -> Path:
        destination = self.objects_dir / (object_name or f"{digest}.blob")
        if destination.exists():
            self._verify_object(destination, digest=digest, size=size)
            temporary.unlink(missing_ok=True)
            return destination
        os.replace(temporary, destination)
        try:
            os.chmod(destination, 0o600)
        except OSError:
            pass
        _fsync_directory(self.objects_dir)
        return destination

    def _object_path(self, version: FileVersion) -> Path:
        return self.objects_dir / (version.object_name or f"{version.sha256}.blob")

    def _load_manifest(self, *, create: bool = True) -> dict[str, Any]:
        self._assert_workspace_identity()
        if not self.manifest_path.exists():
            if create:
                self._ensure_store()
            return {"versions": [], "pins": {}}
        manifest_info = self.manifest_path.lstat()
        if self.manifest_path.is_symlink() or (
            sys.platform == "win32" and windows_lstat_is_reparse(manifest_info)
        ):
            raise FileVersionError("File-version manifest cannot be a symbolic link")
        try:
            raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FileVersionError("File-version manifest is unreadable or corrupt") from exc
        if (
            not isinstance(raw, dict)
            or raw.get("schema_version") not in SUPPORTED_MANIFEST_SCHEMA_VERSIONS
        ):
            raise FileVersionError("Unsupported file-version manifest schema")
        if raw.get("workspace") != str(self.workspace):
            raise FileVersionError("File-version manifest belongs to a different workspace")
        raw_identity = raw.get("workspace_identity")
        if (
            not isinstance(raw_identity, dict)
            or type(raw_identity.get("dev")) is not int
            or type(raw_identity.get("ino")) is not int
            or (
                raw_identity["dev"],
                raw_identity["ino"],
            )
            != self._workspace_identity
        ):
            raise FileVersionError(
                "File-version manifest belongs to a different workspace identity"
            )
        values = raw.get("versions")
        if not isinstance(values, list):
            raise FileVersionError("File-version manifest has an invalid versions list")
        versions = [FileVersion.from_dict(value) for value in values]
        pins = _parse_manifest_pins(raw.get("pins", {}), versions=versions)
        return {"versions": versions, "pins": pins}

    def _write_manifest(
        self,
        versions: list[FileVersion],
        *,
        pins: dict[str, list[str]] | None = None,
    ) -> None:
        self._assert_workspace_identity()
        normalized_pins = _parse_manifest_pins(pins or {}, versions=versions)
        payload = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "workspace": str(self.workspace),
            "workspace_identity": {
                "dev": self._workspace_identity[0],
                "ino": self._workspace_identity[1],
            },
            "versions": [asdict(version) for version in versions],
            "pins": normalized_pins,
        }
        atomic_write_text(
            self.manifest_path,
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            mode=0o600,
        )

    def _apply_retention(
        self,
        versions: list[FileVersion],
        *,
        newest: FileVersion,
        pinned_version_ids: frozenset[str] = frozenset(),
    ) -> list[FileVersion]:
        ordered = sorted(versions, key=_version_sort_key, reverse=True)
        required_ids = {newest.id, *pinned_version_ids}
        available_ids = {version.id for version in ordered}
        missing_pins = pinned_version_ids - available_ids
        if missing_pins:
            raise FileVersionError("A pinned restore source is missing from the manifest")

        # Required records are selected before ordinary newest-first retention.
        # This makes restore a safe two-record transaction: both the displaced
        # current contents and the selected source remain recoverable until the
        # target replacement succeeds.
        retained = [version for version in ordered if version.id in required_ids]
        path_counts: dict[str, int] = {}
        retained_hashes: set[str] = set()
        retained_bytes = 0
        for version in retained:
            path_counts[version.relative_path] = (
                path_counts.get(version.relative_path, 0) + 1
            )
            if path_counts[version.relative_path] > self.limits.max_versions_per_file:
                raise FileVersionError(
                    "The recovery snapshot and restore source exceed the per-file retention limit"
                )
            object_key = version.object_name or version.sha256
            if object_key not in retained_hashes:
                retained_hashes.add(object_key)
                retained_bytes += version.size
        if len(retained) > self.limits.max_total_versions:
            raise FileVersionError(
                "The recovery snapshot and restore source exceed the total retention limit"
            )
        if retained_bytes > self.limits.max_workspace_bytes:
            raise FileVersionError(
                "The recovery snapshot and restore source exceed the workspace retention limit"
            )

        for version in ordered:
            if version.id in required_ids:
                continue
            if len(retained) >= self.limits.max_total_versions:
                break
            count = path_counts.get(version.relative_path, 0)
            if count >= self.limits.max_versions_per_file:
                continue
            object_key = version.object_name or version.sha256
            additional = 0 if object_key in retained_hashes else version.size
            if retained_bytes + additional > self.limits.max_workspace_bytes:
                continue
            retained.append(version)
            path_counts[version.relative_path] = count + 1
            if object_key not in retained_hashes:
                retained_hashes.add(object_key)
                retained_bytes += version.size

        if not any(version.id == newest.id for version in retained):
            raise FileVersionError("Could not retain the required pre-mutation snapshot")
        return sorted(retained, key=_version_sort_key)

    def _delete_unreferenced_objects(self, versions: list[FileVersion]) -> None:
        referenced = {
            version.object_name or f"{version.sha256}.blob" for version in versions
        }
        try:
            candidates = list(self.objects_dir.iterdir())
        except OSError:
            return
        for candidate in candidates:
            if candidate.name.startswith(".snapshot-") or (
                candidate.suffix == ".blob" and candidate.name not in referenced
            ):
                try:
                    candidate.unlink()
                except OSError:
                    logger.warning("Could not prune file-version object %s", candidate)

    def _verify_object(self, path: Path, *, digest: str, size: int) -> None:
        try:
            path_info = path.lstat()
            if path.is_symlink() or (
                sys.platform == "win32" and windows_lstat_is_reparse(path_info)
            ):
                raise FileVersionError(
                    f"Recovery object cannot be a symbolic link: {path}"
                )
            actual, actual_size = _sha256_regular_file(path)
            if actual_size != size:
                raise FileVersionError(f"Recovery object is missing or has the wrong size: {path}")
        except OSError as exc:
            raise FileVersionError(f"Cannot read recovery object: {path}") from exc
        if actual != digest:
            raise FileVersionError(f"Recovery object checksum mismatch: {path}")

    def _copy_object_atomically(
        self,
        source: Path,
        target: Path,
        *,
        expected_digest: str,
        expected_size: int,
        mode: int | None,
    ) -> None:
        if sys.platform == "win32":
            self._copy_object_guarded(
                source,
                relative_path=target.relative_to(self.workspace).as_posix(),
                expected_current=None,
                expected_digest=expected_digest,
                expected_size=expected_size,
                mode=mode,
            )
            return
        temporary_fd, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".restore.tmp",
            dir=target.parent,
        )
        temporary = Path(temporary_name)
        digest = hashlib.sha256()
        copied = 0
        source_fd = -1
        try:
            source_fd = os.open(
                source,
                os.O_RDONLY
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            source_stat = os.fstat(source_fd)
            if not stat.S_ISREG(source_stat.st_mode):
                raise FileVersionError("Recovery object is not a regular file")
            with os.fdopen(source_fd, "rb") as src, os.fdopen(temporary_fd, "wb") as dst:
                source_fd = -1
                temporary_fd = -1
                while True:
                    chunk = src.read(_COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    copied += len(chunk)
                    digest.update(chunk)
                    dst.write(chunk)
                dst.flush()
                os.fsync(dst.fileno())
            if copied != expected_size or digest.hexdigest() != expected_digest:
                raise FileVersionError("Recovery object changed during restore")
            if mode is not None:
                try:
                    os.chmod(temporary, mode)
                except OSError:
                    pass
            os.replace(temporary, target)
            _fsync_directory(target.parent)
        finally:
            if temporary_fd >= 0:
                os.close(temporary_fd)
            if source_fd >= 0:
                os.close(source_fd)
            temporary.unlink(missing_ok=True)


def _lock_for_workspace(workspace_key: str) -> threading.RLock:
    with _LOCKS_GUARD:
        return _WORKSPACE_LOCKS.setdefault(workspace_key, threading.RLock())


def _workspace_storage_key(
    workspace: Path,
    identity: tuple[int, int],
) -> str:
    digest = hashlib.sha256()
    digest.update(os.fsencode(str(workspace)))
    digest.update(b"\0")
    digest.update(str(identity[0]).encode("ascii"))
    digest.update(b"\0")
    digest.update(str(identity[1]).encode("ascii"))
    return digest.hexdigest()


def _safe_operation(value: str) -> str:
    operation = str(value).strip()[:80]
    return operation or "write"


def _safe_pin_owner(value: Any) -> str:
    if not isinstance(value, str):
        raise FileVersionError("pin owner must be a string")
    owner = value.strip()
    if not owner or len(owner) > 240:
        raise FileVersionError("pin owner must contain between 1 and 240 characters")
    return owner


def _safe_version_ids(values: Any) -> frozenset[str]:
    if isinstance(values, (str, bytes)):
        raise FileVersionError("version_ids must be a collection")
    try:
        raw_values = list(values)
    except TypeError as exc:
        raise FileVersionError("version_ids must be a collection") from exc
    if any(not isinstance(value, str) for value in raw_values):
        raise FileVersionError("version_ids must contain strings")
    requested = frozenset(value.strip() for value in raw_values)
    if any(not value or len(value) > 200 for value in requested):
        raise FileVersionError("version_ids contains an invalid identifier")
    return requested


def _parse_manifest_pins(
    value: Any,
    *,
    versions: list[FileVersion],
) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        raise FileVersionError("File-version manifest has an invalid pins map")
    available = {version.id for version in versions}
    pins: dict[str, list[str]] = {}
    for raw_owner, raw_ids in value.items():
        if not isinstance(raw_owner, str):
            raise FileVersionError("File-version manifest has an invalid pin owner")
        owner = _safe_pin_owner(raw_owner)
        if not isinstance(raw_ids, list):
            raise FileVersionError("File-version manifest has an invalid pin list")
        requested = _safe_version_ids(raw_ids)
        if requested - available:
            raise FileVersionError(
                "File-version manifest pins a version that is not retained"
            )
        if requested:
            pins[owner] = sorted(requested)
    return pins


def _manifest_pinned_version_ids(
    pins: dict[str, list[str]],
) -> frozenset[str]:
    return frozenset(version_id for values in pins.values() for version_id in values)


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:200] if text else None


def _version_sort_key(version: FileVersion) -> tuple[int, str]:
    return version.created_at_ns, version.id


def _sha256_regular_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    file_stat = os.fstat(descriptor)
    if not stat.S_ISREG(file_stat.st_mode):
        os.close(descriptor)
        raise FileVersionError(f"Recovery object is not a regular file: {path}")
    with os.fdopen(descriptor, "rb") as handle:
        for chunk in iter(lambda: handle.read(_COPY_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest(), file_stat.st_size


def _hash_regular_descriptor(descriptor: int) -> tuple[str, int]:
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


def _open_version_parent_fd(
    workspace: Path,
    relative_path: str,
    *,
    expected_identity: tuple[int, int],
) -> tuple[int, str]:
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise FileVersionError("File-version manifest contains an unsafe path")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(workspace, flags)
    try:
        root_info = os.fstat(descriptor)
        if (root_info.st_dev, root_info.st_ino) != expected_identity:
            raise FileVersionError("Workspace root changed during guarded restore")
        for component in relative.parts[:-1]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor, relative.parts[-1]
    except Exception:
        os.close(descriptor)
        raise


def _ensure_version_parent_fd(
    workspace: Path,
    relative_path: str,
    *,
    expected_identity: tuple[int, int],
) -> tuple[int, str]:
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise FileVersionError("File-version manifest contains an unsafe path")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(workspace, flags)
    try:
        root_info = os.fstat(descriptor)
        if (root_info.st_dev, root_info.st_ino) != expected_identity:
            raise FileVersionError("Workspace root changed during guarded restore")
        for component in relative.parts[:-1]:
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                os.mkdir(component, mode=0o755, dir_fd=descriptor)
                child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor, relative.parts[-1]
    except Exception:
        os.close(descriptor)
        raise


def _verify_version_parent_fd_reachable(
    workspace: Path,
    relative_path: str,
    held_parent_fd: int,
    *,
    expected_identity: tuple[int, int],
) -> None:
    current_fd, _target_name = _open_version_parent_fd(
        workspace,
        relative_path,
        expected_identity=expected_identity,
    )
    try:
        held = os.fstat(held_parent_fd)
        current = os.fstat(current_fd)
        if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
            raise FileVersionError(
                f"Workspace parent moved during file-version operation: {relative_path}"
            )
    finally:
        os.close(current_fd)


def _version_atomic_rename(
    source_fd: int,
    source_name: str,
    destination_fd: int,
    destination_name: str,
    *,
    exchange: bool,
) -> None:
    reason = guarded_file_mutation_unavailable_reason()
    if reason is not None:
        raise FileVersionError(reason)
    libc = ctypes.CDLL(None, use_errno=True)
    source = os.fsencode(source_name)
    destination = os.fsencode(destination_name)
    if sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        function = libc.renameat2
        flags = 2 if exchange else 1
    elif sys.platform == "darwin" and hasattr(libc, "renameatx_np"):
        function = libc.renameatx_np
        flags = 2 if exchange else 4
    else:
        raise FileVersionError("Guarded file-version restore is unavailable on this platform")
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    if function(source_fd, source, destination_fd, destination, flags) != 0:
        error = ctypes.get_errno()
        if not exchange and error == errno.EEXIST:
            raise FileExistsError(error, os.strerror(error), destination_name)
        raise OSError(error, os.strerror(error), destination_name)


def _version_entry_at(parent_fd: int, name: str) -> dict[str, object] | None:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return {
            "kind": "symlink" if stat.S_ISLNK(info.st_mode) else "other",
            "mode": stat.S_IMODE(info.st_mode),
            "size": 0,
            "sha256": None,
            "link_target": (
                os.readlink(name, dir_fd=parent_fd) if stat.S_ISLNK(info.st_mode) else None
            ),
        }
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )
    try:
        opened = os.fstat(descriptor)
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(descriptor, _COPY_CHUNK_BYTES)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        after = os.fstat(descriptor)
        if (
            current.st_dev != after.st_dev
            or current.st_ino != after.st_ino
            or opened.st_dev != after.st_dev
            or opened.st_ino != after.st_ino
            or current.st_size != size
            or after.st_size != size
            or current.st_mtime_ns != after.st_mtime_ns
            or opened.st_mtime_ns != after.st_mtime_ns
            or stat.S_IMODE(current.st_mode) != stat.S_IMODE(after.st_mode)
        ):
            raise FileVersionError("Rollback target changed while being inspected")
        return {
            "kind": "file",
            "mode": stat.S_IMODE(after.st_mode),
            "size": size,
            "sha256": digest.hexdigest(),
            "link_target": None,
        }
    finally:
        os.close(descriptor)


def _windows_version_entry(
    api: Win32Backend,
    path: Path,
) -> dict[str, object] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if windows_lstat_is_reparse(info):
        return {
            "kind": "symlink",
            "mode": stat.S_IMODE(info.st_mode),
            "size": 0,
            "sha256": None,
            "link_target": None,
        }
    if not stat.S_ISREG(info.st_mode):
        return {
            "kind": "other",
            "mode": stat.S_IMODE(info.st_mode),
            "size": 0,
            "sha256": None,
            "link_target": None,
        }
    with open_regular_file_for_stable_read(path, backend=api) as (descriptor, native):
        digest, size = _hash_regular_descriptor(descriptor)
        if size != native.size:
            raise FileVersionError("Rollback target changed while being inspected")
        after = os.fstat(descriptor)
        return {
            "kind": "file",
            "mode": stat.S_IMODE(after.st_mode),
            "size": size,
            "sha256": digest,
            "link_target": None,
        }


def _windows_version_name_exists(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if windows_lstat_is_reparse(info):
        raise FileVersionError(f"Windows recovery name is redirected: {path}")
    return True


def _recover_partial_windows_restore(
    api: Win32Backend,
    exchange: GuardedExchange,
    *,
    conflict: Path,
) -> set[Path]:
    """Recover every documented partial ReplaceFileW error state."""

    preserved: set[Path] = set()
    if not _windows_version_name_exists(exchange.displaced):
        if _windows_version_name_exists(exchange.replacement):
            preserved.add(exchange.replacement)
        return preserved
    displaced_info = api.path_info(exchange.displaced, directory=False)
    try:
        if _windows_version_name_exists(exchange.target):
            try:
                exchange.rollback(api, conflict)
                preserved.add(conflict)
            except WindowsGuardedFileError as rollback_exc:
                if not rollback_exc.may_have_mutated:
                    raise
                target_exists = _windows_version_name_exists(exchange.target)
                if target_exists and api.path_info(
                    exchange.target, directory=False
                ).identity == displaced_info.identity:
                    if _windows_version_name_exists(conflict):
                        preserved.add(conflict)
                elif (
                    not target_exists
                    and _windows_version_name_exists(exchange.displaced)
                ):
                    api.move_noreplace(exchange.displaced, exchange.target)
                    if api.path_info(
                        exchange.target, directory=False
                    ).identity != displaced_info.identity:
                        raise FileVersionError(
                            "Partial rollback restored a different Windows object"
                        )
                    if _windows_version_name_exists(conflict):
                        preserved.add(conflict)
                else:
                    raise
        else:
            api.move_noreplace(exchange.displaced, exchange.target)
        restored_info = api.path_info(exchange.target, directory=False)
        if restored_info.identity != displaced_info.identity:
            raise FileVersionError(
                "Partial Windows restore recovered a different target identity"
            )
    except (OSError, WindowsGuardedFileError, FileExistsError) as exc:
        raise FileVersionError(
            "Partial ReplaceFileW restore could not recover the displaced "
            f"object; names preserved near {exchange.target}"
        ) from exc
    if _windows_version_name_exists(exchange.replacement):
        preserved.add(exchange.replacement)
    return preserved


def _version_nlink_at(parent_fd: int, name: str) -> int:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return 0
    return info.st_nlink


def _version_inode_identity_at(
    parent_fd: int,
    name: str,
) -> tuple[int, int] | None:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return info.st_dev, info.st_ino


def _fsync_fd(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    except OSError:
        pass


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
