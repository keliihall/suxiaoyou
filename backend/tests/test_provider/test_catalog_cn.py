from app.provider.catalog import PROVIDER_CATALOG
from app.provider.factory import create_provider


EXPECTED_CHINA_READY_PROVIDERS = [
    "deepseek",
    "qwen",
    "kimi",
    "minimax",
    "zhipu",
    "siliconflow",
    "xiaomi",
]
EXPECTED_NATIVE_PROVIDERS = ["anthropic", "google"]
EXPECTED_BUNDLED_PROVIDERS = EXPECTED_CHINA_READY_PROVIDERS + EXPECTED_NATIVE_PROVIDERS


def test_catalog_exposes_supported_remote_providers() -> None:
    assert list(PROVIDER_CATALOG) == EXPECTED_BUNDLED_PROVIDERS


def test_catalog_display_names_are_localized() -> None:
    for provider_id in EXPECTED_CHINA_READY_PROVIDERS:
        assert any(
            "\u4e00" <= char <= "\u9fff"
            for char in PROVIDER_CATALOG[provider_id].name
        )


def test_every_bundled_provider_has_a_network_free_model_seed() -> None:
    for provider_id in EXPECTED_BUNDLED_PROVIDERS:
        provider = create_provider(provider_id, "test-key")
        models = provider.local_models()
        assert models, provider_id
        assert all(model.provider_id == provider_id for model in models)


def test_native_provider_catalog_routes_to_official_sdk_adapters() -> None:
    assert PROVIDER_CATALOG["anthropic"].kind == "native_anthropic"
    assert PROVIDER_CATALOG["google"].kind == "native_gemini"
    assert type(create_provider("anthropic", "test-key")).__name__ == "AnthropicDesktopProvider"
    assert type(create_provider("google", "test-key")).__name__ == "GeminiDesktopProvider"
