"""Deterministic, text-only instantiation of validated OOXML templates."""

from __future__ import annotations

import copy
import hashlib
import os
import stat
import uuid
import zipfile
from collections import Counter
from collections.abc import Mapping
from io import BytesIO
from pathlib import Path

from app.office_templates.errors import (
    TemplateContractError,
    TemplateInstantiationError,
    TemplateIntegrityError,
)
from app.office_templates.models import (
    TemplateChange,
    TemplateInstantiationResult,
    expected_extension,
)
from app.office_templates.registry import OfficeTemplateRegistry
from app.office_templates.substitution import (
    is_substitutable_part,
    substitute_part,
    validate_placeholder_values,
)
from app.office_templates.validation import inspect_ooxml_package


class OfficeTemplateInstantiator:
    """Instantiate registry templates without code evaluation or model calls."""

    def __init__(self, registry: OfficeTemplateRegistry) -> None:
        if not isinstance(registry, OfficeTemplateRegistry):
            raise TemplateContractError("registry must be OfficeTemplateRegistry")
        self.registry = registry

    def instantiate(
        self,
        template_id: str,
        template_version: str,
        values: Mapping[str, object],
        *,
        staging_root: str | Path,
        output_path: str | Path,
    ) -> TemplateInstantiationResult:
        """Create one validated output beneath a caller-owned staging root.

        Only exact ``{{placeholder}}`` text tokens are substituted.  No Jinja,
        Python, shell, field expression, formula, relationship, or macro is
        evaluated.  An output is atomically exposed only after a full reopen.
        """

        record, source = self.registry.read_source(template_id, template_version)
        manifest = record.manifest
        normalized_values = validate_placeholder_values(
            manifest.required_placeholders,
            values,
        )
        destination = _validate_output_location(
            staging_root,
            output_path,
            expected_extension=expected_extension(manifest.format),
            allowed_extensions=manifest.allowed_output_rules.extensions,
        )

        inspection = inspect_ooxml_package(
            source,
            manifest.format,
            expected_placeholders=manifest.required_placeholders,
            limits=self.registry.limits,
        )
        output, changes = _rewrite_package(
            source,
            inspection.entries,
            manifest.format,
            normalized_values,
        )
        if len(output) > manifest.allowed_output_rules.max_output_bytes:
            raise TemplateInstantiationError(
                "instantiated Office output exceeds the manifest byte budget"
            )
        if len(output) > self.registry.limits.max_package_bytes:
            raise TemplateInstantiationError(
                "instantiated Office output exceeds the OOXML package budget"
            )
        expected_changes = Counter(inspection.placeholder_occurrences)
        actual_changes: Counter[str] = Counter()
        for (_part_name, placeholder), count in changes.items():
            actual_changes[placeholder] += count
        if actual_changes != expected_changes:
            raise TemplateIntegrityError(
                "Office template substitution change count is inconsistent"
            )
        inspect_ooxml_package(
            output,
            manifest.format,
            expected_placeholders=(),
            limits=self.registry.limits,
        )

        published = False
        try:
            _publish_without_overwrite(destination, output)
            published = True
            reopened = _read_published_output(
                destination,
                max_bytes=min(
                    manifest.allowed_output_rules.max_output_bytes,
                    self.registry.limits.max_package_bytes,
                ),
            )
            if reopened != output:
                raise TemplateIntegrityError(
                    "Office output changed while it was being published"
                )
            inspect_ooxml_package(
                reopened,
                manifest.format,
                expected_placeholders=(),
                limits=self.registry.limits,
            )
            output_sha256 = hashlib.sha256(reopened).hexdigest()
        except Exception:
            if published:
                try:
                    destination.unlink()
                except OSError:
                    pass
            raise

        change_list = tuple(
            TemplateChange(
                part_name=part_name,
                placeholder=placeholder,
                occurrences=count,
            )
            for (part_name, placeholder), count in sorted(changes.items())
        )
        return TemplateInstantiationResult(
            template_id=manifest.template_id,
            template_version=manifest.template_version,
            source_sha256=manifest.source_sha256,
            template_sha256=manifest.template_sha256,
            output_sha256=output_sha256,
            output_path=destination.resolve(strict=True),
            changes=change_list,
        )


