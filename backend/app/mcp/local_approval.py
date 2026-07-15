"""Connection-level trust for local stdio MCP subprocesses.

The tool-level permission model runs only after an MCP server has started and
advertised its tools.  A local MCP command can execute arbitrary code during
startup, so its launch configuration needs a separate, durable approval before
the transport is allowed to spawn it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.client.stdio import DEFAULT_INHERITED_ENV_VARS, get_default_environment


LOCAL_APPROVAL_STATE_VERSION = 1
_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_NPM_EXACT_PACKAGE_RE = re.compile(
    r"^(?:@[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+|[A-Za-z0-9_.-]+)"
    r"@[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$"
)
_PYPI_EXACT_PACKAGE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9_,.-]+\])?"
    r"==[A-Za-z0-9][A-Za-z0-9.!+_-]*$"
)
_MAX_APPROVAL_FILE_BYTES = 1024 * 1024
_MAX_EXECUTABLE_BYTES = 512 * 1024 * 1024
_WINDOWS_REPARSE_POINT = 0x400


class LocalMcpApprovalRequired(PermissionError):
    """Raised before spawn when the exact local launch is not approved."""


class LocalMcpApprovalStoreError(RuntimeError):
    """Raised when the private approval store cannot be updated safely."""


@dataclass(frozen=True)
class LocalMcpApprovalResult:
    """Truthful outcome of persisting approval and attempting connection."""

    approval_persisted: bool
    connected: bool
    status: str
    error: str | None = None
    duplicate: bool = False


@dataclass(frozen=True)
class LocalMcpLaunchSpec:
    """Validated snapshot of every value that affects a stdio process launch."""

    # ``command`` is the command that is actually passed to the MCP SDK.  Its
    # first element is always an absolute, canonical executable path.
    command: tuple[str, ...]
    requested_command: tuple[str, ...]
    environment: dict[str, str]
    cwd: str
    executable_path: str
    executable_sha256: str
    fingerprint: str

    def public_descriptor(self) -> dict[str, Any]:
        """Return review metadata without exposing environment values."""

        return {
            "fingerprint": self.fingerprint,
            "command": list(self.command),
            "cwd": self.cwd,
            "environment_keys": sorted(self.environment),
            "executable_path": self.executable_path,
            "executable_sha256": self.executable_sha256,
        }


def _effective_environment(configured: dict[str, str]) -> dict[str, str]:
    """Freeze every value the MCP SDK may otherwise add at spawn time."""

    defaults = get_default_environment()
    # Supplying an explicit empty value is intentional: stdio_client merges
    # its current defaults under server.env.  Including every allow-listed key
    # prevents a value that appears after approval from leaking into spawn.
    effective = {
        key: defaults.get(key, "")
        for key in DEFAULT_INHERITED_ENV_VARS
    }
    effective.update(defaults)

    if os.name != "nt":
        effective.update(configured)
        return effective

    # Windows environment keys are case-insensitive.  Collapse collisions so
    # the approved snapshot cannot mean something different to CreateProcess.
    by_fold = {key.casefold(): key for key in effective}
    for key, value in configured.items():
        previous = by_fold.get(key.casefold())
        canonical_key = previous or key
        effective[canonical_key] = value
        by_fold[key.casefold()] = canonical_key
    return effective


def _environment_value(environment: dict[str, str], name: str) -> str:
    if os.name != "nt":
        return environment.get(name, "")
    folded = name.casefold()
    for key, value in environment.items():
        if key.casefold() == folded:
            return value
    return ""


def _validate_dynamic_package_pin(command: tuple[str, ...]) -> None:
    """Reject package-runner commands whose downloaded code can drift."""

    runner = Path(command[0]).name.casefold()
    for suffix in (".exe", ".cmd", ".bat"):
        if runner.endswith(suffix):
            runner = runner[: -len(suffix)]
            break
    args = list(command[1:])

    if runner == "uvx":
        try:
            from_index = args.index("--from")
            package = args[from_index + 1]
        except (ValueError, IndexError):
            raise ValueError(
                "uvx MCP commands must use --from package==exact-version"
            ) from None
        if not _PYPI_EXACT_PACKAGE_RE.fullmatch(package):
            raise ValueError(
                "uvx MCP packages must be pinned with package==exact-version"
            )

    if runner == "npx":
        packages: list[str] = []
        index = 0
        while index < len(args):
            arg = args[index]
            if arg in {"--package", "-p"} and index + 1 < len(args):
                packages.append(args[index + 1])
                index += 2
                continue
            if arg.startswith("--package="):
                packages.append(arg.split("=", 1)[1])
            elif not arg.startswith("-"):
                packages.append(arg)
                break
            index += 1
        if not packages or not all(
            _NPM_EXACT_PACKAGE_RE.fullmatch(package) for package in packages
        ):
            raise ValueError(
                "npx MCP packages must be pinned with package@exact-semver"
            )


def _windows_search_executable(
    value: str,
    *,
    cwd: Path,
    environment: dict[str, str],
) -> Path:
    """Resolve a Windows command using only the approved PATH/PATHEXT."""

    path_value = _environment_value(environment, "PATH")
    path_ext = _environment_value(environment, "PATHEXT") or ".COM;.EXE;.BAT;.CMD"
    extensions = [entry for entry in path_ext.split(";") if entry]
    raw = Path(value)
    has_path = raw.is_absolute() or any(separator in value for separator in ("/", "\\"))
    directories = (
        [cwd]
        if has_path
        else [
            Path(entry.strip('"'))
            for entry in path_value.split(os.pathsep)
            if entry.strip('"')
        ]
    )
    base = raw if raw.is_absolute() else None
    candidates: list[Path] = []
    for directory in directories:
        candidate = base if base is not None else directory / raw
        if candidate is None:
            continue
        candidates.append(candidate)
        if not candidate.suffix:
            candidates.extend(Path(f"{candidate}{extension}") for extension in extensions)
        if base is not None:
            break
    for candidate in candidates:
        try:
            info = candidate.lstat()
        except OSError:
            continue
        if getattr(info, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT:
            raise ValueError("Local MCP executable cannot be a reparse point")
        if stat.S_ISREG(info.st_mode):
            return candidate.resolve(strict=True)
    raise ValueError(f"Local MCP executable could not be resolved: {value}")


def _resolve_executable(
    value: str,
    *,
    cwd: Path,
    environment: dict[str, str],
) -> Path:
    if os.name == "nt":
        resolved = _windows_search_executable(value, cwd=cwd, environment=environment)
    else:
        raw = Path(value).expanduser()
        if raw.is_absolute() or "/" in value:
            candidate = raw if raw.is_absolute() else cwd / raw
        else:
            candidate = None
            for directory in _environment_value(environment, "PATH").split(os.pathsep):
                if not directory:
                    continue
                possible = Path(directory).expanduser() / value
                try:
                    info = possible.stat()
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode) and os.access(possible, os.X_OK):
                    candidate = possible
                    break
            if candidate is None:
                raise ValueError(f"Local MCP executable could not be resolved: {value}")
        try:
            # The SDK receives this canonical target rather than the mutable
            # PATH entry/symlink used to discover it.
            resolved = candidate.resolve(strict=True)
            info = resolved.lstat()
        except OSError as exc:
            raise ValueError(
                f"Local MCP executable could not be resolved: {value}"
            ) from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ValueError("Local MCP executable must resolve to a regular file")
        if not os.access(resolved, os.X_OK):
            raise ValueError("Local MCP executable is not executable")
        if info.st_mode & 0o022:
            raise ValueError("Local MCP executable cannot be group/world writable")
        current_uid = os.geteuid()
        if info.st_uid not in {0, current_uid}:
            raise ValueError("Local MCP executable has an unsafe owner")

    try:
        final_info = resolved.lstat()
    except OSError as exc:  # pragma: no cover - race after strict resolution
        raise ValueError("Local MCP executable disappeared during validation") from exc
    if not stat.S_ISREG(final_info.st_mode):
        raise ValueError("Local MCP executable must be a regular file")
    if getattr(final_info, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT:
        raise ValueError("Local MCP executable cannot be a reparse point")
    return resolved


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Local MCP executable must remain a regular file")
        if before.st_size > _MAX_EXECUTABLE_BYTES:
            raise ValueError("Local MCP executable is too large to validate safely")
        while chunk := os.read(fd, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(fd)
        current = path.lstat()
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        if identity_after != identity_before or (
            current.st_dev,
            current.st_ino,
        ) != (after.st_dev, after.st_ino):
            raise ValueError("Local MCP executable changed during validation")
    finally:
        os.close(fd)
    return digest.hexdigest()


def local_mcp_launch_spec(config: dict[str, Any]) -> LocalMcpLaunchSpec:
    """Canonicalise and fingerprint the exact effective stdio launch."""

    raw_command = config.get("command")
    if not isinstance(raw_command, (list, tuple)) or not raw_command:
        raise ValueError("Local MCP command must be a non-empty string array")
    if any(not isinstance(item, str) or not item for item in raw_command):
        raise ValueError("Local MCP command entries must be non-empty strings")
    requested_command = tuple(raw_command)
    _validate_dynamic_package_pin(requested_command)

    raw_environment = config.get("environment")
    if raw_environment is None:
        configured_environment: dict[str, str] = {}
    elif isinstance(raw_environment, dict) and all(
        isinstance(key, str)
        and bool(key)
        and "=" not in key
        and "\x00" not in key
        and isinstance(value, str)
        and "\x00" not in value
        for key, value in raw_environment.items()
    ):
        configured_environment = dict(raw_environment)
    else:
        raise ValueError("Local MCP environment must map non-empty strings to strings")
    effective_environment = _effective_environment(configured_environment)

    raw_cwd = config.get("cwd")
    try:
        if raw_cwd is None:
            cwd_path = Path.cwd().resolve(strict=True)
        elif isinstance(raw_cwd, str) and raw_cwd:
            cwd_path = Path(raw_cwd).expanduser().resolve(strict=True)
        else:
            raise ValueError("Local MCP cwd must be a non-empty string")
    except OSError as exc:
        raise ValueError("Local MCP cwd must resolve to an existing directory") from exc
    if not cwd_path.is_dir():
        raise ValueError("Local MCP cwd must resolve to an existing directory")
    cwd = str(cwd_path)

    executable = _resolve_executable(
        requested_command[0],
        cwd=cwd_path,
        environment=effective_environment,
    )
    executable_sha256 = _sha256_file(executable)
    command = (str(executable), *requested_command[1:])

    canonical = json.dumps(
        {
            "version": 2,
            "requested_command": list(requested_command),
            "command": list(command),
            "executable": {
                "path": str(executable),
                "sha256": executable_sha256,
            },
            "environment": effective_environment,
            "cwd": cwd,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    fingerprint = f"sha256:{hashlib.sha256(canonical).hexdigest()}"
    return LocalMcpLaunchSpec(
        command=command,
        requested_command=requested_command,
        environment=effective_environment,
        cwd=cwd,
        executable_path=str(executable),
        executable_sha256=executable_sha256,
        fingerprint=fingerprint,
    )


def valid_local_mcp_fingerprint(value: object) -> bool:
    return isinstance(value, str) and _FINGERPRINT_RE.fullmatch(value) is not None


def _decode_approval_state(data: bytes | None) -> dict[str, str]:
    if data is None:
        return {}
    payload = json.loads(data.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Approval state must be an object")
    if payload.get("version") != LOCAL_APPROVAL_STATE_VERSION:
        raise ValueError("Unsupported local MCP approval state version")
    approvals = payload.get("approvals")
    if not isinstance(approvals, dict) or any(
        not isinstance(name, str)
        or not name
        or len(name) > 160
        or not valid_local_mcp_fingerprint(fingerprint)
        for name, fingerprint in approvals.items()
    ):
        raise ValueError("Invalid local MCP approval state")
    return dict(approvals)


class LocalMcpApprovalStore:
    """Persist per-workspace launch fingerprints in app-private storage.

    The root is retained lexically (never resolved through links) and every
    component is checked again at each read/write.  POSIX operations use a
    no-follow directory descriptor so a redirected approval path cannot turn
    an approval into an arbitrary file write.
    """

    def __init__(
        self,
        project_dir: str | None = None,
        *,
        storage_root: str | Path | None = None,
    ) -> None:
        scope = str(Path(project_dir).expanduser().resolve()) if project_dir else "global"
        scope_hash = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:20]
        if storage_root is None:
            private_root = os.environ.get("SUXIAOYOU_PRIVATE_DATA_DIR") or os.getcwd()
            root_value = Path(private_root).expanduser() / "security" / "mcp-local-approvals"
        else:
            root_value = Path(storage_root).expanduser()
        # abspath is lexical; Path.resolve() here would erase the evidence of
        # an attacker-controlled linked ancestor.
        self._root = Path(os.path.abspath(os.fspath(root_value)))
        self._filename = f"{scope_hash}.json"
        self._path = self._root / self._filename
        self._approvals: dict[str, str] = {}
        self._degraded_reason: str | None = None
        self._root_identity: tuple[int, int] | None = None
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def degraded_reason(self) -> str | None:
        return self._degraded_reason

    def get(self, server_name: str) -> str | None:
        if self._degraded_reason is not None:
            return None
        try:
            if os.name == "nt":
                self._secure_windows_root()
                info = self._root.lstat()
                identity = (info.st_dev, info.st_ino)
                data = self._read_windows()
            else:
                root_fd = self._open_secure_posix_root()
                try:
                    info = os.fstat(root_fd)
                    identity = (info.st_dev, info.st_ino)
                    data = self._read_posix(root_fd)
                finally:
                    os.close(root_fd)
            if self._root_identity is not None and identity != self._root_identity:
                raise OSError("Approval store root changed after validation")
            if _decode_approval_state(data) != self._approvals:
                raise OSError("Approval state changed outside the trusted store")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            self._approvals = {}
            self._degraded_reason = "local_mcp_approval_state_unreadable"
            return None
        return self._approvals.get(server_name)

    def approve(self, server_name: str, fingerprint: str) -> None:
        if not server_name or len(server_name) > 160:
            raise ValueError("Invalid MCP server name")
        if not valid_local_mcp_fingerprint(fingerprint):
            raise ValueError("Invalid local MCP approval fingerprint")
        if self._degraded_reason is not None:
            raise LocalMcpApprovalStoreError(
                "Local MCP approval store is unavailable; review or remove the damaged state"
            )

        previous = dict(self._approvals)
        self._approvals[server_name] = fingerprint
        try:
            self._write()
        except Exception as exc:
            self._approvals = previous
            self._degraded_reason = "local_mcp_approval_state_unwritable"
            raise LocalMcpApprovalStoreError(
                "Local MCP approval could not be persisted"
            ) from exc

    def _load(self) -> None:
        try:
            if os.name == "nt":
                data = self._read_windows()
                root_info = self._root.lstat()
                self._root_identity = (root_info.st_dev, root_info.st_ino)
            else:
                root_fd = self._open_secure_posix_root()
                try:
                    root_info = os.fstat(root_fd)
                    self._root_identity = (root_info.st_dev, root_info.st_ino)
                    data = self._read_posix(root_fd)
                finally:
                    os.close(root_fd)
            approvals = _decode_approval_state(data)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            self._approvals = {}
            self._degraded_reason = "local_mcp_approval_state_unreadable"
            return
        self._approvals = dict(approvals)

    @staticmethod
    def _validate_posix_directory(info: os.stat_result, *, leaf: bool) -> None:
        if not stat.S_ISDIR(info.st_mode):
            raise OSError("Approval store ancestor is not a directory")
        if leaf:
            current_uid = os.geteuid()
            if info.st_uid not in {0, current_uid}:
                raise OSError("Approval store root has an unsafe owner")
            if stat.S_IMODE(info.st_mode) != 0o700:
                raise OSError("Approval store root must have mode 0700")

    def _open_secure_posix_root(self) -> int:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        current_fd = os.open(os.path.sep, flags)
        parts = self._root.parts[1:]
        try:
            for index, component in enumerate(parts):
                leaf = index == len(parts) - 1
                try:
                    info = os.stat(component, dir_fd=current_fd, follow_symlinks=False)
                except FileNotFoundError:
                    os.mkdir(component, 0o700, dir_fd=current_fd)
                    info = os.stat(component, dir_fd=current_fd, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise OSError("Approval store cannot contain symbolic links")
                self._validate_posix_directory(info, leaf=leaf)
                next_fd = os.open(component, flags, dir_fd=current_fd)
                opened = os.fstat(next_fd)
                self._validate_posix_directory(opened, leaf=leaf)
                os.close(current_fd)
                current_fd = next_fd
            return current_fd
        except BaseException:
            os.close(current_fd)
            raise

    @staticmethod
    def _validate_posix_file(info: os.stat_result) -> None:
        if not stat.S_ISREG(info.st_mode):
            raise OSError("Approval state is not a regular file")
        if info.st_uid != os.geteuid():
            raise OSError("Approval state has an unsafe owner")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise OSError("Approval state must have mode 0600")
        if info.st_nlink != 1:
            raise OSError("Approval state has an unsafe link count")

    def _read_posix(self, root_fd: int) -> bytes | None:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self._filename, flags, dir_fd=root_fd)
        except FileNotFoundError:
            return None
        try:
            self._validate_posix_file(os.fstat(fd))
            chunks: list[bytes] = []
            total = 0
            while chunk := os.read(fd, 64 * 1024):
                total += len(chunk)
                if total > _MAX_APPROVAL_FILE_BYTES:
                    raise OSError("Approval state is too large")
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(fd)

    def _write(self) -> None:
        payload = {
            "version": LOCAL_APPROVAL_STATE_VERSION,
            "approvals": dict(sorted(self._approvals.items())),
        }
        data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if os.name == "nt":
            self._write_windows(data)
            return

        root_fd = self._open_secure_posix_root()
        root_info = os.fstat(root_fd)
        root_identity = (root_info.st_dev, root_info.st_ino)
        temp_name = f".{self._filename}.{uuid.uuid4().hex}.tmp"
        temp_created = False
        try:
            # Refuse to overwrite a linked or permissive existing state file.
            existing = self._read_posix(root_fd)
            del existing
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
            )
            fd = os.open(temp_name, flags, 0o600, dir_fd=root_fd)
            temp_created = True
            try:
                self._validate_posix_file(os.fstat(fd))
                view = memoryview(data)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
            # Validate the destination once more immediately before the atomic
            # replacement.  Both names are relative to the trusted root fd.
            self._read_posix(root_fd)
            os.replace(
                temp_name,
                self._filename,
                src_dir_fd=root_fd,
                dst_dir_fd=root_fd,
            )
            temp_created = False
            self._read_posix(root_fd)
            os.fsync(root_fd)
            verification_fd = self._open_secure_posix_root()
            try:
                verification_info = os.fstat(verification_fd)
                verification_identity = (
                    verification_info.st_dev,
                    verification_info.st_ino,
                )
                if verification_identity != root_identity:
                    raise OSError("Approval store root changed during persistence")
                if _decode_approval_state(
                    self._read_posix(verification_fd)
                ) != self._approvals:
                    raise OSError("Approval state failed durable verification")
                self._root_identity = verification_identity
            finally:
                os.close(verification_fd)
        finally:
            if temp_created:
                try:
                    os.unlink(temp_name, dir_fd=root_fd)
                except OSError:
                    pass
            os.close(root_fd)

    @staticmethod
    def _windows_has_reparse_point(path: Path) -> bool:
        try:
            return bool(path.lstat().st_file_attributes & _WINDOWS_REPARSE_POINT)
        except (AttributeError, OSError):
            return False

    def _secure_windows_root(self) -> None:
        current = Path(self._root.anchor)
        for component in self._root.parts[1:]:
            current = current / component
            if current.exists() or current.is_symlink():
                if self._windows_has_reparse_point(current) or not current.is_dir():
                    raise OSError("Approval store cannot contain reparse points")
            else:
                current.mkdir(mode=0o700)

    def _read_windows(self) -> bytes | None:
        self._secure_windows_root()
        if not self._path.exists() and not self._path.is_symlink():
            return None
        info = self._path.lstat()
        if (
            self._windows_has_reparse_point(self._path)
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
        ):
            raise OSError("Approval state is not a safe regular file")
        data = self._path.read_bytes()
        if len(data) > _MAX_APPROVAL_FILE_BYTES:
            raise OSError("Approval state is too large")
        return data

    def _write_windows(self, data: bytes) -> None:
        self._secure_windows_root()
        before_info = self._root.lstat()
        root_identity = (before_info.st_dev, before_info.st_ino)
        self._read_windows()
        temporary = self._root / f".{self._filename}.{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        fd = os.open(temporary, flags, 0o600)
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            self._read_windows()
            os.replace(temporary, self._path)
            self._secure_windows_root()
            after_info = self._root.lstat()
            after_identity = (after_info.st_dev, after_info.st_ino)
            if after_identity != root_identity:
                raise OSError("Approval store root changed during persistence")
            if _decode_approval_state(self._read_windows()) != self._approvals:
                raise OSError("Approval state failed durable verification")
            self._root_identity = after_identity
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass
