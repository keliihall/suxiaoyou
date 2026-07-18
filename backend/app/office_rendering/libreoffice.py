"""Optional LibreOffice -> PDF -> PNG renderer provider.

This provider remains ``approximate``: LibreOffice/Poppler output is useful for
visual review but is not evidence of Microsoft Office pixel equivalence.  No
binary is bundled or auto-enabled here.  Missing or changed executables produce
an explicit unavailable status.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import stat
import struct
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader

from app.office_rendering.errors import (
    CacheIntegrityError,
    PathEscapeError,
    ProviderUnavailableError,
    RenderContractError,
    RenderProcessError,
    RenderTimeoutError,
    StaleSourceError,
)
from app.office_rendering.models import (
    APPROXIMATE_QUALITY,
    PageArtifact,
    PdfArtifact,
    RenderManifest,
    RendererDescriptor,
    RenderRequest,
    validate_sha256,
)
from app.office_rendering.native_sandbox import (
    NativeSandboxContract,
    NativeSandboxContractError,
)
from app.office_rendering.process_runner import (
    LocalProcessTreeRunner,
    RenderProcessResult,
    RenderProcessRunner,
)
from app.office_rendering.provider import ProviderAvailability
from app.office_rendering.sandbox import (
    BundledOfficeRendererSandbox,
    OfficeRendererSandboxInvocation,
    discover_bundled_office_renderer_sandbox,
)


LIBREOFFICE_PARAMETERS_VERSION: Final = "libreoffice-pdf-png-v1"
LIBREOFFICE_RENDERER_ID: Final = "libreoffice-pdf-png"
LIBREOFFICE_PIPELINE_VERSION: Final = "pipeline-3"
DEFAULT_DPI: Final = 144
MIN_DPI: Final = 72
MAX_DPI: Final = 300
MAX_EXECUTABLE_BYTES: Final = 512 * 1024 * 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PDF_MAGIC = b"%PDF-"
_PDF_ARTIFACT_FILENAME = "document.pdf"
_PAGE_NAME = re.compile(r"^page-(\d+)\.png$")


@dataclass(frozen=True, slots=True)
class ExecutableIdentity:
    """Resolved executable path pinned to its content SHA-256."""

    path: Path
    sha256: str

    def __post_init__(self) -> None:
        path = Path(self.path)
        if not path.is_absolute():
            raise RenderContractError("renderer executable path must be absolute")
        object.__setattr__(self, "path", path)
        object.__setattr__(
            self,
            "sha256",
            validate_sha256(self.sha256, "renderer executable sha256"),
        )


@dataclass(frozen=True, slots=True)
class LibreOfficeToolchain:
    """Discovered binaries; either field may be absent without pretending ready."""

    soffice: ExecutableIdentity | None
    pdftoppm: ExecutableIdentity | None

    def __post_init__(self) -> None:
        if self.soffice is not None and not isinstance(
            self.soffice, ExecutableIdentity
        ):
            raise RenderContractError("soffice must be an ExecutableIdentity")
        if self.pdftoppm is not None and not isinstance(
            self.pdftoppm, ExecutableIdentity
        ):
            raise RenderContractError("pdftoppm must be an ExecutableIdentity")

    @property
    def available(self) -> bool:
        return self.soffice is not None and self.pdftoppm is not None

    @property
    def unavailable_reason(self) -> str | None:
        missing: list[str] = []
        if self.soffice is None:
            missing.append("LibreOffice executable")
        if self.pdftoppm is None:
            missing.append("pdftoppm executable")
        if not missing:
            return None
        return "Missing local render tool: " + ", ".join(missing)


@dataclass(frozen=True, slots=True)
class LibreOfficeRenderLimits:
    """Hard resource ceilings applied before a manifest can be returned."""

    timeout_seconds: float = 120.0
    max_source_bytes: int = 512 * 1024 * 1024
    max_pdf_bytes: int = 256 * 1024 * 1024
    max_output_bytes: int = 512 * 1024 * 1024
    max_pages: int = 200
    max_page_pixels: int = 50_000_000

    def __post_init__(self) -> None:
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(float(self.timeout_seconds))
            or self.timeout_seconds <= 0
        ):
            raise RenderContractError("LibreOffice timeout must be positive and finite")
        for name in (
            "max_source_bytes",
            "max_pdf_bytes",
            "max_output_bytes",
            "max_pages",
            "max_page_pixels",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise RenderContractError(f"LibreOffice {name} must be positive")


WhichFunction = Callable[[str], str | None]


def discover_libreoffice_toolchain(
    *,
    soffice_path: str | Path | None = None,
    pdftoppm_path: str | Path | None = None,
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
    which: WhichFunction = shutil.which,
) -> LibreOfficeToolchain:
    """Discover and content-pin the two local executables without running them."""

    selected_platform = (platform_name or sys.platform).lower()
    environment = dict(os.environ if environ is None else environ)
    soffice = _discover_executable(
        explicit=soffice_path,
        candidates=_soffice_candidates(selected_platform, environment, which),
        windows=selected_platform.startswith("win"),
    )
    pdftoppm = _discover_executable(
        explicit=pdftoppm_path,
        candidates=_pdftoppm_candidates(selected_platform, environment, which),
        windows=selected_platform.startswith("win"),
    )
    return LibreOfficeToolchain(soffice=soffice, pdftoppm=pdftoppm)


class LibreOfficeRenderProvider:
    """Optional local converter with fail-closed staging and approximate quality."""

    def __init__(
        self,
        *,
        font_digest: str,
        toolchain: LibreOfficeToolchain | None = None,
        soffice_path: str | Path | None = None,
        pdftoppm_path: str | Path | None = None,
        runner: RenderProcessRunner | None = None,
        limits: LibreOfficeRenderLimits | None = None,
        sandbox: BundledOfficeRendererSandbox | None = None,
        native_sandbox_contract: NativeSandboxContract | None = None,
        platform_name: str | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        pinned_font_digest = validate_sha256(font_digest, "font_digest")
        if toolchain is not None and (
            soffice_path is not None or pdftoppm_path is not None
        ):
            raise RenderContractError(
                "supply either a toolchain or executable paths, not both"
            )
        selected_platform = (platform_name or sys.platform).lower()
        self.toolchain = toolchain or discover_libreoffice_toolchain(
            soffice_path=soffice_path,
            pdftoppm_path=pdftoppm_path,
            platform_name=selected_platform,
            environ=environ,
        )
        if not isinstance(self.toolchain, LibreOfficeToolchain):
            raise RenderContractError("toolchain must be a LibreOfficeToolchain")
        if sandbox is not None and not isinstance(
            sandbox,
            BundledOfficeRendererSandbox,
        ):
            raise RenderContractError("renderer sandbox is invalid")
        executable_paths = tuple(
            identity.path
            for identity in (self.toolchain.soffice, self.toolchain.pdftoppm)
            if identity is not None
        )
        discovered_sandbox = (
            discover_bundled_office_renderer_sandbox(executable_paths)
            if len(executable_paths) == 2
            else None
        )
        self.sandbox = sandbox or discovered_sandbox
        if self.sandbox is not None:
            if set(self.sandbox.executable_paths) != set(executable_paths):
                raise RenderContractError(
                    "renderer sandbox does not bind the selected toolchain"
                )
            self.sandbox.validate()
        if native_sandbox_contract is not None and not isinstance(
            native_sandbox_contract,
            NativeSandboxContract,
        ):
            raise RenderContractError("native renderer sandbox contract is invalid")
        if native_sandbox_contract is not None:
            expected_family = (
                "windows"
                if selected_platform.startswith("win")
                else selected_platform.split("-", 1)[0]
            )
            if (
                self.sandbox is None
                or native_sandbox_contract.platform_target.split("-", 1)[0]
                != expected_family
            ):
                raise RenderContractError(
                    "native renderer sandbox does not bind the selected toolchain"
                )
        self.native_sandbox_contract = native_sandbox_contract
        self.runner = runner or LocalProcessTreeRunner()
        if not isinstance(self.runner, RenderProcessRunner):
            raise RenderContractError("runner must implement RenderProcessRunner")
        self.limits = limits or LibreOfficeRenderLimits()
        if not isinstance(self.limits, LibreOfficeRenderLimits):
            raise RenderContractError("limits must be LibreOfficeRenderLimits")
        self.platform_name = selected_platform
        self._host_environment = _safe_host_environment(
            os.environ if environ is None else environ,
            windows=selected_platform.startswith("win"),
        )
        self._clock = clock
        self._descriptor = RendererDescriptor(
            renderer_id=LIBREOFFICE_RENDERER_ID,
            renderer_version=_renderer_version(
                self.toolchain,
                bundled_fonts=self.sandbox is not None,
            ),
            font_digest=pinned_font_digest,
            quality=APPROXIMATE_QUALITY,
        )

    @property
    def descriptor(self) -> RendererDescriptor:
        return self._descriptor

    def availability(self) -> ProviderAvailability:
        reason = self.toolchain.unavailable_reason
        if reason is None:
            changed = _changed_executable(self.toolchain)
            if changed is not None:
                reason = f"Local render tool changed after discovery: {changed}"
        if reason is None and self.sandbox is not None:
            try:
                self.sandbox.validate()
            except Exception:
                reason = "Bundled renderer sandbox changed after discovery"
        return ProviderAvailability(available=reason is None, reason=reason)

    async def render(
        self,
        request: RenderRequest,
        output_dir: Path,
    ) -> RenderManifest:
        availability = self.availability()
        if not availability.available:
            raise ProviderUnavailableError(
                availability.reason or "LibreOffice render provider is unavailable"
            )
        if request.parameters_version != LIBREOFFICE_PARAMETERS_VERSION:
            raise RenderContractError(
                "LibreOffice render request uses an unsupported parameters version"
            )
        dpi = _render_dpi(request.parameters_dict())
        self._validate_source(request)
        destination = _empty_staging_directory(output_dir)
        work_dir = Path(
            tempfile.mkdtemp(prefix=".libreoffice-", dir=destination)
        ).resolve(strict=True)
        _assert_within(destination, work_dir)
        _harden_directory(work_dir)
        installed: list[Path] = []

        try:
            pdf, pages = await self._render_pipeline(
                request,
                destination=destination,
                work_dir=work_dir,
                dpi=dpi,
                installed=installed,
            )
        except BaseException:
            _remove_files(installed)
            _remove_work_directory(work_dir, ignore_errors=True)
            raise

        try:
            _remove_work_directory(work_dir, ignore_errors=False)
        except OSError as exc:
            _remove_files(installed)
            raise RenderProcessError(
                "LibreOffice private profile could not be removed"
            ) from exc
        try:
            _validate_final_staging(destination, pdf, pages)
            self._validate_source(request)
            if _changed_executable(self.toolchain) is not None:
                raise ProviderUnavailableError(
                    "Local render tool changed during the render operation"
                )
            manifest = RenderManifest.for_request(
                request,
                self.descriptor,
                pages,
                pdf=pdf,
            )
        except BaseException:
            _remove_files(installed)
            raise
        return manifest

    async def _render_pipeline(
        self,
        request: RenderRequest,
        *,
        destination: Path,
        work_dir: Path,
        dpi: int,
        installed: list[Path],
    ) -> tuple[PdfArtifact, tuple[PageArtifact, ...]]:
        assert self.toolchain.soffice is not None
        assert self.toolchain.pdftoppm is not None
        profile_dir = _private_subdirectory(work_dir, "profile")
        home_dir = _private_subdirectory(work_dir, "home")
        temp_dir = _private_subdirectory(work_dir, "tmp")
        input_dir = _private_subdirectory(work_dir, "input")
        pdf_dir = _private_subdirectory(work_dir, "pdf")
        raster_dir = _private_subdirectory(work_dir, "raster")
        xdg_config_dir = _private_subdirectory(work_dir, "xdg-config")
        xdg_cache_dir = _private_subdirectory(work_dir, "xdg-cache")
        xdg_data_dir = _private_subdirectory(work_dir, "xdg-data")
        xdg_runtime_dir = _private_subdirectory(work_dir, "xdg-runtime")
        staged_source = _stage_private_source(
            request,
            input_dir,
            max_bytes=self.limits.max_source_bytes,
        )
        sandbox_invocation = (
            self.sandbox.prepare(work_dir) if self.sandbox is not None else None
        )
        environment = _minimal_environment(
            toolchain=self.toolchain,
            profile_dir=profile_dir,
            home_dir=home_dir,
            temp_dir=temp_dir,
            xdg_config_dir=xdg_config_dir,
            xdg_cache_dir=xdg_cache_dir,
            xdg_data_dir=xdg_data_dir,
            xdg_runtime_dir=xdg_runtime_dir,
            sandbox_invocation=sandbox_invocation,
            host_environment=self._host_environment,
        )
        deadline = self._clock() + float(self.limits.timeout_seconds)
        soffice_argv = (
            str(self.toolchain.soffice.path),
            f"-env:UserInstallation={profile_dir.as_uri()}",
            "--headless",
            "--nologo",
            "--nodefault",
            "--nolockcheck",
            "--norestore",
            "--convert-to",
            "pdf",
            "--outdir",
            str(pdf_dir),
            str(staged_source),
        )
        await self._run_stage(
            "LibreOffice conversion",
            soffice_argv,
            cwd=work_dir,
            env=environment,
            deadline=deadline,
            expected_executable=self.toolchain.soffice.path,
            sandbox_invocation=sandbox_invocation,
        )
        self._validate_source(request)
        pdf_path = _single_pdf(pdf_dir, self.limits.max_pdf_bytes)
        pdf_sha256, pdf_size_bytes, page_count = _validate_pdf(
            pdf_path,
            max_bytes=self.limits.max_pdf_bytes,
            max_pages=self.limits.max_pages,
            max_page_pixels=self.limits.max_page_pixels,
            dpi=dpi,
        )

        raster_prefix = raster_dir / "page"
        pdftoppm_argv = (
            str(self.toolchain.pdftoppm.path),
            "-png",
            "-r",
            str(dpi),
            "-f",
            "1",
            "-l",
            str(page_count),
            str(pdf_path),
            str(raster_prefix),
        )
        await self._run_stage(
            "PDF rasterization",
            pdftoppm_argv,
            cwd=work_dir,
            env=environment,
            deadline=deadline,
            expected_executable=self.toolchain.pdftoppm.path,
            sandbox_invocation=sandbox_invocation,
        )
        if _sha256_regular_file(pdf_path, self.limits.max_pdf_bytes) != pdf_sha256:
            raise CacheIntegrityError("Intermediate Office PDF changed during rasterization")
        self._validate_source(request)
        raster_pages = _validated_raster_pages(
            raster_dir,
            expected_pages=page_count,
            max_output_bytes=self.limits.max_output_bytes,
            max_page_pixels=self.limits.max_page_pixels,
        )

        final_pdf_path = destination / _PDF_ARTIFACT_FILENAME
        _assert_within(destination, final_pdf_path, strict=False)
        try:
            os.replace(pdf_path, final_pdf_path)
        except OSError as exc:
            raise RenderProcessError(
                "Could not install LibreOffice PDF in staging"
            ) from exc
        installed.append(final_pdf_path)
        _harden_file(final_pdf_path)
        pdf = PdfArtifact(
            filename=final_pdf_path.name,
            sha256=pdf_sha256,
            size_bytes=pdf_size_bytes,
            page_count=page_count,
        )

        pages: list[PageArtifact] = []
        for raster in raster_pages:
            final_path = destination / f"page-{raster.page_number}.png"
            _assert_within(destination, final_path, strict=False)
            try:
                os.replace(raster.path, final_path)
            except OSError as exc:
                raise RenderProcessError(
                    "Could not install LibreOffice raster page in staging"
                ) from exc
            installed.append(final_path)
            _harden_file(final_path)
            pages.append(
                PageArtifact(
                    page_number=raster.page_number,
                    filename=final_path.name,
                    sha256=raster.sha256,
                    pixel_sha256=raster.pixel_sha256,
                    size_bytes=raster.size_bytes,
                    width_px=raster.width_px,
                    height_px=raster.height_px,
                )
            )
        return pdf, tuple(pages)

    async def _run_stage(
        self,
        label: str,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        deadline: float,
        expected_executable: Path,
        sandbox_invocation: OfficeRendererSandboxInvocation | None,
    ) -> None:
        _validate_stage_invocation(
            argv,
            cwd=cwd,
            expected_executable=expected_executable,
            sandbox_invocation=sandbox_invocation,
        )
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise RenderTimeoutError(f"{label} exceeded the total render timeout")
        result = await self.runner.run(
            self._native_launch_argv(argv, work_dir=cwd),
            cwd=cwd,
            env=env,
            timeout_seconds=remaining,
        )
        if not isinstance(result, RenderProcessResult):
            raise RenderContractError("render process runner returned an invalid result")
        if result.returncode != 0:
            raise RenderProcessError(
                f"{label} failed with exit code {result.returncode}"
            )

    def _native_launch_argv(
        self,
        argv: Sequence[str],
        *,
        work_dir: Path,
    ) -> tuple[str, ...]:
        """Wrap authoritative bundle stages in the attested native launcher."""

        args = tuple(argv)
        if self.native_sandbox_contract is None:
            return args
        try:
            return self.native_sandbox_contract.build_no_shell_argv(
                args,
                work_dir=work_dir,
            )
        except NativeSandboxContractError as exc:
            raise RenderContractError(
                "native renderer sandbox launch contract is invalid"
            ) from exc

    def _validate_source(self, request: RenderRequest) -> None:
        root = request.workspace_root
        source = request.source_path
        if root.is_symlink() or source.is_symlink():
            raise PathEscapeError("Office render source cannot use a symbolic link")
        try:
            resolved_root = root.resolve(strict=True)
            resolved_source = source.resolve(strict=True)
            resolved_source.relative_to(resolved_root)
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise StaleSourceError("Office render source is missing") from exc
        except ValueError as exc:
            raise PathEscapeError(
                "Office render source resolves outside its workspace"
            ) from exc
        digest = _sha256_regular_file(resolved_source, self.limits.max_source_bytes)
        if digest != request.source_sha256:
            raise StaleSourceError(
                "Office render source SHA-256 no longer matches the request"
            )


@dataclass(frozen=True, slots=True)
class _RasterPage:
    page_number: int
    path: Path
    sha256: str
    pixel_sha256: str
    size_bytes: int
    width_px: int
    height_px: int


def _renderer_version(
    toolchain: LibreOfficeToolchain,
    *,
    bundled_fonts: bool,
) -> str:
    soffice = toolchain.soffice.sha256[:16] if toolchain.soffice else "missing"
    pdftoppm = toolchain.pdftoppm.sha256[:16] if toolchain.pdftoppm else "missing"
    fonts = "private" if bundled_fonts else "host"
    return (
        f"{LIBREOFFICE_PIPELINE_VERSION}.lo-{soffice}.poppler-{pdftoppm}"
        f".fonts-{fonts}"
    )


def _changed_executable(toolchain: LibreOfficeToolchain) -> str | None:
    for label, identity in (
        ("LibreOffice", toolchain.soffice),
        ("pdftoppm", toolchain.pdftoppm),
    ):
        if identity is None:
            continue
        try:
            current = _sha256_regular_file(identity.path, MAX_EXECUTABLE_BYTES)
        except (OSError, CacheIntegrityError, RenderContractError):
            return label
        if current != identity.sha256:
            return label
    return None


def _discover_executable(
    *,
    explicit: str | Path | None,
    candidates: Sequence[str | Path],
    windows: bool,
) -> ExecutableIdentity | None:
    raw_candidates: Sequence[str | Path]
    if explicit is not None:
        raw_candidates = (explicit,)
    else:
        raw_candidates = candidates
    seen: set[Path] = set()
    for raw in raw_candidates:
        try:
            candidate = Path(raw).expanduser()
            if not candidate.is_absolute():
                continue
            resolved = candidate.resolve(strict=True)
            if resolved in seen:
                continue
            seen.add(resolved)
            if not resolved.is_file():
                continue
            if not windows and not os.access(resolved, os.X_OK):
                continue
            digest = _sha256_regular_file(resolved, MAX_EXECUTABLE_BYTES)
            return ExecutableIdentity(path=resolved, sha256=digest)
        except (OSError, CacheIntegrityError, RenderContractError):
            continue
    return None


def _soffice_candidates(
    platform_name: str,
    environ: Mapping[str, str],
    which: WhichFunction,
) -> tuple[Path, ...]:
    candidates: list[Path] = []
    for command in ("libreoffice", "soffice"):
        discovered = _safe_which(which, command)
        if discovered is not None:
            candidates.append(discovered)
    if platform_name == "darwin":
        candidates.extend(
            (
                Path("/Applications/LibreOffice.app/Contents/MacOS/soffice"),
                Path.home()
                / "Applications/LibreOffice.app/Contents/MacOS/soffice",
            )
        )
    elif platform_name.startswith("win"):
        for key in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
            root = environ.get(key)
            if root:
                candidates.append(
                    Path(root) / "LibreOffice/program/soffice.exe"
                )
    else:
        candidates.extend((Path("/usr/bin/libreoffice"), Path("/usr/bin/soffice")))
    return tuple(candidates)


def _pdftoppm_candidates(
    platform_name: str,
    environ: Mapping[str, str],
    which: WhichFunction,
) -> tuple[Path, ...]:
    candidates: list[Path] = []
    discovered = _safe_which(which, "pdftoppm")
    if discovered is not None:
        candidates.append(discovered)
    poppler_bin = environ.get("POPPLER_BIN")
    if poppler_bin:
        candidates.append(
            Path(poppler_bin)
            / ("pdftoppm.exe" if platform_name.startswith("win") else "pdftoppm")
        )
    if platform_name == "darwin":
        candidates.extend(
            (Path("/opt/homebrew/bin/pdftoppm"), Path("/usr/local/bin/pdftoppm"))
        )
    elif not platform_name.startswith("win"):
        candidates.append(Path("/usr/bin/pdftoppm"))
    return tuple(candidates)


def _safe_which(which: WhichFunction, command: str) -> Path | None:
    try:
        value = which(command)
    except (OSError, ValueError):
        return None
    if not value:
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else None


def _safe_host_environment(
    environ: Mapping[str, str],
    *,
    windows: bool,
) -> dict[str, str]:
    safe: dict[str, str] = {}
    if not windows:
        return safe
    for key in ("SYSTEMROOT", "WINDIR"):
        value = environ.get(key)
        if isinstance(value, str) and value and "\x00" not in value:
            safe[key] = value
    return safe


def _minimal_environment(
    *,
    toolchain: LibreOfficeToolchain,
    profile_dir: Path,
    home_dir: Path,
    temp_dir: Path,
    xdg_config_dir: Path,
    xdg_cache_dir: Path,
    xdg_data_dir: Path,
    xdg_runtime_dir: Path,
    sandbox_invocation: OfficeRendererSandboxInvocation | None,
    host_environment: Mapping[str, str],
) -> dict[str, str]:
    assert toolchain.soffice is not None
    assert toolchain.pdftoppm is not None
    path_parts = list(
        dict.fromkeys(
            (str(toolchain.soffice.path.parent), str(toolchain.pdftoppm.path.parent))
        )
    )
    environment = {
        "HOME": str(home_dir),
        "USERPROFILE": str(home_dir),
        "TMPDIR": str(temp_dir),
        "TMP": str(temp_dir),
        "TEMP": str(temp_dir),
        "XDG_CONFIG_HOME": str(xdg_config_dir),
        "XDG_CACHE_HOME": str(xdg_cache_dir),
        "XDG_DATA_HOME": str(xdg_data_dir),
        "XDG_RUNTIME_DIR": str(xdg_runtime_dir),
        "SAL_USE_VCLPLUGIN": "svp",
        "SAL_DISABLE_OPENCL": "1",
        "SAL_DISABLEGL": "1",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.pathsep.join(path_parts),
    }
    if sandbox_invocation is not None:
        sandbox_invocation.validate()
        environment.update(sandbox_invocation.environment)
    environment.update(host_environment)
    return environment


def _validate_stage_invocation(
    argv: Sequence[str],
    *,
    cwd: Path,
    expected_executable: Path,
    sandbox_invocation: OfficeRendererSandboxInvocation | None,
) -> None:
    if not argv or argv[0] != str(expected_executable):
        raise RenderContractError("renderer stage executable changed")
    try:
        executable_info = expected_executable.lstat()
        executable = expected_executable.resolve(strict=True)
        work_dir = cwd.resolve(strict=True)
    except OSError as exc:
        raise RenderContractError("renderer stage boundary is unavailable") from exc
    if (
        stat.S_ISLNK(executable_info.st_mode)
        or not stat.S_ISREG(executable_info.st_mode)
        or executable != expected_executable
        or cwd.is_symlink()
        or work_dir != cwd
    ):
        raise RenderContractError("renderer stage boundary is redirected")
    if sandbox_invocation is not None:
        sandbox_invocation.validate()
        if (
            executable not in sandbox_invocation.sandbox.executable_paths
            or work_dir != sandbox_invocation.work_dir
        ):
            raise RenderContractError("renderer stage escaped its sandbox")


def _stage_private_source(
    request: RenderRequest,
    input_dir: Path,
    *,
    max_bytes: int,
) -> Path:
    """Copy one stable workspace source into a private read-only input file."""

    source = request.source_path.resolve(strict=True)
    _assert_within(request.workspace_root, source)
    suffix = source.suffix.casefold()
    if not suffix or len(suffix) > 16 or not suffix[1:].isalnum():
        raise RenderContractError("Office render source extension is invalid")
    target = input_dir / f"document{suffix}"
    _assert_within(input_dir, target, strict=False)
    read_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    read_flags |= getattr(os, "O_NOFOLLOW", 0)
    write_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    write_flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    source_descriptor: int | None = None
    target_descriptor: int | None = None
    digest = hashlib.sha256()
    total = 0
    try:
        source_descriptor = os.open(source, read_flags)
        before = os.fstat(source_descriptor)
        visible_before = source.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size > max_bytes
            or stat.S_ISLNK(visible_before.st_mode)
            or (before.st_dev, before.st_ino)
            != (visible_before.st_dev, visible_before.st_ino)
        ):
            raise StaleSourceError("Office render source is not a stable regular file")
        target_descriptor = os.open(target, write_flags, 0o400)
        while chunk := os.read(source_descriptor, 1024 * 1024):
            total += len(chunk)
            if total > max_bytes:
                raise StaleSourceError("Office render source exceeds its byte budget")
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(target_descriptor, view)
                if written <= 0:
                    raise OSError("short Office input staging write")
                view = view[written:]
        os.fsync(target_descriptor)
        after = os.fstat(source_descriptor)
        visible_after = source.lstat()
    except (OSError, FileNotFoundError) as exc:
        raise StaleSourceError("Office render source changed while staging") from exc
    finally:
        if target_descriptor is not None:
            os.close(target_descriptor)
        if source_descriptor is not None:
            os.close(source_descriptor)
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    visible_identity = (
        visible_after.st_dev,
        visible_after.st_ino,
        visible_after.st_size,
        visible_after.st_mtime_ns,
        visible_after.st_ctime_ns,
    )
    if (
        total != before.st_size
        or identity_after != identity_before
        or visible_identity != identity_after
        or digest.hexdigest() != request.source_sha256
    ):
        try:
            target.unlink()
        except OSError:
            pass
        raise StaleSourceError("Office render source changed while staging")
    try:
        os.chmod(target, stat.S_IREAD)
        target_info = target.lstat()
    except OSError as exc:
        raise RenderProcessError("Office render input could not be hardened") from exc
    if stat.S_ISLNK(target_info.st_mode) or not stat.S_ISREG(target_info.st_mode):
        raise PathEscapeError("Office render input was redirected")
    return target.resolve(strict=True)


def _render_dpi(parameters: Mapping[str, Any]) -> int:
    if set(parameters) - {"dpi"}:
        raise RenderContractError("LibreOffice render parameters contain unknown keys")
    dpi = parameters.get("dpi", DEFAULT_DPI)
    if (
        not isinstance(dpi, int)
        or isinstance(dpi, bool)
        or not MIN_DPI <= dpi <= MAX_DPI
    ):
        raise RenderContractError(
            f"LibreOffice dpi must be between {MIN_DPI} and {MAX_DPI}"
        )
    return dpi


def _empty_staging_directory(output_dir: Path) -> Path:
    destination = Path(output_dir)
    if not destination.is_absolute():
        raise RenderContractError("Office renderer output directory must be absolute")
    if destination.is_symlink() or not destination.is_dir():
        raise PathEscapeError("Office renderer output directory is redirected or invalid")
    resolved = destination.resolve(strict=True)
    try:
        entries = list(resolved.iterdir())
    except OSError as exc:
        raise RenderProcessError("Office renderer output directory is unreadable") from exc
    if entries:
        raise CacheIntegrityError("Office renderer requires an empty staging directory")
    return resolved


def _private_subdirectory(root: Path, name: str) -> Path:
    path = root / name
    _assert_within(root, path, strict=False)
    path.mkdir(mode=0o700)
    _harden_directory(path)
    return path


def _single_pdf(directory: Path, max_bytes: int) -> Path:
    entries = _regular_entries(directory)
    if len(entries) != 1 or entries[0].suffix.lower() != ".pdf":
        raise CacheIntegrityError(
            "LibreOffice conversion must produce exactly one PDF and no other files"
        )
    pdf_path = entries[0]
    if pdf_path.stat().st_size > max_bytes:
        raise CacheIntegrityError("Intermediate Office PDF exceeds its byte budget")
    return pdf_path


def _validate_pdf(
    path: Path,
    *,
    max_bytes: int,
    max_pages: int,
    max_page_pixels: int,
    dpi: int,
) -> tuple[str, int, int]:
    digest, size_bytes, _head = _hash_regular_file(path, max_bytes)
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise CacheIntegrityError("Intermediate Office PDF cannot be opened") from exc
    try:
        with os.fdopen(fd, "rb", closefd=False) as handle:
            if handle.read(len(_PDF_MAGIC)) != _PDF_MAGIC:
                raise CacheIntegrityError("Intermediate Office output is not a PDF")
            handle.seek(0)
            try:
                reader = PdfReader(handle, strict=True)
                if reader.is_encrypted:
                    raise CacheIntegrityError("Intermediate Office PDF is encrypted")
                page_count = len(reader.pages)
                if not 1 <= page_count <= max_pages:
                    raise CacheIntegrityError(
                        "Intermediate Office PDF page count exceeds its budget"
                    )
                for page in reader.pages:
                    width = float(page.mediabox.width) * dpi / 72.0
                    height = float(page.mediabox.height) * dpi / 72.0
                    if (
                        not math.isfinite(width)
                        or not math.isfinite(height)
                        or width <= 0
                        or height <= 0
                        or width * height > max_page_pixels
                    ):
                        raise CacheIntegrityError(
                            "Intermediate Office PDF page dimensions exceed the pixel budget"
                        )
            except CacheIntegrityError:
                raise
            except Exception as exc:
                raise CacheIntegrityError(
                    "Intermediate Office PDF is structurally invalid"
                ) from exc
    finally:
        os.close(fd)
    return digest, size_bytes, page_count


def _validated_raster_pages(
    directory: Path,
    *,
    expected_pages: int,
    max_output_bytes: int,
    max_page_pixels: int,
) -> tuple[_RasterPage, ...]:
    entries = _regular_entries(directory)
    numbered: dict[int, Path] = {}
    for path in entries:
        match = _PAGE_NAME.fullmatch(path.name)
        if match is None:
            raise CacheIntegrityError("PDF rasterizer produced an undeclared artifact")
        number = int(match.group(1))
        if number in numbered:
            raise CacheIntegrityError("PDF rasterizer produced a duplicate page")
        numbered[number] = path
    if sorted(numbered) != list(range(1, expected_pages + 1)):
        raise CacheIntegrityError("PDF rasterizer page set does not match the PDF")

    total_bytes = 0
    pages: list[_RasterPage] = []
    for number in range(1, expected_pages + 1):
        path = numbered[number]
        remaining = max_output_bytes - total_bytes
        if remaining < 1:
            raise CacheIntegrityError("Office raster output exceeds its byte budget")
        digest, pixel_digest, size_bytes, width, height = _validate_png(
            path,
            remaining,
        )
        total_bytes += size_bytes
        if width * height > max_page_pixels:
            raise CacheIntegrityError("Office raster page exceeds its pixel budget")
        pages.append(
            _RasterPage(
                page_number=number,
                path=path,
                sha256=digest,
                pixel_sha256=pixel_digest,
                size_bytes=size_bytes,
                width_px=width,
                height_px=height,
            )
        )
    return tuple(pages)


def _validate_png(path: Path, max_bytes: int) -> tuple[str, str, int, int, int]:
    digest, size_bytes, head = _hash_regular_file(path, max_bytes)
    if (
        len(head) < 24
        or not head.startswith(_PNG_SIGNATURE)
        or head[8:12] != b"\x00\x00\x00\r"
        or head[12:16] != b"IHDR"
    ):
        raise CacheIntegrityError("PDF rasterizer output is not a valid PNG")
    width, height = struct.unpack(">II", head[16:24])
    if width < 1 or height < 1:
        raise CacheIntegrityError("PDF rasterizer output has invalid dimensions")
    pixel_digest = _canonical_rgba_sha256(path, width=width, height=height)
    return digest, pixel_digest, size_bytes, width, height


def _canonical_rgba_sha256(path: Path, *, width: int, height: int) -> str:
    """Hash decoded row-major RGBA bytes, independent of PNG encoding."""

    try:
        with Image.open(path) as image:
            if image.format != "PNG" or getattr(image, "n_frames", 1) != 1:
                raise CacheIntegrityError(
                    "PDF rasterizer output is not a single-frame PNG"
                )
            if image.size != (width, height):
                raise CacheIntegrityError(
                    "PDF rasterizer PNG dimensions changed during decoding"
                )
            image.load()
            rgba = image.convert("RGBA")
            digest = hashlib.sha256()
            for top in range(0, height, 256):
                bottom = min(height, top + 256)
                digest.update(
                    rgba.crop((0, top, width, bottom)).tobytes("raw", "RGBA")
                )
            return digest.hexdigest()
    except CacheIntegrityError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise CacheIntegrityError(
            "PDF rasterizer output cannot be decoded as PNG"
        ) from exc


def _validate_final_staging(
    destination: Path,
    pdf: PdfArtifact,
    pages: Sequence[PageArtifact],
) -> None:
    expected = {pdf.filename, *(page.filename for page in pages)}
    entries = _regular_entries(destination)
    if {path.name for path in entries} != expected or len(entries) != len(expected):
        raise CacheIntegrityError("Office renderer staging contains undeclared artifacts")


def _regular_entries(directory: Path) -> list[Path]:
    try:
        entries = list(directory.iterdir())
    except OSError as exc:
        raise CacheIntegrityError("Office renderer staging cannot be listed") from exc
    if any(path.is_symlink() or not path.is_file() for path in entries):
        raise CacheIntegrityError("Office renderer staging entries must be regular files")
    for path in entries:
        _assert_within(directory, path)
    return entries


def _sha256_regular_file(path: Path, max_bytes: int) -> str:
    digest, _size, _head = _hash_regular_file(path, max_bytes)
    return digest


def _hash_regular_file(path: Path, max_bytes: int) -> tuple[str, int, bytes]:
    if path.is_symlink():
        raise CacheIntegrityError("Office renderer file cannot be a symbolic link")
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise CacheIntegrityError("Office renderer file cannot be opened") from exc
    digest = hashlib.sha256()
    total = 0
    head = bytearray()
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > max_bytes:
            raise CacheIntegrityError(
                "Office renderer file is not regular or exceeds its byte budget"
            )
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise CacheIntegrityError("Office renderer file exceeds its byte budget")
            digest.update(chunk)
            if len(head) < 24:
                head.extend(chunk[: 24 - len(head)])
        if total != metadata.st_size:
            raise CacheIntegrityError("Office renderer file changed while reading")
    finally:
        os.close(fd)
    return digest.hexdigest(), total, bytes(head)


def _assert_within(boundary: Path, candidate: Path, *, strict: bool = True) -> Path:
    try:
        resolved_boundary = boundary.resolve(strict=True)
        resolved_candidate = candidate.resolve(strict=strict)
        resolved_candidate.relative_to(resolved_boundary)
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        raise PathEscapeError("Office renderer path escapes its staging boundary") from exc
    return resolved_candidate


def _harden_directory(path: Path) -> None:
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _harden_file(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _remove_files(paths: Sequence[Path]) -> None:
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # The original failure remains primary.  The enclosing cache owns
            # and removes the complete private staging tree as a final guard.
            pass


def _remove_work_directory(path: Path, *, ignore_errors: bool) -> None:
    # Windows maps the staged input's read-only mode to a filesystem attribute
    # that can prevent recursive deletion.  Remove only that disposable tree's
    # attributes immediately before cleanup; POSIX directory ownership already
    # permits unlinking a read-only child.
    if os.name == "nt":
        try:
            for directory, names, filenames in os.walk(path, topdown=False):
                directory_path = Path(directory)
                for name in filenames:
                    os.chmod(directory_path / name, stat.S_IREAD | stat.S_IWRITE)
                for name in names:
                    os.chmod(directory_path / name, stat.S_IREAD | stat.S_IWRITE)
            os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
        except OSError:
            if not ignore_errors:
                raise
    shutil.rmtree(path, ignore_errors=ignore_errors)
