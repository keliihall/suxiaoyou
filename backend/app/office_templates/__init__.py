"""Safe, immutable Office template registry and instantiation foundation.

The signed first-party catalog is integrated behind the code-owned Office v1.1
release gate and remains disabled by default.  It never evaluates Jinja,
Python, shell, macros, or embedded objects.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from app.office_templates.errors import (
    OfficeTemplateError,
    TemplateConflictError,
    TemplateContractError,
    TemplateFeatureDisabledError,
    TemplateInUseError,
    TemplateInstantiationError,
    TemplateIntegrityError,
    TemplateNotFoundError,
    TemplateSecurityError,
)
from app.office_templates.bundled import (
    BUNDLED_CATALOG_ID,
    BUNDLED_CATALOG_SCHEMA_VERSION,
    BUNDLED_SIGNATURE_SCHEMA_VERSION,
    BundledOfficeTemplateCatalog,
    BundledOfficeTemplateService,
    BundledPlaceholderSchema,
    BundledRenderBaseline,
    BundledTemplateDescriptor,
)
from app.office_templates.instantiation import OfficeTemplateInstantiator
from app.office_templates.models import (
    OFFICE_TEMPLATES_DEFAULT_ENABLED,
    TEMPLATE_MANIFEST_SCHEMA_VERSION,
    AllowedOutputRules,
    OfficeTemplateFormat,
    TemplateChange,
    TemplateInstantiationResult,
    TemplatePackageManifest,
    TemplateRecord,
)
from app.office_templates.registry import (
    MAX_REGISTRY_RECORD_BYTES,
    REGISTRY_RECORD_SCHEMA_VERSION,
    OfficeTemplateRegistry,
)
from app.office_templates.validation import (
    OOXMLInspection,
    TemplateSafetyLimits,
    inspect_ooxml_package,
)
_LAZY_USER_EXPORTS = frozenset(
    {
        "USER_TEMPLATE_MAX_SOURCE_BYTES",
        "USER_TEMPLATE_SCHEMA_VERSION",
        "UserOfficeTemplateService",
        "UserTemplateEvidenceError",
        "UserTemplateFeatureDisabledError",
        "UserTemplateImportCandidate",
        "UserTemplatePlaceholder",
        "UserTemplateReopenError",
        "decode_user_template_placeholder_schema",
        "get_user_office_template_service",
        "normalize_placeholder_schema",
        "set_user_office_template_service",
        "validate_user_template_values",
        "validate_user_template_ref",
    }
)


def __getattr__(name: str) -> Any:
    """Load the user-template Beta without creating validation import cycles."""

    if name in _LAZY_USER_EXPORTS:
        return getattr(import_module("app.office_templates.user"), name)
    raise AttributeError(name)

__all__ = [
    "OFFICE_TEMPLATES_DEFAULT_ENABLED",
    "MAX_REGISTRY_RECORD_BYTES",
    "REGISTRY_RECORD_SCHEMA_VERSION",
    "TEMPLATE_MANIFEST_SCHEMA_VERSION",
    "AllowedOutputRules",
    "BUNDLED_CATALOG_ID",
    "BUNDLED_CATALOG_SCHEMA_VERSION",
    "BUNDLED_SIGNATURE_SCHEMA_VERSION",
    "BundledOfficeTemplateCatalog",
    "BundledOfficeTemplateService",
    "BundledPlaceholderSchema",
    "BundledRenderBaseline",
    "BundledTemplateDescriptor",
    "OfficeTemplateError",
    "OfficeTemplateFormat",
    "OfficeTemplateInstantiator",
    "OfficeTemplateRegistry",
    "OOXMLInspection",
    "TemplateChange",
    "TemplateConflictError",
    "TemplateContractError",
    "TemplateFeatureDisabledError",
    "TemplateInUseError",
    "TemplateInstantiationError",
    "TemplateInstantiationResult",
    "TemplateIntegrityError",
    "TemplateNotFoundError",
    "TemplatePackageManifest",
    "TemplateRecord",
    "TemplateSecurityError",
    "TemplateSafetyLimits",
    "USER_TEMPLATE_MAX_SOURCE_BYTES",
    "USER_TEMPLATE_SCHEMA_VERSION",
    "UserOfficeTemplateService",
    "UserTemplateEvidenceError",
    "UserTemplateFeatureDisabledError",
    "UserTemplateImportCandidate",
    "UserTemplatePlaceholder",
    "UserTemplateReopenError",
    "decode_user_template_placeholder_schema",
    "get_user_office_template_service",
    "inspect_ooxml_package",
    "normalize_placeholder_schema",
    "set_user_office_template_service",
    "validate_user_template_values",
    "validate_user_template_ref",
]
