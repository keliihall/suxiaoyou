"""Shared fixtures for MCP trust-boundary tests."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def private_test_executable(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Return an executable whose ownership and mode are test-controlled.

    Hosted Python installations may legitimately be group-writable.  Local MCP
    production code must reject those executables, so approval-path tests use a
    private copy instead of depending on the permissions of ``sys.executable``.
    """

    source = Path(sys.executable).resolve(strict=True)
    executable = tmp_path_factory.mktemp("mcp-private-bin") / source.name
    shutil.copy2(source, executable)
    if os.name != "nt":
        executable.chmod(0o700)
    return str(executable.resolve(strict=True))
