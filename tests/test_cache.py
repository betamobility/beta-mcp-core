from __future__ import annotations

import time

from beta_mcp_core.cache import ttl_cache


def test_ttl_cache_expires_values():
    cache = ttl_cache(maxsize=10, ttl=0.01)
    cache["key"] = "value"
    assert cache["key"] == "value"
    time.sleep(0.02)
    assert "key" not in cache

