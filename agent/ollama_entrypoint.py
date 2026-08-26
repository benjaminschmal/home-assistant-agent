import asyncio
import json
import os
import urllib.request

OPENAI_CONFIGURED = bool(os.environ.get("OPENAI_API_KEY", "").strip())
if not OPENAI_CONFIGURED:
    os.environ["OPENAI_API_KEY"] = "local-only"

import agent as base

base.AGENT_VERSION = "1.13.7"

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:8b").strip()
OLLAMA_TIMEOUT = float(os.environ.get("OLLAMA_TIMEOUT_SECONDS", "120"))
OLLAMA_NUM_THREADS = int(os.environ.get("OLLAMA_NUM_THREADS", "8"))
OLLAMA_TOOL_CHECK_TIMEOUT = float(os.environ.get("OLLAMA_TOOL_CHECK_TIMEOUT_SECONDS", "5"))

if OLLAMA_NUM_THREADS < 1:
    raise RuntimeError("OLLAMA_NUM_THREADS must be at least 1")

ollama_client = base.AsyncOpenAI(
    base_url=f"{OLLAMA_URL}/v1",
    api_key="ollama",
    timeout=OLLAMA_TIMEOUT,
    max_retries=1,
)


def _remove_routes(paths):
    base.app.routes[:] = [
        route
        for route in base.app.routes
        if getattr(route, "path", None) not in paths
    ]


async def ollama_models():
    try:
        result = await asyncio.wait_for(
            ollama_client.models.list(),
            timeout=min(OLLAMA_TIMEOUT, 10),
        )
        return list(result.data)
    except Exception as exc:
        base.logger.warning("Ollama model discovery failed: %s", exc)
        return []


async def ollama_capabilities(model):
    def fetch():
        payload = json.dumps({"model": model}).encode("utf-8")
        request = urllib.request.Request(
            f"{OLLAMA_URL}/api/show",
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "home-assistant-agent"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=OLLAMA_TOOL_CHECK_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
        return data.get("capabilities", [])

    try:
        return await asyncio.wait_for(
            asyncio.to_thread(fetch), timeout=OLLAMA_TOOL_CHECK_TIMEOUT + 1
        )
    except Exception as exc:
        base.logger.warning("Ollama capability check failed for %s: %s", model, exc)
        return []


async def ollama_model_info(model):
    capabilities = await ollama_capabilities(model)
    return {
        "id": model,
        "capabilities": capabilities,
        "tools": "tools" in capabilities,
    }


async def ollama_available(model=None, require_tools=False):
    model = model or OLLAMA_MODEL
    if not any(item.id == model for item in await ollama_models()):
        return False
    if require_tools:
        info = await ollama_model_info(model)
        return info["tools"]
    return True


def ollama_model_name(model_id, tools_available=True):
    prefix = model_id.split(":", 1)[0].lower()
    labels = {
        "qwen": "Qwen",
        "qwen2": "Qwen",
        "qwen3": "Qwen",
        "gemma": "Gemma",
        "llama": "Llama",
        "mistral": "Mistral",
        "phi": "Phi",
        "deepseek": "DeepSeek",
    }
    label = next((value for key, value in labels.items() if prefix.startswith(key)), None)
    name = f"{label} ({model_id})" if label else f"Ollama ({model_id})"
    return name if tools_available else f"{name} – keine HA-Tools"


async def get_mcp_version():
    try:
        async with base.streamable_http_client(base.MCP_URL) as (read_stream, write_stream):
            async with base.ClientSession(read_stream, write_stream) as session:
                result = await base.run_with_timeout(
                    session.initialize(),
                    base.MCP_TIMEOUT,
                    "MCP version initialization",
                )
                server_info = getattr(result, "server_info", None)
                version = getattr(server_info, "version", None)
                return str(version).strip() if version else None
    except Exception as exc:
        base.logger.warning("MCP version check failed: %s", exc)
        return None


async def run_ollama_agent(session, tools, user_message, history, model):
    names = base.tool_names(tools)
    messages = [
        {"role": "system", "content": base.SYSTEM_PROMPT},
        *base.clean_history(history),
        {"role": "user", "content": user_message},
    ]

    for _ in range(base.MAX_TOOL_ROUNDS):
        response = await base.run_with_timeout(
            ollama_client.chat.completions.create(
                model=model,
                messages=messages,
                tools=base.openai_tools(tools),
                tool_choice="auto",
                extra_body={"options": {"num_thread": OLLAMA_NUM_THREADS}},
            ),
            OLLAMA_TIMEOUT,
            f"Ollama request ({model})",
        )
        message = response.choices[0].message
        if not message.tool_calls:
            return message.content or ""

        messages.append(message.model_dump(exclude_none=True))
        for tool_call in message.tool_calls:
            try:
                arguments = json.loads(tool_call.function.arguments or "{}")
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"Invalid arguments for MCP tool {tool_call.function.name}"
                ) from exc
            result = await base.call_mcp_tool(
                session,
                tool_call.function.name,
                arguments,
                names,
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                }
            )

    raise RuntimeError("Maximum tool-call rounds exceeded")


