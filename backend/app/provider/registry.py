"""Provider registry — manages provider instances and model lookup."""

from __future__ import annotations

import asyncio
import logging

from app.provider.base import BaseProvider
from app.provider.vision_allowlist import model_supports_vision
from app.schemas.provider import ModelInfo, ProviderStatus
logger = logging.getLogger(__name__)

MODEL_REFRESH_TIMEOUT_SECONDS = 45.0

# Aggregator providers — their models should yield to direct providers
# when no explicit provider_id is given.
_AGGREGATOR_PROVIDERS = {"openrouter"}


def _external_runtime_stopped() -> bool:
    try:
        from app.security.control import get_security_control

        return get_security_control().emergency_stop
    except RuntimeError:
        return False


def _provider_priority(provider_id: str) -> int:
    """Lower is better when deduplicating model IDs across providers."""
    if provider_id in _AGGREGATOR_PROVIDERS:
        return 1
    return 0


def _promote_vision_capability(models: list[ModelInfo]) -> None:
    """Rescue vision-capable models that upstream metadata reported as text-only.

    Providers source ``capabilities.vision`` from metadata that's often missing
    (a ``/v1/models`` listing has no modalities) or stale (models.dev lags new
    releases), so genuinely multimodal models arrive ``vision=False`` and get a
    false "can't read images" gate. We OR in a curated allowlist here, at the
    one point every provider's models pass through — additive only, so a model
    a provider already flagged ``vision=True`` is never touched. Mutates in
    place; providers rebuild their ``ModelInfo`` objects each refresh
    (``clear_cache()`` runs before ``list_models()``) and the flip is idempotent.
    """
    for m in models:
        if not m.capabilities.vision and model_supports_vision(m.id, m.name):
            m.capabilities.vision = True


