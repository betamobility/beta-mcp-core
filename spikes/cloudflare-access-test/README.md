# Cloudflare Access managed OAuth: test server

A throwaway MCP server for one question from
`docs/plans/2026-10-09-cloudflare-access-for-the-mcp-fleet.md`: can Claude's connector sign in
through Cloudflare Access? It is not part of the package and is removed when the question
is settled.

- Address: `https://mcp-access-test.betamobility.ai/mcp`
- Worker `beta-access-test`, deployed with `env -u CLOUDFLARE_API_TOKEN npx wrangler deploy`
  from this folder.
- Access application "beta-access-test (MCP managed OAuth spike)", id
  `72ec8bcf-accb-48bb-984c-b03a5d9cc6de`: Google sign-in, allow `@betamobility.io`, managed
  OAuth on, dynamic client registration on with Claude's two callback addresses and loopback
  clients allowed, access tokens 15 minutes.
- The worker has no auth of its own. It serves `/mcp` only when the request carries an Access
  assertion that verifies against the team's keys, is for this application, has not expired
  and names a `@betamobility.io` email. One tool, `whoami`, returns that email.

## What is verified (2026-10-09)

| Check | Result |
|---|---|
| `POST /mcp` without a token | `401` with `WWW-Authenticate: Bearer ... resource_metadata=".../.well-known/cloudflare-access-protected-resource/mcp"` |
| Protected-resource metadata | names `https://round-smoke-5856.cloudflareaccess.com` as authorization server |
| Authorization-server metadata | authorization, token, revocation and registration endpoints; PKCE S256 |
| Registering a client with `https://claude.ai/api/mcp/auth_callback` | `201` |
| Registering a client with a redirect that is not on the list | `400`, "redirect_uri is not allowed by the account configuration" |

The last row is the failure an Anthropic collaborator described in
anthropics/claude-ai-mcp issue 478: it is the allowed-redirect list, and with Claude's
callback on the list the registration succeeds.

## What is not verified

A sign-in from Claude and a tool call. That needs a person in a browser. Two results are
wanted, each either "the tool answered with my email" or the reference id Claude shows:

1. Claude's hosted connector (claude.ai, Settings, Connectors, add custom connector, the
   address above, Connect, then ask Claude to "run whoami on beta-access-test").
2. Claude Code: `claude mcp add --transport http beta-access-test https://mcp-access-test.betamobility.ai/mcp`,
   then `/mcp` and authenticate.

## Removing it

Delete the Access application, `npx wrangler delete` the worker (which removes the custom
domain), remove the connector from Claude, delete this folder.
