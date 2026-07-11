"""PDF generation using redistributable ReportLab components.

The renderer supports the Markdown structures used by conversation and
artifact exports while keeping the packaged runtime license-compatible.
"""

from __future__ import annotations

import html
import io
import logging
import os
import sys
from pathlib import Path

import markdown
from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.fonts import addMapping
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    ListFlowable,
    ListItem,
    PageBreak,
    Paragraph,
    Preformatted,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

log = logging.getLogger(__name__)

_WINDOWS_FONT_DIR = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
_BUNDLED_CJK_FONT = "SuxiaoyouCJK-Regular.ttf"
_BODY_FAMILY = "SuxiaoyouBody"
_MONO_FAMILY = "SuxiaoyouMono"
_fonts_registered = False
_body_font = "Helvetica"
_mono_font = "Courier"
_CONTENT_WIDTH = A4[0] - (5 * cm) - 12
_CONTENT_HEIGHT = A4[1] - (4 * cm) - 12
_HIDDEN_TAGS = {"head", "link", "meta", "noscript", "script", "style", "template", "title"}
_MAX_TABLE_COLUMNS = 12
_MAX_TABLE_CELL_TEXT = 1_500


def _bundled_font_candidates() -> list[tuple[Path, bool]]:
    """Return application-owned CJK fonts before any host font fallback.

    PyInstaller exposes bundled data below ``sys._MEIPASS``.  The module-
    relative path covers both an unpackaged checkout and PyInstaller's
    extracted ``app/api`` module layout.
    """
    roots = [Path(__file__).resolve().parents[1] / "data"]
    pyinstaller_root = getattr(sys, "_MEIPASS", None)
    if pyinstaller_root:
        roots.insert(0, Path(pyinstaller_root) / "app" / "data")

    candidates: list[tuple[Path, bool]] = []
    seen: set[Path] = set()
    for root in roots:
        path = root / "fonts" / _BUNDLED_CJK_FONT
        if path not in seen:
            candidates.append((path, False))
            seen.add(path)
    return candidates


def _system_font_candidates() -> tuple[list[tuple[Path, bool]], list[tuple[Path, bool]]]:
    """Return optional host font fallbacks for the current platform."""
    if sys.platform == "darwin":
        return (
            [
                (Path("/System/Library/Fonts/STHeiti Medium.ttc"), True),
                (Path("/System/Library/Fonts/Supplemental/Songti.ttc"), True),
                (Path("/System/Library/Fonts/STHeiti Light.ttc"), True),
                (Path("/Library/Fonts/Arial Unicode.ttf"), False),
            ],
            [
                (Path("/System/Library/Fonts/Menlo.ttc"), True),
                (Path("/System/Library/Fonts/Monaco.ttf"), False),
                (Path("/System/Library/Fonts/Supplemental/Courier New.ttf"), False),
            ],
        )
    if sys.platform == "win32":
        return (
            [
                (_WINDOWS_FONT_DIR / "msyh.ttc", True),
                (_WINDOWS_FONT_DIR / "simhei.ttf", False),
                (_WINDOWS_FONT_DIR / "simsun.ttc", True),
                (_WINDOWS_FONT_DIR / "malgun.ttf", False),
                (_WINDOWS_FONT_DIR / "arial.ttf", False),
            ],
            [
                (_WINDOWS_FONT_DIR / "consola.ttf", False),
                (_WINDOWS_FONT_DIR / "cour.ttf", False),
                (_WINDOWS_FONT_DIR / "lucon.ttf", False),
            ],
        )

    body: list[tuple[Path, bool]] = []
    mono: list[tuple[Path, bool]] = []
    for directory in (
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
        Path(os.path.expanduser("~/.fonts")),
    ):
        if not directory.exists():
            continue
        for name in (
            "NotoSansCJK-Regular.ttc",
            "NotoSansSC-Regular.otf",
            "wqy-microhei.ttc",
            "DroidSansFallbackFull.ttf",
        ):
            body.extend((path, name.endswith(".ttc")) for path in directory.rglob(name))
        for name in (
            "DejaVuSansMono.ttf",
            "NotoSansMono-Regular.ttf",
            "LiberationMono-Regular.ttf",
        ):
            mono.extend((path, False) for path in directory.rglob(name))
    return body, mono


