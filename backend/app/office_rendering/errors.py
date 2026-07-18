"""Fail-closed errors for the Office rendering boundary."""

from __future__ import annotations


class OfficeRenderingError(RuntimeError):
    """Base class for a rendering operation that cannot be used safely."""


class RenderContractError(OfficeRenderingError, ValueError):
    """A request, provider response, or manifest violates the frozen contract."""


class ProviderUnavailableError(OfficeRenderingError):
    """No explicitly available provider can satisfy the render request."""


class StaleSourceError(OfficeRenderingError):
    """The source content no longer matches the request's pinned SHA-256."""


class CacheIntegrityError(OfficeRenderingError):
    """A cache entry is redirected, corrupt, tampered with, or inconsistent."""


class CacheWriteError(OfficeRenderingError):
    """A fully validated cache entry could not be installed atomically."""


class PathEscapeError(OfficeRenderingError):
    """A source or cache artifact resolves outside its declared boundary."""


class RenderProcessError(OfficeRenderingError):
    """A renderer subprocess failed or could not be supervised safely."""


class RenderTimeoutError(RenderProcessError):
    """The renderer exceeded its wall-clock budget and its tree was stopped."""
