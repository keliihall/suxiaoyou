from __future__ import annotations

from io import BytesIO
import hashlib
from pathlib import Path

from PIL import Image
import pytest

from app.office_rendering import RenderManifest
from app.office_validation import (
    EvidenceBox,
    OfficeValidationSecurityError,
    VisualDiffPolicy,
    compare_rendered_pages,
)
from tests.test_office_rendering.helpers import write_render_artifacts


FONT = "f" * 64


def _png(pixels: list[list[tuple[int, int, int, int]]]) -> bytes:
    height = len(pixels)
    width = len(pixels[0])
    image = Image.new("RGBA", (width, height))
    image.putdata([item for row in pixels for item in row])
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _manifest(
    entry: Path,
    content: bytes,
    *,
    source: str,
    quality: str = "authoritative",
    renderer: str = "renderer",
) -> RenderManifest:
    entry.mkdir()
    pdf, pages = write_render_artifacts(entry, (content,))
    return RenderManifest(
        cache_key=hashlib.sha256((source + renderer).encode()).hexdigest(),
        source_sha256=hashlib.sha256(source.encode()).hexdigest(),
        document_format="docx",
        renderer_id=renderer,
        renderer_version="1.0",
        font_digest=FONT,
        parameters_version="v1",
        parameters_sha256="a" * 64,
        quality=quality,  # type: ignore[arg-type]
        pdf=pdf,
        pages=pages,
    )


def _manifest_pages(
    entry: Path,
    contents: list[bytes],
    *,
    source: str,
) -> RenderManifest:
    entry.mkdir()
    pdf, pages = write_render_artifacts(entry, contents)
    return RenderManifest(
        cache_key=hashlib.sha256((source + "renderer").encode()).hexdigest(),
        source_sha256=hashlib.sha256(source.encode()).hexdigest(),
        document_format="docx",
        renderer_id="renderer",
        renderer_version="1.0",
        font_digest=FONT,
        parameters_version="v1",
        parameters_sha256="a" * 64,
        quality="authoritative",
        pdf=pdf,
        pages=pages,
    )


def test_visual_diff_reports_outside_region_and_allows_explicit_box(
    tmp_path: Path,
) -> None:
    white = (255, 255, 255, 255)
    black = (0, 0, 0, 255)
    before_bytes = _png([[white, white], [white, white]])
    after_bytes = _png([[black, white], [white, white]])
    before = _manifest(tmp_path / "before", before_bytes, source="before")
    after = _manifest(tmp_path / "after", after_bytes, source="after")

    rejected = compare_rendered_pages(
        before,
        tmp_path / "before",
        after,
        tmp_path / "after",
        VisualDiffPolicy(max_blank_fraction_increase=1.0),
        checkpoint_id="checkpoint",
        root_turn_id="turn",
    )
    allowed = compare_rendered_pages(
        before,
        tmp_path / "before",
        after,
        tmp_path / "after",
        VisualDiffPolicy(
            allowed_regions=(EvidenceBox(1, 0, 0, 1, 1),),
            max_blank_fraction_increase=1.0,
        ),
    )

    assert rejected.verdict == "fail"
    pixel_check = next(item for item in rejected.checks if item.code == "pixel_delta")
    assert pixel_check.box == EvidenceBox(1, 0, 0, 1, 1)
    assert rejected.checkpoint_id == "checkpoint"
    assert rejected.root_turn_id == "turn"
    assert allowed.verdict == "pass"


def test_approximate_or_changed_renderer_requires_review(tmp_path: Path) -> None:
    content = _png([[(20, 30, 40, 255)]])
    before = _manifest(tmp_path / "before", content, source="before")
    after = _manifest(
        tmp_path / "after",
        content,
        source="after",
        quality="approximate",
        renderer="renderer-v2",
    )

    report = compare_rendered_pages(
        before,
        tmp_path / "before",
        after,
        tmp_path / "after",
        VisualDiffPolicy(),
    )

    assert report.verdict == "needs_review"
    outcomes = {item.code: item.outcome for item in report.checks}
    assert outcomes["renderer_identity"] == "needs_review"
    assert outcomes["authoritative_quality"] == "needs_review"


def test_blank_area_increase_is_a_deterministic_failure(tmp_path: Path) -> None:
    black = (0, 0, 0, 255)
    white = (255, 255, 255, 255)
    before = _manifest(tmp_path / "before", _png([[black, black]]), source="before")
    after = _manifest(tmp_path / "after", _png([[white, white]]), source="after")

    report = compare_rendered_pages(
        before,
        tmp_path / "before",
        after,
        tmp_path / "after",
        VisualDiffPolicy(
            allowed_regions=(EvidenceBox(1, 0, 0, 2, 1),),
            max_blank_fraction_increase=0.25,
        ),
    )

    assert report.verdict == "fail"
    assert next(item for item in report.checks if item.code == "blank_area").outcome == "fail"


