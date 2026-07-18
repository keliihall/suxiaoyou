from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

from app.office_rendering import (
    LocalProcessTreeRunner,
    RenderContractError,
    RenderProcessResult,
    RenderTimeoutError,
)
from app.tool.posix_process import PosixProcessResult
from app.tool.windows_process import WindowsProcessResult


def _executable(path: Path) -> Path:
    path.write_bytes(b"private renderer executable")
    path.chmod(0o700)
    return path.resolve()


@pytest.mark.asyncio
async def test_local_runner_delegates_argv_to_posix_tree_supervisor(
    tmp_path: Path,
) -> None:
    recorded: dict[str, object] = {}

    def supervisor(argv, **kwargs) -> PosixProcessResult:
        recorded["argv"] = tuple(argv)
        recorded.update(kwargs)
        return PosixProcessResult(
            exit_code=0,
            stdout=b"out",
            stderr=b"err",
            termination=None,
            truncated=False,
        )

    runner = LocalProcessTreeRunner(
        platform_name="posix",
        _posix_supervisor=supervisor,
    )
    executable = _executable(tmp_path / "fake-tool")

    result = await runner.run(
        (str(executable), "--safe", "value with spaces"),
        cwd=tmp_path,
        env={"PATH": str(tmp_path)},
        timeout_seconds=3,
    )

    assert result == RenderProcessResult(0, b"out", b"err")
    assert recorded["argv"] == (
        str(executable),
        "--safe",
        "value with spaces",
    )
    assert "shell" not in recorded
    assert callable(recorded["should_abort"])
    assert recorded["max_output_bytes"] == 64 * 1024


@pytest.mark.asyncio
async def test_local_runner_maps_windows_job_object_result(tmp_path: Path) -> None:
    recorded: dict[str, object] = {}

    def supervisor(argv, **kwargs) -> WindowsProcessResult:
        recorded["argv"] = tuple(argv)
        recorded.update(kwargs)
        return WindowsProcessResult(
            exit_code=0,
            stdout=b"windows out",
            stderr=b"",
            termination=None,
            pid=42,
            stdout_total_bytes=11,
            stderr_total_bytes=0,
            stdout_truncated=False,
            stderr_truncated=False,
        )

    runner = LocalProcessTreeRunner(
        platform_name="nt",
        _windows_supervisor=supervisor,
    )
    executable = _executable(tmp_path / "tool.exe")
    result = await runner.run(
        (str(executable), "--headless"),
        cwd=tmp_path,
        env={"PATH": str(tmp_path)},
        timeout_seconds=2,
    )

    assert result.returncode == 0
    assert result.stdout == b"windows out"
    assert "shell" not in recorded
    assert callable(recorded["should_abort"])


@pytest.mark.asyncio
async def test_local_runner_timeout_is_explicit_after_supervisor_cleanup(
    tmp_path: Path,
) -> None:
    def supervisor(_argv, **_kwargs) -> PosixProcessResult:
        return PosixProcessResult(
            exit_code=-15,
            stdout=b"",
            stderr=b"",
            termination="timeout",
            truncated=False,
        )

    runner = LocalProcessTreeRunner(
        platform_name="posix",
        _posix_supervisor=supervisor,
    )
    executable = _executable(tmp_path / "tool")
    with pytest.raises(RenderTimeoutError, match="process tree was terminated"):
        await runner.run(
            (str(executable),),
            cwd=tmp_path,
            env={},
            timeout_seconds=1,
        )


