from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.worktree import service as service_module
from app.worktree import (
    GitCommandError,
    GitCommandTimeout,
    NoWorktreeReferences,
    RepositoryValidationError,
    WorktreeActiveError,
    WorktreeConflictError,
    WorktreeDirtyError,
    WorktreeFeatureDisabled,
    WorktreeOwnershipError,
    WorktreePathError,
    WorktreeReferences,
    WorktreeService,
    WorktreeState,
)


def _git(
    repository: Path, *arguments: str, check: bool = True
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [
            "git",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-C",
            os.fspath(repository),
            *arguments,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
        shell=False,
    )


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "源 仓库"
    root.mkdir()
    subprocess.run(
        ["git", "init", "--initial-branch=main", os.fspath(root)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        shell=False,
    )
    (root / "说明 文档.txt").write_text("第一版\n", encoding="utf-8")
    _git(root, "add", "--", "说明 文档.txt")
    _git(
        root,
        "-c",
        "user.name=Suxiaoyou Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "initial",
    )
    return root


def _service(tmp_path: Path, **kwargs: object) -> WorktreeService:
    reference_guard = kwargs.pop("reference_guard", NoWorktreeReferences())
    return WorktreeService(
        managed_root=tmp_path / "应用 私有" / "工作树 管理",
        enabled=True,
        reference_guard=reference_guard,
        **kwargs,
    )


def test_feature_gate_is_closed_by_default_without_creating_storage(
    tmp_path: Path, repository: Path
) -> None:
    managed_root = tmp_path / "closed"
    service = WorktreeService(managed_root=managed_root, enabled=False)

    with pytest.raises(WorktreeFeatureDisabled):
        service.create(repository, workspace_instance_id="instance-closed")

    assert not managed_root.exists()


@pytest.mark.skipif(os.name == "nt", reason="uses the POSIX false executable")
def test_git_nonzero_exit_surfaces_typed_error(
    tmp_path: Path,
) -> None:
    false_executable = shutil.which("false")
    if false_executable is None:
        pytest.skip("false executable is unavailable")
    service = _service(tmp_path, git_executable=false_executable)
    service._prepare()

    with pytest.raises(GitCommandError) as raised:
        service._run_git("negative-path", ["--version"])

    assert raised.value.operation == "negative-path"
    assert raised.value.returncode != 0


@pytest.mark.workspace_identity_v2
def test_detached_create_bind_remove_and_gc_with_cjk_paths(
    tmp_path: Path, repository: Path
) -> None:
    service = _service(tmp_path)

    created = service.create(repository, workspace_instance_id="instance-01")

    assert created.state is WorktreeState.CREATED
    assert created.branch is None
    assert Path(created.checkout_path).is_dir()
    assert Path(created.git_common_dir) == (repository / ".git").resolve()
    inspection = service.inspect("instance-01")
    assert inspection.clean is True
    assert inspection.registered is True
    assert inspection.branch is None
    symbolic_ref = _git(
        Path(created.checkout_path), "symbolic-ref", "-q", "HEAD", check=False
    )
    assert symbolic_ref.returncode == 1

    bound = service.bind("instance-01", expected_repository=repository)
    assert bound.state is WorktreeState.BOUND
    detached = service.detach("instance-01")
    assert detached.state is WorktreeState.DETACHED
    removed = service.remove("instance-01")
    assert removed.state is WorktreeState.REMOVED
    assert not Path(removed.checkout_path).exists()

    gc_result = service.gc("instance-01")
    assert gc_result.collected == ("instance-01",)
    assert not (service.managed_root / "instance-01").exists()


def test_create_refuses_dirty_source_repository(
    tmp_path: Path, repository: Path
) -> None:
    (repository / "未跟踪.txt").write_text("dirty", encoding="utf-8")
    service = _service(tmp_path)

    with pytest.raises(WorktreeDirtyError, match="source repository"):
        service.create(repository, workspace_instance_id="dirty-source")

    assert not (service.managed_root / "dirty-source").exists()


@pytest.mark.workspace_identity_v2
def test_create_rejects_filesystem_that_would_dirty_every_checkout(
    tmp_path: Path,
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr(
        service_module,
        "workspace_identity_uses_file_fallback",
        lambda _path: True,
    )

    with pytest.raises(WorktreePathError, match="native durable identity"):
        service.create(repository, workspace_instance_id="no-native-identity")

    assert not (service.managed_root / "no-native-identity").exists()
    assert (
        len(_git(repository, "worktree", "list", "--porcelain").stdout.splitlines())
        >= 1
    )


@pytest.mark.workspace_identity_v2
def test_durable_worktree_identity_ignores_apfs_device_renumbering(
    tmp_path: Path,
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    created = service.create(repository, workspace_instance_id="device-renumbered")
    assert all(
        identity is not None and identity.durable_token is not None
        for identity in (
            created.instance_identity,
            created.repository_identity,
            created.common_dir_identity,
            created.checkout_identity,
        )
    )
    inspect_directory = service_module.FilesystemIdentity.inspect_directory.__func__

    def renumbered_identity(cls, path: Path):
        current = inspect_directory(cls, path)
        return service_module.FilesystemIdentity(
            device=current.device + 10_000,
            inode=current.inode,
            durable_token=current.durable_token,
        )

    monkeypatch.setattr(
        service_module.FilesystemIdentity,
        "inspect_directory",
        classmethod(renumbered_identity),
    )

    inspection = service.inspect("device-renumbered")

    assert inspection.record.workspace_instance_id == "device-renumbered"
    assert inspection.clean is True


@pytest.mark.workspace_identity_v2
def test_mixed_schema_one_manifest_upgrades_without_downgrading_durable_fields(
    tmp_path: Path,
    repository: Path,
) -> None:
    service = _service(tmp_path)
    created = service.create(repository, workspace_instance_id="mixed-manifest")
    manifest = service.managed_root / "mixed-manifest" / "ownership-v1.json"
    value = json.loads(manifest.read_text(encoding="utf-8"))
    original_instance_token = value["instance_identity"]["durable_token"]
    value["repository_identity"].pop("durable_token")
    manifest.write_text(json.dumps(value), encoding="utf-8")

    inspection = service.inspect("mixed-manifest")

    upgraded = json.loads(manifest.read_text(encoding="utf-8"))
    assert upgraded["instance_identity"]["durable_token"] == original_instance_token
    assert upgraded["repository_identity"]["durable_token"] == (
        created.repository_identity.durable_token
    )
    assert inspection.record.repository_identity.durable_token is not None
    assert not service_module.FilesystemIdentity(
        device=1,
        inode=2,
        durable_token=original_instance_token,
    ).matches(service_module.FilesystemIdentity(device=1, inode=2))


@pytest.mark.workspace_identity_v2
@pytest.mark.skipif(os.name == "nt", reason="simulates Darwin APFS stat semantics")
def test_legacy_schema_one_manifest_safely_adopts_durable_tokens_after_apfs_renumber(
    tmp_path: Path,
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    created = service.create(repository, workspace_instance_id="legacy-renumbered")
    manifest = service.managed_root / "legacy-renumbered" / "ownership-v1.json"
    value = json.loads(manifest.read_text(encoding="utf-8"))
    identity_fields = (
        "instance_identity",
        "repository_identity",
        "common_dir_identity",
        "checkout_identity",
    )
    for field in identity_fields:
        value[field].pop("durable_token")
        value[field]["device"] += 10_000
    manifest.write_text(json.dumps(value), encoding="utf-8")

    real_snapshot = service_module._legacy_directory_snapshot

    def darwin_snapshot(path: Path):
        current, _birth_time = real_snapshot(path)
        return current, 0.0

    monkeypatch.setattr(
        service_module,
        "sys",
        SimpleNamespace(platform="darwin"),
    )
    monkeypatch.setattr(
        service_module,
        "_legacy_directory_snapshot",
        darwin_snapshot,
    )

    inspection = service.inspect("legacy-renumbered")

    upgraded = json.loads(manifest.read_text(encoding="utf-8"))
    assert inspection.clean is True
    for field in identity_fields:
        assert upgraded[field]["durable_token"].startswith("marker-v2:")
    assert inspection.record.instance_identity.durable_token == (
        created.instance_identity.durable_token
    )


@pytest.mark.workspace_identity_v2
@pytest.mark.skipif(os.name == "nt", reason="simulates Darwin APFS stat semantics")
def test_legacy_schema_one_device_drift_with_late_birthtime_fails_closed(
    tmp_path: Path,
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    service.create(repository, workspace_instance_id="legacy-replaced")
    manifest = service.managed_root / "legacy-replaced" / "ownership-v1.json"
    value = json.loads(manifest.read_text(encoding="utf-8"))
    value["instance_identity"].pop("durable_token")
    value["instance_identity"]["device"] += 10_000
    manifest.write_text(json.dumps(value), encoding="utf-8")

    real_snapshot = service_module._legacy_directory_snapshot

    def late_birth_snapshot(path: Path):
        current, _birth_time = real_snapshot(path)
        return current, 9_999_999_999.0

    monkeypatch.setattr(
        service_module,
        "sys",
        SimpleNamespace(platform="darwin"),
    )
    monkeypatch.setattr(
        service_module,
        "_legacy_directory_snapshot",
        late_birth_snapshot,
    )

    with pytest.raises(WorktreeOwnershipError, match="identity changed"):
        service.inspect("legacy-replaced")

    retained = json.loads(manifest.read_text(encoding="utf-8"))
    assert "durable_token" not in retained["instance_identity"]


def test_non_git_directory_is_an_ineligible_repository_not_a_git_failure(
    tmp_path: Path,
) -> None:
    ordinary_folder = tmp_path / "普通文件夹"
    ordinary_folder.mkdir()
    service = _service(tmp_path)

    with pytest.raises(RepositoryValidationError, match="not a Git worktree"):
        service.validate_source(ordinary_folder)
    with pytest.raises(RepositoryValidationError, match="not a Git worktree"):
        service.create(ordinary_folder, workspace_instance_id="not-a-repository")

    assert not (service.managed_root / "not-a-repository").exists()


def test_remove_refuses_dirty_managed_worktree(
    tmp_path: Path, repository: Path
) -> None:
    service = _service(tmp_path)
    record = service.create(repository, workspace_instance_id="dirty-checkout")
    service.bind("dirty-checkout")
    (Path(record.checkout_path) / "本地 修改.txt").write_text(
        "keep me", encoding="utf-8"
    )
    service.detach("dirty-checkout")

    with pytest.raises(WorktreeDirtyError, match="dirty managed worktree"):
        service.remove("dirty-checkout")

    assert Path(record.checkout_path).is_dir()
    contents = (Path(record.checkout_path) / "本地 修改.txt").read_text(
        encoding="utf-8"
    )
    assert contents == "keep me"


class _BlockingGuard:
    def blockers_for(
        self, *, workspace_instance_id: str, checkout_path: Path
    ) -> WorktreeReferences:
        assert checkout_path.is_dir()
        return WorktreeReferences(
            workspace_instance_ids=(workspace_instance_id,),
            turn_ids=("turn-active",),
            checkpoint_ids=("checkpoint-pinned",),
        )


def test_active_workspace_turn_and_checkpoint_refs_block_detach(
    tmp_path: Path, repository: Path
) -> None:
    service = _service(tmp_path, reference_guard=_BlockingGuard())
    service.create(repository, workspace_instance_id="active-instance")
    service.bind("active-instance")

    with pytest.raises(WorktreeActiveError, match="turn-active"):
        service.detach("active-instance")

    assert service.inspect("active-instance").record.state is WorktreeState.BOUND


def test_missing_persistent_reference_adapter_fails_closed(
    tmp_path: Path, repository: Path
) -> None:
    service = WorktreeService(
        managed_root=tmp_path / "unwired-private-root",
        enabled=True,
    )
    record = service.create(repository, workspace_instance_id="unwired-instance")
    service.bind("unwired-instance")

    with pytest.raises(WorktreeActiveError, match="not configured"):
        service.detach("unwired-instance")

    assert Path(record.checkout_path).is_dir()


def test_foreign_directory_is_never_removed(tmp_path: Path, repository: Path) -> None:
    service = _service(tmp_path)
    service.create(repository, workspace_instance_id="owned")
    foreign = service.managed_root / "foreign"
    foreign.mkdir()
    marker = foreign / "do-not-delete.txt"
    marker.write_text("foreign", encoding="utf-8")

    with pytest.raises(WorktreeOwnershipError, match="no ownership manifest"):
        service.remove("foreign")

    report = service.reconcile()
    gc_report = service.gc()
    assert "foreign" in report.foreign
    assert "foreign" in gc_report.foreign
    assert marker.read_text(encoding="utf-8") == "foreign"


def test_path_escape_and_symlink_roots_are_rejected(
    tmp_path: Path, repository: Path
) -> None:
    service = _service(tmp_path)
    with pytest.raises(WorktreePathError):
        service.create(repository, workspace_instance_id="../escape")
    assert not (tmp_path / "escape").exists()

    actual = tmp_path / "actual-root"
    actual.mkdir()
    linked = tmp_path / "linked-root"
    linked.symlink_to(actual, target_is_directory=True)
    with pytest.raises(WorktreePathError, match="symbolic link"):
        WorktreeService(managed_root=linked, enabled=True)


def test_replaced_checkout_symlink_is_not_followed_or_removed(
    tmp_path: Path, repository: Path
) -> None:
    service = _service(tmp_path)
    record = service.create(repository, workspace_instance_id="symlink-swap")
    service.detach("symlink-swap")
    _git(repository, "worktree", "remove", "--", record.checkout_path)
    Path(record.checkout_path).symlink_to(repository, target_is_directory=True)

    with pytest.raises(WorktreeOwnershipError, match="symlink"):
        service.inspect("symlink-swap")
    with pytest.raises(WorktreeOwnershipError, match="symlink"):
        service.remove("symlink-swap")

    assert repository.is_dir()
    assert (repository / "说明 文档.txt").is_file()


def test_manifest_path_tampering_cannot_escape_managed_root(
    tmp_path: Path, repository: Path
) -> None:
    service = _service(tmp_path)
    service.create(repository, workspace_instance_id="tampered")
    manifest = service.managed_root / "tampered" / "ownership-v1.json"
    value = json.loads(manifest.read_text(encoding="utf-8"))
    victim = tmp_path / "victim"
    victim.mkdir()
    value["checkout_path"] = os.fspath(victim)
    manifest.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(WorktreeOwnershipError, match="escaped"):
        service.inspect("tampered")

    assert victim.is_dir()


def test_reconcile_repairs_crash_after_git_removed_checkout(
    tmp_path: Path, repository: Path
) -> None:
    service = _service(tmp_path)
    record = service.create(repository, workspace_instance_id="crash-remove")
    service.bind("crash-remove")
    service.detach("crash-remove")

    # Simulate a process crash after Git completed removal but before the
    # service durably changed the ownership state to "removed".
    _git(repository, "worktree", "remove", "--", record.checkout_path)
    report = service.reconcile()

    assert report.repaired == ("crash-remove",)
    assert report.removed_pending_gc == ("crash-remove",)
    assert report.errors == ()
    assert service.gc("crash-remove").collected == ("crash-remove",)


def test_concurrent_existing_branch_binding_has_single_winner(
    tmp_path: Path, repository: Path
) -> None:
    _git(repository, "branch", "feature/concurrent")
    service = _service(tmp_path)

    def create(instance_id: str) -> str:
        try:
            service.create(
                repository,
                workspace_instance_id=instance_id,
                branch="feature/concurrent",
            )
            return "created"
        except WorktreeConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, ("branch-one", "branch-two")))

    assert sorted(results) == ["conflict", "created"]
    created_ids = [
        instance_id
        for instance_id in ("branch-one", "branch-two")
        if (service.managed_root / instance_id / "ownership-v1.json").exists()
    ]
    assert len(created_ids) == 1
    assert service.inspect(created_ids[0]).branch == "feature/concurrent"


def test_unborn_repository_and_repository_symlink_are_rejected(tmp_path: Path) -> None:
    unborn = tmp_path / "unborn"
    unborn.mkdir()
    subprocess.run(
        ["git", "init", os.fspath(unborn)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        shell=False,
    )
    service = _service(tmp_path)
    with pytest.raises(RepositoryValidationError, match="does not resolve"):
        service.create(unborn, workspace_instance_id="unborn")

    linked = tmp_path / "repo-link"
    linked.symlink_to(unborn, target_is_directory=True)
    with pytest.raises(RepositoryValidationError, match="symlink"):
        service.create(linked, workspace_instance_id="repo-link")


def test_external_filter_or_fsmonitor_config_is_rejected(
    tmp_path: Path, repository: Path
) -> None:
    _git(repository, "config", "filter.unsafe.smudge", "echo unsafe")
    service = _service(tmp_path)

    with pytest.raises(RepositoryValidationError, match="external"):
        service.create(repository, workspace_instance_id="unsafe-filter")

    assert not (service.managed_root / "unsafe-filter").exists()


@pytest.mark.skipif(os.name == "nt", reason="uses a POSIX executable test fixture")
def test_git_command_timeout_stops_the_supervised_process(tmp_path: Path) -> None:
    if " " in sys.executable:
        pytest.skip("test shebang cannot represent this Python executable")
    fake_git = tmp_path / "fake-git"
    fake_git.write_text(
        f"#!{sys.executable}\nimport time\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o700)
    service = WorktreeService(
        managed_root=tmp_path / "timeout-private-root",
        git_executable=fake_git,
        timeout_seconds=0.05,
        enabled=True,
    )
    service._prepare()

    with pytest.raises(GitCommandTimeout, match="timed out"):
        service._run_git("timeout-test", ["version"])
