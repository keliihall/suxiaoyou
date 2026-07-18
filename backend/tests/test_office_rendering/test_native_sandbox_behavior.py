from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import threading
from typing import Mapping, Sequence

import pytest

import app.office_rendering.native_sandbox_behavior as behavior_module
from app.office_rendering.errors import RenderTimeoutError
from app.office_rendering.native_sandbox import load_native_sandbox_contract
from app.office_rendering.native_sandbox_behavior import (
    NativeSandboxBehaviorProbeError,
    NativeSandboxBehaviorReport,
    _ProbeTiming,
    _current_platform_target,
    run_native_sandbox_behavior_probe,
)
from app.office_rendering.process_runner import RenderProcessResult


_CONTRACTS = {
    "darwin": (
        "suxiaoyou.office-sandbox.macos-app-sandbox-xpc.v1",
        "bin/suxiaoyou-office-sandbox-launcher",
        {
            "app_sandbox",
            "host_filesystem_read_only",
            "network_denied",
            "private_input_read_only",
            "private_output_write_only",
            "process_tree_contained",
            "xpc_service",
        },
    ),
    "windows": (
        "suxiaoyou.office-sandbox.windows-appcontainer-restricted-token.v1",
        "bin/suxiaoyou-office-sandbox-launcher.exe",
        {
            "app_container",
            "host_filesystem_read_only",
            "kill_on_close_job",
            "network_denied",
            "private_input_read_only",
            "private_output_write_only",
            "process_tree_contained",
            "restricted_token",
        },
    ),
    "linux": (
        "suxiaoyou.office-sandbox.linux-namespaces-seccomp-cgroup.v1",
        "bin/suxiaoyou-office-sandbox-launcher",
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
        },
    ),
}
_ATTEMPTS = (
    "delayed_descendant_marker",
    "host_canary_write",
    "input_write",
    "loopback_connect",
    "output_proof_write",
)
_OBSERVED_CAPABILITIES = {
    "host_filesystem_read_only",
    "network_denied",
    "private_input_read_only",
    "private_output_write_only",
    "process_tree_contained",
}
_FAST_TIMING = _ProbeTiming(
    helper_timeout_seconds=1.0,
    descendant_delay_ms=10,
    observation_grace_seconds=0.05,
)


def _canonical(value: object) -> bytes:
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


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _write_executable(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o700)


def _make_contract(tmp_path: Path, *, include_helper: bool = True):
    target = _current_platform_target()
    family = target.split("-", 1)[0]
    contract_id, launcher_relative, capabilities = _CONTRACTS[family]
    helper_relative = (
        "bin/suxiaoyou-office-sandbox-probe.exe"
        if family == "windows"
        else "bin/suxiaoyou-office-sandbox-probe"
    )
    root = (tmp_path / "native-renderer").absolute()
    root.mkdir(parents=True)
    launcher = root.joinpath(*launcher_relative.split("/"))
    helper = root.joinpath(*helper_relative.split("/"))
    renderer = root / "bin" / ("soffice.exe" if family == "windows" else "soffice")
    _write_executable(launcher, b"fixed native sandbox launcher")
    _write_executable(renderer, b"fixed native renderer")
    if include_helper:
        _write_executable(helper, b"fixed signed adversarial sandbox probe")

    sandbox = {
        "schema_version": 1,
        "platform_target": target,
        "contract_id": contract_id,
        "launcher_path": launcher_relative,
        "capabilities": {name: True for name in sorted(capabilities)},
    }
    sandbox_bytes = _canonical(sandbox)
    (root / "sandbox-manifest.json").write_bytes(sandbox_bytes)

    executables = [launcher, renderer]
    if include_helper:
        executables.append(helper)
    records = []
    for path in executables:
        content = path.read_bytes()
        records.append(
            {
                "path": path.relative_to(root).as_posix(),
                "kind": "executable",
                "size": len(content),
                "sha256": _sha256(content),
                "dependencies": [],
            }
        )
    records.sort(key=lambda value: value["path"])
    dependency_bytes = _canonical(
        {
            "schema_version": 1,
            "platform_target": target,
            "files": records,
        }
    )
    (root / "dependency-manifest.json").write_bytes(dependency_bytes)
    contract = load_native_sandbox_contract(
        root,
        platform_target=target,
        attested_components={
            "sandbox-manifest": _sha256(sandbox_bytes),
            "dependency-manifest": _sha256(dependency_bytes),
            "bundle-tree": "f" * 64,
        },
    )
    return contract, root, launcher, helper


