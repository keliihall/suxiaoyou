"""Platform-aware workspace lifecycle for command execution.

Linux and macOS execute against the existing private, versioned workspace
transaction.  Windows currently executes in the user-approved workspace
directly: the Win32 Job Object is a process-lifetime boundary, but Windows has
no mount namespace with which to present a private copy at the original path.
Keeping this distinction explicit avoids claiming rollback guarantees that a
native terminal cannot provide.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import sys
from dataclasses import dataclass

from app.tool.context import ToolContext
from app.tool.sandbox import SandboxUnavailable, validate_workspace_private_boundary
from app.tool.workspace_transaction import (
    WorkspaceCommitResult,
    WorkspaceMutationTransaction,
)


class ExecutionWorkspace:
    """Prepare, map, commit, and clean one command's workspace view."""

    def __init__(
        self,
        workspace: str | os.PathLike[str],
        ctx: ToolContext,
        *,
        operation: str,
    ) -> None:
        self.workspace = validate_workspace_private_boundary(workspace)
        self._session_key = hashlib.sha256(
            ctx.session_id.encode("utf-8", errors="surrogatepass"),
        ).hexdigest()[:32]
        self._direct = sys.platform == "win32"
        self._transaction = (
            None
            if self._direct
            else WorkspaceMutationTransaction(self.workspace, ctx, operation=operation)
        )
        self._scratch_paths: list[Path] = []
        self._direct_baseline: _DirectWorkspaceSnapshot | None = None
        self._direct_written_files: tuple[str, ...] = ()
        self._direct_deleted_files: tuple[str, ...] = ()
        self._direct_tracking_complete = True
        self._staged_workspace: Path | None = None
        self._transaction_root_for_redaction: Path | None = None
        self._scratch_paths_for_redaction: list[Path] = []
        self._persistent_environment_prepared = False

    @property
    def transactional(self) -> bool:
        return not self._direct

    @property
    def failure_metadata(self) -> dict[str, object]:
        environment_metadata = {
            "execution_environment_scope": (
                "session" if self._persistent_environment_prepared else "ephemeral"
            ),
            "persistent_environment_changes_may_have_persisted": (
                self._persistent_environment_prepared
            ),
        }
        if self.transactional:
            return {
                "workspace_transaction": True,
                "workspace_changes_committed": False,
                **environment_metadata,
            }
        return {
            "workspace_transaction": False,
            "direct_workspace_execution": True,
            # A native terminal writes as it runs.  Be explicit that a failed
            # command may have changed files before returning non-zero.
            "workspace_changes_may_have_persisted": True,
            **environment_metadata,
        }

    def prepare(self) -> Path:
        if self._transaction is not None:
            self._staged_workspace = self._transaction.prepare()
            self._transaction_root_for_redaction = self._transaction.transaction_root
            return self._staged_workspace
        if not self.workspace.is_dir():
            raise SandboxUnavailable(f"Workspace does not exist: {self.workspace}")
        self._direct_baseline = _snapshot_direct_workspace(self.workspace)
        self._staged_workspace = self.workspace
        return self.workspace

    def staged_path(self, logical_path: str | os.PathLike[str]) -> Path:
        if self._transaction is not None:
            return self._transaction.staged_path(logical_path)
        path = Path(logical_path)
        if not path.is_absolute():
            path = self.workspace / path
        try:
            path.relative_to(self.workspace)
        except ValueError as exc:
            raise SandboxUnavailable(
                f"Execution path is outside the workspace: {path}"
            ) from exc
        return path

    def create_scratch(self, *, prefix: str) -> tuple[Path, Path]:
        if self._transaction is not None:
            scratch = self._transaction.create_scratch(prefix=prefix)
            self._scratch_paths_for_redaction.append(scratch[0])
            return scratch

        internal = self.workspace / ".suxiaoyou"
        sandbox_root = internal / "sandbox"
        for path in (internal, sandbox_root):
            if path.exists() and (
                path.is_symlink()
                or bool(getattr(os.path, "isjunction", lambda _value: False)(path))
                or not path.is_dir()
            ):
                raise SandboxUnavailable("Sandbox scratch path is redirected")
            path.mkdir(mode=0o700, exist_ok=True)
        scratch = sandbox_root / f"{_safe_prefix(prefix)}{secrets.token_hex(12)}"
        scratch.mkdir(mode=0o700)
        self._scratch_paths.append(scratch)
        self._scratch_paths_for_redaction.append(scratch)
        return scratch, scratch

    def create_persistent_environment(self) -> Path:
        """Return a stable, session-isolated HOME/cache root.

        The environment is application-private workspace state, not a user
        artifact.  It deliberately lives under ``.suxiaoyou`` so ordinary
        workspace transactions neither publish it nor discard it between tool
        calls.  The opaque session digest prevents command-controlled path
        components and keeps credentials/configuration isolated by session.
        """

        environment = (
            self.workspace
            / ".suxiaoyou"
            / "execution-environments"
            / self._session_key
        )
        _ensure_unredirected_directory_chain(
            self.workspace,
            (".suxiaoyou", "execution-environments", self._session_key),
        )
        for relative in ("home", "cache"):
            _ensure_unredirected_directory_chain(
                self.workspace,
                (
                    ".suxiaoyou",
                    "execution-environments",
                    self._session_key,
                    relative,
                ),
            )
        self._persistent_environment_prepared = True
        return environment

    def redact_output(self, value: str) -> str:
        """Hide per-call physical transaction/scratch paths from the model.

        A physical staging path is deleted immediately after this call.  If it
        is returned verbatim, the next model turn commonly copies that stale
        path into a command and enters a failure loop.  Logical workspace paths
        remain actionable; scratch paths are explicitly marked temporary.
        """

        redacted = value
        for scratch in sorted(
            self._scratch_paths_for_redaction,
            key=lambda path: len(str(path)),
            reverse=True,
        ):
            redacted = redacted.replace(
                str(scratch),
                "<temporary-execution-directory>",
            )
        staged = self._staged_workspace
        if staged is not None and staged != self.workspace:
            redacted = redacted.replace(str(staged), str(self.workspace))
        if self._transaction_root_for_redaction is not None:
            redacted = redacted.replace(
                str(self._transaction_root_for_redaction),
                "<private-execution-transaction>",
            )
        redacted = _STALE_TRANSACTION_PATH_RE.sub(str(self.workspace), redacted)
        redacted = _STALE_SCRATCH_PATH_RE.sub(
            "<temporary-execution-directory>",
            redacted,
        )
        redacted = _STALE_TRANSACTION_FRAGMENT_RE.sub(
            "<private-execution-transaction>",
            redacted,
        )
        redacted = _STALE_SCRATCH_FRAGMENT_RE.sub(
            "<temporary-execution-directory>",
            redacted,
        )
        return redacted

    def commit(self) -> WorkspaceCommitResult | None:
        if self._transaction is not None:
            return self._transaction.commit()
        self._cleanup_scratch()
        baseline = self._direct_baseline or _DirectWorkspaceSnapshot({}, False)
        current = _snapshot_direct_workspace(self.workspace)
        self._direct_written_files = tuple(
            str(self.workspace / relative)
            for relative, identity in current.files.items()
            if baseline.files.get(relative) != identity
        )
        self._direct_deleted_files = tuple(
            str(self.workspace / relative)
            for relative in baseline.files
            if relative not in current.files
        )
        self._direct_tracking_complete = baseline.complete and current.complete
        return None

    def success_metadata(
        self,
        commit: WorkspaceCommitResult | None,
    ) -> dict[str, object]:
        if commit is not None:
            return {
                "workspace_changes_committed": True,
                "persistent_environment_changes_may_have_persisted": (
                    self._persistent_environment_prepared
                ),
                **commit.metadata,
            }
        return {
            "workspace_transaction": False,
            "direct_workspace_execution": True,
            "workspace_changes_committed": True,
            "atomic_file_install": False,
            "written_files": list(self._direct_written_files),
            "deleted_files": list(self._direct_deleted_files),
            "artifact_tracking_complete": self._direct_tracking_complete,
            "persistent_environment_changes_may_have_persisted": (
                self._persistent_environment_prepared
            ),
        }

    def abort(self) -> None:
        if self._transaction is not None:
            self._transaction.abort()
        self._cleanup_scratch()

    def _cleanup_scratch(self) -> None:
        for path in reversed(self._scratch_paths):
            shutil.rmtree(path, ignore_errors=True)
        self._scratch_paths.clear()


