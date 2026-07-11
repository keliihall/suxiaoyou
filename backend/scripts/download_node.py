#!/usr/bin/env python3
"""Download a minimal Node.js 22 runtime for bundling with 苏小有 desktop.

Usage:
    python scripts/download_node.py [--output resources/nodejs]

Downloads the correct Node.js binary for the current platform, extracts only
the essential files (~30 MB), and places them in the output directory.

The resulting layout:
    resources/nodejs/
        node.exe          (Windows)
        node              (macOS / Linux)
        npm.cmd / npm     (Windows / Unix)
        npx.cmd / npx     (Windows / Unix)
        node_modules/npm/ (npm package)
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import platform
import shutil
import stat
import sys
import tarfile
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
            src = tf.extractfile(member)
            if src:
                with open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                # Preserve executable bits
                if member.mode & stat.S_IXUSR:
                    dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            print(f"  {rel}")


def install_node_runtime(
    key: tuple[str, str],
    output: Path,
    *,
    download=_download,
    extract_windows=_extract_windows,
    extract_unix=_extract_unix,
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

    # Keep an existing known-good runtime until the replacement has been
    # authenticated. A network or checksum failure must not destroy it.
    if output.exists():
        print(f"Removing existing {output} ...")
        shutil.rmtree(output)

    is_win = key[0] == "Windows"
    if is_win:
        extract_windows(data, output)
    else:
        extract_unix(data, output)

    node_bin = output / ("node.exe" if is_win else "bin/node")
    if not node_bin.exists():
        raise ValueError(f"node binary not found at {node_bin}")

    print(f"\nNode.js {NODE_VERSION} ready at: {output}")
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
