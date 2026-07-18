from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import struct

import pytest

from app.office_rendering.native_bundle import (
    DEPENDENCY_MANIFEST_FILENAME,
    NativeBundleVerificationError,
    verify_native_bundle,
)


def _elf64(
    target: str,
    dependencies: list[str],
    *,
    search_paths: list[str] | None = None,
) -> bytes:
    endian = "<"
    machine = 183 if target.endswith("-arm64") else 62
    string_table = bytearray(b"\x00")
    name_offsets: list[int] = []
    for name in dependencies:
        name_offsets.append(len(string_table))
        string_table.extend(name.encode("utf-8") + b"\x00")
    runpath_offset: int | None = None
    if search_paths:
        runpath_offset = len(string_table)
        string_table.extend(":".join(search_paths).encode("utf-8") + b"\x00")
    dynamic_entries = [
        *((1, offset) for offset in name_offsets),
        (5, 0x400300),
        (10, len(string_table)),
    ]
    if runpath_offset is not None:
        dynamic_entries.append((29, runpath_offset))
    dynamic_entries.append((0, 0))
    dynamic = b"".join(struct.pack(endian + "qQ", *entry) for entry in dynamic_entries)
    size = max(0x400, 0x300 + len(string_table))
    data = bytearray(size)
    data[:16] = b"\x7fELF\x02\x01\x01" + b"\x00" * 9
    struct.pack_into(
        endian + "HHIQQQIHHHHHH",
        data,
        16,
        3,
        machine,
        1,
        0,
        64,
        0,
        0,
        64,
        56,
        2,
        0,
        0,
        0,
    )
    struct.pack_into(
        endian + "IIQQQQQQ",
        data,
        64,
        1,
        5,
        0,
        0x400000,
        0x400000,
        len(data),
        len(data),
        0x1000,
    )
    struct.pack_into(
        endian + "IIQQQQQQ",
        data,
        120,
        2,
        4,
        0x200,
        0x400200,
        0x400200,
        len(dynamic),
        len(dynamic),
        8,
    )
    data[0x200 : 0x200 + len(dynamic)] = dynamic
    data[0x300 : 0x300 + len(string_table)] = string_table
    return bytes(data)


def _macho64(
    target: str,
    dependencies: list[str],
    *,
    search_paths: list[str] | None = None,
    kind: str = "executable",
) -> bytes:
    commands: list[bytes] = []
    for name in dependencies:
        encoded = name.encode("utf-8") + b"\x00"
        command_size = (24 + len(encoded) + 7) & ~7
        command = bytearray(command_size)
        struct.pack_into("<IIIIII", command, 0, 0x0C, command_size, 24, 0, 0, 0)
        command[24 : 24 + len(encoded)] = encoded
        commands.append(bytes(command))
    for path in search_paths or []:
        encoded = path.encode("utf-8") + b"\x00"
        command_size = (12 + len(encoded) + 7) & ~7
        command = bytearray(command_size)
        struct.pack_into("<III", command, 0, 0x8000001C, command_size, 12)
        command[12 : 12 + len(encoded)] = encoded
        commands.append(bytes(command))
    cpu_type = 0x0100000C if target.endswith("-arm64") else 0x01000007
    header = struct.pack(
        "<IiiIIIII",
        0xFEEDFACF,
        cpu_type,
        0,
        2 if kind == "executable" else 6,
        len(commands),
        sum(len(command) for command in commands),
        0,
        0,
    )
    return header + b"".join(commands)


def _pe64(
    target: str,
    dependencies: list[str],
    *,
    kind: str = "executable",
) -> bytes:
    pe_offset = 0x80
    optional_size = 240
    section_offset = pe_offset + 24 + optional_size
    raw_offset = 0x200
    virtual_address = 0x1000
    data = bytearray(0x1000)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, pe_offset)
    data[pe_offset : pe_offset + 4] = b"PE\x00\x00"
    machine = 0xAA64 if target.endswith("-arm64") else 0x8664
    struct.pack_into(
        "<HHIIIHH",
        data,
        pe_offset + 4,
        machine,
        1,
        0,
        0,
        0,
        optional_size,
        0x0022 | (0x2000 if kind == "library" else 0),
    )
    optional_offset = pe_offset + 24
    struct.pack_into("<H", data, optional_offset, 0x20B)
    struct.pack_into("<I", data, optional_offset + 108, 16)
    descriptor_size = 20 * (len(dependencies) + 1)
    if dependencies:
        struct.pack_into(
            "<II", data, optional_offset + 120, virtual_address, descriptor_size
        )
    data[section_offset : section_offset + 8] = b".rdata\x00\x00"
    struct.pack_into(
        "<IIIIIIHHI",
        data,
        section_offset + 8,
        0x800,
        virtual_address,
        0x800,
        raw_offset,
        0,
        0,
        0,
        0,
        0x40000040,
    )
    cursor = raw_offset + descriptor_size
    for index, name in enumerate(dependencies):
        encoded = name.encode("ascii") + b"\x00"
        name_rva = virtual_address + cursor - raw_offset
        struct.pack_into(
            "<IIIII",
            data,
            raw_offset + index * 20,
            0,
            0,
            0,
            name_rva,
            0,
        )
        data[cursor : cursor + len(encoded)] = encoded
        cursor += len(encoded)
    return bytes(data)


