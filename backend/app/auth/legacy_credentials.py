"""Fail-closed migration for credential artifacts written by v0.8.x.

This migration deliberately runs before release feature gates are consulted.
Remote access and messaging channels remain disabled in v1.0, but disabling a
consumer must not strand its old bearer tokens in plaintext indefinitely.

Structured files that still have a supported schema are rewritten in place
with opaque :class:`~app.auth.credential_store.CredentialStore` references.
Obsolete or malformed runtime state is preserved byte-for-byte in the
credential store and replaced with an owner-only recovery tombstone.  Nothing
is silently discarded, and any failure to install the protected replacement
aborts startup so the plaintext file is never mistaken for a completed
migration.
"""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import logging
import os
import secrets
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from app.auth.credential_store import (
    CredentialStore,
    CredentialStoreError,
    StagedSecretTree,
    credential_tree_id,
    is_credential_reference,
    prepare_stale_secret_cleanup,
    stage_protected_secret_tree,
)
from app.utils.atomic_write import atomic_write_text

logger = logging.getLogger(__name__)

ARCHIVE_FORMAT = "suxiaoyou-legacy-credential-archive-v1"
_ARCHIVE_PAYLOAD_PREFIX = "base64:"


class LegacyCredentialMigrationError(RuntimeError):
    """A legacy credential artifact could not be secured safely."""


@dataclass
class LegacyCredentialMigrationReport:
    """Auditable summary of one idempotent startup migration pass."""

    protected: list[str] = field(default_factory=list)
    archived: list[str] = field(default_factory=list)
    imported: list[str] = field(default_factory=list)
    hardened: list[str] = field(default_factory=list)

    @property
    def changed_count(self) -> int:
        return len(self.protected) + len(self.archived) + len(self.imported)


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path.expanduser().resolve(strict=False)))