class FakeProbeRunner:
    def __init__(self, mode: str = "success", *, external_target: Path | None = None):
        self.mode = mode
        self.external_target = external_target
        self.argv: tuple[str, ...] | None = None
        self.cwd: Path | None = None
        self.env: dict[str, str] | None = None
        self.timeout_seconds: float | None = None
        self.external_mode_before_cleanup: int | None = None
        self.started = asyncio.Event()
        self.reaped = False
        self._timers: list[threading.Timer] = []

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_seconds: float,
    ) -> RenderProcessResult:
        self.argv = tuple(argv)
        self.cwd = cwd
        self.env = dict(env)
        self.timeout_seconds = timeout_seconds
        separator = self.argv.index("--")
        inner = self.argv[separator + 1 :]
        assert len(inner) == 19
        options = dict(zip(inner[1::2], inner[2::2], strict=True))
        nonce = options["--nonce"]
        output = Path(options["--output-proof"])

        if self.mode == "cancel":
            self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.reaped = True
        if self.mode == "timeout":
            self.reaped = True
            raise RenderTimeoutError("simulated timeout with a path that must not escape")

        if self.mode != "missing_proof":
            proof_nonce = "wrong-nonce" if self.mode == "wrong_proof_nonce" else nonce
            proof = {
                "schema_version": 1,
                "nonce": proof_nonce,
                "proof": "allowed-output-write",
            }
            output.parent.mkdir(parents=True, exist_ok=True)
            if self.mode == "noncanonical_proof":
                output.write_text(json.dumps(proof, indent=2), encoding="utf-8")
            else:
                output.write_bytes(_canonical(proof))

        if self.mode == "connect":
            with socket.create_connection(
                (options["--loopback-host"], int(options["--loopback-port"])),
                timeout=0.5,
            ):
                pass
        elif self.mode == "host_write":
            Path(options["--host-canary"]).write_bytes(b"forbidden host write")
        elif self.mode == "input_write":
            input_file = Path(options["--input-file"])
            input_file.chmod(0o600)
            input_file.write_bytes(b"forbidden input write")
        elif self.mode == "descendant_escape":
            timer = threading.Timer(
                int(options["--descendant-delay-ms"]) / 1000.0,
                Path(options["--descendant-marker"]).write_bytes,
                args=(b"escaped",),
            )
            timer.daemon = True
            self._timers.append(timer)
            timer.start()
        elif self.mode == "extra_output":
            (output.parent / "extra.bin").write_bytes(b"unexpected")
        elif self.mode == "proof_symlink":
            output.unlink()
            assert self.external_target is not None
            output.symlink_to(self.external_target)
        elif self.mode == "proof_hardlink":
            assert self.external_target is not None
            os.link(output, self.external_target)
            self.external_mode_before_cleanup = stat.S_IMODE(
                self.external_target.stat().st_mode
            )
        elif self.mode == "redirect_output_directory":
            original = output.parent.with_name("moved-output")
            output.parent.rename(original)
            output.parent.symlink_to(original, target_is_directory=True)
        elif self.mode == "redirect_input_directory":
            input_directory = Path(options["--input-file"]).parent
            original = input_directory.with_name("moved-input")
            input_directory.rename(original)
            input_directory.symlink_to(original, target_is_directory=True)
        elif self.mode == "redirect_output_external":
            assert self.external_target is not None
            original = output.parent.with_name("abandoned-output")
            output.parent.rename(original)
            external_proof = self.external_target / output.name
            external_mode = stat.S_IMODE(self.external_target.stat().st_mode)
            self.external_target.chmod(0o700)
            try:
                external_proof.write_bytes((original / output.name).read_bytes())
            finally:
                self.external_target.chmod(external_mode)
            output.parent.symlink_to(self.external_target, target_is_directory=True)

        attempts: dict[str, object] = {name: True for name in _ATTEMPTS}
        if self.mode == "attempt_false":
            attempts["loopback_connect"] = False
        declaration: dict[str, object] = {
            "schema_version": 1,
            "nonce": nonce,
            "attempts": attempts,
        }
        if self.mode == "fake_denial_report":
            declaration["denials"] = {"network": "denied"}
        if self.mode == "path_leak_report":
            declaration["host_path"] = options["--host-canary"]

        stdout = _canonical(declaration)
        if self.mode == "noncanonical_stdout":
            stdout = json.dumps(declaration, indent=2).encode("utf-8")
        stderr = b"helper diagnostic" if self.mode == "stderr" else b""
        self.reaped = True
        if self.mode == "invalid_result":
            return None  # type: ignore[return-value]
        return RenderProcessResult(
            7 if self.mode == "nonzero" else 0,
            stdout,
            stderr,
        )


