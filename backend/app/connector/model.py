"""Connector data model — a single MCP server connection."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


CONNECTOR_PROVENANCE_BUILTIN = "builtin"
CONNECTOR_PROVENANCE_CUSTOM = "custom"
SUPPORTED_CONNECTOR_PROVENANCE = frozenset(
    {CONNECTOR_PROVENANCE_BUILTIN, CONNECTOR_PROVENANCE_CUSTOM}
)

REMOTE_AUTH_OAUTH_BEARER = "oauth_bearer"
REMOTE_AUTH_RAW_AUTHORIZATION = "raw_authorization"
SUPPORTED_REMOTE_AUTH_MODES = frozenset({
    REMOTE_AUTH_OAUTH_BEARER,
    REMOTE_AUTH_RAW_AUTHORIZATION,
})


@dataclass
class ConnectorInfo:
    """Represents a single, deduplicated MCP server connection.

    Unlike the old plugin-namespaced approach (``engineering:slack``),
    each connector is a unique entity identified by its ``id``
    (e.g. ``"slack"``, ``"notion"``).
    """

    id: str  # unique slug: "slack", "notion", "github"
    name: str  # display name: "Slack", "Notion"
    url: str  # MCP server URL (empty for local)
    type: str  # "remote" | "local"
    description: str
    category: str  # "communication", "productivity", etc.
    enabled: bool = False
    source: str = CONNECTOR_PROVENANCE_CUSTOM  # "builtin" | "custom"
    local_config: dict[str, Any] = field(default_factory=dict)
    referenced_by: list[str] = field(default_factory=list)
    # Remote authentication is intentionally an enum, not a user supplied
    # header map.  ``raw_authorization`` exists for trusted built-ins such as
    # Tencent Docs whose official contract requires the personal token as the
    # complete Authorization value (without a Bearer prefix).
    auth_mode: str = REMOTE_AUTH_OAUTH_BEARER
    credential_url: str = ""
    allowed_tool_patterns: list[str] = field(default_factory=list)
    approval_required_tool_patterns: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialise for API responses."""
        return {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "type": self.type,
            "description": self.description,
            "category": self.category,
            "enabled": self.enabled,
            "source": self.source,
            "referenced_by": self.referenced_by,
            "auth_mode": self.auth_mode,
            "credential_url": self.credential_url,
        }
