# Project Status — First Working Milestone

**Date:** 2026-08-21
**Repository:** `benjaminschmal/home-assistant-agent`
**Branch:** `main`

## 1. Current architecture

```text
Client Browser
    |
    v
QNAP:192.168.1.232:8080
    |
    +-- home-assistant-agent :8080
    |       |
    |       +-- OpenAI API
    |       |
    |       +-- MCP_URL -> http://192.168.1.233:8000/mcp
    |                         |
    |                         v
    |                 home-assistant-mcp :8000
    |                         |
    |                         v
    |                 Home Assistant :8123
    |                 192.168.1.235
    |
    +-- home-assistant-mcp :8000
```

The **MCP server and the AI agent are now both running on the QNAP as Docker containers**. The Mac is currently a development workstation only and is no longer required for the runtime path.

Target principle: the production runtime must not depend on the Mac. The QNAP containers should be reproducible from the GitHub repository and later preferably from Docker Hub images.

## 2. Repository / Git status

The repository is public. No secrets are to be committed.

Relevant history:

```text
95712c5 Handle null values in entity search
c747dca Document QNAP DHCP network and fixed MAC deployment
07e00f4 Use Home Assistant WebSocket registries with REST fallback
b9a9892 Add Home Assistant WebSocket client dependency
aaf674a Improve entity discovery using Home Assistant registries
65c4654 Add agent container healthcheck
666e8ee Document hardened runtime configuration
fcb4355 Document runtime hardening settings
```

The agent behavior enhancement was committed as:

```text
ce2a13d Improve device status summaries
```

The current project-status documentation is the checkpoint for continuing development.

## 3. QNAP network

Both containers use the QNAP Docker network:

```text
qnet-dhcp-bond0-6d6da6
```

The containers use **DHCP for their IP addresses** and **fixed MAC addresses**. This follows the deployment pattern used in the KACO project and keeps the DHCP-assigned IP stable.

### MCP server

```text
Container: home-assistant-mcp
IP:        192.168.1.233
MAC:       02:42:81:c4:95:29
Port:      8000
MCP URL:   http://192.168.1.233:8000/mcp
```

### AI agent

```text
Container: home-assistant-agent
IP:        192.168.1.232
MAC:       02:42:81:c4:95:2a
Port:      8080
Web UI:    http://192.168.1.232:8080/
Health:    http://192.168.1.232:8080/health
```

### Home Assistant

```text
URL: http://192.168.1.235:8123
```

## 4. MCP server — QNAP

### Container

```text
home-assistant-mcp:latest
```

Runtime configuration:

```bash
docker run -d \
  --name home-assistant-mcp \
  --restart unless-stopped \
  --network qnet-dhcp-bond0-6d6da6 \
  --mac-address 02:42:81:c4:95:29 \
  -p 8000:8000 \
  -e HA_URL=http://192.168.1.235:8123 \
  -e HA_TOKEN='YOUR_HOME_ASSISTANT_TOKEN' \
  home-assistant-mcp:latest
```

The real `HA_TOKEN` is supplied only at runtime. It must never be committed to GitHub.

### MCP functionality validated

The MCP endpoint is reachable and the MCP client successfully discovers:

```text
search_entities
get_entity_state
```

The validated flow is:

```text
MCP client
  -> Streamable HTTP /mcp
  -> MCP server
  -> Home Assistant WebSocket / REST
  -> entity registry + state data
```

`search_entities` uses Home Assistant entity/device registries in addition to state data. Search ranking considers normalized friendly name, entity ID, registry/device information, device class, domain and current state.

Null values are handled safely by `normalize()`.

### Important MCP HTTP test note

A plain request such as:

```bash
curl http://192.168.1.233:8000/mcp
```

or a basic Python `urllib.request.urlopen()` call can return:

```text
HTTP 406 Not Acceptable
```

This is **not evidence that MCP is broken**. The Streamable HTTP MCP endpoint expects MCP-specific request/accept headers and protocol handling. The actual MCP client used by the agent has been tested successfully and receives HTTP 200 responses.

## 5. MCP deployment on QNAP without Git

The QNAP does not have Git available. Source deployment therefore uses `wget` from GitHub's raw content endpoint.

From:

```bash
cd /share/Container/home-assistant-agent
```

Example MCP update:

```bash
wget -O mcp-server/server.py https://raw.githubusercontent.com/benjaminschmal/home-assistant-agent/main/mcp-server/server.py
wget -O mcp-server/Dockerfile https://raw.githubusercontent.com/benjaminschmal/home-assistant-agent/main/mcp-server/Dockerfile
wget -O mcp-server/requirements.txt https://raw.githubusercontent.com/benjaminschmal/home-assistant-agent/main/mcp-server/requirements.txt
```

Rebuild:

```bash
docker build --no-cache -t home-assistant-mcp ./mcp-server
```

When updating the MCP container, stop/remove the old container and recreate it with the same network, fixed MAC and runtime token parameters. Never put the token into source files.

## 6. AI agent — current QNAP runtime

The agent is now running as a Docker container on the QNAP.

Current image:

```text
home-assistant-agent:latest
```

Current runtime configuration:

```bash
docker run -d \
  --name home-assistant-agent \
  --restart unless-stopped \
  --network qnet-dhcp-bond0-6d6da6 \
  --mac-address 02:42:81:c4:95:2a \
  -p 8080:8080 \
  -e MCP_URL=http://192.168.1.233:8000/mcp \
  -e OPENAI_MODEL=gpt-5 \
  -e OPENAI_API_KEY='YOUR_OPENAI_API_KEY' \
  home-assistant-agent:latest
```

The real OpenAI API key is passed as a runtime parameter and is not stored in the repository.

### Current runtime health

Validated on 2026-08-21:

```text
Container: Up / healthy
Uvicorn:   0.0.0.0:8080
Health:    HTTP 200
MCP:       HTTP 200 through the MCP client
OpenAI:    HTTP 200
```

The container log showed successful calls to:

```text
POST http://192.168.1.233:8000/mcp  -> 200 OK
POST https://api.openai.com/v1/chat/completions -> 200 OK
```

The browser successfully loaded:

```text
http://192.168.1.232:8080/
```

## 7. Agent deployment on QNAP without Git

The QNAP agent source is synchronized with `wget`.

From:

```bash
cd /share/Container/home-assistant-agent
```

Current deployment commands:

```bash
wget -O agent/agent.py https://raw.githubusercontent.com/benjaminschmal/home-assistant-agent/main/agent/agent.py
wget -O agent/Dockerfile https://raw.githubusercontent.com/benjaminschmal/home-assistant-agent/main/agent/Dockerfile
wget -O agent/requirements.txt https://raw.githubusercontent.com/benjaminschmal/home-assistant-agent/main/agent/requirements.txt
```

Build:

```bash
docker build --no-cache -t home-assistant-agent ./agent
```

Then recreate the container with the runtime command documented above.

## 8. Agent application

The agent provides:

```text
GET  /
GET  /health
POST /chat
```

The web UI is a simple browser chat interface.

Current Python base image:

```text
python:3.12-slim
```

The container runs the application as the non-root user:

```text
agent (UID 1000)
```

The current agent includes:

- OpenAI Chat Completions integration
- dynamic MCP tool discovery
- MCP tool calling
- OpenAI request timeout/retries
- MCP initialization/tool-call timeouts
- maximum tool-call rounds
- JSON argument validation
- unknown MCP tool validation
- controlled error handling
- `/health` endpoint
- environment-based configuration

## 9. Entity discovery / HP printer test

The MCP layer successfully discovered the HP printer through searches such as `HP` and `M477`.

Validated entities:

```text
sensor.hp_color_laserjet_mfp_m477fdn
sensor.hp_color_laserjet_mfp_m477fdn_black_cartridge_hp_cf410x
sensor.hp_color_laserjet_mfp_m477fdn_cyan_cartridge_hp_cf411x
sensor.hp_color_laserjet_mfp_m477fdn_magenta_cartridge_hp_cf413x
sensor.hp_color_laserjet_mfp_m477fdn_yellow_cartridge_hp_cf412x
```

Observed test values:

```text
Printer: idle
Black: 48 %
Cyan: 95 %
Magenta: 96 %
Yellow: 96 %
```

The MCP search results identify the cartridge entities as belonging to the same HP printer device.

## 10. Current known limitation — natural-language device aliases

The infrastructure is working, but the natural-language search is **not yet fully robust**.

Direct MCP searches showed:

```text
HP        -> printer + cartridges found
M477      -> printer + cartridges found
printer   -> []
toner     -> []
vorlauf   -> []
```

Therefore a user question such as:

> Was macht der Drucker?

can still result in the agent saying that no printer was found, even though the actual printer entities are available in Home Assistant.

