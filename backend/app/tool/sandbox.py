"""OS process sandbox construction for command and Python execution.

The policy is deliberately fail-closed on platforms where the required
sandbox primitive is missing.  Every successful launch has an OS process-tree
boundary and restricts filesystem writes and network access before user code
starts.  v0.9 enables execution only on Linux, where bubblewrap's PID namespace
provides a kernel-enforced lifetime for detached descendants.
"""

from __future__ import annotations

import os
import secrets
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from app.tool.workspace import (
    APP_PRIVATE_DIR_ENV,
    WorkspaceBoundaryViolation,
    validate_agent_workspace_root,
)


class SandboxUnavailable(RuntimeError):
    """Raised when an execution request cannot be safely sandboxed."""


@dataclass(frozen=True)
class SandboxLaunch:
    """Fully prepared child-process launch parameters."""

    argv: list[str]
    cwd: str
    env: dict[str, str]
    backend: str
    filesystem_isolated: bool
    network_isolated: bool

    @property
    def metadata(self) -> dict[str, object]:
        return {
            "sandbox": self.backend,
            "filesystem_isolated": self.filesystem_isolated,
            "network_isolated": self.network_isolated,
            "environment_sanitized": True,
            "process_tree_isolated": True,
        }


_SAFE_ENV_KEYS = frozenset(
    {
        "COLORTERM",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "NO_COLOR",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TERM",
        "TZ",
        "WINDIR",
    }
)
def validate_execution_platform() -> None:
    """Reject platforms without a complete filesystem and lifetime sandbox."""

    if sys.platform == "win32":
        # A Job Object is a termination boundary, not a filesystem/network
        # sandbox. Until an audited AppContainer/restricted-token launcher is
        # shipped, executing here would silently restore host access.
        raise SandboxUnavailable(
            "Command and Python execution are disabled on Windows until an "
            "AppContainer sandbox is available"
        )
    if sys.platform == "darwin":
        # Seatbelt constrains filesystem/network access, but it is not a hard
        # lifetime owner. A child can create a new session/process group and
        # survive both normal completion and timeout. Coalition creation and
        # assignment are launchd-private/privileged, so a normal desktop app
        # cannot use them as a per-command kill boundary. Do not substitute a
        # racy process-table scanner for an OS guarantee.
        raise SandboxUnavailable(
            "Command and Python execution are disabled on macOS until an "
            "OS-enforced detached-process lifetime sandbox is available"
        )


def _sanitized_environment(scratch_dir: Path) -> dict[str, str]:
    """Return a minimal environment with no backend/API credentials."""

    home = scratch_dir / "home"
    temp = scratch_dir / "tmp"
    cache = scratch_dir / "cache"
    for path in (home, temp, cache):
        path.mkdir(parents=True, exist_ok=True)

    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in _SAFE_ENV_KEYS and value
    }
    env.setdefault("PATH", os.defpath)
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "TMPDIR": str(temp),
            "TMP": str(temp),
            "TEMP": str(temp),
            "XDG_CACHE_HOME": str(cache),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "MPLCONFIGDIR": str(cache / "matplotlib"),
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return env


def _runtime_read_paths(command: Sequence[str]) -> list[Path]:
    """Return runtime locations that user code may execute/import but not write."""

    paths: list[Path] = []
    if command:
        executable = shutil.which(command[0]) or command[0]
        paths.append(Path(executable))

    # Python's venv/base prefix contains the interpreter, stdlib, extension
    # modules and installed data-science packages used by code_execute.
    paths.extend((Path(sys.prefix), Path(sys.base_prefix), Path(sys.executable)))
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        paths.append(Path(meipass))
    resource_dir = os.environ.get("SUXIAOYOU_RESOURCE_DIR")
    if resource_dir:
        paths.append(Path(resource_dir))
    return paths


