import asyncio
import json
import os

# The base agent historically requires an OpenAI key at import time.
# Allow a fully local Ollama deployment without weakening the normal OpenAI path.
OPENAI_CONFIGURED = bool(os.environ.get("OPENAI_API_KEY", "").strip())
if not OPENAI_CONFIGURED:
    os.environ["OPENAI_API_KEY"] = "local-only"

import agent as base

# This entrypoint contains the latest Ollama-specific functionality. Keep the
# displayed agent version in sync with this release without changing the base
# agent source solely for the wrapper release number.
base.AGENT_VERSION = "1.13.4"

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:8b").strip()
OLLAMA_TIMEOUT = float(os.environ.get("OLLAMA_TIMEOUT_SECONDS", "120"))
OLLAMA_NUM_THREADS = int(os.environ.get("OLLAMA_NUM_THREADS", "8"))

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


async def ollama_available(model=None):
    model = model or OLLAMA_MODEL
    return any(item.id == model for item in await ollama_models())


def ollama_model_name(model_id):
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
    return f"{label} ({model_id})" if label else f"Ollama ({model_id})"


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


_remove_routes({"/models", "/chat", "/health"})


@base.app.get("/models")
async def models_endpoint():
    models = await ollama_models()
    ollama_entries = [
        {
            "id": item.id,
            "name": ollama_model_name(item.id),
            "available": True,
        }
        for item in models
    ]

    default_model = "openai" if OPENAI_CONFIGURED else OLLAMA_MODEL
    return {
        "default_model": default_model,
        "models": [
            {
                "id": "openai",
                "name": f"GPT ({base.OPENAI_MODEL})",
                "available": OPENAI_CONFIGURED,
            },
            {
                "id": "anthropic",
                "name": f"Claude ({base.ANTHROPIC_MODEL})",
                "available": bool(base.ANTHROPIC_API_KEY),
            },
            *ollama_entries,
        ],
    }


@base.app.get("/health")
async def health():
    models = await ollama_models()
    model_ids = [item.id for item in models]
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
        "ollama_num_threads": OLLAMA_NUM_THREADS,
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
            if not await ollama_available(request.model):
                raise ValueError(
                    f"Ollama model {request.model} is not reachable or not installed"
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
