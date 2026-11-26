// A throwaway MCP server for the gateway validation plan (U2): can a Beta-run custom connector
// reach a partner with no sign-in through Claude-issued Auto-connect (enterprise-managed
// authorization, jwt-bearer grant), and with one Google sign-in through a fallback, on the same
// server? One OAuthProvider instance carries both paths. One tool, whoami, says which one ran.

import {
  AuthorizationError,
  CimdFetchError,
  OAuthProvider,
  authorizationErrorRedirect,
  type AuthRequest,
  type ConsentDescription,
  type OAuthHelpers,
} from "@cloudflare/workers-oauth-provider";

export interface Env {
  OAUTH_KV: KVNamespace;
  OAUTH_PROVIDER: OAuthHelpers;
  ALLOWED_DOMAIN: string;
  CLAUDE_ISSUER: string;
  GOOGLE_CLIENT_ID: string;
  GOOGLE_CLIENT_SECRET: string;
}

export type Source = "claude-issued" | "google";
export interface Props extends Record<string, unknown> {
  email: string;
  source: Source;
}

// The resource (token audience) is fixed at construction, so it is a constant, not a var.
export const RESOURCE = "https://mcp-auto-connect-test.betamobility.ai/mcp";
const USER_AGENT = "beta-auto-connect-test/0.1 (+https://betamobility.ai)";
const ACCESS_TOKEN_TTL = 3600;
const CLAUDE_ISSUER_PREFIX = "https://identity.anthropic.com/issuers/";

// Claude's CIMD client ids: the public document, and the form that signs token requests with a
// client assertion (offered once the metadata lists private_key_jwt, which CIMD turns on).
export const CLAUDE_CLIENT_IDS: ReadonlySet<string> = new Set([
  "https://claude.ai/oauth/mcp-oauth-client-metadata",
  "https://claude.ai/oauth/mcp-oauth-client-metadata/confidential_client",
]);

// Redirect URIs a client may use at /authorize. Mirrors beta-mcp-core's
// ALLOWED_CLIENT_REDIRECT_URIS (beta_mcp_core/auth.py), but with Claude's two exact callbacks
// instead of the claude.ai/* and claude.com/* origin wildcards.
const CLAUDE_CALLBACKS = new Set([
  "https://claude.ai/api/mcp/auth_callback",
  "https://claude.com/api/mcp/auth_callback",
]);

export function isAllowedRedirectUri(value: string): boolean {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return false;
  }
  if (url.username || url.password || url.hash) return false;
  if (url.protocol === "https:" && CLAUDE_CALLBACKS.has(`${url.origin}${url.pathname}`) && !url.search) {
    return true;
  }
  // Loopback clients (Claude Code, local tools): any port, http only.
  return url.protocol === "http:" && (url.hostname === "localhost" || url.hostname === "127.0.0.1");
}

export function isAllowedEmail(email: unknown, allowedDomain: string): email is string {
  if (typeof email !== "string") return false;
  const parts = email.split("@");
  return parts.length === 2 && parts[0].length > 0 && parts[1] === allowedDomain.toLowerCase();
}

function log(event: string, fields: Record<string, string | undefined> = {}) {
  // Reasons only. No tokens, assertions or addresses.
  console.log(JSON.stringify({ event, ...fields }));
}

// ---------------------------------------------------------------------------------------------
// Auto-connect: Claude's issuer discovery document, cached in memory per issuer.
// ---------------------------------------------------------------------------------------------

const discoveryCache = new Map<string, { jwksUri: string; expires: number }>();
const DISCOVERY_TTL_MS = 10 * 60 * 1000;

export function resetCaches() {
  discoveryCache.clear();
}

async function discoverJwksUri(issuer: string): Promise<string | null> {
  const cached = discoveryCache.get(issuer);
  if (cached && cached.expires > Date.now()) return cached.jwksUri;
  const response = await fetch(`${issuer}/.well-known/openid-configuration`, {
    headers: { "User-Agent": USER_AGENT, Accept: "application/json" },
  });
  if (!response.ok) return null;
  const document = (await response.json()) as { issuer?: unknown; jwks_uri?: unknown };
  // Claude's document holds issuer and jwks_uri only. The issuer must match what we trust.
  if (document.issuer !== issuer) return null;
  if (typeof document.jwks_uri !== "string" || !document.jwks_uri.startsWith("https://")) return null;
  discoveryCache.set(issuer, { jwksUri: document.jwks_uri, expires: Date.now() + DISCOVERY_TTL_MS });
  return document.jwks_uri;
}

