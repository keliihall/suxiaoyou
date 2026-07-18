from __future__ import annotations

import os
from importlib.machinery import ModuleSpec
from pathlib import Path
import stat
import sys
from types import ModuleType

import pytest

from app.office_rendering.attested import AuthoritativeRendererReleaseIdentity
from app.office_rendering.release_identity import (
    FrozenReleaseIdentityError,
    MAX_RELEASE_IDENTITY_BYTES,
    RELEASE_IDENTITY_BINDING_MODULE,
    RELEASE_IDENTITY_FILENAME,
    canonical_release_identity_bytes,
    load_frozen_renderer_release_identity,
)


COMMIT = "a" * 40
IDENTITY = AuthoritativeRendererReleaseIdentity(
    app_version="1.1.0",
    release_commit=COMMIT,
)


def _frozen_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw: bytes | None = None,
) -> tuple[Path, Path]:
    root = tmp_path / "frozen-root"
    data = root / "app" / "data"
    data.mkdir(parents=True, mode=0o700)
    path = data / RELEASE_IDENTITY_FILENAME
    path.write_bytes(raw if raw is not None else canonical_release_identity_bytes(IDENTITY))
    path.chmod(0o600)
    loader_module = ModuleType("pyimod02_importers")

    class PyiFrozenLoader:
        pass

    loader_module.PyiFrozenLoader = PyiFrozenLoader
    monkeypatch.setitem(sys.modules, "pyimod02_importers", loader_module)
    binding = ModuleType(RELEASE_IDENTITY_BINDING_MODULE)
    binding.__spec__ = ModuleSpec(
        RELEASE_IDENTITY_BINDING_MODULE,
        PyiFrozenLoader(),
        origin=str(root / f"{RELEASE_IDENTITY_BINDING_MODULE}.py"),
    )
    binding.RELEASE_IDENTITY_SHA256 = __import__("hashlib").sha256(
        canonical_release_identity_bytes(IDENTITY)
    ).hexdigest()
    monkeypatch.setitem(sys.modules, RELEASE_IDENTITY_BINDING_MODULE, binding)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(root), raising=False)
    return root, path


def test_missing_or_replayed_executable_binding_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _frozen_tree(tmp_path, monkeypatch)
    monkeypatch.delitem(sys.modules, RELEASE_IDENTITY_BINDING_MODULE)
    with pytest.raises(FrozenReleaseIdentityError, match="binding is unavailable"):
        load_frozen_renderer_release_identity()

    _frozen_tree(tmp_path / "second", monkeypatch)
    binding = sys.modules[RELEASE_IDENTITY_BINDING_MODULE]
    binding.RELEASE_IDENTITY_SHA256 = "b" * 64
    with pytest.raises(FrozenReleaseIdentityError, match="does not match"):
        load_frozen_renderer_release_identity()


