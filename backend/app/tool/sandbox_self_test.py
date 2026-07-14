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


def _detached_probe_code(
    ready_path: Path,
    survived_path: Path,
    *,
    keep_parent_alive: bool,
) -> str:
    """Build a probe that escapes its launch process group before waiting."""

    parent_tail = "time.sleep(30)" if keep_parent_alive else "print('parent-complete')"
    return (
        "import os, time\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        " os.setsid()\n"
        f" open({str(ready_path)!r}, 'w').write('ready')\n"
        " for fd in (0, 1, 2):\n"
        "  try:\n"
        "   os.close(fd)\n"
        "  except OSError:\n"
        "   pass\n"
        " time.sleep(2)\n"
        f" open({str(survived_path)!r}, 'w').write('survived')\n"
        " os._exit(0)\n"
        "deadline = time.monotonic() + 1\n"
        f"while not os.path.exists({str(ready_path)!r}) and time.monotonic() < deadline:\n"
        " time.sleep(0.01)\n"
        f"if not os.path.exists({str(ready_path)!r}):\n"
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
        if sys.platform in {"darwin", "win32"}:
            result = await CodeExecuteTool().execute(
                {"code": "print('must not execute')"},
                _context(workspace, f"{sys.platform}-disabled"),
            )
            expected_platform = "macOS" if sys.platform == "darwin" else "Windows"
            if f"disabled on {expected_platform}" not in (result.error or ""):
                raise RuntimeError(f"{expected_platform} execution did not fail closed")
            return {
                "status": "disabled",
                "platform": sys.platform,
                "reason": result.error,
            }

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
        if not python_result.success or "6\nFalse" not in python_result.output:
            raise RuntimeError(f"sandboxed Python/import smoke failed: {python_result.error or python_result.output}")

        shell_result = await BashTool().execute(
            {"command": "printf ok > shell-sandbox-ok.txt && printf shell-ok"},
            _context(workspace, "shell-positive"),
        )
        if not shell_result.success or "shell-ok" not in shell_result.output:
            raise RuntimeError(f"sandboxed shell smoke failed: {shell_result.error or shell_result.output}")

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
        denial_result = await CodeExecuteTool().execute(
            {"code": denial_code},
            _context(workspace, "negative-policy"),
        )
        expected = "outside-read-denied,outside-write-denied,network-denied"
        if not denial_result.success or expected not in denial_result.output:
            raise RuntimeError(f"sandbox policy denial failed: {denial_result.error or denial_result.output}")
        if outside_read.read_text(encoding="utf-8") != "outside-secret" or outside_write.exists():
            raise RuntimeError("sandbox modified an outside canary")

        success_ready = workspace / "detached-success.ready"
        success_survived = workspace / "detached-success.survived"
        success_result = await CodeExecuteTool().execute(
            {
                "code": _detached_probe_code(
                    success_ready,
                    success_survived,
                    keep_parent_alive=False,
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
                ),
                "timeout": 1,
            },
            _context(workspace, "detached-timeout"),
        )
        if timeout_result.metadata.get("timeout") is not True or not timeout_ready.exists():
            raise RuntimeError("sandbox timeout contract did not trigger")
        await _assert_descendant_did_not_survive(timeout_survived, "timeout")

        return {
            "status": "ok",
            "platform": sys.platform,
            "sandbox": python_result.metadata.get("sandbox"),
            "filesystem_isolated": python_result.metadata.get("filesystem_isolated"),
            "network_isolated": python_result.metadata.get("network_isolated"),
            "environment_sanitized": python_result.metadata.get("environment_sanitized"),
            "descendant_terminated": True,
            "detached_success_terminated": True,
            "detached_timeout_terminated": True,
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
