"""Canonical `/mcp`, legacy aliases, trailing slashes, and the Beta icon in the handshake."""

import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from beta_mcp_core.access import AccessMiddleware
from beta_mcp_core.app import build_asgi_app
from beta_mcp_core.branding import BETA_ICON_URL, BETA_WEBSITE_URL, apply_beta_branding
from beta_mcp_core.paths import McpPathAliasMiddleware, normalize_mcp_paths
from tests.test_access import assertion, keys, verifier  # noqa: F401  (fixture re-export)

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "t", "version": "1"}}}
HEADERS = {"Accept": "application/json, text/event-stream"}


def echo_app():
    async def where(request):
        return JSONResponse({"path": request.url.path})
    return Starlette(routes=[Route("/mcp", where, methods=["GET", "POST"]),
                             Route("/other", where), Route("/folio/sub", where)])


@pytest.mark.parametrize("path", ["/mcp", "/mcp/", "/folio", "/folio/"])
def test_aliases_reach_the_canonical_path_without_redirect(path):
    client = TestClient(normalize_mcp_paths(echo_app(), legacy_paths=["/folio"]))
    response = client.post(path, follow_redirects=False)
    assert response.status_code == 200
    assert response.json() == {"path": "/mcp"}


def test_only_exact_aliases_are_rewritten():
    client = TestClient(normalize_mcp_paths(echo_app(), legacy_paths=["/folio"]))
    assert client.get("/other").json() == {"path": "/other"}
    assert client.get("/folio/sub").json() == {"path": "/folio/sub"}
    assert client.get("/folioX", follow_redirects=False).status_code == 404


@pytest.mark.parametrize("bad", [{"mcp_path": "mcp"}, {"mcp_path": "/"}, {"mcp_path": "/mcp/"},
                                 {"legacy_paths": ["folio"]}, {"legacy_paths": ["/mcp"]},
                                 {"legacy_paths": ["/"]}])
def test_invalid_paths_refuse_to_build(bad):
    with pytest.raises(ValueError):
        McpPathAliasMiddleware(echo_app(), **bad)


@pytest.mark.parametrize("path", ["/mcp/", "/folio", "/folio/"])
def test_alias_does_not_bypass_access(keys, path):  # noqa: F811
    inner = AccessMiddleware(echo_app(), verifier=verifier(keys), allowed_domain="betamobility.io")
    client = TestClient(normalize_mcp_paths(inner, legacy_paths=["/folio"]))
    assert client.post(path, follow_redirects=False).status_code == 401
    signed = client.post(path, headers={"Cf-Access-Jwt-Assertion": assertion(keys[0])})
    assert signed.status_code == 200
    assert signed.json() == {"path": "/mcp"}


def test_branding_sets_icon_and_website_only_when_unset():
    plain = FastMCP("plain")
    apply_beta_branding(plain)
    assert [icon.src for icon in plain.icons] == [BETA_ICON_URL]
    assert plain.website_url == BETA_WEBSITE_URL

    from mcp.types import Icon
    own = FastMCP("own", icons=[Icon(src="https://example.org/i.png")], website_url="https://example.org")
    apply_beta_branding(own)
    assert [icon.src for icon in own.icons] == ["https://example.org/i.png"]
    assert own.website_url == "https://example.org"


def test_build_asgi_app_serves_legacy_and_slash_with_icon_in_handshake():
    mcp = FastMCP("fleet")

    @mcp.tool
    def ping() -> str:
        return "pong"

    app = build_asgi_app(mcp, legacy_paths=["/ssb"], stateless_http=True, json_response=True)
    with TestClient(app) as client:
        for path in ("/mcp", "/mcp/", "/ssb", "/ssb/"):
            response = client.post(path, json=INIT, headers=HEADERS, follow_redirects=False)
            assert response.status_code == 200, path
            info = response.json()["result"]["serverInfo"]
            assert info["icons"][0]["src"] == BETA_ICON_URL
            assert info["websiteUrl"] == BETA_WEBSITE_URL
        assert client.get("/health").status_code == 200


def test_branding_can_be_switched_off():
    mcp = FastMCP("plain")
    build_asgi_app(mcp, branding=False)
    assert mcp.icons == []


@pytest.mark.asyncio
async def test_real_client_lists_tools_through_a_legacy_alias():
    mcp = FastMCP("fleet")

    @mcp.tool
    def ping() -> str:
        return "pong"

    app = build_asgi_app(mcp, legacy_paths=["/dst"], stateless_http=True)
    import httpx
    transport = StreamableHttpTransport(
        "http://test/dst",
        httpx_client_factory=lambda **kw: httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), **{k: v for k, v in kw.items() if k != "transport"}),
    )
    async with app.router.lifespan_context(app):
        async with Client(transport) as client:
            assert [tool.name for tool in await client.list_tools()] == ["ping"]
