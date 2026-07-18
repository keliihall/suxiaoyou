from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import release_features
from app.office_rendering import (
    AdmissionControlledOfficeRenderProvider,
    AuthoritativeRendererReleaseIdentity,
    RendererDescriptor,
)
from app.office_validation.precommit import (
    get_office_precommit_coordinator,
    set_office_precommit_coordinator,
)
from app.office_validation.runtime import uninstall_office_v11_runtime
from app.office_validation.startup import initialize_office_v11_runtime
from tests.test_office_rendering.helpers import FakeProvider, make_request


RELEASE_IDENTITY = AuthoritativeRendererReleaseIdentity(
    app_version="1.1.0",
    release_commit="a" * 40,
)


@pytest.fixture(autouse=True)
def _frozen_release_identity(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    monkeypatch.setattr(
        "app.office_validation.startup.load_frozen_renderer_release_identity",
        lambda: RELEASE_IDENTITY,
    )
    renderer_probe = AsyncMock(return_value=object())
    monkeypatch.setattr(
        "app.office_validation.startup.run_attested_authoritative_office_renderer_probe",
        renderer_probe,
    )
    return renderer_probe


@pytest.fixture(autouse=True)
def _native_sandbox_behavior(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    contract = object()
    binder = Mock(return_value=contract)
    behavior_probe = AsyncMock(return_value=object())
    monkeypatch.setattr(
        "app.office_validation.startup.bind_attested_native_sandbox_contract",
        binder,
    )
    monkeypatch.setattr(
        "app.office_validation.startup.run_native_sandbox_behavior_probe",
        behavior_probe,
    )
    return SimpleNamespace(
        contract=contract,
        binder=binder,
        probe=behavior_probe,
    )


class _Policies:
    def resolve_edit(self, request, baseline):  # pragma: no cover - composition only
        raise AssertionError((request, baseline))

    def resolve_create(self, request):  # pragma: no cover - composition only
        raise AssertionError(request)


class _StaleCoordinator:
    async def begin(self, *, request, view):  # pragma: no cover - must be revoked
        raise AssertionError((request, view))


class _TrackingProvider(FakeProvider):
    def __init__(self, descriptor: RendererDescriptor) -> None:
        super().__init__(descriptor)
        self.active = 0
        self.max_active = 0

    async def render(self, request, output_dir):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.02)
            return await super().render(request, output_dir)
        finally:
            self.active -= 1


def _provider(quality: str, *, renderer_id: str) -> FakeProvider:
    return FakeProvider(
        RendererDescriptor(
            renderer_id=renderer_id,
            renderer_version="1",
            font_digest="f" * 64,
            quality=quality,  # type: ignore[arg-type]
        )
    )


def _set_gates(monkeypatch: pytest.MonkeyPatch, *, authoring: bool) -> None:
    monkeypatch.setattr(release_features, "V11_OFFICE_V2_RELEASED", True)
    for name in (
        "V11_CHECKPOINTS_RELEASED",
        "V11_REWIND_RELEASED",
        "V11_VALIDATION_AGENT_RELEASED",
    ):
        monkeypatch.setattr(release_features, name, authoring)


@pytest.mark.asyncio
async def test_closed_preview_gate_clears_stale_state_without_loading_renderer(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release_features, "V11_OFFICE_V2_RELEASED", False)
    state = SimpleNamespace(office_preview_service=object())
    set_office_precommit_coordinator(_StaleCoordinator())

    def explode():
        raise AssertionError("closed gate loaded a renderer")

    monkeypatch.setattr(
        "app.office_validation.startup.build_local_office_render_provider",
        explode,
    )
    result = await initialize_office_v11_runtime(
        state,
        session_factory,
        data_dir=tmp_path,
    )

    assert result.preview_installed is False
    assert result.authoring_installed is False
    assert not hasattr(state, "office_preview_service")
    assert get_office_precommit_coordinator() is None


@pytest.mark.asyncio
async def test_preview_only_gate_never_probes_authoritative_deployment(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    _frozen_release_identity: AsyncMock,
    _native_sandbox_behavior: SimpleNamespace,
) -> None:
    _set_gates(monkeypatch, authoring=False)
    approximate = _provider("approximate", renderer_id="preview")
    monkeypatch.setattr(
        "app.office_validation.startup.build_local_office_render_provider",
        lambda: approximate,
    )
    monkeypatch.setattr(
        "app.office_validation.startup.build_attested_office_render_provider",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("preview-only gate probed release deployment")
        ),
    )
    state = SimpleNamespace()

    result = await initialize_office_v11_runtime(
        state,
        session_factory,
        data_dir=tmp_path,
    )

    assert result.preview_installed is True
    assert result.authoring_installed is False
    assert state.office_preview_service.provider is approximate
    assert get_office_precommit_coordinator() is None
    _native_sandbox_behavior.binder.assert_not_called()
    _native_sandbox_behavior.probe.assert_not_awaited()
    _frozen_release_identity.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_attestation_keeps_approximate_preview_fail_closed(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    _frozen_release_identity: AsyncMock,
    _native_sandbox_behavior: SimpleNamespace,
) -> None:
    _set_gates(monkeypatch, authoring=True)
    approximate = _provider("approximate", renderer_id="preview")
    unavailable = _provider("approximate", renderer_id="not-attested")
    monkeypatch.setattr(
        "app.office_validation.startup.build_local_office_render_provider",
        lambda: approximate,
    )
    monkeypatch.setattr(
        "app.office_validation.startup.build_attested_office_render_provider",
        lambda **_kwargs: unavailable,
    )
    state = SimpleNamespace()

    result = await initialize_office_v11_runtime(
        state,
        session_factory,
        data_dir=tmp_path,
    )

    assert result.renderer_quality == "approximate"
    assert result.authoring_installed is False
    assert state.office_preview_service.provider is approximate
    assert not hasattr(state, "office_v11_runtime")
    assert get_office_precommit_coordinator() is None
    _native_sandbox_behavior.binder.assert_not_called()
    _native_sandbox_behavior.probe.assert_not_awaited()
    _frozen_release_identity.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_real_renderer_probe_never_installs_authoring(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    _frozen_release_identity: AsyncMock,
    _native_sandbox_behavior: SimpleNamespace,
) -> None:
    _set_gates(monkeypatch, authoring=True)
    approximate = _provider("approximate", renderer_id="preview")
    authoritative = _provider("authoritative", renderer_id="attested")
    monkeypatch.setattr(
        "app.office_validation.startup.build_local_office_render_provider",
        lambda: approximate,
    )
    monkeypatch.setattr(
        "app.office_validation.startup.build_attested_office_render_provider",
        lambda **_kwargs: authoritative,
    )
    events: list[str] = []

    def bind_contract(provider):
        assert provider is authoritative
        events.append("bind")
        return _native_sandbox_behavior.contract

    async def prove_behavior(contract):
        assert contract is _native_sandbox_behavior.contract
        events.append("behavior")
        return object()

    async def fail_golden(provider):
        assert provider is authoritative
        events.append("golden")
        raise RuntimeError("private /Users/alice/renderer probe failed")

    _native_sandbox_behavior.binder.side_effect = bind_contract
    _native_sandbox_behavior.probe.side_effect = prove_behavior
    _frozen_release_identity.side_effect = fail_golden
    state = SimpleNamespace()

    result = await initialize_office_v11_runtime(
        state,
        session_factory,
        data_dir=tmp_path,
    )

    assert result.preview_installed is True
    assert result.authoring_installed is False
    assert result.renderer_quality == "approximate"
    assert state.office_preview_service.provider is approximate
    assert not hasattr(state, "office_v11_runtime")
    assert get_office_precommit_coordinator() is None
    assert events == ["bind", "behavior", "golden"]
    _native_sandbox_behavior.binder.assert_called_once_with(authoritative)
    _native_sandbox_behavior.probe.assert_awaited_once_with(
        _native_sandbox_behavior.contract
    )
    _frozen_release_identity.assert_awaited_once_with(authoritative)