def test_absolute_candidate_blank_guard_rejects_a_self_comparison(
    tmp_path: Path,
) -> None:
    blank = _png([[(255, 255, 255, 255), (255, 255, 255, 255)]])
    before = _manifest(tmp_path / "before", blank, source="before")
    after = _manifest(tmp_path / "after", blank, source="after")

    report = compare_rendered_pages(
        before,
        tmp_path / "before",
        after,
        tmp_path / "after",
        VisualDiffPolicy(max_candidate_blank_fraction=0.999),
    )

    assert report.verdict == "fail"
    assert next(
        item for item in report.checks if item.code == "candidate_blank_area"
    ).outcome == "fail"


def test_reviewed_policy_rejects_a_full_page_pixel_replacement(tmp_path: Path) -> None:
    before = _manifest(
        tmp_path / "before",
        _png([[(10, 20, 30, 255), (10, 20, 30, 255)]]),
        source="before",
    )
    after = _manifest(
        tmp_path / "after",
        _png([[(200, 10, 20, 255), (200, 10, 20, 255)]]),
        source="after",
    )

    report = compare_rendered_pages(
        before,
        tmp_path / "before",
        after,
        tmp_path / "after",
        VisualDiffPolicy(
            max_outside_changed_ratio=0.85,
            max_total_changed_ratio=0.85,
            max_blank_fraction_increase=1.0,
        ),
    )

    assert report.verdict == "fail"
    pixel = next(item for item in report.checks if item.code == "pixel_delta")
    assert dict(pixel.metrics)["total_changed_ratio"] == 1.0


def test_bounded_added_pages_must_contain_rendered_content(tmp_path: Path) -> None:
    content = _png([[(10, 20, 30, 255), (10, 20, 30, 255)]])
    blank = _png([[(255, 255, 255, 255), (255, 255, 255, 255)]])
    baseline = _manifest_pages(
        tmp_path / "baseline",
        [content],
        source="baseline",
    )
    valid = _manifest_pages(
        tmp_path / "valid",
        [content, content],
        source="valid",
    )
    invalid = _manifest_pages(
        tmp_path / "invalid",
        [content, blank],
        source="invalid",
    )
    policy = VisualDiffPolicy(
        max_added_pages=1,
        max_new_page_blank_fraction=0.999,
        max_blank_fraction_increase=1.0,
    )

    accepted = compare_rendered_pages(
        baseline,
        tmp_path / "baseline",
        valid,
        tmp_path / "valid",
        policy,
    )
    rejected = compare_rendered_pages(
        baseline,
        tmp_path / "baseline",
        invalid,
        tmp_path / "invalid",
        policy,
    )

    assert accepted.verdict == "pass"
    assert rejected.verdict == "fail"
    assert next(
        item for item in rejected.checks if item.code == "new_page_blank_area"
    ).outcome == "fail"


def test_required_page_delta_is_exact_for_slide_append_intent(
    tmp_path: Path,
) -> None:
    content = _png([[(10, 20, 30, 255)]])
    baseline = _manifest_pages(tmp_path / "baseline", [content], source="baseline")
    unchanged = _manifest_pages(tmp_path / "unchanged", [content], source="unchanged")
    appended = _manifest_pages(
        tmp_path / "appended",
        [content, content],
        source="appended",
    )
    policy = VisualDiffPolicy(
        max_added_pages=1,
        required_page_delta=1,
        max_new_page_blank_fraction=0.999,
    )

    rejected = compare_rendered_pages(
        baseline,
        tmp_path / "baseline",
        unchanged,
        tmp_path / "unchanged",
        policy,
    )
    accepted = compare_rendered_pages(
        baseline,
        tmp_path / "baseline",
        appended,
        tmp_path / "appended",
        policy,
    )

    assert rejected.verdict == "fail"
    assert accepted.verdict == "pass"


def test_visual_diff_revalidates_artifact_digest(tmp_path: Path) -> None:
    content = _png([[(20, 30, 40, 255)]])
    before = _manifest(tmp_path / "before", content, source="before")
    after = _manifest(tmp_path / "after", content, source="after")
    (tmp_path / "after" / "page-1.png").write_bytes(content + b"tamper")

    with pytest.raises(OfficeValidationSecurityError, match="size"):
        compare_rendered_pages(
            before,
            tmp_path / "before",
            after,
            tmp_path / "after",
            VisualDiffPolicy(),
        )
