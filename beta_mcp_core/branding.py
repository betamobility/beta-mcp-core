"""The Beta icon and website a Beta MCP server declares in its MCP handshake."""

from __future__ import annotations

from mcp import types as mcp_types

BETA_ICON_URL = "https://design.betamobility.ai/public/logo/favicon-32.png"
BETA_WEBSITE_URL = "https://betamobility.com"


def beta_icons() -> list[mcp_types.Icon]:
    return [mcp_types.Icon(src=BETA_ICON_URL, mimeType="image/png", sizes=["32x32"])]


def apply_beta_branding(mcp) -> None:
    """Set the Beta icon and website on a FastMCP server where the server set none.

    FastMCP 3 and 4 expose `icons` and `website_url` as read-only properties over the
    low-level server, so the values are written there. A server that passed its own
    icons or website to `FastMCP(...)` keeps them.
    """
    server = getattr(mcp, "_mcp_server", None)
    if server is None:
        return
    if not getattr(server, "icons", None):
        server.icons = beta_icons()
    if not getattr(server, "website_url", None):
        server.website_url = BETA_WEBSITE_URL
