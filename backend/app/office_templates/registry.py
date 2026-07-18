"""Private, content-addressed registry for immutable Office templates."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import stat
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Final, cast

from app.office_templates.errors import (
    TemplateConflictError,
    TemplateContractError,
    TemplateInUseError,
    TemplateIntegrityError,
    TemplateNotFoundError,
)
from app.office_templates.models import (
    TemplatePackageManifest,
    TemplateRecord,
    expected_extension,
    validate_reference_id,
    validate_sha256,
    validate_template_id,
    validate_template_version,
)
from app.office_templates.validation import TemplateSafetyLimits, inspect_ooxml_package


REGISTRY_RECORD_SCHEMA_VERSION: Final = 1
MAX_REGISTRY_RECORD_BYTES: Final = 2 * 1024 * 1024


class OfficeTemplateRegistry:
    """Store immutable manifests separately from deduplicated OOXML objects.

    The registry is intentionally local and application-private.  It has no
    runtime router integration and performs full OOXML validation again on
    every read so a modified on-disk object is never trusted as a cache hit.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        limits: TemplateSafetyLimits | None = None,
    ) -> None:
        try:
            raw_root = Path(root).expanduser()
        except TypeError as exc:
            raise TemplateContractError("Office template registry root is invalid") from exc
        if not raw_root.is_absolute():
            raise TemplateContractError(
                "Office template registry root must be absolute"
            )
        _reject_existing_symlink_components(raw_root)
        try:
            raw_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise TemplateIntegrityError(
                "Office template registry root cannot be created"
            ) from exc
        if raw_root.is_symlink() or not raw_root.is_dir():
            raise TemplateIntegrityError(
                "Office template registry root is redirected or invalid"
            )
        self.root = raw_root.resolve(strict=True)
        self.limits = limits or TemplateSafetyLimits()
        if not isinstance(self.limits, TemplateSafetyLimits):
            raise TemplateContractError("Office template safety limits are invalid")
        self._lock = threading.RLock()
        _harden_directory(self.root)
        self._objects = self.root / "objects"
        self._records = self.root / "records"
        self._staging = self.root / ".staging"
        self._trash = self.root / ".trash"
        for directory in (
            self._objects,
            self._records,
            self._staging,
            self._trash,
        ):
            _ensure_private_directory(directory, self.root)

    def import_template(
        self,
        manifest: TemplatePackageManifest,
        source_path: str | Path,
    ) -> TemplateRecord:
        """Validate and import one immutable id/version from a regular file."""

        if not isinstance(manifest, TemplatePackageManifest):
            raise TemplateContractError("manifest must be TemplatePackageManifest")
        source = _validate_source_path(source_path, manifest)
        content = _read_regular_file(
            source,
            max_bytes=self.limits.max_package_bytes,
            error_type=TemplateContractError,
            require_private=False,
        )
        digest = hashlib.sha256(content).hexdigest()
        if digest != manifest.source_sha256:
            raise TemplateContractError(
                "template source SHA-256 does not match the manifest"
            )
        inspect_ooxml_package(
            content,
            manifest.format,
            expected_placeholders=manifest.required_placeholders,
            limits=self.limits,
        )

        with self._lock:
            record_dir = self._record_dir(*manifest.immutable_key)
            if record_dir.exists() or record_dir.is_symlink():
                existing, _ = self._load_locked(*manifest.immutable_key)
                if existing.manifest.canonical_bytes() != manifest.canonical_bytes():
                    raise TemplateConflictError(
                        "immutable Office template id/version already exists"
                    )
                return existing

            object_path = self._ensure_object(content, manifest)
            template_parent = record_dir.parent
            _ensure_private_directory(template_parent, self._records)
            staged = Path(
                tempfile.mkdtemp(prefix="record-", dir=self._staging)
            ).resolve(strict=True)
            _assert_within(self._staging, staged)
            _harden_directory(staged)
            try:
                _atomic_write(
                    staged / "record.json",
                    _encode_record(manifest, ()),
                    replace=False,
                )
                _fsync_directory(staged)
                try:
                    os.rename(staged, record_dir)
                except FileExistsError:
                    existing, _ = self._load_locked(*manifest.immutable_key)
                    if (
                        existing.manifest.canonical_bytes()
                        != manifest.canonical_bytes()
                    ):
                        raise TemplateConflictError(
                            "immutable Office template id/version already exists"
                        )
                    return existing
                except OSError as exc:
                    raise TemplateIntegrityError(
                        "Office template record cannot be published atomically"
                    ) from exc
                _fsync_directory(template_parent)
            finally:
                if staged.exists() and staged != record_dir:
                    shutil.rmtree(staged, ignore_errors=True)

            record, loaded = self._load_locked(*manifest.immutable_key)
            if record.content_path != object_path or loaded != content:
                raise TemplateIntegrityError(
                    "published Office template does not match its imported object"
                )
            return record

    def list_templates(self, template_id: str | None = None) -> tuple[TemplateRecord, ...]:
        """List records in stable id/version order, validating every object."""

        if template_id is not None:
            validate_template_id(template_id)
        with self._lock:
            records: list[TemplateRecord] = []
            for record_dir in self._iter_record_dirs(template_id):
                record, _ = self._load_record_dir(record_dir)
                records.append(record)
            return tuple(
                sorted(records, key=lambda item: item.manifest.immutable_key)
            )

    def read(self, template_id: str, template_version: str) -> TemplateRecord:
        """Read and fully validate one immutable template version."""

        with self._lock:
            record, _ = self._load_locked(template_id, template_version)
            return record

    def read_source(
        self,
        template_id: str,
        template_version: str,
    ) -> tuple[TemplateRecord, bytes]:
        """Return a validated record and its validated immutable OOXML bytes."""

        with self._lock:
            return self._load_locked(template_id, template_version)

    def retain(
        self,
        template_id: str,
        template_version: str,
        reference_id: str,
    ) -> TemplateRecord:
        """Idempotently add one durable reference that protects deletion."""

        reference = validate_reference_id(reference_id)
        with self._lock:
            record, _ = self._load_locked(template_id, template_version)
            references = tuple(sorted(set(record.reference_ids) | {reference}))
            if references != record.reference_ids:
                self._replace_references(record, references)
            refreshed, _ = self._load_locked(template_id, template_version)
            return refreshed

    def release(
        self,
        template_id: str,
        template_version: str,
        reference_id: str,
    ) -> TemplateRecord:
        """Idempotently remove a durable reference."""

        reference = validate_reference_id(reference_id)
        with self._lock:
            record, _ = self._load_locked(template_id, template_version)
            references = tuple(
                item for item in record.reference_ids if item != reference
            )
            if references != record.reference_ids:
                self._replace_references(record, references)
            refreshed, _ = self._load_locked(template_id, template_version)
            return refreshed

    def delete(self, template_id: str, template_version: str) -> TemplateRecord:
        """Delete an unreferenced record and its now-unreferenced object."""

        with self._lock:
            record, _ = self._load_locked(template_id, template_version)
            if record.reference_ids:
                raise TemplateInUseError(
                    "Office template has durable references and cannot be deleted"
                )
            record_dir = self._record_dir(template_id, template_version)
            quarantine = self._trash / f"record-{uuid.uuid4().hex}"
            _assert_within(self._trash, quarantine, strict=False)
            try:
                os.rename(record_dir, quarantine)
                _fsync_directory(record_dir.parent)
            except OSError as exc:
                raise TemplateIntegrityError(
                    "Office template record cannot be hidden atomically"
                ) from exc
            try:
                shutil.rmtree(quarantine, ignore_errors=False)
                _fsync_directory(self._trash)
            except OSError as exc:
                raise TemplateIntegrityError(
                    "Office template record was hidden but cleanup failed"
                ) from exc
            try:
                record_dir.parent.rmdir()
            except OSError:
                pass

            source_in_use = any(
                candidate.manifest.source_sha256 == record.manifest.source_sha256
                for candidate in self.list_templates()
            )
            if not source_in_use:
                object_path = self._object_path(
                    record.manifest.source_sha256,
                    record.manifest.format,
                )
                if object_path.is_symlink() or not object_path.is_file():
                    raise TemplateIntegrityError(
                        "Office template object disappeared during deletion"
                    )
                try:
                    object_path.unlink()
                    _fsync_directory(object_path.parent)
                except OSError as exc:
                    raise TemplateIntegrityError(
                        "unreferenced Office template object cannot be deleted"
                    ) from exc
                try:
                    object_path.parent.rmdir()
                except OSError as exc:
                    if exc.errno not in {errno.ENOTEMPTY, errno.EEXIST}:
                        raise TemplateIntegrityError(
                            "Office template object directory cannot be cleaned"
                        ) from exc
            return record

    def _replace_references(
        self,
        record: TemplateRecord,
        references: tuple[str, ...],
    ) -> None:
        record_dir = self._record_dir(*record.manifest.immutable_key)
        _assert_safe_directory(record_dir, self._records)
        _atomic_write(
            record_dir / "record.json",
            _encode_record(record.manifest, references),
            replace=True,
        )
        _fsync_directory(record_dir)

    def _load_locked(
        self,
        template_id: str,
        template_version: str,
    ) -> tuple[TemplateRecord, bytes]:
        record_dir = self._record_dir(template_id, template_version)
        if not record_dir.exists() and not record_dir.is_symlink():
            raise TemplateNotFoundError(
                f"Office template {template_id}@{template_version} was not found"
            )
        return self._load_record_dir(record_dir)

    def _load_record_dir(
        self,
        record_dir: Path,
    ) -> tuple[TemplateRecord, bytes]:
        _assert_safe_directory(record_dir, self._records)
        payload = _read_regular_file(
            record_dir / "record.json",
            max_bytes=MAX_REGISTRY_RECORD_BYTES,
            error_type=TemplateIntegrityError,
            require_private=True,
        )
        try:
            manifest, references = _decode_record(payload)
        except TemplateIntegrityError:
            raise
        except Exception as exc:
            raise TemplateIntegrityError("Office template record is corrupt") from exc
        expected_dir = self._record_dir(*manifest.immutable_key)
        if record_dir != expected_dir:
            raise TemplateIntegrityError(
                "Office template record path does not match its manifest"
            )
        object_path = self._object_path(manifest.source_sha256, manifest.format)
        content = _read_regular_file(
            object_path,
            max_bytes=self.limits.max_package_bytes,
            error_type=TemplateIntegrityError,
            require_private=True,
        )
        if hashlib.sha256(content).hexdigest() != manifest.source_sha256:
            raise TemplateIntegrityError("Office template object digest is corrupt")
        try:
            inspect_ooxml_package(
                content,
                manifest.format,
                expected_placeholders=manifest.required_placeholders,
                limits=self.limits,
            )
        except Exception as exc:
            raise TemplateIntegrityError(
                "stored Office template object no longer passes validation"
            ) from exc
        return (
            TemplateRecord(
                manifest=manifest,
                content_path=object_path,
                reference_count=len(references),
                reference_ids=references,
            ),
            content,
        )

    def _ensure_object(
        self,
        content: bytes,
        manifest: TemplatePackageManifest,
    ) -> Path:
        object_path = self._object_path(manifest.source_sha256, manifest.format)
        _ensure_private_directory(object_path.parent, self._objects)
        if object_path.exists() or object_path.is_symlink():
            existing = _read_regular_file(
                object_path,
                max_bytes=self.limits.max_package_bytes,
                error_type=TemplateIntegrityError,
                require_private=True,
            )
            if existing != content:
                raise TemplateIntegrityError(
                    "content-addressed Office template object is corrupt"
                )
            return object_path
        try:
            _atomic_write(object_path, content, replace=False)
        except FileExistsError:
            existing = _read_regular_file(
                object_path,
                max_bytes=self.limits.max_package_bytes,
                error_type=TemplateIntegrityError,
                require_private=True,
            )
            if existing != content:
                raise TemplateIntegrityError(
                    "content-addressed Office template object is corrupt"
                )
        _fsync_directory(object_path.parent)
        return object_path

    def _object_path(self, digest: str, format_name: str) -> Path:
        validated_digest = validate_sha256(digest, "template source SHA-256")
        extension = expected_extension(cast(Any, format_name))
        path = self._objects / validated_digest[:2] / (
            validated_digest + extension
        )
        _assert_within(self._objects, path, strict=False)
        return path

    def _record_dir(self, template_id: str, template_version: str) -> Path:
        identifier = validate_template_id(template_id)
        version = validate_template_version(template_version)
        path = self._records / identifier / version
        _assert_within(self._records, path, strict=False)
        return path

    def _iter_record_dirs(self, template_id: str | None) -> tuple[Path, ...]:
        if template_id is not None:
            parents = (self._records / template_id,)
            if not parents[0].exists() and not parents[0].is_symlink():
                return ()
        else:
            try:
                parents = tuple(sorted(self._records.iterdir(), key=lambda item: item.name))
            except OSError as exc:
                raise TemplateIntegrityError(
                    "Office template registry cannot be listed"
                ) from exc
        record_dirs: list[Path] = []
        for parent in parents:
            try:
                validate_template_id(parent.name)
            except TemplateContractError as exc:
                raise TemplateIntegrityError(
                    "Office template registry contains an invalid id directory"
                ) from exc
            _assert_safe_directory(parent, self._records)
            try:
                versions = tuple(sorted(parent.iterdir(), key=lambda item: item.name))
            except OSError as exc:
                raise TemplateIntegrityError(
                    "Office template versions cannot be listed"
                ) from exc
            for version_dir in versions:
                try:
                    validate_template_version(version_dir.name)
                except TemplateContractError as exc:
                    raise TemplateIntegrityError(
                        "Office template registry contains an invalid version directory"
                    ) from exc
                _assert_safe_directory(version_dir, self._records)
                record_dirs.append(version_dir)
        return tuple(record_dirs)


