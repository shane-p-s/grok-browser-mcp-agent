"""Bearer token gate for the MCP Streamable HTTP mount."""

from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse


def is_production() -> bool:
    return bool(os.getenv("K_SERVICE")) or os.getenv("ENVIRONMENT", "").lower() in (
        "production",
        "prod",
    )


def expected_bearer_token() -> str | None:
    token = (os.getenv("AUTH_TOKEN") or "").strip()
    return token or None


def validate_auth_token_at_startup() -> None:
    """Fail fast on Cloud Run if AUTH_TOKEN is missing."""
    if is_production() and not expected_bearer_token():
        raise RuntimeError(
            "AUTH_TOKEN must be set when running on Cloud Run (K_SERVICE) or ENVIRONMENT=production."
        )


def _parse_bearer(authorization_header: str | None) -> tuple[str | None, str | None]:
    """
    Return (error_detail, token) where token is the bearer secret without 'Bearer ' prefix.
    error_detail is set when the header is missing, malformed, or wrong scheme.
    """
    raw = (authorization_header or "").strip()
    if not raw:
        return ("Missing Authorization header; required: Authorization: Bearer <token>", None)
    parts = raw.split(None, 1)
    if len(parts) < 2:
        if parts[0].lower() == "bearer":
            return ("Invalid Authorization header: Bearer token is empty after 'Bearer '", None)
        return ("Invalid Authorization header: expected 'Bearer <token>' with a space after Bearer", None)
    scheme, token = parts[0], parts[1].strip()
    if scheme.lower() != "bearer":
        return (f"Invalid Authorization scheme '{scheme}'; only Bearer is supported", None)
    if not token:
        return ("Invalid Authorization header: Bearer token is empty", None)
    return (None, token)


class BearerAuthMiddleware:
    """ASGI wrapper: require Authorization: Bearer <AUTH_TOKEN> for all requests."""

    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        token: str | None,
    ) -> None:
        self.app = app
        self._token = token

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if self._token is None:
            resp = JSONResponse(
                {"detail": "Server misconfiguration: AUTH_TOKEN is not set."},
                status_code=503,
            )
            await resp(scope, receive, send)
            return

        request = Request(scope)
        err, presented = _parse_bearer(request.headers.get("authorization"))
        if err is not None or presented is None:
            resp = JSONResponse({"detail": err or "Unauthorized"}, status_code=401)
            await resp(scope, receive, send)
            return

        p_hash = hashlib.sha256(presented.encode("utf-8")).hexdigest()
        e_hash = hashlib.sha256(self._token.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(p_hash, e_hash):
            resp = JSONResponse({"detail": "Invalid bearer token"}, status_code=401)
            await resp(scope, receive, send)
            return

        await self.app(scope, receive, send)
