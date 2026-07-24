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
    monkeypatch.setenv("PIP_INDEX_URL", "https://attacker.invalid")
    monkeypatch.setenv("OPENSSL_DIR", "C:/attacker")
    monkeypatch.setenv("OPENSSL_STATIC", "0")
    monkeypatch.setenv("VCPKG_ROOT", "C:/attacker-vcpkg")
    monkeypatch.setenv("DEP_OPENSSL_VERSION_NUMBER", "bad")
    env = wheelhouse.locked_network_environment()
    assert "PIP_INDEX_URL" not in env
    assert "OPENSSL_DIR" not in env
    assert "OPENSSL_STATIC" not in env
    assert "VCPKG_ROOT" not in env
    assert "DEP_OPENSSL_VERSION_NUMBER" not in env


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


def test_openssl_build_contract_enforces_reproducibility_and_tests() -> None:
    source = Path(wheelhouse.__file__).read_text(encoding="utf-8")
    assert '"SOURCE_DATE_EPOCH": str(source_date_epoch)' in source
    assert '"ARFLAGS": "/nologo /Brepro"' in source
    assert '"CL": "/FS /Brepro"' in source
    assert '"LINK": "/Brepro"' in source
    assert 'run_checked([nmake, "/E", "/NOLOGO", "test"]' in source
    assert "OpenSSL_version(0)" in source
    assert "AESGCM" in source
    assert "CertificateBuilder" in source


def test_approval_lock_requires_manual_bootstrap_then_nonzero_digest(
    tmp_path: Path,
) -> None:
    assert wheelhouse.approved_content_sha256(wheelhouse.APPROVAL_LOCK) is None
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


def test_tag_style_build_fails_before_preflight_without_approval(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = wheelhouse.main(
        ["build", "--output", str(tmp_path / "artifact")]
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