class ProviderRegistry:
    """Registry of LLM providers."""

    def __init__(self) -> None:
        self._providers: dict[str, BaseProvider] = {}
        # Last known-good models are retained independently for each provider.
        # A partial network failure must never erase another provider's cache.
        self._provider_models: dict[str, list[ModelInfo]] = {}
        # Quick lookup: model_id → best (provider, model) — used when no provider_id given
        self._model_index: dict[str, tuple[BaseProvider, ModelInfo]] = {}
        # Full list: ALL (provider, model) pairs — used for all_models() and provider-aware resolve
        self._full_models: list[tuple[BaseProvider, ModelInfo]] = []
        # Full refreshes are single-flight: concurrent GET /models callers join
        # the same task instead of multiplying remote requests.
        self._refresh_task: asyncio.Task[dict[str, list[ModelInfo]]] | None = None
        self._refresh_task_lock = asyncio.Lock()
        # Serialize full and per-provider refresh mutations.
        self._refresh_operation_lock = asyncio.Lock()

    def register(self, provider: BaseProvider) -> None:
        """Register a provider."""
        self._providers[provider.id] = provider
        self._rebuild_indexes()
        logger.info("Registered provider: %s", provider.id)

    def unregister(self, provider_id: str) -> None:
        """Remove a provider and its models from the index."""
        self._providers.pop(provider_id, None)
        self._provider_models.pop(provider_id, None)
        self._rebuild_indexes()
        logger.info("Unregistered provider: %s", provider_id)

    def get_provider(self, provider_id: str) -> BaseProvider | None:
        """Get provider by ID."""
        return self._providers.get(provider_id)

    async def refresh_models(self) -> dict[str, list[ModelInfo]]:
        """Refresh every provider, joining any already in-flight refresh."""
        if _external_runtime_stopped():
            return {
                provider_id: list(self._provider_models.get(provider_id, []))
                for provider_id in self._providers
            }
        async with self._refresh_task_lock:
            task = self._refresh_task
            if task is None or task.done():
                task = asyncio.create_task(
                    self._refresh_models_once(),
                    name="provider-model-refresh",
                )
                self._refresh_task = task
                task.add_done_callback(self._observe_refresh_completion)

        # A cancelled HTTP client must not cancel the refresh shared by other
        # callers.  ``shutdown()`` explicitly cancels it during app teardown.
        return await asyncio.shield(task)

    async def _refresh_models_once(self) -> dict[str, list[ModelInfo]]:
        """Perform one serialized full refresh."""
        async with self._refresh_operation_lock:
            providers = list(self._providers.items())
            if not providers:
                return {}

            refreshes = await asyncio.gather(
                *(
                    self._refresh_provider_models(pid, provider)
                    for pid, provider in providers
                ),
            )

            result: dict[str, list[ModelInfo]] = {}
            failed: list[tuple[str, Exception]] = []
            for pid, provider, models, error in refreshes:
                # Ignore a stale result if configuration replaced the provider
                # instance while its request was in flight.
                if self._providers.get(pid) is not provider:
                    result[pid] = list(self._provider_models.get(pid, []))
                    continue
                if error is not None:
                    logger.error("Failed to refresh models for %s: %s", pid, error)
                    result[pid] = list(self._provider_models.get(pid, []))
                    failed.append((pid, error))
                    continue

                result[pid] = list(models)
                self._provider_models[pid] = list(models)

            self._rebuild_indexes()

            if failed and len(failed) == len(providers) and not self._full_models:
                raise failed[0][1]

            logger.info(
                "Model index: %d unique models, %d total across %d providers",
                len(self._model_index),
                len(self._full_models),
                len(self._providers),
            )
            return result

    def seed_registered_models(self) -> dict[str, int]:
        """Populate last-known-good caches using network-free provider data."""
        counts: dict[str, int] = {}
        for pid, provider in self._providers.items():
            try:
                models = provider.local_models()
            except Exception as exc:
                logger.warning("Failed to build local model seed for %s: %s", pid, exc)
                counts[pid] = 0
                continue
            if models:
                _promote_vision_capability(models)
                self._provider_models[pid] = list(models)
            counts[pid] = len(models)
        self._rebuild_indexes()
        logger.info(
            "Seeded model index locally: %d unique models, %d total",
            len(self._model_index),
            len(self._full_models),
        )
        return counts

    def _rebuild_indexes(self) -> None:
        new_index: dict[str, tuple[BaseProvider, ModelInfo]] = {}
        new_full: list[tuple[BaseProvider, ModelInfo]] = []
        for pid, provider in self._providers.items():
            for model in self._provider_models.get(pid, []):
                new_full.append((provider, model))
                existing = new_index.get(model.id)
                if existing is None or (
                    _provider_priority(pid) < _provider_priority(existing[0].id)
                ):
                    new_index[model.id] = (provider, model)
        self._model_index = new_index
        self._full_models = new_full

    def _observe_refresh_completion(
        self,
        task: asyncio.Task[dict[str, list[ModelInfo]]],
    ) -> None:
        """Retrieve task exceptions even if every HTTP waiter disconnected."""
        if self._refresh_task is task:
            self._refresh_task = None
        if task.cancelled():
            return
        try:
            task.exception()
        except asyncio.CancelledError:
            pass

    @property
    def refresh_in_progress(self) -> bool:
        task = self._refresh_task
        return task is not None and not task.done()

    async def shutdown(self) -> None:
        """Cancel and retrieve any shared refresh task during app shutdown."""
        async with self._refresh_task_lock:
            task = self._refresh_task
            self._refresh_task = None
        if task is not None and not task.done():
            task.cancel()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def refresh_provider(self, provider_id: str) -> list[ModelInfo]:
        """Refresh just one provider's models, leaving the rest untouched.

        Used by self-healing paths (e.g. GET /config/providers) that need to
        re-register a single dropped provider without paying for a full
        cross-provider refresh.

        Returns the provider's model list, or ``[]`` if the provider is
        absent or refresh failed.
        """
        if _external_runtime_stopped():
            return list(self._provider_models.get(provider_id, []))
        async with self._refresh_operation_lock:
            provider = self._providers.get(provider_id)
            if provider is None:
                return []

            pid, _, models, error = await self._refresh_provider_models(provider_id, provider)
            if error is not None:
                logger.warning("Failed to refresh models for %s: %s", pid, error)
                return []

            if self._providers.get(pid) is not provider:
                return list(self._provider_models.get(pid, []))

            self._provider_models[pid] = list(models)
            self._rebuild_indexes()

        logger.info(
            "Refreshed provider %s: %d models (index=%d, total=%d)",
            pid,
            len(models),
            len(self._model_index),
            len(self._full_models),
        )
        return models

    async def _refresh_provider_models(
        self,
        pid: str,
        provider: BaseProvider,
    ) -> tuple[str, BaseProvider, list[ModelInfo], Exception | None]:
        try:
            provider.clear_cache()
            models = await asyncio.wait_for(
                provider.list_models(),
                timeout=MODEL_REFRESH_TIMEOUT_SECONDS,
            )
            _promote_vision_capability(models)
            return pid, provider, models, None
        except Exception as e:
            if isinstance(e, TimeoutError):
                e = TimeoutError(
                    f"Timed out refreshing models for {pid} after "
                    f"{MODEL_REFRESH_TIMEOUT_SECONDS:g}s"
                )
            return pid, provider, [], e

    def resolve_model(
        self,
        model_id: str,
        provider_id: str | None = None,
    ) -> tuple[BaseProvider, ModelInfo] | None:
        """Resolve a model ID to its provider and info.

        If provider_id is given, returns the model from that specific provider.
        Otherwise falls back to the default priority (direct > aggregator).
        """
        if provider_id:
            for p, m in self._full_models:
                if m.id == model_id and p.id == provider_id:
                    return (p, m)
            # Provider specified but not found — fall through to default
        return self._model_index.get(model_id)

    def all_models(self) -> list[ModelInfo]:
        """All models from all providers (includes duplicates from different providers)."""
        return [info for _, info in self._full_models]

    async def health(self) -> dict[str, ProviderStatus]:
        """Health check all providers."""
        result = {}
        for pid, provider in self._providers.items():
            result[pid] = await provider.health_check()
        return result
