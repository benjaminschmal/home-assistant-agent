import asyncio
import json
import logging
import os

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
import uvicorn
from openai import AsyncOpenAI, OpenAIError
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from config import load_config, save_config, public_config

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("home-assistant-agent")

app = FastAPI(title="AI Agent")

BACKENDS = [
    {"id": "homeassistant", "name": "Home Assistant", "available": True},
    {"id": "salesforce", "name": "Salesforce", "available": False, "status": "Connector vorbereitet"},
]
PROVIDERS = [
    {"id": "openai", "name": "OpenAI / GPT", "available": True},
    {"id": "ollama", "name": "Ollama (lokal)", "available": False, "status": "Connector vorbereitet"},
]

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    model: str = "openai"

class SetupRequest(BaseModel):
    ai_provider: str = "openai"
    backend: str = "homeassistant"
    openai_api_key: str = ""
    openai_model: str = "gpt-5"
    mcp_url: str = ""
    ha_url: str = ""
    ha_token: str = ""
    salesforce_instance_url: str = ""
    salesforce_client_id: str = ""
    salesforce_client_secret: str = ""


def configured() -> bool:
    return bool(load_config().get("setup_complete"))


def validate_setup(request: SetupRequest) -> None:
    if request.ai_provider not in {item["id"] for item in PROVIDERS}:
        raise ValueError("Unknown AI provider")
    if request.backend not in {item["id"] for item in BACKENDS}:
        raise ValueError("Unknown backend")
    if request.ai_provider == "openai" and not request.openai_api_key and not load_config().get("openai_api_key"):
        raise ValueError("OpenAI API key is required")
    if request.backend == "homeassistant" and not request.mcp_url and not load_config().get("homeassistant", {}).get("mcp_url"):
        raise ValueError("MCP URL is required for Home Assistant")
    if request.backend == "salesforce":
        raise ValueError("Salesforce connector is prepared but not enabled yet")
    if request.ai_provider == "ollama":
        raise ValueError("Ollama connector is prepared but not enabled yet")


def setup_html() -> str:
    return SETUP_HTML


SETUP_HTML = r'''<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>AI Agent Setup</title><style>
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f5f5f5;margin:0}.card{max-width:720px;margin:35px auto;background:#fff;padding:28px;border-radius:16px;box-shadow:0 4px 20px #0001}h1{margin-top:0}.grid{display:grid;gap:14px}label{font-weight:600}input,select{box-sizing:border-box;width:100%;padding:12px;border:1px solid #ccc;border-radius:8px;font-size:15px}button{padding:12px 18px;border:0;border-radius:8px;background:#1976d2;color:white;font-size:15px;cursor:pointer}.hidden{display:none}.hint{font-size:13px;color:#666}.error{color:#b00020;margin-top:10px}.section{border-top:1px solid #eee;padding-top:18px;margin-top:18px}</style></head><body><div class="card"><h1>AI Agent Setup</h1><p class="hint">Wähle zuerst KI-Provider und Backend. Salesforce und Ollama sind bereits als Erweiterung vorbereitet.</p><div class="grid">
<label>KI-Provider</label><select id="provider"><option value="openai">OpenAI / GPT</option><option value="ollama" disabled>Ollama (später)</option></select>
<label>Backend</label><select id="backend"><option value="homeassistant">Home Assistant</option><option value="salesforce">Salesforce (Connector vorbereitet)</option></select>
<div id="openai" class="section"><label>OpenAI API Key</label><input id="key" type="password" placeholder="sk-..."><label>Modell</label><input id="model" value="gpt-5"></div>
<div id="ha" class="section"><label>MCP URL</label><input id="mcp" placeholder="http://home-assistant-mcp:8000/mcp"><label>Home Assistant URL <span class="hint">(für die Backend-Verbindung)</span></label><input id="haurl" placeholder="http://homeassistant:8123"><label>Home Assistant Token</label><input id="hatoken" type="password"></div>
<div id="sf" class="section hidden"><p><strong>Salesforce</strong></p><p class="hint">Der Salesforce-Connector wird im nächsten Schritt aktiviert. Zugangsdaten können danach hier hinterlegt werden.</p><label>Instance URL</label><input id="sfurl" placeholder="https://your-org.my.salesforce.com"><label>Client ID</label><input id="sfid"><label>Client Secret</label><input id="sfsecret" type="password"></div>
<button id="save">Setup speichern</button><div id="error" class="error"></div></div></div><script>
const provider=document.getElementById('provider'),backend=document.getElementById('backend'),sf=document.getElementById('sf'),ha=document.getElementById('ha');
function toggle(){sf.classList.toggle('hidden',backend.value!=='salesforce');ha.classList.toggle('hidden',backend.value!=='homeassistant');document.getElementById('openai').classList.toggle('hidden',provider.value!=='openai')}
backend.onchange=toggle;provider.onchange=toggle;toggle();
document.getElementById('save').onclick=async()=>{const error=document.getElementById('error');error.textContent='';const body={ai_provider:provider.value,backend:backend.value,openai_api_key:document.getElementById('key').value,openai_model:document.getElementById('model').value,mcp_url:document.getElementById('mcp').value,ha_url:document.getElementById('haurl').value,ha_token:document.getElementById('hatoken').value,salesforce_instance_url:document.getElementById('sfurl').value,salesforce_client_id:document.getElementById('sfid').value,salesforce_client_secret:document.getElementById('sfsecret').value};try{const r=await fetch('/api/setup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const d=await r.json();if(!r.ok)throw Error(d.error||'Setup fehlgeschlagen');location.href='/'}catch(e){error.textContent=e.message}};
</script></body></html>'''

