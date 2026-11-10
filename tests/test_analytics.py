import asyncio
import json

import httpx
import pytest
from fastmcp import Client, FastMCP

from beta_mcp_core.analytics import ToolAnalyticsMiddleware


def setup(handler, **kwargs):
    sender = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    analytics = ToolAnalyticsMiddleware(
        component_id="test-mcp", ingest_url="https://mc.example/analytics/event",
        ingest_token="private-token", enabled=True, environment="production",
        client=sender, **kwargs,
    )
    server = FastMCP("test", middleware=[analytics])

    @server.tool
    def echo(secret: str) -> str:
        return secret

    @server.tool
    def fail(secret: str) -> str:
        raise ValueError(secret)

    return server, analytics, sender


async def test_real_calls_emit_once_and_list_emits_nothing():
    requests = []

    def receive(request):
        requests.append(request)
        return httpx.Response(200, json={"status": "ok", "plausible": "ok"})

    server, analytics, sender = setup(receive)
    async with sender, Client(server) as client:
        await client.list_tools()
        await analytics.drain()
        assert requests == []
        await client.call_tool("echo", {"secret": "person@example.org"})
        await client.call_tool("fail", {"secret": "SECRET RESULT"}, raise_on_error=False)
        await analytics.drain()
    assert len(requests) == 2
    payloads = [json.loads(request.content) for request in requests]
    assert [p["outcome"] for p in payloads] == ["success", "error"]
    assert [p["props"]["tool"] for p in payloads] == ["echo", "fail"]
    for request, payload in zip(requests, payloads):
        assert request.headers["authorization"] == "Bearer private-token"
        assert request.headers["user-agent"] == "beta-mcp-core/analytics"
        assert "x-forwarded-for" not in request.headers
        assert payload["site_id"] == "betamobility.ai"
        assert payload["url"] == "https://betamobility.ai/mcp/test-mcp"
        assert payload["event_name"] == "tool_call"
        assert payload["forward_to_plausible"] is True
        assert payload["props"]["duration_band"] == "lt_1s"
        assert "person@" not in request.content.decode()
        assert "SECRET" not in request.content.decode()


@pytest.mark.parametrize("response", [
    httpx.Response(502, text="private upstream error"),
    httpx.Response(200, json={"status": "ok", "plausible": "dropped"}),
    httpx.Response(200, text="private malformed body"),
])
async def test_delivery_failure_preserves_tool_and_warns(response, caplog):
    server, analytics, sender = setup(lambda request: response)
    async with sender, Client(server) as client:
        result = await client.call_tool("echo", {"secret": "preserved"})
        await analytics.drain()
    assert result.data == "preserved"
    assert "analytics delivery failed" in caplog.text
    assert "private" not in caplog.text


async def test_pending_bound_and_shutdown_drain(caplog):
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def receive(request):
        calls.append(request)
        started.set()
        await release.wait()
        return httpx.Response(200, json={"status": "ok", "plausible": "ok"})

    server, analytics, sender = setup(receive, max_pending=1)
    async with sender, Client(server) as client:
        await client.call_tool("echo", {"secret": "one"})
        await started.wait()
        await client.call_tool("echo", {"secret": "two"})
        assert len(calls) == 1
        assert "queue full" in caplog.text
        drain = asyncio.create_task(analytics.drain())
        await asyncio.sleep(0)
        assert not drain.done()
        release.set()
        await drain
    assert not analytics.pending_count


async def test_disabled_missing_configuration_preserves_tools(caplog):
    analytics = ToolAnalyticsMiddleware(component_id="test-mcp", enabled=True)
    server = FastMCP("test", middleware=[analytics])

    @server.tool
    def healthy() -> str:
        return "ok"

    async with Client(server) as client:
        assert (await client.call_tool("healthy")).data == "ok"
    assert "analytics disabled" in caplog.text
    assert analytics.pending_count == 0


async def test_error_result_and_unknown_tool_are_distinct():
    from fastmcp.tools import ToolResult

    payloads = []

    def receive(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "ok", "plausible": "ok"})

    server, analytics, sender = setup(receive)

    @server.tool
    def rejected() -> ToolResult:
        return ToolResult(content="private error result", is_error=True)

    async with sender, Client(server) as client:
        result = await client.call_tool("rejected", raise_on_error=False)
        assert result.is_error
        await client.call_tool("unknown_private_identifier", raise_on_error=False)
        await analytics.drain()
    assert len(payloads) == 1
    assert payloads[0]["outcome"] == "error"
    assert payloads[0]["props"]["tool"] == "rejected"
    assert "private" not in json.dumps(payloads)


@pytest.mark.parametrize("elapsed, expected", [
    (0.9, "lt_1s"), (1, "1_5s"), (5, "5_30s"), (30, "gte_30s"),
])
def test_duration_boundaries(elapsed, expected):
    from beta_mcp_core.analytics import _duration_band

    assert _duration_band(elapsed) == expected
