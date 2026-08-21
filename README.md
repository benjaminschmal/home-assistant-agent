# Home Assistant AI Agent

An AI assistant for Home Assistant that combines an OpenAI-powered agent with a dedicated MCP server.

The project is designed so that the **agent can run locally during development**, while the **MCP server runs permanently on a QNAP** and connects to Home Assistant.

## Architecture

```text
┌──────────────────────────────┐
│ Mac                          │
│                              │
│  Browser                     │
│     │                        │
│     ▼                        │
│  Home Assistant Agent        │
│  FastAPI / Uvicorn :8080     │
│     │                        │
│     │ OpenAI API             │
│     │ MCP / HTTP             │
└─────┼────────────────────────┘
      │
      ▼
┌──────────────────────────────┐
│ QNAP                         │
│                              │
│  home-assistant-mcp          │
│  MCP Server :8000            │
│     │                        │
│     ▼                        │
│  Home Assistant :8123        │
└──────────────────────────────┘
```

### Components

- `agent/` — local AI agent and web UI.
- `mcp-server/` — MCP server exposing Home Assistant functionality as tools.
- `deploy/qnap/` — QNAP deployment template.

The current MCP server provides two tools:

- `search_entities` — searches Home Assistant entities by entity ID or friendly name.
- `get_entity_state` — retrieves the current state and attributes of an entity.

The agent discovers the MCP tools at runtime and exposes them to the OpenAI model. The agent is instructed to search for an entity first and retrieve its state when required; it must not invent sensor values.

## Requirements

### Local development

- macOS
- Python 3.12+
- Git
- OpenAI API key
- Network access to the QNAP MCP server

### QNAP

- Docker
- Docker Compose
- A persistent QNAP network
- Home Assistant reachable from the MCP container

## Configuration

Secrets are deliberately kept outside Git. `.env` is ignored by Git and `.env.example` contains only placeholders.

Create the local environment file:

```bash
cp .env.example .env
```

Set:

```text
OPENAI_API_KEY=<your-openai-api-key>
OPENAI_MODEL=gpt-5
MCP_URL=http://<QNAP_IP>:8000/mcp

HA_URL=http://<HOME_ASSISTANT_IP>:8123
HA_TOKEN=<home-assistant-long-lived-access-token>
```

The agent loads the local `.env` at startup. The MCP server reads `HA_URL` and `HA_TOKEN` from its environment.

**Never commit `.env`, API keys, Home Assistant tokens, passwords or other secrets.**

## Local development

Create a Python virtual environment:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
```

Install the agent dependencies:

```bash
python -m pip install -r agent/requirements.txt
```

Start the agent:

```bash
python agent/agent.py
```

The web UI is available at:

```text
http://localhost:8080
```

The local agent connects to the MCP server defined by `MCP_URL`; the MCP server itself does not need to run locally.

## QNAP MCP deployment

The MCP server is the persistent integration point to Home Assistant and is deployed on the QNAP.

Build the image from the repository root:

```bash
docker build -t home-assistant-mcp ./mcp-server
```

The QNAP deployment uses an external Docker network and a fixed MAC address. Copy the example file to the QNAP deployment directory and replace the placeholders with the real values:

```text
deploy/qnap/docker-compose.yml.example
```

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
5. The local agent can use those tools through OpenAI tool calling.

## Development workflow

The intended workflow is:

```text
1. Develop locally on Mac
2. Test agent against QNAP MCP
3. Commit changes
4. Push to GitHub
5. Pull on QNAP
6. Build/deploy MCP container on QNAP
7. Test end-to-end
```

The agent can normally remain local during development. Only MCP-server changes require a QNAP image rebuild/redeployment.

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

The complete path from the local web UI through the OpenAI model and the QNAP MCP server to Home Assistant has been validated with a real Home Assistant query. The agent successfully identified the HP Color LaserJet MFP M477fdn and returned its current printer and toner status.

The next development focus is improving entity discovery, natural-language interpretation and tool handling while keeping the MCP interface stable.
