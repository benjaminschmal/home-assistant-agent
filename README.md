# Home Assistant AI Agent

[![Docker Build](https://github.com/benjaminschmal/home-assistant-agent/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/benjaminschmal/home-assistant-agent/actions/workflows/docker-publish.yml)

An AI assistant for Home Assistant that combines an OpenAI-powered agent with a dedicated MCP server.

The project uses a **two-container architecture**. Both components are published as independent Docker images to GitHub Container Registry and can be created as normal standalone Docker containers in QNAP Container Station — no Docker Application / Compose stack is required.

## Architecture

```text
┌──────────────────────────────┐
│ Docker Host / QNAP           │
│                              │
│  home-assistant-agent        │
│  FastAPI / Uvicorn :8080     │
│       │                      │
│       ├── OpenAI API         │
│       │                      │
│       └── MCP / HTTP         │
│              │               │
│              ▼               │
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
- `mcp-server/` — MCP server exposing Home Assistant functionality as a Docker image.

The current MCP server provides two tools:

- `search_entities` — searches Home Assistant entities by entity ID, friendly name, device class and current state.
- `get_entity_state` — retrieves the current state and attributes of an entity.

The agent discovers the MCP tools at runtime and exposes them to the OpenAI model. The agent is instructed to search for an entity first and retrieve its state when required; it must not invent sensor values.

## Docker Images

GitHub Actions automatically builds and publishes both images to **GitHub Container Registry (GHCR)** whenever changes are pushed to `main`.

### Agent

```text
ghcr.io/benjaminschmal/home-assistant-agent:latest
```

### MCP Server

```text
ghcr.io/benjaminschmal/home-assistant-mcp:latest
```

Commit-specific image tags are also published using the Git SHA for reproducible deployments.

The Docker workflow is located at:

```text
.github/workflows/docker-publish.yml
```

The **Docker Build** badge at the top of this README shows the current workflow status. It is green when both Docker images were successfully built and published.

## Requirements

### Docker host

- Docker
- OpenAI API key
- Network access between the agent and MCP server
- Network access from the MCP server to Home Assistant

### QNAP

- QNAP Container Station
- Docker support
- Home Assistant reachable from the MCP container
- Optional persistent QNAP network if a fixed MAC/IP is required

## Configuration

Secrets are deliberately kept outside Git. `.env` is ignored by Git and `.env.example` contains only placeholders.

The required environment variables are:

```text
OPENAI_API_KEY=<your-openai-api-key>
OPENAI_MODEL=gpt-5
MCP_URL=http://<MCP_HOST>:8000/mcp
MCP_TIMEOUT_SECONDS=15
OPENAI_TIMEOUT_SECONDS=60
MAX_TOOL_ROUNDS=5
LOG_LEVEL=INFO

HA_URL=http://<HOME_ASSISTANT_HOST>:8123
HA_TOKEN=<home-assistant-long-lived-access-token>
HA_TIMEOUT_SECONDS=15
MAX_SEARCH_RESULTS=50
```

The agent validates its required configuration at startup. The MCP server likewise requires `HA_URL` and `HA_TOKEN`. Timeouts and tool-call limits prevent a failed dependency or runaway tool loop from hanging the service indefinitely.

**Never commit `.env`, API keys, Home Assistant tokens, passwords or other secrets.**

## QNAP Deployment — Standalone Containers

Both components can be created directly in **QNAP Container Station → Create Container**. No Compose file and no QNAP Application are required.

### Container 1 — Home Assistant MCP

Use the following image:

```text
ghcr.io/benjaminschmal/home-assistant-mcp:latest
```

Recommended container name:

```text
home-assistant-mcp
```

Port mapping:

```text
Container port: 8000
Host port:      8000
Protocol:       TCP
```

Environment variables:

```text
HA_URL=http://<HOME_ASSISTANT_HOST>:8123
HA_TOKEN=<home-assistant-long-lived-access-token>
HA_TIMEOUT_SECONDS=15
MAX_SEARCH_RESULTS=50
```

If the QNAP installation uses a persistent external network with a fixed MAC address, configure that network and MAC address directly in Container Station. Do not store those installation-specific values in the public repository.

### Container 2 — Home Assistant Agent

Use the following image:

```text
ghcr.io/benjaminschmal/home-assistant-agent:latest
```

Recommended container name:

```text
home-assistant-agent
```

Port mapping:

```text
Container port: 8080
Host port:      8080
Protocol:       TCP
```

Environment variables:

```text
OPENAI_API_KEY=<your-openai-api-key>
OPENAI_MODEL=gpt-5
MCP_URL=http://<MCP_HOST>:8000/mcp
MCP_TIMEOUT_SECONDS=15
OPENAI_TIMEOUT_SECONDS=60
MAX_TOOL_ROUNDS=5
LOG_LEVEL=INFO
```

Set both containers to:

```text
Restart policy: unless-stopped
```

The resulting QNAP setup is intentionally simple:

```text
DOCKER
├── home-assistant-mcp
│   └── ghcr.io/benjaminschmal/home-assistant-mcp:latest
│
└── home-assistant-agent
    └── ghcr.io/benjaminschmal/home-assistant-agent:latest
```

The **Application** column should remain empty (`--`) for both containers.

## Manual Docker Deployment

The same images can be deployed on any Docker host without QNAP Container Station.

### MCP server

```bash
docker run -d \
  --name home-assistant-mcp \
  --restart unless-stopped \
  -p 8000:8000 \
  -e HA_URL=http://<HOME_ASSISTANT_HOST>:8123 \
  -e HA_TOKEN='<HOME_ASSISTANT_TOKEN>' \
  -e HA_TIMEOUT_SECONDS=15 \
  -e MAX_SEARCH_RESULTS=50 \
  ghcr.io/benjaminschmal/home-assistant-mcp:latest
```

### Agent

```bash
docker run -d \
  --name home-assistant-agent \
  --restart unless-stopped \
  -p 8080:8080 \
  -e OPENAI_API_KEY='<OPENAI_API_KEY>' \
  -e OPENAI_MODEL=gpt-5 \
  -e MCP_URL=http://<MCP_HOST>:8000/mcp \
  -e MCP_TIMEOUT_SECONDS=15 \
  -e OPENAI_TIMEOUT_SECONDS=60 \
  -e MAX_TOOL_ROUNDS=5 \
  -e LOG_LEVEL=INFO \
  ghcr.io/benjaminschmal/home-assistant-agent:latest
```

The agent web UI is available on port `8080`. The MCP server listens on port `8000`.

## Development Builds

Build the agent locally from the repository root:

```bash
docker build -t home-assistant-agent ./agent
```

Build the MCP server locally:

```bash
docker build -t home-assistant-mcp ./mcp-server
```

The GitHub Actions workflow is the standard production build and publishing path.

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

## Robustness and error handling

The runtime is deliberately defensive:

- required secrets and endpoints are validated during startup
- OpenAI requests have a configurable timeout and SDK retries
- MCP initialization, tool discovery and tool calls have configurable timeouts
- the number of model/tool rounds is limited
- invalid JSON tool arguments are rejected
- tool names are validated against the MCP tool list
- invalid Home Assistant entity IDs are rejected
- Home Assistant HTTP errors are logged without exposing the access token
- the agent returns controlled error responses instead of exposing full unexpected exception details to the browser
- `/health` provides a basic agent container health check

Entity search ranks matches instead of returning the first arbitrary substring matches. It considers normalized entity IDs, friendly names, device classes and current states.

## Testing the MCP server

The repository contains a small MCP client for connectivity and tool testing:

```text
mcp-server/test_client.py
```

The first basic checks should be:

1. MCP endpoint is reachable.
2. `list_tools()` returns `search_entities` and `get_entity_state`.
3. `search_entities` finds a known Home Assistant entity.
4. `get_entity_state` returns its current state.
5. The agent can use those tools through OpenAI tool calling.
6. `GET /health` returns `status: ok` on the agent container.

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
