import asyncio
import json
import logging
import os
import re
import urllib.error
import urllib.request

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

AGENT_VERSION = "1.13.2"
MCP_URL = os.environ.get("MCP_URL", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5").strip()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5").strip()
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:8b").strip()
MCP_TIMEOUT = float(os.environ.get("MCP_TIMEOUT_SECONDS", "15"))
OPENAI_TIMEOUT = float(os.environ.get("OPENAI_TIMEOUT_SECONDS", "60"))
ANTHROPIC_TIMEOUT = float(os.environ.get("ANTHROPIC_TIMEOUT_SECONDS", "60"))
OLLAMA_TIMEOUT = float(os.environ.get("OLLAMA_TIMEOUT_SECONDS", "120"))
RELEASE_CHECK_TIMEOUT = float(os.environ.get("RELEASE_CHECK_TIMEOUT_SECONDS", "10"))
PUBLIC_GIT_CHECK_TIMEOUT = float(os.environ.get("PUBLIC_GIT_CHECK_TIMEOUT_SECONDS", "8"))
MAX_TOOL_ROUNDS = int(os.environ.get("MAX_TOOL_ROUNDS", "8"))
MAX_HISTORY_MESSAGES = int(os.environ.get("MAX_HISTORY_MESSAGES", "12"))

if not MCP_URL:
    raise RuntimeError("MCP_URL is not configured")
if not OPENAI_API_KEY and not OLLAMA_URL:
    raise RuntimeError("Neither OPENAI_API_KEY nor OLLAMA_URL is configured")
if not 1 <= MAX_TOOL_ROUNDS <= 10:
    raise RuntimeError("MAX_TOOL_ROUNDS must be between 1 and 10")
if not 0 <= MAX_HISTORY_MESSAGES <= 30:
    raise RuntimeError("MAX_HISTORY_MESSAGES must be between 0 and 30")

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY, timeout=OPENAI_TIMEOUT, max_retries=2) if OPENAI_API_KEY else None
ollama_client = AsyncOpenAI(base_url=f"{OLLAMA_URL}/v1", api_key="ollama", timeout=OLLAMA_TIMEOUT, max_retries=1)
anthropic_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY, timeout=ANTHROPIC_TIMEOUT, max_retries=2) if ANTHROPIC_API_KEY else None
app = FastAPI(title="Home Assistant AI")

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    model: str = "openai"
    history: list[ChatMessage] = Field(default_factory=list, max_length=30)

