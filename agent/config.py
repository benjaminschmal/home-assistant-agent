import json
import os
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(os.environ.get("AGENT_CONFIG_PATH", "/data/config.json"))

DEFAULT_CONFIG: dict[str, Any] = {
    "setup_complete": False,
    "ai_provider": "openai",
    "openai_model": os.environ.get("OPENAI_MODEL", "gpt-5"),
    "openai_api_key": os.environ.get("OPENAI_API_KEY", ""),
    "backend": "homeassistant",
    "homeassistant": {
        "mcp_url": os.environ.get("MCP_URL", ""),
        "ha_url": os.environ.get("HA_URL", ""),
        "ha_token": os.environ.get("HA_TOKEN", ""),
    },
    "salesforce": {
        "instance_url": "",
        "client_id": "",
        "client_secret": "",
    },
    "agent": {
        "mcp_timeout_seconds": int(os.environ.get("MCP_TIMEOUT_SECONDS", "15")),
        "openai_timeout_seconds": int(os.environ.get("OPENAI_TIMEOUT_SECONDS", "60")),
        "max_tool_rounds": int(os.environ.get("MAX_TOOL_ROUNDS", "5")),
    },
}


def _merge(default: Any, current: Any) -> Any:
    if isinstance(default, dict) and isinstance(current, dict):
        return {key: _merge(value, current.get(key)) for key, value in default.items()}
    return default if current is None else current


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return _merge(DEFAULT_CONFIG, data)
    except (OSError, json.JSONDecodeError):
        return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(config: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = CONFIG_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(CONFIG_PATH)


def public_config(config: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(config))
    if result.get("openai_api_key"):
        result["openai_api_key"] = "********"
    result.get("homeassistant", {}).pop("ha_token", None)
    result.get("salesforce", {}).pop("client_secret", None)
    return result
