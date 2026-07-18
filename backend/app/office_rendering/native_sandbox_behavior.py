"""Native-machine adversarial behavior probe for Office sandbox launchers.

The signed helper is required to attempt forbidden network, host-write,
input-write, and delayed-descendant actions while producing one allowed output
proof.  Helper stdout only declares that those attempts were made.  Python
decides success exclusively from externally observed socket, file, output, and
delayed-marker state after the process-tree runner has returned.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import re
import secrets
import shutil
import socket
import stat
import tempfile
import threading
from types import MappingProxyType
from typing import Any, Final, Mapping

from app.office_rendering.native_sandbox import NativeSandboxContract
from app.office_rendering.process_runner import (
    LocalProcessTreeRunner,
    RenderProcessResult,
    RenderProcessRunner,
)


NATIVE_SANDBOX_BEHAVIOR_SCHEMA_VERSION: Final = 1
_PROBE_HELPER = {
    "darwin": PurePosixPath("bin/suxiaoyou-office-sandbox-probe"),
    "linux": PurePosixPath("bin/suxiaoyou-office-sandbox-probe"),
    "windows": PurePosixPath("bin/suxiaoyou-office-sandbox-probe.exe"),
}
_ATTEMPTS: Final = (
    "delayed_descendant_marker",
    "host_canary_write",
    "input_write",
    "loopback_connect",
    "output_proof_write",
)
_MAX_PROTOCOL_BYTES: Final = 64 * 1024
_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
_PLATFORM_TARGET: Final = re.compile(r"^(?:darwin|linux|windows)-(?:arm64|x64)$")
_SAFE_IDENTIFIER: Final = re.compile(r"^[a-z0-9][a-z0-9._-]{0,255}$")
_OBSERVED_CAPABILITIES: Final = frozenset(
    {
        "host_filesystem_read_only",
        "network_denied",
        "private_input_read_only",
        "private_output_write_only",
        "process_tree_contained",
    }
)
_REPORT_IDENTITIES: Final = {
    "darwin": (
        "suxiaoyou.office-sandbox.macos-app-sandbox-xpc.v1",
        frozenset(
            {
                "app_sandbox",
                "host_filesystem_read_only",
                "network_denied",
                "private_input_read_only",
                "private_output_write_only",
                "process_tree_contained",
                "xpc_service",
            }
        ),
    ),
    "windows": (
        "suxiaoyou.office-sandbox.windows-appcontainer-restricted-token.v1",
        frozenset(
            {
                "app_container",
                "host_filesystem_read_only",
                "kill_on_close_job",
                "network_denied",
                "private_input_read_only",
                "private_output_write_only",
                "process_tree_contained",
                "restricted_token",
            }
        ),
    ),
    "linux": (
        "suxiaoyou.office-sandbox.linux-namespaces-seccomp-cgroup.v1",
        frozenset(
            {
                "cgroup",
                "host_filesystem_read_only",
                "mount_namespace",
                "network_denied",
                "network_namespace",
                "private_input_read_only",
                "private_output_write_only",
                "process_tree_contained",
                "seccomp",
                "user_namespace",
            }
        ),
    ),
}


class NativeSandboxBehaviorProbeError(RuntimeError):
    """Native sandbox behavior was not externally proven."""


@dataclass(frozen=True, slots=True)
class NativeSandboxBehaviorReport:
    schema_version: int
    status: str
    platform_target: str
    contract_id: str
    bundle_tree_sha256: str
    sandbox_manifest_sha256: str
    dependency_manifest_sha256: str
    launcher_sha256: str
    helper_sha256: str
    nonce_sha256: str
    attempts_sha256: str
    output_proof_sha256: str
    evidence_sha256: str
    capabilities: Mapping[str, bool]
    native_behavior_proven: bool

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != NATIVE_SANDBOX_BEHAVIOR_SCHEMA_VERSION
            or type(self.status) is not str
            or self.status != "proven"
            or self.native_behavior_proven is not True
        ):
            raise NativeSandboxBehaviorProbeError(
                "native sandbox behavior report is invalid"
            )
        if (
            type(self.platform_target) is not str
            or _PLATFORM_TARGET.fullmatch(self.platform_target) is None
        ):
            raise NativeSandboxBehaviorProbeError(
                "native sandbox behavior report identity is invalid"
            )
        family = self.platform_target.split("-", 1)[0]
        expected_contract_id, expected_capabilities = _REPORT_IDENTITIES[family]
        if (
            type(self.contract_id) is not str
            or _SAFE_IDENTIFIER.fullmatch(self.contract_id) is None
            or self.contract_id != expected_contract_id
        ):
            raise NativeSandboxBehaviorProbeError(
                "native sandbox behavior report identity is invalid"
            )
        hashes = (
            self.bundle_tree_sha256,
            self.sandbox_manifest_sha256,
            self.dependency_manifest_sha256,
            self.launcher_sha256,
            self.helper_sha256,
            self.nonce_sha256,
            self.attempts_sha256,
            self.output_proof_sha256,
            self.evidence_sha256,
        )
        if any(
            type(value) is not str or _SHA256.fullmatch(value) is None
            for value in hashes
        ):
            raise NativeSandboxBehaviorProbeError(
                "native sandbox behavior report digest is invalid"
            )
        if not isinstance(self.capabilities, Mapping):
            raise NativeSandboxBehaviorProbeError(
                "native sandbox behavior capabilities are invalid"
            )
        try:
            capabilities = dict(self.capabilities)
        except Exception:
            raise NativeSandboxBehaviorProbeError(
                "native sandbox behavior capabilities are invalid"
            ) from None
        if (
            not capabilities
            or set(capabilities) != expected_capabilities
            or any(
                type(name) is not str
                or _SAFE_IDENTIFIER.fullmatch(name) is None
                or type(value) is not bool
                for name, value in capabilities.items()
            )
            or any(capabilities[name] is not True for name in _OBSERVED_CAPABILITIES)
            or any(
                capabilities[name] is not False
                for name in expected_capabilities - _OBSERVED_CAPABILITIES
            )
        ):
            raise NativeSandboxBehaviorProbeError(
                "native sandbox behavior capabilities are invalid"
            )
        expected_evidence_sha256 = hashlib.sha256(
            _canonical_json_bytes(
                _behavior_evidence_payload(
                    platform_target=self.platform_target,
                    contract_id=self.contract_id,
                    bundle_tree_sha256=self.bundle_tree_sha256,
                    sandbox_manifest_sha256=self.sandbox_manifest_sha256,
                    dependency_manifest_sha256=self.dependency_manifest_sha256,
                    launcher_sha256=self.launcher_sha256,
                    helper_sha256=self.helper_sha256,
                    nonce_sha256=self.nonce_sha256,
                    attempts_sha256=self.attempts_sha256,
                    output_proof_sha256=self.output_proof_sha256,
                    capabilities=capabilities,
                )
            )
        ).hexdigest()
        if self.evidence_sha256 != expected_evidence_sha256:
            raise NativeSandboxBehaviorProbeError(
                "native sandbox behavior report evidence is inconsistent"
            )
        object.__setattr__(self, "capabilities", MappingProxyType(capabilities))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "platform_target": self.platform_target,
            "contract_id": self.contract_id,
            "bundle_tree_sha256": self.bundle_tree_sha256,
            "sandbox_manifest_sha256": self.sandbox_manifest_sha256,
            "dependency_manifest_sha256": self.dependency_manifest_sha256,
            "launcher_sha256": self.launcher_sha256,
            "helper_sha256": self.helper_sha256,
            "nonce_sha256": self.nonce_sha256,
            "attempts_sha256": self.attempts_sha256,
            "output_proof_sha256": self.output_proof_sha256,
            "evidence_sha256": self.evidence_sha256,
            "capabilities": dict(self.capabilities),
            "native_behavior_proven": self.native_behavior_proven,
        }


@dataclass(frozen=True, slots=True)
class _ProbeTiming:
    helper_timeout_seconds: float = 30.0
    descendant_delay_ms: int = 500
    observation_grace_seconds: float = 1.5

    def __post_init__(self) -> None:
        if (
            not isinstance(self.descendant_delay_ms, int)
            or isinstance(self.descendant_delay_ms, bool)
            or self.descendant_delay_ms < 1
            or not isinstance(self.helper_timeout_seconds, (int, float))
            or isinstance(self.helper_timeout_seconds, bool)
            or not math.isfinite(float(self.helper_timeout_seconds))
            or self.helper_timeout_seconds <= 0
            or self.helper_timeout_seconds > 120
            or not isinstance(self.observation_grace_seconds, (int, float))
            or isinstance(self.observation_grace_seconds, bool)
            or not math.isfinite(float(self.observation_grace_seconds))
            or self.observation_grace_seconds > 30
            or self.descendant_delay_ms > 5_000
            or self.observation_grace_seconds
            < (self.descendant_delay_ms / 1000.0) * 2
        ):
            raise NativeSandboxBehaviorProbeError(
                "native sandbox probe timing is invalid"
            )


class _LoopbackObserver:
    def __init__(self) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(8)
        self._socket.settimeout(0.02)
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._failed = threading.Event()
        self.accepted = threading.Event()
        self._started = False
        self._finished = False
        self._thread = threading.Thread(
            target=self._observe,
            name="office-native-sandbox-loopback-observer",
            daemon=True,
        )

    @property
    def port(self) -> int:
        return int(self._socket.getsockname()[1])

    def start(self) -> None:
        self._thread.start()
        self._started = True
        if not self._ready.wait(timeout=1.0) or self._failed.is_set():
            raise NativeSandboxBehaviorProbeError(
                "native sandbox loopback observer failed"
            )

    def assert_no_connection(self) -> None:
        if not self._started or self._finished:
            raise NativeSandboxBehaviorProbeError(
                "native sandbox loopback observer is invalid"
            )
        self._stop.set()
        self._thread.join(timeout=1.0)
        if self._thread.is_alive() or self._failed.is_set():
            raise NativeSandboxBehaviorProbeError(
                "native sandbox loopback observer failed"
            )
        try:
            self._socket.setblocking(False)
            while True:
                connection, _address = self._socket.accept()
                self.accepted.set()
                connection.close()
        except BlockingIOError:
            pass
        except OSError:
            raise NativeSandboxBehaviorProbeError(
                "native sandbox loopback observer failed"
            ) from None
        finally:
            try:
                self._socket.close()
            except OSError:
                pass
            self._finished = True

    def close(self) -> None:
        self._stop.set()
        try:
            self._socket.close()
        except OSError:
            pass
        if self._started:
            try:
                self._thread.join(timeout=1.0)
            except RuntimeError:
                pass
        self._finished = True

    def _observe(self) -> None:
        self._ready.set()
        while not self._stop.is_set():
            try:
                connection, _address = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:
                if not self._stop.is_set():
                    self._failed.set()
                return
            self.accepted.set()
            try:
                connection.close()
            except OSError:
                pass


async def run_native_sandbox_behavior_probe(
    contract: NativeSandboxContract,
    runner: RenderProcessRunner | None = None,
    *,
    _timing: _ProbeTiming | None = None,
) -> NativeSandboxBehaviorReport:
    """Run the signed native helper and return only path-free failures."""

    try:
        return await _run_native_sandbox_behavior_probe(
            contract,
            runner,
            _timing=_timing,
        )
    except asyncio.CancelledError:
        raise
    except NativeSandboxBehaviorProbeError as exc:
        raise NativeSandboxBehaviorProbeError(str(exc)) from None
    except Exception:
        raise NativeSandboxBehaviorProbeError(
            "native sandbox behavior probe failed"
        ) from None


async def _run_native_sandbox_behavior_probe(
    contract: NativeSandboxContract,
    runner: RenderProcessRunner | None = None,
    *,
    _timing: _ProbeTiming | None = None,
) -> NativeSandboxBehaviorReport:
    """Run the signed native helper and externally prove sandbox outcomes."""

    if not isinstance(contract, NativeSandboxContract):
        raise NativeSandboxBehaviorProbeError("native sandbox contract is invalid")
    timing = _timing if _timing is not None else _ProbeTiming()
    if not isinstance(timing, _ProbeTiming):
        raise NativeSandboxBehaviorProbeError("native sandbox probe timing is invalid")
    selected_runner = runner if runner is not None else LocalProcessTreeRunner()
    if not isinstance(selected_runner, RenderProcessRunner):
        raise NativeSandboxBehaviorProbeError("native sandbox probe runner is invalid")
    if contract.platform_target != _current_platform_target():
        raise NativeSandboxBehaviorProbeError(
            "native sandbox behavior probe target does not match this machine"
        )

    family = contract.platform_target.split("-", 1)[0]
    helper_relative = _PROBE_HELPER[family]
    helper = contract._root.joinpath(*helper_relative.parts)  # noqa: SLF001
    helper_identity = contract._native_executables.get(helper)  # noqa: SLF001
    if helper_identity is None:
        raise NativeSandboxBehaviorProbeError(
            "native sandbox probe helper is outside the native closure"
        )
    helper_sha256 = _stable_file_sha256(
        helper,
        expected_size=helper_identity[1],
        expected_sha256=helper_identity[0],
    )

    probe_root = Path(
        tempfile.mkdtemp(prefix=".suxiaoyou-native-sandbox-probe-")
    ).resolve(strict=True)
    observer: _LoopbackObserver | None = None
    try:
        os.chmod(probe_root, 0o700)
        work = _private_directory(probe_root / "work")
        input_dir = _private_directory(work / "input")
        output_dir = _private_directory(work / "output")
        home = _private_directory(work / "home")
        temporary = _private_directory(work / "tmp")
        xdg_config = _private_directory(work / "xdg-config")
        xdg_cache = _private_directory(work / "xdg-cache")
        xdg_data = _private_directory(work / "xdg-data")
        xdg_runtime = _private_directory(work / "xdg-runtime")

        nonce = secrets.token_hex(32)
        nonce_sha256 = hashlib.sha256(nonce.encode("ascii")).hexdigest()
        canary = probe_root / f"host-canary-{secrets.token_hex(16)}.bin"
        input_file = input_dir / f"input-{secrets.token_hex(16)}.bin"
        output_proof = output_dir / f"proof-{secrets.token_hex(16)}.json"
        descendant_marker = (
            output_dir / f"descendant-{secrets.token_hex(16)}.bin"
        )
        canary_bytes = secrets.token_bytes(64)
        input_bytes = secrets.token_bytes(64)
        _write_private_file(canary, canary_bytes, mode=0o600)
        _write_private_file(input_file, input_bytes, mode=0o400)
        root_identity = _directory_identity(probe_root)
        work_identity = _directory_identity(work)
        input_directory_identity = _directory_identity(input_dir)
        canary_identity = _stat_identity(canary.lstat())
        input_identity = _stat_identity(input_file.lstat())
        output_directory_identity = _directory_identity(output_dir)

        observer = _LoopbackObserver()
        observer.start()
        helper_argv = (
            str(helper),
            "--probe-schema",
            str(NATIVE_SANDBOX_BEHAVIOR_SCHEMA_VERSION),
            "--nonce",
            nonce,
            "--loopback-host",
            "127.0.0.1",
            "--loopback-port",
            str(observer.port),
            "--host-canary",
            str(canary),
            "--input-file",
            str(input_file),
            "--output-proof",
            str(output_proof),
            "--descendant-marker",
            str(descendant_marker),
            "--descendant-delay-ms",
            str(timing.descendant_delay_ms),
        )
        argv = contract.build_no_shell_argv(helper_argv, work_dir=work)
        try:
            result = await selected_runner.run(
                argv,
                cwd=work,
                env=_probe_environment(
                    launcher=contract._launcher_path,  # noqa: SLF001
                    home=home,
                    temporary=temporary,
                    xdg_config=xdg_config,
                    xdg_cache=xdg_cache,
                    xdg_data=xdg_data,
                    xdg_runtime=xdg_runtime,
                ),
                timeout_seconds=float(timing.helper_timeout_seconds),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise NativeSandboxBehaviorProbeError(
                "native sandbox probe helper execution failed"
            ) from None
        if not isinstance(result, RenderProcessResult) or result.returncode != 0:
            raise NativeSandboxBehaviorProbeError(
                "native sandbox probe helper did not exit successfully"
            )
        attempts = _validate_attempts_stdout(result.stdout, result.stderr, nonce=nonce)
        await asyncio.sleep(float(timing.observation_grace_seconds))

        observer.assert_no_connection()
        if observer.accepted.is_set():
            raise NativeSandboxBehaviorProbeError(
                "native sandbox probe observed a forbidden network connection"
            )
        _require_directory_identity(probe_root, root_identity)
        _require_directory_identity(work, work_identity)
        _require_directory_identity(input_dir, input_directory_identity)
        _require_directory_identity(output_dir, output_directory_identity)
        if (
            _stable_read(
                canary,
                max_bytes=1024,
                expected_identity=canary_identity,
            )
            != canary_bytes
        ):
            raise NativeSandboxBehaviorProbeError(
                "native sandbox probe observed a forbidden host write"
            )
        if (
            _stable_read(
                input_file,
                max_bytes=1024,
                expected_identity=input_identity,
            )
            != input_bytes
        ):
            raise NativeSandboxBehaviorProbeError(
                "native sandbox probe observed a forbidden input write"
            )
        if descendant_marker.exists() or descendant_marker.is_symlink():
            raise NativeSandboxBehaviorProbeError(
                "native sandbox probe observed an escaped descendant"
            )
        proof_bytes = _validate_output_directory(
            output_dir,
            output_proof,
            expected_directory_identity=output_directory_identity,
            nonce=nonce,
        )

        attempts_sha256 = hashlib.sha256(attempts).hexdigest()
        output_proof_sha256 = hashlib.sha256(proof_bytes).hexdigest()
        capabilities = {
            name: name in _OBSERVED_CAPABILITIES
            for name in sorted(contract.capabilities)
        }
        evidence = _behavior_evidence_payload(
            platform_target=contract.platform_target,
            contract_id=contract.contract_id,
            bundle_tree_sha256=contract.bundle_tree_sha256,
            sandbox_manifest_sha256=contract.sandbox_manifest_sha256,
            dependency_manifest_sha256=contract.dependency_manifest_sha256,
            launcher_sha256=contract.launcher_sha256,
            helper_sha256=helper_sha256,
            nonce_sha256=nonce_sha256,
            attempts_sha256=attempts_sha256,
            output_proof_sha256=output_proof_sha256,
            capabilities=capabilities,
        )
        evidence_sha256 = hashlib.sha256(_canonical_json_bytes(evidence)).hexdigest()
        return NativeSandboxBehaviorReport(
            schema_version=NATIVE_SANDBOX_BEHAVIOR_SCHEMA_VERSION,
            status="proven",
            platform_target=contract.platform_target,
            contract_id=contract.contract_id,
            bundle_tree_sha256=contract.bundle_tree_sha256,
            sandbox_manifest_sha256=contract.sandbox_manifest_sha256,
            dependency_manifest_sha256=contract.dependency_manifest_sha256,
            launcher_sha256=contract.launcher_sha256,
            helper_sha256=helper_sha256,
            nonce_sha256=nonce_sha256,
            attempts_sha256=attempts_sha256,
            output_proof_sha256=output_proof_sha256,
            evidence_sha256=evidence_sha256,
            capabilities=capabilities,
            native_behavior_proven=True,
        )
    except asyncio.CancelledError:
        raise
    except NativeSandboxBehaviorProbeError:
        raise
    except Exception:
        raise NativeSandboxBehaviorProbeError(
            "native sandbox behavior probe failed"
        ) from None
    finally:
        if observer is not None:
            observer.close()
        _make_tree_removable(probe_root)
        shutil.rmtree(probe_root, ignore_errors=True)


def _behavior_evidence_payload(
    *,
    platform_target: str,
    contract_id: str,
    bundle_tree_sha256: str,
    sandbox_manifest_sha256: str,
    dependency_manifest_sha256: str,
    launcher_sha256: str,
    helper_sha256: str,
    nonce_sha256: str,
    attempts_sha256: str,
    output_proof_sha256: str,
    capabilities: Mapping[str, bool],
) -> dict[str, Any]:
    return {
        "domain": "suxiaoyou-office-native-sandbox-behavior-v1",
        "platform_target": platform_target,
        "contract_id": contract_id,
        "bundle_tree_sha256": bundle_tree_sha256,
        "sandbox_manifest_sha256": sandbox_manifest_sha256,
        "dependency_manifest_sha256": dependency_manifest_sha256,
        "launcher_sha256": launcher_sha256,
        "helper_sha256": helper_sha256,
        "nonce_sha256": nonce_sha256,
        "attempts_sha256": attempts_sha256,
        "output_proof_sha256": output_proof_sha256,
        "capabilities": dict(capabilities),
        "observations": {
            "descendant_marker_absent": True,
            "host_canary_unchanged": True,
            "input_unchanged": True,
            "loopback_connection_absent": True,
            "output_proof_valid": True,
        },
    }


def _current_platform_target() -> str:
    system = platform.system().casefold()
    machine = platform.machine().casefold()
    family = {"darwin": "darwin", "linux": "linux", "windows": "windows"}.get(
        system
    )
    architecture = {
        "aarch64": "arm64",
        "arm64": "arm64",
        "amd64": "x64",
        "x86_64": "x64",
    }.get(machine)
    if family is None or architecture is None:
        raise NativeSandboxBehaviorProbeError(
            "native sandbox behavior probe platform is unsupported"
        )
    return f"{family}-{architecture}"


def _private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    return path.resolve(strict=True)


def _write_private_file(path: Path, content: bytes, *, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, mode)
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short native sandbox probe write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(path, mode)


def _probe_environment(
    *,
    launcher: Path,
    home: Path,
    temporary: Path,
    xdg_config: Path,
    xdg_cache: Path,
    xdg_data: Path,
    xdg_runtime: Path,
) -> dict[str, str]:
    environment = {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "TMPDIR": str(temporary),
        "TMP": str(temporary),
        "TEMP": str(temporary),
        "XDG_CONFIG_HOME": str(xdg_config),
        "XDG_CACHE_HOME": str(xdg_cache),
        "XDG_DATA_HOME": str(xdg_data),
        "XDG_RUNTIME_DIR": str(xdg_runtime),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": str(launcher.parent),
    }
    if os.name == "nt":
        for key in ("SYSTEMROOT", "WINDIR"):
            value = os.environ.get(key)
            if isinstance(value, str) and value and "\x00" not in value:
                environment[key] = value
    return environment


def _validate_attempts_stdout(stdout: bytes, stderr: bytes, *, nonce: str) -> bytes:
    if not isinstance(stdout, bytes) or not isinstance(stderr, bytes) or stderr:
        raise NativeSandboxBehaviorProbeError(
            "native sandbox probe helper emitted invalid output"
        )
    value = _decode_canonical_json(stdout, label="native sandbox probe attempts")
    expected = {
        "schema_version": NATIVE_SANDBOX_BEHAVIOR_SCHEMA_VERSION,
        "nonce": nonce,
        "attempts": {name: True for name in _ATTEMPTS},
    }
    if value != expected:
        raise NativeSandboxBehaviorProbeError(
            "native sandbox probe helper attempts are invalid"
        )
    return stdout


def _validate_output_directory(
    output_dir: Path,
    proof_path: Path,
    *,
    expected_directory_identity: tuple[int, int, int],
    nonce: str,
) -> bytes:
    try:
        info = output_dir.lstat()
        entries = tuple(sorted(output_dir.iterdir(), key=lambda value: value.name))
    except OSError as exc:
        raise NativeSandboxBehaviorProbeError(
            "native sandbox probe output is unavailable"
        ) from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or _directory_identity(output_dir) != expected_directory_identity
        or entries != (proof_path,)
        or proof_path.is_symlink()
    ):
        raise NativeSandboxBehaviorProbeError(
            "native sandbox probe produced unexpected output"
        )
    proof = _stable_read(proof_path, max_bytes=_MAX_PROTOCOL_BYTES)
    value = _decode_canonical_json(proof, label="native sandbox output proof")
    if value != {
        "schema_version": NATIVE_SANDBOX_BEHAVIOR_SCHEMA_VERSION,
        "nonce": nonce,
        "proof": "allowed-output-write",
    }:
        raise NativeSandboxBehaviorProbeError(
            "native sandbox output proof is invalid"
        )
    return proof


def _decode_canonical_json(raw: bytes, *, label: str) -> dict[str, Any]:
    if not isinstance(raw, bytes) or not raw or len(raw) > _MAX_PROTOCOL_BYTES:
        raise NativeSandboxBehaviorProbeError(f"{label} is invalid")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_no_duplicate_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise NativeSandboxBehaviorProbeError(f"{label} is invalid") from exc
    if not isinstance(value, dict) or raw != _canonical_json_bytes(value):
        raise NativeSandboxBehaviorProbeError(f"{label} is not canonical")
    return value


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
            + b"\n"
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise NativeSandboxBehaviorProbeError(
            "native sandbox probe evidence is invalid"
        ) from exc


def _no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _stable_read(
    path: Path,
    *,
    max_bytes: int,
    expected_identity: tuple[int, int, int, int, int] | None = None,
) -> bytes:
    if path.is_symlink():
        raise NativeSandboxBehaviorProbeError(
            "native sandbox probe file is redirected"
        )
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise NativeSandboxBehaviorProbeError(
            "native sandbox probe file is unavailable"
        ) from exc
    chunks: list[bytes] = []
    total = 0
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise NativeSandboxBehaviorProbeError(
                "native sandbox probe file identity is invalid"
            )
        if before.st_size > max_bytes:
            raise NativeSandboxBehaviorProbeError(
                "native sandbox probe file exceeds its budget"
            )
        while chunk := os.read(descriptor, 8192):
            total += len(chunk)
            if total > max_bytes:
                raise NativeSandboxBehaviorProbeError(
                    "native sandbox probe file exceeds its budget"
                )
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    visible = path.lstat()
    if (
        total != before.st_size
        or (expected_identity is not None and _stat_identity(before) != expected_identity)
        or _stat_identity(before) != _stat_identity(after)
        or _stat_identity(after) != _stat_identity(visible)
        or after.st_nlink != 1
        or visible.st_nlink != 1
        or stat.S_ISLNK(visible.st_mode)
    ):
        raise NativeSandboxBehaviorProbeError("native sandbox probe file changed")
    return b"".join(chunks)


def _stable_file_sha256(
    path: Path,
    *,
    expected_size: int,
    expected_sha256: str,
) -> str:
    content = _stable_read(path, max_bytes=max(expected_size, 1))
    actual = hashlib.sha256(content).hexdigest()
    if len(content) != expected_size or actual != expected_sha256:
        raise NativeSandboxBehaviorProbeError(
            "native sandbox probe helper identity changed"
        )
    if os.name != "nt" and not path.stat().st_mode & stat.S_IXUSR:
        raise NativeSandboxBehaviorProbeError(
            "native sandbox probe helper is not executable"
        )
    return actual


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _directory_identity(path: Path) -> tuple[int, int, int]:
    info = path.lstat()
    return (info.st_dev, info.st_ino, info.st_mode)


def _require_directory_identity(
    path: Path,
    expected_identity: tuple[int, int, int],
) -> None:
    try:
        identity = _directory_identity(path)
    except OSError:
        raise NativeSandboxBehaviorProbeError(
            "native sandbox probe directory identity changed"
        ) from None
    if identity != expected_identity or not stat.S_ISDIR(identity[2]):
        raise NativeSandboxBehaviorProbeError(
            "native sandbox probe directory identity changed"
        )


def _make_tree_removable(root: Path) -> None:
    try:
        for directory, names, filenames in os.walk(root, topdown=False):
            base = Path(directory)
            for name in filenames:
                _chmod_probe_entry_without_aliases(
                    base / name,
                    stat.S_IRUSR | stat.S_IWUSR,
                    expect_directory=False,
                )
            for name in names:
                _chmod_probe_entry_without_aliases(
                    base / name,
                    stat.S_IRWXU,
                    expect_directory=True,
                )
        _chmod_probe_entry_without_aliases(
            root,
            stat.S_IRWXU,
            expect_directory=True,
        )
    except OSError:
        pass


def _chmod_probe_entry_without_aliases(
    path: Path,
    mode: int,
    *,
    expect_directory: bool,
) -> None:
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            return
        if expect_directory:
            if not stat.S_ISDIR(info.st_mode):
                return
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return
        os.chmod(path, mode, follow_symlinks=False)
    except (NotImplementedError, OSError):
        pass


__all__ = [
    "NATIVE_SANDBOX_BEHAVIOR_SCHEMA_VERSION",
    "NativeSandboxBehaviorProbeError",
    "NativeSandboxBehaviorReport",
    "run_native_sandbox_behavior_probe",
]
