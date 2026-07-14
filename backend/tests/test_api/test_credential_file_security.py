"""Credential fallback files are atomically installed with private modes."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from app.api import channels, google_auth
from app.auth import credential_store
from app.auth.credential_store import CredentialStore, is_credential_reference
from app.auth.credential_store import CredentialStoreError
from app.channels import config as channels_config
from app.channels.config import (
    ChannelsConfig,
    save_channels_config,
    save_channels_config_dict,
)


pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX mode semantics")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _fallback_credentials(store: CredentialStore) -> dict[str, str]:
    return json.loads(store.fallback_path.read_text(encoding="utf-8"))["credentials"]


@pytest.fixture
def private_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> CredentialStore:
    store = CredentialStore(
        fallback_path=tmp_path / "credential-fallback.json",
        native_backend=None,
    )
    monkeypatch.setattr(credential_store, "get_credential_store", lambda: store)
    return store


def test_google_oauth_tokens_are_references_and_owner_only(
    tmp_path: Path,
    private_store: CredentialStore,
) -> None:
    google_auth._save_tokens(str(tmp_path), {"access_token": "secret"})

    path = tmp_path / ".suxiaoyou" / "google-tokens.json"
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert is_credential_reference(persisted["access_token"])
    assert '"secret"' not in path.read_text(encoding="utf-8")
    assert google_auth.load_google_tokens(str(tmp_path)) == {"access_token": "secret"}
    assert _mode(path) == 0o600


def test_google_plaintext_migration_failure_is_fail_closed(
    tmp_path: Path,
    private_store: CredentialStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / ".suxiaoyou" / "google-tokens.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"access_token":"keep-plaintext"}', encoding="utf-8")

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(google_auth, "atomic_write_text", fail_write)
    with pytest.raises(CredentialStoreError, match="Cannot erase plaintext"):
        google_auth.load_google_tokens(str(tmp_path))
    assert "keep-plaintext" in path.read_text(encoding="utf-8")
    assert _fallback_credentials(private_store) == {}


def test_google_token_write_failure_preserves_old_config_without_orphans(
    tmp_path: Path,
    private_store: CredentialStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    google_auth._save_tokens(str(tmp_path), {"access_token": "old-secret"})
    path = tmp_path / ".suxiaoyou" / "google-tokens.json"
    previous_file = path.read_text(encoding="utf-8")
    previous_credentials = _fallback_credentials(private_store)

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(google_auth, "atomic_write_text", fail_write)
    with pytest.raises(OSError, match="disk full"):
        google_auth._save_tokens(str(tmp_path), {"access_token": "new-secret"})

    assert path.read_text(encoding="utf-8") == previous_file
    assert _fallback_credentials(private_store) == previous_credentials


def test_google_token_serialization_failure_leaves_no_orphans(
    tmp_path: Path,
    private_store: CredentialStore,
) -> None:
    with pytest.raises(UnicodeEncodeError):
        google_auth._save_tokens(
            str(tmp_path),
            {"access_token": "new-secret", "scope": "\ud800"},
        )

    path = tmp_path / ".suxiaoyou" / "google-tokens.json"
    assert not path.exists()
    assert _fallback_credentials(private_store) == {}


def test_google_disconnect_deletes_metadata_then_references(
    tmp_path: Path,
    private_store: CredentialStore,
) -> None:
    google_auth._save_tokens(
        str(tmp_path),
        {"access_token": "old-access", "refresh_token": "old-refresh"},
    )

    google_auth.delete_google_tokens(str(tmp_path))

    path = tmp_path / ".suxiaoyou" / "google-tokens.json"
    assert not path.exists()
    assert _fallback_credentials(private_store) == {}


def test_google_disconnect_invalidates_pending_authorization_without_metadata(
    tmp_path: Path,
    private_store: CredentialStore,
) -> None:
    project_dir = str(tmp_path)
    scope = google_auth._credential_namespace(project_dir)
    state = "pending-test-state"
    generation = google_auth._auth_generations.get(scope, 0)
    google_auth._pending_states[state] = {
        "redirect_uri": "http://localhost/callback",
        "project_dir": project_dir,
        "scope": scope,
        "generation": generation,
    }

    google_auth.delete_google_tokens(project_dir)

    assert state not in google_auth._pending_states
    assert google_auth._auth_generations[scope] == generation + 1


def test_google_generation_cas_removes_reentrant_stale_callback_write(
    tmp_path: Path,
    private_store: CredentialStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir = str(tmp_path)
    scope = google_auth._credential_namespace(project_dir)
    generation = google_auth._auth_generations.get(scope, 0)
    real_save = google_auth._save_tokens

    def save_then_disconnect(project: str | None, tokens: dict[str, object]) -> None:
        real_save(project, tokens)
        google_auth.fence_google_auth_disconnect(project)

    monkeypatch.setattr(google_auth, "_save_tokens", save_then_disconnect)

    committed = google_auth._commit_google_tokens_for_generation(
        project_dir,
        {"access_token": "stale-access", "refresh_token": "stale-refresh"},
        scope=scope,
        generation=generation,
    )

    assert committed is False
    assert not google_auth._get_token_path(project_dir).exists()
    assert _fallback_credentials(private_store) == {}


def test_google_disconnect_failure_preserves_metadata_and_references(
    tmp_path: Path,
    private_store: CredentialStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    google_auth._save_tokens(str(tmp_path), {"access_token": "keep-secret"})
    path = tmp_path / ".suxiaoyou" / "google-tokens.json"
    previous_file = path.read_bytes()
    previous_credentials = _fallback_credentials(private_store)
    real_unlink = Path.unlink

    def fail_token_unlink(target: Path, *args, **kwargs):
        if target == path:
            raise OSError("metadata busy")
        return real_unlink(target, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_token_unlink)
    with pytest.raises(OSError, match="metadata busy"):
        google_auth.delete_google_tokens(str(tmp_path))

    assert path.read_bytes() == previous_file
    assert _fallback_credentials(private_store) == previous_credentials


def test_google_plaintext_is_erased_on_first_successful_load(
    tmp_path: Path,
    private_store: CredentialStore,
) -> None:
    path = tmp_path / ".suxiaoyou" / "google-tokens.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"access_token":"legacy-access","refresh_token":"legacy-refresh"}',
        encoding="utf-8",
    )

    loaded = google_auth.load_google_tokens(str(tmp_path))

    assert loaded is not None
    assert loaded["access_token"] == "legacy-access"
    persisted = path.read_text(encoding="utf-8")
    assert "legacy-access" not in persisted
    assert "legacy-refresh" not in persisted
    assert "suxiaoyou-credential://" in persisted


def test_channel_api_secret_config_is_owner_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    private_store: CredentialStore,
) -> None:
    path = tmp_path / "channels.json"
    monkeypatch.setattr(channels, "_get_channels_config_path", lambda: path)

    channels._save_config_dict(
        {"channels": {"feishu": {"app_secret": "secret"}}}
    )

    persisted = json.loads(path.read_text(encoding="utf-8"))["channels"]["feishu"]
    assert is_credential_reference(persisted["app_secret"])
    assert '"secret"' not in path.read_text(encoding="utf-8")
    assert channels._load_config_dict()["channels"]["feishu"]["app_secret"] == "secret"
    assert _mode(path) == 0o600


def test_channel_config_writer_hardens_existing_file(
    tmp_path: Path,
    private_store: CredentialStore,
) -> None:
    path = tmp_path / "channels.json"
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o644)

    save_channels_config(
        ChannelsConfig(channels={"feishu": {"app_secret": "secret"}}),
        path,
    )

    assert _mode(path) == 0o600
    assert '"secret"' not in path.read_text(encoding="utf-8")


def test_channel_plaintext_is_erased_on_first_successful_load(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    private_store: CredentialStore,
) -> None:
    path = tmp_path / "channels.json"
    path.write_text(
        '{"channels":{"feishu":{"app_secret":"legacy-channel-secret"}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(channels, "_get_channels_config_path", lambda: path)

    loaded = channels._load_config_dict()

    assert loaded["channels"]["feishu"]["app_secret"] == "legacy-channel-secret"
    persisted = path.read_text(encoding="utf-8")
    assert "legacy-channel-secret" not in persisted
    assert "suxiaoyou-credential://" in persisted


def test_channel_write_failure_preserves_old_config_without_orphans(
    tmp_path: Path,
    private_store: CredentialStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "channels.json"
    save_channels_config_dict(
        {"channels": {"feishu": {"app_secret": "old-secret"}}},
        path,
    )
    previous_file = path.read_text(encoding="utf-8")
    previous_credentials = _fallback_credentials(private_store)

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(channels_config, "atomic_write_text", fail_write)
    with pytest.raises(OSError, match="disk full"):
        save_channels_config_dict(
            {"channels": {"feishu": {"app_secret": "new-secret"}}},
            path,
        )

    assert path.read_text(encoding="utf-8") == previous_file
    assert _fallback_credentials(private_store) == previous_credentials


def test_channel_serialization_failure_leaves_no_orphans(
    tmp_path: Path,
    private_store: CredentialStore,
) -> None:
    path = tmp_path / "channels.json"

    with pytest.raises(UnicodeEncodeError):
        save_channels_config_dict(
            {
                "channels": {
                    "feishu": {
                        "app_secret": "new-secret",
                        "label": "\ud800",
                    }
                }
            },
            path,
        )

    assert not path.exists()
    assert _fallback_credentials(private_store) == {}
