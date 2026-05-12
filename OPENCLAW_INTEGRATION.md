## OpenClaw Hybrid Option

Since you already run OpenClaw locally on Windows:

**Recommended Approach**:
- Keep the main MCP on Cloud Run (reliable, always available, good for Cursor)
- Add a tool `delegate_to_openclaw` that securely calls your local OpenClaw when full desktop control is needed

**Security Note**: Full PC control is powerful but risky. We will add strong guardrails and confirmation requirements for any desktop actions.

This gives you the best of both worlds: cloud reliability + local power when needed.