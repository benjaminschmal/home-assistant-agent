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
DEFAULT_ALLOWED_SERVICES = {"light.turn_on","light.turn_off","light.toggle","switch.turn_on","switch.turn_off","switch.toggle","climate.set_temperature","cover.open_cover","cover.close_cover","cover.stop_cover","fan.turn_on","fan.turn_off","fan.toggle","media_player.media_play","media_player.media_pause","media_player.media_stop","media_player.volume_set","scene.turn_on","script.turn_on","automation.turn_on","automation.off","automation.toggle"}
_raw_allowed_services = os.environ.get("MCP_ALLOWED_SERVICES", "")
ALLOWED_SERVICES = ({x.strip().lower() for x in _raw_allowed_services.split(",") if x.strip()} if _raw_allowed_services.strip() else DEFAULT_ALLOWED_SERVICES)
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

async def ha_get_optional(path: str) -> Any | None:
    try:
        return await ha_get(path)
    except RuntimeError:
        return None

async def ha_post(path: str, payload: dict[str, Any]) -> Any:
    try:
        async with httpx.AsyncClient(timeout=HA_TIMEOUT) as client:
            response = await client.post(f"{HA_URL}{path}", headers=auth_headers(), json=payload)
            response.raise_for_status()
            return response.json()
    except httpx.TimeoutException as exc:
        raise RuntimeError(f"Home Assistant request timed out after {HA_TIMEOUT:.0f}s") from exc
    except httpx.HTTPStatusError as exc:
        detail = ""
        try: detail = response.json().get("message", "")
        except Exception: pass
        raise RuntimeError(f"Home Assistant returned HTTP {exc.response.status_code}{(': ' + detail) if detail else ''}") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError("Home Assistant request failed") from exc

async def ha_delete(path: str) -> Any:
    async with httpx.AsyncClient(timeout=HA_TIMEOUT) as client:
        response = await client.delete(f"{HA_URL}{path}", headers=auth_headers())
        response.raise_for_status()
        return response.json() if response.content else {"success": True}

async def get_home_assistant_info() -> dict[str, Any]:
    config = await ha_get("/api/config")
    components = {str(component).lower() for component in (config.get("components") or [])}
    supervisor_available = "hassio" in components
    return {"home_assistant_version": config.get("version"), "location_name": config.get("location_name"), "time_zone": config.get("time_zone"), "supervisor_available": supervisor_available, "addon_store_available": supervisor_available, "installation_family": "supervised_or_home_assistant_os" if supervisor_available else "core_without_supervisor", "capabilities": {"core_api": True, "configuration_yaml": True, "lovelace_dashboards": True, "energy_dashboard": True, "integration_config_flows": True, "supervisor": supervisor_available, "addons": supervisor_available}}

async def ha_call_service(domain: str, service: str, service_data: dict[str, Any]) -> Any:
    key = f"{domain}.{service}".lower()
    if key not in ALLOWED_SERVICES: raise PermissionError(f"Service '{key}' is not allowed")
    if not re.fullmatch(r"[a-z0-9_]+", domain) or not re.fullmatch(r"[a-z0-9_]+", service): raise ValueError("Invalid Home Assistant service")
    return await ha_post(f"/api/services/{domain}/{service}", service_data)

async def ha_ws_command(command_type: str, payload: dict[str, Any] | None = None) -> Any:
    ws_url = HA_URL.replace("http://", "ws://", 1).replace("https://", "wss://", 1) + "/api/websocket"
    async with websockets.connect(ws_url, open_timeout=HA_TIMEOUT, close_timeout=HA_TIMEOUT) as websocket:
        hello = json.loads(await asyncio.wait_for(websocket.recv(), timeout=HA_TIMEOUT))
        if hello.get("type") != "auth_required": raise RuntimeError("Unexpected Home Assistant WebSocket handshake")
        await websocket.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
        auth = json.loads(await asyncio.wait_for(websocket.recv(), timeout=HA_TIMEOUT))
        if auth.get("type") != "auth_ok": raise RuntimeError("Home Assistant WebSocket authentication failed")
        message = {"id": 1, "type": command_type}; message.update(payload or {})
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
    return sorted([{"service": f"{d}.{s}", "name": v.get("name") if isinstance(v, dict) else None, "description": v.get("description") if isinstance(v, dict) else None} for d, items in (result or {}).items() for s, v in (items or {}).items()], key=lambda x: x["service"])

