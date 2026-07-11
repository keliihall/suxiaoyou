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


def test_catalog_only_exposes_china_ready_remote_providers() -> None:
    assert list(PROVIDER_CATALOG) == EXPECTED_CHINA_READY_PROVIDERS


def test_catalog_display_names_are_localized() -> None:
    for provider in PROVIDER_CATALOG.values():
        assert any("\u4e00" <= char <= "\u9fff" for char in provider.name)


def test_every_bundled_provider_has_a_network_free_model_seed() -> None:
    for provider_id in EXPECTED_CHINA_READY_PROVIDERS:
        provider = create_provider(provider_id, "test-key")
        models = provider.local_models()
        assert models, provider_id
        assert all(model.provider_id == provider_id for model in models)
