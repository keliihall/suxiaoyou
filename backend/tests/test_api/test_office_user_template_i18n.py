import json

from starlette.requests import Request

from app.api.office_user_templates import (
    _UserTemplateProvenance,
    _api_error,
)


def _request(language: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/office-v2/user-templates",
            "headers": [(b"accept-language", language.encode("ascii"))],
        }
    )


def test_workspace_provenance_error_is_chinese_for_chinese_ui() -> None:
    response = _api_error(_request("zh-CN"), _UserTemplateProvenance())
    body = json.loads(response.body)

    assert response.status_code == 409
    assert body["code"] == "user_office_template_provenance_mismatch"
    assert body["detail"] == "当前文件夹尚未确认或已发生变化，请重新选择文件夹后重试。"


def test_workspace_provenance_error_preserves_english_ui() -> None:
    response = _api_error(_request("en-US"), _UserTemplateProvenance())
    body = json.loads(response.body)

    assert body["detail"] == "The request is not bound to the current verified workspace"
