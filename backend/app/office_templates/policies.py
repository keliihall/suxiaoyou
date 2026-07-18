"""Trusted, fail-closed Office v1.1 precommit policies for bundled templates.

This module deliberately sits between the signed first-party catalog and the
generic precommit coordinator.  Requests can name a released template, but
they cannot select a golden file, a renderer identity, OOXML allow-list, or a
visual threshold.  The only golden path returned is a private,
content-addressed registry object imported from the freshly signature-verified
catalog asset.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
import tempfile
from typing import Final, Iterator

from app.office_rendering.models import AUTHORITATIVE_QUALITY, RendererDescriptor
from app.office_templates.bundled import BundledOfficeTemplateCatalog
from app.office_templates.errors import OfficeTemplateError
from app.office_templates.registry import OfficeTemplateRegistry
from app.office_templates.substitution import is_substitutable_part, placeholder_counts
from app.office_templates.validation import inspect_ooxml_package
from app.office_validation.draft import OfficeDraftArtifact, OfficeGoldenPolicy
from app.office_validation.precommit import (
    OfficeCreateValidationPlan,
    OfficeEditValidationPlan,
    OfficeEditMutationIntent,
    OfficeStandaloneCreateValidationPlan,
    OfficePrecommitRejectedError,
    OfficePrecommitRequest,
)
from app.office_validation.visual import VisualDiffPolicy


# This is the reviewed mutation matrix for the declarative OfficeTool.  It is
# intentionally narrower than its input compatibility audit: it describes
# only parts the pinned writers can create, delete, or rewrite.  Keep the
# patterns explicit rather than importing OfficeTool internals, so changing a
# writer requires a reviewed policy change as well.
_EDIT_ALLOWED_PARTS: Final[Mapping[str, tuple[str, ...]]] = {
    "docx": (
        "[[]Content_Types].xml",
        "_rels/.rels",
        "docProps/app.xml",
        "docProps/core.xml",
        "docProps/thumbnail.jpg",
        "docProps/thumbnail.jpeg",
        "docProps/thumbnail.png",
        "word/document.xml",
        "word/_rels/document.xml.rels",
        "word/fontTable.xml",
        "word/numbering.xml",
        "word/settings.xml",
        "word/styles.xml",
        "word/stylesWithEffects.xml",
        "word/webSettings.xml",
        "word/theme/theme*.xml",
        "word/header*.xml",
        "word/_rels/header*.xml.rels",
        "word/footer*.xml",
        "word/_rels/footer*.xml.rels",
        "word/media/*.bmp",
        "word/media/*.emf",
        "word/media/*.gif",
        "word/media/*.jpeg",
        "word/media/*.jpg",
        "word/media/*.png",
        "word/media/*.tif",
        "word/media/*.tiff",
        "word/media/*.wmf",
    ),
    "xlsx": (
        "[[]Content_Types].xml",
        "_rels/.rels",
        "docProps/app.xml",
        "docProps/core.xml",
        "docProps/thumbnail.jpg",
        "docProps/thumbnail.jpeg",
        "docProps/thumbnail.png",
        "xl/workbook.xml",
        "xl/_rels/workbook.xml.rels",
        "xl/styles.xml",
        "xl/theme/theme*.xml",
        "xl/worksheets/sheet*.xml",
        "xl/worksheets/_rels/sheet*.xml.rels",
        "xl/charts/chart*.xml",
        "xl/drawings/drawing*.xml",
        "xl/drawings/_rels/drawing*.xml.rels",
    ),
    "pptx": (
        "[[]Content_Types].xml",
        "_rels/.rels",
        "docProps/app.xml",
        "docProps/core.xml",
        "docProps/thumbnail.jpg",
        "docProps/thumbnail.jpeg",
        "docProps/thumbnail.png",
        "ppt/presentation.xml",
        "ppt/_rels/presentation.xml.rels",
        "ppt/presProps.xml",
        "ppt/tableStyles.xml",
        "ppt/viewProps.xml",
        "ppt/printerSettings/printerSettings*.bin",
        "ppt/slideMasters/slideMaster*.xml",
        "ppt/slideMasters/_rels/slideMaster*.xml.rels",
        "ppt/slideLayouts/slideLayout*.xml",
        "ppt/slideLayouts/_rels/slideLayout*.xml.rels",
        "ppt/slides/slide*.xml",
        "ppt/slides/_rels/slide*.xml.rels",
        "ppt/theme/theme*.xml",
        "ppt/media/*.bmp",
        "ppt/media/*.emf",
        "ppt/media/*.gif",
        "ppt/media/*.jpeg",
        "ppt/media/*.jpg",
        "ppt/media/*.png",
        "ppt/media/*.tif",
        "ppt/media/*.tiff",
        "ppt/media/*.wmf",
        "ppt/charts/chart*.xml",
        "ppt/charts/_rels/chart*.xml.rels",
        "ppt/embeddings/Microsoft_Excel_Sheet*.xlsx",
        "ppt/embeddings/Microsoft_Excel_WorkSheet*.xlsx",
        "ppt/notesMasters/notesMaster*.xml",
        "ppt/notesMasters/_rels/notesMaster*.xml.rels",
        "ppt/notesSlides/notesSlide*.xml",
        "ppt/notesSlides/_rels/notesSlide*.xml.rels",
    ),
}

# Declarative text/style/layout edits can reflow an entire rendered unit.  The
# structural matrix, page dimensions/count, authoritative renderer identity,
# and blank-page guard remain mandatory.  These limits are release-reviewed
# constants; no request or model output can widen them.
_EDIT_VISUAL_POLICY: Final = VisualDiffPolicy(
    max_outside_changed_ratio=0.85,
    max_total_changed_ratio=0.85,
    max_blank_fraction_increase=0.15,
    max_added_pages=50,
    max_removed_pages=50,
)
_CREATE_VISUAL_POLICY: Final = VisualDiffPolicy(
    max_outside_changed_ratio=0.65,
    max_total_changed_ratio=0.65,
    max_blank_fraction_increase=0.15,
)
_STANDALONE_CREATE_VISUAL_POLICY: Final = VisualDiffPolicy(
    max_candidate_blank_fraction=0.999,
    max_new_page_blank_fraction=0.999,
)


class FirstPartyOfficePrecommitPolicyResolver:
    """Resolve only signed bundled-template goldens and frozen edit policies.

    ``registry_root`` and renderer/render-parameter identities are deployment
    configuration, not tool arguments.  The resolver never accepts a caller
    supplied file path, digest, part allow-list, or visual policy.
    """

    def __init__(
        self,
        *,
        registry_root: str | Path,
        renderer: RendererDescriptor,
        parameters_version: str,
        parameters_sha256: str,
        catalog: BundledOfficeTemplateCatalog | None = None,
    ) -> None:
        if not isinstance(renderer, RendererDescriptor):
            raise TypeError("Office renderer descriptor is invalid")
        if renderer.quality != AUTHORITATIVE_QUALITY:
            raise OfficePrecommitRejectedError(
                "First-party Office policy requires an authoritative renderer"
            )
        if not isinstance(parameters_version, str) or not parameters_version.strip():
            raise TypeError("Office render parameter version is invalid")
        if (
            not isinstance(parameters_sha256, str)
            or len(parameters_sha256) != 64
            or any(character not in "0123456789abcdef" for character in parameters_sha256)
        ):
            raise TypeError("Office render parameter digest is invalid")
        if catalog is not None and not isinstance(catalog, BundledOfficeTemplateCatalog):
            raise TypeError("Bundled Office template catalog is invalid")
        self._catalog = catalog or BundledOfficeTemplateCatalog()
        try:
            self._registry = OfficeTemplateRegistry(registry_root)
        except (OfficeTemplateError, OSError, TypeError, ValueError) as exc:
            raise OfficePrecommitRejectedError(
                "First-party Office policy registry is unavailable"
            ) from exc
        self._renderer = renderer
        self._parameters_version = parameters_version.strip()
        self._parameters_sha256 = parameters_sha256

    def resolve_create(self, request: OfficePrecommitRequest) -> OfficeCreateValidationPlan:
        """Return the exact signed catalog asset as a private golden plan."""

        if (
            not isinstance(request, OfficePrecommitRequest)
            or request.operation != "create"
            or request.trusted_create_plan is not None
            or request.template_id is None
            or request.template_version is None
        ):
            raise _reject()
        try:
            descriptor, content = self._catalog.read_template(
                request.template_id,
                request.template_version,
            )
            manifest = descriptor.manifest
            if (
                manifest.format != request.document_format
                or manifest.immutable_key
                != (request.template_id, request.template_version)
                or hashlib.sha256(content).hexdigest() != manifest.source_sha256
            ):
                raise ValueError("signed catalog identity differs")
            inspection = inspect_ooxml_package(
                content,
                manifest.format,
                expected_placeholders=manifest.required_placeholders,
                limits=self._registry.limits,
            )
            changed_parts = tuple(
                sorted(
                    name
                    for name, payload in inspection.entries.items()
                    if is_substitutable_part(name, manifest.format)
                    and placeholder_counts(name, payload, manifest.format)
                )
            )
            if not changed_parts:
                raise ValueError("catalog template has no substitution parts")
            with _catalog_source(self._registry, content, manifest.format) as source:
                imported = self._registry.import_template(manifest, source)
            record, stored = self._registry.read_source(
                request.template_id,
                request.template_version,
            )
            if (
                imported.manifest.canonical_bytes() != manifest.canonical_bytes()
                or record.manifest.canonical_bytes() != manifest.canonical_bytes()
                or stored != content
                or record.content_path.parent != self._registry.root / "objects" / manifest.source_sha256[:2]
            ):
                raise ValueError("registry golden does not match signed catalog")
            policy = OfficeGoldenPolicy(
                policy_id=(
                    "first-party/"
                    f"{manifest.template_id}/{manifest.template_version}/"
                    f"{manifest.template_sha256}"
                ),
                template_id=manifest.template_id,
                template_version=manifest.template_version,
                template_manifest_sha256=manifest.template_sha256,
                baseline_sha256=manifest.source_sha256,
                renderer_id=self._renderer.renderer_id,
                renderer_version=self._renderer.renderer_version,
                font_digest=self._renderer.font_digest,
                parameters_version=self._parameters_version,
                parameters_sha256=self._parameters_sha256,
                allowed_changed_parts=changed_parts,
                visual=_CREATE_VISUAL_POLICY,
            )
            return OfficeCreateValidationPlan(
                golden_root=self._registry.root,
                golden_path=record.content_path,
                golden_policy=policy,
                template_manifest=record.manifest,
            )
        except OfficePrecommitRejectedError:
            raise
        except (OfficeTemplateError, OSError, TypeError, ValueError) as exc:
            raise _reject() from exc

    def resolve_standalone_create(
        self,
        request: OfficePrecommitRequest,
    ) -> OfficeStandaloneCreateValidationPlan:
        """Return a frozen policy for an ordinary non-template create."""

        if (
            not isinstance(request, OfficePrecommitRequest)
            or request.operation != "create"
            or request.template_id is not None
            or request.template_version is not None
            or request.trusted_create_plan is not None
        ):
            raise _reject()
        return OfficeStandaloneCreateValidationPlan(
            policy_id=f"first-party/standalone-create/{request.document_format}/1",
            document_format=request.document_format,
            renderer_id=self._renderer.renderer_id,
            renderer_version=self._renderer.renderer_version,
            font_digest=self._renderer.font_digest,
            parameters_version=self._parameters_version,
            parameters_sha256=self._parameters_sha256,
            visual_policy=_STANDALONE_CREATE_VISUAL_POLICY,
        )

    def resolve_edit(
        self,
        request: OfficePrecommitRequest,
        baseline: OfficeDraftArtifact,
    ) -> OfficeEditValidationPlan:
        """Return the frozen, format-specific envelope for a normal edit."""

        if (
            not isinstance(request, OfficePrecommitRequest)
            or not isinstance(baseline, OfficeDraftArtifact)
            or request.operation != "edit"
            or request.template_id is not None
            or request.template_version is not None
            or request.trusted_create_plan is not None
            or not isinstance(request.trusted_edit_intent, OfficeEditMutationIntent)
            or baseline.document_format != request.document_format
            or len(baseline.source_sha256) != 64
        ):
            raise _reject()
        allowed = _EDIT_ALLOWED_PARTS.get(request.document_format)
        if allowed is None:
            raise _reject()
        intent = request.trusted_edit_intent
        assert intent is not None
        visual_policy = replace(
            _EDIT_VISUAL_POLICY,
            max_outside_changed_ratio=min(
                _EDIT_VISUAL_POLICY.max_outside_changed_ratio,
                intent.max_outside_changed_ratio,
            ),
            max_total_changed_ratio=min(
                _EDIT_VISUAL_POLICY.max_total_changed_ratio,
                intent.max_total_changed_ratio,
            ),
            max_blank_fraction_increase=min(
                _EDIT_VISUAL_POLICY.max_blank_fraction_increase,
                intent.max_blank_fraction_increase,
            ),
            max_added_pages=min(
                _EDIT_VISUAL_POLICY.max_added_pages,
                intent.max_added_pages,
            ),
            max_removed_pages=min(
                _EDIT_VISUAL_POLICY.max_removed_pages,
                intent.max_removed_pages,
            ),
            required_page_delta=intent.required_page_delta,
        )
        return OfficeEditValidationPlan(
            allowed_changed_parts=allowed,
            visual_policy=visual_policy,
            expected_logical_unit_delta=intent.expected_logical_unit_delta,
        )


@contextmanager
def _catalog_source(
    registry: OfficeTemplateRegistry,
    content: bytes,
    format_name: str,
) -> Iterator[Path]:
    """Materialize signed bytes briefly inside the private registry staging area."""

    suffix = f".{format_name}"
    descriptor, raw_path = tempfile.mkstemp(
        prefix="signed-catalog-",
        suffix=suffix,
        dir=registry.root / ".staging",
    )
    path = Path(raw_path)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            if os.name != "nt":
                os.fchmod(handle.fileno(), 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        yield path
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _reject() -> OfficePrecommitRejectedError:
    """Return a stable public error without catalog, registry, or path details."""

    return OfficePrecommitRejectedError("First-party Office precommit policy rejected")


__all__ = ["FirstPartyOfficePrecommitPolicyResolver"]