@pytest.mark.asyncio
async def test_failed_native_sandbox_behavior_never_runs_golden_or_installs_authoring(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    _frozen_release_identity: AsyncMock,
    _native_sandbox_behavior: SimpleNamespace,
) -> None:
    _set_gates(monkeypatch, authoring=True)
    approximate = _provider("approximate", renderer_id="preview")
    authoritative = _provider("authoritative", renderer_id="attested")
    monkeypatch.setattr(
        "app.office_validation.startup.build_local_office_render_provider",
        lambda: approximate,
    )
    monkeypatch.setattr(
        "app.office_validation.startup.build_attested_office_render_provider",
        lambda **_kwargs: authoritative,
    )
    _native_sandbox_behavior.probe.side_effect = RuntimeError(
        "private /Users/alice/native-sandbox-helper"
    )
    state = SimpleNamespace()

    result = await initialize_office_v11_runtime(
        state,
        session_factory,
        data_dir=tmp_path,
    )

    assert result.preview_installed is True
    assert result.authoring_installed is False
    assert result.renderer_quality == "approximate"
    assert "Users" not in repr(result)
    assert state.office_preview_service.provider is approximate
    assert not hasattr(state, "office_v11_runtime")
    assert get_office_precommit_coordinator() is None
    _native_sandbox_behavior.binder.assert_called_once_with(authoritative)
    _native_sandbox_behavior.probe.assert_awaited_once_with(
        _native_sandbox_behavior.contract
    )
    _frozen_release_identity.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_frozen_release_identity_never_loads_authoritative_renderer(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    _native_sandbox_behavior: SimpleNamespace,
) -> None:
    _set_gates(monkeypatch, authoring=True)
    approximate = _provider("approximate", renderer_id="preview")
    monkeypatch.setattr(
        "app.office_validation.startup.build_local_office_render_provider",
        lambda: approximate,
    )
    monkeypatch.setattr(
        "app.office_validation.startup.load_frozen_renderer_release_identity",
        lambda: (_ for _ in ()).throw(RuntimeError("source process")),
    )
    monkeypatch.setattr(
        "app.office_validation.startup.build_attested_office_render_provider",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("identity failure still loaded a renderer")
        ),
    )
    state = SimpleNamespace()

    result = await initialize_office_v11_runtime(
        state,
        session_factory,
        data_dir=tmp_path,
    )

    assert result.preview_installed is True
    assert result.authoring_installed is False
    assert result.renderer_quality == "approximate"
    assert state.office_preview_service.provider is approximate
    assert not hasattr(state, "office_v11_runtime")
    assert get_office_precommit_coordinator() is None
    _native_sandbox_behavior.binder.assert_not_called()
    _native_sandbox_behavior.probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_attested_renderer_and_policy_install_one_shared_runtime(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    _frozen_release_identity: AsyncMock,
    _native_sandbox_behavior: SimpleNamespace,
) -> None:
    _set_gates(monkeypatch, authoring=True)
    approximate = _provider("approximate", renderer_id="preview")
    authoritative = _provider("authoritative", renderer_id="attested")
    monkeypatch.setattr(
        "app.office_validation.startup.build_local_office_render_provider",
        lambda: approximate,
    )
    monkeypatch.setattr(
        "app.office_validation.startup.build_attested_office_render_provider",
        lambda **_kwargs: authoritative,
    )
    monkeypatch.setattr(
        "app.office_validation.startup.FirstPartyOfficePrecommitPolicyResolver",
        lambda **_kwargs: _Policies(),
    )
    state = SimpleNamespace()

    result = await initialize_office_v11_runtime(
        state,
        session_factory,
        data_dir=tmp_path,
    )

    assert result.preview_installed is True
    assert result.authoring_installed is True
    assert result.renderer_quality == "authoritative"
    assert state.office_preview_service is state.office_v11_runtime.preview
    admitted = state.office_v11_runtime.provider
    assert isinstance(admitted, AdmissionControlledOfficeRenderProvider)
    assert admitted.delegate is authoritative
    assert state.office_preview_service.provider is admitted
    assert state.office_v11_runtime.draft._provider is admitted
    assert (
        state.office_precommit_coordinator
        is state.office_v11_runtime.coordinator
        is get_office_precommit_coordinator()
    )
    _native_sandbox_behavior.binder.assert_called_once_with(authoritative)
    _native_sandbox_behavior.probe.assert_awaited_once_with(
        _native_sandbox_behavior.contract
    )
    _frozen_release_identity.assert_awaited_once_with(authoritative)

    uninstall_office_v11_runtime(state)
    assert get_office_precommit_coordinator() is None


