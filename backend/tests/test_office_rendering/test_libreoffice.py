from __future__ import annotations

from dataclasses import dataclass
import hashlib
from io import BytesIO
from pathlib import Path
import stat
from types import MappingProxyType
from typing import Any

from PIL import Image
import pytest
from pypdf import PdfWriter

from app.office_rendering import (
    LIBREOFFICE_PARAMETERS_VERSION,
    CacheIntegrityError,
    LibreOfficeRenderLimits,
    LibreOfficeRenderProvider,
    NativeSandboxContract,
    OfficeRenderCache,
    ProviderUnavailableError,
    RenderContractError,
    RenderProcessError,
    RenderProcessResult,
    RenderTimeoutError,
    discover_libreoffice_toolchain,
)
from app.office_rendering.libreoffice import _validate_png
from tests.test_office_rendering.helpers import (
    make_request,
    png_bytes,
    rgba_pixel_sha256,
)


@dataclass(frozen=True, slots=True)
class RecordedCall:
    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    timeout_seconds: float
    fontconfig_bytes: bytes | None
    staged_source_bytes: bytes | None
    staged_source_mode: int | None


class FakePipelineRunner:
    def __init__(
        self,
        *,
        pages: int = 2,
        pdf_page_size: tuple[float, float] = (72, 72),
        png_size: tuple[int, int] = (2, 2),
        invalid_pdf: bool = False,
        invalid_png: bool = False,
        extra_pdf_artifact: bool = False,
        extra_raster_artifact: bool = False,
        failing_stage: str | None = None,
        timeout_stage: str | None = None,
        stage_callback: Any = None,
    ) -> None:
        self.pages = pages
        self.pdf_page_size = pdf_page_size
        self.png_size = png_size
        self.invalid_pdf = invalid_pdf
        self.invalid_png = invalid_png
        self.extra_pdf_artifact = extra_pdf_artifact
        self.extra_raster_artifact = extra_raster_artifact
        self.failing_stage = failing_stage
        self.timeout_stage = timeout_stage
        self.stage_callback = stage_callback
        self.calls: list[RecordedCall] = []

    async def run(
        self,
        argv,
        *,
        cwd: Path,
        env,
        timeout_seconds: float,
    ) -> RenderProcessResult:
        args = tuple(argv)
        stage = "soffice" if "--convert-to" in args else "pdftoppm"
        fontconfig = env.get("FONTCONFIG_FILE")
        staged_source = Path(args[-1]) if stage == "soffice" else None
        self.calls.append(
            RecordedCall(
                argv=args,
                cwd=Path(cwd),
                env=dict(env),
                timeout_seconds=timeout_seconds,
                fontconfig_bytes=(
                    Path(fontconfig).read_bytes() if fontconfig is not None else None
                ),
                staged_source_bytes=(
                    staged_source.read_bytes() if staged_source is not None else None
                ),
                staged_source_mode=(
                    stat.S_IMODE(staged_source.stat().st_mode)
                    if staged_source is not None
                    else None
                ),
            )
        )
        if self.stage_callback is not None:
            self.stage_callback(stage)
        if self.timeout_stage == stage:
            raise RenderTimeoutError(f"fake {stage} timeout")
        if self.failing_stage == stage:
            return RenderProcessResult(9, b"bounded stdout", b"bounded stderr")
        if stage == "soffice":
            output_dir = Path(args[args.index("--outdir") + 1])
            pdf_path = output_dir / "converted.pdf"
            if self.invalid_pdf:
                pdf_path.write_bytes(b"not a pdf")
            else:
                writer = PdfWriter()
                for _ in range(self.pages):
                    writer.add_blank_page(
                        width=self.pdf_page_size[0],
                        height=self.pdf_page_size[1],
                    )
                with pdf_path.open("wb") as handle:
                    writer.write(handle)
            if self.extra_pdf_artifact:
                (output_dir / "unexpected.tmp").write_bytes(b"extra")
        else:
            prefix = Path(args[-1])
            for page_number in range(1, self.pages + 1):
                page_path = prefix.parent / f"{prefix.name}-{page_number}.png"
                if self.invalid_png:
                    page_path.write_bytes(b"not a png")
                else:
                    page_path.write_bytes(
                        png_bytes(
                            width=self.png_size[0],
                            height=self.png_size[1],
                            red=page_number,
                        )
                    )
            if self.extra_raster_artifact:
                (prefix.parent / "debug.log").write_bytes(b"extra")
        return RenderProcessResult(0, b"", b"")


