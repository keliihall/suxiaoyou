"""OS process sandbox construction for command and Python execution.

The policy is deliberately fail-closed on platforms where the required
sandbox primitive is missing.  Every successful launch has an OS process-tree
boundary and restricts filesystem writes before user code starts.  Linux uses
bubblewrap's PID namespace.  macOS uses Seatbelt and denies ``setsid(2)`` and
``setpgid(2)`` so descendants cannot leave the launch process group that owns
their lifetime.
"""

from __future__ import annotations

import os
import ntpath
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
    environment_scope: str = "ephemeral"
    persistent_home: bool = False

    @property
    def metadata(self) -> dict[str, object]:
        return {
            "sandbox": self.backend,
            "filesystem_isolated": self.filesystem_isolated,
            "network_isolated": self.network_isolated,
            "environment_sanitized": True,
            "process_tree_isolated": True,
            "execution_environment_scope": self.environment_scope,
            "home_persistent": self.persistent_home,
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
    """Probe the native execution backend for the current desktop platform."""

    if sys.platform == "win32":
        # The actual Win32 probe occurs while constructing the native Job
        # Object.  Importantly, the runner creates the command suspended and
        # resumes it only after assignment, so no child can race this check.
        return
    if sys.platform == "darwin":
        # Seatbelt itself does not own process lifetime. The macOS profile below
        # closes the escape hatches (setsid/setpgid), after which the launch
        # process group is a complete, synchronously killable descendant set.
        if not shutil.which("sandbox-exec"):
            raise SandboxUnavailable(
                "macOS execution sandbox unavailable: sandbox-exec was not found"
            )
        return
    if not sys.platform.startswith("linux"):
        raise SandboxUnavailable(
            f"Unsupported execution platform: {sys.platform}"
        )


def _sanitized_environment(
    scratch_dir: Path,
    *,
    persistent_environment: Path | None = None,
    persistent_environment_view: Path | None = None,
) -> dict[str, str]:
    """Return a minimal environment with no backend/API credentials."""

    temp = scratch_dir / "tmp"
    if persistent_environment is None:
        physical_home = scratch_dir / "home"
        physical_cache = scratch_dir / "cache"
        home = physical_home
        cache = physical_cache
    else:
        physical_home = persistent_environment / "home"
        physical_cache = persistent_environment / "cache"
        environment_view = persistent_environment_view or persistent_environment
        home = environment_view / "home"
        cache = environment_view / "cache"
    for path in (physical_home, temp, physical_cache):
        path.mkdir(parents=True, exist_ok=True)

    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in _SAFE_ENV_KEYS and value
    }
    env.setdefault("PATH", os.defpath)
    path_separator = ";" if sys.platform == "win32" else ":"
    executable_directories = [home / ".local" / "bin"]
    if sys.platform == "win32":
        executable_directories.extend(
            (home / ".local" / "Scripts", home / "Scripts"),
        )
    env["PATH"] = path_separator.join(
        (*(str(path) for path in executable_directories), env["PATH"]),
    )
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
            "PIP_CACHE_DIR": str(cache / "pip"),
            "UV_CACHE_DIR": str(cache / "uv"),
            "NPM_CONFIG_CACHE": str(cache / "npm"),
            "PYTHONUSERBASE": str(home / ".local"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    if persistent_environment is None:
        # A transient HOME has no intentional user packages. Keep the host's
        # user-site disabled when callers use the lower-level helper directly.
        env["PYTHONNOUSERSITE"] = "1"
    if sys.platform == "darwin":
        # Apple's /usr/bin developer-tool shims use xcrun internally. On a
        # fresh account xcrun otherwise creates its database in the host's
        # Darwin user-temp directory (outside our private workspace), which a
        # deny-by-default Seatbelt profile must reject. Redirect the database
        # itself into this invocation's writable scratch instead. This keeps
        # /usr/bin/python3 and sibling shims usable without granting any host
        # cache write access; the ephemeral database disappears with scratch.
        env["xcrun_db"] = str(temp / "xcrun_db")
    return env


def _runtime_read_paths(command: Sequence[str]) -> list[Path]:
    """Return runtime locations that user code may execute/import but not write."""

    paths: list[Path] = []
    if command:
        if sys.platform == "win32" and ntpath.isabs(str(command[0])):
            executable = command[0]
        else:
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


def _append_bwrap_bind_at_logical_path(argv: list[str], source: Path) -> None:
    """Bind a host config file at its requested path even when it is a symlink."""

    try:
        resolved = source.resolve(strict=True)
    except OSError:
        return
    logical = Path(os.path.abspath(source))
    for parent in _bwrap_parent_dirs(logical):
        argv.extend(("--dir", str(parent)))
    argv.extend(("--ro-bind", str(resolved), str(logical)))


_MACOS_XCODE_SELECT_LINK = Path("/var/db/xcode_select_link")


_MACOS_SYSTEM_READ_ROOTS = (
    Path("/System"),
    Path("/usr"),
    Path("/bin"),
    Path("/sbin"),
    Path("/Library/Apple"),
    # Homebrew executables commonly link libraries from sibling formulae, so
    # granting only the executable's own prefix makes dyld fail before exec.
    # These package-manager trees are read-only; user Home and app-private data
    # remain absent from the profile.
    Path("/opt/homebrew"),
    Path("/usr/local/Homebrew"),
    Path("/usr/local/Cellar"),
    Path("/usr/local/opt"),
    Path("/usr/local/bin"),
    Path("/usr/local/lib"),
    Path("/private/etc"),
    Path("/private/var/db/timezone"),
    Path("/private/var/select"),
    # /usr/bin/python3 consults xcode-select before entering the selected
    # Developer runtime. The canonical target is added as a read-only root;
    # the logical symlink itself is granted separately below.
    _MACOS_XCODE_SELECT_LINK,
)

_MACOS_LOGICAL_READ_PATHS = (
    _MACOS_XCODE_SELECT_LINK,
    Path("/private/var/db/xcode_select_link"),
)


def _existing_canonical_paths(paths: Iterable[Path]) -> list[Path]:
    """Return unique existing canonical paths without following missing inputs."""

    result: list[Path] = []
    for path in paths:
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            continue
        if resolved not in result:
            result.append(resolved)
    return result


def _existing_logical_paths(paths: Iterable[Path]) -> list[Path]:
    """Return existing lexical paths without resolving their final symlink."""

    result: list[Path] = []
    for path in paths:
        logical = Path(os.path.abspath(path))
        if os.path.lexists(logical) and logical not in result:
            result.append(logical)
    return result


def _selected_xcode_contents() -> Path | None:
    """Resolve a full selected Xcode bundle without trusting lookalike paths."""

    try:
        developer_root = _MACOS_XCODE_SELECT_LINK.resolve(strict=True)
    except OSError:
        return None
    if (
        developer_root.name != "Developer"
        or developer_root.parent.name != "Contents"
        or not developer_root.is_dir()
    ):
        return None
    xcode_bundle = developer_root.parent.parent
    if xcode_bundle.suffix.casefold() != ".app" or not xcode_bundle.is_dir():
        return None
    return developer_root.parent


def _selected_xcode_metadata_paths() -> list[Path]:
    """Return exact metadata files required by Apple's developer-tool shims.

    ``/usr/bin/python3`` enters the selected developer runtime through xcrun.
    When xcode-select points at a full Xcode installation, xcrun also stats the
    enclosing ``Contents/Info.plist`` before launching the tool.  Derive that
    file only from the canonical, system-owned selection link and require the
    standard ``Contents/Developer`` layout.  Never expose the enclosing Xcode
    bundle (or ``/Applications``) as a recursive Seatbelt read root.
    """

    contents = _selected_xcode_contents()
    if contents is None:
        return []
    info_plist = contents / "Info.plist"
    try:
        # Xcode ships a regular Info.plist. Reject a redirected final component
        # instead of silently widening the trusted read set to its target.
        if info_plist.is_symlink() or not info_plist.is_file():
            return []
        canonical_info = info_plist.resolve(strict=True)
    except OSError:
        return []
    if canonical_info != info_plist:
        return []
    return [canonical_info]


def _selected_xcode_runtime_roots() -> list[Path]:
    """Return only the Xcode framework proven necessary for tool shims.

    On a full Xcode selection, Apple's ``/usr/bin/python3`` shim loads
    ``DVTSystemPrerequisites.framework`` before entering the Developer runtime.
    Grant that one signed framework as a recursive read root so dyld can read
    its binary, bundle metadata, and internal symlinks without exposing sibling
    frameworks or the enclosing Xcode application.
    """

    contents = _selected_xcode_contents()
    if contents is None:
        return []
    framework = contents / "SharedFrameworks" / "DVTSystemPrerequisites.framework"
    try:
        if framework.is_symlink() or not framework.is_dir():
            return []
        canonical_framework = framework.resolve(strict=True)
        binary = framework / "Versions" / "A" / "DVTSystemPrerequisites"
        if binary.is_symlink() or not binary.is_file():
            return []
        canonical_binary = binary.resolve(strict=True)
    except OSError:
        return []
    if canonical_framework != framework or canonical_binary != binary:
        return []
    return [canonical_framework]


def _map_workspace_text(
    value: str,
    logical: Path,
    staged: Path,
    *,
    aliases: Iterable[Path] = (),
) -> str:
    """Map logical absolute workspace references into the private macOS stage."""

    sources = {logical, *aliases}
    for source in sorted(sources, key=lambda item: len(str(item)), reverse=True):
        if source != staged:
            value = value.replace(str(source), str(staged))
    return value


def _macos_seatbelt_profile(
    *,
    read_root_names: Sequence[str],
    traversal_names: Sequence[str],
    literal_read_names: Sequence[str] = (),
    writable_root_names: Sequence[str] = ("WORKSPACE",),
    allow_network: bool,
) -> str:
    """Build a deny-by-default Seatbelt profile using ``sandbox-exec -D`` paths."""

    read_filters = " ".join(
        f'(subpath (param "{name}"))' for name in read_root_names
    )
    traversal_filters = " ".join(
        f'(literal (param "{name}"))' for name in traversal_names
    )
    literal_read_filters = " ".join(
        f'(literal (param "{name}"))' for name in literal_read_names
    )
    writable_filters = " ".join(
        f'(subpath (param "{name}"))' for name in writable_root_names
    )
    network_rules = ""
    if allow_network:
        # ``system-network`` grants DNS/reachability helpers. Restrict the data
        # plane to remote IP sockets plus mDNS instead of exposing arbitrary
        # local Unix-domain services such as Docker Desktop.
        network_rules = """
        (system-network)
        (allow network-outbound
          (remote ip)
          (literal "/private/var/run/mDNSResponder"))
        """
    return f"""
    (version 1)
    (deny default)
    (import "system.sb")

    (allow process*)
    (allow sysctl-read)

    ;; A command is launched as a new session/process-group leader. Prevent
    ;; descendants (including posix_spawn children) from escaping that group,
    ;; so killpg owns their lifetime on completion, timeout, and cancellation.
    (deny syscall-unix (syscall-number SYS_setsid))
    (deny syscall-unix (syscall-number SYS_setpgid))

    (allow file-read* file-test-existence {read_filters} {literal_read_filters})
    ;; Third-party interpreters and extension modules are outside Apple's
    ;; system.sb roots. Read access alone is insufficient for dyld; grant the
    ;; executable mapping operation only for the same trusted runtime roots.
    (allow file-map-executable {read_filters})
    ;; Path resolution (including Python virtual-environment symlinks) needs
    ;; more than metadata access on each ancestor. Literal filters expose the
    ;; ancestor object itself without granting reads of sibling contents.
    (allow file-read* file-test-existence {traversal_filters})
    (allow file-read* file-test-existence
      (literal "/dev/null")
      (literal "/dev/zero")
      (literal "/dev/random")
      (literal "/dev/urandom"))
    (allow file-write*
      {writable_filters}
      (literal "/dev/null"))
    {network_rules}
    """


def prepare_sandbox_launch(
    command: Sequence[str],
    *,
    workspace: str | None,
    workspace_source: str | Path | None = None,
    cwd: str | None,
    scratch_dir: str | Path,
    persistent_environment: str | Path | None = None,
    read_only_paths: Iterable[str | Path] = (),
    allow_network: bool = False,
) -> SandboxLaunch:
    """Wrap *command* in the strongest mandatory platform sandbox available."""

    if not workspace:
        raise SandboxUnavailable("Execution requires a selected workspace")
    if not command:
        raise SandboxUnavailable("Execution command is empty")

    workspace_path = validate_workspace_private_boundary(workspace)
    workspace_input_path = Path(os.path.abspath(os.path.expanduser(workspace)))
    if not workspace_path.is_dir():
        raise SandboxUnavailable(f"Workspace does not exist: {workspace_path}")
    source_candidate = Path(workspace_source or workspace_path)
    if source_candidate.is_symlink():
        raise SandboxUnavailable("Sandbox workspace source may not be a symlink")
    try:
        workspace_source_path = source_candidate.resolve(strict=True)
    except OSError as exc:
        raise SandboxUnavailable("Sandbox workspace source does not exist") from exc
    if not workspace_source_path.is_dir():
        raise SandboxUnavailable("Sandbox workspace source is not a directory")
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
        scratch_path.relative_to(workspace_source_path)
    except ValueError as exc:
        raise SandboxUnavailable("Sandbox scratch directory escaped the workspace source") from exc
    if not scratch_path.is_dir():
        raise SandboxUnavailable("Sandbox scratch path is not a directory")

    environment_source_path: Path | None = None
    environment_view_path: Path | None = None
    if persistent_environment is not None:
        environment_candidate = Path(persistent_environment)
        if environment_candidate.is_symlink():
            raise SandboxUnavailable(
                "Persistent execution environment may not be a symlink",
            )
        try:
            environment_source_path = environment_candidate.resolve(strict=True)
            environment_relative = environment_source_path.relative_to(workspace_path)
        except (OSError, ValueError) as exc:
            raise SandboxUnavailable(
                "Persistent execution environment escaped the workspace",
            ) from exc
        expected_prefix = Path(".suxiaoyou") / "execution-environments"
        if (
            environment_relative.parts[:2] != expected_prefix.parts
            or len(environment_relative.parts) != 3
        ):
            raise SandboxUnavailable(
                "Persistent execution environment is outside the private runtime root",
            )
        if not environment_source_path.is_dir():
            raise SandboxUnavailable(
                "Persistent execution environment is not a directory",
            )
        environment_view_path = (
            workspace_path / environment_relative
            if sys.platform.startswith("linux")
            else environment_source_path
        )

    env = _sanitized_environment(
        scratch_path,
        persistent_environment=environment_source_path,
        persistent_environment_view=environment_view_path,
    )

    runtime_paths = _runtime_read_paths(command)
    runtime_paths.extend(Path(value) for value in read_only_paths)

    if sys.platform.startswith("linux"):
        if workspace_source_path != workspace_path:
            for key, value in tuple(env.items()):
                candidate = Path(value)
                try:
                    relative = candidate.relative_to(workspace_source_path)
                except ValueError:
                    continue
                env[key] = str(workspace_path / relative)
        env["SUXIAOYOU_WORKSPACE"] = str(workspace_path)
        env["PWD"] = str(cwd_path)
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
        if allow_network:
            # ``--share-net`` is bubblewrap's documented override for the
            # network namespace included by ``--unshare-all``. All other
            # namespaces (especially PID) remain private.
            argv.append("--share-net")

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
        if allow_network:
            for resource in (
                Path("/etc/resolv.conf"),
                Path("/etc/hosts"),
                Path("/etc/host.conf"),
                Path("/etc/gai.conf"),
            ):
                _append_bwrap_bind_at_logical_path(argv, resource)
        for runtime_path in runtime_paths:
            _append_bwrap_bind(argv, runtime_path, bound_roots=bound_roots)

        argv.extend(("--proc", "/proc", "--dev", "/dev", "--dir", "/tmp"))
        for parent in _bwrap_parent_dirs(workspace_path):
            argv.extend(("--dir", str(parent)))
        argv.extend(("--bind", str(workspace_source_path), str(workspace_path)))
        if environment_source_path is not None and environment_view_path is not None:
            environment_relative = environment_view_path.relative_to(workspace_path)
            placeholder = workspace_source_path / environment_relative
            placeholder.mkdir(parents=True, exist_ok=True)
            argv.extend(
                (
                    "--bind",
                    str(environment_source_path),
                    str(environment_view_path),
                )
            )
        # Parent directories created in the empty root are otherwise writable.
        # Remount the root read-only; the workspace remains its own RW bind.
        argv.extend(("--remount-ro", "/"))
        argv.extend(("--chdir", str(cwd_path), "--clearenv"))
        for key, value in sorted(env.items()):
            argv.extend(("--setenv", key, value))
        argv.extend(("--", *map(str, command)))
        return SandboxLaunch(
            argv=argv,
            # This is the host cwd used only while bubblewrap constructs the
            # namespace. The child enters ``cwd_path`` through --chdir.
            cwd=str(workspace_path),
            env={},
            backend="linux-bubblewrap",
            filesystem_isolated=True,
            network_isolated=not allow_network,
            environment_scope=("session" if environment_source_path else "ephemeral"),
            persistent_home=environment_source_path is not None,
        )

    if sys.platform == "darwin":
        sandbox_exec = shutil.which("sandbox-exec")
        if not sandbox_exec:
            raise SandboxUnavailable(
                "macOS execution sandbox unavailable: sandbox-exec was not found"
            )

        cwd_relative = cwd_path.relative_to(workspace_path)
        staged_cwd = workspace_source_path / cwd_relative
        if not staged_cwd.is_dir():
            raise SandboxUnavailable(
                f"Execution cwd does not exist in the staged workspace: {staged_cwd}"
            )

        mac_command = [
            _map_workspace_text(
                str(value),
                workspace_path,
                workspace_source_path,
                aliases=(workspace_input_path,),
            )
            for value in command
        ]
        mapped_runtime_paths = [
            Path(
                _map_workspace_text(
                    str(value),
                    workspace_path,
                    workspace_source_path,
                    aliases=(workspace_input_path,),
                )
            )
            for value in runtime_paths
        ]
        read_roots = _existing_canonical_paths(
            (
                *_MACOS_SYSTEM_READ_ROOTS,
                *_selected_xcode_runtime_roots(),
                workspace_source_path,
                *((environment_source_path,) if environment_source_path else ()),
                *mapped_runtime_paths,
            )
        )
        literal_read_paths = _existing_logical_paths(
            (*_MACOS_LOGICAL_READ_PATHS, *_selected_xcode_metadata_paths())
        )

        traversal_roots: list[Path] = []
        for read_root in (*read_roots, *literal_read_paths):
            for parent in reversed(read_root.parents):
                if parent not in traversal_roots:
                    traversal_roots.append(parent)

        parameters: list[tuple[str, Path]] = [("WORKSPACE", workspace_source_path)]
        read_names: list[str] = []
        for index, read_root in enumerate(read_roots):
            name = f"READ_ROOT_{index}"
            parameters.append((name, read_root))
            read_names.append(name)
        traversal_names: list[str] = []
        for index, traversal_root in enumerate(traversal_roots):
            name = f"TRAVERSE_{index}"
            parameters.append((name, traversal_root))
            traversal_names.append(name)
        literal_read_names: list[str] = []
        for index, logical_read_path in enumerate(literal_read_paths):
            name = f"LITERAL_READ_{index}"
            parameters.append((name, logical_read_path))
            literal_read_names.append(name)
        writable_root_names = ["WORKSPACE"]
        if environment_source_path is not None:
            parameters.append(("ENVIRONMENT", environment_source_path))
            writable_root_names.append("ENVIRONMENT")

        profile = _macos_seatbelt_profile(
            read_root_names=read_names,
            traversal_names=traversal_names,
            literal_read_names=literal_read_names,
            writable_root_names=writable_root_names,
            allow_network=allow_network,
        )
        argv = [sandbox_exec]
        for name, value in parameters:
            argv.extend(("-D", f"{name}={value}"))
        argv.extend(("-p", profile, *mac_command))

        env["SUXIAOYOU_WORKSPACE"] = str(workspace_source_path)
        env["PWD"] = str(staged_cwd)
        return SandboxLaunch(
            argv=argv,
            cwd=str(staged_cwd),
            env=env,
            backend="macos-seatbelt",
            filesystem_isolated=True,
            network_isolated=not allow_network,
            environment_scope=("session" if environment_source_path else "ephemeral"),
            persistent_home=environment_source_path is not None,
        )

    if sys.platform == "win32":
        # Windows has no mount namespace equivalent.  The command therefore
        # runs in the explicitly approved workspace with a sanitized HOME/TEMP
        # and a race-free kill-on-close Job Object.  Report the absence of
        # filesystem/network isolation honestly; permission and durable audit
        # remain the host-access boundary on this platform.
        cwd_relative = cwd_path.relative_to(workspace_path)
        physical_cwd = workspace_source_path / cwd_relative
        if not physical_cwd.is_dir():
            raise SandboxUnavailable(
                f"Execution cwd does not exist in the workspace: {physical_cwd}"
            )
        windows_command = [
            _map_workspace_text(
                str(value),
                workspace_path,
                workspace_source_path,
                aliases=(workspace_input_path,),
            )
            for value in command
        ]
        env["SUXIAOYOU_WORKSPACE"] = str(workspace_source_path)
        env["PWD"] = str(physical_cwd)
        return SandboxLaunch(
            argv=windows_command,
            cwd=str(physical_cwd),
            env=env,
            backend="windows-job-object",
            filesystem_isolated=False,
            network_isolated=False,
            environment_scope=("session" if environment_source_path else "ephemeral"),
            persistent_home=environment_source_path is not None,
        )

    raise SandboxUnavailable(f"Unsupported execution sandbox platform: {sys.platform}")
