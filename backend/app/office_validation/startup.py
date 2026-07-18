"""Production startup composition for Office v1.1 preview and authoring.

The approximate host renderer is useful for preview, but it never authorizes a
write.  Authoring is installed only when the composed source gates are open and
the application-bundled renderer attestation, signed template catalog, policy
resolver, and deterministic validation runtime all agree on one exact identity.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.office_rendering import (
    AdmissionControlledOfficeRenderProvider,
    AUTHORITATIVE_QUALITY,
    LIBREOFFICE_PARAMETERS_VERSION,
    OfficePreviewService,
    OfficeRenderCache,
    OfficeRenderProvider,
    build_local_office_render_provider,
)
from app.office_rendering.deployment import (
    bind_attested_native_sandbox_contract,
    build_attested_office_render_provider,
)
from app.office_rendering.native_sandbox_behavior import (
    run_native_sandbox_behavior_probe,
)
from app.office_rendering.probe import (
    run_attested_authoritative_office_renderer_probe,
)
from app.office_rendering.release_identity import (
    load_frozen_renderer_release_identity,
)
from app.office_rendering.models import canonical_json_bytes
from app.office_templates.policies import FirstPartyOfficePrecommitPolicyResolver
from app.office_templates.user import set_user_office_template_service
from app.office_validation.precommit import set_office_precommit_coordinator
from app.office_validation.runtime import (
    build_office_v11_runtime,
    install_office_v11_runtime,
    uninstall_office_v11_runtime,
)
from app.release_readiness import v11_capability_released


OFFICE_RENDER_PARAMETERS_VERSION = LIBREOFFICE_PARAMETERS_VERSION
OFFICE_RENDER_PARAMETERS: Mapping[str, Any] = {"dpi": 144}
OFFICE_RENDER_MAX_CONCURRENT: int = 1
OFFICE_RENDER_ADMISSION_TIMEOUT_SECONDS: float = 5.0


@dataclass(frozen=True, slots=True)
class OfficeV11StartupResult:
    """Path-free result suitable for startup logging and focused tests."""

    preview_installed: bool
    authoring_installed: bool
    renderer_quality: str | None


async def initialize_office_v11_runtime(
    app_state: object,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    data_dir: str | Path,
) -> OfficeV11StartupResult:
    """Install preview, then atomically upgrade to attested authoring if ready."""

    root = Path(data_dir).expanduser()
    if not root.is_absolute():
        raise ValueError("Office runtime data root must be absolute")

    # Reinitialization and dynamic test compositions must never retain a commit
    # coordinator from an older renderer/policy identity.
    uninstall_office_v11_runtime(app_state)
    # This desktop backend owns one application runtime per process.  Clear any
    # orphaned authority left by an interrupted/older assembly even when it is
    # no longer attached to the new app.state object.
    set_office_precommit_coordinator(None)
    set_user_office_template_service(None)
    for name in (
        "office_preview_service",
        "office_precommit_coordinator",
        "office_user_template_service",
        "office_v11_runtime",
    ):
        try:
            delattr(app_state, name)
        except AttributeError:
            pass

    if not v11_capability_released("office_preview"):
        return OfficeV11StartupResult(False, False, None)

    parameters = dict(OFFICE_RENDER_PARAMETERS)
    preview_installed = False
    preview_quality: str | None = None
    try:
        approximate = await asyncio.to_thread(
            build_local_office_render_provider
        )
        preview = _build_preview_service(
            session_factory,
            data_dir=root,
            provider=approximate,
            parameters=parameters,
        )
        setattr(app_state, "office_preview_service", preview)
        preview_installed = True
        preview_quality = getattr(approximate.descriptor, "quality", None)
    except Exception:
        # Authoring has its own immutable deployment and can still be attempted;
        # no host-renderer diagnostic crosses this boundary.
        preview_installed = False
        preview_quality = None

    if not v11_capability_released("office_authoring"):
        return OfficeV11StartupResult(
            preview_installed,
            False,
            preview_quality,
        )

    try:
        release_identity = await asyncio.to_thread(
            load_frozen_renderer_release_identity
        )
        authoritative = await asyncio.to_thread(
            build_attested_office_render_provider,
            release_identity=release_identity,
        )
    except Exception:
        # Source/dev processes and damaged frozen resources can retain an
        # approximate preview, but can never acquire Office write authority.
        return OfficeV11StartupResult(
            preview_installed,
            False,
            preview_quality,
        )
    descriptor = authoritative.descriptor
    availability = authoritative.availability()
    if (
        descriptor.quality != AUTHORITATIVE_QUALITY
        or not availability.available
    ):
        return OfficeV11StartupResult(
            preview_installed,
            False,
            preview_quality,
        )

    try:
        # Static attestation proves which bytes were selected.  Before those
        # bytes may authorize a write, the target-native sandbox must also
        # demonstrate its externally observable denial/containment behavior
        # on this machine.  A manifest declaration is never substituted for
        # this adversarial execution proof.
        native_sandbox_contract = await asyncio.to_thread(
            bind_attested_native_sandbox_contract,
            authoritative,
        )
        await run_native_sandbox_behavior_probe(native_sandbox_contract)

        # The signed golden document then proves that the same provider really
        # executes at release DPI, produces the reviewed RGBA pixels, and
        # embeds a font in the PDF on this host.
        await run_attested_authoritative_office_renderer_probe(authoritative)
    except Exception:
        return OfficeV11StartupResult(
            preview_installed,
            False,
            preview_quality,
        )

    parameters_sha256 = hashlib.sha256(
        canonical_json_bytes(parameters)
    ).hexdigest()
    try:
        admitted_authoritative = AdmissionControlledOfficeRenderProvider(
            authoritative,
            max_concurrent_renders=OFFICE_RENDER_MAX_CONCURRENT,
            admission_timeout_seconds=OFFICE_RENDER_ADMISSION_TIMEOUT_SECONDS,
        )
        policies = await asyncio.to_thread(
            FirstPartyOfficePrecommitPolicyResolver,
            registry_root=(
                root / "v1.1" / "office-first-party-policy-templates"
            ).absolute(),
            renderer=descriptor,
            parameters_version=OFFICE_RENDER_PARAMETERS_VERSION,
            parameters_sha256=parameters_sha256,
        )
        runtime = build_office_v11_runtime(
            session_factory,
            data_dir=(root / "v1.1").absolute(),
            provider=admitted_authoritative,
            policies=policies,
            parameters_version=OFFICE_RENDER_PARAMETERS_VERSION,
            parameters=parameters,
        )
        install_office_v11_runtime(app_state, runtime)
    except Exception:
        # The approximate preview remains useful, but no partial authoring
        # composition or commit authority is retained.
        uninstall_office_v11_runtime(app_state)
        if preview_installed:
            setattr(app_state, "office_preview_service", preview)
        return OfficeV11StartupResult(
            preview_installed,
            False,
            preview_quality,
        )

    return OfficeV11StartupResult(True, True, AUTHORITATIVE_QUALITY)


def _build_preview_service(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    data_dir: Path,
    provider: OfficeRenderProvider,
    parameters: Mapping[str, Any],
) -> OfficePreviewService:
    cache = OfficeRenderCache(
        (data_dir / "v1.1" / "office-preview-cache").absolute()
    )
    return OfficePreviewService(
        session_factory,
        cache=cache,
        provider=provider,
        parameters_version=OFFICE_RENDER_PARAMETERS_VERSION,
        parameters=dict(parameters),
        enabled=None,
    )


__all__ = [
    "OFFICE_RENDER_ADMISSION_TIMEOUT_SECONDS",
    "OFFICE_RENDER_MAX_CONCURRENT",
    "OFFICE_RENDER_PARAMETERS",
    "OFFICE_RENDER_PARAMETERS_VERSION",
    "OfficeV11StartupResult",
    "initialize_office_v11_runtime",
]
