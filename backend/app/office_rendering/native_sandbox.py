"""Strict declarative contract for platform-native Office sandbox launchers.

The contract proves only that a release-owned native launcher declaration is
canonical, attestation-bound, and present in the verified native dependency
inventory.  It never treats manifest claims as behavioral evidence.  Release
publication must separately prove the native launcher actually denies network
access, restricts host filesystem writes, and contains descendants on its
target operating system.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from types import MappingProxyType
from typing import Any, Final, Mapping, Sequence


SANDBOX_MANIFEST_FILENAME: Final = "sandbox-manifest.json"
SANDBOX_MANIFEST_SCHEMA_VERSION: Final = 1
SANDBOX_LAUNCH_CONTRACT_SCHEMA_VERSION: Final = 1
MAX_SANDBOX_MANIFEST_BYTES: Final = 64 * 1024
MAX_DEPENDENCY_MANIFEST_BYTES: Final = 2 * 1024 * 1024
MAX_LAUNCHER_BYTES: Final = 1024 * 1024 * 1024

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TARGET = re.compile(r"^(?:darwin|linux|windows)-(?:arm64|x64)$")
_DEPENDENCY_MANIFEST_FILENAME: Final = "dependency-manifest.json"
_COMPONENT_SANDBOX: Final = "sandbox-manifest"
_COMPONENT_DEPENDENCY: Final = "dependency-manifest"
_COMPONENT_BUNDLE_TREE: Final = "bundle-tree"

_COMMON_CAPABILITIES: Final = frozenset(
    {
        "host_filesystem_read_only",
        "network_denied",
        "private_input_read_only",
        "private_output_write_only",
        "process_tree_contained",
    }
)
_PLATFORM_CONTRACTS: Final = {
    "darwin": (
        "suxiaoyou.office-sandbox.macos-app-sandbox-xpc.v1",
        PurePosixPath("bin/suxiaoyou-office-sandbox-launcher"),
        _COMMON_CAPABILITIES | {"app_sandbox", "xpc_service"},
    ),
    "windows": (
        "suxiaoyou.office-sandbox.windows-appcontainer-restricted-token.v1",
        PurePosixPath("bin/suxiaoyou-office-sandbox-launcher.exe"),
        _COMMON_CAPABILITIES
        | {"app_container", "kill_on_close_job", "restricted_token"},
    ),
    "linux": (
        "suxiaoyou.office-sandbox.linux-namespaces-seccomp-cgroup.v1",
        PurePosixPath("bin/suxiaoyou-office-sandbox-launcher"),
        _COMMON_CAPABILITIES
        | {
            "cgroup",
            "mount_namespace",
            "network_namespace",
            "seccomp",
            "user_namespace",
        },
    ),
}


class NativeSandboxContractError(RuntimeError):
    """A native sandbox launcher declaration is not release-safe."""


@dataclass(frozen=True, slots=True)
class NativeSandboxContract:
    """Validated declaration plus no-shell launcher construction boundary."""

    platform_target: str
    contract_id: str
    capabilities: frozenset[str]
    sandbox_manifest_sha256: str
    dependency_manifest_sha256: str
    bundle_tree_sha256: str
    launcher_sha256: str
    _root: Path = field(repr=False, compare=False)
    _launcher_path: Path = field(repr=False, compare=False)
    _native_executables: Mapping[Path, tuple[str, int]] = field(
        repr=False,
        compare=False,
    )

    def build_no_shell_argv(
        self,
        inner_argv: Sequence[str],
        *,
        work_dir: Path,
    ) -> tuple[str, ...]:
        """Wrap one native renderer argv without a shell or ambient lookup."""

        args = tuple(inner_argv)
        if (
            not args
            or any(not isinstance(value, str) or not value for value in args)
            or any("\x00" in value or len(value) > 32_768 for value in args)
        ):
            raise NativeSandboxContractError("native sandbox inner argv is invalid")
        executable = _canonical_existing_file(Path(args[0]), label="inner executable")
        executable_identity = self._native_executables.get(executable)
        if executable == self._launcher_path or executable_identity is None:
            raise NativeSandboxContractError(
                "native sandbox inner executable is outside the native closure"
            )
        _validate_native_executable_file(
            executable,
            root=self._root,
            expected_sha256=executable_identity[0],
            expected_size=executable_identity[1],
        )
        work = _canonical_private_work_directory(Path(work_dir))
        if _contains(self._root, work) or _contains(work, self._root):
            raise NativeSandboxContractError(
                "native sandbox work directory overlaps renderer bundle"
            )
        _validate_native_executable_file(
            self._launcher_path,
            root=self._root,
            expected_sha256=self.launcher_sha256,
            expected_size=self._native_executables[self._launcher_path][1],
        )
        return (
            str(self._launcher_path),
            "--contract-id",
            self.contract_id,
            "--sandbox-manifest-sha256",
            self.sandbox_manifest_sha256,
            "--work-dir",
            str(work),
            "--",
            *args,
        )

    def path_free_evidence(self) -> dict[str, Any]:
        """Return declaration evidence that explicitly remains unproven."""

        return {
            "schema_version": SANDBOX_LAUNCH_CONTRACT_SCHEMA_VERSION,
            "status": "declared-not-proven",
            "platform_target": self.platform_target,
            "contract_id": self.contract_id,
            "capabilities": sorted(self.capabilities),
            "sandbox_manifest_sha256": self.sandbox_manifest_sha256,
            "dependency_manifest_sha256": self.dependency_manifest_sha256,
            "bundle_tree_sha256": self.bundle_tree_sha256,
            "launcher_sha256": self.launcher_sha256,
            "native_behavior_proven": False,
            "adversarial_evidence_required": True,
        }


def load_native_sandbox_contract(
    root: Path,
    *,
    platform_target: str,
    attested_components: Mapping[str, str],
) -> NativeSandboxContract:
    """Load a canonical, attestation-bound native sandbox declaration.

    ``attested_components`` must come from an already signature-verified
    renderer attestation.  This function re-reads both manifests and the
    launcher, then compares their identities with those signed components.
    The complete binary format/import closure remains the responsibility of
    :func:`app.office_rendering.native_bundle.verify_native_bundle`.
    """

    contract_id, launcher_relative, capabilities = _target_contract(platform_target)
    private_root = _canonical_private_directory(Path(root), label="renderer bundle")
    signed = _attested_component_identities(attested_components)

    sandbox_bytes = _read_manifest(
        private_root,
        SANDBOX_MANIFEST_FILENAME,
        max_bytes=MAX_SANDBOX_MANIFEST_BYTES,
    )
    sandbox_sha256 = hashlib.sha256(sandbox_bytes).hexdigest()
    if sandbox_sha256 != signed[_COMPONENT_SANDBOX]:
        raise NativeSandboxContractError(
            "native sandbox manifest does not match attestation"
        )
    sandbox_manifest = _decode_canonical_json(
        sandbox_bytes,
        label="native sandbox manifest",
    )
    _validate_sandbox_manifest(
        sandbox_manifest,
        platform_target=platform_target,
        contract_id=contract_id,
        launcher_path=launcher_relative,
        capabilities=capabilities,
    )

    dependency_bytes = _read_manifest(
        private_root,
        _DEPENDENCY_MANIFEST_FILENAME,
        max_bytes=MAX_DEPENDENCY_MANIFEST_BYTES,
    )
    dependency_sha256 = hashlib.sha256(dependency_bytes).hexdigest()
    if dependency_sha256 != signed[_COMPONENT_DEPENDENCY]:
        raise NativeSandboxContractError(
            "native dependency manifest does not match attestation"
        )
    dependency_manifest = _decode_canonical_json(
        dependency_bytes,
        label="native dependency manifest",
    )
    launcher_record, executable_records = _native_executable_inventory(
        dependency_manifest,
        platform_target=platform_target,
        launcher_path=launcher_relative,
    )

    launcher_path = _path_under(private_root, launcher_relative)
    launcher_sha256 = _validate_native_executable_file(
        launcher_path,
        root=private_root,
        expected_sha256=launcher_record["sha256"],
        expected_size=launcher_record["size"],
    )
    native_executables = MappingProxyType(
        {
            _path_under(private_root, relative): (sha256, size)
            for relative, sha256, size in executable_records
        }
    )
    if launcher_path not in native_executables:
        raise NativeSandboxContractError(
            "native sandbox launcher is outside dependency closure"
        )

    return NativeSandboxContract(
        platform_target=platform_target,
        contract_id=contract_id,
        capabilities=capabilities,
        sandbox_manifest_sha256=sandbox_sha256,
        dependency_manifest_sha256=dependency_sha256,
        bundle_tree_sha256=signed[_COMPONENT_BUNDLE_TREE],
        launcher_sha256=launcher_sha256,
        _root=private_root,
        _launcher_path=launcher_path,
        _native_executables=native_executables,
    )


def _target_contract(
    platform_target: object,
) -> tuple[str, PurePosixPath, frozenset[str]]:
    if not isinstance(platform_target, str) or _TARGET.fullmatch(platform_target) is None:
        raise NativeSandboxContractError("native sandbox target is unsupported")
    family = platform_target.split("-", 1)[0]
    contract = _PLATFORM_CONTRACTS.get(family)
    if contract is None:
        raise NativeSandboxContractError("native sandbox target is unsupported")
    contract_id, launcher_path, capabilities = contract
    return contract_id, launcher_path, frozenset(capabilities)


def _attested_component_identities(
    value: Mapping[str, str],
) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise NativeSandboxContractError("native sandbox attestation binding is invalid")
    required = {
        _COMPONENT_SANDBOX,
        _COMPONENT_DEPENDENCY,
        _COMPONENT_BUNDLE_TREE,
    }
    if not required.issubset(value):
        raise NativeSandboxContractError("native sandbox attestation binding is incomplete")
    result: dict[str, str] = {}
    for name in required:
        digest = value.get(name)
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise NativeSandboxContractError(
                "native sandbox attestation identity is invalid"
            )
        result[name] = digest
    return result


def _validate_sandbox_manifest(
    value: Mapping[str, Any],
    *,
    platform_target: str,
    contract_id: str,
    launcher_path: PurePosixPath,
    capabilities: frozenset[str],
) -> None:
    if set(value) != {
        "capabilities",
        "contract_id",
        "launcher_path",
        "platform_target",
        "schema_version",
    }:
        raise NativeSandboxContractError("native sandbox manifest schema is invalid")
    if type(value.get("schema_version")) is not int or value.get(
        "schema_version"
    ) != SANDBOX_MANIFEST_SCHEMA_VERSION:
        raise NativeSandboxContractError("native sandbox manifest schema is invalid")
    if value.get("platform_target") != platform_target:
        raise NativeSandboxContractError("native sandbox manifest target does not match")
    if value.get("contract_id") != contract_id:
        raise NativeSandboxContractError("native sandbox contract id is invalid")
    if value.get("launcher_path") != launcher_path.as_posix():
        raise NativeSandboxContractError("native sandbox launcher path is invalid")
    declared = value.get("capabilities")
    if (
        not isinstance(declared, dict)
        or set(declared) != capabilities
        or any(flag is not True for flag in declared.values())
    ):
        raise NativeSandboxContractError("native sandbox capabilities are invalid")


def _native_executable_inventory(
    value: Mapping[str, Any],
    *,
    platform_target: str,
    launcher_path: PurePosixPath,
) -> tuple[dict[str, Any], tuple[tuple[PurePosixPath, str, int], ...]]:
    if set(value) != {"files", "platform_target", "schema_version"}:
        raise NativeSandboxContractError("native dependency manifest schema is invalid")
    if type(value.get("schema_version")) is not int or value.get("schema_version") != 1:
        raise NativeSandboxContractError("native dependency manifest schema is invalid")
    if value.get("platform_target") != platform_target:
        raise NativeSandboxContractError("native dependency manifest target does not match")
    files = value.get("files")
    if not isinstance(files, list) or not files:
        raise NativeSandboxContractError("native dependency manifest files are invalid")
    executable_records: list[tuple[PurePosixPath, str, int]] = []
    ordered_paths: list[PurePosixPath] = []
    launcher: dict[str, Any] | None = None
    seen: set[PurePosixPath] = set()
    for record in files:
        if not isinstance(record, dict) or set(record) != {
            "dependencies",
            "kind",
            "path",
            "sha256",
            "size",
        }:
            raise NativeSandboxContractError("native dependency file is invalid")
        path = _relative_path(record.get("path"))
        if path in seen:
            raise NativeSandboxContractError("native dependency paths are duplicated")
        seen.add(path)
        ordered_paths.append(path)
        kind = record.get("kind")
        size = record.get("size")
        sha256 = record.get("sha256")
        if (
            kind not in {"executable", "library"}
            or type(size) is not int
            or not 1 <= size <= MAX_LAUNCHER_BYTES
            or not isinstance(sha256, str)
            or _SHA256.fullmatch(sha256) is None
            or not isinstance(record.get("dependencies"), list)
        ):
            raise NativeSandboxContractError("native dependency file is invalid")
        if kind == "executable":
            executable_records.append((path, sha256, size))
        if path == launcher_path:
            if kind != "executable" or launcher is not None:
                raise NativeSandboxContractError(
                    "native sandbox launcher is not an executable closure entry"
                )
            launcher = record
    if tuple(path.as_posix() for path in ordered_paths) != tuple(
        sorted(path.as_posix() for path in ordered_paths)
    ):
        # Canonical JSON sorts object keys, not array entries.  Dependency
        # records themselves must retain the native_bundle path ordering.
        raise NativeSandboxContractError("native dependency files are not canonical")
    if launcher is None:
        raise NativeSandboxContractError(
            "native sandbox launcher is absent from dependency closure"
        )
    return launcher, tuple(executable_records)


def _read_manifest(root: Path, filename: str, *, max_bytes: int) -> bytes:
    return _read_private_file(_path_under(root, PurePosixPath(filename)), root, max_bytes)


def _decode_canonical_json(raw: bytes, *, label: str) -> dict[str, Any]:
    try:
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_no_duplicate_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise NativeSandboxContractError(f"{label} is invalid") from exc
    if not isinstance(decoded, dict) or raw != _canonical_json_bytes(decoded):
        raise NativeSandboxContractError(f"{label} is not canonical")
    return decoded


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
        raise NativeSandboxContractError("native sandbox manifest is invalid") from exc


def _no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _relative_path(value: object) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise NativeSandboxContractError("native dependency path is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise NativeSandboxContractError("native dependency path is invalid")
    return path


def _canonical_private_directory(path: Path, *, label: str) -> Path:
    if not path.is_absolute():
        raise NativeSandboxContractError(f"{label} path is invalid")
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise NativeSandboxContractError(f"{label} is unavailable") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or resolved != path
    ):
        raise NativeSandboxContractError(f"{label} is redirected")
    _reject_unsafe_mode(info, label)
    return resolved


def _canonical_existing_file(path: Path, *, label: str) -> Path:
    if not path.is_absolute():
        raise NativeSandboxContractError(f"{label} path is invalid")
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise NativeSandboxContractError(f"{label} is unavailable") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or resolved != path
    ):
        raise NativeSandboxContractError(f"{label} is redirected")
    _reject_unsafe_mode(info, label)
    return resolved


def _canonical_private_work_directory(path: Path) -> Path:
    resolved = _canonical_private_directory(path, label="sandbox work directory")
    if os.name != "nt":
        info = resolved.lstat()
        if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise NativeSandboxContractError(
                "sandbox work directory permissions are not private"
            )
        required = stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
        if info.st_mode & required != required:
            raise NativeSandboxContractError(
                "sandbox work directory permissions are invalid"
            )
    return resolved


def _path_under(root: Path, relative: PurePosixPath) -> Path:
    candidate = root.joinpath(*relative.parts)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise NativeSandboxContractError("native sandbox path escaped bundle") from exc
    return candidate


def _read_private_file(path: Path, root: Path, max_bytes: int) -> bytes:
    secured = _canonical_existing_file(path, label="native sandbox file")
    try:
        secured.relative_to(root)
    except ValueError as exc:
        raise NativeSandboxContractError("native sandbox file escaped bundle") from exc
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(secured, flags)
    except OSError as exc:
        raise NativeSandboxContractError("native sandbox file is unavailable") from exc
    chunks: list[bytes] = []
    total = 0
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise NativeSandboxContractError("native sandbox file exceeds its budget")
        while chunk := os.read(descriptor, 8192):
            total += len(chunk)
            if total > max_bytes:
                raise NativeSandboxContractError("native sandbox file exceeds its budget")
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    visible = path.lstat()
    if (
        total != before.st_size
        or _stat_identity(before) != _stat_identity(after)
        or _stat_identity(after) != _stat_identity(visible)
        or stat.S_ISLNK(visible.st_mode)
    ):
        raise NativeSandboxContractError("native sandbox file changed")
    return b"".join(chunks)


def _validate_native_executable_file(
    path: Path,
    *,
    root: Path,
    expected_sha256: str,
    expected_size: int,
) -> str:
    actual_sha256, actual_size = _hash_private_file(
        path,
        root,
        MAX_LAUNCHER_BYTES,
    )
    if actual_sha256 != expected_sha256 or actual_size != expected_size:
        raise NativeSandboxContractError("native sandbox executable identity changed")
    try:
        info = path.lstat()
    except OSError as exc:
        raise NativeSandboxContractError("native sandbox launcher is unavailable") from exc
    if os.name != "nt" and not info.st_mode & stat.S_IXUSR:
        raise NativeSandboxContractError("native sandbox launcher is not executable")
    _reject_unsafe_mode(info, "native sandbox launcher")
    return actual_sha256


def _hash_private_file(path: Path, root: Path, max_bytes: int) -> tuple[str, int]:
    secured = _canonical_existing_file(path, label="native sandbox executable")
    try:
        secured.relative_to(root)
    except ValueError as exc:
        raise NativeSandboxContractError(
            "native sandbox executable escaped bundle"
        ) from exc
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(secured, flags)
    except OSError as exc:
        raise NativeSandboxContractError(
            "native sandbox executable is unavailable"
        ) from exc
    digest = hashlib.sha256()
    total = 0
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise NativeSandboxContractError(
                "native sandbox executable exceeds its budget"
            )
        while chunk := os.read(descriptor, 1024 * 1024):
            total += len(chunk)
            if total > max_bytes:
                raise NativeSandboxContractError(
                    "native sandbox executable exceeds its budget"
                )
            digest.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    visible = path.lstat()
    if (
        total != before.st_size
        or _stat_identity(before) != _stat_identity(after)
        or _stat_identity(after) != _stat_identity(visible)
        or stat.S_ISLNK(visible.st_mode)
    ):
        raise NativeSandboxContractError("native sandbox executable changed")
    return digest.hexdigest(), total


def _reject_unsafe_mode(info: os.stat_result, label: str) -> None:
    if os.name != "nt" and info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise NativeSandboxContractError(f"{label} permissions are unsafe")


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _contains(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


__all__ = [
    "MAX_SANDBOX_MANIFEST_BYTES",
    "NativeSandboxContract",
    "NativeSandboxContractError",
    "SANDBOX_LAUNCH_CONTRACT_SCHEMA_VERSION",
    "SANDBOX_MANIFEST_FILENAME",
    "SANDBOX_MANIFEST_SCHEMA_VERSION",
    "load_native_sandbox_contract",
]