HTML = """
<!DOCTYPE html><html lang="de"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Home Assistant AI</title>
<style>body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f5f5f5;margin:0;padding:0}.container{max-width:900px;margin:40px auto;background:white;border-radius:14px;padding:24px;box-shadow:0 4px 20px rgba(0,0,0,.08)}h1{margin-top:0}.controls{display:flex;gap:10px;align-items:center;margin-bottom:14px}select{padding:10px 12px;border:1px solid #ccc;border-radius:8px;font-size:15px;background:white}.status{font-size:13px;color:#666}#chat{min-height:350px;max-height:600px;overflow-y:auto;border:1px solid #ddd;border-radius:10px;padding:16px;margin-bottom:16px}.message{margin:12px 0;padding:12px 14px;border-radius:10px;white-space:pre-wrap}.user{background:#e8f0fe}.assistant{background:#f0f0f0}.input-row{display:flex;gap:10px}input{flex:1;padding:14px;border:1px solid #ccc;border-radius:8px;font-size:16px}button{padding:14px 22px;border:0;border-radius:8px;background:#1976d2;color:white;font-size:16px;cursor:pointer}button:disabled{opacity:.5}.footer{margin-top:14px;text-align:center;font-size:12px;color:#888}</style></head>
<body><div class="container"><h1>Home Assistant AI</h1><div class="controls"><label for="model">KI-Modell:</label><select id="model"></select><span id="modelStatus" class="status"></span></div><div id="chat"></div><div class="input-row"><input id="message" type="text" placeholder="z.B. Wie warm ist der Vorlauf?" autocomplete="off"/><button id="send">Senden</button></div><div class="footer" id="versionInfo">Agent v1.13.2 · MCP wird geladen …</div></div>
<script>
const input=document.getElementById("message"),button=document.getElementById("send"),chat=document.getElementById("chat"),modelSelect=document.getElementById("model"),modelStatus=document.getElementById("modelStatus");
const history=[];
function addMessage(text,type){const div=document.createElement("div");div.className="message "+type;div.textContent=text;chat.appendChild(div);chat.scrollTop=chat.scrollHeight}
async function loadModels(){try{const response=await fetch("/models"),data=await response.json();modelSelect.innerHTML="";for(const model of data.models||[]){const option=document.createElement("option");option.value=model.id;option.textContent=model.name+(model.available?"":" (nicht verfügbar)");option.disabled=!model.available;modelSelect.appendChild(option)}modelSelect.value=data.default_model||"openai"}catch(error){modelStatus.textContent="Modellliste nicht verfügbar"}}
async function sendMessage(){const message=input.value.trim();if(!message)return;const selectedModel=modelSelect.value;addMessage(message,"user");history.push({role:"user",content:message});input.value="";button.disabled=true;modelSelect.disabled=true;addMessage("Denke nach ...","assistant");try{const response=await fetch("/chat",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({message,model:selectedModel,history:history.slice(0,-1).slice(-12)})}),data=await response.json();chat.lastChild.remove();if(!response.ok||data.error){history.pop();addMessage("Fehler: "+(data.error||"Unbekannter Fehler"),"assistant")}else{addMessage(data.response,"assistant");history.push({role:"assistant",content:data.response});if(history.length>12)history.splice(0,history.length-12)}}catch(error){chat.lastChild.remove();history.pop();addMessage("Verbindungsfehler: "+error,"assistant")}finally{button.disabled=false;modelSelect.disabled=false;input.focus()}}
async function loadVersion(){try{const response=await fetch("/version"),data=await response.json();let text=`Agent v${data.agent_version} · MCP v${data.mcp_version||"unbekannt"}`;if(data.public_update_available)text+=` · ⚠ Neue Version verfügbar: Agent v${data.public_agent_version}`;document.getElementById("versionInfo").textContent=text}catch(error){document.getElementById("versionInfo").textContent="Agent v1.13.2 · MCP-Version nicht verfügbar"}}
button.addEventListener("click",sendMessage);input.addEventListener("keydown",event=>{if(event.key==="Enter")sendMessage()});input.focus();loadModels();loadVersion();
</script></body></html>
"""

