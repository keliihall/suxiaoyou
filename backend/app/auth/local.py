"""Dependencies for operations that require the local desktop credential."""

from __future__ import annotations

from fastapi import HTTPException, Request


LOCALHOST_IPS = frozenset({"127.0.0.1", "::1", "localhost"})


def require_local_session(request: Request) -> None:
    """Require both a loopback peer and the rotating local session bearer.

    Tunnel processes terminate on loopback, so peer IP alone cannot establish
    that the original caller is local.  ``AuthMiddleware`` classifies the
    credential source before routing and stores it on ``request.state``.
    """

    client_ip = request.client.host if request.client else "unknown"
    credential_source = getattr(request.state, "source", None)
    if client_ip not in LOCALHOST_IPS or credential_source != "local":
        raise HTTPException(
            status_code=403,
            detail="This endpoint requires the local desktop session",
        )
