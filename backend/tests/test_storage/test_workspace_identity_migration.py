"""Regression contracts for stat-v1 workspace identity migration."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.checkpoint_change import CheckpointChange
from app.models.session import Session
from app.models.session_checkpoint import SessionCheckpoint
from app.models.turn_run import TurnRun
from app.models.workspace_instance import WorkspaceInstance
from app.storage import file_versions as file_versions_module
from app.storage import workspace_identity_migration as migration_module
from app.storage.file_versions import FileVersionError, FileVersionStore
from app.storage.workspace_identity import (
    ensure_workspace_identity,
    inspect_workspace_identity,
)
from app.tool.workspace import APP_PRIVATE_DIR_ENV


pytestmark = [
    pytest.mark.workspace_identity_v2,
    pytest.mark.skipif(
        sys.platform == "win32",
        reason="stat-v1 to marker-v2 migration is a POSIX compatibility path",
    ),
]


def _stat_identity(workspace: Path) -> tuple[int, int]:
    info = workspace.stat()
    return int(info.st_dev), int(info.st_ino)


def _legacy_token(identity: tuple[int, int]) -> str:
    return f"stat-v1:{identity[0]}:{identity[1]}"


async def _insert_workspace_instance(
    session_factory: async_sessionmaker[AsyncSession],
    workspace: Path,
    identity: tuple[int, int],
    *,
    instance_id: str = "workspace-instance",
) -> str:
    async with session_factory() as db:
        async with db.begin():
            db.add(
                WorkspaceInstance(
                    id=instance_id,
                    root_path=str(workspace.resolve()),
                    identity_token=_legacy_token(identity),
                    status="active",
                    details={},
                )
            )
    return instance_id


async def _identity_token(
    session_factory: async_sessionmaker[AsyncSession],
    instance_id: str,
) -> str:
    async with session_factory() as db:
        instance = await db.get(WorkspaceInstance, instance_id)
        assert instance is not None
        return instance.identity_token


async def _workspace_instance(
    session_factory: async_sessionmaker[AsyncSession],
    instance_id: str,
) -> WorkspaceInstance:
    async with session_factory() as db:
        instance = await db.get(WorkspaceInstance, instance_id)
        assert instance is not None
        return instance


def _version_payload(
    *,
    version_id: str,
    contents: bytes,
    relative_path: str = "document.txt",
) -> dict[str, object]:
    created = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    return {
        "id": version_id,
        "relative_path": relative_path,
        "sha256": hashlib.sha256(contents).hexdigest(),
        "size": len(contents),
        "created_at": created.isoformat(),
        "created_at_ns": int(created.timestamp() * 1_000_000_000),
        "operation": "test.migration",
        "session_id": "session",
        "message_id": "message",
        "call_id": "call",
        "original_mode": 0o600,
        "object_name": None,
    }


def _write_schema2_store(
    workspace: Path,
    storage_root: Path,
    identity: tuple[int, int],
    *,
    versions: list[tuple[str, bytes]],
    pins: dict[str, list[str]] | None = None,
    corrupt_version_ids: frozenset[str] = frozenset(),
) -> tuple[Path, list[dict[str, object]]]:
    workspace = workspace.resolve()
    legacy_key = file_versions_module._legacy_workspace_storage_key(
        workspace,
        identity,
    )
    legacy_root = storage_root / legacy_key
    objects = legacy_root / "objects"
    objects.mkdir(parents=True)
    payloads: list[dict[str, object]] = []
    for version_id, contents in versions:
        payload = _version_payload(version_id=version_id, contents=contents)
        payloads.append(payload)
        object_contents = (
            b"corrupt recovery object"
            if version_id in corrupt_version_ids
            else contents
        )
        (objects / f"{payload['sha256']}.blob").write_bytes(object_contents)
    manifest = {
        "schema_version": 2,
        "workspace": str(workspace),
        "workspace_identity": {"dev": identity[0], "ino": identity[1]},
        "versions": payloads,
        "pins": pins or {},
    }
    (legacy_root / "manifest-v1.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return legacy_root, payloads


async def _add_checkpoint_restore_source(
    session_factory: async_sessionmaker[AsyncSession],
    workspace: Path,
    *,
    instance_id: str,
    version_id: str,
    before_sha256: str,
) -> None:
    now = datetime.now(timezone.utc)
    async with session_factory() as db:
        async with db.begin():
            db.add(
                Session(
                    id="session",
                    directory=str(workspace),
                    slug="migration-test",
                    title="Migration test",
                    version="1.1.0",
                )
            )
            await db.flush()
            db.add(
                TurnRun(
                    id="turn",
                    session_id="session",
                    workspace_instance_id=instance_id,
                    root_turn_id="turn",
                    parent_turn_id=None,
                    depth=0,
                    source_kind="interactive",
                    status="completed",
                    has_irreversible_side_effects=False,
                    external_side_effects=[],
                    details={},
                    time_started=now,
                    time_finished=now,
                )
            )
            await db.flush()
            db.add(
                SessionCheckpoint(
                    id="checkpoint",
                    session_id="session",
                    workspace_instance_id=instance_id,
                    root_turn_id="turn",
                    turn_run_id="turn",
                    sequence=1,
                    todo_snapshot=[],
                    child_turn_ids=[],
                    state="finalized",
                    pin_state="pinned",
                    has_irreversible_side_effects=False,
                    external_side_effects=[],
                    details={},
                    time_finalized=now,
                )
            )
            await db.flush()
            db.add(
                CheckpointChange(
                    id="change",
                    checkpoint_id="checkpoint",
                    turn_run_id="turn",
                    sequence=1,
                    operation="modified",
                    node_kind="file",
                    relative_path="document.txt",
                    before_exists=True,
                    before_version_id=version_id,
                    before_sha256=before_sha256,
                    before_mode=0o600,
                    after_exists=True,
                    after_sha256=hashlib.sha256(b"after").hexdigest(),
                    after_mode=0o600,
                    after_size=len(b"after"),
                    details={},
                )
            )


@pytest.mark.asyncio
async def test_exact_stat_v1_identity_migrates_to_durable_marker(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    identity = _stat_identity(workspace)
    instance_id = await _insert_workspace_instance(
        session_factory,
        workspace,
        identity,
    )
    monkeypatch.setenv(APP_PRIVATE_DIR_ENV, str(tmp_path / "private"))

    result = await migration_module.migrate_legacy_workspace_identities(session_factory)

    durable = inspect_workspace_identity(workspace)
    assert result == {"migrated": 1, "missing": 0, "blocked": 0}
    assert await _identity_token(session_factory, instance_id) == durable.durable_token
    assert durable.durable_token.startswith("marker-v2:")
    instance = await _workspace_instance(session_factory, instance_id)
    provenance = instance.details["workspace_identity_v2"]
    assert provenance["schema_version"] == 1
    assert provenance["legacy_identity_token"] == _legacy_token(identity)
    assert provenance["durable_identity_token"] == durable.durable_token
    assert provenance["retained_legacy_source_present"] is False


@pytest.mark.asyncio
async def test_continuity_exception_isolated_from_healthy_workspace(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked_workspace = tmp_path / "blocked"
    healthy_workspace = tmp_path / "healthy"
    blocked_workspace.mkdir()
    healthy_workspace.mkdir()
    await _insert_workspace_instance(
        session_factory,
        blocked_workspace,
        _stat_identity(blocked_workspace),
        instance_id="blocked-instance",
    )
    await _insert_workspace_instance(
        session_factory,
        healthy_workspace,
        _stat_identity(healthy_workspace),
        instance_id="healthy-instance",
    )
    monkeypatch.setenv(APP_PRIVATE_DIR_ENV, str(tmp_path / "private"))
    real_continuity = migration_module._legacy_continuity_is_safe

    def flaky_continuity(instance, legacy_identity):
        if instance.id == "blocked-instance":
            raise RuntimeError("simulated path inspection failure")
        return real_continuity(instance, legacy_identity)

    monkeypatch.setattr(
        migration_module,
        "_legacy_continuity_is_safe",
        flaky_continuity,
    )

    result = await migration_module.migrate_legacy_workspace_identities(
        session_factory
    )

    assert result == {"migrated": 1, "missing": 0, "blocked": 1}
    assert (await _identity_token(session_factory, "blocked-instance")).startswith(
        "stat-v1:"
    )
    assert (await _identity_token(session_factory, "healthy-instance")).startswith(
        "marker-v2:"
    )


@pytest.mark.asyncio
async def test_darwin_device_renumbering_with_same_inode_migrates(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    current_identity = _stat_identity(workspace)
    legacy_identity = (current_identity[0] + 10_000, current_identity[1])
    instance_id = await _insert_workspace_instance(
        session_factory,
        workspace,
        legacy_identity,
    )
    monkeypatch.setenv(APP_PRIVATE_DIR_ENV, str(tmp_path / "private"))
    actual_info = workspace.stat()

    class SimulatedDarwinPath:
        def __init__(self, value: str) -> None:
            self.path = Path(value).expanduser().resolve(strict=True)

        def expanduser(self) -> "SimulatedDarwinPath":
            return self

        def resolve(self, *, strict: bool) -> "SimulatedDarwinPath":
            assert strict is True
            return self

        def stat(self, *, follow_symlinks: bool) -> SimpleNamespace:
            assert follow_symlinks is False
            return SimpleNamespace(
                st_mode=actual_info.st_mode,
                st_dev=current_identity[0],
                st_ino=current_identity[1],
                st_birthtime=0.0,
            )

        def __str__(self) -> str:
            return str(self.path)

    monkeypatch.setattr(migration_module, "Path", SimulatedDarwinPath)
    monkeypatch.setattr(migration_module, "sys", SimpleNamespace(platform="darwin"))

    result = await migration_module.migrate_legacy_workspace_identities(session_factory)

    assert result == {"migrated": 1, "missing": 0, "blocked": 0}
    assert await _identity_token(session_factory, instance_id) == (
        inspect_workspace_identity(workspace).durable_token
    )


@pytest.mark.asyncio
async def test_inode_replacement_is_blocked_without_creating_a_marker(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    current_identity = _stat_identity(workspace)
    legacy_identity = (current_identity[0], current_identity[1] + 1)
    instance_id = await _insert_workspace_instance(
        session_factory,
        workspace,
        legacy_identity,
    )
    monkeypatch.setenv(APP_PRIVATE_DIR_ENV, str(tmp_path / "private"))

    result = await migration_module.migrate_legacy_workspace_identities(session_factory)

    assert result == {"migrated": 0, "missing": 0, "blocked": 1}
    assert await _identity_token(session_factory, instance_id) == _legacy_token(
        legacy_identity
    )
    assert not (workspace / ".suxiaoyou").exists()


@pytest.mark.asyncio
async def test_retry_reuses_prepared_marker_and_then_becomes_a_noop(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    identity = _stat_identity(workspace)
    prepared = ensure_workspace_identity(workspace)
    instance_id = await _insert_workspace_instance(
        session_factory,
        workspace,
        identity,
    )
    monkeypatch.setenv(APP_PRIVATE_DIR_ENV, str(tmp_path / "private"))

    first = await migration_module.migrate_legacy_workspace_identities(session_factory)
    second = await migration_module.migrate_legacy_workspace_identities(session_factory)

    assert first == {"migrated": 1, "missing": 0, "blocked": 0}
    assert second == {"migrated": 0, "missing": 0, "blocked": 0}
    assert inspect_workspace_identity(workspace).durable_token == (
        prepared.durable_token
    )
    assert await _identity_token(session_factory, instance_id) == (
        prepared.durable_token
    )


def test_schema2_store_is_copied_to_schema3_and_legacy_source_is_retained(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    identity = _stat_identity(workspace)
    storage_root = tmp_path / "private" / "file-versions"
    legacy_root, versions = _write_schema2_store(
        workspace,
        storage_root,
        identity,
        versions=[("version-one", b"recoverable contents")],
        pins={"checkpoint-one": ["version-one"]},
    )
    legacy_manifest_bytes = (legacy_root / "manifest-v1.json").read_bytes()
    durable = ensure_workspace_identity(workspace)

    store = FileVersionStore(
        workspace,
        storage_root=storage_root,
        expected_workspace_identity=identity,
        expected_durable_workspace_identity=durable.durable_token,
        legacy_workspace_identity=identity,
    )

    migrated_manifest = json.loads(store.manifest_path.read_text(encoding="utf-8"))
    assert store.verify_integrity() == 1
    assert [version.id for version in store.list_versions()] == ["version-one"]
    assert store.list_pins() == {"checkpoint-one": frozenset({"version-one"})}
    assert migrated_manifest["schema_version"] == 3
    assert migrated_manifest["workspace_identity"] == {"token": durable.durable_token}
    assert migrated_manifest["versions"] == versions
    assert migrated_manifest["pins"] == {"checkpoint-one": ["version-one"]}
    assert legacy_root != store.root
    assert legacy_root.is_dir()
    assert (legacy_root / "manifest-v1.json").read_bytes() == legacy_manifest_bytes
    assert (legacy_root / "objects" / f"{versions[0]['sha256']}.blob").read_bytes() == (
        b"recoverable contents"
    )


@pytest.mark.parametrize("destination_shape", ["empty", "partial"])
def test_legacy_store_reconstructs_safe_incomplete_durable_destination(
    destination_shape: str,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    identity = _stat_identity(workspace)
    storage_root = tmp_path / "private" / "file-versions"
    legacy_root, versions = _write_schema2_store(
        workspace,
        storage_root,
        identity,
        versions=[
            ("version-one", b"first recovery object"),
            ("version-two", b"second recovery object"),
        ],
        pins={"checkpoint-one": ["version-one"]},
    )
    durable = ensure_workspace_identity(workspace)
    probe = FileVersionStore(
        workspace,
        storage_root=storage_root,
        expected_workspace_identity=identity,
        expected_durable_workspace_identity=durable.durable_token,
    )
    probe.root.mkdir()
    if destination_shape == "partial":
        probe.objects_dir.mkdir()
        first_object = f"{versions[0]['sha256']}.blob"
        (probe.objects_dir / first_object).write_bytes(
            (legacy_root / "objects" / first_object).read_bytes()
        )

    store = FileVersionStore(
        workspace,
        storage_root=storage_root,
        expected_workspace_identity=identity,
        expected_durable_workspace_identity=durable.durable_token,
        legacy_workspace_identity=identity,
    )

    assert store.verify_integrity() == 2
    assert {version.id for version in store.list_versions()} == {
        "version-one",
        "version-two",
    }
    assert store.list_pins() == {"checkpoint-one": frozenset({"version-one"})}
    assert legacy_root.is_dir()


def test_conflicting_durable_destination_blocks_legacy_adoption(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    identity = _stat_identity(workspace)
    storage_root = tmp_path / "private" / "file-versions"
    legacy_root, _versions = _write_schema2_store(
        workspace,
        storage_root,
        identity,
        versions=[("legacy-version", b"authoritative legacy object")],
    )
    durable = ensure_workspace_identity(workspace)
    probe = FileVersionStore(
        workspace,
        storage_root=storage_root,
        expected_workspace_identity=identity,
        expected_durable_workspace_identity=durable.durable_token,
    )
    probe.objects_dir.mkdir(parents=True)
    conflicting_manifest = {
        "schema_version": 3,
        "workspace": str(workspace.resolve()),
        "workspace_identity": {"token": durable.durable_token},
        "versions": [],
        "pins": {},
    }
    probe.manifest_path.write_text(
        json.dumps(conflicting_manifest, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    destination_before = probe.manifest_path.read_bytes()

    with pytest.raises(FileVersionError, match="conflicts with legacy history"):
        FileVersionStore(
            workspace,
            storage_root=storage_root,
            expected_workspace_identity=identity,
            expected_durable_workspace_identity=durable.durable_token,
            legacy_workspace_identity=identity,
        )

    assert probe.manifest_path.read_bytes() == destination_before
    assert legacy_root.is_dir()


def test_publish_durability_failure_blocks_then_retry_reuses_complete_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    identity = _stat_identity(workspace)
    storage_root = tmp_path / "private" / "file-versions"
    legacy_root, _versions = _write_schema2_store(
        workspace,
        storage_root,
        identity,
        versions=[("version-one", b"recoverable after retry")],
    )
    durable = ensure_workspace_identity(workspace)
    probe = FileVersionStore(
        workspace,
        storage_root=storage_root,
        expected_workspace_identity=identity,
        expected_durable_workspace_identity=durable.durable_token,
    )
    real_fsync_directory = file_versions_module._fsync_directory_strict
    failed = False

    def fail_first_published_parent_sync(directory: Path) -> None:
        nonlocal failed
        if (
            Path(directory) == storage_root
            and probe.manifest_path.exists()
            and not failed
        ):
            failed = True
            raise FileVersionError("simulated parent fsync failure")
        real_fsync_directory(directory)

    monkeypatch.setattr(
        file_versions_module,
        "_fsync_directory_strict",
        fail_first_published_parent_sync,
    )
    with pytest.raises(FileVersionError, match="simulated parent fsync failure"):
        FileVersionStore(
            workspace,
            storage_root=storage_root,
            expected_workspace_identity=identity,
            expected_durable_workspace_identity=durable.durable_token,
            legacy_workspace_identity=identity,
        )

    assert failed is True
    assert probe.manifest_path.is_file()
    assert legacy_root.is_dir()

    monkeypatch.setattr(
        file_versions_module,
        "_fsync_directory_strict",
        real_fsync_directory,
    )
    retried = FileVersionStore(
        workspace,
        storage_root=storage_root,
        expected_workspace_identity=identity,
        expected_durable_workspace_identity=durable.durable_token,
        legacy_workspace_identity=identity,
    )

    assert retried.verify_integrity() == 1
    assert [version.id for version in retried.list_versions()] == ["version-one"]
    assert legacy_root.is_dir()


@pytest.mark.asyncio
async def test_vanished_durable_store_is_repaired_from_provenance_and_legacy_source(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    identity = _stat_identity(workspace)
    instance_id = await _insert_workspace_instance(
        session_factory,
        workspace,
        identity,
    )
    private_root = tmp_path / "private"
    storage_root = private_root / "file-versions"
    monkeypatch.setenv(APP_PRIVATE_DIR_ENV, str(private_root))
    legacy_root, _versions = _write_schema2_store(
        workspace,
        storage_root,
        identity,
        versions=[("version-one", b"survives a power loss")],
        pins={"checkpoint-one": ["version-one"]},
    )

    first = await migration_module.migrate_legacy_workspace_identities(session_factory)
    instance = await _workspace_instance(session_factory, instance_id)
    durable_token = instance.identity_token
    provenance = instance.details["workspace_identity_v2"]
    assert first == {"migrated": 1, "missing": 0, "blocked": 0}
    assert provenance["legacy_identity_token"] == _legacy_token(identity)
    assert provenance["retained_legacy_source_present"] is True

    migrated = FileVersionStore(
        workspace,
        storage_root=storage_root,
        expected_durable_workspace_identity=durable_token,
    )
    shutil.rmtree(migrated.root)
    assert legacy_root.is_dir()
    assert not migrated.root.exists()

    repaired = await migration_module.migrate_legacy_workspace_identities(
        session_factory
    )

    restored = FileVersionStore(
        workspace,
        storage_root=storage_root,
        expected_durable_workspace_identity=durable_token,
    )
    assert repaired == {"migrated": 0, "missing": 0, "blocked": 0}
    assert restored.verify_integrity() == 1
    assert [version.id for version in restored.list_versions()] == ["version-one"]
    assert legacy_root.is_dir()
    assert (await _workspace_instance(session_factory, instance_id)).identity_token == (
        durable_token
    )


@pytest.mark.asyncio
async def test_vanished_durable_store_blocks_when_retained_source_is_missing(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    identity = _stat_identity(workspace)
    instance_id = await _insert_workspace_instance(
        session_factory,
        workspace,
        identity,
    )
    private_root = tmp_path / "private"
    storage_root = private_root / "file-versions"
    monkeypatch.setenv(APP_PRIVATE_DIR_ENV, str(private_root))
    legacy_root, _versions = _write_schema2_store(
        workspace,
        storage_root,
        identity,
        versions=[("version-one", b"required retained source")],
    )
    first = await migration_module.migrate_legacy_workspace_identities(session_factory)
    instance = await _workspace_instance(session_factory, instance_id)
    durable_token = instance.identity_token
    migrated = FileVersionStore(
        workspace,
        storage_root=storage_root,
        expected_durable_workspace_identity=durable_token,
    )
    shutil.rmtree(migrated.root)
    shutil.rmtree(legacy_root)

    second = await migration_module.migrate_legacy_workspace_identities(session_factory)

    assert first == {"migrated": 1, "missing": 0, "blocked": 0}
    assert second == {"migrated": 0, "missing": 0, "blocked": 1}
    assert not migrated.root.exists()
    assert (await _workspace_instance(session_factory, instance_id)).identity_token == (
        durable_token
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["corrupt", "missing-required"])
async def test_invalid_required_recovery_history_blocks_database_update(
    damage: str,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    identity = _stat_identity(workspace)
    instance_id = await _insert_workspace_instance(
        session_factory,
        workspace,
        identity,
    )
    private_root = tmp_path / "private"
    storage_root = private_root / "file-versions"
    monkeypatch.setenv(APP_PRIVATE_DIR_ENV, str(private_root))
    required_contents = b"required restore source"
    required_digest = hashlib.sha256(required_contents).hexdigest()
    if damage == "corrupt":
        required_id = "required-version"
        _write_schema2_store(
            workspace,
            storage_root,
            identity,
            versions=[(required_id, required_contents)],
            pins={"checkpoint": [required_id]},
            corrupt_version_ids=frozenset({required_id}),
        )
    else:
        required_id = "missing-required-version"
        _write_schema2_store(
            workspace,
            storage_root,
            identity,
            versions=[("unrelated-version", b"other valid contents")],
            pins={"other-checkpoint": ["unrelated-version"]},
        )
    await _add_checkpoint_restore_source(
        session_factory,
        workspace,
        instance_id=instance_id,
        version_id=required_id,
        before_sha256=required_digest,
    )

    result = await migration_module.migrate_legacy_workspace_identities(session_factory)

    assert result == {"migrated": 0, "missing": 0, "blocked": 1}
    assert await _identity_token(session_factory, instance_id) == _legacy_token(
        identity
    )
    # External identity preparation is intentionally idempotent and may
    # precede version-store validation; the database remains the commit point.
    assert inspect_workspace_identity(workspace).durable_token.startswith("marker-v2:")
