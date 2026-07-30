import pytest

from app.api.chat import _unsupported_images_error


def test_unsupported_image_error_follows_request_language() -> None:
    zh = _unsupported_images_error("zh")
    en = _unsupported_images_error("en")

    assert zh.detail == {
        "code": "MODEL_DOES_NOT_SUPPORT_IMAGES",
        "message": "当前所选模型不支持图片，请选择支持视觉的模型后重试。",
    }
    assert en.detail == {
        "code": "MODEL_DOES_NOT_SUPPORT_IMAGES",
        "message": (
            "The selected model does not support images. "
            "Choose a vision model and try again."
        ),
    }


@pytest.mark.asyncio
async def test_manual_compaction_http_errors_follow_request_language(
    app_client,
) -> None:
    zh = await app_client.post(
        "/api/chat/compact",
        json={"session_id": "missing", "model_id": None},
    )
    en = await app_client.post(
        "/api/chat/compact",
        headers={"Accept-Language": "en"},
        json={"session_id": "missing", "model_id": None},
    )

    assert zh.status_code == 404
    assert zh.json()["detail"] == {
        "code": "compaction_session_not_found",
        "message": "未找到对应的对话。",
    }
    assert en.status_code == 404
    assert en.json()["detail"] == {
        "code": "compaction_session_not_found",
        "message": "The conversation was not found.",
    }
