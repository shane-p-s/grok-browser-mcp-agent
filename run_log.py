"""Bounded, redacted run logs for MCP tools (Grok debugging). Thread-safe in-process store + optional JSONL."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_disk_lock = threading.Lock()
_runs: dict[str, dict[str, Any]] = {}
_recent_ids: deque[str] = deque()

_DISK_LOG_NAME = "agent_events.ndjson"

_MAX_EVENTS = int(os.getenv("AGENT_LOG_MAX_EVENTS_PER_RUN", "200"))
_RETAIN_RUNS = int(os.getenv("AGENT_LOG_RETAIN_RUNS", "50"))
_MAX_RESPONSE = int(os.getenv("AGENT_LOG_MAX_RESPONSE_CHARS", "96000"))
_DISK = os.getenv("AGENT_LOG_ENABLE_DISK", "false").lower() in ("1", "true", "yes", "on")


def _default_log_dir() -> Path:
    base = os.getenv("LOCALAPPDATA") or os.getenv("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(os.getenv("AGENT_LOG_DIR", str(Path(base) / "grok-mcp-agent" / "logs")))


def redact_string(text: str) -> str:
    if not text:
        return text
    s = text
    s = re.sub(r"(?i)Bearer\s+\S+", "Bearer [REDACTED]", s)
    for key in (
        "CURSOR_API_KEY",
        "DEEPSEEK_API_KEY",
        "GITHUB_TOKEN",
        "AUTH_TOKEN",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "SECRETS_MASTER_KEY",
    ):
        s = re.sub(rf"(?i){re.escape(key)}\s*=\s*\S+", f"{key}=[REDACTED]", s)
    s = re.sub(r"(?i)(password|passwd|pwd)\s*[:=]\s*\S+", r"\1=[REDACTED]", s)
    s = re.sub(r"(?i)set-cookie:\s*[^\n]+", "set-cookie: [REDACTED]", s)
    s = re.sub(r"(?i)cookie:\s*[^\n]+", "cookie: [REDACTED]", s)
    return s


def redact_value(obj: Any) -> Any:
    if isinstance(obj, str):
        return redact_string(obj)
    if isinstance(obj, dict):
        return {k: redact_value(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_value(x) for x in obj]
    return obj


def start_run(tool_name: str, meta: dict[str, Any] | None = None) -> str:
    run_id = str(uuid.uuid4())
    entry = {
        "run_id": run_id,
        "tool": tool_name,
        "started_at": time.time(),
        "events": [],
        "meta": redact_value(meta or {}),
    }
    with _lock:
        _runs[run_id] = entry
        _recent_ids.append(run_id)
        while len(_recent_ids) > _RETAIN_RUNS:
            old = _recent_ids.popleft()
            _runs.pop(old, None)
    _append_disk({"type": "start", "run_id": run_id, "tool": tool_name, "ts": time.time(), "meta": entry["meta"]})
    return run_id


def append_event(run_id: str, event: dict[str, Any]) -> None:
    ev = redact_value(dict(event))
    ev.setdefault("ts", time.time())
    with _lock:
        r = _runs.get(run_id)
        if not r:
            return
        events: list = r["events"]
        events.append(ev)
        if len(events) > _MAX_EVENTS:
            del events[: len(events) - _MAX_EVENTS]
    _append_disk({"type": "event", "run_id": run_id, **ev})


def finish_run(run_id: str, status: str, summary: dict[str, Any] | None = None) -> None:
    payload = {"status": status, "summary": redact_value(summary or {})}
    append_event(run_id, {"kind": "finish", **payload})


def get_run(run_id: str) -> dict[str, Any] | None:
    with _lock:
        r = _runs.get(run_id)
        if not r:
            return None
        out = {
            "run_id": r["run_id"],
            "tool": r["tool"],
            "started_at": r["started_at"],
            "events": list(r["events"]),
            "meta": r.get("meta", {}),
        }
    text = json.dumps(out, default=str)
    if len(text) > _MAX_RESPONSE:
        evs = out["events"]
        while len(json.dumps({**out, "events": evs}, default=str)) > _MAX_RESPONSE and len(evs) > 5:
            evs = evs[len(evs) // 10 :]  # drop oldest 10%
        out["events"] = evs
        out["response_truncated"] = True
        out["note"] = f"Payload capped near {_MAX_RESPONSE} characters."
    return out


def list_recent_runs(limit: int = 20) -> list[dict[str, Any]]:
    lim = max(1, min(limit, 100))
    with _lock:
        ids = list(_recent_ids)[-lim:][::-1]
        rows = []
        for rid in ids:
            r = _runs.get(rid)
            if not r:
                continue
            rows.append(
                {
                    "run_id": rid,
                    "tool": r["tool"],
                    "started_at": r["started_at"],
                    "event_count": len(r["events"]),
                }
            )
    return rows


def _append_disk(record: dict[str, Any]) -> None:
    if not _DISK:
        return
    try:
        d = _default_log_dir()
        d.mkdir(parents=True, exist_ok=True)
        line = json.dumps(redact_value(record), default=str) + "\n"
        path = d / _DISK_LOG_NAME
        with _disk_lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
    except OSError as e:
        logger.warning("AGENT_LOG disk write failed: %s", e)
    except (TypeError, ValueError) as e:
        logger.warning("AGENT_LOG disk serialize failed: %s", e)


def summarize_browser_history(history: Any) -> dict[str, Any]:
    """Extract non-sensitive step summary from Browser Use AgentHistoryList (no model thinking)."""
    steps_out: list[dict[str, Any]] = []
    try:
        hist = getattr(history, "history", None) or []
        total = len(hist)
        tail = hist[-80:]
        start_idx = total - len(tail)
        for i, h in enumerate(tail):
            step: dict[str, Any] = {"step_index": start_idx + i}
            try:
                st = getattr(h, "state", None)
                if st is not None and hasattr(st, "to_dict"):
                    d = st.to_dict()
                    if isinstance(d, dict) and "url" in d:
                        step["url"] = d.get("url")
            except Exception:
                pass
            actions: list[str] = []
            try:
                mo = getattr(h, "model_output", None)
                if mo and getattr(mo, "action", None):
                    for act in mo.action:
                        name = type(act).__name__
                        actions.append(name)
                # Include short model text so server-side headed-retry can see Cloudflare/captcha signals
                # (these fields are not in final_result until done; action_types alone miss them).
                if mo is not None:
                    bits: list[str] = []
                    for attr in ("evaluation_previous_goal", "memory", "next_goal"):
                        try:
                            raw = getattr(mo, attr, None)
                        except Exception:
                            raw = None
                        if isinstance(raw, str) and raw.strip():
                            bits.append(raw.strip())
                    if bits:
                        joined = " | ".join(bits)
                        step["model_notes"] = joined[:2000]
            except Exception:
                pass
            if actions:
                step["action_types"] = actions[:15]
            err = getattr(h, "error", None)
            if err:
                step["error"] = str(err)[:500]
            steps_out.append(step)
    except Exception as e:
        return {"error": "summarize_failed", "message": str(e)[:500]}
    return {"step_count": len(getattr(history, "history", []) or []), "steps": steps_out}