async def list_dashboards() -> list[dict[str, Any]]: return await ha_ws_command("lovelace/dashboards/list") or []
async def get_dashboard(url_path: str | None) -> dict[str, Any]: return await ha_ws_command("lovelace/config", {} if url_path in (None, "", "lovelace") else {"url_path": url_path}) or {}
async def save_dashboard(url_path: str | None, config: dict[str, Any]) -> Any:
    payload = {"config": config}
    if url_path not in (None, "", "lovelace"): payload["url_path"] = url_path
    return await ha_ws_command("lovelace/config/save", payload)

async def create_dashboard(url_path: str, title: str, icon: str = "mdi:view-dashboard", show_in_sidebar: bool = True, require_admin: bool = False) -> Any:
    existing = await list_dashboards()
    matches = [d for d in existing if str(d.get("url_path", "")).strip().lower() == url_path.strip().lower()]
    if matches: return {"success": False, "already_exists": True, "message": f'The dashboard URL "{url_path}" is already in use.', "dashboard": matches[0]}
    return await ha_ws_command("lovelace/dashboards/create", {"url_path": url_path, "title": title, "icon": icon, "show_in_sidebar": show_in_sidebar, "require_admin": require_admin})

async def delete_dashboard(dashboard_id: str) -> Any: return await ha_ws_command("lovelace/dashboards/delete", {"dashboard_id": dashboard_id.strip()})

async def get_energy_preferences() -> dict[str, Any]:
    try: result = await ha_ws_command("energy/get_prefs")
    except RuntimeError as exc:
        if str(exc).endswith("No prefs"): return {"configured": False, "energy_sources": [], "device_consumption": [], "device_consumption_water": [], "message": "The Home Assistant Energy Dashboard is not configured yet."}
        raise
    return {"configured": True, **(result or {})}

async def get_energy_info() -> dict[str, Any]: return await ha_ws_command("energy/info") or {}

async def validate_energy_preferences() -> dict[str, Any]:
    try: return await ha_ws_command("energy/validate") or {}
    except RuntimeError as exc:
        if str(exc).endswith("No prefs"): return {"configured": False, "valid": False, "message": "The Home Assistant Energy Dashboard is not configured yet."}
        raise

async def save_energy_preferences(prefs: dict[str, Any]) -> Any:
    required = {"energy_sources", "device_consumption", "device_consumption_water"}
    missing = sorted(required - set(prefs))
    if missing: raise ValueError(f"Energy preferences must contain all keys: {', '.join(missing)}")
    return await ha_ws_command("energy/save_prefs", {k: prefs[k] for k in required})

