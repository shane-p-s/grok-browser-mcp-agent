# Grok Remote MCP Agent v1 (Final)

**Production-ready shared MCP server for Grok + Cursor**

This agent gives Grok real "hands" for everything I can't do natively:
- Full browser automation (Browser Use)
- Login & secrets handling (Google Secret Manager)
- Smart confirmations with memory of your preferences
- Code interpreter
- Persistent browser sessions
- Vision + screenshot analysis
- Long-term memory + reflection (learns over time)
- Smart DeepSeek model routing (fast vs reasoner)

## Quick Deploy (Google Cloud Run - Recommended)

```bash
gcloud run deploy grok-mcp-agent \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --set-secrets DEEPSEEK_API_KEY=deepseek-key:latest,BROWSERBASE_API_KEY=... # (if using Browserbase later)
```

## Add as Custom MCP Connector in Grok
1. Deploy above
2. Go to grok.com/connectors
3. New Connector → Custom
4. Server URL: `https://your-url/mcp`
5. Add Authorization header with a secret token (for security)

## Security
- All secrets in Google Secret Manager
- One-time secure links for new credentials
- Smart confirmation system with memory
- Only Grok (via connector) and you can access it

## What This Agent Can Do
- Book tee times, manage logins, fill forms
- Control Cursor for development tasks
- Run code safely
- Remember what worked on specific sites
- Automatically use the right DeepSeek model

Built for long-term capability expansion. Ready when you are.