def _fake_executable(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o700)
    return path


def _toolchain(tmp_path: Path):
    soffice = _fake_executable(tmp_path / "tools" / "soffice", b"fake soffice v1")
    pdftoppm = _fake_executable(
        tmp_path / "tools" / "pdftoppm",
        b"fake pdftoppm v1",
    )
    return discover_libreoffice_toolchain(
        soffice_path=soffice,
        pdftoppm_path=pdftoppm,
        platform_name="linux",
        environ={},
    )


def _bundled_toolchain(tmp_path: Path):
    root = tmp_path / "private-renderer"
    soffice = _fake_executable(root / "bin" / "soffice", b"bundled soffice")
    pdftoppm = _fake_executable(
        root / "bin" / "pdftoppm",
        b"bundled pdftoppm",
    )
    font = root / "fonts" / "SuxiaoyouCJK-Regular.ttf"
    font.parent.mkdir(parents=True)
    font.write_bytes(b"bundled font")
    return (
        discover_libreoffice_toolchain(
            soffice_path=soffice,
            pdftoppm_path=pdftoppm,
            platform_name="linux",
            environ={},
        ),
        root.resolve(),
    )
def _request(tmp_path: Path, **parameters: object):
    return make_request(
        tmp_path / "workspace",
        parameters_version=LIBREOFFICE_PARAMETERS_VERSION,
        parameters={"dpi": 144, **parameters},
    )


def test_pixel_digest_is_canonical_rgba_not_encoded_png_bytes(tmp_path: Path) -> None:
    image = Image.new("RGBA", (3, 2), (12, 34, 56, 78))
    encoded: list[bytes] = []
    for compression in (0, 9):
        output = BytesIO()
        image.save(output, format="PNG", compress_level=compression)
        encoded.append(output.getvalue())
    assert encoded[0] != encoded[1]

    results = []
    for index, content in enumerate(encoded):
        path = tmp_path / f"encoded-{index}.png"
        path.write_bytes(content)
        results.append(_validate_png(path, len(content)))

    assert results[0][0] != results[1][0]
    assert results[0][1] == results[1][1]


