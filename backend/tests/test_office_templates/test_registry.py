from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from app.office_templates import (
    OFFICE_TEMPLATES_DEFAULT_ENABLED,
    TEMPLATE_MANIFEST_SCHEMA_VERSION,
    OfficeTemplateRegistry,
    TemplateConflictError,
    TemplateContractError,
    TemplateInUseError,
    TemplateIntegrityError,
    TemplateNotFoundError,
)
from tests.test_office_templates.helpers import manifest_for, write_source


DOCX_PLACEHOLDERS = ("body", "client", "footer", "header", "table")


def _import_docx(
    tmp_path: Path,
    content: bytes,
    *,
    version: str = "1.0.0",
) -> tuple[OfficeTemplateRegistry, object, Path]:
    registry = OfficeTemplateRegistry(tmp_path / "registry")
    manifest = manifest_for(
        content,
        "docx",
        DOCX_PLACEHOLDERS,
        version=version,
    )
    source = write_source(tmp_path / "source", "fixture.docx", content)
    return registry, manifest, source


def test_manifest_is_strict_versioned_and_gate_is_off(
    docx_template_bytes: bytes,
) -> None:
    manifest = manifest_for(
        docx_template_bytes,
        "docx",
        DOCX_PLACEHOLDERS,
    )

    assert OFFICE_TEMPLATES_DEFAULT_ENABLED is False
    assert manifest.schema_version == TEMPLATE_MANIFEST_SCHEMA_VERSION == 1
    assert manifest.to_dict() == {
        "schema_version": 1,
        "template_id": "quarterly-report",
        "template_version": "1.0.0",
        "format": "docx",
        "source_sha256": hashlib.sha256(docx_template_bytes).hexdigest(),
        "license": "CC0-1.0",
        "provenance": "unit-test fixture generated with the OOXML format library",
        "required_placeholders": [
            "body",
            "client",
            "footer",
            "header",
            "table",
        ],
        "allowed_output_rules": {
            "extensions": [".docx"],
            "max_output_bytes": 10 * 1024 * 1024,
            "allow_overwrite": False,
        },
    }
    assert len(manifest.template_sha256) == 64
    with pytest.raises(TemplateContractError, match="schema"):
        replace(manifest, schema_version=True)
    with pytest.raises(TemplateContractError, match="sorted"):
        replace(manifest, required_placeholders=("client", "body"))
    with pytest.raises(TemplateContractError, match="extension"):
        replace(
            manifest,
            allowed_output_rules=replace(
                manifest.allowed_output_rules,
                extensions=(".xlsx",),
            ),
        )


def test_content_addressed_import_list_read_and_idempotency(
    tmp_path: Path,
    docx_template_bytes: bytes,
) -> None:
    registry, manifest, source = _import_docx(tmp_path, docx_template_bytes)

    first = registry.import_template(manifest, source)  # type: ignore[arg-type]
    second = registry.import_template(manifest, source)  # type: ignore[arg-type]

    assert first == second == registry.read("quarterly-report", "1.0.0")
    assert registry.list_templates() == (first,)
    assert registry.list_templates("quarterly-report") == (first,)
    assert registry.list_templates("not-present") == ()
    assert first.reference_count == 0
    assert first.reference_ids == ()
    assert first.content_path == (
        registry.root
        / "objects"
        / manifest.source_sha256[:2]  # type: ignore[attr-defined]
        / f"{manifest.source_sha256}.docx"  # type: ignore[attr-defined]
    )
    assert first.content_path.read_bytes() == docx_template_bytes
    envelope = json.loads(
        (
            registry.root
            / "records"
            / "quarterly-report"
            / "1.0.0"
            / "record.json"
        ).read_text(encoding="utf-8")
    )
    assert envelope["record"]["template_sha256"] == manifest.template_sha256  # type: ignore[attr-defined]
    assert envelope["record"]["manifest"]["license"] == "CC0-1.0"
    if os.name != "nt":
        assert stat.S_IMODE(registry.root.stat().st_mode) == 0o700
        assert stat.S_IMODE(first.content_path.stat().st_mode) == 0o600


