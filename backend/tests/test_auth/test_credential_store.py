"""Credential references, native storage, and private fallback behavior."""

from __future__ import annotations

import json
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.auth.credential_store import (
    MACOS_VAULT_ACCOUNT,
    SERVICE_NAME,
    CredentialStore,
    CredentialStoreError,
    _AtomicMacOSKeyringBackend,
    _MacOSVaultBackend,
    _MacOSVaultState,
    is_credential_reference,
    migrate_settings_credentials,
    protect_secret_tree,
    resolve_settings_references,
    resolve_secret_tree,
    stage_protected_env_value,
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


class CountingBackend(MemoryBackend):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.reads: list[str] = []
        self.writes: list[str] = []
        self.deletes: list[str] = []
        self._lock = threading.Lock()

    def get_password(self, service: str, username: str) -> str | None:
        with self._lock:
            self.reads.append(username)
            return super().get_password(service, username)

    def set_password(self, service: str, username: str, password: str) -> None:
        with self._lock:
            self.writes.append(username)
            super().set_password(service, username, password)

    def delete_password(self, service: str, username: str) -> None:
        with self._lock:
            self.deletes.append(username)
            super().delete_password(service, username)


def _macos_vault_payload(credentials: dict[str, str]) -> str:
    return _MacOSVaultBackend._encode_vault(credentials, set(credentials), set())


def test_macos_vault_multi_reference_concurrent_use_reads_one_keychain_item(
    tmp_path: Path,
) -> None:
    backend = CountingBackend()
    backend.values[(SERVICE_NAME, MACOS_VAULT_ACCOUNT)] = _macos_vault_payload(
        {"access": "access-secret", "refresh": "refresh-secret"}
    )
    store = CredentialStore(
        fallback_path=tmp_path / "fallback.json",
        native_backend=_MacOSVaultBackend(backend, state=_MacOSVaultState()),
    )

    identifiers = ["access", "refresh"] * 12
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(store.get, identifiers))

    assert values == [
        "access-secret" if identifier == "access" else "refresh-secret"
        for identifier in identifiers
    ]
    assert backend.reads == [MACOS_VAULT_ACCOUNT]


