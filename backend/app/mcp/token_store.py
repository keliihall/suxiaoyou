"""Persistent token storage for MCP OAuth tokens."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from app.auth.credential_store import (
    CredentialStore,
    CredentialStoreError,
    StagedSecretTree,
    get_credential_store,
    prepare_stale_secret_cleanup,
    resolve_secret_tree,
    stage_protected_secret_tree,
)
from app.mcp.oauth import AuthServerMeta, TokenSet
from app.utils.atomic_write import atomic_write_text

logger = logging.getLogger(__name__)


class McpTokenStore:
    """Persist OAuth tokens per MCP server to a JSON file.

    Tokens live under the per-user application data directory (the backend
    process working directory in packaged desktop mode), never inside the user
    workspace. A hash of the project path retains the previous per-project
    separation without exposing the path in the filename.
    """

    def __init__(
        self,
        project_dir: str | None = None,
        *,
        storage_root: str | Path | None = None,
        credential_store: CredentialStore | None = None,
    ) -> None:
        scope = str(Path(project_dir).expanduser().resolve()) if project_dir else "global"
        scope_hash = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:20]
        root = (
            Path(storage_root).expanduser().resolve()
            if storage_root is not None
            else Path.cwd().resolve() / "data" / "credentials"
        )
        self._path = root / "mcp" / f"{scope_hash}.json"
        self._namespace = f"mcp:{scope_hash}"
        if credential_store is not None:
            self._credential_store = credential_store
        elif storage_root is None:
            # Production MCP credentials share the process-wide store and its
            # one macOS aggregate-vault read/cache with provider credentials.
            self._credential_store = get_credential_store()
        else:
            # Explicit storage roots are isolated test/embedding instances.
            self._credential_store = CredentialStore(
                fallback_path=root / "fallback.json"
            )

        # v0.8.x stored OAuth credentials in the selected workspace. Import the
        # legacy file once, persist it privately, then remove it only after the
        # atomic destination write succeeds.
        if project_dir:
            self._legacy_path = (
                Path(project_dir).expanduser().resolve()
                / ".suxiaoyou"
                / "mcp-tokens.json"
            )
        else:
            self._legacy_path = Path.home() / ".suxiaoyou" / "mcp-tokens.json"
        self._data: dict[str, dict[str, Any]] = self._load()
        self._migrate_legacy_file()
        self._migrate_namespaced_keys()

    @property
    def path(self) -> Path:
        """Return the private storage path for diagnostics and tests."""

        return self._path

    def get(self, server_name: str) -> TokenSet | None:
        """Retrieve stored tokens for a server."""
        protected_entry = self._data.get(server_name)
        if not protected_entry:
            return None
        # Keep native credential access at the connector-use boundary. Startup
        # and ``has_token`` only need protected metadata and must never open an
        # OS vault while the desktop is still becoming ready.
        entry = resolve_secret_tree(
            protected_entry,
            store=self._credential_store,
        )
        if not isinstance(entry, dict):
            return None
        return TokenSet(
            access_token=entry.get("access_token", ""),
            refresh_token=entry.get("refresh_token"),
            expires_at=entry.get("expires_at", 0.0),
            token_type=entry.get("token_type", "Bearer"),
            scope=entry.get("scope", ""),
        )

    def get_auth_meta(self, server_name: str) -> AuthServerMeta | None:
        """Retrieve stored auth server metadata for a server."""
        entry = self._data.get(server_name)
        if not entry or "auth_meta" not in entry:
            return None
        meta = entry["auth_meta"]
        return AuthServerMeta(
            authorization_endpoint=meta.get("authorization_endpoint", ""),
            token_endpoint=meta.get("token_endpoint", ""),
            scopes=meta.get("scopes", []),
            resource_url=meta.get("resource_url", ""),
            registration_endpoint=meta.get("registration_endpoint", ""),
            client_id_metadata_document_supported=meta.get(
                "client_id_metadata_document_supported", False
            ),
        )

    def save(
        self,
        server_name: str,
        tokens: TokenSet,
        auth_meta: AuthServerMeta | None = None,
    ) -> None:
        """Store tokens for a server."""
        entry: dict[str, Any] = {
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "expires_at": tokens.expires_at,
            "token_type": tokens.token_type,
            "scope": tokens.scope,
        }
        if auth_meta:
            entry["auth_meta"] = {
                "authorization_endpoint": auth_meta.authorization_endpoint,
                "token_endpoint": auth_meta.token_endpoint,
                "scopes": auth_meta.scopes,
                "resource_url": auth_meta.resource_url,
                "registration_endpoint": auth_meta.registration_endpoint,
                "client_id_metadata_document_supported": auth_meta.client_id_metadata_document_supported,
            }
        previous = copy.deepcopy(self._data.get(server_name))
        self._data[server_name] = entry
        if not self._persist():
            if previous is None:
                self._data.pop(server_name, None)
            else:
                self._data[server_name] = previous
            raise CredentialStoreError("MCP OAuth tokens could not be persisted")

    def delete(self, server_name: str) -> None:
        """Remove stored tokens for a server."""
        if server_name in self._data:
            previous = self._data.pop(server_name)
            if not self._persist():
                self._data[server_name] = previous
                raise CredentialStoreError("MCP OAuth token deletion could not be persisted")

    def has_token(self, server_name: str) -> bool:
        """Check if we have tokens stored for a server."""
        return server_name in self._data

    def get_client_id(self, server_name: str) -> str | None:
        """Retrieve stored client_id from dynamic registration."""
        entry = self._data.get(server_name)
        if entry:
            return entry.get("client_id")
        return None

    def save_client_id(self, server_name: str, client_id: str) -> None:
        """Store a dynamically registered client_id."""
        previous = copy.deepcopy(self._data.get(server_name))
        entry = self._data.setdefault(server_name, {})
        entry["client_id"] = client_id
        if not self._persist():
            if previous is None:
                self._data.pop(server_name, None)
            else:
                self._data[server_name] = previous
            raise CredentialStoreError("MCP client registration could not be persisted")

    def _migrate_namespaced_keys(self) -> None:
        """Migrate old plugin-namespaced keys (e.g. 'engineering:slack') to
        plain connector IDs (e.g. 'slack').

        When multiple namespaced keys map to the same connector, keep the
        one with the most recent expiry.
        """
        migrated = False
        old_keys = [k for k in self._data if ":" in k]
        for old_key in old_keys:
            new_key = old_key.split(":", 1)[1]
            entry = self._data[old_key]

            if new_key not in self._data:
                self._data[new_key] = entry
            else:
                # Keep the one with the later expiry
                existing_expiry = self._data[new_key].get("expires_at", 0)
                new_expiry = entry.get("expires_at", 0)
                if new_expiry > existing_expiry:
                    self._data[new_key] = entry

            del self._data[old_key]
            migrated = True

        if migrated:
            logger.info("Migrated %d namespaced token key(s)", len(old_keys))
            if not self._persist():
                raise CredentialStoreError("Namespaced MCP credentials could not be migrated")

    def _load(self) -> dict[str, dict[str, Any]]:
        data, previous_exists, previous_content = self._read_file_snapshot(self._path)
        if data is None:
            return {}
        staged = stage_protected_secret_tree(
            self._namespace,
            data,
            previous_value=data,
            store=self._credential_store,
        )
        protected = staged.value
        if protected != data:
            cleanup_transaction = None
            try:
                next_text = json.dumps(protected, indent=2, ensure_ascii=False) + "\n"
                cleanup_transaction = prepare_stale_secret_cleanup(
                    data,
                    protected,
                    evidence_path=self._path,
                    previous_exists=previous_exists,
                    previous_content=previous_content,
                    next_exists=True,
                    next_content=next_text,
                    store=self._credential_store,
                )
                self._write_protected(next_text)
            except Exception as exc:
                if cleanup_transaction is not None:
                    cleanup_transaction.cancel()
                self._discard_failed_stage(staged)
                raise CredentialStoreError(
                    f"Cannot erase plaintext MCP credentials in {self._path}: {exc}"
                ) from exc
            if cleanup_transaction is not None:
                cleanup_transaction.commit()
        return protected if isinstance(protected, dict) else {}

    @staticmethod
    def _read_file(path: Path) -> dict[str, dict[str, Any]] | None:
        data, _, _ = McpTokenStore._read_file_snapshot(path)
        return data

    @staticmethod
    def _read_file_snapshot(
        path: Path,
    ) -> tuple[dict[str, dict[str, Any]] | None, bool, bytes]:
        if not path.is_file():
            return {}, False, b""
        try:
            content = path.read_bytes()
            data = json.loads(content)
            if isinstance(data, dict) and all(
                isinstance(key, str) and isinstance(value, dict)
                for key, value in data.items()
            ):
                return data, True, content
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            logger.warning("Cannot read MCP tokens from %s: %s", path, e)
        return None, True, b""

    def _migrate_legacy_file(self) -> None:
        if self._legacy_path == self._path or not self._legacy_path.is_file():
            return
        legacy = self._read_file(self._legacy_path)
        if legacy is None:
            # An unreadable or malformed credential file must be retained for
            # explicit recovery; silently deleting it could force re-auth.
            return

        for key, entry in legacy.items():
            existing = self._data.get(key)
            if existing is None or float(entry.get("expires_at", 0) or 0) > float(
                existing.get("expires_at", 0) or 0
            ):
                self._data[key] = entry

        if self._persist():
            try:
                self._legacy_path.unlink()
            except OSError as e:
                logger.warning(
                    "Imported MCP tokens but could not remove legacy file %s: %s",
                    self._legacy_path,
                    e,
                )
            else:
                logger.info("Migrated MCP OAuth tokens out of workspace %s", self._legacy_path)

    def _persist(self) -> bool:
        previous, previous_exists, previous_content = self._read_file_snapshot(
            self._path
        )
        if previous is None:
            logger.warning(
                "Refusing to overwrite unreadable MCP credential metadata: %s",
                self._path,
            )
            return False
        try:
            staged = stage_protected_secret_tree(
                self._namespace,
                self._data,
                previous_value=previous,
                store=self._credential_store,
            )
        except Exception as e:
            logger.warning("Cannot protect MCP tokens: %s", e)
            return False
        cleanup_transaction = None
        try:
            next_text = json.dumps(staged.value, indent=2, ensure_ascii=False) + "\n"
            cleanup_transaction = prepare_stale_secret_cleanup(
                previous,
                staged.value,
                evidence_path=self._path,
                previous_exists=previous_exists,
                previous_content=previous_content,
                next_exists=True,
                next_content=next_text,
                store=self._credential_store,
            )
            self._write_protected(next_text)
        except Exception as e:
            if cleanup_transaction is not None:
                cleanup_transaction.cancel()
            self._discard_failed_stage(staged)
            logger.warning("Cannot persist MCP tokens: %s", e)
            return False
        if cleanup_transaction is not None:
            cleanup_transaction.commit()
        return True

    def _discard_failed_stage(self, staged: StagedSecretTree) -> None:
        installed = self._read_file(self._path)
        if installed is None:
            installed = staged.value
        staged.discard_unreferenced((installed,))

    def _write_protected(self, next_text: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            self._path,
            next_text,
            mode=0o600,
        )
