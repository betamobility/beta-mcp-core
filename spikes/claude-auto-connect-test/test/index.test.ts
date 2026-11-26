import { env, exports } from "cloudflare:workers";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import {
  CLAUDE_CLIENT_IDS,
  GoogleIdentityError,
  RESOURCE,
  isAllowedEmail,
  isAllowedRedirectUri,
  resetCaches,
  verifyGoogleIdToken,
} from "../src/index";

const ORIGIN = "https://mcp-auto-connect-test.betamobility.ai";
const ISS = (env as unknown as { CLAUDE_ISSUER: string }).CLAUDE_ISSUER;
const GOOGLE_CLIENT_ID = (env as unknown as { GOOGLE_CLIENT_ID: string }).GOOGLE_CLIENT_ID;
const CLAUDE_CLIENT = "https://claude.ai/oauth/mcp-oauth-client-metadata";
const CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback";
const JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer";
const JWKS_URI = "https://identity.anthropic.com/issuers/test-beta-issuer/.well-known/jwks.json";

const b64 = (bytes: ArrayBuffer | Uint8Array | string) => {
  const array = typeof bytes === "string" ? new TextEncoder().encode(bytes) : new Uint8Array(bytes);
  let binary = "";
  for (const byte of array) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
};

let signingKey: CryptoKey;
let jwk: JsonWebKey;

beforeAll(async () => {
  const pair = (await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, true, ["sign", "verify"])) as CryptoKeyPair;
  signingKey = pair.privateKey;
  jwk = { ...(await crypto.subtle.exportKey("jwk", pair.publicKey)), kid: "test-key", alg: "ES256", use: "sig" } as unknown as JsonWebKey;
});

async function signJwt(header: Record<string, unknown>, payload: Record<string, unknown>) {
  const input = `${b64(JSON.stringify(header))}.${b64(JSON.stringify(payload))}`;
  const signature = await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, signingKey, new TextEncoder().encode(input));
  return `${input}.${b64(signature)}`;
}

let jtiCounter = 0;
async function assertion(overrides: Record<string, unknown> = {}, headerOverrides: Record<string, unknown> = {}) {
  const now = Math.floor(Date.now() / 1000);
  return signJwt(
    { alg: "ES256", typ: "oauth-id-jag+jwt", kid: "test-key", ...headerOverrides },
    {
      iss: ISS,
      sub: "opaque-subject",
      email: "partner@betamobility.io",
      aud: [ORIGIN],
      client_id: CLAUDE_CLIENT,
      resource: RESOURCE,
      iat: now,
      nbf: now - 5,
      exp: now + 45,
      jti: `jti-${++jtiCounter}-${crypto.randomUUID()}`,
      ...overrides,
    },
  );
}

// ---- outbound fetch stub: Claude's discovery, JWKS, CIMD documents, Google ----

let calls: string[] = [];
let googleIdToken = "";
const cimdDocs: Record<string, unknown> = {};

function stubFetch() {
  calls = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : (input as Request).url;
    calls.push(url);
    if (url === `${ISS}/.well-known/openid-configuration`) return Response.json({ issuer: ISS, jwks_uri: JWKS_URI });
    if (url === JWKS_URI) return Response.json({ keys: [jwk] });
    if (url in cimdDocs) return Response.json(cimdDocs[url]);
    if (url === "https://oauth2.googleapis.com/token") {
      const sent = init?.body instanceof URLSearchParams ? init.body : new URLSearchParams(String(init?.body));
      expect(sent.get("client_secret")).toBe("test-google-client-secret");
      expect(sent.get("code_verifier")).toBeTruthy();
      return Response.json({ id_token: googleIdToken, access_token: "google-access" });
    }
    return new Response("unexpected fetch in test: " + url, { status: 599 });
  });
}

beforeEach(() => {
  resetCaches();
  cimdDocs[CLAUDE_CLIENT] = {
    client_id: CLAUDE_CLIENT,
    client_name: "Claude",
    redirect_uris: [CLAUDE_CALLBACK, "https://claude.com/api/mcp/auth_callback"],
    token_endpoint_auth_method: "none",
    grant_types: ["authorization_code", "refresh_token", JWT_BEARER],
    response_types: ["code"],
  };
  stubFetch();
});
afterEach(() => vi.restoreAllMocks());

const worker = (path: string, init?: RequestInit) => (exports as any).default.fetch(new Request(`${ORIGIN}${path}`, { redirect: "manual", ...init }));

