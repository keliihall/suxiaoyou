"""Cross-platform subprocess helpers.

Centralises platform detection, shell selection, encoding handling,
and creationflags so individual tools don't duplicate this logic.
"""

from __future__ import annotations

import locale
import ntpath
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

IS_WINDOWS = sys.platform == "win32"


def get_subprocess_kwargs() -> dict[str, Any]:
    """Return platform-specific kwargs for subprocess.run().

    On Windows: includes ``creationflags=CREATE_NO_WINDOW``.
    On other platforms: returns an empty dict (no ``creationflags`` kwarg).
    """
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def find_shell() -> list[str]:
    """Return the command prefix for running a shell command on this platform.

    On Windows: an absolute PowerShell/cmd path with non-interactive flags.
    On other platforms: a shell with strict pipeline failure propagation.
    """
    if IS_WINDOWS:
        # Never resolve a shell from the command cwd: a workspace containing a
        # fake powershell.exe must not be able to replace the trusted launcher.
        program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        candidates = (
            (
                program_files / "PowerShell" / "7" / "pwsh.exe",
                ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command"],
            ),
            (
                system_root
                / "System32"
                / "WindowsPowerShell"
                / "v1.0"
                / "powershell.exe",
                [
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                ],
            ),
            (
                system_root / "System32" / "cmd.exe",
                ["/D", "/S", "/C"],
            ),
        )
        for executable, arguments in candidates:
            if executable.is_file():
                return [str(executable), *arguments]
        # Keep the diagnostic deterministic; the subsequent launch reports a
        # missing trusted system shell rather than falling back to PATH.
        executable, arguments = candidates[1]
        return [str(executable), *arguments]

    bash = shutil.which("bash")
    if bash:
        return [bash, "-o", "pipefail", "-c"]
    for name in ("zsh", "ksh"):
        shell = shutil.which(name)
        if shell:
            return [shell, "-o", "pipefail", "-c"]
    return ["sh", "-c"]


def prepare_shell_command(shell_prefix: list[str], command: str) -> str:
    """Wrap a command so shell/native failures reach the tool exit status."""

    executable = ntpath.basename(shell_prefix[0]).lower()
    if executable in {"powershell.exe", "powershell"} and "|" in command:
        # Windows PowerShell 5 exposes only the last native process status for
        # a pipeline. There is no pipefail-equivalent that can prove every
        # producer succeeded, so do not execute an ambiguous pipeline.
        raise ValueError(
            "Native pipelines require PowerShell 7 (pwsh) so producer "
            "failures can be propagated safely",
        )
    if executable in {"powershell.exe", "pwsh.exe", "powershell", "pwsh"}:
        native_error_preference = (
            "$PSNativeCommandUseErrorActionPreference = $true; "
            if executable in {"pwsh.exe", "pwsh"}
            else ""
        )
        return (
            "$ErrorActionPreference = 'Stop'; "
            f"{native_error_preference}& {{ {command} }}; "
            "if ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) { "
            "exit $LASTEXITCODE }"
        )
    if executable in {"sh", "sh.exe", "cmd", "cmd.exe"} and "|" in command:
        # POSIX sh and cmd.exe expose only the final pipeline element. Running
        # such a command would report false success, so fail closed when no
        # pipefail-capable shell is installed.
        raise ValueError(
            "Pipelines require bash, zsh, ksh, or PowerShell so failures "
            "can be propagated safely",
        )
    return command


def decode_subprocess_output(data: bytes) -> str:
    """Decode subprocess stdout/stderr with platform-aware fallback.

    Strategy:
      1. Try UTF-8 (strict) — works for bash / modern tools.
      2. On Windows only: try the system code page (e.g. CP936, CP1252).
      3. Fall back to UTF-8 with ``errors='replace'``.
    """
    # Try UTF-8 first (most common for modern tools)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass

    # On Windows, try system code page
    if IS_WINDOWS:
        try:
            system_encoding = locale.getpreferredencoding(False)
            if system_encoding and system_encoding.lower().replace("-", "") != "utf8":
                return data.decode(system_encoding)
        except (UnicodeDecodeError, LookupError):
            pass

    # Final fallback
    return data.decode("utf-8", errors="replace")
