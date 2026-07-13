"""Small backend localization primitives for user-visible API/tool text.

The frontend owns the full translation catalog.  The backend only translates
text that it creates dynamically (tool activity, API errors, and external
product metadata), so keeping the supported locale set deliberately small
prevents request-specific language from leaking into technical identifiers.
"""

from __future__ import annotations

from typing import Literal

from fastapi import Request


Language = Literal["zh", "en"]


def normalize_language(value: str | None, *, default: Language = "zh") -> Language:
    """Normalize a locale or Accept-Language value to a supported language.

    The first supported language in preference order wins.  Quality values of
    zero are ignored; unknown locales fall back to the caller's default.
    """

    if not value:
        return default

    candidates: list[tuple[float, int, Language]] = []
    for index, item in enumerate(value.split(",")):
        parts = [part.strip() for part in item.split(";")]
        tag = parts[0].lower().replace("_", "-")
        quality = 1.0
        for parameter in parts[1:]:
            if parameter.lower().startswith("q="):
                try:
                    quality = float(parameter[2:])
                except ValueError:
                    quality = 0.0
        if quality <= 0:
            continue
        if tag == "en" or tag.startswith("en-"):
            candidates.append((quality, -index, "en"))
        elif tag in {"zh", "cmn"} or tag.startswith(("zh-", "cmn-")):
            candidates.append((quality, -index, "zh"))

    if not candidates:
        return default
    return max(candidates)[2]


def request_language(request: Request, *, default: Language = "zh") -> Language:
    """Return the supported language requested by an HTTP client."""

    return normalize_language(request.headers.get("accept-language"), default=default)


def localize(language: Language | str, zh: str, en: str) -> str:
    """Select one already-authored backend string without translating data."""

    return en if normalize_language(language) == "en" else zh


def product_name(language: Language | str) -> str:
    """Return the localized user-facing product name.

    Internal package/env/protocol identifiers remain ``suxiaoyou``.
    """

    return localize(language, "苏小有", "suyo")
