"""Typed failures for the v1.1 managed Git worktree service."""

from __future__ import annotations


class WorktreeError(RuntimeError):
    """Base class for a worktree operation that failed safely."""


class WorktreeFeatureDisabled(WorktreeError):
    """The v1.1 Beta feature is disabled by the active runtime policy."""


class GitUnavailableError(WorktreeError):
    """A usable Git executable could not be found."""


class GitCommandError(WorktreeError):
    """A supervised Git command returned a non-success status."""

    def __init__(
        self,
        operation: str,
        *,
        returncode: int,
        stderr: str = "",
    ) -> None:
        self.operation = operation
        self.returncode = returncode
        self.stderr = stderr
        detail = stderr.strip() or f"exit status {returncode}"
        super().__init__(f"Git {operation} failed: {detail}")


class GitCommandTimeout(GitCommandError):
    """A Git command exceeded its deadline and its process tree was stopped."""

    def __init__(self, operation: str, *, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds
        super().__init__(
            operation,
            returncode=-1,
            stderr=f"timed out after {timeout_seconds:g} seconds",
        )


class RepositoryValidationError(WorktreeError):
    """The source is not a supported, trustworthy clean Git worktree."""


class WorktreePathError(WorktreeError):
    """A managed path or workspace identifier is unsafe."""


class WorktreeNotFoundError(WorktreeError):
    """No owned worktree manifest exists for the requested instance."""


class WorktreeOwnershipError(WorktreeError):
    """A path or manifest does not prove application ownership."""


class WorktreeDirtyError(WorktreeError):
    """A source or managed worktree contains uncommitted changes."""


class WorktreeActiveError(WorktreeError):
    """Runtime/checkpoint references make detach or removal unsafe."""


class WorktreeConflictError(WorktreeError):
    """A lifecycle, branch, path, or Git-registration conflict exists."""