@pytest.mark.asyncio
async def test_proven_report_is_path_free_and_binds_no_shell_execution(
    tmp_path: Path,
) -> None:
    contract, root, launcher, helper = _make_contract(tmp_path)
    runner = FakeProbeRunner()

    report = await run_native_sandbox_behavior_probe(
        contract,
        runner,
        _timing=_FAST_TIMING,
    )

    assert report.status == "proven"
    assert report.native_behavior_proven is True
    assert dict(report.capabilities) == {
        name: name in _OBSERVED_CAPABILITIES
        for name in sorted(contract.capabilities)
    }
    assert report.helper_sha256 == _sha256(helper.read_bytes())
    assert report.launcher_sha256 == contract.launcher_sha256
    assert report.bundle_tree_sha256 == "f" * 64
    assert runner.argv is not None
    assert runner.argv[0] == str(launcher)
    assert runner.argv[runner.argv.index("--") + 1] == str(helper)
    inner = runner.argv[runner.argv.index("--") + 1 :]
    options = dict(zip(inner[1::2], inner[2::2], strict=True))
    for name in (
        "--host-canary",
        "--input-file",
        "--output-proof",
        "--descendant-marker",
    ):
        path = Path(options[name])
        assert path.is_absolute()
        assert path == path.resolve(strict=False)
    assert runner.reaped is True
    assert runner.env is not None
    assert not any("proxy" in key.casefold() for key in runner.env)
    assert Path(runner.env["HOME"]).is_relative_to(runner.cwd)
    assert runner.timeout_seconds == _FAST_TIMING.helper_timeout_seconds
    encoded = json.dumps(report.to_dict(), sort_keys=True)
    assert str(root) not in encoded
    assert runner.cwd is not None
    assert str(runner.cwd.parent) not in encoded
    assert not runner.cwd.parent.exists()
    assert contract.path_free_evidence()["status"] == "declared-not-proven"
    assert contract.path_free_evidence()["native_behavior_proven"] is False


