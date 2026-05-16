"""Shared Chromium with per-task tabs; tabs stay open after browser_task (keep_alive)."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

TabStatus = Literal["running", "idle", "closed"]


@dataclass
class TabRecord:
    tab_id: str
    run_id: str
    label: str
    target_id: str | None = None
    url: str | None = None
    title: str | None = None
    status: TabStatus = "running"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


_hub_lock = asyncio.Lock()
_launch_lock = asyncio.Lock()
_tabs: dict[str, TabRecord] = {}
_cdp_url: str | None = None
_headed_launched: bool | None = None
_user_data_dir: str | None = None
_run_sem: asyncio.Semaphore | None = None


def max_concurrent() -> int:
    return max(1, min(int(os.getenv("BROWSER_TASK_MAX_CONCURRENT", "3")), 8))


def _semaphore() -> asyncio.Semaphore:
    global _run_sem
    if _run_sem is None:
        _run_sem = asyncio.Semaphore(max_concurrent())
    return _run_sem


def run_slot() -> asyncio.Semaphore:
    """Limit concurrent agent.run() calls against the shared browser."""
    return _semaphore()


def _cdp_json_version_http_url(cdp_ws: str) -> str | None:
    """Map ws://host:port/... to http://host:port/json/version for a cheap liveness probe."""
    try:
        u = urlparse(cdp_ws)
        if not u.hostname or not u.port:
            return None
        host = u.hostname
        if u.scheme not in ("ws", "wss"):
            return None
        return f"http://{host}:{u.port}/json/version"
    except Exception:
        return None


async def _cdp_url_alive(cdp_ws: str) -> bool:
    http_u = _cdp_json_version_http_url(cdp_ws)
    if not http_u:
        return True
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(2.0, connect=1.0)) as client:
            r = await client.get(http_u)
            return r.status_code == 200
    except Exception:
        return False


async def force_reset_browser_hub(reason: str = "manual_or_recovery") -> None:
    """Clear cached CDP URL and tab registry (e.g. after user closed Chromium). Next browser_task launches fresh."""
    global _cdp_url, _headed_launched, _user_data_dir
    logger.warning("browser hub force reset: %s", reason)
    async with _launch_lock:
        _cdp_url = None
        _headed_launched = None
        _user_data_dir = None
    async with _hub_lock:
        _tabs.clear()


async def _ensure_cdp_url(*, headed: bool, user_data_dir: str | None) -> str:
    global _cdp_url, _headed_launched, _user_data_dir
    async with _launch_lock:
        if _cdp_url:
            if not await _cdp_url_alive(_cdp_url):
                logger.warning("cached CDP port is dead (browser closed?); clearing hub state")
                _cdp_url = None
                _headed_launched = None
                _user_data_dir = None
                async with _hub_lock:
                    _tabs.clear()
            else:
                if headed and _headed_launched is False:
                    logger.warning(
                        "browser hub already running headless; later headed tasks may not show a visible window"
                    )
                return _cdp_url
        from browser_use import BrowserSession

        _user_data_dir = user_data_dir
        session = BrowserSession(
            headless=not headed,
            user_data_dir=user_data_dir,
            keep_alive=True,
        )
        await session.start()
        url = session.cdp_url or session.browser_profile.cdp_url
        if not url:
            raise RuntimeError("browser hub failed to obtain CDP URL after start()")
        _cdp_url = url
        _headed_launched = headed
        logger.info("browser hub started (headed=%s) cdp=%s", headed, _cdp_url[:48])
        return _cdp_url


async def create_attached_session(*, headed: bool, user_data_dir: str | None) -> Any:
    from browser_use import BrowserSession

    last_err: BaseException | None = None
    for attempt in range(2):
        try:
            cdp = await _ensure_cdp_url(headed=headed, user_data_dir=user_data_dir)
            session = BrowserSession(
                cdp_url=cdp,
                headless=not headed,
                user_data_dir=user_data_dir or _user_data_dir,
                keep_alive=True,
            )
            await session.start()
            return session
        except BaseException as e:
            last_err = e
            logger.warning(
                "create_attached_session failed (attempt %s/2): %s",
                attempt + 1,
                e,
            )
            await force_reset_browser_hub(f"attach_or_start_failed:{type(e).__name__}")
    assert last_err is not None
    raise last_err


async def open_tab_for_run(
    run_id: str,
    label: str,
    *,
    headed: bool,
    user_data_dir: str | None,
) -> tuple[Any, str]:
    """New tab in shared Chrome; returns (BrowserSession for this task, tab_id)."""
    from browser_use.browser.events import SwitchTabEvent

    session = await create_attached_session(headed=headed, user_data_dir=user_data_dir)
    page = await session.new_page("about:blank")
    target_id = page._target_id
    await session.event_bus.dispatch(SwitchTabEvent(target_id=target_id))
    tab_id = secrets.token_urlsafe(8)
    text = (label or "browser_task").strip()[:200] or "browser_task"
    async with _hub_lock:
        _tabs[tab_id] = TabRecord(
            tab_id=tab_id,
            run_id=run_id,
            label=text,
            target_id=target_id,
            url="about:blank",
            status="running",
        )
    return session, tab_id


