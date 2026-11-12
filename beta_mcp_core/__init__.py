"""Shared fail-closed MCP plumbing for Beta Mobility servers."""

from beta_mcp_core.app import build_asgi_app, run
from beta_mcp_core.auth import (
    ALLOWED_CLIENT_REDIRECT_URIS,
    ALLOWED_DOMAIN,
    DomainGuardGoogleProvider,
    build_auth_provider,
    claims_allowed,
    resolve_identity,
)
from beta_mcp_core.branding import BETA_ICON_URL, BETA_WEBSITE_URL, apply_beta_branding, beta_icons
from beta_mcp_core.cache import ttl_cache
from beta_mcp_core.db import close_pool, create_pool, parse_pooler_dsn
from beta_mcp_core.identity import get_caller
from beta_mcp_core.paths import McpPathAliasMiddleware, normalize_mcp_paths
from beta_mcp_core.settings import McpBaseSettings

__all__ = [
    "ALLOWED_CLIENT_REDIRECT_URIS",
    "ALLOWED_DOMAIN",
    "BETA_ICON_URL",
    "BETA_WEBSITE_URL",
    "DomainGuardGoogleProvider",
    "McpBaseSettings",
    "McpPathAliasMiddleware",
    "apply_beta_branding",
    "beta_icons",
    "build_asgi_app",
    "build_auth_provider",
    "claims_allowed",
    "close_pool",
    "create_pool",
    "get_caller",
    "normalize_mcp_paths",
    "parse_pooler_dsn",
    "resolve_identity",
    "run",
    "ttl_cache",
]

__version__ = "0.5.0"
