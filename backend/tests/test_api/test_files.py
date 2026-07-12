"""Tests for file attachment API endpoints."""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api import files as files_api
from app.api.files import (
    BrowseDirectoryRequest,
    BrowseRequest,
    FileContentRequest,
    NativeFileActionRequest,
    browse_directory,
    browse_files,
    get_native_source_info,
    open_authorized_file_default,
    open_with_system,
    reveal_authorized_file,
    reveal_with_system,
    stream_native_source_content,
)
from app.models.session import Session

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


class TestAuthorizedNativeFileActions:
    @staticmethod
    async def create_session(
        session_factory,
        workspace: Path,
        session_id: str = "native-files",
    ) -> str:
        workspace.mkdir(exist_ok=True)
        async with session_factory() as db:
            db.add(
                Session(
                    id=session_id,
                    directory=str(workspace),
                    title="Native file actions",
                )
            )
            await db.commit()
        return session_id

    async def test_source_info_is_bound_to_persisted_session_workspace(
        self,
        session_factory,
        tmp_path,
    ):
        workspace = tmp_path / "workspace"
        session_id = await self.create_session(session_factory, workspace)
        inside = workspace / "report.txt"
        inside.write_text("authorized", encoding="utf-8")

        response = await get_native_source_info(
            Request({"type": "http", "state": {"source": "local"}}),
            NativeFileActionRequest(path=str(inside), session_id=session_id),
            session_factory,
        )

        assert response["identity"].startswith("v1:")
        assert {key: value for key, value in response.items() if key != "identity"} == {
            "path": str(inside.resolve()),
            "name": "report.txt",
            "size": len(b"authorized"),
        }

    async def test_native_source_routes_accept_the_rust_bridge_contract(
        self,
        app_client,
        session_factory,
        tmp_path,
    ):
        workspace = tmp_path / "http-workspace"
        workspace.mkdir()
        target = workspace / "large report.bin"
        payload = b"native-http-stream" * 90_000
        target.write_bytes(payload)
        async with session_factory() as session:
            session.add(
                Session(
                    id="native-http",
                    directory=str(workspace),
                    title="Native HTTP bridge",
                )
            )
            await session.commit()

        request_body = {"path": str(target), "session_id": "native-http"}
        info = await app_client.post("/api/files/native-source-info", json=request_body)
        content = await app_client.post(
            "/api/files/native-source-content",
            json=request_body,
        )

        assert info.status_code == 200
        info_body = info.json()
        assert info_body["identity"].startswith("v1:")
        assert {
            key: value for key, value in info_body.items() if key != "identity"
        } == {
            "path": str(target.resolve()),
            "name": target.name,
            "size": len(payload),
        }
        assert content.status_code == 200
        assert content.headers["content-length"] == str(len(payload))
        assert hashlib.sha256(content.content).digest() == hashlib.sha256(payload).digest()

    async def test_source_outside_persisted_workspace_is_rejected(
        self,
        session_factory,
        tmp_path,
        monkeypatch,
    ):
        workspace = tmp_path / "workspace"
        session_id = await self.create_session(session_factory, workspace)
        outside = tmp_path / "private.txt"
        outside.write_text("private", encoding="utf-8")
        launched = False

        def fail_if_launched(*_args, **_kwargs):
            nonlocal launched
            launched = True

        monkeypatch.setattr("app.api.files.subprocess.Popen", fail_if_launched)
        request = Request({"type": "http", "state": {"source": "local"}})

        with pytest.raises(HTTPException) as exc_info:
            await open_authorized_file_default(
                request,
                NativeFileActionRequest(path=str(outside), session_id=session_id),
                session_factory,
            )

        assert exc_info.value.status_code == 403
        assert launched is False

    async def test_symlink_escape_is_rejected(self, session_factory, tmp_path):
        workspace = tmp_path / "workspace"
        session_id = await self.create_session(session_factory, workspace)
        outside = tmp_path / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        linked = workspace / "linked.txt"
        try:
            linked.symlink_to(outside)
        except (NotImplementedError, OSError):
            pytest.skip("symlinks are unavailable on this platform")

        with pytest.raises(HTTPException) as exc_info:
            await get_native_source_info(
                Request({"type": "http", "state": {"source": "local"}}),
                NativeFileActionRequest(path=str(linked), session_id=session_id),
                session_factory,
            )

        assert exc_info.value.status_code == 403

    async def test_symlink_in_parent_chain_is_rejected(
        self,
        session_factory,
        tmp_path,
    ):
        workspace = tmp_path / "workspace"
        session_id = await self.create_session(session_factory, workspace)
        outside_directory = tmp_path / "outside-directory"
        outside_directory.mkdir()
        outside = outside_directory / "secret.txt"
        outside.write_text("outside", encoding="utf-8")
        linked_directory = workspace / "linked-directory"
        try:
            linked_directory.symlink_to(outside_directory, target_is_directory=True)
        except (NotImplementedError, OSError):
            pytest.skip("directory symlinks are unavailable on this platform")

        with pytest.raises(HTTPException) as exc_info:
            await get_native_source_info(
                Request({"type": "http", "state": {"source": "local"}}),
                NativeFileActionRequest(
                    path=str(linked_directory / outside.name),
                    session_id=session_id,
                ),
                session_factory,
            )

        assert exc_info.value.status_code == 403

    async def test_stream_rejects_a_symlink_swapped_at_final_open(
        self,
        session_factory,
        tmp_path,
        monkeypatch,
    ):
        if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
            pytest.skip("component-relative no-follow open is unavailable")

        workspace = tmp_path / "workspace"
        session_id = await self.create_session(session_factory, workspace)
        target = workspace / "report.bin"
        target.write_bytes(b"authorized bytes")
        outside = tmp_path / "outside-secret.bin"
        outside.write_bytes(b"must never be streamed")
        probe = workspace / "symlink-probe"
        try:
            probe.symlink_to(outside)
            probe.unlink()
        except (NotImplementedError, OSError):
            pytest.skip("symlinks are unavailable on this platform")

        original_secure_open = files_api._open_native_source_with_dir_fd
        swapped = False

        def swap_before_final_open(workspace_path, relative_path):
            nonlocal swapped
            if not swapped:
                swapped = True
                target.unlink()
                target.symlink_to(outside)
            return original_secure_open(workspace_path, relative_path)

        monkeypatch.setattr(
            files_api,
            "_open_native_source_with_dir_fd",
            swap_before_final_open,
        )

        with pytest.raises(HTTPException) as exc_info:
            await stream_native_source_content(
                Request({"type": "http", "state": {"source": "local"}}),
                NativeFileActionRequest(path=str(target), session_id=session_id),
                session_factory,
            )

        assert swapped is True
        assert exc_info.value.status_code == 403
        assert outside.read_bytes() == b"must never be streamed"

    async def test_fallback_rejects_kernel_resolved_path_outside_workspace(
        self,
        session_factory,
        tmp_path,
        monkeypatch,
    ):
        workspace = tmp_path / "workspace"
        session_id = await self.create_session(session_factory, workspace)
        target = workspace / "report.bin"
        target.write_bytes(b"authorized bytes")
        outside = tmp_path / "outside-secret.bin"
        outside.write_bytes(b"must never be streamed")

        monkeypatch.setattr(files_api.os, "supports_dir_fd", set())
        monkeypatch.setattr(
            files_api,
            "_windows_path_from_handle",
            lambda _descriptor: outside,
        )

        with pytest.raises(HTTPException) as exc_info:
            await stream_native_source_content(
                Request({"type": "http", "state": {"source": "local"}}),
                NativeFileActionRequest(path=str(target), session_id=session_id),
                session_factory,
            )

        assert exc_info.value.status_code == 403
        assert outside.read_bytes() == b"must never be streamed"

    async def test_missing_in_workspace_source_has_a_distinct_result(
        self,
        session_factory,
        tmp_path,
    ):
        workspace = tmp_path / "workspace"
        session_id = await self.create_session(session_factory, workspace)

        with pytest.raises(HTTPException) as exc_info:
            await get_native_source_info(
                Request({"type": "http", "state": {"source": "local"}}),
                NativeFileActionRequest(
                    path=str(workspace / "gone.txt"),
                    session_id=session_id,
                ),
                session_factory,
            )

        assert exc_info.value.status_code == 404
        assert exc_info.value.detail == "Source file no longer exists"

    @pytest.mark.parametrize(
        "handler",
        [get_native_source_info, stream_native_source_content],
    )
    async def test_remote_token_cannot_authorize_or_stream_host_files(
        self,
        handler,
        session_factory,
        tmp_path,
    ):
        workspace = tmp_path / f"workspace-{handler.__name__}"
        session_id = await self.create_session(
            session_factory,
            workspace,
            handler.__name__,
        )
        target = workspace / "secret.txt"
        target.write_text("secret", encoding="utf-8")

        with pytest.raises(HTTPException) as exc_info:
            await handler(
                Request({"type": "http", "state": {"source": "remote"}}),
                NativeFileActionRequest(path=str(target), session_id=session_id),
                session_factory,
            )

        assert exc_info.value.status_code == 403

    async def test_large_source_is_emitted_in_bounded_binary_chunks(
        self,
        session_factory,
        tmp_path,
    ):
        workspace = tmp_path / "workspace"
        session_id = await self.create_session(session_factory, workspace)
        payload = (b"large-native-copy" * 80_000) + b"tail"
        target = workspace / "large.bin"
        target.write_bytes(payload)

        response = await stream_native_source_content(
            Request({"type": "http", "state": {"source": "local"}}),
            NativeFileActionRequest(path=str(target), session_id=session_id),
            session_factory,
        )
        chunks = [chunk async for chunk in response.body_iterator]

        assert chunks
        assert max(map(len, chunks)) <= files_api.NATIVE_FILE_STREAM_CHUNK_BYTES
        assert sum(map(len, chunks)) == len(payload)
        assert hashlib.sha256(b"".join(chunks)).digest() == hashlib.sha256(payload).digest()
        assert response.headers["content-length"] == str(len(payload))

    async def test_stream_closes_database_session_before_body_iteration(
        self,
        session_factory,
        tmp_path,
    ):
        workspace = tmp_path / "workspace"
        session_id = await self.create_session(session_factory, workspace)
        payload = b"stream after database close"
        target = workspace / "report.bin"
        target.write_bytes(payload)
        state = {"closed": False}

        class TrackingSessionContext:
            def __init__(self):
                self.session = session_factory()

            async def __aenter__(self):
                return await self.session.__aenter__()

            async def __aexit__(self, exc_type, exc, traceback):
                try:
                    return await self.session.__aexit__(exc_type, exc, traceback)
                finally:
                    state["closed"] = True

        class TrackingFactory:
            def __call__(self):
                return TrackingSessionContext()

        response = await stream_native_source_content(
            Request({"type": "http", "state": {"source": "local"}}),
            NativeFileActionRequest(path=str(target), session_id=session_id),
            TrackingFactory(),
        )

        assert state["closed"] is True
        assert b"".join([chunk async for chunk in response.body_iterator]) == payload

    @pytest.mark.parametrize(
        "handler",
        [open_authorized_file_default, reveal_authorized_file],
    )
    async def test_launcher_entry_replacement_is_revalidated_before_os_call(
        self,
        handler,
        session_factory,
        tmp_path,
        monkeypatch,
    ):
        workspace = tmp_path / "workspace"
        session_id = await self.create_session(session_factory, workspace)
        target = workspace / "report.txt"
        target.write_text("authorized", encoding="utf-8")
        replacement = workspace / "replacement.txt"
        replacement.write_text("replacement", encoding="utf-8")
        original_launcher_entry = files_api._launch_authorized_native_path
        entered_launcher = False
        launched = False

        def replace_at_launcher_entry(source, action):
            nonlocal entered_launcher
            entered_launcher = True
            replacement.replace(target)
            return original_launcher_entry(source, action)

        def fail_if_launched(*_args, **_kwargs):
            nonlocal launched
            launched = True

        monkeypatch.setattr(
            files_api,
            "_launch_authorized_native_path",
            replace_at_launcher_entry,
        )
        monkeypatch.setattr(files_api, "_invoke_native_path_action", fail_if_launched)

        with pytest.raises(HTTPException) as exc_info:
            await handler(
                Request({"type": "http", "state": {"source": "local"}}),
                NativeFileActionRequest(path=str(target), session_id=session_id),
                session_factory,
            )

        assert entered_launcher is True
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == "Source file changed before the native action"
        assert launched is False

    async def test_identity_handles_are_held_through_launcher_call(
        self,
        session_factory,
        tmp_path,
        monkeypatch,
    ):
        workspace = tmp_path / "workspace"
        session_id = await self.create_session(session_factory, workspace)
        target = workspace / "report.txt"
        target.write_text("authorized", encoding="utf-8")
        original_secure_open = files_api._securely_open_native_source
        opened_sources = []
        invoked = False

        def capture_secure_open(workspace_path, relative_path):
            opened = original_secure_open(workspace_path, relative_path)
            opened_sources.append(opened)
            return opened

        def assert_handles_open(path, action, system):
            nonlocal invoked
            invoked = True
            assert path == target.resolve()
            assert action == "open"
            assert system
            assert len(opened_sources) == 2
            assert all(not source.handle.closed for source in opened_sources)

        monkeypatch.setattr(
            files_api,
            "_securely_open_native_source",
            capture_secure_open,
        )
        monkeypatch.setattr(
            files_api,
            "_invoke_native_path_action",
            assert_handles_open,
        )

        result = await open_authorized_file_default(
            Request({"type": "http", "state": {"source": "local"}}),
            NativeFileActionRequest(path=str(target), session_id=session_id),
            session_factory,
        )

        assert result == {"status": "ok"}
        assert invoked is True
        assert all(source.handle.closed for source in opened_sources)

    async def test_authorized_default_open_and_reveal_use_canonical_file(
        self,
        session_factory,
        tmp_path,
        monkeypatch,
    ):
        workspace = tmp_path / "workspace"
        session_id = await self.create_session(session_factory, workspace)
        target = workspace / "report with spaces.txt"
        target.write_text("report", encoding="utf-8")
        launched: list[list[str]] = []
        monkeypatch.setattr("app.api.files.platform.system", lambda: "Darwin")
        monkeypatch.setattr(
            "app.api.files.subprocess.Popen",
            lambda args: launched.append(args),
        )
        request = Request({"type": "http", "state": {"source": "local"}})
        body = NativeFileActionRequest(path=str(target), session_id=session_id)

        assert await open_authorized_file_default(request, body, session_factory) == {
            "status": "ok"
        }
        assert await reveal_authorized_file(request, body, session_factory) == {
            "status": "ok"
        }
        assert launched == [
            ["open", str(target.resolve())],
            ["open", "-R", str(target.resolve())],
        ]

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
