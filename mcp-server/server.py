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
        from yaml.nodes import MappingNode, ScalarNode, SequenceNode
        class HomeAssistantLoader(yaml.SafeLoader): pass
        def construct_tag(loader, tag_suffix, node):
            if isinstance(node, ScalarNode): return loader.construct_scalar(node)
            if isinstance(node, SequenceNode): return loader.construct_sequence(node)
            if isinstance(node, MappingNode): return loader.construct_mapping(node)
            return None
        HomeAssistantLoader.add_multi_constructor("!", construct_tag)
        yaml.load(text, Loader=HomeAssistantLoader)
    except ImportError as exc:
        raise RuntimeError("PyYAML is required for configuration validation") from exc
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
    if path.exists():
        path.replace(backup_path(path))
    path.write_text(content, encoding="utf-8")
    return {"success": True, "file": filename, "backup": backup_path(path).name, "validated": True}


async def ha_ws_command(command_type: str, payload: dict[str, Any] | None = None) -> Any:
    ws_url = HA_URL.replace("http://", "ws://", 1).replace("https://", "wss://", 1) + "/api/websocket"
    async with websockets.connect(ws_url, open_timeout=HA_TIMEOUT, close_timeout=HA_TIMEOUT) as websocket:
        hello = json.loads(await asyncio.wait_for(websocket.recv(), timeout=HA_TIMEOUT))
        if hello.get("type") != "auth_required":
            raise RuntimeError("Unexpected Home Assistant WebSocket handshake")
        await websocket.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
        auth = json.loads(await asyncio.wait_for(websocket.recv(), timeout=HA_TIMEOUT))
        if auth.get("type") != "auth_ok":
            raise RuntimeError("Home Assistant WebSocket authentication failed")
        message = {"id": 1, "type": command_type}
        if payload:
            message.update(payload)
        await websocket.send(json.dumps(message))
        while True:
            response = json.loads(await asyncio.wait_for(websocket.recv(), timeout=HA_TIMEOUT))
            if response.get("id") == 1:
                if not response.get("success"):
                    error = response.get("error", {})
                    raise RuntimeError(f"Home Assistant WebSocket command failed: {error.get('message', command_type)}")
                return response.get("result")


async def list_ha_services() -> list[dict[str, Any]]:
    result = await ha_ws_command("get_services")
    services = []
    for domain, domain_services in (result or {}).items():
        for service, definition in (domain_services or {}).items():
            services.append({"service": f"{domain}.{service}", "name": definition.get("name") if isinstance(definition, dict) else None, "description": definition.get("description") if isinstance(definition, dict) else None})
    return sorted(services, key=lambda item: item["service"])


async def list_dashboards() -> list[dict[str, Any]]:
    return await ha_ws_command("lovelace/dashboards/list") or []


async def get_dashboard(url_path: str | None) -> dict[str, Any]:
    payload = {} if url_path in (None, "", "lovelace") else {"url_path": url_path}
    return await ha_ws_command("lovelace/config", payload) or {}


async def save_dashboard(url_path: str | None, config: dict[str, Any]) -> Any:
    payload = {"config": config}
    if url_path not in (None, "", "lovelace"):
        payload["url_path"] = url_path
    return await ha_ws_command("lovelace/config/save", payload)


async def create_dashboard(url_path: str, title: str, icon: str = "mdi:view-dashboard", show_in_sidebar: bool = True, require_admin: bool = False) -> Any:
    if "-" not in url_path:
        raise ValueError("Dashboard url_path must contain a hyphen, e.g. printer-dashboard")
    payload = {"url_path": url_path, "title": title, "icon": icon, "show_in_sidebar": show_in_sidebar, "require_admin": require_admin}
    return await ha_ws_command("lovelace/dashboards/create", payload)


