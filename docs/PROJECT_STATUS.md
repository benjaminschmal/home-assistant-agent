# Project Status — First Working Milestone

**Date:** 2026-08-21
**Repository:** `benjaminschmal/home-assistant-agent`
**Branch:** `main`

## 1. Current architecture

```text
Mac (current development agent)
    |
    | MCP_URL=http://192.168.1.233:8000/mcp
    v
QNAP
    |
    +-- home-assistant-mcp :8000
    |       |
    |       v
    |   Home Assistant :8123
    |
    +-- home-assistant-agent :8080   <-- target runtime
            |
            +-- OpenAI API
            +-- MCP server
```

The **MCP server is already running persistently on the QNAP**. The AI agent is currently being developed/tested as a Python process on the Mac. The target architecture is for the agent to run permanently as a Docker container, preferably from a Docker image/Docker Hub deployment, so the Mac is not part of the production runtime.

## 2. Git status

The repository is public and currently clean on `main`.

Relevant commits reached `origin/main`:

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

The current agent enhancement commit is:

```text
ce2a13d Improve device status summaries
```

After this document is committed, pull the latest `main` on the Mac and QNAP as appropriate.

## 3. MCP server — QNAP

### Container

Container name:

```text
home-assistant-mcp
```

Image:

```text
home-assistant-mcp:latest
```

Network:

```text
qnet-dhcp-bond0-6d6da6
```

The container uses **DHCP for its IP address** and a **fixed MAC address** so that the DHCP lease remains stable.

Current deployment values used during testing:

```text
MAC: 02:42:81:c4:95:29
IP: 192.168.1.233
MCP: http://192.168.1.233:8000/mcp
```

These values are documented here as the current test/deployment state. Secrets must never be stored here or in Git.

Home Assistant endpoint:

```text
http://192.168.1.235:8123
```

The Home Assistant token is passed to the container at runtime through `HA_TOKEN`; it is **not stored in the repository**.

### Runtime parameters

The working QNAP container was started with the equivalent configuration:

```bash
docker run -d \
  --name home-assistant-mcp \
  --restart unless-stopped \
  --network qnet-dhcp-bond0-6d6da6 \
  --mac-address 02:42:81:c4:95:29 \
  -p 8000:8000 \
  -e HA_URL=http://192.168.1.235:8123 \
  -e HA_TOKEN='YOUR_TOKEN' \
  home-assistant-mcp:latest
```

Replace `YOUR_TOKEN` locally with the actual Home Assistant long-lived access token. Do not commit it.

### Deployment without Git on QNAP

The QNAP does not have Git available. Deployment from GitHub therefore uses `wget` against `raw.githubusercontent.com`.

Example:

```bash
cd /share/Container/home-assistant-agent
wget -O mcp-server/server.py https://raw.githubusercontent.com/benjaminschmal/home-assistant-agent/main/mcp-server/server.py
wget -O mcp-server/Dockerfile https://raw.githubusercontent.com/benjaminschmal/home-assistant-agent/main/mcp-server/Dockerfile
wget -O mcp-server/requirements.txt https://raw.githubusercontent.com/benjaminschmal/home-assistant-agent/main/mcp-server/requirements.txt
```

Then rebuild:

```bash
docker build --no-cache -t home-assistant-mcp ./mcp-server
```

For a production deployment, all relevant files in `mcp-server/` and `deploy/qnap/` should be synchronized before rebuilding.

## 4. MCP functionality validated

The MCP endpoint is reachable and returns HTTP 200.

The MCP client successfully discovers these tools:

```text
search_entities
get_entity_state
```

The following complete flow has been validated:

```text
MCP client
  -> streamable HTTP /mcp
  -> MCP server
  -> Home Assistant API/WebSocket
  -> entity registry/state data
```

### Entity discovery

`search_entities` now uses Home Assistant entity/device registries in addition to state data. Search ranking considers normalized:

- friendly name
- entity ID
- registry/device information
- device class
- domain
- current state

Null values are handled safely by `normalize()`.

### Real test result — HP printer

The following searches were executed successfully:

```text
HP
M477
printer
toner
```