def test_source_file_binding_cannot_shadow_the_frozen_archive_module(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _frozen_tree(tmp_path, monkeypatch)
    binding = sys.modules[RELEASE_IDENTITY_BINDING_MODULE]
    binding.__spec__ = ModuleSpec(
        RELEASE_IDENTITY_BINDING_MODULE,
        loader=object(),
        origin=str(tmp_path / f"{RELEASE_IDENTITY_BINDING_MODULE}.py"),
    )

    with pytest.raises(FrozenReleaseIdentityError, match="origin is invalid"):
        load_frozen_renderer_release_identity()


def test_loads_only_the_canonical_frozen_resource(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _frozen_tree(tmp_path, monkeypatch)
    monkeypatch.setenv("SUXIAOYOU_APP_VERSION", "9.9.9")
    monkeypatch.setenv("SUXIAOYOU_RELEASE_COMMIT", "b" * 40)

    assert load_frozen_renderer_release_identity() == IDENTITY


def test_source_mode_and_incomplete_frozen_state_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _path = _frozen_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    with pytest.raises(FrozenReleaseIdentityError, match="only in a frozen"):
        load_frozen_renderer_release_identity()

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    with pytest.raises(FrozenReleaseIdentityError, match="root is unavailable"):
        load_frozen_renderer_release_identity()

    monkeypatch.setattr(sys, "_MEIPASS", str(root / "missing"), raising=False)
    with pytest.raises(FrozenReleaseIdentityError, match="path is unavailable"):
        load_frozen_renderer_release_identity()


@pytest.mark.parametrize(
    "raw",
    [
        b'{"app_version":"1.1.0","release_commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","schema_version":1}',
        b'{ "app_version":"1.1.0","release_commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","schema_version":1}\n',
        b'{"schema_version":1,"release_commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","app_version":"1.1.0"}\n',
        b'{"app_version":"1.1.0","release_commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","schema_version":1}\r\n',
        b'{"app_version":"1.1.0","app_version":"1.1.0","release_commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","schema_version":1}\n',
        b'{"app_version":"1.1.0","release_commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","schema_version":1,"unexpected":true}\n',
        b'{"app_version":"1.1.0","release_commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}\n',
        b'{"app_version":"1.1.0","release_commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","schema_version":true}\n',
        b'{"app_version":"1.1.0","release_commit":"0000000000000000000000000000000000000000","schema_version":1}\n',
        b'{"app_version":"1.1.0","release_commit":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA","schema_version":1}\n',
        b'{"app_version":"1.1","release_commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","schema_version":1}\n',
        b"\xff\n",
    ],
)
def test_ambiguous_noncanonical_or_drifting_payloads_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw: bytes,
) -> None:
    _frozen_tree(tmp_path, monkeypatch, raw)

    with pytest.raises(FrozenReleaseIdentityError):
        load_frozen_renderer_release_identity()


def test_oversized_symlinked_hardlinked_and_special_files_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root, path = _frozen_tree(tmp_path, monkeypatch)
    path.write_bytes(b"x" * (MAX_RELEASE_IDENTITY_BYTES + 1))
    with pytest.raises(FrozenReleaseIdentityError, match="size is invalid"):
        load_frozen_renderer_release_identity()

    path.unlink()
    target = tmp_path / "outside-identity.json"
    target.write_bytes(canonical_release_identity_bytes(IDENTITY))
    path.symlink_to(target)
    with pytest.raises(FrozenReleaseIdentityError, match="regular file"):
        load_frozen_renderer_release_identity()

    path.unlink()
    target.chmod(0o600)
    os.link(target, path)
    with pytest.raises(FrozenReleaseIdentityError, match="hard-linked"):
        load_frozen_renderer_release_identity()

    path.unlink()
    os.mkfifo(path, mode=0o600)
    assert stat.S_ISFIFO(path.lstat().st_mode)
    with pytest.raises(FrozenReleaseIdentityError, match="regular file"):
        load_frozen_renderer_release_identity()


def test_symlinked_path_components_and_unsafe_modes_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, path = _frozen_tree(tmp_path, monkeypatch)
    path.chmod(0o666)
    with pytest.raises(FrozenReleaseIdentityError, match="permissions are unsafe"):
        load_frozen_renderer_release_identity()

    path.chmod(0o700)
    with pytest.raises(FrozenReleaseIdentityError, match="file mode is unsafe"):
        load_frozen_renderer_release_identity()

    path.chmod(0o600)
    data = path.parent
    data.chmod(0o777)
    with pytest.raises(FrozenReleaseIdentityError, match="permissions are unsafe"):
        load_frozen_renderer_release_identity()
    data.chmod(0o700)

    real_app = tmp_path / "real-app"
    (real_app / "data").mkdir(parents=True)
    (real_app / "data" / RELEASE_IDENTITY_FILENAME).write_bytes(
        canonical_release_identity_bytes(IDENTITY)
    )
    (root / "app" / "data" / RELEASE_IDENTITY_FILENAME).unlink()
    (root / "app" / "data").rmdir()
    (root / "app").rmdir()
    (root / "app").symlink_to(real_app, target_is_directory=True)
    with pytest.raises(FrozenReleaseIdentityError, match="path is invalid"):
        load_frozen_renderer_release_identity()