def _binary(
    target: str,
    dependencies: list[str],
    *,
    private_resolution: bool = False,
    kind: str = "executable",
) -> bytes:
    if target.startswith("linux-"):
        return _elf64(
            target,
            dependencies,
            search_paths=["$ORIGIN/../lib"] if private_resolution else None,
        )
    if target.startswith("darwin-"):
        return _macho64(
            target,
            dependencies,
            search_paths=["@loader_path/../lib"] if private_resolution else None,
            kind=kind,
        )
    return _pe64(target, dependencies, kind=kind)


def _dependency_names(target: str) -> tuple[str, str]:
    if target.startswith("linux-"):
        return "libprivate.so", "libc.so.6"
    if target.startswith("darwin-"):
        return "@rpath/libprivate.dylib", "/usr/lib/libSystem.B.dylib"
    return "private.dll", "kernel32.dll"


def _paths(target: str) -> tuple[str, str, str]:
    if target.startswith("windows-"):
        return "bin/soffice.exe", "bin/pdftoppm.exe", "bin/private.dll"
    if target.startswith("darwin-"):
        return "bin/soffice", "bin/pdftoppm", "lib/libprivate.dylib"
    return "bin/soffice", "bin/pdftoppm", "lib/libprivate.so"


def _canonical(value: object) -> bytes:
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


def _dependency(name: str, *, private_path: str | None = None) -> dict[str, str]:
    if private_path is None:
        return {"name": name, "scope": "system"}
    return {"name": name, "path": private_path, "scope": "private"}


def _write_fixture(
    tmp_path: Path,
    target: str,
    *,
    binary_target: str | None = None,
) -> tuple[Path, tuple[PurePosixPath, PurePosixPath], dict[str, object]]:
    root = tmp_path / target
    root.mkdir(parents=True)
    private_name, system_name = _dependency_names(target)
    soffice, pdftoppm, private_library = _paths(target)
    binary_platform = binary_target or target
    payloads = {
        soffice: _binary(
            binary_platform,
            [private_name, system_name],
            private_resolution=True,
        ),
        pdftoppm: _binary(binary_platform, [system_name]),
        private_library: _binary(binary_platform, [system_name], kind="library"),
    }
    for relative, payload in payloads.items():
        path = root.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        path.chmod(0o755 if relative in {soffice, pdftoppm} else 0o644)

    dependencies = {
        soffice: sorted(
            [
                _dependency(private_name, private_path=private_library),
                _dependency(system_name),
            ],
            key=lambda item: (item["name"], item["scope"], item.get("path", "")),
        ),
        pdftoppm: [_dependency(system_name)],
        private_library: [_dependency(system_name)],
    }
    files = []
    for relative in sorted(payloads):
        payload = payloads[relative]
        files.append(
            {
                "dependencies": dependencies[relative],
                "kind": "executable" if relative in {soffice, pdftoppm} else "library",
                "path": relative,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            }
        )
    manifest: dict[str, object] = {
        "files": files,
        "platform_target": target,
        "schema_version": 1,
    }
    (root / DEPENDENCY_MANIFEST_FILENAME).write_bytes(_canonical(manifest))
    return root, (PurePosixPath(soffice), PurePosixPath(pdftoppm)), manifest


@pytest.mark.parametrize(
    "target",
    [
        "linux-x64",
        "linux-arm64",
        "darwin-x64",
        "darwin-arm64",
        "windows-x64",
        "windows-arm64",
    ],
)
def test_verifies_native_format_architecture_and_recursive_closure(
    tmp_path: Path,
    target: str,
) -> None:
    root, executables, _manifest = _write_fixture(tmp_path, target)

    closure = verify_native_bundle(
        root,
        platform_target=target,
        executable_paths=executables,
    )

    assert closure.platform_target == target
    assert closure.native_file_count == 3
    assert closure.dependency_count == 4
    assert len(closure.manifest_sha256) == 64
    assert len(closure.closure_sha256) == 64
    assert str(root) not in repr(closure)