// ---------------------------------------------------------------------------------------------
// Google fallback: the ID token checks, as a pure function so it can be tested alone.
// ---------------------------------------------------------------------------------------------

function base64urlToBytes(part: string): Uint8Array {
  const padded = part.replace(/-/g, "+").replace(/_/g, "/");
  return Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
}

function base64url(bytes: ArrayBuffer | Uint8Array): string {
  const array = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  let binary = "";
  for (const byte of array) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export class GoogleIdentityError extends Error {}

/**
 * Checks the claims of the ID token Google returned from its token endpoint and returns the
 * lower-cased email. The token arrives straight from Google over TLS in exchange for our
 * client secret, so its signature is not re-verified (OpenID Connect Core 3.1.3.7); the claims
 * are. Throws GoogleIdentityError naming the failed check.
 */
export function verifyGoogleIdToken(
  idToken: string,
  expected: { clientId: string; allowedDomain: string; nowSeconds?: number },
): string {
  const parts = (idToken || "").split(".");
  if (parts.length !== 3) throw new GoogleIdentityError("malformed_id_token");
  let claims: Record<string, unknown>;
  try {
    claims = JSON.parse(new TextDecoder().decode(base64urlToBytes(parts[1])));
  } catch {
    throw new GoogleIdentityError("malformed_id_token");
  }
  const now = expected.nowSeconds ?? Math.floor(Date.now() / 1000);
  const domain = expected.allowedDomain.toLowerCase();
  if (claims.iss !== "https://accounts.google.com" && claims.iss !== "accounts.google.com") {
    throw new GoogleIdentityError("wrong_issuer");
  }
  const audiences = [].concat(claims.aud as never);
  if (audiences.length !== 1 || audiences[0] !== expected.clientId) throw new GoogleIdentityError("wrong_audience");
  if (typeof claims.exp !== "number" || claims.exp <= now) throw new GoogleIdentityError("expired");
  if (claims.email_verified !== true) throw new GoogleIdentityError("email_not_verified");
  if (typeof claims.hd !== "string" || claims.hd.toLowerCase() !== domain) throw new GoogleIdentityError("wrong_hosted_domain");
  const email = typeof claims.email === "string" ? claims.email.toLowerCase() : "";
  if (!isAllowedEmail(email, domain)) throw new GoogleIdentityError("email_outside_domain");
  return email;
}

// ---------------------------------------------------------------------------------------------
// The interactive handler: /authorize (consent, then Google) and /auth/callback.
// ---------------------------------------------------------------------------------------------

const escapeHtml = (value: string) => value.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

function page(status: number, body: string, headers = new Headers()): Response {
  headers.set("Content-Type", "text/html; charset=utf-8");
  headers.set("Cache-Control", "no-store");
  return new Response(`<!doctype html><meta charset="utf-8"><title>Beta MCP test</title>${body}`, { status, headers });
}

function consentPage(details: ConsentDescription, handle: string): string {
  const name = escapeHtml(details.clientName);
  const domain = details.clientDomain
    ? `Published by <strong>${escapeHtml(details.clientDomain)}</strong>.`
    : "This app registered itself; its name is not verified.";
  return `<h1>Allow ${name} to use this test connector?</h1>
<p>${domain} Access will be sent to <strong>${escapeHtml(details.redirectHost)}</strong>.</p>
${details.redirectIsLoopback ? "<p><strong>This sends access to an app on your computer.</strong> Continue only if you just started signing in from it.</p>" : ""}
<form method="post">
  <input type="hidden" name="handle" value="${escapeHtml(handle)}">
  <p><button name="decision" value="approve">Continue with Google</button> <button name="decision" value="deny">Deny</button></p>
</form>`;
}

async function pkceChallenge(verifier: string): Promise<string> {
  return base64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
}

function googleAuthorizeUrl(env: Env, origin: string, state: string, challenge: string): string {
  const url = new URL("https://accounts.google.com/o/oauth2/v2/auth");
  url.search = new URLSearchParams({
    client_id: env.GOOGLE_CLIENT_ID,
    redirect_uri: `${origin}/auth/callback`,
    response_type: "code",
    scope: "openid email",
    state,
    hd: env.ALLOWED_DOMAIN, // a hint to Google only; the ID token is checked server-side
    code_challenge: challenge,
    code_challenge_method: "S256",
  }).toString();
  return url.toString();
}

async function exchangeGoogleCode(env: Env, origin: string, code: string, verifier: string): Promise<string> {
  const response = await fetch("https://oauth2.googleapis.com/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT },
    body: new URLSearchParams({
      code,
      client_id: env.GOOGLE_CLIENT_ID,
      client_secret: env.GOOGLE_CLIENT_SECRET,
      redirect_uri: `${origin}/auth/callback`,
      grant_type: "authorization_code",
      code_verifier: verifier,
    }),
  });
  if (!response.ok) throw new GoogleIdentityError(`google_token_${response.status}`);
  const body = (await response.json()) as { id_token?: string };
  if (!body.id_token) throw new GoogleIdentityError("no_id_token");
  return body.id_token;
}

