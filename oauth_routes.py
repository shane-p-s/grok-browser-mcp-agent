"""Minimal OAuth 2.0 endpoints for Grok Custom Connector (PKCE + optional client secret).

Not a full IdP: single registered client, JWT access tokens for /mcp Bearer auth.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import jwt
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

logger = logging.getLogger(__name__)

_code_lock = threading.Lock()
# auth_code -> record
_pending_codes: dict[str, dict[str, Any]] = {}

_CODE_TTL_SEC = 600


def _wants_html(request: Request) -> bool:
    accept = (request.headers.get("accept") or "").lower()
    return "text/html" in accept


def _oauth_disabled_response(request: Request, *, for_browser: bool) -> JSONResponse | HTMLResponse:
    """503 so clients do not confuse OAuth-off with 'route missing' (404)."""
    msg = (
        "OAuth is not enabled on this server. Set OAUTH_ENABLED=true, OAUTH_CLIENT_ID, and OAUTH_JWT_SECRET "
        "in .env, then restart uvicorn/start.ps1."
    )
    if for_browser and _wants_html(request):
        return HTMLResponse(
            f"<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>OAuth disabled</title></head><body>"
            f"<h1>OAuth not enabled</h1><p>{msg}</p></body></html>",
            status_code=503,
        )
    return JSONResponse({"detail": msg}, status_code=503)


def _oauth_enabled() -> bool:
    return os.getenv("OAUTH_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def _client_id() -> str:
    return (os.getenv("OAUTH_CLIENT_ID") or "").strip()


def _client_secret() -> str:
    return (os.getenv("OAUTH_CLIENT_SECRET") or "").strip()


def _jwt_secret() -> str:
    return (os.getenv("OAUTH_JWT_SECRET") or "").strip()


def _jwt_ttl_seconds() -> int:
    raw = (os.getenv("OAUTH_ACCESS_TOKEN_TTL_SECONDS") or "").strip()
    if raw.isdigit():
        return max(300, int(raw))
    return 90 * 24 * 3600  # 90 days


def _redirect_host_allowed(netloc: str) -> bool:
    """Allow https redirect_uri hosts matching suffix list (default: x.ai)."""
    suffixes_raw = os.getenv("OAUTH_REDIRECT_URI_HOST_SUFFIX", ".x.ai").strip()
    if suffixes_raw == "*":
        return True
    if not suffixes_raw:
        return False
    host = (netloc or "").split("@")[-1].lower().split(":")[0]
    for part in suffixes_raw.split(","):
        s = part.strip().lower()
        if not s:
            continue
        if s.startswith("."):
            if host.endswith(s) or host == s[1:]:
                return True
        elif host == s or host.endswith("." + s):
            return True
    return False


def _purge_expired() -> None:
    now = time.time()
    dead = [k for k, v in _pending_codes.items() if v.get("exp", 0) < now]
    for k in dead:
        _pending_codes.pop(k, None)


def _pkce_s256_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _issue_access_token() -> str:
    now = int(time.time())
    ttl = _jwt_ttl_seconds()
    payload = {
        "sub": "mcp",
        "iat": now,
        "exp": now + ttl,
        "typ": "oauth_access",
    }
    return jwt.encode(payload, _jwt_secret(), algorithm="HS256")


async def oauth_metadata(request: Request):
    """RFC 8414-style metadata so clients can discover endpoints."""
    if not _oauth_enabled():
        return JSONResponse(
            {"detail": "OAuth is not enabled (OAUTH_ENABLED=false). Set OAUTH_ENABLED=true in .env and restart."},
            status_code=503,
        )
    base = f"{request.url.scheme}://{request.url.netloc}"
    body = {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "grant_types_supported": ["authorization_code", "client_credentials"],
        "code_challenge_methods_supported": ["S256"],
        "response_types_supported": ["code"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "none"],
    }
    return JSONResponse(body)


async def oauth_authorize(request: Request):
    if not _oauth_enabled():
        return _oauth_disabled_response(request, for_browser=True)

    q = request.query_params
    response_type = (q.get("response_type") or "").strip()
    client_id = (q.get("client_id") or "").strip()
    redirect_uri = (q.get("redirect_uri") or "").strip()
    state = q.get("state")
    code_challenge = (q.get("code_challenge") or "").strip()
    code_challenge_method = (q.get("code_challenge_method") or "").strip().upper()

    if response_type != "code":
        return JSONResponse({"error": "unsupported_grant_type", "error_description": "response_type must be code"}, status_code=400)
    if client_id != _client_id():
        logger.warning(
            "oauth authorize: invalid client_id (check OAUTH_CLIENT_ID matches Grok Client ID field)"
        )
        return JSONResponse({"error": "invalid_client", "error_description": "Unknown client_id"}, status_code=400)
    if not redirect_uri:
        return JSONResponse({"error": "invalid_request", "error_description": "redirect_uri required"}, status_code=400)

    ru = urlparse(redirect_uri)
    if ru.scheme != "https":
        return JSONResponse({"error": "invalid_request", "error_description": "redirect_uri must use https"}, status_code=400)
    if not _redirect_host_allowed(ru.netloc):
        return JSONResponse(
            {"error": "invalid_request", "error_description": "redirect_uri host not allowed (see OAUTH_REDIRECT_URI_HOST_SUFFIX)"},
            status_code=400,
        )

    if not code_challenge or code_challenge_method != "S256":
        return JSONResponse(
            {"error": "invalid_request", "error_description": "PKCE required: code_challenge + code_challenge_method=S256"},
            status_code=400,
        )

    auth_code = secrets.token_urlsafe(32)
    with _code_lock:
        _purge_expired()
        _pending_codes[auth_code] = {
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "exp": time.time() + _CODE_TTL_SEC,
        }

    parsed = urlparse(redirect_uri)
    q_existing = parse_qs(parsed.query)
    q_new = {k: v[0] for k, v in q_existing.items()}
    q_new["code"] = auth_code
    if state is not None:
        q_new["state"] = state
    new_query = urlencode(q_new)
    loc = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))
    logger.info("oauth authorize: issuing code for client_id=%s redirect_host=%s", client_id, ru.netloc)
    return RedirectResponse(url=loc, status_code=302)


def _form_error(status: int, error: str, desc: str) -> JSONResponse:
    return JSONResponse({"error": error, "error_description": desc}, status_code=status)


async def oauth_token(request: Request):
    if not _oauth_enabled():
        return _oauth_disabled_response(request, for_browser=False)

    ctype = (request.headers.get("content-type") or "").lower()
    if "application/json" in ctype:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        grant_type = str(body.get("grant_type") or "").strip()
        code = str(body.get("code") or "").strip()
        redirect_uri = str(body.get("redirect_uri") or "").strip()
        client_id = str(body.get("client_id") or "").strip()
        client_secret = str(body.get("client_secret") or "").strip()
        code_verifier = str(body.get("code_verifier") or "").strip()
    else:
        form = await request.form()
        grant_type = str(form.get("grant_type") or "").strip()
        code = str(form.get("code") or "").strip()
        redirect_uri = str(form.get("redirect_uri") or "").strip()
        client_id = str(form.get("client_id") or "").strip()
        client_secret = str(form.get("client_secret") or "").strip()
        code_verifier = str(form.get("code_verifier") or "").strip()

    if grant_type == "client_credentials":
        if client_id != _client_id():
            return _form_error(400, "invalid_client", "Unknown client_id")
        sec = _client_secret()
        if not sec:
            return _form_error(400, "unauthorized_client", "client_credentials requires OAUTH_CLIENT_SECRET on server")
        if not secrets.compare_digest(client_secret, sec):
            return _form_error(401, "invalid_client", "Invalid client_secret")
        token = _issue_access_token()
        return JSONResponse({"access_token": token, "token_type": "Bearer", "expires_in": _jwt_ttl_seconds()})

    if grant_type != "authorization_code":
        return _form_error(400, "unsupported_grant_type", "Only authorization_code and client_credentials are supported")

    if client_id != _client_id():
        return _form_error(400, "invalid_client", "Unknown client_id")

    sec = _client_secret()
    if sec and not secrets.compare_digest(client_secret, sec):
        return _form_error(401, "invalid_client", "Invalid client_secret")

    with _code_lock:
        _purge_expired()
        rec = _pending_codes.pop(code, None)

    if not rec:
        return _form_error(400, "invalid_grant", "Unknown or expired authorization code")

    if rec.get("redirect_uri") != redirect_uri:
        return _form_error(400, "invalid_grant", "redirect_uri mismatch")

    if not code_verifier:
        return _form_error(400, "invalid_grant", "code_verifier required for PKCE")

    expected_challenge = _pkce_s256_challenge(code_verifier)
    stored = str(rec.get("code_challenge", "")).rstrip("=")
    if not secrets.compare_digest(expected_challenge.rstrip("="), stored):
        return _form_error(400, "invalid_grant", "PKCE verification failed")

    token = _issue_access_token()
    logger.info("oauth token: issued access token (authorization_code)")
    return JSONResponse({"access_token": token, "token_type": "Bearer", "expires_in": _jwt_ttl_seconds()})


def oauth_auth_configured() -> bool:
    return _oauth_enabled() and bool(_client_id()) and bool(_jwt_secret())


def mcp_auth_configured() -> bool:
    """At least one of static AUTH_TOKEN or full OAuth (for /mcp) is configured."""
    if (os.getenv("AUTH_TOKEN") or "").strip():
        return True
    return oauth_auth_configured()


def verify_mcp_access_token(presented: str) -> bool:
    """True if Bearer secret matches AUTH_TOKEN or JWT from /oauth/token."""
    static = (os.getenv("AUTH_TOKEN") or "").strip()
    if static:
        p_hash = hashlib.sha256(presented.encode("utf-8")).hexdigest()
        e_hash = hashlib.sha256(static.encode("utf-8")).hexdigest()
        if hmac.compare_digest(p_hash, e_hash):
            return True
    if _oauth_enabled() and _jwt_secret():
        try:
            claims = jwt.decode(
                presented,
                _jwt_secret(),
                algorithms=["HS256"],
                options={"require": ["exp", "iat"]},
            )
        except jwt.InvalidTokenError:
            claims = None
        if isinstance(claims, dict) and claims.get("typ") == "oauth_access":
            return True
    return False


def validate_oauth_at_startup() -> None:
    if not _oauth_enabled():
        return
    if not _client_id():
        raise RuntimeError("OAUTH_ENABLED requires OAUTH_CLIENT_ID")
    if not _jwt_secret():
        raise RuntimeError("OAUTH_ENABLED requires OAUTH_JWT_SECRET (long random secret for signing access tokens)")


def log_oauth_boot_status() -> None:
    if _oauth_enabled():
        logger.info(
            "OAuth enabled: GET /.well-known/oauth-authorization-server, GET /oauth/authorize, POST /oauth/token "
            "(restart required after .env changes)"
        )
