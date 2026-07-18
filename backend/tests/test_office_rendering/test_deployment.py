from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import struct

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from app.office_rendering import (
    ATTESTATION_SCHEMA_VERSION,
    AUTHORITATIVE_QUALITY,
    AuthoritativeRendererReleaseIdentity,
)
from app.office_rendering.attested import attestation_payload_bytes
from app.office_rendering.deployment import (
    AttestedOfficeRendererDeployment,
    OfficeRendererDeploymentError,
    authoritative_office_renderer_self_test,
    bind_attested_native_sandbox_contract,
    bind_authoritative_renderer_probe,
    build_attested_office_render_provider,
    fingerprint_office_renderer_bundle,
)
from app.office_rendering.libreoffice import (
    ExecutableIdentity,
    LibreOfficeRenderProvider,
    LibreOfficeToolchain,
)
from app.office_rendering.runtime import fingerprint_font_environment


APP_VERSION = "1.1.0"
RELEASE_COMMIT = "a" * 40
RELEASE_IDENTITY = AuthoritativeRendererReleaseIdentity(
    app_version=APP_VERSION,
    release_commit=RELEASE_COMMIT,
)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _elf64_binary(*dependencies: str) -> bytes:
    string_table = bytearray(b"\x00")
    offsets: list[int] = []
    for dependency in dependencies:
        offsets.append(len(string_table))
        string_table.extend(dependency.encode("ascii") + b"\x00")
    dynamic_entries = [
        *((1, offset) for offset in offsets),
        (5, 0x400300),
        (10, len(string_table)),
        (0, 0),
    ]
    dynamic = b"".join(struct.pack("<qQ", *entry) for entry in dynamic_entries)
    data = bytearray(max(0x400, 0x300 + len(string_table)))
    data[:16] = b"\x7fELF\x02\x01\x01" + b"\x00" * 9
    struct.pack_into(
        "<HHIQQQIHHHHHH",
        data,
        16,
        3,
        62,
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
        "<IIQQQQQQ",
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
        "<IIQQQQQQ",
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


def _release_bundle(tmp_path: Path) -> tuple[AttestedOfficeRendererDeployment, bytes]:
    root = tmp_path / "attested-office"
    (root / "bin").mkdir(parents=True)
    (root / "fonts").mkdir()
    (root / "lib" / "libreoffice" / "program").mkdir(parents=True)
    (root / "share" / "filter").mkdir(parents=True)
    soffice = root / "bin" / "soffice"
    pdftoppm = root / "bin" / "pdftoppm"
    font_manifest = root / "font-manifest.json"
    license_manifest = root / "license-manifest.json"
    dependency_manifest = root / "dependency-manifest.json"
    sandbox_manifest = root / "sandbox-manifest.json"
    sandbox_launcher = root / "bin" / "suxiaoyou-office-sandbox-launcher"
    sandbox_probe = root / "bin" / "suxiaoyou-office-sandbox-probe"
    soffice.write_bytes(_elf64_binary("libc.so.6"))
    pdftoppm.write_bytes(_elf64_binary("libc.so.6"))
    sandbox_launcher.write_bytes(_elf64_binary("libc.so.6"))
    sandbox_probe.write_bytes(_elf64_binary("libc.so.6"))
    font_manifest.write_bytes(b'{"fonts":["CJK.ttf"]}\n')
    license_manifest.write_bytes(b'{"licenses":["OFL-1.1"]}\n')
    (root / "fonts" / "CJK.ttf").write_bytes(b"release-reviewed-font")
    (root / "lib" / "libreoffice" / "program" / "libmergedlo.so").write_bytes(
        b"release-reviewed-library"
    )
    (root / "share" / "filter" / "writer8.xcu").write_bytes(
        b"release-reviewed-filter"
    )
    helper = root / "bin" / "uno-helper"
    helper.write_bytes(b"release-reviewed-helper")
    for path in (soffice, pdftoppm, sandbox_launcher, sandbox_probe):
        path.chmod(0o755)
    helper.chmod(0o755)
    native_files = []
    for relative, path in (
        ("bin/pdftoppm", pdftoppm),
        ("bin/soffice", soffice),
        ("bin/suxiaoyou-office-sandbox-launcher", sandbox_launcher),
        ("bin/suxiaoyou-office-sandbox-probe", sandbox_probe),
    ):
        native_files.append(
            {
                "dependencies": [{"name": "libc.so.6", "scope": "system"}],
                "kind": "executable",
                "path": relative,
                "sha256": _digest(path),
                "size": path.stat().st_size,
            }
        )
    native_files.sort(key=lambda item: item["path"])
    dependency_manifest.write_bytes(
        (
            json.dumps(
                {
                    "files": native_files,
                    "platform_target": "linux-x64",
                    "schema_version": 1,
                },
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("ascii")
    )
    sandbox_manifest.write_bytes(
        (
            json.dumps(
                {
                    "capabilities": {
                        name: True
                        for name in sorted(
                            {
                                "cgroup",
                                "host_filesystem_read_only",
                                "mount_namespace",
                                "network_denied",
                                "network_namespace",
                                "private_input_read_only",
                                "private_output_write_only",
                                "process_tree_contained",
                                "seccomp",
                                "user_namespace",
                            }
                        )
                    },
                    "contract_id": (
                        "suxiaoyou.office-sandbox."
                        "linux-namespaces-seccomp-cgroup.v1"
                    ),
                    "launcher_path": (
                        "bin/suxiaoyou-office-sandbox-launcher"
                    ),
                    "platform_target": "linux-x64",
                    "schema_version": 1,
                },
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("ascii")
    )
    components = {
        "bundle-tree": fingerprint_office_renderer_bundle(root),
        "dependency-manifest": _digest(dependency_manifest),
        "font-manifest": _digest(font_manifest),
        "license-manifest": _digest(license_manifest),
        "pdftoppm": _digest(pdftoppm),
        "sandbox-manifest": _digest(sandbox_manifest),
        "soffice": _digest(soffice),
    }
    font_digest = fingerprint_font_environment(roots=(root / "fonts",))
    toolchain = LibreOfficeToolchain(
        soffice=ExecutableIdentity(path=soffice, sha256=components["soffice"]),
        pdftoppm=ExecutableIdentity(path=pdftoppm, sha256=components["pdftoppm"]),
    )
    descriptor = LibreOfficeRenderProvider(
        font_digest=font_digest,
        toolchain=toolchain,
        platform_name="linux",
        environ={},
    ).descriptor
    private = Ed25519PrivateKey.generate()
    payload = attestation_payload_bytes(
        app_version=APP_VERSION,
        release_commit=RELEASE_COMMIT,
        platform_target="linux-x64",
        base_renderer_id=descriptor.renderer_id,
        base_renderer_version=descriptor.renderer_version,
        font_digest=font_digest,
        components=components,
    )
    signed = {
        "schema_version": ATTESTATION_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "release_commit": RELEASE_COMMIT,
        "platform_target": "linux-x64",
        "base_renderer_id": descriptor.renderer_id,
        "base_renderer_version": descriptor.renderer_version,
        "font_digest": font_digest,
        "components": components,
        "signature": base64.b64encode(private.sign(payload)).decode("ascii"),
    }
    # Keep the JSON keys canonical.  The attestation parser separately rejects
    # duplicate object keys and its model requires sorted component keys.
    (root / "office-renderer-attestation.json").write_text(
        json.dumps(signed, separators=(",", ":")), encoding="utf-8"
    )
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return AttestedOfficeRendererDeployment(root=root), public


def _install_test_release_key(monkeypatch: pytest.MonkeyPatch, public: bytes) -> None:
    monkeypatch.setattr(
        "app.office_rendering.deployment.AUTHORITATIVE_RENDERER_PUBLIC_KEY", public
    )
    monkeypatch.setattr(
        "app.office_rendering.deployment.current_platform_target", lambda: "linux-x64"
    )


def test_only_exact_signed_private_bundle_becomes_authoritative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment, public = _release_bundle(tmp_path)
    _install_test_release_key(monkeypatch, public)
    secret = "must-not-enter-renderer-self-test"
    monkeypatch.setenv("SUXIAOYOU_OFFICE_RENDERER_SECRET", secret)

    provider = build_attested_office_render_provider(
        deployment=deployment,
        release_identity=RELEASE_IDENTITY,
    )

    assert provider.availability().available
    assert provider.descriptor.quality == AUTHORITATIVE_QUALITY
    assert provider.delegate.descriptor.quality == "approximate"  # type: ignore[attr-defined]
    probe_binding = bind_authoritative_renderer_probe(
        provider,
        deployment=deployment,
    )
    assert probe_binding.bundle_root == deployment.root.resolve(strict=True)
    assert (
        probe_binding.bundle_tree_sha256
        == provider.attestation.components["bundle-tree"]  # type: ignore[attr-defined]
    )
    assert str(deployment.root) not in repr(probe_binding)
    native_sandbox_contract = bind_attested_native_sandbox_contract(provider)
    assert native_sandbox_contract is provider.delegate.native_sandbox_contract  # type: ignore[attr-defined]
    assert str(deployment.root) not in repr(native_sandbox_contract)

    report = authoritative_office_renderer_self_test(
        deployment=deployment,
        release_identity=RELEASE_IDENTITY,
    )
    assert report == {
        "schema_version": 2,
        "status": "ok",
        "available": True,
        "quality": "authoritative",
        "app_version": APP_VERSION,
        "release_commit": RELEASE_COMMIT,
        "platform_target": "linux-x64",
        "renderer_id": "suxiaoyou-attested-office",
        "renderer_version": f"attestation-{report['attestation_sha256']}",
        "font_digest": provider.descriptor.font_digest,
        "attestation_sha256": report["attestation_sha256"],
        "bundle_tree_sha256": provider.attestation.components["bundle-tree"],  # type: ignore[attr-defined]
        "native_closure_sha256": report["native_closure_sha256"],
        "native_dependency_count": 4,
        "native_file_count": 4,
        "font_tree_sha256": report["font_tree_sha256"],
        "native_sandbox_contract": {
            "schema_version": 1,
            "status": "declared-not-proven",
            "platform_target": "linux-x64",
            "contract_id": (
                "suxiaoyou.office-sandbox."
                "linux-namespaces-seccomp-cgroup.v1"
            ),
            "capabilities": [
                "cgroup",
                "host_filesystem_read_only",
                "mount_namespace",
                "network_denied",
                "network_namespace",
                "private_input_read_only",
                "private_output_write_only",
                "process_tree_contained",
                "seccomp",
                "user_namespace",
            ],
            "sandbox_manifest_sha256": provider.attestation.components[  # type: ignore[attr-defined]
                "sandbox-manifest"
            ],
            "dependency_manifest_sha256": provider.attestation.components[  # type: ignore[attr-defined]
                "dependency-manifest"
            ],
            "bundle_tree_sha256": provider.attestation.components[  # type: ignore[attr-defined]
                "bundle-tree"
            ],
            "launcher_sha256": _digest(
                deployment.root / "bin" / "suxiaoyou-office-sandbox-launcher"
            ),
            "native_behavior_proven": False,
            "adversarial_evidence_required": True,
        },
        "component_count": 7,
        "components": [
            "bundle-tree",
            "dependency-manifest",
            "font-manifest",
            "license-manifest",
            "pdftoppm",
            "sandbox-manifest",
            "soffice",
        ],
    }
    serialized_report = json.dumps(report)
    assert str(deployment.root) not in serialized_report
    assert secret not in serialized_report

    # The wrapper re-fingerprints the deployment, so a post-startup font swap
    # cannot leave an authoritative descriptor cached in memory.
    (deployment.root / "fonts" / "CJK.ttf").write_bytes(b"changed-after-startup")
    assert not provider.availability().available
    assert provider.availability().reason == "Authoritative Office renderer is unavailable"
    with pytest.raises(OfficeRendererDeploymentError, match="unavailable"):
        bind_attested_native_sandbox_contract(provider)


def test_authoritative_renderer_self_test_rejects_unavailable_bundle(
    tmp_path: Path,
) -> None:
    deployment = AttestedOfficeRendererDeployment(root=(tmp_path / "missing").absolute())

    with pytest.raises(
        OfficeRendererDeploymentError,
        match="Authoritative Office renderer is unavailable",
    ):
        authoritative_office_renderer_self_test(
            deployment=deployment,
            release_identity=RELEASE_IDENTITY,
        )


def test_missing_or_replayed_release_identity_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment, public = _release_bundle(tmp_path)
    _install_test_release_key(monkeypatch, public)
    monkeypatch.setenv("SUXIAOYOU_RELEASE_COMMIT", RELEASE_COMMIT)
    monkeypatch.setenv("SUXIAOYOU_APP_VERSION", APP_VERSION)

    missing = build_attested_office_render_provider(deployment=deployment)
    assert not missing.availability().available

    for replay_identity in (
        AuthoritativeRendererReleaseIdentity(
            app_version="1.2.0",
            release_commit=RELEASE_COMMIT,
        ),
        AuthoritativeRendererReleaseIdentity(
            app_version=APP_VERSION,
            release_commit="b" * 40,
        ),
    ):
        replay = build_attested_office_render_provider(
            deployment=deployment,
            release_identity=replay_identity,
        )
        assert not replay.availability().available

    with pytest.raises(OfficeRendererDeploymentError):
        authoritative_office_renderer_self_test(deployment=deployment)


def test_legacy_v1_attestation_is_not_publishable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment, public = _release_bundle(tmp_path)
    _install_test_release_key(monkeypatch, public)
    path = deployment.root / "office-renderer-attestation.json"
    legacy = json.loads(path.read_text(encoding="utf-8"))
    legacy["schema_version"] = 1
    del legacy["app_version"]
    del legacy["release_commit"]
    path.write_text(json.dumps(legacy, separators=(",", ":")), encoding="utf-8")

    provider = build_attested_office_render_provider(
        deployment=deployment,
        release_identity=RELEASE_IDENTITY,
    )

    assert not provider.availability().available


def test_fixed_key_bad_bytes_or_component_drift_close_without_leaking_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment, public = _release_bundle(tmp_path)
    monkeypatch.setattr(
        "app.office_rendering.deployment.current_platform_target", lambda: "linux-x64"
    )

    wrong_key_provider = build_attested_office_render_provider(
        deployment=deployment,
        release_identity=RELEASE_IDENTITY,
    )
    assert not wrong_key_provider.availability().available
    assert wrong_key_provider.availability().reason == "Authoritative Office renderer is unavailable"

    _install_test_release_key(monkeypatch, public)
    (deployment.root / "bin" / "soffice").write_bytes(b"tampered")
    drifted_provider = build_attested_office_render_provider(
        deployment=deployment,
        release_identity=RELEASE_IDENTITY,
    )
    assert not drifted_provider.availability().available
    assert drifted_provider.availability().reason == "Authoritative Office renderer is unavailable"
    assert str(deployment.root) not in drifted_provider.availability().reason


@pytest.mark.parametrize(
    ("relative_path", "replacement"),
    [
        ("lib/libreoffice/program/libmergedlo.so", b"tampered-library"),
        ("share/filter/writer8.xcu", b"tampered-filter"),
        ("bin/uno-helper", b"tampered-helper"),
    ],
)
def test_nested_bundle_file_drift_closes_authoritative_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
    replacement: bytes,
) -> None:
    deployment, public = _release_bundle(tmp_path)
    _install_test_release_key(monkeypatch, public)
    provider = build_attested_office_render_provider(
        deployment=deployment,
        release_identity=RELEASE_IDENTITY,
    )
    assert provider.availability().available

    deployment.root.joinpath(*relative_path.split("/")).write_bytes(replacement)

    assert not provider.availability().available
    assert provider.availability().reason == "Authoritative Office renderer is unavailable"


def test_unsigned_added_file_closes_authoritative_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment, public = _release_bundle(tmp_path)
    _install_test_release_key(monkeypatch, public)
    provider = build_attested_office_render_provider(
        deployment=deployment,
        release_identity=RELEASE_IDENTITY,
    )
    assert provider.availability().available

    (deployment.root / "lib" / "libreoffice" / "program" / "injected.so").write_bytes(
        b"not-in-the-signed-tree"
    )

    assert not provider.availability().available
    assert provider.availability().reason == "Authoritative Office renderer is unavailable"


def test_safe_mode_drift_is_part_of_the_signed_tree_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment, public = _release_bundle(tmp_path)
    _install_test_release_key(monkeypatch, public)
    provider = build_attested_office_render_provider(
        deployment=deployment,
        release_identity=RELEASE_IDENTITY,
    )
    assert provider.availability().available

    helper = deployment.root / "bin" / "uno-helper"
    helper.chmod(0o744)

    assert not provider.availability().available


def test_unlisted_symlink_or_writable_nested_file_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment, public = _release_bundle(tmp_path)
    _install_test_release_key(monkeypatch, public)
    nested = deployment.root / "share" / "filter" / "writer8.xcu"
    nested.chmod(0o666)
    assert not build_attested_office_render_provider(
        deployment=deployment,
        release_identity=RELEASE_IDENTITY,
    ).availability().available

    deployment, public = _release_bundle(tmp_path / "symlink")
    _install_test_release_key(monkeypatch, public)
    link = deployment.root / "share" / "filter" / "redirected.xcu"
    try:
        link.symlink_to(deployment.root / "share" / "filter" / "writer8.xcu")
    except OSError:
        pytest.skip("symbolic links are unavailable")
    assert not build_attested_office_render_provider(
        deployment=deployment,
        release_identity=RELEASE_IDENTITY,
    ).availability().available


def test_tree_drift_during_fingerprinting_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment, _public = _release_bundle(tmp_path)
    from app.office_rendering import deployment as deployment_module

    original_hash = deployment_module._sha256_private_file
    injected = False

    def _hash_and_mutate(path: Path, *, max_bytes: int) -> str:
        nonlocal injected
        result = original_hash(path, max_bytes=max_bytes)
        if not injected:
            injected = True
            (deployment.root / "lib" / "late-injection.so").write_bytes(b"late")
        return result

    monkeypatch.setattr(deployment_module, "_sha256_private_file", _hash_and_mutate)

    with pytest.raises(OfficeRendererDeploymentError, match="bundle changed"):
        fingerprint_office_renderer_bundle(deployment.root)


def test_environment_cannot_select_or_upgrade_a_renderer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment, public = _release_bundle(tmp_path)
    _install_test_release_key(monkeypatch, public)
    monkeypatch.setenv("SUXIAOYOU_OFFICE_RENDERER_ROOT", str(deployment.root))
    monkeypatch.setattr(
        "app.office_rendering.deployment._application_data_root", lambda: tmp_path / "empty-app-data"
    )

    provider = build_attested_office_render_provider(
        release_identity=RELEASE_IDENTITY,
    )

    assert not provider.availability().available
    assert provider.availability().reason == "Authoritative Office renderer is unavailable"


def test_duplicate_json_writable_or_symlinked_components_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment, public = _release_bundle(tmp_path)
    _install_test_release_key(monkeypatch, public)
    attestation = deployment.root / "office-renderer-attestation.json"
    original = attestation.read_text(encoding="utf-8")
    attestation.write_text(
        original[:-1] + ',"signature":"duplicated"}', encoding="utf-8"
    )
    duplicate = build_attested_office_render_provider(
        deployment=deployment,
        release_identity=RELEASE_IDENTITY,
    )
    assert not duplicate.availability().available

    # Rebuild restores a valid manifest, then an unsafe component must still
    # fail before its contents can be trusted.
    deployment, public = _release_bundle(tmp_path / "next")
    _install_test_release_key(monkeypatch, public)
    component = deployment.root / "license-manifest.json"
    component.chmod(0o666)
    unsafe = build_attested_office_render_provider(
        deployment=deployment,
        release_identity=RELEASE_IDENTITY,
    )
    assert not unsafe.availability().available

    component.chmod(0o644)
    target = deployment.root / "outside-license.json"
    target.write_bytes(component.read_bytes())
    component.unlink()
    try:
        component.symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    redirected = build_attested_office_render_provider(
        deployment=deployment,
        release_identity=RELEASE_IDENTITY,
    )
    assert not redirected.availability().available


def test_deployment_config_rejects_noncanonical_or_nonprivate_paths(tmp_path: Path) -> None:
    with pytest.raises(OfficeRendererDeploymentError):
        AttestedOfficeRendererDeployment(root=Path("relative"))
    with pytest.raises(OfficeRendererDeploymentError):
        AttestedOfficeRendererDeployment(
            root=tmp_path.absolute(),
            component_paths={
                "dependency-manifest": PurePosixPath("dependency-manifest.json"),
                "font-manifest": PurePosixPath("font-manifest.json"),
                "license-manifest": PurePosixPath("license-manifest.json"),
                "pdftoppm": PurePosixPath("bin/pdftoppm"),
                "soffice": PurePosixPath("../soffice"),
            },
        )


def test_windows_deployment_uses_executable_component_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.office_rendering.deployment.current_platform_target",
        lambda: "windows-x64",
    )

    deployment = AttestedOfficeRendererDeployment(root=tmp_path.absolute())

    assert deployment.component_paths["soffice"] == PurePosixPath("bin/soffice.exe")
    assert deployment.component_paths["pdftoppm"] == PurePosixPath("bin/pdftoppm.exe")
    assert deployment.component_paths["dependency-manifest"] == PurePosixPath(
        "dependency-manifest.json"
    )
