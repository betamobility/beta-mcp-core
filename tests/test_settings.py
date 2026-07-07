from __future__ import annotations

import pytest
from pydantic import ValidationError

from beta_mcp_core.settings import McpBaseSettings


def test_partial_auth_config_rejected():
    with pytest.raises(ValidationError, match="GOOGLE_CLIENT_SECRET"):
        McpBaseSettings(google_client_id="client")


def test_all_empty_auth_config_allowed_for_local_dev():
    settings = McpBaseSettings()
    assert settings.auth_enabled is False


def test_env_alias_mapping(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("JWT_SIGNING_KEY", "a" * 64)
    settings = McpBaseSettings()
    assert settings.google_client_id == "client"
    assert settings.auth_enabled is True