async function tokenRequest(assertionJwt: string, extra: Record<string, string> = {}) {
  return worker("/oauth/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ grant_type: JWT_BEARER, assertion: assertionJwt, client_id: CLAUDE_CLIENT, resource: RESOURCE, ...extra }),
  });
}

async function whoami(accessToken: string) {
  const response = await worker("/mcp", {
    method: "POST",
    headers: { Authorization: `Bearer ${accessToken}`, "Content-Type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/call", params: { name: "whoami", arguments: {} } }),
  });
  expect(response.status).toBe(200);
  const body = (await response.json()) as { result: { content: { text: string }[] } };
  return JSON.parse(body.result.content[0].text);
}

describe("authorization server metadata", () => {
  it("lists jwt-bearer, puts the token endpoint on the request host, and has no DCR", async () => {
    const response = await worker("/.well-known/oauth-authorization-server");
    expect(response.status).toBe(200);
    const metadata = (await response.json()) as Record<string, any>;
    expect(metadata.issuer).toBe(ORIGIN);
    expect(metadata.token_endpoint).toBe(`${ORIGIN}/oauth/token`);
    expect(metadata.grant_types_supported).toContain(JWT_BEARER);
    expect(metadata.grant_types_supported).toContain("authorization_code");
    expect(metadata.client_id_metadata_document_supported).toBe(true);
    expect(metadata.registration_endpoint).toBeUndefined();
  });
});

describe("Auto-connect (jwt-bearer grant)", () => {
  it("accepts a valid Beta-issuer assertion: token with expires_in, no refresh token, whoami says claude-issued", async () => {
    const response = await tokenRequest(await assertion());
    expect(response.status).toBe(200);
    const body = (await response.json()) as Record<string, any>;
    expect(body.access_token).toBeTruthy();
    expect(body.expires_in).toBeGreaterThan(0);
    expect(body.refresh_token).toBeUndefined();
    expect(await whoami(body.access_token)).toEqual({ email: "partner@betamobility.io", source: "claude-issued" });
    expect(calls).toContain(`${ISS}/.well-known/openid-configuration`);
  });

  it("lower-cases the email claim", async () => {
    const response = await tokenRequest(await assertion({ email: "Partner@BetaMobility.io" }));
    expect(response.status).toBe(200);
    expect((await whoami(((await response.json()) as any).access_token)).email).toBe("partner@betamobility.io");
  });

  it.each([
    ["another issuer", "https://identity.anthropic.com/issuers/someone-else"],
    ["Beta's issuer with a different path", `${ISS}/extra`],
    ["Beta's issuer with a trailing slash", `${ISS}/`],
    ["a prefix of Beta's issuer", ISS.slice(0, -1)],
  ])("refuses %s with invalid_grant before fetching any key", async (_name, iss) => {
    const response = await tokenRequest(await assertion({ iss }));
    expect(response.status).toBe(400);
    expect(((await response.json()) as any).error).toBe("invalid_grant");
    // The stub would serve valid keys for these URLs, so nothing was fetched on trust alone.
    expect(calls.filter((c) => c.includes("openid-configuration") || c.includes("jwks"))).toEqual([]);
  });

  it("refuses an email outside betamobility.io", async () => {
    for (const email of ["partner@example.com", "partner@betamobility.io.evil.com", "partner@sub.betamobility.io", "a@b@betamobility.io", ""]) {
      const response = await tokenRequest(await assertion({ email }));
      expect(response.status, email).toBe(400);
      expect(((await response.json()) as any).error, email).toBe("invalid_grant");
    }
  });

  it("refuses a missing email", async () => {
    const response = await tokenRequest(await assertion({ email: undefined }));
    expect(((await response.json()) as any).error).toBe("invalid_grant");
  });

  it("refuses a replayed jti", async () => {
    const jwt = await assertion({ jti: "replay-me" });
    expect((await tokenRequest(jwt)).status).toBe(200);
    const second = await tokenRequest(jwt);
    expect(second.status).toBe(400);
    expect(((await second.json()) as any).error).toBe("invalid_grant");
  });

  it("refuses each single broken field, proving the baseline otherwise passes", async () => {
    const cases: [string, Record<string, unknown>, Record<string, unknown>?][] = [
      ["wrong audience", { aud: ["https://other.example"] }],
      ["wrong client_id claim", { client_id: "https://claude.ai/oauth/other" }],
      ["wrong typ", {}, { typ: "JWT" }],
      ["wrong alg", {}, { alg: "RS256" }],
      ["expired", { exp: Math.floor(Date.now() / 1000) - 120, iat: Math.floor(Date.now() / 1000) - 200, nbf: undefined }],
    ];
    for (const [name, claims, header] of cases) {
      const response = await tokenRequest(await assertion(claims, header));
      expect(response.status, name).toBe(400);
      expect(((await response.json()) as any).error, name).toBe("invalid_grant");
    }
    expect((await tokenRequest(await assertion())).status).toBe(200);
  });

  it("refuses a validly signed assertion from a non-Claude CIMD client", async () => {
    const other = "https://evil.example/client.json";
    cimdDocs[other] = { client_id: other, client_name: "Evil", redirect_uris: ["https://evil.example/cb"], token_endpoint_auth_method: "none" };
    const response = await tokenRequest(await assertion({ client_id: other }), { client_id: other });
    expect(response.status).toBe(400);
    expect(CLAUDE_CLIENT_IDS.has(other)).toBe(false);
  });
});