async def resume_tab_for_run(
    run_id: str,
    tab_id: str,
    *,
    headed: bool,
    user_data_dir: str | None,
) -> tuple[Any, str]:
    """Focus an existing idle tab and continue automation there (no new tab)."""
    from browser_use.browser.events import SwitchTabEvent

    tid = (tab_id or "").strip()
    if not tid:
        raise ValueError("tab_id is required")
    async with _hub_lock:
        rec = _tabs.get(tid)
        if not rec or rec.status == "closed":
            raise ValueError(f"tab not found or closed: {tid}")
        if rec.status == "running":
            raise ValueError(f"tab {tid} is still running another browser_task")
        target_id = rec.target_id
        label = rec.label
    if not target_id:
        raise ValueError(f"tab {tid} has no browser target yet")
    session = await create_attached_session(headed=headed, user_data_dir=user_data_dir)
    await session.event_bus.dispatch(SwitchTabEvent(target_id=target_id))
    async with _hub_lock:
        rec = _tabs.get(tid)
        if rec:
            rec.run_id = run_id
            rec.status = "running"
            rec.updated_at = time.time()
    await sync_tab_metadata(tid, session)
    logger.info("resumed browser tab %s (%s)", tid, label[:60])
    return session, tid


async def sync_tab_metadata(tab_id: str, session: Any) -> None:
    url: str | None = None
    title: str | None = None
    target_id: str | None = None
    try:
        info = await session.get_current_target_info()
        if info:
            url = info.get("url")
            title = info.get("title")
            target_id = info.get("targetId")
    except Exception as e:
        logger.debug("sync_tab_metadata failed for %s: %s", tab_id, e)
    async with _hub_lock:
        rec = _tabs.get(tab_id)
        if not rec:
            return
        if url:
            rec.url = url
        if title:
            rec.title = title
        if target_id:
            rec.target_id = target_id
        rec.updated_at = time.time()


async def mark_tab_idle(tab_id: str, session: Any) -> None:
    await sync_tab_metadata(tab_id, session)
    async with _hub_lock:
        rec = _tabs.get(tab_id)
        if rec and rec.status != "closed":
            rec.status = "idle"
            rec.updated_at = time.time()


def list_tabs(*, include_closed: bool = False) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for rec in sorted(_tabs.values(), key=lambda r: r.created_at):
        if rec.status == "closed" and not include_closed:
            continue
        out.append(
            {
                "tab_id": rec.tab_id,
                "run_id": rec.run_id,
                "label": rec.label,
                "status": rec.status,
                "url": rec.url,
                "title": rec.title,
                "target_id": rec.target_id,
                "created_at": rec.created_at,
                "updated_at": rec.updated_at,
            }
        )
    return out


def tabs_summary_for_status() -> dict[str, Any]:
    running = sum(1 for t in _tabs.values() if t.status == "running")
    idle = sum(1 for t in _tabs.values() if t.status == "idle")
    open_tabs = list_tabs(include_closed=False)
    return {
        "browser_hub_active": _cdp_url is not None,
        "browser_tabs_running": running,
        "browser_tabs_idle": idle,
        "browser_task_max_concurrent": max_concurrent(),
        "browser_tabs": open_tabs[:25],
        "browser_tabs_hint": (
            "Before opening a duplicate task, check browser_tabs (or list_browser_tabs). "
            "To continue on an existing idle tab, call browser_task with continue_tab_id=<tab_id>. "
            "For a fast PNG without running the agent, use browser_capture_tab_screenshot(tab_id)."
            if open_tabs
            else None
        ),
    }


async def capture_tab_viewport_png_b64(tab_id: str) -> tuple[str | None, str | None]:
    """
    Focus tab and capture viewport via CDP (no Browser Use / DeepSeek).
    Returns (standard base64 PNG string, error code) — error is None on success.
    """
    import base64

    from browser_use.browser.events import SwitchTabEvent

    tid = (tab_id or "").strip()
    if not tid:
        return None, "tab_id_required"
    if not _cdp_url:
        return None, "browser_hub_inactive_run_browser_task_first"
    async with _hub_lock:
        rec = _tabs.get(tid)
        if not rec or rec.status == "closed":
            return None, "tab_not_found_or_closed"
        target_id = rec.target_id
        headed = bool(_headed_launched)
        udd = _user_data_dir
    if not target_id:
        return None, "tab_has_no_target_id"
    try:
        session = await create_attached_session(headed=headed, user_data_dir=udd)
        await session.event_bus.dispatch(SwitchTabEvent(target_id=target_id))
        data = await session.take_screenshot()
    except Exception as e:
        logger.warning("capture_tab_viewport_png_b64 %s: %s", tid, e)
        return None, f"capture_failed:{type(e).__name__}"
    if not data:
        return None, "empty_screenshot"
    try:
        await sync_tab_metadata(tid, session)
    except Exception:
        pass
    try:
        return base64.b64encode(data).decode("ascii"), None
    except Exception:
        return None, "base64_encode_failed"


async def close_tab(tab_id: str) -> dict[str, Any]:
    tid = (tab_id or "").strip()
    if not tid:
        return {"error": "tab_id is required"}
    async with _hub_lock:
        rec = _tabs.get(tid)
        if not rec:
            return {"error": "not_found", "tab_id": tid}
        if rec.status == "closed":
            return {"ok": True, "tab_id": tid, "already_closed": True}
        target_id = rec.target_id
        headed = bool(_headed_launched)
        udd = _user_data_dir
    if not _cdp_url or not target_id:
        async with _hub_lock:
            rec = _tabs.get(tid)
            if rec:
                rec.status = "closed"
        return {"ok": True, "tab_id": tid, "note": "no_cdp_or_target; marked closed"}
    try:
        session = await create_attached_session(headed=headed, user_data_dir=udd)
        await session.close_page(target_id)
    except Exception as e:
        logger.warning("close_tab %s: %s", tid, e)
        return {"error": "close_failed", "tab_id": tid, "message": str(e)[:500]}
    async with _hub_lock:
        rec = _tabs.get(tid)
        if rec:
            rec.status = "closed"
            rec.updated_at = time.time()
    return {"ok": True, "tab_id": tid}
