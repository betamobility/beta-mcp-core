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

Access mode is released as v0.3.0. Resolve the release to a commit SHA and pin
that SHA in member repositories; the tag below identifies the release:

```text
beta-mcp-core @ git+https://github.com/betamobility/beta-mcp-core@v0.3.0
```

## Remote Auth

Google-mode remote HTTP must set all of:

- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `JWT_SIGNING_KEY`

`JWT_SIGNING_KEY` must be at least 64 characters. Missing or partial remote auth
configuration raises `SystemExit`; the server must not bind unauthenticated.

## Local Stdio

Local stdio mode remains unauthenticated by design. The trust boundary is the
operator's machine and MCP client process.

## Access mode

Set `CF_ACCESS_TEAM_DOMAIN` and one `CF_ACCESS_AUD`, with no Google trio or
legacy MCP_SERVICE_TOKEN. `CF_ACCESS_SERVICE_TOKENS` is a JSON array of allowed
service-token client IDs (`common_name`), not secret values. It defaults empty.
Use `build_auth_provider(settings)` when constructing FastMCP (returns None in
Access mode); pass `settings=settings` to `build_asgi_app`, or use `run`.
The app helper also loads these environment settings when settings is omitted.

Requests validate the Access assertion independently of Authorization. Verified
human email must match the allowed domain; machine identities must be listed.
`get_caller()` reads verified request state. No OAuth metadata or bearer-token
provider is installed in Access mode. Stateless HTTP is mandatory.

## Deploy scaffold

`scaffold/` contains the non-root, volume-free Access-mode container template.
Google-mode containers keep their existing OAuth persistence configuration until
cutover. See `docs/access-verification.md` for test evidence and live gates.
