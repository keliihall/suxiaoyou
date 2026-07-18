from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.office_templates import (
    OfficeTemplateRegistry,
    TemplateContractError,
    TemplateSecurityError,
    TemplateSafetyLimits,
    inspect_ooxml_package,
)
from tests.test_office_templates.helpers import (
    manifest_for,
    rewrite_zip,
    write_source,
    zip_entries,
)


DOCX_PLACEHOLDERS = ("body", "client", "footer", "header", "table")


def _replace_relationship(
    content: bytes,
    part_name: str,
    old: bytes,
    new: bytes,
) -> bytes:
    entries = zip_entries(content)
    assert old in entries[part_name]
    return rewrite_zip(
        content,
        replacements={part_name: entries[part_name].replace(old, new)},
    )


def _external_relationship(content: bytes) -> bytes:
    entries = zip_entries(content)
    relationships = entries["_rels/.rels"].replace(
        b"</Relationships>",
        (
            b'<Relationship Id="rExternal" '
            b'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            b'relationships/image" Target="https://example.invalid/tracker.png" '
            b'TargetMode="External"/></Relationships>'
        ),
    )
    return rewrite_zip(
        content,
        replacements={"_rels/.rels": relationships},
    )


@pytest.mark.parametrize(
    ("builder", "message"),
    [
        (
            lambda content: rewrite_zip(
                content,
                additions={"../outside.xml": b"<outside/>",},
            ),
            "escapes|path",
        ),
        (
            lambda content: rewrite_zip(
                content,
                additions={"%2e%2e/escaped.xml": b"<outside/>",},
            ),
            "escapes|path",
        ),
        (
            lambda content: rewrite_zip(
                content,
                additions={"word/vbaProject.bin": b"macro"},
            ),
            "Unsafe OOXML part",
        ),
        (
            lambda content: rewrite_zip(
                content,
                additions={"word/embeddings/object1.xml": b"<object/>",},
            ),
            "Unsafe OOXML part",
        ),
        (_external_relationship, "External"),
        (
            lambda content: _replace_relationship(
                content,
                "_rels/.rels",
                b"word/document.xml",
                b"word/missing.xml",
            ),
            "target is missing",
        ),
        (
            lambda content: rewrite_zip(
                content,
                replacements={"word/document.xml": b"<broken"},
            ),
            "Invalid OOXML XML",
        ),
        (
            lambda content: rewrite_zip(
                content,
                replacements={
                    "word/document.xml": (
                        b" " * 9000
                        + b'<!DOCTYPE x [<!ENTITY e "boom">]><x>&e;</x>'
                    )
                },
            ),
            "DTD|entity",
        ),
        (
            lambda content: rewrite_zip(
                content,
                additions={"word/media/bomb.dat": b"0" * (1024 * 1024)},
            ),
            "compression ratio",
        ),
    ],
    ids=[
        "path-traversal",
        "encoded-path-traversal",
        "macro",
        "embedded-object",
        "external-relationship",
        "missing-relationship-target",
        "malformed-xml",
        "dtd-after-prefix",
        "zip-bomb",
    ],
)
def test_malicious_ooxml_packages_are_rejected(
    docx_template_bytes: bytes,
    builder,
    message: str,
) -> None:
    malicious = builder(docx_template_bytes)
    with pytest.raises(TemplateSecurityError, match=message):
        inspect_ooxml_package(
            malicious,
            "docx",
            expected_placeholders=DOCX_PLACEHOLDERS,
        )


def test_corrupt_or_truncated_zip_is_rejected(docx_template_bytes: bytes) -> None:
    with pytest.raises(TemplateSecurityError, match="corrupt"):
        inspect_ooxml_package(
            docx_template_bytes[:-100],
            "docx",
            expected_placeholders=DOCX_PLACEHOLDERS,
        )