def _validate_source_path(
    source_path: str | Path,
    manifest: TemplatePackageManifest,
) -> Path:
    try:
        source = Path(source_path).expanduser()
    except TypeError as exc:
        raise TemplateContractError("template source path is invalid") from exc
    if not source.is_absolute():
        raise TemplateContractError("template source path must be absolute")
    if source.suffix.casefold() != expected_extension(manifest.format):
        raise TemplateContractError(
            "template source extension does not match the manifest format"
        )
    if source.is_symlink():
        raise TemplateContractError("template source cannot be a symbolic link")
    try:
        resolved = source.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise TemplateContractError("template source does not exist") from exc
    if not resolved.is_file():
        raise TemplateContractError("template source must be a regular file")
    return resolved


def _encode_record(
    manifest: TemplatePackageManifest,
    references: tuple[str, ...],
) -> bytes:
    normalized = tuple(references)
    if tuple(sorted(set(normalized))) != normalized:
        raise TemplateContractError("template reference ids must be sorted and unique")
    for reference in normalized:
        validate_reference_id(reference)
    record = {
        "schema_version": REGISTRY_RECORD_SCHEMA_VERSION,
        "manifest": manifest.to_dict(),
        "template_sha256": manifest.template_sha256,
        "references": list(normalized),
    }
    record_bytes = _canonical_json(record)
    envelope = {
        "record": record,
        "record_sha256": hashlib.sha256(record_bytes).hexdigest(),
    }
    return _canonical_json(envelope)


