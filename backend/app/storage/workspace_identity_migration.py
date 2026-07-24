"""Crash-resumable migration from volatile stat-v1 workspace identities.

External recovery state is prepared first and the database token is updated
last.  A crash at any earlier point leaves the legacy row authoritative and a
subsequent startup reuses the same durable marker and migrated history tree.
"""

from __future__ import annotations

import logging
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.checkpoint_change import CheckpointChange
from app.models.session_checkpoint import SessionCheckpoint
from app.models.workspace_instance import WorkspaceInstance
from app.storage.file_versions import FileVersionError, FileVersionStore
from app.storage.workspace_identity import (
    WorkspaceIdentityError,
    ensure_workspace_identity,
    inspect_workspace_identity,
    parse_legacy_stat_token,
)
from app.utils.windows_guarded_file import windows_path_identity

logger = logging.getLogger(__name__)

_PROVENANCE_KEY = "workspace_identity_v2"
_PROVENANCE_SCHEMA_VERSION = 1


def _provenance_payload(
    *,
    legacy_token: str,
    durable_token: str,
    retained_legacy_source_present: bool,
) -> dict[str, object]:
    return {
        "schema_version": _PROVENANCE_SCHEMA_VERSION,
        "legacy_identity_token": legacy_token,
        "durable_identity_token": durable_token,
        "retained_legacy_source_present": retained_legacy_source_present,
        "time_migrated": datetime.now(timezone.utc).isoformat(),
    }


def _provenance_legacy_identity(
    instance: WorkspaceInstance,
) -> tuple[tuple[int, int], bool]:
    details = instance.details
    raw = details.get(_PROVENANCE_KEY) if isinstance(details, dict) else None
    if (
        not isinstance(raw, dict)
        or type(raw.get("schema_version")) is not int
        or raw["schema_version"] != _PROVENANCE_SCHEMA_VERSION
        or raw.get("durable_identity_token") != instance.identity_token
        or type(raw.get("retained_legacy_source_present")) is not bool
    ):
        raise WorkspaceIdentityError(
            "workspace identity migration provenance is invalid"
        )
    legacy_token = raw.get("legacy_identity_token")
    if not isinstance(legacy_token, str):
        raise WorkspaceIdentityError(
            "workspace identity migration provenance has no legacy token"
        )
    legacy_identity = parse_legacy_stat_token(legacy_token)
    if legacy_identity is None:
        raise WorkspaceIdentityError(
            "workspace identity migration provenance has an invalid stat token"
        )
    return legacy_identity, raw["retained_legacy_source_present"]


def _with_provenance(
    instance: WorkspaceInstance,
    *,
    legacy_token: str,
    durable_token: str,
    retained_legacy_source_present: bool,
) -> dict[str, object]:
    details = dict(instance.details) if isinstance(instance.details, dict) else {}
    details[_PROVENANCE_KEY] = _provenance_payload(
        legacy_token=legacy_token,
        durable_token=durable_token,
        retained_legacy_source_present=retained_legacy_source_present,
    )
    return details


