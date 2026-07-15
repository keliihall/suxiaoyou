"""Platform policy construction tests for the mandatory execution sandbox."""

from __future__ import annotations

import os
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


def test_linux_policy_mounts_private_stage_at_logical_workspace_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    staged = tmp_path / "private" / "stage"
    scratch = staged / ".suxiaoyou" / "sandbox" / "call"
    cwd = workspace / "suxiaoyou_written"
    workspace.mkdir()
    scratch.mkdir(parents=True)
    (staged / "suxiaoyou_written").mkdir()
    environment = (
        workspace / ".suxiaoyou" / "execution-environments" / "session-key"
    )
    (environment / "home").mkdir(parents=True)
    (environment / "cache").mkdir()
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(
        sandbox.shutil,
        "which",
        lambda name: "/usr/bin/bwrap" if name == "bwrap" else f"/usr/bin/{name}",
    )

    launch = sandbox.prepare_sandbox_launch(
        ["/bin/bash", "-c", "pwd"],
        workspace=str(workspace),
        workspace_source=staged,
        cwd=str(cwd),
        scratch_dir=scratch,
        persistent_environment=environment,
    )

    bind_index = launch.argv.index("--bind")
    assert launch.argv[bind_index + 1:bind_index + 3] == [
        str(staged.resolve()),
        str(workspace.resolve()),
    ]
    chdir_index = launch.argv.index("--chdir")
    assert launch.argv[chdir_index + 1] == str(cwd)
    home_index = launch.argv.index("HOME")
    assert launch.argv[home_index + 1] == str(environment / "home")
    assert str(staged) not in launch.argv[home_index + 1]
    environment_bind = [
        index
        for index, value in enumerate(launch.argv)
        if value == "--bind" and launch.argv[index + 1] == str(environment)
    ]
    assert len(environment_bind) == 1
    assert launch.argv[environment_bind[0] + 2] == str(environment)
    assert launch.metadata["execution_environment_scope"] == "session"
    assert launch.metadata["home_persistent"] is True
    assert launch.cwd == str(workspace)


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


