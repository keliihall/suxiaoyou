from __future__ import annotations

from pathlib import Path

import pytest

from app.office_rendering import FontFingerprintError, fingerprint_font_environment


def test_font_fingerprint_is_content_ordered_and_invalidates_on_change(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "fonts-a"
    second_root = tmp_path / "fonts-b"
    first_root.mkdir()
    second_root.mkdir()
    (first_root / "CJK.ttc").write_bytes(b"font-one")
    (second_root / "Body.ttf").write_bytes(b"font-two")

    first = fingerprint_font_environment(roots=(first_root, second_root))
    repeated = fingerprint_font_environment(roots=(second_root, first_root))
    assert first == repeated

    (second_root / "Body.ttf").write_bytes(b"font-two-updated")
    updated = fingerprint_font_environment(roots=(first_root, second_root))
    assert updated != first


def test_font_fingerprint_rejects_empty_symlink_and_resource_overflow(
    tmp_path: Path,
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(FontFingerprintError, match="No regular fonts"):
        fingerprint_font_environment(roots=(empty,))

    fonts = tmp_path / "fonts"
    fonts.mkdir()
    (fonts / "one.ttf").write_bytes(b"1234")
    (fonts / "two.otf").write_bytes(b"5678")
    with pytest.raises(FontFingerprintError, match="file budget"):
        fingerprint_font_environment(roots=(fonts,), max_files=1)
    with pytest.raises(FontFingerprintError, match="byte budget"):
        fingerprint_font_environment(roots=(fonts,), max_bytes=4)

    target = tmp_path / "outside.ttf"
    target.write_bytes(b"outside")
    link = fonts / "linked.ttf"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("Symbolic links are unavailable")
    # Symlinked font entries never affect the pinned inventory.
    without_link = fingerprint_font_environment(roots=(fonts,))
    link.unlink()
    assert fingerprint_font_environment(roots=(fonts,)) == without_link