function refuseRedirect(request: AuthRequest): boolean {
  return !isAllowedRedirectUri(request.redirectUri);
}

async function authorize(request: Request, env: Env): Promise<Response> {
  const oauth = env.OAUTH_PROVIDER;
  const origin = new URL(request.url).origin;
  if (request.method === "GET") {
    const authRequest = await oauth.parseAuthRequest(request);
    // Never redirect to a URI outside Claude's set, not even with an error.
    if (refuseRedirect(authRequest)) {
      log("refused", { reason: "redirect_uri_not_allowed" });
      return page(400, "<p>This client is not allowed to use this connector.</p>");
    }
    const details = await oauth.describeConsent(authRequest);
    const consent = await oauth.beginConsent(authRequest);
    return page(200, consentPage(details, consent.handle), consent.headers);
  }
  if (request.method !== "POST") return new Response(null, { status: 405, headers: { Allow: "GET, POST" } });
  const form = await request.formData();
  const handle = String(form.get("handle") ?? "");
  if (form.get("decision") !== "approve") {
    const denied = await oauth.denyConsent(request, handle);
    denied.headers.set("Location", denied.redirectTo);
    return new Response(null, { status: 302, headers: denied.headers });
  }
  const approved = await oauth.approveConsent(request, handle);
  if (refuseRedirect(approved.request)) return page(400, "<p>This client is not allowed to use this connector.</p>");
  const verifier = crypto.randomUUID() + crypto.randomUUID();
  const upstream = await oauth.beginUpstream(approved.request, { data: { verifier }, headers: approved.headers });
  upstream.headers.set("Location", googleAuthorizeUrl(env, origin, upstream.state, await pkceChallenge(verifier)));
  return new Response(null, { status: 302, headers: upstream.headers });
}

async function googleCallback(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  const resumed = await env.OAUTH_PROVIDER.finishUpstream<{ verifier: string }>(request);
  const deny = (reason: string) => {
    log("refused", { path: "google", reason });
    resumed.headers.set("Location", authorizationErrorRedirect(resumed.request, "access_denied"));
    return new Response(null, { status: 302, headers: resumed.headers });
  };
  if (url.searchParams.get("error")) return deny("google_error");
  const code = url.searchParams.get("code");
  if (!code) return deny("no_code");
  let email: string;
  try {
    const idToken = await exchangeGoogleCode(env, url.origin, code, resumed.data.verifier);
    email = verifyGoogleIdToken(idToken, { clientId: env.GOOGLE_CLIENT_ID, allowedDomain: env.ALLOWED_DOMAIN });
  } catch (error) {
    return deny(error instanceof GoogleIdentityError ? error.message : "google_exchange_failed");
  }
  const props: Props = { email, source: "google" };
  const { redirectTo } = await env.OAUTH_PROVIDER.completeAuthorization({
    request: resumed.request,
    userId: encodeURIComponent(email), // no ":" allowed in a user id
    metadata: { source: "google" },
    scope: resumed.request.scope,
    props,
  });
  log("granted", { path: "google" });
  resumed.headers.set("Location", redirectTo);
  return new Response(null, { status: 302, headers: resumed.headers });
}

const defaultHandler: ExportedHandler<Env> = {
  async fetch(request, env) {
    const { pathname } = new URL(request.url);
    try {
      if (pathname === "/health") return Response.json({ status: "ok", service: "beta-auto-connect-test" });
      if (pathname === "/authorize") return await authorize(request, env);
      if (pathname === "/auth/callback") return await googleCallback(request, env);
    } catch (error) {
      if (error instanceof AuthorizationError || error instanceof CimdFetchError) {
        const reason = error instanceof AuthorizationError ? error.code : error.reason;
        log("refused", { path: "authorize", reason });
        return page(400, "<p>This sign-in request could not be completed. Start again from Claude.</p>");
      }
      throw error;
    }
    return new Response("Not found", { status: 404 });
  },
};

// ---------------------------------------------------------------------------------------------
// The MCP endpoint: minimal Streamable HTTP, stateless, JSON only. One tool.
// ---------------------------------------------------------------------------------------------

