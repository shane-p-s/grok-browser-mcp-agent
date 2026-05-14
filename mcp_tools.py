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

import browser_prefill
import memory_store
import secrets_store
import secrets_submit
import tool_gating
from cursor_agent_tools import register_cursor_tools
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

_HEADED_RETRY_KEYS = (
    "captcha",
    "cloudflare",
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
)


def _mcp_json_response_flag() -> bool:
    v = os.getenv("MCP_JSON_RESPONSE", "true").strip().lower()
    return v in ("1", "true", "yes", "on")


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


def _clip_screenshot_base64(b64: str) -> tuple[str | None, str | None]:
    """Cap screenshot payload size for MCP clients. Returns (data, None) or (None, reason)."""
    max_chars = int(os.getenv("BROWSER_TASK_SCREENSHOT_MAX_BASE64_CHARS", "700000"))
    if max_chars <= 0 or len(b64) <= max_chars:
        return b64, None
    return None, f"screenshot_too_large_base64_chars={len(b64)}_max={max_chars}"


def _build_screenshot_payload(raw_b64: str) -> dict[str, Any]:
    """
    Prefer PUBLIC_MCP_BASE_URL + one-time GET /browser-screenshot/{token} so clients fetch the full PNG
    instead of embedding megabytes of base64 in JSON. Set BROWSER_SCREENSHOT_INCLUDE_BASE64=true to also inline.
    """
    import base64
    import binascii

    out: dict[str, Any] = {}
    try:
        data = base64.b64decode(raw_b64, validate=True)
    except (ValueError, binascii.Error):
        return {"screenshot_note": "invalid_screenshot_base64"}

    base = (os.getenv("PUBLIC_MCP_BASE_URL") or "").strip().rstrip("/")
    include_b64 = (os.getenv("BROWSER_SCREENSHOT_INCLUDE_BASE64") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    url_ok = False
    if base:
        import screenshot_serve as ss

        tok = ss.register_png_bytes(data)
        if tok:
            out["screenshot_url"] = f"{base}/browser-screenshot/{tok}"
            out["screenshot_url_single_use"] = True
            out["screenshot_url_ttl_seconds"] = int(os.getenv("BROWSER_SCREENSHOT_URL_TTL_SECONDS", "600"))
            url_ok = True
        else:
            out["screenshot_note"] = "png_too_large_for_url_register_see_BROWSER_SCREENSHOT_MAX_BYTES"

    if (not url_ok) or include_b64:
        cb, note = _clip_screenshot_base64(raw_b64)
        if cb:
            out["screenshot_base64"] = cb
            out["screenshot_mime"] = "image/png"
        if note:
            prev = out.get("screenshot_note")
            out["screenshot_note"] = f"{prev};{note}" if prev else note
    elif url_ok:
        out["screenshot_delivery"] = "url_only_no_inline_base64"
    return out


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
    bs_kwargs: dict[str, Any] = {"headless": not headed_eff}
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
        history = await asyncio.wait_for(
            agent.run(max_steps=ms, on_step_end=on_step_end),
            timeout=float(to),
        )
        return history, None, raw_b64[0]
    except BaseException as e:
        return None, e, raw_b64[0]


def register_tools(mcp: FastMCP) -> None:
    register_cursor_tools(mcp)

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
            "secrets_configured": secrets_store.master_key_configured(),
            "memory_file": str(memory_store.memory_file_path()),
            "memory_summary": mem,
            "agent_log_dir_configured": log_dir or None,
            "browser_user_data_configured": bool(_browser_user_data_dir()),
            "mcp_disabled_tools_raw": (os.getenv("MCP_DISABLED_TOOLS") or "").strip() or None,
            "tools": tools,
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
    ) -> dict[str, Any]:
        """
        Run a natural-language web automation task using Browser Use with DeepSeek (OpenAI-compatible API).
        Default is headless; per-domain memory may force headed. After a headless run, the server may retry
        once in headed mode if the result looks like bot/login/captcha friction. Set BROWSER_USER_DATA_DIR
        for persistent cookies. Requires DEEPSEEK_API_KEY.

        Optional secret_prefill: list of {url, fills:[{selector, secret_name}]} — Playwright fills secrets
        locally before the agent runs so values are not sent to the LLM. URLs must be https://. Never put
        raw secret values in task (task is sent to DeepSeek); reference stored names only.

        If return_screenshot=true, when a viewport PNG is available the tool prefers a short-lived HTTPS URL
        (GET /browser-screenshot/{token}) when PUBLIC_MCP_BASE_URL matches your Funnel origin — Grok fetches the
        full image without megabytes of base64 in JSON. Optional BROWSER_SCREENSHOT_INCLUDE_BASE64=true also inlines
        clipped base64 (BROWSER_TASK_SCREENSHOT_MAX_BASE64_CHARS, default 700000). Without PUBLIC_MCP_BASE_URL,
        behavior matches the legacy inline path only.
        """
        if (g := tool_gating.tool_disabled_error("browser_task")) is not None:
            return g
        api_key = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
        if not api_key:
            return {"error": "DEEPSEEK_API_KEY is not configured on the server."}
        if not task or not task.strip():
            return {"error": "task must be a non-empty string"}

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
        }
        if prefill:
            run_meta["secret_prefill_summary"] = _secret_prefill_summary(prefill)
        run_id = start_run("browser_task", run_meta)

        if prefill:
            append_event(run_id, {"kind": "secret_prefill_start"})
            perr = await browser_prefill.run_secret_prefill(prefill, headed_eff, user_data)
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
            task.strip(),
            model,
            ms,
            float(to),
            use_vision,
            headed_eff,
            user_data,
            return_screenshot=return_screenshot,
        )
        if return_screenshot and raw_shot:
            screenshot_payload = _build_screenshot_payload(raw_shot)
            _after_screenshot_built(screenshot_payload)

        headed_retry = False
        ms2 = max(1, ms - 10)

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
                "final_result": final,
                "last_url": last_url,
                "max_steps": ms if not retry else ms2,
                "timeout_seconds": to,
                "model": model,
                "headed": headed_used,
                "headed_retry": retry,
                "domain_hints": domain_hints,
            }
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
                    "hint": "Retry with a narrower task or higher timeout_seconds (up to 900).",
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
                    task.strip(),
                    model,
                    ms2,
                    float(to),
                    use_vision,
                    True,
                    user_data,
                    return_screenshot=return_screenshot,
                )
                if return_screenshot and raw2:
                    screenshot_payload = _build_screenshot_payload(raw2)
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
                task.strip(),
                model,
                ms2,
                float(to),
                use_vision,
                True,
                user_data,
                return_screenshot=return_screenshot,
            )
            if return_screenshot and raw2:
                screenshot_payload = _build_screenshot_payload(raw2)
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
