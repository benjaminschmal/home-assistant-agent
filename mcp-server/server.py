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
        try:
            detail = response.json().get("message", "")
        except Exception:
            pass
        raise RuntimeError(f"Home Assistant returned HTTP {exc.response.status_code}{(': ' + detail) if detail else ''}") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError("Home Assistant request failed") from exc


async def ha_delete(path: str) -> Any:
    async with httpx.AsyncClient(timeout=HA_TIMEOUT) as client:
        response = await client.delete(f"{HA_URL}{path}", headers=auth_headers())
        response.raise_for_status()
        return response.json() if response.content else {"success": True}


async def get_home_assistant_info() -> dict[str, Any]:
    """Detect the connected Home Assistant Core version and exposed platform capabilities.

    The public Core API reliably exposes the Core version and loaded components. The
    presence of the hassio integration is used as a capability signal for Supervisor
    features. We deliberately do not claim an exact host type when Core cannot prove it.
    """
    config = await ha_get("/api/config")
    components = {str(component).lower() for component in (config.get("components") or [])}
    supervisor_available = "hassio" in components
    return {
        "home_assistant_version": config.get("version"),
        "location_name": config.get("location_name"),
        "time_zone": config.get("time_zone"),
        "supervisor_available": supervisor_available,
        "addon_store_available": supervisor_available,
        "installation_family": "supervised_or_home_assistant_os" if supervisor_available else "core_without_supervisor",
        "capabilities": {
            "core_api": True,
            "configuration_yaml": True,
            "lovelace_dashboards": True,
            "energy_dashboard": True,
            "integration_config_flows": True,
            "supervisor": supervisor_available,
            "addons": supervisor_available,
        },
        "detection_notes": [
            "The Core API exposes the Home Assistant Core version and loaded components.",
            "Exact host/container type is not asserted unless the connected Home Assistant exposes a reliable signal.",
            "If supervisor_available is false, do not recommend Home Assistant Add-ons or the Add-on Store.",
        ],
    }


async def ha_call_service(domain: str, service: str, service_data: dict[str, Any]) -> Any:
    key = f"{domain}.{service}".lower()
    if key not in ALLOWED_SERVICES:
        raise PermissionError(f"Service '{key}' is not allowed")
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
    return await ha_post(f"/api/services/{domain}/{service}", service_data)


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
        message.update(payload or {})
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
    for domain, items in (result or {}).items():
        for service, definition in (items or {}).items():
            services.append({"service": f"{domain}.{service}", "name": definition.get("name") if isinstance(definition, dict) else None, "description": definition.get("description") if isinstance(definition, dict) else None})
    return sorted(services, key=lambda x: x["service"])


async def list_dashboards() -> list[dict[str, Any]]:
    return await ha_ws_command("lovelace/dashboards/list") or []


async def get_dashboard(url_path: str | None) -> dict[str, Any]:
    return await ha_ws_command("lovelace/config", {} if url_path in (None, "", "lovelace") else {"url_path": url_path}) or {}


async def save_dashboard(url_path: str | None, config: dict[str, Any]) -> Any:
    payload = {"config": config}
    if url_path not in (None, "", "lovelace"):
        payload["url_path"] = url_path
    return await ha_ws_command("lovelace/config/save", payload)


async def create_dashboard(url_path: str, title: str, icon: str = "mdi:view-dashboard", show_in_sidebar: bool = True, require_admin: bool = False) -> Any:
    if "-" not in url_path:
        raise ValueError("Dashboard url_path must contain a hyphen, e.g. printer-dashboard")
    existing = await list_dashboards()
    matches = [d for d in existing if str(d.get("url_path", "")).strip().lower() == url_path.strip().lower()]
    if matches:
        return {"success": False, "already_exists": True, "message": f'The dashboard URL "{url_path}" is already in use. Use update_dashboard or choose a different url_path.', "dashboard": matches[0]}
    return await ha_ws_command("lovelace/dashboards/create", {"url_path": url_path, "title": title, "icon": icon, "show_in_sidebar": show_in_sidebar, "require_admin": require_admin})


async def delete_dashboard(dashboard_id: str) -> Any:
    if not dashboard_id.strip():
        raise ValueError("dashboard_id is required")
    return await ha_ws_command("lovelace/dashboards/delete", {"dashboard_id": dashboard_id.strip()})


