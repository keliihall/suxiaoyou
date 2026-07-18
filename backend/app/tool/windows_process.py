"""Race-free Windows process-tree execution.

The standard-library ``subprocess.Popen`` API returns only after the new
process has started.  Assigning that process to a Job Object afterwards leaves
a small but real interval in which a shell can create a child outside the Job.
This module closes that interval by creating the process suspended, assigning
it to a kill-on-close Job Object, and only then resuming its primary thread.

The high-level state machine is intentionally platform-neutral and accepts an
injectable :class:`WindowsProcessApi`.  Unit tests can therefore exercise every
cleanup path on non-Windows hosts.  The ctypes implementation is imported and
constructed only on native Windows.

This runner is a *process lifetime* boundary.  It does not by itself restrict
filesystem or network access; those policies belong to the sandbox launch
layer that supplies ``argv``, ``cwd`` and ``env``.
"""

from __future__ import annotations

import ntpath
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import BinaryIO, Callable, Mapping, Protocol, Sequence


DEFAULT_MAX_OUTPUT_BYTES = 2 * 1024 * 1024
DEFAULT_POLL_INTERVAL_SECONDS = 0.2
DEFAULT_TERMINATION_GRACE_SECONDS = 5.0
_READ_CHUNK_BYTES = 64 * 1024
_WINDOWS_TERMINATION_EXIT_CODE = 0xC000013A


class WindowsProcessError(RuntimeError):
    """Raised when a Windows child cannot be safely launched or reaped."""


@dataclass(slots=True)
class WindowsChildProcess:
    """Opaque native process handles plus captured-output read streams."""

    process_handle: object
    thread_handle: object | None
    pid: int
    stdout: BinaryIO
    stderr: BinaryIO


@dataclass(frozen=True, slots=True)
class WindowsProcessResult:
    """Bounded captured output and the command's terminal state."""

    exit_code: int
    stdout: bytes
    stderr: bytes
    termination: str | None
    pid: int
    stdout_total_bytes: int
    stderr_total_bytes: int
    stdout_truncated: bool
    stderr_truncated: bool


class WindowsProcessApi(Protocol):
    """Small injectable Win32 surface used by the launch state machine."""

    def create_kill_on_close_job(self) -> object:
        """Create and configure a Job Object with KILL_ON_JOB_CLOSE."""

    def create_suspended_process(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        stdin_bytes: bytes | None = None,
    ) -> WindowsChildProcess:
        """Create a child with its primary thread suspended."""

    def assign_process_to_job(
        self,
        job: object,
        process: WindowsChildProcess,
    ) -> None:
        """Assign the still-suspended process to ``job``."""

    def resume_process(self, process: WindowsChildProcess) -> None:
        """Resume and release the child's primary thread handle."""

    def wait_process(self, process: WindowsChildProcess, timeout_ms: int) -> bool:
        """Return true when the process was signalled within ``timeout_ms``."""

    def get_exit_code(self, process: WindowsChildProcess) -> int:
        """Return the native process exit code."""

    def terminate_job(self, job: object, exit_code: int) -> None:
        """Terminate every process currently associated with ``job``."""

    def terminate_process(self, process: WindowsChildProcess, exit_code: int) -> None:
        """Terminate an unassigned suspended process after assignment failure."""

    def close_job(self, job: object) -> None:
        """Close the Job Object, killing any remaining descendants."""

    def close_process(self, process: WindowsChildProcess) -> None:
        """Close the process/thread handles (but not output streams)."""


