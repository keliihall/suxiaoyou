"""Platform capability contract for guarded visible-file mutation.

Linux and Darwin use descriptor-relative rename/exchange primitives.  Windows
uses ``ReplaceFileW`` with a same-directory backup plus directory handles that
do not share DELETE; the backup is the exact object displaced at the replace
linearization point and can be validated or atomically restored.
"""

from __future__ import annotations

import sys
from typing import Final


WINDOWS_GUARDED_MUTATION_UNAVAILABLE: Final = (
    "Safe atomic file mutation requires Windows 10 or newer with ReplaceFileW, "
    "MoveFileExW, and file-ID directory handles. No workspace file was changed."
)


def guarded_file_mutation_unavailable_reason(
    platform_name: str | None = None,
) -> str | None:
    """Return a stable fail-closed reason, or ``None`` when v1 is supported."""

    platform = sys.platform if platform_name is None else platform_name
    # Windows declarative file tools use the native ReplaceFileW/MoveFileExW
    # implementation in ``windows_guarded_file``.  Do not disable every file
    # mutation merely because the host is Windows: the transaction layer still
    # rejects unsupported operation shapes (for example full-workspace command
    # staging and multi-file commits) before any visible path is changed.
    if platform == "win32":
        return None
    if platform.startswith("linux") or platform == "darwin":
        return None
    return (
        "Safe atomic file mutation is unavailable on this platform: v1 requires "
        "descriptor-anchored no-replace and two-way exchange operations. "
        "No workspace file was changed."
    )


def guarded_file_mutation_supported(platform_name: str | None = None) -> bool:
    """Whether the platform has every primitive required by the v1 protocol."""

    return guarded_file_mutation_unavailable_reason(platform_name) is None


__all__ = [
    "WINDOWS_GUARDED_MUTATION_UNAVAILABLE",
    "guarded_file_mutation_supported",
    "guarded_file_mutation_unavailable_reason",
]