@pytest.mark.asyncio
async def test_libreoffice_pipeline_is_approximate_private_and_no_shell(
    tmp_path: Path,
) -> None:
    runner = FakePipelineRunner(pages=2)
    provider = LibreOfficeRenderProvider(
        font_digest="a" * 64,
        toolchain=_toolchain(tmp_path),
        runner=runner,
        platform_name="linux",
        environ={"PATH": "/untrusted", "SECRET_TOKEN": "must-not-leak"},
    )
    request = _request(tmp_path)
    output = tmp_path / "output"
    output.mkdir()

    manifest = await provider.render(request, output)

    assert provider.availability().available is True
    assert provider.descriptor.quality == "approximate"
    assert ".fonts-host" in provider.descriptor.renderer_version
    assert manifest.quality == "approximate"
    assert len(manifest.pages) == 2
    assert manifest.pdf.filename == "document.pdf"
    assert manifest.pdf.page_count == 2
    assert {path.name for path in output.iterdir()} == {
        "document.pdf",
        "page-1.png",
        "page-2.png",
    }
    assert manifest.pdf.sha256 == hashlib.sha256(
        (output / "document.pdf").read_bytes()
    ).hexdigest()
    for page in manifest.pages:
        assert page.pixel_sha256 == rgba_pixel_sha256(
            (output / page.filename).read_bytes()
        )
    assert len(runner.calls) == 2
    soffice_call, raster_call = runner.calls
    assert Path(soffice_call.argv[0]).is_absolute()
    assert Path(raster_call.argv[0]).is_absolute()
    assert "--headless" in soffice_call.argv
    assert "--convert-to" in soffice_call.argv
    assert raster_call.argv[1:4] == ("-png", "-r", "144")
    assert all("shell" not in argument.casefold() for argument in soffice_call.argv)
    assert soffice_call.cwd == raster_call.cwd
    assert soffice_call.cwd.name.startswith(".libreoffice-")
    assert not soffice_call.cwd.exists()
    assert soffice_call.env == raster_call.env
    assert "SECRET_TOKEN" not in soffice_call.env
    assert "/untrusted" not in soffice_call.env["PATH"]
    assert soffice_call.staged_source_bytes == request.source_path.read_bytes()
    assert soffice_call.staged_source_mode is not None
    assert soffice_call.staged_source_mode & 0o222 == 0
    staged_source = Path(soffice_call.argv[-1])
    assert staged_source.parent.name == "input"
    assert staged_source.is_relative_to(soffice_call.cwd)
    assert set(soffice_call.env) == {
        "HOME",
        "USERPROFILE",
        "TMPDIR",
        "TMP",
        "TEMP",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
        "SAL_USE_VCLPLUGIN",
        "SAL_DISABLE_OPENCL",
        "SAL_DISABLEGL",
        "LANG",
        "LC_ALL",
        "PATH",
    }
    profile_arguments = [
        item for item in soffice_call.argv if item.startswith("-env:UserInstallation=")
    ]
    assert len(profile_arguments) == 1
    assert profile_arguments[0].startswith("-env:UserInstallation=file://")


@pytest.mark.asyncio
async def test_bundled_renderer_uses_only_private_fontconfig_and_no_host_proxy(
    tmp_path: Path,
) -> None:
    toolchain, bundle_root = _bundled_toolchain(tmp_path)
    runner = FakePipelineRunner(pages=1)
    provider = LibreOfficeRenderProvider(
        font_digest="7" * 64,
        toolchain=toolchain,
        runner=runner,
        platform_name="linux",
        environ={
            "HOME": "/host/home",
            "FONTCONFIG_FILE": "/etc/fonts/fonts.conf",
            "FONTCONFIG_PATH": "/etc/fonts",
            "HTTP_PROXY": "http://host-proxy",
            "HTTPS_PROXY": "http://host-proxy",
            "ALL_PROXY": "socks5://host-proxy",
        },
    )
    output = tmp_path / "output"
    output.mkdir()

    await provider.render(_request(tmp_path), output)

    assert provider.sandbox is not None
    assert provider.sandbox.bundle_root == bundle_root
    assert ".fonts-private" in provider.descriptor.renderer_version
    first, second = runner.calls
    assert first.env == second.env
    assert first.fontconfig_bytes == second.fontconfig_bytes
    assert first.fontconfig_bytes is not None
    assert str(bundle_root / "fonts").encode() in first.fontconfig_bytes
    assert b"/etc/fonts" not in first.fontconfig_bytes
    assert b"/usr/share/fonts" not in first.fontconfig_bytes
    assert first.env["FONTCONFIG_FILE"].startswith(str(first.cwd))
    assert first.env["FONTCONFIG_PATH"].startswith(str(first.cwd))
    assert first.env["HOME"].startswith(str(first.cwd))
    assert first.env["XDG_CACHE_HOME"].startswith(str(first.cwd))
    assert not any("proxy" in key.casefold() for key in first.env)
    assert "/host/home" not in first.env.values()


