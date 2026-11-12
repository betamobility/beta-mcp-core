"""Starlette and entrypoint helpers for dual-transport FastMCP servers."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable

import structlog
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from beta_mcp_core.access import AccessConfig, AccessMiddleware, AccessVerifier
from beta_mcp_core.auth import build_auth_provider
from beta_mcp_core.branding import apply_beta_branding
from beta_mcp_core.paths import McpPathAliasMiddleware
from beta_mcp_core.settings import McpBaseSettings

log = structlog.get_logger()


def build_asgi_app(
    mcp,
    *,
    mcp_path: str = "/mcp",
    auth_provider=None,
    settings: McpBaseSettings | None = None,
    service_name: str = "mcp",
    extra_routes: Iterable = (),
    json_response: bool | None = None,
    stateless_http: bool | None = None,
    legacy_paths: Iterable[str] = (),
    branding: bool = True,
) -> Starlette:
    """Build a Starlette app with `/health`, OAuth discovery, and MCP mount.

    `legacy_paths` are old MCP paths that keep working as aliases of `mcp_path`; the
    trailing-slash form of every path is accepted too. `branding` sets the Beta icon and
    website in the MCP handshake where the server set none.
    """

    async def health(request):
        return JSONResponse({"status": "ok", "service": service_name})

    settings = settings or McpBaseSettings()
    if branding:
        apply_beta_branding(mcp)
    middleware = []
    if settings.access_enabled:
        if auth_provider is not None or getattr(mcp, "auth", None) is not None:
            raise ValueError("Access mode cannot run with a FastMCP auth provider")
        if stateless_http is False:
            raise ValueError("Access mode requires stateless_http=True")
        stateless_http = True
        middleware = [Middleware(
            AccessMiddleware,
            verifier=AccessVerifier(AccessConfig(settings.cf_access_team_domain, settings.cf_access_aud)),
            allowed_domain=settings.allowed_domain,
            service_tokens=settings.cf_access_service_tokens,
        )]

    mcp_app = mcp.http_app(
        **({"middleware": middleware} if middleware else {}),
        transport="streamable-http",
        path=mcp_path,
        json_response=json_response,
        stateless_http=stateless_http,
    )
    well_known_routes = (
        auth_provider.get_well_known_routes(mcp_path=mcp_path) if auth_provider else []
    )

    return Starlette(
        middleware=[
            Middleware(McpPathAliasMiddleware, mcp_path=mcp_path, legacy_paths=tuple(legacy_paths)),
            *middleware,
        ],
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
    legacy_paths: Iterable[str] = (),
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
            settings=settings,
            service_name=service_name,
            legacy_paths=legacy_paths,
        )
        port = int(os.environ.get("PORT", settings.port))
        log.info("mcp_starting", mode="remote", service=service_name, port=port, path=mcp_path)
        uvicorn.run(app, host="0.0.0.0", port=port)
    else:
        log.info("mcp_starting", mode="stdio", service=service_name)
        mcp.run(transport="stdio")
