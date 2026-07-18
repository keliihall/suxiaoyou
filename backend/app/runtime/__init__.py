"""Versioned runtime contracts shared by desktop, ACP, hooks, and automation."""

from app.runtime.events import LifecycleEventV1, lifecycle_event_from_transport

__all__ = ["LifecycleEventV1", "lifecycle_event_from_transport"]