def _font_candidates() -> tuple[list[tuple[Path, bool]], list[tuple[Path, bool]]]:
    """Return the bundled CJK face first, with host fonts only as fallback."""
    system_body, system_mono = _system_font_candidates()
    return _bundled_font_candidates() + system_body, system_mono


def _register_family(family: str, path: Path, is_collection: bool) -> bool:
    face = f"{family}Regular"
    try:
        kwargs = {"subfontIndex": 0} if is_collection else {}
        pdfmetrics.registerFont(TTFont(face, str(path), **kwargs))
        for bold in (0, 1):
            for italic in (0, 1):
                addMapping(family, bold, italic, face)
        return True
    except Exception as error:  # pragma: no cover - depends on host fonts
        log.debug("Skipping PDF font %s: %s", path, error)
        return False


def _register_fonts() -> None:
    global _fonts_registered, _body_font, _mono_font
    if _fonts_registered:
        return

    body_candidates, mono_candidates = _font_candidates()
    for path, is_collection in body_candidates:
        if path.exists() and _register_family(_BODY_FAMILY, path, is_collection):
            # Paragraph styles use the registered PostScript face.  The
            # family mappings created by ``_register_family`` let ReportLab
            # resolve inline bold/italic markup back to this face.
            _body_font = f"{_BODY_FAMILY}Regular"
            break
    else:
        log.warning(
            "Bundled and system CJK PDF fonts are unavailable; some characters may not render"
        )

    if _body_font != "Helvetica":
        # The body face was selected for its CJK coverage.  Common
        # monospace faces (Menlo, Consolas) omit many CJK glyphs, so reuse the
        # body face for code to avoid silently replacing text with NUL glyphs.
        _mono_font = _body_font
    else:
        for path, is_collection in mono_candidates:
            if path.exists() and _register_family(_MONO_FAMILY, path, is_collection):
                _mono_font = f"{_MONO_FAMILY}Regular"
                break

    _fonts_registered = True


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "body": ParagraphStyle(
            "SuxiaoyouBody",
            parent=base["BodyText"],
            fontName=_body_font,
            fontSize=10.5,
            leading=16,
            spaceAfter=7,
            textColor=colors.HexColor("#1a1a1a"),
        ),
        "h1": ParagraphStyle(
            "SuxiaoyouH1",
            parent=base["Heading1"],
            fontName=_body_font,
            fontSize=20,
            leading=25,
            spaceBefore=14,
            spaceAfter=10,
        ),
        "h2": ParagraphStyle(
            "SuxiaoyouH2",
            parent=base["Heading2"],
            fontName=_body_font,
            fontSize=16,
            leading=21,
            spaceBefore=12,
            spaceAfter=8,
        ),
        "h3": ParagraphStyle(
            "SuxiaoyouH3",
            parent=base["Heading3"],
            fontName=_body_font,
            fontSize=13,
            leading=18,
            spaceBefore=10,
            spaceAfter=6,
        ),
        "code": ParagraphStyle(
            "SuxiaoyouCode",
            parent=base["Code"],
            fontName=_mono_font,
            fontSize=8.5,
            leading=12,
            leftIndent=8,
            rightIndent=8,
            borderColor=colors.HexColor("#dddddd"),
            borderWidth=0.5,
            borderPadding=7,
            backColor=colors.HexColor("#f6f6f6"),
            spaceBefore=5,
            spaceAfter=8,
        ),
        "quote": ParagraphStyle(
            "SuxiaoyouQuote",
            parent=base["BodyText"],
            fontName=_body_font,
            fontSize=10.5,
            leading=16,
            leftIndent=16,
            borderColor=colors.HexColor("#b7b7b7"),
            borderWidth=0,
            borderPadding=5,
            textColor=colors.HexColor("#555555"),
            spaceAfter=7,
        ),
        "title": ParagraphStyle(
            "SuxiaoyouTitle",
            parent=base["Title"],
            fontName=_body_font,
            fontSize=22,
            leading=28,
            alignment=TA_CENTER,
            spaceAfter=14,
        ),
    }