def test_macos_vault_concurrent_puts_do_not_lose_updates(tmp_path: Path) -> None:
    backend = CountingBackend()
    state = _MacOSVaultState()
    first = CredentialStore(
        fallback_path=tmp_path / "fallback.json",
        native_backend=_MacOSVaultBackend(backend, state=state),
    )
    second = CredentialStore(
        fallback_path=tmp_path / "fallback.json",
        native_backend=_MacOSVaultBackend(backend, state=state),
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        references = list(
            pool.map(
                lambda item: item[0].put(item[1], item[2]),
                ((first, "first", "one"), (second, "second", "two")),
            )
        )

    payload = json.loads(backend.values[(SERVICE_NAME, MACOS_VAULT_ACCOUNT)])
    assert all(is_credential_reference(reference) for reference in references)
    assert payload["credentials"] == {"first": "one", "second": "two"}
    assert backend.reads == [MACOS_VAULT_ACCOUNT]


def test_macos_fallback_handoff_is_serialized_with_global_journal_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = CountingBackend()
    backend.values[(SERVICE_NAME, MACOS_VAULT_ACCOUNT)] = _macos_vault_payload(
        {"target": "native-stale", "sibling": "keep-me"}
    )
    fallback = tmp_path / "fallback.json"
    fallback.write_text(
        json.dumps(
            {
                "version": 1,
                "credentials": {"target": "fallback-old"},
                "pending_native_deletions": [],
                "pending_native_vault_deletions": [],
                "cleanup_transactions": {},
            }
        ),
        encoding="utf-8",
    )
    state = _MacOSVaultState()
    primary = CredentialStore(
        fallback_path=fallback,
        native_backend=_MacOSVaultBackend(backend, state=state),
    )
    concurrent = CredentialStore(
        fallback_path=fallback,
        native_backend=_MacOSVaultBackend(backend, state=state),
    )

    handoff_entered = threading.Event()
    allow_handoff = threading.Event()
    retry_holds_file_lock = threading.Event()
    allow_retry = threading.Event()
    concurrent_reconcile_entered = threading.Event()

    real_delete_fallback = primary._delete_fallback

    def pause_before_file_handoff(identifier: str, *, target: str) -> None:
        handoff_entered.set()
        assert allow_handoff.wait(timeout=3)
        real_delete_fallback(identifier, target=target)

    monkeypatch.setattr(primary, "_delete_fallback", pause_before_file_handoff)

    real_retry = concurrent._retry_pending_native_deletions

    def coordinated_retry() -> None:
        # Acquiring this lock here makes the old race deterministic: without
        # the outer handoff guard, the retry owns the journal while the primary
        # writer is paused after its successful vault write.
        with credential_store._FILE_LOCK:
            retry_holds_file_lock.set()
            assert allow_retry.wait(timeout=3)
            real_retry()

    monkeypatch.setattr(
        concurrent,
        "_retry_pending_native_deletions",
        coordinated_retry,
    )
    real_reconcile = concurrent._reconcile_cleanup_transactions

    def announce_reconcile(*args, **kwargs) -> None:
        concurrent_reconcile_entered.set()
        real_reconcile(*args, **kwargs)

    monkeypatch.setattr(
        concurrent,
        "_reconcile_cleanup_transactions",
        announce_reconcile,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        primary_write = pool.submit(primary.put, "target", "native-new")
        assert handoff_entered.wait(timeout=3)
        concurrent_write = pool.submit(concurrent.put, "other", "other-new")
        assert concurrent_reconcile_entered.wait(timeout=3)
        try:
            # The primary writer still owns the shared handoff lock, so the
            # concurrent retry cannot adopt the temporary marker.
            assert not retry_holds_file_lock.wait(timeout=0.25)
        finally:
            allow_retry.set()
            allow_handoff.set()
        assert primary_write.result(timeout=3).endswith("target")
        assert concurrent_write.result(timeout=3).endswith("other")

    vault = json.loads(backend.values[(SERVICE_NAME, MACOS_VAULT_ACCOUNT)])
    persisted = json.loads(fallback.read_text(encoding="utf-8"))
    assert vault["credentials"] == {
        "other": "other-new",
        "sibling": "keep-me",
        "target": "native-new",
    }
    assert persisted["credentials"] == {}
    assert persisted["pending_native_vault_deletions"] == []


def test_corrupt_macos_vault_fails_closed_without_overwrite(
    tmp_path: Path,
) -> None:
    backend = CountingBackend()
    backend.values[(SERVICE_NAME, MACOS_VAULT_ACCOUNT)] = "{not-valid-json"
    fallback = tmp_path / "fallback.json"
    wrapper = _MacOSVaultBackend(backend, state=_MacOSVaultState())

    with pytest.raises(CredentialStoreError, match="corrupt"):
        wrapper.get_password(SERVICE_NAME, "existing")

    store = CredentialStore(fallback_path=fallback, native_backend=wrapper)
    reference = store.put("new", "fallback-secret")

    assert store.resolve(reference) == "fallback-secret"
    assert backend.values[(SERVICE_NAME, MACOS_VAULT_ACCOUNT)] == "{not-valid-json"
    persisted = json.loads(fallback.read_text(encoding="utf-8"))
    assert persisted["credentials"] == {"new": "fallback-secret"}
    assert persisted["pending_native_vault_deletions"] == ["new"]


def test_legacy_macos_item_migration_has_recoverable_cleanup_marker() -> None:
    class CleanupBackend(CountingBackend):
        fail_legacy_delete = True

        def delete_password(self, service: str, username: str) -> None:
            if username == "legacy" and self.fail_legacy_delete:
                assert (service, MACOS_VAULT_ACCOUNT) in self.values
                self.deletes.append(username)
                raise RuntimeError("legacy ACL locked")
            super().delete_password(service, username)

    backend = CleanupBackend()
    backend.values[(SERVICE_NAME, "legacy")] = "legacy-secret"
    first = _MacOSVaultBackend(backend, state=_MacOSVaultState())

    assert first.get_password(SERVICE_NAME, "legacy") == "legacy-secret"
    first_payload = json.loads(
        backend.values[(SERVICE_NAME, MACOS_VAULT_ACCOUNT)]
    )
    assert first_payload["credentials"] == {"legacy": "legacy-secret"}
    assert first_payload["pending_legacy_deletions"] == ["legacy"]
    assert backend.values[(SERVICE_NAME, "legacy")] == "legacy-secret"
    assert backend.reads.count("legacy") == 1

    # Simulate the next process after the legacy ACL becomes available.  The
    # normal read does not fan out into historical per-item ACL prompts.
    backend.fail_legacy_delete = False
    recovered = _MacOSVaultBackend(backend, state=_MacOSVaultState())
    assert recovered.get_password(SERVICE_NAME, "legacy") == "legacy-secret"
    assert backend.values[(SERVICE_NAME, "legacy")] == "legacy-secret"

    # A later mutation of this same identifier performs only its own pending
    # cleanup and never scans unrelated historical items.
    recovered.set_password(SERVICE_NAME, "legacy", "legacy-secret")
    recovered_payload = json.loads(
        backend.values[(SERVICE_NAME, MACOS_VAULT_ACCOUNT)]
    )
    assert (SERVICE_NAME, "legacy") not in backend.values
    assert recovered_payload["pending_legacy_deletions"] == []


def test_atomic_macos_adapter_never_calls_keyring_delete_then_add_setter() -> None:
    class DestructiveKeyringBackend(CountingBackend):
        def set_password(self, service: str, username: str, password: str) -> None:
            self.values.pop((service, username), None)
            raise RuntimeError("SecItemAdd failed after delete")

    backend = DestructiveKeyringBackend()
    original = _macos_vault_payload(
        {"first": "one", "second": "two"}
    )
    backend.values[(SERVICE_NAME, MACOS_VAULT_ACCOUNT)] = original
    atomic_calls = 0

    def fail_atomic_update(_service: str, _username: str, _password: str) -> None:
        nonlocal atomic_calls
        atomic_calls += 1
        raise RuntimeError("SecItemUpdate denied")

    adapter = _AtomicMacOSKeyringBackend(
        backend,
        atomic_setter=fail_atomic_update,
    )

    with pytest.raises(RuntimeError, match="SecItemUpdate denied"):
        adapter.set_password(SERVICE_NAME, MACOS_VAULT_ACCOUNT, "replacement")

    assert atomic_calls == 1
    assert backend.values[(SERVICE_NAME, MACOS_VAULT_ACCOUNT)] == original
    assert backend.deletes == []


def test_atomic_macos_adapter_normalizes_wrapped_not_found_without_read() -> None:
    class NativeNotFound(Exception):
        pass

    class MissingBackend(CountingBackend):
        def delete_password(self, service: str, username: str) -> None:
            try:
                raise NativeNotFound("missing")
            except NativeNotFound as exc:
                raise RuntimeError("wrapped keyring delete error") from exc

    backend = MissingBackend()
    adapter = _AtomicMacOSKeyringBackend(
        backend,
        atomic_setter=lambda *_args: None,
        missing_error_types=(NativeNotFound,),
    )

    adapter.delete_password(SERVICE_NAME, "already-gone")

    assert backend.reads == []


def test_legacy_delete_denial_never_follows_with_secret_read() -> None:
    class DeleteDeniedBackend(CountingBackend):
        def delete_password(self, service: str, username: str) -> None:
            self.deletes.append(username)
            raise RuntimeError("ACL denied")

    backend = DeleteDeniedBackend()
    wrapper = _MacOSVaultBackend(backend, state=_MacOSVaultState())

    with pytest.raises(CredentialStoreError, match="deletion failed"):
        wrapper.delete_legacy_password(SERVICE_NAME, "legacy")

    assert backend.reads == []
    assert backend.deletes == ["legacy"]


def test_macos_vault_denied_read_is_not_reprompted_in_same_process() -> None:
    class DeniedBackend(CountingBackend):
        def get_password(self, service: str, username: str) -> str | None:
            self.reads.append(username)
            raise RuntimeError("user denied Keychain access")

    backend = DeniedBackend()
    wrapper = _MacOSVaultBackend(backend, state=_MacOSVaultState())

    for _ in range(2):
        with pytest.raises(CredentialStoreError, match="unavailable"):
            wrapper.get_password(SERVICE_NAME, "token")

    assert backend.reads == [MACOS_VAULT_ACCOUNT]


def test_ambiguous_macos_vault_write_uses_typed_journal_and_preserves_other_keys(
    tmp_path: Path,
) -> None:
    class AmbiguousWriteBackend(CountingBackend):
        fail_next_write = True
        block_reads = False

        def set_password(self, service: str, username: str, password: str) -> None:
            if self.fail_next_write:
                self.writes.append(username)
                self.values[(service, username)] = password
                self.fail_next_write = False
                self.block_reads = True
                raise RuntimeError("write committed but acknowledgement was lost")
            super().set_password(service, username, password)

        def get_password(self, service: str, username: str) -> str | None:
            if self.block_reads:
                raise RuntimeError("Keychain temporarily unavailable")
            return super().get_password(service, username)

    backend = AmbiguousWriteBackend()
    backend.values[(SERVICE_NAME, MACOS_VAULT_ACCOUNT)] = _macos_vault_payload(
        {"other": "keep-me"}
    )
    fallback = tmp_path / "fallback.json"
    store = CredentialStore(
        fallback_path=fallback,
        native_backend=_MacOSVaultBackend(backend, state=_MacOSVaultState()),
    )

    reference = store.put("target", "new-secret")
    persisted = json.loads(fallback.read_text(encoding="utf-8"))
    assert persisted["credentials"] == {"target": "new-secret"}
    assert persisted["pending_native_deletions"] == []
    assert persisted["pending_native_vault_deletions"] == ["target"]
    # Fallback reads remain usable and do not retry a locked Keychain.
    assert store.resolve(reference) == "new-secret"

    backend.block_reads = False
    recovered = CredentialStore(
        fallback_path=fallback,
        native_backend=_MacOSVaultBackend(backend, state=_MacOSVaultState()),
    )
    recovered._retry_pending_native_deletions()

    vault = json.loads(backend.values[(SERVICE_NAME, MACOS_VAULT_ACCOUNT)])
    journal = json.loads(fallback.read_text(encoding="utf-8"))
    assert vault["credentials"] == {"other": "keep-me"}
    # Adoption makes the vault deletion durable, but the fallback remains the
    # committed value and therefore keeps its authority marker until a later
    # successful put/delete retires both together.
    assert journal["pending_native_vault_deletions"] == ["target"]


def test_adopted_vault_journal_keeps_fallback_authority_without_rewriting(
    tmp_path: Path,
) -> None:
    backend = CountingBackend()
    backend.values[(SERVICE_NAME, MACOS_VAULT_ACCOUNT)] = _macos_vault_payload(
        {"target": "native-stale", "other": "keep-me"}
    )
    fallback = tmp_path / "fallback.json"
    fallback.write_text(
        json.dumps(
            {
                "version": 1,
                "credentials": {"target": "fallback-current"},
                "pending_native_deletions": [],
                "pending_native_vault_deletions": ["target"],
                "cleanup_transactions": {},
            }
        ),
        encoding="utf-8",
    )
    store = CredentialStore(
        fallback_path=fallback,
        native_backend=_MacOSVaultBackend(backend, state=_MacOSVaultState()),
    )

    store._retry_pending_native_deletions()

    vault = json.loads(backend.values[(SERVICE_NAME, MACOS_VAULT_ACCOUNT)])
    journal = json.loads(fallback.read_text(encoding="utf-8"))
    assert vault["credentials"] == {"other": "keep-me"}
    assert journal["pending_native_vault_deletions"] == ["target"]

    writes_after_adoption = list(backend.writes)
    store._retry_pending_native_deletions()
    assert backend.writes == writes_after_adoption

    class DeniedBackend(CountingBackend):
        def get_password(self, service: str, username: str) -> str | None:
            self.reads.append(username)
            raise RuntimeError("Keychain denied after restart")

    denied = DeniedBackend()
    denied.values.update(backend.values)
    recovered = CredentialStore(
        fallback_path=fallback,
        native_backend=_MacOSVaultBackend(denied, state=_MacOSVaultState()),
    )

    assert recovered.get("target") == "fallback-current"
    assert denied.reads == []


def test_native_success_cleanup_failure_compensates_before_reporting_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = CountingBackend()
    backend.values[(SERVICE_NAME, MACOS_VAULT_ACCOUNT)] = _macos_vault_payload(
        {"target": "native-stale", "other": "keep-me"}
    )
    fallback = tmp_path / "fallback.json"
    fallback.write_text(
        json.dumps(
            {
                "version": 1,
                "credentials": {"target": "fallback-old"},
                "pending_native_deletions": [],
                "pending_native_vault_deletions": [],
                "cleanup_transactions": {},
            }
        ),
        encoding="utf-8",
    )
    store = CredentialStore(
        fallback_path=fallback,
        native_backend=_MacOSVaultBackend(backend, state=_MacOSVaultState()),
    )
    real_set_password = backend.set_password

    def assert_authority_marker_before_vault_set(
        service: str,
        username: str,
        password: str,
    ) -> None:
        persisted = json.loads(fallback.read_text(encoding="utf-8"))
        assert persisted["credentials"] == {"target": "fallback-old"}
        assert persisted["pending_native_vault_deletions"] == ["target"]
        real_set_password(service, username, password)

    monkeypatch.setattr(
        backend,
        "set_password",
        assert_authority_marker_before_vault_set,
    )

    real_atomic_write = credential_store.atomic_write_text
    fallback_writes = 0

    def fail_fallback_handoff(path, content, **kwargs):
        nonlocal fallback_writes
        if Path(path) == fallback:
            fallback_writes += 1
            if fallback_writes == 2:
                raise OSError("disk full during authority handoff")
        return real_atomic_write(path, content, **kwargs)

    monkeypatch.setattr(
        credential_store,
        "atomic_write_text",
        fail_fallback_handoff,
    )

    with pytest.raises(OSError, match="disk full during authority handoff"):
        store.put("target", "native-new")

    vault = json.loads(backend.values[(SERVICE_NAME, MACOS_VAULT_ACCOUNT)])
    journal = json.loads(fallback.read_text(encoding="utf-8"))
    assert fallback_writes == 2
    assert vault["credentials"] == {"other": "keep-me"}
    assert journal["credentials"] == {"target": "fallback-old"}
    assert journal["pending_native_vault_deletions"] == ["target"]

    class DeniedBackend(CountingBackend):
        def get_password(self, service: str, username: str) -> str | None:
            self.reads.append(username)
            raise RuntimeError("vault should not be read after restart")

    denied = DeniedBackend()
    denied.values.update(backend.values)
    recovered = CredentialStore(
        fallback_path=fallback,
        native_backend=_MacOSVaultBackend(denied, state=_MacOSVaultState()),
    )
    assert recovered.get("target") == "fallback-old"
    assert denied.reads == []


def test_committed_vault_cleanup_journals_identifier_even_with_fallback(
    tmp_path: Path,
) -> None:
    backend = CountingBackend()
    backend.values[(SERVICE_NAME, MACOS_VAULT_ACCOUNT)] = _macos_vault_payload(
        {"target": "native-stale", "other": "keep-me"}
    )
    fallback = tmp_path / "fallback.json"
    fallback.write_text(
        json.dumps(
            {
                "version": 1,
                "credentials": {"target": "fallback-current"},
                "pending_native_deletions": [],
                "pending_native_vault_deletions": [],
                "cleanup_transactions": {},
            }
        ),
        encoding="utf-8",
    )
    evidence = tmp_path / "settings.env"
    evidence.write_text("old", encoding="utf-8")
    store = CredentialStore(
        fallback_path=fallback,
        native_backend=_MacOSVaultBackend(backend, state=_MacOSVaultState()),
    )
    transaction = store.prepare_cleanup_transaction(
        ["target"],
        evidence_path=evidence,
        previous_exists=True,
        previous_content="old",
        next_exists=True,
        next_content="new",
    )
    assert transaction is not None

    evidence.write_text("new", encoding="utf-8")
    transaction.commit()

    vault = json.loads(backend.values[(SERVICE_NAME, MACOS_VAULT_ACCOUNT)])
    persisted = json.loads(fallback.read_text(encoding="utf-8"))
    assert vault["credentials"] == {"other": "keep-me"}
    assert persisted["credentials"] == {}
    assert persisted["pending_native_vault_deletions"] == []
    assert persisted["cleanup_transactions"] == {}


def test_old_native_deletion_journal_removes_legacy_item_not_vault_key(
    tmp_path: Path,
) -> None:
    backend = CountingBackend()
    backend.values[(SERVICE_NAME, MACOS_VAULT_ACCOUNT)] = _macos_vault_payload(
        {"shared": "vault-secret"}
    )
    backend.values[(SERVICE_NAME, "shared")] = "legacy-secret"
    fallback = tmp_path / "fallback.json"
    fallback.write_text(
        json.dumps(
            {
                "version": 1,
                "credentials": {},
                "pending_native_deletions": ["shared"],
                "cleanup_transactions": {},
            }
        ),
        encoding="utf-8",
    )
    store = CredentialStore(
        fallback_path=fallback,
        native_backend=_MacOSVaultBackend(backend, state=_MacOSVaultState()),
    )

    store._retry_pending_native_deletions()

    # The old physical-item journal is adopted in one vault write; automatic
    # retry never fans out through legacy ACLs.
    assert backend.values[(SERVICE_NAME, "shared")] == "legacy-secret"
    adopted = json.loads(backend.values[(SERVICE_NAME, MACOS_VAULT_ACCOUNT)])
    assert adopted["credentials"] == {"shared": "vault-secret"}
    assert adopted["pending_legacy_deletions"] == ["shared"]
    assert backend.deletes == []
    assert store.get("shared") == "vault-secret"
    assert json.loads(fallback.read_text())["pending_native_deletions"] == []

    # A mutation of this identifier alone can finish its physical cleanup.
    store.put("shared", "vault-secret")
    assert (SERVICE_NAME, "shared") not in backend.values


def test_macos_put_adopts_many_legacy_journals_without_physical_acl_reads(
    tmp_path: Path,
) -> None:
    backend = CountingBackend()
    for identifier in ("old-a", "old-b", "old-c"):
        backend.values[(SERVICE_NAME, identifier)] = f"secret-{identifier}"
    fallback = tmp_path / "fallback.json"
    fallback.write_text(
        json.dumps(
            {
                "version": 1,
                "credentials": {},
                "pending_native_deletions": ["old-a", "old-b", "old-c"],
                "cleanup_transactions": {},
            }
        ),
        encoding="utf-8",
    )
    store = CredentialStore(
        fallback_path=fallback,
        native_backend=_MacOSVaultBackend(backend, state=_MacOSVaultState()),
    )

    store.put("new", "new-secret")

    vault = json.loads(backend.values[(SERVICE_NAME, MACOS_VAULT_ACCOUNT)])
    assert vault["pending_legacy_deletions"] == ["old-a", "old-b", "old-c"]
    assert backend.deletes == []
    assert backend.reads == [MACOS_VAULT_ACCOUNT]


def test_vault_cleanup_of_unresolved_legacy_only_reference_deletes_old_item(
    tmp_path: Path,
) -> None:
    backend = CountingBackend()
    backend.values[(SERVICE_NAME, "legacy-only")] = "retired-secret"
    store = CredentialStore(
        fallback_path=tmp_path / "fallback.json",
        native_backend=_MacOSVaultBackend(backend, state=_MacOSVaultState()),
    )

    store.delete("legacy-only")

    # Logical deletion is durable without reopening the legacy ACL. Physical
    # cleanup remains targeted inside the aggregate vault marker.
    assert backend.values[(SERVICE_NAME, "legacy-only")] == "retired-secret"
    vault = json.loads(backend.values[(SERVICE_NAME, MACOS_VAULT_ACCOUNT)])
    assert vault["credentials"] == {}
    assert vault["legacy_tombstones"] == ["legacy-only"]
    assert vault["pending_legacy_deletions"] == ["legacy-only"]
    fallback = json.loads(
        (tmp_path / "fallback.json").read_text(encoding="utf-8")
    )
    assert fallback["pending_native_vault_deletions"] == []


def test_failed_vault_tombstone_write_keeps_legacy_delete_journal(
    tmp_path: Path,
) -> None:
    backend = CountingBackend()
    backend.values[(SERVICE_NAME, "legacy-only")] = "retired-secret"

    def fail_update(_service: str, _username: str, _password: str) -> None:
        raise RuntimeError("atomic update denied")

    wrapper = _MacOSVaultBackend(
        _AtomicMacOSKeyringBackend(backend, atomic_setter=fail_update),
        state=_MacOSVaultState(),
    )
    fallback = tmp_path / "fallback.json"
    store = CredentialStore(fallback_path=fallback, native_backend=wrapper)

    store.delete("legacy-only")

    journal = json.loads(fallback.read_text(encoding="utf-8"))
    assert backend.values[(SERVICE_NAME, "legacy-only")] == "retired-secret"
    assert journal["pending_native_vault_deletions"] == ["legacy-only"]
    assert backend.reads == [MACOS_VAULT_ACCOUNT]


def test_delete_persistence_failure_happens_before_any_vault_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = CountingBackend()
    original_vault = _macos_vault_payload(
        {"target": "native-secret", "other": "keep-me"}
    )
    backend.values[(SERVICE_NAME, MACOS_VAULT_ACCOUNT)] = original_vault
    fallback = tmp_path / "fallback.json"
    fallback.write_text(
        json.dumps(
            {
                "version": 1,
                "credentials": {"target": "fallback-secret"},
                "pending_native_deletions": [],
                "pending_native_vault_deletions": [],
                "cleanup_transactions": {},
            }
        ),
        encoding="utf-8",
    )
    store = CredentialStore(
        fallback_path=fallback,
        native_backend=_MacOSVaultBackend(backend, state=_MacOSVaultState()),
    )

    def fail_intent(*_args, **_kwargs):
        raise OSError("disk full before delete intent")

    monkeypatch.setattr(credential_store, "atomic_write_text", fail_intent)

    with pytest.raises(OSError, match="before delete intent"):
        store.delete("target")

    assert backend.values[(SERVICE_NAME, MACOS_VAULT_ACCOUNT)] == original_vault
    assert backend.reads == []
    assert backend.writes == []
    persisted = json.loads(fallback.read_text(encoding="utf-8"))
    assert persisted["credentials"] == {"target": "fallback-secret"}
    assert persisted["pending_native_vault_deletions"] == []


def test_vault_delete_intent_prevents_stale_value_resurrection_without_read(
    tmp_path: Path,
) -> None:
    class DeniedBackend(CountingBackend):
        def get_password(self, service: str, username: str) -> str | None:
            self.reads.append(username)
            raise RuntimeError("vault should not be read for a logical deletion")

    backend = DeniedBackend()
    backend.values[(SERVICE_NAME, MACOS_VAULT_ACCOUNT)] = _macos_vault_payload(
        {"target": "native-stale", "other": "keep-me"}
    )
    fallback = tmp_path / "fallback.json"
    fallback.write_text(
        json.dumps(
            {
                "version": 1,
                "credentials": {},
                "pending_native_deletions": [],
                "pending_native_vault_deletions": ["target"],
                "cleanup_transactions": {},
            }
        ),
        encoding="utf-8",
    )
    recovered = CredentialStore(
        fallback_path=fallback,
        native_backend=_MacOSVaultBackend(backend, state=_MacOSVaultState()),
    )

    assert recovered.get("target") is None
    assert backend.reads == []
    # The physical value may remain until a later mutating operation can adopt
    # the durable intent; reads must honor the already-committed deletion.
    vault = json.loads(backend.values[(SERVICE_NAME, MACOS_VAULT_ACCOUNT)])
    assert vault["credentials"] == {"other": "keep-me", "target": "native-stale"}


def test_delete_clear_failure_leaves_durable_typed_intent_after_vault_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = CountingBackend()
    backend.values[(SERVICE_NAME, MACOS_VAULT_ACCOUNT)] = _macos_vault_payload(
        {"target": "native-secret", "other": "keep-me"}
    )
    fallback = tmp_path / "fallback.json"
    fallback.write_text(
        json.dumps(
            {
                "version": 1,
                "credentials": {"target": "fallback-secret"},
                "pending_native_deletions": [],
                "pending_native_vault_deletions": [],
                "cleanup_transactions": {},
            }
        ),
        encoding="utf-8",
    )
    store = CredentialStore(
        fallback_path=fallback,
        native_backend=_MacOSVaultBackend(backend, state=_MacOSVaultState()),
    )
    real_atomic_write = credential_store.atomic_write_text
    fallback_writes = 0

    def fail_clear(path, content, **kwargs):
        nonlocal fallback_writes
        if Path(path) == fallback:
            fallback_writes += 1
            if fallback_writes == 2:
                raise OSError("disk full clearing delete intent")
        return real_atomic_write(path, content, **kwargs)

    monkeypatch.setattr(credential_store, "atomic_write_text", fail_clear)

    with pytest.raises(OSError, match="clearing delete intent"):
        store.delete("target")

    vault = json.loads(backend.values[(SERVICE_NAME, MACOS_VAULT_ACCOUNT)])
    persisted = json.loads(fallback.read_text(encoding="utf-8"))
    assert vault["credentials"] == {"other": "keep-me"}
    assert persisted["credentials"] == {}
    assert persisted["pending_native_vault_deletions"] == ["target"]

    monkeypatch.setattr(credential_store, "atomic_write_text", real_atomic_write)
    recovered = CredentialStore(
        fallback_path=fallback,
        native_backend=_MacOSVaultBackend(backend, state=_MacOSVaultState()),
    )
    assert recovered.get("target") is None


def test_legacy_journal_fallback_adoption_removes_stale_vault_before_conversion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = CountingBackend()
    backend.values[(SERVICE_NAME, MACOS_VAULT_ACCOUNT)] = _macos_vault_payload(
        {"target": "native-stale", "other": "keep-me"}
    )
    fallback = tmp_path / "fallback.json"
    fallback.write_text(
        json.dumps(
            {
                "version": 1,
                "credentials": {"target": "fallback-current"},
                "pending_native_deletions": ["target"],
                "pending_native_vault_deletions": [],
                "cleanup_transactions": {},
            }
        ),
        encoding="utf-8",
    )
    store = CredentialStore(
        fallback_path=fallback,
        native_backend=_MacOSVaultBackend(backend, state=_MacOSVaultState()),
    )
    real_atomic_write = credential_store.atomic_write_text

    def fail_file_conversion(*_args, **_kwargs):
        raise OSError("crash after vault adoption")

    monkeypatch.setattr(
        credential_store,
        "atomic_write_text",
        fail_file_conversion,
    )

    with pytest.raises(OSError, match="after vault adoption"):
        store._retry_pending_native_deletions()

    vault = json.loads(backend.values[(SERVICE_NAME, MACOS_VAULT_ACCOUNT)])
    persisted = json.loads(fallback.read_text(encoding="utf-8"))
    assert vault["credentials"] == {"other": "keep-me"}
    assert persisted["credentials"] == {"target": "fallback-current"}
    assert persisted["pending_native_deletions"] == ["target"]
    assert persisted["pending_native_vault_deletions"] == []

    class DeniedBackend(CountingBackend):
        def get_password(self, service: str, username: str) -> str | None:
            self.reads.append(username)
            raise RuntimeError("vault should not be read")

    monkeypatch.setattr(credential_store, "atomic_write_text", real_atomic_write)
    denied = DeniedBackend()
    denied.values.update(backend.values)
    recovered = CredentialStore(
        fallback_path=fallback,
        native_backend=_MacOSVaultBackend(denied, state=_MacOSVaultState()),
    )

    assert recovered.get("target") == "fallback-current"
    assert denied.reads == []


def test_native_vault_wins_after_crash_before_stale_fallback_cleanup(
    tmp_path: Path,
) -> None:
    backend = CountingBackend()
    backend.values[(SERVICE_NAME, MACOS_VAULT_ACCOUNT)] = _macos_vault_payload(
        {"same": "native-new"}
    )
    fallback = tmp_path / "fallback.json"
    fallback.write_text(
        json.dumps(
            {
                "version": 1,
                "credentials": {"same": "fallback-old"},
                "pending_native_deletions": [],
                "pending_native_vault_deletions": [],
                "cleanup_transactions": {},
            }
        ),
        encoding="utf-8",
    )
    store = CredentialStore(
        fallback_path=fallback,
        native_backend=_MacOSVaultBackend(backend, state=_MacOSVaultState()),
    )

    assert store.get("same") == "native-new"
    assert json.loads(fallback.read_text(encoding="utf-8"))["credentials"] == {}


def test_vault_read_denial_never_returns_unmarked_stale_fallback(
    tmp_path: Path,
) -> None:
    class DeniedBackend(CountingBackend):
        def get_password(self, service: str, username: str) -> str | None:
            self.reads.append(username)
            raise RuntimeError("Keychain denied")

    fallback = tmp_path / "fallback.json"
    fallback.write_text(
        json.dumps(
            {
                "version": 1,
                "credentials": {"same": "fallback-old"},
                "pending_native_deletions": [],
                "pending_native_vault_deletions": [],
                "cleanup_transactions": {},
            }
        ),
        encoding="utf-8",
    )
    backend = DeniedBackend()
    store = CredentialStore(
        fallback_path=fallback,
        native_backend=_MacOSVaultBackend(backend, state=_MacOSVaultState()),
    )

    assert store.get("same") is None
    assert store.get("same") is None
    assert backend.reads == [MACOS_VAULT_ACCOUNT]


def test_process_credential_store_singleton_is_concurrent_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = MemoryBackend()
    discoveries = 0

    def discover() -> MemoryBackend:
        nonlocal discoveries
        discoveries += 1
        return backend

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(credential_store, "_discover_native_backend", discover)
    credential_store.get_credential_store.cache_clear()
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            stores = list(
                pool.map(
                    lambda _index: credential_store.get_credential_store(),
                    range(24),
                )
            )
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda store: store.get("missing"), stores))

        assert len({id(store) for store in stores}) == 1
        assert discoveries == 1
    finally:
        credential_store.get_credential_store.cache_clear()