def test_linux_policy_can_share_only_the_network_namespace(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    scratch = workspace / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(
        sandbox.shutil,
        "which",
        lambda name: "/usr/bin/bwrap" if name == "bwrap" else f"/usr/bin/{name}",
    )

    launch = sandbox.prepare_sandbox_launch(
        ["/bin/sh", "-c", "curl https://example.com"],
        workspace=str(workspace),
        cwd=str(workspace),
        scratch_dir=scratch,
        allow_network=True,
    )

    assert "--unshare-all" in launch.argv
    assert "--share-net" in launch.argv
    assert "--die-with-parent" in launch.argv
    assert launch.filesystem_isolated is True
    assert launch.network_isolated is False


def test_windows_uses_sanitized_workspace_launch_and_reports_native_access(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(sandbox.sys, "platform", "win32")
    workspace = tmp_path / "workspace"
    cwd = workspace / "output"
    scratch = workspace / ".suxiaoyou" / "sandbox" / "call"
    cwd.mkdir(parents=True)
    scratch.mkdir(parents=True)
    environment = (
        workspace / ".suxiaoyou" / "execution-environments" / "session-key"
    )
    environment.mkdir(parents=True)

    launch = sandbox.prepare_sandbox_launch(
        [
            r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            "-Command",
            "Write-Output ok",
        ],
        workspace=str(workspace),
        cwd=str(cwd),
        scratch_dir=scratch,
        persistent_environment=environment,
        allow_network=True,
    )

    assert launch.backend == "windows-job-object"
    assert launch.cwd == str(cwd)
    assert launch.env["HOME"] == str(environment / "home")
    assert str(environment / "home" / ".local" / "Scripts") in launch.env["PATH"]
    assert launch.env["SUXIAOYOU_WORKSPACE"] == str(workspace)
    assert launch.filesystem_isolated is False
    assert launch.network_isolated is False
    assert launch.metadata["process_tree_isolated"] is True


def test_macos_without_seatbelt_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: None)

    with pytest.raises(sandbox.SandboxUnavailable, match="sandbox-exec"):
        sandbox.prepare_sandbox_launch(
            ["/bin/sh", "-c", "true"],
            workspace=str(tmp_path),
            cwd=str(tmp_path),
            scratch_dir=tmp_path / "scratch",
        )

    assert not (tmp_path / "scratch").exists()


@pytest.mark.parametrize("allow_network", [False, True])
def test_macos_policy_uses_seatbelt_and_private_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    allow_network: bool,
) -> None:
    workspace = tmp_path / "logical workspace"
    # A quote is the strongest Seatbelt source-injection probe, but Windows
    # cannot create a filename containing one.  Keep the policy-construction
    # test portable there with a parenthesis-based probe; macOS and Linux still
    # exercise the quote case.
    hostile_component = (
        "private ) (allow default)"
        if os.name == "nt"
        else 'private ) " (allow default)'
    )
    staged = tmp_path / hostile_component / "stage"
    cwd = workspace / "suxiaoyou_written"
    staged_cwd = staged / "suxiaoyou_written"
    scratch = staged / ".suxiaoyou" / "sandbox" / "call"
    workspace.mkdir()
    staged_cwd.mkdir(parents=True)
    scratch.mkdir(parents=True)
    environment = (
        workspace / ".suxiaoyou" / "execution-environments" / "session-key"
    )
    environment.mkdir(parents=True)
    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    monkeypatch.setattr(
        sandbox.shutil,
        "which",
        lambda name: "/usr/bin/sandbox-exec"
        if name == "sandbox-exec"
        else (name if Path(name).is_absolute() else f"/usr/bin/{name}"),
    )

    logical_output = workspace / "suxiaoyou_written" / "result.txt"
    launch = sandbox.prepare_sandbox_launch(
        ["/bin/sh", "-c", f"printf ok > {logical_output}"],
        workspace=str(workspace),
        workspace_source=staged,
        cwd=str(cwd),
        scratch_dir=scratch,
        persistent_environment=environment,
        allow_network=allow_network,
    )

    profile = launch.argv[launch.argv.index("-p") + 1]
    command = launch.argv[-1]
    assert launch.argv[0] == "/usr/bin/sandbox-exec"
    assert "SYS_setsid" in profile
    assert "SYS_setpgid" in profile
    assert '(subpath (param "WORKSPACE"))' in profile
    assert '(subpath (param "ENVIRONMENT"))' in profile
    # User-controlled paths are values of distinct ``-D`` argv entries, never
    # interpolated into the Seatbelt source where quotes/parentheses are syntax.
    assert str(staged.resolve()) not in profile
    assert f"WORKSPACE={staged.resolve()}" in launch.argv
    assert ("(system-network)" in profile) is allow_network
    assert ("(remote ip)" in profile) is allow_network
    assert str(staged / "suxiaoyou_written" / "result.txt") in command
    assert str(workspace) not in command
    assert launch.cwd == str(staged_cwd)
    assert launch.env["HOME"] == str(environment / "home")
    assert f"ENVIRONMENT={environment.resolve()}" in launch.argv
    assert str(environment / "home" / ".local" / "bin") in launch.env["PATH"]
    assert launch.env["SUXIAOYOU_WORKSPACE"] == str(staged)
    assert launch.backend == "macos-seatbelt"
    assert launch.filesystem_isolated is True
    assert launch.network_isolated is (not allow_network)


def test_macos_policy_reads_xcode_select_symlink_and_resolved_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    staged = tmp_path / "private" / "stage"
    scratch = staged / ".suxiaoyou" / "sandbox" / "call"
    logical = tmp_path / "var" / "db" / "xcode_select_link"
    developer = tmp_path / "Applications" / "Xcode.app" / "Contents" / "Developer"
    info_plist = developer.parent / "Info.plist"
    workspace.mkdir()
    scratch.mkdir(parents=True)
    developer.mkdir(parents=True)
    info_plist.write_text("xcode metadata", encoding="utf-8")
    logical.parent.mkdir(parents=True)
    logical.symlink_to(developer, target_is_directory=True)
    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    monkeypatch.setattr(sandbox, "_MACOS_XCODE_SELECT_LINK", logical)
    monkeypatch.setattr(sandbox, "_MACOS_SYSTEM_READ_ROOTS", (logical,))
    monkeypatch.setattr(sandbox, "_MACOS_LOGICAL_READ_PATHS", (logical,))
    monkeypatch.setattr(
        sandbox.shutil,
        "which",
        lambda name: "/usr/bin/sandbox-exec"
        if name == "sandbox-exec"
        else (name if Path(name).is_absolute() else f"/usr/bin/{name}"),
    )

    launch = sandbox.prepare_sandbox_launch(
        ["/usr/bin/python3", "--version"],
        workspace=str(workspace),
        workspace_source=staged,
        cwd=str(workspace),
        scratch_dir=scratch,
    )

    profile = launch.argv[launch.argv.index("-p") + 1]
    assert any(value == f"LITERAL_READ_0={logical.absolute()}" for value in launch.argv)
    assert any(
        value.startswith("LITERAL_READ_") and value.endswith(f"={info_plist}")
        for value in launch.argv
    )
    assert any(
        value.startswith("READ_ROOT_") and value.endswith(f"={developer.resolve()}")
        for value in launch.argv
    )
    assert '(literal (param "LITERAL_READ_0"))' in profile
    read_root_values = {
        value.split("=", 1)[1]
        for value in launch.argv
        if value.startswith("READ_ROOT_")
    }
    assert str(developer.resolve()) in read_root_values
    assert str(developer.parent) in read_root_values
    assert str(developer.parent.parent) not in read_root_values
    assert str(developer.parents[2]) not in read_root_values