def _ensure_unredirected_directory_chain(
    workspace: Path,
    components: tuple[str, ...],
) -> Path:
    """Create an app-owned directory chain without accepting redirections."""

    current = workspace
    is_junction = getattr(os.path, "isjunction", lambda _value: False)
    for component in components:
        current = current / component
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        try:
            info = current.lstat()
        except OSError as exc:
            raise SandboxUnavailable(
                "Persistent execution environment is unavailable",
            ) from exc
        if current.is_symlink() or is_junction(current) or not current.is_dir():
            raise SandboxUnavailable(
                "Persistent execution environment path is redirected",
            )
        try:
            resolved = current.resolve(strict=True)
            resolved.relative_to(workspace)
        except (OSError, ValueError) as exc:
            raise SandboxUnavailable(
                "Persistent execution environment escaped the workspace",
            ) from exc
        if not stat.S_ISDIR(info.st_mode):
            raise SandboxUnavailable(
                "Persistent execution environment path is not a directory",
            )
    return current


def _safe_prefix(value: str) -> str:
    cleaned = "".join(character for character in value if character.isalnum() or character in "-_")
    return cleaned[:80] or "execution-"


_DIRECT_SNAPSHOT_MAX_ENTRIES = 100_000
_STALE_TRANSACTION_PATH_RE = re.compile(
    r"(?:[A-Za-z]:[/\\]|/)[^\s\"']*execution-transactions[/\\]"
    r"[^\s\"']+[/\\]tx-[^/\\\s\"']+[/\\]workspace",
)
_STALE_SCRATCH_PATH_RE = re.compile(
    r"(?:[A-Za-z]:[/\\]|/)[^\s\"']*\.suxiaoyou[/\\]sandbox"
    r"[/\\][^/\\\s\"']+",
)
_STALE_TRANSACTION_FRAGMENT_RE = re.compile(r"\S*execution-transactions\S*")
_STALE_SCRATCH_FRAGMENT_RE = re.compile(r"\S*\.suxiaoyou[/\\]sandbox\S*")
_DIRECT_SNAPSHOT_IGNORED_DIRECTORIES = frozenset(
    {".git", ".suxiaoyou", ".venv", "venv", "node_modules", "__pycache__"}
)


