"""Deterministic per-page visual comparison for rendered Office artifacts."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path
import stat
from typing import Any

import numpy as np
from PIL import Image, UnidentifiedImageError

from app.office_rendering.models import AUTHORITATIVE_QUALITY, PageArtifact, RenderManifest
from app.office_validation.errors import (
    OfficeValidationContractError,
    OfficeValidationSecurityError,
)
from app.office_validation.models import (
    EvidenceBox,
    OfficeValidationReport,
    ValidationCheck,
    derive_verdict,
)


@dataclass(frozen=True, slots=True)
class VisualDiffPolicy:
    """Golden-case visual limits, including explicitly mutable page regions."""

    allowed_regions: tuple[EvidenceBox, ...] = ()
    pixel_tolerance: int = 0
    max_outside_changed_ratio: float = 0.0
    max_total_changed_ratio: float = 1.0
    max_blank_fraction_increase: float = 0.25
    max_candidate_blank_fraction: float = 1.0
    max_added_pages: int = 0
    max_removed_pages: int = 0
    required_page_delta: int | None = None
    max_new_page_blank_fraction: float = 0.999
    white_threshold: int = 250
    require_authoritative: bool = True
    max_pages: int = 1_000
    max_page_pixels: int = 100_000_000

    def __post_init__(self) -> None:
        try:
            regions = tuple(self.allowed_regions)
        except TypeError as exc:
            raise OfficeValidationContractError("allowed visual regions are invalid") from exc
        if len(regions) > 10_000 or any(
            not isinstance(region, EvidenceBox) for region in regions
        ):
            raise OfficeValidationContractError("allowed visual regions are invalid")
        if regions != tuple(
            sorted(regions, key=lambda item: (item.page_number, item.y, item.x, item.height, item.width))
        ):
            raise OfficeValidationContractError("allowed visual regions must be sorted")
        for field in ("pixel_tolerance", "white_threshold"):
            value = getattr(self, field)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 0 <= value <= 255
            ):
                raise OfficeValidationContractError(f"{field} must be between 0 and 255")
        for field in (
            "max_outside_changed_ratio",
            "max_total_changed_ratio",
            "max_blank_fraction_increase",
            "max_candidate_blank_fraction",
            "max_new_page_blank_fraction",
        ):
            value = getattr(self, field)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0 <= float(value) <= 1
            ):
                raise OfficeValidationContractError(f"{field} must be between 0 and 1")
        for field in ("max_pages", "max_page_pixels"):
            value = getattr(self, field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise OfficeValidationContractError(f"{field} must be positive")
        for field in ("max_added_pages", "max_removed_pages"):
            value = getattr(self, field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise OfficeValidationContractError(
                    f"{field} must be a non-negative integer"
                )
        if self.required_page_delta is not None:
            value = self.required_page_delta
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < -self.max_removed_pages
                or value > self.max_added_pages
            ):
                raise OfficeValidationContractError(
                    "required_page_delta must fit the approved page envelope"
                )
        if not isinstance(self.require_authoritative, bool):
            raise OfficeValidationContractError("require_authoritative must be boolean")
        object.__setattr__(self, "allowed_regions", regions)


def compare_rendered_pages(
    baseline: RenderManifest,
    baseline_entry: Path,
    candidate: RenderManifest,
    candidate_entry: Path,
    policy: VisualDiffPolicy,
    *,
    checkpoint_id: str | None = None,
    root_turn_id: str | None = None,
) -> OfficeValidationReport:
    """Compare validated PNG pages without trusting caller-provided dimensions."""

    if not isinstance(baseline, RenderManifest) or not isinstance(candidate, RenderManifest):
        raise OfficeValidationContractError("visual comparison requires render manifests")
    if not isinstance(policy, VisualDiffPolicy):
        raise OfficeValidationContractError("visual comparison policy is invalid")
    if baseline.document_format != candidate.document_format:
        raise OfficeValidationContractError("rendered Office formats cannot be compared")
    if len(baseline.pages) > policy.max_pages or len(candidate.pages) > policy.max_pages:
        raise OfficeValidationSecurityError("render page count exceeds validation budget")

    checks: list[ValidationCheck] = []
    same_renderer = (
        baseline.renderer_id,
        baseline.renderer_version,
        baseline.font_digest,
    ) == (
        candidate.renderer_id,
        candidate.renderer_version,
        candidate.font_digest,
    )
    checks.append(
        ValidationCheck(
            code="renderer_identity",
            outcome="pass" if same_renderer else "needs_review",
            message=(
                "Renderer and font identities match the approved baseline."
                if same_renderer
                else "Renderer or font identity changed; the visual baseline requires review."
            ),
        )
    )
    same_parameters = (
        baseline.parameters_version,
        baseline.parameters_sha256,
    ) == (
        candidate.parameters_version,
        candidate.parameters_sha256,
    )
    checks.append(
        ValidationCheck(
            code="render_parameters",
            outcome="pass" if same_parameters else "needs_review",
            message=(
                "Render parameters match the approved baseline."
                if same_parameters
                else "Render parameters changed; the visual baseline requires review."
            ),
        )
    )
    authoritative = (
        baseline.quality == AUTHORITATIVE_QUALITY
        and candidate.quality == AUTHORITATIVE_QUALITY
    )
    checks.append(
        ValidationCheck(
            code="authoritative_quality",
            outcome=(
                "pass"
                if authoritative or not policy.require_authoritative
                else "needs_review"
            ),
            message=(
                "Both render sets are authoritative."
                if authoritative
                else "At least one render set is approximate and cannot prove high fidelity."
            ),
        )
    )
    page_delta = len(candidate.pages) - len(baseline.pages)
    page_count_allowed = (
        page_delta == policy.required_page_delta
        if policy.required_page_delta is not None
        else (
            page_delta <= policy.max_added_pages
            and page_delta >= -policy.max_removed_pages
        )
    )
    checks.append(
        ValidationCheck(
            code="page_count",
            outcome="pass" if page_count_allowed else "fail",
            message=(
                "Rendered page-count change is within the approved edit envelope."
                if page_count_allowed
                else "Rendered page count changed outside the approved edit envelope."
            ),
            metrics=_metrics(
                baseline_pages=len(baseline.pages),
                candidate_pages=len(candidate.pages),
                page_delta=page_delta,
            ),
        )
    )

    baseline_root = _validated_entry_root(Path(baseline_entry))
    candidate_root = _validated_entry_root(Path(candidate_entry))
    regions_by_page: dict[int, list[EvidenceBox]] = {}
    for region in policy.allowed_regions:
        regions_by_page.setdefault(region.page_number, []).append(region)

    common = min(len(baseline.pages), len(candidate.pages))
    for index in range(common):
        before_artifact = baseline.pages[index]
        after_artifact = candidate.pages[index]
        before = _load_png(baseline_root, before_artifact, policy.max_page_pixels)
        after = _load_png(candidate_root, after_artifact, policy.max_page_pixels)
        page_number = index + 1
        if before.shape != after.shape:
            checks.append(
                ValidationCheck(
                    code="page_dimensions",
                    outcome="fail",
                    message=f"Rendered dimensions changed on page {page_number}.",
                    box=EvidenceBox(
                        page_number=page_number,
                        x=0,
                        y=0,
                        width=max(before.shape[1], after.shape[1]),
                        height=max(before.shape[0], after.shape[0]),
                    ),
                    metrics=_metrics(
                        baseline_height=before.shape[0],
                        baseline_width=before.shape[1],
                        candidate_height=after.shape[0],
                        candidate_width=after.shape[1],
                    ),
                )
            )
            continue

        height, width, _channels = before.shape
        allowed = np.zeros((height, width), dtype=bool)
        for region in regions_by_page.get(page_number, []):
            if region.x + region.width > width or region.y + region.height > height:
                raise OfficeValidationContractError(
                    f"allowed region exceeds rendered page {page_number}"
                )
            allowed[
                region.y : region.y + region.height,
                region.x : region.x + region.width,
            ] = True

        channel_delta = np.abs(before.astype(np.int16) - after.astype(np.int16))
        changed = np.max(channel_delta, axis=2) > policy.pixel_tolerance
        total_pixels = width * height
        total_changed = int(np.count_nonzero(changed))
        outside_changed_mask = changed & ~allowed
        outside_changed = int(np.count_nonzero(outside_changed_mask))
        total_ratio = total_changed / total_pixels
        outside_ratio = outside_changed / total_pixels
        failed = (
            total_ratio > float(policy.max_total_changed_ratio)
            or outside_ratio > float(policy.max_outside_changed_ratio)
        )
        checks.append(
            ValidationCheck(
                code="pixel_delta",
                outcome="fail" if failed else "pass",
                message=(
                    f"Visual changes exceed the approved regions on page {page_number}."
                    if failed
                    else f"Visual changes are within the approved limits on page {page_number}."
                ),
                box=_bounding_box(outside_changed_mask, page_number),
                metrics=_metrics(
                    outside_changed_pixels=outside_changed,
                    outside_changed_ratio=outside_ratio,
                    total_changed_pixels=total_changed,
                    total_changed_ratio=total_ratio,
                ),
            )
        )

        before_blank = _blank_fraction(before, policy.white_threshold)
        after_blank = _blank_fraction(after, policy.white_threshold)
        blank_increase = max(0.0, after_blank - before_blank)
        blank_failed = blank_increase > float(policy.max_blank_fraction_increase)
        checks.append(
            ValidationCheck(
                code="blank_area",
                outcome="fail" if blank_failed else "pass",
                message=(
                    f"Unexpected blank area increased on page {page_number}."
                    if blank_failed
                    else f"Blank-area change is within limits on page {page_number}."
                ),
                metrics=_metrics(
                    baseline_blank_fraction=before_blank,
                    blank_fraction_increase=blank_increase,
                    candidate_blank_fraction=after_blank,
                ),
            )
        )
        candidate_blank_failed = after_blank > float(
            policy.max_candidate_blank_fraction
        )
        checks.append(
            ValidationCheck(
                code="candidate_blank_area",
                outcome="fail" if candidate_blank_failed else "pass",
                message=(
                    f"Rendered page {page_number} is unexpectedly blank."
                    if candidate_blank_failed
                    else f"Rendered page {page_number} contains sufficient content."
                ),
                metrics=_metrics(
                    candidate_blank_fraction=after_blank,
                    max_candidate_blank_fraction=float(
                        policy.max_candidate_blank_fraction
                    ),
                ),
            )
        )

    # Added pages have no pixel-aligned golden. Structural validation and the
    # semantic repair invariant still bind their content; this independent
    # check prevents an allowed append from silently producing blank pages.
    for index in range(common, len(candidate.pages)):
        artifact = candidate.pages[index]
        page = _load_png(candidate_root, artifact, policy.max_page_pixels)
        blank_fraction = _blank_fraction(page, policy.white_threshold)
        failed = blank_fraction > float(policy.max_new_page_blank_fraction)
        checks.append(
            ValidationCheck(
                code="new_page_blank_area",
                outcome="fail" if failed else "pass",
                message=(
                    f"Added page {index + 1} is unexpectedly blank."
                    if failed
                    else f"Added page {index + 1} contains rendered content."
                ),
                metrics=_metrics(
                    candidate_blank_fraction=blank_fraction,
                    max_new_page_blank_fraction=float(
                        policy.max_new_page_blank_fraction
                    ),
                ),
            )
        )

    frozen_checks = tuple(checks)
    return OfficeValidationReport(
        document_format=baseline.document_format,
        baseline_sha256=baseline.source_sha256,
        candidate_sha256=candidate.source_sha256,
        renderer_id=candidate.renderer_id,
        renderer_version=candidate.renderer_version,
        font_digest=candidate.font_digest,
        verdict=derive_verdict(frozen_checks),
        checks=frozen_checks,
        checkpoint_id=checkpoint_id,
        root_turn_id=root_turn_id,
    )


def _metrics(**values: float | int) -> tuple[tuple[str, float | int], ...]:
    return tuple(sorted(values.items()))


def _validated_entry_root(path: Path) -> Path:
    if not path.is_absolute():
        raise OfficeValidationContractError("render entry path must be absolute")
    try:
        info = path.lstat()
    except OSError as exc:
        raise OfficeValidationSecurityError("render entry is unavailable") from exc
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise OfficeValidationSecurityError("render entry must be a real directory")
    return path.resolve(strict=True)


def _load_png(root: Path, artifact: PageArtifact, max_pixels: int) -> np.ndarray[Any, Any]:
    path = root / artifact.filename
    if path.parent != root:
        raise OfficeValidationSecurityError("render artifact escapes its entry")
    try:
        before = path.lstat()
    except OSError as exc:
        raise OfficeValidationSecurityError("render artifact is unavailable") from exc
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise OfficeValidationSecurityError("render artifact must be a regular file")
    if before.st_size != artifact.size_bytes:
        raise OfficeValidationSecurityError("render artifact size does not match manifest")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise OfficeValidationSecurityError("render artifact cannot be read") from exc
    after = path.lstat()
    if (
        (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        or hashlib.sha256(payload).hexdigest() != artifact.sha256
    ):
        raise OfficeValidationSecurityError("render artifact changed or failed its digest")
    try:
        from io import BytesIO

        with Image.open(BytesIO(payload)) as image:
            if image.format != "PNG" or image.size != (
                artifact.width_px,
                artifact.height_px,
            ):
                raise OfficeValidationSecurityError(
                    "render artifact image metadata does not match manifest"
                )
            if image.width * image.height > max_pixels:
                raise OfficeValidationSecurityError(
                    "render artifact exceeds the pixel budget"
                )
            rgba = image.convert("RGBA")
            rgba.load()
            return np.asarray(rgba, dtype=np.uint8).copy()
    except OfficeValidationSecurityError:
        raise
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        raise OfficeValidationSecurityError("render artifact is not a safe PNG") from exc


def _bounding_box(mask: np.ndarray[Any, Any], page_number: int) -> EvidenceBox | None:
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return None
    left = int(xs.min())
    top = int(ys.min())
    right = int(xs.max())
    bottom = int(ys.max())
    return EvidenceBox(
        page_number=page_number,
        x=left,
        y=top,
        width=right - left + 1,
        height=bottom - top + 1,
    )


def _blank_fraction(image: np.ndarray[Any, Any], threshold: int) -> float:
    rgb = image[:, :, :3]
    alpha = image[:, :, 3]
    blank = (np.min(rgb, axis=2) >= threshold) | (alpha == 0)
    return float(np.count_nonzero(blank)) / int(blank.size)
