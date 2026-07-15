"""Credential-backed providers stay inert until an explicit provider use."""

from __future__ import annotations

from typing import Any, AsyncIterator

import pytest

from app.auth import credential_store
from app.auth.credential_store import CredentialStore, is_credential_reference
from app.config import Settings
from app.provider.base import BaseProvider
from app.provider.deferred import DeferredCredentialProvider
from app.provider.factory import create_provider
from app.schemas.provider import ModelInfo, ProviderStatus, StreamChunk


class CountingBackend:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.reads = 0

    def get_password(self, service: str, username: str) -> str | None:
        self.reads += 1
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.values.pop((service, username), None)


class FakeProvider(BaseProvider):
    def __init__(self, provider_id: str = "fake") -> None:
        self._id = provider_id

    @property
    def id(self) -> str:
        return self._id

    def local_models(self) -> list[ModelInfo]:
        return [ModelInfo(id="fake-model", name="Fake", provider_id=self.id)]

    async def list_models(self) -> list[ModelInfo]:
        return self.local_models()

    async def stream_chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        yield StreamChunk(type="text-delta", data={"text": "ok"})

    async def health_check(self) -> ProviderStatus:
        return ProviderStatus(status="connected", model_count=1)


def test_settings_loading_does_not_open_credential_store(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "SUXIAOYOU_DEEPSEEK_API_KEY="
        "'suxiaoyou-credential://env%3ASUXIAOYOU_DEEPSEEK_API_KEY%3Atest'\n",
        encoding="utf-8",
    )

    def unexpected_store() -> CredentialStore:
        raise AssertionError("Settings construction must not access native credentials")

    monkeypatch.setattr(credential_store, "get_credential_store", unexpected_store)
    settings = Settings(_env_file=env_path)

    assert is_credential_reference(settings.deepseek_api_key)


def test_startup_migration_can_preserve_reference_without_native_read(
    tmp_path,
) -> None:
    backend = CountingBackend()
    store = CredentialStore(
        fallback_path=tmp_path / "fallback.json",
        native_backend=backend,
    )
    reference = store.put("env:SUXIAOYOU_DEEPSEEK_API_KEY:test", "sk-secret")
    env_path = tmp_path / ".env"
    env_path.write_text(
        f"SUXIAOYOU_DEEPSEEK_API_KEY='{reference}'\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=env_path)

    migrated = credential_store.migrate_settings_credentials(
        settings,
        env_path,
        store=store,
        hydrate_references=False,
    )

    assert migrated == 0
    assert settings.deepseek_api_key == reference
    assert backend.reads == 0


@pytest.mark.asyncio
async def test_deferred_provider_metadata_does_not_activate_but_chat_does() -> None:
    activations = 0
    live = FakeProvider()

    def activate() -> BaseProvider:
        nonlocal activations
        activations += 1
        return live

    provider = DeferredCredentialProvider(
        provider_id="fake",
        metadata_provider=FakeProvider(),
        activate=activate,
    )

    assert provider.local_models()[0].id == "fake-model"
    assert (await provider.list_models())[0].id == "fake-model"
    assert activations == 0

    chunks = [
        chunk
        async for chunk in provider.stream_chat("fake-model", [{"role": "user"}])
    ]
    assert chunks[0].data == {"text": "ok"}
    assert activations == 1


@pytest.mark.asyncio
async def test_factory_defers_native_read_until_activation(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = CountingBackend()
    store = CredentialStore(
        fallback_path=tmp_path / "fallback.json",
        native_backend=backend,
    )
    reference = store.put("env:SUXIAOYOU_DEEPSEEK_API_KEY:test", "sk-secret")
    monkeypatch.setattr(credential_store, "get_credential_store", lambda: store)

    provider = create_provider("deepseek", reference)
    assert isinstance(provider, DeferredCredentialProvider)
    assert provider.local_models()
    assert await provider.list_models()
    assert backend.reads == 0

    delegate = await provider._activated()  # noqa: SLF001 - activation boundary contract
    assert delegate.id == "deepseek"
    assert backend.reads == 1
