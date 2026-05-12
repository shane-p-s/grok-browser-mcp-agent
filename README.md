# Grok Browser MCP Agent

**Full autonomous browser control for Grok via custom MCP connector.**

Uses **Browserbase** (best-in-class managed browsers with anti-bot in 2026) + flexible LLM support (DeepSeek, Grok, OpenAI, Anthropic, etc.).

Once connected as a Custom MCP Connector in Grok, you can say things like:
- "Book me a tee time at Highland Pines for Saturday at 8am for 4 players"
- "Check my upcoming golf reservations"
- "Go to example.com and summarize the pricing page"

Grok will directly call tools on this agent with **zero manual steps** from you.

## Quick Start (10-15 minutes)

### 1. Get Browserbase API Key
1. Go to [browserbase.com](https://www.browserbase.com) and sign up (free tier available).
2. Create a new project and copy your **API Key** and **Project ID**.

### 2. Deploy to Google Cloud Run (Recommended - Cheapest)

#### Option A: One-click deploy (easiest)
Click the button below (you'll need to connect your GitHub):

[![Deploy to Cloud Run](https://deploy.cloud.run/button.svg)](https://deploy.cloud.run)

#### Option B: Manual (still easy)
```bash
gcloud run deploy grok-browser-mcp-agent \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --set-secrets BROWSERBASE_API_KEY=browserbase-api-key:latest,BROWSERBASE_PROJECT_ID=browserbase-project-id:latest,LLM_API_KEY=your-llm-key:latest
```

### 3. Add as Custom MCP Connector in Grok
1. Go to [grok.com/connectors](https://grok.com/connectors)
2. Click **New Connector** > **Custom**
3. Server URL: `https://your-cloud-run-url/mcp`
4. Label: `browser-agent`
5. (Optional) Add Authorization header if you set one

### 4. Test
Tell Grok: "Using my browser agent, check what tee times are available at Highland Pines this weekend."

Grok will now have real hands in the browser.

## Configuration (Environment Variables)

| Variable | Required | Description |
|----------|----------|-------------|
| `BROWSERBASE_API_KEY` | Yes | Your Browserbase API key |
| `BROWSERBASE_PROJECT_ID` | Yes | Your Browserbase Project ID |
| `LLM_PROVIDER` | No | `deepseek`, `openai`, `anthropic`, `grok`, `groq` (default: deepseek) |
| `LLM_API_KEY` | Yes | Your API key for the chosen provider |
| `LLM_MODEL` | No | Model name (e.g. `deepseek-chat`, `gpt-4o`) |

## Supported Tools (Grok can call these directly)

- `book_pines_golf_tee_time` - Book at Highland or Augusta Pines
- `get_upcoming_golf_reservations`
- `execute_browser_task` - General purpose ("go to X and do Y")
- `take_screenshot` - Useful for debugging

## Why This Stack?
- **Browserbase**: Best anti-bot, persistent sessions, fast, reliable in 2026
- **Pluggable LLM**: Use cheap DeepSeek or whatever you prefer
- **MCP Native**: Works perfectly as Grok Custom Connector
- **Extensible**: Easy to add more tools

## Next Steps / Customization

This is a strong starter. Want me to add more tools, Cursor integration helpers, scheduling, or improve the golf booking logic? Just tell me (or open an issue).

Built for you by Grok. Let's make it do everything you need.