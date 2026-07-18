from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.office_rendering import (
    AdmissionControlledOfficeRenderProvider,
    OFFICE_RENDERING_DEFAULT_ENABLED,
    OfficeRenderProvider,
    ProviderAvailability,
    ProviderUnavailableError,
    RenderContractError,
    UnavailableOfficeRenderProvider,
)
from tests.test_office_rendering.helpers import FakeProvider, make_request


class _BlockingProvider(FakeProvider):
    def __init__(self, descriptor) -> None:
        super().__init__(descriptor)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.active = 0
        self.max_active = 0

    async def render(self, request, output_dir):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.entered.set()
        try:
            await self.release.wait()
            return await super().render(request, output_dir)
        finally:
            self.active -= 1


def test_unavailable_placeholder_is_explicit_and_never_authoritative() -> None:
    provider = UnavailableOfficeRenderProvider("renderer binary was not installed")

    assert isinstance(provider, OfficeRenderProvider)
    assert provider.availability() == ProviderAvailability(
        available=False,
        reason="renderer binary was not installed",
    )
    assert provider.descriptor.quality == "approximate"
    assert OFFICE_RENDERING_DEFAULT_ENABLED is False


@pytest.mark.asyncio
async def test_unavailable_placeholder_render_fails(tmp_path: Path) -> None:
    provider = UnavailableOfficeRenderProvider("renderer unavailable")
    request = make_request(tmp_path / "workspace")

    with pytest.raises(ProviderUnavailableError, match="unavailable"):
        await provider.render(request, tmp_path / "output")


def test_availability_requires_an_explicit_reason_when_unavailable() -> None:
    with pytest.raises(RenderContractError, match="requires a reason"):
        ProviderAvailability(available=False)
    with pytest.raises(RenderContractError, match="cannot carry"):
        ProviderAvailability(available=True, reason="contradiction")


def test_admission_wrapper_delegates_identity_and_availability() -> None:
    delegate = UnavailableOfficeRenderProvider("signed renderer is unavailable")
    provider = AdmissionControlledOfficeRenderProvider(delegate)

    assert isinstance(provider, OfficeRenderProvider)
    assert provider.delegate is delegate
    assert provider.descriptor is delegate.descriptor
    assert provider.availability() is delegate.availability()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_concurrent_renders": True}, "concurrency"),
        ({"max_concurrent_renders": 0}, "concurrency"),
        ({"max_concurrent_renders": 9}, "concurrency"),
        ({"admission_timeout_seconds": True}, "timeout"),
        ({"admission_timeout_seconds": 0}, "timeout"),
        ({"admission_timeout_seconds": 31}, "timeout"),
    ],
)
def test_admission_wrapper_rejects_unsafe_limits(kwargs, message: str) -> None:
    delegate = UnavailableOfficeRenderProvider()

    with pytest.raises(ValueError, match=message):
        AdmissionControlledOfficeRenderProvider(delegate, **kwargs)


@pytest.mark.asyncio
async def test_admission_timeout_does_not_leak_a_render_slot(tmp_path: Path) -> None:
    request = make_request(tmp_path / "workspace")
    delegate = _BlockingProvider(
        UnavailableOfficeRenderProvider().descriptor
    )
    provider = AdmissionControlledOfficeRenderProvider(
        delegate,
        admission_timeout_seconds=0.02,
    )
    first_output = tmp_path / "first"
    second_output = tmp_path / "second"
    third_output = tmp_path / "third"
    for output in (first_output, second_output, third_output):
        output.mkdir()

    holder = asyncio.create_task(provider.render(request, first_output))
    await delegate.entered.wait()
    with pytest.raises(ProviderUnavailableError, match="admission timed out"):
        await provider.render(request, second_output)

    delegate.release.set()
    await holder
    await provider.render(request, third_output)

    assert delegate.calls == 2
    assert delegate.max_active == 1


@pytest.mark.asyncio
async def test_queued_cancellation_does_not_leak_a_render_slot(tmp_path: Path) -> None:
    request = make_request(tmp_path / "workspace")
    delegate = _BlockingProvider(
        UnavailableOfficeRenderProvider().descriptor
    )
    provider = AdmissionControlledOfficeRenderProvider(
        delegate,
        admission_timeout_seconds=1,
    )
    first_output = tmp_path / "first"
    queued_output = tmp_path / "queued"
    third_output = tmp_path / "third"
    for output in (first_output, queued_output, third_output):
        output.mkdir()

    holder = asyncio.create_task(provider.render(request, first_output))
    await delegate.entered.wait()
    queued = asyncio.create_task(provider.render(request, queued_output))
    await asyncio.sleep(0)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued

    delegate.release.set()
    await holder
    await provider.render(request, third_output)

    assert delegate.calls == 2
    assert delegate.max_active == 1


@pytest.mark.asyncio
async def test_active_cancellation_releases_its_render_slot(tmp_path: Path) -> None:
    request = make_request(tmp_path / "workspace")
    delegate = _BlockingProvider(
        UnavailableOfficeRenderProvider().descriptor
    )
    provider = AdmissionControlledOfficeRenderProvider(
        delegate,
        admission_timeout_seconds=0.1,
    )
    first_output = tmp_path / "first"
    next_output = tmp_path / "next"
    first_output.mkdir()
    next_output.mkdir()

    active = asyncio.create_task(provider.render(request, first_output))
    await delegate.entered.wait()
    active.cancel()
    with pytest.raises(asyncio.CancelledError):
        await active

    delegate.release.set()
    await provider.render(request, next_output)

    assert delegate.calls == 1
    assert delegate.max_active == 1
