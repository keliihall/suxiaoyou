"""Minimal Python child used by :mod:`code_execute`.

This module intentionally imports only the standard library.  The parent
places the process inside an OS sandbox before this code is reached.
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path
from typing import Any


def run_code_file(code_path: str) -> int:
    """Execute one UTF-8 source file and return a process exit status."""

    try:
        code = Path(code_path).read_text(encoding="utf-8")
        compiled = compile(code, "<code_execute>", "exec")
        namespace: dict[str, Any] = {
            "__builtins__": __builtins__,
            "__file__": "<code_execute>",
            "__name__": "__main__",
        }
        exec(compiled, namespace)
        return 0
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    except BaseException:
        traceback.print_exc()
        return 1


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("sandbox worker requires exactly one code file", file=sys.stderr)
        return 2
    return run_code_file(args[0])


if __name__ == "__main__":
    raise SystemExit(main())