async def get_hacs_info() -> dict[str, Any]:
    """Return HACS status and only locally installed repositories.

    Avoid the HACS websocket repository-list command because it can return the
    complete HACS catalog and exceed the MCP/WebSocket 1 MiB frame limit.
    The local HACS storage is authoritative for the installed repository set.
    """
    hacs_dir = CONFIG_ROOT / "custom_components" / "hacs"
    manifest_path = hacs_dir / "manifest.json"
    if not (hacs_dir.is_dir() and manifest_path.is_file()):
        return {
            "installed": False,
            "version": None,
            "latest_version": None,
            "update_available": False,
            "installed_repository_count": 0,
            "installed_repositories": [],
            "message": "HACS is not installed in the connected Home Assistant instance.",
        }

    try:
        version = json.loads(manifest_path.read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError):
        version = None

    def version_key(value: Any) -> tuple:
        parts = re.split(r"[.+\-_]", str(value or "").lstrip("vV"))
        return tuple(
            (0, int(match.group(1))) if (match := re.match(r"(\d+)", part)) else (1, part.casefold())
            for part in parts
        )

    latest_version = None
    try:
        async with httpx.AsyncClient(timeout=HA_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(
                "https://api.github.com/repos/hacs/integration/releases/latest",
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "home-assistant-mcp",
                },
            )
            response.raise_for_status()
            latest_version = str(response.json().get("tag_name") or "") or None
    except (httpx.HTTPError, ValueError):
        pass

    repositories_path = CONFIG_ROOT / ".storage" / "hacs.repositories"
    installed: list[dict[str, Any]] = []
    source = "hacs_storage"
    storage_error = None

    try:
        raw = json.loads(repositories_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raw = None
        storage_error = str(exc)

    def repository_records(value: Any):
        """Recursively find repository records without assuming HACS storage shape."""
        if isinstance(value, dict):
            if value.get("full_name"):
                yield value
            for child in value.values():
                yield from repository_records(child)
        elif isinstance(value, list):
            for child in value:
                yield from repository_records(child)

    seen: set[str] = set()
    if raw is not None:
        for repo in repository_records(raw):
            full_name = str(repo.get("full_name") or "")
            if not full_name or full_name.casefold() == "hacs/integration" or full_name in seen:
                continue

            category = repo.get("category")
            domain = repo.get("domain")
            local_path = repo.get("local_path")
            installed_flag = bool(repo.get("installed"))

            # HACS storage versions differ. If the explicit installed flag is
            # absent, verify the local installation on disk.
            if not installed_flag:
                if category == "integration" and domain:
                    installed_flag = (CONFIG_ROOT / "custom_components" / str(domain)).is_dir()
                elif local_path:
                    try:
                        installed_flag = Path(str(local_path)).expanduser().exists()
                    except OSError:
                        installed_flag = False

            if not installed_flag:
                continue

            installed_version = repo.get("installed_version")
            available_version = repo.get("available_version") or repo.get("version")
            pending_upgrade = bool(repo.get("pending_upgrade"))
            update_available = pending_upgrade or bool(
                installed_version and available_version and
                version_key(available_version) > version_key(installed_version)
            )

            installed.append({
                "full_name": full_name,
                "name": repo.get("name"),
                "category": category,
                "domain": domain,
                "description": repo.get("description"),
                "installed": True,
                "installed_version": installed_version,
                "available_version": available_version,
                "update_available": update_available,
                "status": repo.get("status"),
                "pending_upgrade": pending_upgrade,
                "local_path": local_path,
            })
            seen.add(full_name)

    installed.sort(key=lambda item: str(item.get("name") or item.get("full_name") or "").casefold())

    return {
        "installed": True,
        "version": version,
        "latest_version": latest_version,
        "update_available": bool(
            version and latest_version and version_key(latest_version) > version_key(version)
        ),
        "installed_repository_count": len(installed),
        "installed_repositories": installed,
        "source": source,
        "storage_error": storage_error,
        "message": "HACS is installed. Only locally installed HACS repositories are returned.",
    }

async def list_config_entries() -> list[dict[str, Any]]: return await ha_get("/api/config/config_entries/entry")
async def start_config_flow(handler: str, user_input: dict[str, Any] | None = None) -> dict[str, Any]: return await ha_post("/api/config/config_entries/flow", {"handler":handler, **({"data":user_input} if user_input is not None else {})})
async def submit_config_flow(flow_id: str, user_input: dict[str, Any]) -> dict[str, Any]: return await ha_post(f"/api/config/config_entries/flow/{flow_id}", user_input)
async def abort_config_flow(flow_id: str) -> Any: return await ha_delete(f"/api/config/config_entries/flow/{flow_id}")

def config_file_path(filename: str) -> Path:
    if filename not in ALLOWED_CONFIG_FILES: raise PermissionError(f"Configuration file '{filename}' is not allowed")
    return (CONFIG_ROOT / filename).resolve()

def validate_yaml(text: str) -> None:
    import yaml
    class HomeAssistantLoader(yaml.SafeLoader): pass
    def construct_tag(loader, tag_suffix, node):
        if isinstance(node, yaml.nodes.ScalarNode): return loader.construct_scalar(node)
        if isinstance(node, yaml.nodes.SequenceNode): return loader.construct_sequence(node)
        if isinstance(node, yaml.nodes.MappingNode): return loader.construct_mapping(node)
        return None
    HomeAssistantLoader.add_multi_constructor("!", construct_tag)
    try: yaml.load(text, Loader=HomeAssistantLoader)
    except yaml.YAMLError as exc: raise ValueError(f"Invalid YAML: {exc}") from exc

def read_config_file(filename: str) -> str:
    if not ALLOW_CONFIGURATION: raise PermissionError("Configuration editing is disabled. Enable MCP_ALLOW_CONFIGURATION=true")
    path=config_file_path(filename)
    if not path.exists(): raise FileNotFoundError(filename)
    return path.read_text(encoding="utf-8")

def update_config_file(filename: str, content: str) -> dict[str, Any]:
    if not ALLOW_CONFIGURATION: raise PermissionError("Configuration editing is disabled. Enable MCP_ALLOW_CONFIGURATION=true")
    path=config_file_path(filename); validate_yaml(content)
    if path.exists(): path.replace(path.with_name(path.name+".agent-backup"))
    path.write_text(content,encoding="utf-8")
    return {"success":True,"file":filename,"validated":True}

def normalize(value: Any) -> str: return re.sub(r"[^a-z0-9]+"," ",str(value or "").casefold().replace("ä","ae").replace("ö","oe").replace("ü","ue").replace("ß","ss")).strip()

def text_result(value: Any) -> CallToolResult: return CallToolResult(content=[TextContent(type="text",text=json.dumps(value,ensure_ascii=False,indent=2) if not isinstance(value,str) else value)])

async def list_tools(context, params) -> ListToolsResult:
    return ListToolsResult(tools=[Tool(name="get_hacs_info",description="Inspect HACS installation, HACS version, installed HACS repositories and their current/update status directly from HACS without assuming Home Assistant OS or Supervisor.",inputSchema={"type":"object","properties":{}}),Tool(name="get_home_assistant_info",description="Detect connected Home Assistant version and capabilities.",inputSchema={"type":"object","properties":{}}),Tool(name="get_energy_preferences",description="Read Energy Dashboard preferences.",inputSchema={"type":"object","properties":{}}),Tool(name="get_energy_info",description="Read Energy Dashboard metadata.",inputSchema={"type":"object","properties":{}}),Tool(name="validate_energy_preferences",description="Validate Energy Dashboard configuration.",inputSchema={"type":"object","properties":{}}),Tool(name="search_entities",description="Search Home Assistant entities.",inputSchema={"type":"object","properties":{"query":{"type":"string"}}}),Tool(name="get_entity_state",description="Read an entity state.",inputSchema={"type":"object","properties":{"entity_id":{"type":"string"}},"required":["entity_id"]}),Tool(name="list_config_entries",description="List configured integrations.",inputSchema={"type":"object","properties":{"domain":{"type":"string"}}})])

async def call_tool(context, params) -> CallToolResult:
    args=params.arguments or {}
    if params.name=="get_hacs_info": return text_result(await get_hacs_info())
    if params.name=="get_home_assistant_info": return text_result(await get_home_assistant_info())
    if params.name=="get_energy_preferences": return text_result(await get_energy_preferences())
    if params.name=="get_energy_info": return text_result(await get_energy_info())
    if params.name=="validate_energy_preferences": return text_result(await validate_energy_preferences())
    if params.name=="list_config_entries":
        entries=await list_config_entries(); domain=normalize(args.get("domain"));
        if domain: entries=[e for e in entries if normalize(e.get("domain"))==domain]
        return text_result(entries)
    if params.name=="search_entities": return text_result(await ha_get("/api/states"))
    if params.name=="get_entity_state": return text_result(await ha_get(f"/api/states/{args.get('entity_id')}"))
    raise ValueError(f"Unknown tool: {params.name}")

server=Server("home-assistant-mcp",version="1.12.0",on_list_tools=list_tools,on_call_tool=call_tool)

async def main():
    app=server.streamable_http_app(streamable_http_path="/mcp",host="0.0.0.0",stateless_http=True)
    await uvicorn.Server(uvicorn.Config(app,host="0.0.0.0",port=8000,log_level="info")).serve()

if __name__=="__main__": asyncio.run(main())