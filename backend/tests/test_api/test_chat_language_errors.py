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
