"""One-time PNG download URLs for browser_task screenshots (avoid huge base64 in MCP JSON)."""

from __future__ import annotations

import logging
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_store: dict[str, tuple[Path, float]] = {}


def _ttl_seconds() -> float:
    return float(int(os.getenv("BROWSER_SCREENSHOT_URL_TTL_SECONDS", "600")))


def _max_bytes() -> int:
    return int(os.getenv("BROWSER_SCREENSHOT_MAX_BYTES", "12000000"))


def _purge_expired_locked(now: float | None = None) -> None:
    """Caller must hold _lock."""
    t = time.time() if now is None else now
    dead = [k for k, (_, exp) in _store.items() if exp < t]
    for k in dead:
        p, _ = _store.pop(k, (None, 0))
        if p is not None:
            try:
                p.unlink(missing_ok=True)
            except OSError as e:
                logger.debug("screenshot purge unlink: %s", e)


def purge_expired() -> None:
    with _lock:
        _purge_expired_locked()


def register_png_bytes(data: bytes) -> str | None:
    """Store PNG on disk; return opaque token for URL. None if too large."""
    max_b = _max_bytes()
    if len(data) > max_b:
        logger.warning("screenshot register skipped: %s bytes > max %s", len(data), max_b)
        return None
    tok = secrets.token_urlsafe(24)
    path = Path(tempfile.gettempdir()) / f"grok-mcp-sc-{tok}.png"
    try:
        path.write_bytes(data)
    except OSError as e:
        logger.warning("screenshot register write failed: %s", e)
        return None
    exp = time.time() + _ttl_seconds()
    with _lock:
        _purge_expired_locked()
        _store[tok] = (path, exp)
    return tok


def take_path_for_token(token: str) -> Path | None:
    """Pop a valid token and return path to PNG (caller reads then deletes file)."""
    if not token or len(token) > 200 or ".." in token:
        return None
    purge_expired()
    with _lock:
        ent = _store.pop(token, None)
    if not ent:
        return None
    path, exp = ent
    if time.time() > exp:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    return path
