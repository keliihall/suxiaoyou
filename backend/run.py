"""Standalone entry point for 苏小有 backend in desktop mode.

Usage:
    python run.py --port 8100 --data-dir /path/to/app/data
"""

import argparse
import faulthandler
import os
import signal
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path


_DESKTOP_PARENT_PID_ENV = "SUXIAOYOU_DESKTOP_PARENT_PID"
_APP_PRIVATE_DIR_ENV = "SUXIAOYOU_PRIVATE_DATA_DIR"
_PROCESS_GROUP_GRACE_SECONDS = 12.0
_WINDOWS_BACKEND_JOB_HANDLE = None


def _write_lifecycle_diagnostic(message: str) -> None:
    """Best-effort diagnostics that remain safe after Tauri closes its pipe."""
    try:
        sys.stderr.write(f"[desktop-lifecycle] {message}\n")
        sys.stderr.flush()
    except Exception:
        pass


def _configure_windows_process_job():
    """Put the Windows backend tree in a kill-on-close Job Object.

    The backend keeps the only Job handle. Windows closes it when the backend
    exits, including via ``os._exit``, and then terminates every inherited
    Node/npm/npx helper that is still assigned to the Job.
    """
    global _WINDOWS_BACKEND_JOB_HANDLE

    if os.name != "nt" or _WINDOWS_BACKEND_JOB_HANDLE is not None:
        return _WINDOWS_BACKEND_JOB_HANDLE

    import ctypes
    from ctypes import wintypes

    class JobObjectBasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class JobObjectExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JobObjectBasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    job_object_extended_limit_information = 9
    job_object_limit_kill_on_job_close = 0x00002000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        _write_lifecycle_diagnostic(
            f"could not create Windows backend Job Object (error {ctypes.get_last_error()})"
        )
        return None

    info = JobObjectExtendedLimitInformation()
    info.BasicLimitInformation.LimitFlags = job_object_limit_kill_on_job_close
    configured = kernel32.SetInformationJobObject(
        job,
        job_object_extended_limit_information,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    assigned = configured and kernel32.AssignProcessToJobObject(
        job, kernel32.GetCurrentProcess()
    )
    if not assigned:
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        _write_lifecycle_diagnostic(
            f"could not isolate Windows backend process tree (error {error})"
        )
        return None

    _WINDOWS_BACKEND_JOB_HANDLE = job
    return job


def _wait_for_desktop_parent_exit(parent_pid: int) -> bool:
    """Block until Tauri exits; return false when observation itself fails."""
    if os.name == "nt":
        # Waiting on a real process handle avoids polling tasklist and avoids
        # os.kill(pid, 0), whose semantics are unsafe on Windows.
        import ctypes
        from ctypes import wintypes

        synchronize = 0x00100000
        infinite = 0xFFFFFFFF
        wait_object_0 = 0x00000000
        wait_failed = 0xFFFFFFFF
        error_invalid_parameter = 87
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(synchronize, False, parent_pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == error_invalid_parameter:
                return True
            _write_lifecycle_diagnostic(
                f"could not observe desktop parent {parent_pid} (error {error})"
            )
            return False
        try:
            wait_result = kernel32.WaitForSingleObject(handle, infinite)
            wait_error = ctypes.get_last_error()
        finally:
            kernel32.CloseHandle(handle)
        if wait_result == wait_object_0:
            return True
        if wait_result == wait_failed:
            _write_lifecycle_diagnostic(
                f"waiting for desktop parent {parent_pid} failed (error {wait_error})"
            )
        else:
            _write_lifecycle_diagnostic(
                f"waiting for desktop parent {parent_pid} returned {wait_result}"
            )
        return False

    # Unix reparents an orphan to launchd/init, so getppid() changes as soon as
    # the desktop shell disappears. This also avoids PID-reuse ambiguity.
    while os.getppid() == parent_pid:
        time.sleep(0.5)
    return True


def _launch_unix_process_group_reaper(process_group: int, grace_seconds: float) -> None:
    """Launch an isolated short-lived reaper before signalling our own group."""
    tick_seconds = 0.25
    tick_count = max(1, int((grace_seconds + tick_seconds - 0.001) / tick_seconds))
    script = (
        'ticks="$1"; pgid="$2"; count=0; '
        'while [ "$count" -lt "$ticks" ]; do '
        '/bin/kill -0 -- "-$pgid" 2>/dev/null || exit 0; '
        f"sleep {tick_seconds}; count=$((count + 1)); "
        "done; /bin/kill -KILL -- \"-$pgid\" 2>/dev/null || true"
    )
    subprocess.Popen(
        [
            "/bin/sh",
            "-c",
            script,
            "suxiaoyou-process-group-reaper",
            str(tick_count),
            str(process_group),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )


def _terminate_backend_after_parent_exit(
    *,
    platform_name: str | None = None,
    grace_seconds: float = _PROCESS_GROUP_GRACE_SECONDS,
) -> None:
    """Terminate the backend group, with a bounded hard-exit fallback."""
    _write_lifecycle_diagnostic("desktop parent exited; stopping backend")
    platform_name = platform_name or os.name

    if platform_name != "nt":
        process_id = os.getpid()
        process_group = os.getpgrp()
        if process_group != process_id:
            _write_lifecycle_diagnostic(
                "backend is not an isolated process-group leader; refusing group signal"
            )
            os._exit(0)
            return

        try:
            # The reaper lives in its own session. If Uvicorn exits after TERM,
            # it remains alive long enough to KILL any helper that ignored it.
            _launch_unix_process_group_reaper(process_group, grace_seconds)
        except Exception as exc:
            _write_lifecycle_diagnostic(f"could not launch process-group reaper: {exc}")
            try:
                os.killpg(process_group, signal.SIGKILL)
            except OSError:
                os._exit(0)
            return

        try:
            os.killpg(process_group, signal.SIGTERM)
        except OSError:
            os._exit(0)
            return
        time.sleep(grace_seconds + 1.0)

    # On Windows the kill-on-close Job handle is owned by this process, so this
    # hard exit also terminates inherited helpers. On Unix the external reaper
    # should have killed this process group before the fallback is reached.
    os._exit(0)


def _watch_desktop_parent(
    parent_pid: int,
    *,
    wait_for_exit=_wait_for_desktop_parent_exit,
    terminate_backend=_terminate_backend_after_parent_exit,
) -> None:
    """Wait for the desktop process and terminate this sidecar afterwards."""
    if wait_for_exit(parent_pid):
        terminate_backend()


def _start_desktop_parent_watchdog() -> threading.Thread | None:
    """Bind a packaged backend's lifetime to the Tauri desktop process."""
    raw_parent_pid = os.environ.get(_DESKTOP_PARENT_PID_ENV, "").strip()
    if not raw_parent_pid:
        return None
    try:
        parent_pid = int(raw_parent_pid)
    except ValueError:
        _write_lifecycle_diagnostic("invalid desktop parent pid")
        return None
    if parent_pid <= 1 or parent_pid == os.getpid():
        _write_lifecycle_diagnostic("invalid desktop parent pid")
        return None

    thread = threading.Thread(
        target=_watch_desktop_parent,
        args=(parent_pid,),
        name="desktop-parent-watchdog",
        daemon=True,
    )
    thread.start()
    return thread


def _install_crash_reporter() -> None:
    """Install global crash handlers so unhandled exceptions are always logged to stderr.

    stderr is piped to backend.log by the desktop shell (Tauri/Electron),
    so this ensures crash tracebacks are captured for diagnosis.
    """
    # faulthandler: prints C-level tracebacks on segfaults, aborts, etc.
    faulthandler.enable(file=sys.stderr, all_threads=True)

    # sys.excepthook: catches unhandled Python exceptions in the main thread
    _original_excepthook = sys.excepthook

    def _crash_excepthook(exc_type, exc_value, exc_tb):
        msg = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        # Write with a clear marker so the desktop shell can detect it
        sys.stderr.write(f"\n[FATAL CRASH] Unhandled exception:\n{msg}\n")
        sys.stderr.flush()
        _original_excepthook(exc_type, exc_value, exc_tb)

    sys.excepthook = _crash_excepthook

    # threading.excepthook: catches unhandled exceptions in spawned threads
    import threading

    _original_thread_excepthook = threading.excepthook

    def _thread_crash_hook(args):
        msg = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
        sys.stderr.write(
            f"\n[FATAL CRASH] Unhandled exception in thread {args.thread}:\n{msg}\n"
        )
        sys.stderr.flush()
        _original_thread_excepthook(args)

    threading.excepthook = _thread_crash_hook


def _configure_bundled_node(resource_dir: str | None) -> Path | None:
    """Prepend the packaged Node tool directory to PATH when it exists."""
    if not resource_dir:
        return None
    runtime_root = Path(resource_dir) / "nodejs"
    bin_dir = runtime_root if os.name == "nt" else runtime_root / "bin"
    if not bin_dir.is_dir():
        return None

    resolved = bin_dir.resolve()
    current = [Path(part) for part in os.environ.get("PATH", "").split(os.pathsep) if part]
    deduplicated = [
        part
        for part in current
        if os.path.normcase(os.path.abspath(part))
        != os.path.normcase(os.path.abspath(resolved))
    ]
    os.environ["PATH"] = os.pathsep.join([str(resolved), *(str(part) for part in deduplicated)])
    os.environ["SUXIAOYOU_NODE_BIN_DIR"] = str(resolved)
    return resolved


def _configure_runtime(args: argparse.Namespace):
    """Apply desktop launcher arguments before importing application modules.

    The Tauri shell selects a random loopback port.  Persisted ``.env`` files
    commonly still contain 8000, so merely passing the CLI port to Uvicorn
    leaves OAuth callbacks and tunnels using a different port via Settings.
    Keep one explicit Settings instance as the source of truth without writing
    the ephemeral port back to disk.
    """
    if args.data_dir:
        os.makedirs(args.data_dir, exist_ok=True)
        os.chdir(args.data_dir)

    # Command/Python sandboxes use this canonical boundary to reject a broad
    # workspace that would otherwise contain per-user config and credentials.
    os.environ[_APP_PRIVATE_DIR_ENV] = str(Path.cwd().resolve())

    if args.resource_dir:
        os.environ["SUXIAOYOU_RESOURCE_DIR"] = args.resource_dir
    _configure_bundled_node(args.resource_dir)

    os.environ["SUXIAOYOU_HOST"] = "127.0.0.1"
    os.environ["SUXIAOYOU_PORT"] = str(args.port)

    if args.legacy_data_dir:
        try:
            from app.legacy_data import migrate_legacy_data

            report = migrate_legacy_data(Path(args.legacy_data_dir), Path.cwd())
            if report["status"] == "complete":
                print("[data-migration] predecessor data imported", file=sys.stderr)
        except Exception as exc:
            # Migration is deliberately retryable and must never prevent the
            # application from starting. No completion marker is written when
            # a phase fails.
            print(f"[data-migration] import deferred: {exc}", file=sys.stderr)

    # Import only after cwd/env are final so pydantic-settings cannot cache a
    # stale port or load a different data-directory .env file.
    from app.config import Settings

    return Settings(host="127.0.0.1", port=args.port)


def _handle_database_recovery(args: argparse.Namespace) -> bool:
    """Run offline DB recovery without constructing Settings or the app."""

    if not args.list_backups and not args.restore_backup:
        return False
    if args.data_dir:
        os.makedirs(args.data_dir, exist_ok=True)
        os.chdir(args.data_dir)

    import json

    from app.storage.migrations import (
        list_database_backups,
        restore_database_backup,
    )

    database_url = (
        args.database_url or "sqlite+aiosqlite:///./data/suxiaoyou.db"
    )
    if args.list_backups:
        payload = {
            "database_url": database_url,
            "backups": list_database_backups(database_url),
        }
    else:
        result = restore_database_backup(database_url, args.restore_backup)
        payload = {
            "status": "restored",
            "database_path": str(result.database_path),
            "restored_backup_path": str(result.restored_backup_path),
            "restored_revision": result.restored_revision,
            "safety_backup_path": (
                str(result.safety_backup_path) if result.safety_backup_path else None
            ),
            "safety_backup_metadata_path": (
                str(result.safety_backup_metadata_path)
                if result.safety_backup_metadata_path
                else None
            ),
        }
    print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
    return True


def main() -> None:
    # The frozen backend executable doubles as the isolated Python worker.
    # Dispatch before server lifecycle setup so arbitrary code can never run in
    # the long-lived API process or inherit its watchdog/job state.
    if len(sys.argv) == 3 and sys.argv[1] == "--sandbox-python-worker":
        from app.tool.sandbox_worker import run_code_file

        raise SystemExit(run_code_file(sys.argv[2]))

    if len(sys.argv) == 3 and sys.argv[1] == "--sandbox-self-test":
        from app.tool.sandbox_self_test import main as sandbox_self_test

        raise SystemExit(sandbox_self_test(sys.argv[2]))

    if len(sys.argv) == 2 and sys.argv[1] == "--provider-self-test":
        # Offline constructor/import contract for the final PyInstaller bundle.
        # No provider API method is called and the fake keys never leave this
        # process; verify-bundle.mjs requires the JSON marker before shipping.
        import json

        from anthropic import AsyncAnthropic
        from google import genai

        from app.provider.factory import create_provider

        anthropic_provider = create_provider("anthropic", "bundle-smoke-key")
        google_provider = create_provider("google", "bundle-smoke-key")
        if not isinstance(anthropic_provider._client, AsyncAnthropic):
            raise RuntimeError("Anthropic provider did not construct the official SDK client")
        if not isinstance(google_provider._client, genai.Client):
            raise RuntimeError("Gemini provider did not construct the official SDK client")
        print(
            json.dumps(
                {"status": "ok", "providers": ["anthropic", "google"]},
                separators=(",", ":"),
            ),
            flush=True,
        )
        raise SystemExit(0)

    _install_crash_reporter()
    _configure_windows_process_job()
    _start_desktop_parent_watchdog()

    parser = argparse.ArgumentParser(description="苏小有 backend server")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on")
    parser.add_argument("--data-dir", type=str, default=None, help="Data directory (for desktop mode)")
    parser.add_argument("--resource-dir", type=str, default=None, help="Resource directory (bundled assets from Tauri)")
    parser.add_argument(
        "--legacy-data-dir",
        type=str,
        default=None,
        help="Optional predecessor desktop data directory to import once",
    )
    parser.add_argument(
        "--database-url",
        type=str,
        default=None,
        help="File-backed SQLite URL for offline backup recovery",
    )
    recovery = parser.add_mutually_exclusive_group()
    recovery.add_argument(
        "--list-backups",
        action="store_true",
        help="List and checksum-verify database backups without starting the service",
    )
    recovery.add_argument(
        "--restore-backup",
        type=str,
        default=None,
        metavar="MANIFEST_OR_BACKUP",
        help="Atomically restore a verified backup without starting the service",
    )
    args = parser.parse_args()

    if _handle_database_recovery(args):
        return

    settings = _configure_runtime(args)

    import uvicorn
    from app.main import create_app

    app = create_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
