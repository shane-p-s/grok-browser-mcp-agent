# Security Model

- All secrets stored in Google Secret Manager (encrypted)
- MCP endpoint requires Bearer token (set in AUTH_TOKEN)
- One-time credential upload links are single-use + short-lived
- Smart confirmation system prevents unwanted actions
- Audit logging enabled by default
- Least-privilege IAM on Cloud Run

Only you and Grok (via the connector) should have access.