"""Security Center services."""

from app.security.audit import record_security_event

__all__ = ["record_security_event"]
