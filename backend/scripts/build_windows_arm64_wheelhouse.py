#!/usr/bin/env python3
"""Build and attest the CPython 3.12.10 Windows ARM64 wheel supply chain.

The checked-in production lock intentionally retains the official sdist
hashes for ``cryptography`` and ``tiktoken`` because those projects do not
publish win_arm64 wheels.  This script:

1. downloads every other wheel through a hash-locked, binary-only pip command;
2. downloads the two exact sdists by immutable URL and verifies their SHA-256;
3. vendors Cargo.lock-pinned crates, then disables the network;
4. builds the two native wheels with a hash-locked Python build toolchain;
5. verifies wheel tags and every PE binary's ARM64 machine field;
6. emits wheel-specific install locks plus a canonical manifest and SHA-256.

``verify`` requires an out-of-band approved content digest.  The raw manifest
digest remains available for exact-run provenance, but is not the release
approval identity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
import uuid
import venv
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence


BACKEND_DIR = Path(__file__).resolve().parents[1]
PRODUCTION_LOCK = BACKEND_DIR / "requirements-windows-arm64.txt"
BUILD_LOCK = BACKEND_DIR / "requirements-windows-arm64-build.txt"
BUILD_INPUT = BACKEND_DIR / "requirements-windows-arm64-build.in"
RELEASE_TOOLS_LOCK = BACKEND_DIR / "requirements-release-tools-windows-arm64.txt"
OVERRIDES_LOCK = BACKEND_DIR / "requirements-windows-arm64-overrides.txt"
SOURCES_LOCK = BACKEND_DIR / "requirements-windows-arm64-sources.json"
TIKTOKEN_CARGO_LOCK = BACKEND_DIR / "requirements-windows-arm64-tiktoken.Cargo.lock"
APPROVAL_LOCK = (
    BACKEND_DIR / "requirements-windows-arm64-wheelhouse-approval.json"
)

MANIFEST_NAME = "windows-arm64-wheelhouse-manifest.json"
MANIFEST_DIGEST_NAME = f"{MANIFEST_NAME}.sha256"
EXPECTED_PYTHON = (3, 12, 10)
EXPECTED_PLATFORM_TAG = "win_arm64"
EXPECTED_RUST_HOST = "aarch64-pc-windows-msvc"
PE_MACHINE_ARM64 = 0xAA64
NATIVE_SOURCE_NAMES = frozenset({"cryptography", "tiktoken"})
MAX_SOURCE_BYTES = 256 * 1024 * 1024
MAX_WHEEL_UNCOMPRESSED_BYTES = 4 * 1024 * 1024 * 1024

_REQUIREMENT_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*)"
    r"(?:\[[^\]]+\])?==(?P<version>[^\s;\\]+)"
)
_HASH_RE = re.compile(r"--hash=sha256:(?P<digest>[0-9a-f]{64})(?:\s|\\|$)")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RUST_HOST_RE = re.compile(r"^host:\s*(?P<host>\S+)\s*$", re.MULTILINE)
_MSVC_VERSION_RE = re.compile(
    r"Compiler Version\s+(?P<version>[0-9.]+)\s+for\s+(?P<arch>\S+)",
    re.IGNORECASE,
)


class SupplyChainError(RuntimeError):
    """A fail-closed supply-chain contract violation."""


@dataclass(frozen=True)
class RequirementPin:
    name: str
    normalized_name: str
    version: str
    hashes: tuple[str, ...]


@dataclass(frozen=True)
class WheelEvidence:
    path: Path
    distribution: str
    normalized_name: str
    version: str
    python_tags: tuple[str, ...]
    abi_tags: tuple[str, ...]
    platform_tags: tuple[str, ...]
    sha256: str
    size: int
    native_members: tuple[dict[str, Any], ...]

    def to_manifest(self, root: Path) -> dict[str, Any]:
        return {
            "abi_tags": list(self.abi_tags),
            "distribution": self.distribution,
            "filename": self.path.relative_to(root).as_posix(),
            "native_members": list(self.native_members),
            "normalized_name": self.normalized_name,
            "platform_tags": list(self.platform_tags),
            "python_tags": list(self.python_tags),
            "sha256": self.sha256,
            "size": self.size,
            "version": self.version,
        }


def normalize_distribution(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def parse_requirement_lock(path: Path) -> dict[str, RequirementPin]:
    text = path.read_text(encoding="utf-8")
    pins: dict[str, RequirementPin] = {}
    mutable: dict[str, dict[str, Any]] = {}
    current: str | None = None

    for line_number, line in enumerate(text.splitlines(), start=1):
        match = _REQUIREMENT_RE.match(line)
        if match:
            normalized = normalize_distribution(match.group("name"))
            if normalized in mutable:
                raise SupplyChainError(
                    f"{path}: duplicate requirement {normalized!r} at line "
                    f"{line_number}"
                )
            mutable[normalized] = {
                "name": match.group("name"),
                "version": match.group("version"),
                "hashes": set(),
            }
            current = normalized
            continue

        hash_match = _HASH_RE.search(line)
        if hash_match:
            if current is None:
                raise SupplyChainError(
                    f"{path}: orphaned SHA-256 at line {line_number}"
                )
            mutable[current]["hashes"].add(hash_match.group("digest"))
            continue

        stripped = line.strip()
        if (
            stripped
            and not stripped.startswith("#")
            and not stripped.startswith("--")
            and not line[:1].isspace()
        ):
            raise SupplyChainError(
                f"{path}: non-exact or unsupported requirement at line "
                f"{line_number}: {stripped!r}"
            )

    if not mutable:
        raise SupplyChainError(f"{path}: lock contains no requirements")

    for normalized, value in mutable.items():
        hashes = tuple(sorted(value["hashes"]))
        if not hashes:
            raise SupplyChainError(
                f"{path}: requirement {normalized!r} has no SHA-256 hashes"
            )
        pins[normalized] = RequirementPin(
            name=value["name"],
            normalized_name=normalized,
            version=value["version"],
            hashes=hashes,
        )
    return pins


def filtered_requirement_lock(
    path: Path,
    *,
    excluded_names: Iterable[str],
) -> str:
    """Return a lock with complete uv blocks for selected names removed."""

    excluded = {normalize_distribution(name) for name in excluded_names}
    output: list[str] = []
    include_block = True
    found: set[str] = set()

    for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
        match = _REQUIREMENT_RE.match(line)
        if match:
            normalized = normalize_distribution(match.group("name"))
            include_block = normalized not in excluded
            if not include_block:
                found.add(normalized)
        if include_block:
            output.append(line)

    missing = excluded - found
    if missing:
        raise SupplyChainError(
            f"{path}: cannot filter absent requirements: {sorted(missing)}"
        )
    return "".join(output)


def load_sources_lock(path: Path = SOURCES_LOCK) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SupplyChainError(f"cannot read source lock {path}: {exc}") from exc

    if value.get("schema_version") != 1:
        raise SupplyChainError(f"{path}: unsupported schema_version")
    target = value.get("target")
    if not isinstance(target, dict):
        raise SupplyChainError(f"{path}: target must be an object")
    expected_target = {
        "implementation": "CPython",
        "python_version": ".".join(map(str, EXPECTED_PYTHON)),
        "platform_tag": EXPECTED_PLATFORM_TAG,
        "rust_host": EXPECTED_RUST_HOST,
        "pe_machine": hex(PE_MACHINE_ARM64),
    }
    if target != expected_target:
        raise SupplyChainError(
            f"{path}: target drift: expected {expected_target!r}, got {target!r}"
        )

    packages = value.get("packages")
    if not isinstance(packages, list):
        raise SupplyChainError(f"{path}: packages must be an array")
    names: set[str] = set()
    for package in packages:
        if not isinstance(package, dict):
            raise SupplyChainError(f"{path}: package entries must be objects")
        name = normalize_distribution(str(package.get("name", "")))
        if name in names or name not in NATIVE_SOURCE_NAMES:
            raise SupplyChainError(f"{path}: unexpected or duplicate package {name!r}")
        names.add(name)
        digest = package.get("sha256")
        cargo_digest = package.get("cargo_lock_sha256")
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise SupplyChainError(f"{path}: invalid source digest for {name}")
        if not isinstance(cargo_digest, str) or not _SHA256_RE.fullmatch(
            cargo_digest
        ):
            raise SupplyChainError(f"{path}: invalid Cargo.lock digest for {name}")
        parsed = urllib.parse.urlparse(str(package.get("url", "")))
        if (
            parsed.scheme != "https"
            or parsed.hostname != "files.pythonhosted.org"
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise SupplyChainError(f"{path}: untrusted source URL for {name}")
        filename = package.get("filename")
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise SupplyChainError(f"{path}: unsafe source filename for {name}")
    if names != NATIVE_SOURCE_NAMES:
        raise SupplyChainError(
            f"{path}: source package set drift: {sorted(names)!r}"
        )

    openssl = value.get("openssl")
    if not isinstance(openssl, dict):
        raise SupplyChainError(f"{path}: openssl must be an object")
    if openssl.get("version") != "4.0.1":
        raise SupplyChainError(f"{path}: OpenSSL version drift")
    if openssl.get("license") != "Apache-2.0":
        raise SupplyChainError(f"{path}: OpenSSL license drift")
    if openssl.get("configure_target") != "VC-WIN64-ARM":
        raise SupplyChainError(f"{path}: OpenSSL target drift")
    if openssl.get("build_flags") != [
        "no-zlib",
        "no-shared",
        "no-module",
        "no-comp",
        "no-apps",
        "no-docs",
        "no-sm2-precomp",
        "no-atexit",
    ]:
        raise SupplyChainError(f"{path}: OpenSSL build flags drift")
    openssl_digest = openssl.get("sha256")
    if not isinstance(openssl_digest, str) or not _SHA256_RE.fullmatch(
        openssl_digest
    ):
        raise SupplyChainError(f"{path}: invalid OpenSSL source digest")
    openssl_url = urllib.parse.urlparse(str(openssl.get("url", "")))
    if (
        openssl_url.scheme != "https"
        or openssl_url.hostname != "github.com"
        or openssl_url.username is not None
        or openssl_url.password is not None
    ):
        raise SupplyChainError(f"{path}: untrusted OpenSSL source URL")
    openssl_filename = openssl.get("filename")
    if (
        not isinstance(openssl_filename, str)
        or Path(openssl_filename).name != openssl_filename
    ):
        raise SupplyChainError(f"{path}: unsafe OpenSSL source filename")
    return value


def run_checked(
    command: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    capture_output: bool = False,
    acceptable_returncodes: frozenset[int] = frozenset({0}),
) -> subprocess.CompletedProcess[str]:
    display = " ".join(str(part) for part in command)
    completed = subprocess.run(
        [str(part) for part in command],
        cwd=cwd,
        env=dict(env) if env is not None else None,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
    )
    if completed.returncode not in acceptable_returncodes:
        details = ""
        if capture_output:
            details = (
                f"\nstdout:\n{completed.stdout or ''}"
                f"\nstderr:\n{completed.stderr or ''}"
            )
        raise SupplyChainError(
            f"command failed ({completed.returncode}): {display}{details}"
        )
    return completed


def locked_network_environment() -> dict[str, str]:
    env = os.environ.copy()
    for key in tuple(env):
        upper = key.upper()
        if (
            upper.startswith("PIP_")
            or upper.startswith("UV_")
            or upper.startswith("CARGO_REGISTRIES_")
            or upper.startswith("OPENSSL_")
            or upper.startswith("DEP_OPENSSL_")
            or upper.startswith("VCPKG_")
            or upper
            in {
                "CARGO_HOME",
                "RUSTFLAGS",
                "VCPKGRS_DYNAMIC",
                "VCPKGRS_TRIPLET",
            }
        ):
            env.pop(key, None)
    env.update(
        {
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
            "PYTHONHASHSEED": "0",
        }
    )
    return env


def initialize_native_arm64_msvc_environment() -> None:
    """Replace ambient VS state with the latest native ARM64 toolchain."""

    candidates = [
        shutil.which("vswhere.exe"),
        Path(os.environ.get("ProgramFiles(x86)", ""))
        / "Microsoft Visual Studio"
        / "Installer"
        / "vswhere.exe",
    ]
    vswhere = next(
        (
            Path(candidate)
            for candidate in candidates
            if candidate and Path(candidate).is_file()
        ),
        None,
    )
    if vswhere is None:
        raise SupplyChainError("vswhere.exe is required to initialize MSVC")
    result = run_checked(
        [
            vswhere,
            "-latest",
            "-products",
            "*",
            "-requires",
            "Microsoft.VisualStudio.Component.VC.Tools.ARM64",
            "-property",
            "installationPath",
        ],
        capture_output=True,
    )
    installation = result.stdout.strip()
    if not installation:
        raise SupplyChainError("Visual Studio ARM64 tools were not found")
    vsdevcmd = (
        Path(installation) / "Common7" / "Tools" / "VsDevCmd.bat"
    )
    if not vsdevcmd.is_file():
        raise SupplyChainError(f"missing Visual Studio setup script: {vsdevcmd}")
    cmd = shutil.which("cmd.exe")
    if cmd is None:
        raise SupplyChainError("cmd.exe is required to initialize MSVC")
    # Visual Studio 2022 17.4+ provides a native HostARM64 toolchain.  A local
    # wrapper avoids cmd.exe's fragile nested quoting for a setup path with
    # spaces, preserves batch-file control flow with `call`, and gives cmd.exe
    # a guaranteed drive-backed working directory.
    with tempfile.TemporaryDirectory(prefix="suxiaoyou-vsdevcmd-") as temporary:
        wrapper_root = Path(temporary)
        wrapper = wrapper_root / "capture-arm64-environment.cmd"
        wrapper.write_bytes(
            (
                "@echo off\r\n"
                f'call "{vsdevcmd}" -no_logo -arch=arm64 -host_arch=arm64\r\n'
                "if errorlevel 1 exit /b %errorlevel%\r\n"
                "set\r\n"
            ).encode("utf-8")
        )
        environment_result = run_checked(
            [cmd, "/d", "/c", wrapper.name],
            cwd=wrapper_root,
            capture_output=True,
        )
    captured: dict[str, str] = {}
    for line in environment_result.stdout.splitlines():
        if "=" not in line or line.startswith("="):
            continue
        key, value = line.split("=", 1)
        if key:
            # Windows environment-variable names are case-insensitive, while
            # a regular Python dict is not.  `set` commonly emits `Path=...`
            # even though callers and Visual Studio use `PATH`.
            captured[key.upper()] = value
    if not captured.get("PATH"):
        raise SupplyChainError("VsDevCmd did not emit a usable environment")
    host_arch = captured.get("VSCMD_ARG_HOST_ARCH", "").lower()
    target_arch = captured.get("VSCMD_ARG_TGT_ARCH", "").lower()
    if host_arch != "arm64" or target_arch != "arm64":
        raise SupplyChainError(
            "VsDevCmd selected the wrong compiler architecture: "
            f"host={host_arch!r}, target={target_arch!r}"
        )
    os.environ.update(captured)


def ensure_bootstrap_pip() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "pip", "--version"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if completed.returncode == 0:
        return
    run_checked([sys.executable, "-m", "ensurepip", "--default-pip"])


def preflight_native_builder(source_lock: Mapping[str, Any]) -> dict[str, str]:
    if sys.implementation.name != "cpython":
        raise SupplyChainError("builder must use CPython")
    if sys.version_info[:3] != EXPECTED_PYTHON:
        raise SupplyChainError(
            "builder must use CPython "
            f"{'.'.join(map(str, EXPECTED_PYTHON))}; got "
            f"{platform.python_version()}"
        )
    if platform.system() != "Windows":
        raise SupplyChainError(
            f"native build requires Windows; got {platform.system()}"
        )
    machine = platform.machine().upper()
    if machine not in {"ARM64", "AARCH64"}:
        raise SupplyChainError(
            f"native build requires Windows ARM64; got {platform.machine()}"
        )
    if struct.calcsize("P") != 8:
        raise SupplyChainError("native builder must be a 64-bit process")

    initialize_native_arm64_msvc_environment()

    rustc = run_checked(
        ["rustc", "--version"], capture_output=True
    ).stdout.strip()
    cargo = run_checked(
        ["cargo", "--version"], capture_output=True
    ).stdout.strip()
    rustc_verbose = run_checked(
        ["rustc", "-Vv"], capture_output=True
    ).stdout
    host_match = _RUST_HOST_RE.search(rustc_verbose)
    rust_host = host_match.group("host") if host_match else ""

    toolchain = source_lock.get("toolchain")
    if not isinstance(toolchain, Mapping):
        raise SupplyChainError("source lock toolchain must be an object")
    if rustc != toolchain.get("rustc_version"):
        raise SupplyChainError(
            f"rustc drift: expected {toolchain.get('rustc_version')!r}, "
            f"got {rustc!r}"
        )
    if cargo != toolchain.get("cargo_version"):
        raise SupplyChainError(
            f"cargo drift: expected {toolchain.get('cargo_version')!r}, "
            f"got {cargo!r}"
        )
    if rust_host != EXPECTED_RUST_HOST:
        raise SupplyChainError(
            f"Rust host drift: expected {EXPECTED_RUST_HOST!r}, "
            f"got {rust_host!r}"
        )

    compiler = shutil.which("cl.exe")
    if compiler is None:
        raise SupplyChainError(
            "cl.exe is not on PATH; run from a native ARM64 MSVC developer "
            "environment"
        )
    cl_result = run_checked(
        [compiler],
        capture_output=True,
        acceptable_returncodes=frozenset({0, 2}),
    )
    cl_banner = f"{cl_result.stdout}\n{cl_result.stderr}"
    cl_match = _MSVC_VERSION_RE.search(cl_banner)
    if cl_match is None:
        raise SupplyChainError("could not parse the native MSVC compiler banner")
    compiler_arch = cl_match.group("arch").lower()
    if compiler_arch not in {"arm64", "aarch64"}:
        raise SupplyChainError(
            f"MSVC compiler is not ARM64: {cl_match.group(0)!r}"
        )

    perl = shutil.which("perl.exe") or shutil.which("perl")
    if perl is None:
        raise SupplyChainError(
            "perl is required to configure the locked OpenSSL source"
        )
    perl_result = run_checked([perl, "-v"], capture_output=True)
    perl_line = next(
        (
            line.strip()
            for line in perl_result.stdout.splitlines()
            if "This is perl" in line
        ),
        "",
    )
    if not perl_line:
        raise SupplyChainError("could not parse perl -v output")

    nmake = shutil.which("nmake.exe") or shutil.which("nmake")
    if nmake is None:
        raise SupplyChainError(
            "nmake.exe is required to build the locked OpenSSL source"
        )
    nmake_result = run_checked([nmake, "/?"], capture_output=True)
    nmake_line = next(
        (
            line.strip()
            for line in nmake_result.stdout.splitlines()
            if "Program Maintenance Utility Version" in line
        ),
        "",
    )
    if not nmake_line:
        raise SupplyChainError("could not parse nmake /? output")

    ensure_bootstrap_pip()
    return {
        "cargo": cargo,
        "msvc_arch": compiler_arch,
        "msvc_version": cl_match.group("version"),
        "nmake": nmake_line,
        "perl": perl_line,
        "python": platform.python_version(),
        "rust_host": rust_host,
        "rustc": rustc,
    }


def download_locked_url(url: str, destination: Path, expected_sha256: str) -> None:
    parsed = urllib.parse.urlparse(url)
    allowed_initial_hosts = {"files.pythonhosted.org", "github.com"}
    if (
        parsed.scheme != "https"
        or parsed.hostname not in allowed_initial_hosts
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise SupplyChainError(f"refusing unapproved source URL: {url}")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "suxiaoyou-windows-arm64-wheelhouse/1"},
    )
    digest = hashlib.sha256()
    size = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.download-{uuid.uuid4().hex}"
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            final = urllib.parse.urlparse(response.geturl())
            allowed_final_hosts = {
                "files.pythonhosted.org",
                "github.com",
                "release-assets.githubusercontent.com",
            }
            if (
                final.scheme != "https"
                or final.hostname not in allowed_final_hosts
            ):
                raise SupplyChainError(
                    f"source URL redirected outside approved hosts: "
                    f"{response.geturl()}"
                )
            with temporary.open("xb") as handle:
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_SOURCE_BYTES:
                        raise SupplyChainError(
                            f"source archive exceeds {MAX_SOURCE_BYTES} bytes"
                        )
                    digest.update(chunk)
                    handle.write(chunk)
        actual = digest.hexdigest()
        if actual != expected_sha256:
            raise SupplyChainError(
                f"source hash mismatch for {destination.name}: expected "
                f"{expected_sha256}, got {actual}"
            )
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def pip_download_locked(requirements: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    command = [
        sys.executable,
        "-m",
        "pip",
        "--isolated",
        "download",
        "--disable-pip-version-check",
        "--no-cache-dir",
        "--no-deps",
        "--require-hashes",
        "--only-binary=:all:",
        "--index-url",
        "https://pypi.org/simple",
        "--platform",
        EXPECTED_PLATFORM_TAG,
        "--implementation",
        "cp",
        "--python-version",
        "312",
        "--abi",
        "cp312",
        "--abi",
        "abi3",
        "--abi",
        "none",
        "--dest",
        destination,
        "--requirement",
        requirements,
    ]
    run_checked(command, env=locked_network_environment())


def parse_wheel_filename(path: Path) -> tuple[str, str, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    if path.suffix != ".whl":
        raise SupplyChainError(f"not a wheel: {path.name}")
    parts = path.name[:-4].split("-")
    if len(parts) not in {5, 6}:
        raise SupplyChainError(f"unsupported wheel filename: {path.name}")
    prefix = parts[:-3]
    if len(prefix) == 3 and not prefix[2][:1].isdigit():
        raise SupplyChainError(f"invalid wheel build tag: {path.name}")
    distribution, version = prefix[0], prefix[1]
    if not distribution or not version:
        raise SupplyChainError(f"invalid wheel filename: {path.name}")
    python_tags = tuple(sorted(set(parts[-3].split("."))))
    abi_tags = tuple(sorted(set(parts[-2].split("."))))
    platform_tags = tuple(sorted(set(parts[-1].split("."))))
    if not python_tags or not abi_tags or not platform_tags:
        raise SupplyChainError(f"wheel has empty compatibility tag: {path.name}")
    return distribution, version, python_tags, abi_tags, platform_tags


def _cpython_tag_is_abi3_compatible(python_tag: str) -> bool:
    match = re.fullmatch(r"cp(?P<major>\d)(?P<minor>\d+)", python_tag)
    if match is None:
        return False
    return (
        int(match.group("major")) == EXPECTED_PYTHON[0]
        and int(match.group("minor")) <= EXPECTED_PYTHON[1]
    )


def _tag_combination_is_compatible(
    python_tag: str,
    abi_tag: str,
    platform_tag: str,
) -> bool:
    if platform_tag == "any":
        return abi_tag == "none" and (
            python_tag in {"py3", "py2.py3", "cp312"}
            or python_tag.startswith("py3")
        )
    if platform_tag != EXPECTED_PLATFORM_TAG:
        return False
    if python_tag == "cp312" and abi_tag in {"cp312", "abi3", "none"}:
        return True
    if abi_tag == "abi3" and _cpython_tag_is_abi3_compatible(python_tag):
        return True
    return abi_tag == "none" and python_tag.startswith("py3")


def pe_machine(data: bytes, *, member: str) -> int:
    if len(data) < 64 or data[:2] != b"MZ":
        raise SupplyChainError(f"{member}: native member is not a PE image")
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if pe_offset > len(data) - 6 or data[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise SupplyChainError(f"{member}: invalid PE signature")
    return struct.unpack_from("<H", data, pe_offset + 4)[0]


def inspect_wheel(path: Path) -> WheelEvidence:
    (
        distribution,
        version,
        python_tags,
        abi_tags,
        platform_tags,
    ) = parse_wheel_filename(path)
    combinations = (
        (python_tag, abi_tag, platform_tag)
        for python_tag in python_tags
        for abi_tag in abi_tags
        for platform_tag in platform_tags
    )
    if not any(_tag_combination_is_compatible(*tags) for tags in combinations):
        raise SupplyChainError(
            f"{path.name}: no CPython 3.12 win_arm64-compatible wheel tag"
        )
    if any(tag not in {"any", EXPECTED_PLATFORM_TAG} for tag in platform_tags):
        raise SupplyChainError(
            f"{path.name}: forbidden platform tags {platform_tags!r}"
        )

    native: list[dict[str, Any]] = []
    seen_members: set[str] = set()
    total_uncompressed = 0
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                member_path = PurePosixPath(info.filename)
                if (
                    info.filename in seen_members
                    or member_path.is_absolute()
                    or ".." in member_path.parts
                ):
                    raise SupplyChainError(
                        f"{path.name}: unsafe or duplicate ZIP member "
                        f"{info.filename!r}"
                    )
                seen_members.add(info.filename)
                total_uncompressed += info.file_size
                if total_uncompressed > MAX_WHEEL_UNCOMPRESSED_BYTES:
                    raise SupplyChainError(
                        f"{path.name}: wheel exceeds the uncompressed size limit"
                    )
                if not info.filename.lower().endswith((".pyd", ".dll", ".exe")):
                    continue
                data = archive.read(info)
                machine = pe_machine(
                    data, member=f"{path.name}:{info.filename}"
                )
                if machine != PE_MACHINE_ARM64:
                    raise SupplyChainError(
                        f"{path.name}:{info.filename}: expected PE machine "
                        f"{hex(PE_MACHINE_ARM64)}, got {hex(machine)}"
                    )
                native.append(
                    {
                        "member": info.filename,
                        "pe_machine": hex(machine),
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "size": len(data),
                    }
                )
    except zipfile.BadZipFile as exc:
        raise SupplyChainError(f"{path.name}: invalid wheel ZIP: {exc}") from exc

    if platform_tags == ("any",) and native:
        raise SupplyChainError(
            f"{path.name}: pure wheel contains native PE members"
        )
    return WheelEvidence(
        path=path,
        distribution=distribution,
        normalized_name=normalize_distribution(distribution),
        version=version,
        python_tags=python_tags,
        abi_tags=abi_tags,
        platform_tags=platform_tags,
        sha256=sha256_file(path),
        size=path.stat().st_size,
        native_members=tuple(native),
    )


def verify_wheel_directory(
    directory: Path,
    expected: Mapping[str, RequirementPin],
) -> dict[str, WheelEvidence]:
    if not directory.is_dir() or directory.is_symlink():
        raise SupplyChainError(f"wheelhouse is missing or unsafe: {directory}")
    unexpected_files = sorted(
        path.name
        for path in directory.iterdir()
        if not path.is_file() or path.suffix != ".whl"
    )
    if unexpected_files:
        raise SupplyChainError(
            f"{directory}: non-wheel entries are forbidden: {unexpected_files}"
        )

    wheels: dict[str, WheelEvidence] = {}
    for path in sorted(directory.glob("*.whl"), key=lambda item: item.name.lower()):
        if path.is_symlink():
            raise SupplyChainError(f"symlink wheel is forbidden: {path}")
        evidence = inspect_wheel(path)
        if evidence.normalized_name in wheels:
            raise SupplyChainError(
                f"{directory}: duplicate distribution "
                f"{evidence.normalized_name!r}"
            )
        wheels[evidence.normalized_name] = evidence

    expected_names = set(expected)
    actual_names = set(wheels)
    if actual_names != expected_names:
        raise SupplyChainError(
            f"{directory}: wheel set drift; missing="
            f"{sorted(expected_names - actual_names)}, extra="
            f"{sorted(actual_names - expected_names)}"
        )
    for normalized, pin in expected.items():
        wheel = wheels[normalized]
        if wheel.version != pin.version:
            raise SupplyChainError(
                f"{wheel.path.name}: expected {pin.name}=={pin.version}, "
                f"got version {wheel.version}"
            )
    return wheels


def write_install_lock(
    path: Path,
    pins: Mapping[str, RequirementPin],
    wheels: Mapping[str, WheelEvidence],
) -> None:
    if set(pins) != set(wheels):
        raise SupplyChainError(f"cannot write incomplete install lock {path}")
    lines = [
        "# Generated by build_windows_arm64_wheelhouse.py.",
        "# Install only with --no-index --find-links=<matching wheelhouse>",
        "# --require-hashes --no-deps.  Each pin accepts exactly one wheel.",
        "--only-binary=:all:",
        "",
    ]
    for normalized in sorted(pins):
        pin = pins[normalized]
        wheel = wheels[normalized]
        lines.extend(
            [
                f"{pin.name}=={pin.version} \\",
                f"    --hash=sha256:{wheel.sha256}",
            ]
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def safe_extract_sdist(archive_path: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=False)
    roots: set[str] = set()
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            for member in archive.getmembers():
                pure = PurePosixPath(member.name)
                if (
                    pure.is_absolute()
                    or ".." in pure.parts
                    or member.issym()
                    or member.islnk()
                    or member.isdev()
                ):
                    raise SupplyChainError(
                        f"{archive_path.name}: unsafe tar member "
                        f"{member.name!r}"
                    )
                if pure.parts:
                    roots.add(pure.parts[0])
            if len(roots) != 1:
                raise SupplyChainError(
                    f"{archive_path.name}: expected one archive root, got "
                    f"{sorted(roots)!r}"
                )
            archive.extractall(destination, filter="data")
    except tarfile.TarError as exc:
        raise SupplyChainError(
            f"{archive_path.name}: invalid sdist archive: {exc}"
        ) from exc
    root = destination / next(iter(roots))
    if not root.is_dir() or root.is_symlink():
        raise SupplyChainError(f"{archive_path.name}: invalid extracted root")
    return root


def build_locked_openssl(
    archive_path: Path,
    destination: Path,
    openssl_lock: Mapping[str, Any],
    *,
    source_date_epoch: int,
) -> dict[str, Any]:
    """Build the exact static OpenSSL dependency without vcpkg."""

    source_root = safe_extract_sdist(
        archive_path, destination.parent / "openssl-source"
    )
    if source_root.name != f"openssl-{openssl_lock['version']}":
        raise SupplyChainError(
            f"OpenSSL archive root drift: {source_root.name!r}"
        )
    perl = shutil.which("perl.exe") or shutil.which("perl")
    nmake = shutil.which("nmake.exe") or shutil.which("nmake")
    dumpbin = shutil.which("dumpbin.exe") or shutil.which("dumpbin")
    if perl is None or nmake is None or dumpbin is None:
        raise SupplyChainError(
            "OpenSSL build requires perl, nmake.exe, and dumpbin.exe"
        )

    env = locked_network_environment()
    env.update(
        {
            "ARFLAGS": "/nologo /Brepro",
            "CL": "/FS /Brepro",
            "LINK": "/Brepro",
            "SOURCE_DATE_EPOCH": str(source_date_epoch),
            "TZ": "UTC",
            "ZERO_AR_DATE": "1",
        }
    )
    run_checked(
        [
            perl,
            "Configure",
            *openssl_lock["build_flags"],
            openssl_lock["configure_target"],
            f"--prefix={destination}",
            f"--openssldir={destination / 'ssl'}",
        ],
        cwd=source_root,
        env=env,
    )
    run_checked([nmake, "/E", "/NOLOGO"], cwd=source_root, env=env)
    run_checked([nmake, "/E", "/NOLOGO", "test"], cwd=source_root, env=env)

    destination.mkdir(parents=True, exist_ok=False)
    library_dir = destination / "lib"
    library_dir.mkdir()
    library_records: list[dict[str, Any]] = []
    for name in ("libcrypto.lib", "libssl.lib"):
        source_library = source_root / name
        if not source_library.is_file():
            raise SupplyChainError(
                f"OpenSSL did not produce static library {source_library}"
            )
        destination_library = library_dir / name
        shutil.copy2(source_library, destination_library)
        headers = run_checked(
            [dumpbin, "/headers", destination_library],
            capture_output=True,
        ).stdout
        if "AA64 machine (ARM64)" not in headers:
            raise SupplyChainError(
                f"{name}: dumpbin did not find ARM64 COFF members"
            )
        if (
            "8664 machine (x64)" in headers
            or "14C machine (x86)" in headers
        ):
            raise SupplyChainError(
                f"{name}: dumpbin found a non-ARM64 COFF member"
            )
        library_records.append(
            {
                "filename": name,
                "sha256": sha256_file(destination_library),
                "size": destination_library.stat().st_size,
            }
        )
    shutil.copytree(source_root / "include", destination / "include")
    prefix_sha256, prefix_file_count = tree_digest(destination)
    return {
        "build_flags": list(openssl_lock["build_flags"]),
        "configure_target": openssl_lock["configure_target"],
        "license": openssl_lock["license"],
        "libraries": library_records,
        "prefix_file_count": prefix_file_count,
        "prefix_tree_sha256": prefix_sha256,
        "source_sha256": openssl_lock["sha256"],
        "version": openssl_lock["version"],
    }


def tree_digest(root: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    file_count = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise SupplyChainError(f"symlink is forbidden in vendored tree: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(struct.pack(">I", len(relative)))
        digest.update(relative)
        digest.update(bytes.fromhex(sha256_file(path)))
        file_count += 1
    if file_count == 0:
        raise SupplyChainError(f"vendored tree is empty: {root}")
    return digest.hexdigest(), file_count


def prepare_cargo_vendor(
    source_root: Path,
    *,
    cargo_home: Path,
    expected_lock_sha256: str,
) -> dict[str, Any]:
    cargo_lock = source_root / "Cargo.lock"
    actual_lock_sha256 = sha256_file(cargo_lock)
    if actual_lock_sha256 != expected_lock_sha256:
        raise SupplyChainError(
            f"{cargo_lock}: expected SHA-256 {expected_lock_sha256}, "
            f"got {actual_lock_sha256}"
        )

    env = locked_network_environment()
    env.update(
        {
            "CARGO_HOME": str(cargo_home),
            "CARGO_REGISTRIES_CRATES_IO_PROTOCOL": "sparse",
        }
    )
    vendor_dir = source_root / ".cargo-vendor"
    completed = run_checked(
        [
            "cargo",
            "vendor",
            "--locked",
            "--versioned-dirs",
            vendor_dir.name,
        ],
        cwd=source_root,
        env=env,
        capture_output=True,
    )
    cargo_config_dir = source_root / ".cargo"
    cargo_config_dir.mkdir(exist_ok=False)
    cargo_config = cargo_config_dir / "config.toml"
    cargo_config.write_text(completed.stdout, encoding="utf-8", newline="\n")

    offline_env = env.copy()
    offline_env["CARGO_NET_OFFLINE"] = "true"
    run_checked(
        [
            "cargo",
            "metadata",
            "--locked",
            "--offline",
            "--format-version",
            "1",
            "--no-deps",
        ],
        cwd=source_root,
        env=offline_env,
        capture_output=True,
    )
    vendor_sha256, vendor_file_count = tree_digest(vendor_dir)
    return {
        "cargo_lock_sha256": actual_lock_sha256,
        "cargo_vendor_file_count": vendor_file_count,
        "cargo_vendor_tree_sha256": vendor_sha256,
    }


def venv_python(venv_dir: Path) -> Path:
    return venv_dir / "Scripts" / "python.exe"


def create_locked_build_environment(
    venv_dir: Path,
    build_wheelhouse: Path,
    build_install_lock: Path,
) -> Path:
    venv.EnvBuilder(with_pip=True, clear=False, symlinks=False).create(venv_dir)
    python = venv_python(venv_dir)
    if not python.is_file():
        raise SupplyChainError(f"venv did not create {python}")
    env = locked_network_environment()
    env["PIP_NO_INDEX"] = "1"
    run_checked(
        [
            python,
            "-m",
            "pip",
            "--isolated",
            "install",
            "--disable-pip-version-check",
            "--no-index",
            "--no-deps",
            "--require-hashes",
            "--only-binary=:all:",
            "--find-links",
            build_wheelhouse,
            "--requirement",
            build_install_lock,
        ],
        env=env,
    )
    return python


def build_native_wheel(
    python: Path,
    source_root: Path,
    output_directory: Path,
    *,
    cargo_home: Path,
    source_date_epoch: int,
    extra_environment: Mapping[str, str] | None = None,
) -> WheelEvidence:
    before = set(output_directory.glob("*.whl"))
    env = locked_network_environment()
    env.update(
        {
            "CARGO_HOME": str(cargo_home),
            "CARGO_INCREMENTAL": "0",
            "CARGO_NET_OFFLINE": "true",
            "PIP_NO_INDEX": "1",
            "PYTHONHASHSEED": "0",
            "SOURCE_DATE_EPOCH": str(source_date_epoch),
        }
    )
    if extra_environment:
        env.update(extra_environment)
    run_checked(
        [
            python,
            "-m",
            "pip",
            "--isolated",
            "wheel",
            "--disable-pip-version-check",
            "--no-index",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            output_directory,
            source_root,
        ],
        env=env,
    )
    created = set(output_directory.glob("*.whl")) - before
    if len(created) != 1:
        raise SupplyChainError(
            f"{source_root.name}: expected one built wheel, got "
            f"{sorted(path.name for path in created)!r}"
        )
    return inspect_wheel(next(iter(created)))


def verify_bundled_openssl(
    python: Path,
    cryptography_wheel: WheelEvidence,
    *,
    expected_version: str,
) -> str:
    if any(
        Path(str(member["member"])).name.lower().startswith(
            ("libcrypto", "libssl")
        )
        and str(member["member"]).lower().endswith(".dll")
        for member in cryptography_wheel.native_members
    ):
        raise SupplyChainError(
            f"{cryptography_wheel.path.name}: OpenSSL must be statically linked"
        )
    env = locked_network_environment()
    env["PIP_NO_INDEX"] = "1"
    run_checked(
        [
            python,
            "-m",
            "pip",
            "--isolated",
            "install",
            "--disable-pip-version-check",
            "--no-index",
            "--no-deps",
            cryptography_wheel.path,
        ],
        env=env,
    )
    probe = (
        "import datetime;"
        "from cryptography import x509;"
        "from cryptography.hazmat.bindings.openssl.binding import Binding;"
        "from cryptography.hazmat.primitives.asymmetric import ed25519;"
        "from cryptography.hazmat.primitives.ciphers.aead import AESGCM;"
        "from cryptography.x509.oid import NameOID;"
        "b=Binding();"
        "v=b.ffi.string(b.lib.OpenSSL_version(0)).decode('ascii');"
        "k=ed25519.Ed25519PrivateKey.generate();"
        "m=b'windows-arm64';s=k.sign(m);k.public_key().verify(s,m);"
        "a=AESGCM(bytes(range(32)));"
        "n=bytes(range(12));c=a.encrypt(n,m,b'aad');"
        "assert a.decrypt(n,c,b'aad')==m;"
        "name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'suxiaoyou')]);"
        "t=datetime.datetime(2026,1,1,tzinfo=datetime.timezone.utc);"
        "cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name)"
        ".public_key(k.public_key()).serial_number(1).not_valid_before(t)"
        ".not_valid_after(t+datetime.timedelta(days=1)).sign(k,None));"
        "k.public_key().verify(cert.signature,cert.tbs_certificate_bytes);"
        "print(v)"
    )
    version = run_checked(
        [python, "-c", probe],
        env=env,
        capture_output=True,
    ).stdout.strip()
    expected_prefix = f"OpenSSL {expected_version}"
    if not version.startswith(expected_prefix):
        raise SupplyChainError(
            f"cryptography linked {version!r}, expected {expected_prefix!r}"
        )
    return version


def validate_native_source_wheel(
    wheel: WheelEvidence,
    package: Mapping[str, Any],
) -> None:
    expected_name = normalize_distribution(str(package["name"]))
    if wheel.normalized_name != expected_name:
        raise SupplyChainError(
            f"built {wheel.path.name}, expected distribution {expected_name}"
        )
    if wheel.version != package["version"]:
        raise SupplyChainError(
            f"{wheel.path.name}: expected version {package['version']}"
        )
    if set(wheel.python_tags).isdisjoint(package["expected_python_tags"]):
        raise SupplyChainError(
            f"{wheel.path.name}: Python tag drift {wheel.python_tags!r}"
        )
    if set(wheel.abi_tags).isdisjoint(package["expected_abi_tags"]):
        raise SupplyChainError(
            f"{wheel.path.name}: ABI tag drift {wheel.abi_tags!r}"
        )
    for suffix in package["required_native_suffixes"]:
        if not any(
            str(member["member"]).lower().endswith(str(suffix).lower())
            for member in wheel.native_members
        ):
            raise SupplyChainError(
                f"{wheel.path.name}: missing required native member {suffix!r}"
            )


def copy_unique(source: Path, destination_directory: Path) -> Path:
    destination_directory.mkdir(parents=True, exist_ok=True)
    destination = destination_directory / source.name
    if destination.exists():
        raise SupplyChainError(f"duplicate artifact filename: {destination}")
    shutil.copy2(source, destination)
    return destination


def copy_input_locks(output: Path) -> None:
    destination = output / "locks"
    destination.mkdir(parents=True, exist_ok=False)
    for source in (
        PRODUCTION_LOCK,
        BUILD_LOCK,
        BUILD_INPUT,
        RELEASE_TOOLS_LOCK,
        OVERRIDES_LOCK,
        SOURCES_LOCK,
        TIKTOKEN_CARGO_LOCK,
    ):
        if not source.is_file() or source.is_symlink():
            raise SupplyChainError(f"required input lock is missing: {source}")
        shutil.copy2(source, destination / source.name)


def _file_role(relative: Path) -> str:
    top = relative.parts[0]
    return {
        "build-wheelhouse": "build-wheel",
        "install-locks": "derived-install-lock",
        "locks": "input-lock",
        "production-wheelhouse": "production-wheel",
        "release-tools-wheelhouse": "release-tool-wheel",
        "source-archives": "source-archive",
    }.get(top, "artifact")


def file_records(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.name in {MANIFEST_NAME, MANIFEST_DIGEST_NAME}:
            continue
        if path.is_symlink():
            raise SupplyChainError(f"symlink artifact is forbidden: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        records.append(
            {
                "path": relative.as_posix(),
                "role": _file_role(relative),
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
            }
        )
    return records


def _copy_wheel_group(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    for wheel in sorted(source.glob("*.whl"), key=lambda item: item.name.lower()):
        copy_unique(wheel, destination)


def offline_install_dry_run(
    python: Path,
    wheelhouse: Path,
    install_lock: Path,
) -> None:
    env = locked_network_environment()
    env["PIP_NO_INDEX"] = "1"
    run_checked(
        [
            python,
            "-m",
            "pip",
            "--isolated",
            "install",
            "--dry-run",
            "--disable-pip-version-check",
            "--no-index",
            "--no-deps",
            "--require-hashes",
            "--only-binary=:all:",
            "--find-links",
            wheelhouse,
            "--requirement",
            install_lock,
        ],
        env=env,
    )


def _manifest_wheel_groups(
    root: Path,
    groups: Mapping[str, Mapping[str, WheelEvidence]],
) -> dict[str, list[dict[str, Any]]]:
    return {
        name: [
            wheel.to_manifest(root)
            for wheel in sorted(
                wheels.values(), key=lambda item: item.normalized_name
            )
        ]
        for name, wheels in sorted(groups.items())
    }


def build_into(
    output: Path,
    work: Path,
    source_lock: Mapping[str, Any],
    toolchain_evidence: Mapping[str, str],
) -> tuple[str, str]:
    production_pins = parse_requirement_lock(PRODUCTION_LOCK)
    build_pins = parse_requirement_lock(BUILD_LOCK)
    release_pins = parse_requirement_lock(RELEASE_TOOLS_LOCK)
    source_packages = {
        normalize_distribution(package["name"]): package
        for package in source_lock["packages"]
    }
    for name, package in source_packages.items():
        pin = production_pins.get(name)
        if pin is None or pin.version != package["version"]:
            raise SupplyChainError(
                f"source lock and production lock disagree for {name}"
            )
        if package["sha256"] not in pin.hashes:
            raise SupplyChainError(
                f"{name} sdist SHA-256 is absent from production lock"
            )

    output.mkdir(parents=True, exist_ok=False)
    copy_input_locks(output)

    generated_locks = work / "download-locks"
    generated_locks.mkdir()
    production_binary_lock = generated_locks / "production-binary.txt"
    production_binary_lock.write_text(
        filtered_requirement_lock(
            PRODUCTION_LOCK, excluded_names=NATIVE_SOURCE_NAMES
        ),
        encoding="utf-8",
        newline="\n",
    )
    production_binary_pins = {
        name: pin
        for name, pin in production_pins.items()
        if name not in NATIVE_SOURCE_NAMES
    }

    downloads = work / "downloads"
    production_downloads = downloads / "production"
    build_downloads = downloads / "build"
    release_downloads = downloads / "release-tools"
    pip_download_locked(production_binary_lock, production_downloads)
    pip_download_locked(BUILD_LOCK, build_downloads)
    pip_download_locked(RELEASE_TOOLS_LOCK, release_downloads)

    production_binary_wheels = verify_wheel_directory(
        production_downloads, production_binary_pins
    )
    build_wheels = verify_wheel_directory(build_downloads, build_pins)
    release_wheels = verify_wheel_directory(release_downloads, release_pins)

    temporary_install_locks = work / "install-locks"
    temporary_install_locks.mkdir()
    build_install_lock = temporary_install_locks / "build.txt"
    write_install_lock(build_install_lock, build_pins, build_wheels)
    build_python = create_locked_build_environment(
        work / "build-venv", build_downloads, build_install_lock
    )

    source_archives_out = output / "source-archives"
    source_archives_out.mkdir()
    openssl_lock = source_lock["openssl"]
    openssl_archive = (
        work / "source-archives" / openssl_lock["filename"]
    )
    download_locked_url(
        openssl_lock["url"],
        openssl_archive,
        openssl_lock["sha256"],
    )
    openssl_archive_output = copy_unique(
        openssl_archive, source_archives_out
    )
    openssl_prefix = work / "openssl-prefix"
    openssl_evidence = build_locked_openssl(
        openssl_archive,
        openssl_prefix,
        openssl_lock,
        source_date_epoch=int(source_lock["toolchain"]["source_date_epoch"]),
    )
    openssl_evidence["source_archive"] = (
        openssl_archive_output.relative_to(output).as_posix()
    )

    native_build_directory = work / "native-wheels"
    native_build_directory.mkdir()
    cargo_home = work / "cargo-home"
    cargo_home.mkdir()
    source_builds: list[dict[str, Any]] = []
    native_wheels: dict[str, WheelEvidence] = {}

    source_date_epoch = int(source_lock["toolchain"]["source_date_epoch"])
    for package in source_lock["packages"]:
        normalized = normalize_distribution(package["name"])
        archive = work / "source-archives" / package["filename"]
        download_locked_url(package["url"], archive, package["sha256"])
        archive_output = copy_unique(archive, source_archives_out)
        source_root = safe_extract_sdist(
            archive, work / "extracted" / normalized
        )

        lock_template = package.get("cargo_lock_template")
        if lock_template:
            template = BACKEND_DIR / str(lock_template)
            if template != TIKTOKEN_CARGO_LOCK:
                raise SupplyChainError(
                    f"{normalized}: unapproved Cargo.lock template"
                )
            shutil.copy2(template, source_root / package["cargo_lock_path"])

        vendor_evidence = prepare_cargo_vendor(
            source_root,
            cargo_home=cargo_home,
            expected_lock_sha256=package["cargo_lock_sha256"],
        )
        built = build_native_wheel(
            build_python,
            source_root,
            native_build_directory,
            cargo_home=cargo_home,
            source_date_epoch=source_date_epoch,
            extra_environment=(
                {
                    "OPENSSL_DIR": str(openssl_prefix),
                    "OPENSSL_STATIC": "1",
                }
                if normalized == "cryptography"
                else None
            ),
        )
        validate_native_source_wheel(built, package)
        if normalized == "cryptography":
            openssl_evidence["runtime_version"] = verify_bundled_openssl(
                build_python,
                built,
                expected_version=openssl_lock["version"],
            )
        if normalized in native_wheels:
            raise SupplyChainError(f"duplicate native wheel for {normalized}")
        native_wheels[normalized] = built
        source_builds.append(
            {
                **vendor_evidence,
                "built_wheel_sha256": built.sha256,
                "name": normalized,
                "source_archive": archive_output.relative_to(output).as_posix(),
                "source_sha256": package["sha256"],
                "version": package["version"],
            }
        )

    production_out = output / "production-wheelhouse"
    _copy_wheel_group(production_downloads, production_out)
    for wheel in native_wheels.values():
        copy_unique(wheel.path, production_out)
    build_out = output / "build-wheelhouse"
    release_out = output / "release-tools-wheelhouse"
    _copy_wheel_group(build_downloads, build_out)
    _copy_wheel_group(release_downloads, release_out)

    production_wheels = verify_wheel_directory(production_out, production_pins)
    build_wheels_out = verify_wheel_directory(build_out, build_pins)
    release_wheels_out = verify_wheel_directory(release_out, release_pins)

    install_locks_out = output / "install-locks"
    install_locks_out.mkdir()
    production_install_lock = (
        install_locks_out / "requirements-windows-arm64-install.txt"
    )
    build_install_lock_out = (
        install_locks_out / "requirements-windows-arm64-build-install.txt"
    )
    release_install_lock = (
        install_locks_out
        / "requirements-release-tools-windows-arm64-install.txt"
    )
    write_install_lock(production_install_lock, production_pins, production_wheels)
    write_install_lock(build_install_lock_out, build_pins, build_wheels_out)
    write_install_lock(release_install_lock, release_pins, release_wheels_out)

    offline_install_dry_run(
        build_python, production_out, production_install_lock
    )
    offline_install_dry_run(
        build_python, release_out, release_install_lock
    )

    wheel_groups = {
        "build": build_wheels_out,
        "production": production_wheels,
        "release_tools": release_wheels_out,
    }
    content = {
        "contract": {
            "build_network": (
                "Only hash-locked PyPI files and Cargo.lock-checksummed "
                "crates may be fetched; native compilation is offline."
            ),
            "id": "suxiaoyou-cpython-3.12.10-windows-arm64-v1",
            "install_command": (
                "python -m pip install --no-index --no-deps "
                "--require-hashes --only-binary=:all: "
                "--find-links=<wheelhouse> -r <derived-install-lock>"
            ),
            "manifest_attestation": (
                "Tag releases must compare content_sha256 with the tracked "
                "out-of-band approval lock."
            ),
            "pe_machine": hex(PE_MACHINE_ARM64),
            "platform_tag": EXPECTED_PLATFORM_TAG,
            "python": ".".join(map(str, EXPECTED_PYTHON)),
        },
        "files": file_records(output),
        "openssl_build": openssl_evidence,
        "source_builds": sorted(
            source_builds, key=lambda value: value["name"]
        ),
        "wheel_groups": _manifest_wheel_groups(output, wheel_groups),
    }
    content_sha256 = hashlib.sha256(canonical_json_bytes(content)).hexdigest()
    manifest = {
        "content": content,
        "content_sha256": content_sha256,
        "schema_version": 1,
        "toolchain": dict(sorted(toolchain_evidence.items())),
    }
    manifest_bytes = canonical_json_bytes(manifest)
    manifest_path = output / MANIFEST_NAME
    manifest_path.write_bytes(manifest_bytes)
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    (output / MANIFEST_DIGEST_NAME).write_text(
        f"{manifest_sha256}  {MANIFEST_NAME}\n",
        encoding="utf-8",
        newline="\n",
    )
    verify_artifact(
        output,
        expected_content_sha256=content_sha256,
        expected_manifest_sha256=manifest_sha256,
    )
    return manifest_sha256, content_sha256


def _safe_manifest_relative_path(value: str) -> Path:
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        raise SupplyChainError(f"unsafe manifest path: {value!r}")
    return Path(*pure.parts)


def _parse_digest_sidecar(path: Path) -> str:
    fields = path.read_text(encoding="utf-8").strip().split()
    if len(fields) != 2 or fields[1] != MANIFEST_NAME:
        raise SupplyChainError(f"invalid manifest digest sidecar: {path}")
    digest = fields[0].lower()
    if not _SHA256_RE.fullmatch(digest):
        raise SupplyChainError(f"invalid manifest SHA-256 in {path}")
    return digest


def verify_artifact(
    output: Path,
    *,
    expected_content_sha256: str,
    expected_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    expected_content_sha256 = expected_content_sha256.lower()
    if not _SHA256_RE.fullmatch(expected_content_sha256):
        raise SupplyChainError("expected content SHA-256 must be 64 hex digits")
    if expected_manifest_sha256 is not None:
        expected_manifest_sha256 = expected_manifest_sha256.lower()
        if not _SHA256_RE.fullmatch(expected_manifest_sha256):
            raise SupplyChainError(
                "expected manifest SHA-256 must be 64 hex digits"
            )
    if not output.is_dir() or output.is_symlink():
        raise SupplyChainError(f"artifact directory is missing or unsafe: {output}")

    manifest_path = output / MANIFEST_NAME
    digest_path = output / MANIFEST_DIGEST_NAME
    actual_manifest_sha256 = sha256_file(manifest_path)
    sidecar_sha256 = _parse_digest_sidecar(digest_path)
    if actual_manifest_sha256 != sidecar_sha256:
        raise SupplyChainError(
            "manifest SHA-256 does not match its sidecar"
        )
    if (
        expected_manifest_sha256 is not None
        and actual_manifest_sha256 != expected_manifest_sha256
    ):
        raise SupplyChainError(
            f"manifest attestation mismatch: expected "
            f"{expected_manifest_sha256}, got {actual_manifest_sha256}"
        )

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SupplyChainError(f"invalid manifest JSON: {exc}") from exc
    if manifest.get("schema_version") != 1:
        raise SupplyChainError("unsupported wheelhouse manifest schema")
    if canonical_json_bytes(manifest) != manifest_path.read_bytes():
        raise SupplyChainError("manifest is not in canonical JSON form")

    content = manifest.get("content")
    if not isinstance(content, dict):
        raise SupplyChainError("manifest content must be an object")
    content_sha256 = hashlib.sha256(canonical_json_bytes(content)).hexdigest()
    if content_sha256 != manifest.get("content_sha256"):
        raise SupplyChainError("manifest content SHA-256 is internally invalid")
    if content_sha256 != expected_content_sha256:
        raise SupplyChainError(
            f"content approval mismatch: expected {expected_content_sha256}, "
            f"got {content_sha256}"
        )

    records = content.get("files")
    if not isinstance(records, list):
        raise SupplyChainError("manifest files must be an array")
    recorded: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise SupplyChainError("manifest file record must be an object")
        relative_value = record.get("path")
        if not isinstance(relative_value, str):
            raise SupplyChainError("manifest file path must be a string")
        relative = _safe_manifest_relative_path(relative_value)
        if relative_value in recorded:
            raise SupplyChainError(f"duplicate manifest path: {relative_value}")
        recorded.add(relative_value)
        path = output / relative
        if not path.is_file() or path.is_symlink():
            raise SupplyChainError(f"recorded artifact is missing: {relative_value}")
        if path.stat().st_size != record.get("size"):
            raise SupplyChainError(f"artifact size drift: {relative_value}")
        if sha256_file(path) != record.get("sha256"):
            raise SupplyChainError(f"artifact hash drift: {relative_value}")

    actual_files = {
        path.relative_to(output).as_posix()
        for path in output.rglob("*")
        if path.is_file()
        and path.name not in {MANIFEST_NAME, MANIFEST_DIGEST_NAME}
    }
    if actual_files != recorded:
        raise SupplyChainError(
            f"artifact file-set drift; missing={sorted(recorded - actual_files)}, "
            f"extra={sorted(actual_files - recorded)}"
        )

    group_contracts = {
        "build": (
            output / "build-wheelhouse",
            output
            / "install-locks"
            / "requirements-windows-arm64-build-install.txt",
        ),
        "production": (
            output / "production-wheelhouse",
            output / "install-locks" / "requirements-windows-arm64-install.txt",
        ),
        "release_tools": (
            output / "release-tools-wheelhouse",
            output
            / "install-locks"
            / "requirements-release-tools-windows-arm64-install.txt",
        ),
    }
    manifest_groups = content.get("wheel_groups")
    if not isinstance(manifest_groups, dict):
        raise SupplyChainError("manifest wheel_groups must be an object")
    for group_name, (wheelhouse, install_lock) in group_contracts.items():
        pins = parse_requirement_lock(install_lock)
        wheels = verify_wheel_directory(wheelhouse, pins)
        recomputed = [
            wheel.to_manifest(output)
            for wheel in sorted(
                wheels.values(), key=lambda item: item.normalized_name
            )
        ]
        if recomputed != manifest_groups.get(group_name):
            raise SupplyChainError(
                f"manifest wheel evidence drift in group {group_name}"
            )
    if set(manifest_groups) != set(group_contracts):
        raise SupplyChainError("manifest has unknown wheel groups")
    return manifest


def approved_content_sha256(path: Path) -> str | None:
    try:
        approval = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SupplyChainError(f"cannot read approval lock {path}: {exc}") from exc
    if approval.get("schema_version") != 1:
        raise SupplyChainError(f"{path}: unsupported approval schema")
    if (
        approval.get("contract_id")
        != "suxiaoyou-cpython-3.12.10-windows-arm64-v1"
    ):
        raise SupplyChainError(f"{path}: approval contract drift")
    digest = str(approval.get("content_sha256", "")).lower()
    if not _SHA256_RE.fullmatch(digest):
        raise SupplyChainError(f"{path}: invalid content SHA-256")
    if digest == "0" * 64:
        if approval.get("status") != "bootstrap-required":
            raise SupplyChainError(
                f"{path}: zero digest is only valid for bootstrap-required"
            )
        return None
    if approval.get("status") != "approved":
        raise SupplyChainError(
            f"{path}: nonzero digest requires approved status"
        )
    return digest


def build_artifact(
    output: Path,
    *,
    expected_content_sha256: str | None,
    expected_manifest_sha256: str | None,
    approval_file: Path,
    bootstrap: bool,
) -> tuple[str, str]:
    output = output.expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise SupplyChainError(
            f"output must not already exist (prevents stale wheel drift): {output}"
        )
    if bootstrap and expected_content_sha256 is not None:
        raise SupplyChainError(
            "--bootstrap and --expected-content-sha256 are mutually exclusive"
        )
    approved = approved_content_sha256(approval_file)
    if expected_content_sha256 is None and not bootstrap:
        expected_content_sha256 = approved
        if expected_content_sha256 is None:
            raise SupplyChainError(
                f"{approval_file} has no approved content digest; run a "
                "manual --bootstrap build, review it, then commit its "
                "content_sha256 before a tag release"
            )
    if expected_content_sha256 is not None:
        expected_content_sha256 = expected_content_sha256.lower()
        if (
            not _SHA256_RE.fullmatch(expected_content_sha256)
            or expected_content_sha256 == "0" * 64
        ):
            raise SupplyChainError(
                "expected content SHA-256 must be a nonzero 64-hex digest"
            )
    if expected_manifest_sha256 is not None:
        expected_manifest_sha256 = expected_manifest_sha256.lower()
        if not _SHA256_RE.fullmatch(expected_manifest_sha256):
            raise SupplyChainError(
                "expected manifest SHA-256 must be a 64-hex digest"
            )
    output.parent.mkdir(parents=True, exist_ok=True)
    source_lock = load_sources_lock()
    toolchain_evidence = preflight_native_builder(source_lock)

    staging = output.parent / f".{output.name}.staging-{uuid.uuid4().hex}"
    try:
        with tempfile.TemporaryDirectory(
            prefix=f".{output.name}.work-", dir=output.parent
        ) as work_name:
            manifest_sha256, content_sha256 = build_into(
                staging,
                Path(work_name),
                source_lock,
                toolchain_evidence,
            )
        if (
            expected_content_sha256 is not None
            and content_sha256 != expected_content_sha256
        ):
            raise SupplyChainError(
                f"new wheelhouse content {content_sha256} does not match "
                f"approved {expected_content_sha256}"
            )
        if (
            expected_manifest_sha256 is not None
            and manifest_sha256 != expected_manifest_sha256
        ):
            raise SupplyChainError(
                f"new wheelhouse manifest {manifest_sha256} does not match "
                f"expected {expected_manifest_sha256}"
            )
        os.replace(staging, output)
        return manifest_sha256, content_sha256
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def create_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser(
        "build", help="build a new atomic Windows ARM64 wheelhouse artifact"
    )
    build.add_argument(
        "--output",
        required=True,
        type=Path,
        help="new output directory; existing paths are rejected",
    )
    build.add_argument(
        "--expected-content-sha256",
        help=(
            "approved stable content digest; otherwise the tracked approval "
            "file is authoritative"
        ),
    )
    build.add_argument(
        "--expected-manifest-sha256",
        help="optional raw-manifest digest for exact runner attestation",
    )
    build.add_argument(
        "--approval-file",
        type=Path,
        default=APPROVAL_LOCK,
        help="tracked content approval contract",
    )
    build.add_argument(
        "--bootstrap",
        action="store_true",
        help=(
            "manual first-build mode; permits an unapproved content digest "
            "and prints the value that must be reviewed and committed"
        ),
    )

    verify = subparsers.add_parser(
        "verify", help="verify an existing artifact against a trusted digest"
    )
    verify.add_argument("--output", required=True, type=Path)
    verify.add_argument(
        "--expected-content-sha256",
        required=True,
        help="out-of-band approved stable content SHA-256",
    )
    verify.add_argument(
        "--expected-manifest-sha256",
        help="optional raw-manifest SHA-256",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = create_argument_parser().parse_args(argv)
    try:
        if args.command == "build":
            manifest_digest, content_digest = build_artifact(
                args.output,
                expected_content_sha256=args.expected_content_sha256,
                expected_manifest_sha256=args.expected_manifest_sha256,
                approval_file=args.approval_file,
                bootstrap=args.bootstrap,
            )
            print(
                "WINDOWS_ARM64_WHEELHOUSE_CONTENT_SHA256="
                f"{content_digest}"
            )
            print(
                "WINDOWS_ARM64_WHEELHOUSE_MANIFEST_SHA256="
                f"{manifest_digest}"
            )
            return 0
        verify_artifact(
            args.output,
            expected_content_sha256=args.expected_content_sha256,
            expected_manifest_sha256=args.expected_manifest_sha256,
        )
        print(
            "WINDOWS_ARM64_WHEELHOUSE_VERIFIED_CONTENT_SHA256="
            f"{args.expected_content_sha256.lower()}"
        )
        return 0
    except (OSError, SupplyChainError, subprocess.SubprocessError) as exc:
        print(f"windows-arm64 wheelhouse error: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