@pytest.mark.parametrize("target", ["linux-x64", "darwin-x64", "windows-x64"])
def test_rejects_fake_entry_point_bytes_even_when_manifest_hash_matches(
    tmp_path: Path,
    target: str,
) -> None:
    root, executables, manifest = _write_fixture(tmp_path, target)
    fake = b"release-reviewed-soffice"
    root.joinpath(*executables[0].parts).write_bytes(fake)
    file_entry = next(
        item for item in manifest["files"] if item["path"] == executables[0].as_posix()  # type: ignore[index,union-attr]
    )
    file_entry["size"] = len(fake)
    file_entry["sha256"] = hashlib.sha256(fake).hexdigest()
    (root / DEPENDENCY_MANIFEST_FILENAME).write_bytes(_canonical(manifest))

    with pytest.raises(NativeBundleVerificationError):
        verify_native_bundle(root, platform_target=target, executable_paths=executables)


@pytest.mark.parametrize(
    ("target", "binary_target"),
    [
        ("linux-arm64", "linux-x64"),
        ("darwin-arm64", "darwin-x64"),
        ("windows-arm64", "windows-x64"),
    ],
)
def test_rejects_wrong_native_architecture(
    tmp_path: Path,
    target: str,
    binary_target: str,
) -> None:
    root, executables, _manifest = _write_fixture(
        tmp_path,
        target,
        binary_target=binary_target,
    )

    with pytest.raises(NativeBundleVerificationError, match="architecture"):
        verify_native_bundle(root, platform_target=target, executable_paths=executables)


def test_rejects_unlisted_native_file_and_missing_private_resolution(tmp_path: Path) -> None:
    root, executables, manifest = _write_fixture(tmp_path, "linux-x64")
    (root / "lib" / "injected.so").write_bytes(_elf64("linux-x64", ["libc.so.6"]))

    with pytest.raises(NativeBundleVerificationError, match="inventory"):
        verify_native_bundle(
            root,
            platform_target="linux-x64",
            executable_paths=executables,
        )

    (root / "lib" / "injected.so").unlink()
    soffice = next(item for item in manifest["files"] if item["path"] == "bin/soffice")  # type: ignore[index,union-attr]
    private = next(
        item for item in soffice["dependencies"] if item["scope"] == "private"  # type: ignore[index,union-attr]
    )
    private["path"] = "lib/missing.so"
    (root / DEPENDENCY_MANIFEST_FILENAME).write_bytes(_canonical(manifest))
    with pytest.raises(NativeBundleVerificationError, match="recursively"):
        verify_native_bundle(
            root,
            platform_target="linux-x64",
            executable_paths=executables,
        )


def test_private_resolution_must_match_loader_search_result(tmp_path: Path) -> None:
    root, executables, manifest = _write_fixture(tmp_path, "linux-x64")
    soffice = next(item for item in manifest["files"] if item["path"] == "bin/soffice")  # type: ignore[index,union-attr]
    private = next(
        item for item in soffice["dependencies"] if item["scope"] == "private"  # type: ignore[index,union-attr]
    )
    # The substituted target is signed and exists, but $ORIGIN/../lib cannot
    # resolve libprivate.so to it.  Hash membership alone must not be enough.
    private["path"] = "bin/pdftoppm"
    (root / DEPENDENCY_MANIFEST_FILENAME).write_bytes(_canonical(manifest))

    with pytest.raises(NativeBundleVerificationError, match="resolution"):
        verify_native_bundle(
            root,
            platform_target="linux-x64",
            executable_paths=executables,
        )


@pytest.mark.parametrize("target", ["linux-x64", "darwin-x64"])
def test_rejects_host_or_absolute_native_search_paths(tmp_path: Path, target: str) -> None:
    root, executables, manifest = _write_fixture(tmp_path, target)
    private_name, system_name = _dependency_names(target)
    if target.startswith("linux-"):
        replacement = _elf64(
            target,
            [private_name, system_name],
            search_paths=["/usr/local/lib"],
        )
    else:
        replacement = _macho64(
            target,
            [private_name, system_name],
            search_paths=["/Library/Frameworks"],
        )
    soffice_path = root.joinpath(*executables[0].parts)
    soffice_path.write_bytes(replacement)
    soffice = next(
        item for item in manifest["files"] if item["path"] == executables[0].as_posix()  # type: ignore[index,union-attr]
    )
    soffice["size"] = len(replacement)
    soffice["sha256"] = hashlib.sha256(replacement).hexdigest()
    (root / DEPENDENCY_MANIFEST_FILENAME).write_bytes(_canonical(manifest))

    with pytest.raises(NativeBundleVerificationError, match="private bundle"):
        verify_native_bundle(root, platform_target=target, executable_paths=executables)