def normalize(value: Any) -> str:
    value = "" if value is None else str(value)
    value = value.casefold().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def search_score(query: str, entity: dict[str, Any], registry_text: str = "") -> int:
    terms = normalize(query).split()
    if not terms: return 0
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
    if device: values += [device.get("name"), device.get("name_by_user"), device.get("manufacturer"), device.get("model"), device.get("model_id")]
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
        indexed.append({"entity_id": entity_id, "friendly_name": attrs.get("friendly_name", ""), "state": state.get("state"), "unit_of_measurement": attrs.get("unit_of_measurement"), "device_class": attrs.get("device_class"), "domain": entity_id.split(".", 1)[0] if "." in entity_id else "", "device_name": (device or {}).get("name_by_user") or (device or {}).get("name"), "manufacturer": (device or {}).get("manufacturer"), "model": (device or {}).get("model"), "registry_text": registry_text(entry, device)})
    return indexed


async def list_tools(context, params) -> ListToolsResult:
    tools = [
        Tool(name="search_entities", description="Search Home Assistant entities. Empty query lists available entities.", inputSchema={"type":"object","properties":{"query":{"type":"string"}}}),
        Tool(name="get_entity_state", description="Get the current state and attributes of one Home Assistant entity.", inputSchema={"type":"object","properties":{"entity_id":{"type":"string"}},"required":["entity_id"]}),
        Tool(name="list_services", description="List services currently registered by Home Assistant. Use this to discover available actions before attempting a service call.", inputSchema={"type":"object","properties":{"query":{"type":"string","description":"Optional filter such as printer, print, light or climate."}}}),
        Tool(name="call_service", description="Call an allowed Home Assistant service to control a device or entity.", inputSchema={"type":"object","properties":{"domain":{"type":"string"},"service":{"type":"string"},"service_data":{"type":"object"}},"required":["domain","service","service_data"]}),
        Tool(name="configuration_status", description="Show whether configuration editing is enabled and which YAML files are allowed.", inputSchema={"type":"object","properties":{}}),
        Tool(name="read_config", description="Read an allowed Home Assistant YAML configuration file. Requires configuration editing to be enabled.", inputSchema={"type":"object","properties":{"filename":{"type":"string","enum":sorted(ALLOWED_CONFIG_FILES)}},"required":["filename"]}),
        Tool(name="update_config", description="Replace an allowed Home Assistant YAML configuration file. The new YAML is validated and the existing file is backed up before writing. Requires configuration editing to be enabled.", inputSchema={"type":"object","properties":{"filename":{"type":"string","enum":sorted(ALLOWED_CONFIG_FILES)},"content":{"type":"string"}},"required":["filename","content"]}),
        Tool(name="list_dashboards", description="List Home Assistant Lovelace dashboards. Requires configuration editing to be enabled.", inputSchema={"type":"object","properties":{}}),
        Tool(name="read_dashboard", description="Read the current Lovelace dashboard configuration. Requires configuration editing to be enabled.", inputSchema={"type":"object","properties":{"url_path":{"type":"string","description":"Dashboard URL path. Omit for the default Overview dashboard."}}}),
        Tool(name="create_dashboard", description="Create a storage-mode Lovelace dashboard. Requires configuration editing to be enabled. The URL path must contain a hyphen.", inputSchema={"type":"object","properties":{"url_path":{"type":"string"},"title":{"type":"string"},"icon":{"type":"string"},"show_in_sidebar":{"type":"boolean"},"require_admin":{"type":"boolean"}},"required":["url_path","title"]}),
        Tool(name="update_dashboard", description="Save a complete storage-mode Lovelace dashboard configuration. Read the dashboard first, preserve unrelated content, then make the smallest requested change. Requires configuration editing to be enabled.", inputSchema={"type":"object","properties":{"url_path":{"type":"string"},"config":{"type":"object"}},"required":["url_path","config"]}),
    ]
    return ListToolsResult(tools=tools)


