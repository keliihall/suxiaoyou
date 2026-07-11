"""Regression tests for XSS in OAuth callback HTML builders.

The OAuth callback responses embed values that can originate from
attacker-controlled query parameters (``state``, ``error``,
``error_description``) or from external identity providers (``email``).
These must never be reflected into the HTML/JS response verbatim.
"""

from __future__ import annotations

from app.api.callback_html import render_callback
from app.api.openai_auth import _error_html, _success_html

_PAYLOAD = '</script><img src=x onerror=alert(1)>'


def test_render_callback_escapes_extra_data() -> None:
    """A malicious ``state`` value must not break out of the <script>."""
    html = render_callback(True, extra_data={"state": _PAYLOAD})
    assert "</script><img" not in html
    assert "onerror=alert(1)>" not in html
    # The value survives in escaped form inside the JS string.
    assert "\\u003c/script\\u003e" in html


def test_render_callback_escapes_message_type() -> None:
    html = render_callback(False, message_type='x"});alert(1)//')
    script = html.split("<script>")[1]
    # The quote must be escaped (\") so the payload stays inside the JS
    # string literal and cannot terminate the postMessage() call early.
    assert '\\"});alert(1)//' in script


def test_error_html_escapes_message() -> None:
    html = _error_html(_PAYLOAD)
    assert "<img src=x" not in html
    assert "&lt;/script&gt;&lt;img" in html


def test_success_html_escapes_email() -> None:
    html = _success_html('a@b.com<script>alert(1)</script>')
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
