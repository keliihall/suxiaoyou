"""Errors raised by deterministic Office validation boundaries."""


class OfficeValidationError(RuntimeError):
    """Base error for an invalid or unsafe validation request."""


class OfficeValidationContractError(OfficeValidationError):
    """The caller supplied an invalid manifest, policy, or artifact set."""


class OfficeValidationSecurityError(OfficeValidationError):
    """An Office package or rendered artifact crossed a trust boundary."""
