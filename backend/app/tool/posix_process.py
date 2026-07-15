"""Bounded, synchronous POSIX process execution.

This module owns the low-level lifetime contract shared by sandboxed command
tools: the child starts in a new session, both output pipes are continuously
drained without unbounded buffering, and the complete process group is reaped
on normal completion as well as timeout or cancellation.

The runner is intentionally synchronous.  Callers that live on an asyncio
event loop should invoke it in a worker thread and use ``should_abort`` as the
thread-safe cancellation bridge.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import errno
import math
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time
from typing import Literal


TerminationReason = Literal["timeout", "aborted"]

_POLL_SECONDS = 0.02
_TERM_GRACE_SECONDS = 0.25
_GROUP_REAP_SECONDS = 2.0
_PIPE_REAP_SECONDS = 0.5
_READ_CHUNK_BYTES = 64 * 1024
_MAX_READS_PER_EVENT = 4


class PosixProcessCleanupError(RuntimeError):
    """Raised when a launched process group cannot be proven terminated."""


@dataclass(frozen=True, slots=True)
class PosixProcessResult:
    """Final result of one :func:`run_posix_process` invocation.

    ``max_output_bytes`` is applied independently to stdout and stderr.  The
    pipes continue to be drained after that limit so a noisy child cannot
    deadlock; excess bytes are discarded and reported through ``truncated``.
    """

    exit_code: int
    stdout: bytes
    stderr: bytes
    termination: TerminationReason | None
    truncated: bool


class _BoundedBytes:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._data = bytearray()
        self.truncated = False

    def extend(self, chunk: bytes) -> None:
        remaining = self._limit - len(self._data)
        if remaining > 0:
            self._data.extend(chunk[:remaining])
        if len(chunk) > max(remaining, 0):
            self.truncated = True

    def value(self) -> bytes:
        return bytes(self._data)


def _validate_inputs(
    argv: Sequence[str | os.PathLike[str]],
    *,
    timeout_seconds: float,
    max_output_bytes: int,
) -> None:
    if os.name != "posix":
        raise RuntimeError("run_posix_process is available only on POSIX platforms")
    if not argv:
        raise ValueError("argv must not be empty")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(float(timeout_seconds))
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be a positive finite number")
    if isinstance(max_output_bytes, bool) or not isinstance(max_output_bytes, int):
        raise ValueError("max_output_bytes must be an integer")
    if max_output_bytes < 0:
        raise ValueError("max_output_bytes must not be negative")


def _group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        if exc.errno == errno.EPERM:
            return True
        raise
    return True


def _signal_group(process_group: int, signum: int) -> bool:
    """Signal *process_group* and return whether it still existed."""

    try:
        os.killpg(process_group, signum)
    except ProcessLookupError:
        return False
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        raise PosixProcessCleanupError(
            f"Could not signal process group {process_group}: {exc}"
        ) from exc
    return True


def run_posix_process(
    argv: Sequence[str | os.PathLike[str]],
    *,
    cwd: str | os.PathLike[str],
    env: Mapping[str, str],
    timeout_seconds: float,
    should_abort: Callable[[], bool],
    max_output_bytes: int,
) -> PosixProcessResult:
    """Run an argv-form command with bounded output and process-group cleanup.

    The returned ``termination`` is ``None`` for a normal child exit,
    ``"timeout"`` when the wall-clock limit fired, and ``"aborted"`` when
    ``should_abort`` returned true.  A callback exception is re-raised only
    after the process group has been terminated and reaped.

    Every launched process takes the same cleanup path.  Even after a normal
    shell exit, SIGTERM followed by SIGKILL (when necessary) is applied to its
    process group so background descendants cannot leak into later tool calls.
    """

    _validate_inputs(
        argv,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
    )
    if should_abort():
        return PosixProcessResult(
            exit_code=-1,
            stdout=b"",
            stderr=b"",
            termination="aborted",
            truncated=False,
        )

    process = subprocess.Popen(
        [os.fspath(value) for value in argv],
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=os.fspath(Path(cwd)),
        env=dict(env),
        start_new_session=True,
        bufsize=0,
    )
    process_group = process.pid
    stdout = _BoundedBytes(max_output_bytes)
    stderr = _BoundedBytes(max_output_bytes)
    selector = selectors.DefaultSelector()
    streams: dict[int, tuple[object, _BoundedBytes]] = {}
    termination: TerminationReason | None = None
    pending_error: BaseException | None = None
    cleanup_error: BaseException | None = None
    cleanup_drain_error: BaseException | None = None

    def register_stream(stream: object, collector: _BoundedBytes) -> None:
        fileno = stream.fileno()  # type: ignore[attr-defined]
        os.set_blocking(fileno, False)
        selector.register(fileno, selectors.EVENT_READ)
        streams[fileno] = (stream, collector)

    def close_stream(fileno: int) -> None:
        stream, _collector = streams.pop(fileno)
        try:
            selector.unregister(fileno)
        except (KeyError, OSError, ValueError):
            pass
        try:
            stream.close()  # type: ignore[attr-defined]
        except OSError:
            pass

    def drain_ready(wait_seconds: float) -> None:
        if not streams:
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            return
        try:
            events = selector.select(max(0.0, wait_seconds))
        except OSError as exc:
            if exc.errno == errno.EINTR:
                return
            raise
        for key, _mask in events:
            fileno = int(key.fd)
            stream_entry = streams.get(fileno)
            if stream_entry is None:
                continue
            _stream, collector = stream_entry
            for _ in range(_MAX_READS_PER_EVENT):
                try:
                    chunk = os.read(fileno, _READ_CHUNK_BYTES)
                except BlockingIOError:
                    break
                except InterruptedError:
                    continue
                if not chunk:
                    close_stream(fileno)
                    break
                collector.extend(chunk)
                # A short non-blocking read consumed what was immediately
                # available.  Return to the selector so the other stream gets
                # an equal opportunity to drain during output floods.
                if len(chunk) < _READ_CHUNK_BYTES:
                    break

    def drain_during_cleanup(wait_seconds: float) -> None:
        """Best-effort drain that never interrupts the signal escalation."""

        nonlocal cleanup_drain_error
        if cleanup_drain_error is not None:
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            return
        try:
            drain_ready(wait_seconds)
        except BaseException as exc:
            cleanup_drain_error = exc
            if wait_seconds > 0:
                time.sleep(wait_seconds)

    try:
        try:
            assert process.stdout is not None
            assert process.stderr is not None
            register_stream(process.stdout, stdout)
            register_stream(process.stderr, stderr)
            started = time.monotonic()

            while True:
                drain_ready(_POLL_SECONDS)
                if process.poll() is not None:
                    break
                try:
                    abort_requested = bool(should_abort())
                except BaseException as exc:
                    pending_error = exc
                    termination = "aborted"
                    break
                if abort_requested:
                    termination = "aborted"
                    break
                if time.monotonic() - started >= timeout_seconds:
                    termination = "timeout"
                    break
        except BaseException as exc:
            pending_error = exc

        try:
            # The leader may already have exited normally.  Its process group
            # can still contain background children, so cleanup is mandatory
            # on the success path too.
            group_present = _signal_group(process_group, signal.SIGTERM)
            if group_present:
                term_deadline = time.monotonic() + _TERM_GRACE_SECONDS
                while _group_exists(process_group) and time.monotonic() < term_deadline:
                    # Reap the leader as soon as it exits.  On Darwin a zombie
                    # session leader keeps ``killpg(pgid, 0)`` reporting that
                    # the group exists even after SIGTERM did its job.
                    process.poll()
                    drain_during_cleanup(
                        min(_POLL_SECONDS, max(0.0, term_deadline - time.monotonic()))
                    )
                if _group_exists(process_group):
                    _signal_group(process_group, signal.SIGKILL)

            reap_deadline = time.monotonic() + _GROUP_REAP_SECONDS
            # Reap the direct child before using killpg(0) as the final group
            # existence proof; otherwise its zombie alone looks like a leaked
            # process group on macOS.
            remaining = max(0.01, reap_deadline - time.monotonic())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired as exc:
                raise PosixProcessCleanupError(
                    f"Process {process.pid} could not be reaped"
                ) from exc
            while _group_exists(process_group) and time.monotonic() < reap_deadline:
                drain_during_cleanup(
                    min(_POLL_SECONDS, max(0.0, reap_deadline - time.monotonic()))
                )
            if _group_exists(process_group):
                raise PosixProcessCleanupError(
                    f"Process group {process_group} survived SIGKILL"
                )

            # All in-group writers should now be closed.  Continue draining to
            # EOF so final output emitted during SIGTERM is not lost.
            pipe_deadline = time.monotonic() + _PIPE_REAP_SECONDS
            while streams and time.monotonic() < pipe_deadline:
                drain_during_cleanup(
                    min(_POLL_SECONDS, max(0.0, pipe_deadline - time.monotonic()))
                )
            if cleanup_drain_error is not None:
                raise PosixProcessCleanupError(
                    "Could not drain process output during cleanup"
                ) from cleanup_drain_error
            if streams:
                raise PosixProcessCleanupError(
                    "Process output pipes remained open after group cleanup"
                )
        except BaseException as exc:
            cleanup_error = exc
    finally:
        for fileno in tuple(streams):
            close_stream(fileno)
        selector.close()

    if cleanup_error is not None:
        if pending_error is not None:
            raise cleanup_error from pending_error
        raise cleanup_error
    if pending_error is not None:
        raise pending_error

    return PosixProcessResult(
        exit_code=int(process.returncode if process.returncode is not None else -1),
        stdout=stdout.value(),
        stderr=stderr.value(),
        termination=termination,
        truncated=stdout.truncated or stderr.truncated,
    )


__all__ = [
    "PosixProcessCleanupError",
    "PosixProcessResult",
    "TerminationReason",
    "run_posix_process",
]
