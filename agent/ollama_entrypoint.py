import asyncio
import os

from fastapi import FastAPI
from openai import AsyncOpenAI

import agent as base

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:8b").strip()
OLLAMA_TIMEOUT = float(os.environ.get("OLLAMA_TIMEOUT_SECONDS", "120"))

ollama_client = AsyncOpenAI(
    base_url=f"{OLLAMA_URL}/v1",
    api_key="ollama",
    timeout=OLLAMA_TIMEOUT,
    max_retries=1,
)


def _remove_routes(paths):
    base.app.routes[:] = [route for route in base.app.routes if getattr(route, "path", None) not in paths]


async def ollama_available():
    try:
        result = await asyncio.wait_for(
            ollama_client.models.list(),
            timeout=min(OLLAMA_TIMEOUT, 10),
        )
        model_ids = {item.id for item in result.data}
        return OLLAMA_MODEL in model_ids
    except Exception as exc:
        base.logger.warning("Ollama availability check failed: %s", exc)
        return False


async def run_ollama_agent(session, tools, user_message, history):
    names = base.tool_names(tools)
    messages = [
        {"role": "system", "content": base.SYSTEM_PROMPT},
        *base.clean_history(history),
        {"role": "user", "content": user_message},
    ]

    for _ in range(base.MAX_TOOL_ROUNDS):
        response = await base.run_with_timeout(
            ollama_client.chat.completions.create(
                model=OLLAMA_MODEL,
                messages=messages,
                tools=base.openai_tools(tools),
                tool_choice="auto",
            ),
            OLLAMA_TIMEOUT,
            "Ollama request",
        )
        message = response.choices[0].message
        if not message.tool_calls:
            return message.content or ""

        messages.append(message.model_dump(exclude_none=True))
        for tool_call in message.tool_calls:
            try:
                arguments = __import__("json").loads(tool_call.function.arguments or "{}")
            except ValueError as exc:
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
    available = await ollama_available()
    return {
        "default_model": "openai",
        "models": [
            {
                "id": "openai",
                "name": f"GPT ({base.OPENAI_MODEL})",
                "available": True,
            },
            {
                "id": "anthropic",
                "name": f"Claude ({base.ANTHROPIC_MODEL})",
                "available": bool(base.ANTHROPIC_API_KEY),
            },
            {
                "id": "ollama",
                "name": f"Qwen ({OLLAMA_MODEL})",
                "available": available,
            },
        ],
    }


@base.app.get("/health")
async def health():
    available = await ollama_available()
    return {
        "status": "ok",
        "service": "home-assistant-agent",
        "provider": "openai",
        "model": base.OPENAI_MODEL,
        "mcp_configured": bool(base.MCP_URL),
        "anthropic_configured": bool(base.ANTHROPIC_API_KEY),
        "ollama_available": available,
        "ollama_model": OLLAMA_MODEL,
    }


@base.app.post("/chat")
async def chat(request: base.ChatRequest):
    base.logger.info(
        "Processing chat request using provider=%s history=%d",
        request.model,
        len(request.history),
    )
    try:
        if request.model not in {"openai", "anthropic", "ollama"}:
            raise ValueError("Selected model provider is not available")
        if request.model == "anthropic" and not base.ANTHROPIC_API_KEY:
            raise ValueError("Claude is not configured: ANTHROPIC_API_KEY is missing")
        if request.model == "ollama" and not await ollama_available():
            raise ValueError(
                f"Ollama is not reachable or model {OLLAMA_MODEL} is not installed"
            )

        enriched_message = await base.enrich_release_context(request.message)
        async with base.streamable_http_client(base.MCP_URL) as (read_stream, write_stream):
            async with base.ClientSession(read_stream, write_stream) as session:
                tools = await base.load_mcp_tools(session)
                if request.model == "ollama":
                    response = await run_ollama_agent(
                        session, tools, enriched_message, request.history
                    )
                    return {
                        "response": response,
                        "model": OLLAMA_MODEL,
                        "provider": "ollama",
                    }

                if request.model == "anthropic":
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