def test_rejects_unapproved_system_dependency_and_import_drift(tmp_path: Path) -> None:
    root, executables, manifest = _write_fixture(tmp_path, "linux-x64")
    soffice = next(item for item in manifest["files"] if item["path"] == "bin/soffice")  # type: ignore[index,union-attr]
    system = next(
        item for item in soffice["dependencies"] if item["scope"] == "system"  # type: ignore[index,union-attr]
    )
    system["name"] = "libhost-injection.so"
    soffice["dependencies"] = sorted(  # type: ignore[index]
        soffice["dependencies"],  # type: ignore[index]
        key=lambda item: (item["name"], item["scope"], item.get("path", "")),
    )
    (root / DEPENDENCY_MANIFEST_FILENAME).write_bytes(_canonical(manifest))
    with pytest.raises(NativeBundleVerificationError, match="imports"):
        verify_native_bundle(
            root,
            platform_target="linux-x64",
            executable_paths=executables,
        )

    # Make the binary and declaration agree; the code-owned system allowlist
    # must still reject the host dependency.
    replacement = _elf64(
        "linux-x64",
        ["libprivate.so", "libhost-injection.so"],
        search_paths=["$ORIGIN/../lib"],
    )
    (root / "bin" / "soffice").write_bytes(replacement)
    soffice["size"] = len(replacement)
    soffice["sha256"] = hashlib.sha256(replacement).hexdigest()
    (root / DEPENDENCY_MANIFEST_FILENAME).write_bytes(_canonical(manifest))
    with pytest.raises(NativeBundleVerificationError, match="allowlisted"):
        verify_native_bundle(
            root,
            platform_target="linux-x64",
            executable_paths=executables,
        )


def test_rejects_noncanonical_duplicate_or_escaping_manifest(tmp_path: Path) -> None:
    root, executables, manifest = _write_fixture(tmp_path, "linux-x64")
    path = root / DEPENDENCY_MANIFEST_FILENAME
    path.write_bytes(json.dumps(manifest, indent=2).encode("utf-8"))
    with pytest.raises(NativeBundleVerificationError, match="canonical"):
        verify_native_bundle(
            root,
            platform_target="linux-x64",
            executable_paths=executables,
        )

    path.write_bytes(b'{"files":[],"files":[],"platform_target":"linux-x64","schema_version":1}\n')
    with pytest.raises(NativeBundleVerificationError):
        verify_native_bundle(
            root,
            platform_target="linux-x64",
            executable_paths=executables,
        )

    _root, _executables, manifest = _write_fixture(tmp_path / "next", "linux-x64")
    manifest["files"][0]["path"] = "../escaped"  # type: ignore[index]
    (_root / DEPENDENCY_MANIFEST_FILENAME).write_bytes(_canonical(manifest))
    with pytest.raises(NativeBundleVerificationError, match="path"):
        verify_native_bundle(
            _root,
            platform_target="linux-x64",
            executable_paths=_executables,
        )


def test_rejects_symlink_hash_and_unsafe_executable_mode(tmp_path: Path) -> None:
    root, executables, manifest = _write_fixture(tmp_path, "linux-x64")
    soffice = next(item for item in manifest["files"] if item["path"] == "bin/soffice")  # type: ignore[index,union-attr]
    soffice["sha256"] = "0" * 64
    (root / DEPENDENCY_MANIFEST_FILENAME).write_bytes(_canonical(manifest))
    with pytest.raises(NativeBundleVerificationError, match="identity"):
        verify_native_bundle(
            root,
            platform_target="linux-x64",
            executable_paths=executables,
        )

    root, executables, _manifest = _write_fixture(tmp_path / "mode", "linux-x64")
    (root / "bin" / "soffice").chmod(0o644)
    with pytest.raises(NativeBundleVerificationError, match="executable"):
        verify_native_bundle(
            root,
            platform_target="linux-x64",
            executable_paths=executables,
        )

    root, executables, _manifest = _write_fixture(tmp_path / "link", "linux-x64")
    target = root / "outside.so"
    target.write_bytes((root / "lib" / "libprivate.so").read_bytes())
    library = root / "lib" / "libprivate.so"
    library.unlink()
    try:
        library.symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    with pytest.raises(NativeBundleVerificationError, match="symlink"):
        verify_native_bundle(
            root,
            platform_target="linux-x64",
            executable_paths=executables,
        )
