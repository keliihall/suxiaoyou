from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from app.office_rendering import (
    APPROXIMATE_QUALITY,
    AUTHORITATIVE_QUALITY,
    CacheIntegrityError,
    CacheWriteError,
    OfficeRenderCache,
    PageArtifact,
    PdfArtifact,
    PathEscapeError,
    ProviderUnavailableError,
    RenderManifest,
    RendererDescriptor,
    StaleSourceError,
    build_cache_key,
)
from app.office_rendering.cache import MANIFEST_DIGEST_FILENAME, MANIFEST_FILENAME
from tests.test_office_rendering.helpers import (
    FakeProvider,
    make_request,
    pdf_bytes,
    png_bytes,
    rgba_pixel_sha256,
)


def _descriptor(
    *,
    renderer_id: str = "fixture-renderer",
    renderer_version: str = "1.0.0",
    font_digest: str = "f" * 64,
    quality: str = APPROXIMATE_QUALITY,
) -> RendererDescriptor:
    return RendererDescriptor(
        renderer_id=renderer_id,
        renderer_version=renderer_version,
        font_digest=font_digest,
        quality=quality,  # type: ignore[arg-type]
    )


def _entry_path(cache: OfficeRenderCache, cache_key: str) -> Path:
    return cache.root / "entries" / cache_key[:2] / cache_key


@pytest.mark.asyncio
async def test_atomic_content_addressed_write_then_validated_hit(
    tmp_path: Path,
) -> None:
    request = make_request(tmp_path / "workspace")
    descriptor = _descriptor(quality=APPROXIMATE_QUALITY)
    provider = FakeProvider(descriptor)
    cache = OfficeRenderCache(tmp_path / "private-cache")

    first = await cache.get_or_render(request, provider)
    second = await cache.get_or_render(request, provider)

    assert first == second
    assert first.quality == "approximate"
    assert provider.calls == 1
    entry = _entry_path(cache, first.cache_key)
    assert {path.name for path in entry.iterdir()} == {
        "document.pdf",
        "page-1.png",
        MANIFEST_FILENAME,
        MANIFEST_DIGEST_FILENAME,
    }
    assert cache.page_path(request, descriptor, 1) == entry / "page-1.png"
    assert cache.pdf_path(request, descriptor) == entry / "document.pdf"
    if os.name != "nt":
        assert stat.S_IMODE(cache.root.stat().st_mode) == 0o700
        assert stat.S_IMODE((entry / "page-1.png").stat().st_mode) == 0o600
        assert stat.S_IMODE((entry / MANIFEST_FILENAME).stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_unavailable_provider_is_not_hidden_by_a_cache_hit(
    tmp_path: Path,
) -> None:
    request = make_request(tmp_path / "workspace")
    descriptor = _descriptor()
    cache = OfficeRenderCache(tmp_path / "cache")
    await cache.get_or_render(request, FakeProvider(descriptor))
    unavailable = FakeProvider(descriptor, available=False)

    with pytest.raises(ProviderUnavailableError, match="unavailable"):
        await cache.get_or_render(request, unavailable)
    assert unavailable.calls == 0


@pytest.mark.asyncio
async def test_provider_cannot_upgrade_or_downgrade_manifest_quality(
    tmp_path: Path,
) -> None:
    request = make_request(tmp_path / "workspace")
    authoritative = _descriptor(quality=AUTHORITATIVE_QUALITY)
    approximate = _descriptor(quality=APPROXIMATE_QUALITY)
    provider = FakeProvider(
        authoritative,
        manifest_descriptor=approximate,
    )
    cache = OfficeRenderCache(tmp_path / "cache")

    with pytest.raises(CacheIntegrityError, match="does not match"):
        await cache.get_or_render(request, provider)
    assert list((cache.root / "entries").rglob(MANIFEST_FILENAME)) == []


@pytest.mark.asyncio
async def test_stale_source_sha_is_rejected_before_cache_reuse(
    tmp_path: Path,
) -> None:
    request = make_request(tmp_path / "workspace")
    descriptor = _descriptor()
    cache = OfficeRenderCache(tmp_path / "cache")
    await cache.get_or_render(request, FakeProvider(descriptor))
    request.source_path.write_bytes(b"changed outside the render transaction")

    with pytest.raises(StaleSourceError, match="SHA-256"):
        cache.load(request, descriptor)


@pytest.mark.asyncio
async def test_source_change_during_render_is_never_published(tmp_path: Path) -> None:
    request = make_request(tmp_path / "workspace")
    descriptor = _descriptor()

    def mutate_source(render_request, _output_dir: Path) -> None:
        render_request.source_path.write_bytes(b"raced")

    provider = FakeProvider(descriptor, before_return=mutate_source)
    cache = OfficeRenderCache(tmp_path / "cache")

    with pytest.raises(StaleSourceError, match="SHA-256"):
        await cache.get_or_render(request, provider)
    assert list((cache.root / "entries").rglob(MANIFEST_FILENAME)) == []


@pytest.mark.asyncio
async def test_manifest_tampering_fails_closed(tmp_path: Path) -> None:
    request = make_request(tmp_path / "workspace")
    descriptor = _descriptor()
    cache = OfficeRenderCache(tmp_path / "cache")
    manifest = await cache.get_or_render(request, FakeProvider(descriptor))
    manifest_path = _entry_path(cache, manifest.cache_key) / MANIFEST_FILENAME
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["quality"] = "authoritative"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CacheIntegrityError, match="modified"):
        cache.load(request, descriptor)


