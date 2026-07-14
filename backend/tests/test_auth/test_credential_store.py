"""Credential references, native storage, and private fallback behavior."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from app.auth.credential_store import (
    CredentialStore,
    CredentialStoreError,
    is_credential_reference,
    migrate_settings_credentials,
    protect_secret_tree,
    resolve_secret_tree,
    stage_protected_secret_tree,
)
from app.auth import credential_store
from app.config import Settings


class MemoryBackend:
    def __init__(
        self,
        *,
        fail_writes: bool = False,
        fail_deletes: bool = False,
    ) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.fail_writes = fail_writes
        self.fail_deletes = fail_deletes

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        if self.fail_writes:
            raise RuntimeError("locked")
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if self.fail_deletes:
            raise RuntimeError("locked")
        self.values.pop((service, username), None)


@pytest.fixture
def v08_legacy_credential_env(tmp_path: Path) -> tuple[Path, str]:
    """A byte-for-byte example of the v0.8 shell-style dotenv writer."""

    endpoint_name = r"C:\profiles\new\O'Brien\workspace"
    endpoint_json = json.dumps(
        [
            {
                "id": "legacy_custom",
                "name": endpoint_name,
                "api_key": "legacy-custom-secret",
            }
        ],
        separators=(",", ":"),
    )
    legacy_value = endpoint_json.replace("'", "'\\''")
    env_path = tmp_path / ".env"
    env_path.write_text(
        f"SUXIAOYOU_CUSTOM_ENDPOINTS='{legacy_value}'\n",
        encoding="utf-8",
    )
    return env_path, endpoint_name


class PartialWriteBackend(MemoryBackend):
    """Simulate an OS vault that commits and then reports write failure."""

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password
        raise RuntimeError("vault acknowledgement lost")


def test_native_backend_returns_opaque_reference_without_fallback(tmp_path: Path) -> None:
    backend = MemoryBackend()
    fallback = tmp_path / "fallback.json"
    store = CredentialStore(fallback_path=fallback, native_backend=backend)

    reference = store.put("env:SUXIAOYOU_OPENAI_API_KEY", "sk-native")

    assert is_credential_reference(reference)
    assert "sk-native" not in reference
    assert store.resolve(reference) == "sk-native"
    assert not fallback.exists()

    store.delete(reference)
    assert store.get(reference) is None


def test_native_write_failure_uses_owner_only_json_fallback(tmp_path: Path) -> None:
    fallback = tmp_path / "fallback.json"
    store = CredentialStore(
        fallback_path=fallback,
        native_backend=MemoryBackend(fail_writes=True),
    )

    reference = store.put("env:SUXIAOYOU_OPENAI_API_KEY", "sk-fallback")

    assert store.resolve(reference) == "sk-fallback"
    assert json.loads(fallback.read_text(encoding="utf-8"))["credentials"]
    if os.name != "nt":
        assert stat.S_IMODE(fallback.stat().st_mode) == 0o600


def test_partial_native_write_is_durably_tracked_if_fallback_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = PartialWriteBackend(fail_deletes=True)
    fallback = tmp_path / "fallback.json"
    store = CredentialStore(fallback_path=fallback, native_backend=backend)
    real_atomic_write = credential_store.atomic_write_text
    fallback_writes = 0

    def fail_second_fallback_write(path, content, **kwargs):
        nonlocal fallback_writes
        if Path(path) == fallback:
            fallback_writes += 1
            if fallback_writes == 2:
                raise OSError("disk full after journal")
        return real_atomic_write(path, content, **kwargs)

    monkeypatch.setattr(credential_store, "atomic_write_text", fail_second_fallback_write)
    with pytest.raises(OSError, match="disk full after journal"):
        store.put("partial-native", "orphan-must-be-recoverable")

    persisted = json.loads(fallback.read_text(encoding="utf-8"))
    assert persisted["pending_native_deletions"] == ["partial-native"]
    assert backend.values

    monkeypatch.setattr(credential_store, "atomic_write_text", real_atomic_write)
    backend.fail_deletes = False
    recovered = CredentialStore(fallback_path=fallback, native_backend=backend)
    assert recovered.get("partial-native") is None
    assert backend.values == {}


def test_native_delete_is_journaled_and_retried_after_backend_recovers(
    tmp_path: Path,
) -> None:
    backend = MemoryBackend()
    fallback = tmp_path / "fallback.json"
    store = CredentialStore(fallback_path=fallback, native_backend=backend)
    reference = store.put("native-delete", "secret")

    backend.fail_deletes = True
    store.delete(reference)

    pending = json.loads(fallback.read_text(encoding="utf-8"))
    assert pending["credentials"] == {}
    assert pending["pending_native_deletions"] == ["native-delete"]
    assert store.get(reference) == "secret"
    if os.name != "nt":
        assert stat.S_IMODE(fallback.stat().st_mode) == 0o600

    # Initialization also retries, but remains non-blocking while the native
    # service is still locked.
    CredentialStore(fallback_path=fallback, native_backend=backend)
    backend.fail_deletes = False
    recovered = CredentialStore(fallback_path=fallback, native_backend=backend)

    assert recovered.get(reference) is None
    completed = json.loads(fallback.read_text(encoding="utf-8"))
    assert completed["pending_native_deletions"] == []


def test_native_delete_journal_is_retried_by_later_operation(
    tmp_path: Path,
) -> None:
    backend = MemoryBackend()
    fallback = tmp_path / "fallback.json"
    store = CredentialStore(fallback_path=fallback, native_backend=backend)
    reference = store.put("operation-retry", "secret")

    backend.fail_deletes = True
    store.delete(reference)
    backend.fail_deletes = False

    # Every public operation retries the durable journal before doing its own
    # work, so recovery does not depend on an application restart.
    assert store.get(reference) is None
    completed = json.loads(fallback.read_text(encoding="utf-8"))
    assert completed["pending_native_deletions"] == []


def test_cleanup_recovery_without_native_backend_retains_native_delete(
    tmp_path: Path,
) -> None:
    backend = MemoryBackend()
    fallback = tmp_path / "fallback.json"
    evidence = tmp_path / "config.json"
    evidence.write_text("old", encoding="utf-8")
    native_store = CredentialStore(fallback_path=fallback, native_backend=backend)
    reference = native_store.put("cleanup-native-outage", "secret")
    transaction = native_store.prepare_cleanup_transaction(
        [reference],
        evidence_path=evidence,
        previous_exists=True,
        previous_content="old",
        next_exists=True,
        next_content="new",
    )
    assert transaction is not None
    evidence.write_text("new", encoding="utf-8")

    # Simulate restart while Secret Service/DBus cannot be discovered. Recovery
    # may retire the config transaction, but it must retain the OS-vault delete.
    CredentialStore(fallback_path=fallback, native_backend=None)
    pending = json.loads(fallback.read_text(encoding="utf-8"))
    assert pending["cleanup_transactions"] == {}
    assert pending["pending_native_deletions"] == ["cleanup-native-outage"]
    assert backend.values

    recovered = CredentialStore(fallback_path=fallback, native_backend=backend)
    assert recovered.get(reference) is None
    assert backend.values == {}
    completed = json.loads(fallback.read_text(encoding="utf-8"))
    assert completed["pending_native_deletions"] == []


def test_native_delete_fails_explicitly_if_journal_cannot_be_persisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = MemoryBackend()
    store = CredentialStore(
        fallback_path=tmp_path / "fallback.json",
        native_backend=backend,
    )
    reference = store.put("journal-write", "secret")
    backend.fail_deletes = True

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(credential_store, "atomic_write_text", fail_write)
    with pytest.raises(OSError, match="disk full"):
        store.delete(reference)


def test_two_store_instances_do_not_lose_interleaved_fallback_updates(
    tmp_path: Path,
) -> None:
    fallback = tmp_path / "fallback.json"
    first = CredentialStore(fallback_path=fallback, native_backend=None)
    second = CredentialStore(fallback_path=fallback, native_backend=None)

    first_ref = first.put("first", "one")
    second_ref = second.put("second", "two")
    first.delete(first_ref)

    persisted = json.loads(fallback.read_text(encoding="utf-8"))["credentials"]
    assert "first" not in persisted
    assert persisted["second"] == "two"
    assert first.resolve(second_ref) == "two"


def test_corrupt_fallback_is_rejected_fail_closed(tmp_path: Path) -> None:
    fallback = tmp_path / "fallback.json"
    fallback.write_text("[]", encoding="utf-8")

    with pytest.raises(CredentialStoreError, match="unsupported format"):
        CredentialStore(fallback_path=fallback, native_backend=None)


def test_existing_v1_fallback_without_deletion_journal_remains_readable(
    tmp_path: Path,
) -> None:
    fallback = tmp_path / "fallback.json"
    fallback.write_text(
        json.dumps(
            {
                "version": 1,
                "credentials": {"legacy": "still-readable"},
            }
        ),
        encoding="utf-8",
    )

    store = CredentialStore(fallback_path=fallback, native_backend=None)

    assert store.get("legacy") == "still-readable"


def test_secret_tree_protects_tokens_and_headers(tmp_path: Path) -> None:
    store = CredentialStore(
        fallback_path=tmp_path / "fallback.json",
        native_backend=None,
    )
    plaintext = {
        "access_token": "access-secret",
        "metadata": {"name": "kept"},
        "headers": {"Authorization": "Bearer header-secret"},
    }

    protected = protect_secret_tree("test", plaintext, store=store)

    serialized = json.dumps(protected)
    assert "access-secret" not in serialized
    assert "header-secret" not in serialized
    assert resolve_secret_tree(protected, store=store) == plaintext


def test_secret_tree_staging_failure_removes_only_new_references() -> None:
    class PartiallyFailingStore:
        def __init__(self) -> None:
            self.created: list[str] = []
            self.deleted: list[str] = []

        def put(self, identifier: str, _secret: str) -> str:
            if self.created:
                raise CredentialStoreError("credential backend stopped")
            reference = f"suxiaoyou-credential://{identifier}"
            self.created.append(reference)
            return reference

        def delete(self, reference: str) -> None:
            self.deleted.append(reference)

    store = PartiallyFailingStore()
    value = {
        "api_key": "first-secret",
        "headers": {"Authorization": "second-secret"},
    }

    with pytest.raises(CredentialStoreError, match="backend stopped"):
        stage_protected_secret_tree(
            "tree-stage-test",
            value,
            store=store,  # type: ignore[arg-type]
        )

    assert store.deleted == store.created


def test_app_env_plaintext_is_migrated_and_runtime_is_hydrated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "SUXIAOYOU_OPENAI_API_KEY='sk-provider-plain'\n"
        "SUXIAOYOU_CUSTOM_ENDPOINTS='"
        '[{"id":"custom_demo","api_key":"custom-plain",'
        '"headers":{"Authorization":"Bearer header-plain"}}]'
        "'\n"
        "SUXIAOYOU_LOCAL_BASE_URL='http://127.0.0.1:11434/v1'\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=env_path)
    store = CredentialStore(
        fallback_path=tmp_path / "fallback.json",
        native_backend=None,
    )
    monkeypatch.setattr(credential_store, "get_credential_store", lambda: store)

    migrated = migrate_settings_credentials(settings, env_path, store=store)

    persisted = env_path.read_text(encoding="utf-8")
    assert migrated == 2
    assert "sk-provider-plain" not in persisted
    assert "custom-plain" not in persisted
    assert "header-plain" not in persisted
    assert "suxiaoyou-credential://" in persisted
    assert settings.openai_api_key == "sk-provider-plain"
    assert "custom-plain" in settings.custom_endpoints
    assert settings.local_base_url == "http://127.0.0.1:11434/v1"
    restarted = Settings(_env_file=env_path)
    assert restarted.openai_api_key == "sk-provider-plain"
    assert "header-plain" in restarted.custom_endpoints
    if os.name != "nt":
        assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_settings_migration_write_failure_leaves_no_new_references(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / ".env"
    original = "SUXIAOYOU_OPENAI_API_KEY='keep-plaintext'\n"
    env_path.write_text(original, encoding="utf-8")
    settings = Settings(_env_file=env_path)
    fallback = tmp_path / "fallback.json"
    store = CredentialStore(fallback_path=fallback, native_backend=None)
    real_atomic_write = credential_store.atomic_write_text

    def fail_env_write(path, content, **kwargs):
        if Path(path) == env_path:
            raise OSError("disk full")
        return real_atomic_write(path, content, **kwargs)

    monkeypatch.setattr(credential_store, "atomic_write_text", fail_env_write)
    with pytest.raises(OSError, match="disk full"):
        migrate_settings_credentials(settings, env_path, store=store)

    assert env_path.read_text(encoding="utf-8") == original
    assert json.loads(fallback.read_text(encoding="utf-8"))["credentials"] == {}


def test_settings_migration_recognizes_export_and_key_whitespace(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "  export   SUXIAOYOU_OPENAI_API_KEY   = 'legacy-secret'\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=env_path)
    store = CredentialStore(
        fallback_path=tmp_path / "fallback.json",
        native_backend=None,
    )

    assert migrate_settings_credentials(settings, env_path, store=store) == 1

    persisted = env_path.read_text(encoding="utf-8")
    assert "legacy-secret" not in persisted
    assert persisted.startswith("SUXIAOYOU_OPENAI_API_KEY=")
    assert settings.openai_api_key == "legacy-secret"


def test_settings_migration_collapses_duplicate_assignments(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "SUXIAOYOU_OPENAI_API_KEY='first-plaintext'\n"
        "  export SUXIAOYOU_OPENAI_API_KEY = 'effective-plaintext'\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=env_path)
    store = CredentialStore(
        fallback_path=tmp_path / "fallback.json",
        native_backend=None,
    )

    assert migrate_settings_credentials(settings, env_path, store=store) == 1

    persisted = env_path.read_text(encoding="utf-8")
    assert persisted.count("SUXIAOYOU_OPENAI_API_KEY=") == 1
    assert "first-plaintext" not in persisted
    assert "effective-plaintext" not in persisted
    assert settings.openai_api_key == "effective-plaintext"


def test_settings_migration_writes_python_dotenv_apostrophe_syntax(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / ".env"
    endpoint_name = r"C:\users\new\O'Brien"
    endpoint_json = json.dumps(
        [{"id": "custom_quote", "name": endpoint_name, "api_key": "secret"}],
        separators=(",", ":"),
    )
    env_path.write_text(
        f"SUXIAOYOU_CUSTOM_ENDPOINTS={endpoint_json}\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=env_path)
    store = CredentialStore(
        fallback_path=tmp_path / "fallback.json",
        native_backend=None,
    )
    monkeypatch.setattr(credential_store, "get_credential_store", lambda: store)

    assert migrate_settings_credentials(settings, env_path, store=store) == 1

    persisted = env_path.read_text(encoding="utf-8")
    assert "O\\'Brien" in persisted
    assert "C:\\\\\\\\users" in persisted
    assert "'\\''" not in persisted
    restarted = Settings(_env_file=env_path)
    restarted_endpoint = json.loads(restarted.custom_endpoints)[0]
    assert restarted_endpoint["name"] == endpoint_name
    assert restarted_endpoint["api_key"] == "secret"


def test_settings_migration_recovers_v08_shell_apostrophe_and_backslashes(
    v08_legacy_credential_env: tuple[Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path, endpoint_name = v08_legacy_credential_env
    settings = Settings(_env_file=env_path)
    store = CredentialStore(
        fallback_path=tmp_path / "fallback.json",
        native_backend=None,
    )
    monkeypatch.setattr(credential_store, "get_credential_store", lambda: store)

    assert migrate_settings_credentials(settings, env_path, store=store) == 1

    endpoint = json.loads(settings.custom_endpoints)[0]
    assert endpoint["name"] == endpoint_name
    assert endpoint["api_key"] == "legacy-custom-secret"
    persisted = env_path.read_text(encoding="utf-8")
    assert "legacy-custom-secret" not in persisted
    assert "'\\''" not in persisted
    assert r"C:\\\\profiles" in persisted

    restarted = Settings(_env_file=env_path)
    restarted_endpoint = json.loads(restarted.custom_endpoints)[0]
    assert restarted_endpoint["name"] == endpoint_name
    assert restarted_endpoint["api_key"] == "legacy-custom-secret"


def test_settings_migration_rejects_malformed_dotenv_without_writing_secrets(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / ".env"
    original = "SUXIAOYOU_OPENAI_API_KEY='unterminated-secret\n"
    env_path.write_text(original, encoding="utf-8")
    settings = Settings(_env_file=None)
    fallback = tmp_path / "fallback.json"
    store = CredentialStore(fallback_path=fallback, native_backend=None)

    with pytest.raises(CredentialStoreError, match="invalid dotenv syntax"):
        migrate_settings_credentials(settings, env_path, store=store)

    assert env_path.read_text(encoding="utf-8") == original
    assert not fallback.exists()


def test_settings_migration_rejects_malformed_v08_apostrophe_sequence(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / ".env"
    original = "SUXIAOYOU_OPENAI_API_KEY='sk-O'\\''Brien'broken'\n"
    env_path.write_text(original, encoding="utf-8")
    settings = Settings(_env_file=None)
    fallback = tmp_path / "fallback.json"
    store = CredentialStore(fallback_path=fallback, native_backend=None)

    with pytest.raises(CredentialStoreError, match="invalid dotenv syntax"):
        migrate_settings_credentials(settings, env_path, store=store)

    assert env_path.read_text(encoding="utf-8") == original
    assert not fallback.exists()
