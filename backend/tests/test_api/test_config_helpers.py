"""Tests for app.api.config — pure helper functions."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from dotenv import dotenv_values
from fastapi import HTTPException
from starlette.requests import Request

from app.api import config
from app.api.config import _mask_key, _remove_env_key, _update_env_file
from app.auth import credential_store
from app.auth.credential_store import CredentialStore
from app.schemas.provider import (
    ApiKeyUpdate,
    CustomEndpointUpdate,
    ProviderKeyUpdate,
)


class _FakeProvider:
    def __init__(self, provider_id: str, api_key: str = "") -> None:
        self.id = provider_id
        self._api_key = api_key

    async def list_models(self) -> list[object]:
        return [object()]


class _FakeRegistry:
    def __init__(self, provider: _FakeProvider | None = None) -> None:
        self.providers = {provider.id: provider} if provider is not None else {}
        self._full_models: list[tuple[object, object]] = []
        self.fail_register = False

    def get_provider(self, provider_id: str) -> _FakeProvider | None:
        return self.providers.get(provider_id)

    def register(self, provider: _FakeProvider) -> None:
        if self.fail_register:
            raise RuntimeError("runtime registry rejected update")
        self.providers[provider.id] = provider

    def unregister(self, provider_id: str) -> None:
        self.providers.pop(provider_id, None)

    async def refresh_models(self) -> dict[str, list[object]]:
        return {}

    async def refresh_provider(self, _provider_id: str) -> list[object]:
        return []


def _request() -> Request:
    return Request({"type": "http", "headers": []})


def _fallback_credentials(path: Path) -> dict[str, str]:
    return json.loads(path.read_text(encoding="utf-8"))["credentials"]


def _fail_env_write(*_args: object, **_kwargs: object) -> None:
    raise OSError("disk full")


class TestMaskKey:
    def test_long_key(self):
        result = _mask_key("sk-or-v1-abcdefghijklmnop")
        assert result.startswith("sk-or-v")
        assert result.endswith("mnop")
        assert "..." in result

    def test_short_key(self):
        assert _mask_key("short") == "****"

    def test_boundary_11_chars(self):
        assert _mask_key("12345678901") == "****"  # exactly 11


class TestUpdateEnvFile:
    def test_adds_new_key(self, tmp_path: Path):
        env = tmp_path / ".env"
        with patch("app.api.config._ENV_PATH", env):
            _update_env_file("NEW_KEY", "new_value")
        assert "NEW_KEY='new_value'" in env.read_text()

    def test_updates_existing_key(self, tmp_path: Path):
        env = tmp_path / ".env"
        env.write_text("MY_KEY='old'\nOTHER='keep'\n")
        with patch("app.api.config._ENV_PATH", env):
            _update_env_file("MY_KEY", "new")
        text = env.read_text()
        assert "MY_KEY='new'" in text
        assert "OTHER='keep'" in text
        assert "'old'" not in text

    def test_handles_spaces(self, tmp_path: Path):
        env = tmp_path / ".env"
        env.write_text("MY_KEY = old_val\n")
        with patch("app.api.config._ENV_PATH", env):
            _update_env_file("MY_KEY", "new_val")
        assert "MY_KEY='new_val'" in env.read_text()

    def test_quotes_json_with_hash(self, tmp_path: Path):
        """Values containing # must be quoted to prevent dotenv comment truncation."""
        env = tmp_path / ".env"
        json_val = '[{"url":"https://example.com/#/v1"}]'
        with patch("app.api.config._ENV_PATH", env):
            _update_env_file("ENDPOINTS", json_val)
        text = env.read_text()
        assert json_val in text  # full value preserved, not truncated at #

    def test_escapes_single_quotes(self, tmp_path: Path):
        env = tmp_path / ".env"
        with patch("app.api.config._ENV_PATH", env):
            _update_env_file("KEY", "it's a value")
        assert dotenv_values(env)["KEY"] == "it's a value"

    def test_round_trips_backslashes_and_single_quotes(self, tmp_path: Path):
        env = tmp_path / ".env"
        value = r"C:\users\new\O'Brien"
        with patch("app.api.config._ENV_PATH", env):
            _update_env_file("KEY", value)
        assert dotenv_values(env)["KEY"] == value

    def test_collapses_all_dotenv_assignment_forms(self, tmp_path: Path):
        env = tmp_path / ".env"
        env.write_text(
            "  export KEY = 'stale-first'\n"
            "OTHER='keep'\n"
            "\tKEY='stale-last'\n",
            encoding="utf-8",
        )

        with patch("app.api.config._ENV_PATH", env):
            _update_env_file("KEY", "current")

        persisted = env.read_text(encoding="utf-8")
        assert dotenv_values(env)["KEY"] == "current"
        assert "stale-first" not in persisted
        assert "stale-last" not in persisted
        assert "OTHER='keep'" in persisted

    @pytest.mark.skipif(os.name == "nt", reason="POSIX mode semantics")
    def test_hardens_new_and_existing_env_file_to_owner_only(self, tmp_path: Path):
        env = tmp_path / ".env"
        env.write_text("OLD='secret'\n", encoding="utf-8")
        env.chmod(0o644)

        with patch("app.api.config._ENV_PATH", env):
            _update_env_file("NEW", "secret")

        assert stat.S_IMODE(env.stat().st_mode) == 0o600

    def test_provider_key_is_persisted_as_reference(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        env = tmp_path / ".env"
        store = CredentialStore(
            fallback_path=tmp_path / "fallback.json",
            native_backend=None,
        )
        monkeypatch.setattr(credential_store, "get_credential_store", lambda: store)

        with patch("app.api.config._ENV_PATH", env):
            _update_env_file("SUXIAOYOU_OPENAI_API_KEY", "sk-plain")

        persisted = env.read_text(encoding="utf-8")
        assert "sk-plain" not in persisted
        assert "suxiaoyou-credential://" in persisted

    def test_committed_rotation_recovers_deferred_native_cleanup(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class NativeBackend:
            def __init__(self) -> None:
                self.values: dict[tuple[str, str], str] = {}
                self.fail_deletes = False

            def get_password(self, service: str, username: str) -> str | None:
                return self.values.get((service, username))

            def set_password(self, service: str, username: str, password: str) -> None:
                self.values[(service, username)] = password

            def delete_password(self, service: str, username: str) -> None:
                if self.fail_deletes:
                    raise RuntimeError("vault locked")
                self.values.pop((service, username), None)

        env = tmp_path / ".env"
        fallback = tmp_path / "fallback.json"
        native = NativeBackend()
        store = CredentialStore(fallback_path=fallback, native_backend=native)
        monkeypatch.setattr(credential_store, "get_credential_store", lambda: store)
        with patch("app.api.config._ENV_PATH", env):
            _update_env_file("SUXIAOYOU_OPENAI_API_KEY", "old-secret")
        old_reference = str(dotenv_values(env)["SUXIAOYOU_OPENAI_API_KEY"])

        native.fail_deletes = True
        real_atomic_write = credential_store.atomic_write_text
        failed_activation = False

        def fail_cleanup_activation(path, content, **kwargs):
            nonlocal failed_activation
            if Path(path) == fallback:
                payload = json.loads(content)
                if (
                    not failed_activation
                    and payload.get("pending_native_deletions")
                    and not payload.get("cleanup_transactions")
                ):
                    failed_activation = True
                    raise OSError("cleanup journal temporarily full")
            return real_atomic_write(path, content, **kwargs)

        monkeypatch.setattr(
            credential_store,
            "atomic_write_text",
            fail_cleanup_activation,
        )
        with patch("app.api.config._ENV_PATH", env):
            _update_env_file("SUXIAOYOU_OPENAI_API_KEY", "new-secret")

        # The config operation is truthfully successful, while its prepared
        # evidence record remains durable for recovery.
        new_reference = str(dotenv_values(env)["SUXIAOYOU_OPENAI_API_KEY"])
        journal = json.loads(fallback.read_text(encoding="utf-8"))
        assert journal["cleanup_transactions"]
        assert "new-secret" in native.values.values()

        monkeypatch.setattr(credential_store, "atomic_write_text", real_atomic_write)
        native.fail_deletes = False
        recovered = CredentialStore(fallback_path=fallback, native_backend=native)
        assert recovered.get(old_reference) is None
        assert recovered.resolve(new_reference) == "new-secret"
        assert json.loads(fallback.read_text(encoding="utf-8"))[
            "cleanup_transactions"
        ] == {}

    def test_failed_key_rotation_keeps_installed_reference_unchanged(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        env = tmp_path / ".env"
        store = CredentialStore(
            fallback_path=tmp_path / "fallback.json",
            native_backend=None,
        )
        monkeypatch.setattr(credential_store, "get_credential_store", lambda: store)
        with patch("app.api.config._ENV_PATH", env):
            _update_env_file("SUXIAOYOU_OPENAI_API_KEY", "old-secret")
            old_reference = str(dotenv_values(env)["SUXIAOYOU_OPENAI_API_KEY"])
            old_credentials = _fallback_credentials(store.fallback_path)

            def fail_write(*_args, **_kwargs):
                raise OSError("disk full")

            monkeypatch.setattr("app.api.config.atomic_write_text", fail_write)
            with pytest.raises(OSError, match="disk full"):
                _update_env_file("SUXIAOYOU_OPENAI_API_KEY", "new-secret")

        assert dotenv_values(env)["SUXIAOYOU_OPENAI_API_KEY"] == old_reference
        assert store.resolve(old_reference) == "old-secret"
        assert _fallback_credentials(store.fallback_path) == old_credentials

    def test_successful_key_rotation_cleans_old_reference_after_install(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        env = tmp_path / ".env"
        store = CredentialStore(
            fallback_path=tmp_path / "fallback.json",
            native_backend=None,
        )
        monkeypatch.setattr(credential_store, "get_credential_store", lambda: store)

        with patch("app.api.config._ENV_PATH", env):
            _update_env_file("SUXIAOYOU_OPENAI_API_KEY", "old-secret")
            old_reference = str(dotenv_values(env)["SUXIAOYOU_OPENAI_API_KEY"])
            _update_env_file("SUXIAOYOU_OPENAI_API_KEY", "new-secret")
            new_reference = str(dotenv_values(env)["SUXIAOYOU_OPENAI_API_KEY"])

        assert new_reference != old_reference
        assert store.resolve(new_reference) == "new-secret"
        assert store.get(old_reference) is None


class TestProviderConfigTransactions:
    @pytest.mark.asyncio
    async def test_openrouter_write_failure_keeps_runtime_and_credentials_unchanged(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        env = tmp_path / ".env"
        fallback = tmp_path / "fallback.json"
        store = CredentialStore(fallback_path=fallback, native_backend=None)
        monkeypatch.setattr(credential_store, "get_credential_store", lambda: store)
        old_provider = _FakeProvider("openrouter", "old-secret")
        registry = _FakeRegistry(old_provider)
        settings = SimpleNamespace(openrouter_api_key="old-secret")

        with patch("app.api.config._ENV_PATH", env):
            _update_env_file("SUXIAOYOU_OPENROUTER_API_KEY", "old-secret")
            old_env = env.read_text(encoding="utf-8")
            old_credentials = _fallback_credentials(fallback)
            monkeypatch.setattr(
                config,
                "OpenRouterProvider",
                lambda api_key: _FakeProvider("openrouter", api_key),
            )
            monkeypatch.setattr(
                config,
                "atomic_write_text",
                _fail_env_write,
            )

            with pytest.raises(OSError, match="disk full"):
                await config.update_api_key(
                    settings,
                    registry,
                    ApiKeyUpdate(api_key="new-secret"),
                )

        assert settings.openrouter_api_key == "old-secret"
        assert registry.get_provider("openrouter") is old_provider
        assert env.read_text(encoding="utf-8") == old_env
        assert _fallback_credentials(fallback) == old_credentials

    @pytest.mark.asyncio
    async def test_generic_runtime_failure_restores_env_settings_registry_and_store(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        env = tmp_path / ".env"
        fallback = tmp_path / "fallback.json"
        store = CredentialStore(fallback_path=fallback, native_backend=None)
        monkeypatch.setattr(credential_store, "get_credential_store", lambda: store)
        old_provider = _FakeProvider("deepseek", "old-secret")
        registry = _FakeRegistry(old_provider)
        settings = SimpleNamespace(deepseek_api_key="old-secret")

        with patch("app.api.config._ENV_PATH", env):
            _update_env_file("SUXIAOYOU_DEEPSEEK_API_KEY", "old-secret")
            old_env = env.read_text(encoding="utf-8")
            old_credentials = _fallback_credentials(fallback)
            monkeypatch.setattr(
                config,
                "create_desktop_provider",
                lambda provider_id, api_key, **_kwargs: _FakeProvider(
                    provider_id,
                    api_key,
                ),
            )
            registry.fail_register = True

            with pytest.raises(RuntimeError, match="runtime registry rejected"):
                await config.set_provider_key(
                    "deepseek",
                    ProviderKeyUpdate(api_key="new-secret"),
                    _request(),
                    settings,
                    registry,
                )

        assert settings.deepseek_api_key == "old-secret"
        assert registry.get_provider("deepseek") is old_provider
        assert env.read_text(encoding="utf-8") == old_env
        assert _fallback_credentials(fallback) == old_credentials

    @pytest.mark.asyncio
    async def test_custom_write_failure_does_not_publish_settings_or_new_refs(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        env = tmp_path / ".env"
        fallback = tmp_path / "fallback.json"
        store = CredentialStore(fallback_path=fallback, native_backend=None)
        monkeypatch.setattr(credential_store, "get_credential_store", lambda: store)
        old_config = json.dumps(
            [
                {
                    "id": "custom_demo",
                    "slug": "demo",
                    "name": "Demo",
                    "base_url": "https://example.com/v1",
                    "api_key": "old-secret",
                    "enabled": True,
                    "models": [{"id": "demo-model", "name": None}],
                    "headers": {"Authorization": "Bearer old-header"},
                }
            ]
        )
        settings = SimpleNamespace(custom_endpoints=old_config)
        old_provider = _FakeProvider("custom_demo", "old-secret")
        registry = _FakeRegistry(old_provider)

        with patch("app.api.config._ENV_PATH", env):
            _update_env_file("SUXIAOYOU_CUSTOM_ENDPOINTS", old_config)
            old_env = env.read_text(encoding="utf-8")
            old_credentials = _fallback_credentials(fallback)
            monkeypatch.setattr(
                config,
                "atomic_write_text",
                _fail_env_write,
            )

            with pytest.raises(OSError, match="disk full"):
                await config.update_custom_endpoint(
                    "custom_demo",
                    CustomEndpointUpdate(name="Renamed"),
                    settings,
                    registry,
                )

        assert settings.custom_endpoints == old_config
        assert registry.get_provider("custom_demo") is old_provider
        assert env.read_text(encoding="utf-8") == old_env
        assert _fallback_credentials(fallback) == old_credentials

    @pytest.mark.asyncio
    async def test_custom_success_installs_config_before_publishing_settings(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        env = tmp_path / ".env"
        fallback = tmp_path / "fallback.json"
        store = CredentialStore(fallback_path=fallback, native_backend=None)
        monkeypatch.setattr(credential_store, "get_credential_store", lambda: store)
        old_config = json.dumps(
            [
                {
                    "id": "custom_demo",
                    "slug": "demo",
                    "name": "Demo",
                    "base_url": "https://example.com/v1",
                    "api_key": "old-secret",
                    "enabled": True,
                    "models": [{"id": "demo-model", "name": None}],
                    "headers": {},
                }
            ]
        )
        settings = SimpleNamespace(custom_endpoints=old_config)
        provider = _FakeProvider("custom_demo", "old-secret")
        registry = _FakeRegistry(provider)

        with patch("app.api.config._ENV_PATH", env):
            _update_env_file("SUXIAOYOU_CUSTOM_ENDPOINTS", old_config)
            await config.update_custom_endpoint(
                "custom_demo",
                CustomEndpointUpdate(name="Renamed"),
                settings,
                registry,
            )

        assert json.loads(settings.custom_endpoints)[0]["name"] == "Renamed"
        persisted = str(dotenv_values(env)["SUXIAOYOU_CUSTOM_ENDPOINTS"])
        assert "old-secret" not in persisted
        assert "suxiaoyou-credential://" in persisted
        assert registry.get_provider("custom_demo") is provider

    @pytest.mark.asyncio
    async def test_stale_custom_patch_cannot_overwrite_a_concurrent_commit(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        env = tmp_path / ".env"
        store = CredentialStore(
            fallback_path=tmp_path / "fallback.json",
            native_backend=None,
        )
        monkeypatch.setattr(credential_store, "get_credential_store", lambda: store)
        original = {
            "id": "custom_demo",
            "slug": "demo",
            "name": "Demo",
            "base_url": "https://old.example/v1",
            "api_key": "old-secret",
            "enabled": True,
            "models": [{"id": "demo-model", "name": None}],
            "headers": {},
        }
        settings = SimpleNamespace(custom_endpoints=json.dumps([original]))
        registry = _FakeRegistry(_FakeProvider("custom_demo", "old-secret"))
        validation_started = asyncio.Event()
        finish_validation = asyncio.Event()

        class _SlowProvider(_FakeProvider):
            async def list_models(self) -> list[object]:
                validation_started.set()
                await finish_validation.wait()
                return [object()]

        monkeypatch.setattr(
            config,
            "create_desktop_provider",
            lambda provider_id, api_key, **_kwargs: _SlowProvider(
                provider_id,
                api_key,
            ),
        )

        with patch("app.api.config._ENV_PATH", env):
            _update_env_file("SUXIAOYOU_CUSTOM_ENDPOINTS", settings.custom_endpoints)
            slow = asyncio.create_task(
                config.update_custom_endpoint(
                    "custom_demo",
                    CustomEndpointUpdate(base_url="https://slow.example/v1"),
                    settings,
                    registry,
                )
            )
            await validation_started.wait()
            await config.update_custom_endpoint(
                "custom_demo",
                CustomEndpointUpdate(name="Renamed"),
                settings,
                registry,
            )
            finish_validation.set()
            with pytest.raises(HTTPException) as exc:
                await slow

        assert exc.value.status_code == 409
        persisted = json.loads(settings.custom_endpoints)[0]
        assert persisted["name"] == "Renamed"
        assert persisted["base_url"] == "https://old.example/v1"


class TestRemoveEnvKey:
    def test_removes_existing(self, tmp_path: Path):
        env = tmp_path / ".env"
        env.write_text("KEY1=val1\nKEY2=val2\n")
        with patch("app.api.config._ENV_PATH", env):
            _remove_env_key("KEY1")
        text = env.read_text()
        assert "KEY1" not in text
        assert "KEY2=val2" in text

    def test_noop_missing_key(self, tmp_path: Path):
        env = tmp_path / ".env"
        env.write_text("OTHER=val\n")
        with patch("app.api.config._ENV_PATH", env):
            _remove_env_key("MISSING")
        assert "OTHER=val" in env.read_text()

    def test_noop_missing_file(self, tmp_path: Path):
        env = tmp_path / ".env"
        with patch("app.api.config._ENV_PATH", env):
            _remove_env_key("KEY")  # should not raise

    def test_removes_exported_indented_and_duplicate_assignments(self, tmp_path: Path):
        env = tmp_path / ".env"
        env.write_text(
            " export KEY = 'first'\n\tKEY='second'\nOTHER='keep'\n",
            encoding="utf-8",
        )

        with patch("app.api.config._ENV_PATH", env):
            _remove_env_key("KEY")

        assert "KEY" not in env.read_text(encoding="utf-8")
        assert dotenv_values(env)["OTHER"] == "keep"
