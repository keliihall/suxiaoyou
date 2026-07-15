"""Automation templates must tell the truth about the scheduler boundary."""

from __future__ import annotations

from app.scheduler.executor import LOOP_PRESETS
from app.scheduler.templates import get_templates


def test_every_loop_preset_states_the_unattended_read_only_boundary() -> None:
    assert LOOP_PRESETS
    for preset in LOOP_PRESETS.values():
        normalized = preset.lower()
        assert "without" in normalized
        assert any(noun in normalized for noun in ("changing", "modifying"))


def test_loop_templates_do_not_promise_mutations_the_scheduler_will_deny() -> None:
    templates_by_language = {
        "zh": {item["id"]: item for item in get_templates("zh")},
        "en": {item["id"]: item for item in get_templates("en")},
    }

    for language, templates in templates_by_language.items():
        for template_id in ("loop-email-batch", "loop-doc-review", "loop-data-cleanup"):
            prompt = templates[template_id]["prompt"]
            if language == "zh":
                assert "不要" in prompt
            else:
                assert "without modifying" in prompt or "Do not" in prompt

    email_zh = templates_by_language["zh"]["loop-email-batch"]["prompt"]
    email_en = templates_by_language["en"]["loop-email-batch"]["prompt"]
    assert "标记为已读并归档" not in email_zh
    assert "mark as read and archive" not in email_en

    data_zh = templates_by_language["zh"]["loop-data-cleanup"]["prompt"]
    data_en = templates_by_language["en"]["loop-data-cleanup"]["prompt"]
    assert "标记或删除重复记录" not in data_zh
    assert "remove duplicate entries" not in data_en