def _bwrap_parent_dirs(path: Path) -> list[Path]:
    """Directories bubblewrap must recreate before binding *path*."""

    return [parent for parent in reversed(path.parents) if parent != Path("/")]


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def validate_workspace_private_boundary(
    workspace: str | Path,
    *,
    private_root: str | Path | None = None,
) -> Path:
    """Return the canonical workspace or reject app-private path overlap.

    The desktop launcher publishes its per-user data root. Source/dev launches
    conservatively treat the backend working directory as private. Rejecting
    overlap before scratch creation prevents a broad or mis-selected workspace
    from making configuration, session tokens, or credential fallback files
    visible to an otherwise approved command.
    """

    try:
        return validate_agent_workspace_root(
            workspace,
            private_root=private_root,
        )
    except WorkspaceBoundaryViolation as exc:
        raise SandboxUnavailable(str(exc)) from exc


def create_sandbox_scratch(
    workspace: str | Path,
    *,
    prefix: str,
) -> Path:
    """Create a private scratch directory without following workspace symlinks.

    The scratch path is created relative to verified directory descriptors.
    This matters because checking ``Path.resolve()`` only after ``mkdir`` or a
    code-file write is too late: a pre-existing ``.suxiaoyou/sandbox`` symlink
    could already have redirected that host write outside the workspace.
    """

    workspace_path = validate_workspace_private_boundary(workspace)
    if not workspace_path.is_dir():
        raise SandboxUnavailable(f"Workspace does not exist: {workspace_path}")
    validate_execution_platform()

    directory_flags = os.O_RDONLY
    directory_flags |= getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptors: list[int] = []
    try:
        parent_fd = os.open(workspace_path, directory_flags)
        descriptors.append(parent_fd)
        for component in (".suxiaoyou", "sandbox"):
            try:
                os.mkdir(component, mode=0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
            try:
                child_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            except OSError as exc:
                raise SandboxUnavailable(
                    "Sandbox scratch path contains a symlink or non-directory"
                ) from exc
            if not stat.S_ISDIR(os.fstat(child_fd).st_mode):
                os.close(child_fd)
                raise SandboxUnavailable("Sandbox scratch path is not a directory")
            descriptors.append(child_fd)
            parent_fd = child_fd

        for _ in range(100):
            name = f"{prefix}{secrets.token_hex(12)}"
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent_fd)
            except FileExistsError:
                continue
            scratch_path = workspace_path / ".suxiaoyou" / "sandbox" / name
            resolved = scratch_path.resolve()
            if resolved != scratch_path or not _path_is_within(resolved, workspace_path):
                try:
                    os.rmdir(name, dir_fd=parent_fd)
                except OSError:
                    pass
                raise SandboxUnavailable("Sandbox scratch directory escaped the workspace")
            return scratch_path
        raise SandboxUnavailable("Could not allocate a unique sandbox scratch directory")
    except SandboxUnavailable:
        raise
    except OSError as exc:
        raise SandboxUnavailable(f"Could not create sandbox scratch directory: {exc}") from exc
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _append_bwrap_bind(
    argv: list[str],
    source: Path,
    *,
    bound_roots: list[Path],
) -> None:
    """Bind one runtime path into an otherwise empty bubblewrap root."""

    try:
        source = source.resolve()
    except OSError:
        return
    if not source.exists() or any(_path_is_within(source, root) for root in bound_roots):
        return
    for parent in _bwrap_parent_dirs(source):
        argv.extend(("--dir", str(parent)))
    argv.extend(("--ro-bind", str(source), str(source)))
    bound_roots.append(source)


