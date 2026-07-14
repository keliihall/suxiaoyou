"""Release-boundary tests for v0.8 credential artifact migration."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from app.auth.credential_store import CredentialStore, resolve_secret_tree
from app.auth.legacy_credentials import (
    ARCHIVE_FORMAT,
    LegacyCredentialMigrationError,
    migrate_historical_workspace_credentials,
    migrate_legacy_credential_artifacts,
    recover_archived_legacy_artifact,
)
from app.models.session import Session


class _MemoryNativeBackend:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.values.pop((service, username), None)


def _write(path: Path, content: str | bytes, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    path.chmod(mode)


def _assert_owner_only(path: Path) -> None:
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def _mcp_path(root: Path, project: Path | None) -> Path:
    scope = str(project.resolve()) if project is not None else "global"
    scope_hash = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:20]
    return root / "data" / "credentials" / "mcp" / f"{scope_hash}.json"


def _disk_snapshot(*roots: Path) -> dict[str, bytes]:
    snapshot: dict[str, bytes] = {}
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            snapshot[str(path)] = path.read_bytes()
    return snapshot


def test_v08_fixture_is_secured_without_feature_initialization_and_is_idempotent(
    tmp_path: Path,
) -> None:
    root = tmp_path / "app-data"
    project = tmp_path / "workspace"
    native = _MemoryNativeBackend()
    store = CredentialStore(
        fallback_path=root / "data" / "credentials" / "fallback.json",
        native_backend=native,
    )

    channels_path = root / "data" / "channels.json"
    channels_fixture = {
        "channels": {
            "feishu": {
                "enabled": True,
                "app_id": "not-secret-app-id",
                "app_secret": "v08-feishu-app-secret",
                "verification_token": "v08-feishu-verification-token",
            },
            "mochat": {"claw_token": "v08-mochat-claw-token"},
        },
        "send_progress": True,
    }
    _write(channels_path, json.dumps(channels_fixture))

    google_path = project / ".suxiaoyou" / "google-tokens.json"
    google_fixture = {
        "access_token": "v08-google-access-token",
        "refresh_token": "v08-google-refresh-token",
        "expires_at": 1_900_000_000,
        "token_type": "Bearer",
    }
    _write(google_path, json.dumps(google_fixture))

    mcp_legacy_path = project / ".suxiaoyou" / "mcp-tokens.json"
    mcp_fixture = {
        "engineering:slack": {
            "access_token": "v08-mcp-access-token",
            "refresh_token": "v08-mcp-refresh-token",
            "expires_at": 1_900_000_000,
            "token_type": "Bearer",
        }
    }
    _write(mcp_legacy_path, json.dumps(mcp_fixture))

    remote_path = root / "data" / "remote_token.json"
    remote_fixture = {"token": "suxiaoyou_rt_v08-remote-token"}
    _write(remote_path, json.dumps(remote_fixture))

    weixin_path = root / "data" / "runtime" / "weixin" / "account.json"
    weixin_payload = json.dumps(
        {
            "token": "v08-weixin-token",
            "get_updates_buf": "v08-update-cursor",
            "context_tokens": {"user-1": "v08-context-token"},
            "base_url": "https://ilinkai.weixin.qq.com",
        },
        ensure_ascii=False,
    ).encode("utf-8")
    _write(weixin_path, weixin_payload)

    matrix_path = root / "data" / "matrix-store" / "session.json"
    matrix_payload = json.dumps(
        {"access_token": "v08-matrix-access-token", "device_id": "DEVICE"}
    ).encode("utf-8")
    _write(matrix_path, matrix_payload)

    whatsapp_path = (
        root / "data" / "runtime" / "whatsapp-auth" / "bridge-token"
    )
    whatsapp_payload = b"v08-whatsapp-bridge-token\n"
    _write(whatsapp_path, whatsapp_payload)

    report = migrate_legacy_credential_artifacts(
        data_root=root,
        project_dir=project,
        include_global_legacy=False,
        store=store,
    )

    assert report.changed_count >= 7
    assert not mcp_legacy_path.exists()

    persisted_channels = json.loads(channels_path.read_text(encoding="utf-8"))
    assert resolve_secret_tree(persisted_channels, store=store) == channels_fixture
    persisted_google = json.loads(google_path.read_text(encoding="utf-8"))
    assert resolve_secret_tree(persisted_google, store=store) == google_fixture
    persisted_remote = json.loads(remote_path.read_text(encoding="utf-8"))
    assert resolve_secret_tree(persisted_remote, store=store) == remote_fixture

    mcp_path = _mcp_path(root, project)
    persisted_mcp = json.loads(mcp_path.read_text(encoding="utf-8"))
    resolved_mcp = resolve_secret_tree(persisted_mcp, store=store)
    assert resolved_mcp["slack"]["access_token"] == "v08-mcp-access-token"
    assert resolved_mcp["slack"]["refresh_token"] == "v08-mcp-refresh-token"

    assert recover_archived_legacy_artifact(weixin_path, store=store) == weixin_payload
    assert recover_archived_legacy_artifact(matrix_path, store=store) == matrix_payload
    assert (
        recover_archived_legacy_artifact(whatsapp_path, store=store)
        == whatsapp_payload
    )
    for path in (
        channels_path,
        google_path,
        remote_path,
        mcp_path,
        weixin_path,
        matrix_path,
        whatsapp_path,
    ):
        _assert_owner_only(path)

    plaintext_secrets = (
        "v08-feishu-app-secret",
        "v08-feishu-verification-token",
        "v08-mochat-claw-token",
        "v08-google-access-token",
        "v08-google-refresh-token",
        "v08-mcp-access-token",
        "v08-mcp-refresh-token",
        "v08-remote-token",
        "v08-weixin-token",
        "v08-context-token",
        "v08-matrix-access-token",
        "v08-whatsapp-bridge-token",
    )
    on_disk = b"\n".join(_disk_snapshot(root, project).values()).decode(
        "utf-8", errors="replace"
    )
    for secret in plaintext_secrets:
        assert secret not in on_disk

    first_files = _disk_snapshot(root, project)
    first_native = dict(native.values)
    second = migrate_legacy_credential_artifacts(
        data_root=root,
        project_dir=project,
        include_global_legacy=False,
        store=store,
    )
    assert second.changed_count == 0
    assert _disk_snapshot(root, project) == first_files
    assert native.values == first_native


def test_unreadable_v08_structures_are_recoverably_archived(
    tmp_path: Path,
) -> None:
    root = tmp_path / "app-data"
    project = tmp_path / "workspace"
    native = _MemoryNativeBackend()
    store = CredentialStore(
        fallback_path=root / "data" / "credentials" / "fallback.json",
        native_backend=native,
    )
    channels_path = root / "data" / "channels.json"
    channels_payload = b'{"channels":{"feishu":{"app_secret":"truncated-v08-secret"}'
    _write(channels_path, channels_payload)
    mcp_path = project / ".suxiaoyou" / "mcp-tokens.json"
    mcp_payload = b"not-json-v08-mcp-secret"
    _write(mcp_path, mcp_payload)

    report = migrate_legacy_credential_artifacts(
        data_root=root,
        project_dir=project,
        include_global_legacy=False,
        store=store,
    )

    assert str(channels_path) in report.archived
    assert str(mcp_path) in report.archived
    assert recover_archived_legacy_artifact(channels_path, store=store) == channels_payload
    assert recover_archived_legacy_artifact(mcp_path, store=store) == mcp_payload
    for path in (channels_path, mcp_path):
        tombstone = json.loads(path.read_text(encoding="utf-8"))
        assert tombstone["format"] == ARCHIVE_FORMAT
        assert tombstone["status"] == "disabled-and-archived"
        assert "v08" not in path.read_text(encoding="utf-8")
        _assert_owner_only(path)


async def test_global_and_all_historical_workspaces_are_migrated_from_database(
    tmp_path: Path,
    session_factory,
) -> None:
    root = tmp_path / "app-data"
    home = tmp_path / "home"
    workspace_one = tmp_path / "workspace-one"
    workspace_two = tmp_path / "workspace-two"
    unsafe_private_workspace = root / "managed-workspace"
    native = _MemoryNativeBackend()
    store = CredentialStore(
        fallback_path=root / "data" / "credentials" / "fallback.json",
        native_backend=native,
    )

    global_google = home / ".suxiaoyou" / "google-tokens.json"
    global_mcp = home / ".suxiaoyou" / "mcp-tokens.json"
    _write(global_google, '{"access_token":"global-google-v08"}')
    _write(
        global_mcp,
        '{"global:slack":{"access_token":"global-mcp-v08","expires_at":1}}',
    )

    for index, workspace in enumerate((workspace_one, workspace_two), start=1):
        _write(
            workspace / ".suxiaoyou" / "google-tokens.json",
            json.dumps({"access_token": f"workspace-{index}-google-v08"}),
        )
        _write(
            workspace / ".suxiaoyou" / "mcp-tokens.json",
            json.dumps(
                {
                    f"plugin{index}:slack": {
                        "access_token": f"workspace-{index}-mcp-v08",
                        "expires_at": index,
                    }
                }
            ),
        )
        _write(workspace / ".suxiaoyou" / "instructions.md", f"keep-{index}")
        _write(workspace / "ordinary-user-file.txt", f"ordinary-{index}")

    unsafe_google = (
        unsafe_private_workspace / ".suxiaoyou" / "google-tokens.json"
    )
    _write(unsafe_google, '{"access_token":"private-overlap-must-be-skipped"}')

    async with session_factory() as database:
        async with database.begin():
            database.add_all(
                [
                    Session(id="legacy-workspace-one", directory=str(workspace_one)),
                    # A duplicate directory proves de-duplication is stable.
                    Session(id="legacy-workspace-one-copy", directory=str(workspace_one)),
                    Session(id="folderless", directory="."),
                    Session(
                        id="unsafe-private-overlap",
                        directory=str(unsafe_private_workspace),
                    ),
                ]
            )

    global_report = migrate_legacy_credential_artifacts(
        data_root=root,
        project_dir=None,
        home_dir=home,
        include_global_legacy=True,
        store=store,
    )
    workspace_report = await migrate_historical_workspace_credentials(
        session_factory,
        # workspace-two is explicit settings state while workspace-one comes
        # only from historical Session.directory rows.
        configured_project_dir=workspace_two,
        private_data_root=root,
        store=store,
    )

    assert global_report.changed_count == 2
    assert workspace_report.changed_count == 4
    assert not global_mcp.exists()
    assert not (workspace_one / ".suxiaoyou" / "mcp-tokens.json").exists()
    assert not (workspace_two / ".suxiaoyou" / "mcp-tokens.json").exists()

    assert resolve_secret_tree(
        json.loads(global_google.read_text(encoding="utf-8")), store=store
    )["access_token"] == "global-google-v08"
    for index, workspace in enumerate((workspace_one, workspace_two), start=1):
        google = json.loads(
            (workspace / ".suxiaoyou" / "google-tokens.json").read_text(
                encoding="utf-8"
            )
        )
        assert resolve_secret_tree(google, store=store)["access_token"] == (
            f"workspace-{index}-google-v08"
        )
        mcp = resolve_secret_tree(
            json.loads(_mcp_path(root, workspace).read_text(encoding="utf-8")),
            store=store,
        )
        assert mcp["slack"]["access_token"] == f"workspace-{index}-mcp-v08"
        assert (
            workspace / ".suxiaoyou" / "instructions.md"
        ).read_text(encoding="utf-8") == f"keep-{index}"
        assert (workspace / "ordinary-user-file.txt").read_text(
            encoding="utf-8"
        ) == f"ordinary-{index}"

    global_mcp_data = resolve_secret_tree(
        json.loads(_mcp_path(root, None).read_text(encoding="utf-8")),
        store=store,
    )
    assert global_mcp_data["slack"]["access_token"] == "global-mcp-v08"
    assert "private-overlap-must-be-skipped" in unsafe_google.read_text(
        encoding="utf-8"
    )


def test_failed_protected_replacement_aborts_without_orphaning_new_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.auth import legacy_credentials

    root = tmp_path / "app-data"
    project = tmp_path / "workspace"
    native = _MemoryNativeBackend()
    store = CredentialStore(
        fallback_path=root / "data" / "credentials" / "fallback.json",
        native_backend=native,
    )
    channels_path = root / "data" / "channels.json"
    original = b'{"channels":{"feishu":{"app_secret":"must-remain-on-failure"}}}'
    _write(channels_path, original)

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(legacy_credentials, "atomic_write_text", fail_write)
    with pytest.raises(LegacyCredentialMigrationError, match="failed closed"):
        migrate_legacy_credential_artifacts(
            data_root=root,
            project_dir=project,
            include_global_legacy=False,
            store=store,
        )

    assert channels_path.read_bytes() == original
    assert native.values == {}


def test_legacy_symlink_is_rejected_fail_closed(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("symlink creation requires additional Windows privileges")
    root = tmp_path / "app-data"
    project = tmp_path / "workspace"
    outside = tmp_path / "outside-secret.json"
    _write(outside, '{"token":"outside-secret"}')
    channel_path = root / "data" / "channels.json"
    channel_path.parent.mkdir(parents=True)
    channel_path.symlink_to(outside)
    store = CredentialStore(
        fallback_path=root / "data" / "credentials" / "fallback.json",
        native_backend=_MemoryNativeBackend(),
    )

    with pytest.raises(LegacyCredentialMigrationError, match="symlink"):
        migrate_legacy_credential_artifacts(
            data_root=root,
            project_dir=project,
            include_global_legacy=False,
            store=store,
        )
    assert outside.read_text(encoding="utf-8") == '{"token":"outside-secret"}'