def _deduplicate_paths(paths: Iterable[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        expanded = path.expanduser()
        key = _path_key(expanded)
        if key not in seen:
            result.append(expanded)
            seen.add(key)
    return result


def _configured_path(value: str | Path, data_root: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else data_root / path


def _read_regular_bytes(path: Path) -> bytes | None:
    """Read one app-owned artifact without following aliases or hard links."""

    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise LegacyCredentialMigrationError(
            f"Cannot inspect legacy credential artifact {path}: {exc}"
        ) from exc

    if stat.S_ISLNK(before.st_mode):
        raise LegacyCredentialMigrationError(
            f"Refusing to follow legacy credential symlink: {path}"
        )
    if not stat.S_ISREG(before.st_mode):
        raise LegacyCredentialMigrationError(
            f"Legacy credential artifact is not a regular file: {path}"
        )
    if before.st_nlink != 1:
        raise LegacyCredentialMigrationError(
            f"Refusing to rewrite hard-linked legacy credential artifact: {path}"
        )

    try:
        payload = path.read_bytes()
        after = path.lstat()
    except OSError as exc:
        raise LegacyCredentialMigrationError(
            f"Cannot read legacy credential artifact {path}: {exc}"
        ) from exc

    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after or len(payload) != after.st_size:
        raise LegacyCredentialMigrationError(
            f"Legacy credential artifact changed while it was being migrated: {path}"
        )
    return payload


def _harden_owner_only(path: Path) -> None:
    try:
        path.chmod(0o600)
        if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise OSError("owner-only mode was not applied")
    except OSError as exc:
        raise LegacyCredentialMigrationError(
            f"Cannot apply owner-only permissions to {path}: {exc}"
        ) from exc


def _parse_json_object(payload: bytes) -> dict[str, Any] | None:
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _is_archive_tombstone(data: object) -> bool:
    return isinstance(data, dict) and data.get("format") == ARCHIVE_FORMAT


def _archive_identifier(path: Path, artifact: str, digest: str) -> str:
    path_digest = hashlib.sha256(_path_key(path).encode("utf-8")).hexdigest()[:16]
    artifact_slug = "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in artifact
    )[:40]
    return (
        f"legacy-archive:{artifact_slug}:{path_digest}:{digest[:20]}:"
        f"{secrets.token_hex(8)}"
    )


def _decode_archive_tombstone(
    data: dict[str, Any],
    *,
    store: CredentialStore,
    path: Path,
) -> bytes:
    reference, digest, size = _validate_archive_tombstone(data, path=path)
    try:
        encoded = store.resolve(reference)
    except CredentialStoreError as exc:
        raise LegacyCredentialMigrationError(
            f"Legacy credential archive cannot be resolved: {path}: {exc}"
        ) from exc
    if not encoded.startswith(_ARCHIVE_PAYLOAD_PREFIX):
        raise LegacyCredentialMigrationError(
            f"Legacy credential archive has an unsupported encoding: {path}"
        )
    try:
        payload = base64.b64decode(
            encoded[len(_ARCHIVE_PAYLOAD_PREFIX) :],
            validate=True,
        )
    except (binascii.Error, ValueError, TypeError) as exc:
        raise LegacyCredentialMigrationError(
            f"Legacy credential archive payload is corrupt: {path}"
        ) from exc
    if len(payload) != size or hashlib.sha256(payload).hexdigest() != digest:
        raise LegacyCredentialMigrationError(
            f"Legacy credential archive integrity check failed: {path}"
        )
    return payload


def _validate_archive_tombstone(
    data: dict[str, Any],
    *,
    path: Path,
) -> tuple[str, str, int]:
    """Validate protected archive metadata without opening the credential vault."""

    reference = data.get("content_ref")
    digest = data.get("sha256")
    size = data.get("size")
    if (
        not is_credential_reference(reference)
        or not isinstance(digest, str)
        or len(digest) != 64
        or not isinstance(size, int)
        or size < 0
    ):
        raise LegacyCredentialMigrationError(
            f"Legacy credential recovery tombstone is invalid: {path}"
        )
    return reference, digest, size


def recover_archived_legacy_artifact(
    path: str | Path,
    *,
    store: CredentialStore,
) -> bytes:
    """Return the exact bytes retained by a recovery tombstone.

    Recovery is explicit: this helper never writes the legacy path or enables
    the retired feature.  A future reviewed importer can inspect the bytes and
    decide whether restoring that feature's state is appropriate.
    """

    target = Path(path)
    payload = _read_regular_bytes(target)
    if payload is None:
        raise LegacyCredentialMigrationError(
            f"Legacy credential recovery tombstone does not exist: {target}"
        )
    data = _parse_json_object(payload)
    if not _is_archive_tombstone(data):
        raise LegacyCredentialMigrationError(
            f"File is not a legacy credential recovery tombstone: {target}"
        )
    return _decode_archive_tombstone(data, store=store, path=target)


def _archive_bytes(
    path: Path,
    payload: bytes,
    *,
    artifact: str,
    reason: str,
    store: CredentialStore,
) -> bool:
    """Replace obsolete state with a reference-backed recovery tombstone."""

    parsed = _parse_json_object(payload)
    if _is_archive_tombstone(parsed):
        _validate_archive_tombstone(parsed, path=path)
        _harden_owner_only(path)
        return False

    digest = hashlib.sha256(payload).hexdigest()
    encoded = _ARCHIVE_PAYLOAD_PREFIX + base64.b64encode(payload).decode("ascii")
    reference = store.put(_archive_identifier(path, artifact, digest), encoded)
    tombstone = {
        "format": ARCHIVE_FORMAT,
        "artifact": artifact,
        # Local feature disablement and protected storage do not prove that an
        # external provider token was revoked at its issuer.
        "status": "disabled-and-archived",
        "reason": reason,
        "encoding": "credential-store/base64",
        "content_ref": reference,
        "sha256": digest,
        "size": len(payload),
    }
    # Do not retire references that happened to be present in ``payload``.
    # They remain logically live inside the byte-for-byte recovery archive;
    # deleting them would make a later explicit restore incomplete. The new
    # archive reference itself owns the raw bytes and is installed atomically.
    try:
        atomic_write_text(
            path,
            json.dumps(tombstone, indent=2, sort_keys=True) + "\n",
            mode=0o600,
        )
    except Exception:
        # The old file is still installed, so this fresh archive reference is
        # not live and must not be orphaned.
        store.delete(reference)
        raise
    _harden_owner_only(path)
    return True


def _discard_failed_tree_stage(path: Path, staged: Any, previous: Any) -> None:
    """Delete only newly-created refs that the installed file does not use."""

    installed: Any = previous
    payload = _read_regular_bytes(path)
    if payload is not None:
        parsed = _parse_json_object(payload)
        if parsed is not None:
            installed = parsed
    staged.discard_unreferenced((installed,))


def _collect_references(value: Any) -> set[str]:
    if is_credential_reference(value):
        return {value}
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


@dataclass(frozen=True)
class _CompositeSecretTreeStage:
    value: Any
    created_references: frozenset[str]
    core_stage: StagedSecretTree
    store: CredentialStore

    def discard_unreferenced(self, configured_values: Iterable[Any] = ()) -> None:
        configured = tuple(configured_values)
        self.core_stage.discard_unreferenced(configured)
        installed: set[str] = set()
        for value in configured:
            installed.update(_collect_references(value))
        for reference in self.created_references - installed:
            self.store.delete(reference)

def _stage_legacy_secret_tree(
    namespace: str,
    data: Any,
    *,
    previous_value: Any,
    store: CredentialStore,
    protect_token_suffixes: bool,
) -> StagedSecretTree | _CompositeSecretTreeStage:
    """Protect legacy channel ``*_token`` fields omitted by generic policy."""

    if not protect_token_suffixes:
        return stage_protected_secret_tree(
            namespace,
            data,
            previous_value=previous_value,
            store=store,
        )

    manually_created: set[str] = set()

    def visit(value: Any, path: tuple[str, ...]) -> Any:
        if isinstance(value, dict):
            protected: dict[Any, Any] = {}
            for key, item in value.items():
                key_text = str(key)
                normalized = key_text.strip().casefold().replace("-", "_")
                if (
                    isinstance(item, str)
                    and item
                    and normalized.endswith("_token")
                    and not is_credential_reference(item)
                ):
                    reference = store.put(
                        credential_tree_id(namespace, (*path, key_text)),
                        item,
                    )
                    manually_created.add(reference)
                    protected[key] = reference
                else:
                    protected[key] = visit(item, (*path, key_text))
            return protected
        if isinstance(value, list):
            return [visit(item, (*path, str(index))) for index, item in enumerate(value)]
        return copy.deepcopy(value)

    try:
        preprotected = visit(data, ())
        core_stage = stage_protected_secret_tree(
            namespace,
            preprotected,
            previous_value=previous_value,
            store=store,
        )
    except Exception:
        for reference in manually_created:
            store.delete(reference)
        raise
    return _CompositeSecretTreeStage(
        value=core_stage.value,
        created_references=frozenset(manually_created),
        core_stage=core_stage,
        store=store,
    )


def _migrate_json_secret_tree(
    path: Path,
    *,
    artifact: str,
    namespace: str,
    store: CredentialStore,
    report: LegacyCredentialMigrationReport,
    protect_token_suffixes: bool = False,
) -> None:
    payload = _read_regular_bytes(path)
    if payload is None:
        return
    data = _parse_json_object(payload)
    if _is_archive_tombstone(data):
        _validate_archive_tombstone(data, path=path)
        _harden_owner_only(path)
        report.hardened.append(str(path))
        return
    if data is None:
        if _archive_bytes(
            path,
            payload,
            artifact=artifact,
            reason="unsupported or unreadable legacy JSON structure",
            store=store,
        ):
            report.archived.append(str(path))
        return

    staged = _stage_legacy_secret_tree(
        namespace,
        data,
        previous_value=data,
        store=store,
        protect_token_suffixes=protect_token_suffixes,
    )
    if staged.value == data:
        _harden_owner_only(path)
        report.hardened.append(str(path))
        return
    next_text = json.dumps(staged.value, indent=2, ensure_ascii=False) + "\n"
    try:
        cleanup_transaction = prepare_stale_secret_cleanup(
            data,
            staged.value,
            evidence_path=path,
            previous_exists=True,
            previous_content=payload,
            next_exists=True,
            next_content=next_text,
            store=store,
        )
    except Exception:
        staged.discard_unreferenced((data,))
        raise
    try:
        atomic_write_text(
            path,
            next_text,
            mode=0o600,
        )
    except Exception:
        if cleanup_transaction is not None:
            cleanup_transaction.cancel()
        _discard_failed_tree_stage(path, staged, data)
        raise
    # Permission hardening happens after installation but before cleanup is
    # committed. If it fails, the prepared evidence remains recoverable and
    # the newly-installed references remain live for the next startup retry.
    _harden_owner_only(path)
    if cleanup_transaction is not None:
        cleanup_transaction.commit()
    report.protected.append(str(path))


def _mcp_scope(project_dir: Path | None) -> str:
    return str(project_dir.resolve(strict=False)) if project_dir is not None else "global"


def _mcp_destination(data_root: Path, project_dir: Path | None) -> tuple[Path, str]:
    scope_hash = hashlib.sha256(_mcp_scope(project_dir).encode("utf-8")).hexdigest()[:20]
    return data_root / "data" / "credentials" / "mcp" / f"{scope_hash}.json", scope_hash


def _valid_mcp_data(data: object) -> bool:
    return isinstance(data, dict) and all(
        isinstance(key, str) and isinstance(value, dict)
        for key, value in data.items()
    )


def _expiry(entry: dict[str, Any]) -> float:
    try:
        return float(entry.get("expires_at", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _normalize_mcp_keys(data: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    for old_key, entry in data.items():
        new_key = old_key.split(":", 1)[1] if ":" in old_key else old_key
        existing = normalized.get(new_key)
        if existing is None or _expiry(entry) > _expiry(existing):
            normalized[new_key] = copy.deepcopy(entry)
    return normalized


def _migrate_mcp_legacy_file(
    legacy_path: Path,
    *,
    data_root: Path,
    project_dir: Path | None,
    store: CredentialStore,
    report: LegacyCredentialMigrationReport,
) -> None:
    destination, scope_hash = _mcp_destination(data_root, project_dir)

    # Protect an already-created v0.9 destination even when there is no v0.8
    # source. This keeps startup independent of whether an MCP is enabled.
    if destination.exists():
        destination_payload = _read_regular_bytes(destination)
        destination_data = (
            _parse_json_object(destination_payload)
            if destination_payload is not None
            else None
        )
        if not _valid_mcp_data(destination_data):
            raise LegacyCredentialMigrationError(
                f"Current MCP credential metadata is unreadable: {destination}"
            )
        _migrate_json_secret_tree(
            destination,
            artifact="mcp-current-metadata",
            namespace=f"mcp:{scope_hash}",
            store=store,
            report=report,
        )

    legacy_payload = _read_regular_bytes(legacy_path)
    if legacy_payload is None:
        return
    legacy_data = _parse_json_object(legacy_payload)
    if _is_archive_tombstone(legacy_data):
        _validate_archive_tombstone(legacy_data, path=legacy_path)
        _harden_owner_only(legacy_path)
        report.hardened.append(str(legacy_path))
        return
    if not _valid_mcp_data(legacy_data):
        if _archive_bytes(
            legacy_path,
            legacy_payload,
            artifact="mcp-legacy-oauth",
            reason="unsupported or unreadable legacy MCP token structure",
            store=store,
        ):
            report.archived.append(str(legacy_path))
        return

    destination_payload = _read_regular_bytes(destination)
    if destination_payload is None:
        previous: dict[str, dict[str, Any]] = {}
    else:
        destination_data = _parse_json_object(destination_payload)
        if not _valid_mcp_data(destination_data):
            raise LegacyCredentialMigrationError(
                f"Refusing to overwrite unreadable MCP credential metadata: {destination}"
            )
        previous = destination_data

    merged: dict[str, dict[str, Any]] = copy.deepcopy(previous)
    for key, entry in legacy_data.items():
        existing = merged.get(key)
        if existing is None or _expiry(entry) > _expiry(existing):
            merged[key] = copy.deepcopy(entry)
    merged = _normalize_mcp_keys(merged)

    staged = stage_protected_secret_tree(
        f"mcp:{scope_hash}",
        merged,
        previous_value=previous,
        store=store,
    )
    next_text = json.dumps(staged.value, indent=2, ensure_ascii=False) + "\n"
    try:
        cleanup_transaction = prepare_stale_secret_cleanup(
            previous,
            staged.value,
            evidence_path=destination,
            previous_exists=destination_payload is not None,
            previous_content=destination_payload or b"",
            next_exists=True,
            next_content=next_text,
            store=store,
        )
    except Exception:
        staged.discard_unreferenced((previous,))
        raise
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            destination,
            next_text,
            mode=0o600,
        )
    except Exception:
        if cleanup_transaction is not None:
            cleanup_transaction.cancel()
        _discard_failed_tree_stage(destination, staged, previous)
        raise
    _harden_owner_only(destination)
    if cleanup_transaction is not None:
        cleanup_transaction.commit()

    try:
        legacy_path.unlink()
    except OSError:
        # The import is durable, but leaving a second plaintext copy is not
        # acceptable. Preserve it behind an opaque, recoverable tombstone.
        if _archive_bytes(
            legacy_path,
            legacy_payload,
            artifact="mcp-legacy-oauth",
            reason="legacy MCP credentials imported; source could not be removed",
            store=store,
        ):
            report.archived.append(str(legacy_path))
    report.imported.append(f"{legacy_path} -> {destination}")
    logger.info("Imported legacy MCP credentials from %s to %s", legacy_path, destination)


def _discover_custom_weixin_state_dirs(
    channel_paths: Iterable[Path],
    *,
    data_root: Path,
) -> list[Path]:
    discovered: list[Path] = []
    for path in channel_paths:
        payload = _read_regular_bytes(path)
        if payload is None:
            continue
        data = _parse_json_object(payload)
        channels = data.get("channels") if isinstance(data, dict) else None
        weixin = channels.get("weixin") if isinstance(channels, dict) else None
        state_dir = weixin.get("state_dir") if isinstance(weixin, dict) else None
        if isinstance(state_dir, str) and state_dir.strip():
            configured = Path(state_dir).expanduser()
            discovered.append(
                configured if configured.is_absolute() else data_root / configured
            )
    return _deduplicate_paths(discovered)


def _validate_known_credential_directory(path: Path) -> None:
    """Reject a redirected ``.suxiaoyou`` directory before inspecting it."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise LegacyCredentialMigrationError(
            f"Cannot inspect legacy credential directory {path}: {exc}"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise LegacyCredentialMigrationError(
            f"Refusing to follow legacy credential directory symlink: {path}"
        )
    if not stat.S_ISDIR(metadata.st_mode):
        raise LegacyCredentialMigrationError(
            f"Legacy credential location is not a directory: {path}"
        )


def _merge_reports(
    target: LegacyCredentialMigrationReport,
    source: LegacyCredentialMigrationReport,
) -> None:
    target.protected.extend(source.protected)
    target.archived.extend(source.archived)
    target.imported.extend(source.imported)
    target.hardened.extend(source.hardened)


def migrate_legacy_credential_artifacts(
    *,
    data_root: str | Path | None = None,
    project_dir: str | Path | None = None,
    home_dir: str | Path | None = None,
    include_global_legacy: bool | None = None,
    include_app_data: bool = True,
    channels_config_path: str | Path | None = None,
    remote_token_path: str | Path | None = None,
    store: CredentialStore | None = None,
) -> LegacyCredentialMigrationReport:
    """Secure all known v0.8 credential artifacts before feature startup.

    ``home_dir`` is injectable for release-drill fixtures. Production scans
    the old global location only when there is no selected project, avoiding
    unrelated home-directory mutation during project-scoped test/dev runs.
    """

    root = Path(data_root or Path.cwd()).expanduser().resolve(strict=False)
    selected_project = (
        Path(project_dir).expanduser().resolve(strict=False)
        if project_dir not in (None, "")
        else None
    )
    home = Path(home_dir).expanduser() if home_dir is not None else Path.home()
    scan_global = (
        selected_project is None
        if include_global_legacy is None
        else include_global_legacy
    )
    credential_store = store
    if credential_store is None:
        # Construct this explicitly rather than relying on the cwd-keyed cache;
        # callers such as migration drills may pass a root other than cwd.
        credential_store = CredentialStore(
            fallback_path=root / "data" / "credentials" / "fallback.json"
        )

    channel_paths: list[Path] = []
    if include_app_data:
        channel_paths = [root / "data" / "channels.json", root / "channels.json"]
        if channels_config_path:
            channel_paths.append(_configured_path(channels_config_path, root))
        channel_paths = _deduplicate_paths(channel_paths)

    try:
        custom_weixin_dirs = _discover_custom_weixin_state_dirs(
            channel_paths,
            data_root=root,
        )
        report = LegacyCredentialMigrationReport()

        for path in channel_paths:
            _migrate_json_secret_tree(
                path,
                artifact="channels-config",
                namespace="channels",
                store=credential_store,
                report=report,
                protect_token_suffixes=True,
            )

        google_paths: list[Path] = []
        if selected_project is not None:
            _validate_known_credential_directory(selected_project / ".suxiaoyou")
            google_paths.append(selected_project / ".suxiaoyou" / "google-tokens.json")
        if scan_global:
            _validate_known_credential_directory(home / ".suxiaoyou")
            google_paths.append(home / ".suxiaoyou" / "google-tokens.json")
        for path in _deduplicate_paths(google_paths):
            scope = hashlib.sha256(
                str(path.parent.parent.resolve(strict=False)).encode("utf-8")
            ).hexdigest()[:20]
            _migrate_json_secret_tree(
                path,
                artifact="google-oauth",
                namespace=f"google:{scope}",
                store=credential_store,
                report=report,
            )

        mcp_scopes: list[tuple[Path, Path | None]] = []
        if selected_project is not None:
            mcp_scopes.append(
                (selected_project / ".suxiaoyou" / "mcp-tokens.json", selected_project)
            )
        if scan_global:
            mcp_scopes.append((home / ".suxiaoyou" / "mcp-tokens.json", None))
        seen_mcp_paths: set[str] = set()
        for legacy_path, scope_project in mcp_scopes:
            key = _path_key(legacy_path)
            if key in seen_mcp_paths:
                continue
            seen_mcp_paths.add(key)
            _migrate_mcp_legacy_file(
                legacy_path,
                data_root=root,
                project_dir=scope_project,
                store=credential_store,
                report=report,
            )

        archived_runtime_paths: list[tuple[Path, str]] = []
        if include_app_data:
            remote_path = _configured_path(
                remote_token_path or "data/remote_token.json",
                root,
            )
            _migrate_json_secret_tree(
                remote_path,
                artifact="remote-access-token",
                namespace="remote-access",
                store=credential_store,
                report=report,
            )

            archived_runtime_paths = [
                (
                    root / "data" / "runtime" / "weixin" / "account.json",
                    "weixin-account",
                ),
                (root / "data" / "matrix-store" / "session.json", "matrix-session"),
                (
                    root / "data" / "runtime" / "whatsapp-auth" / "bridge-token",
                    "whatsapp-bridge-token",
                ),
            ]
            archived_runtime_paths.extend(
                (state_dir / "account.json", "weixin-account")
                for state_dir in custom_weixin_dirs
            )
        seen_runtime: set[str] = set()
        for path, artifact in archived_runtime_paths:
            key = _path_key(path)
            if key in seen_runtime:
                continue
            seen_runtime.add(key)
            payload = _read_regular_bytes(path)
            if payload is None:
                continue
            if _archive_bytes(
                path,
                payload,
                artifact=artifact,
                reason="credential-bearing channel runtime is disabled in this release",
                store=credential_store,
            ):
                report.archived.append(str(path))
            else:
                report.hardened.append(str(path))

        if report.changed_count:
            logger.info(
                "Legacy credential migration secured %d artifact(s)",
                report.changed_count,
            )
        return report
    except LegacyCredentialMigrationError:
        raise
    except Exception as exc:
        raise LegacyCredentialMigrationError(
            f"Legacy credential migration failed closed: {exc}"
        ) from exc


def migrate_workspace_legacy_credential_artifacts(
    workspaces: Iterable[str | Path],
    *,
    data_root: str | Path | None = None,
    store: CredentialStore | None = None,
) -> LegacyCredentialMigrationReport:
    """Migrate only known ``<workspace>/.suxiaoyou`` credential artifacts."""

    root = Path(data_root or Path.cwd()).expanduser().resolve(strict=False)
    credential_store = store or CredentialStore(
        fallback_path=root / "data" / "credentials" / "fallback.json"
    )
    combined = LegacyCredentialMigrationReport()
    seen: set[str] = set()
    for raw_workspace in workspaces:
        workspace = Path(raw_workspace).expanduser().resolve(strict=False)
        key = _path_key(workspace)
        if key in seen:
            continue
        seen.add(key)
        _validate_known_credential_directory(workspace / ".suxiaoyou")
        report = migrate_legacy_credential_artifacts(
            data_root=root,
            project_dir=workspace,
            include_global_legacy=False,
            include_app_data=False,
            store=credential_store,
        )
        _merge_reports(combined, report)
    return combined


async def collect_legacy_credential_workspaces(
    session_factory: Any,
    *,
    configured_project_dir: str | Path | None,
    private_data_root: str | Path,
) -> list[Path]:
    """Collect safe historical workspaces without inspecting their contents."""

    from sqlalchemy import select

    from app.models.session import Session
    from app.tool.workspace import (
        WorkspaceBoundaryViolation,
        validate_agent_workspace_root,
    )

    raw_candidates: list[str | Path] = []
    configured = str(configured_project_dir or "").strip()
    if configured and configured != ".":
        raw_candidates.append(configured)
    async with session_factory() as database:
        rows = await database.execute(select(Session.directory).distinct())
        raw_candidates.extend(
            value
            for value in rows.scalars().all()
            if isinstance(value, str) and value.strip() and value.strip() != "."
        )

    result: list[Path] = []
    seen: set[str] = set()
    for raw in raw_candidates:
        try:
            workspace = validate_agent_workspace_root(
                raw,
                private_root=private_data_root,
            )
        except (OSError, WorkspaceBoundaryViolation) as exc:
            logger.warning(
                "Skipping unsafe historical credential workspace %r: %s",
                raw,
                exc,
            )
            continue
        key = _path_key(workspace)
        if key not in seen:
            result.append(workspace)
            seen.add(key)
    return result


async def migrate_historical_workspace_credentials(
    session_factory: Any,
    *,
    configured_project_dir: str | Path | None,
    private_data_root: str | Path,
    store: CredentialStore,
) -> LegacyCredentialMigrationReport:
    """Query DB history, then migrate only known credentials in each workspace."""

    workspaces = await collect_legacy_credential_workspaces(
        session_factory,
        configured_project_dir=configured_project_dir,
        private_data_root=private_data_root,
    )
    return migrate_workspace_legacy_credential_artifacts(
        workspaces,
        data_root=private_data_root,
        store=store,
    )