The agent did call `search_entities` multiple times during the failed natural-language test, so the MCP connection itself was working. The remaining issue is the **semantic/entity resolution strategy**, not Docker, networking, OpenAI authentication or MCP connectivity.

This is intentionally left as the next development item rather than being changed blindly in the current milestone.

Potential future improvement:

- better German/English device alias handling
- query expansion/synonyms
- multi-query entity resolution
- grouping related entities by Home Assistant device
- logging of the actual MCP tool arguments during debugging

Do not hardcode the HP printer as the solution. The goal is a generic Home Assistant entity-resolution mechanism.

## 11. Current agent behavior enhancement

The current agent system instructions tell the model to consider related entities belonging to the same device when answering device-status questions. For example, if a printer and its cartridge entities are returned for the same device, the intended answer should contain the printer status and relevant toner levels.

The code must still obey these rules:

- never invent entity IDs
- never invent states or measurements
- never invent device relationships
- use actual MCP results
- state which entity was used when useful

The enhancement is associated with:

```text
ce2a13d Improve device status summaries
```

## 12. Security

The GitHub repository is public.

Never commit:

```text
OPENAI_API_KEY
HA_TOKEN
passwords
private keys
.env files
other credentials
```

Secrets are supplied at runtime through Docker environment parameters or local ignored configuration.

If a secret is accidentally exposed in shell output, logs, screenshots or Git, revoke and recreate it.

## 13. Useful QNAP commands

Check both containers:

```bash
docker ps | grep -E 'home-assistant-(mcp|agent)'
```

Agent logs:

```bash
docker logs --tail 100 home-assistant-agent
```

MCP logs:

```bash
docker logs --tail 100 home-assistant-mcp
```

Agent health:

```bash
curl http://192.168.1.232:8080/health
```

Check agent network identity:

```bash
docker inspect home-assistant-agent | grep -i -E 'MacAddress|IPAddress|NetworkID'
```

Check MCP network identity:

```bash
docker inspect home-assistant-mcp | grep -i -E 'MacAddress|IPAddress|NetworkID'
```

Check agent environment without displaying the secret:

```bash
docker exec home-assistant-agent python -c "
import os
print('MCP_URL:', os.environ.get('MCP_URL'))
print('MODEL:', os.environ.get('OPENAI_MODEL'))
print('OPENAI_KEY:', 'gesetzt' if os.environ.get('OPENAI_API_KEY') else 'FEHLT')
"
```

Check MCP environment without displaying the token:

```bash
docker exec home-assistant-mcp python -c "
import os
print('HA_URL:', os.environ.get('HA_URL'))
print('HA_TOKEN:', 'gesetzt' if os.environ.get('HA_TOKEN') else 'FEHLT')
"
```

## 14. Rebuild/redeploy workflow

### MCP

```text
GitHub main
   -> wget mcp-server files on QNAP
   -> docker build --no-cache -t home-assistant-mcp ./mcp-server
   -> recreate home-assistant-mcp
   -> verify MCP
```

### Agent

```text
GitHub main
   -> wget agent files on QNAP
   -> docker build --no-cache -t home-assistant-agent ./agent
   -> recreate home-assistant-agent
   -> verify /health
   -> verify browser UI
   -> verify natural-language request
```

The Mac is not required for deployment once the GitHub files are available.

## 15. First working milestone

The following read-only path is working:

- QNAP Docker networking
- DHCP with fixed MAC addresses
- Home Assistant connectivity
- Home Assistant authentication
- MCP Streamable HTTP endpoint
- entity/device registry discovery
- current entity state retrieval
- OpenAI integration
- MCP tool calling from the AI agent
- browser UI
- persistent QNAP agent container
- persistent QNAP MCP container
- no credentials stored in Git

The remaining known functional limitation is semantic resolution of generic natural-language device terms such as `Drucker` to entities whose searchable metadata does not contain the literal term `printer`/`drucker`.

## 16. Next session — start here

Before changing code, verify the runtime:

```bash
cd /share/Container/home-assistant-agent

docker ps | grep -E 'home-assistant-(mcp|agent)'

curl http://192.168.1.232:8080/health

docker logs --tail 30 home-assistant-agent
```

Then the first development task should be:

**Improve generic natural-language entity resolution without hardcoding specific devices.**

The current HP printer example is the reference test case, but the solution should remain generic for the complete Home Assistant entity registry.
