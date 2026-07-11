"""Tests for app.tool.builtin.web_fetch HTML extraction."""

from __future__ import annotations

import app.tool.builtin.web_fetch as web_fetch
from app.tool.builtin.web_fetch import _strip_html, extract_readable_content


def test_extracts_article_as_markdown_without_regex_fallback(monkeypatch) -> None:
    html = """
    <!doctype html>
    <html>
      <head><title>Packaging contracts</title></head>
      <body>
        <nav>Navigation noise that must not be returned.</nav>
        <main>
          <article>
            <h1>Reliable releases</h1>
            <p>A sufficiently detailed article paragraph explains why the
               packaged application must include its runtime dependencies.</p>
            <p>It also preserves <a href="https://example.com/source">source links</a>
               when converted to readable Markdown for the assistant.</p>
          </article>
        </main>
        <script>window.tracking = true;</script>
      </body>
    </html>
    """

    def fail_if_regex_fallback_runs(_html: str) -> str:
        raise AssertionError("regex fallback was used")

    monkeypatch.setattr(web_fetch, "_strip_html", fail_if_regex_fallback_runs)

    result = extract_readable_content(html, "https://example.com/article")

    assert result.startswith("# Packaging contracts")
    assert "Reliable releases" in result
    assert "[source links](https://example.com/source)" in result
    assert "Navigation noise" not in result
    assert "window.tracking" not in result


class TestStripHtml:
    def test_removes_script_tags(self):
        html = '<p>hello</p><script>alert("xss")</script><p>world</p>'
        result = _strip_html(html)
        assert "alert" not in result
        assert "hello" in result
        assert "world" in result

    def test_removes_style_tags(self):
        html = "<style>body{color:red}</style><p>text</p>"
        result = _strip_html(html)
        assert "color" not in result
        assert "text" in result

    def test_removes_html_tags(self):
        html = "<div><p>hello</p></div>"
        result = _strip_html(html)
        assert "hello" in result
        assert "<" not in result

    def test_collapses_whitespace(self):
        html = "<p>hello</p>   \n\n   <p>world</p>"
        result = _strip_html(html)
        # Multiple spaces should be collapsed
        assert "  " not in result

    def test_nested_tags(self):
        html = "<div><span><b>nested</b></span></div>"
        result = _strip_html(html)
        assert "nested" in result
        assert "<" not in result