@pytest.mark.asyncio
async def test_attested_native_sandbox_wraps_both_stages_without_a_shell(
    tmp_path: Path,
) -> None:
    toolchain, bundle_root = _bundled_toolchain(tmp_path)
    assert toolchain.soffice is not None
    assert toolchain.pdftoppm is not None
    launcher = _fake_executable(
        bundle_root / "bin" / "suxiaoyou-office-sandbox-launcher",
        b"native sandbox launcher",
    )
    native_executables = {
        path: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size)
        for path in (launcher, toolchain.soffice.path, toolchain.pdftoppm.path)
    }
    contract = NativeSandboxContract(
        platform_target="linux-x64",
        contract_id=(
            "suxiaoyou.office-sandbox.linux-namespaces-seccomp-cgroup.v1"
        ),
        capabilities=frozenset({"network_denied"}),
        sandbox_manifest_sha256="1" * 64,
        dependency_manifest_sha256="2" * 64,
        bundle_tree_sha256="3" * 64,
        launcher_sha256=native_executables[launcher][0],
        _root=bundle_root,
        _launcher_path=launcher,
        _native_executables=MappingProxyType(native_executables),
    )
    runner = FakePipelineRunner(pages=1)
    provider = LibreOfficeRenderProvider(
        font_digest="8" * 64,
        toolchain=toolchain,
        runner=runner,
        native_sandbox_contract=contract,
        platform_name="linux",
        environ={},
    )
    output = tmp_path / "output"
    output.mkdir()

    await provider.render(_request(tmp_path), output)

    assert len(runner.calls) == 2
    for call, expected_inner in zip(
        runner.calls,
        (toolchain.soffice.path, toolchain.pdftoppm.path),
        strict=True,
    ):
        assert call.argv[0] == str(launcher)
        assert call.argv[1:3] == ("--contract-id", contract.contract_id)
        separator = call.argv.index("--")
        assert call.argv[separator + 1] == str(expected_inner)
        assert "shell" not in call.argv


@pytest.mark.asyncio
async def test_fontconfig_tamper_between_stages_fails_closed(tmp_path: Path) -> None:
    toolchain, _bundle_root = _bundled_toolchain(tmp_path)
    runner: FakePipelineRunner

    def tamper(stage: str) -> None:
        if stage == "soffice":
            fontconfig = Path(runner.calls[-1].env["FONTCONFIG_FILE"])
            fontconfig.write_text(
                '<fontconfig><dir>/host/fonts</dir></fontconfig>',
                encoding="utf-8",
            )

    runner = FakePipelineRunner(pages=1, stage_callback=tamper)
    provider = LibreOfficeRenderProvider(
        font_digest="6" * 64,
        toolchain=toolchain,
        runner=runner,
        platform_name="linux",
        environ={},
    )
    output = tmp_path / "output"
    output.mkdir()

    with pytest.raises(RenderContractError, match="Fontconfig changed"):
        await provider.render(_request(tmp_path), output)
    assert len(runner.calls) == 1
    assert list(output.iterdir()) == []


def test_bundled_font_symlink_cannot_fall_back_to_host_fonts(
    tmp_path: Path,
) -> None:
    toolchain, bundle_root = _bundled_toolchain(tmp_path)
    bundled = bundle_root / "fonts" / "SuxiaoyouCJK-Regular.ttf"
    host = tmp_path / "host-font.ttf"
    host.write_bytes(b"host font")
    bundled.unlink()
    try:
        bundled.symlink_to(host)
    except OSError:
        pytest.skip("symbolic links are unavailable")

    with pytest.raises(RenderContractError, match="font file is invalid"):
        LibreOfficeRenderProvider(
            font_digest="5" * 64,
            toolchain=toolchain,
            runner=FakePipelineRunner(),
            platform_name="linux",
            environ={},
        )


def test_bundled_font_content_drift_closes_provider(tmp_path: Path) -> None:
    toolchain, bundle_root = _bundled_toolchain(tmp_path)
    provider = LibreOfficeRenderProvider(
        font_digest="4" * 64,
        toolchain=toolchain,
        runner=FakePipelineRunner(),
        platform_name="linux",
        environ={},
    )
    assert provider.availability().available

    (bundle_root / "fonts" / "SuxiaoyouCJK-Regular.ttf").write_bytes(
        b"changed bundled font"
    )

    assert not provider.availability().available
    assert "sandbox changed" in (provider.availability().reason or "").casefold()


