"""Header safety tests for OpenAI-compatible custom providers."""

import httpx

from app.provider.generic_openai import GenericOpenAIProvider


def test_custom_provider_default_user_agent_is_ascii_safe():
    provider = GenericOpenAIProvider(
        api_key="sk-test",
        provider_id="custom_test",
        base_url="http://example.com/v1",
        kind="openai_compat_custom",
    )

    user_agent = provider._client.default_headers["User-Agent"]

    assert user_agent == "Suxiaoyou/1.0"
    user_agent.encode("ascii")


def test_custom_provider_lowercase_user_agent_replaces_default():
    provider = GenericOpenAIProvider(
        api_key="sk-test",
        provider_id="custom_test",
        base_url="http://example.com/v1",
        kind="openai_compat_custom",
        default_headers={"user-agent": "Custom/3"},
    )

    default_headers = provider._client.default_headers
    string_headers = httpx.Headers(
        (str(name), str(value)) for name, value in default_headers.items()
    )

    assert string_headers.get_list("user-agent") == ["Custom/3"]
    assert sum(name.lower() == "user-agent" for name in default_headers) == 1
