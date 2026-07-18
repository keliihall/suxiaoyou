"""Composed v1.1 release capabilities.

Raw source gates describe reviewed intent, but several v1.1 surfaces are not
safe in isolation.  This module is the single code-owned dependency graph used
by API, tool-schema, and startup boundaries.  Runtime availability (for
example an attested renderer being present on this machine) is intentionally a
separate concern and must never make a closed source gate appear released.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class V11CapabilityStatus:
    """One source gate after its required v1.1 dependencies are composed."""

    code_gate: bool
    released: bool
    dependencies: tuple[str, ...]
    missing_dependencies: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "code_gate": self.code_gate,
            "released": self.released,
            "dependencies": list(self.dependencies),
            "missing_dependencies": list(self.missing_dependencies),
        }


_DEPENDENCIES: Final[dict[str, tuple[str, ...]]] = {
    "checkpoints": (),
    "rewind": ("checkpoints",),
    "hooks": (),
    "acp": (),
    "worktrees": ("checkpoints",),
    "validator": ("checkpoints",),
    "office_preview": (),
    # Office mutation produces checkpoint-owned evidence, is expected to be
    # independently validated, and promises a reversible user-visible result.
    "office_authoring": ("checkpoints", "rewind", "validator"),
    # User templates are a separately releasable Beta surface and must never
    # become available merely because first-party Office authoring is enabled.
    "user_office_templates": ("office_authoring",),
}


def _raw_gates() -> dict[str, bool]:
    from app import release_features

    office = bool(release_features.V11_OFFICE_V2_RELEASED)
    return {
        "checkpoints": bool(release_features.V11_CHECKPOINTS_RELEASED),
        "rewind": bool(release_features.V11_REWIND_RELEASED),
        "hooks": bool(release_features.V11_HOOKS_RELEASED),
        "acp": bool(release_features.V11_ACP_RELEASED),
        "worktrees": bool(release_features.V11_WORKTREES_RELEASED),
        "validator": bool(release_features.V11_VALIDATION_AGENT_RELEASED),
        "office_preview": office,
        "office_authoring": office,
        "user_office_templates": bool(
            getattr(
                release_features,
                "V11_USER_OFFICE_TEMPLATES_BETA_RELEASED",
                False,
            )
        ),
    }


def v11_capability_matrix() -> dict[str, V11CapabilityStatus]:
    """Return the dependency-composed source status for every v1.1 surface."""

    gates = _raw_gates()
    result: dict[str, V11CapabilityStatus] = {}
    for name, dependencies in _DEPENDENCIES.items():
        missing = tuple(
            dependency
            for dependency in dependencies
            if not result[dependency].released
        )
        code_gate = gates[name]
        result[name] = V11CapabilityStatus(
            code_gate=code_gate,
            released=code_gate and not missing,
            dependencies=dependencies,
            missing_dependencies=missing,
        )
    return result


def v11_capability_released(name: str) -> bool:
    """Read one composed capability dynamically for tests and staged rollout."""

    try:
        return v11_capability_matrix()[name].released
    except KeyError as exc:
        raise ValueError(f"unknown v1.1 capability: {name}") from exc


__all__ = [
    "V11CapabilityStatus",
    "v11_capability_matrix",
    "v11_capability_released",
]
