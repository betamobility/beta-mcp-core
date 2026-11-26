"""U5: count the tools one gateway connector would carry.

Lists tools on each fleet MCP server behind Cloudflare Access, using the read-only
`mcp-fleet-check` service token passed in the environment (never printed).
Spike only; removed with the spike branch.
"""

import asyncio
import os
import sys

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

SERVERS = ["scout", "atlas", "drop", "folio", "plans", "radar", "pulse", "poweroffice", "beta-os", "gbfs", "gtfs"]


def url(name: str) -> str:
    zone = "betamobility.com" if name == "pulse" else "betamobility.ai"
    return f"https://{name}-mcp.{zone}/mcp"


async def list_tools(name: str, headers: dict[str, str]) -> tuple[str, list[str] | str]:
    try:
        async with Client(StreamableHttpTransport(url(name), headers=headers)) as client:
            tools = await client.list_tools()
            return name, [t.name for t in tools]
    except Exception as exc:  # report, don't hide
        return name, f"error: {type(exc).__name__}"


async def main() -> int:
    headers = {
        "CF-Access-Client-Id": os.environ["CF_ACCESS_CLIENT_ID"],
        "CF-Access-Client-Secret": os.environ["CF_ACCESS_CLIENT_SECRET"],
        "User-Agent": "beta-gateway-tool-sweep/1",
    }
    results = await asyncio.gather(*(list_tools(n, headers) for n in SERVERS))
    total, longest = 0, ""
    for name, tools in results:
        if isinstance(tools, str):
            print(f"{name}: {tools}")
            continue
        total += len(tools)
        for t in tools:
            namespaced = f"{name.replace('-', '_')}_{t}"
            longest = max(longest, namespaced, key=len)
        print(f"{name}: {len(tools)}")
    print(f"total: {total}")
    print(f"longest namespaced name: {longest} ({len(longest)} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
