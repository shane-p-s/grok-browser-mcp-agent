# Grok Remote MCP Agent (PC + Tailscale Funnel)

**MCP Streamable HTTP** server for [Grok remote MCP / custom connectors](https://docs.x.ai/developers/tools/remote-mcp). Primary deployment: **Windows PC**, uvicorn bound to **`127.0.0.1`**, reachable from the internet via **[Tailscale Funnel](https://tailscale.com/docs/features/tailscale-funnel)** (TLS at the edge, no port-forward on your router).

Use the connector URL **`https://<your-funnel-host>/mcp/`** with a **trailing slash** so some clients do not strip `Authorization` on a `307` redirect.

**Reference docs:** **[`ARCHITECTURE.md`](ARCHITECTURE.md)** (browser hub, screenshot route), **[`GROK_CONNECTOR_CHECKLIST.md`](GROK_CONNECTOR_CHECKLIST.md)** (Grok: `browser_task` / tabs / screenshots parameters).

## Reliability (PC + Funnel)

Remote **`GET /health`** only proves **something** answered on the funnel URL; **`GET /health/live`** checks that the **asyncio event loop** has ticked within the last **`HEALTH_LIVE_MAX_STALE_SECONDS`** (default **15**) — useful to detect a **wedged** process (blocked thread) vs a **network / sleep / Tailscale** issue.

**Common reasons cell data cannot reach `/health`:**

- **PC asleep or lid closed** — Windows suspends; Tailscale + Funnel + uvicorn stop serving. Use **never sleep on AC** (or keep the machine awake) when this PC is your MCP relay.
- **Tailscale disconnected** — funnel endpoints go away or flap. Check Tailscale on the PC is **Connected**.
- **Uvicorn exited or wedged** — run **`restart-mcp.bat`** (stops then starts; pin it to the taskbar) or **`.\stop.ps1`** then **`.\start.ps1`**. **`stop.ps1`** kills whatever is **LISTEN**ing on **`PORT`** (default **8765**) when **Ctrl+C** does not work.

**One-click restart (pin to taskbar):** in the repo folder, use **`restart-mcp.bat`**. For a **system tray** launcher with **no manual pip step**, use **`mcp-tray.bat`** (or pin **`Grok-PC-MCP.exe`** — [build it once](#grok-pc-mcpexe-taskbar-pin)). **`mcp-tray.bat`** frees **PORT** first, then starts the tray; the tray clears **PORT** again before each uvicorn start. A **second launch** exits quietly if the tray is already running. Right‑click **`restart-mcp.bat`** → **Show more options** → **Pin to taskbar** (Windows 10/11), or create a shortcut to **`restart-mcp.bat`** and pin the shortcut. No need to `cd` manually; scripts use their own folder.
- **`browser_task` load** — long runs + Playwright/Chrome can stress RAM; orphan Chromium after a force-kill may linger. Close extra Chrome windows; lower **`BROWSER_TASK_MAX_CONCURRENT`** if needed.

**`Ctrl+C` on Windows** sometimes does not stop uvicorn (focus, console host, or blocked process). Prefer **`restart-mcp.bat`**, **`.\stop.ps1`**, or closing the window after **`stop.ps1`**.

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
| `browser_task` | **Browser Use** + **DeepSeek** (`DEEPSEEK_API_KEY`); default **headless**, per-domain memory; optional **headed** retry; **`secret_prefill`** / **`BROWSER_USER_DATA_DIR`** as before; **shared Chrome** — new tab or **`continue_tab_id`**, optional **`tab_label`**; **`return_screenshot=true`** + **`PUBLIC_MCP_BASE_URL`** → **`screenshot_url`**; see sections below |
| `list_browser_tabs` | Open automation tabs: **`tab_id`**, **`label`**, **`status`**, **`url`**, **`title`** |
| `close_browser_tab` | Close a tab by **`tab_id`** |
| `reset_browser_hub` | Clear stale shared-Chromium state if you **closed the browser window** or attach fails; next **`browser_task`** launches fresh Chromium (**old `tab_id` values invalid**) |
| `browser_capture_tab_screenshot` | **CDP-only** viewport PNG in seconds — pass **`tab_id`** (or omit when only one tab is unambiguous); returns **`screenshot_url`** like **`browser_task`** (no LLM; use when Grok times out on long **`browser_task`**) |
| `cursor_agent` | [Cursor Agent CLI](https://cursor.com/docs/cli/headless): **`capability_level`** 1=`ask`, 2=`plan` (default), 3=`agent`+`--force` only after **`approve_cursor_writes`** for that workspace; returns **`run_id`** |
| `approve_cursor_writes` | Persist Level 3 (apply) for one workspace; set **`always_allow_level_3_rule=true`** for a durable “always allow” rule until **`revoke_cursor_writes`** |
| `revoke_cursor_writes` | Remove Level 3 permission **and** any always-allow rule for a workspace |
| `get_run_log` | Redacted, bounded event log for a **`run_id`** (debugging; no model chain-of-thought) |
| `list_recent_runs` | Newest **`run_id`** entries from instrumented tools |

### Operator memory

- **`AGENT_MEMORY_PATH`**: JSON file (default under `%LOCALAPPDATA%\grok-mcp-agent\memory.json`) storing Cursor write approvals, optional **`always_allow_level_3`** rules, per-domain headed/headless-ok prefs, and bounded recovery hints.
- **`MCP_DISABLED_TOOLS`**: Comma-separated tool names rejected at call time (e.g. `browser_task,cursor_agent`). **`get_status`** is always allowed.

### `browser_task` tabs (shared Chrome)

Each **`browser_task`** uses one shared Chrome instance (`keep_alive`). By default it opens a **new tab**; pass **`continue_tab_id`** (from **`list_browser_tabs`** or **`get_status`** → `browser_tabs`) to **resume an idle tab** instead of duplicating work. When a task ends, the tab **stays open** (`status: idle`) with its **`tab_label`** so Grok can see what each tab was for. **`get_status`** and **`list_browser_tabs`** expose open tabs; Grok should check those before starting a similar task again. Use **`close_browser_tab(tab_id)`** when finished. If you **closed Chromium manually** and tools fail, the server **auto-detects a dead CDP port** on the next call; you can also call **`reset_browser_hub`** then **`browser_task`** again. Up to **`BROWSER_TASK_MAX_CONCURRENT`** (default **3**) agents may run at once on different tabs.

### `browser_task` screenshots (`return_screenshot`)

You must pass **`return_screenshot=true`** in the tool call for **`screenshot_url`** to appear at all. **`PUBLIC_MCP_BASE_URL`** must match your Funnel origin. The server never embeds PNG bytes as base64 in MCP tool JSON. When the agent finishes with `done` and a bogus **`files_to_display`** (no real file), the server still attempts a **final CDP viewport** capture so Grok can get a PNG URL without relying on browser-use step screenshots.

When **`PUBLIC_MCP_BASE_URL`** is set to the same **`https://…`** origin Grok already uses for MCP (your Funnel URL, no path), successful captures return **`screenshot_url`** pointing at **`GET /browser-screenshot/{token}`** — a **one-time** PNG response (`Cache-Control: no-store`). The server writes the PNG to disk and returns only the URL in MCP JSON (no inline image). **Downscaling is rare:** full-resolution CDP PNGs are written to disk as-is when they fit **`BROWSER_SCREENSHOT_MAX_BYTES`** (default **12MB**). PIL resize runs only when the file is **too large to register**, or if you set **`BROWSER_SCREENSHOT_DOWNSCALE_IF_RAW_BYTES_LARGER_THAN`** > 0 for an early cap (default **0** = off). Some **browser-use** step paths still supply base64 internally; the server decodes once then uses the same register step — Grok still only sees **`screenshot_url`**. Tokens expire after **`BROWSER_SCREENSHOT_URL_TTL_SECONDS`** (default 600). By default anyone who obtains the URL can download the image until it is consumed or expires; treat links as sensitive. Set **`BROWSER_SCREENSHOT_REQUIRE_BEARER=true`** so that GET also requires **`Authorization: Bearer …`** with the same token rules as **`/mcp/`** (opaque URL alone is then insufficient). **`get_status`** reports **`public_mcp_base_url_configured`**. Without **`PUBLIC_MCP_BASE_URL`**, the tool returns a **`screenshot_note`** explaining that **`screenshot_url`** cannot be built.

### Fast screenshot only (`browser_capture_tab_screenshot`)

If Grok’s MCP client **drops long `browser_task` calls** but **`get_status` still works**, call **`browser_capture_tab_screenshot`** with **`tab_id`** from **`list_browser_tabs`** or a prior **`browser_task`** result, or omit **`tab_id`** when exactly one open tab (or exactly one idle tab) makes the target unambiguous. It runs **only CDP `take_screenshot`** (no DeepSeek) and returns **`screenshot_url`** the same way. The tab must still be open and tracked (hub active).

### Grok `allowed_tools`

**Empty list:** Many connectors treat an **empty** “allowed tools” field as **allow all** registered tools — if **`ping`** and **`get_status`** already work, you often **do not** need to list anything.

**If your client only calls a subset or flakes on some tools:** paste the full comma-separated list (same order as `get_status` → **`tools`**, or copy **`grok_allowed_tools_csv`** from a `get_status` response after MCP restart):

```text
ping,fetch_url,github_get_file,github_list_repo_files,github_get_diff,github_create_issue,request_user_secret,list_secrets,revoke_secret,browser_task,cursor_agent,approve_cursor_writes,revoke_cursor_writes,get_run_log,list_recent_runs,get_status,list_browser_tabs,close_browser_tab,reset_browser_hub,browser_capture_tab_screenshot
```

Start with `ping`, `get_status`, then expand as needed. For **`cursor_agent`**, default **`capability_level=2`** (plan / propose). **Level 3** requires **`approve_cursor_writes`** (or **`always_allow_level_3_rule`**) for that workspace on the PC.

### Grok: login page screenshot + operator secrets (read this)

1. Call **`get_status`** first and read **`grok_connector_hints`** — it tells you if **`SECRETS_MASTER_KEY`**, **`PUBLIC_MCP_BASE_URL`**, or screenshot Bearer lockdown are relevant.
2. **Screenshot of the current tab:** `browser_task(..., return_screenshot=true)` **or** `browser_capture_tab_screenshot` (with **`tab_id`** from **`list_browser_tabs`**, or omit **`tab_id`** when unambiguous) after **`list_browser_tabs`**. Without **`PUBLIC_MCP_BASE_URL`**, you will not get **`screenshot_url`**. The connector (or a follow-up **`fetch_url`**) must **HTTPS GET** that URL so Grok can attach or analyze the PNG; when **`BROWSER_SCREENSHOT_REQUIRE_BEARER=true`**, include the same **`Authorization: Bearer …`** as for MCP.
3. **Operator-entered password (never in `task` text):** with **`SECRETS_MASTER_KEY`** set, restart MCP, call **`request_user_secret`**, operator opens **`submit_url`** on the PC, then use **`secret_prefill`** in **`browser_task`** with stored names. If your connector uses an explicit **`allowed_tools`** list, include **`request_user_secret`** there.
4. If the connector reports vague “initialization / transport” errors for some tools but **`ping`** works, that is often **xAI’s MCP client** or **missing `allowed_tools`** — not your PC; still verify **`get_status`** and server logs.
5. **Transport size:** keeping screenshots at **`screenshot_url`** avoids multi‑megabyte **`tools/call`** payloads that some MCP clients reject.

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

### Grok / rmcp: “Send message error” or “error sending request for url …/mcp/”

That message comes from **Grok’s MCP client** (e.g. rmcp) while it **sends** the HTTP request or holds the connection — it is **not** printed by this server’s Python stack. Wrapping **`browser_task`** in retry logic **inside this repo cannot fix it**: `mcp_tools.py` runs on your PC **only after** the connector successfully delivers `tools/call`. Retries belong in the **client** (xAI) or by **you** re-invoking the tool.

What still helps in practice:

1. Toggle **`MCP_JSON_RESPONSE=false`** and restart (see above).
2. Keep the **`task`** argument short; very long tasks are rejected with **`task_too_long_for_mcp_client`** (see **`BROWSER_TASK_MAX_INCOMING_TASK_CHARS`** in [`.env.example`](.env.example)).
3. If **`browser_task`** fails in chat but the browser on the PC is fine, call **`list_browser_tabs`** or **`get_status`**, then **`browser_capture_tab_screenshot`**. You may omit **`tab_id`** only when a single tab is unambiguous (one open tab, or exactly one idle tab); otherwise pass **`tab_id`** explicitly.

Very large **`final_result`** values in the success payload are **truncated** for MCP JSON stability ( **`BROWSER_TASK_MAX_FINAL_RESULT_CHARS`** ); use **`get_run_log(run_id)`** for a bounded, redacted timeline.

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

### System tray (Windows — no PowerShell window)

**Easiest:** double‑click **`mcp-tray.bat`**. It runs **`stop.ps1`** (frees **PORT**) and then starts the supervisor — **no separate `pip install` step**: [`mcp_tray.py`](mcp_tray.py) creates **`.venv`** if needed and **`pip install -r requirements.txt`**. The tray icon may take **longer on the very first run** while it optionally builds **`Grok-PC-MCP.exe`** for taskbar pinning (see below); progress is logged to **`logs/tray-exe-build.log`**.

If **`Grok-PC-MCP.exe`** exists in the repo folder, the batch file launches that **instead** of `pythonw` — same folder must contain **`main.py`**, **`stop.ps1`**, and **`.env`**.

The tray uses **`pythonw.exe`** from **`.venv`** when present so neither uvicorn nor the helper keeps a console. **Right‑click** the notification‑area icon for **Restart MCP**, **Stop MCP**, **Open repo folder**, **Open server log** (`logs/mcp-server.log`), and **Exit**.

If the icon never appears, run **`python mcp_tray.py`** once in PowerShell to see the error.

#### Grok-PC-MCP.exe (taskbar pin)

Windows often **won’t pin `.bat`** files to the taskbar. **`Grok-PC-MCP.exe`** is built **automatically the first time** you start the tray (after `pip install` finishes): PyInstaller runs in the background and drops the **`.exe`** beside **`main.py`**. That step can take **several minutes** and the tray icon appears afterward; if something goes wrong, open **`logs/tray-exe-build.log`**. To **skip** auto-build (e.g. dev machines), set **`GROK_TRAY_NO_AUTO_EXE=1`** in **`.env`** or in Windows environment variables.

**Optional:** run **`.\scripts\build_grok_pc_mcp_exe.ps1`** yourself if you prefer a manual rebuild or a visible PowerShell log. If an older **`.exe`** shows a **pystray / bundled module** error, **delete `Grok-PC-MCP.exe`** and start **`mcp-tray.bat`** again so it rebuilds with the current PyInstaller flags.

That **`.exe`** must stay next to **`main.py`** and **`mcp_tray.py`**. **Pin `Grok-PC-MCP.exe`** (right‑click → Pin to taskbar) or use **`mcp-tray.bat`**, which prefers the **`.exe`** when present. Double‑clicking the **`.exe`** still creates/updates **`.venv`** and installs from **`requirements.txt`** before starting uvicorn.

If the icon is hidden, open the **^** overflow next to the clock and drag the icon to the visible row. For **Task Scheduler**, point the action at **`mcp-tray.bat`** or **`Grok-PC-MCP.exe`** with **Start in** = repo folder.

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
- **`browser_task(..., return_screenshot=true)`** with **`PUBLIC_MCP_BASE_URL`**: returns **`screenshot_url`** (HTTPS one-time GET for full PNG). Without public base, **`screenshot_note`** explains omission; there is no inline-base64 fallback.
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
- **Action:** `C:\Path\to\grok-browser-mcp-agent\mcp-tray.bat` or **`Grok-PC-MCP.exe`** with **“Start in”** set to the repo directory for a **tray-only** run, or `powershell.exe -ExecutionPolicy Bypass -File C:\Path\to\grok-browser-mcp-agent\start.ps1` if you want a visible console (or invoke `python -m uvicorn ...` after setting env vars).
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
