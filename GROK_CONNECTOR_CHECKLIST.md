# Grok / xAI connector verification — `grok-browser-mcp-agent`

This document is written for **Grok (or any LLM)** to validate whether a **custom remote MCP connector** can talk to this server. Paste it into Grok together with the operator’s **public base URL** (after Tailscale Funnel or another HTTPS front).

---

## 1. Official remote MCP constraints (xAI)

Per xAI documentation ([Remote MCP Tools](https://docs.x.ai/developers/tools/remote-mcp)):

- Remote MCP uses an **HTTPS** `server_url`.
- Supported transports are **Streaming HTTP** and **SSE** (this server implements **Streamable HTTP** using the official **Python MCP SDK** on PyPI (`mcp`): **`FastMCP.streamable_http_app()`** delegates to **`StreamableHTTPSessionManager`** + low-level **`MCPServer`** — same transport as the SDK’s streamable HTTP stack, not a separate protocol).
- Optional connector fields include: **`authorization`** (sent as a request header to the MCP server), **`headers`**, **`allowed_tools`**, **`server_label`**, **`server_description`**.

**Implication:** Grok’s infrastructure performs MCP as a **client** to your URL; your server must speak **real MCP** (JSON-RPC over the MCP HTTP transport), not a custom REST shape.

---

## 2. Exact URLs and paths

| Purpose | URL pattern |
|--------|-------------|
| MCP endpoint (use in Grok connector) | `https://<public-host>/mcp/` |
| Health check (no auth) | `https://<public-host>/health` |
| Liveness (no auth; asyncio heartbeat) | `https://<public-host>/health/live` |
| Service root (no auth) | `https://<public-host>/` |

**Trailing slash on `/mcp/`:** Strongly recommended. Some HTTP stacks issue a **307** redirect for `/mcp` → `/mcp/` and may drop or mishandle the **`Authorization`** header on redirect. Using **`/mcp/`** avoids that class of failure.

**How routing works in this repo:**

- FastMCP is configured with **`streamable_http_path="/"`** (internal MCP app).
- That app is **mounted** at **`/mcp`** on the outer Starlette app.
- Effective MCP POST target: **`/mcp/`** (or **`/mcp`** with redirect risk as above).

---

## 3. Authentication

**Required for all MCP requests** under `/mcp/`:

```http
Authorization: Bearer <token>
```

The server parses **`Authorization`**: scheme **`Bearer`** (case-insensitive), exactly **one space** after `Bearer`, then a **non-empty** token.

**Two accepted token types:**

1. **Legacy static secret** — the token equals **`AUTH_TOKEN`** from the operator’s environment (digest-based compare).
2. **OAuth access token** — when **`OAUTH_ENABLED=true`**, the operator runs this server’s **`/oauth/authorize`** + **`/oauth/token`** flow; the **`access_token`** returned there is a signed JWT also accepted as the Bearer value on `/mcp/`. Discovery: **`GET /.well-known/oauth-authorization-server`**. See [README.md](README.md) “Grok connector: OAuth”.

At least one of **`AUTH_TOKEN`** or full OAuth config (**`OAUTH_CLIENT_ID`** + **`OAUTH_JWT_SECRET`**) must be set or `/mcp/` returns **503** (“misconfiguration”).

**Common failure modes Grok should check:**

1. **Missing `Bearer ` prefix or empty token** — **401** with a specific **`detail`** string. Fix: store **`Bearer <secret>`** in the connector authorization field when the client sends the header verbatim.
2. **Wrong token** — **401** (`detail`: invalid bearer token).
3. **Neither static nor OAuth configured on server** — **503** (“misconfiguration”).

**Unauthenticated endpoints:** `GET /health`, **`GET /health/live`**, `GET /`, **`/.well-known/oauth-authorization-server`**, **`/oauth/authorize`**, **`/oauth/token`** (POST) — used for liveness / OAuth; **`/mcp/`** requires Bearer as above.

---

## 4. HTTP headers Grok’s client should send (conceptual)

For MCP JSON-RPC over Streamable HTTP, clients typically send:

```http
POST /mcp/ HTTP/1.1
Host: <public-host>
Content-Type: application/json
Accept: application/json, text/event-stream
Authorization: Bearer <AUTH_TOKEN>
```

Body is JSON-RPC 2.0, e.g. `initialize`, `tools/list`, `tools/call`.

---

## 5. Server MCP metadata (expected)

After `initialize`, expect roughly:

- **`serverInfo.name`:** `grok-browser-mcp-agent`
- **`protocolVersion`:** negotiated; client may request `2024-11-05` (example used in local smoke tests).

---

## 6. Tool catalog (names to allow / expect)

Exact names (for `allowed_tools` in Grok):

| Tool | Role |
|------|------|
| `ping` | Connectivity |
| `get_status` | Redacted configuration + memory counts (**never** disabled by `MCP_DISABLED_TOOLS`) |
| `fetch_url` | HTTPS GET with SSRF guards |
| `github_get_file` | GitHub REST read at optional **`ref`** (branch/tag/SHA); returns **`content_text`** when decodable |
| `github_list_repo_files` | List repo paths at required **`ref`** (recursive tree capped) |
| `github_get_diff` | Compare **`base`…`head`** with capped patches |
| `github_create_issue` | GitHub REST write |
| `request_user_secret` | Localhost-only one-time form URL; operator submits secret; encrypted at rest (`SECRETS_MASTER_KEY`) |
| `list_secrets` | Names (+ optional `created_at`); never secret values |
| `revoke_secret` | Delete stored secret by name (idempotent) |
| `browser_task` | Browser Use + DeepSeek; **shared Chrome**, **new tab per call** (or **`continue_tab_id`** to resume an **idle** tab); optional **`tab_label`** for listing; **`return_screenshot=true`** + operator **`PUBLIC_MCP_BASE_URL`** → **`screenshot_url`** (one-time HTTPS PNG); optional **`secret_prefill`**; **`task`** must not contain raw secrets; returns **`run_id`**, **`browser_tab_id`**, etc. |
| `list_browser_tabs` | Open tabs from **`browser_task`**: **`tab_id`**, **`label`**, **`run_id`**, **`status`** (`running` / `idle`), **`url`**, **`title`** |
| `close_browser_tab` | Close a tab by **`tab_id`** from **`list_browser_tabs`** or a prior **`browser_task`** result |
| `reset_browser_hub` | Clear shared Chromium CDP cache + tab registry (e.g. operator closed the browser); next **`browser_task`** starts fresh (**invalidates old `tab_id`**) |
| `browser_open_tab` | Granular tab (no “Starting agent …”); optional **`url`**, **`headed`** |
| `browser_navigate` | **`tab_id` + https url** (fast) |
| `browser_get_page_state` | Bounded element list with **`index`** for click/type |
| `browser_click` | **`element_index`**, **`css_selector`**, or **`x`/`y`** |
| `browser_type` | Text or **`secret_name`** into a field |
| `browser_press_keys` | Keyboard (e.g. Enter) |
| `browser_capture_tab_screenshot` | Fast CDP PNG → **`screenshot_url`** |
| `cursor_agent` | Cursor `agent` CLI; levels **1=ask**, **2=plan (default)**, **3=agent+force** after **`approve_cursor_writes`** or durable rule; returns **`run_id`** |
| `approve_cursor_writes` | Persist Level-3 permission; optional **`always_allow_level_3_rule`** for durable rule |
| `revoke_cursor_writes` | Clear Level-3 permission **and** always-allow rule for one workspace path |
| `get_run_log` | Redacted log for **`run_id`** |
| `list_recent_runs` | Recent **`run_id`** list |

**Operator memory file:** JSON at **`AGENT_MEMORY_PATH`** (default `%LOCALAPPDATA%\grok-mcp-agent\memory.json`) stores Cursor write approvals, optional **always-allow Level 3** rules, per-domain headed / headless-ok prefs, and bounded recovery hints.

**Grok / xAI checklist:** Call **`get_status`** and read **`grok_connector_hints`**; copy **`grok_allowed_tools_csv`** into the connector if it requires an explicit allowlist. Screenshots need **`PUBLIC_MCP_BASE_URL`** + **`return_screenshot=true`** (tool JSON returns **`screenshot_url`** only — the client must fetch the PNG over HTTPS); optional **`BROWSER_SCREENSHOT_REQUIRE_BEARER=true`** ties that GET to the same Bearer as MCP; **`request_user_secret`** needs **`SECRETS_MASTER_KEY`**.

**Optional lockdown:** env **`MCP_DISABLED_TOOLS`** = comma-separated tool names to reject at **`tools/call`** time (e.g. `browser_task,cursor_agent`). **`get_status`** always runs.

### Granular browser (preferred over long `browser_task`)

1. **`browser_open_tab`** (`headed=true` for login) → **`tab_id`**
2. **`browser_navigate(tab_id, url)`** with **`return_screenshot=true`**
3. **`browser_get_page_state(tab_id)`** → use **`index`** in **`browser_click`** / **`browser_type`**
4. Credentials: **`request_user_secret`** on PC, then **`browser_type(..., secret_name="…")`** — never raw passwords in tool JSON
5. **`browser_press_keys(tab_id, keys="Enter")`** to submit
6. Vision after each step: **`return_screenshot=true`** on actions or **`browser_capture_tab_screenshot`**

Avoid starting a new **`browser_task`** for every step (transport timeouts + extra “Starting agent …” tabs). Use **`browser_task`** only for captcha / exploratory automation.

### `browser_task`: screenshots (what Grok should pass and expect)

- **Parameters**
  - **`return_screenshot`:** set to **`true`** whenever Grok needs a **PNG** of the final viewport. If omitted/false, the tool result has **no** `screenshot_url`.
  - **`tab_label`:** optional short string (e.g. `"Amazon mens joggers"`) stored for **`list_browser_tabs`** / **`get_status`** → **`browser_tabs`**.
  - **`continue_tab_id`:** optional; **`tab_id`** of an **idle** tab from **`list_browser_tabs`** or a prior result — **reuses that tab** instead of opening another (avoid duplicate navigation).
- **Operator setup:** **`PUBLIC_MCP_BASE_URL`** = `https://<same-funnel-host-as-MCP>` (no path). Then **`get_status`** includes **`public_mcp_base_url_configured`**, **`grok_connector_hints`**, and a **`browser_tabs`** summary when tabs exist.
- **Tool result (screenshot path)**  
  - **`screenshot_url`**: HTTPS URL on the same host; **GET once** → full **PNG** binary (`Cache-Control: no-store`, token consumed or TTL). Grok (or the user) must **fetch that URL** to attach or view the image — MCP tool JSON does not embed image bytes. If **`BROWSER_SCREENSHOT_REQUIRE_BEARER=true`**, the GET must include **`Authorization: Bearer …`** (same rules as **`/mcp/`**).  
  - May include **`screenshot_delivery`:** e.g. **`url_only`** when the URL was registered successfully.  
  - **`screenshot_note`** if something was omitted or clipped.
- **“Vision”:** DeepSeek inside browser-use still does **not** use vision for **`deepseek-*`** models (browser-use may log **`use_vision=False`**). Grok’s **multimodal** path is: call **`browser_task`** with **`return_screenshot=true`**, read **`screenshot_url`**, then **load the PNG** (HTTP GET) for analysis or to show the user. If **`browser_task`** responses time out on Grok’s side but the tab is still open on the PC, call **`browser_capture_tab_screenshot`** (with **`tab_id`** from **`list_browser_tabs`**, or omit **`tab_id`** when only one tab matches) (seconds, no LLM) and then **GET** the returned **`screenshot_url`**.

**Security guidance for Grok:** encourage the human to start with **`allowed_tools`** = `["ping","get_status","fetch_url"]`, then expand. **`cursor_agent`** Level **3** / `apply_changes=true` requires **`approve_cursor_writes`** or **`always_allow_level_3_rule`** for that workspace — high impact on disk when allowed.

**Secrets:** the **`browser_task`** `task` argument is sent to **DeepSeek**; never put raw credentials there. Use **`request_user_secret`** / **`secret_prefill`** / secret **names** only. Manual test: set **`SECRETS_MASTER_KEY`**, call **`request_user_secret`**, open **`submit_url`** on the PC, then **`list_secrets`** (CI typically skips the browser step).

---

## 7. Transport mode: JSON vs SSE

Operator env **`MCP_JSON_RESPONSE`:**

- **`true` (default):** JSON-oriented streamable HTTP responses (often best for simple clients).
- **`false`:** SSE-capable mode per MCP Python SDK / FastMCP.

If Grok’s connector **fails initialization** or hangs, operator should try toggling **`MCP_JSON_RESPONSE`** (xAI documents both Streaming HTTP and SSE support).

---

## 8. Connectivity matrix (what to blame when it fails)

| Symptom | Likely cause |
|---------|----------------|
| DNS does not resolve for funnel host | Funnel / DNS propagation (Tailscale docs mention delays) |
| TLS errors | HTTPS cert not provisioned for tailnet / misconfigured Funnel |
| Connection refused | Uvicorn not running, wrong local port, or Funnel not pointed at `127.0.0.1:PORT` |
| 401 on `/mcp/` | Wrong `AUTH_TOKEN`, malformed `Authorization`, or missing `Bearer ` / empty token (see JSON **`detail`**) |
| 421 on `/mcp/` / log `Invalid Host header: *.ts.net` | Set **`MCP_EXTRA_ALLOWED_HOSTS`** to your Funnel hostname (see repo README); MCP SDK DNS rebinding protection vs. public `Host` header |
| 503 on `/mcp/` | `AUTH_TOKEN` unset on server |
| 307 then 401 | Use **`/mcp/`** with trailing slash; verify `Authorization` on final request |
| MCP parse errors | Not hitting real MCP endpoint; wrong path |
| Tool timeout | Long `browser_task` / `cursor_agent`; operator should reduce scope or increase timeouts. For **screenshots only** after a tab is already open, use **`browser_capture_tab_screenshot`** (fast) instead of another full **`browser_task`**. |

---

## 9. Operator environment (PC + Funnel)

Typical production shape:

1. **Python** runs **`uvicorn main:app --host 127.0.0.1 --port 8765`** (or `.\start.ps1`).
2. **Tailscale Funnel** exposes **public HTTPS** → **`http://127.0.0.1:8765`**.
3. **PC must be on** and **Tailscale up**; headed browser additionally needs an **interactive logged-in Windows session**.

---

## 10. Ordered self-test sequence (Grok can suggest this to the human)

1. **GET** `/health` from public URL — expect JSON with `"status": "healthy"`. **GET** `/health/live` — expect **`"status":"live"`**; **`503`** with **`degraded`** suggests the Python process is wedged (event loop not running).
2. **POST** `/mcp/` `initialize` with JSON-RPC — expect **200** and a `result` object.
3. **POST** `tools/list` — expect tool names including `ping`.
4. **POST** `tools/call` `get_status` with `{}` — expect structured result with `tools` array and boolean capability flags (no raw secrets); may include **`browser_tabs`**, **`browser_hub_active`**, **`public_mcp_base_url_configured`** when browser/screenshot features are in use.
5. **POST** `tools/call` `ping` — expect textual **`pong`** in content.
6. **POST** `tools/call` `fetch_url` with `{"url":"https://example.com"}` — expect `status_code` **200** in structured result.
7. Run **`browser_task`** or **`cursor_agent`** once, read returned **`run_id`**, then **`list_recent_runs`** and **`get_run_log`** — expect redacted event arrays (no secrets).
8. For screenshots: run **`browser_task`** with **`return_screenshot=true`** (operator must set **`PUBLIC_MCP_BASE_URL`**); expect **`screenshot_url`** in the result, then **GET** that URL once for the PNG. Optional: **`list_browser_tabs`** after a run to see **`tab_id`** / **`label`** / **`idle`** tabs; **`continue_tab_id`** on a follow-up task reuses a tab.
9. If Grok loses the **`browser_task`** response but the browser tab is still open: **`list_browser_tabs`** (or **`get_status`**) → **`browser_capture_tab_screenshot`** (optional **`tab_id`** when unambiguous) → **`screenshot_url`** → **GET** the PNG (add this tool to **`allowed_tools`**).

---

## 11. What this server does **not** expose

- **No DeepSeek chain-of-thought** or hidden “thinking” channel to Grok.
- **Run logs** are **operational** (URLs, action class names, exit codes, errors), with **redaction** heuristics — not a substitute for full browser HAR or full LLM traces.

---

## 12. References

- xAI Remote MCP: https://docs.x.ai/developers/tools/remote-mcp  
- Tailscale Funnel: https://tailscale.com/docs/features/tailscale-funnel  
- Tailscale `tailscale funnel` CLI: https://tailscale.com/docs/reference/tailscale-cli/funnel  
- MCP Python SDK / transports: https://modelcontextprotocol.github.io/python-sdk/  
- Cursor headless CLI: https://cursor.com/docs/cli/headless  
- Cursor CLI parameters (`--print`, `--force`, `--trust`, `--workspace`): https://cursor.com/docs/cli/reference/parameters  