def _inline_markup(node: Tag | NavigableString) -> str:
    if isinstance(node, Comment):
        return ""
    if isinstance(node, NavigableString):
        return html.escape(str(node))
    name = (node.name or "").lower()
    if name in _HIDDEN_TAGS:
        return ""
    if name == "img":
        alt = " ".join(str(node.get("alt", "")).split())
        source = " ".join(str(node.get("src", "")).split())
        label = alt or source
        if len(label) > 512:
            label = f"{label[:509]}..."
        return html.escape(f"[Image: {label}]" if label else "[Image]")
    inner = "".join(_inline_markup(child) for child in node.children)
    if name == "br":
        return "<br/>"
    if name in {"b", "strong"}:
        return f"<b>{inner}</b>"
    if name in {"i", "em"}:
        return f"<i>{inner}</i>"
    if name == "code":
        return f'<font name="{html.escape(_mono_font, quote=True)}">{inner}</font>'
    if name == "a":
        href = str(node.get("href", ""))
        if href.startswith(("http://", "https://", "mailto:")):
            return f'<a href="{html.escape(href, quote=True)}" color="#0066cc">{inner}</a>'
    return inner


def _paragraph(tag: Tag, style: ParagraphStyle) -> Paragraph | None:
    if (tag.name or "").lower() == "img":
        markup = _inline_markup(tag)
    else:
        markup = "".join(_inline_markup(child) for child in tag.children).strip()
    return Paragraph(markup, style) if markup else None


def _list(tag: Tag, styles: dict[str, ParagraphStyle]) -> ListFlowable | None:
    items: list[ListItem] = []
    for item in tag.find_all("li", recursive=False):
        markup = "".join(
            _inline_markup(child)
            for child in item.children
            if not (isinstance(child, Tag) and (child.name or "").lower() in {"ul", "ol"})
        ).strip()
        contents: list[object] = [Paragraph(markup or " ", styles["body"])]
        for nested in item.find_all(["ul", "ol"], recursive=False):
            nested_list = _list(nested, styles)
            if nested_list:
                contents.append(nested_list)
        items.append(ListItem(contents, leftIndent=12))

    if not items:
        return None
    name = (tag.name or "").lower()
    options: dict[str, object] = {
        "bulletType": "1" if name == "ol" else "bullet",
        "leftIndent": 22,
        "bulletFontName": _body_font,
    }
    if name == "ol":
        options["start"] = "1"
    return ListFlowable(items, **options)


