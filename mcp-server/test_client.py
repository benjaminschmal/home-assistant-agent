import asyncio
import os

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main():
    url = os.environ.get(
        "MCP_URL",
        "http://localhost:8000/mcp",
    )

    async with streamable_http_client(url) as (
        read_stream,
        write_stream,
    ):
        async with ClientSession(
            read_stream,
            write_stream,
        ) as session:
            await session.initialize()

            tools = await session.list_tools()

            print("Available tools:")
            for tool in tools.tools:
                print(f"- {tool.name}")

            print()
            print("Searching for 'vorlauf'...")

            result = await session.call_tool(
                "search_entities",
                {
                    "query": "vorlauf",
                },
            )

            print(result)


if __name__ == "__main__":
    asyncio.run(main())
