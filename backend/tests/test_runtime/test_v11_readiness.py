from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import release_features
from app.runtime.v11_readiness import v11_runtime_readiness


def _open_source_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "V11_CHECKPOINTS_RELEASED",
        "V11_REWIND_RELEASED",
        "V11_HOOKS_RELEASED",
        "V11_ACP_RELEASED",
        "V11_WORKTREES_RELEASED",
        "V11_VALIDATION_AGENT_RELEASED",
        "V11_OFFICE_V2_RELEASED",
        "V11_USER_OFFICE_TEMPLATES_BETA_RELEASED",
    ):
        monkeypatch.setattr(release_features, name, True, raising=False)


def _preview(quality: str) -> SimpleNamespace:
    return SimpleNamespace(
        provider=SimpleNamespace(
            descriptor=SimpleNamespace(quality=quality),
        )
    )


def test_runtime_readiness_never_promotes_closed_source_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "V11_CHECKPOINTS_RELEASED",
        "V11_REWIND_RELEASED",
        "V11_HOOKS_RELEASED",
        "V11_ACP_RELEASED",
        "V11_WORKTREES_RELEASED",
        "V11_VALIDATION_AGENT_RELEASED",
        "V11_OFFICE_V2_RELEASED",
        "V11_USER_OFFICE_TEMPLATES_BETA_RELEASED",
    ):
        monkeypatch.setattr(release_features, name, False, raising=False)
    readiness = v11_runtime_readiness(
        {
            "validation_agent_service": object(),
            "office_preview_service": _preview("authoritative"),
            "office_precommit_coordinator": object(),
            "user_office_template_service": object(),
        }
    )

    assert not any(item["runtime_ready"] for item in readiness.values())


def test_approximate_preview_is_ready_but_cannot_authorize_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _open_source_gates(monkeypatch)
    readiness = v11_runtime_readiness(
        {
            "validation_agent_service": object(),
            "office_preview_service": _preview("approximate"),
            "office_precommit_coordinator": object(),
            "user_office_template_service": object(),
        }
    )

    assert readiness["office_preview"]["runtime_ready"] is True
    assert readiness["office_preview"]["renderer_quality"] == "approximate"
    assert readiness["office_authoring"]["runtime_ready"] is False
    assert readiness["office_authoring"]["missing_runtime"] == [
        "authoritative_renderer"
    ]
    assert readiness["user_office_templates"]["runtime_ready"] is False


def test_complete_attested_runtime_reports_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _open_source_gates(monkeypatch)
    readiness = v11_runtime_readiness(
        SimpleNamespace(
            validation_agent_service=object(),
            office_preview_service=_preview("authoritative"),
            office_precommit_coordinator=object(),
            user_office_template_service=object(),
        )
    )

    assert all(item["runtime_ready"] for item in readiness.values())


def test_runtime_readiness_recursively_inherits_parent_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _open_source_gates(monkeypatch)

    readiness = v11_runtime_readiness(
        SimpleNamespace(
            office_preview_service=_preview("authoritative"),
            office_precommit_coordinator=object(),
            user_office_template_service=object(),
        )
    )

    assert readiness["office_authoring"]["runtime_ready"] is False
    assert "validation_agent_service" in readiness["office_authoring"][
        "missing_runtime"
    ]
    assert readiness["user_office_templates"]["runtime_ready"] is False
    assert "office_authoring_runtime" in readiness["user_office_templates"][
        "missing_runtime"
    ]
