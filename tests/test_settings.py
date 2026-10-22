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


@pytest.mark.parametrize("extra", [
    {"cf_access_aud": "one,two"}, {"cf_access_aud": '["one", "two"]'},
    {"cf_access_aud": "one two"}, {"cf_access_team_domain": "attacker.example"},
    {"google_client_id": "client", "google_client_secret": "secret", "jwt_signing_key": "x"*64},
    {"mcp_service_token": "old-token"},
])
def test_access_rejects_ambiguous_or_mixed_configuration(extra):
    values = dict(cf_access_team_domain="test.cloudflareaccess.com", cf_access_aud="one")
    values.update(extra)
    with pytest.raises(ValidationError):
        McpBaseSettings(**values)


def test_access_env_without_google(monkeypatch):
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "test.cloudflareaccess.com")
    monkeypatch.setenv("CF_ACCESS_AUD", "one")
    settings = McpBaseSettings()
    assert settings.access_enabled
    assert settings.auth_enabled


def test_partial_access_rejected():
    with pytest.raises(ValidationError):
        McpBaseSettings(cf_access_team_domain="test.cloudflareaccess.com")

@pytest.mark.parametrize("field", ["google_client_secret", "mcp_service_token"])
def test_mixed_mode_error_does_not_print_credential_values(field):
    with pytest.raises(ValidationError) as caught:
        McpBaseSettings(cf_access_team_domain="test.cloudflareaccess.com", cf_access_aud="one",
                        **{field: "TEST_ONLY_SECRET"})
    assert "mutually exclusive" in str(caught.value)
    assert "TEST_ONLY_SECRET" not in str(caught.value)
