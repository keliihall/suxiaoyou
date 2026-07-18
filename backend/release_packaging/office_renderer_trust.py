"""Shared, side-effect-free trust root for Office renderer attestations."""

from __future__ import annotations

import base64
import json
from typing import Any, Final, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


AUTHORITATIVE_RENDERER_PUBLIC_KEY: Final = bytes.fromhex(
    "6c0b9c6f91a50f02a8f341501c35029b"
    "1c6d61e3c8a45ec7b4b82fd0e6a2f4a9"
)
SIGNED_ATTESTATION_FIELDS: Final = (
    "app_version",
    "base_renderer_id",
    "base_renderer_version",
    "components",
    "font_digest",
    "platform_target",
    "release_commit",
    "schema_version",
)


class OfficeRendererTrustError(RuntimeError):
    """The renderer attestation does not authenticate under the release key."""


def canonical_office_renderer_attestation_payload(
    value: Mapping[str, Any],
) -> bytes:
    """Return the one byte representation signed by release engineering."""

    if not isinstance(value, Mapping) or set(value) != set(SIGNED_ATTESTATION_FIELDS):
        raise OfficeRendererTrustError("renderer attestation payload fields are invalid")
    try:
        return (
            json.dumps(
                {name: value[name] for name in SIGNED_ATTESTATION_FIELDS},
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
            + b"\n"
        )
    except (KeyError, TypeError, ValueError, UnicodeEncodeError) as exc:
        raise OfficeRendererTrustError(
            "renderer attestation payload encoding is invalid"
        ) from exc


def verify_office_renderer_attestation_signature(
    value: Mapping[str, Any],
    *,
    public_key: bytes | None = None,
) -> None:
    """Verify one complete attestation without importing the runtime app."""

    expected = set(SIGNED_ATTESTATION_FIELDS) | {"signature"}
    if not isinstance(value, Mapping) or set(value) != expected:
        raise OfficeRendererTrustError("renderer attestation fields are invalid")
    selected_key = AUTHORITATIVE_RENDERER_PUBLIC_KEY if public_key is None else public_key
    if not isinstance(selected_key, bytes) or len(selected_key) != 32:
        raise OfficeRendererTrustError("renderer attestation public key is invalid")
    signature = value.get("signature")
    try:
        decoded = base64.b64decode(signature, validate=True)
    except (TypeError, ValueError) as exc:
        raise OfficeRendererTrustError(
            "renderer attestation signature encoding is invalid"
        ) from exc
    if len(decoded) != 64:
        raise OfficeRendererTrustError(
            "renderer attestation signature encoding is invalid"
        )
    payload = canonical_office_renderer_attestation_payload(
        {name: value[name] for name in SIGNED_ATTESTATION_FIELDS}
    )
    try:
        Ed25519PublicKey.from_public_bytes(selected_key).verify(decoded, payload)
    except (InvalidSignature, TypeError, ValueError) as exc:
        raise OfficeRendererTrustError(
            "renderer attestation signature is not trusted"
        ) from exc


__all__ = [
    "AUTHORITATIVE_RENDERER_PUBLIC_KEY",
    "OfficeRendererTrustError",
    "SIGNED_ATTESTATION_FIELDS",
    "canonical_office_renderer_attestation_payload",
    "verify_office_renderer_attestation_signature",
]