@pytest.mark.asyncio
async def test_recomputed_digest_cannot_legitimize_noncanonical_manifest(
    tmp_path: Path,
) -> None:
    request = make_request(tmp_path / "workspace")
    descriptor = _descriptor()
    cache = OfficeRenderCache(tmp_path / "cache")
    manifest = await cache.get_or_render(request, FakeProvider(descriptor))
    entry = _entry_path(cache, manifest.cache_key)
    manifest_path = entry / MANIFEST_FILENAME
    digest_path = entry / MANIFEST_DIGEST_FILENAME
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    noncanonical = (json.dumps(payload, indent=2, sort_keys=False) + "\n").encode()
    manifest_path.write_bytes(noncanonical)
    digest_path.write_text(
        hashlib.sha256(noncanonical).hexdigest() + "\n",
        encoding="ascii",
    )

    with pytest.raises(CacheIntegrityError, match="not canonical"):
        cache.load(request, descriptor)


@pytest.mark.asyncio
async def test_artifact_tampering_fails_closed(tmp_path: Path) -> None:
    request = make_request(tmp_path / "workspace")
    descriptor = _descriptor()
    cache = OfficeRenderCache(tmp_path / "cache")
    manifest = await cache.get_or_render(request, FakeProvider(descriptor))
    page_path = _entry_path(cache, manifest.cache_key) / "page-1.png"
    page_path.write_bytes(png_bytes(red=200))

    with pytest.raises(CacheIntegrityError, match="digest or size"):
        cache.load(request, descriptor)


@pytest.mark.asyncio
async def test_retained_pdf_tampering_fails_closed(tmp_path: Path) -> None:
    request = make_request(tmp_path / "workspace")
    descriptor = _descriptor()
    cache = OfficeRenderCache(tmp_path / "cache")
    manifest = await cache.get_or_render(request, FakeProvider(descriptor))
    pdf_path = _entry_path(cache, manifest.cache_key) / manifest.pdf.filename
    pdf_path.write_bytes(pdf_bytes(pages=2))

    with pytest.raises(CacheIntegrityError, match="PDF digest or size"):
        cache.load(request, descriptor)