SYSTEM_PROMPT=("You are a Home Assistant AI assistant. Use the available Home Assistant tools to answer questions. IMPORTANT: Before giving platform-dependent advice or instructions about Add-ons, Supervisor, MQTT installation, backups, updates, configuration capabilities, or other installation-specific features, call get_home_assistant_info and use its returned capabilities. Never assume Home Assistant OS. If supervisor_available or addon_store_available is false, do not recommend or reference the Home Assistant Add-on Store or Supervisor; explain that the connected installation does not expose those capabilities and, where appropriate, describe the platform-neutral or external-service alternative. Use the actual connected Home Assistant environment, not generic Home Assistant assumptions. For questions about current Home Assistant devices, entities, sensors, states, temperatures, switches, printers, energy, or other live values, you MUST use the Home Assistant tools rather than relying on general knowledge. For unknown devices or sensors, search_entities first. Never invent entity IDs, values or states. Use get_entity_state when current state is required. Use call_service only for actions allowed by the MCP server. Configuration editing is a separate privileged capability. If the user asks to read or change YAML, first call configuration_status. If configuration editing is disabled, explain that it must be enabled. If enabled and the user explicitly asks to change an allowed YAML file, read the current file first, make the smallest necessary change, preserve all unrelated content, and then call update_config with the complete new file. Do not claim a configuration change was made unless update_config returns success. Never replace a configuration file with a guessed or unrelated example. For Lovelace dashboards, always call list_dashboards before create_dashboard. If a dashboard with the requested URL path already exists, never call create_dashboard. Tell the user that the dashboard already exists and, if their request is to modify it, use read_dashboard followed by the smallest necessary update_dashboard change. Only call create_dashboard when the requested dashboard does not already exist. IMPORTANT: maintain conversational context. A short reply such as 'ja', 'nein', 'mach das', 'weiter' or 'genau' refers to the immediately preceding assistant message and must be interpreted using the supplied conversation history. Do not restart the conversation or answer with a generic greeting. For questions about the built-in Home Assistant Energy dashboard, distinguish between the dashboard being visible and its energy sources being configured. If the user asks to configure the Energy dashboard and an energy-specific tool is available, use it rather than editing unrelated YAML. If the requested capability is not exposed by the MCP, say so explicitly. If you offer to search for something and the user replies 'Ja', perform that search immediately. For questions asking whether the installed Home Assistant version is current, latest, newest, or whether an update is available, use get_home_assistant_info for the installed Core version and the verified release context supplied by the Agent. Never infer that an installed version is current merely because it is recent. If the release check is unavailable, say that current release status could not be verified. For questions about HACS, always call get_hacs_info first. Do not assume HACS is installed and do not use Supervisor or the Add-on Store as a proxy for HACS. Use the returned HACS version, latest stable version, installed repository count, categories and update information. If HACS or its repository storage cannot be detected, report that limitation instead of guessing.")

async def get_public_git_version():
    url="https://raw.githubusercontent.com/benjaminschmal/home-assistant-agent/main/agent/agent.py"
    def fetch_source():
        request=urllib.request.Request(url,headers={"User-Agent":"home-assistant-agent"})
        with urllib.request.urlopen(request,timeout=PUBLIC_GIT_CHECK_TIMEOUT) as response:return response.read().decode("utf-8")
    try:
        source=await asyncio.wait_for(asyncio.to_thread(fetch_source),timeout=PUBLIC_GIT_CHECK_TIMEOUT+1)
        match=re.search(r"AGENT_VERSION\s*=\s*['\"]([^'\"]+)['\"]",source)
        version=match.group(1).strip() if match else None
        return {"available":bool(version),"version":version,"source":url,"update_available":bool(version and version!=AGENT_VERSION)}
    except Exception as exc:
        logger.warning("Public Git version check failed: %s",exc)
        return {"available":False,"version":None,"source":url,"update_available":False}

async def get_latest_home_assistant_release():
    url="https://api.github.com/repos/home-assistant/core/releases/latest"
    def fetch_release():
        request=urllib.request.Request(url,headers={"Accept":"application/vnd.github+json","User-Agent":"home-assistant-agent"})
        with urllib.request.urlopen(request,timeout=RELEASE_CHECK_TIMEOUT) as response:return json.loads(response.read().decode("utf-8"))
    try:
        data=await asyncio.wait_for(asyncio.to_thread(fetch_release),timeout=RELEASE_CHECK_TIMEOUT+1)
        return {"available":bool(data.get("tag_name")),"version":str(data.get("tag_name") or "").strip(),"published_at":data.get("published_at"),"url":data.get("html_url"),"source":url}
    except Exception:
        return {"available":False,"version":None,"source":url}

async def enrich_release_context(message):
    if not re.search(r"\b(neueste|aktuellste|aktuelle version|aktuell|update|updates|release|version|latest|newest|current version)\b",message,re.IGNORECASE):return message
    release=await get_latest_home_assistant_release()
    if release.get("available"):
        return message+f"\n\n[VERIFIED RELEASE CONTEXT] Official Home Assistant Core stable release: {release['version']}. Compare it with get_home_assistant_info and do not guess."
    return message+"\n\n[RELEASE CHECK UNAVAILABLE] Current Home Assistant release could not be verified. Do not guess."

