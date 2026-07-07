# beta-mcp-core project notes

This package is the shared source of truth for Beta Mobility MCP auth/scaffold
code. Never copy fail-open provider logic from older MCPs.

Reference implementations:

- `MCPs/folio/mcp_server.py` for fail-closed remote boot and service-token shape.
- `Tools/Photos/src/auth.py` for exact verified-domain claim enforcement.

Security invariants:

- `hd` is only a Google consent-screen hint, never enforcement.
- Enforce `@betamobility.io` from verified FastMCP access-token claims.
- Missing email, false `email_verified`, or non-exact domain match rejects.
- Remote HTTP mode refuses to boot without complete OAuth config and a long JWT key.
- Service tokens use `hmac.compare_digest` and optional expiry.