@pytest.mark.asyncio
async def test_recomputed_pdf_digest_cannot_legitimize_invalid_pdf(
    tmp_path: Path,
) -> None:
    request = make_request(tmp_path / "workspace")
    descriptor = _descriptor()
    cache = OfficeRenderCache(tmp_path / "cache")
    manifest = await cache.get_or_render(request, FakeProvider(descriptor))
    entry = _entry_path(cache, manifest.cache_key)
    invalid_pdf = b"%PDF-1.7\nnot-a-structural-pdf\n"
    (entry / manifest.pdf.filename).write_bytes(invalid_pdf)

    manifest_path = entry / MANIFEST_FILENAME
    digest_path = entry / MANIFEST_DIGEST_FILENAME
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["pdf"]["sha256"] = hashlib.sha256(invalid_pdf).hexdigest()
    payload["pdf"]["size_bytes"] = len(invalid_pdf)
    rewritten = RenderManifest.from_dict(payload).canonical_bytes()
    manifest_path.write_bytes(rewritten)
    digest_path.write_text(
        hashlib.sha256(rewritten).hexdigest() + "\n",
        encoding="ascii",
    )

    with pytest.raises(CacheIntegrityError, match="structurally invalid"):
        cache.load(request, descriptor)


@pytest.mark.asyncio
async def test_recomputed_png_digest_cannot_hide_pixel_tampering(
    tmp_path: Path,
) -> None:
    request = make_request(tmp_path / "workspace")
    descriptor = _descriptor()
    cache = OfficeRenderCache(tmp_path / "cache")
    manifest = await cache.get_or_render(request, FakeProvider(descriptor))
    entry = _entry_path(cache, manifest.cache_key)
    page_path = entry / manifest.pages[0].filename
    changed = png_bytes(red=200)
    page_path.write_bytes(changed)

    manifest_path = entry / MANIFEST_FILENAME
    digest_path = entry / MANIFEST_DIGEST_FILENAME
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["pages"][0]["sha256"] = hashlib.sha256(changed).hexdigest()
    payload["pages"][0]["size_bytes"] = len(changed)
    rewritten = RenderManifest.from_dict(payload).canonical_bytes()
    manifest_path.write_bytes(rewritten)
    digest_path.write_text(
        hashlib.sha256(rewritten).hexdigest() + "\n",
        encoding="ascii",
    )

    with pytest.raises(CacheIntegrityError, match="decoded pixels changed"):
        cache.load(request, descriptor)


@pytest.mark.asyncio
async def test_undeclared_provider_output_is_rejected(tmp_path: Path) -> None:
    request = make_request(tmp_path / "workspace")
    descriptor = _descriptor()
    cache = OfficeRenderCache(tmp_path / "cache")

    with pytest.raises(CacheIntegrityError, match="undeclared"):
        await cache.get_or_render(
            request,
            FakeProvider(descriptor, write_extra_file=True),
        )


def test_source_path_escape_and_symlink_are_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.docx"
    outside.write_bytes(b"outside")
    digest = hashlib.sha256(b"outside").hexdigest()
    from app.office_rendering import RenderRequest

    escaped = RenderRequest(
        workspace_root=workspace,
        source_path=outside,
        document_format="docx",
        source_sha256=digest,
        parameters_version="v1",
    )
    cache = OfficeRenderCache(tmp_path / "cache")
    with pytest.raises(PathEscapeError, match="outside"):
        cache.load(escaped, _descriptor())

    link = workspace / "linked.docx"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    redirected = RenderRequest(
        workspace_root=workspace,
        source_path=link,
        document_format="docx",
        source_sha256=digest,
        parameters_version="v1",
    )
    with pytest.raises(PathEscapeError, match="symbolic"):
        cache.load(redirected, _descriptor())


def test_cache_shard_symlink_cannot_redirect_content_address(tmp_path: Path) -> None:
    request = make_request(tmp_path / "workspace")
    descriptor = _descriptor()
    cache = OfficeRenderCache(tmp_path / "cache")
    key = build_cache_key(request, descriptor)
    outside = tmp_path / "outside-cache"
    outside.mkdir()
    shard = cache.root / "entries" / key[:2]
    try:
        shard.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are unavailable")

    with pytest.raises(PathEscapeError, match="escapes"):
        cache.load(request, descriptor)


