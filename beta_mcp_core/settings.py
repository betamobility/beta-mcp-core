"""Shared pydantic settings base for Beta Mobility MCP servers."""

from __future__ import annotations

from datetime import datetime
import re

from beta_mcp_core.access import AccessConfig

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class McpBaseSettings(BaseSettings):
    """Base env settings for remote-capable FastMCP servers.

    All-empty auth config is allowed for local stdio development. Partial auth
    config is rejected because deployed servers must not drift into an
    accidentally unauthenticated state.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    cf_access_team_domain: str = ""
    cf_access_aud: str = ""
    cf_access_service_tokens: tuple[str, ...] = ()
    google_client_id: str = ""
    google_client_secret: str = ""
    jwt_signing_key: str = ""
    mcp_base_url: str = "http://localhost:8000"
    mcp_service_token: str = ""
    mcp_service_token_expires_at: datetime | None = None
    allowed_domain: str = "betamobility.io"
    database_url: str = ""
    port: int = 8000
    token_expiry_seconds: int = 3600

    @model_validator(mode="after")
    def _auth_config_all_or_none(self) -> "McpBaseSettings":
        values = {
            "GOOGLE_CLIENT_ID": self.google_client_id,
            "GOOGLE_CLIENT_SECRET": self.google_client_secret,
            "JWT_SIGNING_KEY": self.jwt_signing_key,
        }
        if self.cf_access_team_domain or self.cf_access_aud or self.cf_access_service_tokens:
            if any(values.values()) or self.mcp_service_token:
                raise ValueError("Access and Google/service bearer authentication are mutually exclusive")
            if not re.fullmatch(r"[A-Za-z0-9_-]+", self.cf_access_aud):
                raise ValueError("CF_ACCESS_AUD must contain exactly one audience")
            AccessConfig(self.cf_access_team_domain, self.cf_access_aud)
        if any(values.values()) and not all(values.values()):
            missing = ", ".join(name for name, value in values.items() if not value)
            raise ValueError(
                f"Partial auth configuration (missing: {missing}). Set all of "
                "GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, JWT_SIGNING_KEY — or none "
                "of them for local development without auth."
            )
        return self

    @property
    def auth_enabled(self) -> bool:
        return self.access_enabled or bool(self.google_client_id and self.google_client_secret and self.jwt_signing_key)


    @property
    def access_enabled(self) -> bool:
        return bool(self.cf_access_team_domain and self.cf_access_aud)
