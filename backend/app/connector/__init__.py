"""Connector layer — deduplicated MCP server management.

Connectors are independent of plugins. A plugin may *reference*
connectors, but each connector is a standalone entity that users
can enable/disable and connect/disconnect individually.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.connector.model import ConnectorInfo

if TYPE_CHECKING:
    from app.connector.registry import ConnectorRegistry

__all__ = ["ConnectorInfo", "ConnectorRegistry"]


def __getattr__(name: str) -> Any:
    """Lazily expose the registry without creating an MCP import cycle."""

    if name == "ConnectorRegistry":
        from app.connector.registry import ConnectorRegistry

        return ConnectorRegistry
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
