"""Signed release attestation wrapper for authoritative Office rendering.

The underlying local renderer may advertise only approximate quality.  This
wrapper elevates a concrete, release-reviewed build to ``authoritative`` only
when an Ed25519-signed manifest exactly binds its application version, release
commit, descriptor, platform, font pack, renderer binaries, and license
inventory.  It proves reproducibility for the approved local pipeline; it does
not claim pixel equivalence with every third-party Office suite.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import re
from types import MappingProxyType
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.office_rendering.errors import (
    ProviderUnavailableError,
    RenderContractError,
)
from app.office_rendering.models import (
    AUTHORITATIVE_QUALITY,
    RenderManifest,
    RendererDescriptor,
    RenderRequest,
    validate_sha256,
)
from app.office_rendering.provider import OfficeRenderProvider, ProviderAvailability
from release_packaging.office_renderer_trust import (
    canonical_office_renderer_attestation_payload,
)


ATTESTATION_SCHEMA_VERSION = 2
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,159}$")
_COMPONENT = re.compile(r"^[a-z][a-z0-9._-]{0,79}$")
_APP_VERSION = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$"
)
_RELEASE_COMMIT = re.compile(r"^(?!0{40}$)[0-9a-f]{40}$")


def _label(value: object, field: str) -> str:
    if not isinstance(value, str) or _LABEL.fullmatch(value) is None:
        raise RenderContractError(f"attested renderer {field} is invalid")
    return value


def _app_version(value: object) -> str:
    if not isinstance(value, str) or _APP_VERSION.fullmatch(value) is None:
        raise RenderContractError("attested renderer app_version is invalid")
    return value


def _release_commit(value: object) -> str:
    if not isinstance(value, str) or _RELEASE_COMMIT.fullmatch(value) is None:
        raise RenderContractError("attested renderer release_commit is invalid")
    return value


@dataclass(frozen=True, slots=True)
class AuthoritativeRendererReleaseIdentity:
    """Immutable identity supplied by a trusted frozen-release boundary.

    The deployment loader deliberately does not discover either value from
    process environment variables.  Release packaging must supply an identity
    that was bound to the frozen application; absence is a closed state.
    """

    app_version: str
    release_commit: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "app_version", _app_version(self.app_version))
        object.__setattr__(
            self,
            "release_commit",
            _release_commit(self.release_commit),
        )


def _components(value: Mapping[str, str]) -> MappingProxyType[str, str]:
    try:
        copied = dict(value)
    except (TypeError, ValueError) as exc:
        raise RenderContractError("attested renderer components are invalid") from exc
    if (
        not 1 <= len(copied) <= 64
        or tuple(copied) != tuple(sorted(copied))
        or any(
            not isinstance(name, str)
            or _COMPONENT.fullmatch(name) is None
            or not isinstance(digest, str)
            for name, digest in copied.items()
        )
    ):
        raise RenderContractError(
            "attested renderer components must be a sorted bounded mapping"
        )
    validated = {
        name: validate_sha256(digest, f"component {name}")
        for name, digest in copied.items()
    }
    return MappingProxyType(validated)


def attestation_payload_bytes(
    *,
    app_version: str,
    release_commit: str,
    platform_target: str,
    base_renderer_id: str,
    base_renderer_version: str,
    font_digest: str,
    components: Mapping[str, str],
    schema_version: int = ATTESTATION_SCHEMA_VERSION,
) -> bytes:
    """Build the exact canonical bytes signed by the release key."""

    if schema_version != ATTESTATION_SCHEMA_VERSION:
        raise RenderContractError("unsupported renderer attestation schema")
    payload = {
        "schema_version": schema_version,
        "app_version": _app_version(app_version),
        "release_commit": _release_commit(release_commit),
        "platform_target": _label(platform_target, "platform_target"),
        "base_renderer_id": _label(base_renderer_id, "base_renderer_id"),
        "base_renderer_version": _label(
            base_renderer_version,
            "base_renderer_version",
        ),
        "font_digest": validate_sha256(font_digest, "font_digest"),
        "components": dict(_components(components)),
    }
    return canonical_office_renderer_attestation_payload(payload)


@dataclass(frozen=True, slots=True)
class AuthoritativeRendererAttestation:
    app_version: str
    release_commit: str
    platform_target: str
    base_renderer_id: str
    base_renderer_version: str
    font_digest: str
    components: Mapping[str, str]
    signature: str
    schema_version: int = ATTESTATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        payload = attestation_payload_bytes(
            app_version=self.app_version,
            release_commit=self.release_commit,
            platform_target=self.platform_target,
            base_renderer_id=self.base_renderer_id,
            base_renderer_version=self.base_renderer_version,
            font_digest=self.font_digest,
            components=self.components,
            schema_version=self.schema_version,
        )
        del payload
        object.__setattr__(self, "components", _components(self.components))
        if not isinstance(self.signature, str) or len(self.signature) > 256:
            raise RenderContractError("renderer attestation signature is invalid")
        try:
            decoded = base64.b64decode(self.signature, validate=True)
        except (ValueError, TypeError) as exc:
            raise RenderContractError("renderer attestation signature is invalid") from exc
        if len(decoded) != 64:
            raise RenderContractError("renderer attestation signature is invalid")

    @property
    def payload_bytes(self) -> bytes:
        return attestation_payload_bytes(
            app_version=self.app_version,
            release_commit=self.release_commit,
            platform_target=self.platform_target,
            base_renderer_id=self.base_renderer_id,
            base_renderer_version=self.base_renderer_version,
            font_digest=self.font_digest,
            components=self.components,
            schema_version=self.schema_version,
        )

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.payload_bytes).hexdigest()

    @property
    def release_identity(self) -> AuthoritativeRendererReleaseIdentity:
        return AuthoritativeRendererReleaseIdentity(
            app_version=self.app_version,
            release_commit=self.release_commit,
        )

    def verify(self, public_key: bytes) -> None:
        if not isinstance(public_key, bytes) or len(public_key) != 32:
            raise RenderContractError("renderer attestation public key is invalid")
        try:
            key = Ed25519PublicKey.from_public_bytes(public_key)
            key.verify(base64.b64decode(self.signature, validate=True), self.payload_bytes)
        except (InvalidSignature, ValueError, TypeError) as exc:
            raise RenderContractError("renderer attestation signature is not trusted") from exc


class AttestedOfficeRenderProvider:
    """Expose authoritative quality only for an exactly attested deployment."""

    def __init__(
        self,
        delegate: OfficeRenderProvider,
        *,
        attestation: AuthoritativeRendererAttestation,
        trusted_public_key: bytes,
        release_identity: AuthoritativeRendererReleaseIdentity,
        platform_target: str,
        installed_components: Mapping[str, str],
    ) -> None:
        if not isinstance(delegate, OfficeRenderProvider):
            raise RenderContractError("attested renderer delegate is invalid")
        if not isinstance(attestation, AuthoritativeRendererAttestation):
            raise RenderContractError("renderer attestation is invalid")
        if not isinstance(
            release_identity,
            AuthoritativeRendererReleaseIdentity,
        ):
            raise RenderContractError("renderer release identity is invalid")
        self.delegate = delegate
        self.attestation = attestation
        self._trusted_public_key = bytes(trusted_public_key)
        self._release_identity = release_identity
        self._platform_target = _label(platform_target, "platform_target")
        self._installed_components = _components(installed_components)
        self._validate_binding()
        self._descriptor = RendererDescriptor(
            renderer_id="suxiaoyou-attested-office",
            renderer_version=f"attestation-{attestation.digest}",
            font_digest=attestation.font_digest,
            quality=AUTHORITATIVE_QUALITY,
        )

    @property
    def descriptor(self) -> RendererDescriptor:
        return self._descriptor

    def _validate_binding(self) -> None:
        self.attestation.verify(self._trusted_public_key)
        descriptor = self.delegate.descriptor
        if not isinstance(descriptor, RendererDescriptor):
            raise RenderContractError("attested renderer delegate descriptor is invalid")
        if (
            self.attestation.release_identity != self._release_identity
            or self.attestation.platform_target != self._platform_target
            or self.attestation.base_renderer_id != descriptor.renderer_id
            or self.attestation.base_renderer_version != descriptor.renderer_version
            or self.attestation.font_digest != descriptor.font_digest
            or dict(self.attestation.components) != dict(self._installed_components)
        ):
            raise RenderContractError(
                "renderer attestation does not match the installed deployment"
            )

    def availability(self) -> ProviderAvailability:
        try:
            self._validate_binding()
            availability = self.delegate.availability()
        except Exception:
            return ProviderAvailability(
                available=False,
                reason="Authoritative renderer attestation no longer matches",
            )
        if not isinstance(availability, ProviderAvailability):
            return ProviderAvailability(
                available=False,
                reason="Attested renderer delegate availability is invalid",
            )
        return availability

    async def render(
        self,
        request: RenderRequest,
        output_dir: Any,
    ) -> RenderManifest:
        availability = self.availability()
        if not availability.available:
            raise ProviderUnavailableError(
                availability.reason or "Attested renderer is unavailable"
            )
        delegate_descriptor = self.delegate.descriptor
        manifest = await self.delegate.render(request, output_dir)
        self._validate_binding()
        if not isinstance(manifest, RenderManifest):
            raise RenderContractError("attested renderer delegate returned no manifest")
        actual = (
            manifest.renderer_id,
            manifest.renderer_version,
            manifest.font_digest,
            manifest.quality,
        )
        expected = (
            delegate_descriptor.renderer_id,
            delegate_descriptor.renderer_version,
            delegate_descriptor.font_digest,
            delegate_descriptor.quality,
        )
        if actual != expected:
            raise RenderContractError(
                "attested renderer delegate changed identity during rendering"
            )
        return RenderManifest.for_request(
            request,
            self.descriptor,
            manifest.pages,
            pdf=manifest.pdf,
        )


__all__ = [
    "ATTESTATION_SCHEMA_VERSION",
    "AttestedOfficeRenderProvider",
    "AuthoritativeRendererAttestation",
    "AuthoritativeRendererReleaseIdentity",
    "attestation_payload_bytes",
]
