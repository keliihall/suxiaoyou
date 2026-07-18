"""Desktop launcher runtime configuration regression tests."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, Mock

import pytest

import run


def test_main_dispatches_acp_stdio_before_server_lifecycle(monkeypatch):
    fake_acp_cli = ModuleType("app.acp.cli")
    acp_main = Mock(side_effect=SystemExit(78))
    fake_acp_cli.main = acp_main
    monkeypatch.setitem(sys.modules, "app.acp.cli", fake_acp_cli)
    crash_reporter = Mock()
    windows_job = Mock()
    parent_watchdog = Mock()
    monkeypatch.setattr(run, "_install_crash_reporter", crash_reporter)
    monkeypatch.setattr(run, "_configure_windows_process_job", windows_job)
    monkeypatch.setattr(run, "_start_desktop_parent_watchdog", parent_watchdog)
    monkeypatch.setattr(sys, "argv", ["run.py", "--acp-stdio"])

    with pytest.raises(SystemExit) as exit_info:
        run.main()

    assert exit_info.value.code == 78
    acp_main.assert_called_once_with()
    crash_reporter.assert_not_called()
    windows_job.assert_not_called()
    parent_watchdog.assert_not_called()


def test_main_dispatches_office_self_test_before_server_lifecycle(monkeypatch, capsys):
    fake_contract = ModuleType("app.tool.office_contract")
    contract_runner = Mock(
        return_value={
            "status": "ok",
            "all_passed": True,
            "platform": "windows-x64",
        }
    )
    fake_contract.run_office_contract_sync = contract_runner
    monkeypatch.setitem(sys.modules, "app.tool.office_contract", fake_contract)
    crash_reporter = Mock()
    monkeypatch.setattr(run, "_install_crash_reporter", crash_reporter)
    monkeypatch.setattr(
        sys,
        "argv",
        ["run.py", "--office-self-test", "windows-x64"],
    )

    with pytest.raises(SystemExit) as exit_info:
        run.main()

    assert exit_info.value.code == 0
    contract_runner.assert_called_once_with(expected_platform="windows-x64")
    assert json.loads(capsys.readouterr().out)["all_passed"] is True
    crash_reporter.assert_not_called()


def test_main_dispatches_v11_self_test_before_server_lifecycle(monkeypatch, capsys):
    fake_probe = ModuleType("app.runtime.frozen_self_test")
    probe = Mock(
        return_value={
            "status": "ok",
            "module_count": 52,
            "gates_closed": True,
            "templates": [],
            "office_repair_prompt_sha256": "7c9cd1613c47761539cd04fa22634e467881bac9917f5c88018454d5b91b5272",
        }
    )
    fake_probe.run_frozen_v11_self_test = probe
    monkeypatch.setitem(sys.modules, "app.runtime.frozen_self_test", fake_probe)
    crash_reporter = Mock()
    monkeypatch.setattr(run, "_install_crash_reporter", crash_reporter)
    monkeypatch.setattr(sys, "argv", ["run.py", "--v11-self-test"])

    with pytest.raises(SystemExit) as exit_info:
        run.main()

    assert exit_info.value.code == 0
    assert json.loads(capsys.readouterr().out)["gates_closed"] is True
    probe.assert_called_once_with()
    crash_reporter.assert_not_called()


def test_main_dispatches_office_renderer_self_test_before_server_lifecycle(
    monkeypatch, capsys
):
    fake_deployment = ModuleType("app.office_rendering.deployment")
    fake_behavior_probe = ModuleType(
        "app.office_rendering.native_sandbox_behavior"
    )
    fake_execution_probe = ModuleType("app.office_rendering.probe")
    fake_release_identity = ModuleType("app.office_rendering.release_identity")
    release_identity = object()
    provider = object()
    native_sandbox_contract = object()
    events: list[str] = []
    identity_loader = Mock(return_value=release_identity)
    fake_release_identity.load_frozen_renderer_release_identity = identity_loader
    contract_report = {
        "schema_version": 1,
        "status": "declared-not-proven",
        "native_behavior_proven": False,
    }
    renderer_probe = Mock(
        return_value={
            "schema_version": 2,
            "status": "ok",
            "available": True,
            "quality": "authoritative",
            "native_sandbox_contract": contract_report,
        }
    )
    fake_deployment.authoritative_office_renderer_self_test = renderer_probe
    provider_builder = Mock(return_value=provider)
    fake_deployment.build_attested_office_render_provider = provider_builder
    contract_binder = Mock()

    def bind_contract(bound_provider):
        assert bound_provider is provider
        events.append("bind")
        return native_sandbox_contract

    contract_binder.side_effect = bind_contract
    fake_deployment.bind_attested_native_sandbox_contract = contract_binder
    behavior_report = Mock()
    behavior_report.to_dict.return_value = {
        "schema_version": 1,
        "status": "proven",
        "native_behavior_proven": True,
    }

    async def prove_behavior(contract):
        assert contract is native_sandbox_contract
        events.append("behavior")
        return behavior_report

    behavior_probe = AsyncMock(side_effect=prove_behavior)
    fake_behavior_probe.run_native_sandbox_behavior_probe = behavior_probe
    execution_report = Mock()
    execution_report.to_dict.return_value = {
        "schema_version": 1,
        "bundle_tree_sha256": "a" * 64,
        "embedded_font_count": 1,
        "page_count": 1,
        "pages": [],
        "pdf_sha256": "b" * 64,
        "probe_manifest_sha256": "c" * 64,
        "probe_source_sha256": "d" * 64,
        "render_manifest_sha256": "e" * 64,
    }

    async def prove_execution(bound_provider):
        assert bound_provider is provider
        events.append("golden")
        return execution_report

    execution_probe = AsyncMock(side_effect=prove_execution)
    fake_execution_probe.run_attested_authoritative_office_renderer_probe = (
        execution_probe
    )
    monkeypatch.setitem(
        sys.modules, "app.office_rendering.deployment", fake_deployment
    )
    monkeypatch.setitem(
        sys.modules,
        "app.office_rendering.native_sandbox_behavior",
        fake_behavior_probe,
    )
    monkeypatch.setitem(
        sys.modules, "app.office_rendering.probe", fake_execution_probe
    )
    monkeypatch.setitem(
        sys.modules,
        "app.office_rendering.release_identity",
        fake_release_identity,
    )
    crash_reporter = Mock()
    monkeypatch.setattr(run, "_install_crash_reporter", crash_reporter)
    monkeypatch.setattr(sys, "argv", ["run.py", "--office-renderer-self-test"])

    with pytest.raises(SystemExit) as exit_info:
        run.main()

    assert exit_info.value.code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["quality"] == "authoritative"
    assert output["native_sandbox_contract"] == contract_report
    assert (
        output["native_sandbox_behavior"]
        == behavior_report.to_dict.return_value
    )
    assert output["execution_probe"] == execution_report.to_dict.return_value
    identity_loader.assert_called_once_with()
    renderer_probe.assert_called_once_with(release_identity=release_identity)
    provider_builder.assert_called_once_with(release_identity=release_identity)
    contract_binder.assert_called_once_with(provider)
    behavior_probe.assert_awaited_once_with(native_sandbox_contract)
    execution_probe.assert_awaited_once_with(provider)
    assert events == ["bind", "behavior", "golden"]
    crash_reporter.assert_not_called()


def test_office_renderer_self_test_failure_is_path_free(monkeypatch, capsys):
    fake_deployment = ModuleType("app.office_rendering.deployment")
    fake_behavior_probe = ModuleType(
        "app.office_rendering.native_sandbox_behavior"
    )
    fake_execution_probe = ModuleType("app.office_rendering.probe")
    fake_release_identity = ModuleType("app.office_rendering.release_identity")
    release_identity = object()
    fake_release_identity.load_frozen_renderer_release_identity = Mock(
        return_value=release_identity
    )
    fake_deployment.authoritative_office_renderer_self_test = Mock(
        side_effect=RuntimeError("private /Users/alice/renderer path")
    )
    contract_binder = Mock()
    fake_deployment.bind_attested_native_sandbox_contract = contract_binder
    fake_deployment.build_attested_office_render_provider = Mock()
    behavior_probe = AsyncMock()
    fake_behavior_probe.run_native_sandbox_behavior_probe = behavior_probe
    fake_execution_probe.run_attested_authoritative_office_renderer_probe = AsyncMock()
    monkeypatch.setitem(
        sys.modules, "app.office_rendering.deployment", fake_deployment
    )
    monkeypatch.setitem(
        sys.modules,
        "app.office_rendering.native_sandbox_behavior",
        fake_behavior_probe,
    )
    monkeypatch.setitem(
        sys.modules,
        "app.office_rendering.release_identity",
        fake_release_identity,
    )
    monkeypatch.setitem(
        sys.modules, "app.office_rendering.probe", fake_execution_probe
    )
    monkeypatch.setattr(sys, "argv", ["run.py", "--office-renderer-self-test"])

    with pytest.raises(SystemExit) as exit_info:
        run.main()

    output = capsys.readouterr()
    assert exit_info.value.code == 1
    assert json.loads(output.out) == {"schema_version": 2, "status": "unavailable"}
    assert "Users" not in output.out
    assert output.err == ""
    contract_binder.assert_not_called()
    behavior_probe.assert_not_awaited()


def test_office_renderer_identity_failure_is_path_free(monkeypatch, capsys):
    fake_deployment = ModuleType("app.office_rendering.deployment")
    fake_behavior_probe = ModuleType(
        "app.office_rendering.native_sandbox_behavior"
    )
    fake_execution_probe = ModuleType("app.office_rendering.probe")
    fake_release_identity = ModuleType("app.office_rendering.release_identity")
    renderer_probe = Mock()
    fake_deployment.authoritative_office_renderer_self_test = renderer_probe
    contract_binder = Mock()
    fake_deployment.bind_attested_native_sandbox_contract = contract_binder
    fake_deployment.build_attested_office_render_provider = Mock()
    behavior_probe = AsyncMock()
    fake_behavior_probe.run_native_sandbox_behavior_probe = behavior_probe
    fake_execution_probe.run_attested_authoritative_office_renderer_probe = AsyncMock()
    fake_release_identity.load_frozen_renderer_release_identity = Mock(
        side_effect=RuntimeError("private /Users/alice/release-identity.json")
    )
    monkeypatch.setitem(
        sys.modules, "app.office_rendering.deployment", fake_deployment
    )
    monkeypatch.setitem(
        sys.modules,
        "app.office_rendering.native_sandbox_behavior",
        fake_behavior_probe,
    )
    monkeypatch.setitem(
        sys.modules,
        "app.office_rendering.release_identity",
        fake_release_identity,
    )
    monkeypatch.setitem(
        sys.modules, "app.office_rendering.probe", fake_execution_probe
    )
    monkeypatch.setattr(sys, "argv", ["run.py", "--office-renderer-self-test"])

    with pytest.raises(SystemExit) as exit_info:
        run.main()

    output = capsys.readouterr()
    assert exit_info.value.code == 1
    assert json.loads(output.out) == {"schema_version": 2, "status": "unavailable"}
    assert "Users" not in output.out
    assert output.err == ""
    renderer_probe.assert_not_called()
    contract_binder.assert_not_called()
    behavior_probe.assert_not_awaited()


def test_office_renderer_behavior_probe_failure_is_path_free(monkeypatch, capsys):
    fake_deployment = ModuleType("app.office_rendering.deployment")
    fake_behavior_probe = ModuleType(
        "app.office_rendering.native_sandbox_behavior"
    )
    fake_execution_probe = ModuleType("app.office_rendering.probe")
    fake_release_identity = ModuleType("app.office_rendering.release_identity")
    release_identity = object()
    provider = object()
    native_sandbox_contract = object()
    fake_release_identity.load_frozen_renderer_release_identity = Mock(
        return_value=release_identity
    )
    fake_deployment.authoritative_office_renderer_self_test = Mock(
        return_value={"schema_version": 2, "status": "ok"}
    )
    fake_deployment.build_attested_office_render_provider = Mock(
        return_value=provider
    )
    contract_binder = Mock(return_value=native_sandbox_contract)
    fake_deployment.bind_attested_native_sandbox_contract = contract_binder
    behavior_probe = AsyncMock(
        side_effect=RuntimeError(
            "private /Users/alice/native-sandbox-helper"
        )
    )
    fake_behavior_probe.run_native_sandbox_behavior_probe = behavior_probe
    execution_probe = AsyncMock()
    fake_execution_probe.run_attested_authoritative_office_renderer_probe = (
        execution_probe
    )
    monkeypatch.setitem(
        sys.modules, "app.office_rendering.deployment", fake_deployment
    )
    monkeypatch.setitem(
        sys.modules,
        "app.office_rendering.native_sandbox_behavior",
        fake_behavior_probe,
    )
    monkeypatch.setitem(
        sys.modules, "app.office_rendering.probe", fake_execution_probe
    )
    monkeypatch.setitem(
        sys.modules,
        "app.office_rendering.release_identity",
        fake_release_identity,
    )
    monkeypatch.setattr(sys, "argv", ["run.py", "--office-renderer-self-test"])

    with pytest.raises(SystemExit) as exit_info:
        run.main()

    output = capsys.readouterr()
    assert exit_info.value.code == 1
    assert json.loads(output.out) == {"schema_version": 2, "status": "unavailable"}
    assert "Users" not in output.out
    assert output.err == ""
    contract_binder.assert_called_once_with(provider)
    behavior_probe.assert_awaited_once_with(native_sandbox_contract)
    execution_probe.assert_not_awaited()


def test_office_renderer_execution_probe_failure_is_path_free(monkeypatch, capsys):
    fake_deployment = ModuleType("app.office_rendering.deployment")
    fake_behavior_probe = ModuleType(
        "app.office_rendering.native_sandbox_behavior"
    )
    fake_execution_probe = ModuleType("app.office_rendering.probe")
    fake_release_identity = ModuleType("app.office_rendering.release_identity")
    release_identity = object()
    provider = object()
    events: list[str] = []
    fake_release_identity.load_frozen_renderer_release_identity = Mock(
        return_value=release_identity
    )
    fake_deployment.authoritative_office_renderer_self_test = Mock(
        return_value={"schema_version": 2, "status": "ok"}
    )
    fake_deployment.build_attested_office_render_provider = Mock(
        return_value=provider
    )
    native_sandbox_contract = object()
    def bind_contract(bound_provider):
        assert bound_provider is provider
        events.append("bind")
        return native_sandbox_contract

    contract_binder = Mock(side_effect=bind_contract)
    fake_deployment.bind_attested_native_sandbox_contract = contract_binder
    behavior_report = Mock()

    async def prove_behavior(contract):
        assert contract is native_sandbox_contract
        events.append("behavior")
        return behavior_report

    behavior_probe = AsyncMock(side_effect=prove_behavior)
    fake_behavior_probe.run_native_sandbox_behavior_probe = behavior_probe

    async def fail_golden(bound_provider):
        assert bound_provider is provider
        events.append("golden")
        raise RuntimeError("private /Users/alice/probe.docx")

    execution_probe = AsyncMock(side_effect=fail_golden)
    fake_execution_probe.run_attested_authoritative_office_renderer_probe = (
        execution_probe
    )
    monkeypatch.setitem(
        sys.modules, "app.office_rendering.deployment", fake_deployment
    )
    monkeypatch.setitem(
        sys.modules,
        "app.office_rendering.native_sandbox_behavior",
        fake_behavior_probe,
    )
    monkeypatch.setitem(
        sys.modules, "app.office_rendering.probe", fake_execution_probe
    )
    monkeypatch.setitem(
        sys.modules,
        "app.office_rendering.release_identity",
        fake_release_identity,
    )
    monkeypatch.setattr(sys, "argv", ["run.py", "--office-renderer-self-test"])

    with pytest.raises(SystemExit) as exit_info:
        run.main()

    output = capsys.readouterr()
    assert exit_info.value.code == 1
    assert json.loads(output.out) == {"schema_version": 2, "status": "unavailable"}
    assert "Users" not in output.out
    assert output.err == ""
    contract_binder.assert_called_once_with(provider)
    behavior_probe.assert_awaited_once_with(native_sandbox_contract)
    execution_probe.assert_awaited_once_with(provider)
    assert events == ["bind", "behavior", "golden"]


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
