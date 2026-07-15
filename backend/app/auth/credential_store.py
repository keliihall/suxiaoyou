"""OS-native credential storage with an explicit private-file fallback.

Production builds use ``keyring`` to select macOS Keychain, Windows Credential
Manager, or Linux Secret Service.  Some Linux/headless environments have no
native service; only then do we use one atomically-written 0600 JSON fallback.
Configuration files store opaque references and never the credential itself.
"""

from __future__ import annotations

import copy
import ctypes
import hashlib
import json
import logging
import os
import re
import secrets
import sys
import threading
from contextlib import nullcontext
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import Any, Iterable, Protocol
from urllib.parse import quote, unquote

from app.utils.atomic_write import atomic_write_text

logger = logging.getLogger(__name__)

SERVICE_NAME = "com.suxiaoyou.credentials"
REFERENCE_PREFIX = "suxiaoyou-credential://"
FALLBACK_VERSION = 1
MACOS_VAULT_ACCOUNT = "__suxiaoyou_vault_v1__"
MACOS_VAULT_VERSION = 1

_NATIVE_TARGET_INDIVIDUAL = "individual"
_NATIVE_TARGET_LEGACY = "legacy"
_NATIVE_TARGET_VAULT = "vault"
_NATIVE_TARGETS = frozenset(
    {_NATIVE_TARGET_INDIVIDUAL, _NATIVE_TARGET_LEGACY, _NATIVE_TARGET_VAULT}
)

_FILE_LOCK = threading.RLock()
_NATIVE_BACKEND_UNDISCOVERED = object()
_CREDENTIAL_STORE_SINGLETON_LOCK = threading.RLock()
_ENV_LINE = re.compile(
    r"^(?P<prefix>[ \t]*(?:export[ \t]+)?)(?P<key>[A-Za-z_][A-Za-z0-9_]*)[ \t]*="
)
_EPHEMERAL_ENV_IDENTIFIER = re.compile(
    r"^env:[A-Z_][A-Z0-9_]*:[0-9a-f]{24}$"
)
_V08_SHELL_APOSTROPHE = "'\\''"
_SECRET_FIELD_NAMES = frozenset(
    {
        "access_token",
        "refresh_token",
        "token",
        "bot_token",
        "app_token",
        "api_key",
        "app_secret",
        "client_secret",
        "signing_secret",
        "webhook_secret",
        "encrypt_key",
        "password",
    }
)


class CredentialStoreError(RuntimeError):
    """Credential persistence or resolution failed closed."""


@dataclass(frozen=True)
class StagedEnvValue:
    """A protected env value whose new references are not committed yet.

    Protecting plaintext necessarily writes the secret before the opaque
    reference can be installed in ``.env``.  Keeping the exact references
    created by one attempt lets the caller remove only those entries when the
    config write fails, without risking an older reference that is still live.
    """

    value: str
    created_references: frozenset[str]
    _store: CredentialStore = field(repr=False, compare=False)
    _ephemeral_references: frozenset[str] = field(
        default_factory=frozenset,
        repr=False,
        compare=False,
    )

    def discard_unreferenced(self, configured_values: Iterable[Any] = ()) -> None:
        """Remove newly-created entries that no installed config references."""

        if not self.created_references:
            return
        installed: set[str] = set()
        for configured_value in configured_values:
            installed.update(_collect_references(configured_value))
        for reference in self.created_references - installed:
            self._store._discard_uncommitted_reference(  # noqa: SLF001
                reference,
                ephemeral=reference in self._ephemeral_references,
            )

    def commit_replacement(self, previous_value: Any) -> None:
        """Delete superseded references after config + runtime both commit."""

        delete_stale_secret_references(
            previous_value,
            self.value,
            store=self._store,
        )


@dataclass(frozen=True)
class StagedSecretTree:
    """A protected JSON-like tree with precise rollback ownership."""

    value: Any
    created_references: frozenset[str]
    _store: CredentialStore = field(repr=False, compare=False)

    def discard_unreferenced(self, configured_values: Iterable[Any] = ()) -> None:
        """Remove only entries created by this stage and not yet installed."""

        if not self.created_references:
            return
        installed: set[str] = set()
        for configured_value in configured_values:
            installed.update(_collect_references(configured_value))
        for reference in self.created_references - installed:
            self._store.delete(reference)

    def commit_replacement(self, previous_value: Any) -> None:
        """Retire old references only after the protected tree is installed."""

        delete_stale_secret_references(
            previous_value,
            self.value,
            store=self._store,
        )


@dataclass(frozen=True)
class CredentialCleanupTransaction:
    """Durable, evidence-bound retirement of superseded secret references.

    The intent is written before the owning config file changes. If the
    process exits between file replacement and cleanup, startup compares the
    file's exact digest with the recorded before/after digests and either
    activates or cancels the cleanup without guessing.
    """

    transaction_id: str
    _store: CredentialStore = field(repr=False, compare=False)

    def commit(self) -> None:
        """Activate cleanup after the config and runtime commit.

        A failure here must not turn an already-committed API operation into an
        ambiguous error. The prepared intent remains durable and startup will
        reconcile it from the config-file digest.
        """

        try:
            self._store._finish_cleanup_transaction(  # noqa: SLF001
                self.transaction_id,
                committed=True,
            )
        except Exception:
            logger.exception(
                "Credential cleanup %s deferred for startup recovery",
                self.transaction_id,
            )

    def cancel(self) -> None:
        """Cancel cleanup after a failed config transaction, when provable."""

        try:
            self._store._finish_cleanup_transaction(  # noqa: SLF001
                self.transaction_id,
                committed=False,
            )
        except Exception:
            # Retaining a prepared record is safe: recovery never deletes an
            # entry unless the target file matches the recorded new digest.
            logger.exception(
                "Credential cleanup cancellation %s deferred",
                self.transaction_id,
            )


