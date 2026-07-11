#!/usr/bin/env python3
"""Download a minimal Node.js 22 runtime for bundling with 苏小有 desktop.

Usage:
    python scripts/download_node.py [--output resources/nodejs]

Downloads the correct Node.js binary for the current platform, extracts only
the essential files (~30 MB), and places them in the output directory.

The resulting layout:
    resources/nodejs/
        node.exe / bin/node             (Windows / Unix)
        npm.cmd / bin/npm               (Windows / Unix)
        npx.cmd / bin/npx               (Windows / Unix)
        node_modules/npm/                (Windows npm package)
        lib/node_modules/npm/            (Unix npm package)
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

NODE_VERSION = "22.22.0"

# Official Node.js download URLs per platform
_URLS = {
    ("Windows", "AMD64"): f"https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-win-x64.zip",
    ("Windows", "ARM64"): f"https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-win-arm64.zip",
    ("Darwin", "x86_64"): f"https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-darwin-x64.tar.gz",
    ("Darwin", "arm64"): f"https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-darwin-arm64.tar.gz",
    ("Linux", "x86_64"): f"https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-linux-x64.tar.xz",
    ("Linux", "aarch64"): f"https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-linux-arm64.tar.xz",
}

# Files to keep from the Node.js distribution (relative to the root inside archive)
_KEEP_PREFIXES_WIN = {
    "LICENSE",
    "node.exe",
    "npm",
    "npm.cmd",
    "npx",
    "npx.cmd",
    "node_modules/npm/",
}
_KEEP_PREFIXES_UNIX = {
    "LICENSE",
    "bin/node",
    "bin/npm",
    "bin/npx",
    "lib/node_modules/npm/",
}

_UNIX_NPM_LAUNCHERS = {
    "bin/npm": "npm-cli.js",
    "bin/npx": "npx-cli.js",
}


def _write_unix_npm_launcher(destination: Path, cli_name: str) -> None:
    """Write a relocation-safe launcher that survives Tauri resource copying.

    The official Node archive exposes ``bin/npm`` and ``bin/npx`` as symlinks.
    Desktop bundlers may dereference resource symlinks into plain JavaScript
    files, which changes ``__dirname`` and breaks npm's relative imports.  A
    small shell launcher always invokes the packaged sibling Node binary and
    the stable npm CLI path instead.
    """
    destination.write_text(
        "#!/bin/sh\n"
        'script_dir=$(CDPATH= cd -P "$(dirname "$0")" && pwd) || exit 1\n'
        f'exec "$script_dir/node" '
        f'"$script_dir/../lib/node_modules/npm/bin/{cli_name}" "$@"\n',
        encoding="utf-8",
        newline="\n",
    )
    destination.chmod(0o755)


def _platform_key() -> tuple[str, str]:
    system = platform.system()
    machine = platform.machine()
    # Normalize
    if machine in ("x86_64", "AMD64"):
        machine = "AMD64" if system == "Windows" else "x86_64"
    elif machine in ("arm64", "aarch64", "ARM64"):
        machine = "ARM64" if system == "Windows" else ("arm64" if system == "Darwin" else "aarch64")
    return system, machine


def _download(url: str) -> bytes:
    import ssl

    import certifi

    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    print(f"Downloading {url} ...")
    req = urllib.request.Request(url, headers={"User-Agent": "suxiaoyou-builder/1.0"})
    with urllib.request.urlopen(req, timeout=120, context=ssl_ctx) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        data = bytearray()
        while True:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            data.extend(chunk)
            if total:
                pct = len(data) * 100 // total
                print(f"\r  {len(data) // (1024*1024)} MB / {total // (1024*1024)} MB ({pct}%)", end="", flush=True)
        print()
    return bytes(data)


def _shasums_url(archive_url: str) -> str:
    """Return the official checksum manifest beside a Node archive."""
    return f"{archive_url.rsplit('/', 1)[0]}/SHASUMS256.txt"


def _verify_archive_checksum(
    archive_bytes: bytes,
    shasums_bytes: bytes,
    archive_name: str,
) -> str:
    """Verify an archive against its exact SHASUMS256.txt entry."""
    expected = None
    try:
        lines = shasums_bytes.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError("official SHASUMS256.txt is not ASCII") from exc

    for line in lines:
        fields = line.split(maxsplit=1)
        if len(fields) != 2:
            continue
        digest, filename = fields
        if filename.lstrip("*") == archive_name:
            expected = digest.lower()
            break

    if expected is None:
        raise ValueError(
            f"{archive_name} is not listed in official SHASUMS256.txt"
        )
    if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
        raise ValueError(f"invalid SHA-256 entry for {archive_name}")

    actual = hashlib.sha256(archive_bytes).hexdigest()
    if actual != expected:
        raise ValueError(
            f"SHA-256 checksum mismatch for {archive_name}: "
            f"expected {expected}, got {actual}"
        )
    print(f"Verified SHA-256 for {archive_name}: {actual}")
    return actual


def _safe_destination(output: Path, relative_path: str) -> Path:
    """Resolve an archive member below output or reject it as unsafe."""
    if "\\" in relative_path or "\x00" in relative_path:
        raise ValueError(f"unsafe archive path: {relative_path}")
    archive_path = PurePosixPath(relative_path)
    if archive_path.is_absolute() or ".." in archive_path.parts:
        raise ValueError(f"unsafe archive path: {relative_path}")

    root = output.resolve()
    destination = output.joinpath(*archive_path.parts).resolve()
    try:
        destination.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"unsafe archive path: {relative_path}") from exc
    return destination


def _validate_symlink_target(output: Path, destination: Path, link_name: str) -> None:
    """Reject absolute or escaping archive symlinks.

    Official Node.js archives use relative ``..`` links for ``bin/npm`` and
    ``bin/npx``. Those are safe because they still resolve below the runtime
    root, so validate the resolved destination instead of rejecting every
    parent component.
    """
    if not link_name or "\\" in link_name or "\x00" in link_name:
        raise ValueError(f"unsafe archive symlink target: {link_name}")
    link_path = PurePosixPath(link_name)
    if link_path.is_absolute():
        raise ValueError(f"unsafe archive symlink target: {link_name}")

    root = output.resolve()
    target = destination.parent.joinpath(*link_path.parts).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"unsafe archive symlink target: {link_name}") from exc


def _extract_windows(archive_bytes: bytes, output: Path) -> None:
    """Extract minimal files from Windows .zip archive."""
    output.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as zf:
        # Find the root dir inside the zip (e.g. "node-v22.22.0-win-x64/")
        root = zf.namelist()[0].split("/")[0] + "/"
        for info in zf.infolist():
            if info.is_dir():
                continue
            rel = info.filename[len(root):]
            if not rel:
                continue
            if not any(rel == p or rel.startswith(p) for p in _KEEP_PREFIXES_WIN):
                continue
            dest = _safe_destination(output, rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst)
            print(f"  {rel}")


def _extract_unix(archive_bytes: bytes, output: Path) -> None:
    """Extract minimal files from macOS/Linux tar.gz or tar.xz archive."""
    output.mkdir(parents=True, exist_ok=True)
    mode = "r:gz" if archive_bytes[:2] == b'\x1f\x8b' else "r:xz"
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode=mode) as tf:
        root = tf.getnames()[0].split("/")[0] + "/"
        for member in tf.getmembers():
            if member.isdir():
                continue
            rel = member.name[len(root):]
            if not rel:
                continue
            if not any(rel == p or rel.startswith(p) for p in _KEEP_PREFIXES_UNIX):
                continue
            dest = _safe_destination(output, rel)
            dest.parent.mkdir(parents=True, exist_ok=True)

            if member.issym():
                _validate_symlink_target(output, dest, member.linkname)
                launcher_cli = _UNIX_NPM_LAUNCHERS.get(rel)
                if launcher_cli is None:
                    raise ValueError(
                        f"unsupported retained archive symlink: {rel} -> "
                        f"{member.linkname}"
                    )
                expected_target = f"../lib/node_modules/npm/bin/{launcher_cli}"
                if member.linkname != expected_target:
                    raise ValueError(
                        f"unexpected archive symlink target for {rel}: "
                        f"{member.linkname}"
                    )
                dest.unlink(missing_ok=True)
                _write_unix_npm_launcher(dest, launcher_cli)
                print(f"  {rel} (relocation-safe launcher)")
                continue
            if not member.isfile():
                raise ValueError(f"unsupported archive entry type: {rel}")

            src = tf.extractfile(member)
            if src:
                with open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                # Preserve executable bits
                if member.mode & stat.S_IXUSR:
                    dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            print(f"  {rel}")


def _runtime_paths(output: Path, is_windows: bool) -> dict[str, Path]:
    if is_windows:
        return {
            "node": output / "node.exe",
            "npm": output / "npm.cmd",
            "npx": output / "npx.cmd",
        }
    return {
        "node": output / "bin" / "node",
        "npm": output / "bin" / "npm",
        "npx": output / "bin" / "npx",
    }


def _version_command(executable: Path, is_windows: bool) -> list[str]:
    if is_windows and executable.suffix.lower() == ".cmd":
        # Python's Windows argv encoding and cmd.exe both interpret quotes,
        # which makes direct .cmd acceptance checks path-dependent.  Execute
        # npm's authenticated JavaScript entry point with the bundled
        # node.exe instead.  The .cmd launcher is still required above as a
        # packaged entry point, while this command validates npm/npx itself
        # without a shell in the middle.
        cli = (
            executable.parent
            / "node_modules"
            / "npm"
            / "bin"
            / f"{executable.stem}-cli.js"
        )
        return [
            str(executable.parent / "node.exe"),
            str(cli),
            "--version",
        ]
    return [str(executable), "--version"]


def _verify_node_runtime(
    output: Path,
    key: tuple[str, str],
    *,
    run=subprocess.run,
) -> dict[str, str]:
    """Execute the extracted node, npm and npx entry points."""
    is_windows = key[0] == "Windows"
    paths = _runtime_paths(output, is_windows)
    for name, executable in paths.items():
        if not executable.exists():
            raise ValueError(f"{name} executable not found at {executable}")

    bin_dir = output if is_windows else output / "bin"
    env = os.environ.copy()
    prior_path = env.get("PATH", "")
    env["PATH"] = str(bin_dir) + (os.pathsep + prior_path if prior_path else "")

    versions: dict[str, str] = {}
    for name, executable in paths.items():
        command = _version_command(executable, is_windows)
        try:
            result = run(
                command,
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            detail = getattr(exc, "stderr", "") or getattr(exc, "stdout", "") or str(exc)
            raise ValueError(f"{name} --version failed: {str(detail).strip()}") from exc

        version = (result.stdout or result.stderr or "").strip()
        if name == "node":
            expected = f"v{NODE_VERSION}"
            if version != expected:
                raise ValueError(f"expected Node {expected}, got {version or 'no output'}")
        elif not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", version):
            raise ValueError(f"invalid {name} version output: {version or 'no output'}")
        versions[name] = version

    return versions


def install_node_runtime(
    key: tuple[str, str],
    output: Path,
    *,
    download=_download,
    extract_windows=_extract_windows,
    extract_unix=_extract_unix,
    verify_runtime=_verify_node_runtime,
) -> Path:
    """Download, authenticate, and extract the Node runtime for ``key``."""
    url = _URLS.get(key)
    if not url:
        raise ValueError(f"Unsupported platform: {key}; supported: {list(_URLS.keys())}")

    archive_name = url.rsplit("/", 1)[-1]
    data = download(url)
    print(f"Downloaded {len(data) // (1024 * 1024)} MB")
    shasums = download(_shasums_url(url))
    _verify_archive_checksum(data, shasums, archive_name)

    is_win = key[0] == "Windows"
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{output.name}.staging-",
            dir=output.parent,
        )
    )
    backup: Path | None = None
    replacement_succeeded = False
    try:
        if is_win:
            extract_windows(data, staging)
        else:
            extract_unix(data, staging)

        versions = verify_runtime(staging, key)

        # Only replace a known-good existing runtime after the complete new
        # toolchain has passed execution checks. Both renames stay on the same
        # filesystem, and a failed second rename restores the previous tree.
        if output.exists() or output.is_symlink():
            backup = Path(
                tempfile.mkdtemp(
                    prefix=f".{output.name}.backup-",
                    dir=output.parent,
                )
            )
            backup.rmdir()
            os.replace(output, backup)
        try:
            os.replace(staging, output)
        except Exception:
            if backup is not None and backup.exists():
                try:
                    os.replace(backup, output)
                except Exception as rollback_error:
                    # Never delete the user's only known-good runtime after a
                    # double filesystem failure.  Leave it at the reported
                    # backup path for manual recovery.
                    preserved_backup = backup
                    backup = None
                    raise RuntimeError(
                        "Node runtime replacement and rollback both failed; "
                        f"the previous runtime is preserved at {preserved_backup}"
                    ) from rollback_error
                else:
                    backup = None
            raise
        else:
            replacement_succeeded = True
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if backup is not None and backup.exists():
            if replacement_succeeded:
                try:
                    shutil.rmtree(backup)
                except OSError as exc:
                    print(
                        f"WARNING: could not remove Node runtime backup "
                        f"{backup}: {exc}"
                    )
            else:
                print(
                    f"WARNING: previous Node runtime preserved at {backup} "
                    "after an incomplete replacement"
                )

    print(
        f"\nNode.js {NODE_VERSION} ready at: {output} "
        f"(npm {versions['npm']}, npx {versions['npx']})"
    )
    total_size = sum(f.stat().st_size for f in output.rglob("*") if f.is_file())
    print(f"Total size: {total_size // (1024 * 1024)} MB")
    return output


def main():
    parser = argparse.ArgumentParser(description="Download Node.js runtime for bundling")
    parser.add_argument("--output", default="resources/nodejs", help="Output directory")
    parser.add_argument("--platform", help="Override platform (e.g. 'Darwin-arm64')")
    args = parser.parse_args()

    if args.platform:
        parts = args.platform.split("-")
        key = (parts[0], parts[1])
    else:
        key = _platform_key()

    output = Path(args.output)
    try:
        install_node_runtime(key, output)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