async def run_with_timeout(awaitable,timeout,operation):
    try:return await asyncio.wait_for(awaitable,timeout=timeout)
    except asyncio.TimeoutError as exc:raise TimeoutError(f"{operation} timed out after {timeout:.0f}s") from exc

async def load_mcp_tools(session):
    await run_with_timeout(session.initialize(),MCP_TIMEOUT,"MCP initialization")
    return (await run_with_timeout(session.list_tools(),MCP_TIMEOUT,"MCP tool discovery")).tools

def tool_names(tools):return {tool.name for tool in tools}
def openai_tools(tools):return [{"type":"function","function":{"name":t.name,"description":t.description or "Home Assistant tool","parameters":t.input_schema}} for t in tools]

def clean_history(history):
    return [{"role":m.role,"content":m.content[:4000]} for m in history[-MAX_HISTORY_MESSAGES:] if m.role in {"user","assistant"} and m.content.strip()]

async def call_mcp_tool(session,name,arguments,names):
    if name not in names:raise RuntimeError(f"Model requested unknown MCP tool: {name}")
    logger.info("Calling MCP tool: %s",name)
    result=await run_with_timeout(session.call_tool(name,arguments),MCP_TIMEOUT,f"MCP tool {name}")
    text="".join(c.text for c in result.content if hasattr(c,"text") and c.text)
    return f"MCP tool error: {text or 'unknown error'}" if getattr(result,"is_error",False) else (text or "No data returned.")

async def run_openai_compatible(client,model,session,tools,user_message,history,timeout):
    names=tool_names(tools);messages=[{"role":"system","content":SYSTEM_PROMPT}]+clean_history(history)+[{"role":"user","content":user_message}]
    for _ in range(MAX_TOOL_ROUNDS):
        response=await run_with_timeout(client.chat.completions.create(model=model,messages=messages,tools=openai_tools(tools),tool_choice="auto"),timeout,f"{model} request")
        message=response.choices[0].message
        if not message.tool_calls:return message.content or ""
        messages.append(message.model_dump(exclude_none=True))
        for tc in message.tool_calls:
            arguments=json.loads(tc.function.arguments or "{}")
            result=await call_mcp_tool(session,tc.function.name,arguments,names)
            messages.append({"role":"tool","tool_call_id":tc.id,"content":result})
    raise RuntimeError("Maximum tool-call rounds exceeded")

async def run_anthropic_agent(session,tools,user_message,history):
    if not anthropic_client:raise RuntimeError("Claude is not configured: ANTHROPIC_API_KEY is missing")
    names=tool_names(tools);anthropic_tools=[{"name":t.name,"description":t.description or "Home Assistant tool","input_schema":t.input_schema} for t in tools]
    messages=clean_history(history)+[{"role":"user","content":user_message}]
    for _ in range(MAX_TOOL_ROUNDS):
        response=await run_with_timeout(anthropic_client.messages.create(model=ANTHROPIC_MODEL,max_tokens=4096,system=SYSTEM_PROMPT,messages=messages,tools=anthropic_tools,tool_choice={"type":"auto"}),ANTHROPIC_TIMEOUT,"Anthropic request")
        tool_uses=[b for b in response.content if getattr(b,"type",None)=="tool_use"]
        text="".join(b.text for b in response.content if getattr(b,"type",None)=="text")
        if not tool_uses:return text
        messages.append({"role":"assistant","content":[b.model_dump() for b in response.content]})
        results=[]
        for tu in tool_uses:
            results.append({"type":"tool_result","tool_use_id":tu.id,"content":await call_mcp_tool(session,tu.name,tu.input or {},names)})
        messages.append({"role":"user","content":results})
    raise RuntimeError("Maximum tool-call rounds exceeded")