@pytest.mark.asyncio
async def test_cancellation_waits_for_process_tree_abort_handshake(
    tmp_path: Path,
) -> None:
    started = threading.Event()
    reaped = threading.Event()

    def supervisor(_argv, **kwargs) -> PosixProcessResult:
        should_abort = kwargs["should_abort"]
        started.set()
        deadline = time.monotonic() + 2
        while not should_abort() and time.monotonic() < deadline:
            time.sleep(0.002)
        if should_abort():
            reaped.set()
        return PosixProcessResult(
            exit_code=-15,
            stdout=b"",
            stderr=b"",
            termination="aborted",
            truncated=False,
        )

    runner = LocalProcessTreeRunner(
        platform_name="posix",
        _posix_supervisor=supervisor,
    )
    executable = _executable(tmp_path / "tool")
    task = asyncio.create_task(
        runner.run(
            (str(executable),),
            cwd=tmp_path,
            env={},
            timeout_seconds=10,
        )
    )
    assert await asyncio.to_thread(started.wait, 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert reaped.is_set()


@pytest.mark.asyncio
async def test_repeated_cancellation_cannot_bypass_process_tree_reap(
    tmp_path: Path,
) -> None:
    started = threading.Event()
    abort_seen = threading.Event()
    allow_reap = threading.Event()
    reaped = threading.Event()

    def supervisor(_argv, **kwargs) -> PosixProcessResult:
        should_abort = kwargs["should_abort"]
        started.set()
        deadline = time.monotonic() + 2
        while not should_abort() and time.monotonic() < deadline:
            time.sleep(0.002)
        abort_seen.set()
        allow_reap.wait(timeout=2)
        reaped.set()
        return PosixProcessResult(
            exit_code=-15,
            stdout=b"",
            stderr=b"",
            termination="aborted",
            truncated=False,
        )

    runner = LocalProcessTreeRunner(
        platform_name="posix",
        _posix_supervisor=supervisor,
    )
    executable = _executable(tmp_path / "tool")
    task = asyncio.create_task(
        runner.run(
            (str(executable),),
            cwd=tmp_path,
            env={},
            timeout_seconds=10,
        )
    )
    assert await asyncio.to_thread(started.wait, 1)
    task.cancel()
    assert await asyncio.to_thread(abort_seen.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    assert not reaped.is_set()

    allow_reap.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert reaped.is_set()


@pytest.mark.asyncio
async def test_runner_rejects_proxy_environment_and_redirected_paths(
    tmp_path: Path,
) -> None:
    supervisor_called = False

    def supervisor(_argv, **_kwargs) -> PosixProcessResult:
        nonlocal supervisor_called
        supervisor_called = True
        raise AssertionError("unsafe invocation reached the process supervisor")

    runner = LocalProcessTreeRunner(
        platform_name="posix",
        _posix_supervisor=supervisor,
    )
    executable = _executable(tmp_path / "tool")
    with pytest.raises(RenderContractError, match="not allowed"):
        await runner.run(
            (str(executable),),
            cwd=tmp_path,
            env={"PATH": str(tmp_path), "HTTPS_PROXY": "http://host-proxy"},
            timeout_seconds=1,
        )
    with pytest.raises(RenderContractError, match="escaped"):
        await runner.run(
            (str(executable),),
            cwd=tmp_path,
            env={"PATH": str(tmp_path), "HOME": str(tmp_path.parent)},
            timeout_seconds=1,
        )

    redirected = tmp_path / "redirected-tool"
    try:
        redirected.symlink_to(executable)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    with pytest.raises(RenderContractError, match="redirected"):
        await runner.run(
            (str(redirected),),
            cwd=tmp_path,
            env={"PATH": str(tmp_path)},
            timeout_seconds=1,
        )
    assert supervisor_called is False


@pytest.mark.asyncio
async def test_windows_runner_rejects_case_ambiguous_environment(
    tmp_path: Path,
) -> None:
    runner = LocalProcessTreeRunner(
        platform_name="nt",
        _windows_supervisor=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("ambiguous environment reached Windows")
        ),
    )
    executable = _executable(tmp_path / "tool.exe")

    with pytest.raises(RenderContractError, match="ambiguous"):
        await runner.run(
            (str(executable),),
            cwd=tmp_path,
            env={"PATH": str(tmp_path), "Path": str(tmp_path)},
            timeout_seconds=1,
        )
