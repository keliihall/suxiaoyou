"""Release packaging contracts for the PyInstaller backend."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

from app.channels.registry import CHINA_READY_CHANNELS


SPEC_PATH = Path(__file__).parents[1] / "suxiaoyou.spec"


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
