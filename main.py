"""
Grok-ready MCP server: Streamable HTTP (stateless) + Bearer auth on /mcp, health on /health.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from dotenv import load_dotenv

_env_path = Path(__file__).resolve().parent / ".env"
if _env_path.is_file():
    load_dotenv(_env_path, override=False)

from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from auth_middleware import BearerAuthMiddleware, validate_auth_token_at_startup
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp_tools import register_tools
from oauth_routes import (
    log_oauth_boot_status,
    oauth_authorize,
    oauth_metadata,
    oauth_token,
    validate_oauth_at_startup,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


def _json_response_flag() -> bool:
    v = os.getenv("MCP_JSON_RESPONSE", "true").strip().lower()
    return v in ("1", "true", "yes", "on")


def _parse_mcp_extra_host_token(part: str) -> str | None:
    """Hostname (or host:port) for MCP SDK Host allowlist; accepts optional https:// prefix."""
    s = part.strip()
    if not s:
        return None
    low = s.lower()
    for prefix in ("https://", "http://"):
        if low.startswith(prefix):
            s = s[len(prefix) :]
            break
    s = s.split("/", maxsplit=1)[0].strip()
    if not s or any(c in s for c in " \t\r\n"):
        return None
    return s


def _mcp_transport_security_from_env() -> TransportSecuritySettings | None:
    """
    FastMCP enables DNS rebinding protection for localhost by default. Behind Tailscale Funnel,
    clients send Host: <your-machine>.ts.net — add those via MCP_EXTRA_ALLOWED_HOSTS or POST /mcp/ returns 421.
    """
    off = (os.getenv("MCP_DNS_REBINDING_PROTECTION") or "").strip().lower()
    if off in ("0", "false", "no", "off"):
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)

    extra_raw = (os.getenv("MCP_EXTRA_ALLOWED_HOSTS") or "").strip()
    if not extra_raw:
        return None

    allowed_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    allowed_origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    for part in extra_raw.split(","):
        h = _parse_mcp_extra_host_token(part)
        if not h:
            continue
        allowed_hosts.append(h)
        allowed_hosts.append(f"{h}:*")
        allowed_origins.append(f"https://{h}")
        allowed_origins.append(f"https://{h}:*")
        allowed_origins.append(f"http://{h}")
        allowed_origins.append(f"http://{h}:*")

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


def build_mcp() -> FastMCP:
    mcp = FastMCP(
        "grok-browser-mcp-agent",
        instructions=(
            "Remote tools: ping, fetch_url, github_get_file (ref=branch/tag/SHA + content_text), github_list_repo_files, "
            "github_get_diff, github_create_issue, request_user_secret (127.0.0.1 form on PC), list_secrets, revoke_secret, "
            "browser_open_tab, browser_navigate, browser_get_page_state, browser_click, browser_type, browser_press_keys (fast CDP, no agent), "
            "browser_task (shared Chrome; continue_tab_id), browser_capture_tab_screenshot, list_browser_tabs, close_browser_tab, "
            "cursor_agent (levels 1/2/3; approve_cursor_writes with optional always_allow_level_3_rule), revoke_cursor_writes, "
            "get_status, get_run_log, list_recent_runs. "
            "Streamable HTTP: FastMCP wraps official mcp MCPServer + StreamableHTTPSessionManager (same transport as streamable_http_app). "
            "browser_task/cursor_agent return run_id."
        ),
        stateless_http=True,
        json_response=_json_response_flag(),
        streamable_http_path="/",
        transport_security=_mcp_transport_security_from_env(),
    )
    register_tools(mcp)
    return mcp


mcp_server = build_mcp()
_mcp_asgi = mcp_server.streamable_http_app()
_mcp_wrapped = BearerAuthMiddleware(_mcp_asgi)


async def health(_):
    return JSONResponse(
        {
            "status": "healthy",
            "mcp_json_response": _json_response_flag(),
            "mcp_path": "/mcp/",
            "live_probe": "/health/live",
        }
    )


async def health_live(request):
    """
    Liveness: fails if the asyncio loop has not run the heartbeat recently (possible deadlock / blocking call).
    """
    max_age = float(os.getenv("HEALTH_LIVE_MAX_STALE_SECONDS", "15"))
    last = getattr(request.app.state, "last_loop_tick", None)
    if last is None:
        return JSONResponse(
            {"status": "unknown", "detail": "heartbeat not started"},
            status_code=503,
        )
    age = time.monotonic() - last
    if age > max_age:
        return JSONResponse(
            {
                "status": "degraded",
                "loop_stale_seconds": round(age, 1),
                "detail": "event loop has not ticked recently; server may be wedged",
            },
            status_code=503,
        )
    return JSONResponse({"status": "live", "loop_stale_seconds": round(age, 1)})


async def root(_):
    return JSONResponse(
        {
            "service": "grok-browser-mcp-agent",
            "health": "/health",
            "health_live": "/health/live",
            "mcp": "/mcp/",
            "oauth_metadata": "/.well-known/oauth-authorization-server",
            "browser_screenshot": "/browser-screenshot/{token} (GET; one-time PNG when PUBLIC_MCP_BASE_URL is set; optional BROWSER_SCREENSHOT_REQUIRE_BEARER)",
        }
    )


async def browser_screenshot(request):
    """
    Serve a one-time browser_task PNG. By default only the opaque path token (+ TTL) is required.
    Set BROWSER_SCREENSHOT_REQUIRE_BEARER=true to also require Authorization: Bearer (same rules as /mcp/).
    """
    import screenshot_serve as ss

    if (os.getenv("BROWSER_SCREENSHOT_REQUIRE_BEARER") or "").strip().lower() in ("1", "true", "yes", "on"):
        from auth_middleware import verify_mcp_bearer_from_request

        bad = verify_mcp_bearer_from_request(request)
        if bad is not None:
            return bad

    token = (request.path_params.get("token") or "").strip()
    ss.purge_expired()
    path = ss.take_path_for_token(token)
    if path is None:
        return JSONResponse({"error": "not_found_or_expired"}, status_code=404)
    try:
        data = path.read_bytes()
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    return Response(
        content=data,
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@asynccontextmanager
async def lifespan(app: Starlette):
    app.state.last_loop_tick = time.monotonic()

    async def _heartbeat() -> None:
        try:
            while True:
                app.state.last_loop_tick = time.monotonic()
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            return

    hb = asyncio.create_task(_heartbeat())
    try:
        async with _mcp_asgi.router.lifespan_context(_mcp_asgi):
            yield
    finally:
        hb.cancel()
        with suppress(asyncio.CancelledError):
            await hb


routes = [
    Route("/", root, methods=["GET"]),
    Route("/browser-screenshot/{token}", browser_screenshot, methods=["GET"]),
    Route("/health", health, methods=["GET"]),
    Route("/health/live", health_live, methods=["GET"]),
    Route("/.well-known/oauth-authorization-server", oauth_metadata, methods=["GET"]),
    Route("/.well-known/oauth-authorization-server/", oauth_metadata, methods=["GET"]),
    Route("/oauth/authorize", oauth_authorize, methods=["GET"]),
    Route("/oauth/authorize/", oauth_authorize, methods=["GET"]),
    Route("/oauth/token", oauth_token, methods=["POST"]),
    Route("/oauth/token/", oauth_token, methods=["POST"]),
    Mount("/mcp", app=_mcp_wrapped),
]

app = Starlette(routes=routes, lifespan=lifespan)

validate_oauth_at_startup()
validate_auth_token_at_startup()
log_oauth_boot_status()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8765")),
        factory=False,
    )
