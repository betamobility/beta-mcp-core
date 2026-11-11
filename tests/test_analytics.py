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
        ingest_token="private-token", enabled=True,
        environment=kwargs.pop("environment", "production"),
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


@pytest.mark.parametrize("trusted", [False, True])
async def test_real_calls_emit_once_and_list_emits_nothing(trusted):
    requests = []

    def receive(request):
        requests.append(request)
        return httpx.Response(200, json={"status": "ok", "plausible": "ok"})

    server, analytics, sender = setup(receive, trust_cloudflare_client_ip=trusted)
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
        assert "x-beta-visitor-ip" not in request.headers
        assert payload["site_id"] == "betamobility.ai"
        assert payload["url"] == "https://betamobility.ai/mcp/test-mcp"
        assert payload["event_name"] == "tool_call"
        assert payload["forward_to_plausible"] is True
        assert payload["props"]["duration_band"] in {"lt_1s", "1_5s", "5_30s", "gte_30s"}
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


@pytest.mark.parametrize("enabled", [False, True])
async def test_malformed_url_preserves_tool_availability(enabled, caplog):
    analytics = ToolAnalyticsMiddleware(
        component_id="test-mcp", ingest_url="https://[", ingest_token="secret",
        enabled=enabled, environment="production",
    )
    server = FastMCP("test", middleware=[analytics])

    @server.tool
    def healthy() -> str:
        return "ok"

    async with Client(server) as client:
        assert (await client.call_tool("healthy")).data == "ok"
    assert analytics.pending_count == 0
    assert ("analytics disabled" in caplog.text) is enabled
    assert "secret" not in caplog.text


@pytest.mark.parametrize("name", ["tables.search", "t" * 128])
async def test_registered_mcp_names_are_recorded(name):
    payloads = []

    def receive(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "ok", "plausible": "ok"})

    server, analytics, sender = setup(receive)

    @server.tool(name=name)
    def healthy() -> str:
        return "ok"

    async with sender, Client(server) as client:
        assert (await client.call_tool(name)).data == "ok"
        await analytics.drain()
    assert len(payloads) == 1
    assert payloads[0]["props"]["tool"] == name


@pytest.mark.parametrize("environment", ["development", "staging"])
async def test_nonproduction_with_valid_credentials_does_not_send(environment):
    requests = []

    def receive(request):
        requests.append(request)
        return httpx.Response(200)

    server, analytics, sender = setup(receive, environment=environment)
    async with sender, Client(server) as client:
        assert (await client.call_tool("echo", {"secret": "local-only"})).data == "local-only"
        await analytics.drain()
    assert requests == []


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


@pytest.mark.parametrize("error_key, marker, expected", [
    ("error", "private failure", "error"),
    ("error", None, "success"),
    (None, "private failure", "success"),
])
async def test_opt_in_domain_error_preserves_result(error_key, marker, expected):
    payloads = []

    def receive(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "ok", "plausible": "ok"})

    server, analytics, sender = setup(receive, error_result_key=error_key)

    @server.tool
    def domain_result() -> dict:
        return {"error": marker, "detail": "private customer data"}

    async with sender, Client(server) as client:
        result = await client.call_tool("domain_result")
        assert result.data == {"error": marker, "detail": "private customer data"}
        assert not result.is_error
        await analytics.drain()
    assert len(payloads) == 1
    assert payloads[0]["outcome"] == expected
    assert "private" not in json.dumps(payloads)


