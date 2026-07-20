"""Fail-closed production composition for the Office v1.1 authoring runtime."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.office_rendering import (
    AUTHORITATIVE_QUALITY,
    OfficePreviewService,
    OfficeRenderCache,
    OfficeRenderProvider,
    ProviderAvailability,
    RendererDescriptor,
)
from app.office_validation.draft import OfficeDraftValidationService
from app.office_validation.precommit import (
    DeterministicOfficePrecommitCoordinator,
    OfficePrecommitPolicyResolver,
    get_office_precommit_coordinator,
    set_office_precommit_coordinator,
)
from app.release_readiness import v11_capability_released


class OfficeV11RuntimeAssemblyError(RuntimeError):
    """The signed renderer/policy combination cannot authorize Office writes."""


@dataclass(frozen=True, slots=True)
class OfficeV11Runtime:
    """One shared provider/cache composition for preview and precommit seals."""

    cache: OfficeRenderCache
    provider: OfficeRenderProvider
    preview: OfficePreviewService
    draft: OfficeDraftValidationService
    coordinator: DeterministicOfficePrecommitCoordinator
    data_dir: Path


def build_office_v11_runtime(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    data_dir: str | Path,
    provider: OfficeRenderProvider,
    policies: OfficePrecommitPolicyResolver,
    parameters_version: str,
    parameters: Mapping[str, Any],
) -> OfficeV11Runtime:
    """Build only an available, authoritative, policy-backed composition."""

    if not isinstance(provider, OfficeRenderProvider):
        raise OfficeV11RuntimeAssemblyError("Office renderer contract is invalid")
    descriptor = provider.descriptor
    if (
        not isinstance(descriptor, RendererDescriptor)
        or descriptor.quality != AUTHORITATIVE_QUALITY
    ):
        raise OfficeV11RuntimeAssemblyError(
            "Office authoring requires an attested authoritative renderer"
        )
    availability = provider.availability()
    if (
        not isinstance(availability, ProviderAvailability)
        or not availability.available
    ):
        raise OfficeV11RuntimeAssemblyError(
            "The attested Office renderer is unavailable"
        )
    if not isinstance(policies, OfficePrecommitPolicyResolver):
        raise OfficeV11RuntimeAssemblyError(
            "The signed Office validation policy registry is unavailable"
        )
    root = Path(data_dir).expanduser()
    if not root.is_absolute():
        raise OfficeV11RuntimeAssemblyError("Office runtime data root must be absolute")
    if not isinstance(parameters_version, str) or not parameters_version.strip():
        raise OfficeV11RuntimeAssemblyError(
            "Office render parameter version is unavailable"
        )
    cache = OfficeRenderCache((root / "office-preview-cache").absolute())
    normalized_parameters = dict(parameters)
    preview = OfficePreviewService(
        session_factory,
        cache=cache,
        provider=provider,
        parameters_version=parameters_version,
        parameters=normalized_parameters,
        enabled=None,
    )
    draft = OfficeDraftValidationService(
        cache=cache,
        provider=provider,
        parameters_version=parameters_version,
        parameters=normalized_parameters,
    )
    coordinator = DeterministicOfficePrecommitCoordinator(
        service=draft,
        policies=policies,
    )
    return OfficeV11Runtime(
        cache=cache,
        provider=provider,
        preview=preview,
        draft=draft,
        coordinator=coordinator,
        data_dir=root.absolute(),
    )


def install_office_v11_runtime(
    app_state: object,
    runtime: OfficeV11Runtime,
) -> None:
    """Install one reviewed composition; a closed dependency graph is rejected."""

    if not v11_capability_released("office_authoring"):
        raise OfficeV11RuntimeAssemblyError(
            "Office authoring source gates or dependencies are closed"
        )
    if not isinstance(runtime, OfficeV11Runtime):
        raise OfficeV11RuntimeAssemblyError("Office runtime assembly is invalid")
    user_templates = None
    if v11_capability_released("user_office_templates"):
        from app.office_templates.user import (
            UserOfficeTemplateService,
        )

        user_templates = UserOfficeTemplateService(
            runtime.data_dir / "office-user-templates",
            draft_validation=runtime.draft,
        )

    # Construct every fallible dependency before publishing any process-global
    # commit authority.  Replacing an existing composition is explicit and
    # leaves no stale coordinator or user-template service behind.
    uninstall_office_v11_runtime(app_state)
    setattr(app_state, "office_preview_service", runtime.preview)
    setattr(app_state, "office_precommit_coordinator", runtime.coordinator)
    setattr(app_state, "office_v11_runtime", runtime)
    set_office_precommit_coordinator(runtime.coordinator)
    if user_templates is not None:
        from app.office_templates.user import set_user_office_template_service

        setattr(app_state, "office_user_template_service", user_templates)
        set_user_office_template_service(user_templates)


def uninstall_office_v11_runtime(app_state: object) -> None:
    """Remove app-owned Office state and restore global fail-closed mode."""

    runtime = getattr(app_state, "office_v11_runtime", None)
    coordinator = (
        runtime.coordinator
        if isinstance(runtime, OfficeV11Runtime)
        else getattr(app_state, "office_precommit_coordinator", None)
    )
    from app.office_templates.user import (
        get_user_office_template_service,
        set_user_office_template_service,
    )

    user_templates = getattr(app_state, "office_user_template_service", None)
    if (
        get_user_office_template_service() is user_templates
        and user_templates is not None
    ):
        set_user_office_template_service(None)
    if get_office_precommit_coordinator() is coordinator and coordinator is not None:
        set_office_precommit_coordinator(None)
    for name in (
        "office_v11_runtime",
        "office_precommit_coordinator",
        "office_preview_service",
        "office_user_template_service",
    ):
        try:
            delattr(app_state, name)
        except (AttributeError, KeyError):
            pass


__all__ = [
    "OfficeV11Runtime",
    "OfficeV11RuntimeAssemblyError",
    "build_office_v11_runtime",
    "install_office_v11_runtime",
    "uninstall_office_v11_runtime",
]
