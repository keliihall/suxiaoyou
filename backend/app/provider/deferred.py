"""Provider proxy that opens the OS credential store only on real use.

Desktop configuration persists secrets as opaque references.  Constructing a
provider during process startup must not resolve those references: on macOS an
unsigned or newly signed backend can otherwise block the splash screen behind
a Keychain ACL prompt.  This proxy exposes network-free model metadata while
keeping activation behind ``stream_chat`` or an explicit ``health_check``.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any, AsyncIterator

from app.provider.base import BaseProvider
from app.schemas.provider import ModelInfo, ProviderStatus, StreamChunk


class DeferredCredentialProvider(BaseProvider):
    """Activate a credential-backed provider at its first real operation."""

    def __init__(
        self,
        *,
        provider_id: str,
        metadata_provider: BaseProvider,
        activate: Callable[[], BaseProvider],
    ) -> None:
        self._provider_id = provider_id
        self._metadata_provider = metadata_provider
        self._activate = activate
        self._delegate: BaseProvider | None = None
        self._activation_lock = threading.Lock()

    @property
    def id(self) -> str:
        return self._provider_id

    @property
    def credential_deferred(self) -> bool:
        """Let status surfaces distinguish configured from hydrated state."""

        return self._delegate is None

    @property
    def supports_native_web_search(self) -> bool:
        """Preserve subscription transport capability through the proxy."""

        return self._metadata_provider.id == "openai-subscription"

    def _activate_sync(self) -> BaseProvider:
        delegate = self._delegate
        if delegate is not None:
            return delegate
        with self._activation_lock:
            delegate = self._delegate
            if delegate is None:
                delegate = self._activate()
                if delegate.id != self.id:
                    raise RuntimeError(
                        "Deferred credential provider activated with a different ID"
                    )
                self._delegate = delegate
            return delegate

    async def _activated(self) -> BaseProvider:
        # Native keyring implementations are synchronous and may display an OS
        # consent dialog.  Keep that explicit operation off the event loop so
        # the rest of the desktop UI remains responsive while the user decides.
        return await asyncio.to_thread(self._activate_sync)

    def local_models(self) -> list[ModelInfo]:
        delegate = self._delegate
        if delegate is not None:
            return delegate.local_models()
        return self._metadata_provider.local_models()

    async def list_models(self) -> list[ModelInfo]:
        # Automatic startup refresh is metadata work, not authorization to
        # open Keychain/Credential Manager/Secret Service.  Before activation,
        # return the same local seed used for cold startup.
        delegate = self._delegate
        if delegate is None:
            return self._metadata_provider.local_models()
        return await delegate.list_models()

    async def stream_chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        system: str | list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra_body: dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
        native_web_search_enabled: bool = True,
    ) -> AsyncIterator[StreamChunk]:
        delegate = await self._activated()
        kwargs: dict[str, Any] = {
            "tools": tools,
            "system": system,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "extra_body": extra_body,
            "response_format": response_format,
        }
        if self.supports_native_web_search:
            kwargs["native_web_search_enabled"] = native_web_search_enabled
        async for chunk in delegate.stream_chat(model, messages, **kwargs):
            yield chunk

    async def health_check(self) -> ProviderStatus:
        # A user-requested connectivity check is an actual provider operation
        # and therefore an appropriate boundary for native credential access.
        delegate = await self._activated()
        return await delegate.health_check()

    def clear_cache(self) -> None:
        self._metadata_provider.clear_cache()
        if self._delegate is not None:
            self._delegate.clear_cache()
