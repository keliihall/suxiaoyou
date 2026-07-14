"""Desktop launcher runtime configuration regression tests."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest

import run


def test_main_uses_cli_port_for_settings_and_uvicorn(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    original_env = "SUXIAOYOU_PORT=8000\nSUXIAOYOU_HOST=0.0.0.0\n"
    env_file.write_text(original_env, encoding="utf-8")

    created_app = object()
    create_app = Mock(return_value=created_app)
    uvicorn_run = Mock()

    fake_app_main = ModuleType("app.main")
    fake_app_main.create_app = create_app
    fake_uvicorn = ModuleType("uvicorn")
    fake_uvicorn.run = uvicorn_run

    monkeypatch.setitem(sys.modules, "app.main", fake_app_main)
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.setattr(run, "_install_crash_reporter", lambda: None)
    windows_job = Mock()
    monkeypatch.setattr(run, "_configure_windows_process_job", windows_job)
    parent_watchdog = Mock()
    monkeypatch.setattr(run, "_start_desktop_parent_watchdog", parent_watchdog)
    # Record process-global state with monkeypatch so run.py's intentional
    # chdir/env mutations are restored for tests that execute afterwards.
    monkeypatch.chdir(tmp_path.parent)
    monkeypatch.delenv("SUXIAOYOU_HOST", raising=False)
    monkeypatch.delenv("SUXIAOYOU_PORT", raising=False)
    monkeypatch.delenv("SUXIAOYOU_RESOURCE_DIR", raising=False)
    monkeypatch.delenv("SUXIAOYOU_NODE_BIN_DIR", raising=False)
    monkeypatch.delenv(run._APP_PRIVATE_DIR_ENV, raising=False)
    monkeypatch.setenv("PATH", run.os.environ.get("PATH", ""))
    resource_dir = tmp_path / "resources"
    node_bin_dir = resource_dir / "nodejs"
    if run.os.name != "nt":
        node_bin_dir /= "bin"
    node_bin_dir.mkdir(parents=True)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run.py",
            "--port",
            "17321",
            "--data-dir",
            str(tmp_path),
            "--resource-dir",
            str(resource_dir),
        ],
    )

    run.main()

    settings = create_app.call_args.args[0]
    assert settings.host == "127.0.0.1"
    assert settings.port == 17321
    uvicorn_run.assert_called_once_with(
        created_app,
        host="127.0.0.1",
        port=17321,
        log_level="info",
    )
    assert env_file.read_text(encoding="utf-8") == original_env
    assert Path(run.os.environ["PATH"].split(run.os.pathsep)[0]) == node_bin_dir
    assert Path(run.os.environ["SUXIAOYOU_NODE_BIN_DIR"]) == node_bin_dir
    assert Path(run.os.environ[run._APP_PRIVATE_DIR_ENV]) == tmp_path.resolve()
    windows_job.assert_called_once_with()
    parent_watchdog.assert_called_once_with()


def test_parent_watchdog_waits_then_terminates_backend():
    events = []

    run._watch_desktop_parent(
        43123,
        wait_for_exit=lambda pid: (events.append(("wait", pid)), True)[1],
        terminate_backend=lambda: events.append(("terminate", None)),
    )

    assert events == [("wait", 43123), ("terminate", None)]


def test_parent_watchdog_does_not_terminate_when_parent_observation_fails():
    terminate = Mock()

    run._watch_desktop_parent(
        43123,
        wait_for_exit=lambda _pid: False,
        terminate_backend=terminate,
    )

    terminate.assert_not_called()


def test_invalid_parent_pid_does_not_start_watchdog(monkeypatch):
    thread = Mock()
    monkeypatch.setattr(run.threading, "Thread", thread)

    for value in ("not-a-pid", "0", "1", str(run.os.getpid())):
        monkeypatch.setenv(run._DESKTOP_PARENT_PID_ENV, value)
        assert run._start_desktop_parent_watchdog() is None

    thread.assert_not_called()


@pytest.mark.skipif(run.os.name == "nt", reason="Unix process-group contract")
def test_parent_exit_cleanup_survives_closed_tauri_stderr_pipe(monkeypatch):
    class ClosedPipe:
        def write(self, _value):
            raise BrokenPipeError("desktop log reader is gone")

        def flush(self):
            raise BrokenPipeError("desktop log reader is gone")

    killpg = Mock()
    hard_exit = Mock()
    reaper = Mock()
    monkeypatch.setattr(run.sys, "stderr", ClosedPipe())
    monkeypatch.setattr(run.os, "getpid", lambda: 43123)
    monkeypatch.setattr(run.os, "getpgrp", lambda: 43123)
    monkeypatch.setattr(run.os, "killpg", killpg)
    monkeypatch.setattr(run, "_launch_unix_process_group_reaper", reaper)
    monkeypatch.setattr(run.time, "sleep", Mock())
    monkeypatch.setattr(run.os, "_exit", hard_exit)

    run._terminate_backend_after_parent_exit(grace_seconds=0)

    reaper.assert_called_once_with(43123, 0)
    killpg.assert_called_once_with(43123, run.signal.SIGTERM)
    hard_exit.assert_called_once_with(0)


@pytest.mark.skipif(run.os.name == "nt", reason="Unix process-group contract")
def test_parent_exit_cleanup_refuses_to_signal_shared_process_group(monkeypatch):
    killpg = Mock()
    reaper = Mock()
    hard_exit = Mock()
    monkeypatch.setattr(run.os, "getpid", lambda: 43123)
    monkeypatch.setattr(run.os, "getpgrp", lambda: 43000)
    monkeypatch.setattr(run.os, "killpg", killpg)
    monkeypatch.setattr(run, "_launch_unix_process_group_reaper", reaper)
    monkeypatch.setattr(run.os, "_exit", hard_exit)

    run._terminate_backend_after_parent_exit(grace_seconds=0)

    hard_exit.assert_called_once_with(0)
    reaper.assert_not_called()
    killpg.assert_not_called()


def test_windows_parent_exit_cleanup_hard_exits_without_unix_apis(monkeypatch):
    hard_exit = Mock()
    monkeypatch.setattr(run.os, "_exit", hard_exit)

    run._terminate_backend_after_parent_exit(platform_name="nt")

    hard_exit.assert_called_once_with(0)


@pytest.mark.skipif(run.os.name == "nt", reason="Unix parent reparenting contract")
def test_unix_parent_wait_observes_reparenting(monkeypatch):
    parent_pid = 43123
    parents = iter([parent_pid, parent_pid, 1])
    sleeps = []
    monkeypatch.setattr(run.os, "getppid", lambda: next(parents))
    monkeypatch.setattr(run.time, "sleep", lambda seconds: sleeps.append(seconds))

    assert run._wait_for_desktop_parent_exit(parent_pid) is True
    assert sleeps == [0.5, 0.5]


def test_valid_parent_pid_starts_watchdog_thread(monkeypatch):
    thread = Mock()
    thread_type = Mock(return_value=thread)
    monkeypatch.setattr(run.threading, "Thread", thread_type)
    monkeypatch.setenv(run._DESKTOP_PARENT_PID_ENV, str(run.os.getpid() + 1000))

    assert run._start_desktop_parent_watchdog() is thread
    thread.start.assert_called_once_with()
    thread_type.assert_called_once()


def _process_is_effectively_running(pid: int) -> bool:
    result = subprocess.run(
        ["ps", "-o", "state=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    state = result.stdout.strip()
    return result.returncode == 0 and bool(state) and not state.startswith("Z")


@pytest.mark.skipif(run.os.name == "nt", reason="Unix process-group contract")
def test_unix_reaper_kills_term_ignoring_backend_and_helper():
    child_code = """