@pytest.mark.parametrize(
    ("mode", "message"),
    (
        ("connect", "network connection"),
        ("host_write", "file changed"),
        ("input_write", "file changed"),
        ("descendant_escape", "escaped descendant"),
        ("extra_output", "unexpected output"),
        ("wrong_proof_nonce", "output proof"),
        ("noncanonical_proof", "not canonical"),
        ("missing_proof", "unexpected output"),
        ("attempt_false", "attempts are invalid"),
        ("fake_denial_report", "attempts are invalid"),
        ("path_leak_report", "attempts are invalid"),
        ("noncanonical_stdout", "not canonical"),
        ("stderr", "invalid output"),
        ("nonzero", "did not exit successfully"),
        ("invalid_result", "did not exit successfully"),
        ("redirect_output_directory", "directory identity changed"),
        ("redirect_input_directory", "directory identity changed"),
    ),
)
@pytest.mark.asyncio
async def test_adversarial_observation_or_protocol_failure_fails_closed(
    tmp_path: Path,
    mode: str,
    message: str,
) -> None:
    contract, _root, _launcher, _helper = _make_contract(tmp_path)
    runner = FakeProbeRunner(mode)

    with pytest.raises(NativeSandboxBehaviorProbeError, match=message) as caught:
        await run_native_sandbox_behavior_probe(
            contract,
            runner,
            _timing=_FAST_TIMING,
        )

    assert runner.cwd is not None
    assert str(runner.cwd.parent) not in str(caught.value)
    assert caught.value.__cause__ is None
    assert not runner.cwd.parent.exists()


@pytest.mark.asyncio
async def test_output_symlink_failure_does_not_touch_external_target(
    tmp_path: Path,
) -> None:
    contract, _root, _launcher, _helper = _make_contract(tmp_path)
    external = tmp_path / "outside.bin"
    external.write_bytes(b"outside must remain unchanged")
    external.chmod(0o400)
    before_mode = stat.S_IMODE(external.stat().st_mode)
    runner = FakeProbeRunner("proof_symlink", external_target=external)

    with pytest.raises(NativeSandboxBehaviorProbeError, match="unexpected output"):
        await run_native_sandbox_behavior_probe(
            contract,
            runner,
            _timing=_FAST_TIMING,
        )

    assert external.read_bytes() == b"outside must remain unchanged"
    assert stat.S_IMODE(external.stat().st_mode) == before_mode
    assert runner.cwd is not None and not runner.cwd.parent.exists()


@pytest.mark.asyncio
async def test_output_proof_hardlink_cannot_forge_private_output(
    tmp_path: Path,
) -> None:
    contract, _root, _launcher, _helper = _make_contract(tmp_path / "bundle")
    external = tmp_path / "outside-proof.json"
    runner = FakeProbeRunner("proof_hardlink", external_target=external)

    with pytest.raises(NativeSandboxBehaviorProbeError, match="identity is invalid"):
        await run_native_sandbox_behavior_probe(
            contract,
            runner,
            _timing=_FAST_TIMING,
        )

    assert external.is_file()
    assert stat.S_IMODE(external.stat().st_mode) == runner.external_mode_before_cleanup
    assert runner.cwd is not None and not runner.cwd.parent.exists()


@pytest.mark.asyncio
async def test_output_parent_symlink_cleanup_does_not_chmod_external_directory(
    tmp_path: Path,
) -> None:
    contract, _root, _launcher, _helper = _make_contract(tmp_path / "bundle")
    external = tmp_path / "outside-output"
    external.mkdir(mode=0o500)
    before_mode = stat.S_IMODE(external.stat().st_mode)
    runner = FakeProbeRunner("redirect_output_external", external_target=external)

    with pytest.raises(NativeSandboxBehaviorProbeError, match="directory identity"):
        await run_native_sandbox_behavior_probe(
            contract,
            runner,
            _timing=_FAST_TIMING,
        )

    assert stat.S_IMODE(external.stat().st_mode) == before_mode
    assert runner.cwd is not None and not runner.cwd.parent.exists()


