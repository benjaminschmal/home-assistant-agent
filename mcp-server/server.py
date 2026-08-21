import asyncio
import json
import logging
import os
import re
from typing import Any

import httpx
import uvicorn

from mcp.server import Server
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("home-assistant-mcp")

HA_URL = os.environ.get("HA_URL", "").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
HA_TIMEOUT = float(os.environ.get("HA_TIMEOUT_SECONDS", "15"))
MAX_SEARCH_RESULTS = int(os.environ.get("MAX_SEARCH_RESULTS", "50"))

if not HA_URL:
    raise RuntimeError("HA_URL is not configured")
if not HA_TOKEN:
    raise RuntimeError("HA_TOKEN is not configured")
if not 1 <= MAX_SEARCH_RESULTS <= 200:
    raise RuntimeError("MAX_SEARCH_RESULTS must be between 1 and 200")


async def ha_get(path: str) -> Any:
    headers = {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=HA_TIMEOUT) as client:
            response = await client.get(f"{HA_URL}{path}", headers=headers)
            response.raise_for_status()
            return response.json()
    except httpx.TimeoutException as exc:
        raise RuntimeError(f"Home Assistant request timed out after {HA_TIMEOUT:.0f}s") from exc
    except httpx.HTTPStatusError as exc:
        logger.error("Home Assistant returned HTTP %s for %s", exc.response.status_code, path)
        raise RuntimeError(f"Home Assistant returned HTTP {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        logger.error("Home Assistant request failed: %s", type(exc).__name__)
        raise RuntimeError("Home Assistant request failed") from exc


def normalize(value: str) -> str:
    value = value.casefold().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def search_score(query: str, entity: dict[str, Any]) -> int:
    normalized_query = normalize(query)
    if not normalized_query:
        return 0

    terms = normalized_query.split()
    entity_id = normalize(entity.get("entity_id", ""))
    friendly_name = normalize(entity.get("friendly_name", ""))
    device_class = normalize(entity.get("device_class", ""))
    state = normalize(str(entity.get("state", "")))

    score = 0
    for term in terms:
        if term in friendly_name:
            score += 100
        elif term in entity_id:
            score += 70
        elif term in device_class:
            score += 50
        elif term in state:
            score += 5

    if normalized_query == friendly_name:
        score += 100
    if normalized_query == entity_id:
        score += 80
    return score


async def list_tools(context, params) -> ListToolsResult:
    return ListToolsResult(
        tools=[
            Tool(
                name="search_entities",
                description=(
                    "Search Home Assistant entities by entity ID, friendly name, "
                    "device class or current state. Use this before get_entity_state "
                    "when the exact entity ID is unknown."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search terms such as 'vorlauf', 'temperature', 'drucker' or 'carel'.",
                        }
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="get_entity_state",
                description=(
                    "Get the current state and attributes of one Home Assistant entity."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "entity_id": {
                            "type": "string",
                            "description": "Home Assistant entity ID, e.g. sensor.temperature",
                        }
                    },
                    "required": ["entity_id"],
                },
            ),
        ]
    )


async def call_tool(context, params) -> CallToolResult:
    if params.name == "search_entities":
        query = str(params.arguments.get("query", "")).strip()
        if not query:
            raise ValueError("query is required")

        states = await ha_get("/api/states")
        results = []

        for entity in states:
            attributes = entity.get("attributes", {}) or {}
            result = {
                "entity_id": entity.get("entity_id", ""),
                "friendly_name": attributes.get("friendly_name", ""),
                "state": entity.get("state"),
                "unit_of_measurement": attributes.get("unit_of_measurement"),
                "device_class": attributes.get("device_class"),
            }
            score = search_score(query, result)
            if score > 0:
                result["_score"] = score
                results.append(result)

        results.sort(key=lambda item: (-item.pop("_score"), item["entity_id"]))
        logger.info("Entity search '%s' returned %d results", query, len(results))

        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=json.dumps(results[:MAX_SEARCH_RESULTS], ensure_ascii=False, indent=2),
                )
            ]
        )

    if params.name == "get_entity_state":
        entity_id = str(params.arguments.get("entity_id", "")).strip()
        if not entity_id:
            raise ValueError("entity_id is required")
        if not re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+", entity_id):
            raise ValueError("invalid entity_id")

        data = await ha_get(f"/api/states/{entity_id}")
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=json.dumps(data, ensure_ascii=False, indent=2),
                )
            ]
        )

    raise ValueError(f"Unknown tool: {params.name}")


server = Server(
    "home-assistant-mcp",
    version="1.1.0",
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
