"""Contract tests for the signed first-party Office precommit resolver."""

from __future__ import annotations

import fnmatch
import hashlib
from pathlib import Path

import pytest

from app.office_rendering import (
    PageArtifact,
    PdfArtifact,
    RenderManifest,
    RenderRequest,
    RendererDescriptor,
)
from app.office_templates.bundled import BundledOfficeTemplateCatalog
from app.office_templates.policies import (
    FirstPartyOfficePrecommitPolicyResolver,
    _EDIT_ALLOWED_PARTS,
)
from app.office_templates.substitution import is_substitutable_part, placeholder_counts
from app.office_templates.validation import inspect_ooxml_package
from app.office_validation import (
    OfficeDraftArtifact,
    OfficeEditMutationIntent,
    OfficePrecommitRejectedError,
    OfficePrecommitRequest,
    OfficePrecommitPolicyResolver,
    OfficeStandaloneCreatePolicyResolver,
)
from app.office_validation.structure import OOXMLPartManifest


_PARAMETERS_SHA256 = hashlib.sha256(b'{"dpi":144}').hexdigest()


def _resolver(tmp_path: Path) -> FirstPartyOfficePrecommitPolicyResolver:
    return FirstPartyOfficePrecommitPolicyResolver(
        registry_root=(tmp_path / "registry").resolve(),
        renderer=RendererDescriptor(
            renderer_id="signed-policy-test-renderer",
            renderer_version="1",
            font_digest="f" * 64,
            quality="authoritative",
        ),
        parameters_version="precommit-v1",
        parameters_sha256=_PARAMETERS_SHA256,
    )


def _request(
    *,
    operation: str,
    document_format: str,
    template_id: str | None = None,
    template_version: str | None = None,
) -> OfficePrecommitRequest:
    edit_intent = (
        OfficeEditMutationIntent(
            document_format=document_format,  # type: ignore[arg-type]
            max_added_pages=1,
            max_removed_pages=0,
            max_outside_changed_ratio=0.30,
            max_total_changed_ratio=0.30,
            max_blank_fraction_increase=0.10,
            required_page_delta=0 if document_format == "pptx" else None,
            expected_logical_unit_delta=(
                0 if document_format in {"xlsx", "pptx"} else None
            ),
        )
        if operation == "edit"
        else None
    )
    return OfficePrecommitRequest(
        operation=operation,  # type: ignore[arg-type]
        document_format=document_format,  # type: ignore[arg-type]
        relative_path=f"output.{document_format}",
        session_id="session",
        message_id="message",
        call_id="call",
        root_turn_id="root-turn",
        turn_run_id="turn-run",
        checkpoint_id="checkpoint",
        workspace_instance_id="workspace-instance",
        template_id=template_id,
        template_version=template_version,
        trusted_edit_intent=edit_intent,
    )


def _baseline(tmp_path: Path, format_name: str) -> OfficeDraftArtifact:
    root = (tmp_path / "workspace").resolve()
    root.mkdir(parents=True)
    source = root / f"existing.{format_name}"
    content = b"baseline Office source"
    source.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    stat = source.stat()
    descriptor = RendererDescriptor(
        renderer_id="signed-policy-test-renderer",
        renderer_version="1",
        font_digest="f" * 64,
        quality="authoritative",
    )
    request = RenderRequest(
        workspace_root=root,
        source_path=source,
        document_format=format_name,  # type: ignore[arg-type]
        source_sha256=digest,
        parameters_version="precommit-v1",
        parameters={"dpi": 144},
    )
    page = PageArtifact(
        page_number=1,
        filename="page-1.png",
        sha256="a" * 64,
        pixel_sha256="b" * 64,
        size_bytes=1,
        width_px=1,
        height_px=1,
    )
    return OfficeDraftArtifact(
        boundary_root=root,
        source_path=source,
        root_identity=(stat.st_dev, stat.st_ino),
        source_identity=(stat.st_dev, stat.st_ino),
        source_mode=stat.st_mode,
        source_size=stat.st_size,
        source_mtime_ns=stat.st_mtime_ns,
        structural=OOXMLPartManifest(
            document_format=format_name,  # type: ignore[arg-type]
            package_sha256=digest,
            parts={"content.xml": digest},
        ),
        manifest=RenderManifest.for_request(
            request,
            descriptor,
            (page,),
            pdf=PdfArtifact(
                filename="document.pdf",
                sha256="c" * 64,
                size_bytes=1,
                page_count=1,
            ),
        ),
        entry_path=(tmp_path / "renders" / "entry").resolve(),
    )