@pytest.mark.asyncio
async def test_concrete_provider_composes_with_content_cache(tmp_path: Path) -> None:
    runner = FakePipelineRunner(pages=1)
    provider = LibreOfficeRenderProvider(
        font_digest="b" * 64,
        toolchain=_toolchain(tmp_path),
        runner=runner,
    )
    request = _request(tmp_path)
    cache = OfficeRenderCache(tmp_path / "cache")

    first = await cache.get_or_render(request, provider)
    second = await cache.get_or_render(request, provider)

    assert first == second
    assert first.quality == "approximate"
    assert cache.pdf_path(request, provider.descriptor) is not None
    assert len(runner.calls) == 2


@pytest.mark.asyncio
async def test_each_render_uses_a_distinct_disposable_profile(tmp_path: Path) -> None:
    runner = FakePipelineRunner(pages=1)
    provider = LibreOfficeRenderProvider(
        font_digest="9" * 64,
        toolchain=_toolchain(tmp_path),
        runner=runner,
    )
    request = _request(tmp_path)
    first_output = tmp_path / "first-output"
    second_output = tmp_path / "second-output"
    first_output.mkdir()
    second_output.mkdir()

    await provider.render(request, first_output)
    await provider.render(request, second_output)

    profile_arguments = [
        next(
            item
            for item in call.argv
            if item.startswith("-env:UserInstallation=")
        )
        for call in (runner.calls[0], runner.calls[2])
    ]
    assert profile_arguments[0] != profile_arguments[1]
    assert not runner.calls[0].cwd.exists()
    assert not runner.calls[2].cwd.exists()


@pytest.mark.asyncio
async def test_missing_or_changed_tools_are_explicitly_unavailable(
    tmp_path: Path,
) -> None:
    missing = discover_libreoffice_toolchain(
        soffice_path=tmp_path / "missing-soffice",
        pdftoppm_path=tmp_path / "missing-pdftoppm",
        platform_name="linux",
        environ={},
    )
    provider = LibreOfficeRenderProvider(
        font_digest="c" * 64,
        toolchain=missing,
        runner=FakePipelineRunner(),
    )
    assert provider.descriptor.quality == "approximate"
    assert provider.availability().available is False
    assert "LibreOffice" in (provider.availability().reason or "")
    output = tmp_path / "output"
    output.mkdir()
    with pytest.raises(ProviderUnavailableError, match="Missing"):
        await provider.render(_request(tmp_path), output)

    available_tools = _toolchain(tmp_path / "changed")
    changed_provider = LibreOfficeRenderProvider(
        font_digest="d" * 64,
        toolchain=available_tools,
        runner=FakePipelineRunner(),
    )
    assert available_tools.soffice is not None
    available_tools.soffice.path.write_bytes(b"replacement binary")
    assert changed_provider.availability().available is False
    assert "changed" in (changed_provider.availability().reason or "")


