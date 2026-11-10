"""Opt-in, sanitized tool outcomes sent through Mission Control."""

from __future__ import annotations

import asyncio
import logging
from ipaddress import ip_address
import re
import time
from urllib.parse import urlsplit

import httpx
from fastmcp.server.middleware import Middleware
from fastmcp.server.dependencies import get_http_request

logger = logging.getLogger(__name__)
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,120}\Z")


def _duration_band(seconds: float) -> str:
    if seconds < 1:
        return "lt_1s"
    if seconds < 5:
        return "1_5s"
    if seconds < 30:
        return "5_30s"
    return "gte_30s"


class ToolAnalyticsMiddleware(Middleware):
    """Record completed tool calls across HTTP and stdio, without recording content.

    Register once on the FastMCP instance. Call ``drain`` during shutdown, before
    closing an injected HTTP client. Delivery failures never fail a tool call.
    """

    def __init__(
        self,
        *,
        component_id: str,
        ingest_url: str = "",
        ingest_token: str = "",
        enabled: bool = False,
        environment: str = "development",
        max_pending: int = 64,
        trust_cloudflare_client_ip: bool = False,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._trust_cloudflare_client_ip = trust_cloudflare_client_ip
        self._component_id = component_id
        self._url = ingest_url
        self._token = ingest_token
        self._client = client
        self._pending: set[asyncio.Task] = set()
        self._max_pending = max_pending
        target = urlsplit(ingest_url)
        self._enabled = bool(
            enabled and environment == "production" and ingest_token
            and _IDENTIFIER.fullmatch(component_id)
            and target.scheme == "https" and target.hostname
            and not target.username and not target.password
            and not target.query and not target.fragment
            and max_pending > 0
        )
        if enabled and not self._enabled:
            logger.warning("analytics disabled: incomplete production configuration")

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    async def on_call_tool(self, context, call_next):
        if not self._enabled:
            return await call_next(context)
        name = context.message.name
        try:
            registered = (
                _IDENTIFIER.fullmatch(name)
                and context.fastmcp_context is not None
                and await context.fastmcp_context.fastmcp.get_tool(name) is not None
            )
        except Exception:
            logger.warning("analytics skipped: tool registration could not be verified")
            registered = False
        if not registered:
            return await call_next(context)
        attribution = self._request_attribution()
        started = time.monotonic()
        outcome = "error"
        try:
            result = await call_next(context)
            outcome = (
                "error" if getattr(result, "is_error", False) or getattr(result, "isError", False)
                else "success"
            )
            return result
        finally:
            self._enqueue(name, outcome, time.monotonic() - started, attribution)

    def _request_attribution(self) -> dict[str, str]:
        headers = {"User-Agent": "beta-mcp-core/analytics"}
        try:
            request = get_http_request()
        except RuntimeError:
            return headers
        user_agent = request.headers.get("user-agent")
        if user_agent:
            headers["User-Agent"] = user_agent
        if self._trust_cloudflare_client_ip:
            # Only trust this header when the deployment verifies the Cloudflare
            # edge and prevents callers from reaching the origin directly.
            values = request.headers.getlist("cf-connecting-ip")
            if len(values) == 1:
                try:
                    address = ip_address(values[0])
                    if address.is_global and not address.is_multicast and "%" not in values[0]:
                        headers["X-Beta-Visitor-IP"] = str(address)
                except ValueError:
                    pass
        return headers

    def _enqueue(
        self, tool: str, outcome: str, elapsed: float, attribution: dict[str, str],
    ) -> None:
        if len(self._pending) >= self._max_pending:
            logger.warning("analytics delivery failed: queue full")
            return
        payload = {
            "site_id": "betamobility.ai",
            "component_id": self._component_id,
            "surface": "mcp",
            "environment": "production",
            "event_name": "tool_call",
            "outcome": outcome,
            "url": f"https://betamobility.ai/mcp/{self._component_id}",
            "forward_to_plausible": True,
            "props": {"tool": tool, "duration_band": _duration_band(elapsed)},
        }
        task = asyncio.create_task(self._send(payload, attribution))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _send(self, payload: dict, attribution: dict[str, str]) -> None:
        try:
            async with asyncio.timeout(10):
                if self._client is not None:
                    await self._post(self._client, payload, attribution)
                else:
                    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
                        await self._post(client, payload, attribution)
        except Exception:
            logger.warning("analytics delivery failed: request did not complete")

    async def _post(
        self, client: httpx.AsyncClient, payload: dict, attribution: dict[str, str],
    ) -> None:
        response = await client.post(
            self._url,
            json=payload,
            headers={
                "Authorization": f"Bearer {self._token}",
                **attribution,
            },
            follow_redirects=False,
        )
        if not response.is_success:
            logger.warning("analytics delivery failed: ingestion rejected event")
            return
        receipt = response.json()
        if not isinstance(receipt, dict) or (
            receipt.get("status") != "ok" or receipt.get("plausible") != "ok"
        ):
            logger.warning("analytics delivery failed: forwarding was not confirmed")

    async def drain(self) -> None:
        """Wait for currently queued deliveries after stopping new tool calls."""
        if self._pending:
            await asyncio.gather(*tuple(self._pending), return_exceptions=True)
