import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import httpx
import uvicorn
import websockets
from mcp.server import Server
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("home-assistant-mcp")

HA_URL = os.environ.get("HA_URL", "").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
HA_TIMEOUT = float(os.environ.get("HA_TIMEOUT_SECONDS", "15"))
MAX_SEARCH_RESULTS = int(os.environ.get("MAX_SEARCH_RESULTS", "50"))
CONFIG_ROOT = Path(os.environ.get("HA_CONFIG_PATH", "/config")).resolve()
ALLOW_CONFIGURATION = os.environ.get("MCP_ALLOW_CONFIGURATION", "false").strip().lower() in {"1", "true", "yes", "on"}
ALLOWED_CONFIG_FILES = {"configuration.yaml", "automations.yaml", "scripts.yaml", "scenes.yaml"}

DEFAULT_ALLOWED_SERVICES = {
    "light.turn_on", "light.turn_off", "light.toggle", "switch.turn_on", "switch.turn_off", "switch.toggle",
    "climate.set_temperature", "cover.open_cover", "cover.close_cover", "cover.stop_cover",
    "fan.turn_on", "fan.turn_off", "fan.toggle", "media_player.media_play", "media_player.media_pause",
    "media_player.media_stop", "media_player.volume_set", "scene.turn_on", "script.turn_on",
    "automation.turn_on", "automation.turn_off", "automation.toggle",
}
_raw_allowed_services = os.environ.get("MCP_ALLOWED_SERVICES", "")
ALLOWED_SERVICES = ({item.strip().lower() for item in _raw_allowed_services.split(",") if item.strip()}
                    if _raw_allowed_services.strip() else DEFAULT_ALLOWED_SERVICES)

if not HA_URL:
    raise RuntimeError("HA_URL is not configured")
if not HA_TOKEN:
    raise RuntimeError("HA_TOKEN is not configured")
if not 1 <= MAX_SEARCH_RESULTS <= 200:
    raise RuntimeError("MAX_SEARCH_RESULTS must be between 1 and 200")


def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}


async def ha_get(path: str) -> Any:
    try:
        async with httpx.AsyncClient(timeout=HA_TIMEOUT) as client:
            response = await client.get(f"{HA_URL}{path}", headers=auth_headers())
            response.raise_for_status()
            return response.json()
    except httpx.TimeoutException as exc:
        raise RuntimeError(f"Home Assistant request timed out after {HA_TIMEOUT:.0f}s") from exc
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(f"Home Assistant returned HTTP {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError("Home Assistant request failed") from exc


async def ha_call_service(domain: str, service: str, service_data: dict[str, Any]) -> Any:
    service_key = f"{domain}.{service}".lower()
    if service_key not in ALLOWED_SERVICES:
        raise PermissionError(f"Service '{service_key}' is not allowed")
    if not re.fullmatch(r"[a-z0-9_]+", domain) or not re.fullmatch(r"[a-z0-9_]+", service):
        raise ValueError("Invalid Home Assistant service")
    if not isinstance(service_data, dict):
        raise ValueError("service_data must be an object")
    entity_ids = service_data.get("entity_id")
    if entity_ids is not None:
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]
            service_data = {**service_data, "entity_id": entity_ids}
        if not isinstance(entity_ids, list) or not entity_ids:
            raise ValueError("entity_id must be a non-empty string or list")
        for entity_id in entity_ids:
            if not isinstance(entity_id, str) or not re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+", entity_id):
                raise ValueError("Invalid entity_id in service_data")
    async with httpx.AsyncClient(timeout=HA_TIMEOUT) as client:
        response = await client.post(f"{HA_URL}/api/services/{domain}/{service}", headers=auth_headers(), json=service_data)
        response.raise_for_status()
        return response.json()


def config_file_path(filename: str) -> Path:
    filename = str(filename or "").strip()
    if filename not in ALLOWED_CONFIG_FILES:
        raise PermissionError(f"Configuration file '{filename}' is not allowed")
    path = (CONFIG_ROOT / filename).resolve()
    if path.parent != CONFIG_ROOT:
        raise PermissionError("Invalid configuration path")
    return path


def backup_path(path: Path) -> Path:
    return path.with_name(path.name + ".agent-backup")


