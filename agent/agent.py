import asyncio
import json
import logging
import os

import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
import uvicorn

from openai import AsyncOpenAI
from openai import OpenAIError

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

load_dotenv()
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("home-assistant-agent")

MCP_URL = os.environ.get("MCP_URL", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5").strip()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5").strip()
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
MCP_TIMEOUT = float(os.environ.get("MCP_TIMEOUT_SECONDS", "15"))
OPENAI_TIMEOUT = float(os.environ.get("OPENAI_TIMEOUT_SECONDS", "60"))
ANTHROPIC_TIMEOUT = float(os.environ.get("ANTHROPIC_TIMEOUT_SECONDS", "60"))
MAX_TOOL_ROUNDS = int(os.environ.get("MAX_TOOL_ROUNDS", "5"))

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not configured")
if not MCP_URL:
    raise RuntimeError("MCP_URL is not configured")
if not 1 <= MAX_TOOL_ROUNDS <= 10:
    raise RuntimeError("MAX_TOOL_ROUNDS must be between 1 and 10")

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY, timeout=OPENAI_TIMEOUT, max_retries=2)
anthropic_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY, timeout=ANTHROPIC_TIMEOUT, max_retries=2) if ANTHROPIC_API_KEY else None
app = FastAPI(title="Home Assistant AI")

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    model: str = "openai"

HTML = """
<!DOCTYPE html><html lang="de"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Home Assistant AI</title>
<style>body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f5f5f5;margin:0;padding:0}.container{max-width:900px;margin:40px auto;background:white;border-radius:14px;padding:24px;box-shadow:0 4px 20px rgba(0,0,0,.08)}h1{margin-top:0}.controls{display:flex;gap:10px;align-items:center;margin-bottom:14px}select{padding:10px 12px;border:1px solid #ccc;border-radius:8px;font-size:15px;background:white}.status{font-size:13px;color:#666}#chat{min-height:350px;max-height:600px;overflow-y:auto;border:1px solid #ddd;border-radius:10px;padding:16px;margin-bottom:16px}.message{margin:12px 0;padding:12px 14px;border-radius:10px;white-space:pre-wrap}.user{background:#e8f0fe}.assistant{background:#f0f0f0}.input-row{display:flex;gap:10px}input{flex:1;padding:14px;border:1px solid #ccc;border-radius:8px;font-size:16px}button{padding:14px 22px;border:0;border-radius:8px;background:#1976d2;color:white;font-size:16px;cursor:pointer}button:disabled{opacity:.5}</style></head>
<body><div class="container"><h1>Home Assistant AI</h1><div class="controls"><label for="model">KI-Modell:</label><select id="model"></select><span id="modelStatus" class="status"></span></div><div id="chat"></div><div class="input-row"><input id="message" type="text" placeholder="z.B. Wie warm ist der Vorlauf?" autocomplete="off"/><button id="send">Senden</button></div></div>
<script>const input=document.getElementById("message"),button=document.getElementById("send"),chat=document.getElementById("chat"),modelSelect=document.getElementById("model"),modelStatus=document.getElementById("modelStatus");function addMessage(text,type){const div=document.createElement("div");div.className="message "+type;div.textContent=text;chat.appendChild(div);chat.scrollTop=chat.scrollHeight}async function loadModels(){try{const response=await fetch("/models"),data=await response.json();modelSelect.innerHTML="";for(const model of data.models||[]){const option=document.createElement("option");option.value=model.id;option.textContent=model.name+(model.available?"":" (nicht verfügbar)");option.disabled=!model.available;modelSelect.appendChild(option)}modelSelect.value=data.default_model||"openai"}catch(error){modelStatus.textContent="Modellliste nicht verfügbar"}}async function sendMessage(){const message=input.value.trim();if(!message)return;addMessage(message,"user");input.value="";button.disabled=true;modelSelect.disabled=true;addMessage("Denke nach ...","assistant");try{const response=await fetch("/chat",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({message,model:modelSelect.value})}),data=await response.json();chat.lastChild.remove();if(!response.ok||data.error)addMessage("Fehler: "+(data.error||"Unbekannter Fehler"),"assistant");else addMessage(data.response,"assistant")}catch(error){chat.lastChild.remove();addMessage("Verbindungsfehler: "+error,"assistant")}finally{button.disabled=false;modelSelect.disabled=false;input.focus()}}button.addEventListener("click",sendMessage);input.addEventListener("keydown",event=>{if(event.key==="Enter")sendMessage()});input.focus();loadModels();</script></body></html>
"""

SYSTEM_PROMPT=("You are a Home Assistant AI assistant. Use the available Home Assistant tools to answer questions. For unknown devices or sensors, search_entities first. Never invent entity IDs, values or states. Use get_entity_state when current state is required. Use call_service only for actions allowed by the MCP server. Configuration editing is a separate privileged capability. If the user asks to read or change YAML, first call configuration_status. If configuration editing is disabled, explain that it must be enabled. If enabled and the user explicitly asks to change an allowed YAML file, read the current file first, make the smallest necessary change, preserve all unrelated content, and then call update_config with the complete new file. Do not claim a configuration change was made unless update_config returns success. Never replace a configuration file with a guessed or unrelated example. For a requested configuration change, explain what will be changed before performing it when the change is consequential. For a harmless test such as adding a comment, it is acceptable to execute directly when explicitly requested. When several entities match, use the most relevant match and state which entity was used.")

