"""OS-native credential storage with an explicit private-file fallback.

Production builds use ``keyring`` to select macOS Keychain, Windows Credential
Manager, or Linux Secret Service.  Some Linux/headless environments have no
native service; only then do we use one atomically-written 0600 JSON fallback.
Configuration files store opaque references and never the credential itself.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import secrets
import sys
import threading
from dataclasses import dataclass, field
from functools import lru_cache
from io import StringIO
from pathlib import Path
from typing import Any, Iterable, Protocol
from urllib.parse import quote, unquote

from app.utils.atomic_write import atomic_write_text

logger = logging.getLogger(__name__)

SERVICE_NAME = "com.suxiaoyou.credentials"
REFERENCE_PREFIX = "suxiaoyou-credential://"
FALLBACK_VERSION = 1

_FILE_LOCK = threading.RLock()
_ENV_LINE = re.compile(
    r"^(?P<prefix>[ \t]*(?:export[ \t]+)?)(?P<key>[A-Za-z_][A-Za-z0-9_]*)[ \t]*="
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

    def discard_unreferenced(self, configured_values: Iterable[Any] = ()) -> None:
        """Remove newly-created entries that no installed config references."""

        if not self.created_references:
            return
        installed: set[str] = set()
        for configured_value in configured_values:
            installed.update(_collect_references(configured_value))
        for reference in self.created_references - installed:
            self._store.delete(reference)

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


def _discover_native_backend() -> NativeCredentialBackend | None:
    try:
        # Select only the operating system's native secret service. Avoid the
        # generic keyring chainer here: third-party plaintext keyring backends
        # must never become an implicit production credential store.
        if sys.platform == "darwin":
            from keyring.backends.macOS import Keyring  # type: ignore[import-not-found]

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
        self.native_backend = (
            _discover_native_backend() if native_backend is ... else native_backend
        )
        (
            self._fallback,
            self._pending_native_deletions,
            self._cleanup_transactions,
        ) = self._load_fallback_state()
        self._reconcile_cleanup_transactions()
        self._retry_pending_native_deletions()

    @property
    def uses_native_backend(self) -> bool:
        return self.native_backend is not None

    def put(self, identifier: str, secret: str) -> str:
        self._reconcile_cleanup_transactions()
        self._retry_pending_native_deletions()
        identifier = _identifier(identifier)
        if is_credential_reference(secret):
            return secret
        if not isinstance(secret, str) or not secret:
            raise CredentialStoreError("Refusing to persist an empty credential")

        if self.native_backend is not None:
            try:
                self.native_backend.set_password(SERVICE_NAME, identifier, secret)  # type: ignore[union-attr]
            except Exception as exc:
                logger.warning(
                    "Native credential write failed for %s; using private fallback: %s",
                    identifier,
                    exc,
                )
                # Native services may report failure after committing the
                # write. Compensate inside put(), before callers could lose the
                # identifier needed to track that partial write.
                if not self._try_native_delete(identifier):
                    try:
                        self._queue_native_deletion(identifier)
                    except Exception as cleanup_exc:
                        cleanup_exc.add_note(
                            f"native write also failed for {identifier}: {exc}"
                        )
                        raise
            else:
                try:
                    self._delete_fallback(identifier)
                except Exception:
                    # Do not expose a reference after native success if the
                    # old fallback/journal state could not be retired. Undo or
                    # durably queue the native write before propagating.
                    if not self._try_native_delete(identifier):
                        self._queue_native_deletion(identifier)
                    raise
                return _reference(identifier)

        with _FILE_LOCK:
            # Multiple CredentialStore instances can share one fallback file
            # (env + MCP + channel stores). Merge the latest on-disk state
            # before every mutation so one namespace cannot erase another.
            self._reload_fallback_locked()
            self._fallback[identifier] = secret
            self._persist_fallback_locked()
        return _reference(identifier)

    def get(self, reference_or_identifier: str) -> str | None:
        self._reconcile_cleanup_transactions()
        self._retry_pending_native_deletions()
        identifier = _identifier(reference_or_identifier)
        if self.native_backend is not None:
            try:
                value = self.native_backend.get_password(SERVICE_NAME, identifier)  # type: ignore[union-attr]
            except Exception as exc:
                logger.warning("Native credential read failed for %s: %s", identifier, exc)
            else:
                if value is not None:
                    return value
        with _FILE_LOCK:
            self._reload_fallback_locked()
            return self._fallback.get(identifier)

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
        self._reconcile_cleanup_transactions()
        self._retry_pending_native_deletions()
        identifier = _identifier(reference_or_identifier)
        native_deleted = True
        if self.native_backend is not None:
            native_deleted = self._try_native_delete(identifier)
        with _FILE_LOCK:
            self._reload_fallback_locked()
            changed = self._fallback.pop(identifier, None) is not None
            if self.native_backend is not None:
                if native_deleted:
                    if identifier in self._pending_native_deletions:
                        self._pending_native_deletions.discard(identifier)
                        changed = True
                elif identifier not in self._pending_native_deletions:
                    self._pending_native_deletions.add(identifier)
                    changed = True
            if changed:
                # A failed native deletion is considered safely handed off only
                # after this owner-only journal write succeeds.
                self._persist_fallback_locked()

    def _load_fallback_state(
        self,
    ) -> tuple[dict[str, str], set[str], dict[str, dict[str, Any]]]:
        if not self.fallback_path.is_file():
            return {}, set(), {}
        try:
            payload = json.loads(self.fallback_path.read_text(encoding="utf-8"))
            credentials = payload.get("credentials") if isinstance(payload, dict) else None
            pending = (
                payload.get("pending_native_deletions", [])
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
                    "cleanup_transactions": self._cleanup_transactions,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            mode=0o600,
        )

    def _delete_fallback(self, identifier: str) -> None:
        with _FILE_LOCK:
            self._reload_fallback_locked()
            changed = self._fallback.pop(identifier, None) is not None
            if identifier in self._pending_native_deletions:
                self._pending_native_deletions.discard(identifier)
                changed = True
            if changed:
                self._persist_fallback_locked()

    def _reload_fallback_locked(self) -> None:
        (
            self._fallback,
            self._pending_native_deletions,
            self._cleanup_transactions,
        ) = self._load_fallback_state()

    def _try_native_delete(self, identifier: str) -> bool:
        if self.native_backend is None:
            return False
        try:
            self.native_backend.delete_password(SERVICE_NAME, identifier)  # type: ignore[union-attr]
            return True
        except Exception as exc:
            # Some native backends signal a missing item as a deletion error.
            # A follow-up read distinguishes that harmless state from a locked
            # or otherwise unavailable credential service.
            try:
                still_present = self.native_backend.get_password(  # type: ignore[union-attr]
                    SERVICE_NAME,
                    identifier,
                )
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

    def _queue_native_deletion(self, identifier: str) -> None:
        """Durably hand a possibly-partial native write to the retry journal."""

        with _FILE_LOCK:
            self._reload_fallback_locked()
            if identifier in self._pending_native_deletions:
                return
            self._pending_native_deletions.add(identifier)
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
        native_candidates = identifiers - self._fallback.keys()
        for identifier in identifiers:
            self._fallback.pop(identifier, None)
        # Native-backend discovery is allowed to be temporarily unavailable
        # (notably when Linux Secret Service/DBus is offline).  A reference may
        # still have been written to the OS vault by an earlier process, so the
        # cleanup responsibility must survive even when *this* process cannot
        # currently attempt it.  Entries present in ``_fallback`` are known to
        # be file-backed; missing entries may live in the OS vault and therefore
        # remain in the durable retry journal until a native backend returns.
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

    def _reconcile_cleanup_transactions(self) -> None:
        """Recover prepared cleanup records from exact on-disk evidence."""

        should_retry_native = False
        with _FILE_LOCK:
            self._reload_fallback_locked()
            changed = False
            for transaction_id, record in list(self._cleanup_transactions.items()):
                current = _path_evidence(Path(record["evidence_path"]))
                if current == record["next_evidence"]:
                    self._activate_cleanup_locked(transaction_id)
                    should_retry_native = should_retry_native or self.native_backend is not None
                    changed = True
                elif current == record["previous_evidence"]:
                    del self._cleanup_transactions[transaction_id]
                    changed = True
            if changed:
                self._persist_fallback_locked()
        if should_retry_native:
            self._retry_pending_native_deletions()

    def _retry_pending_native_deletions(self) -> None:
        if self.native_backend is None:
            return
        with _FILE_LOCK:
            self._reload_fallback_locked()
            if not self._pending_native_deletions:
                return
            remaining = {
                identifier
                for identifier in self._pending_native_deletions
                if not self._try_native_delete(identifier)
            }
            if remaining != self._pending_native_deletions:
                self._pending_native_deletions = remaining
                self._persist_fallback_locked()


@lru_cache(maxsize=1)
def get_credential_store() -> CredentialStore:
    return CredentialStore()


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
            credential_store.delete(reference)
        raise
    return StagedEnvValue(
        value=protected,
        created_references=frozenset(created),
        _store=credential_store,
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
) -> int:
    """Hydrate references and migrate plaintext values found in ``.env``.

    Process-environment plaintext remains externally managed; only values that
    actually reside in the app-owned env file are rewritten and erased.
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
                resolved = resolve_env_value(env_key, protected, store=credential_store)
                setattr(settings, field_name, resolved)
            elif isinstance(runtime_value, str) and is_credential_reference(runtime_value):
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
