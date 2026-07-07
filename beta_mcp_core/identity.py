"""Caller identity helpers for FastMCP tools."""

from __future__ import annotations

import getpass

from fastmcp.server.dependencies import get_access_token


def get_caller(*, local_prefix: str = "local") -> str:
    """Return the verified FastMCP caller, with a local stdio fallback."""

    try:
        token = get_access_token()
    except Exception:
        return f"{local_prefix}:{getpass.getuser()}"
    client_id = getattr(token, "client_id", None)
    if client_id:
        return str(client_id)
    return f"{local_prefix}:{getpass.getuser()}"