async def ollama_available():
    try:
        result=await asyncio.wait_for(ollama_client.models.list(),timeout=min(OLLAMA_TIMEOUT,10))
        return any(item.id==OLLAMA_MODEL for item in result.data)
    except Exception as exc:
        logger.warning("Ollama availability check failed: %s",exc)
        return False

@app.get("/",response_class=HTMLResponse)
async def index():return HTML

@app.get("/models")
async def models_endpoint():
    ollama_ok=await ollama_available()
    default="openai" if OPENAI_API_KEY else "ollama"
    return {"default_model":default,"models":[{"id":"openai","name":f"GPT ({OPENAI_MODEL})","available":bool(OPENAI_API_KEY)},{"id":"anthropic","name":f"Claude ({ANTHROPIC_MODEL})","available":bool(ANTHROPIC_API_KEY)},{"id":"ollama","name":f"Qwen ({OLLAMA_MODEL})","available":ollama_ok}]}

@app.get("/version")
async def version_endpoint():
    mcp_version=None
    try:
        async with streamable_http_client(MCP_URL) as (read_stream,write_stream):
            async with ClientSession(read_stream,write_stream) as session:
                init=await run_with_timeout(session.initialize(),MCP_TIMEOUT,"MCP version discovery")
                info=getattr(init,"server_info",None)
                mcp_version=getattr(info,"version",None) if info else None
    except Exception as exc:logger.warning("MCP version discovery failed: %s",exc)
    public=await get_public_git_version()
    return {"agent_version":AGENT_VERSION,"mcp_version":mcp_version,"public_agent_version":public.get("version"),"public_update_available":bool(public.get("update_available")),"public_git_check_available":bool(public.get("available"))}

@app.get("/health")
async def health():return {"status":"ok","service":"home-assistant-agent","provider":"openai" if OPENAI_API_KEY else "ollama","model":OPENAI_MODEL if OPENAI_API_KEY else OLLAMA_MODEL,"mcp_configured":bool(MCP_URL),"anthropic_configured":bool(ANTHROPIC_API_KEY),"ollama_available":await ollama_available()}

@app.post("/chat")
async def chat(request:ChatRequest):
    logger.info("Processing chat request using provider=%s history=%d",request.model,len(request.history))
    try:
        if request.model=="openai":
            if not openai_client:raise ValueError("OpenAI is not configured: OPENAI_API_KEY is missing")
        elif request.model=="anthropic":
            if not anthropic_client:raise ValueError("Claude is not configured: ANTHROPIC_API_KEY is missing")
        elif request.model=="ollama":
            if not await ollama_available():raise ValueError(f"Ollama is not reachable or model {OLLAMA_MODEL} is not installed")
        else:raise ValueError("Selected model provider is not available")
        enriched=await enrich_release_context(request.message)
        async with streamable_http_client(MCP_URL) as (read_stream,write_stream):
            async with ClientSession(read_stream,write_stream) as session:
                tools=await load_mcp_tools(session)
                if request.model=="openai":response=await run_openai_compatible(openai_client,OPENAI_MODEL,session,tools,enriched,request.history,OPENAI_TIMEOUT)
                elif request.model=="ollama":response=await run_openai_compatible(ollama_client,OLLAMA_MODEL,session,tools,enriched,request.history,OLLAMA_TIMEOUT)
                else:response=await run_anthropic_agent(session,tools,enriched,request.history)
        return {"response":response,"model":OPENAI_MODEL if request.model=="openai" else ANTHROPIC_MODEL if request.model=="anthropic" else OLLAMA_MODEL,"provider":request.model}
    except (OpenAIError,anthropic.APIError,TimeoutError,OSError,RuntimeError,ValueError,json.JSONDecodeError) as exc:
        logger.exception("Agent request failed")
        return {"error":str(exc)}
    except Exception as exc:
        logger.exception("Unexpected agent error")
        return {"error":f"Unexpected error: {type(exc).__name__}"}

if __name__=="__main__":uvicorn.run(app,host="0.0.0.0",port=8080)
