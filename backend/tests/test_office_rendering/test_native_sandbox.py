from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.office_rendering.native_sandbox import (
    NativeSandboxContractError,
    load_native_sandbox_contract,
)


CONTRACTS = {
    "darwin-arm64": (
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
    "windows-x64": (
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
    "linux-arm64": (
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
CONTRACTS.update(
    {
        "darwin-x64": CONTRACTS["darwin-arm64"],
        "windows-arm64": CONTRACTS["windows-x64"],
        "linux-x64": CONTRACTS["linux-arm64"],
    }
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


def _bundle(tmp_path: Path, target: str):
    contract_id, launcher_relative, capabilities = CONTRACTS[target]
    root = (tmp_path / "renderer").absolute()
    root.mkdir(parents=True)
    launcher = root.joinpath(*launcher_relative.split("/"))
    inner_name = "soffice.exe" if target.startswith("windows-") else "soffice"
    inner = root / "bin" / inner_name
    _write_executable(launcher, b"native sandbox launcher")
    _write_executable(inner, b"native renderer")

    sandbox = {
        "schema_version": 1,
        "platform_target": target,
        "contract_id": contract_id,
        "launcher_path": launcher_relative,
        "capabilities": {name: True for name in sorted(capabilities)},
    }
    sandbox_bytes = _canonical(sandbox)
    (root / "sandbox-manifest.json").write_bytes(sandbox_bytes)

    records = [
        {
            "path": path.relative_to(root).as_posix(),
            "kind": "executable",
            "size": len(path.read_bytes()),
            "sha256": _sha256(path.read_bytes()),
            "dependencies": [],
        }
        for path in (launcher, inner)
    ]
    records.sort(key=lambda value: value["path"])
    dependency = {
        "schema_version": 1,
        "platform_target": target,
        "files": records,
    }
    dependency_bytes = _canonical(dependency)
    (root / "dependency-manifest.json").write_bytes(dependency_bytes)
    components = {
        "sandbox-manifest": _sha256(sandbox_bytes),
        "dependency-manifest": _sha256(dependency_bytes),
        "bundle-tree": "f" * 64,
    }
    return root, launcher, inner, sandbox, dependency, components


def _rewrite_manifest(
    root: Path,
    name: str,
    value: object,
    components: dict[str, str],
    component: str,
) -> None:
    content = _canonical(value)
    (root / name).write_bytes(content)
    components[component] = _sha256(content)


@pytest.mark.parametrize("target", tuple(CONTRACTS))
def test_exact_target_contract_builds_no_shell_argv_and_unproven_evidence(
    tmp_path: Path,
    target: str,
) -> None:
    root, launcher, inner, _sandbox, _dependency, components = _bundle(
        tmp_path,
        target,
    )
    work = (tmp_path / "work").absolute()
    work.mkdir(mode=0o700)

    contract = load_native_sandbox_contract(
        root,
        platform_target=target,
        attested_components=components,
    )
    argv = contract.build_no_shell_argv(
        (str(inner), "--headless", "value with spaces"),
        work_dir=work,
    )
    evidence = contract.path_free_evidence()

    assert argv == (
        str(launcher),
        "--contract-id",
        CONTRACTS[target][0],
        "--sandbox-manifest-sha256",
        components["sandbox-manifest"],
        "--work-dir",
        str(work),
        "--",
        str(inner),
        "--headless",
        "value with spaces",
    )
    assert evidence["status"] == "declared-not-proven"
    assert evidence["native_behavior_proven"] is False
    assert evidence["adversarial_evidence_required"] is True
    assert set(evidence["capabilities"]) == CONTRACTS[target][2]
    assert str(root) not in json.dumps(evidence)


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        (lambda value: value.update(schema_version=2), "schema"),
        (lambda value: value.update(schema_version=True), "schema"),
        (lambda value: value.update(platform_target="linux-x64"), "target"),
        (lambda value: value.update(contract_id="unreviewed"), "contract id"),
        (lambda value: value.update(launcher_path="bin/../escape"), "launcher path"),
        (
            lambda value: value["capabilities"].pop("xpc_service"),
            "capabilities",
        ),
        (
            lambda value: value["capabilities"].update(network_denied=False),
            "capabilities",
        ),
        (
            lambda value: value["capabilities"].update(unreviewed=True),
            "capabilities",
        ),
    ],
)
def test_manifest_schema_target_path_and_capabilities_are_exact(
    tmp_path: Path,
    mutation,
    error: str,
) -> None:
    root, _launcher, _inner, sandbox, _dependency, components = _bundle(
        tmp_path,
        "darwin-arm64",
    )
    mutation(sandbox)
    _rewrite_manifest(
        root,
        "sandbox-manifest.json",
        sandbox,
        components,
        "sandbox-manifest",
    )

    with pytest.raises(NativeSandboxContractError, match=error):
        load_native_sandbox_contract(
            root,
            platform_target="darwin-arm64",
            attested_components=components,
        )


def test_noncanonical_or_unattested_manifest_fails_closed(tmp_path: Path) -> None:
    root, _launcher, _inner, sandbox, _dependency, components = _bundle(
        tmp_path,
        "linux-arm64",
    )
    pretty = json.dumps(sandbox, indent=2).encode("utf-8")
    (root / "sandbox-manifest.json").write_bytes(pretty)
    components["sandbox-manifest"] = _sha256(pretty)
    with pytest.raises(NativeSandboxContractError, match="not canonical"):
        load_native_sandbox_contract(
            root,
            platform_target="linux-arm64",
            attested_components=components,
        )

    root, _launcher, _inner, _sandbox, _dependency, components = _bundle(
        tmp_path / "next",
        "linux-arm64",
    )
    components["sandbox-manifest"] = "0" * 64
    with pytest.raises(NativeSandboxContractError, match="attestation"):
        load_native_sandbox_contract(
            root,
            platform_target="linux-arm64",
            attested_components=components,
        )


def test_launcher_must_be_exact_native_closure_entry(tmp_path: Path) -> None:
    root, launcher, _inner, _sandbox, dependency, components = _bundle(
        tmp_path,
        "linux-arm64",
    )
    dependency["files"] = [
        value
        for value in dependency["files"]
        if value["path"] != launcher.relative_to(root).as_posix()
    ]
    _rewrite_manifest(
        root,
        "dependency-manifest.json",
        dependency,
        components,
        "dependency-manifest",
    )
    with pytest.raises(NativeSandboxContractError, match="absent"):
        load_native_sandbox_contract(
            root,
            platform_target="linux-arm64",
            attested_components=components,
        )

    root, launcher, _inner, _sandbox, _dependency, components = _bundle(
        tmp_path / "drift",
        "linux-arm64",
    )
    launcher.write_bytes(b"changed launcher")
    launcher.chmod(0o700)
    with pytest.raises(NativeSandboxContractError, match="identity changed"):
        load_native_sandbox_contract(
            root,
            platform_target="linux-arm64",
            attested_components=components,
        )


@pytest.mark.parametrize("mode", (0o600, 0o766))
def test_launcher_mode_is_private_and_executable(tmp_path: Path, mode: int) -> None:
    root, launcher, _inner, _sandbox, _dependency, components = _bundle(
        tmp_path,
        "linux-arm64",
    )
    launcher.chmod(mode)

    with pytest.raises(NativeSandboxContractError, match="executable|permissions"):
        load_native_sandbox_contract(
            root,
            platform_target="linux-arm64",
            attested_components=components,
        )


def test_launcher_and_work_directory_symlinks_are_rejected(tmp_path: Path) -> None:
    root, launcher, inner, _sandbox, _dependency, components = _bundle(
        tmp_path,
        "linux-arm64",
    )
    outside = tmp_path / "outside-launcher"
    _write_executable(outside, launcher.read_bytes())
    launcher.unlink()
    try:
        launcher.symlink_to(outside)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    with pytest.raises(NativeSandboxContractError, match="redirected"):
        load_native_sandbox_contract(
            root,
            platform_target="linux-arm64",
            attested_components=components,
        )

    root, _launcher, inner, _sandbox, _dependency, components = _bundle(
        tmp_path / "work-link",
        "linux-arm64",
    )
    contract = load_native_sandbox_contract(
        root,
        platform_target="linux-arm64",
        attested_components=components,
    )
    real_work = (tmp_path / "real-work").absolute()
    real_work.mkdir(mode=0o700)
    linked_work = (tmp_path / "linked-work").absolute()
    linked_work.symlink_to(real_work, target_is_directory=True)
    with pytest.raises(NativeSandboxContractError, match="redirected"):
        contract.build_no_shell_argv((str(inner),), work_dir=linked_work)
    public_work = (tmp_path / "public-work").absolute()
    public_work.mkdir(mode=0o755)
    with pytest.raises(NativeSandboxContractError, match="not private"):
        contract.build_no_shell_argv((str(inner),), work_dir=public_work)


def test_manifest_symlink_is_rejected_even_when_bytes_and_digest_match(
    tmp_path: Path,
) -> None:
    root, _launcher, _inner, _sandbox, _dependency, components = _bundle(
        tmp_path,
        "darwin-x64",
    )
    manifest = root / "sandbox-manifest.json"
    outside = tmp_path / "outside-sandbox-manifest.json"
    outside.write_bytes(manifest.read_bytes())
    manifest.unlink()
    try:
        manifest.symlink_to(outside)
    except OSError:
        pytest.skip("symbolic links are unavailable")

    with pytest.raises(NativeSandboxContractError, match="redirected"):
        load_native_sandbox_contract(
            root,
            platform_target="darwin-x64",
            attested_components=components,
        )


def test_argv_rejects_nonclosure_executable_and_invalid_data(tmp_path: Path) -> None:
    root, _launcher, inner, _sandbox, _dependency, components = _bundle(
        tmp_path,
        "windows-x64",
    )
    contract = load_native_sandbox_contract(
        root,
        platform_target="windows-x64",
        attested_components=components,
    )
    work = (tmp_path / "work").absolute()
    work.mkdir(mode=0o700)
    outside = (tmp_path / "outside.exe").absolute()
    _write_executable(outside, b"outside")

    with pytest.raises(NativeSandboxContractError, match="outside"):
        contract.build_no_shell_argv((str(outside),), work_dir=work)
    with pytest.raises(NativeSandboxContractError, match="invalid"):
        contract.build_no_shell_argv((str(inner), "bad\x00arg"), work_dir=work)
    with pytest.raises(NativeSandboxContractError, match="path is invalid"):
        contract.build_no_shell_argv((str(inner),), work_dir=Path("relative"))
    inner.write_bytes(b"replaced after contract load")
    inner.chmod(0o700)
    with pytest.raises(NativeSandboxContractError, match="identity changed"):
        contract.build_no_shell_argv((str(inner),), work_dir=work)
