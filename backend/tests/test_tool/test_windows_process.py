"""Platform-neutral state-machine tests for the Win32 process runner."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import threading
import time
from collections.abc import Mapping, Sequence

import pytest

from app.tool.windows_process import (
    CtypesWindowsProcessApi,
    WindowsChildProcess,
    WindowsProcessError,
    run_windows_process,
)


class _FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance_ms(self, milliseconds: int) -> None:
        self.value += milliseconds / 1000


class _DrainStream:
    """Small-chunk byte stream that records whether all output was drained."""

    def __init__(self, data: bytes, *, chunk_size: int = 7) -> None:
        self._data = data
        self._chunk_size = chunk_size
        self._offset = 0
        self._lock = threading.Lock()
        self.closed = False

    @property
    def bytes_read(self) -> int:
        return self._offset

    def read(self, _size: int = -1) -> bytes:
        with self._lock:
            if self.closed or self._offset >= len(self._data):
                return b""
            end = min(len(self._data), self._offset + self._chunk_size)
            chunk = self._data[self._offset:end]
            self._offset = end
            return chunk

    def close(self) -> None:
        with self._lock:
            self.closed = True


class _FakeWindowsApi:
    def __init__(
        self,
        *,
        clock: _FakeClock,
        stdout: bytes = b"",
        stderr: bytes = b"",
        exit_after_waits: int | None = 1,
        exit_code: int = 0,
        create_error: BaseException | None = None,
        assign_error: BaseException | None = None,
        resume_error: BaseException | None = None,
        wait_error: BaseException | None = None,
    ) -> None:
        self.clock = clock
        self.stdout_stream = _DrainStream(stdout)
        self.stderr_stream = _DrainStream(stderr)
        self.exit_after_waits = exit_after_waits
        self.exit_code = exit_code
        self.create_error = create_error
        self.assign_error = assign_error
        self.resume_error = resume_error
        self.wait_error = wait_error
        self.events: list[str] = []
        self.wait_calls = 0
        self.job_terminated = False
        self.process_terminated = False
        self.stdin_bytes: bytes | None = None

    def create_kill_on_close_job(self) -> object:
        self.events.append("create_job")
        return "job"

    def create_suspended_process(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        stdin_bytes: bytes | None = None,
    ) -> WindowsChildProcess:
        assert list(argv) == [r"C:\Windows\System32\cmd.exe", "/c", "echo ok"]
        assert cwd == r"C:\workspace"
        assert env == {"SAFE": "1"}
        self.stdin_bytes = stdin_bytes
        self.events.append("create_suspended")
        if self.create_error is not None:
            raise self.create_error
        return WindowsChildProcess(
            process_handle="process",
            thread_handle="thread",
            pid=4312,
            stdout=self.stdout_stream,  # type: ignore[arg-type]
            stderr=self.stderr_stream,  # type: ignore[arg-type]
        )

    def assign_process_to_job(
        self,
        job: object,
        process: WindowsChildProcess,
    ) -> None:
        assert job == "job"
        assert process.thread_handle == "thread"
        self.events.append("assign")
        if self.assign_error is not None:
            raise self.assign_error

    def resume_process(self, process: WindowsChildProcess) -> None:
        assert process.thread_handle == "thread"
        self.events.append("resume")
        if self.resume_error is not None:
            raise self.resume_error
        process.thread_handle = None

    def wait_process(self, process: WindowsChildProcess, timeout_ms: int) -> bool:
        assert process.process_handle == "process"
        self.events.append("wait")
        self.wait_calls += 1
        self.clock.advance_ms(timeout_ms)
        if self.wait_error is not None:
            error, self.wait_error = self.wait_error, None
            raise error
        if self.job_terminated or self.process_terminated:
            return True
        return (
            self.exit_after_waits is not None
            and self.wait_calls >= self.exit_after_waits
        )

    def get_exit_code(self, process: WindowsChildProcess) -> int:
        assert process.process_handle == "process"
        self.events.append("get_exit_code")
        if self.job_terminated or self.process_terminated:
            return 0xC000013A
        return self.exit_code

    def terminate_job(self, job: object, exit_code: int) -> None:
        assert job == "job"
        assert exit_code == 0xC000013A
        self.events.append("terminate_job")
        self.job_terminated = True

    def terminate_process(self, process: WindowsChildProcess, exit_code: int) -> None:
        assert process.process_handle == "process"
        assert exit_code == 0xC000013A
        self.events.append("terminate_process")
        self.process_terminated = True

    def close_job(self, job: object) -> None:
        assert job == "job"
        self.events.append("close_job")
        # KILL_ON_JOB_CLOSE also terminates a still-running assigned child.
        self.job_terminated = True

    def close_process(self, process: WindowsChildProcess) -> None:
        assert process.process_handle == "process"
        self.events.append("close_process")


class _DelayedTerminationWindowsApi(_FakeWindowsApi):
    """Make the grace wait expire until kill-on-close supplies the hard stop."""

    def wait_process(self, process: WindowsChildProcess, timeout_ms: int) -> bool:
        if self.job_terminated and "close_job" not in self.events:
            assert process.process_handle == "process"
            self.events.append("wait")
            self.wait_calls += 1
            self.clock.advance_ms(timeout_ms)
            return False
        return super().wait_process(process, timeout_ms)


_ARGV = [r"C:\Windows\System32\cmd.exe", "/c", "echo ok"]
_CWD = r"C:\workspace"
_ENV = {"SAFE": "1"}
WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32",
    reason="requires native Windows process and Job Object APIs",
)


def _run(api: _FakeWindowsApi, **kwargs):
    return run_windows_process(
        _ARGV,
        cwd=_CWD,
        env=_ENV,
        timeout_seconds=10,
        api=api,
        poll_interval_seconds=0.1,
        termination_grace_seconds=0.1,
        _clock=api.clock,
        **kwargs,
    )


def test_suspended_process_is_assigned_before_resume_and_job_closes_normally() -> None:
    clock = _FakeClock()
    api = _FakeWindowsApi(
        clock=clock,
        stdout=b"stdout-data",
        stderr=b"stderr-data",
    )

    result = _run(api)

    assert result.exit_code == 0
    assert result.termination is None
    assert result.stdout == b"stdout-data"
    assert result.stderr == b"stderr-data"
    assert api.events[:4] == [
        "create_job",
        "create_suspended",
        "assign",
        "resume",
    ]
    assert api.events.count("close_job") == 1
    assert api.events.index("get_exit_code") < api.events.index("close_job")
    assert api.events[-1] == "close_process"


def test_stdin_bytes_are_bound_to_the_suspended_process_creation() -> None:
    clock = _FakeClock()
    api = _FakeWindowsApi(clock=clock)

    result = _run(api, stdin_bytes=b'{"version":1}\n')

    assert result.exit_code == 0
    assert api.stdin_bytes == b'{"version":1}\n'


def test_output_is_bounded_but_both_pipes_are_fully_drained() -> None:
    clock = _FakeClock()
    stdout = b"x" * 200_000
    stderr = b"y" * 180_000
    api = _FakeWindowsApi(clock=clock, stdout=stdout, stderr=stderr)

    result = _run(api, max_output_bytes=31)

    assert result.stdout == b"x" * 31
    assert result.stderr == b"y" * 31
    assert result.stdout_total_bytes == len(stdout)
    assert result.stderr_total_bytes == len(stderr)
    assert result.stdout_truncated is True
    assert result.stderr_truncated is True
    assert api.stdout_stream.bytes_read == len(stdout)
    assert api.stderr_stream.bytes_read == len(stderr)
    assert api.stdout_stream.closed is True
    assert api.stderr_stream.closed is True


def test_assignment_failure_terminates_suspended_process_without_resuming() -> None:
    clock = _FakeClock()
    api = _FakeWindowsApi(
        clock=clock,
        assign_error=WindowsProcessError("assignment denied"),
    )

    with pytest.raises(WindowsProcessError, match="assignment denied"):
        _run(api)

    assert "resume" not in api.events
    assert api.events.index("assign") < api.events.index("terminate_process")
    assert api.events.count("terminate_process") == 1
    assert api.events.count("close_job") == 1
    assert api.events[-1] == "close_process"


def test_process_creation_failure_still_closes_the_job() -> None:
    clock = _FakeClock()
    api = _FakeWindowsApi(
        clock=clock,
        create_error=WindowsProcessError("create failed"),
    )

    with pytest.raises(WindowsProcessError, match="create failed"):
        _run(api)

    assert api.events == ["create_job", "create_suspended", "close_job"]


def test_resume_failure_closes_assigned_job_and_all_handles() -> None:
    clock = _FakeClock()
    api = _FakeWindowsApi(
        clock=clock,
        resume_error=WindowsProcessError("resume failed"),
    )

    with pytest.raises(WindowsProcessError, match="resume failed"):
        _run(api)

    assert "terminate_process" not in api.events
    assert api.events.index("assign") < api.events.index("resume")
    assert api.events.index("resume") < api.events.index("close_job")
    assert api.events[-1] == "close_process"


def test_timeout_terminates_job_then_closes_it() -> None:
    clock = _FakeClock()
    api = _FakeWindowsApi(clock=clock, exit_after_waits=None)

    result = run_windows_process(
        _ARGV,
        cwd=_CWD,
        env=_ENV,
        timeout_seconds=0.25,
        api=api,
        poll_interval_seconds=0.1,
        termination_grace_seconds=0.1,
        _clock=clock,
    )

    assert result.termination == "timeout"
    assert result.exit_code == 0xC000013A
    assert api.events.count("terminate_job") == 1
    assert api.events.index("terminate_job") < api.events.index("close_job")
    assert api.events[-1] == "close_process"


def test_timeout_does_not_read_still_active_exit_code_before_final_job_close() -> None:
    clock = _FakeClock()
    api = _DelayedTerminationWindowsApi(clock=clock, exit_after_waits=None)

    result = run_windows_process(
        _ARGV,
        cwd=_CWD,
        env=_ENV,
        timeout_seconds=0.25,
        api=api,
        poll_interval_seconds=0.1,
        termination_grace_seconds=0.1,
        _clock=clock,
    )

    assert result.termination == "timeout"
    assert result.exit_code == 0xC000013A
    assert api.events.index("close_job") < api.events.index("get_exit_code")


def test_abort_callback_terminates_entire_job() -> None:
    clock = _FakeClock()
    api = _FakeWindowsApi(clock=clock, exit_after_waits=None)

    result = _run(api, should_abort=lambda: api.wait_calls >= 1)

    assert result.termination == "aborted"
    assert api.wait_calls >= 2  # one poll plus the post-termination wait
    assert api.events.count("terminate_job") == 1
    assert api.events.count("close_job") == 1


def test_preexisting_abort_does_not_create_a_windows_process() -> None:
    clock = _FakeClock()
    api = _FakeWindowsApi(clock=clock)

    result = _run(api, should_abort=lambda: True)

    assert result.termination == "aborted"
    assert result.pid == -1
    assert api.events == []


def test_wait_exception_still_closes_job_streams_and_process_handles() -> None:
    clock = _FakeClock()
    api = _FakeWindowsApi(
        clock=clock,
        exit_after_waits=None,
        wait_error=WindowsProcessError("wait failed"),
    )

    with pytest.raises(WindowsProcessError, match="wait failed"):
        _run(api)

    assert api.events.count("close_job") == 1
    assert api.stdout_stream.closed is True
    assert api.stderr_stream.closed is True
    assert api.events[-1] == "close_process"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"timeout_seconds": 0}, "timeout"),
        ({"poll_interval_seconds": 0}, "poll interval"),
        ({"termination_grace_seconds": 0}, "termination grace"),
        ({"max_output_bytes": -1}, "output limit"),
    ],
)
def test_invalid_limits_fail_before_creating_native_objects(
    kwargs: dict[str, float | int],
    message: str,
) -> None:
    clock = _FakeClock()
    api = _FakeWindowsApi(clock=clock)
    values = {
        "timeout_seconds": 1,
        "poll_interval_seconds": 0.1,
        "termination_grace_seconds": 0.1,
        "max_output_bytes": 100,
        **kwargs,
    }

    with pytest.raises(ValueError, match=message):
        run_windows_process(
            _ARGV,
            cwd=_CWD,
            env=_ENV,
            api=api,
            _clock=clock,
            **values,
        )

    assert api.events == []


def test_environment_block_is_sorted_and_double_nul_terminated() -> None:
    block = CtypesWindowsProcessApi._environment_block(
        {"zeta": "2", "Alpha": "1"}
    )

    assert block == "Alpha=1\0zeta=2\0\0"


def test_ctypes_api_fails_cleanly_off_windows() -> None:
    if sys.platform == "win32":
        pytest.skip("native Windows constructor is covered by the CI runner")

    with pytest.raises(WindowsProcessError, match="native Windows"):
        CtypesWindowsProcessApi()


@WINDOWS_ONLY
def test_native_ctypes_runner_executes_suspended_child_and_captures_output(
    tmp_path: Path,
) -> None:
    result = run_windows_process(
        [
            sys.executable,
            "-c",
            "import os,sys;print(os.environ['SUXIAOYOU_TEST']);"
            "print('native-stderr', file=sys.stderr)",
        ],
        cwd=str(tmp_path),
        env={**os.environ, "SUXIAOYOU_TEST": "native-stdout"},
        timeout_seconds=10,
    )

    assert result.exit_code == 0
    assert result.termination is None
    assert result.stdout.strip() == b"native-stdout"
    assert result.stderr.strip() == b"native-stderr"


@WINDOWS_ONLY
def test_native_ctypes_runner_delivers_stdin(tmp_path: Path) -> None:
    payload = b'{"version":1,"event":"PreToolUse"}\n'
    result = run_windows_process(
        [
            sys.executable,
            "-c",
            "import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())",
        ],
        cwd=str(tmp_path),
        env=os.environ,
        timeout_seconds=10,
        stdin_bytes=payload,
    )

    assert result.exit_code == 0
    assert result.stdout == payload


@WINDOWS_ONLY
def test_native_job_close_kills_descendant_after_parent_exits(tmp_path: Path) -> None:
    escaped_marker = tmp_path / "descendant-escaped.txt"
    child_code = (
        "import pathlib,time;time.sleep(1);"
        f"pathlib.Path({str(escaped_marker)!r}).write_text('escaped')"
    )
    parent_code = (
        "import subprocess,sys;"
        f"child=subprocess.Popen([sys.executable, '-c', {child_code!r}]);"
        "print(child.pid, flush=True)"
    )

    result = run_windows_process(
        [sys.executable, "-c", parent_code],
        cwd=str(tmp_path),
        env=os.environ,
        timeout_seconds=10,
    )

    assert result.exit_code == 0
    assert result.stdout.strip().isdigit()
    time.sleep(1.5)
    assert not escaped_marker.exists()
