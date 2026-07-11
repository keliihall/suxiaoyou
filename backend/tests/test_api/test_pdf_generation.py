"""Contract tests for the redistributable PDF export implementation."""

from __future__ import annotations

import io
import inspect

import pytest
from pypdf import PdfReader

from app.api import pdf


def _text(pdf_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    assert len(reader.pages) >= 1
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def test_markdown_to_pdf_uses_redistributable_reportlab_pipeline() -> None:
    source = inspect.getsource(pdf)
    assert "from xhtml2pdf" not in source
    assert "import xhtml2pdf" not in source
    assert "from reportlab.platypus" in source

    output = pdf.markdown_to_pdf(
        "# Release report\n\nA verified paragraph.\n\n- First item\n- Second item"
    )

    assert output.startswith(b"%PDF-")
    extracted = _text(output)
    assert "Release report" in extracted
    assert "A verified paragraph" in extracted
    assert "First item" in extracted


def test_html_to_pdf_preserves_basic_table_content() -> None:
    output = pdf.html_to_pdf(
        "<h2>Metrics</h2><table><tr><th>Name</th><th>Value</th></tr>"
        "<tr><td>Passed</td><td>72</td></tr></table>"
    )

    extracted = _text(output)
    assert "Metrics" in extracted
    assert "Name" in extracted
    assert "Passed" in extracted
    assert "72" in extracted


def test_html_to_pdf_omits_hidden_content() -> None:
    output = pdf.html_to_pdf(
        "<p>Visible</p><!-- TOP_SECRET --><script>SCRIPT_SECRET</script>"
        "<style>STYLE_SECRET</style><template>TEMPLATE_SECRET</template>"
    )

    extracted = _text(output)
    assert "Visible" in extracted
    assert "TOP_SECRET" not in extracted
    assert "SCRIPT_SECRET" not in extracted
    assert "STYLE_SECRET" not in extracted
    assert "TEMPLATE_SECRET" not in extracted


def test_html_to_pdf_degrades_extreme_tables_without_losing_text() -> None:
    wide_header = "".join(f"<th>Column {index}</th>" for index in range(20))
    wide_values = "".join(f"<td>Value {index}</td>" for index in range(20))
    long_value = "Long cell " + ("content " * 700)

    output = pdf.html_to_pdf(
        f"<table><tr>{wide_header}</tr><tr>{wide_values}</tr></table>"
        f"<table><tr><td>{long_value}</td></tr></table>"
    )

    extracted = _text(output)
    assert "Column 19" in extracted
    assert "Value 19" in extracted
    assert "Long cell" in extracted


@pytest.mark.parametrize("columns", [8, 12])
def test_html_to_pdf_degrades_rows_that_are_too_tall_after_wrapping(columns: int) -> None:
    cells = "".join(
        f"<td>cell-{index} " + ("word " * 50) + f"end-{index}</td>"
        for index in range(columns)
    )

    output = pdf.html_to_pdf(f"<table><tr>{cells}</tr></table>")

    assert output.startswith(b"%PDF-")
    extracted = _text(output)
    assert "cell-0" in extracted
    assert f"end-{columns - 1}" in extracted


def test_html_to_pdf_preserves_image_alt_text_or_source_placeholder() -> None:
    output = pdf.html_to_pdf(
        '<p>Before <img src="https://example.invalid/chart.png" '
        'alt="Quarterly revenue chart"> after.</p>'
        '<img src="https://example.invalid/fallback.png">'
    )

    extracted = _text(output)
    assert "Quarterly revenue chart" in extracted
    assert "fallback.png" in extracted


def test_markdown_to_pdf_preserves_cjk_in_code_and_nested_lists() -> None:
    output = pdf.markdown_to_pdf(
        "# 中文导出\n\n正文可见，行内代码 `代码` 也应可见。\n\n"
        "```text\n代码块\n```\n\n- 父项\n  - 子项\n"
    )

    extracted = _text(output)
    for expected in ["中文导出", "正文可见", "代码", "代码块", "父项", "子项"]:
        assert expected in extracted


def test_bundled_cjk_font_works_without_any_system_font(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pdf, "_system_font_candidates", lambda: ([], []))
    monkeypatch.setattr(pdf, "_fonts_registered", False)
    monkeypatch.setattr(pdf, "_body_font", "Helvetica")
    monkeypatch.setattr(pdf, "_mono_font", "Courier")

    bundled = pdf._bundled_font_candidates()
    assert bundled
    assert bundled[0][0].name == "SuxiaoyouCJK-Regular.ttf"
    assert bundled[0][0].is_file()

    output = pdf.markdown_to_pdf(
        "# 无系统字体测试\n\n苏小有中文导出不应显示方块。\n\n"
        "```text\n代码块中文\n```"
    )

    assert pdf._body_font == "SuxiaoyouBodyRegular"
    assert pdf._mono_font == "SuxiaoyouBodyRegular"
    extracted = _text(output)
    for expected in ["无系统字体测试", "苏小有中文导出", "不应显示方块", "代码块中文"]:
        assert expected in extracted

    reader = PdfReader(io.BytesIO(output))
    font_objects = [
        reference.get_object()
        for reference in reader.pages[0]["/Resources"]["/Font"].values()
    ]
    embedded_cjk = [
        font
        for font in font_objects
        if "SuxiaoyouCJK-Regular" in str(font.get("/BaseFont", ""))
        and "/FontFile2" in font["/FontDescriptor"].get_object()
    ]
    assert embedded_cjk, "portable CJK TrueType subset must be embedded in the PDF"
