"""Ephemeral localhost HTTP server for one-time secret submission (127.0.0.1 only)."""

from __future__ import annotations

import html
import logging
import secrets as std_secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import secrets_store

logger = logging.getLogger(__name__)

_MAX_CONCURRENT = 2
_server_lock = threading.Lock()
_active_count = 0


def _pick_port() -> int:
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_secret_submit_server(
    name: str,
    description: str,
    ttl_seconds: int = 900,
) -> dict[str, Any]:
    """
    Start a daemon HTTP server on 127.0.0.1. Returns submit_url or error.
    """
    if not secrets_store.master_key_configured():
        return {"error": "secrets_not_configured", "hint": "Set SECRETS_MASTER_KEY in .env (Fernet key)."}
    err = secrets_store.validate_secret_name(name)
    if err:
        return {"error": "invalid_name", "detail": err}

    global _active_count
    with _server_lock:
        if _active_count >= _MAX_CONCURRENT:
            return {
                "error": "too_many_pending_submits",
                "hint": f"At most {_MAX_CONCURRENT} local submit servers at once. Wait for TTL or finish a pending form.",
            }
        _active_count += 1

    token = std_secrets.token_urlsafe(32)
    port = _pick_port()
    holder: dict[str, Any] = {"server": None, "done": False, "error": None}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path != f"/submit/{token}":
                self._send(404, b"Not found")
                return
            desc_esc = html.escape(description or "")
            name_esc = html.escape(name)
            page = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Submit secret</title></head>
<body>
<h1>Submit secret: {name_esc}</h1>
<p>{desc_esc}</p>
<p><strong>This page is only on your PC (127.0.0.1).</strong> Do not expose this port publicly.</p>
<form method="POST" action="/submit/{token}">
<label>Secret value: <input type="password" name="secret" required autocomplete="off" size="48"/></label>
<button type="submit">Save locally</button>
</form>
</body></html>"""
            self._send(200, page.encode("utf-8"))

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if path != f"/submit/{token}":
                self._send(404, b"Not found")
                return
            raw_cl = (self.headers.get("Content-Length") or "0").strip()
            try:
                length = int(raw_cl)
            except ValueError:
                self._send(400, b"Bad content length header")
                return
            if length <= 0 or length > 256_000:
                self._send(400, b"Bad content length")
                return
            raw = self.rfile.read(length)
            try:
                body = raw.decode("utf-8", errors="replace")
                fields = parse_qs(body, keep_blank_values=True)
                vals = fields.get("secret") or []
                secret_val = vals[0] if vals else ""
            except Exception:
                self._send(400, b"Bad body")
                return
            ok, serr = secrets_store.set_secret(name, secret_val)
            if not ok:
                msg = html.escape(serr or "save failed")
                self._send(
                    500,
                    f"<html><body><p>Error: {msg}</p></body></html>".encode("utf-8"),
                )
                holder["error"] = serr
            else:
                self._send(
                    200,
                    b"<html><body><p>Saved. You can close this tab.</p></body></html>",
                )
            srv: HTTPServer | None = holder.get("server")
            if srv is not None:
                threading.Thread(target=srv.shutdown, daemon=True).start()
            holder["done"] = True

    def run() -> None:
        try:
            srv = HTTPServer(("127.0.0.1", port), Handler)
            holder["server"] = srv

            def shutdown_after_ttl() -> None:
                time.sleep(max(30, ttl_seconds))
                threading.Thread(target=srv.shutdown, daemon=True).start()

            threading.Thread(target=shutdown_after_ttl, daemon=True).start()
            srv.serve_forever()
        except Exception as e:
            logger.warning("secret submit HTTP server failed: %s", e)
        finally:
            global _active_count
            with _server_lock:
                _active_count = max(0, _active_count - 1)

    t = threading.Thread(target=run, name="secret-submit-http", daemon=True)
    t.start()
    # brief yield so bind succeeds
    time.sleep(0.05)

    return {
        "ok": True,
        "name": name,
        "submit_url": f"http://127.0.0.1:{port}/submit/{token}",
        "expires_in_seconds": ttl_seconds,
        "hint": "Open submit_url on this PC only, enter the secret once, then use list_secrets to confirm. Server stops after submit or when process exits.",
    }
