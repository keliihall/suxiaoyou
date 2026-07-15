"""Read tool tests."""

import asyncio
import base64
import json
import os
from pathlib import Path

import pytest

from app.schemas.agent import AgentInfo
from app.tool.builtin import read as read_module
from app.tool.builtin.read import (
    ReadTool,
    _MAX_DIRECTORY_ENTRIES,
    _MAX_IMAGE_BYTES,
)
from app.tool.context import ToolContext


def _make_ctx(
    language: str = "zh",
    *,
    workspace: str | None = None,
    attachment_paths: frozenset[str] = frozenset(),
) -> ToolContext:
    return ToolContext(
        session_id="test-session",
        message_id="test-msg",
        agent=AgentInfo(name="test", description="", mode="primary"),
        call_id="test-call",
        language=language,
        workspace=workspace,
        attachment_paths=attachment_paths,
    )


class TestReadTool:
    @pytest.fixture
    def tool(self):
        return ReadTool()

    @pytest.mark.asyncio
    async def test_read_file(self, tool: ReadTool, tmp_path: Path):
        f = tmp_path / "hello.txt"
        f.write_text("line1\nline2\nline3\n")

        result = await tool.execute({"file_path": str(f)}, _make_ctx())
        assert result.success
        assert "line1" in result.output
        assert "line2" in result.output
        assert "line3" in result.output

    @pytest.mark.asyncio
    async def test_read_with_offset_limit(self, tool: ReadTool, tmp_path: Path):
        f = tmp_path / "lines.txt"
        f.write_text("\n".join(f"line{i}" for i in range(1, 11)))

        result = await tool.execute({"file_path": str(f), "offset": 3, "limit": 2}, _make_ctx())
        assert result.success
        assert "line3" in result.output
        assert "line4" in result.output
        assert "line1" not in result.output

    @pytest.mark.asyncio
    async def test_read_nonexistent(self, tool: ReadTool):
        result = await tool.execute({"file_path": "/nonexistent/file.txt"}, _make_ctx())
        assert not result.success
        assert "not found" in result.error.lower()

    @pytest.mark.asyncio
    async def test_read_directory(self, tool: ReadTool, tmp_path: Path):
        (tmp_path / "a.txt").touch()
        (tmp_path / "b.txt").touch()

        result = await tool.execute({"file_path": str(tmp_path)}, _make_ctx())
        assert result.success
        assert "a.txt" in result.output
        assert "b.txt" in result.output
        assert result.title == f"已列出 {tmp_path.name} 中的 2 个条目"
        assert result.metadata == {
            "source": "filesystem",
            "total_entries": 2,
            "shown": 2,
            "entry_limit": _MAX_DIRECTORY_ENTRIES,
            "directory_truncated": False,
            "truncated": False,
            "has_more": False,
        }

        english = await tool.execute(
            {"file_path": str(tmp_path)}, _make_ctx(language="en")
        )
        assert english.title == f"Listed 2 entries in {tmp_path.name}"

    @pytest.mark.asyncio
    async def test_large_directory_is_globally_sorted_and_memory_bounded(
        self,
        tool: ReadTool,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        total = _MAX_DIRECTORY_ENTRIES + 25
        names = [f"entry-{index:04d}.txt" for index in range(total)]
        for name in reversed(names):
            (tmp_path / name).touch()

        def unbounded_listdir_must_not_run(*_args, **_kwargs):
            raise AssertionError("directory listing must not materialize os.listdir")

        monkeypatch.setattr(read_module.os, "listdir", unbounded_listdir_must_not_run)
        first = await tool.execute({"file_path": str(tmp_path)}, _make_ctx())
        second = await tool.execute({"file_path": str(tmp_path)}, _make_ctx())

        expected = sorted(names)[:_MAX_DIRECTORY_ENTRIES]
        assert first.success
        assert first.output.splitlines() == expected
        assert second.output == first.output
        assert first.metadata["total_entries"] == total
        assert first.metadata["shown"] == _MAX_DIRECTORY_ENTRIES
        assert first.metadata["entry_limit"] == _MAX_DIRECTORY_ENTRIES
        assert first.metadata["directory_truncated"] is True
        assert first.metadata["truncated"] is True
        assert first.metadata["has_more"] is True
        assert first.title == (
            f"已列出 {tmp_path.name} 中的 {_MAX_DIRECTORY_ENTRIES} 个条目"
        )

    @pytest.mark.asyncio
    async def test_directory_symlink_swap_during_scan_fails_closed(
        self,
        tool: ReadTool,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        workspace = tmp_path / "workspace"
        directory = workspace / "listed"
        outside = tmp_path / "outside"
        directory.mkdir(parents=True)
        outside.mkdir()
        (directory / "inside.txt").touch()
        (outside / "secret.txt").touch()
        moved = workspace / "listed-original"
        real_nsmallest = read_module.heapq.nsmallest
        swapped = False

        def swap_after_scan(*args, **kwargs):
            nonlocal swapped
            result = real_nsmallest(*args, **kwargs)
            if not swapped:
                directory.rename(moved)
                try:
                    directory.symlink_to(outside, target_is_directory=True)
                except OSError:
                    pytest.skip("directory symlinks are unavailable")
                swapped = True
            return result

        monkeypatch.setattr(read_module.heapq, "nsmallest", swap_after_scan)
        result = await tool.execute(
            {"file_path": str(directory)},
            _make_ctx(workspace=str(workspace)),
        )

        assert not result.success
        assert "changed while being listed" in result.error
        assert "secret.txt" not in result.output

    @pytest.mark.asyncio
    async def test_line_numbers_format(self, tool: ReadTool, tmp_path: Path):
        f = tmp_path / "num.txt"
        f.write_text("hello\nworld\n")

        result = await tool.execute({"file_path": str(f)}, _make_ctx())
        assert result.success
        # Should have line number prefixes
        assert "\t" in result.output  # tab separator between number and content

    @pytest.mark.asyncio
    async def test_image_outside_workspace_is_not_authorized_by_existence(
        self, tool: ReadTool, tmp_path: Path
    ):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        external = tmp_path / "private.png"
        external.write_bytes(b"\x89PNG\r\n\x1a\nnot-a-real-image")

        result = await tool.execute(
            {"file_path": str(external)},
            _make_ctx(workspace=str(workspace)),
        )

        assert not result.success
        assert "outside the workspace" in result.error

    @pytest.mark.asyncio
    async def test_exact_registered_session_image_can_be_read_outside_workspace(
        self, tool: ReadTool, tmp_path: Path
    ):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        external = tmp_path / "selected.png"
        external.write_bytes(b"\x89PNG\r\n\x1a\nregistered")

        result = await tool.execute(
            {"file_path": str(external)},
            _make_ctx(
                workspace=str(workspace),
                attachment_paths=frozenset({str(external.resolve())}),
            ),
        )

        assert result.success
        assert result.metadata["source"] == "session_attachment"
        assert result.metadata["image_data_url"].startswith("data:image/png;base64,")

    @pytest.mark.asyncio
    async def test_oversized_image_is_rejected_before_read_or_base64_encoding(
        self,
        tool: ReadTool,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        image = tmp_path / "oversized.png"
        with image.open("wb") as handle:
            handle.truncate(_MAX_IMAGE_BYTES + 1)
        encoded = False
        read_called = False

        def must_not_read(_fd, _size):
            nonlocal read_called
            read_called = True
            raise AssertionError("oversized image reached byte reading")

        def must_not_encode(_raw):
            nonlocal encoded
            encoded = True
            raise AssertionError("oversized image reached base64 encoding")

        monkeypatch.setattr(read_module.os, "read", must_not_read)
        monkeypatch.setattr(base64, "b64encode", must_not_encode)
        result = await tool.execute({"file_path": str(image)}, _make_ctx())

        assert not result.success
        assert f"{_MAX_IMAGE_BYTES // (1024 * 1024)} MiB read limit" in result.error
        assert read_called is False
        assert encoded is False

    @pytest.mark.asyncio
    async def test_image_symlink_swap_during_read_fails_closed(
        self,
        tool: ReadTool,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        image = workspace / "inside.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\ninside")
        outside = tmp_path / "outside.png"
        outside.write_bytes(b"must-not-be-read")
        moved = workspace / "inside-original.png"
        real_read = os.read
        swapped = False

        def swap_after_fd_open(fd: int, size: int) -> bytes:
            nonlocal swapped
            data = real_read(fd, size)
            if not swapped:
                image.rename(moved)
                try:
                    image.symlink_to(outside)
                except OSError:
                    pytest.skip("file symlinks are unavailable")
                swapped = True
            return data

        encoded = False

        def must_not_encode(_raw):
            nonlocal encoded
            encoded = True
            raise AssertionError("changed image reached base64 encoding")

        monkeypatch.setattr(read_module.os, "read", swap_after_fd_open)
        monkeypatch.setattr(base64, "b64encode", must_not_encode)
        result = await tool.execute(
            {"file_path": str(image)},
            _make_ctx(workspace=str(workspace)),
        )

        assert not result.success
        assert "changed while being read" in result.error
        assert encoded is False

    @pytest.mark.asyncio
    async def test_registered_attachment_does_not_authorize_a_sibling_image(
        self, tool: ReadTool, tmp_path: Path
    ):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        registered = tmp_path / "selected.png"
        registered.write_bytes(b"registered")
        sibling = tmp_path / "sibling.png"
        sibling.write_bytes(b"sibling")

        result = await tool.execute(
            {"file_path": str(sibling)},
            _make_ctx(
                workspace=str(workspace),
                attachment_paths=frozenset({str(registered.resolve())}),
            ),
        )

        assert not result.success
        assert "outside the workspace" in result.error

    @pytest.mark.asyncio
    async def test_exact_registered_text_attachment_can_be_read_without_sibling_access(
        self, tool: ReadTool, tmp_path: Path
    ):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        registered = tmp_path / "selected.txt"
        registered.write_text("allowed attachment", encoding="utf-8")
        sibling = tmp_path / "private.txt"
        sibling.write_text("must stay private", encoding="utf-8")
        ctx = _make_ctx(
            workspace=str(workspace),
            attachment_paths=frozenset({str(registered.resolve())}),
        )

        allowed = await tool.execute({"file_path": str(registered)}, ctx)
        denied = await tool.execute({"file_path": str(sibling)}, ctx)

        assert allowed.success
        assert "allowed attachment" in allowed.output
        assert allowed.metadata["source"] == "session_attachment"
        assert not denied.success
        assert "outside the workspace" in denied.error

    @pytest.mark.asyncio
    async def test_rejects_unbounded_or_unsupported_options(
        self, tool: ReadTool, tmp_path: Path
    ):
        text_path = tmp_path / "plain.txt"
        text_path.write_text("hello", encoding="utf-8")

        bad_limit = await tool.execute(
            {"file_path": str(text_path), "limit": 2001}, _make_ctx()
        )
        bad_pages = await tool.execute(
            {"file_path": str(text_path), "pages": "1"}, _make_ctx()
        )
        bad_format = await tool.execute(
            {"file_path": str(text_path), "format": "json"}, _make_ctx()
        )

        assert not bad_limit.success
        assert "between 1 and 2000" in bad_limit.error
        assert not bad_pages.success
        assert "pages is supported only" in bad_pages.error
        assert not bad_format.success
        assert "format=json is supported only" in bad_format.error

    @pytest.mark.asyncio
    async def test_legacy_xls_fails_with_actionable_message(
        self, tool: ReadTool, tmp_path: Path
    ):
        legacy = tmp_path / "legacy.xls"
        legacy.write_bytes(b"not really an xls")

        result = await tool.execute({"file_path": str(legacy)}, _make_ctx())

        assert not result.success
        assert "not supported" in result.error
        assert ".xlsx or CSV" in result.error


class TestReadStructuredData:
    @pytest.fixture
    def tool(self):
        return ReadTool()

    @pytest.mark.asyncio
    async def test_csv_json_respects_row_offset_and_limit(
        self, tool: ReadTool, tmp_path: Path
    ):
        csv_path = tmp_path / "sales.csv"
        csv_path.write_text(
            "name,amount\nA,10\nB,20\nC,30\nD,40\n",
            encoding="utf-8",
        )

        result = await tool.execute(
            {
                "file_path": str(csv_path),
                "format": "json",
                "offset": 2,
                "limit": 2,
            },
            _make_ctx(),
        )

        assert result.success
        payload = json.loads(result.output)
        assert payload["columns"] == ["name", "amount"]
        assert payload["rows"] == [["B", "20"], ["C", "30"]]
        assert result.metadata["total_rows"] == 4
        assert result.metadata["shown"] == 2
        assert result.metadata["has_more"] is True

    @pytest.mark.asyncio
    async def test_xlsx_named_sheet_json_respects_row_offset_and_limit(
        self, tool: ReadTool, tmp_path: Path
    ):
        openpyxl = pytest.importorskip("openpyxl")
        xlsx_path = tmp_path / "book.xlsx"
        wb = openpyxl.Workbook()
        summary = wb.active
        summary.title = "Summary"
        summary.append(["name", "amount"])
        summary.append(["Hidden from selection", 1])
        revenue = wb.create_sheet("Revenue")
        revenue.append(["month", "amount"])
        revenue.append(["Jan", 10])
        revenue.append(["Feb", 20])
        revenue.append(["Mar", 30])
        wb.save(xlsx_path)

        result = await tool.execute(
            {
                "file_path": str(xlsx_path),
                "pages": "Revenue",
                "format": "json",
                "offset": 2,
                "limit": 1,
            },
            _make_ctx(),
        )

        assert result.success
        payload = json.loads(result.output)
        assert payload["available_sheets"] == ["Summary", "Revenue"]
        assert [sheet["name"] for sheet in payload["sheets"]] == ["Revenue"]
        assert payload["sheets"][0]["rows"] == [["Feb", 20]]
        assert result.metadata["selected_sheet"] == "Revenue"
        assert result.metadata["sheets"]["Revenue"]["has_more"] is True

    @pytest.mark.asyncio
    async def test_xlsx_missing_named_sheet_is_explicit(
        self, tool: ReadTool, tmp_path: Path
    ):
        openpyxl = pytest.importorskip("openpyxl")
        xlsx_path = tmp_path / "book.xlsx"
        wb = openpyxl.Workbook()
        wb.active.title = "Available"
        wb.save(xlsx_path)

        result = await tool.execute(
            {"file_path": str(xlsx_path), "pages": "Missing"}, _make_ctx()
        )

        assert not result.success
        assert "Sheet not found: Missing" in result.error
        assert "Available" in result.error


class TestReadOfficeFormats:
    """Test reading binary office document formats via ReadTool."""

    @pytest.fixture
    def tool(self):
        return ReadTool()

    @pytest.mark.asyncio
    async def test_read_pdf(self, tool: ReadTool, tmp_path: Path):
        pytest.importorskip("pypdf")
        rc = pytest.importorskip("reportlab.pdfgen.canvas")

        pdf_path = tmp_path / "test.pdf"
        c = rc.Canvas(str(pdf_path))
        c.drawString(72, 700, "Hello PDF")
        c.save()

        result = await tool.execute({"file_path": str(pdf_path)}, _make_ctx())
        assert result.success
        assert "Hello PDF" in result.output

    @pytest.mark.asyncio
    async def test_read_docx(self, tool: ReadTool, tmp_path: Path):
        docx_mod = pytest.importorskip("docx")

        docx_path = tmp_path / "test.docx"
        doc = docx_mod.Document()
        doc.add_paragraph("Hello DOCX")
        doc.save(str(docx_path))

        result = await tool.execute({"file_path": str(docx_path)}, _make_ctx())
        assert result.success
        assert "Hello DOCX" in result.output

    @pytest.mark.asyncio
    async def test_read_xlsx(self, tool: ReadTool, tmp_path: Path):
        openpyxl = pytest.importorskip("openpyxl")

        xlsx_path = tmp_path / "test.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws["A1"] = "Hello XLSX"
        wb.save(str(xlsx_path))

        result = await tool.execute({"file_path": str(xlsx_path)}, _make_ctx())
        assert result.success
        assert "Hello XLSX" in result.output

    @pytest.mark.asyncio
    async def test_read_pptx(self, tool: ReadTool, tmp_path: Path):
        pptx_mod = pytest.importorskip("pptx")

        pptx_path = tmp_path / "test.pptx"
        prs = pptx_mod.Presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[0])
        slide.shapes.title.text = "Hello PPTX"
        prs.save(str(pptx_path))

        result = await tool.execute({"file_path": str(pptx_path)}, _make_ctx())
        assert result.success
        assert "Hello PPTX" in result.output

    @pytest.mark.asyncio
    async def test_read_pdf_with_offset_limit(self, tool: ReadTool, tmp_path: Path):
        pytest.importorskip("pypdf")
        rc = pytest.importorskip("reportlab.pdfgen.canvas")

        pdf_path = tmp_path / "multiline.pdf"
        c = rc.Canvas(str(pdf_path))
        y = 750
        for i in range(1, 21):
            c.drawString(72, y, f"Line {i}")
            y -= 14
        c.save()

        result = await tool.execute(
            {"file_path": str(pdf_path), "offset": 3, "limit": 5}, _make_ctx()
        )
        assert result.success
        # Should respect offset/limit on extracted text lines
        assert result.metadata["shown"] == 5

    @pytest.mark.asyncio
    async def test_read_selected_pdf_pages(self, tool: ReadTool, tmp_path: Path):
        pytest.importorskip("pypdf")
        rc = pytest.importorskip("reportlab.pdfgen.canvas")

        pdf_path = tmp_path / "pages.pdf"
        c = rc.Canvas(str(pdf_path))
        for page_number in range(1, 4):
            c.drawString(72, 700, f"PDF page {page_number} content")
            c.showPage()
        c.save()

        result = await tool.execute(
            {"file_path": str(pdf_path), "pages": "2-3"}, _make_ctx()
        )

        assert result.success
        assert "PDF page 1 content" not in result.output
        assert "PDF page 2 content" in result.output
        assert "PDF page 3 content" in result.output
        assert result.metadata["selected_pages"] == [2, 3]
        assert result.metadata["total_pages"] == 3

    @pytest.mark.asyncio
    async def test_pdf_page_selection_rejects_invalid_range(
        self, tool: ReadTool, tmp_path: Path
    ):
        pytest.importorskip("pypdf")
        rc = pytest.importorskip("reportlab.pdfgen.canvas")
        pdf_path = tmp_path / "one-page.pdf"
        c = rc.Canvas(str(pdf_path))
        c.drawString(72, 700, "only page")
        c.save()

        result = await tool.execute(
            {"file_path": str(pdf_path), "pages": "2"}, _make_ctx()
        )

        assert not result.success
        assert "valid range is 1-1" in result.error

    @pytest.mark.asyncio
    async def test_read_selected_pptx_slide(self, tool: ReadTool, tmp_path: Path):
        pptx_mod = pytest.importorskip("pptx")
        pptx_path = tmp_path / "slides.pptx"
        prs = pptx_mod.Presentation()
        first = prs.slides.add_slide(prs.slide_layouts[0])
        first.shapes.title.text = "First slide"
        second = prs.slides.add_slide(prs.slide_layouts[0])
        second.shapes.title.text = "Second slide"
        prs.save(pptx_path)

        result = await tool.execute(
            {"file_path": str(pptx_path), "pages": "2"}, _make_ctx()
        )

        assert result.success
        assert "First slide" not in result.output
        assert "Second slide" in result.output
        assert result.metadata["selected_slides"] == [2]
        assert result.metadata["total_slides"] == 2
