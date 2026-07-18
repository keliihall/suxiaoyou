from __future__ import annotations

import base64
from dataclasses import replace
import hashlib
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from app.office_rendering import (
    AUTHORITATIVE_QUALITY,
    AttestedOfficeRenderProvider,
    AuthoritativeRendererAttestation,
    AuthoritativeRendererReleaseIdentity,
    OfficeRenderCache,
    RenderContractError,
    RendererDescriptor,
    attestation_payload_bytes,
)
from tests.test_office_rendering.helpers import FakeProvider, make_request


APP_VERSION = "1.1.0"
RELEASE_COMMIT = "a" * 40
RELEASE_IDENTITY = AuthoritativeRendererReleaseIdentity(
    app_version=APP_VERSION,
    release_commit=RELEASE_COMMIT,
)


def _signed_attestation(
    descriptor: RendererDescriptor,
    components: dict[str, str],
    *,
    app_version: str = APP_VERSION,
    release_commit: str = RELEASE_COMMIT,
) -> tuple[AuthoritativeRendererAttestation, bytes]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    payload = attestation_payload_bytes(
        app_version=app_version,
        release_commit=release_commit,
        platform_target="test-x64",
        base_renderer_id=descriptor.renderer_id,
        base_renderer_version=descriptor.renderer_version,
        font_digest=descriptor.font_digest,
        components=components,
    )
    signature = base64.b64encode(private.sign(payload)).decode("ascii")
    return (
        AuthoritativeRendererAttestation(
            app_version=app_version,
            release_commit=release_commit,
            platform_target="test-x64",
            base_renderer_id=descriptor.renderer_id,
            base_renderer_version=descriptor.renderer_version,
            font_digest=descriptor.font_digest,
            components=components,
            signature=signature,
        ),
        public,
    )


@pytest.mark.asyncio
async def test_signed_exact_deployment_is_the_only_authoritative_path(
    tmp_path: Path,
) -> None:
    descriptor = RendererDescriptor(
        renderer_id="local-renderer",
        renderer_version="build-1",
        font_digest=hashlib.sha256(b"fonts").hexdigest(),
        quality="approximate",
    )
    components = {
        "font-manifest": hashlib.sha256(b"font manifest").hexdigest(),
        "license-manifest": hashlib.sha256(b"licenses").hexdigest(),
        "pdftoppm": hashlib.sha256(b"pdftoppm").hexdigest(),
        "soffice": hashlib.sha256(b"soffice").hexdigest(),
    }
    attestation, public = _signed_attestation(descriptor, components)
    delegate = FakeProvider(descriptor)
    provider = AttestedOfficeRenderProvider(
        delegate,
        attestation=attestation,
        trusted_public_key=public,
        release_identity=RELEASE_IDENTITY,
        platform_target="test-x64",
        installed_components=components,
    )
    workspace = tmp_path / "workspace"
    request = make_request(workspace)
    cache = OfficeRenderCache((tmp_path / "cache").absolute())

    manifest = await cache.get_or_render(request, provider)

    assert provider.availability().available
    assert provider.descriptor.quality == AUTHORITATIVE_QUALITY
    assert manifest.quality == AUTHORITATIVE_QUALITY
    assert manifest.renderer_id == "suxiaoyou-attested-office"
    assert manifest.renderer_version.endswith(attestation.digest)


def test_attestation_rejects_wrong_component_or_signature() -> None:
    descriptor = RendererDescriptor(
        renderer_id="local-renderer",
        renderer_version="build-1",
        font_digest="f" * 64,
        quality="approximate",
    )
    components = {"soffice": "a" * 64}
    attestation, public = _signed_attestation(descriptor, components)
    delegate = FakeProvider(descriptor)

    with pytest.raises(RenderContractError, match="installed deployment"):
        AttestedOfficeRenderProvider(
            delegate,
            attestation=attestation,
            trusted_public_key=public,
            release_identity=RELEASE_IDENTITY,
            platform_target="test-x64",
            installed_components={"soffice": "b" * 64},
        )

    wrong_public = Ed25519PrivateKey.generate().public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    with pytest.raises(RenderContractError, match="not trusted"):
        AttestedOfficeRenderProvider(
            delegate,
            attestation=attestation,
            trusted_public_key=wrong_public,
            release_identity=RELEASE_IDENTITY,
            platform_target="test-x64",
            installed_components=components,
        )


def test_runtime_descriptor_change_closes_availability() -> None:
    descriptor = RendererDescriptor(
        renderer_id="local-renderer",
        renderer_version="build-1",
        font_digest="f" * 64,
        quality="approximate",
    )
    components = {"soffice": "a" * 64}
    attestation, public = _signed_attestation(descriptor, components)
    delegate = FakeProvider(descriptor)
    provider = AttestedOfficeRenderProvider(
        delegate,
        attestation=attestation,
        trusted_public_key=public,
        release_identity=RELEASE_IDENTITY,
        platform_target="test-x64",
        installed_components=components,
    )

    delegate._descriptor = RendererDescriptor(
        renderer_id="local-renderer",
        renderer_version="build-2",
        font_digest="f" * 64,
        quality="approximate",
    )

    availability = provider.availability()
    assert not availability.available
    assert availability.reason == "Authoritative renderer attestation no longer matches"


def test_release_identity_is_signed_and_cross_release_replay_is_rejected() -> None:
    descriptor = RendererDescriptor(
        renderer_id="local-renderer",
        renderer_version="build-1",
        font_digest="f" * 64,
        quality="approximate",
    )
    components = {"soffice": "a" * 64}
    attestation, public = _signed_attestation(descriptor, components)
    delegate = FakeProvider(descriptor)

    for replay_identity in (
        AuthoritativeRendererReleaseIdentity(
            app_version="1.2.0",
            release_commit=RELEASE_COMMIT,
        ),
        AuthoritativeRendererReleaseIdentity(
            app_version=APP_VERSION,
            release_commit="b" * 40,
        ),
    ):
        with pytest.raises(RenderContractError, match="installed deployment"):
            AttestedOfficeRenderProvider(
                delegate,
                attestation=attestation,
                trusted_public_key=public,
                release_identity=replay_identity,
                platform_target="test-x64",
                installed_components=components,
            )

    for tampered in (
        replace(attestation, app_version="1.2.0"),
        replace(attestation, release_commit="c" * 40),
    ):
        with pytest.raises(RenderContractError, match="not trusted"):
            tampered.verify(public)


@pytest.mark.parametrize(
    ("app_version", "release_commit"),
    [
        ("v1.1.0", RELEASE_COMMIT),
        ("1.1", RELEASE_COMMIT),
        ("01.1.0", RELEASE_COMMIT),
        (APP_VERSION, "A" * 40),
        (APP_VERSION, "0" * 40),
        (APP_VERSION, "a" * 39),
    ],
)
def test_release_identity_requires_canonical_version_and_full_commit(
    app_version: str,
    release_commit: str,
) -> None:
    with pytest.raises(RenderContractError):
        AuthoritativeRendererReleaseIdentity(
            app_version=app_version,
            release_commit=release_commit,
        )


def test_schema_v1_payload_is_not_a_publishable_compatibility_path() -> None:
    with pytest.raises(RenderContractError, match="unsupported"):
        attestation_payload_bytes(
            app_version=APP_VERSION,
            release_commit=RELEASE_COMMIT,
            platform_target="test-x64",
            base_renderer_id="local-renderer",
            base_renderer_version="build-1",
            font_digest="f" * 64,
            components={"soffice": "a" * 64},
            schema_version=1,
        )
