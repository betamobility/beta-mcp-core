# Gateway identity spike

Throwaway code for unit U4 of `beta-infra-mcp-gateway/docs/plans/2026-11-26-1156-feat-mcp-gateway-single-sign-in-plan.md`.
It is not part of the package. Its folk half lives on folk's branch `spike/gateway-identity`
(never merged).

## What is proven

A FastMCP gateway can sit in front of two backends and tell each of them who the caller is,
without a shared service key (KTD5):

- Tools of both backends are listed under `<prefix>_<tool>` (R3).
- For every forwarded call the gateway opens a fresh backend session, signs a 30-second ES256
  statement for that caller and that backend's audience, and sends it in
  `X-Gateway-Statement` with the gateway credential as the bearer.
- The backend verifies the credential, the signature, the audience and the expiry, and returns
  the address it verified. A call for A reaches the backend as A; twelve overlapping calls from
  A and B across both backends each arrive with their own address; a statement for backend one
  is refused by backend two.
- Folk's side (per-person log, 24-hour differencing, project member list: R8, R9, AE1, AE2) is
  proven in folk's own suite on the spike branch, not here.

Not proven: a real network hop, a real sign-in at the gateway, key rotation, replay protection.

## Header and claim layout (the same in folk's spike door)

    Authorization: Bearer fgw_<43 base64url characters>    the gateway credential
    X-Gateway-Statement: <ES256 JWT>                        header {alg: ES256, kid}

Claims: `email`, `aud` (the target backend's audience), `iat`, `exp` (at most 60 s after
`iat`), `jti`. Backends hold public keys by `kid`; only the gateway holds the private key.

## Spike-only parts

The caller comes from an inbound `X-Spike-User` header. It proves nothing. A real gateway takes
the address from its own verified sign-in. Never ship that header.

## Run

    uv run pytest spikes/gateway-identity-test

The repo's own `uv run pytest` does not collect it (`testpaths = ["tests"]`).

## Findings

FastMCP 4.1.6 (the version `uv.lock` pins; `pyproject.toml` allows `>=3.4.11,<5`). API used,
read from the installed package:

- `fastmcp.server.providers.proxy.ProxyProvider(client_factory, cache_ttl=0)` added with
  `gateway.add_provider(provider, namespace="one")`. Namespacing gives `one_whoami`. This is
  used instead of `create_proxy`/`mount`, because the factory is where per-call headers go.
- The factory is called for each backend session: once per forwarded tool call and once per
  listing. It runs inside the inbound request's context, so `get_http_headers()` there sees
  the inbound request. That is how the statement is bound to the caller of that call.
- `ProxyClient(transport, forward_incoming_headers=False)`: by default the proxy forwards
  eligible inbound headers (including `Authorization`) to HTTP backends. Switched off here so
  nothing of the inbound request rides along; the statement and credential are the only
  identity headers the backend sees.
- Caveats:
  1. `ProxyProvider` caches component lists across all callers for 300 s by default. A
     backend whose listing depends on the caller would leak it. The spike sets `cache_ttl=0`;
     the real gateway must decide per backend.
  2. Default fresh-session-per-call is what makes per-call headers safe. A shared/reused
     backend session pins the first request's headers (the FastMCP proxy docs warn of this);
     never pass a connected `Client` as the target.
  3. A failing factory (here: no caller) is swallowed by the aggregate provider's default
     error strategy `"warn"`: the client sees `Unknown tool`, and the real reason appears
     only in the gateway's warning log. Fails closed, but it makes the failure hard to read.
  4. fastmcp 4.x uses `httpx2`, not `httpx`. A custom `httpx_client_factory` must return an
     `httpx2.AsyncClient`, or the transport rejects its `auth` object ("Invalid auth
     argument"). `httpx2` arrives only as a transitive dependency of fastmcp.
  5. The same statement header is reused by every request of one backend session
     (initialize, list, call). A server-side one-time `jti` check would therefore refuse the
     second request of a session. Replay protection has to rest on the 60 s lifetime and TLS,
     or on a per-statement session.
  6. The signing helper is duplicated here, not imported from folk.

## Removing it

Delete `spikes/gateway-identity-test/`. Nothing else in beta-mcp-core imports it, and it adds
no dependency.