describe("whoami over MCP", () => {
  it("answers initialize and tools/list, and refuses an unauthenticated call", async () => {
    const unauthenticated = await worker("/mcp", { method: "POST", body: "{}" });
    expect(unauthenticated.status).toBe(401);
    const token = ((await (await tokenRequest(await assertion())).json()) as any).access_token;
    const call = (method: string) =>
      worker("/mcp", {
        method: "POST",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify({ jsonrpc: "2.0", id: 7, method }),
      }).then((r: Response) => r.json() as Promise<any>);
    expect((await call("initialize")).result.serverInfo.name).toBe("beta-auto-connect-test");
    expect((await call("tools/list")).result.tools.map((t: any) => t.name)).toEqual(["whoami"]);
  });
});

// ---- the Google fallback, end to end, on the same instance ----

function cookiesFrom(response: Response): string {
  return response.headers
    .getSetCookie()
    .map((c) => c.split(";")[0])
    .join("; ");
}

function idToken(claims: Record<string, unknown>) {
  const now = Math.floor(Date.now() / 1000);
  return `${b64(JSON.stringify({ alg: "RS256" }))}.${b64(
    JSON.stringify({
      iss: "https://accounts.google.com",
      aud: GOOGLE_CLIENT_ID,
      exp: now + 600,
      email: "partner@betamobility.io",
      email_verified: true,
      hd: "betamobility.io",
      ...claims,
    }),
  )}.sig`;
}

async function startAuthorize(clientId = CLAUDE_CLIENT, redirectUri = CLAUDE_CALLBACK) {
  const verifier = "v".repeat(50);
  const challenge = b64(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
  const params = new URLSearchParams({
    response_type: "code",
    client_id: clientId,
    redirect_uri: redirectUri,
    state: "client-state",
    code_challenge: challenge,
    code_challenge_method: "S256",
    resource: RESOURCE,
  });
  const path = `/authorize?${params}`;
  return { verifier, path, response: await worker(path) };
}

async function fallbackToCallback(token: string) {
  googleIdToken = token;
  const { verifier, response, path } = await startAuthorize();
  expect(response.status).toBe(200);
  const html = await response.text();
  const handle = /name="handle" value="([^"]+)"/.exec(html)![1];
  const consentCookies = cookiesFrom(response);
  const approve = await worker(path, { // the form posts back to the same URL
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded", Cookie: consentCookies },
    body: new URLSearchParams({ handle, decision: "approve" }),
  });
  expect(approve.status).toBe(302);
  const google = new URL(approve.headers.get("Location")!);
  expect(google.origin + google.pathname).toBe("https://accounts.google.com/o/oauth2/v2/auth");
  expect(google.searchParams.get("hd")).toBe("betamobility.io");
  expect(google.searchParams.get("redirect_uri")).toBe(`${ORIGIN}/auth/callback`);
  const callback = await worker(`/auth/callback?code=google-code&state=${google.searchParams.get("state")}`, {
    headers: { Cookie: [consentCookies, cookiesFrom(approve)].filter(Boolean).join("; ") },
  });
  return { verifier, callback };
}