def _registered_timestamp(value: datetime) -> float:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def _legacy_continuity_is_safe(
    instance: WorkspaceInstance,
    legacy_identity: tuple[int, int],
) -> tuple[bool, tuple[int, int] | None, str]:
    """Classify whether an old token can be bound to the current directory.

    Exact native identity is accepted on every platform.  macOS additionally
    permits the observed APFS remount case only when the canonical path and
    inode are unchanged and the directory birth time predates registration.
    A changed inode is never auto-adopted.
    """

    try:
        root = Path(instance.root_path).expanduser().resolve(strict=True)
        info = root.stat(follow_symlinks=False)
    except (OSError, RuntimeError, TypeError, ValueError):
        return False, None, "missing"
    if str(root) != instance.root_path or not stat.S_ISDIR(info.st_mode):
        return False, None, "replaced"
    try:
        current = (
            windows_path_identity(root, directory=True)
            if sys.platform == "win32"
            else (info.st_dev, info.st_ino)
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return False, None, "unavailable"
    if current == legacy_identity:
        return True, current, "exact"
    if sys.platform != "darwin" or current[1] != legacy_identity[1]:
        return False, current, "replaced"
    birth_time = getattr(info, "st_birthtime", None)
    if not isinstance(birth_time, (int, float)):
        return False, current, "unverifiable"
    if birth_time > _registered_timestamp(instance.time_created) + 2.0:
        return False, current, "replaced"
    return True, current, "device-renumbered"


async def _checkpoint_version_ids(
    db: AsyncSession,
    workspace_instance_id: str,
) -> frozenset[str]:
    values = (
        await db.execute(
            select(CheckpointChange.before_version_id)
            .join(
                SessionCheckpoint,
                SessionCheckpoint.id == CheckpointChange.checkpoint_id,
            )
            .where(
                SessionCheckpoint.workspace_instance_id == workspace_instance_id,
                CheckpointChange.before_version_id.is_not(None),
            )
        )
    ).scalars()
    return frozenset(value for value in values if value is not None)


async def migrate_legacy_workspace_identities(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, int]:
    """Migrate stat-v1 rows and repair durable stores from retained sources."""

    async with session_factory() as db:
        active_instances = list(
            (
                await db.execute(
                    select(WorkspaceInstance).where(
                        WorkspaceInstance.status == "active",
                    )
                )
            ).scalars()
        )
        legacy_instances = [
            instance
            for instance in active_instances
            if instance.identity_token.startswith("stat-v1:")
        ]
        repair_instances = [
            instance
            for instance in active_instances
            if not instance.identity_token.startswith("stat-v1:")
            and isinstance(instance.details, dict)
            and _PROVENANCE_KEY in instance.details
        ]
        required_versions = {
            instance.id: await _checkpoint_version_ids(db, instance.id)
            for instance in [*legacy_instances, *repair_instances]
        }

    migrated = 0
    missing = 0
    blocked = 0
    repaired = 0
    for instance in repair_instances:
        try:
            legacy_identity, retained_legacy_source_present = (
                _provenance_legacy_identity(instance)
            )
            identity = inspect_workspace_identity(instance.root_path)
            if identity.durable_token != instance.identity_token:
                raise WorkspaceIdentityError(
                    "workspace durable identity no longer matches the database"
                )
            store = FileVersionStore(
                identity.canonical_path,
                expected_workspace_identity=identity.volatile_identity,
                expected_durable_workspace_identity=identity.durable_token,
            )
            if store.manifest_path.exists() or store.manifest_path.is_symlink():
                store.verify_integrity(required_versions[instance.id])
                continue
            if not retained_legacy_source_present:
                # The stat-v1 workspace never had a file-version tree, so an
                # absent durable tree is the correct empty state.
                continue
            store = FileVersionStore(
                identity.canonical_path,
                expected_workspace_identity=identity.volatile_identity,
                expected_durable_workspace_identity=identity.durable_token,
                legacy_workspace_identity=legacy_identity,
            )
            if (
                not store.retained_legacy_source_present
                or not store.manifest_path.is_file()
            ):
                raise FileVersionError(
                    "Retained legacy file-version history is unavailable for repair"
                )
            store.verify_integrity(required_versions[instance.id])
            confirmed = inspect_workspace_identity(instance.root_path)
            if confirmed != identity:
                raise WorkspaceIdentityError(
                    "Workspace identity changed during file-version repair"
                )
            repaired += 1
            logger.warning(
                "Repaired durable file-version history for workspace %s from retained stat-v1 source",
                instance.id,
            )
        except (
            FileVersionError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            WorkspaceIdentityError,
        ) as exc:
            blocked += 1
            logger.error(
                "Workspace identity repair blocked for %s (%s): %s",
                instance.id,
                instance.root_path,
                exc,
            )

    for instance in legacy_instances:
        try:
            legacy_identity = parse_legacy_stat_token(instance.identity_token)
            if legacy_identity is None:
                raise WorkspaceIdentityError("invalid stat-v1 token")
            compatible, observed_identity, reason = _legacy_continuity_is_safe(
                instance,
                legacy_identity,
            )
        except (
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            WorkspaceIdentityError,
        ) as exc:
            blocked += 1
            logger.error(
                "Workspace identity migration blocked for %s during continuity inspection: %s",
                instance.id,
                exc,
            )
            continue
        if not compatible or observed_identity is None:
            if reason == "missing":
                missing += 1
            else:
                blocked += 1
            logger.error(
                "Workspace identity migration deferred for %s (%s): %s",
                instance.id,
                instance.root_path,
                reason,
            )
            continue
        try:
            identity = ensure_workspace_identity(instance.root_path)
            if identity.volatile_identity != observed_identity:
                raise WorkspaceIdentityError(
                    "Workspace root changed while durable identity was established"
                )
            store = FileVersionStore(
                identity.canonical_path,
                expected_workspace_identity=observed_identity,
                expected_durable_workspace_identity=identity.durable_token,
                legacy_workspace_identity=legacy_identity,
            )
            # Legacy adoption validates every copied object before publishing
            # (and validates a previously published destination before reuse).
            # Recheck the ledger-required subset here so missing checkpoint
            # references still block the database commit without hashing the
            # entire history a second time on every startup retry.
            store.verify_integrity(required_versions[instance.id])
            confirmed = inspect_workspace_identity(instance.root_path)
            if confirmed != identity:
                raise WorkspaceIdentityError(
                    "Workspace identity changed before database migration commit"
                )
            async with session_factory() as db:
                async with db.begin():
                    current = await db.get(WorkspaceInstance, instance.id)
                    if current is None:
                        raise WorkspaceIdentityError(
                            "Workspace instance disappeared during identity migration"
                        )
                    if current.identity_token == identity.durable_token:
                        current.details = _with_provenance(
                            current,
                            legacy_token=instance.identity_token,
                            durable_token=identity.durable_token,
                            retained_legacy_source_present=(
                                store.retained_legacy_source_present
                            ),
                        )
                        continue
                    if current.identity_token != instance.identity_token:
                        raise WorkspaceIdentityError(
                            "Workspace instance changed during identity migration"
                        )
                    collision = (
                        await db.execute(
                            select(WorkspaceInstance.id).where(
                                WorkspaceInstance.root_path == instance.root_path,
                                WorkspaceInstance.identity_token
                                == identity.durable_token,
                                WorkspaceInstance.id != instance.id,
                            )
                        )
                    ).scalar_one_or_none()
                    if collision is not None:
                        raise WorkspaceIdentityError(
                            "Durable workspace identity is already registered"
                        )
                    current.identity_token = identity.durable_token
                    current.details = _with_provenance(
                        current,
                        legacy_token=instance.identity_token,
                        durable_token=identity.durable_token,
                        retained_legacy_source_present=(
                            store.retained_legacy_source_present
                        ),
                    )
            migrated += 1
            logger.warning(
                "Migrated workspace identity %s from stat-v1 (%s)",
                instance.id,
                reason,
            )
        except (FileVersionError, WorkspaceIdentityError, OSError) as exc:
            blocked += 1
            logger.error(
                "Workspace identity migration blocked for %s (%s): %s",
                instance.id,
                instance.root_path,
                exc,
            )

    if repaired:
        logger.warning("Repaired %s durable workspace history store(s)", repaired)
    return {
        "migrated": migrated,
        "missing": missing,
        "blocked": blocked,
    }


__all__ = ["migrate_legacy_workspace_identities"]
