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

Access mode was released in v0.3.0; v0.4.0 supports FastMCP 3.4.11 and later 3.x
as well as FastMCP 4. Resolve the release to a commit SHA and pin that SHA in member
repositories; the tag below identifies the release:

```text
beta-mcp-core @ git+https://github.com/betamobility/beta-mcp-core@v0.4.0
```

A member repo that installs with `pip --require-hashes` cannot hash a git requirement.
Pin the commit's archive URL instead, which `uv pip compile --generate-hashes` can hash:

```text
beta-mcp-core @ https://github.com/betamobility/beta-mcp-core/archive/<sha>.tar.gz
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

## Tool analytics

`ToolAnalyticsMiddleware` is opt-in and runs at FastMCP's tool boundary for both
HTTP and stdio. Register it **once** on the server, replacing any HTTP request
analytics middleware. Listing tools, health checks, and unknown tool names do
not generate tool-use events.

```python
from beta_mcp_core.analytics import ToolAnalyticsMiddleware

analytics = ToolAnalyticsMiddleware(
    component_id="ssb-mcp",  # fixed registered app identifier
    ingest_url=settings.mc_analytics_url,  # HTTPS /analytics/event endpoint
    ingest_token=settings.mc_analytics_token,
    enabled=settings.analytics_enabled,
    environment=settings.environment,  # must explicitly be "production"
)
mcp.add_middleware(analytics)
# In the server lifespan's shutdown, after stopping new tool calls:
await analytics.drain()
```

Each completed call records one `tool_call` outcome (`success` or `error`) with
its registered tool name and duration band (`lt_1s`, `1_5s`, `5_30s`, `gte_30s`).
Arguments, results, caller identities, error text, and visitor IPs are never
included in event JSON. Mission Control receives events first, targeting `betamobility.ai`
with the synthetic URL `https://betamobility.ai/mcp/<component_id>`.

Protocol errors and raised exceptions count as errors. For servers whose
existing result contract returns a top-level `error` field inside a normal
structured response, set `error_result_key="error"`. A truthy value counts as
an error without changing the tool response or including that value in analytics.
Leave this unset when an `error` field is ordinary data rather than a failure.

Missing production configuration disables analytics with a warning, without
breaking tools. Pending deliveries are capped at 64 by default; a full queue
logs an explicit delivery failure. Each send has a ten-second deadline and no
retry, so an uncertain receipt cannot duplicate the event. A successful HTTP
response counts as delivered only when Mission Control returns both
`status: "ok"` and `plausible: "ok"`. Dropped events, failed forwarding, and
transport failures log sanitized warnings and never change tool results.
Call `drain()` before closing any injected HTTP client at shutdown. Production
verification must confirm accepted forwarding and the component's actual
Plausible event receipt; a healthy service or HTTP 200 alone is insufficient.

For HTTP calls, the middleware forwards the actual inbound User-Agent as a
transport header. Set `trust_cloudflare_client_ip=True` only in deployments
whose Cloudflare Access boundary is verified and whose origin cannot be reached
by callers bypassing that boundary. This setting forwards a single valid public
`CF-Connecting-IP` to Mission Control as `X-Beta-Visitor-IP`. It defaults to false;
invalid, private, repeated, or comma-separated addresses are ignored. There is
no `X-Forwarded-For` fallback. These headers are not stored in event JSON or
logged by this middleware; inbound cookies, authorization, and Access credentials
are never copied. Mission Control must preserve that transport-only treatment.

Without an HTTP request (including stdio), events retain the truthful
`beta-mcp-core/analytics` User-Agent and carry no visitor IP. Cloud and stdio
traffic may be filtered by Plausible and cannot guarantee distinct user counts.
A dropped receipt remains a delivery failure; never fabricate a browser or
visitor IP to bypass filtering.
