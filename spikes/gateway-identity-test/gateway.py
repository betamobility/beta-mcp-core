"""SPIKE: a FastMCP gateway that tells each backend who the caller is.

Spike only, removed with the folder. See README.md.

For every call (and every tool listing) the gateway opens a fresh backend session, signs an
ES256 statement naming the caller's address and the target backend's audience, and sends the
statement in `X-Gateway-Statement` with the gateway's credential as the bearer. Nothing of the
inbound request's headers is forwarded (`forward_incoming_headers=False`).

SPIKE-ONLY inbound identity: the caller is whatever the inbound `X-Spike-User` header says.
A real gateway takes it from its own verified sign-in; this header proves nothing and must
never exist in a deployed gateway.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

import httpx2
import jwt
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey
from fastmcp import FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.providers.proxy import ProxyClient, ProxyProvider

STATEMENT_HEADER = "x-gateway-statement"
SPIKE_USER_HEADER = "x-spike-user"
LIFETIME_SECONDS = 30


@dataclass(frozen=True)
class Backend:
    prefix: str  # tool-name namespace: "<prefix>_<tool>"
    url: str  # the backend's /mcp address
    audience: str  # the `aud` its statements carry


def sign_statement(
    private_key: EllipticCurvePrivateKey,
    kid: str,
    email: str,
    audience: str,
    *,
    now: float | None = None,
    lifetime: int = LIFETIME_SECONDS,
) -> str:
    """Same layout as folk's spike door: header kid; claims email, aud, iat, exp, jti.
    (A copy of folk/server/gateway_signing.py so core depends on nothing of folk's.)"""
    iat = int(time.time() if now is None else now)
    claims = {
        "email": email,
        "aud": audience,
        "iat": iat,
        "exp": iat + lifetime,
        "jti": secrets.token_urlsafe(12),
    }
    return jwt.encode(claims, private_key, algorithm="ES256", headers={"kid": kid})


class PerRequestStatement(httpx2.Auth):
    """Adds the credential and a freshly signed statement to every HTTP request of a backend
    session. One MCP call is several HTTP requests (initialize, list, call) and a backend that
    refuses a repeated statement (folk does, by `jti`) needs a new one for each."""

    def __init__(self, private_key, kid, email, audience, credential):
        self._args = (private_key, kid, email, audience)
        self._credential = credential

    def auth_flow(self, request):
        request.headers["authorization"] = f"Bearer {self._credential}"
        request.headers[STATEMENT_HEADER] = sign_statement(*self._args)
        yield request


def spike_identity() -> str:
    """SPIKE ONLY: the inbound `X-Spike-User` header. Raises when there is none."""
    who = get_http_headers().get(SPIKE_USER_HEADER, "").strip().lower()
    if not who:
        raise PermissionError("no caller: the spike gateway needs an X-Spike-User header")
    return who


def build_gateway(
    backends: list[Backend],
    private_key: EllipticCurvePrivateKey,
    kid: str,
    credential: str,
    *,
    identity=spike_identity,
    httpx_client_factory=None,
) -> FastMCP:
    gateway = FastMCP("gateway-identity-spike")

    def provider_for(backend: Backend) -> ProxyProvider:
        def client_factory() -> ProxyClient:
            # Called once per backend session, so per call: headers are fixed at this moment
            # for this caller and this backend, and the client is dropped afterwards.
            email = identity()
            options = {"httpx_client_factory": httpx_client_factory} if httpx_client_factory else {}
            transport = StreamableHttpTransport(
                backend.url,
                auth=PerRequestStatement(private_key, kid, email, backend.audience, credential),
                **options,
            )
            return ProxyClient(transport, forward_incoming_headers=False)

        # cache_ttl=0: the default caches component lists across callers for 300 s, which
        # is wrong for a backend whose listing depends on who is asking.
        return ProxyProvider(client_factory, cache_ttl=0)

    for backend in backends:
        gateway.add_provider(provider_for(backend), namespace=backend.prefix)
    return gateway
