"""MCP tool implementations: ping, fetch_url, GitHub helpers, browser_task (Browser Use + DeepSeek)."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from mcp.server.fastmcp import FastMCP

import browser_granular
import browser_hub
import browser_watch
import browser_prefill
import memory_store
import secrets_store
import secrets_submit
import tool_gating
from cursor_agent_tools import register_cursor_tools
from omi_tools import index_ready, omi_api_key_configured, register_omi_tools
from oauth_routes import mcp_auth_configured, oauth_auth_configured
from github_tools import (
    github_get_diff as github_compare_api,
    github_get_file_enriched,
    github_list_repo_files as github_list_repo_files_api,
    github_request,
    safe_github_segment as _safe_github_segment,
)
from run_log import (
    append_event,
    finish_run,
    get_run,
    list_recent_runs as list_recent_runs_store,
    start_run,
    summarize_browser_history,
)

logger = logging.getLogger(__name__)

_MAX_FETCH_BYTES = int(os.getenv("FETCH_MAX_BYTES", "2_000_000"))
_DEFAULT_BROWSER_TIMEOUT = int(os.getenv("BROWSER_TASK_TIMEOUT_SECONDS", "300"))
_DEFAULT_BROWSER_MAX_STEPS = int(os.getenv("BROWSER_TASK_MAX_STEPS", "40"))


def _final_result_mcp_payload(final: Any) -> dict[str, Any]:
    """
    Keep browser_task tool JSON small enough for remote MCP clients (e.g. rmcp / Grok)
    that fail on very large tools/call responses.
    """
    max_chars = int(os.getenv("BROWSER_TASK_MAX_FINAL_RESULT_CHARS", "16000"))
    if max_chars <= 0:
        return {"final_result": final}
    text: str
    try:
        if isinstance(final, str):
            text = final
        elif isinstance(final, (dict, list)):
            text = json.dumps(final, ensure_ascii=False, default=str)
        else:
            text = str(final)
    except Exception:
        text = str(final)
    if len(text) <= max_chars:
        return {"final_result": final}
    return {
        "final_result": text[:max_chars] + "\n…[truncated; increase BROWSER_TASK_MAX_FINAL_RESULT_CHARS in .env if needed]",
        "final_result_truncated": True,
        "final_result_original_chars": len(text),
    }

_HEADED_RETRY_KEYS = (
    "captcha",
    "cloudflare",
    "turnstile",
    "just a moment",
    "checking your browser",
    "security verification",
    "ddos protection",
    "checking if the site connection is secure",
    "access denied",
    "sign in",
    "log in",
    "verify you are human",
    "unusual traffic",
    "automated queries",
    "automated access",
    "bot",
    "403",
    "forbidden",
    "blocked",
    "robot check",
    "press and hold",
    "prove you're not a robot",
    "challenge",
    "cf-ray",
    "attention required",
    "enable javascript",
    "ray id",
    "one more step",
)


def _mcp_json_response_flag() -> bool:
    v = os.getenv("MCP_JSON_RESPONSE", "true").strip().lower()
    return v in ("1", "true", "yes", "on")


def _grok_browser_playbook() -> list[str]:
    """Browser guidance returned on every get_status (read after connector reset)."""
    return [
        "CONNECTOR RESET: Copy grok_allowed_tools_csv from this get_status into the Grok allowlist; fully stop/start MCP so new tools (e.g. browser_watch_*) register — hot reload is not enough.",
        "TRANSPORT: browser_click/browser_type/browser_press_keys/browser_navigate default return_screenshot=false. return_screenshot=true runs a full CDP capture and often causes rmcp transport timeouts if used every click.",
        "WATCH MODE (SPA / Kalshi-style): browser_open_tab → browser_watch_start → poll browser_watch_status or fetch_url(latest_frame_url) → browser_click(x, y, return_screenshot=false) from the image → browser_watch_stop.",
        "SPA PAGE STATE: browser_get_page_state(include_visible_text=true) returns visible_regions with center_x/center_y. If element text is null, use light=true or coordinate clicks — not element_index alone.",
        "TABS: tab_reused=true is normal for same tab_label. stale=true or tab_stale_hub_disconnected: reset_browser_hub then browser_open_tab(reuse_existing_tab=false).",
        "browser_task: minutes-long; transport errors often mean client timeout while Chrome still works. Use granular tools + watch for browsing; browser_task only for captcha/heavy automation.",
    ]


def _grok_connector_hints() -> dict[str, Any]:
    """Actionable flags for Grok / xAI MCP clients (screenshots + secrets)."""
    pub = bool((os.getenv("PUBLIC_MCP_BASE_URL") or "").strip())
    secret_ok = secrets_store.master_key_configured()
    hints: list[str] = list(_grok_browser_playbook())
    if not secret_ok:
        hints.append(
            "request_user_secret returns secrets_not_configured until SECRETS_MASTER_KEY is set in .env; restart MCP."
        )
    if not pub:
        hints.append(
            "Set PUBLIC_MCP_BASE_URL=https://<same-host-as-MCP> (no path). Screenshots are never inlined in MCP JSON; "
            "the tool returns screenshot_url only — Grok (or fetch_url) must HTTPS GET that URL for the PNG."
        )
    else:
        hints.append(
            "Screenshots: HTTPS GET screenshot_url or Watch latest_frame_url. Optional BROWSER_SCREENSHOT_REQUIRE_BEARER=true "
            "requires the same Bearer as MCP on GET /browser-screenshot/… and /browser-watch/…/latest."
        )
    hints.append(
        "If the connector uses an explicit allowed_tools list, include every tool you need (copy grok_allowed_tools_csv from get_status) or leave the list empty when the client allows all tools."
    )
    if omi_api_key_configured():
        hints.append(
            "Omi wearable: when the user mentions past conversations, people, what they said, preferences, their week, or prep for a call, "
            "proactively call omi_recall once (plain-language query) — do not wait for 'use Omi'. For remember/don't forget, call omi_remember. "
            "In live voice prefer one omi_recall per turn; progressive transcript depth is automatic."
        )
    else:
        hints.append(
            "Omi: set request_user_secret(name='omi_api_key') once, then use omi_recall / omi_remember for personal history in voice or chat."
        )
    return {
        "grok_connector_hints": hints,
        "grok_browser_playbook": _grok_browser_playbook(),
        "screenshot_delivery": "screenshot_url_only",
        "browser_click_default_return_screenshot": False,
        "browser_watch_recommended_for_spa": True,
    }


def _resolve_browser_headed(task: str, headed: bool | None) -> tuple[bool, list[str]]:
    domain_hints = memory_store.extract_domains_from_text(task)
    if headed is not None:
        return headed, domain_hints
    for d in domain_hints:
        if memory_store.domain_headless_ok(d):
            continue
        pref = memory_store.domain_headed_preference(d)
        if pref is True:
            return True, domain_hints
    if os.getenv("BROWSER_HEADED", "false").lower() in ("1", "true", "yes", "on"):
        return True, domain_hints
    return False, domain_hints


def _primary_browser_domain(domain_hints: list[str], last_url: str | None) -> str | None:
    return _host_from_url(last_url) or (domain_hints[0] if domain_hints else None)


def _should_skip_headed_retry(domain_hints: list[str]) -> bool:
    for d in domain_hints:
        if memory_store.domain_headless_ok(d):
            return True
    return False


def _host_from_url(url: str | None) -> str | None:
    if not url or not isinstance(url, str):
        return None
    try:
        p = urlparse(url)
        h = (p.hostname or "").lower()
        return h or None
    except Exception:
        return None


def _browser_signals_blob(final: Any, summary: dict[str, Any] | None, err_text: str) -> str:
    parts = [str(final or ""), str(err_text or "")]
    if summary:
        try:
            parts.append(json.dumps(summary)[:12000])
        except Exception:
            parts.append(str(summary)[:8000])
    return " ".join(parts).lower()


def _needs_headed_retry(blob: str) -> bool:
    return any(k in blob for k in _HEADED_RETRY_KEYS)


def _browser_user_data_dir() -> str | None:
    raw = (os.getenv("BROWSER_USER_DATA_DIR") or "").strip()
    return raw or None


def _secret_prefill_summary(prefill: list[Any]) -> list[dict[str, Any]]:
    """Names and selectors only (no secret values) for run logs."""
    out: list[dict[str, Any]] = []
    for step in prefill:
        if not isinstance(step, dict):
            continue
        url = str(step.get("url") or "")
        names: list[str] = []
        selectors: list[str] = []
        for f in step.get("fills") or []:
            if isinstance(f, dict):
                sn = f.get("secret_name")
                if isinstance(sn, str) and sn:
                    names.append(sn)
                sel = f.get("selector")
                if isinstance(sel, str) and sel:
                    selectors.append(sel)
        out.append({"url": url, "secret_names": names, "selectors": selectors})
    return out


def _secrets_misconfigured_response() -> dict[str, Any]:
    return {
        "error": "secrets_not_configured",
        "hint": (
            "Set SECRETS_MASTER_KEY (urlsafe base64 Fernet key) in .env. "
            'Generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        ),
    }


def _screenshot_register_max_bytes() -> int:
    import screenshot_serve as ss

    return ss._max_bytes()


def _downscale_png_bytes_to_max_edge(data: bytes, max_edge: int) -> bytes | None:
    """Fast lossy resize when raw PNG exceeds BROWSER_SCREENSHOT_MAX_BYTES. Returns None on failure."""
    if max_edge <= 0 or not data:
        return None
    try:
        from io import BytesIO

        from PIL import Image

        im = Image.open(BytesIO(data))
        w, h = im.size
        if w <= max_edge and h <= max_edge:
            buf = BytesIO()
            im.save(buf, format="PNG")
            return buf.getvalue()
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA")
        im.thumbnail((max_edge, max_edge), Image.Resampling.BILINEAR)
        buf = BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        logger.warning("screenshot PNG downscale failed", exc_info=True)
        return None


def _prepare_png_bytes_for_register(data: bytes) -> tuple[bytes, bool]:
    """
    Keep full CDP quality when the PNG fits BROWSER_SCREENSHOT_MAX_BYTES (default 12MB).
    PIL resize runs only when the file is too large to register — not on every HiDPI capture.
    Optional BROWSER_SCREENSHOT_DOWNSCALE_IF_RAW_BYTES_LARGER_THAN (>0) forces early resize for ops tuning.
    """
    if not data:
        return data, False
    max_b = _screenshot_register_max_bytes()
    if len(data) <= max_b:
        try:
            threshold = int(os.getenv("BROWSER_SCREENSHOT_DOWNSCALE_IF_RAW_BYTES_LARGER_THAN", "0"))
        except ValueError:
            threshold = 0
        if threshold <= 0 or len(data) <= threshold:
            return data, False
    raw_edge = (os.getenv("BROWSER_SCREENSHOT_MAX_EDGE") or "1920").strip()
    try:
        max_edge = int(raw_edge)
    except ValueError:
        max_edge = 1920
    shrunk = _downscale_png_bytes_to_max_edge(data, max_edge)
    if shrunk is not None:
        return shrunk, True
    return data, False


def _screenshot_payload_from_png_bytes(data: bytes) -> dict[str, Any]:
    """Write PNG bytes to one-time URL register; MCP JSON never includes image bytes."""
    out: dict[str, Any] = {}
    if not data:
        return {"screenshot_note": "empty_screenshot_bytes"}

    data, downscaled = _prepare_png_bytes_for_register(data)

    base = (os.getenv("PUBLIC_MCP_BASE_URL") or "").strip().rstrip("/")
    if not base:
        out["screenshot_note"] = "set_PUBLIC_MCP_BASE_URL_https_same_origin_as_MCP_no_path_for_screenshot_url"
        out["screenshot_delivery"] = "none_missing_public_mcp_base_url"
        return out

    import screenshot_serve as ss

    tok = ss.register_png_bytes(data)
    if not tok:
        out["screenshot_note"] = "png_too_large_for_url_register_see_BROWSER_SCREENSHOT_MAX_BYTES"
        return out

    out["screenshot_url"] = f"{base}/browser-screenshot/{tok}"
    out["screenshot_url_single_use"] = True
    out["screenshot_url_ttl_seconds"] = int(os.getenv("BROWSER_SCREENSHOT_URL_TTL_SECONDS", "600"))
    out["screenshot_delivery"] = "url_only"
    if downscaled:
        out["screenshot_downscaled"] = True
    return out


def _build_screenshot_payload(raw_b64: str) -> dict[str, Any]:
    """
    Register a one-time HTTPS GET URL (PUBLIC_MCP_BASE_URL + /browser-screenshot/{token}).
    Accepts base64 only for browser-use step screenshots; decodes once then same path as raw bytes.
    """
    import base64
    import binascii

    try:
        data = base64.b64decode(raw_b64, validate=True)
    except (ValueError, binascii.Error):
        return {"screenshot_note": "invalid_screenshot_base64"}
    return _screenshot_payload_from_png_bytes(data)


async def _merge_tab_screenshot(tab_id: str, return_screenshot: bool, out: dict[str, Any]) -> dict[str, Any]:
    if not return_screenshot or not (tab_id or "").strip():
        return out
    png_bytes, err = await browser_hub.capture_tab_viewport_png_bytes(tab_id)
    if err:
        out["screenshot_note"] = err
        return out
    try:
        out.update(await asyncio.to_thread(_screenshot_payload_from_png_bytes, png_bytes))
    except Exception as e:
        out["screenshot_note"] = type(e).__name__
    return out


def _effective_browser_task(task: str, return_screenshot: bool) -> str:
    """Steer browser-use away from PDF-as-screenshot when MCP will return screenshot_url."""
    t = task.strip()
    rel = (
        "[MCP reliability: A long-running browser_task does not mean Tailscale or MCP is broken if "
        "ping/get_status still succeed. Bot walls (Cloudflare, Turnstile, 'verify you are human', "
        "'checking your browser') usually need headed=true on the next browser_task call "
        "(reuse continue_tab_id from list_browser_tabs when the tab is idle), or complete the "
        "challenge once in a headed window. If the operator fully closed Chromium and tools fail to attach, "
        "call reset_browser_hub once then browser_task again (the server also auto-detects a dead CDP port).]"
    )
    if rel not in t:
        t = f"{t}\n\n{rel}"
    if not return_screenshot:
        return t
    marker = "[MCP screenshot delivery:"
    if marker in t:
        return t
    return (
        f"{t}\n\n{marker} Do not use save_as_pdf or done(..., files_to_display) for images; "
        "the server captures the viewport over CDP when the run finishes. "
        "Stay on the final product page. "
        "Explicitly select required size/color options (do not assume defaults). "
        "The MCP tool result will include screenshot_url (HTTPS one-time GET), not embedded image bytes or local file paths.]"
    )


async def _final_viewport_screenshot_b64(browser: Any) -> str | None:
    """Last-resort PNG as urlsafe base64 when step history never attached a screenshot."""
    import base64

    try:
        data = await browser.take_screenshot()
    except Exception:
        logger.debug("browser_task final take_screenshot failed", exc_info=True)
        return None
    if not data:
        return None
    try:
        return base64.b64encode(data).decode("ascii")
    except Exception:
        return None


async def _browser_run_once(
    task: str,
    model: str,
    ms: int,
    to: float,
    use_vision: bool,
    headed_eff: bool,
    user_data_dir: str | None,
    *,
    return_screenshot: bool = False,
    browser: Any | None = None,
    browser_tab_id: str | None = None,
) -> tuple[Any | None, BaseException | None, str | None]:
    from browser_use import Agent, BrowserSession
    from browser_use.llm.deepseek.chat import ChatDeepSeek

    api_key = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
    # ChatOpenAI + DeepSeek returns 400: "This response_format type is unavailable now" — browser_use
    # expects structured steps; ChatDeepSeek uses tool-calling instead of OpenAI json_schema response_format.
    base_url = (os.getenv("DEEPSEEK_BASE_URL") or "").strip() or "https://api.deepseek.com/v1"
    llm = ChatDeepSeek(
        model=model,
        api_key=api_key,
        temperature=0.2,
        base_url=base_url,
    )
    owns_browser = browser is None
    if browser is None:
        bs_kwargs: dict[str, Any] = {"headless": not headed_eff, "keep_alive": True}
        if user_data_dir:
            bs_kwargs["user_data_dir"] = user_data_dir
        browser = BrowserSession(**bs_kwargs)
    agent = Agent(
        task=task.strip(),
        llm=llm,
        browser=browser,
        use_vision=use_vision,
        enable_signal_handler=False,
    )
    raw_b64: list[str | None] = [None]

    async def on_step_end(a: Any) -> None:
        if not return_screenshot:
            return
        try:
            snaps = a.history.screenshots(n_last=1, return_none_if_not_screenshot=True)
            if snaps and snaps[-1]:
                raw_b64[0] = snaps[-1]
        except Exception:
            logger.debug("browser_task on_step_end screenshot failed", exc_info=True)

    try:
        async with browser_hub.run_slot():
            history = await asyncio.wait_for(
                agent.run(max_steps=ms, on_step_end=on_step_end),
                timeout=float(to),
            )
        if return_screenshot and not raw_b64[0]:
            raw_b64[0] = await _final_viewport_screenshot_b64(browser)
            if raw_b64[0] is None:
                logger.warning(
                    "browser_task return_screenshot=true but no step screenshot and take_screenshot failed"
                )
        if browser_tab_id:
            await browser_hub.sync_tab_metadata(browser_tab_id, browser)
        return history, None, raw_b64[0]
    except BaseException as e:
        if return_screenshot and not raw_b64[0]:
            raw_b64[0] = await _final_viewport_screenshot_b64(browser)
        if browser_tab_id:
            await browser_hub.sync_tab_metadata(browser_tab_id, browser)
        return None, e, raw_b64[0]
    finally:
        if owns_browser and browser is not None:
            try:
                await browser.stop()
            except Exception:
                pass


def register_tools(mcp: FastMCP) -> None:
    register_cursor_tools(mcp)
    register_omi_tools(mcp)

    @mcp.tool()
    async def get_status() -> dict[str, Any]:
        """
        Redacted server status: env flags, memory counts, optional paths, configured tool names.
        Does not expose secrets. get_status is never blocked by MCP_DISABLED_TOOLS.
        """
        tools = [
            "ping",
            "fetch_url",
            "github_get_file",
            "github_list_repo_files",
            "github_get_diff",
            "github_create_issue",
            "request_user_secret",
            "list_secrets",
            "revoke_secret",
            "browser_task",
            "cursor_agent",
            "approve_cursor_writes",
            "revoke_cursor_writes",
            "get_run_log",
            "list_recent_runs",
            "get_status",
            "list_browser_tabs",
            "close_browser_tab",
            "reset_browser_hub",
            "browser_open_tab",
            "browser_navigate",
            "browser_get_page_state",
            "browser_click",
            "browser_type",
            "browser_press_keys",
            "browser_watch_start",
            "browser_watch_status",
            "browser_watch_stop",
            "browser_capture_tab_screenshot",
            "omi_ping",
            "omi_recall",
            "omi_remember",
            "omi_sync_index",
            "omi_list_conversations",
            "omi_get_conversation",
        ]
        mem = memory_store.memory_summary_for_status()
        log_dir = os.getenv("AGENT_LOG_DIR") or ""
        return {
            "service": "grok-browser-mcp-agent",
            "mcp_json_response": _mcp_json_response_flag(),
            "mcp_path": "/mcp/",
            "auth_token_configured": bool((os.getenv("AUTH_TOKEN") or "").strip()),
            "oauth_auth_configured": oauth_auth_configured(),
            "mcp_auth_configured": mcp_auth_configured(),
            "mcp_extra_allowed_hosts_configured": bool((os.getenv("MCP_EXTRA_ALLOWED_HOSTS") or "").strip()),
            "public_mcp_base_url_configured": bool((os.getenv("PUBLIC_MCP_BASE_URL") or "").strip()),
            "mcp_dns_rebinding_protection_disabled": (os.getenv("MCP_DNS_REBINDING_PROTECTION") or "")
            .strip()
            .lower()
            in ("0", "false", "no", "off"),
            "deepseek_configured": bool((os.getenv("DEEPSEEK_API_KEY") or "").strip()),
            "cursor_api_configured": bool((os.getenv("CURSOR_API_KEY") or "").strip()),
            "github_token_configured": bool((os.getenv("GITHUB_TOKEN") or "").strip()),
            "omi_api_key_configured": omi_api_key_configured(),
            "omi_index_ready": index_ready() if omi_api_key_configured() else False,
            "secrets_configured": secrets_store.master_key_configured(),
            "memory_file": str(memory_store.memory_file_path()),
            "memory_summary": mem,
            "agent_log_dir_configured": log_dir or None,
            "browser_user_data_configured": bool(_browser_user_data_dir()),
            **browser_hub.tabs_summary_for_status(),
            "mcp_disabled_tools_raw": (os.getenv("MCP_DISABLED_TOOLS") or "").strip() or None,
            "tools": tools,
            # Single-line copy-paste for Grok "allowed tools" when the connector requires an explicit list
            "grok_allowed_tools_csv": ",".join(tools),
            **_grok_connector_hints(),
        }

    @mcp.tool()
    async def get_run_log(run_id: str) -> dict[str, Any]:
        """
        Return a bounded, redacted event log for a prior browser_task or cursor_agent invocation (for debugging).
        Use list_recent_runs to discover run_id values. Does not include model chain-of-thought.
        """
        if (g := tool_gating.tool_disabled_error("get_run_log")) is not None:
            return g
        rid = (run_id or "").strip()
        if not rid:
            return {"error": "run_id is required"}
        data = get_run(rid)
        if data is None:
            return {"error": "not_found", "run_id": rid}
        return data

    @mcp.tool()
    async def list_recent_runs(limit: int = 20) -> dict[str, Any]:
        """List recent instrumented runs (newest first) with run_id and tool name for get_run_log."""
        if (g := tool_gating.tool_disabled_error("list_recent_runs")) is not None:
            return g
        lim = max(1, min(limit, 100))
        return {"runs": list_recent_runs_store(lim)}

    @mcp.tool()
    async def ping() -> Any:
        """Lightweight connectivity check for the MCP connector."""
        if (g := tool_gating.tool_disabled_error("ping")) is not None:
            return g
        return "pong"

    @mcp.tool()
    async def fetch_url(
        url: str,
        extract_text: bool = True,
    ) -> dict[str, Any]:
        """
        HTTP GET a public https URL (read-only). Blocks private IPs and cloud metadata hosts (SSRF guard).
        Returns truncated body; use for simple pages/APIs without a full browser.
        """
        if (g := tool_gating.tool_disabled_error("fetch_url")) is not None:
            return g
        parsed = _parse_public_https_url(url)
        if isinstance(parsed, dict):
            return parsed
        host, normalized = parsed
        if not await _resolve_host_is_safe(host):
            return {"error": "host resolves to a non-public address; request blocked"}

        headers = {"User-Agent": "grok-browser-mcp-agent/1.0"}
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=httpx.Timeout(30.0, connect=10.0),
                limits=httpx.Limits(max_connections=5, max_keepalive_connections=2),
            ) as client:
                r = await client.get(normalized, headers=headers)
                body = r.content[:_MAX_FETCH_BYTES]
                text = None
                if extract_text:
                    text = body.decode("utf-8", errors="replace")[:80000]
                return {
                    "status_code": r.status_code,
                    "final_url": str(r.url),
                    "content_length": len(r.content),
                    "truncated": len(r.content) > _MAX_FETCH_BYTES,
                    "text_excerpt": text,
                }
        except httpx.HTTPError as e:
            logger.warning("fetch_url failed: %s", type(e).__name__)
            return {"error": str(e)}

    @mcp.tool()
    async def github_get_file(
        owner: str,
        repo: str,
        path: str,
        ref: str | None = None,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        """
        Fetch file contents from github.com via REST (requires GITHUB_TOKEN).
        Optional `ref`: branch name, tag, or commit SHA (GitHub Contents API).
        Adds `content_text` (decoded UTF-8) for normal files; symlinks/submodules return hints.
        """
        if (g := tool_gating.tool_disabled_error("github_get_file")) is not None:
            return g
        token = (os.getenv("GITHUB_TOKEN") or "").strip()
        if not token:
            return {"error": "GITHUB_TOKEN is not configured on the server."}
        return await github_get_file_enriched(owner, repo, path, token, ref=ref, max_bytes=max_bytes)

    @mcp.tool()
    async def github_list_repo_files(
        owner: str,
        repo: str,
        ref: str,
        path: str = "",
        recursive: bool = False,
    ) -> dict[str, Any]:
        """
        List repository paths at an exact ref (branch, tag, or commit SHA).
        Non-recursive: one level via Contents API. recursive=true: full tree via Git API (capped).
        """
        if (g := tool_gating.tool_disabled_error("github_list_repo_files")) is not None:
            return g
        token = (os.getenv("GITHUB_TOKEN") or "").strip()
        if not token:
            return {"error": "GITHUB_TOKEN is not configured on the server."}
        return await github_list_repo_files_api(owner, repo, token, path=path, ref=ref, recursive=recursive)

    @mcp.tool()
    async def github_get_diff(
        owner: str,
        repo: str,
        base: str,
        head: str,
    ) -> dict[str, Any]:
        """
        Compare two refs (branches, tags, or SHAs) via GitHub compare API. Returns capped per-file patches.
        """
        if (g := tool_gating.tool_disabled_error("github_get_diff")) is not None:
            return g
        token = (os.getenv("GITHUB_TOKEN") or "").strip()
        if not token:
            return {"error": "GITHUB_TOKEN is not configured on the server."}
        return await github_compare_api(owner, repo, base, head, token)

    @mcp.tool()
    async def github_create_issue(
        owner: str,
        repo: str,
        title: str,
        body: str = "",
    ) -> dict[str, Any]:
        """Create a GitHub issue (requires GITHUB_TOKEN with issues write)."""
        if (g := tool_gating.tool_disabled_error("github_create_issue")) is not None:
            return g
        token = (os.getenv("GITHUB_TOKEN") or "").strip()
        if not token:
            return {"error": "GITHUB_TOKEN is not configured on the server."}
        if not _safe_github_segment(owner) or not _safe_github_segment(repo):
            return {"error": "invalid owner or repo"}
        api_url = f"https://api.github.com/repos/{owner}/{repo}/issues"
        payload = {"title": title[:256], "body": body[:30000]}
        return await github_request("POST", api_url, token, json=payload)

    @mcp.tool()
    async def request_user_secret(name: str, description: str = "") -> dict[str, Any]:
        """
        Start a short-lived HTTP form on 127.0.0.1 only so the operator can submit a secret once; value is
        encrypted locally (SECRETS_MASTER_KEY). Open submit_url on this PC in a browser. Never paste raw
        secrets into browser_task task text (that string is sent to DeepSeek).

        Requires SECRETS_MASTER_KEY in .env and MCP restart; otherwise returns secrets_not_configured. The Grok
        connector must list this tool in allowed_tools. After save, use secret_prefill in browser_task with the same name.
        """
        if (g := tool_gating.tool_disabled_error("request_user_secret")) is not None:
            return g
        return secrets_submit.start_secret_submit_server(name, description)

    @mcp.tool()
    async def list_secrets() -> dict[str, Any]:
        """Return stored secret names and created_at metadata only (no values). Requires SECRETS_MASTER_KEY."""
        if (g := tool_gating.tool_disabled_error("list_secrets")) is not None:
            return g
        if not secrets_store.master_key_configured():
            return _secrets_misconfigured_response()
        meta = secrets_store.list_secret_metadata()
        return {"names": [m["name"] for m in meta], "entries": meta}

    @mcp.tool()
    async def revoke_secret(name: str) -> dict[str, Any]:
        """Delete a stored secret by name (idempotent). Requires valid name format."""
        if (g := tool_gating.tool_disabled_error("revoke_secret")) is not None:
            return g
        if not secrets_store.master_key_configured():
            return _secrets_misconfigured_response()
        err = secrets_store.validate_secret_name(name)
        if err:
            return {"error": "invalid_name", "detail": err}
        secrets_store.delete_secret(name)
        return {"ok": True}

    @mcp.tool()
    async def list_browser_tabs(include_closed: bool = False) -> dict[str, Any]:
        """
        List tabs opened by browser_task on the shared Chrome instance (still open on your PC until you close them).
        If you closed Chromium manually and automation fails, call reset_browser_hub then browser_task again.
        Each tab has tab_id, run_id, label (Grok's stated purpose), status (running|idle|closed), url, title.
        """
        if (g := tool_gating.tool_disabled_error("list_browser_tabs")) is not None:
            return g
        return {"tabs": browser_hub.list_tabs(include_closed=include_closed)}

    @mcp.tool()
    async def close_browser_tab(tab_id: str) -> dict[str, Any]:
        """Close one browser_task tab by tab_id from list_browser_tabs or a prior browser_task result."""
        if (g := tool_gating.tool_disabled_error("close_browser_tab")) is not None:
            return g
        return await browser_hub.close_tab(tab_id)

    @mcp.tool()
    async def reset_browser_hub() -> dict[str, Any]:
        """
        Clear the shared Chromium CDP connection and in-memory tab list (e.g. after you closed the browser
        window manually, or automation cannot attach). The next browser_task starts a fresh Chromium instance.
        Old tab_id values are no longer valid.
        """
        if (g := tool_gating.tool_disabled_error("reset_browser_hub")) is not None:
            return g
        await browser_hub.force_reset_browser_hub("mcp_reset_browser_hub")
        return {
            "ok": True,
            "hint": "Call browser_task again; list_browser_tabs will be empty until a new tab is opened.",
        }

    @mcp.tool()
    async def browser_open_tab(
        tab_label: str = "",
        url: str = "",
        headed: bool | None = None,
        return_screenshot: bool = False,
        reuse_existing_tab: bool = True,
    ) -> dict[str, Any]:
        """
        Open a new tab in shared Chrome for granular control (no Browser Use agent, no 'Starting agent' tab).
        When reuse_existing_tab=true (default) and an idle tab has the same tab_label, returns that tab_id (tab_reused=true).
        Optional url navigates immediately. Prefer this over browser_task for login flows.
        """
        if (g := tool_gating.tool_disabled_error("browser_open_tab")) is not None:
            return g
        headed_eff, _ = _resolve_browser_headed("", headed)
        user_data = _browser_user_data_dir()
        out = await browser_granular.open_tab(
            tab_label,
            headed=headed_eff,
            user_data_dir=user_data,
            url=url or None,
            reuse_existing_tab=reuse_existing_tab,
        )
        if out.get("tab_id"):
            return await _merge_tab_screenshot(out["tab_id"], return_screenshot, out)
        return out

    @mcp.tool()
    async def browser_navigate(
        tab_id: str,
        url: str,
        return_screenshot: bool = False,
    ) -> dict[str, Any]:
        """
        Navigate a tracked tab to an https URL. Fast.
        Default return_screenshot=false (use browser_watch_start or browser_capture_tab_screenshot for vision).
        """
        if (g := tool_gating.tool_disabled_error("browser_navigate")) is not None:
            return g
        out = await browser_granular.navigate(tab_id, url)
        return await _merge_tab_screenshot(tab_id, return_screenshot, out)

    @mcp.tool()
    async def browser_get_page_state(
        tab_id: str,
        light: bool = False,
        include_visible_text: bool = True,
    ) -> dict[str, Any]:
        """
        Interactive elements (index, tag, text, data_testid, …) plus visible_regions for JS/SPA sites.
        light=true: skip heavy selector_map (faster); visible_regions only.
        On React SPAs prefer visible_regions.center_x/center_y with browser_click(x, y) or Watch Mode.
        """
        if (g := tool_gating.tool_disabled_error("browser_get_page_state")) is not None:
            return g
        return await browser_granular.get_page_state(
            tab_id, light=light, include_visible_text=include_visible_text
        )

    @mcp.tool()
    async def browser_click(
        tab_id: str,
        element_index: int | None = None,
        css_selector: str = "",
        x: float | None = None,
        y: float | None = None,
        return_screenshot: bool = False,
    ) -> dict[str, Any]:
        """
        Click by element index, css_selector, or viewport x/y (best for SPAs with Watch Mode).
        Default return_screenshot=false — use Watch or browser_capture_tab_screenshot to avoid transport timeouts.
        """
        if (g := tool_gating.tool_disabled_error("browser_click")) is not None:
            return g
        out = await browser_granular.click(
            tab_id,
            element_index=element_index,
            css_selector=css_selector,
            x=x,
            y=y,
        )
        return await _merge_tab_screenshot(tab_id, return_screenshot, out)

    @mcp.tool()
    async def browser_type(
        tab_id: str,
        text: str = "",
        element_index: int | None = None,
        css_selector: str = "",
        secret_name: str = "",
        clear_first: bool = True,
        return_screenshot: bool = False,
    ) -> dict[str, Any]:
        """
        Type into a field by element index or css_selector. Use secret_name (not raw password in tool args) for credentials.
        Default return_screenshot=false when using Watch Mode.
        """
        if (g := tool_gating.tool_disabled_error("browser_type")) is not None:
            return g
        out = await browser_granular.type_text(
            tab_id,
            text,
            element_index=element_index,
            css_selector=css_selector,
            secret_name=secret_name,
            clear_first=clear_first,
        )
        return await _merge_tab_screenshot(tab_id, return_screenshot, out)

    @mcp.tool()
    async def browser_press_keys(
        tab_id: str,
        keys: str,
        return_screenshot: bool = False,
    ) -> dict[str, Any]:
        """Send keys (e.g. Enter, Tab) to the focused tab. Default return_screenshot=false."""
        if (g := tool_gating.tool_disabled_error("browser_press_keys")) is not None:
            return g
        out = await browser_granular.press_keys(tab_id, keys)
        return await _merge_tab_screenshot(tab_id, return_screenshot, out)

    @mcp.tool()
    async def browser_watch_start(
        tab_id: str,
        duration_seconds: float | None = None,
        interval_seconds: float | None = None,
    ) -> dict[str, Any]:
        """
        Start background viewport captures for a tab (non-blocking). Returns watch_id and latest_frame_url.
        Poll with browser_watch_status or fetch_url(latest_frame_url); call browser_watch_stop when finished.
        """
        if (g := tool_gating.tool_disabled_error("browser_watch_start")) is not None:
            return g
        return await browser_watch.start_watch(tab_id, duration_seconds, interval_seconds)

    @mcp.tool()
    async def browser_watch_status(watch_id: str) -> dict[str, Any]:
        """Poll Watch Mode: active flag, frame_count, latest_frame_url (append ?t=frame_count when fetching)."""
        if (g := tool_gating.tool_disabled_error("browser_watch_status")) is not None:
            return g
        return browser_watch.get_watch_status(watch_id)

    @mcp.tool()
    async def browser_watch_stop(watch_id: str) -> dict[str, Any]:
        """Stop Watch Mode and return recent_screenshot_urls from the capture window."""
        if (g := tool_gating.tool_disabled_error("browser_watch_stop")) is not None:
            return g
        return await browser_watch.stop_watch(watch_id)

    @mcp.tool()
    async def browser_capture_tab_screenshot(tab_id: str = "") -> dict[str, Any]:
        """
        Fast viewport PNG via CDP from a tracked tab (no Browser Use / DeepSeek). Completes in seconds.
        Pass tab_id from a prior browser_task result, list_browser_tabs, or get_status → browser_tabs.
        If tab_id is omitted or empty and exactly one tab is unambiguous (single open tab, or single idle tab
        among several), that tab is used; otherwise the tool returns ambiguous_tabs_specify_tab_id with browser_tabs.
        Returns screenshot_url when PUBLIC_MCP_BASE_URL is set (same one-time URL pattern as browser_task).
        """
        if (g := tool_gating.tool_disabled_error("browser_capture_tab_screenshot")) is not None:
            return g
        resolved, rerr, pick = browser_hub.resolve_tab_id_for_screenshot(tab_id)
        if rerr:
            out: dict[str, Any] = {
                "error": rerr,
                "tab_id": (tab_id or "").strip() or None,
            }
            if rerr == "ambiguous_tabs_specify_tab_id":
                out["browser_tabs"] = browser_hub.list_tabs(include_closed=False)[:25]
                out["hint"] = "Pass tab_id for the tab you want (see browser_tabs)."
            elif rerr == "no_open_tabs":
                out["hint"] = "Run browser_task first, or call reset_browser_hub if Chromium was closed."
            elif rerr == "no_tracked_tabs_in_hub":
                out["hint"] = (
                    "This MCP process has no browser tabs yet (restart clears tabs, or tab_id is from an older chat). "
                    "Call browser_task or list_browser_tabs in this session, then use the tab_id from that response."
                )
            elif rerr == "tab_not_found_or_closed":
                out["hint"] = "Use list_browser_tabs or get_status for current tab_id values."
            return out
        png_bytes, err = await browser_hub.capture_tab_viewport_png_bytes(resolved or "")
        if err:
            out = {
                "error": "capture_failed",
                "detail": err,
                "tab_id": resolved,
            }
            if pick:
                out["tab_id_pick_reason"] = pick
            if not (os.getenv("PUBLIC_MCP_BASE_URL") or "").strip():
                out["hint"] = "Set PUBLIC_MCP_BASE_URL for screenshot_url after capture succeeds."
            return out
        try:
            payload = await asyncio.to_thread(_screenshot_payload_from_png_bytes, png_bytes)
        except Exception as e:
            logger.exception("browser_capture_tab_screenshot screenshot payload failed")
            return {
                "error": "screenshot_payload_build_failed",
                "detail": type(e).__name__,
                "tab_id": resolved,
                "hint": "Check server logs; try adjusting BROWSER_SCREENSHOT_DOWNSCALE_IF_RAW_BYTES_LARGER_THAN.",
            }
        payload["tab_id"] = resolved
        payload["success"] = True
        if pick:
            payload["tab_id_pick_reason"] = pick
        return payload

    @mcp.tool()
    async def browser_task(
        task: str,
        max_steps: int | None = None,
        timeout_seconds: int | None = None,
        use_vision: bool = False,
        headed: bool | None = None,
        use_reasoner: bool = False,
        llm_model: str | None = None,
        secret_prefill: list[Any] | None = None,
        return_screenshot: bool = False,
        tab_label: str | None = None,
        continue_tab_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Run a natural-language web automation task using Browser Use with DeepSeek (OpenAI-compatible API).

        For Grok vision of the page: pass return_screenshot=true and set PUBLIC_MCP_BASE_URL on the server; the tool
        returns screenshot_url (HTTPS one-time GET). Images are not embedded in MCP JSON — the client must fetch the URL.

        Default is headless; per-domain memory may force headed. After a headless run, the server may retry
        once in headed mode if step notes or the final result look like bot/login/Cloudflare friction (do not
        assume MCP or Tailscale is down while POST /mcp/ returns 200). For sites with verification interstitials,
        pass headed=true early. Set BROWSER_USER_DATA_DIR for persistent cookies. Requires DEEPSEEK_API_KEY.

        Optional secret_prefill: list of {url, fills:[{selector, secret_name}]} — Playwright fills secrets in the
        same shared Chrome as the agent when the hub is active (no second browser window).
        locally before the agent runs so values are not sent to the LLM. URLs must be https://. Never put
        raw secret values in task (task is sent to DeepSeek); reference stored names only.

        If return_screenshot=true, when a viewport PNG is available the tool returns a short-lived HTTPS URL
        (GET /browser-screenshot/{token}) when PUBLIC_MCP_BASE_URL matches your Funnel origin.

        IMPORTANT: Pass return_screenshot=true for screenshot_url (and set PUBLIC_MCP_BASE_URL). If return_screenshot
        is omitted, no screenshot fields are returned. The server also runs a final CDP viewport capture when the agent
        ends without a step screenshot (e.g. done + files_to_display) so Grok still gets a PNG URL when appropriate.

        By default each call opens a new tab. To avoid duplicate work, call list_browser_tabs or get_status first;
        if an idle tab already matches the goal, pass continue_tab_id (not a new tab). Tabs stay open when tasks end.
        Optional tab_label records purpose for list_browser_tabs. Use close_browser_tab when done with a tab.
        For a screenshot only (no agent, fast), use browser_capture_tab_screenshot (optional tab_id when only one tab matches) with browser_tab_id from a prior run or list_browser_tabs.
        Up to BROWSER_TASK_MAX_CONCURRENT (default 3) agents may run at once on different tabs.
        """
        if (g := tool_gating.tool_disabled_error("browser_task")) is not None:
            return g
        api_key = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
        if not api_key:
            return {"error": "DEEPSEEK_API_KEY is not configured on the server."}
        if not task or not task.strip():
            return {"error": "task must be a non-empty string"}

        max_task = int(os.getenv("BROWSER_TASK_MAX_INCOMING_TASK_CHARS", "65536"))
        if max_task > 0 and len(task) > max_task:
            return {
                "error": "task_too_long_for_mcp_client",
                "hint": (
                    f"Shorten the task string (max {max_task} chars) so the remote MCP client can send tools/call JSON. "
                    "Move long instructions to follow-up turns; use tab_label and continue_tab_id for tab context."
                ),
                "task_chars": len(task),
                "max_task_chars": max_task,
            }

        prefill = secret_prefill if secret_prefill is not None else []
        if prefill and not isinstance(prefill, list):
            return {"error": "secret_prefill must be a list of steps or omitted"}
        if prefill and not secrets_store.master_key_configured():
            return _secrets_misconfigured_response()

        ms = max_steps if max_steps is not None else _DEFAULT_BROWSER_MAX_STEPS
        ms = max(1, min(ms, 200))
        to = timeout_seconds if timeout_seconds is not None else _DEFAULT_BROWSER_TIMEOUT
        to = max(30, min(to, 900))

        fast = (os.getenv("BROWSER_USE_LLM_MODEL") or "deepseek-chat").strip()
        deep = (os.getenv("BROWSER_USE_LLM_MODEL_DEEP") or "deepseek-reasoner").strip()
        if llm_model and llm_model.strip():
            model = llm_model.strip()
        elif use_reasoner:
            model = deep
        else:
            model = fast

        headed_eff, domain_hints = _resolve_browser_headed(task, headed)
        user_data = _browser_user_data_dir()
        if user_data:
            try:
                Path(user_data).mkdir(parents=True, exist_ok=True)
            except OSError as e:
                logger.warning("BROWSER_USER_DATA_DIR mkdir failed: %s", e)

        try:
            import browser_use  # noqa: F401
        except ImportError as e:
            return {"error": f"browser_use not available: {e}"}

        task_redacted = secrets_store.redact_task_for_log(task.strip())
        run_meta: dict[str, Any] = {
            "task_preview_redacted": task_redacted,
            "max_steps": ms,
            "timeout_seconds": to,
            "model": model,
            "headed": headed_eff,
            "domain_hints": domain_hints,
            "user_data_dir": bool(user_data),
            "secret_prefill": bool(prefill),
            "return_screenshot": return_screenshot,
            "tab_label": (tab_label or "").strip()[:200] or None,
            "continue_tab_id": (continue_tab_id or "").strip() or None,
        }
        if prefill:
            run_meta["secret_prefill_summary"] = _secret_prefill_summary(prefill)

        task_for_agent = _effective_browser_task(task, return_screenshot)
        cont = (continue_tab_id or "").strip() or None
        label_for_match = (tab_label or "").strip()
        if not cont and label_for_match:
            auto_label = os.getenv("BROWSER_AUTO_CONTINUE_TAB_BY_LABEL", "false").strip().lower() in (
                "1",
                "true",
                "yes",
                "on",
            )
            if auto_label:
                existing = browser_hub.find_idle_tab_by_label(label_for_match)
                if existing:
                    cont = existing.tab_id
                    run_meta["continue_tab_id_auto_from_label"] = cont
        if cont:
            task_for_agent = (
                f"{task_for_agent}\n\n[MCP: Continuing on existing browser tab {cont}; "
                "do not restart from homepage unless required. Build on the current page state.]"
            )
        run_id = start_run("browser_task", run_meta)
        label = (tab_label or "").strip() or task.strip()[:120]
        open_tabs = browser_hub.list_tabs(include_closed=False)
        return await _browser_task_body(
            run_id=run_id,
            task_for_agent=task_for_agent,
            tab_label=label,
            continue_tab_id=cont,
            open_tabs_hint=open_tabs if not cont else None,
            prefill=prefill,
            ms=ms,
            ms2=max(1, ms - 10),
            to=to,
            model=model,
            use_vision=use_vision,
            headed_eff=headed_eff,
            domain_hints=domain_hints,
            user_data=user_data,
            return_screenshot=return_screenshot,
        )


async def _browser_task_body(
    *,
    run_id: str,
    task_for_agent: str,
    tab_label: str,
    continue_tab_id: str | None,
    open_tabs_hint: list[dict[str, Any]] | None,
    prefill: list[Any],
    ms: int,
    ms2: int,
    to: int,
    model: str,
    use_vision: bool,
    headed_eff: bool,
    domain_hints: list[str],
    user_data: str | None,
    return_screenshot: bool,
) -> dict[str, Any]:
    browser_session: Any | None = None
    browser_tab_id: str | None = None
    resumed = bool(continue_tab_id)
    try:
        if continue_tab_id:
            browser_session, browser_tab_id = await browser_hub.resume_tab_for_run(
                run_id,
                continue_tab_id,
                headed=headed_eff,
                user_data_dir=user_data,
            )
            append_event(
                run_id,
                {
                    "kind": "browser_tab_resumed",
                    "browser_tab_id": browser_tab_id,
                    "label": tab_label[:200],
                },
            )
        else:
            browser_session, browser_tab_id = await browser_hub.open_tab_for_run(
                run_id,
                tab_label,
                headed=headed_eff,
                user_data_dir=user_data,
            )
            append_event(
                run_id,
                {"kind": "browser_tab_opened", "browser_tab_id": browser_tab_id, "label": tab_label[:200]},
            )
    except Exception as e:
        logger.exception("browser tab setup failed")
        finish_run(run_id, "error", {"error": "browser_tab_setup_failed"})
        err_out: dict[str, Any] = {
            "run_id": run_id,
            "error": "browser_tab_setup_failed",
            "message": str(e)[:2000],
        }
        if open_tabs_hint:
            err_out["open_browser_tabs"] = open_tabs_hint
            err_out["hint"] = "Use continue_tab_id to resume an idle tab instead of opening a duplicate."
        return err_out

    def _with_tab(out: dict[str, Any]) -> dict[str, Any]:
        if browser_tab_id:
            out["browser_tab_id"] = browser_tab_id
            out["browser_tab_label"] = tab_label
            out["browser_tab_left_open"] = True
            out["browser_tab_resumed"] = resumed
        if open_tabs_hint and not resumed:
            idle = [t for t in open_tabs_hint if t.get("status") == "idle"]
            if idle:
                out["open_browser_tabs"] = idle
                out["hint"] = (
                    "Idle tabs are still open. Next time, use continue_tab_id on the tab that already has the "
                    "login page instead of starting another browser_task (each call adds a 'Starting agent …' tab)."
                )
            if len(open_tabs_hint) >= 1:
                out["browser_tabs_before_run"] = len(open_tabs_hint)
        return out

    try:
        return _with_tab(
            await _browser_task_body_inner(
                run_id=run_id,
                task_for_agent=task_for_agent,
                prefill=prefill,
                ms=ms,
                ms2=ms2,
                to=to,
                model=model,
                use_vision=use_vision,
                headed_eff=headed_eff,
                domain_hints=domain_hints,
                user_data=user_data,
                return_screenshot=return_screenshot,
                browser_session=browser_session,
                browser_tab_id=browser_tab_id,
            )
        )
    finally:
        if browser_tab_id and browser_session is not None:
            await browser_hub.mark_tab_idle(browser_tab_id, browser_session)


async def _browser_task_body_inner(
    *,
    run_id: str,
    task_for_agent: str,
    prefill: list[Any],
    ms: int,
    ms2: int,
    to: int,
    model: str,
    use_vision: bool,
    headed_eff: bool,
    domain_hints: list[str],
    user_data: str | None,
    return_screenshot: bool,
    browser_session: Any,
    browser_tab_id: str,
) -> dict[str, Any]:
    if prefill:
        append_event(run_id, {"kind": "secret_prefill_start"})
        perr = await browser_prefill.run_secret_prefill(
            prefill,
            headed_eff,
            user_data,
            cdp_url=browser_hub.hub_cdp_url(),
        )
        if perr:
            append_event(run_id, {"kind": "secret_prefill_failed", "message": perr[:800]})
            finish_run(
                run_id,
                "error",
                {"error": "secret_prefill_failed", "message": perr[:2000]},
            )
            return {
                "run_id": run_id,
                "error": "secret_prefill_failed",
                "message": perr,
                "headed": headed_eff,
                "headed_retry": False,
                "domain_hints": domain_hints,
            }
        append_event(run_id, {"kind": "secret_prefill_ok"})

    append_event(run_id, {"kind": "agent_created"})

    screenshot_payload: dict[str, Any] = {}

    def _merge_shot(out: dict[str, Any]) -> dict[str, Any]:
        if not return_screenshot:
            return out
        r = dict(out)
        r.update(screenshot_payload)
        return r

    def _after_screenshot_built(sp: dict[str, Any]) -> None:
        sn = sp.get("screenshot_note")
        if sn and any(x in sn for x in ("too_large", "invalid_screenshot", "png_too_large")):
            append_event(run_id, {"kind": "screenshot_omitted", "detail": str(sn)[:300]})

    history, err, raw_shot = await _browser_run_once(
        task_for_agent,
        model,
        ms,
        float(to),
        use_vision,
        headed_eff,
        user_data,
        return_screenshot=return_screenshot,
        browser=browser_session,
        browser_tab_id=browser_tab_id,
    )
    if return_screenshot and raw_shot:
        screenshot_payload = await asyncio.to_thread(_build_screenshot_payload, raw_shot)
        _after_screenshot_built(screenshot_payload)

    headed_retry = False

    def _finalize_success(hist: Any, headed_used: bool, retry: bool) -> dict[str, Any]:
        final = None
        try:
            final = hist.final_result()
        except Exception:
            pass
        last_url = None
        try:
            if hist.history:
                h = hist.history[-1]
                state = getattr(h, "state", None)
                if state is not None:
                    last_url = getattr(state, "url", None)
        except Exception:
            pass
        summary: dict[str, Any] = {}
        try:
            summary = summarize_browser_history(hist)
            append_event(run_id, {"kind": "browser_summary", **summary})
        except Exception as ex:
            append_event(run_id, {"kind": "browser_summary_skipped", "message": str(ex)[:500]})

        dom = _primary_browser_domain(domain_hints, last_url)
        if dom and headed_used:
            memory_store.set_domain_headed_preference(dom, True, "successful headed run")
        elif dom and not headed_used:
            memory_store.set_domain_headless_ok(dom, True)

        finish_run(
            run_id,
            "success",
            {"last_url": last_url, "final_result_preview": str(final)[:500] if final else None},
        )
        d = {
            "run_id": run_id,
            "success": True,
            "last_url": last_url,
            "max_steps": ms if not retry else ms2,
            "timeout_seconds": to,
            "model": model,
            "headed": headed_used,
            "headed_retry": retry,
            "domain_hints": domain_hints,
        }
        d.update(_final_result_mcp_payload(final))
        if return_screenshot:
            d.update(screenshot_payload)
        return d

    if isinstance(err, asyncio.TimeoutError):
        append_event(run_id, {"kind": "error", "name": "TimeoutError"})
        finish_run(run_id, "timeout", {"timeout_seconds": to, "max_steps": ms})
        return _merge_shot(
            {
                "run_id": run_id,
                "error": "timeout",
                "timeout_seconds": to,
                "max_steps": ms,
                "hint": (
                    "Retry with a narrower task or higher timeout_seconds (up to 900). "
                    "If the page may be behind Cloudflare or a bot check, retry with headed=true "
                    "(same continue_tab_id if list_browser_tabs shows that tab idle). "
                    "MCP returning 200/202 means the connector reached this PC; do not blame Tailscale "
                    "unless ping or get_status also fail."
                ),
                "headed": headed_eff,
                "headed_retry": False,
                "domain_hints": domain_hints,
            }
        )

    if err is not None:
        err_s = str(err)
        append_event(run_id, {"kind": "error", "name": type(err).__name__, "message": err_s[:1500]})
        blob = _browser_signals_blob(None, None, err_s)
        if not headed_eff and _needs_headed_retry(blob) and not _should_skip_headed_retry(domain_hints):
            append_event(
                run_id,
                {"kind": "headed_retry", "reason": "error_signals", "note": "high_impact: headed retry"},
            )
            headed_retry = True
            h2, err2, raw2 = await _browser_run_once(
                task_for_agent,
                model,
                ms2,
                float(to),
                use_vision,
                True,
                user_data,
                return_screenshot=return_screenshot,
                browser=browser_session,
                browser_tab_id=browser_tab_id,
            )
            if return_screenshot and raw2:
                screenshot_payload = await asyncio.to_thread(_build_screenshot_payload, raw2)
                _after_screenshot_built(screenshot_payload)
            if err2 is None and h2 is not None:
                memory_store.append_failure_hint(
                    "browser_task",
                    f"headed retry recovered after {type(err).__name__}",
                )
                memory_store.append_recovery_pattern(
                    "browser_task", "error_friction", "headed_retry_succeeded"
                )
                return _finalize_success(h2, True, True)
            append_event(
                run_id,
                {"kind": "headed_retry_failed", "message": str(err2)[:800] if err2 else "unknown"},
            )
        logger.exception("browser_task failed")
        finish_run(run_id, "error", {"error": type(err).__name__})
        return _merge_shot(
            {
                "run_id": run_id,
                "error": type(err).__name__,
                "message": err_s[:2000],
                "headed": headed_eff,
                "headed_retry": headed_retry,
                "domain_hints": domain_hints,
            }
        )

    assert history is not None
    final = None
    try:
        final = history.final_result()
    except Exception:
        pass
    summary: dict[str, Any] = {}
    try:
        summary = summarize_browser_history(history)
    except Exception:
        pass
    blob = _browser_signals_blob(final, summary, "")
    if not headed_eff and _needs_headed_retry(blob) and not _should_skip_headed_retry(domain_hints):
        append_event(
            run_id,
            {"kind": "headed_retry", "reason": "result_signals", "note": "high_impact: headed retry"},
        )
        headed_retry = True
        h2, err2, raw2 = await _browser_run_once(
            task_for_agent,
            model,
            ms2,
            float(to),
            use_vision,
            True,
            user_data,
            return_screenshot=return_screenshot,
            browser=browser_session,
            browser_tab_id=browser_tab_id,
        )
        if return_screenshot and raw2:
            screenshot_payload = await asyncio.to_thread(_build_screenshot_payload, raw2)
            _after_screenshot_built(screenshot_payload)
        if err2 is None and h2 is not None:
            dom = domain_hints[0] if domain_hints else None
            if dom:
                memory_store.set_domain_headed_preference(dom, True, "headed retry after bot/login signals")
            memory_store.append_failure_hint("browser_task", "headed retry after friction signals")
            memory_store.append_recovery_pattern("browser_task", "result_friction", "headed_retry_succeeded")
            return _finalize_success(h2, True, True)
        append_event(
            run_id,
            {"kind": "headed_retry_failed", "message": str(err2)[:800] if err2 else "unknown"},
        )
        out = _finalize_success(history, headed_eff, True)
        out["headed_retry_error"] = str(err2)[:1000] if err2 else None
        out["hint"] = "Headed retry failed; returning headless result. Check last_url or widen task."
        return out

    return _finalize_success(history, headed_eff, False)


def _parse_public_https_url(url: str) -> tuple[str, str] | dict[str, Any]:
    raw = (url or "").strip()
    try:
        p = urlparse(raw)
    except Exception:
        return {"error": "invalid url"}
    if p.scheme != "https":
        return {"error": "only https URLs are allowed"}
    host = (p.hostname or "").lower()
    if not host or host == "localhost" or host.endswith(".local"):
        return {"error": "host not allowed"}
    if host in ("metadata.google.internal", "metadata.goog"):
        return {"error": "host blocked"}
    if host == "169.254.169.254":
        return {"error": "host blocked"}
    return host, raw


async def _resolve_host_is_safe(hostname: str) -> bool:
    def _check() -> bool:
        try:
            infos = socket.getaddrinfo(hostname, None)
        except OSError:
            return False
        for info in infos:
            ip_str = info[4][0]
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                continue
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_reserved
                or ip.is_unspecified
            ):
                return False
            if ip.version == 4:
                if ip in ipaddress.ip_network("169.254.0.0/16"):
                    return False
                if ip in ipaddress.ip_network("100.64.0.0/10"):  # CGNAT
                    return False
        return bool(infos)

    return await asyncio.to_thread(_check)
