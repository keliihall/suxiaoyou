"""Provider factory — creates the right provider instance by ID.

Lazy imports ensure that native SDK dependencies (anthropic, google-genai)
are only loaded when the provider is actually used.
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from app.auth.credential_store import is_credential_reference, resolve_secret_tree
from app.provider.base import BaseProvider
from app.provider.catalog import PROVIDER_CATALOG

logger = logging.getLogger(__name__)


def create_provider(
    provider_id: str,
    api_key: str,
    *,
    base_url: str | None = None,
    models_override: list[dict] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> BaseProvider:
    """Create a desktop provider by ID.

    Routes to the correct implementation:
    - "anthropic" → AnthropicDesktopProvider (native SDK via yakAgent)
    - "google"    → GeminiDesktopProvider (native SDK via yakAgent)
    - Others      → GenericOpenAIProvider (OpenAI-compatible)

    Args:
        provider_id: Provider ID from the catalog.
        api_key: API key for the provider.
        base_url: Override base URL (required for Azure).
        models_override: Custom-endpoint-only. Manual model list; when non-empty
            the provider skips the /v1/models discovery call.
        extra_headers: Custom-endpoint-only. Extra headers merged into every
            outgoing chat-completions request.

    Raises:
        ValueError: If provider_id is not in the catalog.
        ImportError: If a native SDK is required but not installed.
    """
    protected_payload = {
        "api_key": api_key,
        "extra_headers": copy.deepcopy(extra_headers),
    }
    if _contains_credential_reference(protected_payload):
        from app.provider.deferred import DeferredCredentialProvider

        metadata_payload = _replace_credential_references(
            protected_payload,
            replacement="deferred-credential",
        )
        metadata_provider = _create_provider(
            provider_id,
            str(metadata_payload["api_key"]),
            base_url=base_url,
            models_override=models_override,
            extra_headers=metadata_payload["extra_headers"],
        )

        def activate() -> BaseProvider:
            resolved = resolve_secret_tree(protected_payload)
            return _create_provider(
                provider_id,
                str(resolved["api_key"]),
                base_url=base_url,
                models_override=models_override,
                extra_headers=resolved["extra_headers"],
            )

        return DeferredCredentialProvider(
            provider_id=provider_id,
            metadata_provider=metadata_provider,
            activate=activate,
        )

    return _create_provider(
        provider_id,
        api_key,
        base_url=base_url,
        models_override=models_override,
        extra_headers=extra_headers,
    )


def _contains_credential_reference(value: Any) -> bool:
    if is_credential_reference(value):
        return True
    if isinstance(value, dict):
        return any(_contains_credential_reference(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_credential_reference(item) for item in value)
    return False


def _replace_credential_references(value: Any, *, replacement: str) -> Any:
    if is_credential_reference(value):
        return replacement
    if isinstance(value, dict):
        return {
            key: _replace_credential_references(item, replacement=replacement)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _replace_credential_references(item, replacement=replacement)
            for item in value
        ]
    return copy.deepcopy(value)


def _create_provider(
    provider_id: str,
    api_key: str,
    *,
    base_url: str | None = None,
    models_override: list[dict] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> BaseProvider:
    """Create a provider from already-resolved runtime configuration."""

    pdef = PROVIDER_CATALOG.get(provider_id)
    if pdef is None:
        if provider_id.startswith("custom_"):
            from app.provider.catalog import ProviderDef
            pdef = ProviderDef(
                id=provider_id,
                name="Custom Endpoint",
                name_en="Custom Endpoint",
                settings_key="custom_endpoints",
                kind="openai_compat_custom",
            )
        else:
            raise ValueError(
                f"Unknown provider: '{provider_id}'. "
                f"Available: {', '.join(sorted(PROVIDER_CATALOG.keys()))}"
            )

    if pdef.kind == "openrouter":
        from app.provider.openrouter import OpenRouterProvider
        return OpenRouterProvider(api_key)

    if pdef.kind == "native_anthropic":
        from app.provider.anthropic_provider import AnthropicDesktopProvider
        return AnthropicDesktopProvider(api_key=api_key)

    if pdef.kind == "native_gemini":
        from app.provider.gemini_provider import GeminiDesktopProvider
        return GeminiDesktopProvider(api_key=api_key)

    if pdef.kind in ("openai_compat", "openai_compat_azure", "openai_compat_custom"):
        from app.provider.generic_openai import GenericOpenAIProvider

        effective_url = base_url or pdef.base_url
        if not effective_url and pdef.kind in ("openai_compat_azure", "openai_compat_custom"):
            raise ValueError(
                f"Provider '{provider_id}' requires a base_url. "
                f"Ensure the corresponding setting is provided."
            )

        merged_headers: dict[str, str] | None = None
        if pdef.default_headers or extra_headers:
            merged_headers = dict(pdef.default_headers or {})
            if extra_headers:
                merged_headers.update(extra_headers)

        return GenericOpenAIProvider(
            api_key=api_key,
            provider_id=provider_id,
            base_url=effective_url,
            kind=pdef.kind,
            default_headers=merged_headers,
            models_override=models_override,
        )

    raise ValueError(f"Unknown provider kind: '{pdef.kind}' for provider '{provider_id}'")
