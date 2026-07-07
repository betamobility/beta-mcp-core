from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastmcp.server.auth.providers.google import GoogleProvider
from mcp.server.auth.provider import AccessToken

from beta_mcp_core.auth import (
    DomainGuardGoogleProvider,
    build_auth_provider,
    claims_allowed,
    resolve_identity,
)
from beta_mcp_core.settings import McpBaseSettings


JWT_KEY = "a" * 64


def settings(**overrides) -> McpBaseSettings:
    data = {
        "google_client_id": "client",
        "google_client_secret": "secret",
        "jwt_signing_key": JWT_KEY,
        "mcp_base_url": "https://example.com",
    }
    data.update(overrides)
    return McpBaseSettings(**data)


def provider(**overrides) -> DomainGuardGoogleProvider:
    return build_auth_provider(settings(**overrides))


def test_allowed_domain_claims_allowed():
    assert claims_allowed({"email": "a@betamobility.io", "email_verified": True})


def test_resolve_identity_reads_upstream_claims():
    claims = {"upstream_claims": {"email": "a@betamobility.io", "email_verified": True}}
    assert resolve_identity(claims) == ("a@betamobility.io", True)
    assert claims_allowed(claims)


@pytest.mark.asyncio
async def test_rejected_domain(monkeypatch):
    async def fake_verify(self, token):
        return AccessToken(
            token=token,
            client_id="google",
            scopes=["openid"],
            claims={"email": "a@evilbetamobility.io", "email_verified": True},
        )

    monkeypatch.setattr(GoogleProvider, "verify_token", fake_verify)
    assert await provider().verify_token("oauth-token") is None


@pytest.mark.asyncio
async def test_missing_email_rejected(monkeypatch):
    async def fake_verify(self, token):
        return AccessToken(
            token=token,
            client_id="google",
            scopes=["openid"],
            claims={"email_verified": True},
        )

    monkeypatch.setattr(GoogleProvider, "verify_token", fake_verify)
    assert await provider().verify_token("oauth-token") is None


@pytest.mark.asyncio
async def test_email_verified_false_rejected(monkeypatch):
    async def fake_verify(self, token):
        return AccessToken(
            token=token,
            client_id="google",
            scopes=["openid"],
            claims={"email": "a@betamobility.io", "email_verified": False},
        )

    monkeypatch.setattr(GoogleProvider, "verify_token", fake_verify)
    assert await provider().verify_token("oauth-token") is None


@pytest.mark.asyncio
async def test_allowed_domain_token_accepted(monkeypatch):
    async def fake_verify(self, token):
        return AccessToken(
            token=token,
            client_id="google",
            scopes=["openid"],
            claims={"email": "a@betamobility.io", "email_verified": True},
        )

    monkeypatch.setattr(GoogleProvider, "verify_token", fake_verify)
    access = await provider().verify_token("oauth-token")
    assert access is not None
    assert access.client_id == "a@betamobility.io"


@pytest.mark.asyncio
async def test_valid_service_token_accepted():
    access = await provider(mcp_service_token="service-secret").verify_token("service-secret")
    assert access is not None
    assert access.client_id == "mcp-service"


@pytest.mark.asyncio
async def test_expired_service_token_rejected():
    expired = datetime.now(timezone.utc) - timedelta(seconds=1)
    access = await provider(
        mcp_service_token="service-secret",
        mcp_service_token_expires_at=expired,
    ).verify_token("service-secret")
    assert access is None


def test_missing_remote_credentials_refuses_to_boot():
    with pytest.raises(SystemExit):
        build_auth_provider(McpBaseSettings())


def test_short_jwt_key_refuses_to_boot():
    with pytest.raises(SystemExit):
        build_auth_provider(settings(jwt_signing_key="short"))