def test_discovery_covers_path_macos_and_windows_locations(tmp_path: Path) -> None:
    unix_soffice = _fake_executable(tmp_path / "path" / "soffice", b"lo")
    unix_pdftoppm = _fake_executable(tmp_path / "path" / "pdftoppm", b"poppler")
    which_calls: list[str] = []

    def fake_which(command: str) -> str | None:
        which_calls.append(command)
        return {
            "soffice": str(unix_soffice),
            "pdftoppm": str(unix_pdftoppm),
        }.get(command)

    mac = discover_libreoffice_toolchain(
        platform_name="darwin",
        environ={},
        which=fake_which,
    )
    assert mac.available is True
    assert {"libreoffice", "soffice", "pdftoppm"}.issubset(which_calls)

    program_files = tmp_path / "Program Files"
    poppler_bin = tmp_path / "Poppler" / "bin"
    windows_soffice = _fake_executable(
        program_files / "LibreOffice" / "program" / "soffice.exe",
        b"windows lo",
    )
    windows_pdftoppm = _fake_executable(
        poppler_bin / "pdftoppm.exe",
        b"windows poppler",
    )
    windows = discover_libreoffice_toolchain(
        platform_name="win32",
        environ={
            "PROGRAMFILES": str(program_files),
            "POPPLER_BIN": str(poppler_bin),
        },
        which=lambda _command: None,
    )
    assert windows.soffice is not None
    assert windows.pdftoppm is not None
    assert windows.soffice.path == windows_soffice.resolve()
    assert windows.pdftoppm.path == windows_pdftoppm.resolve()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runner", "error_match"),
    [
        (FakePipelineRunner(invalid_pdf=True), "not a PDF"),
        (FakePipelineRunner(invalid_png=True), "valid PNG"),
        (FakePipelineRunner(extra_pdf_artifact=True), "exactly one PDF"),
        (FakePipelineRunner(extra_raster_artifact=True), "undeclared"),
    ],
)
async def test_intermediate_magic_and_artifact_whitelists_fail_closed(
    tmp_path: Path,
    runner: FakePipelineRunner,
    error_match: str,
) -> None:
    provider = LibreOfficeRenderProvider(
        font_digest="e" * 64,
        toolchain=_toolchain(tmp_path),
        runner=runner,
    )
    output = tmp_path / "output"
    output.mkdir()

    with pytest.raises(CacheIntegrityError, match=error_match):
        await provider.render(_request(tmp_path), output)
    assert list(output.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runner", "limits", "error_match"),
    [
        (
            FakePipelineRunner(pages=3),
            LibreOfficeRenderLimits(max_pages=2),
            "page count",
        ),
        (
            FakePipelineRunner(pages=1),
            LibreOfficeRenderLimits(max_pdf_bytes=10),
            "PDF exceeds",
        ),
        (
            FakePipelineRunner(pages=1),
            LibreOfficeRenderLimits(max_output_bytes=10),
            "byte budget",
        ),
        (
            FakePipelineRunner(
                pages=1,
                pdf_page_size=(1, 1),
                png_size=(20, 20),
            ),
            LibreOfficeRenderLimits(max_page_pixels=100),
            "pixel budget",
        ),
    ],
)
async def test_page_pixel_and_byte_limits_fail_closed(
    tmp_path: Path,
    runner: FakePipelineRunner,
    limits: LibreOfficeRenderLimits,
    error_match: str,
) -> None:
    provider = LibreOfficeRenderProvider(
        font_digest="f" * 64,
        toolchain=_toolchain(tmp_path),
        runner=runner,
        limits=limits,
    )
    output = tmp_path / "output"
    output.mkdir()

    with pytest.raises(CacheIntegrityError, match=error_match):
        await provider.render(_request(tmp_path), output)
    assert list(output.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runner", "expected_error"),
    [
        (FakePipelineRunner(timeout_stage="soffice"), RenderTimeoutError),
        (FakePipelineRunner(failing_stage="pdftoppm"), RenderProcessError),
    ],
)
async def test_timeout_and_process_failure_leave_no_staging_artifacts(
    tmp_path: Path,
    runner: FakePipelineRunner,
    expected_error: type[Exception],
) -> None:
    provider = LibreOfficeRenderProvider(
        font_digest="1" * 64,
        toolchain=_toolchain(tmp_path),
        runner=runner,
    )
    output = tmp_path / "output"
    output.mkdir()

    with pytest.raises(expected_error):
        await provider.render(_request(tmp_path), output)
    assert list(output.iterdir()) == []


