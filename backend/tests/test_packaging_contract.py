"""Release packaging contracts for the PyInstaller backend."""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import tomllib
from pathlib import Path

import pytest

from app.channels.registry import CHINA_READY_CHANNELS
from app.main import APP_VERSION, create_app
from release_packaging.office_renderer_stage import (
    OfficeRendererPackagingError,
    office_renderer_datas,
)
from release_packaging.release_identity import ReleaseIdentityValues


SPEC_PATH = Path(__file__).parents[1] / "suxiaoyou.spec"
PYPROJECT_PATH = Path(__file__).parents[1] / "pyproject.toml"
REQUIREMENTS_PATH = Path(__file__).parents[1] / "requirements.txt"
BACKEND_LICENSE_PATH = Path(__file__).parents[1] / "LICENSE"
PROJECT_LICENSE_PATH = Path(__file__).parents[2] / "LICENSE"


def test_backend_api_reports_the_release_metadata_version() -> None:
    project = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))

    assert APP_VERSION == project["project"]["version"]
    assert create_app().version == APP_VERSION


def test_acp_release_dependency_is_hash_locked() -> None:
    requirements = REQUIREMENTS_PATH.read_text(encoding="utf-8")
    match = re.search(
        r"^agent-client-protocol==0\.10\.1 \\\n"
        r"    --hash=sha256:355c65ca19f0568344aafc2c1552b7066a8fc491df23ab28e7e253c6c9a85a25 \\\n"
        r"    --hash=sha256:a03d3198f4d772f2e0ec012c00ac1cce131b4710220a3dc9fae3c991d047c750$",
        requirements,
        flags=re.MULTILINE,
    )
    assert match is not None


def test_acp_frozen_entry_and_official_sdk_are_explicitly_packaged() -> None:
    spec_source = SPEC_PATH.read_text(encoding="utf-8")
    hidden_imports = set(_static_hidden_imports())

    assert "collect_all('acp')" in spec_source
    assert {
        "acp",
        "acp.meta",
        "acp.schema",
        "acp.stdio",
        "app.acp",
        "app.acp.bridge",
        "app.acp.cli",
        "app.acp.self_test",
        "app.acp.server",
        "app.acp.session_bridge",
        "app.acp.stdio",
    } <= hidden_imports


def test_v11_runtime_and_office_beta_modules_are_explicitly_packaged() -> None:
    """Frozen builds must not rely on import-graph discovery for gated code."""

    assert {
        "app.api.runtime_control",
        "app.api.office_user_templates",
        "app.api.office_v2",
        "app.hooks",
        "app.hooks.config",
        "app.hooks.dispatcher",
        "app.hooks.registry",
        "app.hooks.runner",
        "app.hooks.runtime",
        "app.hooks.trust",
        "app.models.checkpoint_change",
        "app.models.office_user_template",
        "app.models.session_checkpoint",
        "app.models.turn_run",
        "app.models.workspace_instance",
        "app.office_rendering",
        "app.office_rendering.attested",
        "app.office_rendering.deployment",
        "app.office_rendering.libreoffice",
        "app.office_rendering.native_bundle",
        "app.office_rendering.native_sandbox",
        "app.office_rendering.native_sandbox_behavior",
        "app.office_rendering.probe",
        "app.office_rendering.release_identity",
        "app.office_rendering.runtime",
        "app.office_rendering.sandbox",
        "app.office_rendering.service",
        "release_packaging.office_renderer_trust",
        "app.office_templates.bundled",
        "app.office_templates.instantiation",
        "app.office_templates.policies",
        "app.office_templates.registry",
        "app.office_templates.substitution",
        "app.office_templates.user",
        "app.office_templates.validation",
        "app.office_validation",
        "app.office_validation.draft",
        "app.office_validation.orchestrator",
        "app.office_validation.precommit",
        "app.office_validation.precommit_repair",
        "app.office_validation.repair_agent",
        "app.office_validation.runtime",
        "app.office_validation.startup",
        "app.office_validation.structure",
        "app.office_validation.visual",
        "app.release_readiness",
        "app.runtime.checkpoint_runtime",
        "app.runtime.events",
        "app.runtime.frozen_self_test",
        "app.runtime.rewind",
        "app.runtime.v11_readiness",
        "app.storage.checkpoints",
        "app.validation_agent",
        "app.validation_agent.contracts",
        "app.validation_agent.persistence",
        "app.validation_agent.scheduler",
        "app.validation_agent.service",
        "app.worktree",
        "app.worktree.runtime",
        "app.worktree.service",
    } <= set(_static_hidden_imports())


def test_python_artifacts_ship_the_canonical_project_license() -> None:
    project = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))

    assert project["project"]["license"] == "Apache-2.0"
    assert project["project"]["license-files"] == ["LICENSE"]
    assert BACKEND_LICENSE_PATH.read_bytes() == PROJECT_LICENSE_PATH.read_bytes()


def _spec_tree() -> ast.Module:
    return ast.parse(SPEC_PATH.read_text(encoding="utf-8"), filename=str(SPEC_PATH))


