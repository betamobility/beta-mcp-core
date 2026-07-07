from __future__ import annotations

from beta_mcp_core.identity import get_caller


def test_get_caller_falls_back_to_local_identity():
    caller = get_caller()
    assert caller.startswith("local:")

