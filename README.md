# Home Assistant AI Agent

An AI assistant for Home Assistant that combines an OpenAI-powered agent with a dedicated MCP server.

The project is designed as a **two-container architecture**:

- the **Home Assistant Agent** runs as a Docker container
- the **Home Assistant MCP Server** runs as a Docker container on the QNAP

The repository is intended to be public. Secrets and deployment-specific values are therefore kept outside Git.

## Architecture

```text
┌──────────────────────────────┐
│ Docker Host                  │
│                              │
│  home-assistant-agent        │
│  FastAPI / Uvicorn :8080     │
│       │                      │
│       ├── OpenAI API         │
│       │                      │
│       └── MCP / HTTP         │
└───────┼──────────────────────┘
        │
        ▼
┌──────────────────────────────┐
│ QNAP                         │
│                              │
│  home-assistant-mcp          │
│  MCP Server :8000            │
│       │                      │
│       ▼                      │
│  Home Assistant :8123        │
└──────────────────────────────┘
```

The agent and MCP server are independent containers. The MCP server is the integration layer to Home Assistant. The agent communicates with it through the configured `MCP_URL`.

### Components

- `agent/` — AI agent and web UI, packaged as a Docker image.
- `mcp-server/` — MCP server exposing Home Assistant functionality as tools.
- `deploy/qnap/` — QNAP deployment template.

The current MCP server provides two tools:

- `search_entities` — searches Home Assistant entities by entity ID or friendly name.
- `get_entity_state` — retrieves the current state and attributes of an entity.

The agent discovers the MCP tools at runtime and exposes them to the OpenAI model. The agent is instructed to search for an entity first and retrieve its state when required; it must not invent sensor values.

## Requirements

### Agent host

- Docker
- Docker Compose or equivalent Docker runtime
- OpenAI API key
- Network access to the QNAP MCP server

### QNAP

- Docker
- Docker Compose
- Persistent QNAP network
- Home Assistant reachable from the MCP container

## Configuration

Secrets are deliberately kept outside Git. `.env` is ignored by Git and `.env.example` contains only placeholders.

The environment file contains the configuration for both application components when used for deployment:

```text
OPENAI_API_KEY=<your-openai-api-key>
OPENAI_MODEL=gpt-5
MCP_URL=http://<QNAP_IP>:8000/mcp

HA_URL=http://<HOME_ASSISTANT_IP>:8123
HA_TOKEN=<home-assistant-long-lived-access-token>
```

The agent loads the OpenAI configuration and `MCP_URL` from its environment. The MCP server reads `HA_URL` and `HA_TOKEN` from its environment.

**Never commit `.env`, API keys, Home Assistant tokens, passwords or other secrets.**

## Agent deployment

The agent is intended to run **as a Docker container**, not as a Python process on a developer workstation.

Build the agent image from the repository root:

```bash
docker build -t home-assistant-agent ./agent
```

Run it with the environment file:

```bash
docker run -d \
  --name home-assistant-agent \
  --env-file .env \
  -p 8080:8080 \
  home-assistant-agent:latest
```

The web UI is then available on port `8080` of the Docker host.

For production or permanent deployment, use the same image through Docker Compose or another container deployment mechanism. The container should be restarted automatically, for example with:

```yaml
restart: unless-stopped
```

The agent image contains Python, the required Python dependencies and the application itself. No local Python installation is required on the Docker host.

## QNAP MCP deployment

The MCP server is the persistent integration point to Home Assistant and is deployed on the QNAP.

Build the MCP image from the repository root:

```bash
docker build -t home-assistant-mcp ./mcp-server
```

The QNAP deployment uses an external Docker network and a fixed MAC address. Copy the example file from:

```text
deploy/qnap/docker-compose.yml.example
```

and replace the placeholders with the real deployment values on the QNAP.

The deployment template uses:

- container name `home-assistant-mcp`
- port `8000`
- external QNAP network
- explicit MAC address
- `.env` for secrets
- `restart: unless-stopped`

Example deployment:

```bash
docker compose up -d
```

The exact QNAP network name and MAC address are intentionally not stored in the public repository.

## End-to-end operation

The normal runtime flow is:

```text
User
  │
  ▼
Web UI :8080
  │
  ▼
home-assistant-agent
  │
  ├── OpenAI API
  │
  ▼
MCP :8000
  │
  ▼
home-assistant-mcp
  │
  ▼
Home Assistant :8123
```

The agent sends the user's request to the OpenAI model together with the tools discovered from the MCP server. When the model decides that Home Assistant data is required, the agent calls the corresponding MCP tool and sends the result back to the model for the final response.

## Testing the MCP server

The repository contains a small MCP client for connectivity and tool testing:

```text
mcp-server/test_client.py
```

It can be run against the QNAP MCP endpoint from a container or a Python environment with the MCP dependencies installed.

The first basic checks should be:

1. MCP endpoint is reachable.
2. `list_tools()` returns `search_entities` and `get_entity_state`.
3. `search_entities` finds a known Home Assistant entity.
4. `get_entity_state` returns its current state.
5. The agent can use those tools through OpenAI tool calling.

## Development workflow

Development should follow the containerized deployment model:

```text
1. Change code locally
2. Build/test the Docker image
3. Test the agent against the QNAP MCP server
4. Commit changes
5. Push to GitHub
6. Pull on the target Docker host
7. Rebuild/redeploy the changed container
8. Test end-to-end
```

The agent and MCP server can be developed independently. A change under `agent/` requires rebuilding the agent image. A change under `mcp-server/` requires rebuilding and redeploying the MCP image on the QNAP.

## Security

This repository is intended to be public. Therefore:

- No secrets in source code.
- No real Home Assistant tokens in Git.
- No OpenAI API keys in Git.
- No private keys or credentials.
- No QNAP filesystem paths in source files.
- Real IP addresses, MAC addresses and deployment-specific values belong in local configuration.

Before making changes public, verify the complete Git history as well as the current working tree for accidentally committed secrets.

## Project structure

```text
home-assistant-agent/
├── agent/
│   ├── agent.py
│   ├── Dockerfile
│   └── requirements.txt
├── mcp-server/
│   ├── server.py
│   ├── test_client.py
│   ├── Dockerfile
│   └── requirements.txt
├── deploy/
│   └── qnap/
│       └── docker-compose.yml.example
├── .env.example
├── .gitignore
└── README.md
```

## Current status

The complete path from the web UI through the OpenAI model and the QNAP MCP server to Home Assistant has been validated with a real Home Assistant query. The agent successfully identified the HP Color LaserJet MFP M477fdn and returned its current printer and toner status.

The next development focus is improving entity discovery, natural-language interpretation and tool handling while keeping the MCP interface stable.