`HP` and `M477` returned the printer and four cartridge entities. `printer` and `toner` returned no direct matches, which is acceptable because the device can still be discovered through its manufacturer/model/name.

Validated entities:

```text
sensor.hp_color_laserjet_mfp_m477fdn
sensor.hp_color_laserjet_mfp_m477fdn_black_cartridge_hp_cf410x
sensor.hp_color_laserjet_mfp_m477fdn_cyan_cartridge_hp_cf411x
sensor.hp_color_laserjet_mfp_m477fdn_magenta_cartridge_hp_cf413x
sensor.hp_color_laserjet_mfp_m477fdn_yellow_cartridge_hp_cf412x
```

Observed test state:

```text
Printer: idle
Black: 48 %
Cyan: 95 %
Magenta: 96 %
Yellow: 96 %
```

## 5. Agent — current development state

The agent is currently run on the Mac for development/testing.

Mac Python environment used successfully:

```text
Python 3.12.14
```

A virtual environment was created under:

```text
.venv/
```

Required dependencies installed successfully and import-tested:

```text
openai
mcp
fastapi
uvicorn
python-dotenv
```

The agent connects to the QNAP MCP server using:

```text
MCP_URL=http://192.168.1.233:8000/mcp
```

The OpenAI API key is provided locally through environment configuration and is not stored in Git.

## 6. Agent behavior validated

A real natural-language request was tested:

> Was macht der Drucker?

The agent successfully used the MCP server and returned the printer status:

> Der Drucker ist gerade im Leerlauf (idle).

The current agent code has since been enhanced so that, when a device-status question is asked, the model is instructed to consider related entities belonging to the same device. For the printer use case this is intended to produce a combined response containing printer status and toner levels.

The enhancement is committed as:

```text
ce2a13d Improve device status summaries
```

## 7. Current target behavior

For a question such as:

> Was macht der Drucker?

The intended response is a concise summary such as:

```text
Der HP Color LaserJet MFP M477fdn ist im Leerlauf.
Toner: Schwarz 48 %, Cyan 95 %, Magenta 96 %, Gelb 96 %.
```

The exact wording remains model-generated. Values must always come from Home Assistant tools; the agent must not invent them.

## 8. Robustness already implemented

The current implementation includes:

- mandatory configuration validation
- OpenAI request timeout and retries
- MCP initialization/tool-discovery/tool-call timeouts
- maximum model/tool-call rounds
- validation of requested MCP tools
- JSON argument validation
- Home Assistant entity ID validation
- controlled error responses
- `/health` endpoint for the agent
- safe handling of null entity metadata
- MCP server fallback from WebSocket registries to state data
- non-root user inside the MCP container
- restart policy `unless-stopped`

## 9. Security rules

The repository is public. Never commit:

- `OPENAI_API_KEY`
- `HA_TOKEN`
- passwords
- private keys
- `.env`
- other credentials

Runtime secrets are passed as Docker environment parameters or through local, ignored configuration.

If a token is accidentally exposed in shell output or logs, revoke and recreate it.

## 10. Next steps

1. Pull the latest `main` on the Mac and restart the local agent.
2. Test `Was macht der Drucker?` again and verify status + toner aggregation.
3. Keep the QNAP MCP server as the stable Home Assistant integration endpoint.
4. Containerize the agent and deploy it independently from the Mac.
5. Publish/use a Docker image for the agent so the production runtime is fully containerized.
6. Add write/action capabilities only after the read path is stable; reminders/automation actions must be implemented explicitly rather than inferred from read-only tools.
7. Add further entity discovery and natural-language tests.

## 11. First milestone definition

This milestone is considered working when:

- the QNAP MCP container starts reliably with DHCP + fixed MAC
- MCP is reachable on port 8000
- Home Assistant authentication works
- entity discovery works
- current entity state can be retrieved
- the OpenAI agent can call MCP tools
- a real Home Assistant device can be queried through natural language
- no secrets are stored in Git

All of the above have been demonstrated for the current read-only path. The remaining work is primarily productionizing the agent container and expanding the tool/action model.
