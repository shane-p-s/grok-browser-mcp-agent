"""Optional MCP_DISABLED_TOOLS env: reject named tools at call time."""

from __future__ import annotations

import os
from typing import Any


def tool_disabled_error(tool: str) -> dict[str, Any] | None:
    """Return error dict if tool is disabled. get_status is never disabled."""
    if tool.lower() == "get_status":
        return None
    raw = os.getenv("MCP_DISABLED_TOOLS", "")
    disabled = {x.strip().lower() for x in raw.split(",") if x.strip()}
    if tool.lower() in disabled:
        return {
            "error": "tool_disabled",
            "tool": tool,
            "hint": "Remove this tool name from MCP_DISABLED_TOOLS on the server to enable it.",
        }
    return None