async def public_version_check():
    url = "https://raw.githubusercontent.com/benjaminschmal/home-assistant-agent/main/agent/ollama_entrypoint.py"

    def fetch_source():
        request = urllib.request.Request(url, headers={"User-Agent": "home-assistant-agent"})
        with urllib.request.urlopen(request, timeout=base.PUBLIC_GIT_CHECK_TIMEOUT) as response:
            return response.read().decode("utf-8")

    try:
        source = await asyncio.wait_for(
            asyncio.to_thread(fetch_source), timeout=base.PUBLIC_GIT_CHECK_TIMEOUT + 1
        )
        marker = 'base.AGENT_VERSION = "'
        start = source.find(marker)
        if start < 0:
            return {"available": False, "version": None, "update_available": False}
        start += len(marker)
        end = source.find('"', start)
        version = source[start:end] if end > start else None
        return {
            "available": bool(version),
            "version": version,
            "update_available": bool(version and version != base.AGENT_VERSION),
        }
    except Exception as exc:
        base.logger.warning("Public Ollama version check failed: %s", exc)
        return {"available": False, "version": None, "update_available": False}


_remove_routes({"/models", "/chat", "/health", "/version"})


@base.app.get("/models")
async def models_endpoint():
    models = await ollama_models()
    ollama_entries = []
    for item in models:
        info = await ollama_model_info(item.id)
        ollama_entries.append(
            {
                "id": item.id,
                "name": ollama_model_name(item.id, info["tools"]),
                "available": info["tools"],
                "tools": info["tools"],
                "capabilities": info["capabilities"],
            }
        )

    tool_models = [item["id"] for item in ollama_entries if item["tools"]]
    default_model = "openai" if OPENAI_CONFIGURED else (
        OLLAMA_MODEL if OLLAMA_MODEL in tool_models else (tool_models[0] if tool_models else OLLAMA_MODEL)
    )
    return {
        "default_model": default_model,
        "models": [
            {
                "id": "openai",
                "name": f"GPT ({base.OPENAI_MODEL})",
                "available": OPENAI_CONFIGURED,
                "tools": OPENAI_CONFIGURED,
            },
            {
                "id": "anthropic",
                "name": f"Claude ({base.ANTHROPIC_MODEL})",
                "available": bool(base.ANTHROPIC_API_KEY),
                "tools": bool(base.ANTHROPIC_API_KEY),
            },
            *ollama_entries,
        ],
    }


@base.app.get("/health")
async def health():
    models = await ollama_models()
    model_infos = [await ollama_model_info(item.id) for item in models]
    model_ids = [item["id"] for item in model_infos]
    return {
        "status": "ok",
        "service": "home-assistant-agent",
        "provider": "ollama" if not OPENAI_CONFIGURED else "openai",
        "model": OLLAMA_MODEL if not OPENAI_CONFIGURED else base.OPENAI_MODEL,
        "mcp_configured": bool(base.MCP_URL),
        "anthropic_configured": bool(base.ANTHROPIC_API_KEY),
        "openai_configured": OPENAI_CONFIGURED,
        "ollama_available": bool(model_ids),
        "ollama_model": OLLAMA_MODEL,
        "ollama_models": model_ids,
        "ollama_tool_models": [item["id"] for item in model_infos if item["tools"]],
        "ollama_num_threads": OLLAMA_NUM_THREADS,
    }


@base.app.get("/version")
async def version_endpoint():
    public = await public_version_check()
    mcp = await get_mcp_version()
    return {
        "agent_version": base.AGENT_VERSION,
        "mcp_version": mcp,
        "public_update_available": public.get("update_available", False),
        "public_agent_version": public.get("version"),
        "public_version_available": public.get("available", False),
    }


@base.app.post("/chat")
async def chat(request: base.ChatRequest):
    base.logger.info(
        "Processing chat request using provider/model=%s history=%d",
        request.model,
        len(request.history),
    )
    try:
        if request.model == "openai":
            if not OPENAI_CONFIGURED:
                raise ValueError("OpenAI is not configured: OPENAI_API_KEY is missing")
            selected_provider = "openai"
        elif request.model == "anthropic":
            if not base.ANTHROPIC_API_KEY:
                raise ValueError("Claude is not configured: ANTHROPIC_API_KEY is missing")
            selected_provider = "anthropic"
        else:
            if not await ollama_available(request.model, require_tools=True):
                raise ValueError(
                    f"Das Modell {request.model} unterstützt kein Tool-Calling und kann daher nicht mit Home Assistant verwendet werden. Bitte wähle ein Modell mit HA-Tools, z. B. Qwen 3:4b."
                )
            selected_provider = "ollama"

        enriched_message = await base.enrich_release_context(request.message)
        async with base.streamable_http_client(base.MCP_URL) as (read_stream, write_stream):
            async with base.ClientSession(read_stream, write_stream) as session:
                tools = await base.load_mcp_tools(session)
                if selected_provider == "ollama":
                    response = await run_ollama_agent(
                        session, tools, enriched_message, request.history, request.model
                    )
                    return {
                        "response": response,
                        "model": request.model,
                        "provider": "ollama",
                    }

                if selected_provider == "anthropic":
                    response = await base.run_anthropic_agent(
                        session, tools, enriched_message, request.history
                    )
                    return {
                        "response": response,
                        "model": base.ANTHROPIC_MODEL,
                        "provider": "anthropic",
                    }

                response = await base.run_openai_agent(
                    session, tools, enriched_message, request.history
                )
                return {
                    "response": response,
                    "model": base.OPENAI_MODEL,
                    "provider": "openai",
                }
    except Exception as exc:
        base.logger.exception("Agent request failed")
        return {"error": str(exc)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(base.app, host="0.0.0.0", port=8080)