def _table(tag: Tag, styles: dict[str, ParagraphStyle]) -> list[object]:
    rows: list[list[tuple[str, str]]] = []
    header = False
    for row_index, row in enumerate(tag.find_all("tr")):
        cells = row.find_all(["th", "td"], recursive=False)
        if not cells:
            continue
        header = header or (row_index == 0 and any(cell.name == "th" for cell in cells))
        rows.append(
            [
                (
                    "".join(_inline_markup(child) for child in cell.children).strip() or " ",
                    cell.get_text(" ", strip=True),
                )
                for cell in cells
            ]
        )
    if not rows:
        return []

    columns = max(len(row) for row in rows)
    should_degrade = columns > _MAX_TABLE_COLUMNS or any(
        len(plain_text) > _MAX_TABLE_CELL_TEXT for row in rows for _, plain_text in row
    )

    paragraph_rows = [
        [Paragraph(markup, styles["body"]) for markup, _ in row]
        for row in rows
    ]
    for row in paragraph_rows:
        row.extend(Paragraph(" ", styles["body"]) for _ in range(columns - len(row)))

    if not should_degrade:
        # Table cells cannot split across pages.  A character-count limit is
        # insufficient because the same text becomes much taller as columns
        # get narrower.  Measure each Paragraph at its actual cell width and
        # include table padding plus a repeated header, matching Platypus'
        # effective page frame.  Rows that cannot fit safely are rendered as
        # ordinary paragraphs instead, which can split across pages.
        cell_content_width = max((_CONTENT_WIDTH / columns) - 10, 1)
        row_heights = [
            max(cell.wrap(cell_content_width, _CONTENT_HEIGHT)[1] for cell in row) + 8
            for row in paragraph_rows
        ]
        repeated_header_height = row_heights[0] if header else 0
        for row_index, row_height in enumerate(row_heights):
            required_height = row_height
            if header and row_index > 0:
                required_height += repeated_header_height
            if required_height > _CONTENT_HEIGHT:
                should_degrade = True
                break

    if should_degrade:
        # A table row cannot split across pages in Platypus.  Extremely wide
        # tables or rows taller than the usable frame therefore degrade to
        # ordinary paragraphs, preserving every value while allowing normal
        # wrapping and page breaks.
        return [
            Paragraph(" &nbsp;|&nbsp; ".join(markup for markup, _ in row), styles["body"])
            for row in rows
        ]

    table = Table(
        paragraph_rows,
        colWidths=[_CONTENT_WIDTH / columns] * columns,
        repeatRows=1 if header else 0,
    )
    commands = [
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    if header:
        commands.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")))
    for index in range(1 if header else 0, len(paragraph_rows)):
        if index % 2 == 0:
            commands.append(("BACKGROUND", (0, index), (-1, index), colors.HexColor("#fafafa")))
    table.setStyle(TableStyle(commands))
    return [table]


def _blocks(container: Tag, styles: dict[str, ParagraphStyle]) -> list[object]:
    flowables: list[object] = []
    for child in container.children:
        if isinstance(child, Comment):
            continue
        if isinstance(child, NavigableString):
            text = str(child).strip()
            if text:
                flowables.append(Paragraph(html.escape(text), styles["body"]))
            continue
        if not isinstance(child, Tag):
            continue

        name = (child.name or "").lower()
        if name in _HIDDEN_TAGS:
            continue
        if name in {"h1", "h2", "h3", "h4", "h5", "h6", "p", "blockquote"}:
            key = name if name in {"h1", "h2", "h3"} else "quote" if name == "blockquote" else "body"
            paragraph = _paragraph(child, styles[key])
            if paragraph:
                flowables.append(paragraph)
        elif name == "pre":
            flowables.append(Preformatted(child.get_text(), styles["code"], maxLineLength=110))
        elif name in {"ul", "ol"}:
            rendered_list = _list(child, styles)
            if rendered_list:
                flowables.append(rendered_list)
                flowables.append(Spacer(1, 5))
        elif name == "table":
            table_flowables = _table(child, styles)
            if table_flowables:
                flowables.extend([*table_flowables, Spacer(1, 8)])
        elif name == "hr":
            flowables.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#cccccc")))
            flowables.append(Spacer(1, 8))
        elif name in {"div", "section", "article", "main", "body"}:
            flowables.extend(_blocks(child, styles))
        elif name in {"pagebreak", "page-break"}:
            flowables.append(PageBreak())
        else:
            paragraph = _paragraph(child, styles["body"])
            if paragraph:
                flowables.append(paragraph)
    return flowables


def html_to_pdf(html_body: str) -> bytes:
    """Convert a safe subset of HTML into PDF bytes with ReportLab."""
    _register_fonts()
    styles = _styles()
    soup = BeautifulSoup(html_body, "html.parser")
    root = soup.body or soup
    story = _blocks(root, styles)
    if not story:
        story = [Paragraph(" ", styles["body"])]

    output = io.BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=A4,
        leftMargin=2.5 * cm,
        rightMargin=2.5 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
        title="苏小有 export",
        author="苏小有",
    )
    document.build(story)
    return output.getvalue()


_DEFAULT_EXTENSIONS = ["tables", "fenced_code", "toc", "nl2br"]


def markdown_to_pdf(md_content: str, extensions: list[str] | None = None) -> bytes:
    """Convert Markdown text into PDF bytes."""
    html_body = markdown.markdown(
        md_content,
        extensions=extensions if extensions is not None else _DEFAULT_EXTENSIONS,
    )
    return html_to_pdf(html_body)
