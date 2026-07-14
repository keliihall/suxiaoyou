"""Provider catalog — maps provider IDs to their configuration.

Defines which providers are available, how to create them (native SDK vs
OpenAI-compatible), and which Settings field holds their API key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.i18n import Language, localize


@dataclass
class ProviderDef:
    """Desktop provider definition."""

    id: str
    name: str
    name_en: str
    settings_key: str  # Field name in app.config.Settings (e.g. "openai_api_key")
    kind: str  # "openai_compat" | "native_anthropic" | "native_gemini"
    base_url: str = ""  # Only used by openai_compat providers
    default_headers: dict[str, str] = field(default_factory=dict)
    # Small, conservative network-free seed used until remote model metadata
    # has refreshed.  Keep this intentionally narrower than the live catalog.
    seed_models: tuple[tuple[str, str], ...] = ()

    def display_name(self, language: Language | str) -> str:
        """Return localized UI text while keeping provider IDs stable."""

        return localize(language, self.name, self.name_en)


# All remote providers that can be configured via direct API key (BYOK).
# China-accessible providers remain first in the list. Anthropic and Gemini use
# their official native SDKs so Claude/Gemini-specific streaming and tool calls
# do not silently degrade through an OpenAI-compatibility shim. Ollama,
# Rapid-MLX and custom OpenAI-compatible endpoints have dedicated local flows.
PROVIDER_CATALOG: dict[str, ProviderDef] = {
    "deepseek": ProviderDef(
        id="deepseek",
        name="深度求索（DeepSeek）",
        name_en="DeepSeek",
        settings_key="deepseek_api_key",
        kind="openai_compat",
        base_url="https://api.deepseek.com/v1",
        seed_models=(
            ("deepseek-chat", "DeepSeek Chat"),
            ("deepseek-reasoner", "DeepSeek Reasoner"),
        ),
    ),
    "qwen": ProviderDef(
        id="qwen",
        name="通义千问",
        name_en="Qwen",
        settings_key="qwen_api_key",
        kind="openai_compat",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        seed_models=(
            ("qwen-plus", "Qwen Plus"),
            ("qwen-max", "Qwen Max"),
        ),
    ),
    "kimi": ProviderDef(
        id="kimi",
        name="Kimi（月之暗面）",
        name_en="Kimi",
        settings_key="kimi_api_key",
        kind="openai_compat",
        base_url="https://api.moonshot.cn/v1",
        seed_models=(("kimi-k2.5", "Kimi K2.5"),),
    ),
    "minimax": ProviderDef(
        id="minimax",
        name="MiniMax（稀宇科技）",
        name_en="MiniMax",
        settings_key="minimax_api_key",
        kind="openai_compat",
        base_url="https://api.minimaxi.com/v1",
        seed_models=(("MiniMax-M2.5", "MiniMax M2.5"),),
    ),
    "zhipu": ProviderDef(
        id="zhipu",
        name="智谱 AI",
        name_en="Zhipu AI",
        settings_key="zhipu_api_key",
        kind="openai_compat",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        seed_models=(("glm-4.7", "GLM-4.7"),),
    ),
    "siliconflow": ProviderDef(
        id="siliconflow",
        name="硅基流动",
        name_en="SiliconFlow",
        settings_key="siliconflow_api_key",
        kind="openai_compat",
        base_url="https://api.siliconflow.cn/v1",
        seed_models=(("deepseek-ai/DeepSeek-V3.2", "DeepSeek V3.2"),),
    ),
    "xiaomi": ProviderDef(
        id="xiaomi",
        name="小米 MiMo",
        name_en="Xiaomi MiMo",
        settings_key="xiaomi_api_key",
        kind="openai_compat",
        base_url="https://api.xiaomimimo.com/v1",
        seed_models=(("mimo-v2-flash", "MiMo V2 Flash"),),
    ),
    "anthropic": ProviderDef(
        id="anthropic",
        name="Anthropic Claude",
        name_en="Anthropic Claude",
        settings_key="anthropic_api_key",
        kind="native_anthropic",
        seed_models=(
            ("claude-sonnet-4-5", "Claude Sonnet 4.5"),
            ("claude-haiku-4-5", "Claude Haiku 4.5"),
        ),
    ),
    "google": ProviderDef(
        id="google",
        name="Google Gemini",
        name_en="Google Gemini",
        settings_key="google_api_key",
        kind="native_gemini",
        seed_models=(
            ("gemini-2.5-pro", "Gemini 2.5 Pro"),
            ("gemini-2.5-flash", "Gemini 2.5 Flash"),
        ),
    ),
}