CHAT_HTML = r'''<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>AI Agent</title><style>
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f5f5f5;margin:0}.card{max-width:900px;margin:35px auto;background:#fff;padding:24px;border-radius:16px;box-shadow:0 4px 20px #0001}h1{margin-top:0}.bar{display:flex;gap:10px;align-items:center;margin-bottom:14px}.badge{padding:7px 10px;border-radius:8px;background:#eee;font-size:13px}.link{margin-left:auto}.chat{min-height:350px;max-height:600px;overflow:auto;border:1px solid #ddd;border-radius:10px;padding:15px}.msg{margin:10px 0;padding:11px 13px;border-radius:10px;white-space:pre-wrap}.user{background:#e8f0fe}.assistant{background:#f0f0f0}.row{display:flex;gap:10px;margin-top:14px}input{flex:1;padding:13px;border:1px solid #ccc;border-radius:8px;font-size:16px}button{padding:13px 18px;border:0;border-radius:8px;background:#1976d2;color:white}select{padding:9px;border:1px solid #ccc;border-radius:8px}</style></head><body><div class="card"><div class="bar"><h1>AI Agent</h1><span class="badge" id="provider"></span><span class="badge" id="backend"></span><a class="link" href="/setup">Settings</a></div><div id="chat" class="chat"></div><div class="row"><input id="message" placeholder="Nachricht ..."><button id="send">Senden</button></div></div><script>
const chat=document.getElementById('chat'),input=document.getElementById('message'),send=document.getElementById('send');function add(t,c){const d=document.createElement('div');d.className='msg '+c;d.textContent=t;chat.appendChild(d);chat.scrollTop=chat.scrollHeight}async function init(){const r=await fetch('/api/config');const c=await r.json();document.getElementById('provider').textContent=c.ai_provider;document.getElementById('backend').textContent=c.backend}async function go(){const m=input.value.trim();if(!m)return;add(m,'user');input.value='';send.disabled=true;add('Denke nach ...','assistant');try{const r=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:m,model:'openai'})});const d=await r.json();chat.lastChild.remove();add(r.ok&&!d.error?d.response:'Fehler: '+(d.error||'Unbekannter Fehler'),'assistant')}catch(e){chat.lastChild.remove();add('Verbindungsfehler: '+e,'assistant')}finally{send.disabled=false;input.focus()}}send.onclick=go;input.onkeydown=e=>{if(e.key==='Enter')go()};init();input.focus();</script></body></html>'''

@app.get('/', response_class=HTMLResponse)
async def index():
    return setup_html() if not configured() else CHAT_HTML

@app.get('/setup', response_class=HTMLResponse)
async def setup():
    return setup_html()

@app.get('/api/config')
async def api_config():
    return public_config(load_config())

@app.get('/api/backends')
async def api_backends():
    return {"backends": BACKENDS}

@app.get('/models')
async def models():
    config = load_config()
    return {"default_model": config.get("ai_provider", "openai"), "providers": PROVIDERS, "backends": BACKENDS}

