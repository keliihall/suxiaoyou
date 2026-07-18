"""Redacted runtime readiness layered on the source-owned v1.1 gate graph."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.release_readiness import v11_capability_matrix


def _state_value(app_state: object | None, name: str) -> object | None:
    if app_state is None:
        return None
    if isinstance(app_state, Mapping):
        return app_state.get(name)
    return getattr(app_state, name, None)


def _office_quality(preview_service: object | None) -> str | None:
    provider = getattr(preview_service, "provider", None)
    descriptor = getattr(provider, "descriptor", None)
    quality = getattr(descriptor, "quality", None)
    return quality if quality in {"authoritative", "approximate"} else None


def v11_runtime_readiness(app_state: object | None) -> dict[str, dict[str, object]]:
    """Return path-free release/readiness state without probing external tools."""

    source = v11_capability_matrix()
    validator_service = _state_value(app_state, "validation_agent_service")
    preview_service = _state_value(app_state, "office_preview_service")
    precommit = _state_value(app_state, "office_precommit_coordinator")
    # Runtime/API composition uses ``office_user_template_service``.  Retain
    # the earlier spelling as a compatibility fallback for embedded callers.
    user_templates = _state_value(app_state, "office_user_template_service")
    if user_templates is None:
        user_templates = _state_value(app_state, "user_office_template_service")
    quality = _office_quality(preview_service)

    runtime_requirements: dict[str, tuple[tuple[str, bool], ...]] = {
        "checkpoints": (),
        "rewind": (),
        "hooks": (),
        "acp": (),
        "worktrees": (),
        "validator": (("validation_agent_service", validator_service is not None),),
        "office_preview": (("office_preview_service", preview_service is not None),),
        "office_authoring": (
            ("validation_agent_service", validator_service is not None),
            ("office_precommit_coordinator", precommit is not None),
            ("authoritative_renderer", quality == "authoritative"),
        ),
        "user_office_templates": (
            ("user_office_template_service", user_templates is not None),
        ),
    }
    result: dict[str, dict[str, object]] = {}
    for name, status in source.items():
        direct_missing = tuple(
            requirement
            for requirement, available in runtime_requirements[name]
            if not available
        )
        dependency_missing = tuple(
            f"{dependency}_runtime"
            for dependency in status.dependencies
            if not bool(result[dependency]["runtime_ready"])
        )
        missing_runtime = tuple(dict.fromkeys((*direct_missing, *dependency_missing)))
        item = status.to_dict()
        item.update(
            {
                "runtime_ready": status.released and not missing_runtime,
                "missing_runtime": list(missing_runtime),
            }
        )
        if name in {"office_preview", "office_authoring"}:
            item["renderer_quality"] = quality
        result[name] = item
    return result


__all__ = ["v11_runtime_readiness"]
