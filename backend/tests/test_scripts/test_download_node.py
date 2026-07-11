from __future__ import annotations

import hashlib
import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts import download_node


def test_matching_official_checksum_is_verified_before_extraction(tmp_path: Path) -> None:
    key = ("Darwin", "arm64")
    archive_url = download_node._URLS[key]
    archive_name = archive_url.rsplit("/", 1)[-1]
    archive = b"trusted Node archive bytes"
    checksum = hashlib.sha256(archive).hexdigest()
    shasums = f"{checksum}  {archive_name}\n".encode()
    downloads = {
        archive_url: archive,
        download_node._shasums_url(archive_url): shasums,
    }
    events: list[str] = []

    def fake_download(url: str) -> bytes:
        events.append(f"download:{url.rsplit('/', 1)[-1]}")
        return downloads[url]

    def fake_extract(data: bytes, output: Path) -> None:
        events.append("extract")
        assert data == archive
        (output / "bin").mkdir(parents=True)
        (output / "bin" / "node").write_bytes(b"node")

    output = tmp_path / "nodejs"
    download_node.install_node_runtime(
        key,
        output,
        download=fake_download,
        extract_unix=fake_extract,
    )

    assert events == [
        f"download:{archive_name}",
        "download:SHASUMS256.txt",
        "extract",
    ]
    assert (output / "bin" / "node").is_file()


def test_checksum_mismatch_aborts_before_deleting_or_extracting(tmp_path: Path) -> None:
    key = ("Darwin", "x86_64")
    archive_url = download_node._URLS[key]
    archive_name = archive_url.rsplit("/", 1)[-1]
    archive = b"tampered Node archive bytes"
    shasums = f"{'0' * 64}  {archive_name}\n".encode()
    downloads = {
        archive_url: archive,
        download_node._shasums_url(archive_url): shasums,
    }
    output = tmp_path / "nodejs"
    output.mkdir()
    sentinel = output / "existing-runtime"
    sentinel.write_text("keep until verification succeeds")
    extracted = False

    def fake_extract(_data: bytes, _output: Path) -> None:
        nonlocal extracted
        extracted = True

    with pytest.raises(ValueError, match=r"SHA-256 checksum mismatch"):
        download_node.install_node_runtime(
            key,
            output,
            download=downloads.__getitem__,
            extract_unix=fake_extract,
        )

    assert extracted is False
    assert sentinel.read_text() == "keep until verification succeeds"


def test_missing_archive_entry_in_official_shasums_is_rejected(tmp_path: Path) -> None:
    key = ("Linux", "x86_64")
    archive_url = download_node._URLS[key]
    downloads = {
        archive_url: b"archive",
        download_node._shasums_url(archive_url): f"{'a' * 64}  another-file.tar.xz\n".encode(),
    }

    with pytest.raises(ValueError, match=r"not listed in official SHASUMS256\.txt"):
        download_node.install_node_runtime(
            key,
            tmp_path / "nodejs",
            download=downloads.__getitem__,
            extract_unix=lambda _data, _output: pytest.fail("must not extract"),
        )


def test_windows_extractor_rejects_path_traversal(tmp_path: Path) -> None:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(
            "node-v22.22.0-win-x64/node_modules/npm/../../../escaped",
            b"untrusted",
        )

    output = tmp_path / "nodejs"
    with pytest.raises(ValueError, match="unsafe archive path"):
        download_node._extract_windows(archive.getvalue(), output)

    assert not (tmp_path / "escaped").exists()


def test_unix_extractor_rejects_path_traversal(tmp_path: Path) -> None:
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
        payload = b"untrusted"
        member = tarfile.TarInfo(
            "node-v22.22.0-darwin-arm64/lib/node_modules/npm/../../../escaped"
        )
        member.size = len(payload)
        bundle.addfile(member, io.BytesIO(payload))

    output = tmp_path / "nodejs"
    with pytest.raises(ValueError, match="unsafe archive path"):
        download_node._extract_unix(archive.getvalue(), output)

    assert not (tmp_path / "escaped").exists()


def test_extractors_keep_the_node_distribution_license(tmp_path: Path) -> None:
    windows_archive = io.BytesIO()
    with zipfile.ZipFile(windows_archive, "w") as bundle:
        bundle.writestr("node-v22.22.0-win-x64/LICENSE", b"Node.js license")
        bundle.writestr("node-v22.22.0-win-x64/node.exe", b"node")

    windows_output = tmp_path / "windows-nodejs"
    download_node._extract_windows(windows_archive.getvalue(), windows_output)
    assert (windows_output / "LICENSE").read_bytes() == b"Node.js license"

    unix_archive = io.BytesIO()
    with tarfile.open(fileobj=unix_archive, mode="w:gz") as bundle:
        for name, payload in [
            ("node-v22.22.0-darwin-arm64/LICENSE", b"Node.js license"),
            ("node-v22.22.0-darwin-arm64/bin/node", b"node"),
        ]:
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            bundle.addfile(member, io.BytesIO(payload))

    unix_output = tmp_path / "unix-nodejs"
    download_node._extract_unix(unix_archive.getvalue(), unix_output)
    assert (unix_output / "LICENSE").read_bytes() == b"Node.js license"
