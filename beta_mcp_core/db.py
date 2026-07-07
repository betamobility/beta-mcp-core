"""Shared asyncpg helpers for Supabase transaction-pooler MCP servers."""

from __future__ import annotations

from urllib.parse import unquote, urlparse

import asyncpg


def parse_pooler_dsn(dsn: str) -> dict[str, object]:
    """Parse a Supabase pooler DSN without losing dots in usernames.

    asyncpg's DSN parser can mishandle Supabase transaction-pooler usernames
    like `postgres.<project-ref>`, so MCP servers parse the URL manually before
    creating the pool.
    """

    parsed = urlparse(dsn)
    return {
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "host": parsed.hostname,
        "port": parsed.port or 6543,
        "database": (parsed.path or "/postgres").lstrip("/"),
    }


async def create_pool(
    dsn: str,
    *,
    min_size: int = 1,
    max_size: int = 5,
    **kwargs,
) -> asyncpg.Pool:
    """Create an asyncpg pool safe for the Supabase transaction pooler."""

    return await asyncpg.create_pool(
        **parse_pooler_dsn(dsn),
        min_size=min_size,
        max_size=max_size,
        statement_cache_size=0,
        **kwargs,
    )


async def close_pool(pool: asyncpg.Pool | None) -> None:
    if pool is not None:
        await pool.close()