class _BoundedOutput:
    """Keep a bounded prefix while continuing to count and drain all bytes."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._data = bytearray()
        self.total_bytes = 0

    def append(self, chunk: bytes) -> None:
        self.total_bytes += len(chunk)
        remaining = self._limit - len(self._data)
        if remaining > 0:
            self._data.extend(chunk[:remaining])

    @property
    def value(self) -> bytes:
        return bytes(self._data)

    @property
    def truncated(self) -> bool:
        return self.total_bytes > len(self._data)


def _drain_stream(
    stream: BinaryIO,
    collector: _BoundedOutput,
    errors: list[BaseException],
) -> None:
    """Drain one pipe completely, even after the retained prefix is full."""

    try:
        while True:
            chunk = stream.read(_READ_CHUNK_BYTES)
            if not chunk:
                return
            collector.append(chunk)
    except BaseException as exc:  # pragma: no cover - exercised through state tests
        errors.append(exc)


def _safe_cleanup(
    action: Callable[[], object],
    errors: list[BaseException],
) -> None:
    try:
        action()
    except BaseException as exc:  # cleanup must continue after one failed close
        errors.append(exc)


def run_windows_process(
    argv: Sequence[str],
    *,
    cwd: str,
    env: Mapping[str, str],
    timeout_seconds: float,
    should_abort: Callable[[], bool] | None = None,
    api: WindowsProcessApi | None = None,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    termination_grace_seconds: float = DEFAULT_TERMINATION_GRACE_SECONDS,
    stdin_bytes: bytes | None = None,
    _clock: Callable[[], float] = time.monotonic,
) -> WindowsProcessResult:
    """Run one Windows command inside a race-free kill-on-close Job Object.

    This function is synchronous by design.  Async tools should invoke it via
    ``asyncio.to_thread`` so the same implementation works with Windows'
    SelectorEventLoop.

    The process is always created suspended, assigned to the Job, and resumed
    in that order.  A failed assignment terminates the still-suspended process
    directly.  Closing the Job happens on normal completion as well as timeout,
    cancellation, and exceptions; doing so before joining the pipe readers also
    terminates detached descendants that inherited stdout/stderr handles.
    """

    if not argv or not argv[0]:
        raise ValueError("Windows process argv must contain an executable")
    if timeout_seconds <= 0:
        raise ValueError("Windows process timeout must be positive")
    if poll_interval_seconds <= 0:
        raise ValueError("Windows process poll interval must be positive")
    if termination_grace_seconds <= 0:
        raise ValueError("Windows process termination grace must be positive")
    if max_output_bytes < 0:
        raise ValueError("Windows process output limit cannot be negative")
    if stdin_bytes is not None and not isinstance(stdin_bytes, bytes):
        raise ValueError("Windows process stdin_bytes must be bytes or None")

    process_api = api if api is not None else CtypesWindowsProcessApi()
    abort_requested = should_abort or (lambda: False)
    if abort_requested():
        return WindowsProcessResult(
            exit_code=-1,
            stdout=b"",
            stderr=b"",
            termination="aborted",
            pid=-1,
            stdout_total_bytes=0,
            stderr_total_bytes=0,
            stdout_truncated=False,
            stderr_truncated=False,
        )
    stdout = _BoundedOutput(max_output_bytes)
    stderr = _BoundedOutput(max_output_bytes)
    drain_errors: list[BaseException] = []
    cleanup_errors: list[BaseException] = []
    reader_threads: list[threading.Thread] = []
    job: object | None = None
    process: WindowsChildProcess | None = None
    assigned = False
    exit_code: int | None = None
    termination: str | None = None
    caught: BaseException | None = None
    caught_traceback = None

    poll_ms = max(1, int(poll_interval_seconds * 1000))
    termination_grace_ms = max(1, int(termination_grace_seconds * 1000))

    try:
        job = process_api.create_kill_on_close_job()
        if stdin_bytes is None:
            # Preserve compatibility with existing injectable process APIs.
            process = process_api.create_suspended_process(argv, cwd=cwd, env=env)
        else:
            process = process_api.create_suspended_process(
                argv,
                cwd=cwd,
                env=env,
                stdin_bytes=stdin_bytes,
            )

        # Security-critical ordering: the child is not allowed to execute until
        # its lifetime has been made subordinate to the Job Object.
        process_api.assign_process_to_job(job, process)
        assigned = True

        if abort_requested():
            # The process is still suspended and already contained by the Job,
            # so cancellation at this boundary executes no child code.
            termination = "aborted"
            process_api.terminate_job(job, _WINDOWS_TERMINATION_EXIT_CODE)
            if process_api.wait_process(process, termination_grace_ms):
                exit_code = process_api.get_exit_code(process)
        else:
            reader_threads = [
                threading.Thread(
                    target=_drain_stream,
                    args=(process.stdout, stdout, drain_errors),
                    name=f"windows-process-{process.pid}-stdout",
                    daemon=True,
                ),
                threading.Thread(
                    target=_drain_stream,
                    args=(process.stderr, stderr, drain_errors),
                    name=f"windows-process-{process.pid}-stderr",
                    daemon=True,
                ),
            ]
            for reader in reader_threads:
                reader.start()

            process_api.resume_process(process)
            started = _clock()

            while True:
                if abort_requested():
                    termination = "aborted"
                    process_api.terminate_job(job, _WINDOWS_TERMINATION_EXIT_CODE)
                    if process_api.wait_process(process, termination_grace_ms):
                        exit_code = process_api.get_exit_code(process)
                    break

                elapsed = _clock() - started
                if elapsed >= timeout_seconds:
                    termination = "timeout"
                    process_api.terminate_job(job, _WINDOWS_TERMINATION_EXIT_CODE)
                    if process_api.wait_process(process, termination_grace_ms):
                        exit_code = process_api.get_exit_code(process)
                    break

                remaining_ms = max(1, int((timeout_seconds - elapsed) * 1000))
                if process_api.wait_process(process, min(poll_ms, remaining_ms)):
                    exit_code = process_api.get_exit_code(process)
                    break
    except BaseException as exc:
        caught = exc
        caught_traceback = exc.__traceback__
    finally:
        if process is not None and not assigned:
            # Closing an unrelated Job cannot terminate a process whose
            # assignment failed.  It is still suspended, so direct termination
            # is race-free and no user code has run.
            _safe_cleanup(
                lambda: process_api.terminate_process(
                    process,
                    _WINDOWS_TERMINATION_EXIT_CODE,
                ),
                cleanup_errors,
            )

        if job is not None:
            # On normal completion this is what reaps a detached descendant.
            # It also provides the final hard-stop fallback on every error path.
            _safe_cleanup(lambda: process_api.close_job(job), cleanup_errors)

        if process is not None:
            try:
                signalled = process_api.wait_process(process, termination_grace_ms)
                if not signalled:
                    cleanup_errors.append(
                        WindowsProcessError(
                            "Windows process did not exit after Job termination"
                        )
                    )
                elif caught is None and exit_code is None and assigned:
                    # A timeout/abort grace wait may expire before the Job has
                    # finished terminating the process.  Job closure above is
                    # the final hard stop; only report an exit status after a
                    # wait has positively observed the signalled process.
                    exit_code = process_api.get_exit_code(process)
            except BaseException as exc:
                cleanup_errors.append(exc)

            # Job closure should make every inherited write handle disappear.
            # Use a bounded join anyway so a broken API cannot hang the backend.
            for reader in reader_threads:
                reader.join(termination_grace_seconds)
            alive = [reader for reader in reader_threads if reader.is_alive()]
            if alive:
                _safe_cleanup(process.stdout.close, cleanup_errors)
                _safe_cleanup(process.stderr.close, cleanup_errors)
                for reader in alive:
                    reader.join(termination_grace_seconds)
                if any(reader.is_alive() for reader in alive):
                    cleanup_errors.append(
                        WindowsProcessError(
                            "Windows output pipe did not close after Job termination"
                        )
                    )

            _safe_cleanup(process.stdout.close, cleanup_errors)
            _safe_cleanup(process.stderr.close, cleanup_errors)
            _safe_cleanup(lambda: process_api.close_process(process), cleanup_errors)

    if caught is not None:
        raise caught.with_traceback(caught_traceback)
    if drain_errors:
        raise WindowsProcessError(
            f"Windows output pipe read failed: {drain_errors[0]}"
        ) from drain_errors[0]
    if cleanup_errors:
        raise WindowsProcessError(
            f"Windows process cleanup failed: {cleanup_errors[0]}"
        ) from cleanup_errors[0]
    if process is None or exit_code is None:
        raise WindowsProcessError("Windows process finished without an exit status")

    return WindowsProcessResult(
        exit_code=exit_code,
        stdout=stdout.value,
        stderr=stderr.value,
        termination=termination,
        pid=process.pid,
        stdout_total_bytes=stdout.total_bytes,
        stderr_total_bytes=stderr.total_bytes,
        stdout_truncated=stdout.truncated,
        stderr_truncated=stderr.truncated,
    )


class CtypesWindowsProcessApi:
    """Native Win32 implementation of :class:`WindowsProcessApi`."""

    _CREATE_SUSPENDED = 0x00000004
    _CREATE_NEW_PROCESS_GROUP = 0x00000200
    _CREATE_UNICODE_ENVIRONMENT = 0x00000400
    _CREATE_NO_WINDOW = 0x08000000
    _EXTENDED_STARTUPINFO_PRESENT = 0x00080000
    _STARTF_USESHOWWINDOW = 0x00000001
    _STARTF_USESTDHANDLES = 0x00000100
    _SW_HIDE = 0
    _HANDLE_FLAG_INHERIT = 0x00000001
    _PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _WAIT_OBJECT_0 = 0x00000000
    _WAIT_TIMEOUT = 0x00000102
    _INFINITE_MINUS_ONE = 0xFFFFFFFE

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise WindowsProcessError(
                "The ctypes Windows process API requires native Windows"
            )

        import ctypes
        import msvcrt
        from ctypes import wintypes

        class SecurityAttributes(ctypes.Structure):
            _fields_ = [
                ("nLength", wintypes.DWORD),
                ("lpSecurityDescriptor", wintypes.LPVOID),
                ("bInheritHandle", wintypes.BOOL),
            ]

        class StartupInfoW(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("lpReserved", wintypes.LPWSTR),
                ("lpDesktop", wintypes.LPWSTR),
                ("lpTitle", wintypes.LPWSTR),
                ("dwX", wintypes.DWORD),
                ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD),
                ("dwYSize", wintypes.DWORD),
                ("dwXCountChars", wintypes.DWORD),
                ("dwYCountChars", wintypes.DWORD),
                ("dwFillAttribute", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD),
                ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
                ("hStdInput", wintypes.HANDLE),
                ("hStdOutput", wintypes.HANDLE),
                ("hStdError", wintypes.HANDLE),
            ]

        class StartupInfoExW(ctypes.Structure):
            _fields_ = [
                ("StartupInfo", StartupInfoW),
                ("lpAttributeList", wintypes.LPVOID),
            ]

        class ProcessInformation(ctypes.Structure):
            _fields_ = [
                ("hProcess", wintypes.HANDLE),
                ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD),
                ("dwThreadId", wintypes.DWORD),
            ]

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

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.CreatePipe.argtypes = [
            ctypes.POINTER(wintypes.HANDLE),
            ctypes.POINTER(wintypes.HANDLE),
            ctypes.POINTER(SecurityAttributes),
            wintypes.DWORD,
        ]
        kernel32.CreatePipe.restype = wintypes.BOOL
        kernel32.SetHandleInformation.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        kernel32.SetHandleInformation.restype = wintypes.BOOL
        kernel32.InitializeProcThreadAttributeList.argtypes = [
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
        kernel32.UpdateProcThreadAttribute.argtypes = [
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.c_size_t,
            wintypes.LPVOID,
            ctypes.c_size_t,
            wintypes.LPVOID,
            wintypes.LPVOID,
        ]
        kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
        kernel32.DeleteProcThreadAttributeList.argtypes = [wintypes.LPVOID]
        kernel32.DeleteProcThreadAttributeList.restype = None
        kernel32.CreateProcessW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.LPCWSTR,
            ctypes.POINTER(StartupInfoW),
            ctypes.POINTER(ProcessInformation),
        ]
        kernel32.CreateProcessW.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
        ]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel32.ResumeThread.restype = wintypes.DWORD
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        self._ctypes = ctypes
        self._msvcrt = msvcrt
        self._wintypes = wintypes
        self._kernel32 = kernel32
        self._SecurityAttributes = SecurityAttributes
        self._StartupInfoExW = StartupInfoExW
        self._StartupInfoW = StartupInfoW
        self._ProcessInformation = ProcessInformation
        self._JobObjectExtendedLimitInformation = JobObjectExtendedLimitInformation

    def _raise_last_error(self, action: str) -> None:
        error = self._ctypes.get_last_error()
        raise WindowsProcessError(f"{action} failed: {self._ctypes.WinError(error)}")

    @staticmethod
    def _handle_value(handle: object) -> int:
        value = getattr(handle, "value", handle)
        if value is None:
            return 0
        return int(value)

    def _close_native_handle(self, handle: object | None) -> None:
        if handle is None or self._handle_value(handle) == 0:
            return
        if not self._kernel32.CloseHandle(handle):
            self._raise_last_error("CloseHandle")

    def create_kill_on_close_job(self) -> object:
        job = self._kernel32.CreateJobObjectW(None, None)
        if not job:
            self._raise_last_error("CreateJobObjectW")

        info = self._JobObjectExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = (
            self._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        configured = self._kernel32.SetInformationJobObject(
            job,
            self._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            self._ctypes.byref(info),
            self._ctypes.sizeof(info),
        )
        if not configured:
            error = self._ctypes.get_last_error()
            self._kernel32.CloseHandle(job)
            raise WindowsProcessError(
                "SetInformationJobObject failed: "
                f"{self._ctypes.WinError(error)}"
            )
        return job

    def _create_pipe(self) -> tuple[object, object]:
        read_handle = self._wintypes.HANDLE()
        write_handle = self._wintypes.HANDLE()
        attributes = self._SecurityAttributes(
            self._ctypes.sizeof(self._SecurityAttributes),
            None,
            True,
        )
        if not self._kernel32.CreatePipe(
            self._ctypes.byref(read_handle),
            self._ctypes.byref(write_handle),
            self._ctypes.byref(attributes),
            0,
        ):
            self._raise_last_error("CreatePipe")
        return read_handle, write_handle

    def _make_non_inheritable(self, handle: object) -> None:
        if not self._kernel32.SetHandleInformation(
            handle,
            self._HANDLE_FLAG_INHERIT,
            0,
        ):
            self._raise_last_error("SetHandleInformation")

    def _make_inheritable(self, handle: object) -> None:
        if not self._kernel32.SetHandleInformation(
            handle,
            self._HANDLE_FLAG_INHERIT,
            self._HANDLE_FLAG_INHERIT,
        ):
            self._raise_last_error("SetHandleInformation")

    @staticmethod
    def _environment_block(env: Mapping[str, str]) -> str:
        entries: list[tuple[str, str]] = []
        for key, value in env.items():
            key_text = str(key)
            value_text = str(value)
            if not key_text or "=" in key_text or "\0" in key_text or "\0" in value_text:
                raise ValueError(f"Invalid Windows environment entry: {key_text!r}")
            entries.append((key_text, value_text))
        entries.sort(key=lambda item: item[0].upper())
        return "\0".join(f"{key}={value}" for key, value in entries) + "\0\0"

    def _stream_from_handle(self, handle: object) -> BinaryIO:
        descriptor = self._msvcrt.open_osfhandle(
            self._handle_value(handle),
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
        return os.fdopen(descriptor, "rb", buffering=0)

    def create_suspended_process(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        stdin_bytes: bytes | None = None,
    ) -> WindowsChildProcess:
        if not argv or not argv[0]:
            raise ValueError("Windows process argv must contain an executable")
        executable = os.path.abspath(os.fspath(argv[0]))
        if not ntpath.isabs(executable) or not os.path.isfile(executable):
            raise WindowsProcessError(
                f"Windows executable must be an existing absolute file: {argv[0]}"
            )
        absolute_cwd = os.path.abspath(cwd)
        if not ntpath.isabs(absolute_cwd) or not os.path.isdir(absolute_cwd):
            raise WindowsProcessError(
                f"Windows working directory must be an existing absolute directory: {cwd}"
            )

        handles: list[object] = []
        process_info = self._ProcessInformation()
        attribute_buffer = None
        attribute_list = None
        stdout_stream: BinaryIO | None = None
        stderr_stream: BinaryIO | None = None
        stdin_stream: BinaryIO | None = None
        process_created = False
        try:
            stdin_pipe_handles: tuple[object, ...] = ()
            if stdin_bytes is None:
                stdin_read, stdin_write = self._create_pipe()
                stdin_pipe_handles = (stdin_read, stdin_write)
                handles.extend(stdin_pipe_handles)
                self._make_non_inheritable(stdin_write)
                stdin_child_handle = stdin_read
            else:
                if not isinstance(stdin_bytes, bytes):
                    raise ValueError("Windows process stdin_bytes must be bytes or None")
                stdin_stream = tempfile.TemporaryFile()
                stdin_stream.write(stdin_bytes)
                stdin_stream.seek(0)
                stdin_child_handle = self._msvcrt.get_osfhandle(
                    stdin_stream.fileno()
                )
                self._make_inheritable(stdin_child_handle)
            stdout_read, stdout_write = self._create_pipe()
            handles.extend((stdout_read, stdout_write))
            stderr_read, stderr_write = self._create_pipe()
            handles.extend((stderr_read, stderr_write))
            self._make_non_inheritable(stdout_read)
            self._make_non_inheritable(stderr_read)

            attribute_size = self._ctypes.c_size_t()
            self._kernel32.InitializeProcThreadAttributeList(
                None,
                1,
                0,
                self._ctypes.byref(attribute_size),
            )
            if attribute_size.value == 0:
                self._raise_last_error("InitializeProcThreadAttributeList(size)")
            attribute_buffer = self._ctypes.create_string_buffer(attribute_size.value)
            attribute_list = self._ctypes.cast(
                attribute_buffer,
                self._wintypes.LPVOID,
            )
            if not self._kernel32.InitializeProcThreadAttributeList(
                attribute_list,
                1,
                0,
                self._ctypes.byref(attribute_size),
            ):
                self._raise_last_error("InitializeProcThreadAttributeList")

            inherited_handles = (self._wintypes.HANDLE * 3)(
                self._handle_value(stdin_child_handle),
                self._handle_value(stdout_write),
                self._handle_value(stderr_write),
            )
            if not self._kernel32.UpdateProcThreadAttribute(
                attribute_list,
                0,
                self._PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                self._ctypes.cast(inherited_handles, self._wintypes.LPVOID),
                self._ctypes.sizeof(inherited_handles),
                None,
                None,
            ):
                self._raise_last_error("UpdateProcThreadAttribute(handle list)")

            startup = self._StartupInfoExW()
            startup.StartupInfo.cb = self._ctypes.sizeof(self._StartupInfoExW)
            startup.StartupInfo.dwFlags = (
                self._STARTF_USESHOWWINDOW | self._STARTF_USESTDHANDLES
            )
            startup.StartupInfo.wShowWindow = self._SW_HIDE
            startup.StartupInfo.hStdInput = stdin_child_handle
            startup.StartupInfo.hStdOutput = stdout_write
            startup.StartupInfo.hStdError = stderr_write
            startup.lpAttributeList = attribute_list

            command_line = self._ctypes.create_unicode_buffer(
                subprocess.list2cmdline([os.fspath(value) for value in argv])
            )
            environment = self._ctypes.create_unicode_buffer(
                self._environment_block(env)
            )
            creation_flags = (
                self._CREATE_SUSPENDED
                | self._CREATE_NEW_PROCESS_GROUP
                | self._CREATE_UNICODE_ENVIRONMENT
                | self._CREATE_NO_WINDOW
                | self._EXTENDED_STARTUPINFO_PRESENT
            )
            created = self._kernel32.CreateProcessW(
                executable,
                command_line,
                None,
                None,
                True,
                creation_flags,
                self._ctypes.cast(environment, self._wintypes.LPVOID),
                absolute_cwd,
                self._ctypes.byref(startup.StartupInfo),
                self._ctypes.byref(process_info),
            )
            if not created:
                self._raise_last_error("CreateProcessW(CREATE_SUSPENDED)")
            process_created = True

            # The parent must not retain any writer.  The explicit HANDLE_LIST
            # ensured no unrelated inheritable backend handle crossed over.
            for handle in (*stdin_pipe_handles, stdout_write, stderr_write):
                self._close_native_handle(handle)
                handles.remove(handle)
            if stdin_stream is not None:
                stdin_stream.close()
                stdin_stream = None

            stdout_stream = self._stream_from_handle(stdout_read)
            handles.remove(stdout_read)  # ownership moved to the Python stream
            stderr_stream = self._stream_from_handle(stderr_read)
            handles.remove(stderr_read)

            return WindowsChildProcess(
                process_handle=process_info.hProcess,
                thread_handle=process_info.hThread,
                pid=int(process_info.dwProcessId),
                stdout=stdout_stream,
                stderr=stderr_stream,
            )
        except BaseException:
            if process_created:
                self._kernel32.TerminateProcess(
                    process_info.hProcess,
                    _WINDOWS_TERMINATION_EXIT_CODE,
                )
                self._kernel32.WaitForSingleObject(process_info.hProcess, 5000)
                self._kernel32.CloseHandle(process_info.hThread)
                self._kernel32.CloseHandle(process_info.hProcess)
            if stdout_stream is not None:
                stdout_stream.close()
            if stderr_stream is not None:
                stderr_stream.close()
            if stdin_stream is not None:
                stdin_stream.close()
            for handle in reversed(handles):
                try:
                    self._close_native_handle(handle)
                except BaseException:
                    pass
            raise
        finally:
            if attribute_list is not None:
                self._kernel32.DeleteProcThreadAttributeList(attribute_list)

    def assign_process_to_job(
        self,
        job: object,
        process: WindowsChildProcess,
    ) -> None:
        if not self._kernel32.AssignProcessToJobObject(
            job,
            process.process_handle,
        ):
            self._raise_last_error("AssignProcessToJobObject")

    def resume_process(self, process: WindowsChildProcess) -> None:
        if process.thread_handle is None:
            raise WindowsProcessError("Windows process primary thread is unavailable")
        result = self._kernel32.ResumeThread(process.thread_handle)
        if result == 0xFFFFFFFF:
            self._raise_last_error("ResumeThread")
        thread_handle, process.thread_handle = process.thread_handle, None
        self._close_native_handle(thread_handle)

    def wait_process(self, process: WindowsChildProcess, timeout_ms: int) -> bool:
        timeout = min(max(0, int(timeout_ms)), self._INFINITE_MINUS_ONE)
        result = self._kernel32.WaitForSingleObject(process.process_handle, timeout)
        if result == self._WAIT_OBJECT_0:
            return True
        if result == self._WAIT_TIMEOUT:
            return False
        self._raise_last_error("WaitForSingleObject")
        return False  # pragma: no cover - _raise_last_error always raises

    def get_exit_code(self, process: WindowsChildProcess) -> int:
        exit_code = self._wintypes.DWORD()
        if not self._kernel32.GetExitCodeProcess(
            process.process_handle,
            self._ctypes.byref(exit_code),
        ):
            self._raise_last_error("GetExitCodeProcess")
        return int(exit_code.value)

    def terminate_job(self, job: object, exit_code: int) -> None:
        if not self._kernel32.TerminateJobObject(job, int(exit_code)):
            self._raise_last_error("TerminateJobObject")

    def terminate_process(self, process: WindowsChildProcess, exit_code: int) -> None:
        if not self._kernel32.TerminateProcess(
            process.process_handle,
            int(exit_code),
        ):
            self._raise_last_error("TerminateProcess")

    def close_job(self, job: object) -> None:
        self._close_native_handle(job)

    def close_process(self, process: WindowsChildProcess) -> None:
        errors: list[BaseException] = []
        if process.thread_handle is not None:
            thread_handle, process.thread_handle = process.thread_handle, None
            try:
                self._close_native_handle(thread_handle)
            except BaseException as exc:
                errors.append(exc)
        try:
            self._close_native_handle(process.process_handle)
        except BaseException as exc:
            errors.append(exc)
        if errors:
            raise WindowsProcessError(
                f"Could not close Windows process handles: {errors[0]}"
            ) from errors[0]


__all__ = [
    "CtypesWindowsProcessApi",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "DEFAULT_TERMINATION_GRACE_SECONDS",
    "WindowsChildProcess",
    "WindowsProcessApi",
    "WindowsProcessError",
    "WindowsProcessResult",
    "run_windows_process",
]
