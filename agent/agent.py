import asyncio
import json
import os

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn

from openai import AsyncOpenAI

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


MCP_URL = os.environ.get(
    "MCP_URL",
    "http://localhost:8000/mcp",
)

OPENAI_MODEL = os.environ.get(
    "OPENAI_MODEL",
    "gpt-5",
)

client = AsyncOpenAI()

app = FastAPI(
    title="Home Assistant AI",
)


class ChatRequest(BaseModel):
    message: str


HTML = """
<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Home Assistant AI</title>

<style>
body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: #f5f5f5;
    margin: 0;
    padding: 0;
}

.container {
    max-width: 900px;
    margin: 40px auto;
    background: white;
    border-radius: 14px;
    padding: 24px;
    box-shadow: 0 4px 20px rgba(0,0,0,0.08);
}

h1 {
    margin-top: 0;
}

#chat {
    min-height: 350px;
    max-height: 600px;
    overflow-y: auto;
    border: 1px solid #ddd;
    border-radius: 10px;
    padding: 16px;
    margin-bottom: 16px;
}

.message {
    margin: 12px 0;
    padding: 12px 14px;
    border-radius: 10px;
    white-space: pre-wrap;
}

.user {
    background: #e8f0fe;
}

.assistant {
    background: #f0f0f0;
}

.input-row {
    display: flex;
    gap: 10px;
}

input {
    flex: 1;
    padding: 14px;
    border: 1px solid #ccc;
    border-radius: 8px;
    font-size: 16px;
}

button {
    padding: 14px 22px;
    border: 0;
    border-radius: 8px;
    background: #1976d2;
    color: white;
    font-size: 16px;
    cursor: pointer;
}

button:disabled {
    opacity: 0.5;
}
</style>
</head>

<body>

<div class="container">

<h1>Home Assistant AI</h1>

<div id="chat"></div>

<div class="input-row">

<input
    id="message"
    type="text"
    placeholder="z.B. Wie warm ist der Vorlauf?"
    autocomplete="off"
/>

<button id="send">
    Senden
</button>

</div>

</div>

<script>

const input = document.getElementById("message");
const button = document.getElementById("send");
const chat = document.getElementById("chat");

function addMessage(text, type) {

    const div = document.createElement("div");

    div.className = "message " + type;

    div.textContent = text;

    chat.appendChild(div);

    chat.scrollTop = chat.scrollHeight;
}

async function sendMessage() {

    const message = input.value.trim();

    if (!message) {
        return;
    }

    addMessage(message, "user");

    input.value = "";

    button.disabled = true;

    addMessage("Denke nach ...", "assistant");

    try {

        const response = await fetch("/chat", {

            method: "POST",

            headers: {
                "Content-Type": "application/json"
            },

            body: JSON.stringify({
                message: message
            })

        });

        const data = await response.json();

        chat.lastChild.remove();

        if (data.error) {
            addMessage("Fehler: " + data.error, "assistant");
        } else {
            addMessage(data.response, "assistant");
        }

    } catch (error) {

        chat.lastChild.remove();

        addMessage(
            "Verbindungsfehler: " + error,
            "assistant"
        );

    } finally {

        button.disabled = false;

        input.focus();
    }
}

button.addEventListener(
    "click",
    sendMessage
);

input.addEventListener(
    "keydown",
    event => {

        if (event.key === "Enter") {
            sendMessage();
        }

    }
);

input.focus();

</script>

</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML


@app.post("/chat")
async def chat(request: ChatRequest):

    try:

        async with streamable_http_client(
            MCP_URL
        ) as (
            read_stream,
            write_stream,

        ):

            async with ClientSession(
                read_stream,
                write_stream,
            ) as session:

                await session.initialize()

                tools_result = await session.list_tools()

                openai_tools = []

                for tool in tools_result.tools:

                    openai_tools.append({
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": tool.input_schema,
                        },
                    })

                messages = [

                    {
                        "role": "system",
                        "content": (
                            "You are a Home Assistant AI assistant. "
                            "Use the available Home Assistant tools "
                            "to answer questions about the user's "
                            "smart home. "
                            "If you need to find an entity, use "
                            "search_entities first. "
                            "Then use get_entity_state to retrieve "
                            "the current value. "
                            "Never invent sensor values."
                        ),
                    },

                    {
                        "role": "user",
                        "content": request.message,
                    },

                ]

                while True:

                    response = await client.chat.completions.create(

                        model=OPENAI_MODEL,

                        messages=messages,

                        tools=openai_tools,

                        tool_choice="auto",

                    )

                    message = response.choices[0].message

                    if not message.tool_calls:

                        return {
                            "response": message.content or ""
                        }

                    messages.append(
                        message.model_dump(
                            exclude_none=True
                        )
                    )

                    for tool_call in message.tool_calls:

                        tool_name = tool_call.function.name

                        arguments = json.loads(
                            tool_call.function.arguments
                        )

                        result = await session.call_tool(
                            tool_name,
                            arguments,
                        )

                        tool_text = ""

                        for content in result.content:

                            if hasattr(content, "text"):
                                tool_text += content.text

                        messages.append({

                            "role": "tool",

                            "tool_call_id":
                                tool_call.id,

                            "content":
                                tool_text,

                        })


    except Exception as e:
        import traceback
        traceback.print_exc()
        return {
            "error": f"{type(e).__name__}: {e}"
        }


if __name__ == "__main__":

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8080,
    )
