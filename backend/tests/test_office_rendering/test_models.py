from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from app.office_rendering import (
    APPROXIMATE_QUALITY,
    AUTHORITATIVE_QUALITY,
    PageArtifact,
    PdfArtifact,
    RenderContractError,
    RenderManifest,
    RendererDescriptor,
    RenderRequest,
    build_cache_key,
)


def _request(**overrides: object) -> RenderRequest:
    values: dict[str, object] = {
        "workspace_root": Path("/tmp/workspace"),
        "source_path": Path("/tmp/workspace/report.docx"),
        "document_format": "docx",
        "source_sha256": "a" * 64,
        "parameters_version": "preview-v1",
        "parameters": {"dpi": 144, "pages": [1, 2]},
    }
    values.update(overrides)
    return RenderRequest(**values)  # type: ignore[arg-type]


def _descriptor(**overrides: object) -> RendererDescriptor:
    values: dict[str, object] = {
        "renderer_id": "fixture-renderer",
        "renderer_version": "1.2.3",
        "font_digest": "b" * 64,
        "quality": APPROXIMATE_QUALITY,
    }
    values.update(overrides)
    return RendererDescriptor(**values)  # type: ignore[arg-type]


def _pdf() -> PdfArtifact:
    return PdfArtifact("document.pdf", "d" * 64, 100, 1)


def _page() -> PageArtifact:
    return PageArtifact(1, "page-1.png", "e" * 64, "f" * 64, 24, 1, 1)


def test_quality_is_an_explicit_closed_enum() -> None:
    assert _descriptor(quality=AUTHORITATIVE_QUALITY).quality == "authoritative"
    assert _descriptor(quality=APPROXIMATE_QUALITY).quality == "approximate"
    with pytest.raises(RenderContractError, match="quality"):
        _descriptor(quality="high-fidelity")


def test_cache_key_changes_for_every_reproducibility_input() -> None:
    request = _request()
    descriptor = _descriptor()
    baseline = build_cache_key(request, descriptor)
    variants = [
        build_cache_key(_request(source_sha256="c" * 64), descriptor),
        build_cache_key(
            _request(
                source_path=Path("/tmp/workspace/report.xlsx"),
                document_format="xlsx",
            ),
            descriptor,
        ),
        build_cache_key(request, replace(descriptor, renderer_id="other")),
        build_cache_key(request, replace(descriptor, renderer_version="2")),
        build_cache_key(request, replace(descriptor, font_digest="d" * 64)),
        build_cache_key(
            request,
            replace(descriptor, quality=AUTHORITATIVE_QUALITY),
        ),
        build_cache_key(_request(parameters_version="preview-v2"), descriptor),
        build_cache_key(_request(parameters={"dpi": 300}), descriptor),
    ]

    assert len(baseline) == 64
    assert baseline not in variants
    assert len(set(variants)) == len(variants)


def test_request_freezes_parameters_and_rejects_ambiguous_input() -> None:
    parameters = {"dpi": 144, "nested": {"value": [1, 2]}}
    request = _request(parameters=parameters)
    original_digest = request.parameters_sha256
    parameters["dpi"] = 300
    nested = parameters["nested"]
    assert isinstance(nested, dict)
    nested["value"] = [9]

    assert request.parameters_sha256 == original_digest
    assert request.parameters_dict() == {"dpi": 144, "nested": {"value": [1, 2]}}
    with pytest.raises(TypeError):
        request.parameters["dpi"] = 600  # type: ignore[index]
    with pytest.raises(RenderContractError, match="absolute"):
        _request(source_path=Path("relative.docx"))
    with pytest.raises(RenderContractError, match="extension"):
        _request(document_format="xlsx")
    with pytest.raises(RenderContractError, match="non-finite"):
        _request(parameters={"scale": float("nan")})


def test_page_and_manifest_reject_path_and_schema_smuggling() -> None:
    with pytest.raises(RenderContractError, match="safe PNG basename"):
        PageArtifact(
            page_number=1,
            filename="../outside.png",
            sha256="e" * 64,
            pixel_sha256="f" * 64,
            size_bytes=24,
            width_px=1,
            height_px=1,
        )

    request = _request()
    descriptor = _descriptor()
    page = _page()
    manifest = RenderManifest.for_request(request, descriptor, (page,), pdf=_pdf())
    payload = manifest.to_dict()
    payload["unexpected"] = True
    with pytest.raises(RenderContractError, match="fields"):
        RenderManifest.from_dict(payload)

    missing_pixel = page.to_dict()
    del missing_pixel["pixel_sha256"]
    with pytest.raises(RenderContractError, match="page artifact fields"):
        PageArtifact.from_dict(missing_pixel)

    missing_pdf = manifest.to_dict()
    del missing_pdf["pdf"]
    with pytest.raises(RenderContractError, match="manifest fields"):
        RenderManifest.from_dict(missing_pdf)

    with pytest.raises(RenderContractError, match="page count"):
        RenderManifest.for_request(
            request,
            descriptor,
            (page,),
            pdf=PdfArtifact("document.pdf", "d" * 64, 100, 2),
        )


def test_manifest_round_trip_preserves_explicit_approximate_quality() -> None:
    request = _request()
    descriptor = _descriptor(quality=APPROXIMATE_QUALITY)
    page = _page()
    manifest = RenderManifest.for_request(request, descriptor, (page,), pdf=_pdf())

    restored = RenderManifest.from_dict(manifest.to_dict())

    assert restored == manifest
    assert restored.quality == "approximate"
    assert restored.canonical_bytes().endswith(b"\n")
