"""Starlette and entrypoint helpers for dual-transport FastMCP servers."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable

import structlog
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from beta_mcp_core.auth import build_auth_provider
from beta_mcp_core.settings import McpBaseSettings

log = structlog.get_logger()


def build_asgi_app(
    mcp,
    *,
    mcp_path: str = "/mcp",
    auth_provider=None,
    service_name: str = "mcp",
    extra_routes: Iterable = (),
    json_response: bool | None = None,
    stateless_http: bool | None = None,
) -> Starlette:
    """Build a Starlette app with `/health`, OAuth discovery, and MCP mount."""

    async def health(request):
        return JSONResponse({"status": "ok", "service": service_name})

    mcp_app = mcp.http_app(
        transport="streamable-http",
        path=mcp_path,
        json_response=json_response,
        stateless_http=stateless_http,
    )
    well_known_routes = (
        auth_provider.get_well_known_routes(mcp_path=mcp_path) if auth_provider else []
    )

    return Starlette(
        routes=[
            Route("/health", health),
            *well_known_routes,
            *extra_routes,
            Mount("/", app=mcp_app),
        ],
        lifespan=getattr(mcp_app, "lifespan", None),
    )


def remote_requested(argv: list[str] | None = None) -> bool:
    argv = sys.argv if argv is None else argv
    return "--remote" in argv or os.environ.get("MCP_REMOTE") == "1"


def run(
    mcp,
    *,
    settings: McpBaseSettings,
    mcp_path: str = "/mcp",
    service_name: str = "mcp",
    remote: bool | None = None,
) -> None:
    """Run FastMCP in stdio locally or HTTP remotely."""

    if remote is None:
        remote = remote_requested()

    if remote:
        import uvicorn

        auth_provider = build_auth_provider(settings, remote=True)
        app = build_asgi_app(
            mcp,
            mcp_path=mcp_path,
            auth_provider=auth_provider,
            service_name=service_name,
        )
        port = int(os.environ.get("PORT", settings.port))
        log.info("mcp_starting", mode="remote", service=service_name, port=port, path=mcp_path)
        uvicorn.run(app, host="0.0.0.0", port=port)
    else:
        log.info("mcp_starting", mode="stdio", service=service_name)
        mcp.run(transport="stdio")
