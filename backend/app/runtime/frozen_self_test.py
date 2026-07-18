"""Offline frozen-bundle probe for the complete v1.1 implementation surface."""

from __future__ import annotations

import importlib
from typing import Final


# Keep this list explicit. Starting FastAPI cannot prove that every closed or
# conditionally mounted module survived the PyInstaller analysis step.
FROZEN_V11_MODULES: Final[tuple[str, ...]] = (
    "app.acp.bridge",
    "app.acp.cli",
    "app.acp.self_test",
    "app.acp.server",
    "app.acp.session_bridge",
    "app.acp.stdio",
    "app.api.office_user_templates",
    "app.api.office_v2",
    "app.api.runtime_control",
    "app.hooks.config",
    "app.hooks.dispatcher",
    "app.hooks.models",
    "app.hooks.registry",
    "app.hooks.runner",
    "app.hooks.runtime",
    "app.hooks.trust",
    "app.models.checkpoint_change",
    "app.models.office_user_template",
    "app.models.session_checkpoint",
    "app.models.turn_run",
    "app.models.workspace_instance",
    "app.office_rendering.attested",
    "app.office_rendering.cache",
    "app.office_rendering.deployment",
    "app.office_rendering.libreoffice",
    "app.office_rendering.process_runner",
    "app.office_rendering.runtime",
    "app.office_rendering.service",
    "app.office_templates.bundled",
    "app.office_templates.instantiation",
    "app.office_templates.policies",
    "app.office_templates.registry",
    "app.office_templates.substitution",
    "app.office_templates.user",
    "app.office_templates.validation",
    "app.office_validation.draft",
    "app.office_validation.orchestrator",
    "app.office_validation.precommit",
    "app.office_validation.precommit_repair",
    "app.office_validation.repair_agent",
    "app.office_validation.runtime",
    "app.office_validation.startup",
    "app.office_validation.structure",
    "app.office_validation.visual",
    "app.release_readiness",
    "app.runtime.checkpoint_runtime",
    "app.runtime.events",
    "app.runtime.rewind",
    "app.runtime.v11_readiness",
    "app.storage.checkpoints",
    "app.validation_agent.contracts",
    "app.validation_agent.persistence",
    "app.validation_agent.scheduler",
    "app.validation_agent.service",
    "app.worktree.runtime",
    "app.worktree.service",
)

_V11_GATE_NAMES: Final[tuple[str, ...]] = (
    "V11_CHECKPOINTS_RELEASED",
    "V11_REWIND_RELEASED",
    "V11_HOOKS_RELEASED",
    "V11_ACP_RELEASED",
    "V11_WORKTREES_RELEASED",
    "V11_VALIDATION_AGENT_RELEASED",
    "V11_OFFICE_V2_RELEASED",
    "V11_USER_OFFICE_TEMPLATES_BETA_RELEASED",
)


def run_frozen_v11_self_test() -> dict[str, object]:
    """Import every gated root and verify immutable first-party assets.

    This probe creates no application, database, workspace, provider, process,
    or network client. It accepts only the two coherent source states used by
    release engineering: every v1.1 gate closed, or every contracted v1.1 gate
    released with its dependency graph satisfied. A half-open binary is
    rejected so staged source changes cannot accidentally become a release.
    """

    for module_name in FROZEN_V11_MODULES:
        importlib.import_module(module_name)

    from app import release_features
    from app.office_templates.bundled import BundledOfficeTemplateCatalog
    from app.office_validation.repair_agent import (
        OFFICE_PRECOMMIT_REPAIR_PROMPT_SHA256,
        load_office_precommit_repair_prompt,
    )
    from app.release_readiness import v11_capability_matrix

    gate_values = {
        name: bool(getattr(release_features, name)) for name in _V11_GATE_NAMES
    }
    source_matrix = v11_capability_matrix()
    gates_closed = not any(gate_values.values()) and not any(
        status.released for status in source_matrix.values()
    )
    gates_released = all(gate_values.values()) and all(
        status.released for status in source_matrix.values()
    )
    descriptors = BundledOfficeTemplateCatalog().list_templates()
    template_keys = tuple(
        f"{descriptor.manifest.template_id}@{descriptor.manifest.template_version}"
        for descriptor in descriptors
    )
    expected_templates = (
        "business-brief@1.0.0",
        "project-tracker@1.0.0",
        "status-update@1.0.0",
    )
    if not gates_closed and not gates_released:
        raise RuntimeError(
            "v1.1 frozen self-test rejects a partially released gate graph"
        )
    if template_keys != expected_templates:
        raise RuntimeError("v1.1 frozen template catalog contract changed")
    # Importing repair_agent only proves its Python code reached PYZ. Loading
    # the hash-locked resource proves PyInstaller also shipped the exact
    # capability-free system prompt used at runtime.
    load_office_precommit_repair_prompt()
    return {
        "status": "ok",
        "module_count": len(FROZEN_V11_MODULES),
        "gate_mode": "closed" if gates_closed else "released",
        "gates_closed": gates_closed,
        "gates_released": gates_released,
        "gate_values": gate_values,
        "capabilities": {
            name: status.to_dict() for name, status in source_matrix.items()
        },
        "templates": list(template_keys),
        "office_repair_prompt_sha256": OFFICE_PRECOMMIT_REPAIR_PROMPT_SHA256,
    }


__all__ = ["FROZEN_V11_MODULES", "run_frozen_v11_self_test"]