def test_immutable_version_conflict_and_shared_object_lifecycle(
    tmp_path: Path,
    docx_template_bytes: bytes,
) -> None:
    registry, manifest_v1, source = _import_docx(tmp_path, docx_template_bytes)
    first = registry.import_template(manifest_v1, source)  # type: ignore[arg-type]

    conflicting = replace(manifest_v1, provenance="a different provenance")
    with pytest.raises(TemplateConflictError, match="immutable"):
        registry.import_template(conflicting, source)

    manifest_v2 = replace(manifest_v1, template_version="2.0.0")
    second = registry.import_template(manifest_v2, source)
    assert first.content_path == second.content_path
    assert [item.manifest.template_version for item in registry.list_templates()] == [
        "1.0.0",
        "2.0.0",
    ]

    retained = registry.retain("quarterly-report", "1.0.0", "workspace:report-7")
    assert retained.reference_count == 1
    assert retained.reference_ids == ("workspace:report-7",)
    assert registry.retain(
        "quarterly-report", "1.0.0", "workspace:report-7"
    ) == retained
    reopened_registry = OfficeTemplateRegistry(registry.root)
    assert reopened_registry.read(
        "quarterly-report", "1.0.0"
    ).reference_ids == ("workspace:report-7",)
    with pytest.raises(TemplateInUseError, match="references"):
        registry.delete("quarterly-report", "1.0.0")

    reopened_registry.release(
        "quarterly-report", "1.0.0", "workspace:report-7"
    )
    registry.release("quarterly-report", "1.0.0", "workspace:report-7")
    registry.delete("quarterly-report", "1.0.0")
    assert first.content_path.exists()
    assert tuple(item.manifest.template_version for item in registry.list_templates()) == (
        "2.0.0",
    )
    registry.delete("quarterly-report", "2.0.0")
    assert not first.content_path.exists()
    with pytest.raises(TemplateNotFoundError):
        registry.read("quarterly-report", "2.0.0")


def test_registry_detects_object_and_record_tampering(
    tmp_path: Path,
    docx_template_bytes: bytes,
) -> None:
    registry, manifest, source = _import_docx(tmp_path, docx_template_bytes)
    record = registry.import_template(manifest, source)  # type: ignore[arg-type]
    record.content_path.write_bytes(b"PK\x03\x04tampered")
    with pytest.raises(TemplateIntegrityError, match="digest|validation"):
        registry.read("quarterly-report", "1.0.0")

    other_root = tmp_path / "other"
    other_registry, other_manifest, other_source = _import_docx(
        other_root,
        docx_template_bytes,
    )
    other_registry.import_template(other_manifest, other_source)  # type: ignore[arg-type]
    record_path = (
        other_registry.root
        / "records"
        / "quarterly-report"
        / "1.0.0"
        / "record.json"
    )
    record_path.write_bytes(record_path.read_bytes().replace(b"CC0-1.0", b"CCX-1.0"))
    with pytest.raises(TemplateIntegrityError, match="digest|canonical"):
        other_registry.read("quarterly-report", "1.0.0")


def test_source_and_registry_paths_fail_closed(
    tmp_path: Path,
    docx_template_bytes: bytes,
) -> None:
    registry, manifest, source = _import_docx(tmp_path, docx_template_bytes)
    wrong_extension = write_source(tmp_path / "source", "wrong.xlsx", docx_template_bytes)
    with pytest.raises(TemplateContractError, match="extension"):
        registry.import_template(manifest, wrong_extension)  # type: ignore[arg-type]

    symlink_source = tmp_path / "source" / "link.docx"
    try:
        symlink_source.symlink_to(source)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable")
    with pytest.raises(TemplateContractError, match="symbolic"):
        registry.import_template(manifest, symlink_source)  # type: ignore[arg-type]

    redirected_root = tmp_path / "redirected-registry"
    redirected_root.symlink_to(tmp_path / "actual-registry", target_is_directory=True)
    with pytest.raises(TemplateIntegrityError, match="symbolic"):
        OfficeTemplateRegistry(redirected_root)


def test_invalid_reference_and_missing_template_are_explicit(tmp_path: Path) -> None:
    registry = OfficeTemplateRegistry(tmp_path / "registry")
    with pytest.raises(TemplateContractError, match="reference"):
        registry.retain("quarterly-report", "1.0.0", "../escape")
    with pytest.raises(TemplateNotFoundError, match="not found"):
        registry.read("quarterly-report", "1.0.0")
