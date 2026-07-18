from __future__ import annotations

import base64
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.office_rendering.probe import AuthoritativeRendererProbeManifest
from release_packaging import (
    office_renderer_stage as packaging,
    office_renderer_trust as trust,
)
from release_packaging.office_renderer_stage import (
    LOCK_FILENAME,
    PAYLOAD_CONTRACT,
    OfficeRendererAssets,
    OfficeRendererPackagingError,
    bind_office_renderer_analysis_assets,
    office_renderer_datas,
    prepare_office_renderer_assets,
    verify_office_renderer_analysis_assets,
)
from release_packaging.office_renderer_trust import (
    canonical_office_renderer_attestation_payload,
)
from release_packaging.release_identity import ReleaseIdentityValues


TARGET = "linux-x64"
RELEASE_COMMIT = "a" * 40


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


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


def _tree_digest(files: list[dict[str, object]]) -> str:
    digest = hashlib.sha256()
    for item in files:
        digest.update(
            json.dumps(
                {
                    "mode": item["mode"],
                    "path": item["path"],
                    "sha256": item["sha256"],
                    "size": item["size"],
                },
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _build_staged_renderer(
    tmp_path: Path,
    *,
    attestation_release_commit: str = RELEASE_COMMIT,
    signed_release_commit: str | None = None,
    native_soffice: bytes | None = None,
) -> tuple[Path, str, Path, bytes]:
    source = tmp_path / "source"
    payload = source / "payload" / TARGET
    for relative in ("bin", "fonts", "probe"):
        (payload / relative).mkdir(parents=True, mode=0o755)
        os.chmod(payload / relative, 0o755)
    os.chmod(payload, 0o755)

    executable_content = {
        "bin/pdftoppm": b"signed-pdftoppm-binary",
        "bin/soffice": native_soffice or b"signed-soffice-binary",
        "bin/suxiaoyou-office-sandbox-launcher": b"signed-sandbox-launcher",
        "bin/suxiaoyou-office-sandbox-probe": b"signed-sandbox-probe-helper",
    }
    dependency = {
        "files": [
            {
                "dependencies": [],
                "kind": "executable",
                "path": path,
                "sha256": _sha256(content),
                "size": len(content),
            }
            for path, content in sorted(executable_content.items())
        ],
        "platform_target": TARGET,
        "schema_version": 1,
    }
    dependency_bytes = _canonical(dependency)
    sandbox_bytes = _canonical(
        {
            "capabilities": {
                "cgroup": True,
                "host_filesystem_read_only": True,
                "mount_namespace": True,
                "network_denied": True,
                "network_namespace": True,
                "private_input_read_only": True,
                "private_output_write_only": True,
                "process_tree_contained": True,
                "seccomp": True,
                "user_namespace": True,
            },
            "contract_id": (
                "suxiaoyou.office-sandbox.linux-namespaces-seccomp-cgroup.v1"
            ),
            "launcher_path": "bin/suxiaoyou-office-sandbox-launcher",
            "platform_target": TARGET,
            "schema_version": 1,
        }
    )
    probe_source = b"canonical-authoritative-probe-docx"
    probe_bytes = _canonical(
        {
            "dpi": 144,
            "page_count": 1,
            "pages": [
                {
                    "height_px": 2200,
                    "page_number": 1,
                    "pixel_sha256": _sha256(b"canonical-rgba-pixels"),
                    "width_px": 1700,
                }
            ],
            "schema_version": 1,
            "source_sha256": _sha256(probe_source),
        }
    )
    content = {
        **executable_content,
        "dependency-manifest.json": dependency_bytes,
        "font-manifest.json": b'{"fonts":["CJK.ttf"]}\n',
        "fonts/CJK.ttf": b"release-reviewed-cjk-font",
        "license-manifest.json": b'{"licenses":["OFL-1.1"]}\n',
        "probe/authoritative-renderer-probe.docx": probe_source,
        "probe/authoritative-renderer-probe.json": probe_bytes,
        "sandbox-manifest.json": sandbox_bytes,
    }
    deployment_files = [
        {
            "mode": 0o755 if path.startswith("bin/") else 0o644,
            "path": path,
            "sha256": _sha256(value),
            "size": len(value),
        }
        for path, value in sorted(content.items())
    ]
    components = {
        "bundle-tree": _tree_digest(deployment_files),
        "dependency-manifest": _sha256(dependency_bytes),
        "font-manifest": _sha256(content["font-manifest.json"]),
        "license-manifest": _sha256(content["license-manifest.json"]),
        "pdftoppm": _sha256(content["bin/pdftoppm"]),
        "sandbox-manifest": _sha256(sandbox_bytes),
        "soffice": _sha256(content["bin/soffice"]),
    }
    private_key = Ed25519PrivateKey.generate()
    attestation_payload = {
        "schema_version": 2,
        "app_version": "1.1.0",
        "release_commit": attestation_release_commit,
        "platform_target": TARGET,
        "base_renderer_id": "libreoffice-pdf-png",
        "base_renderer_version": "fixture-v1",
        "font_digest": _sha256(b"font-environment"),
        "components": components,
    }
    signed_payload = {
        **attestation_payload,
        "release_commit": signed_release_commit or attestation_release_commit,
    }
    signature = private_key.sign(
        canonical_office_renderer_attestation_payload(signed_payload)
    )
    content["office-renderer-attestation.json"] = json.dumps(
        {
            **attestation_payload,
            "signature": base64.b64encode(signature).decode("ascii"),
        },
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("ascii")

    for relative, value in content.items():
        destination = payload.joinpath(*relative.split("/"))
        destination.write_bytes(value)
        os.chmod(destination, 0o755 if relative.startswith("bin/") else 0o644)

    directories = [
        {"mode": 0o755, "path": relative}
        for relative in ("bin", "fonts", "probe")
    ]
    files = [
        {
            "mode": 0o755 if path.startswith("bin/") else 0o644,
            "path": path,
            "sha256": _sha256(value),
            "size": len(value),
        }
        for path, value in sorted(content.items())
    ]
    lock = {
        "schema_version": 1,
        "platform_target": TARGET,
        "payload_contract": PAYLOAD_CONTRACT,
        "payload_root_mode": 0o755,
        "directories": directories,
        "files": files,
        "payload_tree_sha256": _tree_digest(files),
    }
    lock_bytes = json.dumps(
        lock,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("ascii") + b"\n"
    (source / LOCK_FILENAME).write_bytes(lock_bytes)
    os.chmod(source / LOCK_FILENAME, 0o644)

    stage_parent = tmp_path / "stage-parent"
    stage_parent.mkdir(mode=0o700)
    os.chmod(stage_parent, 0o700)
    stage = stage_parent / "staged"
    script = Path(__file__).parents[3] / "scripts" / "stage-office-renderer.mjs"
    subprocess.run(
        (
            "node",
            str(script),
            "stage",
            str(source),
            TARGET,
            _sha256(lock_bytes),
            str(stage),
        ),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return stage, _sha256(lock_bytes), payload, public_key


def _configure(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stage: Path,
    lock_sha256: str,
    public_key: bytes,
) -> None:
    monkeypatch.setenv("SUXIAOYOU_V11_OFFICE_RENDERER_REQUIRED", "1")
    monkeypatch.setenv("SUXIAOYOU_OFFICE_RENDERER_STAGE", str(stage))
    monkeypatch.setenv("SUXIAOYOU_OFFICE_RENDERER_TARGET", TARGET)
    monkeypatch.setenv("SUXIAOYOU_OFFICE_RENDERER_LOCK_SHA256", lock_sha256)
    monkeypatch.setattr(packaging, "_native_target", lambda: TARGET)
    monkeypatch.setattr(trust, "AUTHORITATIVE_RENDERER_PUBLIC_KEY", public_key)


def _repository(tmp_path: Path) -> tuple[Path, Path]:
    repository = tmp_path / "repository"
    app_dir = repository / "backend" / "app"
    app_dir.mkdir(parents=True)
    (repository / "package.json").write_text(
        '{"name":"suxiaoyou","version":"1.1.0"}\n',
        encoding="utf-8",
    )
    return repository, app_dir


def _analysis_toc(assets: OfficeRendererAssets) -> list[tuple[str, str, str]]:
    return [
        (record.destination_path, record.source_path, "DATA")
        for record in assets.files
    ]


def test_pre_v11_allows_an_empty_workflow_profile_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, app_dir = _repository(tmp_path)
    (repository / "package.json").write_text(
        '{"name":"suxiaoyou","version":"1.0.0"}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("SUXIAOYOU_V11_OFFICE_RENDERER_REQUIRED", "0")
    monkeypatch.setenv("SUXIAOYOU_OFFICE_RENDERER_PROFILE", "")

    assets = prepare_office_renderer_assets(
        app_dir=str(app_dir),
        repo_root=str(repository),
    )

    assert assets.snapshot_root is None
    assert assets.datas == ()


def _retarget_assets(
    assets: OfficeRendererAssets,
    target: str,
) -> OfficeRendererAssets:
    destination_root = f"app/data/office-renderer/{target}"
    files = tuple(
        replace(
            record,
            destination_path=f"{destination_root}/{record.relative_path}",
        )
        for record in assets.files
    )
    return replace(
        assets,
        destination_root=destination_root,
        files=files,
        datas=tuple(
            (
                record.source_path,
                os.path.join(
                    *record.destination_path.rsplit("/", 1)[0].split("/")
                ),
            )
            for record in files
        ),
    )


def test_node_staged_renderer_is_admitted_by_pyinstaller_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage, lock_sha256, _source_payload, public_key = _build_staged_renderer(tmp_path)
    repository, app_dir = _repository(tmp_path)
    _configure(
        monkeypatch,
        stage=stage,
        lock_sha256=lock_sha256,
        public_key=public_key,
    )

    assets = prepare_office_renderer_assets(
        app_dir=str(app_dir),
        repo_root=str(repository),
        work_root=str(tmp_path / "pyinstaller-office-renderer"),
        release_identity=ReleaseIdentityValues("1.1.0", RELEASE_COMMIT),
    )

    snapshot = tmp_path / "pyinstaller-office-renderer" / TARGET
    assert assets.snapshot_root == str(snapshot.resolve())
    assert assets.destination_root == f"app/data/office-renderer/{TARGET}"
    assert len(assets.datas) == len(assets.files) == 12
    assert assets.datas == tuple(
        (
            record.source_path,
            os.path.join(
                *record.destination_path.rsplit("/", 1)[0].split("/")
            ),
        )
        for record in assets.files
    )
    verify_office_renderer_analysis_assets(assets, _analysis_toc(assets))
    assert snapshot.is_dir()
    assert snapshot != stage / "payload" / TARGET
    if os.name != "nt":
        assert snapshot.stat().st_mode & 0o777 == 0o555
        assert (tmp_path / "pyinstaller-office-renderer").stat().st_mode & 0o777 == 0o500
        assert all(
            (
                Path(record.source_path).stat().st_mode & 0o777
                == (0o555 if record.locked_mode & 0o111 else 0o444)
            )
            for record in assets.files
        )
        assert all(record.mode == 0o555 for record in assets.directories)
    assert (snapshot / "office-renderer-attestation.json").read_bytes() == (
        stage / "payload" / TARGET / "office-renderer-attestation.json"
    ).read_bytes()
    probe_path = (
        stage
        / "payload"
        / TARGET
        / "probe"
        / "authoritative-renderer-probe.json"
    )
    probe_bytes = probe_path.read_bytes()
    runtime_manifest = AuthoritativeRendererProbeManifest.from_dict(
        json.loads(probe_bytes.decode("utf-8"))
    )
    assert runtime_manifest.page_count == 1
    assert runtime_manifest.canonical_bytes() == probe_bytes


def test_unsigned_degraded_profile_admits_no_renderer_and_rejects_ambient_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, app_dir = _repository(tmp_path)
    monkeypatch.setenv("SUXIAOYOU_V11_OFFICE_RENDERER_REQUIRED", "1")
    monkeypatch.setenv(
        "SUXIAOYOU_OFFICE_RENDERER_PROFILE",
        "unsigned-degraded",
    )
    for name in (
        "SUXIAOYOU_OFFICE_RENDERER_STAGE",
        "SUXIAOYOU_OFFICE_RENDERER_TARGET",
        "SUXIAOYOU_OFFICE_RENDERER_LOCK_SHA256",
    ):
        monkeypatch.delenv(name, raising=False)

    assets = prepare_office_renderer_assets(
        app_dir=str(app_dir),
        repo_root=str(repository),
        work_root=str(tmp_path / "unused-renderer-work"),
        release_identity=ReleaseIdentityValues("1.1.0", RELEASE_COMMIT),
    )

    assert assets.snapshot_root is None
    assert assets.payload_tree_sha256 is None
    assert assets.directories == assets.files == assets.datas == ()
    bind_office_renderer_analysis_assets(assets, [], [])

    ambient = tmp_path / "ambient-renderer.bin"
    ambient.write_bytes(b"ambient")
    with pytest.raises(OfficeRendererPackagingError, match="ambient Office renderer"):
        bind_office_renderer_analysis_assets(
            assets,
            [
                (
                    "app/data/office-renderer/bin/ambient-renderer",
                    str(ambient),
                    "DATA",
                )
            ],
            [],
        )


def test_unsigned_degraded_profile_rejects_stage_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, app_dir = _repository(tmp_path)
    monkeypatch.setenv("SUXIAOYOU_V11_OFFICE_RENDERER_REQUIRED", "1")
    monkeypatch.setenv(
        "SUXIAOYOU_OFFICE_RENDERER_PROFILE",
        "unsigned-degraded",
    )
    monkeypatch.setenv("SUXIAOYOU_OFFICE_RENDERER_STAGE", str(tmp_path / "stage"))

    with pytest.raises(
        OfficeRendererPackagingError,
        match="unsigned-degraded.*must not receive renderer stage",
    ):
        prepare_office_renderer_assets(
            app_dir=str(app_dir),
            repo_root=str(repository),
            work_root=str(tmp_path / "unused-renderer-work"),
            release_identity=ReleaseIdentityValues("1.1.0", RELEASE_COMMIT),
        )


def test_unsigned_degraded_profile_requires_frozen_release_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, app_dir = _repository(tmp_path)
    monkeypatch.setenv("SUXIAOYOU_V11_OFFICE_RENDERER_REQUIRED", "1")
    monkeypatch.setenv(
        "SUXIAOYOU_OFFICE_RENDERER_PROFILE",
        "unsigned-degraded",
    )

    with pytest.raises(
        OfficeRendererPackagingError,
        match="requires the frozen checkout release identity",
    ):
        prepare_office_renderer_assets(
            app_dir=str(app_dir),
            repo_root=str(repository),
            work_root=str(tmp_path / "unused-renderer-work"),
        )


def test_unknown_renderer_profile_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, app_dir = _repository(tmp_path)
    monkeypatch.setenv("SUXIAOYOU_V11_OFFICE_RENDERER_REQUIRED", "1")
    monkeypatch.setenv("SUXIAOYOU_OFFICE_RENDERER_PROFILE", "ambient")

    with pytest.raises(
        OfficeRendererPackagingError,
        match="must be signed-authoritative or unsigned-degraded",
    ):
        prepare_office_renderer_assets(
            app_dir=str(app_dir),
            repo_root=str(repository),
            work_root=str(tmp_path / "unused-renderer-work"),
            release_identity=ReleaseIdentityValues("1.1.0", RELEASE_COMMIT),
        )


@pytest.mark.parametrize("drift", ("extra", "missing", "substituted"))
def test_analysis_renderer_inventory_rejects_every_mapping_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    stage, lock_sha256, source_payload, public_key = _build_staged_renderer(tmp_path)
    repository, app_dir = _repository(tmp_path)
    _configure(
        monkeypatch,
        stage=stage,
        lock_sha256=lock_sha256,
        public_key=public_key,
    )
    assets = prepare_office_renderer_assets(
        app_dir=str(app_dir),
        repo_root=str(repository),
        work_root=str(tmp_path / "pyinstaller-office-renderer"),
        release_identity=ReleaseIdentityValues("1.1.0", RELEASE_COMMIT),
    )
    toc = _analysis_toc(assets)
    expected_error = drift
    if drift == "extra":
        toc.append(
            (
                f"{assets.destination_root}/undeclared.bin",
                str(source_payload / "bin" / "soffice"),
                "DATA",
            )
        )
    elif drift == "missing":
        toc.pop()
        expected_error = "omitted"
    else:
        destination, _source, typecode = toc[0]
        toc[0] = (
            destination,
            str(source_payload / assets.files[0].relative_path),
            typecode,
        )

    with pytest.raises(OfficeRendererPackagingError, match=expected_error):
        verify_office_renderer_analysis_assets(assets, toc)


def test_real_native_file_is_injected_only_after_binary_classification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bindepend = pytest.importorskip("PyInstaller.depend.bindepend")
    native_executable = Path(sys.executable).resolve()
    if bindepend.classify_binary_vs_data(str(native_executable)) != "BINARY":
        pytest.skip("host Python executable is not classifiable as a native binary")
    stage, lock_sha256, _source_payload, public_key = _build_staged_renderer(
        tmp_path,
        native_soffice=native_executable.read_bytes(),
    )
    repository, app_dir = _repository(tmp_path)
    _configure(
        monkeypatch,
        stage=stage,
        lock_sha256=lock_sha256,
        public_key=public_key,
    )
    assets = prepare_office_renderer_assets(
        app_dir=str(app_dir),
        repo_root=str(repository),
        work_root=str(tmp_path / "pyinstaller-office-renderer"),
        release_identity=ReleaseIdentityValues("1.1.0", RELEASE_COMMIT),
    )
    native_record = next(
        record for record in assets.files if record.relative_path == "bin/soffice"
    )
    assert bindepend.classify_binary_vs_data(native_record.source_path) == "BINARY"

    with pytest.raises(OfficeRendererPackagingError, match="subject to rewriting"):
        verify_office_renderer_analysis_assets(
            assets,
            [],
            [
                (
                    native_record.destination_path,
                    native_record.source_path,
                    "BINARY",
                )
            ],
        )

    analysis_datas: list[tuple[str, str, str]] = []
    analysis_binaries: list[tuple[str, str, str]] = []
    bind_office_renderer_analysis_assets(
        assets,
        analysis_datas,
        analysis_binaries,
    )
    assert analysis_datas == _analysis_toc(assets)
    assert all(typecode == "DATA" for _destination, _source, typecode in analysis_datas)
    verify_office_renderer_analysis_assets(
        assets,
        analysis_datas,
        analysis_binaries,
    )


def test_windows_casefolded_ambient_binary_destination_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage, lock_sha256, source_payload, public_key = _build_staged_renderer(tmp_path)
    repository, app_dir = _repository(tmp_path)
    _configure(
        monkeypatch,
        stage=stage,
        lock_sha256=lock_sha256,
        public_key=public_key,
    )
    linux_assets = prepare_office_renderer_assets(
        app_dir=str(app_dir),
        repo_root=str(repository),
        work_root=str(tmp_path / "pyinstaller-office-renderer"),
        release_identity=ReleaseIdentityValues("1.1.0", RELEASE_COMMIT),
    )
    windows_assets = _retarget_assets(linux_assets, "windows-x64")
    ambient_source = source_payload / windows_assets.files[0].relative_path
    case_colliding_destination = windows_assets.files[0].destination_path.upper()

    with pytest.raises(OfficeRendererPackagingError, match="ambient.*binary"):
        bind_office_renderer_analysis_assets(
            windows_assets,
            [],
            [
                (
                    case_colliding_destination,
                    str(ambient_source),
                    "BINARY",
                )
            ],
        )


@pytest.mark.parametrize(
    "destination",
    (
        "app./data/office-renderer/windows-x64/bin/soffice.exe",
        "app /data/office-renderer/windows-x64/bin/soffice.exe",
        "app/data/office-renderer/windows-x64/NUL.txt",
        "app/data/office-renderer/windows-x64/CON .txt",
        "app/data/office-renderer/windows-x64/bin/soffice.exe:payload",
        "app/data/office~1/windows-x64/bin/soffice.exe",
    ),
)
def test_windows_ambiguous_or_device_destinations_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    destination: str,
) -> None:
    stage, lock_sha256, source_payload, public_key = _build_staged_renderer(tmp_path)
    repository, app_dir = _repository(tmp_path)
    _configure(
        monkeypatch,
        stage=stage,
        lock_sha256=lock_sha256,
        public_key=public_key,
    )
    assets = _retarget_assets(
        prepare_office_renderer_assets(
            app_dir=str(app_dir),
            repo_root=str(repository),
            work_root=str(tmp_path / "pyinstaller-office-renderer"),
            release_identity=ReleaseIdentityValues("1.1.0", RELEASE_COMMIT),
        ),
        "windows-x64",
    )

    with pytest.raises(
        OfficeRendererPackagingError,
        match="invalid Windows data destination",
    ):
        bind_office_renderer_analysis_assets(
            assets,
            [],
            [(destination, str(source_payload / "bin" / "soffice"), "BINARY")],
        )


def test_windows_source_containment_uses_nt_path_identity() -> None:
    target = "windows-x64"
    root = packaging._source_path_key(target, r"C:\Build\Renderer")
    aliased_child = packaging._source_path_key(
        target,
        r"c:\build\renderer.\BIN\soffice.exe",
    )
    sibling = packaging._source_path_key(
        target,
        r"c:\build\renderer-other\soffice.exe",
    )

    assert packaging._path_is_within(aliased_child, root, target=target)
    assert not packaging._path_is_within(sibling, root, target=target)


def test_macos_casefolded_destination_alias_is_renderer_related(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage, lock_sha256, source_payload, public_key = _build_staged_renderer(tmp_path)
    repository, app_dir = _repository(tmp_path)
    _configure(
        monkeypatch,
        stage=stage,
        lock_sha256=lock_sha256,
        public_key=public_key,
    )
    assets = _retarget_assets(
        prepare_office_renderer_assets(
            app_dir=str(app_dir),
            repo_root=str(repository),
            work_root=str(tmp_path / "pyinstaller-office-renderer"),
            release_identity=ReleaseIdentityValues("1.1.0", RELEASE_COMMIT),
        ),
        "darwin-arm64",
    )
    record = assets.files[0]
    alias = record.destination_path.replace("app/", "APP/", 1)

    with pytest.raises(OfficeRendererPackagingError, match="ambient.*binary"):
        bind_office_renderer_analysis_assets(
            assets,
            [],
            [
                (
                    alias,
                    str(source_payload / record.relative_path),
                    "BINARY",
                )
            ],
        )


def test_macos_destination_key_collapses_apfs_unicode_normalization() -> None:
    assets = OfficeRendererAssets(
        snapshot_root=None,
        destination_root="app/data/office-renderer/darwin-arm64",
        payload_tree_sha256=None,
        directories=(),
        files=(),
        datas=(),
    )
    nfc = "app/data/office-renderer/darwin-arm64/fonts/Caf\u00e9.ttf"
    nfd = "app/data/office-renderer/darwin-arm64/fonts/Cafe\u0301.ttf"

    assert packaging._destination_key(assets, nfc) == packaging._destination_key(
        assets,
        nfd,
    )


def test_pre_v11_renderer_root_uses_native_alias_semantics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(packaging, "_native_target", lambda: "darwin-arm64")
    assets = OfficeRendererAssets(
        snapshot_root=None,
        destination_root="app/data/office-renderer",
        payload_tree_sha256=None,
        directories=(),
        files=(),
        datas=(),
    )

    with pytest.raises(OfficeRendererPackagingError, match="ambient.*binary"):
        bind_office_renderer_analysis_assets(
            assets,
            [],
            [
                (
                    "APP/DATA/OFFICE-RENDERER/ambient.bin",
                    str(tmp_path / "ambient.bin"),
                    "BINARY",
                )
            ],
        )


@pytest.mark.parametrize("drift", ("root", "directory", "executable", "data"))
def test_noncanonical_posix_lock_modes_are_rejected_before_snapshot(
    tmp_path: Path,
    drift: str,
) -> None:
    stage, _lock_sha256, _source_payload, _public_key = _build_staged_renderer(
        tmp_path
    )
    lock = json.loads((stage / LOCK_FILENAME).read_text(encoding="utf-8"))
    if drift == "root":
        lock["payload_root_mode"] = 0o700
    elif drift == "directory":
        lock["directories"][0]["mode"] = 0o700
    else:
        selected = next(
            entry
            for entry in lock["files"]
            if (
                entry["path"] == "bin/soffice"
                if drift == "executable"
                else entry["path"] == "font-manifest.json"
            )
        )
        selected["mode"] = 0o555 if drift == "executable" else 0o600
        lock["payload_tree_sha256"] = _tree_digest(lock["files"])

    with pytest.raises(OfficeRendererPackagingError, match="POSIX modes must be canonical"):
        packaging._validate_lock(lock, TARGET)


def test_post_analysis_snapshot_inode_and_inventory_drift_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage, lock_sha256, _source_payload, public_key = _build_staged_renderer(tmp_path)
    repository, app_dir = _repository(tmp_path)
    _configure(
        monkeypatch,
        stage=stage,
        lock_sha256=lock_sha256,
        public_key=public_key,
    )
    assets = prepare_office_renderer_assets(
        app_dir=str(app_dir),
        repo_root=str(repository),
        work_root=str(tmp_path / "pyinstaller-office-renderer"),
        release_identity=ReleaseIdentityValues("1.1.0", RELEASE_COMMIT),
    )
    victim = Path(assets.files[0].source_path)
    original = victim.read_bytes()
    if os.name != "nt":
        os.chmod(victim.parent, 0o755)
        os.chmod(victim, 0o755)
    replacement = victim.with_suffix(".replacement")
    replacement.write_bytes(original)
    if os.name != "nt":
        os.chmod(replacement, 0o555)
    victim.unlink()
    replacement.rename(victim)

    with pytest.raises(
        OfficeRendererPackagingError,
        match="identity changed",
    ):
        verify_office_renderer_analysis_assets(assets, _analysis_toc(assets))


def test_valid_renderer_refuses_missing_or_reused_private_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage, lock_sha256, _source_payload, public_key = _build_staged_renderer(tmp_path)
    repository, app_dir = _repository(tmp_path)
    _configure(
        monkeypatch,
        stage=stage,
        lock_sha256=lock_sha256,
        public_key=public_key,
    )
    identity = ReleaseIdentityValues("1.1.0", RELEASE_COMMIT)

    with pytest.raises(OfficeRendererPackagingError, match="private work snapshot"):
        office_renderer_datas(
            app_dir=str(app_dir),
            repo_root=str(repository),
            release_identity=identity,
        )

    reused = tmp_path / "reused"
    reused.mkdir()
    with pytest.raises(OfficeRendererPackagingError, match="must not already exist"):
        office_renderer_datas(
            app_dir=str(app_dir),
            repo_root=str(repository),
            work_root=str(reused),
            release_identity=identity,
        )


def test_staged_helper_drift_and_release_identity_mismatch_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage, lock_sha256, _source_payload, public_key = _build_staged_renderer(tmp_path)
    repository, app_dir = _repository(tmp_path)
    _configure(
        monkeypatch,
        stage=stage,
        lock_sha256=lock_sha256,
        public_key=public_key,
    )

    with pytest.raises(OfficeRendererPackagingError, match="release identity"):
        office_renderer_datas(
            app_dir=str(app_dir),
            repo_root=str(repository),
            release_identity=ReleaseIdentityValues("1.1.0", "b" * 40),
        )

    helper = stage / "payload" / TARGET / "bin" / "suxiaoyou-office-sandbox-probe"
    original = helper.read_bytes()
    helper.write_bytes(bytes([original[0] ^ 0xFF]) + original[1:])
    with pytest.raises(OfficeRendererPackagingError, match="bytes or modes"):
        office_renderer_datas(
            app_dir=str(app_dir),
            repo_root=str(repository),
            release_identity=ReleaseIdentityValues("1.1.0", RELEASE_COMMIT),
        )


def test_replayed_attestation_signature_is_rejected_before_pyinstaller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replayed_commit = "b" * 40
    stage, lock_sha256, _source_payload, public_key = _build_staged_renderer(
        tmp_path,
        attestation_release_commit=replayed_commit,
        signed_release_commit=RELEASE_COMMIT,
    )
    repository, app_dir = _repository(tmp_path)
    _configure(
        monkeypatch,
        stage=stage,
        lock_sha256=lock_sha256,
        public_key=public_key,
    )

    with pytest.raises(OfficeRendererPackagingError, match="signature is not trusted"):
        office_renderer_datas(
            app_dir=str(app_dir),
            repo_root=str(repository),
            release_identity=ReleaseIdentityValues("1.1.0", replayed_commit),
        )
