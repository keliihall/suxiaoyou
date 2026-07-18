"""Native tests for bounded POSIX process supervision."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import sys
import time

import pytest

from app.tool.posix_process import run_posix_process


pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX process supervisor tests require macOS or Linux",
)


def _run_python(
    code: str,
    tmp_path: Path,
    *,
    timeout: float = 5,
    should_abort=lambda: False,
    max_output_bytes: int = 64 * 1024,
):
    return run_posix_process(
        [sys.executable, "-I", "-u", "-c", code],
        cwd=tmp_path,
        env=os.environ.copy(),
        timeout_seconds=timeout,
        should_abort=should_abort,
        max_output_bytes=max_output_bytes,
    )


def _wait_for_absence(path: Path, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)


def test_returns_exit_and_both_output_streams(tmp_path: Path) -> None:
    result = _run_python(
        "import sys; print('stdout-ok'); print('stderr-ok', file=sys.stderr)",
        tmp_path,
    )

    assert result.exit_code == 0
    assert result.stdout == b"stdout-ok\n"
    assert result.stderr == b"stderr-ok\n"
    assert result.termination is None
    assert result.truncated is False


def test_bounded_runner_can_deliver_stdin_without_using_argv_or_env(
    tmp_path: Path,
) -> None:
    payload = b'{"version":1,"event":"PreToolUse"}\n'
    result = run_posix_process(
        [sys.executable, "-I", "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
        cwd=tmp_path,
        env=os.environ.copy(),
        timeout_seconds=5,
        should_abort=lambda: False,
        max_output_bytes=4096,
        stdin_bytes=payload,
    )

    assert result.exit_code == 0
    assert result.stdout == payload


def test_nonzero_exit_is_returned_without_becoming_a_termination(tmp_path: Path) -> None:
    result = _run_python("raise SystemExit(17)", tmp_path)

    assert result.exit_code == 17
    assert result.termination is None


def test_child_is_its_own_session_and_process_group_leader(tmp_path: Path) -> None:
    result = _run_python(
        "import os; print(os.getpid(), os.getsid(0), os.getpgrp())",
        tmp_path,
    )

    pid, session_id, process_group = map(int, result.stdout.split())
    assert pid == session_id == process_group


def test_output_flood_is_drained_but_retained_bytes_are_bounded(tmp_path: Path) -> None:
    limit = 4096
    result = _run_python(
        """
import os, threading
chunk = b'x' * 65536
def write_all(fd):
    for _ in range(16):
        os.write(fd, chunk)
t1 = threading.Thread(target=write_all, args=(1,))
t2 = threading.Thread(target=write_all, args=(2,))
t1.start(); t2.start(); t1.join(); t2.join()
""",
        tmp_path,
        max_output_bytes=limit,
    )

    assert result.exit_code == 0
    assert len(result.stdout) == limit
    assert len(result.stderr) == limit
    assert result.truncated is True


def test_continuous_output_cannot_starve_timeout_check(tmp_path: Path) -> None:
    started = time.monotonic()

    result = _run_python(
        "import os\nchunk=b'x'*65536\nwhile True: os.write(1, chunk)",
        tmp_path,
        timeout=0.1,
        max_output_bytes=1024,
    )

    assert result.termination == "timeout"
    assert len(result.stdout) == 1024
    assert result.truncated is True
    assert time.monotonic() - started < 2


def test_normal_parent_exit_reaps_background_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "normal-descendant-survived"
    child_code = (
        "import pathlib,signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(1); "
        f"pathlib.Path({str(marker)!r}).write_text('survived')"
    )
    result = _run_python(
        "import subprocess,sys; "
        f"subprocess.Popen([sys.executable, '-I', '-c', {child_code!r}]); "
        "print('parent-complete')",
        tmp_path,
    )

    assert result.exit_code == 0
    assert result.termination is None
    assert b"parent-complete" in result.stdout
    time.sleep(1.1)
    assert not marker.exists()


def test_timeout_escalates_to_sigkill_and_reaps_group(tmp_path: Path) -> None:
    marker = tmp_path / "timeout-parent-survived"
    code = (
        "import pathlib,signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(1); "
        f"pathlib.Path({str(marker)!r}).write_text('survived')"
    )
    started = time.monotonic()

    result = _run_python(code, tmp_path, timeout=0.1)

    assert result.termination == "timeout"
    assert result.exit_code == -signal.SIGKILL
    assert time.monotonic() - started < 2
    time.sleep(1.1)
    assert not marker.exists()


def test_abort_callback_escalates_and_reaps_group(tmp_path: Path) -> None:
    marker = tmp_path / "aborted-parent-survived"
    started = time.monotonic()

    def should_abort() -> bool:
        return time.monotonic() - started >= 0.1

    result = _run_python(
        (
            "import pathlib,signal,time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "time.sleep(1); "
            f"pathlib.Path({str(marker)!r}).write_text('survived')"
        ),
        tmp_path,
        should_abort=should_abort,
    )

    assert result.termination == "aborted"
    assert result.exit_code == -signal.SIGKILL
    time.sleep(1.1)
    assert not marker.exists()


def test_preexisting_abort_does_not_launch_process(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist"

    result = _run_python(
        f"from pathlib import Path; Path({str(marker)!r}).write_text('bad')",
        tmp_path,
        should_abort=lambda: True,
    )

    assert result.exit_code == -1
    assert result.termination == "aborted"
    assert not marker.exists()


def test_abort_callback_exception_is_reraised_after_cleanup(tmp_path: Path) -> None:
    marker = tmp_path / "callback-error-process-survived"
    callback_calls = 0

    def should_abort() -> bool:
        nonlocal callback_calls
        callback_calls += 1
        if callback_calls > 1:
            raise RuntimeError("abort probe failed")
        return False

    with pytest.raises(RuntimeError, match="abort probe failed"):
        _run_python(
            (
                "import pathlib,time; time.sleep(1); "
                f"pathlib.Path({str(marker)!r}).write_text('survived')"
            ),
            tmp_path,
            should_abort=should_abort,
        )

    time.sleep(1.1)
    assert not marker.exists()


@pytest.mark.parametrize(
    ("timeout", "max_output", "message"),
    [
        (0, 1, "timeout_seconds"),
        (float("inf"), 1, "timeout_seconds"),
        (1, -1, "max_output_bytes"),
    ],
)
def test_rejects_invalid_limits(
    tmp_path: Path,
    timeout: float,
    max_output: int,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        run_posix_process(
            [sys.executable, "-c", "pass"],
            cwd=tmp_path,
            env=os.environ.copy(),
            timeout_seconds=timeout,
            should_abort=lambda: False,
            max_output_bytes=max_output,
        )