def test_platform_backend_discovery_is_lazy_until_credential_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = MemoryBackend()
    discoveries = 0

    def discover() -> MemoryBackend:
        nonlocal discoveries
        discoveries += 1
        return backend

    monkeypatch.setattr(credential_store, "_discover_native_backend", discover)
    store = CredentialStore(fallback_path=tmp_path / "fallback.json")

    assert discoveries == 0
    assert store.get("missing") is None
    assert discoveries == 1


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


def test_staged_simple_env_rollback_forgets_only_its_fallback_reference(
    tmp_path: Path,
) -> None:
    fallback = tmp_path / "fallback.json"
    store = CredentialStore(fallback_path=fallback, native_backend=None)
    installed = stage_protected_env_value(
        "SUXIAOYOU_OPENAI_OAUTH_ACCESS_TOKEN",
        "old-access",
        store=store,
    )
    previous_bytes = fallback.read_bytes()
    staged = stage_protected_env_value(
        "SUXIAOYOU_OPENAI_OAUTH_ACCESS_TOKEN",
        "new-access",
        previous_value=installed.value,
        store=store,
    )

    staged.discard_unreferenced((installed.value,))

    assert fallback.read_bytes() == previous_bytes

    # General deletion still records a durable native cleanup intent. The
    # narrow staged rollback path must not weaken normal deletion authority.
    store.delete(installed.value)
    persisted = json.loads(fallback.read_text(encoding="utf-8"))
    identifier = next(iter(json.loads(previous_bytes)["credentials"]))
    assert persisted["credentials"] == {}
    assert identifier in {
        *persisted["pending_native_deletions"],
        *persisted["pending_native_vault_deletions"],
    }


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

    # Initialization is native-I/O-free; the next explicit credential
    # operation retries the durable cleanup journal.
    CredentialStore(fallback_path=fallback, native_backend=backend)
    backend.fail_deletes = False
    recovered = CredentialStore(fallback_path=fallback, native_backend=backend)

    assert backend.values, "construction must not access the native credential backend"
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
    assert is_credential_reference(restarted.openai_api_key)
    assert "header-plain" not in restarted.custom_endpoints
    assert resolve_settings_references(restarted, store=store) == 2
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
    assert "secret" not in restarted.custom_endpoints
    assert resolve_settings_references(restarted, store=store) == 1
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
    assert "legacy-custom-secret" not in restarted.custom_endpoints
    assert resolve_settings_references(restarted, store=store) == 1
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