class NativeCredentialBackend(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...
    def set_password(self, service: str, username: str, password: str) -> None: ...
    def delete_password(self, service: str, username: str) -> None: ...


@dataclass
class _MacOSVaultState:
    """Process-shared plaintext state for the one macOS Keychain vault item."""

    lock: threading.RLock = field(default_factory=threading.RLock)
    loaded: bool = False
    credentials: dict[str, str] = field(default_factory=dict)
    legacy_tombstones: set[str] = field(default_factory=set)
    pending_legacy_deletions: set[str] = field(default_factory=set)
    legacy_misses: set[str] = field(default_factory=set)
    load_failure: str | None = None


_MACOS_VAULT_STATE = _MacOSVaultState()


def _macos_atomic_set_password(service: str, username: str, password: str) -> None:
    """Atomically replace a generic-password value with Security.framework."""

    from keyring.backends.macOS import api  # type: ignore[import-not-found]

    sec_item_update = api._sec.SecItemUpdate  # noqa: SLF001
    sec_item_update.restype = api.OS_status
    sec_item_update.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    query = api.create_query(
        kSecClass=api.k_("kSecClassGenericPassword"),
        kSecAttrService=service,
        kSecAttrAccount=username,
    )
    attributes = api.create_query(kSecValueData=password)
    status = sec_item_update(query, attributes)
    if status == api.error.item_not_found:
        add_query = api.create_query(
            kSecClass=api.k_("kSecClassGenericPassword"),
            kSecAttrService=service,
            kSecAttrAccount=username,
            kSecValueData=password,
        )
        status = api.SecItemAdd(add_query, None)
        if status == -25299:  # errSecDuplicateItem: another writer won Add.
            status = sec_item_update(query, attributes)
    api.Error.raise_for_status(status)


class _AtomicMacOSKeyringBackend:
    """Use keyring for reads/legacy deletes and atomic SecItemUpdate for writes."""

    atomic_writes = True

    def __init__(
        self,
        delegate: NativeCredentialBackend,
        *,
        atomic_setter: Any = _macos_atomic_set_password,
        missing_error_types: tuple[type[BaseException], ...] = (),
    ) -> None:
        self._delegate = delegate
        self._atomic_setter = atomic_setter
        self._missing_error_types = missing_error_types

    def get_password(self, service: str, username: str) -> str | None:
        return self._delegate.get_password(service, username)

    def set_password(self, service: str, username: str, password: str) -> None:
        self._atomic_setter(service, username, password)

    def delete_password(self, service: str, username: str) -> None:
        try:
            self._delegate.delete_password(service, username)
        except Exception as exc:
            # keyring wraps api.NotFound in PasswordDeleteError.  Missing is a
            # completed cleanup; every other error (including ACL denial) must
            # fail without a follow-up secret read that could reprompt.
            missing = isinstance(exc.__cause__, self._missing_error_types)
            if not missing:
                raise


class _MacOSVaultBackend:
    """Expose many logical credentials through one macOS Keychain item.

    macOS Keychain ACL consent applies per item.  A versioned map under one
    fixed account therefore turns a multi-reference provider/connector
    activation into at most one Keychain read after migration.  The delegate
    is still Apple's native keyring backend; plaintext exists only in process
    memory and in the encrypted Keychain item.

    Legacy per-identifier items are migrated on demand.  A durable cleanup
    marker is committed in the aggregate item before the old item is deleted,
    so a crash can leave a duplicate but can never lose the only copy.
    """

    vault_managed = True

    def __init__(
        self,
        delegate: NativeCredentialBackend,
        *,
        state: _MacOSVaultState | None = None,
    ) -> None:
        self._delegate = delegate
        self._state = state or _MACOS_VAULT_STATE
        self.write_failures_are_atomic = bool(
            getattr(delegate, "atomic_writes", False)
        )

    @staticmethod
    def _validate_service(service: str) -> None:
        if service != SERVICE_NAME:
            raise CredentialStoreError("Unexpected macOS credential service")

    @staticmethod
    def _decode_vault(payload: str | None) -> tuple[dict[str, str], set[str], set[str]]:
        if payload is None:
            return {}, set(), set()
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise CredentialStoreError("macOS credential vault is corrupt") from exc
        credentials = data.get("credentials") if isinstance(data, dict) else None
        tombstones = data.get("legacy_tombstones") if isinstance(data, dict) else None
        pending = (
            data.get("pending_legacy_deletions") if isinstance(data, dict) else None
        )
        if not (
            isinstance(data, dict)
            and data.get("version") == MACOS_VAULT_VERSION
            and isinstance(credentials, dict)
            and all(
                isinstance(identifier, str)
                and 0 < len(identifier) <= 240
                and isinstance(secret, str)
                and bool(secret)
                for identifier, secret in credentials.items()
            )
            and isinstance(tombstones, list)
            and all(
                isinstance(identifier, str) and 0 < len(identifier) <= 240
                for identifier in tombstones
            )
            and isinstance(pending, list)
            and all(
                isinstance(identifier, str) and 0 < len(identifier) <= 240
                for identifier in pending
            )
        ):
            raise CredentialStoreError("macOS credential vault has an unsupported format")
        return dict(credentials), set(tombstones), set(pending)

    @staticmethod
    def _encode_vault(
        credentials: dict[str, str],
        legacy_tombstones: set[str],
        pending_legacy_deletions: set[str],
    ) -> str:
        return json.dumps(
            {
                "version": MACOS_VAULT_VERSION,
                "credentials": credentials,
                "legacy_tombstones": sorted(legacy_tombstones),
                "pending_legacy_deletions": sorted(pending_legacy_deletions),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    def _invalidate_locked(self) -> None:
        self._state.loaded = False
        self._state.credentials = {}
        self._state.legacy_tombstones = set()
        self._state.pending_legacy_deletions = set()
        self._state.legacy_misses = set()
        self._state.load_failure = None

    def _write_vault_locked(
        self,
        service: str,
        credentials: dict[str, str],
        legacy_tombstones: set[str],
        pending_legacy_deletions: set[str],
    ) -> None:
        payload = self._encode_vault(
            credentials,
            legacy_tombstones,
            pending_legacy_deletions,
        )
        try:
            self._delegate.set_password(service, MACOS_VAULT_ACCOUNT, payload)
        except Exception:
            # Keyring may commit and then report failure.  Do not let a stale
            # cache drive the compensating delete; force an exact reload.
            if not self.write_failures_are_atomic:
                self._invalidate_locked()
            raise
        self._state.loaded = True
        self._state.credentials = dict(credentials)
        self._state.legacy_tombstones = set(legacy_tombstones)
        self._state.pending_legacy_deletions = set(pending_legacy_deletions)

    def _legacy_delete_succeeded_locked(self, service: str, identifier: str) -> bool:
        try:
            self._delegate.delete_password(service, identifier)
            return True
        except Exception:
            return False

    def _retry_legacy_cleanup_locked(
        self,
        service: str,
        identifiers: set[str],
    ) -> None:
        all_pending = set(self._state.pending_legacy_deletions)
        candidates = all_pending & identifiers
        if not candidates:
            return
        remaining = {
            identifier
            for identifier in candidates
            if not self._legacy_delete_succeeded_locked(service, identifier)
        }
        if remaining == candidates:
            return
        next_pending = (all_pending - candidates) | remaining
        try:
            self._write_vault_locked(
                service,
                self._state.credentials,
                self._state.legacy_tombstones,
                next_pending,
            )
        except Exception:
            # The previously committed payload still contains every secret and
            # the conservative cleanup marker.  A later operation can retry.
            logger.warning("Could not finalize legacy macOS credential cleanup")

    def _load_vault_locked(self, service: str) -> None:
        if self._state.loaded:
            return
        if self._state.load_failure is not None:
            raise CredentialStoreError(self._state.load_failure)
        try:
            payload = self._delegate.get_password(service, MACOS_VAULT_ACCOUNT)
            credentials, tombstones, pending = self._decode_vault(payload)
        except Exception as exc:
            failure = (
                str(exc)
                if isinstance(exc, CredentialStoreError)
                else "macOS credential vault is unavailable for this app session"
            )
            self._state.load_failure = failure
            raise CredentialStoreError(failure) from exc
        self._state.credentials = credentials
        self._state.legacy_tombstones = tombstones
        self._state.pending_legacy_deletions = pending
        self._state.legacy_misses = set()
        self._state.load_failure = None
        self._state.loaded = True

    def get_vault_password(self, service: str, username: str) -> str | None:
        """Read only the aggregate item, without consulting legacy items."""

        self._validate_service(service)
        with self._state.lock:
            self._load_vault_locked(service)
            return self._state.credentials.get(username)

    def get_legacy_password(self, service: str, username: str) -> str | None:
        """Read only a pre-vault per-identifier Keychain item."""

        self._validate_service(service)
        return self._delegate.get_password(service, username)

    def get_password(self, service: str, username: str) -> str | None:
        self._validate_service(service)
        if username == MACOS_VAULT_ACCOUNT:
            raise CredentialStoreError("Reserved macOS credential identifier")
        with self._state.lock:
            self._load_vault_locked(service)
            value = self._state.credentials.get(username)
            if value is not None:
                return value
            if (
                username in self._state.legacy_tombstones
                or username in self._state.legacy_misses
            ):
                return None

            legacy_value = self._delegate.get_password(service, username)
            if legacy_value is None:
                self._state.legacy_misses.add(username)
                return None

            credentials = {**self._state.credentials, username: legacy_value}
            tombstones = {*self._state.legacy_tombstones, username}
            pending = {*self._state.pending_legacy_deletions, username}
            # Commit the recoverable copy before touching the old item.
            self._write_vault_locked(service, credentials, tombstones, pending)
            self._retry_legacy_cleanup_locked(service, {username})
            return legacy_value

    def set_password(self, service: str, username: str, password: str) -> None:
        self._validate_service(service)
        if username == MACOS_VAULT_ACCOUNT:
            raise CredentialStoreError("Reserved macOS credential identifier")
        with self._state.lock:
            self._load_vault_locked(service)
            credentials = {**self._state.credentials, username: password}
            tombstones = {*self._state.legacy_tombstones, username}
            self._write_vault_locked(
                service,
                credentials,
                tombstones,
                self._state.pending_legacy_deletions,
            )
            self._retry_legacy_cleanup_locked(service, {username})

    def delete_password(self, service: str, username: str) -> None:
        """Delete only one logical key from the aggregate vault."""

        self._validate_service(service)
        if username == MACOS_VAULT_ACCOUNT:
            raise CredentialStoreError("Reserved macOS credential identifier")
        with self._state.lock:
            self._load_vault_locked(service)
            credentials = dict(self._state.credentials)
            was_known_to_vault = (
                username in credentials
                or username in self._state.legacy_tombstones
            )
            credentials.pop(username, None)
            tombstones = {*self._state.legacy_tombstones, username}
            pending = set(self._state.pending_legacy_deletions)
            if not was_known_to_vault:
                # A config cleanup can retire a legacy reference without ever
                # resolving it.  Journal that physical deletion inside the
                # vault before attempting it.  Known vault IDs skip this path,
                # avoiding one legacy Keychain probe per normal deletion.
                pending.add(username)
            self._write_vault_locked(
                service,
                credentials,
                tombstones,
                pending,
            )
            self._retry_legacy_cleanup_locked(service, {username})

    def delete_legacy_password(self, service: str, username: str) -> None:
        """Delete only a pre-vault item; never mutate the aggregate map."""

        self._validate_service(service)
        with self._state.lock:
            if not self._legacy_delete_succeeded_locked(service, username):
                raise CredentialStoreError("Legacy macOS credential deletion failed")

    def adopt_deletion_journals(
        self,
        service: str,
        *,
        legacy_identifiers: set[str],
        vault_identifiers: set[str],
    ) -> None:
        """Move file journals into one atomic vault mutation without ACL fan-out."""

        self._validate_service(service)
        if not legacy_identifiers and not vault_identifiers:
            return
        with self._state.lock:
            self._load_vault_locked(service)
            credentials = dict(self._state.credentials)
            originally_known = set(credentials) | self._state.legacy_tombstones
            for identifier in vault_identifiers:
                credentials.pop(identifier, None)
            tombstones = (
                set(self._state.legacy_tombstones)
                | legacy_identifiers
                | vault_identifiers
            )
            pending_legacy = (
                set(self._state.pending_legacy_deletions)
                | legacy_identifiers
                | (vault_identifiers - originally_known)
            )
            if (
                credentials == self._state.credentials
                and tombstones == self._state.legacy_tombstones
                and pending_legacy == self._state.pending_legacy_deletions
            ):
                return
            self._write_vault_locked(
                service,
                credentials,
                tombstones,
                pending_legacy,
            )


def _discover_native_backend() -> NativeCredentialBackend | None:
    try:
        # Select only the operating system's native secret service. Avoid the
        # generic keyring chainer here: third-party plaintext keyring backends
        # must never become an implicit production credential store.
        is_macos = sys.platform == "darwin"
        if is_macos:
            from keyring.backends.macOS import (  # type: ignore[import-not-found]
                Keyring,
                api,
            )

            backend = Keyring()
        elif sys.platform == "win32":
            from keyring.backends.Windows import WinVaultKeyring  # type: ignore[import-not-found]

            backend = WinVaultKeyring()
        else:
            from keyring.backends.SecretService import Keyring  # type: ignore[import-not-found]

            backend = Keyring()
        backend_name = f"{type(backend).__module__}.{type(backend).__name__}".casefold()
        try:
            priority = float(getattr(backend, "priority", 0))
        except (TypeError, ValueError, RuntimeError):
            priority = 0
        if priority <= 0 or ".fail." in backend_name or ".null." in backend_name:
            return None
        if is_macos:
            return _MacOSVaultBackend(
                _AtomicMacOSKeyringBackend(
                    backend,
                    missing_error_types=(api.NotFound,),
                )
            )
        return backend
    except Exception as exc:
        logger.info("Native credential backend unavailable; using private fallback: %s", exc)
        return None


def is_credential_reference(value: object) -> bool:
    return isinstance(value, str) and value.startswith(REFERENCE_PREFIX)


def _reference(identifier: str) -> str:
    return REFERENCE_PREFIX + quote(identifier, safe="")


def _identifier(reference_or_identifier: str) -> str:
    if is_credential_reference(reference_or_identifier):
        identifier = unquote(reference_or_identifier[len(REFERENCE_PREFIX) :])
    else:
        identifier = reference_or_identifier
    if not identifier or len(identifier) > 240:
        raise CredentialStoreError("Invalid credential identifier")
    if identifier == MACOS_VAULT_ACCOUNT:
        raise CredentialStoreError("Reserved credential identifier")
    return identifier


def _content_evidence(*, exists: bool, content: bytes) -> str:
    marker = b"present\0" if exists else b"absent\0"
    return hashlib.sha256(marker + content).hexdigest()


def _path_evidence(path: Path) -> str:
    try:
        if not path.exists():
            return _content_evidence(exists=False, content=b"")
        if not path.is_file():
            raise CredentialStoreError(
                f"Credential cleanup evidence target is not a file: {path}"
            )
        return _content_evidence(exists=True, content=path.read_bytes())
    except OSError as exc:
        raise CredentialStoreError(
            f"Cannot inspect credential cleanup evidence {path}: {exc}"
        ) from exc


class CredentialStore:
    """Store secrets natively and expose opaque configuration references."""

    def __init__(
        self,
        *,
        fallback_path: str | Path | None = None,
        native_backend: NativeCredentialBackend | None | object = ...,
    ) -> None:
        self.fallback_path = (
            Path(fallback_path).expanduser().resolve()
            if fallback_path is not None
            else Path.cwd().resolve() / "data" / "credentials" / "fallback.json"
        )
        self._native_backend: NativeCredentialBackend | None | object = (
            _NATIVE_BACKEND_UNDISCOVERED
            if native_backend is ...
            else native_backend
        )
        (
            self._fallback,
            self._pending_native_deletions,
            self._pending_native_vault_deletions,
            self._cleanup_transactions,
        ) = self._load_fallback_state()
        # Recover durable file-side evidence immediately, but do not touch the
        # OS credential service merely because a CredentialStore was created
        # during application startup. Public read/write/delete operations retry
        # any resulting native cleanup journal at an explicit use boundary.
        self._reconcile_cleanup_transactions(retry_native=False)

    @property
    def uses_native_backend(self) -> bool:
        return self.native_backend is not None

    @property
    def native_backend(self) -> NativeCredentialBackend | None:
        """Discover the platform vault only at an explicit credential operation."""

        if self._native_backend is _NATIVE_BACKEND_UNDISCOVERED:
            with _FILE_LOCK:
                if self._native_backend is _NATIVE_BACKEND_UNDISCOVERED:
                    self._native_backend = _discover_native_backend()
        return self._native_backend  # type: ignore[return-value]

    @staticmethod
    def _target_for_backend(backend: NativeCredentialBackend) -> str:
        return (
            _NATIVE_TARGET_VAULT
            if getattr(backend, "vault_managed", False)
            else _NATIVE_TARGET_INDIVIDUAL
        )

    def _cleanup_target_for_new_transaction(self) -> str:
        # Do not force Keychain discovery while merely preparing file-side
        # evidence.  All new macOS writes use the aggregate vault, even when
        # this process has not crossed a credential-use boundary yet.
        if self._native_backend is _NATIVE_BACKEND_UNDISCOVERED:
            return (
                _NATIVE_TARGET_VAULT
                if sys.platform == "darwin"
                else _NATIVE_TARGET_INDIVIDUAL
            )
        backend = self._native_backend
        if backend is None:
            return (
                _NATIVE_TARGET_VAULT
                if sys.platform == "darwin"
                else _NATIVE_TARGET_INDIVIDUAL
            )
        return self._target_for_backend(backend)

    def put(self, identifier: str, secret: str) -> str:
        self._reconcile_cleanup_transactions()
        self._retry_pending_native_deletions()
        identifier = _identifier(identifier)
        if is_credential_reference(secret):
            return secret
        if not isinstance(secret, str) or not secret:
            raise CredentialStoreError("Refusing to persist an empty credential")

        intended_target = self._cleanup_target_for_new_transaction()
        backend = self.native_backend
        if backend is not None:
            native_target = self._target_for_backend(backend)
            # Journal adoption also holds this process-wide lock while mutating
            # the aggregate vault. Keep a fallback-to-vault authority handoff
            # under the same lock so another store cannot adopt its temporary
            # marker after the new native value is written but before the file
            # handoff commits.
            native_write_guard = (
                _FILE_LOCK
                if native_target == _NATIVE_TARGET_VAULT
                else nullcontext()
            )
            with native_write_guard:
                if native_target == _NATIVE_TARGET_VAULT:
                    # A fallback remains authoritative until the native write
                    # and file-side handoff both commit. Persist that authority
                    # marker before SecItemUpdate so a crash cannot expose an
                    # unmarked stale fallback or make a denied restart
                    # ambiguous.
                    self._reload_fallback_locked()
                    if (
                        identifier in self._fallback
                        and identifier not in self._pending_native_vault_deletions
                    ):
                        self._pending_native_vault_deletions.add(identifier)
                        self._persist_fallback_locked()
                try:
                    backend.set_password(SERVICE_NAME, identifier, secret)
                except Exception as exc:
                    logger.warning(
                        "Native credential write failed for %s; using private fallback: %s",
                        identifier,
                        exc,
                    )
                    if (
                        native_target == _NATIVE_TARGET_VAULT
                        and getattr(backend, "write_failures_are_atomic", False)
                    ):
                        # SecItemUpdate/Add failures cannot partially replace
                        # the vault. Commit the fallback and its logical-key
                        # cleanup responsibility together; only then may a
                        # later explicit mutation retire the older vault value.
                        with _FILE_LOCK:
                            self._reload_fallback_locked()
                            self._fallback[identifier] = secret
                            self._pending_native_vault_deletions.add(identifier)
                            self._persist_fallback_locked()
                        return _reference(identifier)
                    # Native services may report failure after committing the
                    # write. Compensate inside put(), before callers could lose
                    # the identifier needed to track that partial write.
                    if not self._try_native_delete(identifier, target=native_target):
                        try:
                            self._queue_native_deletion(
                                identifier,
                                target=native_target,
                            )
                        except Exception as cleanup_exc:
                            cleanup_exc.add_note(
                                f"native write also failed for {identifier}: {exc}"
                            )
                            raise
                else:
                    try:
                        self._delete_fallback(identifier, target=native_target)
                    except Exception as cleanup_exc:
                        # Authority has not been durably handed from a prior
                        # fallback to this native write. Compensate the logical
                        # key (aggregate-vault deletion is atomic and preserves
                        # every sibling key) and report failure instead of
                        # letting an older fallback+authority marker mask the
                        # new value.
                        if not self._try_native_delete(
                            identifier,
                            target=native_target,
                        ):
                            try:
                                self._queue_native_deletion(
                                    identifier,
                                    target=native_target,
                                )
                            except Exception as journal_exc:
                                journal_exc.add_note(
                                    "native credential cleanup also failed after "
                                    "fallback handoff failure for "
                                    f"{identifier}: {cleanup_exc}"
                                )
                                raise
                        raise
                    return _reference(identifier)

        with _FILE_LOCK:
            # Multiple CredentialStore instances can share one fallback file
            # (env + MCP + channel stores). Merge the latest on-disk state
            # before every mutation so one namespace cannot erase another.
            self._reload_fallback_locked()
            self._fallback[identifier] = secret
            if intended_target == _NATIVE_TARGET_VAULT:
                self._pending_native_vault_deletions.add(identifier)
            self._persist_fallback_locked()
        return _reference(identifier)

    def get(self, reference_or_identifier: str) -> str | None:
        identifier = _identifier(reference_or_identifier)
        self._reconcile_cleanup_transactions(retry_native=False)
        # A present fallback is the committed value after a native write
        # failure.  Prefer it over a stale/partially-written legacy item and do
        # not ask the user to unlock Keychain unnecessarily.
        with _FILE_LOCK:
            self._reload_fallback_locked()
            fallback_value = self._fallback.get(identifier)
            fallback_overrides_vault = (
                identifier in self._pending_native_vault_deletions
            )
            legacy_journal_with_fallback = (
                fallback_value is not None
                and identifier in self._pending_native_deletions
            )
        if fallback_overrides_vault:
            # The typed marker is a logical authority boundary, not merely a
            # retry hint.  With a fallback it selects that committed value;
            # without one it represents a committed deletion.  In both cases
            # consulting the still-stale aggregate vault could resurrect a
            # value during the file-intent -> Keychain-mutation crash window.
            return fallback_value

        backend = self.native_backend
        if (
            legacy_journal_with_fallback
            and backend is not None
            and getattr(backend, "vault_managed", False)
        ):
            # Pre-vault journals paired with a fallback also represent
            # fallback authority after upgrade.  Avoid letting a stale
            # aggregate key win during the journal-conversion crash window.
            return fallback_value
        if backend is not None and not getattr(backend, "vault_managed", False):
            # Preserve the historical retry-on-use behavior for Credential
            # Manager/Secret Service. macOS vault/legacy journals are skipped
            # on reads so one normal unlock cannot fan out to old ACL items.
            self._retry_pending_native_deletions()
        if backend is not None:
            try:
                value = backend.get_password(SERVICE_NAME, identifier)
            except Exception as exc:
                logger.warning("Native credential read failed for %s: %s", identifier, exc)
                if getattr(backend, "vault_managed", False):
                    # Without an explicit ambiguity marker, a fallback may be
                    # the stale value left by a crash after native commit.
                    # Never silently downgrade to it while vault authority is
                    # unknown.
                    return None
            else:
                if value is not None:
                    if fallback_value is not None:
                        try:
                            self._delete_fallback(
                                identifier,
                                target=self._target_for_backend(backend),
                            )
                        except Exception:
                            logger.warning(
                                "Could not retire stale credential fallback for %s",
                                identifier,
                            )
                    return value
        return fallback_value

    def resolve(self, value: str) -> str:
        if not is_credential_reference(value):
            return value
        secret = self.get(value)
        if secret is None:
            raise CredentialStoreError(
                f"Credential reference cannot be resolved: {_identifier(value)}"
            )
        return secret

    def delete(self, reference_or_identifier: str) -> None:
        identifier = _identifier(reference_or_identifier)
        self._reconcile_cleanup_transactions(retry_native=False)
        native_target = self._cleanup_target_for_new_transaction()

        # Commit logical deletion before touching any OS vault.  A crash after
        # the native mutation can therefore leave only a conservative retry
        # marker, never an unmarked fallback that resurrects the credential.
        with _FILE_LOCK:
            self._reload_fallback_locked()
            changed = self._fallback.pop(identifier, None) is not None
            pending = (
                self._pending_native_vault_deletions
                if native_target == _NATIVE_TARGET_VAULT
                else self._pending_native_deletions
            )
            if identifier not in pending:
                pending.add(identifier)
                changed = True
            if changed:
                self._persist_fallback_locked()

        backend = self.native_backend
        if backend is None:
            return

        actual_target = self._target_for_backend(backend)
        if actual_target != native_target:
            # Injectable/test backends can differ from the platform default.
            # Move the durable intent before invoking that backend.
            with _FILE_LOCK:
                self._reload_fallback_locked()
                old_pending = (
                    self._pending_native_vault_deletions
                    if native_target == _NATIVE_TARGET_VAULT
                    else self._pending_native_deletions
                )
                new_pending = (
                    self._pending_native_vault_deletions
                    if actual_target == _NATIVE_TARGET_VAULT
                    else self._pending_native_deletions
                )
                old_pending.discard(identifier)
                new_pending.add(identifier)
                self._persist_fallback_locked()
            native_target = actual_target

        if native_target == _NATIVE_TARGET_VAULT:
            # The macOS adopter applies all logical removals in one aggregate
            # vault update and clears only journals whose fallback is gone.
            self._retry_pending_native_deletions()
            return

        native_deleted = self._try_native_delete(
            identifier,
            target=native_target,
        )
        if not native_deleted:
            return
        with _FILE_LOCK:
            self._reload_fallback_locked()
            pending = self._pending_native_deletions
            if identifier in pending:
                pending.discard(identifier)
                self._persist_fallback_locked()

    def _discard_uncommitted_reference(
        self,
        reference_or_identifier: str,
        *,
        ephemeral: bool,
    ) -> None:
        """Roll back one staged reference without inventing a vault tombstone.

        Simple protected env values use a fresh random identifier for every
        staging attempt. If that attempt conclusively used only this store's
        fallback (native discovery completed with no backend), no Keychain
        value can have been written under the new identifier. In that narrow
        case the fallback value and its provisional authority marker belong to
        the same uncommitted attempt and can be removed atomically. Every
        native-success, native-ambiguous, stable-tree, or otherwise uncertain
        case retains the normal crash-safe delete path.
        """

        identifier = _identifier(reference_or_identifier)
        if ephemeral:
            with _FILE_LOCK:
                self._reload_fallback_locked()
                if (
                    self._native_backend is None
                    and _EPHEMERAL_ENV_IDENTIFIER.fullmatch(identifier)
                    and identifier in self._fallback
                ):
                    self._fallback.pop(identifier, None)
                    self._pending_native_deletions.discard(identifier)
                    self._pending_native_vault_deletions.discard(identifier)
                    self._persist_fallback_locked()
                    return
        self.delete(identifier)

    def _load_fallback_state(
        self,
    ) -> tuple[
        dict[str, str],
        set[str],
        set[str],
        dict[str, dict[str, Any]],
    ]:
        if not self.fallback_path.is_file():
            return {}, set(), set(), {}
        try:
            payload = json.loads(self.fallback_path.read_text(encoding="utf-8"))
            credentials = payload.get("credentials") if isinstance(payload, dict) else None
            pending = (
                payload.get("pending_native_deletions", [])
                if isinstance(payload, dict)
                else None
            )
            pending_vault = (
                payload.get("pending_native_vault_deletions", [])
                if isinstance(payload, dict)
                else None
            )
            cleanup_transactions = (
                payload.get("cleanup_transactions", {})
                if isinstance(payload, dict)
                else None
            )
            if (
                isinstance(payload, dict)
                and payload.get("version") == FALLBACK_VERSION
                and isinstance(credentials, dict)
                and all(isinstance(k, str) and isinstance(v, str) for k, v in credentials.items())
                and isinstance(pending, list)
                and all(
                    isinstance(identifier, str) and 0 < len(identifier) <= 240
                    for identifier in pending
                )
                and isinstance(pending_vault, list)
                and all(
                    isinstance(identifier, str) and 0 < len(identifier) <= 240
                    for identifier in pending_vault
                )
                and isinstance(cleanup_transactions, dict)
                and all(
                    isinstance(transaction_id, str)
                    and transaction_id
                    and isinstance(record, dict)
                    and isinstance(record.get("identifiers"), list)
                    and bool(record["identifiers"])
                    and all(
                        isinstance(identifier, str) and 0 < len(identifier) <= 240
                        for identifier in record["identifiers"]
                    )
                    and isinstance(record.get("evidence_path"), str)
                    and bool(record["evidence_path"])
                    and isinstance(record.get("previous_evidence"), str)
                    and len(record["previous_evidence"]) == 64
                    and isinstance(record.get("next_evidence"), str)
                    and len(record["next_evidence"]) == 64
                    and (
                        "native_target" not in record
                        or record.get("native_target") in _NATIVE_TARGETS
                    )
                    for transaction_id, record in cleanup_transactions.items()
                )
            ):
                try:
                    os.chmod(self.fallback_path, 0o600)
                except OSError:
                    pass
                return (
                    dict(credentials),
                    set(pending),
                    set(pending_vault),
                    copy.deepcopy(cleanup_transactions),
                )
        except (OSError, json.JSONDecodeError) as exc:
            raise CredentialStoreError(
                f"Cannot read credential fallback {self.fallback_path}: {exc}"
            ) from exc
        raise CredentialStoreError(
            f"Credential fallback has an unsupported format: {self.fallback_path}"
        )

    def _persist_fallback_locked(self) -> None:
        self.fallback_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            self.fallback_path,
            json.dumps(
                {
                    "version": FALLBACK_VERSION,
                    "credentials": self._fallback,
                    "pending_native_deletions": sorted(
                        self._pending_native_deletions
                    ),
                    "pending_native_vault_deletions": sorted(
                        self._pending_native_vault_deletions
                    ),
                    "cleanup_transactions": self._cleanup_transactions,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            mode=0o600,
        )

    def _delete_fallback(self, identifier: str, *, target: str) -> None:
        with _FILE_LOCK:
            self._reload_fallback_locked()
            changed = self._fallback.pop(identifier, None) is not None
            pending = (
                self._pending_native_vault_deletions
                if target == _NATIVE_TARGET_VAULT
                else self._pending_native_deletions
            )
            if identifier in pending:
                pending.discard(identifier)
                changed = True
            if changed:
                self._persist_fallback_locked()

    def _reload_fallback_locked(self) -> None:
        (
            self._fallback,
            self._pending_native_deletions,
            self._pending_native_vault_deletions,
            self._cleanup_transactions,
        ) = self._load_fallback_state()

    def _try_native_delete(self, identifier: str, *, target: str) -> bool:
        if target not in _NATIVE_TARGETS:
            raise CredentialStoreError("Invalid native credential deletion target")
        backend = self.native_backend
        if backend is None:
            return False

        if target == _NATIVE_TARGET_LEGACY:
            deleter = getattr(backend, "delete_legacy_password", None)
            getter = getattr(backend, "get_legacy_password", None)
            if deleter is None or getter is None:
                # Never reinterpret an old physical-item journal as a logical
                # aggregate-vault deletion.
                return False
        elif target == _NATIVE_TARGET_VAULT:
            if not getattr(backend, "vault_managed", False):
                # A vault mutation journal is unsafe to replay against a
                # per-identifier backend.
                return False
            deleter = backend.delete_password
            getter = getattr(backend, "get_vault_password", None)
            if getter is None:
                return False
        else:
            deleter = backend.delete_password
            getter = backend.get_password

        try:
            deleter(SERVICE_NAME, identifier)
            return True
        except Exception as exc:
            if target in {_NATIVE_TARGET_LEGACY, _NATIVE_TARGET_VAULT}:
                logger.warning(
                    "Native credential deletion deferred for %s (%s): %s",
                    identifier,
                    target,
                    exc,
                )
                return False
            # Some native backends signal a missing item as a deletion error.
            # A follow-up read distinguishes that harmless state from a locked
            # or otherwise unavailable credential service.
            try:
                still_present = getter(SERVICE_NAME, identifier)
            except Exception:
                still_present = "unknown"
            if still_present is None:
                return True
            logger.warning(
                "Native credential deletion deferred for %s: %s",
                identifier,
                exc,
            )
            return False

    def _queue_native_deletion(self, identifier: str, *, target: str) -> None:
        """Durably hand a possibly-partial native write to the retry journal."""

        with _FILE_LOCK:
            self._reload_fallback_locked()
            pending = (
                self._pending_native_vault_deletions
                if target == _NATIVE_TARGET_VAULT
                else self._pending_native_deletions
            )
            if identifier in pending:
                return
            pending.add(identifier)
            self._persist_fallback_locked()

    def prepare_cleanup_transaction(
        self,
        references: Iterable[str],
        *,
        evidence_path: str | Path,
        previous_exists: bool,
        previous_content: str | bytes,
        next_exists: bool,
        next_content: str | bytes,
    ) -> CredentialCleanupTransaction | None:
        """Write a recoverable stale-reference intent before config commit."""

        self._reconcile_cleanup_transactions()
        identifiers = sorted({_identifier(reference) for reference in references})
        if not identifiers:
            return None
        # Keep the lexical target path. atomic_write_text replaces a symlink at
        # that path rather than its referent, so resolving here would make the
        # recovery digest observe a different file after the replacement.
        path = Path(os.path.abspath(Path(evidence_path).expanduser()))
        previous_bytes = (
            previous_content.encode("utf-8")
            if isinstance(previous_content, str)
            else previous_content
        )
        next_bytes = (
            next_content.encode("utf-8")
            if isinstance(next_content, str)
            else next_content
        )
        transaction_id = f"cleanup:{secrets.token_hex(16)}"
        record = {
            "identifiers": identifiers,
            "native_target": self._cleanup_target_for_new_transaction(),
            "evidence_path": str(path),
            "previous_evidence": _content_evidence(
                exists=previous_exists,
                content=previous_bytes,
            ),
            "next_evidence": _content_evidence(
                exists=next_exists,
                content=next_bytes,
            ),
        }
        with _FILE_LOCK:
            self._reload_fallback_locked()
            self._cleanup_transactions[transaction_id] = record
            self._persist_fallback_locked()
        return CredentialCleanupTransaction(transaction_id, self)

    def _activate_cleanup_locked(self, transaction_id: str) -> None:
        record = self._cleanup_transactions[transaction_id]
        identifiers = set(record["identifiers"])
        fallback_backed = identifiers & self._fallback.keys()
        for identifier in identifiers:
            self._fallback.pop(identifier, None)
        target = record.get("native_target")
        if target == _NATIVE_TARGET_VAULT:
            # A file fallback can coexist with a stale logical key in the
            # aggregate vault.  This commit removes the fallback, so every
            # referenced identifier must acquire a vault-deletion intent in
            # that same atomic file mutation.
            self._pending_native_vault_deletions.update(identifiers)
        else:
            # Native-backend discovery is allowed to be temporarily
            # unavailable (notably when Linux Secret Service/DBus is offline).
            # References without a file-backed value may still live in a
            # per-identifier native store and therefore remain journaled.
            native_candidates = identifiers - fallback_backed
            # Records created before aggregate-vault support had no target and
            # refer to physical per-identifier items on macOS.
            self._pending_native_deletions.update(native_candidates)
        del self._cleanup_transactions[transaction_id]

    def _finish_cleanup_transaction(
        self,
        transaction_id: str,
        *,
        committed: bool,
    ) -> None:
        should_retry_native = False
        with _FILE_LOCK:
            self._reload_fallback_locked()
            record = self._cleanup_transactions.get(transaction_id)
            if record is None:
                return
            current = _path_evidence(Path(record["evidence_path"]))
            expected = (
                record["next_evidence"]
                if committed
                else record["previous_evidence"]
            )
            if current != expected:
                raise CredentialStoreError(
                    "Credential cleanup evidence does not match the requested outcome"
                )
            if committed:
                self._activate_cleanup_locked(transaction_id)
                should_retry_native = self.native_backend is not None
            else:
                del self._cleanup_transactions[transaction_id]
            self._persist_fallback_locked()
        if should_retry_native:
            self._retry_pending_native_deletions()

    def _reconcile_cleanup_transactions(self, *, retry_native: bool = True) -> None:
        """Recover prepared cleanup records from exact on-disk evidence."""

        should_retry_native = False
        with _FILE_LOCK:
            self._reload_fallback_locked()
            changed = False
            for transaction_id, record in list(self._cleanup_transactions.items()):
                current = _path_evidence(Path(record["evidence_path"]))
                if current == record["next_evidence"]:
                    self._activate_cleanup_locked(transaction_id)
                    should_retry_native = should_retry_native or (
                        retry_native and self.native_backend is not None
                    )
                    changed = True
                elif current == record["previous_evidence"]:
                    del self._cleanup_transactions[transaction_id]
                    changed = True
            if changed:
                self._persist_fallback_locked()
        if should_retry_native and retry_native:
            self._retry_pending_native_deletions()

    def _retry_pending_native_deletions(self) -> None:
        backend = self.native_backend
        if backend is None:
            return
        with _FILE_LOCK:
            self._reload_fallback_locked()
            if (
                not self._pending_native_deletions
                and not self._pending_native_vault_deletions
            ):
                return
            if getattr(backend, "vault_managed", False):
                adopter = getattr(backend, "adopt_deletion_journals", None)
                if adopter is None:
                    return
                previous_legacy = set(self._pending_native_deletions)
                previous_vault = set(self._pending_native_vault_deletions)
                legacy_fallback_authority = (
                    previous_legacy & self._fallback.keys()
                )
                try:
                    adopter(
                        SERVICE_NAME,
                        legacy_identifiers=previous_legacy,
                        vault_identifiers=set(
                            previous_vault | legacy_fallback_authority
                        ),
                    )
                except Exception as exc:
                    logger.warning(
                        "macOS credential deletion journals remain deferred: %s",
                        exc,
                    )
                    return
                # A fallback paired with either historical journal is still
                # the committed value after the vault mutation.  Preserve (or
                # convert) its typed vault authority marker until a successful
                # put/delete retires the fallback itself.  Journal-only IDs can
                # be cleared because their deletion is now durable in the
                # aggregate vault.
                fallback_authority = (
                    previous_legacy | previous_vault
                ) & self._fallback.keys()
                self._pending_native_deletions = set()
                self._pending_native_vault_deletions = set(fallback_authority)
                if (
                    self._pending_native_deletions != previous_legacy
                    or self._pending_native_vault_deletions != previous_vault
                ):
                    self._persist_fallback_locked()
                return
            legacy_target = (
                _NATIVE_TARGET_INDIVIDUAL
            )
            remaining_individual = {
                identifier
                for identifier in self._pending_native_deletions
                if not self._try_native_delete(identifier, target=legacy_target)
            }
            remaining_vault = {
                identifier
                for identifier in self._pending_native_vault_deletions
                if not self._try_native_delete(
                    identifier,
                    target=_NATIVE_TARGET_VAULT,
                )
            }
            if (
                remaining_individual != self._pending_native_deletions
                or remaining_vault != self._pending_native_vault_deletions
            ):
                self._pending_native_deletions = remaining_individual
                self._pending_native_vault_deletions = remaining_vault
                self._persist_fallback_locked()


_CREDENTIAL_STORE_SINGLETON: CredentialStore | None = None


def get_credential_store() -> CredentialStore:
    """Return exactly one process-wide store, including concurrent first use."""

    global _CREDENTIAL_STORE_SINGLETON
    with _CREDENTIAL_STORE_SINGLETON_LOCK:
        if _CREDENTIAL_STORE_SINGLETON is None:
            _CREDENTIAL_STORE_SINGLETON = CredentialStore()
        return _CREDENTIAL_STORE_SINGLETON


def _clear_credential_store_cache() -> None:
    """Test hook matching the historical ``lru_cache.cache_clear`` surface."""

    global _CREDENTIAL_STORE_SINGLETON
    with _CREDENTIAL_STORE_SINGLETON_LOCK:
        _CREDENTIAL_STORE_SINGLETON = None
        with _MACOS_VAULT_STATE.lock:
            _MACOS_VAULT_STATE.loaded = False
            _MACOS_VAULT_STATE.credentials = {}
            _MACOS_VAULT_STATE.legacy_tombstones = set()
            _MACOS_VAULT_STATE.pending_legacy_deletions = set()
            _MACOS_VAULT_STATE.legacy_misses = set()
            _MACOS_VAULT_STATE.load_failure = None


get_credential_store.cache_clear = _clear_credential_store_cache  # type: ignore[attr-defined]


def credential_tree_id(namespace: str, path: tuple[str, ...]) -> str:
    material = "\x1f".join((namespace, *path))
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]
    # A fresh identifier gives config writes transactional semantics: storing
    # a replacement secret cannot mutate what the currently-installed config
    # reference resolves to before that config is atomically replaced.
    return f"tree:{namespace}:{digest}:{secrets.token_hex(12)}"


def _is_secret_field(name: str) -> bool:
    normalized = name.strip().casefold().replace("-", "_")
    return normalized in _SECRET_FIELD_NAMES or normalized.endswith(
        ("_api_key", "_access_token", "_refresh_token", "_password", "_secret")
    )


def protect_secret_tree(
    namespace: str,
    data: Any,
    *,
    store: CredentialStore | None = None,
    created_references: set[str] | None = None,
) -> Any:
    """Replace secret-looking string leaves with credential references."""

    target = copy.deepcopy(data)
    credential_store = store or get_credential_store()

    def visit(value: Any, path: tuple[str, ...], force_secret: bool = False) -> Any:
        if isinstance(value, dict):
            protected: dict[Any, Any] = {}
            for key, item in value.items():
                key_text = str(key)
                child_force = force_secret or key_text.casefold() in {"headers", "extra_headers"}
                if isinstance(item, str) and (force_secret or _is_secret_field(key_text)):
                    if not item:
                        protected[key] = item
                    elif is_credential_reference(item):
                        protected[key] = item
                    else:
                        reference = credential_store.put(
                            credential_tree_id(namespace, (*path, key_text)),
                            item,
                        )
                        protected[key] = reference
                        if created_references is not None:
                            created_references.add(reference)
                else:
                    protected[key] = visit(item, (*path, key_text), child_force)
            return protected
        if isinstance(value, list):
            return [visit(item, (*path, str(index)), force_secret) for index, item in enumerate(value)]
        if force_secret and isinstance(value, str) and value:
            if is_credential_reference(value):
                return value
            reference = credential_store.put(credential_tree_id(namespace, path), value)
            if created_references is not None:
                created_references.add(reference)
            return reference
        return value

    return visit(target, ())


def stage_protected_secret_tree(
    namespace: str,
    data: Any,
    *,
    previous_value: Any = None,
    store: CredentialStore | None = None,
) -> StagedSecretTree:
    """Protect a JSON-like tree and retain exact rollback ownership."""

    credential_store = store or get_credential_store()
    created: set[str] = set()
    try:
        protected = protect_secret_tree(
            namespace,
            data,
            store=credential_store,
            created_references=created,
        )
    except Exception:
        installed = () if previous_value is None else (previous_value,)
        staged = StagedSecretTree(
            value=data,
            created_references=frozenset(created),
            _store=credential_store,
        )
        staged.discard_unreferenced(installed)
        raise
    return StagedSecretTree(
        value=protected,
        created_references=frozenset(created),
        _store=credential_store,
    )


def resolve_secret_tree(data: Any, *, store: CredentialStore | None = None) -> Any:
    """Resolve every credential reference in an arbitrary JSON-like tree."""

    credential_store = store or get_credential_store()
    if isinstance(data, dict):
        return {key: resolve_secret_tree(value, store=credential_store) for key, value in data.items()}
    if isinstance(data, list):
        return [resolve_secret_tree(value, store=credential_store) for value in data]
    if is_credential_reference(data):
        return credential_store.resolve(data)
    return data


def is_secret_env_key(key: str) -> bool:
    normalized = key.upper()
    if normalized.endswith(("_TOKEN_PATH", "_TOKEN_LIMIT")):
        return False
    return normalized.endswith(("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD"))


def protect_env_value(
    key: str,
    value: str,
    *,
    previous_value: str | None = None,
    store: CredentialStore | None = None,
    created_references: set[str] | None = None,
) -> str:
    if key.upper() == "SUXIAOYOU_CUSTOM_ENDPOINTS":
        credential_store = store or get_credential_store()
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise CredentialStoreError("Custom endpoint configuration is invalid JSON") from exc
        protected = protect_secret_tree(
            "custom-endpoints",
            parsed,
            store=credential_store,
            created_references=created_references,
        )
        return json.dumps(protected, ensure_ascii=False)
    if is_secret_env_key(key) and value:
        credential_store = store or get_credential_store()
        if is_credential_reference(value):
            return value
        reference = credential_store.put(
            f"env:{key.upper()}:{secrets.token_hex(12)}",
            value,
        )
        if created_references is not None:
            created_references.add(reference)
        return reference
    return value


def stage_protected_env_value(
    key: str,
    value: str,
    *,
    previous_value: str | None = None,
    store: CredentialStore | None = None,
) -> StagedEnvValue:
    """Protect a value while retaining precise rollback ownership.

    If protection itself fails part-way through a secret tree, entries already
    written by this attempt are removed before the error escapes.
    """

    credential_store = store or get_credential_store()
    created: set[str] = set()
    ephemeral_identifier = (
        key.upper() != "SUXIAOYOU_CUSTOM_ENDPOINTS"
        and is_secret_env_key(key)
        and bool(value)
        and not is_credential_reference(value)
    )
    try:
        protected = protect_env_value(
            key,
            value,
            previous_value=previous_value,
            store=credential_store,
            created_references=created,
        )
    except Exception:
        for reference in created:
            credential_store._discard_uncommitted_reference(  # noqa: SLF001
                reference,
                ephemeral=ephemeral_identifier,
            )
        raise
    return StagedEnvValue(
        value=protected,
        created_references=frozenset(created),
        _store=credential_store,
        _ephemeral_references=(
            frozenset(created) if ephemeral_identifier else frozenset()
        ),
    )


def resolve_env_value(
    key: str,
    value: str,
    *,
    store: CredentialStore | None = None,
) -> str:
    if key.upper() == "SUXIAOYOU_CUSTOM_ENDPOINTS":
        credential_store = store or get_credential_store()
        parsed = json.loads(value)
        return json.dumps(
            resolve_secret_tree(parsed, store=credential_store),
            ensure_ascii=False,
        )
    if is_credential_reference(value):
        credential_store = store or get_credential_store()
        return credential_store.resolve(value)
    return value


def delete_env_credentials(
    key: str,
    persisted_value: str | None,
    *,
    store: CredentialStore | None = None,
) -> None:
    if key.upper() == "SUXIAOYOU_CUSTOM_ENDPOINTS":
        credential_store = store or get_credential_store()
        for reference in _collect_references(persisted_value):
            credential_store.delete(reference)
    elif is_secret_env_key(key):
        credential_store = store or get_credential_store()
        references = _collect_references(persisted_value)
        if references:
            for reference in references:
                credential_store.delete(reference)
        else:
            # Compatibility cleanup for the first deterministic-reference
            # implementation used during v0.9.0 development.
            credential_store.delete(f"env:{key.upper()}")


def migrate_settings_credentials(
    settings: Any,
    env_path: str | Path = ".env",
    *,
    store: CredentialStore | None = None,
    hydrate_references: bool = True,
) -> int:
    """Migrate plaintext values found in ``.env`` and optionally hydrate them.

    Process-environment plaintext remains externally managed; only values that
    actually reside in the app-owned env file are rewritten and erased.

    Desktop startup passes ``hydrate_references=False`` so loading otherwise
    inert configuration cannot trigger an OS credential prompt before the UI
    is ready.  Consumers that actually need a secret resolve the retained
    opaque reference at their operation boundary.  The default remains
    hydrated for explicit migration callers and backwards compatibility.
    """

    credential_store = store or get_credential_store()
    path = Path(env_path)
    previous_exists = path.is_file()
    try:
        previous_content = path.read_bytes() if previous_exists else b""
    except OSError as exc:
        raise CredentialStoreError(
            f"Cannot inspect credential env file {path}: {exc}"
        ) from exc
    persisted = _dotenv_values(path)
    replacements: dict[str, str] = {}
    staged_values: dict[str, StagedEnvValue] = {}
    migrated = 0

    model_fields = getattr(type(settings), "model_fields", {})
    try:
        for field_name in model_fields:
            env_key = f"SUXIAOYOU_{field_name.upper()}"
            if not is_secret_env_key(env_key) and env_key != "SUXIAOYOU_CUSTOM_ENDPOINTS":
                continue
            runtime_value = getattr(settings, field_name, None)
            file_value = persisted.get(env_key)

            if isinstance(file_value, str) and file_value:
                staged = stage_protected_env_value(
                    env_key,
                    file_value,
                    previous_value=file_value,
                    store=credential_store,
                )
                protected = staged.value
                if protected != file_value:
                    replacements[env_key] = protected
                    staged_values[env_key] = staged
                    migrated += 1
                if hydrate_references:
                    resolved = resolve_env_value(env_key, protected, store=credential_store)
                    setattr(settings, field_name, resolved)
                else:
                    setattr(settings, field_name, protected)
            elif isinstance(runtime_value, str) and is_credential_reference(runtime_value):
                if hydrate_references:
                    setattr(settings, field_name, credential_store.resolve(runtime_value))
    except Exception:
        for staged in staged_values.values():
            staged.discard_unreferenced(persisted.values())
        raise

    if replacements:
        cleanup_transaction = None
        try:
            previous_text = previous_content.decode("utf-8")
            next_text = _render_env_values(previous_text, replacements)
            next_values = dict(persisted)
            next_values.update(replacements)
            cleanup_transaction = prepare_stale_secret_cleanup(
                persisted,
                next_values,
                evidence_path=path,
                previous_exists=previous_exists,
                previous_content=previous_content,
                next_exists=True,
                next_content=next_text,
                store=credential_store,
            )
            with _FILE_LOCK:
                current_exists = path.is_file()
                current_content = path.read_bytes() if current_exists else b""
                if (
                    current_exists != previous_exists
                    or current_content != previous_content
                ):
                    raise CredentialStoreError(
                        f"Credential env file changed during migration: {path}"
                    )
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_text(path, next_text, mode=0o600)
        except Exception:
            if cleanup_transaction is not None:
                cleanup_transaction.cancel()
            try:
                installed_values = _dotenv_values(path).values()
            except CredentialStoreError:
                # If the installed file cannot be inspected, retaining the new
                # entries is safer than deleting a possibly-live reference.
                installed_values = replacements.values()
            configured_values = tuple(installed_values)
            for staged in staged_values.values():
                staged.discard_unreferenced(configured_values)
            raise
        if cleanup_transaction is not None:
            cleanup_transaction.commit()
    elif path.is_file():
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return migrated


def resolve_settings_references(
    settings: Any,
    *,
    store: CredentialStore | None = None,
) -> int:
    """Hydrate already-protected values loaded by ``BaseSettings``.

    This deliberately does not migrate process-environment plaintext.  The
    app-owned ``.env`` migration is handled by :func:`migrate_settings_credentials`.
    """

    fields = getattr(type(settings), "model_fields", {})
    resolved_count = 0
    credential_store: CredentialStore | None = store
    for field_name in fields:
        env_key = f"SUXIAOYOU_{field_name.upper()}"
        if not is_secret_env_key(env_key) and env_key != "SUXIAOYOU_CUSTOM_ENDPOINTS":
            continue
        value = getattr(settings, field_name, None)
        needs_resolution = isinstance(value, str) and (
            is_credential_reference(value)
            or (
                env_key == "SUXIAOYOU_CUSTOM_ENDPOINTS"
                and REFERENCE_PREFIX in value
            )
        )
        if not needs_resolution:
            continue
        if credential_store is None:
            credential_store = get_credential_store()
        setattr(
            settings,
            field_name,
            resolve_env_value(env_key, value, store=credential_store),
        )
        resolved_count += 1
    return resolved_count


def _collect_references(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        if is_credential_reference(value):
            return {value}
        try:
            return _collect_references(json.loads(value))
        except (json.JSONDecodeError, TypeError):
            return set()
    if isinstance(value, dict):
        found: set[str] = set()
        for item in value.values():
            found.update(_collect_references(item))
        return found
    if isinstance(value, list):
        found = set()
        for item in value:
            found.update(_collect_references(item))
        return found
    return set()


def prepare_stale_secret_cleanup(
    previous_value: Any,
    new_value: Any,
    *,
    evidence_path: str | Path,
    previous_exists: bool,
    previous_content: str | bytes,
    next_exists: bool,
    next_content: str | bytes,
    extra_references: Iterable[str] = (),
    store: CredentialStore | None = None,
) -> CredentialCleanupTransaction | None:
    """Prepare durable retirement for refs removed by one config replacement."""

    stale = _collect_references(previous_value) - _collect_references(new_value)
    stale.update(extra_references)
    credential_store = store or get_credential_store()
    return credential_store.prepare_cleanup_transaction(
        stale,
        evidence_path=evidence_path,
        previous_exists=previous_exists,
        previous_content=previous_content,
        next_exists=next_exists,
        next_content=next_content,
    )


def delete_stale_secret_references(
    previous_value: Any,
    new_value: Any,
    *,
    store: CredentialStore | None = None,
) -> None:
    """Delete superseded references only after config installation succeeds."""

    stale = _collect_references(previous_value) - _collect_references(new_value)
    if not stale:
        return
    credential_store = store or get_credential_store()
    for reference in stale:
        credential_store.delete(reference)


def _dotenv_values(path: Path) -> dict[str, str | None]:
    if not path.is_file():
        return {}
    try:
        from dotenv import dotenv_values
        from dotenv.parser import parse_stream

        text = path.read_text(encoding="utf-8")
        error_lines = tuple(
            binding.original.line
            for binding in parse_stream(StringIO(text))
            if binding.error
        )
        if error_lines:
            text, recovered = _normalize_v08_dotenv(text)
            remaining_errors = tuple(
                binding.original.line
                for binding in parse_stream(StringIO(text))
                if binding.error
            )
            if not recovered or remaining_errors:
                lines = remaining_errors or error_lines
                joined = ", ".join(str(line) for line in sorted(set(lines)))
                raise CredentialStoreError(
                    f"Cannot parse credential env file {path}: invalid dotenv syntax "
                    f"at line(s) {joined}"
                )

        return dict(dotenv_values(stream=StringIO(text)))
    except CredentialStoreError:
        raise
    except Exception as exc:
        raise CredentialStoreError(f"Cannot parse credential env file {path}: {exc}") from exc


def _normalize_v08_dotenv(text: str) -> tuple[str, bool]:
    """Translate the v0.8 shell apostrophe idiom to valid dotenv in memory.

    The v0.8 writer wrapped values in single quotes, but represented embedded
    apostrophes by closing and reopening the quote as a POSIX shell would.  A
    dotenv parser rejects that form.  Decode only complete one-line assignments
    containing the exact legacy marker, and re-encode them with the current
    writer so original backslashes are not interpreted or lost.
    """

    output: list[str] = []
    recovered = False
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        ending = line[len(content) :]
        if _V08_SHELL_APOSTROPHE not in content:
            output.append(line)
            continue

        match = _ENV_LINE.match(content)
        rhs = content[match.end() :] if match else ""
        if (
            match is None
            or len(rhs) < 2
            or not rhs.startswith("'")
            or not rhs.endswith("'")
        ):
            output.append(line)
            continue

        body = rhs[1:-1]
        # Every interior apostrophe written by v0.8 must belong to one exact
        # close/escape/reopen marker.  Reject lookalikes instead of guessing.
        if "'" in body.replace(_V08_SHELL_APOSTROPHE, ""):
            output.append(line)
            continue

        decoded = body.replace(_V08_SHELL_APOSTROPHE, "'")
        output.append(_env_entry(match.group("key"), decoded) + ending)
        recovered = True

    return "".join(output), recovered


def _render_env_values(previous_text: str, replacements: dict[str, str]) -> str:
    lines = previous_text.splitlines()
    output: list[str] = []
    handled: set[str] = set()
    for line in lines:
        match = _ENV_LINE.match(line)
        if match and match.group("key") in replacements:
            key = match.group("key")
            # python-dotenv uses the last assignment. Replace the first
            # occurrence and remove every duplicate so plaintext cannot
            # remain effective after a successful credential migration.
            if key not in handled:
                output.append(_env_entry(key, replacements[key]))
                handled.add(key)
            continue
        output.append(line)
    for key, value in replacements.items():
        if key not in handled:
            output.append(_env_entry(key, value))
    return "\n".join(output) + "\n"


def _env_entry(key: str, value: str) -> str:
    # This is python-dotenv syntax, not a shell command. Backslash-escape an
    # apostrophe inside a single-quoted value instead of closing/reopening it.
    # Backslashes must be escaped first because python-dotenv decodes them too.
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"{key}='{escaped}'"
