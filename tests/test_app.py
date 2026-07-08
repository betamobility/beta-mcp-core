from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from beta_mcp_core.app import build_asgi_app, run
from beta_mcp_core.settings import McpBaseSettings


class FakeMcp:
    def __init__(self):
        self.stdio_ran = False
        self.http_kwargs: dict[str, object] | None = None

    def http_app(
        self,
        *,
        transport: str,
        path: str,
        json_response: bool | None = None,
        stateless_http: bool | None = None,
    ):
        self.http_kwargs = {
            "transport": transport,
            "path": path,
            "json_response": json_response,
            "stateless_http": stateless_http,
        }

        async def ok(request):
            return JSONResponse({"transport": transport, "path": path})

        return Starlette(routes=[Route(path, ok)])

    def run(self, *, transport: str):
        self.stdio_ran = transport == "stdio"


class FakeProvider:
    def get_well_known_routes(self, *, mcp_path: str):
        async def discovery(request):
            return JSONResponse({"mcp_path": mcp_path})

        return [Route("/.well-known/test", discovery)]


def test_health_ok():
    app = build_asgi_app(FakeMcp(), service_name="test-mcp")
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "test-mcp"}


def test_well_known_routes_present():
    app = build_asgi_app(FakeMcp(), mcp_path="/radar", auth_provider=FakeProvider())
    response = TestClient(app).get("/.well-known/test")
    assert response.status_code == 200
    assert response.json() == {"mcp_path": "/radar"}


def test_http_options_pass_through():
    mcp = FakeMcp()
    build_asgi_app(
        mcp,
        mcp_path="/ssb",
        json_response=True,
        stateless_http=True,
    )
    assert mcp.http_kwargs == {
        "transport": "streamable-http",
        "path": "/ssb",
        "json_response": True,
        "stateless_http": True,
    }


def test_stdio_no_auth_required():
    mcp = FakeMcp()
    run(mcp, settings=McpBaseSettings(), remote=False)
    assert mcp.stdio_ran


def test_http_fails_closed_with_missing_credentials():
    with pytest.raises(SystemExit):
        run(FakeMcp(), settings=McpBaseSettings(), remote=True)
