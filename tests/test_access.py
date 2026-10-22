"""Synthetic signatures over documented Access claim shapes; never live credentials."""
import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from beta_mcp_core.access import AccessConfig, AccessMiddleware, AccessVerifier, JWKSCache

TEAM = "test.cloudflareaccess.com"
AUD = "production-audience"


@pytest.fixture
def keys():
    return [rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(2)]


def assertion(key, kid="one", **overrides):
    claims = dict(iss=f"https://{TEAM}", aud=[AUD], sub="human-id",
                  email="person@betamobility.io", iat=int(time.time()), exp=int(time.time())+300)
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


def jwk(key, kid):
    return dict(json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())), kid=kid)


def verifier(keys):
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"keys": [jwk(keys[0], "one")]})))
    return AccessVerifier(AccessConfig(TEAM, AUD), cache=JWKSCache(client=client))


def app(keys):
    async def identity(request):
        return JSONResponse({"caller": request.state.access_identity.actor_id})
    async def health(request):
        return JSONResponse({"ok": True})
    inner = Starlette(routes=[Route("/mcp", identity), Route("/health", health)])
    return AccessMiddleware(inner, verifier=verifier(keys), allowed_domain="betamobility.io",
                            service_tokens=("machine.access",))


@pytest.mark.parametrize("path", ["/mcp", "/health/", "/health-more", "/%2e%2e/health"])
def test_missing_assertion_denied_except_exact_health(keys, path):
    response = TestClient(app(keys)).get(path, follow_redirects=False)
    assert response.status_code == 401
    assert response.json() == {"error": "Unauthorized"}
    assert TestClient(app(keys)).get("/health").status_code == 200


def test_human_and_machine_identity(keys):
    client = TestClient(app(keys))
    human = client.get("/mcp", headers={"Cf-Access-Jwt-Assertion": assertion(keys[0])})
    machine = client.get("/mcp", headers={"Cf-Access-Jwt-Assertion": assertion(
        keys[0], sub="", email=None, common_name="machine.access", aud=AUD)})
    assert human.json() == {"caller": "person@betamobility.io"}
    assert machine.json() == {"caller": "machine.access"}


@pytest.mark.parametrize("overrides", [
    {"aud": "staging-audience"}, {"iss": "https://other.cloudflareaccess.com"},
    {"exp": 1}, {"email": "person@evilbetamobility.io"},
    {"email": None, "sub": "", "common_name": "unlisted.access"},
    {"email": None, "sub": ""},
])
def test_invalid_claims_have_constant_denial(keys, overrides):
    response = TestClient(app(keys)).get("/mcp", headers={
        "Cf-Access-Jwt-Assertion": assertion(keys[0], **overrides)})
    assert response.status_code == 401
    assert response.json() == {"error": "Unauthorized"}


def test_unknown_key_and_wrong_signature_denied(keys):
    client = TestClient(app(keys))
    for token in [assertion(keys[0], kid="missing"), assertion(keys[1])]:
        assert client.get("/mcp", headers={"Cf-Access-Jwt-Assertion": token}).status_code == 401


@pytest.mark.asyncio
async def test_rotated_key_refreshes_once(keys):
    reads = []
    def fetch(request):
        reads.append(request.url)
        index = 0 if len(reads) == 1 else 1
        return httpx.Response(200, json={"keys": [jwk(keys[index], ["one", "two"][index])]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        check = AccessVerifier(AccessConfig(TEAM, AUD), cache=JWKSCache(client=client))
        await check.verify(assertion(keys[0]))
        assert (await check.verify(assertion(keys[1], kid="two"))).email == "person@betamobility.io"
        await check.verify(assertion(keys[1], kid="two"))
    assert len(reads) == 2

@pytest.mark.asyncio
@pytest.mark.parametrize("machine", [False, True])
async def test_real_fastmcp_request_preserves_verified_caller(keys, monkeypatch, machine):
    from fastmcp import Client, FastMCP
    from fastmcp.client.transports import StreamableHttpTransport
    from beta_mcp_core.app import build_asgi_app
    from beta_mcp_core.identity import get_caller
    from beta_mcp_core.settings import McpBaseSettings
    import beta_mcp_core.app as app_module

    monkeypatch.setattr(app_module, "AccessVerifier", lambda config: verifier(keys))
    mcp = FastMCP("access-test")
    @mcp.tool
    def whoami() -> str:
        return get_caller()
    async def extra(request):
        return JSONResponse({"protected": True})
    application = build_asgi_app(mcp, settings=McpBaseSettings(
        cf_access_team_domain=TEAM, cf_access_aud=AUD,
        cf_access_service_tokens=("machine.access",)),
        extra_routes=[Route("/extra", extra)])
    overrides = dict(sub="", email=None, common_name="machine.access", aud=AUD) if machine else {}
    headers = {"Cf-Access-Jwt-Assertion": assertion(keys[0], **overrides),
               "Authorization": "Bearer opaque-client-token-ignored"}
    def factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=application), **kwargs)
    async with application.router.lifespan_context(application):
        async with Client(StreamableHttpTransport("http://test/mcp", headers=headers,
                                                  httpx_client_factory=factory)) as client:
            result = await client.call_tool("whoami")
            assert result.data == ("machine.access" if machine else "person@betamobility.io")
        async with factory(base_url="http://test") as http:
            assert (await http.get("/extra")).status_code == 401
            assert (await http.get("/extra", headers=headers)).status_code == 200

