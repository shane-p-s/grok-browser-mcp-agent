"""Minimal MCP Streamable HTTP smoke test (initialize + tools/list)."""

from __future__ import annotations

import json
import os
import sys

import httpx


def main() -> int:
    base = os.environ.get("SMOKE_MCP_URL", "http://127.0.0.1:8765/mcp/").rstrip("/") + "/"
    token = os.environ.get("AUTH_TOKEN", "").strip()
    if not token:
        print("Set AUTH_TOKEN in the environment.", file=sys.stderr)
        return 2

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }

    def rpc(method: str, params: dict | None = None, _id: int = 1) -> dict:
        payload = {"jsonrpc": "2.0", "id": _id, "method": method, "params": params or {}}
        r = httpx.post(base, headers=headers, json=payload, timeout=60.0, follow_redirects=True)
        print(method, r.status_code)
        r.raise_for_status()
        return r.json()

    init = rpc(
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "smoke_mcp", "version": "1"},
        },
    )
    print("initialize:", json.dumps(init, indent=2)[:1200])

    tools = rpc("tools/list", {}, _id=2)
    print("tools/list:", json.dumps(tools, indent=2)[:2000])

    ping = rpc("tools/call", {"name": "ping", "arguments": {}}, _id=3)
    print("tools/call ping:", json.dumps(ping, indent=2)[:800])

    recent = rpc("tools/call", {"name": "list_recent_runs", "arguments": {"limit": 5}}, _id=4)
    print("tools/call list_recent_runs:", json.dumps(recent, indent=2)[:1200])

    status = rpc("tools/call", {"name": "get_status", "arguments": {}}, _id=5)
    print("tools/call get_status:", json.dumps(status, indent=2)[:2000])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