def _assignment(name: str) -> ast.expr:
    for node in _spec_tree().body:
        if not isinstance(node, ast.Assign):
            continue
        defines_name = any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        )
        if defines_name:
            return node.value

    raise AssertionError(f"suxiaoyou.spec does not define {name}")


def _static_literal(name: str):
    return ast.literal_eval(_assignment(name))


def _static_hidden_imports() -> list[str]:
    hidden_imports = _static_literal("hiddenimports")
    assert isinstance(hidden_imports, list)
    return hidden_imports


def test_every_static_hidden_import_is_resolvable() -> None:
    missing: list[str] = []
    for module_name in _static_hidden_imports():
        try:
            spec = importlib.util.find_spec(module_name)
        except (ImportError, ModuleNotFoundError, AttributeError, ValueError):
            spec = None
        if spec is None:
            missing.append(module_name)

    assert missing == []


def test_web_extraction_lazy_imports_are_explicitly_packaged() -> None:
    hidden_imports = _static_hidden_imports()
    assert "readabilipy" in hidden_imports
    assert "markdownify" in hidden_imports


def test_every_released_channel_is_explicitly_packaged() -> None:
    hidden_imports = set(_static_hidden_imports())
    released_modules = {f"app.channels.{name}" for name in CHINA_READY_CHANNELS}

    assert released_modules <= hidden_imports
    assert "app.channels.whatsapp" not in hidden_imports


