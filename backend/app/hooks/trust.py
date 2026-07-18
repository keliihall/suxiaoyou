"""Durable, content-bound trust for project and plugin command Hooks."""

from __future__ import annotations

import os
from pathlib import Path

from app.hooks.registry import CommandHook
from app.mcp.local_approval import (
    LocalMcpApprovalStore,
    LocalMcpApprovalStoreError,
)


class HookApprovalRequired(PermissionError):
    """Raised before spawn when the current command identity is untrusted."""

    def __init__(self, hook: CommandHook) -> None:
        self.hook = hook
        super().__init__(
            f"Hook approval is required for {hook.declaration.hook_id}: "
            f"{hook.fingerprint}"
        )


class HookTrustStoreError(RuntimeError):
    """Raised when Hook trust cannot be persisted safely."""


class HookTrustStore:
    """Per-workspace Hook fingerprints backed by the hardened MCP store.

    Reusing ``LocalMcpApprovalStore`` gives Hook trust the same no-follow path
    checks, restrictive permissions, atomic replacement, directory fsync, and
    durable read-back verification as local MCP launch approval.
    """

    def __init__(
        self,
        project_dir: str | Path,
        *,
        storage_root: str | Path | None = None,
    ) -> None:
        if storage_root is None:
            private_root = os.environ.get("SUXIAOYOU_PRIVATE_DATA_DIR") or os.getcwd()
            root = Path(private_root) / "security" / "hook-command-trust"
        else:
            root = Path(storage_root)
        self._store = LocalMcpApprovalStore(
            str(project_dir),
            storage_root=root,
        )

    @property
    def path(self) -> Path:
        return self._store.path

    @property
    def degraded_reason(self) -> str | None:
        return self._store.degraded_reason

    def is_approved(self, hook: CommandHook) -> bool:
        return self._store.get(hook.trust_key) == hook.fingerprint

    def require_approved(self, hook: CommandHook) -> None:
        if not self.is_approved(hook):
            raise HookApprovalRequired(hook)

    def approve(self, hook: CommandHook) -> None:
        try:
            self._store.approve(hook.trust_key, hook.fingerprint)
        except LocalMcpApprovalStoreError as exc:
            raise HookTrustStoreError(
                "Hook trust could not be persisted safely"
            ) from exc

    def revoke(self, hook: CommandHook) -> bool:
        """Remove the durable trust entry for this Hook identity."""

        try:
            return self._store.revoke(hook.trust_key)
        except LocalMcpApprovalStoreError as exc:
            raise HookTrustStoreError(
                "Hook trust revocation could not be persisted safely"
            ) from exc


__all__ = [
    "HookApprovalRequired",
    "HookTrustStore",
    "HookTrustStoreError",
]