def _rewrite_package(
    source: bytes,
    entries: Mapping[str, bytes],
    format_name: str,
    values: Mapping[str, str],
) -> tuple[bytes, Counter[tuple[str, str]]]:
    output = BytesIO()
    changes: Counter[tuple[str, str]] = Counter()
    try:
        with zipfile.ZipFile(BytesIO(source), "r") as input_archive:
            with zipfile.ZipFile(
                output,
                "w",
                allowZip64=False,
            ) as output_archive:
                for info in input_archive.infolist():
                    payload = entries[info.filename]
                    if is_substitutable_part(info.filename, format_name):
                        payload, part_changes = substitute_part(
                            info.filename,
                            payload,
                            format_name,
                            values,
                        )
                        for placeholder, count in part_changes.items():
                            changes[(info.filename, placeholder)] += count
                    cloned = copy.copy(info)
                    cloned.flag_bits &= ~(0x1 | 0x8)
                    cloned.CRC = 0
                    cloned.compress_size = 0
                    cloned.file_size = 0
                    output_archive.writestr(
                        cloned,
                        payload,
                        compress_type=info.compress_type,
                        compresslevel=9,
                    )
    except (
        KeyError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        zipfile.BadZipFile,
    ) as exc:
        raise TemplateInstantiationError(
            "validated Office template could not be rewritten deterministically"
        ) from exc
    return output.getvalue(), changes


def _validate_output_location(
    staging_root: str | Path,
    output_path: str | Path,
    *,
    expected_extension: str,
    allowed_extensions: tuple[str, ...],
) -> Path:
    try:
        root = Path(staging_root).expanduser()
        destination = Path(output_path).expanduser()
    except TypeError as exc:
        raise TemplateContractError("Office output path is invalid") from exc
    if not root.is_absolute() or not destination.is_absolute():
        raise TemplateContractError(
            "Office staging root and output path must be absolute"
        )
    if root.is_symlink() or not root.is_dir():
        raise TemplateContractError(
            "Office staging root must be an existing non-symlink directory"
        )
    if destination.suffix != expected_extension:
        raise TemplateContractError(
            "Office output extension does not match the template format"
        )
    if destination.suffix not in allowed_extensions:
        raise TemplateContractError(
            "Office output extension is not allowed by the manifest"
        )
    try:
        relative = destination.relative_to(root)
    except ValueError as exc:
        raise TemplateContractError(
            "Office output must be beneath the caller staging root"
        ) from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise TemplateContractError("Office output path contains unsafe segments")
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink() or not current.is_dir():
            raise TemplateContractError(
                "Office output parent must be an existing non-symlink directory"
            )
    try:
        resolved_root = root.resolve(strict=True)
        resolved_parent = destination.parent.resolve(strict=True)
        resolved_parent.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise TemplateContractError(
            "Office output resolves outside the caller staging root"
        ) from exc
    normalized = resolved_parent / destination.name
    if normalized.exists() or normalized.is_symlink():
        raise TemplateContractError("Office output already exists")
    return normalized


def _publish_without_overwrite(destination: Path, payload: bytes) -> None:
    temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, flags, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        try:
            os.link(temporary, destination, follow_symlinks=False)
        except TypeError:
            os.link(temporary, destination)
        temporary.unlink()
        _fsync_directory(destination.parent)
    except FileExistsError as exc:
        raise TemplateContractError("Office output already exists") from exc
    except OSError as exc:
        raise TemplateInstantiationError(
            "Office output could not be published atomically"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_published_output(path: Path, *, max_bytes: int) -> bytes:
    if path.is_symlink():
        raise TemplateIntegrityError("published Office output is a symbolic link")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise TemplateIntegrityError("published Office output cannot be reopened") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 1 <= before.st_size <= max_bytes:
            raise TemplateIntegrityError("published Office output is invalid")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise TemplateIntegrityError(
                    "published Office output exceeds its byte budget"
                )
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or total != after.st_size
        ):
            raise TemplateIntegrityError(
                "published Office output changed while reopening"
            )
        return b"".join(chunks)
    except OSError as exc:
        raise TemplateIntegrityError("published Office output cannot be read") from exc
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise TemplateInstantiationError(
            "Office staging directory cannot be synchronized"
        ) from exc


__all__ = ["OfficeTemplateInstantiator"]