async def get_energy_preferences() -> dict[str, Any]:
    """Read Energy Dashboard preferences without treating an unconfigured dashboard as an error."""
    try:
        result = await ha_ws_command("energy/get_prefs")
    except RuntimeError as exc:
        if str(exc).endswith("No prefs"):
            return {
                "configured": False,
                "energy_sources": [],
                "device_consumption": [],
                "device_consumption_water": [],
                "message": "The Home Assistant Energy Dashboard is not configured yet."
            }
        raise
    return {
        "configured": True,
        **(result or {"energy_sources": [], "device_consumption": [], "device_consumption_water": []})
    }


async def get_energy_info() -> dict[str, Any]:
    return await ha_ws_command("energy/info") or {}


async def validate_energy_preferences() -> dict[str, Any]:
    try:
        return await ha_ws_command("energy/validate") or {}
    except RuntimeError as exc:
        if str(exc).endswith("No prefs"):
            return {"configured": False, "valid": False, "message": "The Home Assistant Energy Dashboard is not configured yet."}
        raise


async def save_energy_preferences(prefs: dict[str, Any]) -> Any:
    if not isinstance(prefs, dict):
        raise ValueError("prefs must be an object")
    required = {"energy_sources", "device_consumption", "device_consumption_water"}
    missing = sorted(required - set(prefs))
    if missing:
        raise ValueError(f"Energy preferences must contain all keys: {', '.join(missing)}. Read them first and preserve unrelated settings.")
    if not all(isinstance(prefs[k], list) for k in required):
        raise ValueError("energy_sources, device_consumption and device_consumption_water must be arrays")
    return await ha_ws_command("energy/save_prefs", {"energy_sources": prefs["energy_sources"], "device_consumption": prefs["device_consumption"], "device_consumption_water": prefs["device_consumption_water"]})


async def get_hacs_info() -> dict[str, Any]:
    """Inspect the optional HACS installation without assuming HA OS or Supervisor."""
    hacs_dir = CONFIG_ROOT / "custom_components" / "hacs"
    manifest_path = hacs_dir / "manifest.json"
    storage_dir = CONFIG_ROOT / ".storage"
    hacs_storage_path = storage_dir / "hacs.hacs"
    repositories_path = storage_dir / "hacs.repositories"
    if not (hacs_dir.is_dir() and manifest_path.is_file()):
        return {"installed": False, "version": None, "latest_version": None, "update_available": False, "installed_repositories": [], "installed_repository_count": 0, "message": "HACS is not installed in the connected Home Assistant configuration directory."}
    version = None
    try:
        version = json.loads(manifest_path.read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError):
        pass
    try:
        raw=json.loads(hacs_storage_path.read_text(encoding="utf-8")); data=raw.get("data",raw) if isinstance(raw,dict) else {}
        version=version or data.get("version")
    except (OSError, ValueError):
        pass
    repositories=[]
    try:
        raw=json.loads(repositories_path.read_text(encoding="utf-8")); data=raw.get("data",raw) if isinstance(raw,dict) else {}
        for repo in data.get("repositories",[]) if isinstance(data,dict) else []:
            if not isinstance(repo,dict): continue
            d=repo.get("data",{}) if isinstance(repo.get("data"),dict) else {}
            if not bool(d.get("installed",repo.get("installed",False))): continue
            repositories.append({k:v for k,v in {"full_name":d.get("full_name") or repo.get("full_name"),"name":d.get("name") or repo.get("name") or d.get("manifest_name"),"category":d.get("category") or repo.get("category"),"installed_version":d.get("installed_version") or repo.get("installed_version"),"latest_version":d.get("last_version") or repo.get("last_version"),"pending_restart":bool(d.get("pending_restart",repo.get("pending_restart",False)))}.items() if v not in (None,"")})
    except (OSError, ValueError):
        pass
    def vk(v):
        out=[]
        for p in re.split(r"[.+\-_]",str(v or "").lstrip("vV")):
            m=re.match(r"(\d+)",p); out.append((0,int(m.group(1))) if m else (1,p.casefold()))
        return tuple(out)
    latest_version=None; latest_url="https://github.com/hacs/integration/releases/latest"
    try:
        async with httpx.AsyncClient(timeout=HA_TIMEOUT,follow_redirects=True) as client:
            r=await client.get("https://api.github.com/repos/hacs/integration/releases/latest",headers={"Accept":"application/vnd.github+json","User-Agent":"home-assistant-mcp"}); r.raise_for_status(); release=r.json(); latest_version=str(release.get("tag_name") or "").strip() or None; latest_url=str(release.get("html_url") or latest_url)
    except (httpx.HTTPError,ValueError):
        pass
    update_available=bool(version and latest_version and vk(latest_version)>vk(version))
    repo_updates=[]
    for repo in repositories:
        iv,lv=repo.get("installed_version"),repo.get("latest_version"); repo["update_available"]=bool(iv and lv and vk(lv)>vk(iv))
        if repo["update_available"]: repo_updates.append(repo.get("full_name") or repo.get("name"))
    return {"installed":True,"version":version,"latest_version":latest_version,"update_available":update_available,"latest_release_url":latest_url,"installed_repository_count":len(repositories),"repositories_with_updates":repo_updates,"installed_repositories":repositories,"storage_detected":{"hacs_hacs":hacs_storage_path.is_file(),"hacs_repositories":repositories_path.is_file()}}


