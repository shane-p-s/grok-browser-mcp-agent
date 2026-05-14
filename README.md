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
| `browser_task` | **Browser Use** + **DeepSeek** (`DEEPSEEK_API_KEY`); default **headless**, per-domain headed memory, one **headed** retry on bot/login-like signals; optional **`BROWSER_USER_DATA_DIR`** for persistent cookies; returns **`run_id`** |
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

The server requires a valid **`Authorization: Bearer <token>`** header: scheme **`Bearer`** (case-insensitive), **one space**, then a **non-empty** token that matches **`AUTH_TOKEN`** (constant-time compare). Malformed headers return **401** with a JSON **`detail`** explaining the problem.

- If the Grok connector UI stores a **raw secret** and xAI sends **`Authorization: <secret>`** without the `Bearer ` prefix, requests will **401**. Fix by storing the full value **`Bearer <secret>`** in the connector’s authorization field **or** prefixing your secret accordingly.
- If xAI already sends `Bearer <token>`, set the connector secret to **only** the token string (same as `AUTH_TOKEN` env).

### Connector handshake issues

If Grok fails against JSON-only MCP, set **`MCP_JSON_RESPONSE=false`** (SSE-capable) and restart.

## Run on Windows (recommended)

```powershell
cd C:\Code\Grok-MCP\grok-browser-mcp-agent
copy .env.example .env
# Edit .env: AUTH_TOKEN, DEEPSEEK_API_KEY, CURSOR_* as needed

pip install -r requirements.txt
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

- Default is **headless**. Per-domain memory may switch to **headed** after friction or operator preference.
- Env **`BROWSER_HEADED=true`** or per-call **`headed=true`** still apply when no domain memory overrides.
- **`BROWSER_USER_DATA_DIR`**: optional Playwright user-data dir for **persistent cookies** across `browser_task` runs (create the directory beforehand or let the server create it).
- **Headed automation needs an interactive logged-in Windows session.** Lock screen or another user’s session often breaks Playwright/Chromium UI.

### Run logs (for Grok debugging)

- **`browser_task`** and **`cursor_agent`** return a **`run_id`** (UUID).
- Call **`list_recent_runs`** then **`get_run_log(run_id)`** for redacted timelines (URLs, action class names, exit codes). Not a dump of DeepSeek internal chain-of-thought.
- Env: **`AGENT_LOG_ENABLE_DISK`**, **`AGENT_LOG_DIR`**, **`AGENT_LOG_MAX_EVENTS_PER_RUN`**, **`AGENT_LOG_RETAIN_RUNS`**, **`AGENT_LOG_MAX_RESPONSE_CHARS`** (see [`.env.example`](.env.example)).

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
4. Verify from **outside** the tailnet (e.g. cellular) that `initialize` works.

**Binding rule:** keep the app on **localhost only**; do not listen on `0.0.0.0` on untrusted networks.

## Windows Task Scheduler (stability)

- **Trigger:** At log on (the dedicated automation user), or at startup if appropriate.
- **Action:** `powershell.exe -ExecutionPolicy Bypass -File C:\Path\to\grok-browser-mcp-agent\start.ps1` with **“Start in”** set to the repo directory (or invoke `python -m uvicorn ...` after setting env vars).
- **Settings:** “Restart on failure” with a short delay.
- After updates, restart the task so **Playwright** / **browser-use** pick up changes.

## Security notes

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
