"""Fail-closed loading of a release-attested Office renderer.

This module is deliberately separate from the convenient local LibreOffice
factory.  A host installation is useful for an *approximate* preview, but it
must never acquire ``authoritative`` quality merely because it happens to be
present or because somebody supplied an environment variable.  The only
upgrade path here is a release signed manifest whose exact component and font
identities still match an explicitly selected deployment.

The default deployment is application data (including PyInstaller's private
resource directory).  A caller may pass an ``AttestedOfficeRendererDeployment``
for a separately installed, private renderer bundle, but the public key remains
compiled into this module and every supplied file is re-hashed.  This is an
assembly boundary for trusted application code, not a user preference API.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import stat
import sys
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final, Mapping

from app.office_rendering.attested import (
    ATTESTATION_SCHEMA_VERSION,
    AttestedOfficeRenderProvider,
    AuthoritativeRendererAttestation,
    AuthoritativeRendererReleaseIdentity,
)
from app.office_rendering.errors import RenderContractError
from app.office_rendering.libreoffice import (
    ExecutableIdentity,
    LibreOfficeRenderProvider,
    LibreOfficeToolchain,
)
from app.office_rendering.models import (
    AUTHORITATIVE_QUALITY,
    RendererDescriptor,
    validate_sha256,
)
from app.office_rendering.native_bundle import (
    DEPENDENCY_MANIFEST_FILENAME,
    NativeBundleClosure,
    NativeBundleVerificationError,
    verify_native_bundle,
)
from app.office_rendering.native_sandbox import (
    SANDBOX_MANIFEST_FILENAME,
    NativeSandboxContract,
    NativeSandboxContractError,
    load_native_sandbox_contract,
)
from app.office_rendering.provider import (
    OfficeRenderProvider,
    ProviderAvailability,
    UnavailableOfficeRenderProvider,
)
from app.office_rendering.runtime import (
    FontFingerprintError,
    fingerprint_font_environment,
)
from app.office_rendering.sandbox import BundledOfficeRendererSandbox
from release_packaging.office_renderer_trust import (
    AUTHORITATIVE_RENDERER_PUBLIC_KEY,
)


# This is a public verification key, not a signing key.  Release engineering
# rotates it only through a reviewed application update; neither a setting nor
# an environment variable can replace it at runtime.
ATTESTATION_FILENAME: Final = "office-renderer-attestation.json"
MAX_ATTESTATION_BYTES: Final = 32 * 1024
MAX_COMPONENT_BYTES: Final = 1024 * 1024 * 1024
MAX_BUNDLE_FILES: Final = 100_000
MAX_BUNDLE_BYTES: Final = 8 * 1024 * 1024 * 1024
_UNAVAILABLE_REASON: Final = "Authoritative Office renderer is unavailable"
_FONT_SUFFIXES: Final = frozenset({".otf", ".ttf", ".ttc"})
_BUNDLE_TREE_COMPONENT: Final = "bundle-tree"
_UNIX_COMPONENT_PATHS: Final = MappingProxyType(
    {
        "dependency-manifest": PurePosixPath(DEPENDENCY_MANIFEST_FILENAME),
        "font-manifest": PurePosixPath("font-manifest.json"),
        "license-manifest": PurePosixPath("license-manifest.json"),
        "pdftoppm": PurePosixPath("bin/pdftoppm"),
        "sandbox-manifest": PurePosixPath(SANDBOX_MANIFEST_FILENAME),
        "soffice": PurePosixPath("bin/soffice"),
    }
)
_WINDOWS_COMPONENT_PATHS: Final = MappingProxyType(
    {
        "dependency-manifest": PurePosixPath(DEPENDENCY_MANIFEST_FILENAME),
        "font-manifest": PurePosixPath("font-manifest.json"),
        "license-manifest": PurePosixPath("license-manifest.json"),
        "pdftoppm": PurePosixPath("bin/pdftoppm.exe"),
        "sandbox-manifest": PurePosixPath(SANDBOX_MANIFEST_FILENAME),
        "soffice": PurePosixPath("bin/soffice.exe"),
    }
)
_COMPONENT_NAMES: Final = frozenset(_UNIX_COMPONENT_PATHS)


def _component_paths_for_target(target: str) -> Mapping[str, PurePosixPath]:
    if target.startswith("windows-"):
        return _WINDOWS_COMPONENT_PATHS
    if target.startswith(("darwin-", "linux-")):
        return _UNIX_COMPONENT_PATHS
    raise OfficeRendererDeploymentError("renderer platform target is unsupported")


def _default_component_paths() -> Mapping[str, PurePosixPath]:
    return _component_paths_for_target(current_platform_target())


class OfficeRendererDeploymentError(RuntimeError):
    """A release renderer bundle cannot safely be used."""


@dataclass(frozen=True, slots=True)
class AttestedOfficeRendererDeployment:
    """An application-owned or explicitly private renderer bundle.

    ``root`` is intentionally supplied by the trusted runtime composition,
    rather than discovered from configuration or the environment.  Component
    paths are relative POSIX paths so an attestation cannot redirect the
    loader to arbitrary host files.
    """

    root: Path
    component_paths: Mapping[str, PurePosixPath] = field(
        default_factory=_default_component_paths
    )
    font_roots: tuple[PurePosixPath, ...] = (PurePosixPath("fonts"),)
    attestation_filename: str = ATTESTATION_FILENAME

    def __post_init__(self) -> None:
        root = Path(self.root)
        if not root.is_absolute():
            raise OfficeRendererDeploymentError("renderer deployment root is invalid")
        object.__setattr__(self, "root", root)
        if (
            not isinstance(self.attestation_filename, str)
            or self.attestation_filename != ATTESTATION_FILENAME
        ):
            raise OfficeRendererDeploymentError("renderer attestation name is invalid")
        try:
            component_paths = dict(self.component_paths)
        except (TypeError, ValueError) as exc:
            raise OfficeRendererDeploymentError("renderer component paths are invalid") from exc
        if tuple(component_paths) != tuple(sorted(component_paths)):
            raise OfficeRendererDeploymentError("renderer component paths are not canonical")
        if set(component_paths) != _COMPONENT_NAMES:
            raise OfficeRendererDeploymentError("renderer component set is invalid")
        normalized_components: dict[str, PurePosixPath] = {}
        for name, raw_path in component_paths.items():
            normalized_components[name] = _relative_component_path(raw_path)
        if normalized_components not in (
            dict(_UNIX_COMPONENT_PATHS),
            dict(_WINDOWS_COMPONENT_PATHS),
        ):
            raise OfficeRendererDeploymentError("renderer component layout is invalid")
        object.__setattr__(self, "component_paths", MappingProxyType(normalized_components))
        if not isinstance(self.font_roots, tuple) or not self.font_roots:
            raise OfficeRendererDeploymentError("renderer font roots are invalid")
        normalized_fonts = tuple(_relative_component_path(path) for path in self.font_roots)
        if len(set(normalized_fonts)) != len(normalized_fonts):
            raise OfficeRendererDeploymentError("renderer font roots are invalid")
        object.__setattr__(self, "font_roots", normalized_fonts)


@dataclass(frozen=True, slots=True)
class AuthoritativeRendererProbeBinding:
    """Private bundle location paired with its signed tree identity.

    The path is execution input, never release evidence.  Probe reports expose
    only ``bundle_tree_sha256`` and other hashes/counts.
    """

    bundle_root: Path = field(repr=False)
    bundle_tree_sha256: str

    def __post_init__(self) -> None:
        root = Path(self.bundle_root)
        if not root.is_absolute():
            raise OfficeRendererDeploymentError(
                "renderer probe bundle root is invalid"
            )
        object.__setattr__(self, "bundle_root", root)
        try:
            digest = validate_sha256(
                self.bundle_tree_sha256,
                "renderer probe bundle tree",
            )
        except RenderContractError as exc:
            raise OfficeRendererDeploymentError(
                "renderer probe bundle identity is invalid"
            ) from exc
        object.__setattr__(self, "bundle_tree_sha256", digest)


def build_attested_office_render_provider(
    *,
    deployment: AttestedOfficeRendererDeployment | None = None,
    release_identity: AuthoritativeRendererReleaseIdentity | None = None,
) -> OfficeRenderProvider:
    """Return an exact signed renderer or a stable unavailable provider.

    This function intentionally catches all bundle, filesystem, crypto, and
    platform failures.  Callers can safely install its result and do not need
    to turn initialization errors into a potentially misleading fallback.
    No resource allocated by this factory needs shutdown cleanup: rendering
    subprocesses are owned and reaped by ``LibreOfficeRenderProvider`` per
    request.
    """

    try:
        if not isinstance(
            release_identity,
            AuthoritativeRendererReleaseIdentity,
        ):
            raise OfficeRendererDeploymentError(
                "renderer release identity is unavailable"
            )
        selected = deployment or default_attested_office_renderer_deployment()
        if not isinstance(selected, AttestedOfficeRendererDeployment):
            raise OfficeRendererDeploymentError("renderer deployment is invalid")
        return _load_attested_office_render_provider(
            selected,
            release_identity=release_identity,
        )
    except Exception:
        # Startup and the API intentionally expose one path-free reason.  In
        # particular, a malformed signed file must not become an oracle for
        # private installation paths or release metadata.
        return UnavailableOfficeRenderProvider(_UNAVAILABLE_REASON)


def authoritative_office_renderer_self_test(
    *,
    deployment: AttestedOfficeRendererDeployment | None = None,
    release_identity: AuthoritativeRendererReleaseIdentity | None = None,
) -> dict[str, Any]:
    """Return path-free evidence for the exact bundled authoritative renderer.

    The probe deliberately rebuilds the production provider and asks it to
    revalidate availability.  A descriptor alone is not enough: both the
    signed attestation and every private bundle/font identity must still match
    at the time of the probe.  Callers must treat any exception as a failed
    release check and must not substitute a host-installed renderer.
    """

    provider = build_attested_office_render_provider(
        deployment=deployment,
        release_identity=release_identity,
    )
    if not isinstance(provider, AttestedOfficeRenderProvider):
        raise OfficeRendererDeploymentError(_UNAVAILABLE_REASON)
    descriptor = provider.descriptor
    availability = provider.availability()
    attestation = provider.attestation
    if (
        not isinstance(descriptor, RendererDescriptor)
        or descriptor.quality != AUTHORITATIVE_QUALITY
        or not isinstance(availability, ProviderAvailability)
        or not availability.available
        or not isinstance(
            release_identity,
            AuthoritativeRendererReleaseIdentity,
        )
        or attestation.release_identity != release_identity
        or attestation.platform_target != current_platform_target()
        or descriptor.font_digest != attestation.font_digest
        or descriptor.renderer_version != f"attestation-{attestation.digest}"
    ):
        raise OfficeRendererDeploymentError(_UNAVAILABLE_REASON)
    components = tuple(attestation.components)
    expected_components = tuple(
        sorted((*_component_paths_for_target(attestation.platform_target), _BUNDLE_TREE_COMPONENT))
    )
    if components != expected_components:
        raise OfficeRendererDeploymentError(_UNAVAILABLE_REASON)
    bundle_tree_sha256 = attestation.components.get(_BUNDLE_TREE_COMPONENT)
    native_closure = getattr(provider.delegate, "native_closure", None)
    native_sandbox_contract = getattr(
        provider.delegate,
        "native_sandbox_contract",
        None,
    )
    process_sandbox = getattr(provider.delegate, "sandbox", None)
    if (
        not isinstance(bundle_tree_sha256, str)
        or not isinstance(native_closure, NativeBundleClosure)
        or not isinstance(native_sandbox_contract, NativeSandboxContract)
        or not isinstance(process_sandbox, BundledOfficeRendererSandbox)
        or native_closure.platform_target != attestation.platform_target
        or native_closure.manifest_sha256
        != attestation.components.get("dependency-manifest")
        or native_sandbox_contract.platform_target != attestation.platform_target
        or native_sandbox_contract.bundle_tree_sha256 != bundle_tree_sha256
        or native_sandbox_contract.dependency_manifest_sha256
        != native_closure.manifest_sha256
        or native_sandbox_contract.sandbox_manifest_sha256
        != attestation.components.get("sandbox-manifest")
    ):
        raise OfficeRendererDeploymentError(_UNAVAILABLE_REASON)
    return {
        "schema_version": ATTESTATION_SCHEMA_VERSION,
        "status": "ok",
        "available": True,
        "quality": descriptor.quality,
        "app_version": attestation.app_version,
        "release_commit": attestation.release_commit,
        "platform_target": attestation.platform_target,
        "renderer_id": descriptor.renderer_id,
        "renderer_version": descriptor.renderer_version,
        "font_digest": descriptor.font_digest,
        "attestation_sha256": attestation.digest,
        "bundle_tree_sha256": bundle_tree_sha256,
        "native_closure_sha256": native_closure.closure_sha256,
        "native_dependency_count": native_closure.dependency_count,
        "native_file_count": native_closure.native_file_count,
        "font_tree_sha256": process_sandbox.font_tree_sha256,
        "native_sandbox_contract": native_sandbox_contract.path_free_evidence(),
        "component_count": len(components),
        "components": list(components),
    }


def default_attested_office_renderer_deployment() -> AttestedOfficeRendererDeployment:
    """Locate only application-bundled renderer data, never a user directory."""

    root = _application_data_root() / "office-renderer" / current_platform_target()
    return AttestedOfficeRendererDeployment(root=root)


def bind_authoritative_renderer_probe(
    provider: OfficeRenderProvider,
    *,
    deployment: AttestedOfficeRendererDeployment | None = None,
) -> AuthoritativeRendererProbeBinding:
    """Bind an execution probe to the exact signed provider deployment.

    This deliberately does not execute the probe.  It is the small synchronous
    bridge needed by release/startup composition to obtain a private root and
    the signed bundle-tree digest without accepting either from environment or
    command-line input.
    """

    selected = deployment or default_attested_office_renderer_deployment()
    if (
        not isinstance(selected, AttestedOfficeRendererDeployment)
        or not isinstance(provider, AttestedOfficeRenderProvider)
    ):
        raise OfficeRendererDeploymentError(_UNAVAILABLE_REASON)
    availability = provider.availability()
    expected = provider.attestation.components.get(_BUNDLE_TREE_COMPONENT)
    if (
        not isinstance(availability, ProviderAvailability)
        or not availability.available
        or not isinstance(expected, str)
    ):
        raise OfficeRendererDeploymentError(_UNAVAILABLE_REASON)
    root = _private_directory(selected.root)
    actual = fingerprint_office_renderer_bundle(root)
    if actual != expected:
        raise OfficeRendererDeploymentError(_UNAVAILABLE_REASON)
    return AuthoritativeRendererProbeBinding(
        bundle_root=root,
        bundle_tree_sha256=expected,
    )


def bind_attested_native_sandbox_contract(
    provider: OfficeRenderProvider,
) -> NativeSandboxContract:
    """Return only the native contract bound to a live attested provider.

    Behavior probes must not accept a contract supplied by configuration or a
    caller-controlled path.  This bridge reuses the provider's fail-closed
    availability check, then binds the native closure, launcher declaration,
    signed manifests, platform, and bundle-tree identity before exposing the
    already validated contract object.
    """

    if not isinstance(provider, AttestedOfficeRenderProvider):
        raise OfficeRendererDeploymentError(_UNAVAILABLE_REASON)
    availability = provider.availability()
    attestation = provider.attestation
    native_closure = getattr(provider.delegate, "native_closure", None)
    contract = getattr(provider.delegate, "native_sandbox_contract", None)
    bundle_tree_sha256 = attestation.components.get(_BUNDLE_TREE_COMPONENT)
    if (
        not isinstance(availability, ProviderAvailability)
        or not availability.available
        or not isinstance(native_closure, NativeBundleClosure)
        or not isinstance(contract, NativeSandboxContract)
        or attestation.platform_target != current_platform_target()
        or contract.platform_target != attestation.platform_target
        or contract.bundle_tree_sha256 != bundle_tree_sha256
        or contract.dependency_manifest_sha256 != native_closure.manifest_sha256
        or contract.dependency_manifest_sha256
        != attestation.components.get("dependency-manifest")
        or contract.sandbox_manifest_sha256
        != attestation.components.get("sandbox-manifest")
    ):
        raise OfficeRendererDeploymentError(_UNAVAILABLE_REASON)
    return contract


def current_platform_target() -> str:
    """Return the exact release target for the executing interpreter."""

    system = platform.system().lower()
    machine = platform.machine().lower()
    normalized_machine = {
        "amd64": "x64",
        "x86_64": "x64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(machine)
    normalized_system = {"darwin": "darwin", "windows": "windows", "linux": "linux"}.get(
        system
    )
    if normalized_system is None or normalized_machine is None:
        raise OfficeRendererDeploymentError("renderer platform target is unsupported")
    return f"{normalized_system}-{normalized_machine}"


def fingerprint_office_renderer_bundle(root: Path) -> str:
    """Return the canonical identity of every deployed renderer file.

    The signed attestation is deliberately excluded to avoid a circular
    identity.  Everything else below ``root`` must be a private directory or
    regular file; symlinks and special files are not valid release contents.
    The digest binds each file's POSIX relative path, permission mode, size,
    and SHA-256.  It describes only the supplied bundle and does not attest to
    host dynamic-library closure or provide an operating-system sandbox.
    """

    private_root = _private_directory(Path(root))
    before = _bundle_tree_snapshot(private_root)
    digest = hashlib.sha256()
    for entry in before:
        file_digest = _sha256_private_file(
            _path_under(private_root, PurePosixPath(entry.relative_path)),
            max_bytes=MAX_COMPONENT_BYTES,
        )
        canonical = {
            "mode": entry.mode,
            "path": entry.relative_path,
            "sha256": file_digest,
            "size": entry.size,
        }
        digest.update(
            json.dumps(
                canonical,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
        )
        digest.update(b"\n")
    after = _bundle_tree_snapshot(private_root)
    if after != before:
        raise OfficeRendererDeploymentError("renderer bundle changed")
    return digest.hexdigest()


def _load_attested_office_render_provider(
    deployment: AttestedOfficeRendererDeployment,
    *,
    release_identity: AuthoritativeRendererReleaseIdentity,
) -> AttestedOfficeRenderProvider:
    root = _private_directory(deployment.root)
    expected_target = current_platform_target()
    attestation = _read_attestation(root / deployment.attestation_filename)
    if attestation.platform_target != expected_target:
        raise OfficeRendererDeploymentError("renderer platform target does not match")
    if dict(deployment.component_paths) != dict(_component_paths_for_target(expected_target)):
        raise OfficeRendererDeploymentError("renderer component layout does not match")
    if attestation.release_identity != release_identity:
        raise OfficeRendererDeploymentError("renderer release identity does not match")
    try:
        attestation.verify(AUTHORITATIVE_RENDERER_PUBLIC_KEY)
    except RenderContractError as exc:
        raise OfficeRendererDeploymentError(
            "renderer attestation signature is not trusted"
        ) from exc

    (
        component_paths,
        components,
        font_digest,
        native_closure,
        native_sandbox_contract,
    ) = _deployment_identities(
        deployment,
        root,
        platform_target=expected_target,
        attested_components=attestation.components,
    )

    toolchain = LibreOfficeToolchain(
        soffice=ExecutableIdentity(
            path=component_paths["soffice"], sha256=components["soffice"]
        ),
        pdftoppm=ExecutableIdentity(
            path=component_paths["pdftoppm"], sha256=components["pdftoppm"]
        ),
    )
    base_provider = LibreOfficeRenderProvider(
        font_digest=font_digest,
        toolchain=toolchain,
        platform_name=_platform_name_from_target(expected_target),
        native_sandbox_contract=native_sandbox_contract,
        # Rendering receives only the adapter's fixed minimal environment;
        # passing an empty mapping denies host environment injection here.
        environ={},
    )
    provider = _DeploymentPinnedProvider(
        deployment=deployment,
        root=root,
        delegate=base_provider,
        components=components,
        font_digest=font_digest,
        native_closure=native_closure,
        native_sandbox_contract=native_sandbox_contract,
        platform_target=expected_target,
    )
    # The wrapper verifies signature, base descriptor, platform, font digest,
    # and the complete component map again before exposing authoritative quality.
    return AttestedOfficeRenderProvider(
        provider,
        attestation=attestation,
        trusted_public_key=AUTHORITATIVE_RENDERER_PUBLIC_KEY,
        release_identity=release_identity,
        platform_target=expected_target,
        installed_components=components,
    )


class _DeploymentPinnedProvider:
    """Keep an attested provider closed if private files change after startup."""

    def __init__(
        self,
        *,
        deployment: AttestedOfficeRendererDeployment,
        root: Path,
        delegate: LibreOfficeRenderProvider,
        components: Mapping[str, str],
        font_digest: str,
        native_closure: NativeBundleClosure,
        native_sandbox_contract: NativeSandboxContract,
        platform_target: str,
    ) -> None:
        self._deployment = deployment
        self._root = root
        self._delegate = delegate
        self._components = dict(components)
        self._font_digest = font_digest
        self._native_closure = native_closure
        self._native_sandbox_contract = native_sandbox_contract
        self._platform_target = platform_target

    @property
    def descriptor(self):  # type: ignore[no-untyped-def]
        return self._delegate.descriptor

    @property
    def native_closure(self) -> NativeBundleClosure:
        return self._native_closure

    @property
    def native_sandbox_contract(self) -> NativeSandboxContract:
        return self._native_sandbox_contract

    @property
    def sandbox(self) -> BundledOfficeRendererSandbox | None:
        return self._delegate.sandbox

    def availability(self) -> ProviderAvailability:
        try:
            (
                _paths,
                components,
                font_digest,
                native_closure,
                native_sandbox_contract,
            ) = _deployment_identities(
                self._deployment,
                self._root,
                platform_target=self._platform_target,
                attested_components=self._components,
            )
            if (
                components != self._components
                or font_digest != self._font_digest
                or native_closure != self._native_closure
                or native_sandbox_contract != self._native_sandbox_contract
            ):
                return ProviderAvailability(available=False, reason=_UNAVAILABLE_REASON)
            return self._delegate.availability()
        except Exception:
            return ProviderAvailability(available=False, reason=_UNAVAILABLE_REASON)

    async def render(self, request, output_dir):  # type: ignore[no-untyped-def]
        availability = self.availability()
        if not availability.available:
            from app.office_rendering.errors import ProviderUnavailableError

            raise ProviderUnavailableError(availability.reason or _UNAVAILABLE_REASON)
        manifest = await self._delegate.render(request, output_dir)
        if not self.availability().available:
            from app.office_rendering.errors import ProviderUnavailableError

            raise ProviderUnavailableError(_UNAVAILABLE_REASON)
        return manifest


def _deployment_identities(
    deployment: AttestedOfficeRendererDeployment,
    root: Path,
    *,
    platform_target: str,
    attested_components: Mapping[str, str],
) -> tuple[
    dict[str, Path],
    dict[str, str],
    str,
    NativeBundleClosure,
    NativeSandboxContract,
]:
    """Read every signed identity again without trusting cached path objects."""

    current_root = _private_directory(root)
    tree_digest_before = fingerprint_office_renderer_bundle(current_root)
    component_paths = {
        name: _private_regular_file(current_root, relative, max_bytes=MAX_COMPONENT_BYTES)
        for name, relative in deployment.component_paths.items()
    }
    component_digests = {
        name: _sha256_private_file(path, max_bytes=MAX_COMPONENT_BYTES)
        for name, path in component_paths.items()
    }
    try:
        native_closure = verify_native_bundle(
            current_root,
            platform_target=platform_target,
            executable_paths=(
                deployment.component_paths["soffice"],
                deployment.component_paths["pdftoppm"],
            ),
        )
    except NativeBundleVerificationError as exc:
        raise OfficeRendererDeploymentError(
            "renderer native dependency closure is invalid"
        ) from exc
    if native_closure.manifest_sha256 != component_digests["dependency-manifest"]:
        raise OfficeRendererDeploymentError(
            "renderer native dependency manifest changed"
        )
    font_roots = tuple(
        _private_directory(_path_under(current_root, relative))
        for relative in deployment.font_roots
    )
    for font_root in font_roots:
        _validate_private_font_root(font_root)
    try:
        font_digest = fingerprint_font_environment(roots=font_roots)
    except FontFingerprintError as exc:
        raise OfficeRendererDeploymentError("renderer font identity is invalid") from exc
    tree_digest_after = fingerprint_office_renderer_bundle(current_root)
    if tree_digest_after != tree_digest_before:
        raise OfficeRendererDeploymentError("renderer bundle changed")
    components = dict(
        sorted(
            {
                **component_digests,
                _BUNDLE_TREE_COMPONENT: tree_digest_after,
            }.items()
        )
    )
    if components != dict(attested_components):
        raise OfficeRendererDeploymentError(
            "renderer attestation components do not match deployment"
        )
    try:
        native_sandbox_contract = load_native_sandbox_contract(
            current_root,
            platform_target=platform_target,
            attested_components=attested_components,
        )
    except NativeSandboxContractError as exc:
        raise OfficeRendererDeploymentError(
            "renderer native sandbox contract is invalid"
        ) from exc
    if (
        native_sandbox_contract.bundle_tree_sha256 != tree_digest_after
        or native_sandbox_contract.dependency_manifest_sha256
        != native_closure.manifest_sha256
    ):
        raise OfficeRendererDeploymentError(
            "renderer native sandbox identity does not match deployment"
        )
    return (
        component_paths,
        components,
        font_digest,
        native_closure,
        native_sandbox_contract,
    )


@dataclass(frozen=True, slots=True, order=True)
class _BundleFileIdentity:
    relative_path: str
    mode: int
    size: int
    device: int
    inode: int
    modified_ns: int
    changed_ns: int


def _bundle_tree_snapshot(root: Path) -> tuple[_BundleFileIdentity, ...]:
    """Capture a race-detecting inventory while rejecting unsafe tree nodes."""

    identities: list[_BundleFileIdentity] = []
    total_bytes = 0
    directories: list[tuple[Path, PurePosixPath]] = [(root, PurePosixPath())]
    try:
        while directories:
            directory, relative_directory = directories.pop()
            directory_info = directory.lstat()
            if stat.S_ISLNK(directory_info.st_mode) or not stat.S_ISDIR(
                directory_info.st_mode
            ):
                raise OfficeRendererDeploymentError("renderer bundle directory is invalid")
            _reject_group_or_world_writable(
                directory_info,
                "renderer bundle directory",
            )
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
            for entry in entries:
                relative = relative_directory / entry.name
                if relative == PurePosixPath(ATTESTATION_FILENAME):
                    continue
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise OfficeRendererDeploymentError("renderer bundle symlink is invalid")
                if stat.S_ISDIR(info.st_mode):
                    _reject_group_or_world_writable(
                        info,
                        "renderer bundle directory",
                    )
                    directories.append((Path(entry.path), relative))
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise OfficeRendererDeploymentError("renderer bundle file is invalid")
                _reject_group_or_world_writable(info, "renderer bundle file")
                if info.st_size > MAX_COMPONENT_BYTES:
                    raise OfficeRendererDeploymentError("renderer bundle file is invalid")
                total_bytes += info.st_size
                if (
                    len(identities) >= MAX_BUNDLE_FILES
                    or total_bytes > MAX_BUNDLE_BYTES
                ):
                    raise OfficeRendererDeploymentError("renderer bundle is too large")
                identities.append(
                    _BundleFileIdentity(
                        relative_path=relative.as_posix(),
                        mode=stat.S_IMODE(info.st_mode),
                        size=info.st_size,
                        device=info.st_dev,
                        inode=info.st_ino,
                        modified_ns=info.st_mtime_ns,
                        changed_ns=info.st_ctime_ns,
                    )
                )
    except OSError as exc:
        raise OfficeRendererDeploymentError("renderer bundle is unavailable") from exc
    identities.sort(key=lambda item: item.relative_path)
    return tuple(identities)


def _validate_private_font_root(root: Path) -> None:
    """Reject redirectable or group-writable fonts before fingerprinting them."""

    try:
        for directory, names, filenames in os.walk(root, followlinks=False):
            directory_path = Path(directory)
            directory_info = directory_path.lstat()
            if directory_path.is_symlink() or not stat.S_ISDIR(directory_info.st_mode):
                raise OfficeRendererDeploymentError("renderer font directory is invalid")
            _reject_group_or_world_writable(directory_info, "renderer font directory")
            for name in names:
                child = directory_path / name
                child_info = child.lstat()
                if child.is_symlink() or not stat.S_ISDIR(child_info.st_mode):
                    raise OfficeRendererDeploymentError("renderer font directory is invalid")
                _reject_group_or_world_writable(child_info, "renderer font directory")
            for name in filenames:
                child = directory_path / name
                if child.suffix.lower() not in _FONT_SUFFIXES:
                    continue
                child_info = child.lstat()
                if child.is_symlink() or not stat.S_ISREG(child_info.st_mode):
                    raise OfficeRendererDeploymentError("renderer font is invalid")
                _reject_group_or_world_writable(child_info, "renderer font")
    except OSError as exc:
        raise OfficeRendererDeploymentError("renderer font identity is unavailable") from exc


def _application_data_root() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if isinstance(frozen_root, str) and frozen_root:
        return Path(frozen_root).resolve(strict=False) / "app" / "data"
    return Path(__file__).resolve().parents[1] / "data"


def _platform_name_from_target(target: str) -> str:
    if target.startswith("windows-"):
        return "win32"
    return target.split("-", 1)[0]


def _relative_component_path(value: object) -> PurePosixPath:
    if not isinstance(value, PurePosixPath):
        raise OfficeRendererDeploymentError("renderer component path is invalid")
    if value.is_absolute() or not value.parts or any(part in {"", ".", ".."} for part in value.parts):
        raise OfficeRendererDeploymentError("renderer component path is invalid")
    return value


def _path_under(root: Path, relative: PurePosixPath) -> Path:
    candidate = root.joinpath(*relative.parts)
    try:
        # ``strict=False`` still prevents lexical ``..`` (already excluded)
        # and makes the intended boundary explicit before any file is opened.
        candidate.relative_to(root)
    except ValueError as exc:
        raise OfficeRendererDeploymentError("renderer component escaped deployment") from exc
    return candidate


def _private_directory(path: Path) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise OfficeRendererDeploymentError("renderer deployment is unavailable") from exc
    if path.is_symlink() or not stat.S_ISDIR(info.st_mode):
        raise OfficeRendererDeploymentError("renderer deployment is invalid")
    _reject_group_or_world_writable(info, "renderer deployment")
    return resolved


def _private_regular_file(root: Path, relative: PurePosixPath, *, max_bytes: int) -> Path:
    candidate = _path_under(root, relative)
    try:
        current = root
        for part in relative.parts[:-1]:
            current = current / part
            parent_info = current.lstat()
            if current.is_symlink() or not stat.S_ISDIR(parent_info.st_mode):
                raise OfficeRendererDeploymentError("renderer component is invalid")
            _reject_group_or_world_writable(parent_info, "renderer component directory")
        info = candidate.lstat()
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise OfficeRendererDeploymentError("renderer component is unavailable") from exc
    if candidate.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
        raise OfficeRendererDeploymentError("renderer component is invalid")
    _reject_group_or_world_writable(info, "renderer component")
    return resolved


def _reject_group_or_world_writable(info: os.stat_result, label: str) -> None:
    if os.name != "nt" and info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise OfficeRendererDeploymentError(f"{label} permissions are unsafe")


def _sha256_private_file(path: Path, *, max_bytes: int) -> str:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise OfficeRendererDeploymentError("renderer component is unavailable") from exc
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise OfficeRendererDeploymentError("renderer component is invalid")
        total = 0
        while chunk := os.read(descriptor, 1024 * 1024):
            total += len(chunk)
            if total > max_bytes:
                raise OfficeRendererDeploymentError("renderer component is invalid")
            digest.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        visible = path.lstat()
    except OSError as exc:
        raise OfficeRendererDeploymentError("renderer component changed") from exc
    if (
        total != before.st_size
        or (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        != (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        or (
            visible.st_dev,
            visible.st_ino,
            visible.st_size,
            visible.st_mtime_ns,
            visible.st_ctime_ns,
        )
        != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
    ):
        raise OfficeRendererDeploymentError("renderer component changed")
    return digest.hexdigest()


def _read_attestation(path: Path) -> AuthoritativeRendererAttestation:
    raw = _read_private_file(path, max_bytes=MAX_ATTESTATION_BYTES)
    try:
        decoded = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise OfficeRendererDeploymentError("renderer attestation is invalid") from exc
    if not isinstance(decoded, dict) or set(decoded) != {
        "schema_version",
        "app_version",
        "release_commit",
        "platform_target",
        "base_renderer_id",
        "base_renderer_version",
        "font_digest",
        "components",
        "signature",
    }:
        raise OfficeRendererDeploymentError("renderer attestation is invalid")
    try:
        return AuthoritativeRendererAttestation(**decoded)
    except (TypeError, RenderContractError) as exc:
        raise OfficeRendererDeploymentError("renderer attestation is invalid") from exc


def _read_private_file(path: Path, *, max_bytes: int) -> bytes:
    root = _private_directory(path.parent)
    try:
        relative = PurePosixPath(path.name)
    except TypeError as exc:
        raise OfficeRendererDeploymentError("renderer attestation is invalid") from exc
    secured = _private_regular_file(root, relative, max_bytes=max_bytes)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(secured, flags)
    except OSError as exc:
        raise OfficeRendererDeploymentError("renderer attestation is unavailable") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise OfficeRendererDeploymentError("renderer attestation is invalid")
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(descriptor, 8192):
            total += len(chunk)
            if total > max_bytes:
                raise OfficeRendererDeploymentError("renderer attestation is invalid")
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if total != before.st_size or after != before:
        raise OfficeRendererDeploymentError("renderer attestation changed")
    return b"".join(chunks)


def _no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


__all__ = [
    "ATTESTATION_FILENAME",
    "AUTHORITATIVE_RENDERER_PUBLIC_KEY",
    "AttestedOfficeRendererDeployment",
    "AuthoritativeRendererProbeBinding",
    "AuthoritativeRendererReleaseIdentity",
    "OfficeRendererDeploymentError",
    "authoritative_office_renderer_self_test",
    "bind_attested_native_sandbox_contract",
    "bind_authoritative_renderer_probe",
    "build_attested_office_render_provider",
    "current_platform_target",
    "default_attested_office_renderer_deployment",
    "fingerprint_office_renderer_bundle",
]