async def list_config_entries() -> list[dict[str, Any]]:
    return await ha_get("/api/config/config_entries/entry")


async def start_config_flow(handler: str, user_input: dict[str, Any] | None = None) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z0-9_]+", handler):
        raise ValueError("Invalid integration domain")
    payload = {"handler": handler}
    if user_input is not None:
        payload["data"] = user_input
    return await ha_post("/api/config/config_entries/flow", payload)


async def submit_config_flow(flow_id: str, user_input: dict[str, Any]) -> dict[str, Any]:
    if not flow_id.strip():
        raise ValueError("flow_id is required")
    if not isinstance(user_input, dict):
        raise ValueError("user_input must be an object")
    return await ha_post(f"/api/config/config_entries/flow/{flow_id.strip()}", user_input)


async def abort_config_flow(flow_id: str) -> Any:
    if not flow_id.strip():
        raise ValueError("flow_id is required")
    return await ha_delete(f"/api/config/config_entries/flow/{flow_id.strip()}")


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
        class HomeAssistantLoader(yaml.SafeLoader):
            pass
        def construct_tag(loader, tag_suffix, node):
            if isinstance(node, ScalarNode):
                return loader.construct_scalar(node)
            if isinstance(node, SequenceNode):
                return loader.construct_sequence(node)
            if isinstance(node, MappingNode):
                return loader.construct_mapping(node)
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


def normalize(value: Any) -> str:
    value = "" if value is None else str(value)
    value = value.casefold().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def registry_text(entry: dict[str, Any], device: dict[str, Any] | None) -> str:
    values = [entry.get("name"), entry.get("original_name"), entry.get("platform")]
    if device:
        values += [device.get("name"), device.get("name_by_user"), device.get("manufacturer"), device.get("model"), device.get("model_id")]
    return " ".join(str(v) for v in values if v)


def search_score(query: str, entity: dict[str, Any], registry: str = "") -> int:
    terms = normalize(query).split()
    searchable = {k: normalize(entity.get(k, "")) for k in ("friendly_name", "entity_id", "device_class", "state", "domain")}
    searchable["registry"] = normalize(registry)
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
    return score


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
        indexed.append({"entity_id": entity_id, "friendly_name": attrs.get("friendly_name", ""), "state": state.get("state"), "unit_of_measurement": attrs.get("unit_of_measurement"), "device_class": attrs.get("device_class"), "state_class": attrs.get("state_class"), "domain": entity_id.split(".", 1)[0] if "." in entity_id else "", "device_name": (device or {}).get("name_by_user") or (device or {}).get("name"), "manufacturer": (device or {}).get("manufacturer"), "model": (device or {}).get("model"), "registry_text": registry_text(entry, device)})
    return indexed


def text_result(value: Any) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(value, ensure_ascii=False, indent=2) if not isinstance(value, str) else value)])


