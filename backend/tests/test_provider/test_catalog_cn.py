from app.provider.catalog import PROVIDER_CATALOG


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

