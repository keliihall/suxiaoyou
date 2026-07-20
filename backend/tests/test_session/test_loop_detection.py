from app.session.loop_detection import LoopDetector


def _run_to_hard_stop(language: str) -> tuple[str, str]:
    detector = LoopDetector(warn_threshold=2, hard_limit=3)
    assert detector.check(
        "localized-loop",
        "read",
        {"path": "/tmp/result"},
        language=language,
    ).action == "allow"
    warning = detector.check(
        "localized-loop",
        "read",
        {"path": "/tmp/result"},
        language=language,
    )
    hard_stop = detector.check(
        "localized-loop",
        "read",
        {"path": "/tmp/result"},
        language=language,
    )
    assert warning.action == "warn" and warning.message
    assert hard_stop.action == "block" and hard_stop.message
    return warning.message, hard_stop.message


def test_loop_messages_follow_the_request_language() -> None:
    zh_warning, zh_stop = _run_to_hard_stop("zh")
    en_warning, en_stop = _run_to_hard_stop("en")

    assert "检测到循环" in zh_warning
    assert "安全停止" in zh_stop
    assert "LOOP DETECTED" not in zh_warning
    assert "SAFETY STOP" not in zh_stop

    assert "LOOP DETECTED" in en_warning
    assert "SAFETY STOP" in en_stop
    assert "检测到循环" not in en_warning
    assert "安全停止" not in en_stop


def test_hard_stop_copy_does_not_promise_an_unstarted_final_answer() -> None:
    _, zh_stop = _run_to_hard_stop("zh")
    _, en_stop = _run_to_hard_stop("en")

    assert "正在生成最终" not in zh_stop
    assert "Producing final answer" not in en_stop