def validate_yaml(text: str) -> None:
    try:
        import yaml
        yaml.safe_load(text)
    except ImportError:
        raise RuntimeError("PyYAML is required for configuration validation")
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML: {exc}") from exc


def read_config_file(filename: str) -> str:
    if not ALLOW_CONFIGURATION:
        raise PermissionError("Configuration editing is disabled. Enable MCP_ALLOW_CONFIGURATION=true")
    path = config_file_path(filename)
    if not path.exists():
        raise FileNotFoundError(f"Configuration file '{filename}' does not exist")
    return path.read_text(encoding="utf-8")


def update_config_file(filename: str, content: str) -> dict[str, Any]:
    if not ALLOW_CONFIGURATION:
        raise PermissionError("Configuration editing is disabled. Enable MCP_ALLOW_CONFIGURATION=true")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("content must be a non-empty string")
    path = config_file_path(filename)
    validate_yaml(content)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.replace(backup_path(path))
    path.write_text(content, encoding="utf-8")
    return {"success": True, "file": filename, "backup": str(backup_path(path).name), "validated": True}


async def ha_ws_command(command_type: str) -> Any:
    ws_url = HA_URL.replace("http://", "ws://", 1).replace("https://", "wss://", 1) + "/api/websocket"
    async with websockets.connect(ws_url, open_timeout=HA_TIMEOUT, close_timeout=HA_TIMEOUT) as websocket:
        hello = json.loads(await asyncio.wait_for(websocket.recv(), timeout=HA_TIMEOUT))
        if hello.get("type") != "auth_required":
            raise RuntimeError("Unexpected Home Assistant WebSocket handshake")
        await websocket.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
        auth = json.loads(await asyncio.wait_for(websocket.recv(), timeout=HA_TIMEOUT))
        if auth.get("type") != "auth_ok":
            raise RuntimeError("Home Assistant WebSocket authentication failed")
        await websocket.send(json.dumps({"id": 1, "type": command_type}))
        while True:
            message = json.loads(await asyncio.wait_for(websocket.recv(), timeout=HA_TIMEOUT))
            if message.get("id") == 1:
                if not message.get("success"):
                    raise RuntimeError(f"Home Assistant WebSocket command failed: {command_type}")
                return message.get("result")


def normalize(value: Any) -> str:
    value = "" if value is None else str(value)
    value = value.casefold().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def search_score(query: str, entity: dict[str, Any], registry_text: str = "") -> int:
    terms = normalize(query).split()
    if not terms:
        return 0
    searchable = {k: normalize(entity.get(k, "")) for k in ("friendly_name", "entity_id", "device_class", "state", "domain")}
    searchable["registry"] = normalize(registry_text)
    score = 0
    for term in terms:
        if term in searchable["friendly_name"]: score += 100
        elif term in searchable["registry"]: score += 90
        elif term in searchable["entity_id"]: score += 70
        elif term in searchable["device_class"]: score += 50
        elif term in searchable["domain"]: score += 30
        elif term in searchable["state"]: score += 5
    return score


def registry_text(entry: dict[str, Any], device: dict[str, Any] | None) -> str:
    values = [entry.get("name"), entry.get("original_name"), entry.get("platform")]
    if device:
        values += [device.get("name"), device.get("name_by_user"), device.get("manufacturer"), device.get("model"), device.get("model_id")]
    return " ".join(str(v) for v in values if v)


async def build_entity_index() -> list[dict[str, Any]]:
    states, registries = await asyncio.gather(ha_get("/api/states"), ha_ws_command("config/entity_registry/list"))
    devices = await ha_ws_command("config/device_registry/list")
    by_device = {d.get("id"): d for d in devices or [] if d.get("id")}
    by_entity = {e.get("entity_id"): e for e in registries or [] if e.get("entity_id")}
    indexed = []
    for state in states:
        entity_id = state.get("entity_id", "")
        attrs = state.get("attributes", {}) or {}
        entry = by_entity.get(entity_id, {})
        device = by_device.get(entry.get("device_id"))
        indexed.append({
            "entity_id": entity_id, "friendly_name": attrs.get("friendly_name", ""), "state": state.get("state"),
            "unit_of_measurement": attrs.get("unit_of_measurement"), "device_class": attrs.get("device_class"),
            "domain": entity_id.split(".", 1)[0] if "." in entity_id else "",
            "device_name": (device or {}).get("name_by_user") or (device or {}).get("name"),
            "manufacturer": (device or {}).get("manufacturer"), "model": (device or {}).get("model"),
            "registry_text": registry_text(entry, device),
        })
    return indexed