@pytest.mark.parametrize(
    ("template_id", "template_version", "format_name"),
    (
        ("business-brief", "1.0.0", "docx"),
        ("project-tracker", "1.0.0", "xlsx"),
        ("status-update", "1.0.0", "pptx"),
    ),
)
def test_create_uses_exact_signed_catalog_asset_as_registry_golden(
    tmp_path: Path,
    template_id: str,
    template_version: str,
    format_name: str,
) -> None:
    resolver = _resolver(tmp_path)
    assert isinstance(resolver, OfficePrecommitPolicyResolver)
    catalog = BundledOfficeTemplateCatalog()
    descriptor, content = catalog.read_template(template_id, template_version)

    plan = resolver.resolve_create(
        _request(
            operation="create",
            document_format=format_name,
            template_id=template_id,
            template_version=template_version,
        )
    )

    record, stored = resolver._registry.read_source(template_id, template_version)
    inspection = inspect_ooxml_package(
        content,
        descriptor.manifest.format,
        expected_placeholders=descriptor.manifest.required_placeholders,
    )
    expected_changed = tuple(
        sorted(
            name
            for name, payload in inspection.entries.items()
            if is_substitutable_part(name, descriptor.manifest.format)
            and placeholder_counts(name, payload, descriptor.manifest.format)
        )
    )

    assert plan.golden_root == resolver._registry.root
    assert plan.golden_path == record.content_path
    assert plan.golden_path.is_relative_to(resolver._registry.root)
    assert stored == content
    assert record.manifest.canonical_bytes() == descriptor.manifest.canonical_bytes()
    assert plan.template_manifest == descriptor.manifest
    assert plan.golden_policy.template_id == template_id
    assert plan.golden_policy.template_version == template_version
    assert plan.golden_policy.template_manifest_sha256 == descriptor.manifest.template_sha256
    assert plan.golden_policy.baseline_sha256 == descriptor.manifest.source_sha256
    assert plan.golden_policy.allowed_changed_parts == expected_changed
    assert plan.golden_policy.visual.require_authoritative is True
    assert plan.golden_policy.visual.max_total_changed_ratio < 1.0
    assert plan.golden_policy.visual.max_outside_changed_ratio < 1.0


def test_create_rejects_unknown_or_format_mismatched_template_without_path_details(
    tmp_path: Path,
) -> None:
    resolver = _resolver(tmp_path)
    for request in (
        _request(
            operation="create",
            document_format="docx",
            template_id="unknown-template",
            template_version="1.0.0",
        ),
        _request(
            operation="create",
            document_format="pptx",
            template_id="business-brief",
            template_version="1.0.0",
        ),
        _request(operation="create", document_format="docx"),
    ):
        with pytest.raises(OfficePrecommitRejectedError) as raised:
            resolver.resolve_create(request)
        assert str(raised.value) == "First-party Office precommit policy rejected"
        assert str(tmp_path) not in str(raised.value)


@pytest.mark.parametrize("format_name", ("docx", "xlsx", "pptx"))
def test_ordinary_create_uses_frozen_authoritative_policy(
    tmp_path: Path,
    format_name: str,
) -> None:
    resolver = _resolver(tmp_path / format_name)
    assert isinstance(resolver, OfficeStandaloneCreatePolicyResolver)

    plan = resolver.resolve_standalone_create(
        _request(operation="create", document_format=format_name)
    )

    assert plan.document_format == format_name
    assert plan.renderer_id == "signed-policy-test-renderer"
    assert plan.parameters_sha256 == _PARAMETERS_SHA256
    assert plan.visual_policy.require_authoritative is True
    assert plan.visual_policy.max_added_pages == 0
    assert plan.visual_policy.max_removed_pages == 0
    assert plan.visual_policy.max_candidate_blank_fraction < 1.0


@pytest.mark.parametrize("format_name", ("docx", "xlsx", "pptx"))
def test_edit_uses_the_frozen_format_specific_office_tool_matrix(
    tmp_path: Path,
    format_name: str,
) -> None:
    resolver = _resolver(tmp_path / format_name)
    baseline = _baseline(tmp_path / format_name, format_name)

    plan = resolver.resolve_edit(
        _request(operation="edit", document_format=format_name),
        baseline,
    )

    assert plan.allowed_changed_parts == _EDIT_ALLOWED_PARTS[format_name]
    assert any(
        fnmatch.fnmatchcase("[Content_Types].xml", pattern)
        for pattern in plan.allowed_changed_parts
    )
    assert plan.visual_policy.require_authoritative is True
    assert plan.visual_policy.max_blank_fraction_increase == 0.10
    assert plan.visual_policy.max_total_changed_ratio < 1.0
    assert plan.visual_policy.max_outside_changed_ratio < 1.0
    assert plan.visual_policy.max_added_pages == 1
    assert plan.visual_policy.max_removed_pages == 0
    assert plan.visual_policy.max_total_changed_ratio == 0.30
    assert plan.visual_policy.max_outside_changed_ratio == 0.30
    assert plan.expected_logical_unit_delta == (
        0 if format_name in {"xlsx", "pptx"} else None
    )


def test_edit_rejects_template_identity_and_create_rejects_an_edit_request(
    tmp_path: Path,
) -> None:
    resolver = _resolver(tmp_path)
    baseline = _baseline(tmp_path, "docx")
    with pytest.raises(OfficePrecommitRejectedError):
        resolver.resolve_edit(
            _request(
                operation="edit",
                document_format="docx",
                template_id="business-brief",
                template_version="1.0.0",
            ),
            baseline,
        )
    with pytest.raises(OfficePrecommitRejectedError):
        resolver.resolve_create(_request(operation="edit", document_format="docx"))
