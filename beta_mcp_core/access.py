from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any, Literal

import httpx
import jwt

ACCESS_ASSERTION_HEADER = "cf-access-jwt-assertion"
_DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")


class AccessAuthError(Exception):
    """Generic denial; details must not be reflected to callers."""


@dataclass(frozen=True, slots=True)
class AccessConfig:
    team_domain: str
    audience: str

    def __post_init__(self) -> None:
        domain = self.team_domain.strip().lower()
        if (
            not _DOMAIN_RE.fullmatch(domain)
            or "/" in domain
            or ":" in domain
            or not domain.endswith(".cloudflareaccess.com")
        ):
            raise ValueError("invalid Cloudflare Access team domain")
        if not self.audience.strip():
            raise ValueError("Access audience must not be empty")
        object.__setattr__(self, "team_domain", domain)

    @property
    def issuer(self) -> str:
        return f"https://{self.team_domain}"

    @property
    def jwks_url(self) -> str:
        return f"{self.issuer}/cdn-cgi/access/certs"


@dataclass(frozen=True, slots=True)
class AccessIdentity:
    subject: str
    email: str | None
    kind: Literal["human", "service"]

    @property
    def actor_id(self) -> str:
        return self.email or self.subject


@dataclass(slots=True)
class _CachedKeys:
    keys: dict[str, Any]
    expires_at: float