@pytest.mark.asyncio
async def test_symlink_page_artifact_is_rejected(tmp_path: Path) -> None:
    request = make_request(tmp_path / "workspace")
    descriptor = _descriptor()
    outside = tmp_path / "outside.png"
    content = png_bytes()
    outside.write_bytes(content)

    class SymlinkProvider(FakeProvider):
        async def render(self, render_request, output_dir: Path) -> RenderManifest:
            self.calls += 1
            try:
                (output_dir / "page-1.png").symlink_to(outside)
            except OSError:
                pytest.skip("symbolic links are unavailable")
            page = PageArtifact(
                1,
                "page-1.png",
                hashlib.sha256(content).hexdigest(),
                rgba_pixel_sha256(content),
                len(content),
                2,
                2,
            )
            pdf_content = pdf_bytes()
            pdf_path = output_dir / "document.pdf"
            pdf_path.write_bytes(pdf_content)
            return RenderManifest.for_request(
                render_request,
                self.descriptor,
                (page,),
                pdf=PdfArtifact(
                    pdf_path.name,
                    hashlib.sha256(pdf_content).hexdigest(),
                    len(pdf_content),
                    1,
                ),
            )

    cache = OfficeRenderCache(tmp_path / "cache")
    with pytest.raises(CacheIntegrityError, match="regular files"):
        await cache.get_or_render(request, SymlinkProvider(descriptor))


@pytest.mark.asyncio
async def test_invalidate_uses_pinned_key_even_after_source_changes(
    tmp_path: Path,
) -> None:
    request = make_request(tmp_path / "workspace")
    descriptor = _descriptor()
    cache = OfficeRenderCache(tmp_path / "cache")
    manifest = await cache.get_or_render(request, FakeProvider(descriptor))
    request.source_path.write_bytes(b"new source version")

    assert cache.invalidate(request, descriptor) is True
    assert not _entry_path(cache, manifest.cache_key).exists()
    assert cache.invalidate(request, descriptor) is False


@pytest.mark.asyncio
async def test_install_failure_leaves_no_visible_partial_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = make_request(tmp_path / "workspace")
    descriptor = _descriptor()
    cache = OfficeRenderCache(tmp_path / "cache")

    def fail_rename(_source: Path, _destination: Path) -> None:
        raise OSError("simulated atomic rename failure")

    monkeypatch.setattr("app.office_rendering.cache.os.rename", fail_rename)
    with pytest.raises(CacheWriteError, match="install"):
        await cache.get_or_render(request, FakeProvider(descriptor))

    assert list((cache.root / "entries").rglob(MANIFEST_FILENAME)) == []
    assert list((cache.root / ".staging").iterdir()) == []


@pytest.mark.asyncio
async def test_identical_content_can_share_content_addressed_entry(
    tmp_path: Path,
) -> None:
    content = b"identical source bytes"
    first = make_request(tmp_path / "workspace-a", content=content)
    second = make_request(tmp_path / "workspace-b", content=content)
    descriptor = _descriptor()
    provider = FakeProvider(descriptor)
    cache = OfficeRenderCache(tmp_path / "cache")

    first_manifest = await cache.get_or_render(first, provider)
    second_manifest = await cache.get_or_render(second, provider)

    assert second_manifest.cache_key == first_manifest.cache_key
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_concurrent_same_key_publish_keeps_one_complete_entry(
    tmp_path: Path,
) -> None:
    request = make_request(tmp_path / "workspace")
    descriptor = _descriptor()
    cache = OfficeRenderCache(tmp_path / "cache")

    class RacingProvider(FakeProvider):
        async def render(self, render_request, output_dir: Path) -> RenderManifest:
            await asyncio.sleep(0)
            return await super().render(render_request, output_dir)

    first_provider = RacingProvider(descriptor)
    second_provider = RacingProvider(descriptor)
    first, second = await asyncio.gather(
        cache.get_or_render(request, first_provider),
        cache.get_or_render(request, second_provider),
    )

    assert first == second
    assert first_provider.calls + second_provider.calls == 2
    entry = _entry_path(cache, first.cache_key)
    assert {item.name for item in entry.iterdir()} == {
        "document.pdf",
        "page-1.png",
        MANIFEST_FILENAME,
        MANIFEST_DIGEST_FILENAME,
    }