@pytest.mark.asyncio
async def test_helper_must_remain_executable_in_verified_native_closure(
    tmp_path: Path,
) -> None:
    contract, _root, _launcher, helper = _make_contract(tmp_path / "missing", include_helper=False)
    with pytest.raises(NativeSandboxBehaviorProbeError, match="outside the native closure"):
        await run_native_sandbox_behavior_probe(
            contract,
            FakeProbeRunner(),
            _timing=_FAST_TIMING,
        )

    contract, _root, _launcher, helper = _make_contract(tmp_path / "drift")
    helper.write_bytes(b"drifted helper")
    helper.chmod(0o700)
    with pytest.raises(NativeSandboxBehaviorProbeError, match="identity changed"):
        await run_native_sandbox_behavior_probe(
            contract,
            FakeProbeRunner(),
            _timing=_FAST_TIMING,
        )


@pytest.mark.asyncio
async def test_target_mismatch_fails_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, _root, _launcher, _helper = _make_contract(tmp_path)
    runner = FakeProbeRunner()
    other_target = "linux-x64" if contract.platform_target != "linux-x64" else "darwin-x64"
    monkeypatch.setattr(
        "app.office_rendering.native_sandbox_behavior._current_platform_target",
        lambda: other_target,
    )

    with pytest.raises(NativeSandboxBehaviorProbeError, match="does not match"):
        await run_native_sandbox_behavior_probe(
            contract,
            runner,
            _timing=_FAST_TIMING,
        )
    assert runner.argv is None


@pytest.mark.asyncio
async def test_timeout_fails_and_cleans_reaped_probe_tree(tmp_path: Path) -> None:
    contract, _root, _launcher, _helper = _make_contract(tmp_path)
    runner = FakeProbeRunner("timeout")

    with pytest.raises(
        NativeSandboxBehaviorProbeError,
        match="helper execution failed",
    ) as caught:
        await run_native_sandbox_behavior_probe(
            contract,
            runner,
            _timing=_FAST_TIMING,
        )

    assert runner.reaped is True
    assert runner.cwd is not None and not runner.cwd.parent.exists()
    assert caught.value.__cause__ is None
    assert str(runner.cwd.parent) not in str(caught.value)


@pytest.mark.asyncio
async def test_cancellation_waits_for_runner_cleanup_and_removes_probe_tree(
    tmp_path: Path,
) -> None:
    contract, _root, _launcher, _helper = _make_contract(tmp_path)
    runner = FakeProbeRunner("cancel")
    task = asyncio.create_task(
        run_native_sandbox_behavior_probe(
            contract,
            runner,
            _timing=_FAST_TIMING,
        )
    )
    await asyncio.wait_for(runner.started.wait(), timeout=1.0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert runner.reaped is True
    assert runner.cwd is not None and not runner.cwd.parent.exists()


def _valid_report(target: str | None = None) -> NativeSandboxBehaviorReport:
    selected = target or _current_platform_target()
    family = selected.split("-", 1)[0]
    contract_id, _launcher, capabilities = _CONTRACTS[family]
    capability_results = {
        name: name in _OBSERVED_CAPABILITIES for name in capabilities
    }
    fields = {
        "platform_target": selected,
        "contract_id": contract_id,
        "bundle_tree_sha256": "1" * 64,
        "sandbox_manifest_sha256": "2" * 64,
        "dependency_manifest_sha256": "3" * 64,
        "launcher_sha256": "4" * 64,
        "helper_sha256": "5" * 64,
        "nonce_sha256": "6" * 64,
        "attempts_sha256": "7" * 64,
        "output_proof_sha256": "8" * 64,
    }
    evidence = {
        "domain": "suxiaoyou-office-native-sandbox-behavior-v1",
        **fields,
        "capabilities": capability_results,
        "observations": {
            "descendant_marker_absent": True,
            "host_canary_unchanged": True,
            "input_unchanged": True,
            "loopback_connection_absent": True,
            "output_proof_valid": True,
        },
    }
    return NativeSandboxBehaviorReport(
        schema_version=1,
        status="proven",
        **fields,
        evidence_sha256=_sha256(_canonical(evidence)),
        capabilities=capability_results,
        native_behavior_proven=True,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("schema_version", True),
        ("status", "declared-not-proven"),
        ("platform_target", "../../darwin-arm64"),
        ("contract_id", "fake.contract"),
        ("bundle_tree_sha256", "a" * 63),
        ("sandbox_manifest_sha256", "A" * 64),
        ("dependency_manifest_sha256", 1),
        ("launcher_sha256", "/tmp/launcher"),
        ("helper_sha256", "g" * 64),
        ("nonce_sha256", ""),
        ("attempts_sha256", None),
        ("output_proof_sha256", "0" * 65),
        ("evidence_sha256", "report.json"),
        ("evidence_sha256", "a" * 64),
        ("capabilities", {"network_denied": True}),
        ("capabilities", {"../network_denied": True}),
        ("native_behavior_proven", 1),
    ),
)
def test_report_rejects_fake_proven_or_path_bearing_fields(
    field: str,
    value: object,
) -> None:
    with pytest.raises(NativeSandboxBehaviorProbeError):
        replace(_valid_report(), **{field: value})


