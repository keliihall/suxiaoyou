"""Acceptance test for the *packaged* execution sandbox.

The bundle verifier invokes the backend executable with ``--sandbox-self-test``.
That entrypoint comes back through the real tools and sandbox launcher; it must
never call the worker mode directly because doing so would bypass the policy.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from app.schemas.agent import AgentInfo
from app.tool.builtin.bash import BashTool
from app.tool.builtin.code_execute import CodeExecuteTool
from app.tool.context import ToolContext


def _context(workspace: Path, call_id: str) -> ToolContext:
    return ToolContext(
        session_id="sandbox-self-test",
        message_id="sandbox-self-test",
        agent=AgentInfo(name="sandbox-self-test", description="", mode="hidden"),
        call_id=call_id,
        workspace=str(workspace),
    )


def _has_python_import_smoke_output(output: str) -> bool:
    """Match the two probe lines without assuming an OS newline convention."""

    lines = output.splitlines()
    return any(
        lines[index : index + 2] == ["6", "False"]
        for index in range(len(lines) - 1)
    )


def _detached_probe_code(
    ready_path: Path,
    survived_path: Path,
    *,
    keep_parent_alive: bool,
    workspace_root: Path | None = None,
) -> str:
    """Build a probe that escapes its launch process group before waiting."""

    parent_tail = "time.sleep(30)" if keep_parent_alive else "print('parent-complete')"
    if workspace_root is None:
        ready_expression = repr(str(ready_path))
        survived_expression = repr(str(survived_path))
    else:
        ready_relative = ready_path.relative_to(workspace_root).as_posix()
        survived_relative = survived_path.relative_to(workspace_root).as_posix()
        ready_expression = (
            f"os.path.join(os.environ['SUXIAOYOU_WORKSPACE'], {ready_relative!r})"
        )
        survived_expression = (
            f"os.path.join(os.environ['SUXIAOYOU_WORKSPACE'], {survived_relative!r})"
        )

    if sys.platform == "win32":
        return (
            "import os, subprocess, sys, time\n"
            f"marker = {survived_expression}\n"
            "cmd = os.path.join(os.environ['SYSTEMROOT'], 'System32', 'cmd.exe')\n"
            "child_command = 'ping 127.0.0.1 -n 3 >NUL & echo survived > "
            "\"' + marker.replace('\"', '\"\"') + '\"'\n"
            "flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP\n"
            "subprocess.Popen([cmd, '/D', '/S', '/C', child_command], "
            "creationflags=flags, close_fds=True)\n"
            f"open({ready_expression}, 'w').write('ready')\n"
            f"{parent_tail}\n"
        )

    return (
        "import os, time\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        " try:\n"
        "  os.setsid()\n"
        " except PermissionError:\n"
        "  pass\n"
        f" open({ready_expression}, 'w').write('ready')\n"
        " for fd in (0, 1, 2):\n"
        "  try:\n"
        "   os.close(fd)\n"
        "  except OSError:\n"
        "   pass\n"
        " time.sleep(2)\n"
        f" open({survived_expression}, 'w').write('survived')\n"
        " os._exit(0)\n"
        "deadline = time.monotonic() + 1\n"
        f"while not os.path.exists({ready_expression}) and time.monotonic() < deadline:\n"
        " time.sleep(0.01)\n"
        f"if not os.path.exists({ready_expression}):\n"
        " raise RuntimeError('detached child did not become ready')\n"
        f"{parent_tail}\n"
    )


async def _assert_descendant_did_not_survive(marker: Path, label: str) -> None:
    # The probe writes after two seconds if it survives. Wait beyond that
    # deadline so a missing marker proves namespace teardown, not a race.
    await asyncio.sleep(2.25)
    if marker.exists():
        raise RuntimeError(f"sandbox detached descendant survived {label}")


async def _run(workspace: Path) -> dict[str, object]:
    workspace.mkdir(parents=True, exist_ok=True)
    outside_read = workspace.parent / f".{workspace.name}-outside-read"
    outside_write = workspace.parent / f".{workspace.name}-outside-write"
    outside_read.write_text("outside-secret", encoding="utf-8")
    outside_write.unlink(missing_ok=True)
    os.environ["SUXIAOYOU_SANDBOX_SELF_TEST_SECRET"] = "must-not-cross-boundary"

    try:
        python_result = await CodeExecuteTool().execute(
            {
                "code": (
                    "import os\n"
                    "import numpy as np\n"
                    "import pandas as pd\n"
                    "open('python-sandbox-ok.txt', 'w').write('ok')\n"
                    "print(int(pd.Series(np.arange(4)).sum()))\n"
                    "print('SUXIAOYOU_SANDBOX_SELF_TEST_SECRET' in os.environ)\n"
                )
            },
            _context(workspace, "python-positive"),
        )
        if not python_result.success or not _has_python_import_smoke_output(
            python_result.output
        ):
            raise RuntimeError(f"sandboxed Python/import smoke failed: {python_result.error or python_result.output}")

        shell_command = (
            "Set-Content -Path shell-sandbox-ok.txt -Value ok; "
            "Write-Output shell-ok"
            if sys.platform == "win32"
            else "printf ok > shell-sandbox-ok.txt && printf shell-ok"
        )
        shell_result = await BashTool().execute(
            {"command": shell_command},
            _context(workspace, "shell-positive"),
        )
        if not shell_result.success or "shell-ok" not in shell_result.output:
            raise RuntimeError(f"sandboxed shell smoke failed: {shell_result.error or shell_result.output}")

        if sys.platform == "win32":
            home_write_command = (
                "$marker = Join-Path $env:HOME 'self-test-marker'; "
                "[IO.File]::WriteAllText($marker, 'home-ok'); "
                "Write-Output $env:HOME"
            )
            home_read_command = (
                "$marker = Join-Path $env:HOME 'self-test-marker'; "
                "Get-Content -Raw -LiteralPath $marker; Write-Output $env:HOME"
            )
            pipeline_command = (
                "& \"$env:SystemRoot\\System32\\cmd.exe\" /D /C \"exit 17\" "
                "| Out-Null"
            )
            pwd_command = "Write-Output ((Get-Location).Path)"
        else:
            home_write_command = (
                "printf home-ok > \"$HOME/self-test-marker\"; printf '%s' \"$HOME\""
            )
            home_read_command = (
                "cat \"$HOME/self-test-marker\"; printf '\\n%s' \"$HOME\""
            )
            pipeline_command = "(exit 17) | cat"
            pwd_command = "pwd"

        home_write_result = await BashTool().execute(
            {"command": home_write_command},
            _context(workspace, "persistent-home-write"),
        )
        home_read_result = await BashTool().execute(
            {"command": home_read_command},
            _context(workspace, "persistent-home-read"),
        )
        if (
            not home_write_result.success
            or not home_read_result.success
            or "home-ok" not in home_read_result.output
            or home_write_result.output.strip()
            != home_read_result.output.splitlines()[-1]
            or home_read_result.metadata.get("home_persistent") is not True
        ):
            raise RuntimeError(
                "session-persistent HOME contract failed: "
                f"{home_write_result.error or home_read_result.error or home_read_result.output}"
            )

        pipeline_result = await BashTool().execute(
            {"command": pipeline_command},
            _context(workspace, "pipeline-failure"),
        )
        if pipeline_result.success:
            raise RuntimeError(
                "shell pipeline hid a producer failure: "
                f"{pipeline_result.error or pipeline_result.output}"
            )

        pwd_result = await BashTool().execute(
            {"command": pwd_command},
            _context(workspace, "logical-pwd"),
        )
        lowered_pwd = pwd_result.output.lower()
        if (
            not pwd_result.success
            or str(workspace).lower() not in lowered_pwd
            or "execution-transactions" in lowered_pwd
            or "<private-execution-transaction>" in lowered_pwd
        ):
            raise RuntimeError(
                "shell exposed a deleted physical transaction path: "
                f"{pwd_result.error or pwd_result.output}"
            )

        macos_system_python = True
        if sys.platform == "darwin" and Path("/usr/bin/python3").is_file():
            system_python_result = await BashTool().execute(
                {"command": "/usr/bin/python3 --version"},
                _context(workspace, "macos-system-python"),
            )
            macos_system_python = (
                system_python_result.success
                and "Python 3" in system_python_result.output
            )
            if not macos_system_python:
                raise RuntimeError(
                    "macOS /usr/bin/python3 could not read the selected Developer runtime: "
                    f"{system_python_result.error or system_python_result.output}"
                )

        if sys.platform == "darwin":
            keychain_result = await BashTool().execute(
                {"command": "security list-keychains"},
                _context(workspace, "keychain-negative"),
            )
            if keychain_result.success or "login.keychain" in keychain_result.output:
                raise RuntimeError("macOS sandbox exposed the user's Keychain search list")

        denial_code = (
            "import socket\n"
            "checks = []\n"
            f"\ntry:\n open({str(outside_read)!r}).read()\n checks.append('outside-read-open')"
            "\nexcept (OSError, PermissionError):\n checks.append('outside-read-denied')\n"
            f"\ntry:\n open({str(outside_write)!r}, 'w').write('escaped')\n checks.append('outside-write-open')"
            "\nexcept (OSError, PermissionError):\n checks.append('outside-write-denied')\n"
            "\ntry:\n socket.create_connection(('1.1.1.1', 80), 1)\n checks.append('network-open')"
            "\nexcept OSError:\n checks.append('network-denied')\n"
            "print(','.join(checks))\n"
        )
        if sys.platform != "win32":
            denial_result = await CodeExecuteTool().execute(
                {"code": denial_code},
                _context(workspace, "negative-policy"),
            )
            expected = "outside-read-denied,outside-write-denied,network-denied"
            if not denial_result.success or expected not in denial_result.output:
                raise RuntimeError(
                    "sandbox policy denial failed: "
                    f"{denial_result.error or denial_result.output}"
                )
            if (
                outside_read.read_text(encoding="utf-8") != "outside-secret"
                or outside_write.exists()
            ):
                raise RuntimeError("sandbox modified an outside canary")

        success_ready = workspace / "detached-success.ready"
        success_survived = workspace / "detached-success.survived"
        success_result = await CodeExecuteTool().execute(
            {
                "code": _detached_probe_code(
                    success_ready,
                    success_survived,
                    keep_parent_alive=False,
                    workspace_root=workspace,
                )
            },
            _context(workspace, "detached-success"),
        )
        if not success_result.success or not success_ready.exists():
            raise RuntimeError(
                "detached-success probe did not complete: "
                f"{success_result.error or success_result.output}"
            )
        await _assert_descendant_did_not_survive(success_survived, "normal completion")

        timeout_ready = workspace / "detached-timeout.ready"
        timeout_survived = workspace / "detached-timeout.survived"
        timeout_result = await CodeExecuteTool().execute(
            {
                "code": _detached_probe_code(
                    timeout_ready,
                    timeout_survived,
                    keep_parent_alive=True,
                    workspace_root=workspace,
                ),
                "timeout": 1,
            },
            _context(workspace, "detached-timeout"),
        )
        if (
            timeout_result.metadata.get("timeout") is not True
            or timeout_result.metadata.get("process_tree_reaped") is not True
        ):
            raise RuntimeError("sandbox timeout contract did not trigger")
        if sys.platform == "win32":
            if not timeout_ready.exists():
                raise RuntimeError("Windows timeout descendant did not start")
            await _assert_descendant_did_not_survive(timeout_survived, "timeout")

        return {
            "status": "ok",
            "platform": sys.platform,
            "sandbox": python_result.metadata.get("sandbox"),
            "filesystem_isolated": python_result.metadata.get("filesystem_isolated"),
            "network_isolated": python_result.metadata.get("network_isolated"),
            "environment_sanitized": python_result.metadata.get("environment_sanitized"),
            "process_tree_reaped": timeout_result.metadata.get("process_tree_reaped"),
            "workspace_execution": (
                "direct-approved" if sys.platform == "win32" else "transactional"
            ),
            "descendant_terminated": True,
            "detached_success_terminated": True,
            "detached_timeout_terminated": True,
            "persistent_home": True,
            "pipeline_failure_propagated": True,
            "private_paths_redacted": True,
            "macos_system_python": macos_system_python,
        }
    finally:
        os.environ.pop("SUXIAOYOU_SANDBOX_SELF_TEST_SECRET", None)
        outside_read.unlink(missing_ok=True)
        outside_write.unlink(missing_ok=True)


def main(workspace: str) -> int:
    try:
        report = asyncio.run(_run(Path(workspace).resolve()))
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0
