from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from app.hooks.models import HookCommandDeclaration, HookDecisionKind
from app.hooks.registry import HookRegistry
from app.hooks.runner import HookCommandRunner, HookRunStatus
from app.hooks.trust import HookTrustStore


def _register(
    root: Path,
    executable: Path,
    *,
    timeout: float = 5,
):
    registry = HookRegistry(root)
    hook, = registry.register_project_commands([
        HookCommandDeclaration(
            hook_id="runner-policy",
            event="PreToolUse",
            failure_policy="required",
            timeout_seconds=timeout,
            command=(executable.name,),
        ),
    ])
    return hook


def _approve(root: Path, hook) -> HookTrustStore:
    trust = HookTrustStore(root, storage_root=root / ".runner-hook-trust")
    trust.approve(hook)
    return trust


def test_runner_delivers_versioned_json_on_stdin_with_minimal_environment(
    tmp_path: Path,
    executable_hook,
    hook_event,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    executable = executable_hook("policy", """
import json, os, sys
event = json.load(sys.stdin)
annotation = '|'.join([
    event['event'],
    str(event['version']),
    os.getcwd(),
    str('OPENAI_API_KEY' in os.environ),
])
print(json.dumps({'version': 1, 'decision': 'ask', 'annotation': annotation}))
print('bounded diagnostic', file=sys.stderr)
""")
    hook = _register(tmp_path, executable)

    result = HookCommandRunner().run(
        hook,
        hook_event,
        trust_store=_approve(tmp_path, hook),
    )

    assert result.status is HookRunStatus.SUCCESS
    assert result.decision is not None
    assert result.decision.decision is HookDecisionKind.ASK
    assert result.decision.annotation == (
        f"PreToolUse|1|{tmp_path.resolve()}|False"
    )
    assert result.logs == "bounded diagnostic\n"
    assert result.exit_code == 0


def test_runner_launch_boundary_refuses_untrusted_command(
    tmp_path: Path,
    executable_hook,
    hook_event,
) -> None:
    marker = tmp_path / "untrusted-ran"
    executable = executable_hook("untrusted", f"""
from pathlib import Path
Path({str(marker)!r}).write_text('bad')
print('{{"version":1,"decision":"allow"}}')
""")
    hook = _register(tmp_path, executable)
    trust = HookTrustStore(
        tmp_path,
        storage_root=tmp_path / ".untrusted-hook-trust",
    )

    result = HookCommandRunner().run(
        hook,
        hook_event,
        trust_store=trust,
    )

    assert result.status is HookRunStatus.APPROVAL_REQUIRED
    assert not marker.exists()


def test_runner_rejects_stdout_logs_or_rewrite_fields(
    tmp_path: Path,
    executable_hook,
    hook_event,
) -> None:
    stdout_log = executable_hook("stdout-log", """
print('log line')
print('{"version":1,"decision":"allow"}')
""")
    hook = _register(tmp_path, stdout_log)
    result = HookCommandRunner().run(
        hook,
        hook_event,
        trust_store=_approve(tmp_path, hook),
    )
    assert result.status is HookRunStatus.INVALID_RESPONSE

    rewrite = executable_hook("rewrite", """
print('{"version":1,"decision":"allow","tool_args":{"file_path":"safe"}}')
""")
    hook = _register(tmp_path, rewrite)
    result = HookCommandRunner().run(
        hook,
        hook_event,
        trust_store=_approve(tmp_path, hook),
    )
    assert result.status is HookRunStatus.INVALID_RESPONSE


def test_runner_enforces_output_limit_while_draining_child(
    tmp_path: Path,
    executable_hook,
    hook_event,
) -> None:
    executable = executable_hook("noisy", """
import os
os.write(1, b'x' * 100000)
os.write(2, b'y' * 100000)
""")
    hook = _register(tmp_path, executable)
    result = HookCommandRunner(max_output_bytes=1024).run(
        hook,
        hook_event,
        trust_store=_approve(tmp_path, hook),
    )
    assert result.status is HookRunStatus.OUTPUT_LIMIT
    assert len(result.logs.encode()) == 1024


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-tree evidence")
def test_runner_timeout_reaps_descendant_process_tree(
    tmp_path: Path,
    executable_hook,
    hook_event,
) -> None:
    marker = tmp_path / "descendant-survived"
    child = (
        "import pathlib,time; time.sleep(1); "
        f"pathlib.Path({str(marker)!r}).write_text('bad')"
    )
    executable = executable_hook("timeout", f"""
import subprocess, sys, time
subprocess.Popen([sys.executable, '-c', {child!r}])
time.sleep(5)
""")

    hook = _register(tmp_path, executable, timeout=0.1)
    result = HookCommandRunner().run(
        hook,
        hook_event,
        trust_store=_approve(tmp_path, hook),
    )

    assert result.status is HookRunStatus.TIMEOUT
    time.sleep(1.1)
    assert not marker.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX cancellation evidence")
def test_runner_cancellation_reaps_process_before_return(
    tmp_path: Path,
    executable_hook,
    hook_event,
) -> None:
    marker = tmp_path / "cancelled-survived"
    executable = executable_hook("cancel", f"""
import pathlib, time
time.sleep(1)
pathlib.Path({str(marker)!r}).write_text('bad')
""")
    started = time.monotonic()

    hook = _register(tmp_path, executable)
    result = HookCommandRunner().run(
        hook,
        hook_event,
        trust_store=_approve(tmp_path, hook),
        should_abort=lambda: time.monotonic() - started > 0.1,
    )

    assert result.status is HookRunStatus.CANCELLED
    time.sleep(1.1)
    assert not marker.exists()


def test_runner_revalidates_content_identity_immediately_before_spawn(
    tmp_path: Path,
    executable_hook,
    hook_event,
) -> None:
    marker = tmp_path / "must-not-run"
    executable = executable_hook(
        "identity",
        "print('{\"version\":1,\"decision\":\"allow\"}')\n",
    )
    hook = _register(tmp_path, executable)
    executable.write_text(
        f"#!{sys.executable}\n"
        f"from pathlib import Path; Path({str(marker)!r}).write_text('bad')\n"
        "print('{\"version\":1,\"decision\":\"allow\"}')\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)

    result = HookCommandRunner().run(
        hook,
        hook_event,
        trust_store=_approve(tmp_path, hook),
    )

    assert result.status is HookRunStatus.IDENTITY_CHANGED
    assert not marker.exists()
