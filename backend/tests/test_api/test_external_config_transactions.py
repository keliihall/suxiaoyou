"""Transactional config tests for active local/provider writers."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from dotenv import dotenv_values

from app.api import config, ollama, openai_auth, rapid_mlx
from app.auth import credential_store
from app.auth.credential_store import CredentialStore
from app.provider import ollama as ollama_provider
from app.provider import openai_oauth
from app.provider import rapid_mlx as rapid_mlx_provider
from app.provider.openai_subscription import OpenAISubscriptionProvider


class _FakeProvider:
    def __init__(self, provider_id: str) -> None:
        self.id = provider_id


class _FakeOllamaProvider(_FakeProvider):
    def __init__(self, base_url: str) -> None:
        super().__init__("ollama")
        self._base_url = base_url


class _FakeRapidMLXProvider(_FakeProvider):
    def __init__(self, base_url: str, *, seed_model: str) -> None:
        super().__init__("rapid-mlx")
        self._base_url = base_url
        self._seed_model = seed_model


class _FakeRegistry:
    def __init__(self, provider: _FakeProvider | None = None) -> None:
        self.providers = {provider.id: provider} if provider is not None else {}
        self.fail_register = False

    def get_provider(self, provider_id: str):
        return self.providers.get(provider_id)

    def register(self, provider) -> None:
        if self.fail_register:
            raise RuntimeError("runtime registry rejected update")
        self.providers[provider.id] = provider

    def unregister(self, provider_id: str) -> None:
        self.providers.pop(provider_id, None)

    async def refresh_models(self) -> dict[str, list[object]]:
        return {}

    async def refresh_provider(self, _provider_id: str) -> list[object]:
        return []


class _RapidMLXManager:
    async def start(self, *, model: str, port: int) -> str:
        assert model == "new-model"
        assert port == 18081
        return "http://127.0.0.1:18081/v1"

    async def status(
        self,
        *,
        configured_base_url: str,
        configured_model: str,
    ) -> dict[str, object]:
        assert configured_base_url == "http://127.0.0.1:18081/v1"
        assert configured_model == "new-model"
        return {
            "platform_supported": True,
            "binary_installed": True,
            "running": True,
            "process_running": True,
            "port": 18081,
            "base_url": configured_base_url,
            "version": "test",
            "current_model": configured_model,
            "executable_path": None,
            "install_commands": [],
        }


def _fallback_text(path: Path) -> str | None:
    return path.read_text(encoding="utf-8") if path.exists() else None


def _install_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> CredentialStore:
    store = CredentialStore(
        fallback_path=tmp_path / "fallback.json",
        native_backend=None,
    )
    monkeypatch.setattr(credential_store, "get_credential_store", lambda: store)
    return store


def _openai_settings() -> SimpleNamespace:
    return SimpleNamespace(
        openai_oauth_access_token="old-access",
        openai_oauth_refresh_token="old-refresh",
        openai_oauth_account_id="old-account",
        openai_oauth_expires_at=123,
        openai_oauth_email="old@example.com",
    )


def _seed_openai_env() -> dict[str, str]:
    values = {
        "SUXIAOYOU_OPENAI_OAUTH_ACCESS_TOKEN": "old-access",
        "SUXIAOYOU_OPENAI_OAUTH_REFRESH_TOKEN": "old-refresh",
        "SUXIAOYOU_OPENAI_OAUTH_ACCOUNT_ID": "old-account",
        "SUXIAOYOU_OPENAI_OAUTH_EXPIRES_AT": "123",
        "SUXIAOYOU_OPENAI_OAUTH_EMAIL": "old@example.com",
    }
    config.persist_env_transaction(values, lambda: None, lambda: None)
    return values


@pytest.mark.asyncio
async def test_oauth_completion_registry_failure_restores_every_layer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = tmp_path / ".env"
    store = _install_store(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "_ENV_PATH", env)
    _seed_openai_env()
    old_env = env.read_text(encoding="utf-8")
    old_fallback = _fallback_text(store.fallback_path)

    settings = _openai_settings()
    old_provider = _FakeProvider(openai_auth.PROVIDER_ID)
    registry = _FakeRegistry(old_provider)
    registry.fail_register = True

    async def exchange_code(**_kwargs: object) -> dict[str, object]:
        return {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "id_token": "new-id-token",
            "expires_in": 3600,
        }

    monkeypatch.setattr(openai_auth, "exchange_code", exchange_code)
    monkeypatch.setattr(openai_auth, "extract_account_id", lambda _token: "new-account")
    monkeypatch.setattr(openai_auth, "extract_email", lambda _token: "new@example.com")
    monkeypatch.setattr(openai_auth, "get_settings", lambda: settings)
    monkeypatch.setattr(openai_auth, "get_provider_registry", lambda: registry)
    state = "transaction-test"
    openai_auth._pending_flows[state] = {
        "redirect_uri": "http://127.0.0.1/callback",
        "code_verifier": "verifier",
    }

    with pytest.raises(RuntimeError, match="runtime registry rejected"):
        await openai_auth._complete_oauth_flow_internal("code", state)

    assert settings.openai_oauth_access_token == "old-access"
    assert settings.openai_oauth_refresh_token == "old-refresh"
    assert settings.openai_oauth_account_id == "old-account"
    assert settings.openai_oauth_expires_at == 123
    assert settings.openai_oauth_email == "old@example.com"
    assert registry.get_provider(openai_auth.PROVIDER_ID) is old_provider
    assert env.read_text(encoding="utf-8") == old_env
    assert _fallback_text(store.fallback_path) == old_fallback


@pytest.mark.asyncio
async def test_token_refresh_write_failure_does_not_mutate_provider_or_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = tmp_path / ".env"
    store = _install_store(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "_ENV_PATH", env)
    _seed_openai_env()
    old_env = env.read_text(encoding="utf-8")
    old_fallback = _fallback_text(store.fallback_path)
    settings = _openai_settings()
    provider = OpenAISubscriptionProvider(
        access_token="old-access",
        account_id="old-account",
        refresh_token="old-refresh",
        expires_at_ms=123,
        settings=settings,
    )

    async def refresh_access_token(_refresh_token: str) -> dict[str, object]:
        return {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
        }

    def fail_env_write(*_args: object, **_kwargs: object) -> None:
        assert provider._access_token == "old-access"
        assert provider._refresh_token == "old-refresh"
        assert provider._expires_at_ms == 123
        assert settings.openai_oauth_access_token == "old-access"
        assert settings.openai_oauth_refresh_token == "old-refresh"
        assert settings.openai_oauth_expires_at == 123
        raise OSError("disk full")

    monkeypatch.setattr(openai_oauth, "refresh_access_token", refresh_access_token)
    monkeypatch.setattr(config, "atomic_write_text", fail_env_write)

    with pytest.raises(OSError, match="disk full"):
        await provider._do_refresh()

    assert provider._access_token == "old-access"
    assert provider._refresh_token == "old-refresh"
    assert provider._expires_at_ms == 123
    assert provider._needs_reauth is False
    assert settings.openai_oauth_access_token == "old-access"
    assert settings.openai_oauth_refresh_token == "old-refresh"
    assert settings.openai_oauth_expires_at == 123
    assert env.read_text(encoding="utf-8") == old_env
    assert _fallback_text(store.fallback_path) == old_fallback


@pytest.mark.asyncio
async def test_token_refresh_installs_once_then_retires_old_references(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = tmp_path / ".env"
    store = _install_store(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "_ENV_PATH", env)
    _seed_openai_env()
    old_access_ref = str(
        dotenv_values(env)["SUXIAOYOU_OPENAI_OAUTH_ACCESS_TOKEN"]
    )
    old_refresh_ref = str(
        dotenv_values(env)["SUXIAOYOU_OPENAI_OAUTH_REFRESH_TOKEN"]
    )
    settings = _openai_settings()
    provider = OpenAISubscriptionProvider(
        access_token="old-access",
        account_id="old-account",
        refresh_token="old-refresh",
        expires_at_ms=123,
        settings=settings,
    )

    async def refresh_access_token(_refresh_token: str) -> dict[str, object]:
        return {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
        }

    env_writes = 0
    atomic_write_text = config.atomic_write_text

    def count_env_write(*args: object, **kwargs: object) -> None:
        nonlocal env_writes
        env_writes += 1
        atomic_write_text(*args, **kwargs)

    monkeypatch.setattr(openai_oauth, "refresh_access_token", refresh_access_token)
    monkeypatch.setattr(config, "atomic_write_text", count_env_write)

    await provider._do_refresh()

    persisted = dotenv_values(env)
    new_access_ref = str(persisted["SUXIAOYOU_OPENAI_OAUTH_ACCESS_TOKEN"])
    new_refresh_ref = str(persisted["SUXIAOYOU_OPENAI_OAUTH_REFRESH_TOKEN"])
    assert env_writes == 1
    assert store.resolve(new_access_ref) == "new-access"
    assert store.resolve(new_refresh_ref) == "new-refresh"
    assert store.get(old_access_ref) is None
    assert store.get(old_refresh_ref) is None
    assert provider._access_token == "new-access"
    assert provider._refresh_token == "new-refresh"
    assert settings.openai_oauth_access_token == "new-access"
    assert settings.openai_oauth_refresh_token == "new-refresh"
    assert settings.openai_oauth_expires_at == provider._expires_at_ms
    assert persisted["SUXIAOYOU_OPENAI_OAUTH_EXPIRES_AT"] == str(
        provider._expires_at_ms
    )


@pytest.mark.asyncio
async def test_disconnect_fences_an_inflight_token_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = tmp_path / ".env"
    store = _install_store(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "_ENV_PATH", env)
    _seed_openai_env()
    settings = _openai_settings()
    provider = OpenAISubscriptionProvider(
        access_token="old-access",
        account_id="old-account",
        refresh_token="old-refresh",
        expires_at_ms=123,
        settings=settings,
    )
    registry = _FakeRegistry(provider)  # type: ignore[arg-type]
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()

    async def refresh_access_token(_refresh_token: str) -> dict[str, object]:
        refresh_started.set()
        await release_refresh.wait()
        return {
            "access_token": "late-access",
            "refresh_token": "late-refresh",
            "expires_in": 3600,
        }

    monkeypatch.setattr(openai_oauth, "refresh_access_token", refresh_access_token)
    refresh_task = asyncio.create_task(provider._do_refresh())
    await refresh_started.wait()

    await openai_auth.disconnect_openai_subscription(settings, registry)  # type: ignore[arg-type]
    release_refresh.set()
    with pytest.raises(RuntimeError, match="changed while token refresh"):
        await refresh_task

    persisted = dotenv_values(env)
    assert "SUXIAOYOU_OPENAI_OAUTH_ACCESS_TOKEN" not in persisted
    assert "SUXIAOYOU_OPENAI_OAUTH_REFRESH_TOKEN" not in persisted
    assert settings.openai_oauth_access_token == ""
    assert settings.openai_oauth_refresh_token == ""
    assert registry.get_provider(openai_auth.PROVIDER_ID) is None
    assert store.get("env:SUXIAOYOU_OPENAI_OAUTH_ACCESS_TOKEN") is None


@pytest.mark.asyncio
async def test_ollama_registration_failure_restores_env_settings_and_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = tmp_path / ".env"
    monkeypatch.setattr(config, "_ENV_PATH", env)
    config.persist_env_transaction(
        {"SUXIAOYOU_OLLAMA_BASE_URL": "http://old.example:11434"},
        lambda: None,
        lambda: None,
    )
    old_env = env.read_text(encoding="utf-8")
    settings = SimpleNamespace(ollama_base_url="http://old.example:11434")
    old_provider = _FakeProvider("ollama")
    registry = _FakeRegistry(old_provider)
    registry.fail_register = True

    from app import dependencies

    monkeypatch.setattr(dependencies, "get_settings", lambda: settings)
    monkeypatch.setattr(dependencies, "get_provider_registry", lambda: registry)
    monkeypatch.setattr(ollama_provider, "OllamaProvider", _FakeOllamaProvider)

    with pytest.raises(RuntimeError, match="runtime registry rejected"):
        await ollama._register_ollama_provider("http://new.example:11434")

    assert settings.ollama_base_url == "http://old.example:11434"
    assert registry.get_provider("ollama") is old_provider
    assert env.read_text(encoding="utf-8") == old_env


@pytest.mark.asyncio
async def test_rapid_mlx_start_failure_restores_both_keys_and_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = tmp_path / ".env"
    monkeypatch.setattr(config, "_ENV_PATH", env)
    config.persist_env_transaction(
        {
            "SUXIAOYOU_RAPID_MLX_BASE_URL": "http://127.0.0.1:18080/v1",
            "SUXIAOYOU_RAPID_MLX_MODEL": "old-model",
        },
        lambda: None,
        lambda: None,
    )
    old_env = env.read_text(encoding="utf-8")
    settings = SimpleNamespace(
        rapid_mlx_base_url="http://127.0.0.1:18080/v1",
        rapid_mlx_model="old-model",
    )
    old_provider = _FakeProvider("rapid-mlx")
    registry = _FakeRegistry(old_provider)
    registry.fail_register = True
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(rapid_mlx_manager=_RapidMLXManager())
        )
    )
    monkeypatch.setattr(
        rapid_mlx_provider,
        "RapidMLXProvider",
        _FakeRapidMLXProvider,
    )

    with pytest.raises(RuntimeError, match="runtime registry rejected"):
        await rapid_mlx.start_rapid_mlx(
            request,  # type: ignore[arg-type]
            rapid_mlx.RapidMLXStartRequest(model="new-model", port=18081),
            settings,
            registry,
        )

    assert settings.rapid_mlx_base_url == "http://127.0.0.1:18080/v1"
    assert settings.rapid_mlx_model == "old-model"
    assert registry.get_provider("rapid-mlx") is old_provider
    assert env.read_text(encoding="utf-8") == old_env