async def list_tools(context, params) -> ListToolsResult:
    tools = [
        Tool(name="get_home_assistant_info", description="Detect the connected Home Assistant Core version and available platform capabilities. Use before platform-dependent advice such as Add-ons, Supervisor, MQTT installation, configuration, updates or backups. Do not assume Home Assistant OS.", inputSchema={"type": "object", "properties": {}}),
        Tool(name="search_entities", description="Search Home Assistant entities. Empty query lists available entities.", inputSchema={"type": "object", "properties": {"query": {"type": "string"}}}),
        Tool(name="get_entity_state", description="Get the current state and attributes of one Home Assistant entity.", inputSchema={"type": "object", "properties": {"entity_id": {"type": "string"}}, "required": ["entity_id"]}),
        Tool(name="list_services", description="List services currently registered by Home Assistant.", inputSchema={"type": "object", "properties": {"query": {"type": "string"}}}),
        Tool(name="call_service", description="Call an allowed Home Assistant service.", inputSchema={"type": "object", "properties": {"domain": {"type": "string"}, "service": {"type": "string"}, "service_data": {"type": "object"}}, "required": ["domain", "service", "service_data"]}),
        Tool(name="configuration_status", description="Show whether configuration editing is enabled.", inputSchema={"type": "object", "properties": {}}),
        Tool(name="read_config", description="Read an allowed Home Assistant YAML configuration file.", inputSchema={"type": "object", "properties": {"filename": {"type": "string", "enum": sorted(ALLOWED_CONFIG_FILES)}}, "required": ["filename"]}),
        Tool(name="update_config", description="Replace an allowed Home Assistant YAML configuration file with validation and backup.", inputSchema={"type": "object", "properties": {"filename": {"type": "string", "enum": sorted(ALLOWED_CONFIG_FILES)}, "content": {"type": "string"}}, "required": ["filename", "content"]}),
        Tool(name="list_dashboards", description="List Home Assistant Lovelace dashboards. Use before creating or deleting dashboards.", inputSchema={"type": "object", "properties": {}}),
        Tool(name="read_dashboard", description="Read a storage-mode Lovelace dashboard configuration.", inputSchema={"type": "object", "properties": {"url_path": {"type": "string"}}}),
        Tool(name="create_dashboard", description="Create a storage-mode Lovelace dashboard. Existing url_path values are detected before creation and returned as a clear warning.", inputSchema={"type": "object", "properties": {"url_path": {"type": "string"}, "title": {"type": "string"}, "icon": {"type": "string"}, "show_in_sidebar": {"type": "boolean"}, "require_admin": {"type": "boolean"}}, "required": ["url_path", "title"]}),
        Tool(name="update_dashboard", description="Save a complete storage-mode Lovelace dashboard configuration. Read it first and preserve unrelated content.", inputSchema={"type": "object", "properties": {"url_path": {"type": "string"}, "config": {"type": "object"}}, "required": ["url_path", "config"]}),
        Tool(name="delete_dashboard", description="Delete a storage-mode Lovelace dashboard. Destructive; list dashboards first and only delete on explicit user request.", inputSchema={"type": "object", "properties": {"dashboard_id": {"type": "string"}}, "required": ["dashboard_id"]}),
        Tool(name="get_energy_preferences", description="Read the Home Assistant built-in Energy Dashboard configuration, including grid, solar, battery and individual consumption sources. Returns configured=false when Energy has not been configured yet.", inputSchema={"type": "object", "properties": {}}),
        Tool(name="get_energy_info", description="Read Home Assistant Energy Dashboard metadata.", inputSchema={"type": "object", "properties": {}}),
        Tool(name="get_hacs_info", description="Inspect HACS installation, version, installed repositories and available updates without assuming Home Assistant OS or Supervisor.", inputSchema={"type": "object", "properties": {}}),
        Tool(name="search_energy_sources", description="Search current Home Assistant entities for sensors suitable for the Energy Dashboard. Returns candidates with unit, device_class and state_class.", inputSchema={"type": "object", "properties": {"query": {"type": "string"}}}),
        Tool(name="validate_energy_preferences", description="Validate the current Home Assistant Energy Dashboard configuration. Returns configured=false when Energy has not been configured yet.", inputSchema={"type": "object", "properties": {}}),
        Tool(name="save_energy_preferences", description="Update the Home Assistant built-in Energy Dashboard. Read current preferences first and preserve unrelated settings.", inputSchema={"type": "object", "properties": {"prefs": {"type": "object"}}, "required": ["prefs"]}),
        Tool(name="list_config_entries", description="List configured Home Assistant integrations/config entries. Use before adding an integration to detect existing entries.", inputSchema={"type": "object", "properties": {"domain": {"type": "string"}}}),
        Tool(name="start_integration_setup", description="Start a Home Assistant integration config flow. Use list_config_entries first.", inputSchema={"type": "object", "properties": {"domain": {"type": "string"}, "user_input": {"type": "object"}}, "required": ["domain"]}),
        Tool(name="submit_integration_setup", description="Submit one step of an active Home Assistant integration config flow.", inputSchema={"type": "object", "properties": {"flow_id": {"type": "string"}, "user_input": {"type": "object"}}, "required": ["flow_id", "user_input"]}),
        Tool(name="abort_integration_setup", description="Abort an active Home Assistant integration config flow.", inputSchema={"type": "object", "properties": {"flow_id": {"type": "string"}}, "required": ["flow_id"]}),
    ]
    return ListToolsResult(tools=tools)


