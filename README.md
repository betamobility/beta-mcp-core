# beta-mcp-core

Shared fail-closed MCP plumbing for Beta Mobility servers.

This package owns the security-bearing code that was previously copied between
remote MCP servers:

- Google OAuth domain enforcement for `@betamobility.io`
- constant-time service-token verification with optional expiry
- fail-closed remote boot guards
- Starlette `/health` and dual stdio/HTTP entrypoint helpers
- Supabase asyncpg pooler DSN parsing
- TTL cache and caller identity helpers

## Install

Pin a released tag from member repos:

```text
beta-mcp-core @ git+https://github.com/betamobility/beta-mcp-core@v0.1.1
```

## Remote Auth

Remote HTTP mode must set all of:

- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `JWT_SIGNING_KEY`
- `MCP_SERVICE_TOKEN` for non-interactive internal MCP-to-MCP calls

`JWT_SIGNING_KEY` must be at least 64 characters. Missing or partial remote auth
configuration raises `SystemExit`; the server must not bind unauthenticated.

`MCP_SERVICE_TOKEN` is the single Beta-wide bypass variable name. Compare it in
constant time only, rotate it centrally, and treat it as equivalent to full
tool access.

## Local Stdio

Local stdio mode remains unauthenticated by design. The trust boundary is the
operator's machine and MCP client process.
