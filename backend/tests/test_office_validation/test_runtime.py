from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import release_features
from app.office_rendering import RendererDescriptor
from app.office_validation import (
    OfficeV11RuntimeAssemblyError,
    build_office_v11_runtime,
    get_office_precommit_coordinator,
    install_office_v11_runtime,
    uninstall_office_v11_runtime,
)
from tests.test_office_rendering.helpers import FakeProvider


class _Policies:
    def resolve_edit(self, request, baseline):  # pragma: no cover - composition only
        raise AssertionError((request, baseline))

    def resolve_create(self, request):  # pragma: no cover - composition only
        raise AssertionError(request)


def _provider(quality: str) -> FakeProvider:
    return FakeProvider(
        RendererDescriptor(
            renderer_id="runtime-test",
            renderer_version="1",
            font_digest="f" * 64,
            quality=quality,  # type: ignore[arg-type]
        )
    )


def _open_authoring_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "V11_CHECKPOINTS_RELEASED",
        "V11_REWIND_RELEASED",
        "V11_VALIDATION_AGENT_RELEASED",
        "V11_OFFICE_V2_RELEASED",
    ):
        monkeypatch.setattr(release_features, name, True)


def test_runtime_builder_rejects_approximate_renderer(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:
    with pytest.raises(OfficeV11RuntimeAssemblyError, match="authoritative"):
        build_office_v11_runtime(
            session_factory,
            data_dir=tmp_path,
            provider=_provider("approximate"),
            policies=_Policies(),
            parameters_version="v1",
            parameters={"dpi": 144},
        )


def test_runtime_install_requires_composed_source_gates(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release_features, "V11_OFFICE_V2_RELEASED", False)
    runtime = build_office_v11_runtime(
        session_factory,
        data_dir=tmp_path,
        provider=_provider("authoritative"),
        policies=_Policies(),
        parameters_version="v1",
        parameters={"dpi": 144},
    )

    with pytest.raises(OfficeV11RuntimeAssemblyError, match="gates"):
        install_office_v11_runtime(SimpleNamespace(), runtime)


def test_runtime_installs_one_shared_attested_composition(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _open_authoring_gates(monkeypatch)
    runtime = build_office_v11_runtime(
        session_factory,
        data_dir=tmp_path,
        provider=_provider("authoritative"),
        policies=_Policies(),
        parameters_version="v1",
        parameters={"dpi": 144},
    )
    state = SimpleNamespace()

    install_office_v11_runtime(state, runtime)

    assert state.office_preview_service is runtime.preview
    assert state.office_precommit_coordinator is runtime.coordinator
    assert runtime.preview.cache is runtime.cache
    assert runtime.preview.provider is runtime.provider
    assert get_office_precommit_coordinator() is runtime.coordinator

    uninstall_office_v11_runtime(state)
    assert get_office_precommit_coordinator() is None
    assert not hasattr(state, "office_v11_runtime")