def prepare_sandbox_launch(
    command: Sequence[str],
    *,
    workspace: str | None,
    cwd: str | None,
    scratch_dir: str | Path,
    read_only_paths: Iterable[str | Path] = (),
) -> SandboxLaunch:
    """Wrap *command* in the strongest mandatory platform sandbox available."""

    if not workspace:
        raise SandboxUnavailable("Execution requires a selected workspace")
    if not command:
        raise SandboxUnavailable("Execution command is empty")

    workspace_path = validate_workspace_private_boundary(workspace)
    if not workspace_path.is_dir():
        raise SandboxUnavailable(f"Workspace does not exist: {workspace_path}")
    validate_execution_platform()
    cwd_path = Path(cwd or workspace_path).resolve()
    try:
        cwd_path.relative_to(workspace_path)
    except ValueError as exc:
        raise SandboxUnavailable(
            f"Execution cwd is outside the workspace: {cwd_path}"
        ) from exc

    scratch_candidate = Path(scratch_dir)
    if scratch_candidate.is_symlink():
        raise SandboxUnavailable("Sandbox scratch directory may not be a symlink")
    try:
        scratch_path = scratch_candidate.resolve(strict=True)
    except OSError as exc:
        raise SandboxUnavailable("Sandbox scratch directory does not exist") from exc
    try:
        scratch_path.relative_to(workspace_path)
    except ValueError as exc:
        raise SandboxUnavailable("Sandbox scratch directory escaped the workspace") from exc
    if not scratch_path.is_dir():
        raise SandboxUnavailable("Sandbox scratch path is not a directory")
    env = _sanitized_environment(scratch_path)

    runtime_paths = _runtime_read_paths(command)
    runtime_paths.extend(Path(value) for value in read_only_paths)

    if sys.platform.startswith("linux"):
        bwrap = shutil.which("bwrap")
        if not bwrap:
            raise SandboxUnavailable(
                "Linux execution sandbox unavailable: install bubblewrap (bwrap)"
            )
        argv = [
            bwrap,
            # --unshare-all includes a PID namespace. The sandboxed command is
            # its init process; when it exits, Linux kills every remaining
            # namespace member, including setsid()/setpgid() descendants.
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
        ]

        # Bubblewrap starts from an empty root. Bind only executable/runtime
        # trees and a small set of non-secret TLS/locale files. In particular,
        # host /home, /root, /run and /var (including Docker/desktop sockets)
        # never enter the namespace.
        bound_roots: list[Path] = []
        for root in (Path("/usr"), Path("/nix/store")):
            _append_bwrap_bind(argv, root, bound_roots=bound_roots)
        for legacy in (Path("/bin"), Path("/sbin"), Path("/lib"), Path("/lib64")):
            if legacy.is_symlink():
                argv.extend(("--symlink", os.readlink(legacy), str(legacy)))
            else:
                _append_bwrap_bind(argv, legacy, bound_roots=bound_roots)
        for resource in (
            Path("/etc/ssl"),
            Path("/etc/ca-certificates"),
            Path("/etc/pki"),
            Path("/etc/localtime"),
            Path("/etc/ld.so.cache"),
            Path("/etc/nsswitch.conf"),
        ):
            _append_bwrap_bind(argv, resource, bound_roots=bound_roots)
        for runtime_path in runtime_paths:
            _append_bwrap_bind(argv, runtime_path, bound_roots=bound_roots)

        argv.extend(("--proc", "/proc", "--dev", "/dev", "--dir", "/tmp"))
        for parent in _bwrap_parent_dirs(workspace_path):
            argv.extend(("--dir", str(parent)))
        argv.extend(("--bind", str(workspace_path), str(workspace_path)))
        # Parent directories created in the empty root are otherwise writable.
        # Remount the root read-only; the workspace remains its own RW bind.
        argv.extend(("--remount-ro", "/"))
        argv.extend(("--chdir", str(cwd_path), "--clearenv"))
        for key, value in sorted(env.items()):
            argv.extend(("--setenv", key, value))
        argv.extend(("--", *map(str, command)))
        return SandboxLaunch(
            argv=argv,
            cwd=str(cwd_path),
            env={},
            backend="linux-bubblewrap",
            filesystem_isolated=True,
            network_isolated=True,
        )

    raise SandboxUnavailable(f"Unsupported execution sandbox platform: {sys.platform}")
