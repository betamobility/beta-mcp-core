"""Fail-closed Google OAuth provider for Beta Mobility MCP servers."""

from __future__ import annotations

from datetime import datetime, timezone
import hmac
import inspect

import structlog
from fastmcp.server.auth.providers.google import GoogleProvider
from mcp.server.auth.provider import AccessToken

from beta_mcp_core.settings import McpBaseSettings

ALLOWED_DOMAIN = "betamobility.io"
SERVICE_TOKEN_ENV_VAR = "MCP_SERVICE_TOKEN"

# MCP clients self-register via DCR. Restrict redirect targets to loopback
# clients and Claude's hosted callback origins so a malicious registration
# cannot capture authorization codes.
ALLOWED_CLIENT_REDIRECT_URIS = [
    "http://localhost:*",
    "http://127.0.0.1:*",
    "https://claude.ai/*",
    "https://claude.com/*",
]

log = structlog.get_logger()


def resolve_identity(claims: dict | None) -> tuple[object, object]:
    """Extract `(email, email_verified)` from FastMCP access-token claims.

    FastMCP's OAuthProxy can place upstream Google claims under
    `upstream_claims`; check top-level first, then nested fallback.
    """

    if not claims:
        return None, None
    email = claims.get("email")
    verified = claims.get("email_verified")
    if email is None or verified is None:
        upstream = claims.get("upstream_claims")
        if isinstance(upstream, dict):
            email = email if email is not None else upstream.get("email")
            verified = verified if verified is not None else upstream.get("email_verified")
    return email, verified


def claims_allowed(claims: dict | None, *, allowed_domain: str = ALLOWED_DOMAIN) -> bool:
    """Fail-closed identity check: exact verified-domain match or reject."""

    email, verified = resolve_identity(claims)
    if not isinstance(email, str) or "@" not in email:
        return False
    if verified not in (True, "true", "True"):
        return False
    domain = email.rsplit("@", 1)[1].strip().lower()
    # Exact match only: `endswith` would accept x@evilbetamobility.io.
    return domain == allowed_domain.lower()


def _expires_at_epoch(expires_at: datetime | None) -> int | None:
    if expires_at is None:
        return None
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return int(expires_at.timestamp())


def _is_expired(expires_at: datetime | None) -> bool:
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= expires_at


class DomainGuardGoogleProvider(GoogleProvider):
    """GoogleProvider with service-token bypass and exact-domain enforcement."""

    def __init__(
        self,
        *,
        service_token: str = "",
        service_token_expires_at: datetime | None = None,
        allowed_domain: str = ALLOWED_DOMAIN,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._service_token = service_token
        self._service_token_expires_at = service_token_expires_at
        self._allowed_domain = allowed_domain

    async def verify_token(self, token: str) -> AccessToken | None:
        if self._service_token and hmac.compare_digest(token, self._service_token):
            if _is_expired(self._service_token_expires_at):
                log.warning("mcp_service_token_expired")
                return None
            return AccessToken(
                token=token,
                client_id="mcp-service",
                scopes=["openid", "https://www.googleapis.com/auth/userinfo.email"],
                expires_at=_expires_at_epoch(self._service_token_expires_at),
            )

        access = await super().verify_token(token)
        if access is None:
            return None

        claims = getattr(access, "claims", None)
        if not claims_allowed(claims, allowed_domain=self._allowed_domain):
            email, _ = resolve_identity(claims)
            log.warning("oauth_domain_rejected", email=email or "unresolved")
            return None

        email, _ = resolve_identity(claims)
        return AccessToken(
            token=access.token,
            client_id=str(email),
            scopes=access.scopes,
            expires_at=access.expires_at,
            claims=claims,
        )


def build_auth_provider(
    settings: McpBaseSettings,
    *,
    remote: bool = True,
) -> DomainGuardGoogleProvider | None:
    """Build the remote auth provider, refusing to boot on unsafe config."""

    if not remote:
        return None

    required = {
        "GOOGLE_CLIENT_ID": settings.google_client_id,
        "GOOGLE_CLIENT_SECRET": settings.google_client_secret,
        "JWT_SIGNING_KEY": settings.jwt_signing_key,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise SystemExit(
            "Remote mode requires "
            + ", ".join(missing)
            + ". Refusing to start without authentication."
        )
    if len(settings.jwt_signing_key) < 64:
        raise SystemExit(
            "Remote mode requires JWT_SIGNING_KEY of at least 64 hex chars. "
            "Generate with: openssl rand -hex 32."
        )

    provider_kwargs = {
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "base_url": settings.mcp_base_url,
        "required_scopes": ["openid", "https://www.googleapis.com/auth/userinfo.email"],
        "jwt_signing_key": settings.jwt_signing_key,
        "allowed_client_redirect_uris": ALLOWED_CLIENT_REDIRECT_URIS,
        "extra_authorize_params": {"hd": settings.allowed_domain},
        "service_token": settings.mcp_service_token,
        "service_token_expires_at": settings.mcp_service_token_expires_at,
        "allowed_domain": settings.allowed_domain,
    }
    if "fastmcp_access_token_expiry_seconds" in inspect.signature(GoogleProvider.__init__).parameters:
        provider_kwargs["fastmcp_access_token_expiry_seconds"] = settings.token_expiry_seconds

    return DomainGuardGoogleProvider(**provider_kwargs)