async def run_with_timeout(awaitable,timeout,operation):
    try:return await asyncio.wait_for(awaitable,timeout=timeout)
    except asyncio.TimeoutError as exc:raise TimeoutError(f"{operation} timed out after {timeout:.0f}s") from exc

async def load_mcp_tools(session):
    await run_with_timeout(session.initialize(),MCP_TIMEOUT,"MCP initialization")
    result=await run_with_timeout(session.list_tools(),MCP_TIMEOUT,"MCP tool discovery")
    return result.tools

def tool_names(tools):return {tool.name for tool in tools}

def openai_tools(tools):return [{"type":"function","function":{"name":t.name,"description":t.description or "Home Assistant tool","parameters":t.input_schema}} for t in tools]

async def call_mcp_tool(session,name,arguments,names):
    if name not in names:raise RuntimeError(f"Model requested unknown MCP tool: {name}")
    logger.info("Calling MCP tool: %s",name)
    result=await run_with_timeout(session.call_tool(name,arguments),MCP_TIMEOUT,f"MCP tool {name}")
    text="".join(c.text for c in result.content if hasattr(c,"text") and c.text)
    return f"MCP tool error: {text or 'unknown error'}" if getattr(result,"is_error",False) else (text or "No data returned.")

async def run_openai_agent(session,tools,user_message):
    names=tool_names(tools);messages=[{"role":"system","content":SYSTEM_PROMPT},{"role":"user","content":user_message}]
    for _ in range(MAX_TOOL_ROUNDS):
        response=await run_with_timeout(openai_client.chat.completions.create(model=OPENAI_MODEL,messages=messages,tools=openai_tools(tools),tool_choice="auto"),OPENAI_TIMEOUT,"OpenAI request")
        message=response.choices[0].message
        if not message.tool_calls:return message.content or ""
        messages.append(message.model_dump(exclude_none=True))
        for tc in message.tool_calls:
            try:arguments=json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError as exc:raise RuntimeError(f"Invalid arguments for MCP tool {tc.function.name}") from exc
            result=await call_mcp_tool(session,tc.function.name,arguments,names)
            messages.append({"role":"tool","tool_call_id":tc.id,"content":result})
    raise RuntimeError("Maximum tool-call rounds exceeded")

async def run_anthropic_agent(session,tools,user_message):
    if not anthropic_client:raise RuntimeError("Claude is not configured: ANTHROPIC_API_KEY is missing")
    names=tool_names(tools)
    anthropic_tools=[{"name":t.name,"description":t.description or "Home Assistant tool","input_schema":t.input_schema} for t in tools]
    messages=[{"role":"user","content":user_message}]
    for _ in range(MAX_TOOL_ROUNDS):
        response=await run_with_timeout(anthropic_client.messages.create(model=ANTHROPIC_MODEL,max_tokens=4096,system=SYSTEM_PROMPT,messages=messages,tools=anthropic_tools),ANTHROPIC_TIMEOUT,"Anthropic request")
        tool_uses=[b for b in response.content if getattr(b,"type",None)=="tool_use"]
        text="".join(b.text for b in response.content if getattr(b,"type",None)=="text")
        if not tool_uses:return text
        messages.append({"role":"assistant","content":[b.model_dump() for b in response.content]})
        results=[]
        for tu in tool_uses:
            result=await call_mcp_tool(session,tu.name,tu.input or {},names)
            results.append({"type":"tool_result","tool_use_id":tu.id,"content":result})
        messages.append({"role":"user","content":results})
    raise RuntimeError("Maximum tool-call rounds exceeded")

@app.get("/",response_class=HTMLResponse)
async def index():return HTML

@app.get("/models")
async def models_endpoint():
    return {"default_model":"openai","models":[{"id":"openai","name":f"GPT ({OPENAI_MODEL})","available":True},{"id":"anthropic","name":f"Claude ({ANTHROPIC_MODEL})","available":bool(ANTHROPIC_API_KEY)},{"id":"ollama","name":"Ollama (lokal)","available":False}]}

@app.get("/health")
async def health():
    return {"status":"ok","service":"home-assistant-agent","provider":"openai","model":OPENAI_MODEL,"mcp_configured":bool(MCP_URL),"anthropic_configured":bool(ANTHROPIC_API_KEY),"ollama_available":False}

@app.post("/chat")
async def chat(request:ChatRequest):
    logger.info("Processing chat request using provider=%s",request.model)
    try:
        if request.model not in {"openai","anthropic"}:raise ValueError("Selected model provider is not available yet")
        if request.model=="anthropic" and not ANTHROPIC_API_KEY:raise ValueError("Claude is not configured: ANTHROPIC_API_KEY is missing")
        async with streamable_http_client(MCP_URL) as (read_stream,write_stream):
            async with ClientSession(read_stream,write_stream) as session:
                tools=await load_mcp_tools(session)
                if request.model=="anthropic":return {"response":await run_anthropic_agent(session,tools,request.message),"model":ANTHROPIC_MODEL,"provider":"anthropic"}
                return {"response":await run_openai_agent(session,tools,request.message),"model":OPENAI_MODEL,"provider":"openai"}
    except (OpenAIError,anthropic.APIError) as exc:
        logger.exception("Model provider request failed");return {"error":f"Model provider error: {exc}"}
    except (TimeoutError,OSError,RuntimeError,ValueError,PermissionError) as exc:
        logger.exception("Agent request failed");return {"error":str(exc)}
    except Exception as exc:
        logger.exception("Unexpected agent error");return {"error":f"Unexpected error: {type(exc).__name__}"}

if __name__=="__main__":uvicorn.run(app,host="0.0.0.0",port=8080)
