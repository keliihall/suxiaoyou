"""Tests for file attachment API endpoints."""

from __future__ import annotations

import base64
import logging
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api import files as files_api
from app.api.files import (
    BrowseDirectoryRequest,
    BrowseRequest,
    FileContentRequest,
    browse_directory,
    browse_files,
    open_with_system,
    reveal_with_system,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def isolated_upload_dir(tmp_path, monkeypatch):
    upload_dir = tmp_path / "uploads"
    monkeypatch.setattr(files_api, "UPLOAD_DIR", upload_dir)
    with files_api._hash_index_lock:
        files_api._hash_index.clear()
    yield upload_dir
    with files_api._hash_index_lock:
        files_api._hash_index.clear()


class TestUploadSafety:
    async def test_upload_is_streamed_and_installed_atomically(
        self,
        app_client,
        isolated_upload_dir,
    ):
        payload = b"recording" * 200_000

        response = await app_client.post(
            "/api/files/upload",
            files={"file": ("meeting.m4a", payload, "audio/mp4")},
        )

        assert response.status_code == 200
        metadata = response.json()
        assert metadata["name"] == "meeting.m4a"
        assert metadata["size"] == len(payload)
        assert metadata["content_hash"] == __import__("hashlib").sha256(payload).hexdigest()
        assert Path(metadata["path"]).read_bytes() == payload
        assert not list(isolated_upload_dir.glob(".*.uploading"))

    async def test_duplicate_content_reuses_one_physical_file(
        self,
        app_client,
        isolated_upload_dir,
    ):
        payload = b"same bytes"

        first = await app_client.post(
            "/api/files/upload",
            files={"file": ("first.txt", payload, "text/plain")},
        )
        second = await app_client.post(
            "/api/files/upload",
            files={"file": ("second.txt", payload, "text/plain")},
        )

        assert first.status_code == second.status_code == 200
        assert first.json()["path"] == second.json()["path"]
        assert first.json()["file_id"] == second.json()["file_id"]
        assert first.json()["content_hash"] == second.json()["content_hash"]
        assert len([path for path in isolated_upload_dir.iterdir() if path.is_file()]) == 1

    async def test_oversized_upload_is_rejected_without_partial_file(
        self,
        app_client,
        isolated_upload_dir,
        monkeypatch,
    ):
        monkeypatch.setattr(files_api, "MAX_UPLOAD_BYTES", 8)

        response = await app_client.post(
            "/api/files/upload",
            files={"file": ("too-large.bin", b"123456789", "application/octet-stream")},
        )

        assert response.status_code == 413
        assert list(isolated_upload_dir.iterdir()) == []

    async def test_upload_filename_cannot_escape_upload_directory(
        self,
        app_client,
        isolated_upload_dir,
    ):
        response = await app_client.post(
            "/api/files/upload",
            files={"file": ("../../outside.txt", b"safe", "text/plain")},
        )

        assert response.status_code == 200
        metadata = response.json()
        assert metadata["name"] == "outside.txt"
        assert Path(metadata["path"]).parent == isolated_upload_dir.resolve()

    async def test_long_chinese_upload_name_respects_filesystem_byte_limit(
        self,
        app_client,
        isolated_upload_dir,
    ):
        original_name = "会议记录" * 60 + ".txt"

        response = await app_client.post(
            "/api/files/upload",
            files={"file": (original_name, b"unicode filename", "text/plain")},
        )

        assert response.status_code == 200
        metadata = response.json()
        assert metadata["name"].endswith(".txt")
        assert len(metadata["name"].encode("utf-8")) <= 180
        stored = Path(metadata["path"])
        assert len(stored.name.encode("utf-8")) <= 255
        assert stored.parent == isolated_upload_dir.resolve()
        assert stored.read_bytes() == b"unicode filename"

    async def test_rebuild_never_indexes_crash_staging_file(
        self,
        app_client,
        isolated_upload_dir,
    ):
        isolated_upload_dir.mkdir()
        crashed = isolated_upload_dir / ".crashed.uploading"
        crashed.write_bytes(b"partial recording bytes")

        files_api.rebuild_hash_index()
        response = await app_client.post(
            "/api/files/upload",
            files={
                "file": (
                    "recording.m4a",
                    b"partial recording bytes",
                    "audio/mp4",
                )
            },
        )

        assert response.status_code == 200
        installed = Path(response.json()["path"])
        assert installed != crashed
        assert installed.name.endswith("_recording.m4a")
        assert installed.read_bytes() == b"partial recording bytes"
        assert crashed.exists()


class TestPreviewSafety:
    async def test_large_text_preview_is_rejected_before_reading(
        self,
        app_client,
        tmp_path,
        monkeypatch,
    ):
        target = tmp_path / "large.txt"
        target.write_bytes(b"123456789")
        monkeypatch.setattr(files_api, "MAX_TEXT_PREVIEW_BYTES", 8)

        response = await app_client.post(
            "/api/files/content",
            json={"path": str(target)},
        )

        assert response.status_code == 413
        assert "too large to preview" in response.json()["detail"]

    async def test_binary_preview_returns_the_complete_base64_payload(
        self,
        app_client,
        tmp_path,
    ):
        payload = b"\x00\x01presentation-bytes\xff"
        target = tmp_path / "deck.pptx"
        target.write_bytes(payload)

        response = await app_client.post(
            "/api/files/content-binary",
            json={"path": str(target)},
        )

        assert response.status_code == 200
        body = response.json()
        assert base64.b64decode(body["content_base64"]) == payload
        assert body["name"] == "deck.pptx"
        assert body["size"] == len(payload)

    async def test_large_binary_preview_is_rejected_before_reading(
        self,
        app_client,
        tmp_path,
        monkeypatch,
    ):
        target = tmp_path / "large.pptx"
        target.write_bytes(b"123456789")
        monkeypatch.setattr(files_api, "MAX_BINARY_PREVIEW_BYTES", 8)

        response = await app_client.post(
            "/api/files/content-binary",
            json={"path": str(target)},
        )

        assert response.status_code == 413
        assert "too large to preview" in response.json()["detail"]


class TestAttachByPath:
    async def test_attach_accepts_files_and_directories(self, app_client, tmp_path):
        note = tmp_path / "note.md"
        note.write_text("# Note\n", encoding="utf-8")
        folder = tmp_path / "project-folder"
        folder.mkdir()

        resp = await app_client.post(
            "/api/files/attach",
            json={"paths": [str(note), str(folder), str(tmp_path / "missing.txt")]},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert [item["name"] for item in data] == ["note.md", "project-folder"]
        assert data[0]["path"] == str(note.resolve())
        assert data[0]["source"] == "referenced"
        assert data[1]["path"] == str(folder.resolve())
        assert data[1]["mime_type"] == "inode/directory"
        assert data[1]["size"] == 0


class TestOpenWithSystemSecurity:
    async def test_remote_client_cannot_launch_a_local_path(self, tmp_path, monkeypatch):
        target = tmp_path / "payload.exe"
        target.write_bytes(b"not executable")
        launched = False

        def fail_if_launched(*_args, **_kwargs):
            nonlocal launched
            launched = True

        monkeypatch.setattr("app.api.files.subprocess.Popen", fail_if_launched)
        request = Request({"type": "http", "state": {"source": "remote"}})

        with pytest.raises(HTTPException) as exc_info:
            await open_with_system(request, FileContentRequest(path=str(target)))

        assert exc_info.value.status_code == 403
        assert launched is False

    async def test_local_client_can_open_an_existing_path(self, tmp_path, monkeypatch):
        target = tmp_path / "document.txt"
        target.write_text("hello", encoding="utf-8")
        launched: list[list[str]] = []

        monkeypatch.setattr("app.api.files.platform.system", lambda: "Darwin")
        monkeypatch.setattr(
            "app.api.files.subprocess.Popen",
            lambda args: launched.append(args),
        )
        request = Request({"type": "http", "state": {"source": "local"}})

        result = await open_with_system(request, FileContentRequest(path=str(target)))

        assert result == {"status": "ok"}
        assert launched == [["open", str(target.resolve())]]


class TestNativeDialogSecurity:
    async def test_remote_client_cannot_open_host_native_dialogs(
        self,
        monkeypatch,
    ):
        launched = False

        async def fail_if_launched(*_args, **_kwargs):
            nonlocal launched
            launched = True

        monkeypatch.setattr(files_api, "_open_native_file_dialog", fail_if_launched)
        monkeypatch.setattr(files_api, "_open_native_directory_dialog", fail_if_launched)
        request = Request({"type": "http", "state": {"source": "remote"}})

        with pytest.raises(HTTPException) as file_error:
            await browse_files(request, BrowseRequest())
        with pytest.raises(HTTPException) as directory_error:
            await browse_directory(request, BrowseDirectoryRequest())

        assert file_error.value.status_code == 403
        assert directory_error.value.status_code == 403
        assert launched is False

    async def test_script_dialog_titles_are_bounded_and_literal_escaped(self):
        title = "  user's \"report\"\n" + "x" * 500
        normalized = files_api._normalize_dialog_title(title)

        assert "\n" not in normalized
        assert len(normalized) == files_api.MAX_DIALOG_TITLE_CHARS
        assert files_api._powershell_single_quoted("user's") == "user''s"
        assert (
            files_api._applescript_double_quoted('a\\b"c')
            == 'a\\\\b\\"c'
        )

    async def test_windows_uses_shell_file_association(self, tmp_path, monkeypatch):
        target = tmp_path / "presentation with spaces.pptx"
        target.write_bytes(b"presentation")
        launched: list[str] = []

        monkeypatch.setattr("app.api.files.platform.system", lambda: "Windows")
        monkeypatch.setattr(
            "app.api.files.os.startfile",
            lambda path: launched.append(path),
            raising=False,
        )
        request = Request({"type": "http", "state": {"source": "local"}})

        result = await open_with_system(
            request,
            FileContentRequest(path=str(target)),
        )

        assert result == {"status": "ok"}
        assert launched == [str(target.resolve())]

    async def test_local_client_cannot_open_a_missing_path(self, tmp_path, monkeypatch):
        launched = False

        def fail_if_launched(*_args, **_kwargs):
            nonlocal launched
            launched = True

        monkeypatch.setattr("app.api.files.subprocess.Popen", fail_if_launched)
        request = Request({"type": "http", "state": {"source": "local"}})

        with pytest.raises(HTTPException) as exc_info:
            await open_with_system(
                request,
                FileContentRequest(path=str(tmp_path / "missing.txt")),
            )

        assert exc_info.value.status_code == 404
        assert launched is False


class TestRevealWithSystemSecurity:
    async def test_remote_client_cannot_probe_or_reveal_a_local_path(
        self,
        tmp_path,
        monkeypatch,
    ):
        target = tmp_path / "secret.txt"
        target.write_text("secret", encoding="utf-8")
        launched = False

        def fail_if_launched(*_args, **_kwargs):
            nonlocal launched
            launched = True

        monkeypatch.setattr("app.api.files.subprocess.Popen", fail_if_launched)
        request = Request({"type": "http", "state": {"source": "remote"}})

        with pytest.raises(HTTPException) as exc_info:
            await reveal_with_system(request, FileContentRequest(path=str(target)))

        assert exc_info.value.status_code == 403
        assert launched is False

    @pytest.mark.parametrize(
        ("system", "expected"),
        [
            ("Darwin", lambda path: ["open", "-R", str(path)]),
            ("Windows", lambda path: ["explorer.exe", "/select,", str(path)]),
            ("Linux", lambda path: ["xdg-open", str(path.parent)]),
        ],
    )
    async def test_local_client_reveals_an_existing_file(
        self,
        tmp_path,
        monkeypatch,
        system,
        expected,
    ):
        target = tmp_path / "document with spaces.txt"
        target.write_text("hello", encoding="utf-8")
        launched: list[list[str]] = []

        monkeypatch.setattr("app.api.files.platform.system", lambda: system)
        monkeypatch.setattr(
            "app.api.files.subprocess.Popen",
            lambda args: launched.append(args),
        )
        request = Request({"type": "http", "state": {"source": "local"}})

        result = await reveal_with_system(
            request,
            FileContentRequest(path=str(target)),
        )

        assert result == {"status": "ok"}
        assert launched == [expected(target.resolve())]

    async def test_local_client_cannot_reveal_a_missing_path(self, tmp_path, monkeypatch):
        launched = False

        def fail_if_launched(*_args, **_kwargs):
            nonlocal launched
            launched = True

        monkeypatch.setattr("app.api.files.subprocess.Popen", fail_if_launched)
        request = Request({"type": "http", "state": {"source": "local"}})

        with pytest.raises(HTTPException) as exc_info:
            await reveal_with_system(
                request,
                FileContentRequest(path=str(tmp_path / "missing.txt")),
            )

        assert exc_info.value.status_code == 404
        assert launched is False


def _telemetry_lines(caplog: pytest.LogCaptureFixture, event: str) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "app.api.files"
        and r.getMessage().startswith("telemetry.files_browse ")
        and f"event={event}" in r.getMessage()
    ]


class TestBrowseTelemetry:
    """ADR-0010: every /files/browse* hit emits one structured log line."""

    async def test_browse_files_success(self, app_client, tmp_path, monkeypatch, caplog):
        target = tmp_path / "doc.md"
        target.write_text("hello", encoding="utf-8")

        async def fake_dialog(**_kw) -> list[str]:
            return [str(target)]

        monkeypatch.setattr("app.api.files._open_native_file_dialog", fake_dialog)
        caplog.set_level(logging.INFO, logger="app.api.files")

        resp = await app_client.post("/api/files/browse", json={"multiple": True, "title": "x"})

        assert resp.status_code == 200
        assert len(resp.json()) == 1
        lines = _telemetry_lines(caplog, "files_browse")
        assert len(lines) == 1
        line = lines[0]
        assert "outcome=success" in line
        assert "paths=1" in line
        assert "caller=" in line
        assert "server=" in line

    async def test_browse_files_cancel(self, app_client, monkeypatch, caplog):
        async def fake_dialog(**_kw) -> list[str]:
            return []

        monkeypatch.setattr("app.api.files._open_native_file_dialog", fake_dialog)
        caplog.set_level(logging.INFO, logger="app.api.files")

        resp = await app_client.post("/api/files/browse", json={})

        assert resp.status_code == 200
        assert resp.json() == []
        lines = _telemetry_lines(caplog, "files_browse")
        assert len(lines) == 1
        assert "outcome=cancel" in lines[0]
        assert "paths=0" in lines[0]

    async def test_browse_files_error_outcome_logged(self, app_client, monkeypatch, caplog):
        # Force the platform-specific dialog to raise so the existing
        # except: log+swallow path fires; telemetry must still record one
        # error line.
        async def boom(*_a, **_kw):
            raise RuntimeError("zenity not installed")

        monkeypatch.setattr("app.api.files._dialog_windows", boom)
        monkeypatch.setattr("app.api.files._dialog_macos", boom)
        monkeypatch.setattr("app.api.files._dialog_linux", boom)
        caplog.set_level(logging.INFO, logger="app.api.files")

        resp = await app_client.post("/api/files/browse", json={})

        assert resp.status_code == 200
        assert resp.json() == []
        lines = _telemetry_lines(caplog, "files_browse")
        # One error line from the helper, one cancel line from the endpoint
        # (the helper returned [] after the error, which the endpoint reads
        # as cancel — that's by design; both lines are useful signal).
        outcomes = sorted(
            line.split("outcome=")[1].split(" ")[0] for line in lines
        )
        assert outcomes == ["cancel", "error"]
        error_line = next(line for line in lines if "outcome=error" in line)
        assert "zenity not installed" in error_line

    async def test_browse_directory_success(self, app_client, monkeypatch, caplog):
        async def fake_dialog(*_a, **_kw) -> str:
            return "/picked/path"

        monkeypatch.setattr("app.api.files._open_native_directory_dialog", fake_dialog)
        caplog.set_level(logging.INFO, logger="app.api.files")

        resp = await app_client.post("/api/files/browse-directory", json={})

        assert resp.status_code == 200
        assert resp.json() == {"path": "/picked/path"}
        lines = _telemetry_lines(caplog, "files_browse_directory")
        assert len(lines) == 1
        assert "outcome=success" in lines[0]
        assert "paths=1" in lines[0]

    async def test_browse_directory_cancel(self, app_client, monkeypatch, caplog):
        async def fake_dialog(*_a, **_kw):
            return None

        monkeypatch.setattr("app.api.files._open_native_directory_dialog", fake_dialog)
        caplog.set_level(logging.INFO, logger="app.api.files")

        resp = await app_client.post("/api/files/browse-directory", json={})

        assert resp.status_code == 200
        assert resp.json() == {"path": None}
        lines = _telemetry_lines(caplog, "files_browse_directory")
        assert len(lines) == 1
        assert "outcome=cancel" in lines[0]
        assert "paths=0" in lines[0]

    async def test_caller_class_tauri_vs_browser(self, app_client, monkeypatch, caplog):
        async def fake_dialog(**_kw) -> list[str]:
            return []

        monkeypatch.setattr("app.api.files._open_native_file_dialog", fake_dialog)
        caplog.set_level(logging.INFO, logger="app.api.files")

        await app_client.post(
            "/api/files/browse",
            json={},
            headers={"User-Agent": "Mozilla/5.0 Tauri/1.0"},
        )
        await app_client.post(
            "/api/files/browse",
            json={},
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Firefox/123"},
        )

        lines = _telemetry_lines(caplog, "files_browse")
        assert any("caller=tauri" in line for line in lines)
        assert any("caller=browser" in line for line in lines)
