"""Provider catalog — maps provider IDs to their configuration.

Defines which providers are available, how to create them (native SDK vs
OpenAI-compatible), and which Settings field holds their API key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ProviderDef:
    """Desktop provider definition."""

    id: str
    name: str
    settings_key: str  # Field name in app.config.Settings (e.g. "openai_api_key")
    kind: str  # "openai_compat" | "native_anthropic" | "native_gemini"
    base_url: str = ""  # Only used by openai_compat providers
    default_headers: dict[str, str] = field(default_factory=dict)


# All remote providers that can be configured via direct API key (BYOK).
# Keep this desktop catalog focused on China-accessible defaults; Ollama,
# Rapid-MLX and custom OpenAI-compatible endpoints have dedicated local flows.
PROVIDER_CATALOG: dict[str, ProviderDef] = {
    "deepseek": ProviderDef(
        id="deepseek",
        name="深度求索（DeepSeek）",
        settings_key="deepseek_api_key",
        kind="openai_compat",
        base_url="https://api.deepseek.com/v1",
    ),
    "qwen": ProviderDef(
        id="qwen",
        name="通义千问",
        settings_key="qwen_api_key",
        kind="openai_compat",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    ),
    "kimi": ProviderDef(
        id="kimi",
        name="Kimi（月之暗面）",
        settings_key="kimi_api_key",
        kind="openai_compat",
        base_url="https://api.moonshot.cn/v1",
    ),
    "minimax": ProviderDef(
        id="minimax",
        name="MiniMax（稀宇科技）",
        settings_key="minimax_api_key",
        kind="openai_compat",
        base_url="https://api.minimaxi.com/v1",
    ),
    "zhipu": ProviderDef(
        id="zhipu",
        name="智谱 AI",
        settings_key="zhipu_api_key",
        kind="openai_compat",
        base_url="https://open.bigmodel.cn/api/paas/v4",
    ),
    "siliconflow": ProviderDef(
        id="siliconflow",
        name="硅基流动",
        settings_key="siliconflow_api_key",
        kind="openai_compat",
        base_url="https://api.siliconflow.cn/v1",
    ),
    "xiaomi": ProviderDef(
        id="xiaomi",
        name="小米 MiMo",
        settings_key="xiaomi_api_key",
        kind="openai_compat",
        base_url="https://api.xiaomimimo.com/v1",
    ),
}
