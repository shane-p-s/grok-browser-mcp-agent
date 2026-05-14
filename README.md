# Grok Remote MCP Agent (PC + Tailscale Funnel)

**MCP Streamable HTTP** server for [Grok remote MCP / custom connectors](https://docs.x.ai/developers/tools/remote-mcp). Primary deployment: **Windows PC**, uvicorn bound to **`127.0.0.1`**, reachable from the internet via **[Tailscale Funnel](https://tailscale.com/docs/features/tailscale-funnel)** (TLS at the edge, no port-forward on your router).

Use the connector URL **`https://<your-funnel-host>/mcp/`** with a **trailing slash** so some clients do not strip `Authorization` on a `307` redirect.

## Transport (MCP Python SDK)

This repo uses **`mcp.server.fastmcp.FastMCP`** from the **official [`mcp`](https://github.com/modelcontextprotocol/python-sdk) PyPI package** with **`streamable_http_app()`** (Streamable HTTP / stateless HTTP). Internally the same package wires **`StreamableHTTPSessionManager`** to the low-level **`mcp.server.lowlevel.server.Server`** (`MCPServer`). FastMCP is the **maintained facade** for tool registration and schemas; the wire protocol is the same as calling `streamable_http_app()` through that stack. A future refactor could drop the FastMCP import only if you replace tool registration with explicit `@server.list_tools` / `@server.call_tool` handlers (large change).

## Tools

| Tool | Description |
|------|-------------|
| `ping` | Connectivity check |
| `get_status` | Redacted config snapshot (tokens as booleans only), memory counts, tool list; never blocked by `MCP_DISABLED_TOOLS` |
| `fetch_url` | HTTPS GET with SSRF guards and size limits |
| `github_get_file` | Read file at optional **`ref`** (branch, tag, or commit SHA); includes decoded **`content_text`** for normal files (`GITHUB_TOKEN`) |
| `github_list_repo_files` | List paths at a required **`ref`**; optional **`recursive`** tree (capped) |
| `github_get_diff` | **`base`…`head`** compare with capped patches (`GITHUB_TOKEN`) |
| `github_create_issue` | Open an issue (`GITHUB_TOKEN`) |
| `request_user_secret` | One-time **`submit_url`** on **`127.0.0.1`** only; operator pastes value in browser; stored encrypted (`SECRETS_MASTER_KEY`) |
| `list_secrets` | Stored secret **names** (+ optional `created_at`); never values |
| `revoke_secret` | Delete a stored secret by name (idempotent) |
| `browser_task` | **Browser Use** + **DeepSeek** (`DEEPSEEK_API_KEY`); default **headless**, per-domain headed memory, one **headed** retry on bot/login-like signals; optional **`BROWSER_USER_DATA_DIR`** for persistent cookies; optional **`secret_prefill`** (https URLs + selectors + `secret_name`) fills locally before the agent so values are not sent to the LLM; returns **`run_id`** |
| `cursor_agent` | [Cursor Agent CLI](https://cursor.com/docs/cli/headless): **`capability_level`** 1=`ask`, 2=`plan` (default), 3=`agent`+`--force` only after **`approve_cursor_writes`** for that workspace; returns **`run_id`** |
| `approve_cursor_writes` | Persist Level 3 (apply) for one workspace; set **`always_allow_level_3_rule=true`** for a durable “always allow” rule until **`revoke_cursor_writes`** |
| `revoke_cursor_writes` | Remove Level 3 permission **and** any always-allow rule for a workspace |
| `get_run_log` | Redacted, bounded event log for a **`run_id`** (debugging; no model chain-of-thought) |
| `list_recent_runs` | Newest **`run_id`** entries from instrumented tools |

### Operator memory

- **`AGENT_MEMORY_PATH`**: JSON file (default under `%LOCALAPPDATA%\grok-mcp-agent\memory.json`) storing Cursor write approvals, optional **`always_allow_level_3`** rules, per-domain headed/headless-ok prefs, and bounded recovery hints.
- **`MCP_DISABLED_TOOLS`**: Comma-separated tool names rejected at call time (e.g. `browser_task,cursor_agent`). **`get_status`** is always allowed.

### Grok `allowed_tools`

Start with `ping`, `get_status`, then `fetch_url`, then expand. For **`cursor_agent`**, default **`capability_level=2`** (plan / propose). **Level 3** requires **`approve_cursor_writes`** (or **`always_allow_level_3_rule`**) for that workspace on the PC.

### Grok connector: `Authorization` header

The server requires a valid **`Authorization: Bearer <token>`** header: scheme **`Bearer`** (case-insensitive), **one space**, then a **non-empty** token. The token may be the legacy static **`AUTH_TOKEN`** or an **OAuth access JWT** from this server’s **`/oauth/token`** when **`OAUTH_ENABLED=true`** (see below). Malformed headers return **401** with a JSON **`detail`** explaining the problem.

- If the Grok connector UI stores a **raw secret** and xAI sends **`Authorization: <secret>`** without the `Bearer ` prefix, requests will **401**. Fix by storing the full value **`Bearer <secret>`** in the connector’s authorization field **or** prefixing your secret accordingly.
- If xAI already sends `Bearer <token>`, set the connector secret to **only** the token string (same as `AUTH_TOKEN` env).

### Grok connector: OAuth (optional, for “OAuth Credentials” UI)

When **`OAUTH_ENABLED=true`**, this app exposes a **small OAuth 2.0 surface** on the **same origin** as MCP (so your Funnel URL serves both):

| Grok / xAI field | Value (replace host with your public base, no path) |
|------------------|--------------------------------------------------------|
| **Client ID** | Same as **`OAUTH_CLIENT_ID`** in `.env` |
| **Client Secret** | Same as **`OAUTH_CLIENT_SECRET`** if you set one; leave empty in Grok if you use **PKCE-only** and did not set a server secret |
| **Authorization Endpoint** | `https://<your-host>/oauth/authorize` |
| **Token Endpoint** | `https://<your-host>/oauth/token` |
| **Scopes** | Leave empty or use a placeholder scope name (this server does not require scopes) |
| **Token Auth Method** | **PKCE only** works with **`authorization_code`**; if you set **`OAUTH_CLIENT_SECRET`**, Grok may send it on the token request and the server will validate it |

Discovery (optional for clients): **`GET https://<your-host>/.well-known/oauth-authorization-server`**

**`/mcp/`** accepts **`Authorization: Bearer …`** where the token is either the legacy **`AUTH_TOKEN`** or an **access JWT** returned by **`/oauth/token`**. You can keep **`AUTH_TOKEN`** set for local smoke tests (`scripts/smoke_mcp.py`) while Grok uses OAuth tokens.

**Redirect URI allowlist:** Authorization codes only redirect to **`https`** URLs whose host matches **`OAUTH_REDIRECT_URI_HOST_SUFFIX`** (default **`.x.ai`**). If xAI uses another host, add it (comma-separated suffixes, or `*` to allow any `https` host — weaker).

**Troubleshooting “Not Found” on the authorize page**

1. **Restart the MCP server** after changing `.env` (old process has no `/oauth/authorize` route).
2. Confirm **`OAUTH_ENABLED=true`** and **`OAUTH_CLIENT_ID`** / **`OAUTH_JWT_SECRET`** are set; if OAuth is off, you should now see **503** and a short HTML message instead of a bare 404.
3. Open **`https://<your-host>/.well-known/oauth-authorization-server`** in a browser — you should get JSON (not HTML 404 from another layer).
4. Use the **Funnel public URL** (same host as `/mcp/`), not a **tailnet-only** Serve URL on another port.
5. **Client ID** in Grok must **exactly match** **`OAUTH_CLIENT_ID`** (no extra spaces).

### Connector handshake issues

If Grok fails against JSON-only MCP, set **`MCP_JSON_RESPONSE=false`** (SSE-capable) and restart.

**HTTP 421 on `POST /mcp/` with log line `Invalid Host header: <your>.ts.net`:** The official MCP Streamable HTTP stack validates the **`Host`** header when the server is bound to localhost. Tailscale Funnel forwards with your public **`*.ts.net`** host. Set **`MCP_EXTRA_ALLOWED_HOSTS`** to that hostname (comma-separated if several), for example **`MCP_EXTRA_ALLOWED_HOSTS=your-machine.tail1234.ts.net`**, then restart. Alternatively **`MCP_DNS_REBINDING_PROTECTION=false`** disables the check entirely (weaker on untrusted networks). See [`.env.example`](.env.example).

## Run on Windows (recommended)

```powershell
cd C:\Code\Grok-MCP\grok-browser-mcp-agent
copy .env.example .env
# Edit .env: AUTH_TOKEN, DEEPSEEK_API_KEY, CURSOR_* as needed

pip install -r requirements.txt
python -m playwright install chromium
python -m uvicorn main:app --host 127.0.0.1 --port 8765
```

Or use **[`start.ps1`](start.ps1)** in the repo root (loads `.env` key=value lines, then starts uvicorn):

```powershell
.\start.ps1
```

Defaults: **`HOST=127.0.0.1`** in [`main.py`](main.py) `__main__` when using env; **`start.ps1`** defaults port **8765** if `PORT` is unset.

### Cursor CLI

1. Install the CLI: [CLI installation](https://cursor.com/docs/cli/installation) (Windows: `irm 'https://cursor.com/install?win32=true' | iex`).
2. Set **`CURSOR_API_KEY`** for headless use: [Authentication](https://cursor.com/docs/cli/reference/authentication).
3. Set **`CURSOR_WORKSPACE_ROOTS`** to a **semicolon-separated** list of **absolute** directories Grok may modify (e.g. `C:\Code\repo1;C:\Code\repo2`).
4. Optional: **`CURSOR_AGENT_PATH`** if `agent` is not on `PATH`.

### Browser headed mode

- **`browser_task`** uses browser-use’s **`ChatDeepSeek`** for DeepSeek (not `ChatOpenAI` against `api.deepseek.com`). That avoids DeepSeek **400** errors like **`This response_format type is unavailable now`**, which occur when an OpenAI-style **`response_format` / JSON schema** request is sent to DeepSeek. Optional override: **`DEEPSEEK_BASE_URL`** (default **`https://api.deepseek.com/v1`**). Browser-use may still log that **DeepSeek does not support `use_vision=true`** and fall back to non-vision for those models.
- Default is **headless**. Per-domain memory may switch to **headed** after friction or operator preference.
- Env **`BROWSER_HEADED=true`** or per-call **`headed=true`** still apply when no domain memory overrides.
- **`BROWSER_USER_DATA_DIR`**: optional Playwright user-data dir for **persistent cookies** across `browser_task` runs (create the directory beforehand or let the server create it).
- **`browser_task(..., return_screenshot=true)`**: last step’s viewport PNG is returned as **`screenshot_base64`** (capped by **`BROWSER_TASK_SCREENSHOT_MAX_BASE64_CHARS`**, default 700k chars) for multimodal clients (e.g. Grok) to inspect; **`screenshot_note`** explains omission.
- **Headed automation needs an interactive logged-in Windows session.** Lock screen or another user’s session often breaks Playwright/Chromium UI.

### Run logs (for Grok debugging)

- **`browser_task`** and **`cursor_agent`** return a **`run_id`** (UUID).
- Call **`list_recent_runs`** then **`get_run_log(run_id)`** for redacted timelines (URLs, action class names, exit codes). Not a dump of DeepSeek internal chain-of-thought.
- Env: **`AGENT_LOG_ENABLE_DISK`**, **`AGENT_LOG_DIR`** (disk JSONL: **`agent_events.ndjson`** when enabled), **`AGENT_LOG_MAX_EVENTS_PER_RUN`**, **`AGENT_LOG_RETAIN_RUNS`**, **`AGENT_LOG_MAX_RESPONSE_CHARS`** (see [`.env.example`](.env.example)).

### Smoke test

```powershell
$env:AUTH_TOKEN="your-token"
$env:SMOKE_MCP_URL="http://127.0.0.1:8765/mcp/"
python scripts/smoke_mcp.py
```

## Tailscale Funnel (public HTTPS to localhost)

Official docs: [Tailscale Funnel](https://tailscale.com/docs/features/tailscale-funnel), CLI reference: [`tailscale funnel`](https://tailscale.com/docs/reference/tailscale-cli/funnel).

**Prerequisites (Tailscale admin / device):** Funnel enabled for your tailnet where required, **MagicDNS**, **HTTPS certificates** for your tailnet name, and a **`funnel` [node attribute](https://tailscale.com/docs/reference/syntax/policy-file#node-attributes)** allowing your node to use Funnel. Funnel is **beta** and available on **[all plans](https://tailscale.com/pricing)** including Personal.

### Example Funnel command (verify before use)

Tailscale’s CLI and **allowed HTTPS ports** change over time. **Always confirm** against the current [funnel CLI reference](https://tailscale.com/docs/reference/tailscale-cli/funnel) on your machine (`tailscale funnel --help`).

Illustration only (HTTPS on **443** forwarding to local **8765**):

```bash
tailscale funnel --bg --https=443 http://127.0.0.1:8765
```

Then set Grok’s connector URL to `https://<your-funnel-host>/mcp/` (with trailing slash).

**Typical flow:**

1. Run this MCP server on **`127.0.0.1:PORT`** (e.g. `8765`), e.g. **`.\start.ps1`** or uvicorn as above.
2. Run Funnel so public **HTTPS** terminates at Tailscale and forwards to **`http://127.0.0.1:PORT`**.
3. In Grok connectors, set **Server URL** to `https://<funnel-host>/mcp/` and configure **Authorization** per the [Authorization section](#grok-connector-authorization-header) above.
4. Set **`MCP_EXTRA_ALLOWED_HOSTS=<funnel-host>`** in `.env` (same hostname Grok uses, no `https://`) so MCP requests are not rejected with **421** / `Invalid Host header` — see [Connector handshake issues](#connector-handshake-issues).
5. Verify from **outside** the tailnet (e.g. cellular) that `initialize` works.

**Binding rule:** keep the app on **localhost only**; do not listen on `0.0.0.0` on untrusted networks.

## Windows Task Scheduler (stability)

- **Trigger:** At log on (the dedicated automation user), or at startup if appropriate.
- **Action:** `powershell.exe -ExecutionPolicy Bypass -File C:\Path\to\grok-browser-mcp-agent\start.ps1` with **“Start in”** set to the repo directory (or invoke `python -m uvicorn ...` after setting env vars).
- **Settings:** “Restart on failure” with a short delay.
- After updates, restart the task so **Playwright** / **browser-use** pick up changes.

## Security notes

- **`browser_task`** sends the **`task`** string to **DeepSeek**. Do **not** embed passwords or other secrets in `task`. Use **`request_user_secret`** / **`list_secrets`** and reference names only, or structured **`secret_prefill`** (values resolved on the PC). Run logs store **`task_preview_redacted`** (e.g. `{{secret:name}}` → `[secret:name]`) plus standard env redaction.
- **`request_user_secret`** serves a short-lived form on **localhost only** (no TLS). Anyone who can open a browser on that machine while the URL is valid could submit; treat **`submit_url`** like a capability token.
- **`/mcp/`** requires **`Authorization: Bearer …`** as documented above; **`/health`** is open for probes.
- On **Cloud Run** (`K_SERVICE`) or `ENVIRONMENT=production`, **`AUTH_TOKEN`** is required at startup (legacy self-host path).
- **`fetch_url`** blocks common SSRF targets.
- **Funnel exposes a URL to the entire internet.** A strong **`AUTH_TOKEN`** and tight **`allowed_tools`** are mandatory; **`cursor_agent` Level 3** (`--force`) is gated by **`approve_cursor_writes`** on the operator PC.

## MCP Inspector

```bash
npx @modelcontextprotocol/inspector
```

Point at `http://127.0.0.1:8765/mcp/` and set the Bearer token.

## Shareable Grok verification doc

Paste **[`GROK_CONNECTOR_CHECKLIST.md`](GROK_CONNECTOR_CHECKLIST.md)** into Grok chat so it can cross-check transport, headers, and troubleshooting.

## Optional: Docker (not the primary path)

The [`Dockerfile`](Dockerfile) is **optional** (e.g. Linux dev parity). Primary install is **native Python on Windows** so **Cursor CLI** and **headed Chrome** use your session.

**Legacy:** you can still deploy behind Cloud Run or another HTTPS front; the code still recognizes `K_SERVICE`. It is **not** documented as the default workflow here.
