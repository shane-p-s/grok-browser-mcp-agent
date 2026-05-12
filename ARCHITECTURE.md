## Proposed Architecture

### Core Stack
- **MCP Server**: FastAPI + Browser Use (open source)
- **Hosting**: Google Cloud Run
- **Secrets**: Google Secret Manager
- **LLM**: DeepSeek (primary) + LiteLLM for flexibility
- **Memory**: Simple vector store or file-based semantic memory
- **Confirmation Layer**: Built into the agent with user-configurable rules

### Cursor Integration
The same MCP server exposes development tools that both Grok and Cursor can call. This creates a shared agent layer.

### OpenClaw Integration (Optional)
When full local PC control is needed, the cloud MCP can delegate to your running OpenClaw instance via a secure local bridge (ngrok or Tailscale recommended for security).

### Confirmation Flow Example
User: "Book tee time at Highland Pines Saturday 8am"
Agent: "I need to log in. Should I use stored credentials? (approve / approve forever / approve for golf / no)"
You reply once. Agent remembers your preference for this skill.