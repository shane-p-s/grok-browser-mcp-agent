"""Merge .env with .env.example: same layout as example, keep existing values, add missing keys.

Run: python scripts/merge_env.py
Does not print secret values.
"""

from __future__ import annotations

import re
import secrets
from pathlib import Path


def parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, rest = line.partition("=")
        out[k.strip()] = rest.strip().strip('"')
    return out


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    example_path = root / ".env.example"
    env_path = root / ".env"
    old = parse_env_file(env_path)
    lines_out: list[str] = []

    for raw in example_path.read_text(encoding="utf-8").splitlines():
        stripped = raw.lstrip()
        # Uncommented OAuth template lines in example (if any) — treat as normal below

        # Comment lines like # OAUTH_*= — emit active assignment merged with old .env
        if stripped.startswith("#") and re.match(r"#\s*OAUTH_[A-Z0-9_]+\s*=", stripped):
            inner = stripped[1:].lstrip()
            k, _, default = inner.partition("=")
            k = k.strip()
            default = default.strip().strip('"')
            if k in old:
                val = old[k]
            elif k == "OAUTH_JWT_SECRET":
                if not default or "generate" in default.lower() or len(default) < 32:
                    val = secrets.token_urlsafe(64)
                else:
                    val = default
            elif k == "OAUTH_ENABLED":
                val = old.get(k, "true")
            elif k == "OAUTH_CLIENT_ID":
                val = old.get(k, "grok-pc-mcp")
            elif k == "OAUTH_CLIENT_SECRET":
                val = old.get(k, default)
            elif k == "OAUTH_REDIRECT_URI_HOST_SUFFIX":
                val = old.get(k, default or ".x.ai,.grok.com")
            elif k == "OAUTH_ACCESS_TOKEN_TTL_SECONDS":
                val = old.get(k, default or "7776000")
            else:
                val = old.get(k, default)
            lines_out.append(f"{k}={val}")
            continue

        if "=" in raw and not stripped.startswith("#"):
            k, _, _rest = raw.partition("=")
            k = k.strip()
            if k in old:
                lines_out.append(f"{k}={old[k]}")
            elif k == "AUTH_TOKEN":
                cur = old.get("AUTH_TOKEN", "")
                if not cur or "generate" in _rest.lower():
                    lines_out.append(f"AUTH_TOKEN={secrets.token_urlsafe(48)}")
                else:
                    lines_out.append(raw)
            else:
                lines_out.append(raw)
            continue

        lines_out.append(raw)

    env_path.write_text("\n".join(lines_out) + "\n", encoding="utf-8")
    print(f"Wrote {env_path} ({len(lines_out)} lines). Existing keys preserved; OAuth block filled if missing.")


if __name__ == "__main__":
    main()