class JWKSCache:
    """Bounded Access JWKS cache with one refresh attempt for an unknown key."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        ttl_seconds: int = 300,
        max_document_bytes: int = 256_000,
    ) -> None:
        self._client = client
        self._ttl_seconds = ttl_seconds
        self._max_document_bytes = max_document_bytes
        self._documents: dict[str, _CachedKeys] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._unknown_refreshes: dict[str, float] = {}
        self._failed_until: dict[str, float] = {}

    async def key(self, config: AccessConfig, kid: str) -> Any:
        if not 1 <= len(kid) <= 128:
            raise AccessAuthError("Access assertion denied")
        now = asyncio.get_running_loop().time()
        self._unknown_refreshes = {
            key: refreshed_at
            for key, refreshed_at in self._unknown_refreshes.items()
            if now - refreshed_at < 30
        }
        cached = self._documents.get(config.team_domain)
        if cached is None or cached.expires_at <= now:
            cached = await self._refresh(config)
        key = cached.keys.get(kid)
        if key is not None:
            return key

        refresh_key = config.team_domain
        last_refresh = self._unknown_refreshes.get(refresh_key, 0.0)
        if now - last_refresh < 30:
            raise AccessAuthError("Access assertion denied")
        self._unknown_refreshes[refresh_key] = now
        cached = await self._refresh(config, force=True)
        key = cached.keys.get(kid)
        if key is None:
            raise AccessAuthError("Access assertion denied")
        return key

    async def _refresh(self, config: AccessConfig, *, force: bool = False) -> _CachedKeys:
        lock = self._locks.setdefault(config.team_domain, asyncio.Lock())
        async with lock:
            now = asyncio.get_running_loop().time()
            if self._failed_until.get(config.team_domain, 0.0) > now:
                raise AccessAuthError("Access assertion denied")
            cached = self._documents.get(config.team_domain)
            if not force and cached is not None and cached.expires_at > now:
                return cached
            owns_client = self._client is None
            client = self._client or httpx.AsyncClient(
                timeout=httpx.Timeout(3.0), follow_redirects=False
            )
            try:
                response = await client.get(
                    config.jwks_url, headers={"Accept": "application/json"}
                )
                response.raise_for_status()
                if len(response.content) > self._max_document_bytes:
                    raise AccessAuthError("Access assertion denied")
                document = response.json()
                keys = self._parse(document)
            except Exception as exc:
                # Share an outage denial with queued requests without serving stale keys.
                self._failed_until[config.team_domain] = asyncio.get_running_loop().time() + 5
                raise AccessAuthError("Access assertion denied") from exc
            finally:
                if owns_client:
                    await client.aclose()
            cached = _CachedKeys(keys=keys, expires_at=now + self._ttl_seconds)
            self._documents[config.team_domain] = cached
            self._failed_until.pop(config.team_domain, None)
            return cached

    @staticmethod
    def _parse(document: Any) -> dict[str, Any]:
        if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
            raise AccessAuthError("Access assertion denied")
        result: dict[str, Any] = {}
        for raw in document["keys"]:
            if not isinstance(raw, dict):
                raise AccessAuthError("Access assertion denied")
            kid = raw.get("kid")
            if (
                not isinstance(kid, str)
                or not kid
                or kid in result
                or raw.get("kty") != "RSA"
                or raw.get("alg") not in (None, "RS256")
                or raw.get("use") not in (None, "sig")
            ):
                raise AccessAuthError("Access assertion denied")
            try:
                result[kid] = jwt.algorithms.RSAAlgorithm.from_jwk(raw)
            except Exception as exc:
                raise AccessAuthError("Access assertion denied") from exc
        if not result:
            raise AccessAuthError("Access assertion denied")
        return result


class AccessVerifier:
    def __init__(self, config: AccessConfig, *, cache: JWKSCache | None = None) -> None:
        self.config = config
        self.cache = cache or JWKSCache()

    async def verify(self, token: str) -> AccessIdentity:
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
                raise AccessAuthError("denied: unsupported alg or missing kid")
            key = await self.cache.key(self.config, header["kid"])
            payload = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=self.config.issuer,
                audience=self.config.audience,
                options={"require": ["iss", "aud", "sub", "exp", "iat"]},
            )
        except AccessAuthError:
            raise
        except Exception as exc:
            raise AccessAuthError("Access assertion denied") from exc

        # RFC 7519 allows `aud` as either a string or an array, and Cloudflare
        # uses both: an array for human assertions, a bare string for service
        # tokens. Requiring an array looked like sound hardening and silently
        # rejected every machine. The value is still compared exactly, and
        # PyJWT has already validated it independently above.
        raw_audience = payload.get("aud")
        if isinstance(raw_audience, str):
            audiences = [raw_audience]
        elif isinstance(raw_audience, list):
            audiences = raw_audience
        else:
            audiences = []
        if self.config.audience not in audiences:
            raise AccessAuthError(
                f"denied: audience mismatch (type={type(raw_audience).__name__})"
            )

        raw_email = payload.get("email")
        email = raw_email.strip().lower() if isinstance(raw_email, str) else None

        # Human and service assertions carry identity in different claims, and
        # this is not a detail we may paper over.
        #
        # Cloudflare documents a service-token assertion as having `sub: ""`
        # with the token's Client ID in `common_name`. Reading identity from
        # `sub` for every assertion therefore rejected EVERY service token —
        # which is why the local-repository reporter was built, shipped, and
        # never once authenticated. Verified against a live token 2026-08-07.
        if email:
            candidate = payload.get("sub")
        else:
            candidate = payload.get("common_name")

        subject = candidate.strip() if isinstance(candidate, str) else ""
        if not subject:
            raise AccessAuthError(
                "denied: no identity claim "
                f"(email={'yes' if email else 'no'}, claims={sorted(payload)})"
            )

        kind: Literal["human", "service"] = "human" if email else "service"
        return AccessIdentity(subject=subject, email=email, kind=kind)


class AccessMiddleware:
    """Fail closed on every HTTP route except the literal health URL."""

    def __init__(self, app, *, verifier: AccessVerifier, allowed_domain: str,
                 service_tokens: tuple[str, ...] = ()) -> None:
        self.app = app
        self.verifier = verifier
        self.allowed_domain = allowed_domain.lower()
        self.service_tokens = frozenset(service_tokens)

    async def __call__(self, scope, receive, send):
        from starlette.responses import JSONResponse
        import structlog

        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] != "http":
            await send({"type": "websocket.close", "code": 1008})
            return
        if (scope.get("path") == "/health"
                and scope.get("raw_path", b"/health") == b"/health"
                and scope.get("method") in ("GET", "HEAD")):
            return await self.app(scope, receive, send)
        headers = [value for name, value in scope.get("headers", [])
                   if name.lower() == b"cf-access-jwt-assertion"]
        try:
            if len(headers) != 1 or not headers[0] or len(headers[0]) > 32768:
                raise AccessAuthError("denied")
            identity = await self.verifier.verify(headers[0].decode("ascii"))
            if identity.kind == "human":
                if (identity.email.count("@") != 1
                        or identity.email.rsplit("@", 1)[1] != self.allowed_domain):
                    raise AccessAuthError("denied")
            elif identity.subject not in self.service_tokens:
                raise AccessAuthError("denied")
        except (AccessAuthError, UnicodeError):
            await JSONResponse({"error": "Unauthorized"}, status_code=401)(scope, receive, send)
            return
        scope.setdefault("state", {})["access_identity"] = identity
        structlog.get_logger().info("access_authenticated", actor=identity.actor_id,
                                    identity_kind=identity.kind)
        await self.app(scope, receive, send)