import signal
import subprocess
import sys
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
helper = subprocess.Popen([
    sys.executable,
    "-c",
    "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
])
print(helper.pid, flush=True)
time.sleep(30)
"""
    backend = subprocess.Popen(
        [sys.executable, "-c", child_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert backend.stdout is not None
    helper_pid = int(backend.stdout.readline().strip())

    try:
        run._launch_unix_process_group_reaper(backend.pid, 0.25)
        os.killpg(backend.pid, signal.SIGTERM)
        backend.wait(timeout=3)
        assert backend.returncode == -signal.SIGKILL

        deadline = time.monotonic() + 3
        while _process_is_effectively_running(helper_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _process_is_effectively_running(helper_pid)
    finally:
        try:
            os.killpg(backend.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@pytest.mark.skipif(run.os.name != "nt", reason="Windows Job Object contract")
def test_windows_job_object_kills_inherited_child_when_backend_exits():
    worker_code = """
import os
import subprocess
import sys

import run

assert run._configure_windows_process_job() is not None
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
print(child.pid, flush=True)
os._exit(0)
"""
    worker = subprocess.Popen(
        [
            sys.executable,
            "-c",
            worker_code,
        ],
        cwd=Path(run.__file__).parent,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert worker.stdout is not None
    child_pid = int(worker.stdout.readline().strip())
    worker.wait(timeout=5)
    assert worker.returncode == 0

    deadline = time.monotonic() + 5
    child_running = True
    while child_running and time.monotonic() < deadline:
        tasklist = subprocess.run(
            ["tasklist", "/FI", f"PID eq {child_pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        child_running = str(child_pid) in tasklist.stdout
        if child_running:
            time.sleep(0.05)

    assert not child_running