@pytest.mark.asyncio
async def test_preview_and_precommit_share_one_process_render_limit(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_gates(monkeypatch, authoring=True)
    approximate = _provider("approximate", renderer_id="preview")
    authoritative = _TrackingProvider(
        RendererDescriptor(
            renderer_id="attested",
            renderer_version="1",
            font_digest="f" * 64,
            quality="authoritative",
        )
    )
    monkeypatch.setattr(
        "app.office_validation.startup.build_local_office_render_provider",
        lambda: approximate,
    )
    monkeypatch.setattr(
        "app.office_validation.startup.build_attested_office_render_provider",
        lambda **_kwargs: authoritative,
    )
    monkeypatch.setattr(
        "app.office_validation.startup.FirstPartyOfficePrecommitPolicyResolver",
        lambda **_kwargs: _Policies(),
    )
    state = SimpleNamespace()
    result = await initialize_office_v11_runtime(
        state,
        session_factory,
        data_dir=tmp_path,
    )
    assert result.authoring_installed is True

    preview_provider = state.office_v11_runtime.preview.provider
    precommit_provider = state.office_v11_runtime.draft._provider
    preview_request = make_request(tmp_path / "preview-workspace")
    precommit_request = make_request(tmp_path / "precommit-workspace")
    preview_output = tmp_path / "preview-output"
    precommit_output = tmp_path / "precommit-output"
    preview_output.mkdir()
    precommit_output.mkdir()

    await asyncio.gather(
        preview_provider.render(preview_request, preview_output),
        precommit_provider.render(precommit_request, precommit_output),
    )

    assert preview_provider is precommit_provider
    assert authoritative.calls == 2
    assert authoritative.max_active == 1

    uninstall_office_v11_runtime(state)


@pytest.mark.asyncio
async def test_policy_failure_restores_preview_without_commit_authority(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_gates(monkeypatch, authoring=True)
    approximate = _provider("approximate", renderer_id="preview")
    authoritative = _provider("authoritative", renderer_id="attested")
    monkeypatch.setattr(
        "app.office_validation.startup.build_local_office_render_provider",
        lambda: approximate,
    )
    monkeypatch.setattr(
        "app.office_validation.startup.build_attested_office_render_provider",
        lambda **_kwargs: authoritative,
    )

    def reject_policy(**_kwargs):
        raise RuntimeError("policy unavailable")

    monkeypatch.setattr(
        "app.office_validation.startup.FirstPartyOfficePrecommitPolicyResolver",
        reject_policy,
    )
    state = SimpleNamespace()

    result = await initialize_office_v11_runtime(
        state,
        session_factory,
        data_dir=tmp_path,
    )

    assert result.preview_installed is True
    assert result.authoring_installed is False
    assert state.office_preview_service.provider is approximate
    assert not hasattr(state, "office_v11_runtime")
    assert get_office_precommit_coordinator() is None
