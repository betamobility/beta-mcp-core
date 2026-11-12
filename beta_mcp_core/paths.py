"""Serve one MCP endpoint under its canonical path, its old paths and a trailing slash."""

from __future__ import annotations

from collections.abc import Iterable


def _aliases(mcp_path: str, legacy_paths: Iterable[str]) -> frozenset[str]:
    if not mcp_path.startswith("/") or mcp_path == "/" or mcp_path.endswith("/"):
        raise ValueError("mcp_path must be an absolute path without a trailing slash")
    aliases = {f"{mcp_path}/"}
    for legacy in legacy_paths:
        trimmed = legacy.rstrip("/")
        if not legacy.startswith("/") or trimmed in ("", mcp_path):
            raise ValueError(f"invalid legacy MCP path: {legacy!r}")
        aliases.update({trimmed, f"{trimmed}/"})
    return frozenset(aliases)


class McpPathAliasMiddleware:
    """Rewrite aliases of the MCP endpoint to its canonical path, without a redirect.

    A request for `<mcp_path>/`, a legacy path, or a legacy path with a trailing slash is
    handed on as a request for `mcp_path`. Without this, Starlette answers the slash with a
    307 to an `http://` URL behind Cloud Run, which an MCP client cannot follow. Only the
    exact paths are rewritten; nothing below them is. Put it outermost, so auth middleware
    sees the canonical path.
    """

    def __init__(self, app, *, mcp_path: str = "/mcp", legacy_paths: Iterable[str] = ()):
        self.app = app
        self.mcp_path = mcp_path
        self._raw = mcp_path.encode()
        self._aliases = _aliases(mcp_path, legacy_paths)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") in self._aliases:
            scope = {**scope, "path": self.mcp_path, "raw_path": self._raw}
        await self.app(scope, receive, send)


def normalize_mcp_paths(app, *, mcp_path: str = "/mcp", legacy_paths: Iterable[str] = ()):
    """Wrap an ASGI app a server built itself (instead of `build_asgi_app`)."""
    return McpPathAliasMiddleware(app, mcp_path=mcp_path, legacy_paths=legacy_paths)
