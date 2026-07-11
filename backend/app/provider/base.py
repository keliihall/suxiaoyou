"""Base provider ABC."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator

from app.schemas.provider import ModelInfo, ProviderStatus, StreamChunk


class BaseProvider(ABC):
    """Abstract LLM provider."""

    @property
    @abstractmethod
    def id(self) -> str:
        """Unique provider identifier (e.g. 'openrouter')."""

    @abstractmethod
    async def list_models(self) -> list[ModelInfo]:
        """List available models from this provider."""

    @abstractmethod
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
    ) -> AsyncIterator[StreamChunk]:
        """Stream chat completion. Yields StreamChunk objects."""

    @abstractmethod
    async def health_check(self) -> ProviderStatus:
        """Check provider connectivity and return status."""

    def local_models(self) -> list[ModelInfo]:
        """Return a network-free model seed for cold startup.

        Providers that have a built-in catalog, a user-declared model list, or
        other purely local metadata can override this method.  The application
        calls it before scheduling any background discovery so ``/livez`` and
        the initial model picker never depend on a remote service.
        """
        return []

    def clear_cache(self) -> None:
        """Clear cached models to force refresh on next list_models() call.

        Default implementation does nothing (for providers without caching).
        """
        pass