@pytest.mark.asyncio
async def test_concurrent_jwks_outage_shares_denial_and_recovers(keys):
    import asyncio
    from beta_mcp_core.access import AccessAuthError
    calls = []
    healthy = False
    async def fetch(request):
        calls.append(request.url)
        await asyncio.sleep(0.001)
        if not healthy:
            raise httpx.ReadTimeout("synthetic outage")
        return httpx.Response(200, json={"keys": [jwk(keys[0], "one")]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        check = AccessVerifier(AccessConfig(TEAM, AUD), cache=JWKSCache(client=client))
        results = await asyncio.gather(*(check.verify(assertion(keys[0])) for _ in range(10)),
                                       return_exceptions=True)
        assert all(isinstance(result, AccessAuthError) for result in results)
        assert len(calls) == 1
        healthy = True
        await asyncio.sleep(5.01)
        assert (await check.verify(assertion(keys[0]))).actor_id == "person@betamobility.io"
        assert len(calls) == 2


@pytest.mark.parametrize("failure", ["timeout", "http_error", "invalid_json", "invalid_keys", "oversized"])
def test_jwks_failures_deny_without_calling_handler(keys, failure):
    reached = []
    def fetch(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic outage")
        if failure == "http_error":
            return httpx.Response(503)
        if failure == "invalid_json":
            return httpx.Response(200, content=b"not json")
        if failure == "invalid_keys":
            return httpx.Response(200, json={"keys": []})
        return httpx.Response(200, content=b" " * 256001)
    async def protected(request):
        reached.append(True)
        return JSONResponse({"wrong": True})
    client = httpx.AsyncClient(transport=httpx.MockTransport(fetch))
    check = AccessVerifier(AccessConfig(TEAM, AUD), cache=JWKSCache(client=client))
    application = AccessMiddleware(Starlette(routes=[Route("/mcp", protected)]),
                                   verifier=check, allowed_domain="betamobility.io")
    response = TestClient(application).get("/mcp", headers={
        "Cf-Access-Jwt-Assertion": assertion(keys[0])})
    assert response.status_code == 401
    assert response.json() == {"error": "Unauthorized"}
    assert reached == []

@pytest.mark.asyncio
async def test_google_mode_from_env_still_serves_authenticated_tools(monkeypatch, tmp_path):
    import fastmcp
    from fastmcp import Client, FastMCP
    from fastmcp.client.transports import StreamableHttpTransport
    from beta_mcp_core.app import build_asgi_app
    from beta_mcp_core.auth import build_auth_provider
    from beta_mcp_core.identity import get_caller
    from beta_mcp_core.settings import McpBaseSettings

    monkeypatch.setattr(fastmcp.settings, "home", tmp_path)
    for name, value in {"GOOGLE_CLIENT_ID": "test-client", "GOOGLE_CLIENT_SECRET": "test-secret",
                        "JWT_SIGNING_KEY": "a" * 64, "MCP_SERVICE_TOKEN": "test-machine"}.items():
        monkeypatch.setenv(name, value)
    settings = McpBaseSettings()
    provider = build_auth_provider(settings)
    mcp = FastMCP("google-test", auth=provider)
    @mcp.tool
    def whoami() -> str:
        return get_caller()
    application = build_asgi_app(mcp, auth_provider=provider, settings=settings)
    def factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=application), **kwargs)
    async with application.router.lifespan_context(application):
        async with Client(StreamableHttpTransport("http://test/mcp",
                headers={"Authorization": "Bearer test-machine"},
                httpx_client_factory=factory)) as client:
            assert (await client.call_tool("whoami")).data == "mcp-service"
