"""Tests for app.tool.subprocess_compat."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.tool.subprocess_compat import (
    IS_WINDOWS,
    decode_subprocess_output,
    find_shell,
    get_subprocess_kwargs,
    prepare_shell_command,
)


class TestGetSubprocessKwargs:
    def test_returns_dict(self):
        result = get_subprocess_kwargs()
        assert isinstance(result, dict)

    @pytest.mark.skipif(not IS_WINDOWS, reason="Windows-only")
    def test_windows_has_creationflags(self):
        kwargs = get_subprocess_kwargs()
        assert "creationflags" in kwargs

    @pytest.mark.skipif(IS_WINDOWS, reason="Non-Windows only")
    def test_non_windows_no_creationflags(self):
        kwargs = get_subprocess_kwargs()
        assert "creationflags" not in kwargs


class TestDecodeSubprocessOutput:
    def test_utf8_bytes(self):
        assert decode_subprocess_output(b"hello") == "hello"

    def test_utf8_unicode(self):
        text = "你好世界"
        assert decode_subprocess_output(text.encode("utf-8")) == text

    def test_empty_bytes(self):
        assert decode_subprocess_output(b"") == ""

    def test_invalid_bytes_replaced(self):
        # Invalid UTF-8 sequence — should not raise, returns a string
        result = decode_subprocess_output(b"\xff\xfe")
        assert isinstance(result, str)


class TestFindShell:
    def test_returns_list(self):
        result = find_shell()
        assert isinstance(result, list)
        assert len(result) >= 2

    @pytest.mark.skipif(not IS_WINDOWS, reason="Windows-only")
    def test_windows_uses_absolute_trusted_system_shell(self):
        result = find_shell()
        assert result[0].lower().endswith(("powershell.exe", "pwsh.exe", "cmd.exe"))
        assert Path(result[0]).is_absolute()
        assert "-NonInteractive" in result or "/D" in result

    @pytest.mark.skipif(IS_WINDOWS, reason="Non-Windows only")
    def test_non_windows_uses_bash_or_sh(self):
        result = find_shell()
        assert any(name in result[0] for name in ("bash", "zsh", "ksh", "sh"))
        if any(name in result[0] for name in ("bash", "zsh", "ksh")):
            assert "pipefail" in result

    def test_plain_sh_fails_closed_for_pipeline(self):
        with pytest.raises(ValueError, match="Pipelines require"):
            prepare_shell_command(["/bin/sh", "-c"], "false | cat")

    def test_plain_sh_allows_non_pipeline_command(self):
        assert prepare_shell_command(["/bin/sh", "-c"], "echo ok") == "echo ok"

    def test_powershell_5_fails_closed_for_native_pipeline(self):
        with pytest.raises(ValueError, match="PowerShell 7"):
            prepare_shell_command(
                [
                    r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
                    "-Command",
                ],
                "native-tool | Select-Object -Last 1",
            )

    def test_pwsh_7_wrapper_enables_native_pipeline_error_preference(self):
        wrapped = prepare_shell_command(
            [r"C:\Program Files\PowerShell\7\pwsh.exe", "-Command"],
            "native-tool | Select-Object -Last 1",
        )

        assert "$ErrorActionPreference = 'Stop'" in wrapped
        assert "$PSNativeCommandUseErrorActionPreference = $true" in wrapped
        assert "$LASTEXITCODE -ne 0" in wrapped
        assert "native-tool | Select-Object -Last 1" in wrapped
