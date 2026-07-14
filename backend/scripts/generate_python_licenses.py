#!/usr/bin/env python3
"""Generate deterministic license notices for the locked Python runtime."""

from __future__ import annotations

import hashlib
import importlib.metadata
import re
from collections import defaultdict
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
REQUIREMENTS = REPOSITORY_ROOT / "backend" / "requirements.txt"
OUTPUT = REPOSITORY_ROOT / "release-licenses" / "PYTHON-LICENSES.txt"


def canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def locked_names() -> set[str]:
    names: set[str] = set()
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Za-z0-9_.-]+)==", line)
        if match:
            names.add(canonical_name(match.group(1)))
    return names


def license_expression(metadata: importlib.metadata.PackageMetadata) -> str:
    expression = metadata.get("License-Expression") or metadata.get("License")
    if expression and expression.strip() not in {"UNKNOWN", ""}:
        return " ".join(expression.split())
    classifiers = [
        value.removeprefix("License :: ")
        for value in metadata.get_all("Classifier", [])
        if value.startswith("License :: ")
    ]
    return "; ".join(classifiers) or "UNKNOWN"


def source_urls(metadata: importlib.metadata.PackageMetadata) -> list[str]:
    urls: list[str] = []
    for value in metadata.get_all("Project-URL", []):
        if "," in value:
            _, url = value.split(",", 1)
            urls.append(url.strip())
    for key in ("Home-page", "Download-URL"):
        if metadata.get(key):
            urls.append(str(metadata[key]).strip())
    return sorted(set(filter(None, urls)))


def license_documents(distribution: importlib.metadata.Distribution) -> list[tuple[str, str]]:
    documents: list[tuple[str, str]] = []
    seen: set[str] = set()
    for relative_path in distribution.files or []:
        parts = [part.lower() for part in Path(str(relative_path)).parts]
        basename = parts[-1]
        in_license_directory = "licenses" in parts or "license_files" in parts
        named_license = bool(
            re.match(r"^(?:licen[sc]e|copying|notice|copyright)(?:[._-]|$)", basename)
        )
        if not (in_license_directory or named_license):
            continue
        path = distribution.locate_file(relative_path)
        try:
            if not path.is_file() or path.stat().st_size > 2_000_000:
                continue
            text = path.read_text(encoding="utf-8").replace("\r\n", "\n").strip()
        except (OSError, UnicodeDecodeError):
            continue
        if not text:
            continue
        digest = hashlib.sha256(text.encode()).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        documents.append((str(relative_path), text))
    return sorted(documents)


def main() -> None:
    expected = locked_names()
    installed: dict[str, importlib.metadata.Distribution] = {}
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name")
        if name and canonical_name(name) in expected:
            installed[canonical_name(name)] = distribution

    lines = [
        "Python production dependency licenses",
        "=====================================",
        "",
        "Generated from the hash-locked runtime environment with:",
        "`python backend/scripts/generate_python_licenses.py`.",
        "",
        f"Locked packages: {len(expected)}",
        f"Installed packages represented on this build host: {len(installed)}",
        "Pure-Python platform-conditional wheels are represented here; binary-only wheels use separate versioned files.",
        "",
    ]

    missing_documents: list[tuple[str, str, list[str]]] = []
    for name in sorted(installed):
        distribution = installed[name]
        metadata = distribution.metadata
        display_name = metadata.get("Name", name)
        version = distribution.version
        license_name = license_expression(metadata)
        urls = source_urls(metadata)
        documents = license_documents(distribution)

        lines.extend(
            [
                "-" * 78,
                f"Package: {display_name} {version}",
                f"Declared license: {license_name}",
            ]
        )
        if urls:
            lines.append(f"Source/project: {', '.join(urls)}")
        lines.append("")
        if documents:
            for relative_path, text in documents:
                lines.extend([f"[{relative_path}]", "", text, ""])
        else:
            lines.extend(
                [
                    "The installed wheel contains no standalone license file.",
                    "Its declared license and upstream project location are retained above.",
                    "",
                ]
            )
            missing_documents.append((display_name, license_name, urls))

    platform_only = sorted(expected - installed.keys())
    if platform_only:
        lines.extend(
            [
                "-" * 78,
                "Platform-conditional locked packages not installed on this host",
                "",
            ]
        )
        for name in platform_only:
            companion = {
                "colorama": "COLORAMA-0.4.6-LICENSE.txt",
                "pywin32": "PYWIN32-312-LICENSES.txt",
            }.get(name)
            suffix = f"; see {companion}" if companion else ""
            lines.append(f"* {name}{suffix}")
        lines.append("")

    OUTPUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        f"[python-licenses] wrote {OUTPUT.relative_to(REPOSITORY_ROOT)} for "
        f"{len(installed)} installed packages; {len(platform_only)} platform-only; "
        f"{len(missing_documents)} metadata-only"
    )


if __name__ == "__main__":
    main()
