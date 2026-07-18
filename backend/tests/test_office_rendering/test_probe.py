from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Final
import zipfile

from pypdf import PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
)
import pytest

from app.office_rendering.deployment import fingerprint_office_renderer_bundle
from app.office_rendering.libreoffice import LIBREOFFICE_PARAMETERS_VERSION
from app.office_rendering.models import (
    AUTHORITATIVE_QUALITY,
    PageArtifact,
    PdfArtifact,
    RenderManifest,
    RendererDescriptor,
    RenderRequest,
)
from app.office_rendering.probe import (
    AuthoritativeRendererProbeError,
    AuthoritativeRendererProbeManifest,
    AuthoritativeRendererProbePage,
    PROBE_DIRECTORY,
    PROBE_DPI,
    PROBE_MANIFEST_FILENAME,
    PROBE_PARAMETERS_VERSION,
    PROBE_SOURCE_FILENAME,
    execute_authoritative_office_renderer_probe,
)
from app.office_rendering.provider import ProviderAvailability
from tests.test_office_rendering.helpers import png_bytes, rgba_pixel_sha256


_FONT_PATH: Final = (
    Path(__file__).resolve().parents[2]
    / "app"
    / "data"
    / "fonts"
    / "SuxiaoyouCJK-Regular.ttf"
)