def test_explicit_entry_and_package_budgets_are_enforced(
    docx_template_bytes: bytes,
) -> None:
    with pytest.raises(TemplateSecurityError, match="package"):
        inspect_ooxml_package(
            docx_template_bytes,
            "docx",
            expected_placeholders=DOCX_PLACEHOLDERS,
            limits=TemplateSafetyLimits(max_package_bytes=100),
        )


def test_unknown_missing_malformed_and_unsupported_placeholders_fail_closed(
    tmp_path: Path,
    docx_template_bytes: bytes,
    xlsx_template_bytes: bytes,
) -> None:
    registry = OfficeTemplateRegistry(tmp_path / "registry")
    source = write_source(tmp_path / "source", "fixture.docx", docx_template_bytes)
    unknown_manifest = manifest_for(docx_template_bytes, "docx", ())
    with pytest.raises(TemplateContractError, match="unknown placeholders"):
        registry.import_template(unknown_manifest, source)

    missing_manifest = manifest_for(
        docx_template_bytes,
        "docx",
        tuple(sorted(DOCX_PLACEHOLDERS + ("not_present",))),
        version="2.0.0",
    )
    with pytest.raises(TemplateContractError, match="missing required"):
        registry.import_template(missing_manifest, source)

    entries = zip_entries(docx_template_bytes)
    malformed = rewrite_zip(
        docx_template_bytes,
        replacements={
            "word/document.xml": entries["word/document.xml"].replace(
                b"{{body}}",
                b"{{body}",
            )
        },
    )
    malformed_manifest = manifest_for(
        malformed,
        "docx",
        ("client", "footer", "header", "table"),
        version="3.0.0",
    )
    malformed_source = write_source(
        tmp_path / "source",
        "malformed.docx",
        malformed,
    )
    with pytest.raises(TemplateContractError, match="Malformed"):
        registry.import_template(malformed_manifest, malformed_source)

    xlsx_entries = zip_entries(xlsx_template_bytes)
    chart_name = next(name for name in xlsx_entries if name.startswith("xl/charts/"))
    chart_with_token = xlsx_entries[chart_name].replace(
        "\u9500\u91cf\u56fe".encode(),
        b"{{chart}}",
    )
    assert chart_with_token != xlsx_entries[chart_name]
    unsupported = rewrite_zip(
        xlsx_template_bytes,
        replacements={chart_name: chart_with_token},
    )
    with pytest.raises(TemplateContractError, match="unsupported"):
        inspect_ooxml_package(
            unsupported,
            "xlsx",
            expected_placeholders=("chart", "company"),
        )


def test_manifest_digest_must_match_exact_import_bytes(
    tmp_path: Path,
    docx_template_bytes: bytes,
) -> None:
    registry = OfficeTemplateRegistry(tmp_path / "registry")
    source = write_source(tmp_path / "source", "fixture.docx", docx_template_bytes)
    manifest = manifest_for(docx_template_bytes, "docx", DOCX_PLACEHOLDERS)
    mismatched = manifest.__class__(
        **{
            **manifest.to_dict(),
            "source_sha256": hashlib.sha256(b"different").hexdigest(),
            "required_placeholders": manifest.required_placeholders,
            "allowed_output_rules": manifest.allowed_output_rules,
        }
    )
    with pytest.raises(TemplateContractError, match="SHA-256"):
        registry.import_template(mismatched, source)


def test_formula_placeholder_is_not_treated_as_executable_template_text(
    xlsx_template_bytes: bytes,
) -> None:
    entries = zip_entries(xlsx_template_bytes)
    worksheet = entries["xl/worksheets/sheet1.xml"]
    assert b"SUM(B2:B4)" in worksheet
    formula_token = rewrite_zip(
        xlsx_template_bytes,
        replacements={
            "xl/worksheets/sheet1.xml": worksheet.replace(
                b"SUM(B2:B4)",
                b"{{company}}",
            )
        },
    )

    with pytest.raises(TemplateContractError, match="outside Office text"):
        inspect_ooxml_package(
            formula_token,
            "xlsx",
            expected_placeholders=("company",),
        )
