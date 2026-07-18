from __future__ import annotations

from pathlib import Path

import pytest

from app.office_validation import (
    OfficeValidationContractError,
    OfficeValidationSecurityError,
    compare_ooxml_parts,
    inspect_ooxml_path,
)
from tests.test_office_templates.helpers import (
    make_docx_template,
    rewrite_zip,
    write_source,
    zip_entries,
)


def test_ooxml_delta_allows_only_explicit_parts_and_preserves_placeholders(
    tmp_path: Path,
) -> None:
    content = make_docx_template()
    entries = zip_entries(content)
    changed_document = entries["word/document.xml"].replace(
        "正文".encode(),
        "报告".encode(),
        1,
    )
    assert changed_document != entries["word/document.xml"]
    candidate = rewrite_zip(
        content,
        replacements={"word/document.xml": changed_document},
    )
    baseline_path = write_source(tmp_path, "baseline.docx", content)
    candidate_path = write_source(tmp_path, "candidate.docx", candidate)

    baseline = inspect_ooxml_path(baseline_path, "docx")
    after = inspect_ooxml_path(candidate_path, "docx")
    allowed = compare_ooxml_parts(
        baseline,
        after,
        allowed_changed_parts=("word/document.xml",),
    )
    rejected = compare_ooxml_parts(
        baseline,
        after,
        allowed_changed_parts=("word/header*.xml",),
    )

    assert allowed.passed
    assert [(item.part_name, item.operation) for item in allowed.changes] == [
        ("word/document.xml", "modified")
    ]
    assert not rejected.passed
    assert rejected.rejected_parts == ("word/document.xml",)


def test_ooxml_delta_validates_policy_and_budget(tmp_path: Path) -> None:
    path = write_source(tmp_path, "same.docx", make_docx_template())
    manifest = inspect_ooxml_path(path, "docx")

    with pytest.raises(OfficeValidationContractError, match="patterns"):
        compare_ooxml_parts(
            manifest,
            manifest,
            allowed_changed_parts=("../word/*",),
        )


def test_ooxml_inspection_rejects_final_symlink(tmp_path: Path) -> None:
    source = write_source(tmp_path, "source.docx", make_docx_template())
    link = tmp_path / "link.docx"
    try:
        link.symlink_to(source.name)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable")

    with pytest.raises(OfficeValidationSecurityError, match="regular file"):
        inspect_ooxml_path(link.absolute(), "docx")