def test_unfinished_whatsapp_bridge_is_not_packaged() -> None:
    required_datas = _assignment("_required_datas")
    assert isinstance(required_datas, ast.List)
    assert not any(
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "bridge" in node.value.lower()
        for item in required_datas.elts
        for node in ast.walk(item)
    )

    assigned_names = {
        target.id
        for node in _spec_tree().body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert not any(name.startswith("_bridge_") for name in assigned_names)


def test_pdf_cjk_font_and_license_are_mandatory_app_data() -> None:
    spec_source = SPEC_PATH.read_text(encoding="utf-8")
    font_dir = Path(__file__).parents[1] / "app" / "data" / "fonts"

    assert "_required_pdf_font_files" in spec_source
    assert "SuxiaoyouCJK-Regular.ttf" in spec_source
    assert "OFL-1.1.txt" in spec_source
    assert "PROVENANCE.md" in spec_source
    assert (font_dir / "SuxiaoyouCJK-Regular.ttf").is_file()
    assert (font_dir / "OFL-1.1.txt").is_file()
    assert (font_dir / "PROVENANCE.md").is_file()


def test_validator_and_office_repair_prompts_are_mandatory_build_inputs() -> None:
    required_prompts = _assignment("_required_agent_prompt_files")
    assert isinstance(required_prompts, ast.List)
    assert {
        node.value
        for item in required_prompts.elts
        for node in ast.walk(item)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.endswith(".txt")
    } == {"validator.txt", "office_repair.txt"}

    spec_source = SPEC_PATH.read_text(encoding="utf-8")
    assert "_missing.extend(path for path in _required_agent_prompt_files" in (
        spec_source
    )


def test_signed_office_templates_are_mandatory_app_data() -> None:
    """The desktop bundle must keep the signed first-party catalog usable."""

    required_assets = _assignment("_required_office_template_assets")
    assert isinstance(required_assets, ast.List)
    office_data_entries = [
        item
        for item in required_assets.elts
        if {
            node.value
            for node in ast.walk(item)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        >= {"office_templates", "assets"}
    ]
    assert len(office_data_entries) == 5
    packaged_filenames = {
        node.value
        for item in office_data_entries
        for node in ast.walk(item)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and Path(node.value).suffix in {".json", ".docx", ".xlsx", ".pptx"}
    }
    assert packaged_filenames == {
        "catalog.json",
        "catalog.sig.json",
        "business-brief.docx",
        "project-tracker.xlsx",
        "status-update.pptx",
    }

    required_datas = _assignment("_required_datas")
    assert isinstance(required_datas, ast.List)
    assert any(
        isinstance(item, ast.Starred)
        and isinstance(item.value, ast.Name)
        and item.value.id == "_required_office_template_assets"
        for item in required_datas.elts
    )
    assert "app.office_templates" in _static_hidden_imports()

    asset_dir = Path(__file__).parents[1] / "app" / "office_templates" / "assets"
    expected_assets = {
        "catalog.json",
        "catalog.sig.json",
        "templates/business-brief.docx",
        "templates/project-tracker.xlsx",
        "templates/status-update.pptx",
    }
    assert {
        path.relative_to(asset_dir).as_posix()
        for path in asset_dir.rglob("*")
        if path.is_file()
    } == expected_assets


def test_v11_renderer_uses_only_the_external_lock_bound_native_stage() -> None:
    """Private renderer bytes must never enter through the broad app/data root."""

    spec_source = SPEC_PATH.read_text(encoding="utf-8")
    helper_path = Path(__file__).parents[1] / "release_packaging" / (
        "office_renderer_stage.py"
    )
    helper_source = helper_path.read_text(encoding="utf-8")

    assert "prepare_office_renderer_assets" in spec_source
    assert "_required_office_renderer_assets = list(_office_renderer_build.datas)" in (
        spec_source
    )
    assert spec_source.count("_verify_office_renderer_analysis(") == 3
    assert (
        "_verify_office_renderer_analysis('post-Analysis attach', attach=True)"
        in spec_source
    )
    assert "_verify_office_renderer_analysis('pre-COLLECT')" in spec_source
    assert "work_root=os.path.join(workpath, 'office-renderer')" in spec_source
    assert "*_required_office_renderer_assets" in spec_source
    required_datas = _assignment("_required_datas")
    assert isinstance(required_datas, ast.List)
    assert not any(
        isinstance(item, ast.Starred)
        and isinstance(item.value, ast.Name)
        and item.value.id == "_required_office_renderer_assets"
        for item in required_datas.elts
    )
    assert "bind_office_renderer_analysis_assets" in spec_source
    assert "backend/app/data/office-renderer is forbidden" in helper_source
    assert "target != _native_target()" in helper_source
    assert "staging must contain exactly one native target" in helper_source
    assert "final-native-bytes-attested-after-signing-v1" in helper_source
    assert 'os.path.join("app", "data", "office-renderer", target)' in helper_source
    assert "_lock_snapshot_read_only(snapshot, lock)" in helper_source
    assert "verify_office_renderer_analysis_assets" in helper_source


def test_v11_renderer_and_frozen_app_share_one_checkout_release_identity() -> None:
    spec_source = SPEC_PATH.read_text(encoding="utf-8")
    helper_source = (
        Path(__file__).parents[1]
        / "release_packaging"
        / "office_renderer_stage.py"
    ).read_text(encoding="utf-8")

    assert "prepare_frozen_release_identity" in spec_source
    assert "*_release_identity_build.datas" in spec_source
    assert "_release_identity_build.binding_module_root" in spec_source
    assert "list(_release_identity_build.hiddenimports)" in spec_source
    assert "for entry in a.pure" in spec_source
    assert "module origin was shadowed or omitted" in spec_source
    assert "release_identity=_release_identity_build.identity" in spec_source
    assert "attestation.get(\"app_version\") != release_identity.app_version" in (
        helper_source
    )
    assert "attestation.get(\"release_commit\") != release_identity.release_commit" in (
        helper_source
    )


def test_v11_authoritative_profile_cannot_disable_or_omit_real_renderer_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    app_dir = tmp_path / "app"
    repo_root.mkdir()
    (app_dir / "data").mkdir(parents=True)
    (repo_root / "package.json").write_text(
        json.dumps({"version": "1.1.0"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("SUXIAOYOU_V11_OFFICE_RENDERER_REQUIRED", "0")

    with pytest.raises(
        OfficeRendererPackagingError,
        match="SUXIAOYOU_V11_OFFICE_RENDERER_REQUIRED must be 1",
    ):
        office_renderer_datas(app_dir=str(app_dir), repo_root=str(repo_root))

    monkeypatch.setenv("SUXIAOYOU_V11_OFFICE_RENDERER_REQUIRED", "1")
    monkeypatch.setenv(
        "SUXIAOYOU_OFFICE_RENDERER_PROFILE",
        "signed-authoritative",
    )
    with pytest.raises(
        OfficeRendererPackagingError,
        match=r"v1\.1\+ requires SUXIAOYOU_OFFICE_RENDERER_STAGE",
    ):
        office_renderer_datas(
            app_dir=str(app_dir),
            repo_root=str(repo_root),
            release_identity=ReleaseIdentityValues("1.1.0", "a" * 40),
        )


def test_spec_emits_commit_bound_unsigned_degraded_profile_marker() -> None:
    spec_source = SPEC_PATH.read_text(encoding="utf-8")

    assert "office-renderer-profile.json" in spec_source
    assert "suxiaoyou-office-renderer-profile-v1" in spec_source
    assert "UNSIGNED_DEGRADED_PROFILE" in spec_source
    assert "'authoritative_authoring_available': False" in spec_source
    assert "'authoritative_renderer_bundled': _renderer_bundled" in spec_source
    assert "_release_identity_build.identity.release_commit" in spec_source
    assert "sort_keys=True" in spec_source
    assert "*_office_renderer_profile_datas" in spec_source


def test_ambient_source_renderer_is_rejected_even_before_v11(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    app_dir = tmp_path / "app"
    repo_root.mkdir()
    (app_dir / "data" / "office-renderer").mkdir(parents=True)
    (repo_root / "package.json").write_text(
        json.dumps({"version": "1.0.0"}),
        encoding="utf-8",
    )
    monkeypatch.delenv("SUXIAOYOU_V11_OFFICE_RENDERER_REQUIRED", raising=False)

    with pytest.raises(
        OfficeRendererPackagingError,
        match="backend/app/data/office-renderer is forbidden",
    ):
        office_renderer_datas(app_dir=str(app_dir), repo_root=str(repo_root))