@pytest.mark.parametrize("trusted, visitor, expected_ip", [
    (True, "8.8.8.8", "8.8.8.8"),
    (True, "2606:4700:4700::1111", "2606:4700:4700::1111"),
    (False, "8.8.8.8", None),
    (True, "8.8.8.8, 1.1.1.1", None),
    (True, "invalid", None),
    (True, "duplicate", None),
    (True, "224.0.0.1", None),
    (True, "127.0.0.1", None),
    (True, "10.0.0.1", None),
    (True, "169.254.1.1", None),
    (True, "", None),
    (True, "fc00::1", None),
    (True, "2606:4700:4700::1111%eth0", None),
])
async def test_http_tool_attribution_is_transport_only(trusted, visitor, expected_ip):
    requests = []

    def receive(request):
        requests.append(request)
        return httpx.Response(200, json={"status": "ok", "plausible": "ok"})

    server, analytics, sender = setup(receive, trust_cloudflare_client_ip=trusted)
    app = server.http_app(stateless_http=True, json_response=True)
    inbound = {
        "Accept": "application/json, text/event-stream",
        "User-Agent": "actual-mcp-client/1.0",
        "CF-Connecting-IP": visitor,
        "X-Forwarded-For": "1.1.1.1",
        "Authorization": "Bearer incoming-private",
        "Cookie": "private=session",
        "CF-Access-Client-Secret": "private-edge-secret",
    }
    if visitor == "duplicate":
        inbound["CF-Connecting-IP"] = "8.8.8.8"
        inbound = list(inbound.items()) + [("CF-Connecting-IP", "1.1.1.1")]
    async with app.router.lifespan_context(app), sender:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://mcp.example",
            headers=inbound,
        ) as client:
            for i, (method, params) in enumerate([
                ("tools/list", {}),
                ("tools/call", {"name": "echo", "arguments": {"secret": "private-input"}}),
                ("tools/call", {"name": "fail", "arguments": {"secret": "private-error"}}),
            ]):
                response = await client.post("/mcp", json={
                    "jsonrpc": "2.0", "id": i, "method": method, "params": params,
                })
                assert response.status_code == 200, response.text
            await analytics.drain()
    assert len(requests) == 2
    assert [json.loads(r.content)["outcome"] for r in requests] == ["success", "error"]
    for request in requests:
        assert request.headers["user-agent"] == "actual-mcp-client/1.0"
        assert request.headers.get("x-beta-visitor-ip") == expected_ip
        assert request.headers["authorization"] == "Bearer private-token"
        assert "cookie" not in request.headers
        assert "cf-access-client-secret" not in request.headers
        assert "x-forwarded-for" not in request.headers
        assert "actual-mcp-client" not in request.content.decode()
        assert "8.8.8.8" not in request.content.decode()
        assert "private" not in request.content.decode()


@pytest.mark.parametrize("failed", [False, True])
async def test_custom_domain_error_classifier_preserves_list_results(failed):
    payloads = []
    def receive(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "ok", "plausible": "ok"})
    def classify(name, structured):
        rows = structured.get("result", [])
        return name == "search" and len(rows) == 1 and bool(rows[0].get("error"))
    server, analytics, sender = setup(receive, error_result_predicate=classify)
    rows = [{"error": "private failure"}] if failed else [{"title": "private result"}]
    @server.tool
    def search() -> list[dict]:
        return rows
    @server.tool
    def ordinary_data() -> list[dict]:
        return [{"error": "private ordinary value"}]
    async with sender, Client(server) as client:
        result = await client.call_tool("search")
        assert result.data == rows
        assert not result.is_error
        await client.call_tool("ordinary_data")
        await analytics.drain()
    assert [p["outcome"] for p in payloads] == ["error" if failed else "success", "success"]
    assert "private" not in json.dumps(payloads)


async def test_classifier_failure_does_not_change_tool_result(caplog):
    payloads = []
    def receive(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "ok", "plausible": "ok"})
    def classify(name, structured):
        raise ValueError("private classifier details")
    server, analytics, sender = setup(receive, error_result_predicate=classify)
    async with sender, Client(server) as client:
        result = await client.call_tool("echo", {"secret": "private result"})
        assert result.data == "private result"
        await analytics.drain()
    assert payloads == []
    assert "analytics skipped: outcome classification failed" in caplog.text
    assert "private" not in caplog.text
