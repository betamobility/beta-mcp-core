// A throwaway MCP server for one question: can Claude's connector sign in through
// Cloudflare Access managed OAuth? It has no auth of its own. It serves a request only when
// Cloudflare Access has put a valid, signed assertion on it, and its one tool says who the
// assertion names. Streamable HTTP, stateless, JSON responses only.

const PROTOCOL = "2025-06-18";
const SERVER = { name: "beta-access-test", version: "0.1.0" };
const TOOL = {
  name: "whoami",
  description:
    "Returns the email Cloudflare Access verified for this request, and when the check ran.",
  inputSchema: { type: "object", properties: {}, additionalProperties: false },
};

let cachedKeys = null;
let cachedAt = 0;

async function accessKeys(teamDomain) {
  if (cachedKeys && Date.now() - cachedAt < 10 * 60 * 1000) return cachedKeys;
  const response = await fetch(`https://${teamDomain}/cdn-cgi/access/certs`);
  if (!response.ok) throw new Error(`could not read the Access keys: ${response.status}`);
  cachedKeys = (await response.json()).keys;
  cachedAt = Date.now();
  return cachedKeys;
}

function decode(part) {
  const padded = part.replace(/-/g, "+").replace(/_/g, "/");
  return Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
}

// The claims of a valid Access assertion, or an error naming what failed.
async function verify(assertion, env) {
  const parts = (assertion || "").split(".");
  if (parts.length !== 3) throw new Error("no Access assertion on the request");
  const header = JSON.parse(new TextDecoder().decode(decode(parts[0])));
  const claims = JSON.parse(new TextDecoder().decode(decode(parts[1])));
  if (header.alg !== "RS256") throw new Error("assertion is not RS256");
  const jwk = (await accessKeys(env.ACCESS_TEAM_DOMAIN)).find((k) => k.kid === header.kid);
  if (!jwk) throw new Error("assertion is signed by an unknown key");
  const key = await crypto.subtle.importKey(
    "jwk",
    jwk,
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
    false,
    ["verify"],
  );
  const signed = new TextEncoder().encode(`${parts[0]}.${parts[1]}`);
  if (!(await crypto.subtle.verify("RSASSA-PKCS1-v1_5", key, decode(parts[2]), signed))) {
    throw new Error("assertion signature does not verify");
  }
  const audiences = [].concat(claims.aud || []);
  if (!audiences.includes(env.ACCESS_AUD)) throw new Error("assertion is for another application");
  if (claims.iss !== `https://${env.ACCESS_TEAM_DOMAIN}`) throw new Error("wrong issuer");
  if (!claims.exp || claims.exp * 1000 < Date.now()) throw new Error("assertion has expired");
  const email = String(claims.email || "").toLowerCase();
  if (!email.endsWith(`@${env.ALLOWED_DOMAIN}`)) throw new Error("email is outside the domain");
  return claims;
}

function reply(id, result) {
  return Response.json({ jsonrpc: "2.0", id, result });
}

function handle(message, claims) {
  const { id, method, params } = message;
  if (method === "initialize") {
    return reply(id, {
      protocolVersion: params?.protocolVersion || PROTOCOL,
      capabilities: { tools: {} },
      serverInfo: SERVER,
    });
  }
  if (method === "ping") return reply(id, {});
  if (method === "tools/list") return reply(id, { tools: [TOOL] });
  if (method === "tools/call" && params?.name === TOOL.name) {
    const text = JSON.stringify({
      email: claims.email,
      identity_type: claims.type || null,
      checked_at: new Date().toISOString(),
    });
    return reply(id, { content: [{ type: "text", text }] });
  }
  if (id === undefined) return new Response(null, { status: 202 }); // a notification
  return Response.json({ jsonrpc: "2.0", id, error: { code: -32601, message: "Method not found" } });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/health") return Response.json({ status: "ok", service: SERVER.name });
    if (url.pathname !== "/mcp") return new Response("Not found", { status: 404 });
    let claims;
    try {
      claims = await verify(request.headers.get("Cf-Access-Jwt-Assertion"), env);
    } catch (error) {
      // No user content is logged: the reason and nothing else.
      console.log(JSON.stringify({ event: "refused", reason: error.message }));
      return Response.json({ error: "unauthorized", reason: error.message }, { status: 401 });
    }
    if (request.method !== "POST") return new Response(null, { status: 405, headers: { Allow: "POST" } });
    const message = await request.json();
    console.log(JSON.stringify({ event: "served", method: message.method || "batch" }));
    return handle(message, claims);
  },
};