def test_report_does_not_claim_platform_mechanisms_as_behavior_proven() -> None:
    valid = _valid_report()
    overclaimed = {name: True for name in valid.capabilities}

    with pytest.raises(NativeSandboxBehaviorProbeError, match="capabilities"):
        replace(valid, capabilities=overclaimed)


def test_timing_rejects_nonfinite_and_insufficient_grace() -> None:
    for kwargs in (
        {"helper_timeout_seconds": float("nan")},
        {"helper_timeout_seconds": float("inf")},
        {"observation_grace_seconds": float("nan")},
        {"observation_grace_seconds": float("inf")},
        {"helper_timeout_seconds": 121},
        {"descendant_delay_ms": 5_001, "observation_grace_seconds": 20},
        {"descendant_delay_ms": 100, "observation_grace_seconds": 0.1},
    ):
        with pytest.raises(NativeSandboxBehaviorProbeError, match="timing"):
            _ProbeTiming(**kwargs)


@pytest.mark.asyncio
async def test_pending_loopback_connection_is_detected_even_if_observer_stalls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, _root, _launcher, _helper = _make_contract(tmp_path)

    def stalled_observer(observer) -> None:
        observer._ready.set()
        observer._stop.wait(timeout=1.0)

    monkeypatch.setattr(behavior_module._LoopbackObserver, "_observe", stalled_observer)
    with pytest.raises(NativeSandboxBehaviorProbeError, match="network connection"):
        await run_native_sandbox_behavior_probe(
            contract,
            FakeProbeRunner("connect"),
            _timing=_FAST_TIMING,
        )


@pytest.mark.asyncio
async def test_falsey_injected_runner_is_used(tmp_path: Path) -> None:
    class FalseyRunner(FakeProbeRunner):
        def __bool__(self) -> bool:
            return False

    contract, _root, _launcher, _helper = _make_contract(tmp_path)
    runner = FalseyRunner()

    report = await run_native_sandbox_behavior_probe(
        contract,
        runner,
        _timing=_FAST_TIMING,
    )

    assert report.native_behavior_proven is True
    assert runner.argv is not None


@pytest.mark.asyncio
async def test_injected_runner_cannot_leak_probe_path_in_exception(
    tmp_path: Path,
) -> None:
    class HostileRunner(FakeProbeRunner):
        async def run(self, argv, *, cwd, env, timeout_seconds):
            self.cwd = cwd
            raise NativeSandboxBehaviorProbeError(str(cwd))

    contract, _root, _launcher, _helper = _make_contract(tmp_path)
    runner = HostileRunner()

    with pytest.raises(
        NativeSandboxBehaviorProbeError,
        match="helper execution failed",
    ) as caught:
        await run_native_sandbox_behavior_probe(
            contract,
            runner,
            _timing=_FAST_TIMING,
        )

    assert runner.cwd is not None
    assert str(runner.cwd) not in str(caught.value)
    assert caught.value.__cause__ is None
    assert not runner.cwd.parent.exists()