async def list_tools(context, params) -> ListToolsResult:
    tools = [
        Tool(name="search_entities", description="Search Home Assistant entities. Empty query lists available entities.", inputSchema={"type": "object", "properties": {"query": {"type": "string"}}}),
        Tool(name="get_entity_state", description="Get the current state and attributes of one Home Assistant entity.", inputSchema={"type": "object", "properties": {"entity_id": {"type": "string"}}, "required": ["entity_id"]}),
        Tool(name="call_service", description="Call an allowed Home Assistant service to control a device or entity.", inputSchema={"type": "object", "properties": {"domain": {"type": "string"}, "service": {"type": "string"}, "service_data": {"type": "object"}}, "required": ["domain", "service", "service_data"]}),
        Tool(name="configuration_status", description="Show whether configuration editing is enabled and which YAML files are allowed.", inputSchema={"type": "object", "properties": {}}),
        Tool(name="read_config", description="Read an allowed Home Assistant YAML configuration file. Requires configuration editing to be enabled.", inputSchema={"type": "object", "properties": {"filename": {"type": "string", "enum": sorted(ALLOWED_CONFIG_FILES)}}, "required": ["filename"]}),
        Tool(name="update_config", description="Replace an allowed Home Assistant YAML configuration file. The new YAML is validated and the existing file is backed up before writing. Requires configuration editing to be enabled.", inputSchema={"type": "object", "properties": {"filename": {"type": "string", "enum": sorted(ALLOWED_CONFIG_FILES)}, "content": {"type": "string"}}, "required": ["filename", "content"]}),
    ]
    return ListToolsResult(tools=tools)


async def call_tool(context, params) -> CallToolResult:
    if params.name == "search_entities":
        query = str(params.arguments.get("query", "")).strip()
        entities = await build_entity_index()
        if query:
            results = []
            for entity in entities:
                score = search_score(query, entity, entity.pop("registry_text", ""))
                if score > 0:
                    entity["_score"] = score
                    results.append(entity)
            results.sort(key=lambda x: (-x.pop("_score"), x["entity_id"]))
        else:
            results = entities
        for result in results:
            result.pop("registry_text", None)
            for key in ("device_name", "manufacturer", "model"):
                if not result.get(key): result.pop(key, None)
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(results[:MAX_SEARCH_RESULTS], ensure_ascii=False, indent=2))])

    if params.name == "get_entity_state":
        entity_id = str(params.arguments.get("entity_id", "")).strip()
        if not re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+", entity_id): raise ValueError("invalid entity_id")
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(await ha_get(f"/api/states/{entity_id}"), ensure_ascii=False, indent=2))])

    if params.name == "call_service":
        result = await ha_call_service(str(params.arguments.get("domain", "")).strip().lower(), str(params.arguments.get("service", "")).strip().lower(), params.arguments.get("service_data", {}))
        return CallToolResult(content=[TextContent(type="text", text=json.dumps({"success": True, "result": result}, ensure_ascii=False, indent=2))])

    if params.name == "configuration_status":
        return CallToolResult(content=[TextContent(type="text", text=json.dumps({"enabled": ALLOW_CONFIGURATION, "allowed_files": sorted(ALLOWED_CONFIG_FILES), "config_root": str(CONFIG_ROOT)}, indent=2))])

    if params.name == "read_config":
        return CallToolResult(content=[TextContent(type="text", text=read_config_file(params.arguments.get("filename", "")))])

    if params.name == "update_config":
        result = update_config_file(params.arguments.get("filename", ""), params.arguments.get("content", ""))
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(result, indent=2))])

    raise ValueError(f"Unknown tool: {params.name}")


server = Server("home-assistant-mcp", version="1.5.0", on_list_tools=list_tools, on_call_tool=call_tool)


async def main():
    app = server.streamable_http_app(streamable_http_path="/mcp", host="0.0.0.0", stateless_http=True)
    await uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")).serve()


if __name__ == "__main__":
    asyncio.run(main())
