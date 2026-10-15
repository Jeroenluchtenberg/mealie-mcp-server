"""Call the local development MCP server as a seeded user.

    uv run python dev/call.py <username>                      # list tools
    uv run python dev/call.py <username> <tool> '<json args>' # call a tool

Sends the username in X-User like the gateway would. Local stack only.
"""

import asyncio
import json
import os
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

MCP_URL = os.getenv("DEV_MCP_URL", "http://127.0.0.1:8765/mcp")


async def main(username: str, tool: str | None, arguments: dict) -> int:
    headers = {os.getenv("MCP_USER_HEADER", "X-User"): username}
    async with streamablehttp_client(MCP_URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            if tool is None:
                tools = (await session.list_tools()).tools
                print(f"{len(tools)} tools")
                for item in tools:
                    print(f"- {item.name}")
                return 0
            result = await session.call_tool(tool, arguments)
            if result.structuredContent is not None:
                content = result.structuredContent
                if set(content) == {"result"}:
                    content = content["result"]
                print(json.dumps(content, indent=2, ensure_ascii=False))
            else:
                for block in result.content:
                    print(getattr(block, "text", block))
            return 1 if result.isError else 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    tool_name = sys.argv[2] if len(sys.argv) > 2 else None
    args = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
    sys.exit(asyncio.run(main(sys.argv[1], tool_name, args)))
