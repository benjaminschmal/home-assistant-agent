import asyncio
import json
import os
from typing import Any

import httpx
import uvicorn

from mcp.server import Server
from mcp.types import (
    CallToolResult,
    ListToolsResult,
    TextContent,
    Tool,
)


HA_URL = os.environ["HA_URL"].rstrip("/")
HA_TOKEN = os.environ["HA_TOKEN"]


async def ha_get(path: str) -> Any:
    headers = {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{HA_URL}{path}",
            headers=headers,
        )
        response.raise_for_status()
        return response.json()


async def list_tools(context, params) -> ListToolsResult:
    return ListToolsResult(
        tools=[
            Tool(
                name="search_entities",
                description=(
                    "Search Home Assistant entities by entity ID "
                    "or friendly name."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "Search term, e.g. 'vorlauf', "
                                "'temperature' or 'carel'."
                            ),
                        }
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="get_entity_state",
                description=(
                    "Get the current state and attributes "
                    "of a Home Assistant entity."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "entity_id": {
                            "type": "string",
                            "description": (
                                "Home Assistant entity ID, "
                                "e.g. sensor.temperature"
                            ),
                        }
                    },
                    "required": ["entity_id"],
                },
            ),
        ]
    )


async def call_tool(context, params) -> CallToolResult:

    if params.name == "search_entities":

        query = params.arguments.get("query", "").strip().lower()

        if not query:
            raise ValueError("query is required")

        states = await ha_get("/api/states")

        results = []

        for entity in states:

            entity_id = entity.get("entity_id", "")

            attributes = entity.get("attributes", {})

            friendly_name = attributes.get(
                "friendly_name",
                "",
            )

            if (
                query in entity_id.lower()
                or query in friendly_name.lower()
            ):

                results.append({
                    "entity_id": entity_id,
                    "friendly_name": friendly_name,
                    "state": entity.get("state"),
                    "unit_of_measurement": attributes.get(
                        "unit_of_measurement"
                    ),
                    "device_class": attributes.get(
                        "device_class"
                    ),
                })

        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=json.dumps(
                        results[:50],
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
            ]
        )

    if params.name == "get_entity_state":

        entity_id = params.arguments.get("entity_id")

        if not entity_id:
            raise ValueError("entity_id is required")

        data = await ha_get(
            f"/api/states/{entity_id}"
        )

        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=json.dumps(
                        data,
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
            ]
        )

    raise ValueError(
        f"Unknown tool: {params.name}"
    )


server = Server(
    "home-assistant-mcp",
    version="1.0.0",
    on_list_tools=list_tools,
    on_call_tool=call_tool,
)


async def main():

    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        host="0.0.0.0",
        stateless_http=True,
    )

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="info",
    )

    server_instance = uvicorn.Server(config)

    await server_instance.serve()


if __name__ == "__main__":
    asyncio.run(main())
