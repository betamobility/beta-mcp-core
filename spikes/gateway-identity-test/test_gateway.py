"""SPIKE: the gateway against two in-process stub backends. No network, no real keys."""

import asyncio
import hashlib
import secrets
from contextlib import AsyncExitStack, asynccontextmanager

import httpx2 as httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.dependencies import get_http_headers

from gateway import SPIKE_USER_HEADER, STATEMENT_HEADER, Backend, build_gateway, sign_statement

KID = "gw-test"
CREDENTIAL = "fgw_" + secrets.token_urlsafe(32)
A = "anna@betamobility.io"
B = "bo@betamobility.io"
GATEWAY_KEY = ec.generate_private_key(ec.SECP256R1())


def stub_backend(audience: str, name: str) -> FastMCP:
    """Verifies the credential and the statement as a real backend would, and returns the
    address it verified."""
    server = FastMCP(name)
    digest = hashlib.sha256(CREDENTIAL.encode()).hexdigest()
    public = GATEWAY_KEY.public_key()

    def verified_email() -> str:
        headers = get_http_headers(include={"authorization"})
        bearer = headers.get("authorization", "").removeprefix("Bearer ")
        if hashlib.sha256(bearer.encode()).hexdigest() != digest:
            raise ValueError("refused: no gateway credential")
        claims = jwt.decode(
            headers.get(STATEMENT_HEADER, ""),
            public,
            algorithms=["ES256"],
            audience=audience,
            options={"require": ["email", "aud", "iat", "exp", "jti"]},
        )
        return claims["email"]

    @server.tool
    def whoami() -> str:
        return verified_email()

    @server.tool
    async def slow_whoami() -> str:
        await asyncio.sleep(0.05)  # lets interleaved calls overlap
        return verified_email()

    return server


@asynccontextmanager
async def serve(server: FastMCP):
    app = server.http_app(path="/mcp")
    async with app.router.lifespan_context(app):
        yield app


def in_process(app):
    def factory(**kwargs):
        kwargs.pop("transport", None)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), **kwargs)

    return factory


@asynccontextmanager
async def stack():
    """Two backends and the gateway, all in one process. Yields a function that opens a
    client to the gateway as a given caller."""
    async with AsyncExitStack() as stk:
        one = await stk.enter_async_context(serve(stub_backend("backend-one", "one")))
        two = await stk.enter_async_context(serve(stub_backend("backend-two", "two")))
        routes = {"http://one.test/mcp": one, "http://two.test/mcp": two}

        def to_backend(**kwargs):
            kwargs.pop("transport", None)
            return httpx.AsyncClient(transport=_Route(routes), **kwargs)

        gateway = build_gateway(
            [
                Backend("one", "http://one.test/mcp", "backend-one"),
                Backend("two", "http://two.test/mcp", "backend-two"),
            ],
            GATEWAY_KEY,
            KID,
            CREDENTIAL,
            httpx_client_factory=to_backend,
        )
        front = await stk.enter_async_context(serve(gateway))

        def as_caller(email):
            transport = StreamableHttpTransport(
                "http://gateway.test/mcp",
                headers={SPIKE_USER_HEADER: email},
                httpx_client_factory=in_process(front),
            )
            return Client(transport)

        yield as_caller


class _Route(httpx.AsyncBaseTransport):
    """Sends a request to the in-process ASGI app that owns its host."""

    def __init__(self, routes):
        self._by_host = {
            httpx.URL(url).host: httpx.ASGITransport(app=app) for url, app in routes.items()
        }

    async def handle_async_request(self, request):
        return await self._by_host[request.url.host].handle_async_request(request)


def text(result) -> str:
    return result.content[0].text


async def test_tools_of_both_backends_are_listed_with_prefixes():
    async with stack() as as_caller:
        async with as_caller(A) as client:
            names = {tool.name for tool in await client.list_tools()}
    assert names == {"one_whoami", "one_slow_whoami", "two_whoami", "two_slow_whoami"}


async def test_a_call_for_a_reaches_the_backend_with_a_s_email():
    async with stack() as as_caller:
        async with as_caller(A) as client:
            assert text(await client.call_tool("one_whoami", {})) == A
            assert text(await client.call_tool("two_whoami", {})) == A


async def test_interleaved_calls_each_reach_the_backend_with_their_own_email():
    async with stack() as as_caller:

        async def one_call(email, tool):
            async with as_caller(email) as client:
                return email, text(await client.call_tool(tool, {}))

        # Same gateway process, overlapping in time, both backends.
        jobs = [
            one_call(A if i % 2 == 0 else B, f"{'one' if i % 3 else 'two'}_slow_whoami")
            for i in range(12)
        ]
        results = await asyncio.gather(*jobs)
    assert all(asked == verified for asked, verified in results)
    assert {asked for asked, _ in results} == {A, B}


async def test_one_client_session_switching_nothing_keeps_its_own_identity_per_call():
    """Two calls on one front session, then another caller's: the backend never sees a stale
    identity, because each forwarded call opens a fresh backend session."""
    async with stack() as as_caller:
        async with as_caller(A) as a, as_caller(B) as b:
            first = text(await a.call_tool("one_whoami", {}))
            other = text(await b.call_tool("one_whoami", {}))
            again = text(await a.call_tool("one_whoami", {}))
    assert (first, other, again) == (A, B, A)


async def test_a_statement_for_backend_one_is_refused_by_backend_two():
    async with stack():
        pass
    # Replay the statement the gateway would send to backend one against backend two.
    two = stub_backend("backend-two", "two")
    statement = sign_statement(GATEWAY_KEY, KID, A, "backend-one")
    async with serve(two) as app:
        transport = StreamableHttpTransport(
            "http://two.test/mcp",
            auth=CREDENTIAL,
            headers={STATEMENT_HEADER: statement},
            httpx_client_factory=in_process(app),
        )
        async with Client(transport) as client:
            with pytest.raises(Exception, match="audience|Audience|Invalid"):
                await client.call_tool("whoami", {})


async def test_a_call_without_a_caller_is_refused_by_the_gateway():
    async with stack() as as_caller:
        async with as_caller("") as client:
            with pytest.raises(Exception, match="Unknown tool"):
                await client.call_tool("one_whoami", {})
