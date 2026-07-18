from __future__ import annotations

import pytest

import app.office_validation.repair_agent as repair_agent_module
from app import release_features
from app.office_validation.repair_agent import (
    OFFICE_PRECOMMIT_REPAIR_PROMPT_SHA256,
)
from app.runtime.frozen_self_test import (
    FROZEN_V11_MODULES,
    run_frozen_v11_self_test,
)


def test_frozen_v11_probe_imports_gated_surface_and_signed_assets() -> None:
    report = run_frozen_v11_self_test()

    assert report == {
        "status": "ok",
        "module_count": len(FROZEN_V11_MODULES),
        "gate_mode": "released",
        "gates_closed": False,
        "gates_released": True,
        "gate_values": {
            "V11_CHECKPOINTS_RELEASED": True,
            "V11_REWIND_RELEASED": True,
            "V11_HOOKS_RELEASED": True,
            "V11_ACP_RELEASED": True,
            "V11_WORKTREES_RELEASED": True,
            "V11_VALIDATION_AGENT_RELEASED": True,
            "V11_OFFICE_V2_RELEASED": True,
            "V11_USER_OFFICE_TEMPLATES_BETA_RELEASED": True,
        },
        "capabilities": {
            "checkpoints": {
                "code_gate": True,
                "released": True,
                "dependencies": [],
                "missing_dependencies": [],
            },
            "rewind": {
                "code_gate": True,
                "released": True,
                "dependencies": ["checkpoints"],
                "missing_dependencies": [],
            },
            "hooks": {
                "code_gate": True,
                "released": True,
                "dependencies": [],
                "missing_dependencies": [],
            },
            "acp": {
                "code_gate": True,
                "released": True,
                "dependencies": [],
                "missing_dependencies": [],
            },
            "worktrees": {
                "code_gate": True,
                "released": True,
                "dependencies": ["checkpoints"],
                "missing_dependencies": [],
            },
            "validator": {
                "code_gate": True,
                "released": True,
                "dependencies": ["checkpoints"],
                "missing_dependencies": [],
            },
            "office_preview": {
                "code_gate": True,
                "released": True,
                "dependencies": [],
                "missing_dependencies": [],
            },
            "office_authoring": {
                "code_gate": True,
                "released": True,
                "dependencies": ["checkpoints", "rewind", "validator"],
                "missing_dependencies": [],
            },
            "user_office_templates": {
                "code_gate": True,
                "released": True,
                "dependencies": ["office_authoring"],
                "missing_dependencies": [],
            },
        },
        "templates": [
            "business-brief@1.0.0",
            "project-tracker@1.0.0",
            "status-update@1.0.0",
        ],
        "office_repair_prompt_sha256": OFFICE_PRECOMMIT_REPAIR_PROMPT_SHA256,
    }
    assert len(FROZEN_V11_MODULES) >= 50
    assert len(set(FROZEN_V11_MODULES)) == len(FROZEN_V11_MODULES)


def test_frozen_v11_probe_accepts_only_a_complete_released_gate_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate_names = tuple(
        name for name in vars(release_features) if name.startswith("V11_")
    )
    for name in gate_names:
        monkeypatch.setattr(release_features, name, True)

    report = run_frozen_v11_self_test()

    assert report["gate_mode"] == "released"
    assert report["gates_closed"] is False
    assert report["gates_released"] is True
    assert all(report["gate_values"].values())
    assert all(
        capability["released"]
        for capability in report["capabilities"].values()
    )


def test_frozen_v11_probe_rejects_a_partially_released_gate_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release_features, "V11_HOOKS_RELEASED", False)

    with pytest.raises(RuntimeError, match="partially released"):
        run_frozen_v11_self_test()


def test_frozen_v11_probe_fails_if_repair_prompt_cannot_be_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_prompt_load() -> str:
        raise RuntimeError("repair prompt failed source verification")

    monkeypatch.setattr(
        repair_agent_module,
        "load_office_precommit_repair_prompt",
        fail_prompt_load,
    )

    with pytest.raises(RuntimeError, match="failed source verification"):
        run_frozen_v11_self_test()
