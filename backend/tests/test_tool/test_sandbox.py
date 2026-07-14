"""Platform policy construction tests for the mandatory execution sandbox."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.tool import sandbox


def test_linux_policy_uses_minimal_root_and_read_only_parent_dirs(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(
        sandbox.shutil,
        "which",
        lambda name: "/usr/bin/bwrap" if name == "bwrap" else f"/usr/bin/{name}",
    )
    scratch = workspace / "scratch"
    scratch.mkdir()

    launch = sandbox.prepare_sandbox_launch(
        ["/bin/bash", "-c", "echo ok"],
        workspace=str(workspace),
        cwd=str(workspace),
        scratch_dir=scratch,
    )

    pairs = list(zip(launch.argv, launch.argv[1:]))
    assert ("--ro-bind", "/") not in pairs
    assert "/run" not in launch.argv
    assert "/var" not in launch.argv
    assert ("--ro-bind", "/opt") not in pairs
    assert "--unshare-all" in launch.argv
    assert "--die-with-parent" in launch.argv
    assert "--new-session" in launch.argv
    assert ["--remount-ro", "/"] == launch.argv[
        launch.argv.index("--remount-ro"):launch.argv.index("--remount-ro") + 2
    ]
    workspace_index = launch.argv.index("--bind")
    assert launch.argv[workspace_index + 1:workspace_index + 3] == [
        str(workspace.resolve()),
        str(workspace.resolve()),
    ]


def test_linux_without_bubblewrap_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: None)
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    with pytest.raises(sandbox.SandboxUnavailable, match="bubblewrap"):
        sandbox.prepare_sandbox_launch(
            ["/bin/sh", "-c", "true"],
            workspace=str(tmp_path),
            cwd=str(tmp_path),
            scratch_dir=scratch,
        )


def test_windows_fails_closed_instead_of_using_job_as_a_sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox.sys, "platform", "win32")

    with pytest.raises(sandbox.SandboxUnavailable, match="AppContainer"):
        sandbox.prepare_sandbox_launch(
            ["powershell.exe", "-Command", "Write-Output unsafe"],
            workspace=str(tmp_path),
            cwd=str(tmp_path),
            scratch_dir=tmp_path / "scratch",
        )


def test_macos_fails_closed_without_a_detached_process_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox.sys, "platform", "darwin")

    with pytest.raises(sandbox.SandboxUnavailable, match="detached-process"):
        sandbox.prepare_sandbox_launch(
            ["/bin/sh", "-c", "true"],
            workspace=str(tmp_path),
            cwd=str(tmp_path),
            scratch_dir=tmp_path / "scratch",
        )

    assert not (tmp_path / "scratch").exists()


def test_scratch_creation_rejects_preexisting_symlink_without_external_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    (workspace / ".suxiaoyou").mkdir(parents=True)
    external.mkdir()
    (workspace / ".suxiaoyou" / "sandbox").symlink_to(
        external,
        target_is_directory=True,
    )
    monkeypatch.setattr(sandbox.sys, "platform", "linux")

    with pytest.raises(sandbox.SandboxUnavailable, match="symlink|non-directory"):
        sandbox.create_sandbox_scratch(workspace, prefix="probe-")

    assert list(external.iterdir()) == []


def test_scratch_creation_stays_inside_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(sandbox.sys, "platform", "linux")

    scratch = sandbox.create_sandbox_scratch(workspace, prefix="probe-")

    assert scratch.is_dir()
    assert scratch.parent == workspace / ".suxiaoyou" / "sandbox"


def test_sanitized_environment_drops_backend_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("SUXIAOYOU_OPENROUTER_API_KEY", "secret")
    monkeypatch.setenv(sandbox.APP_PRIVATE_DIR_ENV, str(tmp_path / "private"))
    env = sandbox._sanitized_environment(Path(tmp_path))
    assert "SUXIAOYOU_OPENROUTER_API_KEY" not in env
    assert sandbox.APP_PRIVATE_DIR_ENV not in env
    assert env["HOME"].startswith(str(tmp_path))


def test_workspace_containing_app_private_root_is_rejected_before_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_root = tmp_path / "app-private"
    private_root.mkdir()
    monkeypatch.setenv(sandbox.APP_PRIVATE_DIR_ENV, str(private_root))
    scratch = tmp_path / "scratch"

    with pytest.raises(sandbox.SandboxUnavailable, match="application-private"):
        sandbox.prepare_sandbox_launch(
            ["/bin/sh", "-c", "true"],
            workspace=str(tmp_path),
            cwd=str(tmp_path),
            scratch_dir=scratch,
        )

    assert not scratch.exists()


@pytest.mark.parametrize("selection", ["private", "private-child"])
def test_workspace_inside_app_private_root_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selection: str,
) -> None:
    private_root = tmp_path / "app-private"
    private_root.mkdir()
    workspace = private_root
    if selection == "private-child":
        workspace = private_root / "selected-child"
        workspace.mkdir()
    monkeypatch.setenv(sandbox.APP_PRIVATE_DIR_ENV, str(private_root))

    with pytest.raises(sandbox.SandboxUnavailable, match="application-private"):
        sandbox.validate_workspace_private_boundary(workspace)


def test_separate_workspace_passes_private_boundary_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_root = tmp_path / "app-private"
    workspace = tmp_path / "workspace"
    private_root.mkdir()
    workspace.mkdir()
    monkeypatch.setenv(sandbox.APP_PRIVATE_DIR_ENV, str(private_root))

    assert sandbox.validate_workspace_private_boundary(workspace) == workspace.resolve()