def test_macos_xcode_metadata_requires_full_xcode_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command_line_tools = tmp_path / "Library" / "Developer" / "CommandLineTools"
    logical = tmp_path / "var" / "db" / "xcode_select_link"
    command_line_tools.mkdir(parents=True)
    (command_line_tools / "Info.plist").write_text("metadata", encoding="utf-8")
    logical.parent.mkdir(parents=True)
    logical.symlink_to(command_line_tools, target_is_directory=True)
    monkeypatch.setattr(sandbox, "_MACOS_XCODE_SELECT_LINK", logical)

    assert sandbox._selected_xcode_metadata_paths() == []
    assert sandbox._selected_xcode_runtime_roots() == []


def test_macos_xcode_metadata_requires_app_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    developer = tmp_path / "Applications" / "Xcode" / "Contents" / "Developer"
    logical = tmp_path / "var" / "db" / "xcode_select_link"
    developer.mkdir(parents=True)
    (developer.parent / "Info.plist").write_text("metadata", encoding="utf-8")
    logical.parent.mkdir(parents=True)
    logical.symlink_to(developer, target_is_directory=True)
    monkeypatch.setattr(sandbox, "_MACOS_XCODE_SELECT_LINK", logical)

    assert sandbox._selected_xcode_metadata_paths() == []
    assert sandbox._selected_xcode_runtime_roots() == []


def test_macos_xcode_selection_requires_developer_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    developer = tmp_path / "Applications" / "Xcode.app" / "Contents" / "Developer"
    logical = tmp_path / "var" / "db" / "xcode_select_link"
    developer.parent.mkdir(parents=True)
    developer.write_text("not a directory", encoding="utf-8")
    logical.parent.mkdir(parents=True)
    logical.symlink_to(developer)
    monkeypatch.setattr(sandbox, "_MACOS_XCODE_SELECT_LINK", logical)

    assert sandbox._selected_xcode_contents() is None


def test_macos_xcode_runtime_root_requires_info_plist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contents = tmp_path / "Applications" / "Xcode.app" / "Contents"
    developer = contents / "Developer"
    logical = tmp_path / "var" / "db" / "xcode_select_link"
    developer.mkdir(parents=True)
    logical.parent.mkdir(parents=True)
    logical.symlink_to(developer, target_is_directory=True)
    monkeypatch.setattr(sandbox, "_MACOS_XCODE_SELECT_LINK", logical)

    assert sandbox._selected_xcode_runtime_roots() == []


def test_macos_xcode_runtime_root_rejects_redirected_info_plist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contents = tmp_path / "Applications" / "Xcode.app" / "Contents"
    developer = contents / "Developer"
    redirected = tmp_path / "redirected-info.plist"
    logical = tmp_path / "var" / "db" / "xcode_select_link"
    developer.mkdir(parents=True)
    redirected.write_text("outside bundle", encoding="utf-8")
    (contents / "Info.plist").symlink_to(redirected)
    logical.parent.mkdir(parents=True)
    logical.symlink_to(developer, target_is_directory=True)
    monkeypatch.setattr(sandbox, "_MACOS_XCODE_SELECT_LINK", logical)

    assert sandbox._selected_xcode_runtime_roots() == []


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX descriptor-relative scratch primitive is not used on Windows",
)
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


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX descriptor-relative scratch primitive is not used on Windows",
)
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


def test_macos_sanitized_environment_redirects_xcrun_database_to_scratch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    monkeypatch.setenv("xcrun_db", "/var/folders/host-cache/xcrun_db")

    env = sandbox._sanitized_environment(tmp_path)

    assert env["xcrun_db"] == str(tmp_path / "tmp" / "xcrun_db")
    assert not Path(env["xcrun_db"]).exists()


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