async def call_tool(context, params) -> CallToolResult:
    args = params.arguments or {}
    if params.name == "search_entities":
        query = str(args.get("query", "")).strip(); entities = await build_entity_index(); results = entities
        if query:
            scored=[]
            for entity in entities:
                score=search_score(query, entity, entity.get("registry_text", ""))
                if score: entity["_score"]=score; scored.append(entity)
            scored.sort(key=lambda x:(-x.pop("_score"),x["entity_id"])); results=scored
        for result in results:
            result.pop("registry_text", None)
            for key in ("device_name","manufacturer","model"):
                if not result.get(key): result.pop(key,None)
        return CallToolResult(content=[TextContent(type="text",text=json.dumps(results[:MAX_SEARCH_RESULTS],ensure_ascii=False,indent=2))])

    if params.name == "get_entity_state":
        entity_id=str(args.get("entity_id","")).strip()
        if not re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+",entity_id): raise ValueError("invalid entity_id")
        return CallToolResult(content=[TextContent(type="text",text=json.dumps(await ha_get(f"/api/states/{entity_id}"),ensure_ascii=False,indent=2))])

    if params.name == "list_services":
        query=normalize(args.get("query","")); services=await list_ha_services()
        if query:
            terms=query.split(); services=[s for s in services if all(t in normalize(f"{s['service']} {s.get('name') or ''} {s.get('description') or ''}") for t in terms)]
        return CallToolResult(content=[TextContent(type="text",text=json.dumps(services,ensure_ascii=False,indent=2))])

    if params.name == "call_service":
        result=await ha_call_service(str(args.get("domain","")).strip().lower(),str(args.get("service","")).strip().lower(),args.get("service_data",{}))
        return CallToolResult(content=[TextContent(type="text",text=json.dumps({"success":True,"result":result},ensure_ascii=False,indent=2))])

    if params.name == "configuration_status":
        return CallToolResult(content=[TextContent(type="text",text=json.dumps({"enabled":ALLOW_CONFIGURATION,"allowed_files":sorted(ALLOWED_CONFIG_FILES),"dashboard_management":ALLOW_CONFIGURATION},indent=2))])

    if params.name == "read_config": return CallToolResult(content=[TextContent(type="text",text=read_config_file(args.get("filename","")))])
    if params.name == "update_config": return CallToolResult(content=[TextContent(type="text",text=json.dumps(update_config_file(args.get("filename",""),args.get("content","")),indent=2))])

    if params.name in {"list_dashboards","read_dashboard","create_dashboard","update_dashboard"} and not ALLOW_CONFIGURATION:
        raise PermissionError("Dashboard management is disabled. Enable MCP_ALLOW_CONFIGURATION=true")
    if params.name == "list_dashboards":
        return CallToolResult(content=[TextContent(type="text",text=json.dumps(await list_dashboards(),ensure_ascii=False,indent=2))])
    if params.name == "read_dashboard":
        return CallToolResult(content=[TextContent(type="text",text=json.dumps(await get_dashboard(args.get("url_path")),ensure_ascii=False,indent=2))])
    if params.name == "create_dashboard":
        result=await create_dashboard(str(args["url_path"]).strip(),str(args["title"]).strip(),str(args.get("icon") or "mdi:view-dashboard"),bool(args.get("show_in_sidebar",True)),bool(args.get("require_admin",False)))
        return CallToolResult(content=[TextContent(type="text",text=json.dumps({"success":True,"dashboard":result},ensure_ascii=False,indent=2))])
    if params.name == "update_dashboard":
        config=args.get("config")
        if not isinstance(config,dict): raise ValueError("config must be an object")
        if "views" not in config and "strategy" not in config: raise ValueError("Dashboard config must contain views or strategy")
        result=await save_dashboard(str(args["url_path"]).strip(),config)
        return CallToolResult(content=[TextContent(type="text",text=json.dumps({"success":True,"result":result},ensure_ascii=False,indent=2))])

    raise ValueError(f"Unknown tool: {params.name}")


server=Server("home-assistant-mcp",version="1.7.0",on_list_tools=list_tools,on_call_tool=call_tool)

async def main():
    app=server.streamable_http_app(streamable_http_path="/mcp",host="0.0.0.0",stateless_http=True)
    await uvicorn.Server(uvicorn.Config(app,host="0.0.0.0",port=8000,log_level="info")).serve()

if __name__=="__main__": asyncio.run(main())