def _decode_record(payload: bytes) -> tuple[TemplatePackageManifest, tuple[str, ...]]:
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise TemplateIntegrityError("Office template record is not strict JSON") from exc
    if not isinstance(value, dict) or set(value) != {"record", "record_sha256"}:
        raise TemplateIntegrityError("Office template record envelope is invalid")
    record = value["record"]
    recorded_sha = value["record_sha256"]
    try:
        validate_sha256(recorded_sha, "record SHA-256")
    except TemplateContractError as exc:
        raise TemplateIntegrityError("Office template record digest is invalid") from exc
    if hashlib.sha256(_canonical_json(record)).hexdigest() != recorded_sha:
        raise TemplateIntegrityError("Office template record digest does not match")
    expected_fields = {
        "schema_version",
        "manifest",
        "template_sha256",
        "references",
    }
    if not isinstance(record, dict) or set(record) != expected_fields:
        raise TemplateIntegrityError("Office template record fields are invalid")
    if (
        not isinstance(record["schema_version"], int)
        or isinstance(record["schema_version"], bool)
        or record["schema_version"] != REGISTRY_RECORD_SCHEMA_VERSION
    ):
        raise TemplateIntegrityError("Office template record schema is unsupported")
    try:
        manifest = TemplatePackageManifest.from_dict(record["manifest"])
    except TemplateContractError as exc:
        raise TemplateIntegrityError("Office template manifest is corrupt") from exc
    if record["template_sha256"] != manifest.template_sha256:
        raise TemplateIntegrityError("Office template manifest digest does not match")
    raw_references = record["references"]
    if not isinstance(raw_references, list):
        raise TemplateIntegrityError("Office template references are invalid")
    references = tuple(raw_references)
    try:
        if tuple(sorted(set(references))) != references:
            raise TemplateContractError("references are not sorted and unique")
        for reference in references:
            validate_reference_id(reference)
    except (TemplateContractError, TypeError) as exc:
        raise TemplateIntegrityError("Office template references are corrupt") from exc
    if payload != _encode_record(manifest, references):
        raise TemplateIntegrityError("Office template record is not canonical")
    return manifest, references


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_regular_file(
    path: Path,
    *,
    max_bytes: int,
    error_type: type[Exception],
    require_private: bool,
) -> bytes:
    if path.is_symlink():
        raise error_type(f"unsafe symbolic link: {path.name}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise error_type(f"regular file cannot be opened: {path.name}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise error_type(f"path is not a regular file: {path.name}")
        if require_private and os.name != "nt" and before.st_mode & 0o077:
            raise error_type(f"registry file is not private: {path.name}")
        if before.st_size < 1 or before.st_size > max_bytes:
            raise error_type(f"regular file exceeds its byte budget: {path.name}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise error_type(f"regular file exceeds its byte budget: {path.name}")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or total != after.st_size
        ):
            raise error_type(f"regular file changed while reading: {path.name}")
        return b"".join(chunks)
    except OSError as exc:
        raise error_type(f"regular file cannot be read: {path.name}") from exc
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, payload: bytes, *, replace: bool) -> None:
    _assert_safe_directory(path.parent, path.parent)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
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
        if replace:
            if path.is_symlink() or not path.is_file():
                raise TemplateIntegrityError(
                    "registry record disappeared before atomic replacement"
                )
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path, follow_symlinks=False)
            except TypeError:
                os.link(temporary, path)
            temporary.unlink()
        if os.name != "nt":
            os.chmod(path, 0o600, follow_symlinks=False)
        _fsync_directory(path.parent)
    except FileExistsError:
        raise
    except TemplateIntegrityError:
        raise
    except OSError as exc:
        raise TemplateIntegrityError(
            "registry file cannot be written atomically"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _ensure_private_directory(path: Path, boundary: Path) -> None:
    _assert_within(boundary, path, strict=False)
    if path.is_symlink():
        raise TemplateIntegrityError("registry directory cannot be a symbolic link")
    try:
        path.mkdir(mode=0o700, parents=False, exist_ok=True)
    except FileNotFoundError as exc:
        raise TemplateIntegrityError("registry directory parent is missing") from exc
    except OSError as exc:
        raise TemplateIntegrityError("registry directory cannot be created") from exc
    _assert_safe_directory(path, boundary)
    _harden_directory(path)


def _assert_safe_directory(path: Path, boundary: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise TemplateIntegrityError("registry directory is redirected or invalid")
    _assert_within(boundary, path)
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise TemplateIntegrityError("registry directory is not private")


def _harden_directory(path: Path) -> None:
    if os.name != "nt":
        try:
            os.chmod(path, 0o700, follow_symlinks=False)
        except OSError as exc:
            raise TemplateIntegrityError("registry directory permissions cannot be set") from exc


def _assert_within(boundary: Path, candidate: Path, *, strict: bool = True) -> None:
    try:
        resolved_boundary = boundary.resolve(strict=True)
        resolved_candidate = candidate.resolve(strict=strict)
        resolved_candidate.relative_to(resolved_boundary)
    except (OSError, RuntimeError, ValueError) as exc:
        raise TemplateIntegrityError("registry path escapes its private root") from exc


def _reject_existing_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise TemplateIntegrityError(
                "Office template registry path contains a symbolic link"
            )


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
        raise TemplateIntegrityError("registry directory cannot be synchronized") from exc


__all__ = [
    "MAX_REGISTRY_RECORD_BYTES",
    "REGISTRY_RECORD_SCHEMA_VERSION",
    "OfficeTemplateRegistry",
]
