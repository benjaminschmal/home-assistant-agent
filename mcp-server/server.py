import asyncio
import json
import logging
import os
import re
from typing import Any

import httpx
import uvicorn
import websockets

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


async def ha_ws_command(command_type: str) -> Any:
    ws_url = HA_URL.replace("http://", "ws://", 1).replace("https://", "wss://", 1) + "/api/websocket"

    try:
        async with websockets.connect(ws_url, open_timeout=HA_TIMEOUT, close_timeout=HA_TIMEOUT) as websocket:
            hello = json.loads(await asyncio.wait_for(websocket.recv(), timeout=HA_TIMEOUT))
            if hello.get("type") != "auth_required":
                raise RuntimeError("Unexpected Home Assistant WebSocket handshake")

            await websocket.send(json.dumps({
                "type": "auth",
                "access_token": HA_TOKEN,
            }))
            auth = json.loads(await asyncio.wait_for(websocket.recv(), timeout=HA_TIMEOUT))
            if auth.get("type") != "auth_ok":
                raise RuntimeError("Home Assistant WebSocket authentication failed")

            await websocket.send(json.dumps({
                "id": 1,
                "type": command_type,
            }))

            while True:
                message = json.loads(await asyncio.wait_for(websocket.recv(), timeout=HA_TIMEOUT))
                if message.get("id") != 1:
                    continue
                if not message.get("success"):
                    raise RuntimeError(f"Home Assistant WebSocket command failed: {command_type}")
                return message.get("result")
    except asyncio.TimeoutError as exc:
        raise RuntimeError(f"Home Assistant WebSocket request timed out after {HA_TIMEOUT:.0f}s") from exc
    except websockets.WebSocketException as exc:
        logger.warning("Home Assistant WebSocket request failed: %s", type(exc).__name__)
        raise RuntimeError("Home Assistant WebSocket request failed") from exc


def normalize(value: str) -> str:
    value = value.casefold().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def search_score(query: str, entity: dict[str, Any], registry_text: str = "") -> int:
    normalized_query = normalize(query)
    if not normalized_query:
        return 0

    terms = normalized_query.split()
    searchable = {
        "friendly_name": normalize(entity.get("friendly_name", "")),
        "entity_id": normalize(entity.get("entity_id", "")),
        "device_class": normalize(entity.get("device_class", "")),
        "state": normalize(str(entity.get("state", ""))),
        "domain": normalize(entity.get("domain", "")),
        "registry": normalize(registry_text),
    }

    score = 0
    for term in terms:
        if term in searchable["friendly_name"]:
            score += 100
        elif term in searchable["registry"]:
            score += 90
        elif term in searchable["entity_id"]:
            score += 70
        elif term in searchable["device_class"]:
            score += 50
        elif term in searchable["domain"]:
            score += 30
        elif term in searchable["state"]:
            score += 5

    if normalized_query == searchable["friendly_name"]:
        score += 100
    if normalized_query == searchable["entity_id"]:
        score += 80
    return score


def registry_text(entity_registry_entry: dict[str, Any], device: dict[str, Any] | None) -> str:
    values = [
        entity_registry_entry.get("name"),
        entity_registry_entry.get("original_name"),
        entity_registry_entry.get("platform"),
    ]
    if device:
        values.extend([
            device.get("name"),
            device.get("name_by_user"),
            device.get("manufacturer"),
            device.get("model"),
            device.get("model_id"),
            device.get("sw_version"),
            device.get("hw_version"),
        ])
    return " ".join(str(value) for value in values if value)


async def load_registries() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        entity_registry, device_registry = await asyncio.gather(
            ha_ws_command("config/entity_registry/list"),
            ha_ws_command("config/device_registry/list"),
        )
        return entity_registry or [], device_registry or []
    except RuntimeError as exc:
        logger.warning("Could not load Home Assistant registries; using state data only: %s", exc)
        return [], []


async def build_entity_index() -> list[dict[str, Any]]:
    states = await ha_get("/api/states")
    entity_registry, device_registry = await load_registries()

    devices_by_id = {
        device.get("id"): device
        for device in device_registry
        if device.get("id")
    }
    registry_by_entity_id = {
        entry.get("entity_id"): entry
        for entry in entity_registry
        if entry.get("entity_id")
    }

    indexed = []
    for state in states:
        entity_id = state.get("entity_id", "")
        attributes = state.get("attributes", {}) or {}
        registry_entry = registry_by_entity_id.get(entity_id, {})
        device = devices_by_id.get(registry_entry.get("device_id"))
        domain = entity_id.split(".", 1)[0] if "." in entity_id else ""

        indexed.append({
            "entity_id": entity_id,
            "friendly_name": attributes.get("friendly_name", ""),
            "state": state.get("state"),
            "unit_of_measurement": attributes.get("unit_of_measurement"),
            "device_class": attributes.get("device_class"),
            "domain": domain,
            "device_name": (device or {}).get("name_by_user") or (device or {}).get("name"),
            "manufacturer": (device or {}).get("manufacturer"),
            "model": (device or {}).get("model"),
            "registry_text": registry_text(registry_entry, device),
        })

    return indexed


async def list_tools(context, params) -> ListToolsResult:
    return ListToolsResult(
        tools=[
            Tool(
                name="search_entities",
                description=(
                    "Search Home Assistant entities by entity ID, friendly name, device name, "
                    "manufacturer, model, device class, domain or current state. Use this before "
                    "get_entity_state when the exact entity ID is unknown."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search terms such as 'vorlauf', 'temperature', 'drucker', 'HP' or 'carel'.",
                        }
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="get_entity_state",
                description="Get the current state and attributes of one Home Assistant entity.",
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

        indexed_entities = await build_entity_index()
        results = []

        for entity in indexed_entities:
            registry_text_value = entity.pop("registry_text", "")
            score = search_score(query, entity, registry_text_value)
            if score > 0:
                entity["_score"] = score
                results.append(entity)

        results.sort(key=lambda item: (-item.pop("_score"), item["entity_id"]))
        logger.info("Entity search '%s' returned %d results", query, len(results))

        for result in results:
            if not result.get("device_name"):
                result.pop("device_name", None)
            if not result.get("manufacturer"):
                result.pop("manufacturer", None)
            if not result.get("model"):
                result.pop("model", None)

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
    version="1.3.0",
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
