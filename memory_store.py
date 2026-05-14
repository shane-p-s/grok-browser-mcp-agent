"""File-backed operator memory: Cursor approvals/rules, browser headed/headless prefs, hints."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_DEFAULT_REL = Path("grok-mcp-agent") / "memory.json"


def memory_file_path() -> Path:
    raw = (os.getenv("AGENT_MEMORY_PATH") or "").strip()
    if raw:
        return Path(raw)
    base = os.getenv("LOCALAPPDATA") or os.getenv("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / _DEFAULT_REL


def _default_doc() -> dict[str, Any]:
    return {
        "version": 2,
        "cursor_write_allowed": {},
        "cursor_rules": {},
        "browser_domain_headed": {},
        "browser_domain_notes": {},
        "browser_domain_headless_ok": {},
        "failure_hints": [],
        "recovery_patterns": [],
    }


def _load_unlocked() -> dict[str, Any]:
    p = memory_file_path()
    if not p.is_file():
        return _default_doc()
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("not a dict")
        data.setdefault("version", 1)
        data.setdefault("cursor_write_allowed", {})
        data.setdefault("cursor_rules", {})
        data.setdefault("browser_domain_headed", {})
        data.setdefault("browser_domain_notes", {})
        data.setdefault("browser_domain_headless_ok", {})
        data.setdefault("failure_hints", [])
        data.setdefault("recovery_patterns", [])
        if int(data.get("version", 1)) < 2:
            data["version"] = 2
        return data
    except Exception as e:
        logger.warning("memory load failed, using empty: %s", e)
        return _default_doc()


def _save_unlocked(data: dict[str, Any]) -> None:
    p = memory_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(p)


def _norm_workspace(ws: str) -> str:
    try:
        return str(Path(ws).resolve()).lower()
    except OSError:
        return ws.strip().lower()


def is_cursor_write_allowed(workspace_path: str) -> bool:
    key = _norm_workspace(workspace_path)
    with _lock:
        d = _load_unlocked()
        if bool(d.get("cursor_write_allowed", {}).get(key)):
            return True
        rules = d.get("cursor_rules", {}).get(key) or {}
        return bool(rules.get("always_allow_level_3"))


def set_cursor_write_allowed(workspace_path: str, allowed: bool) -> None:
    key = _norm_workspace(workspace_path)
    with _lock:
        d = _load_unlocked()
        d.setdefault("cursor_write_allowed", {})
        d["cursor_write_allowed"][key] = allowed
        if not allowed:
            d.setdefault("cursor_rules", {})
            d["cursor_rules"].pop(key, None)
        _save_unlocked(d)


def set_cursor_always_allow_level_3(workspace_path: str, enabled: bool) -> None:
    """Persistent rule: treat workspace as approved for Level 3 without separate session flag."""
    key = _norm_workspace(workspace_path)
    with _lock:
        d = _load_unlocked()
        d.setdefault("cursor_rules", {})
        if enabled:
            d["cursor_rules"][key] = {"always_allow_level_3": True}
        else:
            d["cursor_rules"].pop(key, None)
        _save_unlocked(d)


def domain_headed_preference(domain: str) -> bool | None:
    dkey = domain.strip().lower()
    if not dkey:
        return None
    with _lock:
        d = _load_unlocked()
        m = d.get("browser_domain_headed", {})
        if dkey not in m:
            return None
        return bool(m[dkey])


def domain_headless_ok(domain: str) -> bool:
    dkey = domain.strip().lower()
    if not dkey:
        return False
    with _lock:
        d = _load_unlocked()
        return bool(d.get("browser_domain_headless_ok", {}).get(dkey))


def set_domain_headed_preference(domain: str, prefers_headed: bool, note: str = "") -> None:
    dkey = domain.strip().lower()
    if not dkey:
        return
    with _lock:
        d = _load_unlocked()
        d.setdefault("browser_domain_headed", {})
        d.setdefault("browser_domain_notes", {})
        d["browser_domain_headed"][dkey] = prefers_headed
        if note:
            d["browser_domain_notes"][dkey] = note[:500]
        _save_unlocked(d)


def set_domain_headless_ok(domain: str, ok: bool = True) -> None:
    dkey = domain.strip().lower()
    if not dkey:
        return
    with _lock:
        d = _load_unlocked()
        d.setdefault("browser_domain_headless_ok", {})
        if ok:
            d["browser_domain_headless_ok"][dkey] = True
        else:
            d["browser_domain_headless_ok"].pop(dkey, None)
        _save_unlocked(d)


def append_failure_hint(tool: str, hint: str) -> None:
    with _lock:
        d = _load_unlocked()
        lst = d.setdefault("failure_hints", [])
        lst.append({"ts": time.time(), "tool": tool, "hint": hint[:800]})
        del lst[:-50]
        _save_unlocked(d)


def append_recovery_pattern(tool: str, signal: str, action: str) -> None:
    with _lock:
        d = _load_unlocked()
        lst = d.setdefault("recovery_patterns", [])
        lst.append({"ts": time.time(), "tool": tool, "signal": signal[:400], "action": action[:400]})
        del lst[:-40]
        _save_unlocked(d)


def extract_domains_from_text(text: str) -> list[str]:
    out: list[str] = []
    for m in re.finditer(r"https?://([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})", text or ""):
        host = m.group(1).lower()
        if host not in out:
            out.append(host)
    return out


def memory_summary_for_status() -> dict[str, Any]:
    with _lock:
        d = _load_unlocked()
        return {
            "cursor_write_workspaces_count": len(d.get("cursor_write_allowed", {})),
            "cursor_rules_workspaces_count": len(d.get("cursor_rules", {})),
            "browser_domains_tracked": len(d.get("browser_domain_headed", {})),
            "browser_domains_headless_ok_count": len(d.get("browser_domain_headless_ok", {})),
            "recent_failure_hints": d.get("failure_hints", [])[-5:],
            "recent_recovery_patterns": d.get("recovery_patterns", [])[-5:],
        }