@dataclass(frozen=True, slots=True)
class _DirectWorkspaceSnapshot:
    files: dict[str, tuple[int, int, int]]
    complete: bool


def _snapshot_direct_workspace(workspace: Path) -> _DirectWorkspaceSnapshot:
    """Capture a bounded file identity map for honest Windows artifact delivery.

    A Job Object cannot roll back direct terminal writes, but a before/after
    snapshot still lets the normal artifact pipeline surface files created or
    changed by a successful command.  Dependency/cache trees are deliberately
    excluded: they are implementation inputs, not user deliverables.
    """

    files: dict[str, tuple[int, int, int]] = {}
    complete = True
    pending = [workspace]
    seen_entries = 0
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name)
        except OSError:
            complete = False
            continue
        for entry in entries:
            seen_entries += 1
            if seen_entries > _DIRECT_SNAPSHOT_MAX_ENTRIES:
                complete = False
                pending.clear()
                break
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name not in _DIRECT_SNAPSHOT_IGNORED_DIRECTORIES:
                        pending.append(Path(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                info = entry.stat(follow_symlinks=False)
            except OSError:
                complete = False
                continue
            relative = Path(entry.path).relative_to(workspace).as_posix()
            # st_ino is meaningful on NTFS/ReFS in CPython and catches atomic
            # replacement even when size and timestamp happen to match.
            files[relative] = (int(info.st_size), int(info.st_mtime_ns), int(info.st_ino))
    return _DirectWorkspaceSnapshot(files, complete)


__all__ = ["ExecutionWorkspace"]
