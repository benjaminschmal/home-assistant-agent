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

AGENT_VERSION = "1.13.0"
MCP_URL = os.environ.get("MCP_URL", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5").strip()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5").strip()
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
MCP_TIMEOUT = float(os.environ.get("MCP_TIMEOUT_SECONDS", "15"))
OPENAI_TIMEOUT = float(os.environ.get("OPENAI_TIMEOUT_SECONDS", "60"))
ANTHROPIC_TIMEOUT = float(os.environ.get("ANTHROPIC_TIMEOUT_SECONDS", "60"))
RELEASE_CHECK_TIMEOUT = float(os.environ.get("RELEASE_CHECK_TIMEOUT_SECONDS", "10"))
MAX_TOOL_ROUNDS = int(os.environ.get("MAX_TOOL_ROUNDS", "8"))
MAX_HISTORY_MESSAGES = int(os.environ.get("MAX_HISTORY_MESSAGES", "12"))

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not configured")
if not MCP_URL:
    raise RuntimeError("MCP_URL is not configured")
if not 1 <= MAX_TOOL_ROUNDS <= 10:
    raise RuntimeError("MAX_TOOL_ROUNDS must be between 1 and 10")
if not 0 <= MAX_HISTORY_MESSAGES <= 30:
    raise RuntimeError("MAX_HISTORY_MESSAGES must be between 0 and 30")

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY, timeout=OPENAI_TIMEOUT, max_retries=2)
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
<body><div class="container"><h1>Home Assistant AI</h1><div class="controls"><label for="model">KI-Modell:</label><select id="model"></select><span id="modelStatus" class="status"></span></div><div id="chat"></div><div class="input-row"><input id="message" type="text" placeholder="z.B. Wie warm ist der Vorlauf?" autocomplete="off"/><button id="send">Senden</button></div><div class="footer" id="versionInfo">Agent v' + 'AGENT_VERSION" – MCP wird geladen …</div></div>
<script>
const input=document.getElementById("message"),button=document.getElementById("send"),chat=document.getElementById("chat"),modelSelect=document.getElementById("model"),modelStatus=document.getElementById("modelStatus");
const history=[];
function addMessage(text,type){const div=document.createElement("div");div.className="message "+type;div.textContent=text;chat.appendChild(div);chat.scrollTop=chat.scrollHeight}
async function loadModels(){try{const response=await fetch("/models"),data=await response.json();modelSelect.innerHTML="";for(const model of data.models||[]){const option=document.createElement("option");option.value=model.id;option.textContent=model.name+(model.available?"":" (nicht verfügbar)");option.disabled=!model.available;modelSelect.appendChild(option)}modelSelect.value=data.default_model||"openai"}catch(error){modelStatus.textContent="Modellliste nicht verfügbar"}}
async function sendMessage(){const message=input.value.trim();if(!message)return;addMessage(message,"user");history.push({role:"user",content:message});input.value="";button.disabled=true;modelSelect.disabled=true;addMessage("Denke nach ...","assistant");try{const response=await fetch("/chat",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({message,model:modelSelect.value,history:history.slice(0,-1).slice(-12)})}),data=await response.json();chat.lastChild.remove();if(!response.ok||data.error){history.pop();addMessage("Fehler: "+(data.error||"Unbekannter Fehler"),"assistant")}else{addMessage(data.response,"assistant");history.push({role:"assistant",content:data.response});if(history.length>12)history.splice(0,history.length-12)}}catch(error){chat.lastChild.remove();history.pop();addMessage("Verbindungsfehler: "+error,"assistant")}finally{button.disabled=false;modelSelect.disabled=false;input.focus()}}
async function loadVersion(){try{const response=await fetch("/version");const data=await response.json();document.getElementById("versionInfo").textContent=`Agent v${data.agent_version} · MCP v${data.mcp_version||"unbekannt"}`;}catch(error){document.getElementById("versionInfo").textContent="Agent v' + 'AGENT_VERSION" · MCP-Version nicht verfügbar"}}
button.addEventListener("click",sendMessage);input.addEventListener("keydown",event=>{if(event.key==="Enter")sendMessage()});input.focus();loadModels();loadVersion();
</script></body></html>
"""

SYSTEM_PROMPT=("You are a Home Assistant AI assistant. Use the available Home Assistant tools to answer questions. IMPORTANT: Before giving platform-dependent advice or instructions about Add-ons, Supervisor, MQTT installation, backups, updates, configuration capabilities, or other installation-specific features, call get_home_assistant_info and use its returned capabilities. Never assume Home Assistant OS. If supervisor_available or addon_store_available is false, do not recommend or reference the Home Assistant Add-on Store or Supervisor; explain that the connected installation does not expose those capabilities and, where appropriate, describe the platform-neutral or external-service alternative. Use the actual connected Home Assistant environment, not generic Home Assistant assumptions. For questions about current Home Assistant devices, entities, sensors, states, temperatures, switches, printers, energy, or other live values, you MUST use the Home Assistant tools rather than relying on general knowledge. For unknown devices or sensors, search_entities first. Never invent entity IDs, values or states. Use get_entity_state when current state is required. Use call_service only for actions allowed by the MCP server. Configuration editing is a separate privileged capability. If the user asks to read or change YAML, first call configuration_status. If configuration editing is disabled, explain that it must be enabled. If enabled and the user explicitly asks to change an allowed YAML file, read the current file first, make the smallest necessary change, preserve all unrelated content, and then call update_config with the complete new file. Do not claim a configuration change was made unless update_config returns success. Never replace a configuration file with a guessed or unrelated example. For a requested configuration change, explain what will be changed before performing it when the change is consequential. For a harmless test such as adding a comment, it is acceptable to execute directly when explicitly requested. For Lovelace dashboards, always call list_dashboards before create_dashboard. If a dashboard with the requested URL path already exists, never call create_dashboard. Tell the user that the dashboard already exists and, if their request is to modify it, use read_dashboard followed by the smallest necessary update_dashboard change. Only call create_dashboard when the requested dashboard does not already exist. Do not claim a dashboard was created or changed unless the corresponding MCP tool returns success. IMPORTANT: maintain conversational context. A short reply such as 'ja', 'nein', 'mach das', 'weiter' or 'genau' refers to the immediately preceding assistant message and must be interpreted using the supplied conversation history. Do not restart the conversation or answer with a generic greeting. For questions about the built-in Home Assistant Energy dashboard, distinguish between the dashboard being visible and its energy sources being configured. Do not invent a manual procedure if the available MCP tools can inspect or change the actual configuration. If the user asks to search for available energy sources, actually search the current Home Assistant entities and report the matching entities and their relevant attributes. If the user asks to configure the Energy dashboard and an energy-specific tool is available, use it rather than editing unrelated YAML. If the requested capability is not exposed by the MCP, say so explicitly. If you offer to search for something and the user replies 'Ja', perform that search immediately. For questions asking whether the installed Home Assistant version is current, latest, newest, or whether an update is available, use get_home_assistant_info for the installed Core version and use the verified release context supplied by the Agent. Never infer that an installed version is current merely because it is recent. If the release check is unavailable, say that current release status could not be verified rather than guessing. When comparing versions, distinguish stable releases from beta/development releases. For questions about HACS, always call get_hacs_info first. Do not assume HACS is installed and do not use Supervisor or the Add-on Store as a proxy for HACS. Use the returned HACS version, latest stable version, installed repository count, categories and update information. If HACS or its repository storage cannot be detected, report that limitation instead of guessing.")

async def get_latest_home_assistant_release() -> dict[str, str | bool | None]:
    """Check the current stable Home Assistant Core release from the official GitHub repository."""
    url="https://api.github.com/repos/home-assistant/core/releases/latest"
    def fetch_release():
        request=urllib.request.Request(url,headers={"Accept":"application/vnd.github+json","User-Agent":"home-assistant-agent"})
        with urllib.request.urlopen(request,timeout=RELEASE_CHECK_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    try:
        data=await asyncio.wait_for(asyncio.to_thread(fetch_release),timeout=RELEASE_CHECK_TIMEOUT+1)
        tag=str(data.get("tag_name") or "").strip()
        if not tag:return {"available":False,"version":None,"source":url,"message":"The release endpoint returned no stable version."}
        return {"available":True,"version":tag,"published_at":data.get("published_at"),"url":data.get("html_url"),"source":url}
    except (asyncio.TimeoutError,urllib.error.URLError,urllib.error.HTTPError,OSError,ValueError) as exc:
        logger.warning("Home Assistant release check failed: %s",exc)
        return {"available":False,"version":None,"source":url,"message":"Current Home Assistant release could not be verified."}

async def enrich_release_context(message: str) -> str:
    if not re.search(r"\b(neueste|aktuellste|aktuelle version|aktuell|update|updates|release|version|latest|newest|current version)\b",message,re.IGNORECASE):
        return message
    release=await get_latest_home_assistant_release()
    if release.get("available"):
        return message+"\n\n[VERIFIED RELEASE CONTEXT — do not expose this label to the user unless useful] The official Home Assistant Core release endpoint reports the current stable release as "+str(release.get("version"))+". Published: "+str(release.get("published_at") or "unknown")+". Source: "+str(release.get("url") or release.get("source"))+". Compare this with the connected installation version returned by get_home_assistant_info. Do not infer currentness from recency alone."
    return message+"\n\n[RELEASE CHECK UNAVAILABLE] The Agent could not verify the current stable Home Assistant release. Do not guess whether the connected version is current; state that current release status could not be verified."

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

def clean_history(history):
    allowed={"user","assistant"}; cleaned=[]
    for item in history[-MAX_HISTORY_MESSAGES:]:
        if item.role in allowed and item.content.strip(): cleaned.append({"role":item.role,"content":item.content[:4000]})
    return cleaned

async def run_openai_agent(session,tools,user_message,history):
    names=tool_names(tools);messages=[{"role":"system","content":SYSTEM_PROMPT}]+clean_history(history)+[{"role":"user","content":user_message}]
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

async def run_anthropic_agent(session,tools,user_message,history):
    if not anthropic_client:raise RuntimeError("Claude is not configured: ANTHROPIC_API_KEY is missing")
    names=tool_names(tools);anthropic_tools=[{"name":t.name,"description":t.description or "Home Assistant tool","input_schema":t.input_schema} for t in tools]
    messages=clean_history(history)+[{"role":"user","content":user_message}]
    for round_number in range(MAX_TOOL_ROUNDS):
        response=await run_with_timeout(anthropic_client.messages.create(model=ANTHROPIC_MODEL,max_tokens=4096,system=SYSTEM_PROMPT,messages=messages,tools=anthropic_tools,tool_choice={"type":"auto"}),ANTHROPIC_TIMEOUT,"Anthropic request")
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

@app.get("/version")
async def version_endpoint():
    mcp_version = None
    try:
        async with streamable_http_client(MCP_URL) as (read_stream,write_stream):
            async with ClientSession(read_stream,write_stream) as session:
                init = await run_with_timeout(session.initialize(),MCP_TIMEOUT,"MCP version discovery")
                server_info = getattr(init, "server_info", None)
                mcp_version = getattr(server_info, "version", None) if server_info else None
    except Exception as exc:
        logger.warning("MCP version discovery failed: %s", exc)
    return {"agent_version": AGENT_VERSION, "mcp_version": mcp_version}

@app.get("/health")
async def health():
    return {"status":"ok","service":"home-assistant-agent","provider":"openai","model":OPENAI_MODEL,"mcp_configured":bool(MCP_URL),"anthropic_configured":bool(ANTHROPIC_API_KEY),"ollama_available":False}

@app.post("/chat")
async def chat(request:ChatRequest):
    logger.info("Processing chat request using provider=%s history=%d",request.model,len(request.history))
    try:
        if request.model not in {"openai","anthropic"}:raise ValueError("Selected model provider is not available yet")
        if request.model=="anthropic" and not ANTHROPIC_API_KEY:raise ValueError("Claude is not configured: ANTHROPIC_API_KEY is missing")
        enriched_message=await enrich_release_context(request.message)
        async with streamable_http_client(MCP_URL) as (read_stream,write_stream):
            async with ClientSession(read_stream,write_stream) as session:
                tools=await load_mcp_tools(session)
                if request.model=="anthropic":return {"response":await run_anthropic_agent(session,tools,enriched_message,request.history),"model":ANTHROPIC_MODEL,"provider":"anthropic"}
                return {"response":await run_openai_agent(session,tools,enriched_message,request.history),"model":OPENAI_MODEL,"provider":"openai"}
    except (OpenAIError,anthropic.APIError) as exc:
        logger.exception("Model provider request failed");return {"error":f"Model provider error: {exc}"}
    except (TimeoutError,OSError,RuntimeError,ValueError,PermissionError) as exc:
        logger.exception("Agent request failed");return {"error":str(exc)}
    except Exception as exc:
        logger.exception("Unexpected agent error");return {"error":f"Unexpected error: {type(exc).__name__}"}

if __name__=="__main__":uvicorn.run(app,host="0.0.0.0",port=8080)
