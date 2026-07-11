"""Helpers for browser OAuth callbacks served by the local backend."""

from __future__ import annotations

from app.config import Settings


def loopback_redirect_uri(settings: Settings, path: str) -> str:
    """Return a stable localhost callback using the backend's actual port."""
    normalized_path = path if path.startswith("/") else f"/{path}"
    return f"http://localhost:{settings.port}{normalized_path}"
