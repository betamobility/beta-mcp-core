"""Opt-in, sanitized tool outcomes sent through Mission Control."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
import logging
import threading
from ipaddress import ip_address
import re
import time
from urllib.parse import urlsplit

import httpx
from fastmcp.server.middleware import Middleware
from fastmcp.server.dependencies import get_http_request

logger = logging.getLogger(__name__)
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,120}\Z")
_TOOL_NAME = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")


def _duration_band(seconds: float) -> str:
    if seconds < 1:
        return "lt_1s"
    if seconds < 5:
        return "1_5s"
    if seconds < 30:
        return "5_30s"
    return "gte_30s"


@dataclass
class _CallStats:
    """What one tool call did upstream, shared with any thread the call hops to."""

    cache: str | None = None
    upstream_seconds: float | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


_call_stats: ContextVar[_CallStats | None] = ContextVar("beta_mcp_core_call_stats", default=None)


def record_cache(state: str) -> None:
    """Record a cache lookup for the current tool call. A call is a hit only when every
    lookup it made hit; one miss makes it a miss. Outside a tool call this does nothing."""
    stats = _call_stats.get()
    if stats is None:
        return
    with stats._lock:
        if stats.cache != "miss":
            stats.cache = "miss" if state == "miss" else "hit"


@contextmanager
def upstream_timer() -> Iterator[None]:
    """Time an upstream request for the current tool call's ``upstream_band``. Several timed
    requests in one call add up. Usable in sync and async code; outside a tool call it only
    times."""
    started = time.monotonic()
    try:
        yield
    finally:
        stats = _call_stats.get()
        if stats is not None:
            elapsed = time.monotonic() - started
            with stats._lock:
                stats.upstream_seconds = (stats.upstream_seconds or 0.0) + elapsed


class ToolAnalyticsMiddleware(Middleware):
    """Record completed tool calls across HTTP and stdio, without recording content.

    Register once on the FastMCP instance. Call ``drain`` during shutdown, before
    closing an injected HTTP client. Delivery failures never fail a tool call.

    ``report_cache_and_upstream=True`` adds ``cache`` (``hit``, ``miss`` or ``none``) and,
    when the call reached upstream, ``upstream_band`` to each event, from what
    ``stamped_cache`` and ``upstream_timer`` recorded during the call. Turn it on only once
    Mission Control accepts both props: it rejects an event with a prop it does not know.
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
        error_result_key: str | None = None,
        error_result_predicate: Callable[[str, Any], bool] | None = None,
        client: httpx.AsyncClient | None = None,
        report_cache_and_upstream: bool = False,
    ) -> None:
        self._report_cache_and_upstream = report_cache_and_upstream
        self._trust_cloudflare_client_ip = trust_cloudflare_client_ip
        self._error_result_key = error_result_key
        self._error_result_predicate = error_result_predicate
        self._component_id = component_id
        self._url = ingest_url
        self._token = ingest_token
        self._client = client
        self._pending: set[asyncio.Task] = set()
        self._max_pending = max_pending
        try:
            target = urlsplit(ingest_url)
            self._enabled = bool(
                enabled and environment == "production" and ingest_token
                and _IDENTIFIER.fullmatch(component_id)
                and target.scheme == "https" and target.hostname
                and not target.username and not target.password
                and not target.query and not target.fragment
                and max_pending > 0
            )
        except ValueError:
            self._enabled = False
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
                _TOOL_NAME.fullmatch(name)
                and context.fastmcp_context is not None
                and await context.fastmcp_context.fastmcp.get_tool(name) is not None
            )
        except Exception:
            logger.warning("analytics skipped: tool registration could not be verified")
            registered = False
        if not registered:
            return await call_next(context)
        attribution = self._request_attribution()
        stats = _CallStats() if self._report_cache_and_upstream else None
        token = _call_stats.set(stats) if stats is not None else None
        started = time.monotonic()
        outcome = "error"
        try:
            result = await call_next(context)
            structured = getattr(result, "structured_content", None)
            domain_error = (
                self._error_result_key is not None
                and isinstance(structured, dict)
                and bool(structured.get(self._error_result_key))
            )
            if self._error_result_predicate is not None and not domain_error:
                try:
                    domain_error = bool(self._error_result_predicate(name, structured))
                except Exception:
                    logger.warning("analytics skipped: outcome classification failed")
                    outcome = None
                    return result
            outcome = (
                "error" if domain_error or getattr(result, "is_error", False)
                or getattr(result, "isError", False)
                else "success"
            )
            return result
        finally:
            if token is not None:
                _call_stats.reset(token)
            if outcome is not None:
                self._enqueue(name, outcome, time.monotonic() - started, attribution, stats)

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
        stats: _CallStats | None = None,
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
        if stats is not None:
            payload["props"]["cache"] = stats.cache or "none"
            if stats.upstream_seconds is not None:
                payload["props"]["upstream_band"] = _duration_band(stats.upstream_seconds)
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
