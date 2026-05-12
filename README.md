# Grok + Cursor Shared MCP Agent (Production Ready)

**One MCP server that both Grok and Cursor can use.**

- Browser Use (fully open source, DeepSeek-ready)
- Google Cloud Run + Google Secret Manager
- Smart human confirmation system
- Context & token optimization
- Hybrid support for your local OpenClaw

This is designed to be your long-term "remote hands + coding brain".

## Quick Deploy (Google Cloud Run)

1. Get Browserbase is no longer used. We use Browser Use (self-hosted).
2. Deploy with:
```bash
gcloud run deploy grok-mcp-agent --source . --region us-central1 --allow-unauthenticated --set-secrets BROWSER_USE_LLM_API_KEY=deepseek-key:latest
```
3. Add the URL as Custom MCP Connector in Grok.

## How Cursor + Grok Share the Same MCP

Both can call the same tools. Grok orchestrates high-level tasks. Cursor can use the same server for development tasks (run tests, fix code, etc.).

## Smart Confirmation System

The agent will ask for confirmation intelligently:
- One-time approve
- Approve for this skill forever
- Approve for this website/domain
- Approve anytime for low-risk tasks
- Never for this action

You can reply with short commands like "approve forever", "approve for golf", "no".

## Credential Management

When a new login or API key is needed, the agent generates a one-time secure link. You click it, enter the data, and it goes straight into Google Secret Manager. I never see the actual values.

## OpenClaw Hybrid Option

You already have OpenClaw running locally. We can make the cloud agent call OpenClaw when full desktop control is needed (e.g. local apps, file management). This gives the best of both worlds.

## Context & Token Optimization

- LiteLLM for unified LLM calls + caching
- DeepSeek native context caching (enabled by default)
- Semantic memory (remembers successful past actions)
- Prompt compression + summarization for long tasks
- Strategic cache boundaries (stable prompts cached, dynamic content at the end)

This keeps costs low even on long iterative work.

## Next Steps
Tell Grok what to improve or add next. The repo is designed to evolve with you.