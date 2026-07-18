"""Renderer-neutral Office preview contracts and private content cache.

Importing this package selects no renderer.  Production startup may separately
load an application-bundled, release-attested deployment; callers must still
inspect provider availability and the persisted manifest quality.
"""

from app.office_rendering.cache import OfficeRenderCache
from app.office_rendering.errors import (
    CacheIntegrityError,
    CacheWriteError,
    OfficeRenderingError,
    PathEscapeError,
    ProviderUnavailableError,
    RenderContractError,
    RenderProcessError,
    RenderTimeoutError,
    StaleSourceError,
)
from app.office_rendering.models import (
    APPROXIMATE_QUALITY,
    AUTHORITATIVE_QUALITY,
    RENDER_MANIFEST_SCHEMA_VERSION,
    OfficeDocumentFormat,
    PageArtifact,
    PdfArtifact,
    RenderManifest,
    RendererDescriptor,
    RenderQuality,
    RenderRequest,
    build_cache_key,
)
from app.office_rendering.libreoffice import (
    LIBREOFFICE_PARAMETERS_VERSION,
    ExecutableIdentity,
    LibreOfficeRenderLimits,
    LibreOfficeRenderProvider,
    LibreOfficeToolchain,
    discover_libreoffice_toolchain,
)
from app.office_rendering.provider import (
    AdmissionControlledOfficeRenderProvider,
    OFFICE_RENDERING_DEFAULT_ENABLED,
    OfficeRenderProvider,
    ProviderAvailability,
    UnavailableOfficeRenderProvider,
)
from app.office_rendering.process_runner import (
    LocalProcessTreeRunner,
    RenderProcessResult,
    RenderProcessRunner,
)
from app.office_rendering.native_sandbox import (
    NativeSandboxContract,
    NativeSandboxContractError,
    load_native_sandbox_contract,
)
from app.office_rendering.native_sandbox_behavior import (
    NATIVE_SANDBOX_BEHAVIOR_SCHEMA_VERSION,
    NativeSandboxBehaviorProbeError,
    NativeSandboxBehaviorReport,
    run_native_sandbox_behavior_probe,
)
from app.office_rendering.sandbox import (
    BundledOfficeRendererSandbox,
    OfficeRendererSandboxInvocation,
    discover_bundled_office_renderer_sandbox,
)
from app.office_rendering.attested import (
    ATTESTATION_SCHEMA_VERSION,
    AttestedOfficeRenderProvider,
    AuthoritativeRendererAttestation,
    AuthoritativeRendererReleaseIdentity,
    attestation_payload_bytes,
)
from app.office_rendering.service import (
    OfficePreviewBinding,
    OfficePreviewBusyError,
    OfficePreviewContext,
    OfficePreviewDisabledError,
    OfficePreviewError,
    OfficePreviewNotFoundError,
    OfficePreviewProvenanceError,
    OfficePreviewService,
    OfficePreviewStaleError,
    OfficeValidationStatus,
    OfficePreviewValidationSnapshot,
)
from app.office_rendering.runtime import (
    FontFingerprintError,
    build_local_office_render_provider,
    fingerprint_font_environment,
)
from app.office_rendering.probe import (
    AuthoritativeRendererProbeError,
    AuthoritativeRendererProbeManifest,
    AuthoritativeRendererProbePage,
    AuthoritativeRendererProbeReport,
    PROBE_DPI,
    PROBE_SCHEMA_VERSION,
    execute_authoritative_office_renderer_probe,
    run_attested_authoritative_office_renderer_probe,
)

__all__ = [
    "APPROXIMATE_QUALITY",
    "AdmissionControlledOfficeRenderProvider",
    "ATTESTATION_SCHEMA_VERSION",
    "AUTHORITATIVE_QUALITY",
    "RENDER_MANIFEST_SCHEMA_VERSION",
    "CacheIntegrityError",
    "CacheWriteError",
    "BundledOfficeRendererSandbox",
    "AttestedOfficeRenderProvider",
    "AuthoritativeRendererAttestation",
    "AuthoritativeRendererReleaseIdentity",
    "AuthoritativeRendererProbeError",
    "AuthoritativeRendererProbeManifest",
    "AuthoritativeRendererProbePage",
    "AuthoritativeRendererProbeReport",
    "ExecutableIdentity",
    "FontFingerprintError",
    "LIBREOFFICE_PARAMETERS_VERSION",
    "OFFICE_RENDERING_DEFAULT_ENABLED",
    "OfficeRenderProvider",
    "OfficePreviewBinding",
    "OfficePreviewBusyError",
    "OfficePreviewContext",
    "OfficePreviewDisabledError",
    "OfficePreviewError",
    "OfficePreviewNotFoundError",
    "OfficePreviewProvenanceError",
    "OfficePreviewService",
    "OfficePreviewStaleError",
    "OfficeValidationStatus",
    "OfficePreviewValidationSnapshot",
    "OfficeRenderCache",
    "OfficeDocumentFormat",
    "OfficeRenderingError",
    "OfficeRendererSandboxInvocation",
    "LocalProcessTreeRunner",
    "NativeSandboxContract",
    "NativeSandboxContractError",
    "NATIVE_SANDBOX_BEHAVIOR_SCHEMA_VERSION",
    "NativeSandboxBehaviorProbeError",
    "NativeSandboxBehaviorReport",
    "LibreOfficeRenderLimits",
    "LibreOfficeRenderProvider",
    "LibreOfficeToolchain",
    "PageArtifact",
    "PdfArtifact",
    "PathEscapeError",
    "ProviderAvailability",
    "ProviderUnavailableError",
    "PROBE_DPI",
    "PROBE_SCHEMA_VERSION",
    "RenderContractError",
    "RenderManifest",
    "RenderProcessError",
    "RenderProcessResult",
    "RenderProcessRunner",
    "RenderTimeoutError",
    "RendererDescriptor",
    "RenderQuality",
    "RenderRequest",
    "StaleSourceError",
    "UnavailableOfficeRenderProvider",
    "build_cache_key",
    "build_local_office_render_provider",
    "attestation_payload_bytes",
    "discover_libreoffice_toolchain",
    "discover_bundled_office_renderer_sandbox",
    "fingerprint_font_environment",
    "load_native_sandbox_contract",
    "run_native_sandbox_behavior_probe",
    "execute_authoritative_office_renderer_probe",
    "run_attested_authoritative_office_renderer_probe",
]
