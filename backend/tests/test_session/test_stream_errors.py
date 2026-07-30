from app.session.processor import _localized_provider_stream_error


def test_incomplete_chunked_stream_error_is_localized() -> None:
    raw = (
        "peer closed connection without sending complete message body "
        "(incomplete chunked read)"
    )

    code, message = _localized_provider_stream_error(raw, "zh")

    assert code == "model_connection_interrupted"
    assert message == "模型服务连接中断，已自动重试但仍未恢复，请稍后再试。"
    assert "chunked" not in message


def test_provider_error_localization_preserves_english_locale() -> None:
    code, message = _localized_provider_stream_error(
        "HTTP 429 rate limit exceeded",
        "en",
    )

    assert code == "model_rate_limited"
    assert message.startswith("The model service")
