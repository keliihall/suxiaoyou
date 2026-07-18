"""Provider protocol for local Office document rendering."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from app.office_rendering.errors import ProviderUnavailableError, RenderContractError
from app.office_rendering.models import (
    APPROXIMATE_QUALITY,
    RenderManifest,
    RendererDescriptor,
    RenderRequest,
)


# Importing this foundation never enables Office rendering.  The production
# startup path explicitly composes it behind source gates and only an
# application-bundled signed deployment may authorize Office writes.
OFFICE_RENDERING_DEFAULT_ENABLED: Final = False


@dataclass(frozen=True, slots=True)
class ProviderAvailability:
    """Explicit local availability; unavailable providers require a reason."""

    available: bool
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.available, bool):
            raise RenderContractError("provider available must be a boolean")
        if self.available:
            if self.reason is not None:
                raise RenderContractError(
                    "an available render provider cannot carry an unavailable reason"
                )
            return
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise RenderContractError("an unavailable render provider requires a reason")
        if len(self.reason) > 512:
            raise RenderContractError("render provider unavailable reason is too long")


@runtime_checkable
class OfficeRenderProvider(Protocol):
    """Strict boundary implemented by a concrete local renderer adapter.

    Implementations write only the PNG files declared by the returned manifest
    into ``output_dir``.  The cache treats the provider as untrusted input and
    validates all identities, paths, hashes, dimensions, and source freshness
    before publishing the directory.  Output must be deterministic from the
    pinned source bytes, explicit document format, descriptor, and parameters;
    the source filename or workspace location must not change render semantics.
    """

    @property
    def descriptor(self) -> RendererDescriptor:
        """Return an immutable renderer/font identity and explicit quality."""
        ...

    def availability(self) -> ProviderAvailability:
        """Return explicit local availability without performing a render."""
        ...

    async def render(
        self,
        request: RenderRequest,
        output_dir: Path,
    ) -> RenderManifest:
        """Render into the private staging directory and describe every page."""
        ...


class AdmissionControlledOfficeRenderProvider:
    """Share one bounded render admission queue across all Office consumers.

    Production startup installs one instance as the provider for both preview
    and precommit validation.  The wrapper deliberately owns no rendering
    identity: descriptor and availability always come from the attested
    delegate, while every actual render must first acquire the same slot pool.
    """

    def __init__(
        self,
        delegate: OfficeRenderProvider,
        *,
        max_concurrent_renders: int = 1,
        admission_timeout_seconds: float = 5.0,
    ) -> None:
        if not isinstance(delegate, OfficeRenderProvider):
            raise TypeError("Office render admission delegate is invalid")
        if (
            not isinstance(max_concurrent_renders, int)
            or isinstance(max_concurrent_renders, bool)
            or not 1 <= max_concurrent_renders <= 8
        ):
            raise ValueError("Office render admission concurrency is invalid")
        if (
            isinstance(admission_timeout_seconds, bool)
            or not isinstance(admission_timeout_seconds, (int, float))
            or not 0 < float(admission_timeout_seconds) <= 30
        ):
            raise ValueError("Office render admission timeout is invalid")
        self._delegate = delegate
        self._slots = asyncio.Semaphore(max_concurrent_renders)
        self._admission_timeout_seconds = float(admission_timeout_seconds)

    @property
    def delegate(self) -> OfficeRenderProvider:
        """Return the wrapped provider for startup identity verification."""

        return self._delegate

    @property
    def descriptor(self) -> RendererDescriptor:
        return self._delegate.descriptor

    def availability(self) -> ProviderAvailability:
        return self._delegate.availability()

    async def render(
        self,
        request: RenderRequest,
        output_dir: Path,
    ) -> RenderManifest:
        acquired = False
        try:
            try:
                await asyncio.wait_for(
                    self._slots.acquire(),
                    timeout=self._admission_timeout_seconds,
                )
                acquired = True
            except TimeoutError as exc:
                raise ProviderUnavailableError(
                    "Office renderer admission timed out"
                ) from exc
            return await self._delegate.render(request, output_dir)
        finally:
            # Semaphore.acquire() is cancellation-safe while queued.  Once the
            # slot is granted, this finally also covers delegate failures and
            # cancellation during a native render.
            if acquired:
                self._slots.release()


class UnavailableOfficeRenderProvider:
    """Safe placeholder used until a concrete renderer is explicitly installed.

    This class intentionally advertises ``approximate`` quality and can never
    produce a manifest.  Its existence is not evidence that an Office renderer
    or a high-fidelity preview is available.
    """

    def __init__(self, reason: str = "No local Office render provider is installed") -> None:
        self._availability = ProviderAvailability(available=False, reason=reason)
        self._descriptor = RendererDescriptor(
            renderer_id="unavailable",
            renderer_version="0",
            font_digest=hashlib.sha256(b"no-office-render-fonts").hexdigest(),
            quality=APPROXIMATE_QUALITY,
        )

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
        del request, output_dir
        raise ProviderUnavailableError(self._availability.reason or "Renderer unavailable")