async def call_tool(context, params) -> CallToolResult:
    args = params.arguments or {}
    if params.name == "get_home_assistant_info":
        return text_result(await get_home_assistant_info())
    if params.name == "search_entities":
        query = str(args.get("query", "")).strip()
        entities = await build_entity_index()
        results = entities
        if query:
            scored = []
            for entity in entities:
                score = search_score(query, entity, entity.get("registry_text", ""))
                if score:
                    entity["_score"] = score
                    scored.append(entity)
            scored.sort(key=lambda x: (-x.pop("_score"), x["entity_id"]))
            results = scored
        for result in results:
            result.pop("registry_text", None)
            for key in ("device_name", "manufacturer", "model"):
                if not result.get(key):
                    result.pop(key, None)
        return text_result(results[:MAX_SEARCH_RESULTS])
    if params.name == "get_entity_state":
        entity_id = str(args.get("entity_id", "")).strip()
        if not re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+", entity_id):
            raise ValueError("invalid entity_id")
        return text_result(await ha_get(f"/api/states/{entity_id}"))
    if params.name == "list_services":
        query = normalize(args.get("query", ""))
        services = await list_ha_services()
        if query:
            terms = query.split()
            services = [s for s in services if all(t in normalize(f"{s['service']} {s.get('name') or ''} {s.get('description') or ''}") for t in terms)]
        return text_result(services)
    if params.name == "call_service":
        return text_result({"success": True, "result": await ha_call_service(str(args.get("domain", "")).strip().lower(), str(args.get("service", "")).strip().lower(), args.get("service_data", {}))})
    if params.name == "configuration_status":
        return text_result({"enabled": ALLOW_CONFIGURATION, "allowed_files": sorted(ALLOWED_CONFIG_FILES), "dashboard_management": ALLOW_CONFIGURATION, "integration_config_flows": ALLOW_CONFIGURATION, "energy_dashboard_management": ALLOW_CONFIGURATION})
    if params.name == "read_config":
        return text_result(read_config_file(args.get("filename", "")))
    if params.name == "update_config":
        return text_result(update_config_file(args.get("filename", ""), args.get("content", "")))
    dashboard_tools = {"list_dashboards", "read_dashboard", "create_dashboard", "update_dashboard", "delete_dashboard"}
    energy_tools = {"get_energy_preferences", "get_energy_info", "search_energy_sources", "validate_energy_preferences", "save_energy_preferences"}
    flow_tools = {"list_config_entries", "start_integration_setup", "submit_integration_setup", "abort_integration_setup"}
    if (params.name in dashboard_tools or params.name in flow_tools or params.name in energy_tools) and not ALLOW_CONFIGURATION and params.name in {"create_dashboard", "update_dashboard", "delete_dashboard", "save_energy_preferences", "update_config", "start_integration_setup", "submit_integration_setup", "abort_integration_setup"}:
        raise PermissionError("Configuration management is disabled. Enable MCP_ALLOW_CONFIGURATION=true")
    if params.name == "list_dashboards":
        return text_result(await list_dashboards())
    if params.name == "read_dashboard":
        return text_result(await get_dashboard(args.get("url_path")))
    if params.name == "create_dashboard":
        return text_result(await create_dashboard(str(args["url_path"]).strip(), str(args["title"]).strip(), str(args.get("icon") or "mdi:view-dashboard"), bool(args.get("show_in_sidebar", True)), bool(args.get("require_admin", False))))
    if params.name == "update_dashboard":
        config = args.get("config")
        if not isinstance(config, dict):
            raise ValueError("config must be an object")
        if "views" not in config and "strategy" not in config:
            raise ValueError("Dashboard config must contain views or strategy")
        return text_result({"success": True, "result": await save_dashboard(str(args["url_path"]).strip(), config)})
    if params.name == "delete_dashboard":
        return text_result({"success": True, "result": await delete_dashboard(str(args["dashboard_id"]).strip()), "dashboard_id": str(args["dashboard_id"]).strip()})
    if params.name == "get_energy_preferences":
        return text_result(await get_energy_preferences())
    if params.name == "get_energy_info":
        return text_result(await get_energy_info())
    if params.name == "get_hacs_info":
        return text_result(await get_hacs_info())
    if params.name == "search_energy_sources":
        query = normalize(args.get("query", "energy"))
        entities = await build_entity_index()
        candidates = []
        energy_units = {"kwh", "wh", "mwh", "m³", "m3", "l"}
        energy_classes = {"energy", "power", "gas", "water", "monetary"}
        for entity in entities:
            text = normalize(f"{entity.get('friendly_name','')} {entity.get('entity_id','')} {entity.get('device_class','')} {entity.get('state_class','')} {entity.get('unit_of_measurement','')}")
            unit = str(entity.get("unit_of_measurement") or "").casefold()
            device_class = str(entity.get("device_class") or "").casefold()
            state_class = str(entity.get("state_class") or "").casefold()
            score = 0
            if device_class in energy_classes:
                score += 100
            if unit in energy_units:
                score += 60
            if state_class in {"total", "total_increasing", "measurement"}:
                score += 25
            for term in query.split():
                if term in text:
                    score += 20
            if score:
                candidate = dict(entity)
                candidate["energy_score"] = score
                candidates.append(candidate)
        candidates.sort(key=lambda x: (-x.pop("energy_score"), x["entity_id"]))
        for result in candidates:
            result.pop("registry_text", None)
        return text_result(candidates[:MAX_SEARCH_RESULTS])
    if params.name == "validate_energy_preferences":
        return text_result(await validate_energy_preferences())
    if params.name == "save_energy_preferences":
        return text_result({"success": True, "result": await save_energy_preferences(args.get("prefs", {}))})
    if params.name == "list_config_entries":
        entries = await list_config_entries()
        domain = normalize(args.get("domain", ""))
        if domain:
            entries = [e for e in entries if normalize(e.get("domain")) == domain]
        safe = []
        for e in entries:
            safe.append({k: e.get(k) for k in ("entry_id", "domain", "title", "source", "state", "disabled_by", "pref_disable_new_entities", "pref_disable_polling", "supports_options", "supports_remove_device") if k in e})
        return text_result(safe)
    if params.name == "start_integration_setup":
        domain = str(args["domain"]).strip().lower()
        existing = await list_config_entries()
        matches = [e for e in existing if str(e.get("domain", "")).lower() == domain]
        result = await start_config_flow(domain, args.get("user_input"))
        if matches:
            result = {"warning": "An existing config entry for this integration was found. Verify whether the user really wants another instance.", "existing_entries": [{k: e.get(k) for k in ("entry_id", "domain", "title", "source", "state") if k in e} for e in matches], "flow": result}
        return text_result(result)
    if params.name == "submit_integration_setup":
        return text_result(await submit_config_flow(str(args["flow_id"]), args.get("user_input", {})))
    if params.name == "abort_integration_setup":
        return text_result(await abort_config_flow(str(args["flow_id"])))
    raise ValueError(f"Unknown tool: {params.name}")


server = Server("home-assistant-mcp", version="1.11.1", on_list_tools=list_tools, on_call_tool=call_tool)


async def main():
    app = server.streamable_http_app(streamable_http_path="/mcp", host="0.0.0.0", stateless_http=True)
    await uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")).serve()


if __name__ == "__main__":
    asyncio.run(main())
