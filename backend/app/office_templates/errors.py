"""Fail-closed errors for the Office template boundary."""

from __future__ import annotations


class OfficeTemplateError(RuntimeError):
    """Base class for a template operation that cannot be completed safely."""


class TemplateContractError(OfficeTemplateError, ValueError):
    """A manifest, placeholder, output rule, or API input is invalid."""


class TemplateSecurityError(OfficeTemplateError):
    """An OOXML package violates the frozen safety policy."""


class TemplateIntegrityError(OfficeTemplateError):
    """Stored template content or registry metadata is missing or corrupt."""


class TemplateConflictError(OfficeTemplateError):
    """An immutable template id/version already has different content."""


class TemplateNotFoundError(OfficeTemplateError):
    """The requested immutable template version does not exist."""


class TemplateInUseError(OfficeTemplateError):
    """A template version still has durable references and cannot be deleted."""


class TemplateInstantiationError(OfficeTemplateError):
    """A template could not be instantiated without risking partial output."""


class TemplateFeatureDisabledError(OfficeTemplateError):
    """The signed first-party template delivery surface is release-gated."""
