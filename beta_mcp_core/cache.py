"""Cache helpers shared by small MCP proxy servers."""

from __future__ import annotations

from cachetools import TTLCache


def ttl_cache(*, maxsize: int = 1024, ttl: int = 300) -> TTLCache:
    return TTLCache(maxsize=maxsize, ttl=ttl)

