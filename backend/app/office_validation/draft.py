"""Checkpoint-free deterministic validation for private Office draft trees.

This is a low-level server primitive for the private staging tree owned by a
``WorkspaceMutationTransaction``.  It deliberately does not use
``OfficePreviewService``: a pre-commit draft has no finalized checkpoint and
must never be made visible merely so the validation Agent can read it.

The primitive does not commit or repair files.  Its caller remains responsible
for binding the returned candidate seal to the transaction's commit boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any, Mapping

from app.office_rendering.cache import OfficeRenderCache
from app.office_rendering.errors import OfficeRenderingError
from app.office_rendering.models import RenderManifest, RenderRequest
from app.office_rendering.provider import OfficeRenderProvider
from app.office_templates.models import TemplatePackageManifest
from app.office_validation.errors import (
    OfficeValidationContractError,
    OfficeValidationError,
    OfficeValidationSecurityError,
)
from app.office_validation.models import (
    OfficeValidationReport,
    ValidationCheck,
    derive_verdict,
)
from app.office_validation.structure import (
    OOXMLPartManifest,
    compare_ooxml_parts,
    inspect_ooxml_path,
)
from app.office_validation.visual import VisualDiffPolicy, compare_rendered_pages


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _logical_unit_count(manifest: OOXMLPartManifest) -> int:
    """Count format-owned sheet/slide units for an intent-bound edit."""

    if manifest.document_format == "xlsx":
        pattern = re.compile(r"xl/worksheets/sheet\d+\.xml")
    elif manifest.document_format == "pptx":
        pattern = re.compile(r"ppt/slides/slide\d+\.xml")
    else:
        raise OfficeValidationContractError(
            "DOCX edits do not expose countable logical units"
        )
    return sum(bool(pattern.fullmatch(name)) for name in manifest.parts)


class OfficeDraftStaleError(OfficeValidationError):
    """A private draft/root/cache identity changed after server capture."""


@dataclass(frozen=True, slots=True)
class OfficeGoldenPolicy:
    """Immutable template/golden identity and its approved change envelope.

    Production callers must load this contract from a signed first-party
    manifest (or an equivalently trusted user-template approval record).  A
    model must never create or widen it at request time.
    """

    policy_id: str
    template_id: str
    template_version: str
    template_manifest_sha256: str
    baseline_sha256: str
    renderer_id: str
    renderer_version: str
    font_digest: str
    parameters_version: str
    parameters_sha256: str
    allowed_changed_parts: tuple[str, ...]
    visual: VisualDiffPolicy

    def __post_init__(self) -> None:
        for value, field in (
            (self.policy_id, "policy_id"),
            (self.template_id, "template_id"),
            (self.template_version, "template_version"),
            (self.renderer_id, "renderer_id"),
            (self.renderer_version, "renderer_version"),
            (self.parameters_version, "parameters_version"),
        ):
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > 256
                or any(ord(character) < 32 for character in value)
            ):
                raise OfficeValidationContractError(f"{field} is invalid")
        for value, field in (
            (self.template_manifest_sha256, "template_manifest_sha256"),
            (self.baseline_sha256, "baseline_sha256"),
            (self.font_digest, "font_digest"),
            (self.parameters_sha256, "parameters_sha256"),
        ):
            if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                raise OfficeValidationContractError(
                    f"{field} must be a lowercase SHA-256"
                )
        try:
            patterns = tuple(self.allowed_changed_parts)
        except TypeError as exc:
            raise OfficeValidationContractError(
                "golden allowed parts are invalid"
            ) from exc
        if (
            len(patterns) > 256
            or len(patterns) != len(set(patterns))
            or any(
                not isinstance(pattern, str)
                or not pattern
                or pattern.startswith("/")
                or "\\" in pattern
                or ".." in pattern.split("/")
                for pattern in patterns
            )
        ):
            raise OfficeValidationContractError(
                "golden allowed parts are invalid"
            )
        if not isinstance(self.visual, VisualDiffPolicy):
            raise OfficeValidationContractError("golden visual policy is invalid")
        if not self.visual.require_authoritative:
            raise OfficeValidationContractError(
                "golden policy must require authoritative rendering"
            )
        object.__setattr__(self, "allowed_changed_parts", patterns)


@dataclass(frozen=True, slots=True)
class OfficeDraftSeal:
    """Exact staged file/render identity intended for transaction commit.

    This value is server-internal evidence, not a self-authenticating token.
    A future transaction integration must only accept the instance returned by
    :attr:`OfficeDraftValidationResult.commit_seal`; request payloads and model
    output must never be deserialized into this type as commit authority.
    """

    relative_path: str
    source_sha256: str
    source_mode: int
    source_size: int
    root_identity: tuple[int, int]
    source_identity: tuple[int, int]
    renderer_id: str
    renderer_version: str
    font_digest: str
    parameters_version: str
    parameters_sha256: str
    quality: str
    cache_key: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.relative_path, str)
            or not self.relative_path
            or len(self.relative_path) > 4096
            or "\\" in self.relative_path
        ):
            raise OfficeValidationContractError("draft seal path is invalid")
        relative = PurePosixPath(self.relative_path)
        if relative.is_absolute() or any(
            part in {"", ".", ".."} for part in relative.parts
        ):
            raise OfficeValidationContractError("draft seal path is invalid")
        for value, field in (
            (self.source_sha256, "source_sha256"),
            (self.font_digest, "font_digest"),
            (self.parameters_sha256, "parameters_sha256"),
            (self.cache_key, "cache_key"),
        ):
            if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                raise OfficeValidationContractError(f"draft seal {field} is invalid")
        for value, field in (
            (self.renderer_id, "renderer_id"),
            (self.renderer_version, "renderer_version"),
            (self.parameters_version, "parameters_version"),
        ):
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > 256
            ):
                raise OfficeValidationContractError(f"draft seal {field} is invalid")
        if self.quality not in {"authoritative", "approximate"}:
            raise OfficeValidationContractError("draft seal quality is invalid")
        if (
            not isinstance(self.source_mode, int)
            or isinstance(self.source_mode, bool)
            or self.source_mode < 0
            or not isinstance(self.source_size, int)
            or isinstance(self.source_size, bool)
            or self.source_size < 0
        ):
            raise OfficeValidationContractError("draft seal metadata is invalid")
        for value in (self.root_identity, self.source_identity):
            if (
                not isinstance(value, tuple)
                or len(value) != 2
                or any(
                    not isinstance(item, int)
                    or isinstance(item, bool)
                    or item < 0
                    for item in value
                )
            ):
                raise OfficeValidationContractError(
                    "draft seal filesystem identity is invalid"
                )

@dataclass(frozen=True, slots=True)
class OfficeDraftArtifact:
    """Exact package and render evidence for one private or visible source."""

    boundary_root: Path
    source_path: Path
    root_identity: tuple[int, int]
    source_identity: tuple[int, int]
    source_mode: int
    source_size: int
    source_mtime_ns: int
    structural: OOXMLPartManifest
    manifest: RenderManifest
    entry_path: Path

    def __post_init__(self) -> None:
        if not self.boundary_root.is_absolute() or not self.source_path.is_absolute():
            raise OfficeValidationContractError("draft paths must be absolute")
        if not self.entry_path.is_absolute():
            raise OfficeValidationContractError("draft render entry must be absolute")
        try:
            self.source_path.relative_to(self.boundary_root)
        except ValueError as exc:
            raise OfficeValidationContractError(
                "draft source escapes its boundary"
            ) from exc
        for value, field in (
            (self.root_identity, "root_identity"),
            (self.source_identity, "source_identity"),
        ):
            if (
                not isinstance(value, tuple)
                or len(value) != 2
                or any(
                    not isinstance(item, int)
                    or isinstance(item, bool)
                    or item < 0
                    for item in value
                )
            ):
                raise OfficeValidationContractError(f"{field} is invalid")
        for value, field in (
            (self.source_mode, "source_mode"),
            (self.source_size, "source_size"),
            (self.source_mtime_ns, "source_mtime_ns"),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise OfficeValidationContractError(f"{field} is invalid")
        if not isinstance(self.structural, OOXMLPartManifest) or not isinstance(
            self.manifest,
            RenderManifest,
        ):
            raise OfficeValidationContractError("draft evidence is invalid")
        if (
            self.structural.document_format != self.manifest.document_format
            or self.structural.package_sha256 != self.manifest.source_sha256
        ):
            raise OfficeValidationContractError(
                "draft structure and render identities differ"
            )

    @property
    def source_sha256(self) -> str:
        return self.structural.package_sha256

    @property
    def document_format(self) -> str:
        return self.structural.document_format

@dataclass(frozen=True, slots=True)
class OfficeDraftValidationResult:
    """A report bound to the exact candidate from which it was derived."""

    report: OfficeValidationReport
    candidate: OfficeDraftSeal

    def __post_init__(self) -> None:
        if not isinstance(self.report, OfficeValidationReport) or not isinstance(
            self.candidate,
            OfficeDraftSeal,
        ):
            raise OfficeValidationContractError(
                "draft validation result is invalid"
            )
        if (
            self.report.candidate_sha256 != self.candidate.source_sha256
            or self.report.renderer_id != self.candidate.renderer_id
            or self.report.renderer_version != self.candidate.renderer_version
            or self.report.font_digest != self.candidate.font_digest
        ):
            raise OfficeValidationContractError(
                "draft report and candidate seal identities differ"
            )

    @property
    def commit_seal(self) -> OfficeDraftSeal | None:
        """Expose a commit credential only for an authoritative full pass."""

        if self.report.verdict != "pass" or self.candidate.quality != "authoritative":
            return None
        return self.candidate


class OfficeDraftValidationService:
    """Capture and compare Office files without granting checkpoint authority."""

    def __init__(
        self,
        *,
        cache: OfficeRenderCache,
        provider: OfficeRenderProvider,
        parameters_version: str,
        parameters: Mapping[str, Any],
    ) -> None:
        if not isinstance(cache, OfficeRenderCache):
            raise TypeError("draft render cache is invalid")
        if not isinstance(provider, OfficeRenderProvider):
            raise TypeError("draft render provider is invalid")
        if not isinstance(parameters_version, str) or not parameters_version.strip():
            raise ValueError("draft parameters_version is required")
        self._cache = cache
        self._provider = provider
        self._parameters_version = parameters_version
        self._parameters = dict(parameters)

    async def capture(
        self,
        *,
        boundary_root: Path,
        source_path: Path,
        expected_source_sha256: str | None = None,
    ) -> OfficeDraftArtifact:
        """Capture one current regular OOXML file and its complete render entry."""

        (
            root,
            source,
            root_identity,
            source_identity,
            source_mode,
            source_size,
            source_mtime_ns,
        ) = _source_boundary(
            boundary_root,
            source_path,
        )
        document_format = _document_format(source)
        structural = inspect_ooxml_path(source, document_format)  # type: ignore[arg-type]
        if (
            expected_source_sha256 is not None
            and structural.package_sha256 != expected_source_sha256
        ):
            raise OfficeDraftStaleError(
                "Office draft does not match its expected source digest"
            )
        request = self._request(
            root=root,
            source=source,
            document_format=document_format,
            source_sha256=structural.package_sha256,
        )
        try:
            manifest = await self._cache.get_or_render(request, self._provider)
            entry = self._cache.entry_path(request, self._provider.descriptor)
        except OfficeRenderingError as exc:
            raise OfficeDraftStaleError(
                "Office draft or render cache changed during capture"
            ) from exc
        if entry is None:
            raise OfficeDraftStaleError("Office draft render entry is unavailable")
        artifact = OfficeDraftArtifact(
            boundary_root=root,
            source_path=source,
            root_identity=root_identity,
            source_identity=source_identity,
            source_mode=source_mode,
            source_size=source_size,
            source_mtime_ns=source_mtime_ns,
            structural=structural,
            manifest=manifest,
            entry_path=entry,
        )
        self.revalidate(artifact)
        return artifact

    def revalidate(self, artifact: OfficeDraftArtifact) -> None:
        """Re-read package, root/source inodes, provider identity, and cache."""

        if not isinstance(artifact, OfficeDraftArtifact):
            raise OfficeValidationContractError("draft artifact is invalid")
        try:
            (
                root,
                source,
                root_identity,
                source_identity,
                source_mode,
                source_size,
                source_mtime_ns,
            ) = _source_boundary(
                artifact.boundary_root,
                artifact.source_path,
            )
            if (
                root_identity != artifact.root_identity
                or source_identity != artifact.source_identity
                or source_mode != artifact.source_mode
                or source_size != artifact.source_size
                or source_mtime_ns != artifact.source_mtime_ns
            ):
                raise OfficeDraftStaleError(
                    "Office draft filesystem identity changed"
                )
            structural = inspect_ooxml_path(
                source,
                artifact.structural.document_format,
            )
            if structural != artifact.structural:
                raise OfficeDraftStaleError("Office draft package changed")
            request = self._request(
                root=root,
                source=source,
                document_format=artifact.structural.document_format,
                source_sha256=artifact.source_sha256,
            )
            manifest = self._cache.load(request, self._provider.descriptor)
            entry = self._cache.entry_path(request, self._provider.descriptor)
        except OfficeDraftStaleError:
            raise
        except (OfficeRenderingError, OfficeValidationSecurityError, OSError) as exc:
            raise OfficeDraftStaleError(
                "Office draft evidence is no longer current"
            ) from exc
        if manifest != artifact.manifest or entry != artifact.entry_path:
            raise OfficeDraftStaleError(
                "Office draft render identity changed"
            )

    def compare(
        self,
        *,
        baseline: OfficeDraftArtifact,
        candidate: OfficeDraftArtifact,
        allowed_changed_parts: tuple[str, ...],
        visual_policy: VisualDiffPolicy,
        expected_logical_unit_delta: int | None = None,
    ) -> OfficeDraftValidationResult:
        """Compare and bind a baseline/golden to one private candidate draft."""

        if not isinstance(visual_policy, VisualDiffPolicy):
            raise OfficeValidationContractError("draft visual policy is invalid")
        if not visual_policy.require_authoritative:
            raise OfficeValidationContractError(
                "draft approval always requires authoritative rendering"
            )
        self.revalidate(baseline)
        self.revalidate(candidate)
        structural = compare_ooxml_parts(
            baseline.structural,
            candidate.structural,
            allowed_changed_parts=allowed_changed_parts,
        )
        rejected = structural.rejected_parts
        structure_check = ValidationCheck(
            code="structural_parts",
            outcome="pass" if structural.passed else "fail",
            message=(
                "OOXML draft changes are confined to approved parts."
                if structural.passed
                else "OOXML draft changed outside approved parts: "
                + ", ".join(rejected[:8])
            ),
            metrics=tuple(
                sorted(
                    {
                        "changed_parts": len(structural.changes),
                        "rejected_parts": len(rejected),
                    }.items()
                )
            ),
        )
        logical_check: ValidationCheck | None = None
        if expected_logical_unit_delta is not None:
            baseline_units = _logical_unit_count(baseline.structural)
            candidate_units = _logical_unit_count(candidate.structural)
            observed_delta = candidate_units - baseline_units
            logical_check = ValidationCheck(
                code="logical_unit_delta",
                outcome=(
                    "pass"
                    if observed_delta == expected_logical_unit_delta
                    else "fail"
                ),
                message=(
                    "OOXML logical-unit count matches the normalized edit intent."
                    if observed_delta == expected_logical_unit_delta
                    else "OOXML logical-unit count differs from the normalized edit intent."
                ),
                metrics=tuple(
                    sorted(
                        {
                            "baseline_units": baseline_units,
                            "candidate_units": candidate_units,
                            "expected_delta": expected_logical_unit_delta,
                            "observed_delta": observed_delta,
                        }.items()
                    )
                ),
            )
        visual = compare_rendered_pages(
            baseline.manifest,
            baseline.entry_path,
            candidate.manifest,
            candidate.entry_path,
            visual_policy,
        )
        # Close mutations after both parsers/comparators finished.  A repair
        # must produce a new capture; it cannot reuse an old passing report.
        self.revalidate(baseline)
        self.revalidate(candidate)
        checks = (
            (structure_check, logical_check, *visual.checks)
            if logical_check is not None
            else (structure_check, *visual.checks)
        )
        report = OfficeValidationReport(
            document_format=baseline.structural.document_format,
            baseline_sha256=baseline.source_sha256,
            candidate_sha256=candidate.source_sha256,
            renderer_id=candidate.manifest.renderer_id,
            renderer_version=candidate.manifest.renderer_version,
            font_digest=candidate.manifest.font_digest,
            verdict=derive_verdict(checks),
            checks=checks,
        )
        return OfficeDraftValidationResult(
            report=report,
            candidate=_candidate_seal(candidate),
        )

    def compare_with_golden(
        self,
        *,
        golden: OfficeDraftArtifact,
        candidate: OfficeDraftArtifact,
        policy: OfficeGoldenPolicy,
        template_manifest: TemplatePackageManifest,
    ) -> OfficeDraftValidationResult:
        """Apply a signed/template-owned policy, never caller-selected limits."""

        if not isinstance(policy, OfficeGoldenPolicy):
            raise OfficeValidationContractError("golden policy is invalid")
        if not isinstance(template_manifest, TemplatePackageManifest):
            raise OfficeValidationContractError("template manifest is invalid")
        if (
            template_manifest.template_id != policy.template_id
            or template_manifest.template_version != policy.template_version
            or template_manifest.template_sha256
            != policy.template_manifest_sha256
            or template_manifest.source_sha256 != golden.source_sha256
        ):
            raise OfficeDraftStaleError(
                "Office template manifest or golden source changed"
            )
        manifest = golden.manifest
        observed = (
            golden.source_sha256,
            manifest.renderer_id,
            manifest.renderer_version,
            manifest.font_digest,
            manifest.parameters_version,
            manifest.parameters_sha256,
        )
        expected = (
            policy.baseline_sha256,
            policy.renderer_id,
            policy.renderer_version,
            policy.font_digest,
            policy.parameters_version,
            policy.parameters_sha256,
        )
        if observed != expected or manifest.quality != "authoritative":
            raise OfficeDraftStaleError(
                "Office golden source or rendering pipeline changed"
            )
        return self.compare(
            baseline=golden,
            candidate=candidate,
            allowed_changed_parts=policy.allowed_changed_parts,
            visual_policy=policy.visual,
        )

    def validate_standalone_create(
        self,
        *,
        candidate: OfficeDraftArtifact,
        renderer_id: str,
        renderer_version: str,
        font_digest: str,
        parameters_version: str,
        parameters_sha256: str,
        visual_policy: VisualDiffPolicy,
    ) -> OfficeDraftValidationResult:
        """Validate an ordinary create against a code-owned runtime envelope."""

        self.revalidate(candidate)
        observed = (
            candidate.manifest.renderer_id,
            candidate.manifest.renderer_version,
            candidate.manifest.font_digest,
            candidate.manifest.parameters_version,
            candidate.manifest.parameters_sha256,
        )
        expected = (
            renderer_id,
            renderer_version,
            font_digest,
            parameters_version,
            parameters_sha256,
        )
        runtime_check = ValidationCheck(
            code="standalone_runtime_identity",
            outcome="pass" if observed == expected else "fail",
            message=(
                "The ordinary-create rendering runtime matches the released policy."
                if observed == expected
                else "The ordinary-create rendering runtime differs from the released policy."
            ),
        )
        compared = self.compare(
            baseline=candidate,
            candidate=candidate,
            allowed_changed_parts=(),
            visual_policy=visual_policy,
        )
        checks = (runtime_check, *compared.report.checks)
        report = OfficeValidationReport(
            document_format=candidate.document_format,  # type: ignore[arg-type]
            baseline_sha256=candidate.source_sha256,
            candidate_sha256=candidate.source_sha256,
            renderer_id=candidate.manifest.renderer_id,
            renderer_version=candidate.manifest.renderer_version,
            font_digest=candidate.manifest.font_digest,
            verdict=derive_verdict(checks),
            checks=checks,
        )
        self.revalidate(candidate)
        return OfficeDraftValidationResult(
            report=report,
            candidate=compared.candidate,
        )

    def _request(
        self,
        *,
        root: Path,
        source: Path,
        document_format: str,
        source_sha256: str,
    ) -> RenderRequest:
        return RenderRequest(
            workspace_root=root,
            source_path=source,
            document_format=document_format,  # type: ignore[arg-type]
            source_sha256=source_sha256,
            parameters_version=self._parameters_version,
            parameters=self._parameters,
        )


def _document_format(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix not in {".docx", ".xlsx", ".pptx"}:
        raise OfficeValidationContractError(
            "Office draft must be DOCX, XLSX, or PPTX"
        )
    return suffix[1:]


def _candidate_seal(candidate: OfficeDraftArtifact) -> OfficeDraftSeal:
    """Create sealed evidence only inside the compare return path."""

    return OfficeDraftSeal(
        relative_path=candidate.source_path.relative_to(
            candidate.boundary_root
        ).as_posix(),
        source_sha256=candidate.source_sha256,
        source_mode=candidate.source_mode,
        source_size=candidate.source_size,
        root_identity=candidate.root_identity,
        source_identity=candidate.source_identity,
        renderer_id=candidate.manifest.renderer_id,
        renderer_version=candidate.manifest.renderer_version,
        font_digest=candidate.manifest.font_digest,
        parameters_version=candidate.manifest.parameters_version,
        parameters_sha256=candidate.manifest.parameters_sha256,
        quality=candidate.manifest.quality,
        cache_key=candidate.manifest.cache_key,
    )


def _source_boundary(
    boundary_root: Path,
    source_path: Path,
) -> tuple[
    Path,
    Path,
    tuple[int, int],
    tuple[int, int],
    int,
    int,
    int,
]:
    root = Path(boundary_root).expanduser()
    source = Path(source_path).expanduser()
    if not root.is_absolute() or not source.is_absolute():
        raise OfficeValidationContractError("draft source paths must be absolute")
    try:
        root_visible = root.lstat()
        source_visible = source.lstat()
        resolved_root = root.resolve(strict=True)
        resolved_source = source.resolve(strict=True)
        resolved_source.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise OfficeDraftStaleError(
            "Office draft source boundary is unavailable"
        ) from exc
    if (
        resolved_root != root
        or resolved_source != source
        or stat.S_ISLNK(root_visible.st_mode)
        or not stat.S_ISDIR(root_visible.st_mode)
        or stat.S_ISLNK(source_visible.st_mode)
        or not stat.S_ISREG(source_visible.st_mode)
    ):
        raise OfficeValidationSecurityError(
            "Office draft source cannot traverse symbolic links"
        )
    root_identity = (root_visible.st_dev, root_visible.st_ino)
    source_identity = (source_visible.st_dev, source_visible.st_ino)
    # Opening is performed independently by OOXML inspection and the render
    # cache; the repeated lstat here binds those reads to this exact pathname.
    return (
        root,
        source,
        root_identity,
        source_identity,
        stat.S_IMODE(source_visible.st_mode),
        source_visible.st_size,
        source_visible.st_mtime_ns,
    )


__all__ = [
    "OfficeDraftArtifact",
    "OfficeDraftSeal",
    "OfficeDraftStaleError",
    "OfficeDraftValidationResult",
    "OfficeDraftValidationService",
    "OfficeGoldenPolicy",
]
