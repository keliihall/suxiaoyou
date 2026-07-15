"""Tests for app.mcp.token_store — OAuth token persistence."""

from __future__ import annotations

import pytest

pytest.importorskip("mcp")

import hashlib
import json
import os
import stat
from pathlib import Path

from app.auth.credential_store import CredentialStore, CredentialStoreError
from app.mcp.oauth import AuthServerMeta, TokenSet
from app.mcp.token_store import McpTokenStore


@pytest.fixture(autouse=True)
def _disable_host_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.auth.credential_store._discover_native_backend",
        lambda: None,
    )


def _make_tokens(**kwargs) -> TokenSet:
    defaults = {"access_token": "at_123", "refresh_token": "rt_456", "expires_at": 99999999.0}
    defaults.update(kwargs)
    return TokenSet(**defaults)


def _make_auth_meta() -> AuthServerMeta:
    return AuthServerMeta(
        authorization_endpoint="https://auth.example.com/authorize",
        token_endpoint="https://auth.example.com/token",
        scopes=["read"],
        resource_url="https://mcp.example.com/sse",
    )


class TestMcpTokenStore:
    def test_default_store_reuses_process_credential_store(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        shared = CredentialStore(
            fallback_path=tmp_path / "shared-fallback.json",
            native_backend=None,
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            "app.mcp.token_store.get_credential_store",
            lambda: shared,
        )

        store = McpTokenStore(project_dir=str(tmp_path / "workspace"))

        assert store._credential_store is shared

    def test_protected_tokens_are_not_resolved_during_store_startup(
        self,
        tmp_path: Path,
    ) -> None:
        class ExplodingReadBackend:
            def __init__(self) -> None:
                self.reads = 0

            def get_password(self, _service: str, _username: str) -> str | None:
                self.reads += 1
                raise AssertionError("native credential read before connector use")

            def set_password(self, *_args) -> None:
                return None

            def delete_password(self, *_args) -> None:
                return None

        backend = ExplodingReadBackend()
        storage_root = tmp_path / "private"
        credential_backend = CredentialStore(
            fallback_path=storage_root / "fallback.json",
            native_backend=backend,
        )
        scope = str(tmp_path.resolve())
        scope_hash = hashlib.sha256(scope.encode()).hexdigest()[:20]
        token_path = storage_root / "mcp" / f"{scope_hash}.json"
        token_path.parent.mkdir(parents=True)
        token_path.write_text(
            json.dumps(
                {
                    "slack": {
                        "access_token": "suxiaoyou-credential://mcp-access",
                        "refresh_token": "suxiaoyou-credential://mcp-refresh",
                        "expires_at": 99999999.0,
                    }
                }
            ),
            encoding="utf-8",
        )

        store = McpTokenStore(
            project_dir=str(tmp_path),
            storage_root=storage_root,
            credential_store=credential_backend,
        )

        assert store.has_token("slack") is True
        assert backend.reads == 0
        with pytest.raises(CredentialStoreError, match="cannot be resolved"):
            store.get("slack")
        assert backend.reads == 1

    def test_save_and_get_round_trip(self, tmp_path: Path):
        store = McpTokenStore(project_dir=str(tmp_path), storage_root=tmp_path / "private")
        tokens = _make_tokens()
        store.save("slack", tokens)
        got = store.get("slack")
        assert got is not None
        assert got.access_token == "at_123"
        assert got.refresh_token == "rt_456"
        assert got.expires_at == 99999999.0

    def test_get_nonexistent(self, tmp_path: Path):
        store = McpTokenStore(project_dir=str(tmp_path), storage_root=tmp_path / "private")
        assert store.get("nonexistent") is None

    def test_has_token(self, tmp_path: Path):
        store = McpTokenStore(project_dir=str(tmp_path), storage_root=tmp_path / "private")
        store.save("slack", _make_tokens())
        assert store.has_token("slack") is True
        assert store.has_token("other") is False

    def test_delete(self, tmp_path: Path):
        store = McpTokenStore(project_dir=str(tmp_path), storage_root=tmp_path / "private")
        store.save("slack", _make_tokens())
        store.delete("slack")
        assert store.has_token("slack") is False

    def test_save_with_auth_meta(self, tmp_path: Path):
        store = McpTokenStore(project_dir=str(tmp_path), storage_root=tmp_path / "private")
        store.save("slack", _make_tokens(), auth_meta=_make_auth_meta())
        meta = store.get_auth_meta("slack")
        assert meta is not None
        assert meta.authorization_endpoint == "https://auth.example.com/authorize"
        assert meta.scopes == ["read"]

    def test_get_auth_meta_none(self, tmp_path: Path):
        store = McpTokenStore(project_dir=str(tmp_path), storage_root=tmp_path / "private")
        store.save("slack", _make_tokens())  # no auth_meta
        assert store.get_auth_meta("slack") is None

    def test_client_id_round_trip(self, tmp_path: Path):
        store = McpTokenStore(project_dir=str(tmp_path), storage_root=tmp_path / "private")
        store.save_client_id("slack", "cid_123")
        assert store.get_client_id("slack") == "cid_123"

    def test_persistence_to_disk(self, tmp_path: Path):
        store1 = McpTokenStore(project_dir=str(tmp_path), storage_root=tmp_path / "private")
        store1.save("slack", _make_tokens())
        # New instance reads from disk
        store2 = McpTokenStore(project_dir=str(tmp_path), storage_root=tmp_path / "private")
        got = store2.get("slack")
        assert got is not None
        assert got.access_token == "at_123"

    def test_failed_save_is_reported_and_rolled_back_in_memory(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from app.mcp import token_store

        credential_backend = CredentialStore(
            fallback_path=tmp_path / "private" / "fallback.json",
            native_backend=None,
        )
        store = McpTokenStore(
            project_dir=str(tmp_path),
            storage_root=tmp_path / "private",
            credential_store=credential_backend,
        )

        def fail_write(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(token_store, "atomic_write_text", fail_write)
        with pytest.raises(CredentialStoreError, match="could not be persisted"):
            store.save("slack", _make_tokens())
        assert store.has_token("slack") is False
        fallback = json.loads(
            credential_backend.fallback_path.read_text(encoding="utf-8")
        )
        assert fallback["credentials"] == {}

    def test_serialization_failure_is_reported_and_rolled_back_without_orphans(
        self,
        tmp_path: Path,
    ) -> None:
        credential_backend = CredentialStore(
            fallback_path=tmp_path / "private" / "fallback.json",
            native_backend=None,
        )
        store = McpTokenStore(
            project_dir=str(tmp_path),
            storage_root=tmp_path / "private",
            credential_store=credential_backend,
        )

        with pytest.raises(CredentialStoreError, match="could not be persisted"):
            store.save("slack", _make_tokens(scope="\ud800"))

        assert store.has_token("slack") is False
        assert not store.path.exists()
        fallback = json.loads(
            credential_backend.fallback_path.read_text(encoding="utf-8")
        )
        assert fallback["credentials"] == {}

    @pytest.mark.skipif(os.name == "nt", reason="POSIX mode semantics")
    def test_token_file_is_private_and_outside_workspace_metadata(self, tmp_path: Path):
        store = McpTokenStore(
            project_dir=str(tmp_path),
            storage_root=tmp_path / "private",
        )
        store.save("slack", _make_tokens())

        assert store.path.parent.parent == (tmp_path / "private").resolve()
        assert store.path != tmp_path / ".suxiaoyou" / "mcp-tokens.json"
        persisted = store.path.read_text(encoding="utf-8")
        assert "at_123" not in persisted
        assert "rt_456" not in persisted
        assert "suxiaoyou-credential://" in persisted
        assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


class TestMigrateNamespacedKeys:
    def test_migrates_colon_key(self, tmp_path: Path):
        # Pre-populate JSON with namespaced key
        tokens_path = tmp_path / ".suxiaoyou" / "mcp-tokens.json"
        tokens_path.parent.mkdir(parents=True)
        tokens_path.write_text(json.dumps({
            "engineering:slack": {"access_token": "at", "expires_at": 100}
        }))
        store = McpTokenStore(project_dir=str(tmp_path), storage_root=tmp_path / "private")
        assert store.get("slack") is not None
        assert store.has_token("engineering:slack") is False
        assert not tokens_path.exists()

    def test_keeps_latest_expiry(self, tmp_path: Path):
        tokens_path = tmp_path / ".suxiaoyou" / "mcp-tokens.json"
        tokens_path.parent.mkdir(parents=True)
        tokens_path.write_text(json.dumps({
            "a:slack": {"access_token": "old", "expires_at": 100},
            "b:slack": {"access_token": "new", "expires_at": 200},
        }))
        store = McpTokenStore(project_dir=str(tmp_path), storage_root=tmp_path / "private")
        got = store.get("slack")
        assert got is not None
        assert got.access_token == "new"

    def test_no_migration_needed(self, tmp_path: Path):
        tokens_path = tmp_path / ".suxiaoyou" / "mcp-tokens.json"
        tokens_path.parent.mkdir(parents=True)
        tokens_path.write_text(json.dumps({
            "slack": {"access_token": "at", "expires_at": 100}
        }))
        store = McpTokenStore(project_dir=str(tmp_path), storage_root=tmp_path / "private")
        assert store.get("slack") is not None

    def test_failed_legacy_import_keeps_workspace_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from app.mcp import token_store

        tokens_path = tmp_path / ".suxiaoyou" / "mcp-tokens.json"
        tokens_path.parent.mkdir(parents=True)
        tokens_path.write_text(
            json.dumps({"slack": {"access_token": "keep", "expires_at": 100}}),
            encoding="utf-8",
        )

        def fail_write(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(token_store, "atomic_write_text", fail_write)
        store = McpTokenStore(
            project_dir=str(tmp_path),
            storage_root=tmp_path / "private",
        )

        assert store.get("slack") is not None
        assert tokens_path.exists()
        assert not store.path.exists()