describe("Google fallback", () => {
  it("one Google sign-in yields a token with a refresh token, and whoami says google", async () => {
    const { verifier, callback } = await fallbackToCallback(idToken({}));
    expect(callback.status).toBe(302);
    const redirect = new URL(callback.headers.get("Location")!);
    expect(redirect.origin + redirect.pathname).toBe(CLAUDE_CALLBACK);
    expect(redirect.searchParams.get("state")).toBe("client-state");
    const code = redirect.searchParams.get("code")!;
    expect(code).toBeTruthy();
    const token = await worker("/oauth/token", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        grant_type: "authorization_code",
        code,
        client_id: CLAUDE_CLIENT,
        redirect_uri: CLAUDE_CALLBACK,
        code_verifier: verifier,
        resource: RESOURCE,
      }),
    });
    expect(token.status).toBe(200);
    const body = (await token.json()) as Record<string, any>;
    expect(body.refresh_token).toBeTruthy();
    expect(body.expires_in).toBe(3600);
    expect(await whoami(body.access_token)).toEqual({ email: "partner@betamobility.io", source: "google" });
  });

  it("a Google account outside the domain gets access_denied and no code", async () => {
    const { callback } = await fallbackToCallback(idToken({ email: "someone@gmail.com", hd: undefined }));
    expect(callback.status).toBe(302);
    const redirect = new URL(callback.headers.get("Location")!);
    expect(redirect.searchParams.get("error")).toBe("access_denied");
    expect(redirect.searchParams.get("code")).toBeNull();
  });

  it("a CIMD client with a redirect URI outside Claude's set is refused", async () => {
    const evil = "https://evil.example/client.json";
    cimdDocs[evil] = {
      client_id: evil,
      client_name: "Evil",
      redirect_uris: ["https://evil.example/cb"],
      token_endpoint_auth_method: "none",
    };
    const { response } = await startAuthorize(evil, "https://evil.example/cb");
    expect(response.status).toBe(400);
    expect(response.headers.get("Location")).toBeNull();
    expect(response.headers.getSetCookie()).toEqual([]);
  });
});

describe("verifyGoogleIdToken", () => {
  const expected = { clientId: GOOGLE_CLIENT_ID, allowedDomain: "betamobility.io" };
  const reason = (token: string) => {
    try {
      verifyGoogleIdToken(token, expected);
    } catch (error) {
      expect(error).toBeInstanceOf(GoogleIdentityError);
      return (error as Error).message;
    }
    return "accepted";
  };

  it("accepts a verified Beta account and lower-cases it", () => {
    expect(verifyGoogleIdToken(idToken({ email: "Partner@Betamobility.io" }), expected)).toBe("partner@betamobility.io");
  });
  it("refuses unverified email, wrong or missing hd, foreign domain, wrong audience, expiry, wrong issuer", () => {
    expect(reason(idToken({ email_verified: false }))).toBe("email_not_verified");
    expect(reason(idToken({ email_verified: "true" }))).toBe("email_not_verified");
    expect(reason(idToken({ hd: "example.com" }))).toBe("wrong_hosted_domain");
    expect(reason(idToken({ hd: undefined }))).toBe("wrong_hosted_domain");
    expect(reason(idToken({ email: "x@gmail.com" }))).toBe("email_outside_domain");
    expect(reason(idToken({ email: "x@betamobility.io.evil.com" }))).toBe("email_outside_domain");
    expect(reason(idToken({ aud: "someone-else" }))).toBe("wrong_audience");
    expect(reason(idToken({ exp: 1 }))).toBe("expired");
    expect(reason(idToken({ iss: "https://evil.example" }))).toBe("wrong_issuer");
    expect(reason("not-a-jwt")).toBe("malformed_id_token");
  });
});

describe("allow lists", () => {
  it("accepts Claude's callbacks and loopback only", () => {
    for (const ok of [
      "https://claude.ai/api/mcp/auth_callback",
      "https://claude.com/api/mcp/auth_callback",
      "http://localhost:33418/callback",
      "http://127.0.0.1:8080/cb",
    ]) {
      expect(isAllowedRedirectUri(ok), ok).toBe(true);
    }
    for (const bad of [
      "https://claude.ai/other",
      "https://claude.ai.evil.com/api/mcp/auth_callback",
      "http://claude.ai/api/mcp/auth_callback",
      "https://evil.example/cb",
      "http://localhost.evil.com/cb",
      "https://localhost:8080/cb",
      "javascript:alert(1)",
      "not a url",
    ]) {
      expect(isAllowedRedirectUri(bad), bad).toBe(false);
    }
  });
  it("accepts exactly one @ and the exact domain", () => {
    expect(isAllowedEmail("a@betamobility.io", "betamobility.io")).toBe(true);
    expect(isAllowedEmail("a@b@betamobility.io", "betamobility.io")).toBe(false);
    expect(isAllowedEmail("@betamobility.io", "betamobility.io")).toBe(false);
    expect(isAllowedEmail(undefined, "betamobility.io")).toBe(false);
  });
});