const PROTOCOL = "2025-06-18";
const SERVER = { name: "beta-auto-connect-test", version: "0.1.0" };
const TOOL = {
  name: "whoami",
  description: "Returns the email this connector authenticated you as, and which path did it: claude-issued or google.",
  inputSchema: { type: "object", properties: {}, additionalProperties: false },
};

function rpcResult(id: unknown, result: unknown) {
  return Response.json({ jsonrpc: "2.0", id, result });
}

export function handleRpc(message: { id?: unknown; method?: string; params?: { protocolVersion?: string; name?: string } }, props: Props) {
  const { id, method, params } = message;
  if (method === "initialize") {
    return rpcResult(id, { protocolVersion: params?.protocolVersion || PROTOCOL, capabilities: { tools: {} }, serverInfo: SERVER });
  }
  if (method === "ping") return rpcResult(id, {});
  if (method === "tools/list") return rpcResult(id, { tools: [TOOL] });
  if (method === "tools/call" && params?.name === TOOL.name) {
    // Exactly two fields, nothing else.
    const text = JSON.stringify({ email: props.email, source: props.source });
    return rpcResult(id, { content: [{ type: "text", text }] });
  }
  if (id === undefined) return new Response(null, { status: 202 }); // a notification
  return Response.json({ jsonrpc: "2.0", id, error: { code: -32601, message: "Method not found" } });
}

const apiHandler = {
  async fetch(request: Request, _env: Env, ctx: ExecutionContext) {
    if (request.method !== "POST") return new Response(null, { status: 405, headers: { Allow: "POST" } });
    const props = (ctx as ExecutionContext & { props?: Props }).props;
    if (!props?.email || !props.source) return new Response("Unauthorized", { status: 401 });
    let message: Parameters<typeof handleRpc>[0];
    try {
      message = await request.json();
    } catch {
      return Response.json({ jsonrpc: "2.0", id: null, error: { code: -32700, message: "Parse error" } }, { status: 400 });
    }
    log("served", { method: String(message?.method ?? "batch"), source: props.source });
    return handleRpc(message, props);
  },
};

// ---------------------------------------------------------------------------------------------
// The provider.
// ---------------------------------------------------------------------------------------------

export const provider = new OAuthProvider<Env>({
  apiRoute: "/mcp",
  apiHandler,
  defaultHandler,
  authorizeEndpoint: "/authorize",
  tokenEndpoint: "/oauth/token",
  // No clientRegistrationEndpoint: dynamic client registration is off. Claude registers by CIMD.
  clientIdMetadataDocumentEnabled: true,
  resourceMetadata: { resource: RESOURCE },
  accessTokenTTL: ACCESS_TOKEN_TTL,
  // Refresh tokens stay on for the interactive (Google) grant, as Claude's guidance asks. The
  // library issues none for the jwt-bearer grant.
  enterpriseManagedAuthorization: {
    allowPublicClients: true, // CIMD clients are public
    trustedIssuers: async ({ iss, env, clientInfo }) => {
      // Exact string match on the whole URL, path included, before any fetch.
      if (!env.CLAUDE_ISSUER.startsWith(CLAUDE_ISSUER_PREFIX) || iss !== env.CLAUDE_ISSUER) {
        log("refused", { path: "claude-issued", reason: "issuer_not_trusted" });
        return null;
      }
      if (!CLAUDE_CLIENT_IDS.has(clientInfo.clientId)) {
        log("refused", { path: "claude-issued", reason: "client_not_claude" });
        return null;
      }
      try {
        const jwksUri = await discoverJwksUri(iss);
        if (!jwksUri) {
          log("refused", { path: "claude-issued", reason: "discovery_invalid" });
          return null;
        }
        return { issuer: iss, jwksUri, algorithms: ["ES256"] };
      } catch {
        log("refused", { path: "claude-issued", reason: "discovery_failed" });
        return null;
      }
    },
    mapClaims: async ({ claims, requestedScope, env }) => {
      const email = typeof claims.email === "string" ? claims.email.toLowerCase() : "";
      if (!isAllowedEmail(email, env.ALLOWED_DOMAIN)) {
        log("refused", { path: "claude-issued", reason: "email_outside_domain" });
        return null;
      }
      const props: Props = { email, source: "claude-issued" };
      log("granted", { path: "claude-issued" });
      return { userId: encodeURIComponent(email), scope: requestedScope, metadata: { source: "claude-issued" }, props };
    },
  },
  onError({ internal }) {
    log("oauth_error", { category: internal?.category, reason: internal?.reason });
  },
});

export default provider;
