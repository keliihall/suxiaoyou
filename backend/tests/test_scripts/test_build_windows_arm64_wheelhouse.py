from __future__ import annotations

import hashlib
import io
import json
import os
import struct
import subprocess
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import build_windows_arm64_wheelhouse as wheelhouse


OFFICIAL_ARM64_WHEEL_HASHES = {
    "greenlet": (
        "3.3.1",
        "bfb2d1763d777de5ee495c85309460f6fd8146e50ec9d0ae0183dbf6f0a829d1",
    ),
    "markupsafe": (
        "3.0.3",
        "35add3b638a5d900e807944a078b51922212fb3dedb01633a8defc4b01a3c85f",
    ),
    "numpy": (
        "2.3.0",
        "bd8df082b6c4695753ad6193018c05aac465d634834dca47a3ae06d4bb22d9ea",
    ),
    "pandas": (
        "3.0.0",
        "da768007b5a33057f6d9053563d6b74dd6d029c337d93c6d0d22a763a5c2ecc0",
    ),
    "pyyaml": (
        "6.0.3",
        "64386e5e707d03a7e172c0701abfb7e10f0fb753ee1d773128192742712a98fd",
    ),
}


def _fake_pe(machine: int) -> bytes:
    payload = bytearray(256)
    payload[:2] = b"MZ"
    struct.pack_into("<I", payload, 0x3C, 0x80)
    payload[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", payload, 0x84, machine)
    return bytes(payload)


def _wheel(
    directory: Path,
    filename: str,
    *,
    native_member: str | None = None,
    machine: int = wheelhouse.PE_MACHINE_ARM64,
) -> Path:
    path = directory / filename
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("demo/__init__.py", "")
        if native_member is not None:
            archive.writestr(native_member, _fake_pe(machine))
    return path


def test_checked_in_windows_arm64_locks_are_exact_and_complete() -> None:
    production = wheelhouse.parse_requirement_lock(wheelhouse.PRODUCTION_LOCK)
    build = wheelhouse.parse_requirement_lock(wheelhouse.BUILD_LOCK)
    release = wheelhouse.parse_requirement_lock(wheelhouse.RELEASE_TOOLS_LOCK)

    assert len(production) == 100
    assert len(build) == 9
    assert len(release) == 40
    for name, (version, official_wheel_hash) in OFFICIAL_ARM64_WHEEL_HASHES.items():
        assert production[name].version == version
        assert official_wheel_hash in production[name].hashes

    assert production["cryptography"].version == "48.0.1"
    assert (
        "266f4ee051abb2f725b74ef8072b521ce1feacf685a3364fa6a6b45548db791a"
        in production["cryptography"].hashes
    )
    assert production["tiktoken"].version == "0.8.0"
    assert (
        "9ccbb2740f24542534369c5635cfd9b2b3c2490754a78ac8831d99f89f94eeb2"
        in production["tiktoken"].hashes
    )
    assert build["pip"].version == "26.1.2"
    assert release["pyinstaller"].version == "6.21.0"
    assert release["pip-audit"].version == "2.10.1"

    assert "--only-binary=:all:" in wheelhouse.BUILD_LOCK.read_text()
    assert "--only-binary=:all:" in wheelhouse.RELEASE_TOOLS_LOCK.read_text()
    for lock in (build, release):
        for digest, contract in wheelhouse.PURE_WHEEL_LAUNCHER_CONTRACTS.items():
            distribution = str(contract["distribution"])
            assert lock[distribution].version == contract["version"]
            assert digest in lock[distribution].hashes


def test_source_contract_locks_openssl_and_both_native_sdists() -> None:
    source_lock = wheelhouse.load_sources_lock()
    assert source_lock["openssl"] == {
        "build_flags": [
            "no-zlib",
            "no-shared",
            "no-module",
            "no-comp",
            "no-apps",
            "no-docs",
            "no-sm2-precomp",
            "no-atexit",
        ],
        "configure_target": "VC-WIN64-ARM",
        "filename": "openssl-4.0.1.tar.gz",
        "license": "Apache-2.0",
        "pyca_infra_reference_commit": (
            "bcb2ad33b83662257c619d8806a856533296a8d4"
        ),
        "sha256": (
            "2db3f3a0d6ea4b59e1f094ace2c8cd536dffb87cdc39084c5afa1e6f7f37dd09"
        ),
        "url": (
            "https://github.com/openssl/openssl/releases/download/"
            "openssl-4.0.1/openssl-4.0.1.tar.gz"
        ),
        "version": "4.0.1",
    }
    assert {package["name"] for package in source_lock["packages"]} == {
        "cryptography",
        "tiktoken",
    }
    assert (
        hashlib.sha256(wheelhouse.TIKTOKEN_CARGO_LOCK.read_bytes()).hexdigest()
        == "0283ef6771d432d962b0ee9483c4259ac5140b4bee77cee97b700192ab52a9e3"
    )


def test_binary_download_lock_excludes_only_source_built_packages(
    tmp_path: Path,
) -> None:
    filtered = wheelhouse.filtered_requirement_lock(
        wheelhouse.PRODUCTION_LOCK,
        excluded_names=wheelhouse.NATIVE_SOURCE_NAMES,
    )
    path = tmp_path / "binary.txt"
    path.write_text(filtered, encoding="utf-8")
    pins = wheelhouse.parse_requirement_lock(path)
    assert len(pins) == 98
    assert not wheelhouse.NATIVE_SOURCE_NAMES & pins.keys()


def test_arm64_wheel_and_pe_are_accepted(tmp_path: Path) -> None:
    path = _wheel(
        tmp_path,
        "demo-1.0-cp312-cp312-win_arm64.whl",
        native_member="demo/native.pyd",
    )
    evidence = wheelhouse.inspect_wheel(path)
    assert evidence.normalized_name == "demo"
    assert evidence.platform_tags == ("win_arm64",)
    assert evidence.native_members[0]["pe_machine"] == "0xaa64"


def test_x64_pe_in_arm64_tag_is_rejected(tmp_path: Path) -> None:
    path = _wheel(
        tmp_path,
        "demo-1.0-cp312-cp312-win_arm64.whl",
        native_member="demo/native.pyd",
        machine=0x8664,
    )
    with pytest.raises(wheelhouse.SupplyChainError, match="expected PE machine"):
        wheelhouse.inspect_wheel(path)


def test_non_arm64_platform_tag_is_rejected(tmp_path: Path) -> None:
    path = _wheel(
        tmp_path,
        "demo-1.0-cp312-cp312-win_amd64.whl",
        native_member="demo/native.pyd",
        machine=0x8664,
    )
    with pytest.raises(wheelhouse.SupplyChainError, match="no CPython"):
        wheelhouse.inspect_wheel(path)


def test_pure_wheel_cannot_hide_native_binary(tmp_path: Path) -> None:
    path = _wheel(
        tmp_path,
        "demo-1.0-py3-none-any.whl",
        native_member="demo/native.pyd",
    )
    with pytest.raises(wheelhouse.SupplyChainError, match="pure wheel"):
        wheelhouse.inspect_wheel(path)


def test_exact_hash_locked_pure_wheel_launcher_contract_is_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _wheel(
        tmp_path,
        "demo-1.0-py3-none-any.whl",
        native_member="demo/launcher-x64.exe",
        machine=0x8664,
    )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setitem(
        wheelhouse.PURE_WHEEL_LAUNCHER_CONTRACTS,
        digest,
        {
            "distribution": "demo",
            "members": {"demo/launcher-x64.exe": 0x8664},
            "version": "1.0",
        },
    )

    evidence = wheelhouse.inspect_wheel(path)

    assert evidence.native_members == (
        {
            "member": "demo/launcher-x64.exe",
            "pe_machine": "0x8664",
            "role": "multi-architecture-launcher-template",
            "sha256": hashlib.sha256(_fake_pe(0x8664)).hexdigest(),
            "size": len(_fake_pe(0x8664)),
        },
    )


def test_pure_wheel_launcher_contract_requires_every_reviewed_member(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _wheel(
        tmp_path,
        "demo-1.0-py3-none-any.whl",
        native_member="demo/launcher-arm64.exe",
    )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setitem(
        wheelhouse.PURE_WHEEL_LAUNCHER_CONTRACTS,
        digest,
        {
            "distribution": "demo",
            "members": {
                "demo/launcher-arm64.exe": wheelhouse.PE_MACHINE_ARM64,
                "demo/missing-launcher.exe": 0x8664,
            },
            "version": "1.0",
        },
    )

    with pytest.raises(wheelhouse.SupplyChainError, match="incomplete"):
        wheelhouse.inspect_wheel(path)


def test_install_lock_accepts_exactly_the_materialized_wheel(
    tmp_path: Path,
) -> None:
    path = _wheel(tmp_path, "demo-1.0-py3-none-any.whl")
    evidence = wheelhouse.inspect_wheel(path)
    pin = wheelhouse.RequirementPin(
        name="demo",
        normalized_name="demo",
        version="1.0",
        hashes=("f" * 64,),
    )
    lock = tmp_path / "install.txt"
    wheelhouse.write_install_lock(lock, {"demo": pin}, {"demo": evidence})
    parsed = wheelhouse.parse_requirement_lock(lock)
    assert parsed["demo"].hashes == (hashlib.sha256(path.read_bytes()).hexdigest(),)
    assert "f" * 64 not in lock.read_text()


def test_sdist_traversal_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        member = tarfile.TarInfo("../escape")
        payload = b"bad"
        member.size = len(payload)
        tar.addfile(member, io.BytesIO(payload))
    with pytest.raises(wheelhouse.SupplyChainError, match="unsafe tar member"):
        wheelhouse.safe_extract_sdist(archive, tmp_path / "extract")
    assert not (tmp_path / "escape").exists()


def test_ambient_package_manager_and_openssl_inputs_are_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hostile = {
        "PIP_INDEX_URL": "https://attacker.invalid",
        "OPENSSL_DIR": "C:/attacker",
        "OPENSSL_STATIC": "0",
        "VCPKG_ROOT": "C:/attacker-vcpkg",
        "DEP_OPENSSL_VERSION_NUMBER": "bad",
        "CARGO_BUILD_JOBS": "99",
        "CARGO_ENCODED_RUSTFLAGS": "malicious",
        "CARGO_BUILD_RUSTFLAGS": "malicious",
        "CARGO_PROFILE_RELEASE_CODEGEN_UNITS": "99",
        "CARGO_TARGET_AARCH64_PC_WINDOWS_MSVC_RUSTFLAGS": "malicious",
        "RUSTFLAGS": "malicious",
        "RUSTDOCFLAGS": "malicious",
        "RUSTC_WRAPPER": "C:/attacker/wrapper.exe",
        "RUSTC_WORKSPACE_WRAPPER": "C:/attacker/workspace-wrapper.exe",
        "CL": "/DATTACKER",
        "_CL_": "/DATTACKER",
        "LINK": "/INCLUDE:attacker",
        "_LINK_": "/INCLUDE:attacker",
        "ARFLAGS": "attacker",
    }
    for key, value in hostile.items():
        monkeypatch.setenv(key, value)
    env = wheelhouse.locked_network_environment()
    for key in hostile:
        assert key not in env


def test_locked_build_environment_activates_and_verifies_arm64_maturin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    venv_dir = tmp_path / "build-venv"
    scripts = venv_dir / "Scripts"
    scripts.mkdir(parents=True)
    python = scripts / "python.exe"
    python.write_bytes(b"python")
    maturin = scripts / "maturin.exe"
    maturin.write_bytes(_fake_pe(wheelhouse.PE_MACHINE_ARM64))
    install_lock = tmp_path / "build-install.txt"
    install_lock.write_text(
        "maturin==1.14.1 \\\n"
        f"    --hash=sha256:{'a' * 64}\n",
        encoding="utf-8",
    )
    build_wheelhouse = tmp_path / "wheels"
    build_wheelhouse.mkdir()

    monkeypatch.setattr(
        wheelhouse.venv.EnvBuilder,
        "create",
        lambda _self, _path: None,
    )
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(command, **kwargs):
        calls.append(
            (
                [str(part) for part in command],
                dict(kwargs.get("env", {})),
            )
        )
        return SimpleNamespace(
            stdout="maturin 1.14.1\n"
            if str(command[0]) == str(maturin)
            else "",
            stderr="",
        )

    monkeypatch.setattr(wheelhouse, "run_checked", fake_run)
    monkeypatch.setattr(
        wheelhouse,
        "locked_network_environment",
        lambda: {"Path": "C:\\locked-tools", "SAFE": "1"},
    )

    assert (
        wheelhouse.create_locked_build_environment(
            venv_dir, build_wheelhouse, install_lock
        )
        == python
    )
    assert calls[0][1]["PATH"].split(os.pathsep)[0] == str(scripts)
    assert "Path" not in calls[0][1]
    assert calls[1][0] == [str(maturin), "--version"]
    assert calls[1][1]["PATH"].split(os.pathsep)[0] == str(scripts)


def test_locked_build_environment_rejects_non_arm64_maturin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    venv_dir = tmp_path / "build-venv"
    scripts = venv_dir / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").write_bytes(b"python")
    (scripts / "maturin.exe").write_bytes(_fake_pe(0x8664))
    install_lock = tmp_path / "build-install.txt"
    install_lock.write_text("", encoding="utf-8")

    monkeypatch.setattr(
        wheelhouse.venv.EnvBuilder,
        "create",
        lambda _self, _path: None,
    )
    monkeypatch.setattr(
        wheelhouse,
        "run_checked",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="", stderr=""),
    )

    with pytest.raises(wheelhouse.SupplyChainError, match="expected PE machine"):
        wheelhouse.create_locked_build_environment(
            venv_dir, tmp_path / "wheels", install_lock
        )


def test_native_wheel_build_exposes_locked_venv_entry_points(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripts = tmp_path / "build-venv" / "Scripts"
    scripts.mkdir(parents=True)
    python = scripts / "python.exe"
    python.write_bytes(b"python")
    source_root = tmp_path / "demo-source"
    source_root.mkdir()
    output = tmp_path / "native-wheels"
    output.mkdir()
    cargo_home = tmp_path / "cargo-home"
    cargo_home.mkdir()
    captured: dict[str, str] = {}

    def fake_run(_command, **kwargs):
        captured.update(kwargs["env"])
        _wheel(output, "demo-1.0-py3-none-any.whl")
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(wheelhouse, "run_checked", fake_run)
    monkeypatch.setattr(
        wheelhouse,
        "locked_network_environment",
        lambda: {"Path": "C:\\locked-tools"},
    )

    built = wheelhouse.build_native_wheel(
        python,
        source_root,
        output,
        cargo_home=cargo_home,
        source_date_epoch=1,
        extra_environment={
            "CARGO_ENCODED_RUSTFLAGS": "malicious",
            "CARGO_INCREMENTAL": "1",
        },
    )

    assert built.normalized_name == "demo"
    assert captured["PATH"].split(os.pathsep)[0] == str(scripts)
    assert "Path" not in captured
    rust_flags = captured["CARGO_ENCODED_RUSTFLAGS"].split("\x1f")
    assert rust_flags == [
        (
            f"--remap-path-prefix={tmp_path.resolve()}="
            f"{wheelhouse.REPRODUCIBLE_BUILD_ROOT}"
        ),
        "-C",
        "codegen-units=1",
        "-C",
        "link-arg=/Brepro",
        "-C",
        "link-arg=/PDBALTPATH:%_PDB%",
    ]
    assert captured["CL"] == wheelhouse.reproducible_msvc_cl_flags(tmp_path)
    assert captured["LINK"] == "/Brepro /PDBALTPATH:%_PDB%"
    assert captured["CARGO_INCREMENTAL"] == "0"
    assert captured["SOURCE_DATE_EPOCH"] == "1"


def test_native_output_rejects_real_build_root_in_utf8_or_utf16(
    tmp_path: Path,
) -> None:
    for payload in (
        f"prefix:{tmp_path.resolve()}".encode(),
        f"prefix:{tmp_path.resolve()}".encode("utf-16-le"),
    ):
        with pytest.raises(
            wheelhouse.SupplyChainError, match="embeds the private build root"
        ):
            wheelhouse.reject_build_path_leak(
                payload,
                label="native.pyd",
                build_root=tmp_path,
            )


def test_msvc_environment_is_initialized_for_native_arm64(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vswhere = tmp_path / "vswhere.exe"
    vswhere.write_bytes(b"")
    installation = tmp_path / "Visual Studio"
    vsdevcmd = installation / "Common7" / "Tools" / "VsDevCmd.bat"
    vsdevcmd.parent.mkdir(parents=True)
    vsdevcmd.write_text("@echo off", encoding="utf-8")
    cmd = tmp_path / "cmd.exe"
    cmd.write_bytes(b"")

    def fake_which(name: str):
        return {
            "vswhere.exe": str(vswhere),
            "cmd.exe": str(cmd),
        }.get(name)

    calls: list[list[str]] = []
    wrappers: list[str] = []

    def fake_run(command, **kwargs):
        rendered = [str(part) for part in command]
        calls.append(rendered)
        if rendered[0] == str(vswhere):
            return SimpleNamespace(stdout=f"{installation}\n", stderr="")
        wrappers.append(
            (Path(kwargs["cwd"]) / rendered[-1]).read_text(encoding="utf-8")
        )
        return SimpleNamespace(
            stdout=(
                "Path=C:\\VS\\ARM64\n"
                "vscmd_arg_host_arch=arm64\n"
                "Vscmd_Arg_Tgt_Arch=arm64\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(wheelhouse.shutil, "which", fake_which)
    monkeypatch.setattr(wheelhouse, "run_checked", fake_run)
    monkeypatch.setenv("PATH", os.environ["PATH"])
    monkeypatch.delenv("VSCMD_ARG_HOST_ARCH", raising=False)
    monkeypatch.delenv("VSCMD_ARG_TGT_ARCH", raising=False)
    wheelhouse.initialize_native_arm64_msvc_environment()
    assert os.environ["VSCMD_ARG_HOST_ARCH"] == "arm64"
    assert os.environ["VSCMD_ARG_TGT_ARCH"] == "arm64"
    assert calls[1][1:3] == ["/d", "/c"]
    assert calls[1][-1] == "capture-arm64-environment.cmd"
    assert (
        f'call "{vsdevcmd}" -no_logo -arch=arm64 -host_arch=arm64'
        in wrappers[0]
    )
    assert "if errorlevel 1 exit /b %errorlevel%" in wrappers[0]


@pytest.mark.parametrize(
    ("host_arch", "target_arch"),
    [("x64", "arm64"), ("arm64", "x64")],
)
def test_msvc_environment_rejects_wrong_host_or_target_architecture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_arch: str,
    target_arch: str,
) -> None:
    vswhere = tmp_path / "vswhere.exe"
    vswhere.write_bytes(b"")
    installation = tmp_path / "Visual Studio"
    vsdevcmd = installation / "Common7" / "Tools" / "VsDevCmd.bat"
    vsdevcmd.parent.mkdir(parents=True)
    vsdevcmd.write_text("@echo off", encoding="utf-8")
    cmd = tmp_path / "cmd.exe"
    cmd.write_bytes(b"")

    def fake_which(name: str):
        return {
            "vswhere.exe": str(vswhere),
            "cmd.exe": str(cmd),
        }.get(name)

    def fake_run(command, **_kwargs):
        if str(command[0]) == str(vswhere):
            return SimpleNamespace(stdout=f"{installation}\n", stderr="")
        return SimpleNamespace(
            stdout=(
                "PATH=C:\\VS\\ARM64\n"
                f"VSCMD_ARG_HOST_ARCH={host_arch}\n"
                f"VSCMD_ARG_TGT_ARCH={target_arch}\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(wheelhouse.shutil, "which", fake_which)
    monkeypatch.setattr(wheelhouse, "run_checked", fake_run)

    with pytest.raises(wheelhouse.SupplyChainError, match="wrong compiler"):
        wheelhouse.initialize_native_arm64_msvc_environment()


def test_native_builder_accepts_nmake_banner_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_lock = wheelhouse.load_sources_lock()
    toolchain = source_lock["toolchain"]

    monkeypatch.setattr(
        wheelhouse, "EXPECTED_PYTHON", wheelhouse.sys.version_info[:3]
    )
    monkeypatch.setattr(wheelhouse.platform, "system", lambda: "Windows")
    monkeypatch.setattr(wheelhouse.platform, "machine", lambda: "ARM64")
    monkeypatch.setattr(
        wheelhouse, "initialize_native_arm64_msvc_environment", lambda: None
    )
    monkeypatch.setattr(wheelhouse, "ensure_bootstrap_pip", lambda: None)

    def fake_which(name: str):
        return {
            "cl.exe": "cl.exe",
            "perl.exe": "perl.exe",
            "nmake.exe": "nmake.exe",
        }.get(name)

    def fake_run(command, **_kwargs):
        rendered = [str(part) for part in command]
        if rendered == ["rustc", "--version"]:
            return SimpleNamespace(
                stdout=f"{toolchain['rustc_version']}\n", stderr=""
            )
        if rendered == ["cargo", "--version"]:
            return SimpleNamespace(
                stdout=f"{toolchain['cargo_version']}\n", stderr=""
            )
        if rendered == ["rustc", "-Vv"]:
            return SimpleNamespace(
                stdout=f"host: {wheelhouse.EXPECTED_RUST_HOST}\n", stderr=""
            )
        if rendered == ["cl.exe"]:
            return SimpleNamespace(
                stdout="",
                stderr=(
                    "Microsoft (R) C/C++ Optimizing Compiler "
                    "Version 19.44.35207.1 for ARM64\n"
                ),
            )
        if rendered == ["perl.exe", "-v"]:
            return SimpleNamespace(
                stdout="This is perl 5, version 40, subversion 2\n", stderr=""
            )
        if rendered == ["nmake.exe", "/?"]:
            return SimpleNamespace(
                stdout="",
                stderr=(
                    "Microsoft (R) Program Maintenance Utility "
                    "Version 14.44.35207.1\n"
                ),
            )
        raise AssertionError(f"unexpected command: {rendered!r}")

    monkeypatch.setattr(wheelhouse.shutil, "which", fake_which)
    monkeypatch.setattr(wheelhouse, "run_checked", fake_run)

    evidence = wheelhouse.preflight_native_builder(source_lock)

    assert evidence["msvc_arch"] == "arm64"
    assert (
        evidence["nmake"]
        == "Microsoft (R) Program Maintenance Utility Version 14.44.35207.1"
    )


def test_openssl_build_contract_enforces_reproducibility_and_tests() -> None:
    source = Path(wheelhouse.__file__).read_text(encoding="utf-8")
    openssl_lock = wheelhouse.load_sources_lock()["openssl"]
    configure = wheelhouse.locked_openssl_configure_command(
        "perl.exe", openssl_lock
    )

    assert configure == [
        "perl.exe",
        "Configure",
        *openssl_lock["build_flags"],
        "VC-WIN64-ARM",
    ]
    assert not any(
        argument.startswith(("--prefix=", "--openssldir="))
        for argument in configure
    )
    assert '"SOURCE_DATE_EPOCH": str(source_date_epoch)' in source
    assert '"ARFLAGS": "/nologo /Brepro"' in source
    assert (
        wheelhouse.reproducible_msvc_cl_flags(Path("C:/private")).startswith(
            "/experimental:deterministic /FS /Brepro "
        )
    )
    assert "/pathmap:" in wheelhouse.reproducible_msvc_cl_flags(
        Path("C:/private")
    )
    rust_flags = wheelhouse.reproducible_rust_flags(
        Path("C:/private")
    ).split("\x1f")
    assert rust_flags.count("codegen-units=1") == 1
    assert "link-arg=/Brepro" in rust_flags
    assert '"LINK": "/Brepro /PDBALTPATH:%_PDB%"' in source
    assert 'run_checked([nmake, "/E", "/NOLOGO", "test"]' in source
    assert "OpenSSL_version(0)" in source
    assert "AESGCM" in source
    assert "CertificateBuilder" in source


def test_approval_lock_matches_reproduced_builder_content(
    tmp_path: Path,
) -> None:
    assert wheelhouse.approved_content_sha256(wheelhouse.APPROVAL_LOCK) == (
        "d56c0744b2ed3dda64b27519e11916e42bd5bed2d91f487da4c16bb4b3bb9d4f"
    )
    digest = "a" * 64
    approved = tmp_path / "approval.json"
    approved.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "contract_id": (
                    "suxiaoyou-cpython-3.12.10-windows-arm64-v1"
                ),
                "content_sha256": digest,
                "status": "approved",
            }
        ),
        encoding="utf-8",
    )
    assert wheelhouse.approved_content_sha256(approved) == digest


def test_build_artifact_uses_fixed_private_paths_and_promotes_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "wheelhouse"
    expected_manifest = "a" * 64
    expected_content = "b" * 64
    observed: dict[str, Path] = {}

    monkeypatch.setattr(wheelhouse, "load_sources_lock", lambda: {"locked": True})
    monkeypatch.setattr(
        wheelhouse,
        "preflight_native_builder",
        lambda _source_lock: {"toolchain": "locked"},
    )

    def fake_build_into(staging, work, source_lock, toolchain):
        observed["staging"] = staging
        observed["work"] = work
        assert source_lock == {"locked": True}
        assert toolchain == {"toolchain": "locked"}
        assert staging.is_dir() and not any(staging.iterdir())
        assert work.is_dir() and not any(work.iterdir())
        (staging / wheelhouse.MANIFEST_NAME).write_text("manifest")
        (staging / wheelhouse.MANIFEST_DIGEST_NAME).write_text("digest")
        (staging / "payload.whl").write_bytes(b"payload")
        (work / "private-build-file").write_bytes(b"private")
        return expected_manifest, expected_content

    monkeypatch.setattr(wheelhouse, "build_into", fake_build_into)
    assert wheelhouse.build_artifact(
        output,
        expected_content_sha256=None,
        expected_manifest_sha256=None,
        approval_file=wheelhouse.APPROVAL_LOCK,
        bootstrap=True,
    ) == (expected_manifest, expected_content)

    work, staging, diagnostics, diagnostics_staging = (
        wheelhouse.artifact_auxiliary_paths(output)
    )
    assert observed == {"staging": staging, "work": work}
    assert (output / "payload.whl").read_bytes() == b"payload"
    assert not work.exists()
    assert not staging.exists()
    assert not diagnostics.exists()
    assert not diagnostics_staging.exists()


def test_digest_mismatch_preserves_only_manifest_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "wheelhouse"
    actual_manifest = "c" * 64
    actual_content = "d" * 64
    monkeypatch.setattr(wheelhouse, "load_sources_lock", lambda: {})
    monkeypatch.setattr(
        wheelhouse, "preflight_native_builder", lambda _source_lock: {}
    )

    def fake_build_into(staging, work, _source_lock, _toolchain):
        (staging / wheelhouse.MANIFEST_NAME).write_text("rejected-manifest")
        (staging / wheelhouse.MANIFEST_DIGEST_NAME).write_text(
            "rejected-sidecar"
        )
        (staging / "unapproved.whl").write_bytes(b"must-not-escape")
        (work / "private").write_bytes(b"must-be-removed")
        return actual_manifest, actual_content

    monkeypatch.setattr(wheelhouse, "build_into", fake_build_into)
    with pytest.raises(
        wheelhouse.SupplyChainError, match="manifest evidence"
    ):
        wheelhouse.build_artifact(
            output,
            expected_content_sha256="e" * 64,
            expected_manifest_sha256=None,
            approval_file=wheelhouse.APPROVAL_LOCK,
            bootstrap=False,
        )

    work, staging, diagnostics, diagnostics_staging = (
        wheelhouse.artifact_auxiliary_paths(output)
    )
    assert not output.exists()
    assert not work.exists()
    assert not staging.exists()
    assert not diagnostics_staging.exists()
    assert sorted(path.name for path in diagnostics.iterdir()) == [
        wheelhouse.MANIFEST_NAME,
        wheelhouse.MANIFEST_DIGEST_NAME,
    ]
    assert (diagnostics / wheelhouse.MANIFEST_NAME).read_text() == (
        "rejected-manifest"
    )


def test_preexisting_or_linked_private_build_path_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for linked in (False, True):
        case = tmp_path / ("linked" if linked else "existing")
        case.mkdir()
        output = case / "wheelhouse"
        work, _staging, _diagnostics, _diagnostics_staging = (
            wheelhouse.artifact_auxiliary_paths(output)
        )
        if linked:
            target = case / "owned-by-someone-else"
            target.mkdir()
            work.symlink_to(target, target_is_directory=True)
        else:
            work.mkdir()
            (work / "owner-data").write_text("keep")

        monkeypatch.setattr(
            wheelhouse,
            "preflight_native_builder",
            lambda _source_lock: pytest.fail("preflight must not run"),
        )
        with pytest.raises(
            wheelhouse.SupplyChainError,
            match="refusing stale or concurrent state",
        ):
            wheelhouse.build_artifact(
                output,
                expected_content_sha256=None,
                expected_manifest_sha256=None,
                approval_file=wheelhouse.APPROVAL_LOCK,
                bootstrap=True,
            )
        assert work.exists() or work.is_symlink()
        if linked:
            assert (target).is_dir()
        else:
            assert (work / "owner-data").read_text() == "keep"


def test_cleanup_failure_does_not_mask_primary_build_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "wheelhouse"
    monkeypatch.setattr(wheelhouse, "load_sources_lock", lambda: {})
    monkeypatch.setattr(
        wheelhouse, "preflight_native_builder", lambda _source_lock: {}
    )

    def fail_build(_staging, _work, _source_lock, _toolchain):
        raise wheelhouse.SupplyChainError("primary build failure")

    monkeypatch.setattr(wheelhouse, "build_into", fail_build)
    monkeypatch.setattr(
        wheelhouse,
        "_remove_owned_directory",
        lambda _path, *, role: (_ for _ in ()).throw(
            wheelhouse.SupplyChainError(f"{role} cleanup failure")
        ),
    )

    with pytest.raises(
        wheelhouse.SupplyChainError, match="primary build failure"
    ) as raised:
        wheelhouse.build_artifact(
            output,
            expected_content_sha256=None,
            expected_manifest_sha256=None,
            approval_file=wheelhouse.APPROVAL_LOCK,
            bootstrap=True,
        )
    assert raised.value.__notes__ == [
        (
            "cleanup errors: build staging cleanup failure; "
            "build work cleanup failure"
        )
    ]


def test_outer_exception_cannot_hide_success_path_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "wheelhouse"
    monkeypatch.setattr(wheelhouse, "load_sources_lock", lambda: {})
    monkeypatch.setattr(
        wheelhouse, "preflight_native_builder", lambda _source_lock: {}
    )

    def successful_build(staging, _work, _source_lock, _toolchain):
        (staging / "payload").write_bytes(b"built")
        return "a" * 64, "b" * 64

    monkeypatch.setattr(wheelhouse, "build_into", successful_build)
    monkeypatch.setattr(
        wheelhouse,
        "_remove_owned_directory",
        lambda _path, *, role: (_ for _ in ()).throw(
            wheelhouse.SupplyChainError(f"{role} cleanup failure")
        ),
    )

    outer = ValueError("outer exception")
    try:
        raise outer
    except ValueError:
        with pytest.raises(
            wheelhouse.SupplyChainError,
            match="build work cleanup failure",
        ):
            wheelhouse.build_artifact(
                output,
                expected_content_sha256=None,
                expected_manifest_sha256=None,
                approval_file=wheelhouse.APPROVAL_LOCK,
                bootstrap=True,
            )
    assert not hasattr(outer, "__notes__")


def test_owned_cleanup_retries_windows_readonly_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owned = tmp_path / "owned"
    owned.mkdir()
    readonly = owned / "readonly.txt"
    readonly.write_text("generated")
    readonly.chmod(0o400)

    def simulate_windows_rmtree(path, *, onexc):
        onexc(os.unlink, readonly, PermissionError("readonly"))
        os.rmdir(path)

    monkeypatch.setattr(wheelhouse.shutil, "rmtree", simulate_windows_rmtree)
    wheelhouse._remove_owned_directory(owned, role="test")
    assert not owned.exists()


def test_tag_style_build_fails_before_preflight_without_approval(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bootstrap_required = tmp_path / "bootstrap-required.json"
    bootstrap_required.write_text(
        json.dumps(
            {
                "content_sha256": "0" * 64,
                "contract_id": (
                    "suxiaoyou-cpython-3.12.10-windows-arm64-v1"
                ),
                "schema_version": 1,
                "status": "bootstrap-required",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    result = wheelhouse.main(
        [
            "build",
            "--output",
            str(tmp_path / "artifact"),
            "--approval-file",
            str(bootstrap_required),
        ]
    )
    assert result == 3
    assert "manual --bootstrap build" in capsys.readouterr().err


def test_content_identity_excludes_runner_toolchain() -> None:
    content = {"contract": {"id": "stable"}, "files": []}
    first = hashlib.sha256(wheelhouse.canonical_json_bytes(content)).hexdigest()
    manifest_a = {
        "content": content,
        "content_sha256": first,
        "schema_version": 1,
        "toolchain": {"runner": "one"},
    }
    manifest_b = {
        **manifest_a,
        "toolchain": {"runner": "two", "git_commit": "different"},
    }
    assert manifest_a["content_sha256"] == manifest_b["content_sha256"]
    assert (
        hashlib.sha256(wheelhouse.canonical_json_bytes(manifest_a)).hexdigest()
        != hashlib.sha256(wheelhouse.canonical_json_bytes(manifest_b)).hexdigest()
    )