@pytest.mark.asyncio
async def test_total_deadline_prevents_starting_a_late_second_stage(
    tmp_path: Path,
) -> None:
    clock_values = iter((0.0, 1.0, 20.0))
    runner = FakePipelineRunner()
    provider = LibreOfficeRenderProvider(
        font_digest="8" * 64,
        toolchain=_toolchain(tmp_path),
        runner=runner,
        limits=LibreOfficeRenderLimits(timeout_seconds=10),
        clock=lambda: next(clock_values),
    )
    output = tmp_path / "output"
    output.mkdir()

    with pytest.raises(RenderTimeoutError, match="total render timeout"):
        await provider.render(_request(tmp_path), output)
    assert len(runner.calls) == 1
    assert list(output.iterdir()) == []


@pytest.mark.asyncio
async def test_source_mutation_and_invalid_parameters_fail_closed(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)

    def mutate_after_conversion(stage: str) -> None:
        if stage == "pdftoppm":
            request.source_path.write_bytes(b"changed while rendering")

    runner = FakePipelineRunner(stage_callback=mutate_after_conversion)
    provider = LibreOfficeRenderProvider(
        font_digest="2" * 64,
        toolchain=_toolchain(tmp_path),
        runner=runner,
    )
    output = tmp_path / "output"
    output.mkdir()
    from app.office_rendering import StaleSourceError

    with pytest.raises(StaleSourceError, match="SHA-256"):
        await provider.render(request, output)
    assert list(output.iterdir()) == []

    unsupported = make_request(
        tmp_path / "other-workspace",
        parameters_version="unknown-version",
        parameters={"dpi": 144},
    )
    with pytest.raises(RenderContractError, match="parameters version"):
        await provider.render(unsupported, output)
    unknown_parameter = make_request(
        tmp_path / "third-workspace",
        parameters_version=LIBREOFFICE_PARAMETERS_VERSION,
        parameters={"dpi": 144, "network": True},
    )
    with pytest.raises(RenderContractError, match="unknown keys"):
        await provider.render(unknown_parameter, output)

    occupied = tmp_path / "occupied-output"
    occupied.mkdir()
    sentinel = occupied / "keep.txt"
    sentinel.write_text("do not overwrite", encoding="utf-8")
    with pytest.raises(CacheIntegrityError, match="empty staging"):
        await provider.render(_request(tmp_path / "occupied-request"), occupied)
    assert sentinel.read_text(encoding="utf-8") == "do not overwrite"


@pytest.mark.asyncio
async def test_source_and_output_symlink_boundaries_fail_before_process_launch(
    tmp_path: Path,
) -> None:
    runner = FakePipelineRunner()
    provider = LibreOfficeRenderProvider(
        font_digest="0" * 64,
        toolchain=_toolchain(tmp_path),
        runner=runner,
    )
    request = _request(tmp_path)
    outside = tmp_path / "outside.docx"
    outside.write_bytes(request.source_path.read_bytes())
    request.source_path.unlink()
    try:
        request.source_path.symlink_to(outside)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    output = tmp_path / "output"
    output.mkdir()

    from app.office_rendering import PathEscapeError

    with pytest.raises(PathEscapeError, match="symbolic link"):
        await provider.render(request, output)
    assert runner.calls == []

    request = _request(tmp_path / "next")
    real_output = tmp_path / "real-output"
    real_output.mkdir()
    redirected_output = tmp_path / "redirected-output"
    redirected_output.symlink_to(real_output, target_is_directory=True)
    with pytest.raises(PathEscapeError, match="redirected"):
        await provider.render(request, redirected_output)
    assert runner.calls == []


def test_font_digest_is_mandatory_and_part_of_descriptor(tmp_path: Path) -> None:
    tools = _toolchain(tmp_path)
    with pytest.raises(RenderContractError, match="font_digest"):
        LibreOfficeRenderProvider(font_digest="not-a-digest", toolchain=tools)

    first = LibreOfficeRenderProvider(font_digest="3" * 64, toolchain=tools)
    second = LibreOfficeRenderProvider(font_digest="4" * 64, toolchain=tools)
    assert first.descriptor.font_digest != second.descriptor.font_digest
    assert first.descriptor.quality == second.descriptor.quality == "approximate"
