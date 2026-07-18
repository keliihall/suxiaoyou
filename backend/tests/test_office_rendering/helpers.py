from __future__ import annotations

import binascii
import hashlib
import io
import struct
import zlib
from collections.abc import Callable, Sequence
from pathlib import Path

from PIL import Image
from pypdf import PdfWriter

from app.office_rendering import (
    PageArtifact,
    PdfArtifact,
    ProviderAvailability,
    RenderManifest,
    RendererDescriptor,
    RenderRequest,
)


def png_bytes(width: int = 2, height: int = 2, *, red: int = 24) -> bytes:
    """Return a small, valid RGBA PNG using only the standard library."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", binascii.crc32(kind + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    row = b"\x00" + bytes((red, 48, 72, 255)) * width
    pixels = row * height
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )


def rgba_pixel_sha256(content: bytes) -> str:
    with Image.open(io.BytesIO(content)) as image:
        image.load()
        return hashlib.sha256(image.convert("RGBA").tobytes("raw", "RGBA")).hexdigest()


def pdf_bytes(*, pages: int = 1) -> bytes:
    output = io.BytesIO()
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    writer.write(output)
    return output.getvalue()


def write_render_artifacts(
    output_dir: Path,
    page_contents: Sequence[bytes],
) -> tuple[PdfArtifact, tuple[PageArtifact, ...]]:
    pdf_content = pdf_bytes(pages=len(page_contents))
    pdf_path = output_dir / "document.pdf"
    pdf_path.write_bytes(pdf_content)
    pages: list[PageArtifact] = []
    for page_number, content in enumerate(page_contents, start=1):
        page_path = output_dir / f"page-{page_number}.png"
        page_path.write_bytes(content)
        with Image.open(io.BytesIO(content)) as image:
            width, height = image.size
        pages.append(
            PageArtifact(
                page_number=page_number,
                filename=page_path.name,
                sha256=hashlib.sha256(content).hexdigest(),
                pixel_sha256=rgba_pixel_sha256(content),
                size_bytes=len(content),
                width_px=width,
                height_px=height,
            )
        )
    return (
        PdfArtifact(
            filename=pdf_path.name,
            sha256=hashlib.sha256(pdf_content).hexdigest(),
            size_bytes=len(pdf_content),
            page_count=len(pages),
        ),
        tuple(pages),
    )


def make_request(
    workspace: Path,
    *,
    filename: str = "source.docx",
    content: bytes = b"office source",
    parameters_version: str = "preview-v1",
    parameters: dict[str, object] | None = None,
) -> RenderRequest:
    workspace.mkdir(parents=True, exist_ok=True)
    source = workspace / filename
    source.write_bytes(content)
    return RenderRequest(
        workspace_root=workspace,
        source_path=source,
        document_format=filename.rsplit(".", 1)[-1].lower(),  # type: ignore[arg-type]
        source_sha256=hashlib.sha256(content).hexdigest(),
        parameters_version=parameters_version,
        parameters=parameters or {"dpi": 144},
    )


class FakeProvider:
    def __init__(
        self,
        descriptor: RendererDescriptor,
        *,
        available: bool = True,
        unavailable_reason: str = "test renderer is unavailable",
        manifest_descriptor: RendererDescriptor | None = None,
        before_return: Callable[[RenderRequest, Path], None] | None = None,
        write_extra_file: bool = False,
    ) -> None:
        self._descriptor = descriptor
        self._availability = ProviderAvailability(
            available=available,
            reason=None if available else unavailable_reason,
        )
        self.manifest_descriptor = manifest_descriptor
        self.before_return = before_return
        self.write_extra_file = write_extra_file
        self.calls = 0

    @property
    def descriptor(self) -> RendererDescriptor:
        return self._descriptor

    def availability(self) -> ProviderAvailability:
        return self._availability

    async def render(
        self,
        request: RenderRequest,
        output_dir: Path,
    ) -> RenderManifest:
        self.calls += 1
        content = png_bytes()
        pdf, pages = write_render_artifacts(output_dir, (content,))
        if self.write_extra_file:
            (output_dir / "undeclared.bin").write_bytes(b"not declared")
        if self.before_return is not None:
            self.before_return(request, output_dir)
        return RenderManifest.for_request(
            request,
            self.manifest_descriptor or self._descriptor,
            pages,
            pdf=pdf,
        )