@app.post('/api/setup')
async def api_setup(request: SetupRequest):
    try:
        validate_setup(request)
        config = load_config()
        if request.openai_api_key and request.openai_api_key != '********': config['openai_api_key'] = request.openai_api_key
        config['ai_provider'] = request.ai_provider
        config['openai_model'] = request.openai_model.strip() or 'gpt-5'
        config['backend'] = request.backend
        config['homeassistant'].update({"mcp_url": request.mcp_url.strip(), "ha_url": request.ha_url.strip()})
        if request.ha_token: config['homeassistant']['ha_token'] = request.ha_token
        config['salesforce'].update({"instance_url": request.salesforce_instance_url.strip(), "client_id": request.salesforce_client_id.strip()})
        if request.salesforce_client_secret: config['salesforce']['client_secret'] = request.salesforce_client_secret
        config['setup_complete'] = True
        save_config(config)
        return {"success": True, "config": public_config(config)}
    except ValueError as exc:
        return {"error": str(exc)}

@app.get('/health')
async def health():
    config = load_config()
    return {"status":"ok","service":"ai-agent","setup_complete":bool(config.get('setup_complete')),"provider":config.get('ai_provider'),"backend":config.get('backend')}

async def run_timeout(awaitable, timeout: float, operation: str):
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise TimeoutError(f"{operation} timed out after {timeout:.0f}s") from exc

async def load_mcp_tools(session):
    config=load_config(); timeout=float(config['agent']['mcp_timeout_seconds'])
    await run_timeout(session.initialize(), timeout, 'MCP initialization')
    return (await run_timeout(session.list_tools(), timeout, 'MCP tool discovery')).tools

async def run_agent(session, tools, user_message: str):
    config=load_config(); openai_tools=[]; names=set()
    for tool in tools:
        names.add(tool.name);openai_tools.append({"type":"function","function":{"name":tool.name,"description":tool.description or 'Backend tool',"parameters":tool.input_schema}})
    messages=[{"role":"system","content":"You are an AI agent. Use the available backend tools. Never invent IDs or current values. For configuration changes, first inspect current configuration and make the smallest requested change. Never claim a write succeeded unless the tool confirms success."},{"role":"user","content":user_message}]
    client=AsyncOpenAI(api_key=config['openai_api_key'],timeout=float(config['agent']['openai_timeout_seconds']),max_retries=2)
    for _ in range(int(config['agent']['max_tool_rounds'])):
        response=await run_timeout(client.chat.completions.create(model=config['openai_model'],messages=messages,tools=openai_tools,tool_choice='auto'),float(config['agent']['openai_timeout_seconds']),'OpenAI request')
        message=response.choices[0].message
        if not message.tool_calls:return message.content or ''
        messages.append(message.model_dump(exclude_none=True))
        for call in message.tool_calls:
            if call.function.name not in names: raise RuntimeError(f"Unknown backend tool: {call.function.name}")
            try: arguments=json.loads(call.function.arguments or '{}')
            except json.JSONDecodeError as exc: raise RuntimeError('Invalid tool arguments') from exc
            result=await run_timeout(session.call_tool(call.function.name,arguments),float(config['agent']['mcp_timeout_seconds']),f"Backend tool {call.function.name}")
            text=''.join(getattr(c,'text','') or '' for c in result.content)
            messages.append({"role":"tool","tool_call_id":call.id,"content":text or 'No data returned.'})
    raise RuntimeError('Maximum tool-call rounds exceeded')

@app.post('/chat')
async def chat(request: ChatRequest):
    config=load_config()
    if not config.get('setup_complete'): return {"error":"Agent setup is not complete"}
    if config.get('ai_provider')!='openai': return {"error":"Selected AI provider is not available yet"}
    if config.get('backend')=='salesforce': return {"error":"Salesforce backend is prepared but the connector is not enabled yet"}
    if config.get('backend')!='homeassistant': return {"error":"Unknown backend"}
    mcp_url=config['homeassistant'].get('mcp_url','')
    if not mcp_url:return {"error":"Home Assistant MCP URL is not configured"}
    try:
        async with streamable_http_client(mcp_url) as (read_stream,write_stream):
            async with ClientSession(read_stream,write_stream) as session:
                tools=await load_mcp_tools(session)
                return {"response":await run_agent(session,tools,request.message),"model":config['openai_model'],"provider":config['ai_provider'],"backend":config['backend']}
    except OpenAIError as exc:
        logger.exception('OpenAI request failed');return {"error":f"OpenAI error: {exc}"}
    except Exception as exc:
        logger.exception('Agent request failed');return {"error":str(exc)}

if __name__=='__main__':
    uvicorn.run(app,host='0.0.0.0',port=8080)
