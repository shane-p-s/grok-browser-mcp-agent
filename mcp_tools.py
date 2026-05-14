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

import memory_store
import tool_gating
from cursor_agent_tools import register_cursor_tools
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


async def _browser_run_once(
    task: str,
    model: str,
    ms: int,
    to: float,
    use_vision: bool,
    headed_eff: bool,
    user_data_dir: str | None,
) -> tuple[Any | None, BaseException | None]:
    from browser_use import Agent, BrowserSession, ChatOpenAI

    api_key = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
    llm = ChatOpenAI(
        model=model,
        base_url="https://api.deepseek.com",
        api_key=api_key,
        temperature=0.2,
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
    try:
        history = await asyncio.wait_for(agent.run(max_steps=ms), timeout=float(to))
        return history, None
    except BaseException as e:
        return None, e


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
            "deepseek_configured": bool((os.getenv("DEEPSEEK_API_KEY") or "").strip()),
            "cursor_api_configured": bool((os.getenv("CURSOR_API_KEY") or "").strip()),
            "github_token_configured": bool((os.getenv("GITHUB_TOKEN") or "").strip()),
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
    async def browser_task(
        task: str,
        max_steps: int | None = None,
        timeout_seconds: int | None = None,
        use_vision: bool = False,
        headed: bool | None = None,
        use_reasoner: bool = False,
        llm_model: str | None = None,
    ) -> dict[str, Any]:
        """
        Run a natural-language web automation task using Browser Use with DeepSeek (OpenAI-compatible API).
        Default is headless; per-domain memory may force headed. After a headless run, the server may retry
        once in headed mode if the result looks like bot/login/captcha friction. Set BROWSER_USER_DATA_DIR
        for persistent cookies. Requires DEEPSEEK_API_KEY.
        """
        if (g := tool_gating.tool_disabled_error("browser_task")) is not None:
            return g
        api_key = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
        if not api_key:
            return {"error": "DEEPSEEK_API_KEY is not configured on the server."}
        if not task or not task.strip():
            return {"error": "task must be a non-empty string"}

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

        run_id = start_run(
            "browser_task",
            {
                "task_preview": task.strip()[:240],
                "max_steps": ms,
                "timeout_seconds": to,
                "model": model,
                "headed": headed_eff,
                "domain_hints": domain_hints,
                "user_data_dir": bool(user_data),
            },
        )
        append_event(run_id, {"kind": "agent_created"})

        history, err = await _browser_run_once(
            task.strip(), model, ms, float(to), use_vision, headed_eff, user_data
        )

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
            return {
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

        if isinstance(err, asyncio.TimeoutError):
            append_event(run_id, {"kind": "error", "name": "TimeoutError"})
            finish_run(run_id, "timeout", {"timeout_seconds": to, "max_steps": ms})
            return {
                "run_id": run_id,
                "error": "timeout",
                "timeout_seconds": to,
                "max_steps": ms,
                "hint": "Retry with a narrower task or higher timeout_seconds (up to 900).",
                "headed": headed_eff,
                "headed_retry": False,
                "domain_hints": domain_hints,
            }

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
                h2, err2 = await _browser_run_once(
                    task.strip(), model, ms2, float(to), use_vision, True, user_data
                )
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
            return {
                "run_id": run_id,
                "error": type(err).__name__,
                "message": err_s[:2000],
                "headed": headed_eff,
                "headed_retry": headed_retry,
                "domain_hints": domain_hints,
            }

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
            h2, err2 = await _browser_run_once(
                task.strip(), model, ms2, float(to), use_vision, True, user_data
            )
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
