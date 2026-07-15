"""Token counting utilities."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile

import tiktoken

_encoding: tiktoken.Encoding | None = None
_CL100K_URL = (
    "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
)
_CL100K_CACHE_KEY = hashlib.sha1(_CL100K_URL.encode()).hexdigest()


def _encoding_cache_path() -> Path | None:
    configured = os.environ.get("TIKTOKEN_CACHE_DIR")
    if configured is None:
        configured = os.environ.get("DATA_GYM_CACHE_DIR")
    if configured == "":
        return None
    cache_dir = (
        Path(configured)
        if configured
        else Path(tempfile.gettempdir()) / "data-gym-cache"
    )
    return cache_dir / _CL100K_CACHE_KEY


def _get_encoding() -> tiktoken.Encoding | None:
    global _encoding
    if _encoding is None:
        cache_path = _encoding_cache_path()
        allow_download = os.environ.get("SUXIAOYOU_TOKENIZER_ALLOW_DOWNLOAD") == "1"
        if not allow_download and (cache_path is None or not cache_path.is_file()):
            return None
        _encoding = tiktoken.get_encoding("cl100k_base")
    return _encoding


def count_tokens(text: str) -> int:
    """Count tokens without making an implicit network request.

    Use the cached cl100k tokenizer when available.  A fresh/offline install
    falls back to a UTF-8 estimate instead of hanging generation startup while
    tiktoken tries to download its encoding table.
    """

    if not text:
        return 0
    encoding = _get_encoding()
    if encoding is not None:
        return len(encoding.encode(text))
    return max(1, (len(text.encode("utf-8")) + 3) // 4)


def estimate_tokens(text: str) -> int:
    """Fast token estimate without full encoding (~4 chars per token)."""
    return max(1, len(text) // 4)
