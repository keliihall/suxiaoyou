from __future__ import annotations

import pytest

from app import release_features
from app.release_readiness import (
    v11_capability_matrix,
    v11_capability_released,
)


_GATES = (
    "V11_CHECKPOINTS_RELEASED",
    "V11_REWIND_RELEASED",
    "V11_HOOKS_RELEASED",
    "V11_ACP_RELEASED",
    "V11_WORKTREES_RELEASED",
    "V11_VALIDATION_AGENT_RELEASED",
    "V11_OFFICE_V2_RELEASED",
    "V11_USER_OFFICE_TEMPLATES_BETA_RELEASED",
)


def _set_all(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    for name in _GATES:
        monkeypatch.setattr(release_features, name, value, raising=False)


def test_all_v11_capabilities_are_released_by_default() -> None:
    matrix = v11_capability_matrix()

    assert set(matrix) == {
        "checkpoints",
        "rewind",
        "hooks",
        "acp",
        "worktrees",
        "validator",
        "office_preview",
        "office_authoring",
        "user_office_templates",
    }
    assert all(status.released for status in matrix.values())
    assert all(status.missing_dependencies == () for status in matrix.values())


def test_illegal_gate_combinations_do_not_advertise_dependents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_all(monkeypatch, False)
    monkeypatch.setattr(release_features, "V11_REWIND_RELEASED", True)
    monkeypatch.setattr(release_features, "V11_WORKTREES_RELEASED", True)
    monkeypatch.setattr(release_features, "V11_VALIDATION_AGENT_RELEASED", True)
    monkeypatch.setattr(release_features, "V11_OFFICE_V2_RELEASED", True)
    monkeypatch.setattr(
        release_features,
        "V11_USER_OFFICE_TEMPLATES_BETA_RELEASED",
        True,
        raising=False,
    )

    matrix = v11_capability_matrix()

    assert matrix["office_preview"].released is True
    assert matrix["rewind"].released is False
    assert matrix["worktrees"].released is False
    assert matrix["validator"].released is False
    assert matrix["office_authoring"].released is False
    assert matrix["user_office_templates"].released is False
    assert matrix["office_authoring"].missing_dependencies == (
        "checkpoints",
        "rewind",
        "validator",
    )


def test_complete_dependency_graph_releases_all_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_all(monkeypatch, True)

    matrix = v11_capability_matrix()

    assert all(status.released for status in matrix.values())
    assert v11_capability_released("office_authoring") is True
    assert v11_capability_released("user_office_templates") is True


def test_unknown_capability_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown v1.1 capability"):
        v11_capability_released("made_up")
