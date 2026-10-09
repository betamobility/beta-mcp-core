# Cloudflare Access in front of the MCP fleet: plan

2026-10-09. A plan, not a decision. It answers three questions: what changes per server,
whether Claude's connector can sign in through Access, and what the product owner must do
by hand. Nothing here has been built or tested by us.

## The short version

- Cloudflare Access can act as the OAuth authorization server for an MCP server ("managed
  OAuth"). The server then stops running its own Google sign-in and only checks a signed
  header from Cloudflare.
- **Claude Code can sign in this way today, by several users' reports. Claude's hosted
  connector (claude.ai web, desktop, mobile, Cowork) could not as of September 2026.** The
  reports are open in Anthropic's own tracker and no fix is confirmed there.
- So the fleet should not be moved yet. The first step is a one-server test that costs
  little and settles the question. Until it passes, servers keep Google sign-in through
  `beta-mcp-core`.

## Can Claude's connector authenticate through Access?

What the sources say, each opened on 2026-10-09 unless marked.

| Claim | Source | How seen |
|---|---|---|
| With managed OAuth on, Access "returns a `401` response instead of a `302` redirect to non-browser clients", with a `WWW-Authenticate` header pointing to OAuth discovery metadata; it issues opaque tokens and "the MCP server must validate the Access JWT sent in the `Cf-Access-Jwt-Assertion` header" | [Cloudflare, Managed OAuth](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/managed-oauth/) | opened |
| Dynamic client registration is a setting of the Access application, with a list of allowed redirect URIs and separate switches for loopback clients. The client must support RFC 8707 | same page | opened |
| Claude's hosted surfaces register by dynamic client registration and use the callback `https://claude.ai/api/mcp/auth_callback`, which may move to claude.com; Claude Code uses a loopback callback | [Claude docs, authentication for connectors](https://claude.com/docs/connectors/building/authentication), [support article 11503834](https://support.claude.com/en/articles/11503834) | search summary only, pages not opened |
| A custom connector against Access managed OAuth fails on claude.ai web and mobile after sign-in and consent succeed; "the origin logged zero authenticated requests"; Claude Code completes the same flow | [anthropics/claude-ai-mcp issue 980](https://github.com/anthropics/claude-ai-mcp/issues/980), opened 2026-09-03, still open, last updated 2026-09-09 | opened |
| An Anthropic collaborator traced one such failure to Cloudflare refusing the registration: "redirect_uri is not allowed by the account configuration". That is the allowed-redirect list above, and is a configuration matter | [issue 478](https://github.com/anthropics/claude-ai-mcp/issues/478), closed as not planned 2026-06-29 | opened |
| Earlier reports of the same pattern | [issue 410](https://github.com/anthropics/claude-ai-mcp/issues/410), closed as not planned | opened |

Reading (ours): there are two failure causes in the reports. One is a missing allowed
redirect URI, which we can configure. The other (issue 980) happens after consent, on
Anthropic's side of the exchange, and is not ours to fix. We have not reproduced either. A
user report is not a statement from Anthropic or Cloudflare, and neither vendor's
documentation names the other as supported.

Not found: a Cloudflare page that lists Claude's hosted connector as a tested client, or an
Anthropic page that lists Cloudflare Access as a tested authorization server.

## The test that settles it

One server, one throwaway hostname, no change to anything in use.

1. Deploy a copy of the smallest fleet server (or a stock fastmcp "hello" server) to a new
   Railway service with auth switched to the Access check below.
2. Put it on a new hostname on a Cloudflare zone, with an Access application, managed OAuth
   on, dynamic client registration on, and the allowed redirect URIs
   `https://claude.ai/api/mcp/auth_callback` and `https://claude.com/api/mcp/auth_callback`,
   plus the loopback switches for Claude Code.
3. Add it as a custom connector in claude.ai and in Claude Code, sign in, call one tool.

Pass: a tool call from claude.ai reaches the origin with a valid assertion for the signed-in
`@betamobility.io` user. Fail: record the reference id Claude shows and stop; the fleet stays
on Google sign-in and the test is repeated when issue 980 closes.

### State of the test, 2026-10-09

Built: `spikes/cloudflare-access-test/`, live at `https://mcp-access-test.betamobility.ai/mcp`.
Steps 1 and 2 are done and checked from the command line: the unauthenticated `401` carries
the `WWW-Authenticate` header, discovery names Access as the authorization server, and a
client registering with Claude's callback gets `201` where one with another redirect gets
the refusal quoted in issue 478. Step 3, the sign-in from Claude and one tool call, is not
done: it needs a person in a signed-in browser. Until it is, the question is still open.

## What changes per server, if the test passes

| Step | What | Who |
|---|---|---|
| 1 | A hostname on a Cloudflare zone, proxied, pointing at the Railway service. Zone SSL mode Full (Strict). Today the servers answer on `*.up.railway.app`, which Access cannot sit in front of | agent, except the Railway domain verification record |
| 2 | An Access application on `host/mcp` with managed OAuth and dynamic client registration as in the test; an allow policy for `@betamobility.io`, or a named group where a server is narrower (Folk's project instance) | agent with an Access-scoped token, or by hand |
| 3 | A bypass or separate application for `/health`, and a path-scoped application with a service-token policy for each machine caller | agent |
| 4 | The server validates `Cf-Access-Jwt-Assertion` on every request: signature against the team's keys, audience equal to the application's, verified email in the allowed domain. No request without it is served, so the Railway address stops being a way round Access | code, once, in `beta-mcp-core` |
| 5 | Remove the Google provider from that server: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `JWT_SIGNING_KEY`, and the volume that holds fastmcp's OAuth state. Add `CF_ACCESS_TEAM_DOMAIN` and `CF_ACCESS_AUD` | agent |
| 6 | Each user removes the old connector and adds the new address | each user |

In `beta-mcp-core` this is one new auth mode beside the Google one, chosen by settings, so a
server moves by configuration and the two modes can coexist across the fleet during the
move. Managed OAuth replaces the application's own `401` behaviour, so one server cannot run
both at once.

Consequences to weigh:

- Sign-in, session length and who is allowed move from each server's code to one place in
  Cloudflare. Removing a person is one change instead of one per server.
- Access tokens last 15 minutes by default and policies are checked again at each refresh.
- Every server needs its own hostname. Sixteen repositories depend on `beta-mcp-core`
  today.
- A server reached both by people and by scheduled agents needs two Access applications.
  The four traps recorded in `Docs/auth-setup.md` apply.
- Identity inside the server changes source (Access assertion instead of Google token).
  Per-user allowlists such as Folk's `folk-internal` keep working on the email.

## What the product owner must do by hand

1. Decide whether to run the test now or wait for Anthropic's issue 980 to close.
2. Give the agent a Cloudflare API token with the Access scopes (Apps and Policies, Service
   Tokens, Organizations and Identity Providers), or create the Access applications in the
   dashboard. The token in use has no Access scope.
3. For each server: add Railway's domain verification record when asked. It is shown in the
   Railway dashboard only.
4. For each server: add the new address as a connector in Claude, sign in, and make one
   call per tool. This is the same step that is still open for the two Folk servers.
5. Tell the other users of each server when its address changes.

## Order, if it goes ahead

1. The test server.
2. `beta-mcp-core` release with the Access mode.
3. One server nobody else depends on, then the two Folk servers, then the rest in the order
   used for the v0.2.0 move.

## Limits of this note

No server was changed and no sign-in was attempted. The Claude documentation pages were
seen through search summaries. The reports of failure are from users, dated June to
September 2026; the state today is unknown until the test is run.