def _docx_bytes() -> bytes:
    parts = {
        "[Content_Types].xml": b"""<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Override PartName="/word/document.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>""",
        "_rels/.rels": b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
    Target="word/document.xml"/>
</Relationships>""",
        "word/document.xml": """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>苏小有 authoritative renderer probe</w:t></w:r></w:p></w:body>
</w:document>""".encode(),
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in sorted(parts.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    return output.getvalue()


def _pdf_bytes(
    *,
    embedded_font: bool,
    width_px: int,
    height_px: int,
) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(
        width=width_px * 72 / PROBE_DPI,
        height=height_px * 72 / PROBE_DPI,
    )
    if embedded_font:
        font_file = DecodedStreamObject()
        font_file.set_data(_FONT_PATH.read_bytes())
        font_file_reference = writer._add_object(font_file)
        descriptor = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/FontDescriptor"),
                NameObject("/FontName"): NameObject("/SuxiaoyouProbe"),
                NameObject("/Flags"): NumberObject(4),
                NameObject("/FontFile2"): font_file_reference,
            }
        )
        descriptor_reference = writer._add_object(descriptor)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/TrueType"),
                NameObject("/BaseFont"): NameObject("/SuxiaoyouProbe"),
                NameObject("/FontDescriptor"): descriptor_reference,
            }
        )
        font_reference = writer._add_object(font)
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject(
                    {NameObject("/F1"): font_reference}
                )
            }
        )
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class _RenderingFakeProvider:
    def __init__(
        self,
        *,
        page_content: bytes,
        embedded_font: bool = True,
        forged_pixel_sha256: str | None = None,
        pdf_dimensions_px: tuple[int, int] | None = None,
    ) -> None:
        self._descriptor = RendererDescriptor(
            renderer_id="test-authoritative-renderer",
            renderer_version="probe-test-v1",
            font_digest=hashlib.sha256(b"probe-test-font-environment").hexdigest(),
            quality=AUTHORITATIVE_QUALITY,
        )
        self.page_content = page_content
        self.embedded_font = embedded_font
        self.forged_pixel_sha256 = forged_pixel_sha256
        self.pdf_dimensions_px = pdf_dimensions_px
        self.calls = 0
        self.requests: list[RenderRequest] = []

    @property
    def descriptor(self) -> RendererDescriptor:
        return self._descriptor

    def availability(self) -> ProviderAvailability:
        return ProviderAvailability(available=True)

    async def render(
        self,
        request: RenderRequest,
        output_dir: Path,
    ) -> RenderManifest:
        self.calls += 1
        self.requests.append(request)
        page_path = output_dir / "page-1.png"
        page_path.write_bytes(self.page_content)
        from PIL import Image

        with Image.open(io.BytesIO(self.page_content)) as image:
            width, height = image.size
        pdf_width, pdf_height = self.pdf_dimensions_px or (width, height)
        pdf_content = _pdf_bytes(
            embedded_font=self.embedded_font,
            width_px=pdf_width,
            height_px=pdf_height,
        )
        pdf_path = output_dir / "document.pdf"
        pdf_path.write_bytes(pdf_content)
        page = PageArtifact(
            page_number=1,
            filename=page_path.name,
            sha256=hashlib.sha256(self.page_content).hexdigest(),
            pixel_sha256=(
                self.forged_pixel_sha256
                if self.forged_pixel_sha256 is not None
                else rgba_pixel_sha256(self.page_content)
            ),
            size_bytes=len(self.page_content),
            width_px=width,
            height_px=height,
        )
        pdf = PdfArtifact(
            filename=pdf_path.name,
            sha256=hashlib.sha256(pdf_content).hexdigest(),
            size_bytes=len(pdf_content),
            page_count=1,
        )
        return RenderManifest.for_request(
            request,
            self.descriptor,
            (page,),
            pdf=pdf,
        )


def _probe_bundle(
    tmp_path: Path,
    *,
    expected_page: bytes,
    canonical_manifest: bool = True,
) -> tuple[Path, str, AuthoritativeRendererProbeManifest]:
    root = tmp_path / "renderer"
    probe = root.joinpath(*PROBE_DIRECTORY.parts)
    probe.mkdir(parents=True)
    source = _docx_bytes()
    (probe / PROBE_SOURCE_FILENAME).write_bytes(source)
    from PIL import Image

    with Image.open(io.BytesIO(expected_page)) as image:
        width, height = image.size
    manifest = AuthoritativeRendererProbeManifest(
        source_sha256=hashlib.sha256(source).hexdigest(),
        pages=(
            AuthoritativeRendererProbePage(
                page_number=1,
                width_px=width,
                height_px=height,
                pixel_sha256=rgba_pixel_sha256(expected_page),
            ),
        ),
    )
    manifest_path = probe / PROBE_MANIFEST_FILENAME
    if canonical_manifest:
        manifest_path.write_bytes(manifest.canonical_bytes())
    else:
        manifest_path.write_text(
            json.dumps(manifest.to_dict(), indent=2) + "\n",
            encoding="utf-8",
        )
    return root, fingerprint_office_renderer_bundle(root), manifest


@pytest.mark.asyncio
async def test_probe_executes_provider_and_returns_only_path_free_hashes_and_counts(
    tmp_path: Path,
) -> None:
    page = png_bytes(width=3, height=2, red=37)
    root, bundle_tree, golden = _probe_bundle(tmp_path, expected_page=page)
    provider = _RenderingFakeProvider(page_content=page)

    report = await execute_authoritative_office_renderer_probe(
        provider,
        bundle_root=root,
        expected_bundle_tree_sha256=bundle_tree,
    )

    assert provider.calls == 1
    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert PROBE_PARAMETERS_VERSION == LIBREOFFICE_PARAMETERS_VERSION
    assert request.parameters_version == PROBE_PARAMETERS_VERSION
    assert request.parameters_dict() == {"dpi": PROBE_DPI}
    assert request.document_format == "docx"
    assert report.bundle_tree_sha256 == bundle_tree
    assert report.probe_source_sha256 == golden.source_sha256
    assert report.page_count == 1
    assert report.embedded_font_count == 1
    assert report.pages == golden.pages
    payload = report.to_dict()
    assert set(payload) == {
        "bundle_tree_sha256",
        "embedded_font_count",
        "page_count",
        "pages",
        "pdf_sha256",
        "probe_manifest_sha256",
        "probe_source_sha256",
        "render_manifest_sha256",
        "schema_version",
    }
    assert str(tmp_path) not in json.dumps(payload, sort_keys=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("actual_page", "embedded_font", "forged_pixel_sha256", "error"),
    (
        (png_bytes(width=3, height=2, red=99), True, None, "decoded pixels"),
        (png_bytes(width=3, height=2, red=37), False, None, "embedded font"),
        (png_bytes(width=3, height=2, red=37), True, "0" * 64, "decoded pixels"),
    ),
    ids=(
        "wrong-golden-pixels",
        "missing-embedded-font",
        "forged-pixel-manifest",
    ),
)
async def test_probe_rejects_wrong_pixels_missing_font_and_forged_page_manifest(
    tmp_path: Path,
    actual_page: bytes,
    embedded_font: bool,
    forged_pixel_sha256: str | None,
    error: str,
) -> None:
    expected_page = png_bytes(width=3, height=2, red=37)
    root, bundle_tree, _golden = _probe_bundle(
        tmp_path,
        expected_page=expected_page,
    )
    provider = _RenderingFakeProvider(
        page_content=actual_page,
        embedded_font=embedded_font,
        forged_pixel_sha256=forged_pixel_sha256,
    )

    with pytest.raises(AuthoritativeRendererProbeError, match=error):
        await execute_authoritative_office_renderer_probe(
            provider,
            bundle_root=root,
            expected_bundle_tree_sha256=bundle_tree,
        )
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_probe_rejects_noncanonical_manifest_before_render(tmp_path: Path) -> None:
    page = png_bytes(red=37)
    root, bundle_tree, _golden = _probe_bundle(
        tmp_path,
        expected_page=page,
        canonical_manifest=False,
    )
    provider = _RenderingFakeProvider(page_content=page)

    with pytest.raises(AuthoritativeRendererProbeError, match="not canonical"):
        await execute_authoritative_office_renderer_probe(
            provider,
            bundle_root=root,
            expected_bundle_tree_sha256=bundle_tree,
        )
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_probe_rejects_pdf_dimensions_unrelated_to_144dpi_pages(
    tmp_path: Path,
) -> None:
    page = png_bytes(width=3, height=2, red=37)
    root, bundle_tree, _golden = _probe_bundle(tmp_path, expected_page=page)
    provider = _RenderingFakeProvider(
        page_content=page,
        pdf_dimensions_px=(4, 2),
    )

    with pytest.raises(AuthoritativeRendererProbeError, match="144-DPI"):
        await execute_authoritative_office_renderer_probe(
            provider,
            bundle_root=root,
            expected_bundle_tree_sha256=bundle_tree,
        )
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_probe_rejects_bundle_or_source_drift_before_render(tmp_path: Path) -> None:
    page = png_bytes(red=37)
    root, signed_tree, _golden = _probe_bundle(tmp_path, expected_page=page)
    source = root.joinpath(*PROBE_DIRECTORY.parts, PROBE_SOURCE_FILENAME)
    source.write_bytes(_docx_bytes() + b"drift")
    provider = _RenderingFakeProvider(page_content=page)

    with pytest.raises(AuthoritativeRendererProbeError, match="bundle tree"):
        await execute_authoritative_office_renderer_probe(
            provider,
            bundle_root=root,
            expected_bundle_tree_sha256=signed_tree,
        )
    assert provider.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing_name",
    (PROBE_MANIFEST_FILENAME, PROBE_SOURCE_FILENAME),
)
async def test_probe_rejects_signed_tree_that_omits_required_probe_input(
    tmp_path: Path,
    missing_name: str,
) -> None:
    page = png_bytes(red=37)
    root, _complete_tree, _golden = _probe_bundle(tmp_path, expected_page=page)
    root.joinpath(*PROBE_DIRECTORY.parts, missing_name).unlink()
    incomplete_tree = fingerprint_office_renderer_bundle(root)
    provider = _RenderingFakeProvider(page_content=page)

    with pytest.raises(AuthoritativeRendererProbeError, match="file is unavailable"):
        await execute_authoritative_office_renderer_probe(
            provider,
            bundle_root=root,
            expected_bundle_tree_sha256=incomplete_tree,
        )
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_probe_rejects_tree_bound_bytes_that_only_claim_to_be_docx(
    tmp_path: Path,
) -> None:
    page = png_bytes(red=37)
    root, _original_tree, golden = _probe_bundle(tmp_path, expected_page=page)
    probe = root.joinpath(*PROBE_DIRECTORY.parts)
    fake_docx = b"not-an-ooxml-package"
    (probe / PROBE_SOURCE_FILENAME).write_bytes(fake_docx)
    forged = AuthoritativeRendererProbeManifest(
        source_sha256=hashlib.sha256(fake_docx).hexdigest(),
        pages=golden.pages,
    )
    (probe / PROBE_MANIFEST_FILENAME).write_bytes(forged.canonical_bytes())
    forged_tree = fingerprint_office_renderer_bundle(root)
    provider = _RenderingFakeProvider(page_content=page)

    with pytest.raises(AuthoritativeRendererProbeError, match="DOCX is invalid"):
        await execute_authoritative_office_renderer_probe(
            provider,
            bundle_root=root,
            expected_bundle_tree_sha256=forged_tree,
        )
    assert provider.calls == 0
