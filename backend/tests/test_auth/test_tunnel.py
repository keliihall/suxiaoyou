"""Lifecycle regression tests for the Cloudflare quick-tunnel manager."""

from __future__ import annotations

import asyncio
import io
import subprocess
from pathlib import Path

import pytest

from app.auth.tunnel import TunnelManager, _DOWNLOAD_URLS


pytestmark = pytest.mark.asyncio


class _FakeProcess:
    def __init__(self, output: str) -> None:
        self.stdout = io.StringIO(output)
        self.returncode: int | None = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.returncode = -15

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.wait_calls += 1
        if self.returncode is None:
            raise subprocess.TimeoutExpired("cloudflared", 5)
        return self.returncode


async def test_monitor_restart_does_not_cancel_itself(monkeypatch) -> None:
    processes = [
        _FakeProcess("https://first.trycloudflare.com\n"),
        _FakeProcess("https://second.trycloudflare.com\n"),
    ]
    created: list[_FakeProcess] = []
    changes: list[tuple[str | None, str | None]] = []
    replacement_ready = asyncio.Event()
    original_sleep = asyncio.sleep

    def fake_popen(*args, **kwargs):
        del args, kwargs
        process = processes[len(created)]
        created.append(process)
        return process

    async def fast_sleep(_delay: float) -> None:
        await original_sleep(0)

    def record_change(old: str | None, new: str | None) -> None:
        changes.append((old, new))
        if new == "https://second.trycloudflare.com":
            replacement_ready.set()

    monkeypatch.setattr("app.auth.tunnel.subprocess.Popen", fake_popen)
    monkeypatch.setattr("app.auth.tunnel.shutil.which", lambda _name: "/fake/cloudflared")
    monkeypatch.setattr("app.auth.tunnel.asyncio.sleep", fast_sleep)

    manager = TunnelManager(on_url_change=record_change)
    assert await manager.start() == "https://first.trycloudflare.com"
    first_monitor = manager._monitor_task
    assert first_monitor is not None

    processes[0].returncode = 1
    await asyncio.wait_for(replacement_ready.wait(), timeout=5)

    assert len(created) == 2
    assert manager.tunnel_url == "https://second.trycloudflare.com"
    assert manager._monitor_task is not None
    assert manager._monitor_task is not first_monitor
    assert changes[:2] == [
        (None, "https://first.trycloudflare.com"),
        (
            "https://first.trycloudflare.com",
            "https://second.trycloudflare.com",
        ),
    ]

    await manager.stop()
    assert manager._monitor_task is None
    assert manager.tunnel_url is None
    assert changes[-1] == ("https://second.trycloudflare.com", None)


async def test_failed_replacement_reaps_process_and_removes_stale_url(
    monkeypatch,
) -> None:
    process = _FakeProcess("")
    changes: list[tuple[str | None, str | None]] = []

    monkeypatch.setattr("app.auth.tunnel.subprocess.Popen", lambda *a, **k: process)
    monkeypatch.setattr("app.auth.tunnel.shutil.which", lambda _name: "/fake/cloudflared")

    manager = TunnelManager(on_url_change=lambda old, new: changes.append((old, new)))
    manager._set_tunnel_url("https://stale.trycloudflare.com")

    async def fail_to_read(_process) -> str:
        raise TimeoutError("no URL")

    monkeypatch.setattr(manager, "_read_tunnel_url", fail_to_read)

    with pytest.raises(TimeoutError, match="no URL"):
        await manager.start()

    assert manager._process is None
    assert manager.tunnel_url is None
    assert process.terminate_calls == 1
    assert process.wait_calls >= 1
    assert changes[-1] == ("https://stale.trycloudflare.com", None)


async def test_repeated_start_reuses_healthy_tunnel(monkeypatch) -> None:
    process = _FakeProcess("https://once.trycloudflare.com\n")
    popen_calls = 0

    def fake_popen(*args, **kwargs):
        nonlocal popen_calls
        del args, kwargs
        popen_calls += 1
        return process

    monkeypatch.setattr("app.auth.tunnel.subprocess.Popen", fake_popen)
    monkeypatch.setattr("app.auth.tunnel.shutil.which", lambda _name: "/fake/cloudflared")

    manager = TunnelManager(bin_dir=Path("unused"))
    first = await manager.start()
    second = await manager.start()

    assert first == second == "https://once.trycloudflare.com"
    assert popen_calls == 1
    await manager.stop()


async def test_arm64_downloads_use_only_official_cloudflared_assets(
    monkeypatch,
    tmp_path: Path,
) -> None:
    linux_url = (
        "https://github.com/cloudflare/cloudflared/releases/latest/download/"
        "cloudflared-linux-arm64"
    )
    darwin_url = (
        "https://github.com/cloudflare/cloudflared/releases/latest/download/"
        "cloudflared-darwin-arm64.tgz"
    )
    assert _DOWNLOAD_URLS[("Linux", "aarch64")] == linux_url
    assert _DOWNLOAD_URLS[("Linux", "arm64")] == linux_url
    assert _DOWNLOAD_URLS[("Darwin", "aarch64")] == darwin_url
    assert _DOWNLOAD_URLS[("Darwin", "arm64")] == darwin_url

    # Cloudflare's official release currently has no Windows ARM64 binary.
    # Do not silently download the amd64 build into a native ARM64 package.
    assert ("Windows", "ARM64") not in _DOWNLOAD_URLS
    monkeypatch.setattr("app.auth.tunnel.platform.system", lambda: "Windows")
    monkeypatch.setattr("app.auth.tunnel.platform.machine", lambda: "ARM64")
    manager = TunnelManager(bin_dir=tmp_path)
    with pytest.raises(RuntimeError, match=r"Windows/ARM64"):
        await manager._download(tmp_path / "cloudflared.exe")
