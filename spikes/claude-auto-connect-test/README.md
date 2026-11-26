# Claude Auto-connect test server

A throwaway MCP server for U2 of `docs/plans/2026-11-26-1156-feat-mcp-gateway-single-sign-in-plan.md`
(beta-infra repo): can a Beta-run custom connector reach a partner with no sign-in through Claude-issued
Auto-connect, and with one Google sign-in through a fallback, on the same server? It is not part of the
package and is removed when the question is settled (see "Removing it").

- Address: `https://mcp-auto-connect-test.betamobility.ai/mcp`
- Worker `beta-auto-connect-test`, one `OAuthProvider` instance
  (`@cloudflare/workers-oauth-provider` 1.4.0, experimental `enterpriseManagedAuthorization`).
- One tool, `whoami`, returns only `{email, source}`; `source` is `claude-issued` or `google`.

## What it proves

- Auto-connect: Claude sends a jwt-bearer assertion signed by Beta's Claude issuer. The Worker trusts exactly
  `CLAUDE_ISSUER` (whole URL, path included, compared before any key fetch), ES256 only, keys from
  `<issuer>/.well-known/openid-configuration` (cached 10 minutes, sends a User-Agent), maps the lower-cased
  `email` claim and refuses any email whose domain is not exactly `betamobility.io`. No refresh token is issued for
  this grant (library behaviour, covered by a test).
- Fallback: Google authorization code with PKCE in `/authorize`. `hd=betamobility.io` is only a hint; the ID token
  claims are checked server-side (`iss`, `aud`, `exp`, `email_verified === true`, `hd`, email domain). Google
  callback path is `/auth/callback`.
- Clients: CIMD on, DCR off (no `registration_endpoint`). `/authorize` refuses any redirect URI other than
  `https://claude.ai/api/mcp/auth_callback`, `https://claude.com/api/mcp/auth_callback` and loopback
  (`http://localhost:*`, `http://127.0.0.1:*`). The jwt-bearer grant accepts only Claude's CIMD client ids
  (`https://claude.ai/oauth/mcp-oauth-client-metadata` and its `/confidential_client` form).
- A minimal consent page shows before the Google redirect (the library's per-client consent for non-pre-registered clients).
- Logs hold reasons only, never tokens, assertions or addresses.

## Local tests

`npm install && npm test` (Vitest with `@cloudflare/vitest-plugin`, running in workerd). Keys are generated in
the test; Claude's discovery document, JWKS, CIMD documents and Google are stubbed. Seams worth knowing:

- Replay: tested through the library's KV marker. KV is eventually consistent, so replay protection is best effort
  across Cloudflare locations, while Anthropic requires each `jti` to be accepted once. Not testable at unit level.
- CIMD redirect limit: the library has no option for it, so it is enforced in our `/authorize` handler (after
  `parseAuthRequest`, render locally, never redirect) and tested there. The jwt-bearer grant does not use redirect URIs;
  it is limited by the Claude client-id check instead.
- Google: the token endpoint is stubbed, so Google's real response is only seen at deploy.
- Claude's real CIMD document and assertion shape are not tested locally; the owner's Run test is the evidence.

## Deploy (agent does this, from this folder)

1. `npm install`
2. KV: `env -u CLOUDFLARE_API_TOKEN npx wrangler kv namespace create OAUTH_KV`; put the id in `wrangler.toml`.
3. Google OAuth web client (Beta Workspace): redirect URI `https://mcp-auto-connect-test.betamobility.ai/auth/callback`.
   Store the secret in 1Password with the `op` CLI at once. Put the client id in `GOOGLE_CLIENT_ID`.
4. `CLAUDE_ISSUER` in `wrangler.toml`: from Organization settings > Connectors > (the connector) > Authentication >
   Auto-connect > Set up > Claude-issued. Copy it exactly; the Worker refuses to trust anything not under
   `https://identity.anthropic.com/issuers/`.
5. `env -u CLOUDFLARE_API_TOKEN npx wrangler secret put GOOGLE_CLIENT_SECRET`
6. `env -u CLOUDFLARE_API_TOKEN npx wrangler deploy`
7. Check `https://mcp-auto-connect-test.betamobility.ai/.well-known/oauth-authorization-server`: `jwt-bearer` in
   `grant_types_supported`, token endpoint on the same host, `client_id_metadata_document_supported: true`.

## Owner steps

1. Organization settings > Connectors: add a custom connector at the URL above (no client id or secret).
2. Open its Configuration tab, Authentication > Auto-connect > Set up, Identity source Claude-issued. Copy the
   issuer URL to the agent if not yet in `wrangler.toml` (then redeploy).
3. Click Run test. Note the failed step, if any, and the decoded claims shown.
4. Call `whoami` from the connector: expect your email and `claude-issued` with no sign-in screen.
5. Turn Auto-connect off (or use a fresh connector), connect, sign in with Google once, call `whoami`:
   expect `google`.

## What is verified

| Check | Pass / Fail |
|---|---|
| Authorization server metadata lists jwt-bearer; token endpoint on `mcp-auto-connect-test.betamobility.ai`; no `registration_endpoint` (deployed) | |
| Run test passes; decoded assertion has Beta's issuer, ES256, `email` | |
| `whoami` via Auto-connect: email and `claude-issued`, no sign-in screen | |
| Auto-connect token response has `expires_in` and no refresh token | |
| Claude registers by CIMD (no DCR); its real CIMD document is accepted | |
| `whoami` via Google fallback: email and `google` | |
| A non-`betamobility.io` Google account cannot finish the fallback | |
| Both paths work on one deployed instance (plan assumption) | |

## U3 matrix (recorded per cell: works / prompts / duplicates / which path authenticated)

| Surface | Auth path | Org connector only | Test plugin only | Both |
|---|---|---|---|---|
| Desktop app | Auto-connect | | | |
| Desktop app | Google fallback | | | |
| claude.ai web | Auto-connect | | | |
| claude.ai web | Google fallback | | | |
| Mobile | Auto-connect | | | |
| Mobile | Google fallback | | | |
| Claude Code | Auto-connect | | | |
| Claude Code | Google fallback | | | |

Further records:

- Second partner's account, best-working set-up: 
- Redeploy check (one call per auth path after a redeploy): 
- Fallback access-token lifetime (configured 3600 s), refresh token issued, refresh or grant lifetime against R6's 30 days
  (library default `refreshTokenTTL` is 30 days, fixed from the code exchange): 
- Cells not run because U1 found no Auto-connect row: 

## Removing it

`env -u CLOUDFLARE_API_TOKEN npx wrangler delete` (removes the custom domain), delete the KV namespace, delete the
Google OAuth client and its 1Password item, remove the connector and test plugin from Claude, delete this folder.
