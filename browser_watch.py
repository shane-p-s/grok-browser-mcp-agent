"""Background viewport capture (Watch Mode) with a reusable latest-frame HTTPS URL."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shutil
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from tempfile import gettempdir

import browser_hub

logger = logging.getLogger(__name__)

_lock = asyncio.Lock()
_watches: dict[str, "WatchSession"] = {}
_tab_to_watch: dict[str, str] = {}


@dataclass
class WatchSession:
    watch_id: str
    tab_id: str
    task: asyncio.Task[None]
    started_at: float
    ends_at: float
    interval_sec: float
    frame_count: int = 0
    latest_path: Path = field(default_factory=Path)
    history_urls: list[str] = field(default_factory=list)
    stopped: bool = False
    grace_until: float = 0.0


def _public_base() -> str:
    return (os.getenv("PUBLIC_MCP_BASE_URL") or "").strip().rstrip("/")


def _max_duration() -> float:
    try:
        return float(max(5, min(int(os.getenv("BROWSER_WATCH_MAX_DURATION_SECONDS", "30")), 120)))
    except ValueError:
        return 30.0


def _default_interval() -> float:
    try:
        return float(max(1.0, min(float(os.getenv("BROWSER_WATCH_DEFAULT_INTERVAL_SECONDS", "2")), 10.0)))
    except ValueError:
        return 2.0


def _max_history() -> int:
    try:
        return max(0, min(int(os.getenv("BROWSER_WATCH_MAX_HISTORY_FRAMES", "10")), 50))
    except ValueError:
        return 10


def _grace_seconds() -> float:
    try:
        return float(max(0, int(os.getenv("BROWSER_WATCH_HTTP_GRACE_SECONDS", "60"))))
    except ValueError:
        return 60.0


def latest_frame_url(watch_id: str) -> str | None:
    base = _public_base()
    if not base or not watch_id:
        return None
    return f"{base}/browser-watch/{watch_id}/latest"


def get_latest_path_for_http(watch_id: str) -> Path | None:
    wid = (watch_id or "").strip()
    if not wid or ".." in wid or len(wid) > 200:
        return None
    sess = _watches.get(wid)
    if not sess:
        return None
    now = time.time()
    if sess.stopped and now > sess.grace_until:
        return None
    if not sess.stopped and now >= sess.ends_at:
        return None
    if sess.latest_path.is_file():
        return sess.latest_path
    return None


async def _schedule_watch_cleanup(watch_id: str, watch_dir: Path, delay_sec: float) -> None:
    try:
        await asyncio.sleep(max(0.0, delay_sec))
    except asyncio.CancelledError:
        return
    async with _lock:
        _watches.pop(watch_id, None)
    try:
        if watch_dir.is_dir():
            shutil.rmtree(watch_dir, ignore_errors=True)
    except OSError as e:
        logger.debug("watch delayed cleanup %s: %s", watch_id, e)


async def start_watch(
    tab_id: str,
    duration_seconds: float | None = None,
    interval_seconds: float | None = None,
) -> dict[str, object]:
    tid = (tab_id or "").strip()
    if not tid:
        return {"error": "tab_id_required"}
    rec = browser_hub.get_tab_record(tid)
    if not rec or rec.status == "closed":
        return {"error": "tab_not_found_or_closed", "tab_id": tid}

    dur = float(duration_seconds) if duration_seconds is not None else _max_duration()
    dur = max(5.0, min(dur, _max_duration()))
    interval = float(interval_seconds) if interval_seconds is not None else _default_interval()
    interval = max(1.0, min(interval, 10.0))

    async with _lock:
        if tid in _tab_to_watch:
            existing_id = _tab_to_watch[tid]
            return {
                "error": "watch_already_active_for_tab",
                "tab_id": tid,
                "watch_id": existing_id,
                "hint": "Call browser_watch_stop first or use browser_watch_status.",
            }

        watch_id = secrets.token_urlsafe(16)
        watch_dir = Path(gettempdir()) / f"grok-mcp-watch-{watch_id}"
        watch_dir.mkdir(parents=True, exist_ok=True)
        latest_path = watch_dir / "latest.png"
        now = time.time()
        ends_at = now + dur

        task = asyncio.create_task(_watch_loop(watch_id, tid, ends_at, interval, latest_path))
        sess = WatchSession(
            watch_id=watch_id,
            tab_id=tid,
            task=task,
            started_at=now,
            ends_at=ends_at,
            interval_sec=interval,
            latest_path=latest_path,
        )
        _watches[watch_id] = sess
        _tab_to_watch[tid] = watch_id

    base = _public_base()
    out: dict[str, object] = {
        "success": True,
        "watch_id": watch_id,
        "tab_id": tid,
        "active": True,
        "interval_seconds": interval,
        "duration_seconds": dur,
        "ends_at": datetime.fromtimestamp(ends_at, tz=timezone.utc).isoformat(),
        "frame_count": 0,
        "poll_hint": "Call browser_watch_status or fetch_url(latest_frame_url); append ?t=<frame_count> to avoid stale cache.",
    }
    if base:
        out["latest_frame_url"] = latest_frame_url(watch_id)
    else:
        out["screenshot_note"] = "set_PUBLIC_MCP_BASE_URL_for_latest_frame_url"
    return out


def get_watch_status(watch_id: str) -> dict[str, object]:
    wid = (watch_id or "").strip()
    sess = _watches.get(wid)
    if not sess:
        return {"error": "watch_not_found", "watch_id": wid}
    now = time.time()
    active = not sess.stopped and now < sess.ends_at and not sess.task.done()
    remaining = max(0.0, sess.ends_at - now) if active else 0.0
    out: dict[str, object] = {
        "watch_id": wid,
        "tab_id": sess.tab_id,
        "active": active,
        "frame_count": sess.frame_count,
        "seconds_remaining": round(remaining, 1),
        "interval_seconds": sess.interval_sec,
    }
    url = latest_frame_url(wid)
    if url:
        out["latest_frame_url"] = f"{url}?t={sess.frame_count}"
    return out


async def stop_watch(watch_id: str) -> dict[str, object]:
    wid = (watch_id or "").strip()
    async with _lock:
        sess = _watches.get(wid)
        if not sess:
            return {"error": "watch_not_found", "watch_id": wid}
        _tab_to_watch.pop(sess.tab_id, None)
        sess.stopped = True
        sess.grace_until = time.time() + _grace_seconds()
        watch_dir = sess.latest_path.parent
        frame_count = sess.frame_count
        tab_id = sess.tab_id
        history = list(sess.history_urls)

    if not sess.task.done():
        sess.task.cancel()
        try:
            await sess.task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("watch task end %s: %s", wid, e)

    asyncio.create_task(_schedule_watch_cleanup(wid, watch_dir, _grace_seconds()))

    return {
        "success": True,
        "watch_id": wid,
        "tab_id": tab_id,
        "active": False,
        "frame_count": frame_count,
        "recent_screenshot_urls": history,
    }


async def cancel_watch_for_tab(tab_id: str) -> None:
    tid = (tab_id or "").strip()
    async with _lock:
        wid = _tab_to_watch.pop(tid, None)
    if wid:
        await stop_watch(wid)


async def cancel_all_watches() -> None:
    async with _lock:
        sessions = list(_watches.values())
        _watches.clear()
        _tab_to_watch.clear()
    for sess in sessions:
        if not sess.task.done():
            sess.task.cancel()
        try:
            if sess.latest_path.parent.is_dir():
                shutil.rmtree(sess.latest_path.parent, ignore_errors=True)
        except OSError as e:
            logger.debug("watch cancel cleanup %s: %s", sess.watch_id, e)


async def _watch_loop(
    watch_id: str,
    tab_id: str,
    ends_at: float,
    interval_sec: float,
    latest_path: Path,
) -> None:
    import screenshot_serve as ss

    base = _public_base()
    max_hist = _max_history()
    try:
        while time.time() < ends_at:
            png, err = await browser_hub.capture_tab_viewport_png_bytes(tab_id)
            if png and not err:
                try:
                    latest_path.write_bytes(png)
                    async with _lock:
                        sess = _watches.get(watch_id)
                        if sess:
                            sess.frame_count += 1
                            if base and max_hist > 0:
                                tok = ss.register_png_bytes(png)
                                if tok:
                                    url = f"{base}/browser-screenshot/{tok}"
                                    sess.history_urls.append(url)
                                    if len(sess.history_urls) > max_hist:
                                        sess.history_urls = sess.history_urls[-max_hist:]
                except OSError as e:
                    logger.warning("watch write frame %s: %s", watch_id, e)
            elif err:
                logger.debug("watch capture %s: %s", watch_id, err)
            await asyncio.sleep(interval_sec)
    except asyncio.CancelledError:
        pass
    finally:
        watch_dir = latest_path.parent
        async with _lock:
            sess = _watches.get(watch_id)
            if sess and not sess.stopped:
                sess.stopped = True
                sess.grace_until = time.time() + _grace_seconds()
                _tab_to_watch.pop(tab_id, None)
        asyncio.create_task(_schedule_watch_cleanup(watch_id, watch_dir, _grace_seconds()))
