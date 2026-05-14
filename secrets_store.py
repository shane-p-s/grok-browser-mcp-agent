"""Encrypted local secret storage (never sent to remote LLMs). Thread-safe."""

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
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,62}$")

_DEFAULT_REL = Path("grok-mcp-agent") / "secrets.enc.json"


def secrets_file_path() -> Path:
    raw = (os.getenv("SECRETS_STORE_PATH") or "").strip()
    if raw:
        return Path(raw)
    base = os.getenv("LOCALAPPDATA") or os.getenv("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / _DEFAULT_REL


def _fernet():
    from cryptography.fernet import Fernet

    key = (os.getenv("SECRETS_MASTER_KEY") or "").strip().encode("ascii")
    if not key:
        return None
    try:
        return Fernet(key)
    except Exception as e:
        logger.warning("invalid SECRETS_MASTER_KEY: %s", e)
        return None


def master_key_configured() -> bool:
    return _fernet() is not None


def validate_secret_name(name: str) -> str | None:
    n = (name or "").strip()
    if not _NAME_RE.fullmatch(n):
        return "invalid secret name: use lowercase letters, digits, underscore; start with a letter; length 2-63"
    return None


def _load_doc() -> dict[str, Any]:
    p = secrets_file_path()
    if not p.is_file():
        return {"version": 1, "entries": {}}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("bad format")
        data.setdefault("version", 1)
        data.setdefault("entries", {})
        return data
    except Exception as e:
        logger.warning("secrets file load failed: %s", e)
        return {"version": 1, "entries": {}}


def _save_doc(data: dict[str, Any]) -> None:
    p = secrets_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(p)


def set_secret(name: str, plaintext: str) -> tuple[bool, str | None]:
    err = validate_secret_name(name)
    if err:
        return False, err
    f = _fernet()
    if f is None:
        return False, "SECRETS_MASTER_KEY is not set or invalid (use `python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"`)"
    if not plaintext:
        return False, "secret value must be non-empty"
    token = f.encrypt(plaintext.encode("utf-8")).decode("ascii")
    with _lock:
        d = _load_doc()
        ent = d.setdefault("entries", {})
        ent[name] = {"v": token, "created_at": time.time()}
        _save_doc(d)
    return True, None


def get_secret(name: str) -> str | None:
    err = validate_secret_name(name)
    if err:
        return None
    f = _fernet()
    if f is None:
        return None
    with _lock:
        d = _load_doc()
        row = d.get("entries", {}).get(name)
        if not row or "v" not in row:
            return None
        token = row["v"]
    try:
        return f.decrypt(token.encode("ascii")).decode("utf-8")
    except Exception:
        logger.warning("secret decrypt failed for name=%s", name)
        return None


def delete_secret(name: str) -> bool:
    """Remove secret by name. Returns False if name is invalid; True if absent or deleted (idempotent)."""
    err = validate_secret_name(name)
    if err:
        return False
    with _lock:
        d = _load_doc()
        ent = d.setdefault("entries", {})
        if name in ent:
            del ent[name]
            _save_doc(d)
    return True


def list_secret_metadata() -> list[dict[str, Any]]:
    """Name and created_at only; never values."""
    with _lock:
        d = _load_doc()
        out: list[dict[str, Any]] = []
        for name, row in sorted((d.get("entries") or {}).items()):
            if isinstance(row, dict) and "created_at" in row:
                out.append({"name": name, "created_at": row["created_at"]})
            else:
                out.append({"name": name})
    return out


def redact_task_for_log(task: str, max_len: int = 240) -> str:
    """Strip {{secret:name}} markers to [secret:name] for run logs."""
    if not task:
        return ""
    s = re.sub(r"\{\{\s*secret:([a-z][a-z0-9_]*)\s*\}\}", r"[secret:\1]", task, flags=re.IGNORECASE)
    return s[:max_len]
