from __future__ import annotations

from beta_mcp_core.db import parse_pooler_dsn


def test_parse_pooler_dsn_preserves_supabase_project_ref_username():
    parsed = parse_pooler_dsn(
        "postgresql://postgres.abc123:p%40ss@aws-1-eu-central-1.pooler.supabase.com:6543/postgres"
    )
    assert parsed["user"] == "postgres.abc123"
    assert parsed["password"] == "p@ss"
    assert parsed["host"] == "aws-1-eu-central-1.pooler.supabase.com"
    assert parsed["port"] == 6543
    assert parsed["database"] == "postgres"


def test_parse_pooler_dsn_defaults_port_and_database():
    parsed = parse_pooler_dsn("postgresql://postgres.abc:pw@example.com")
    assert parsed["port"] == 6543
    assert parsed["database"] == "postgres"

