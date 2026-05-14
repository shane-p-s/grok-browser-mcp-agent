"""Quick OAuth + MCP smoke (run from repo root: python scripts/test_oauth_flow.py)."""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

# Configure before importing app modules
os.environ["OAUTH_ENABLED"] = "true"
os.environ["OAUTH_CLIENT_ID"] = "cid"
os.environ["OAUTH_JWT_SECRET"] = "x" * 32
os.environ["AUTH_TOKEN"] = ""
os.environ.pop("K_SERVICE", None)
os.environ.setdefault("MCP_JSON_RESPONSE", "true")

repo = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo))

import main  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

with TestClient(main.app, base_url="https://127.0.0.1") as c:
    r = c.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200, r.text
    print("metadata ok", r.json()["token_endpoint"])

    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    q = (
        f"response_type=code&client_id=cid&redirect_uri={quote('https://api.x.ai/cb', safe='')}"
        f"&code_challenge={quote(challenge)}&code_challenge_method=S256&state=abc"
    )
    r2 = c.get(f"/oauth/authorize?{q}", follow_redirects=False)
    assert r2.status_code == 302, r2.text
    loc = r2.headers["location"]
    code = parse_qs(urlparse(loc).query)["code"][0]

    r3 = c.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://api.x.ai/cb",
            "client_id": "cid",
            "code_verifier": verifier,
        },
    )
    assert r3.status_code == 200, r3.text
    tok = r3.json()["access_token"]
    print("token ok")

    from oauth_routes import verify_mcp_access_token

    assert verify_mcp_access_token(tok), "JWT must be accepted for /mcp Bearer auth"
    print("mcp bearer jwt accepted")
print("all passed")
