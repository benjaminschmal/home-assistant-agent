# Home Assistant AI Agent

[![Docker Build](https://github.com/benjaminschmal/home-assistant-agent/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/benjaminschmal/home-assistant-agent/actions/workflows/docker-publish.yml)

An AI assistant for Home Assistant that combines an OpenAI-powered agent with a dedicated MCP server.

The project uses a **two-container architecture**. Both components are published as independent Docker images to GitHub Container Registry and can be created as normal standalone Docker containers in QNAP Container Station — no Docker Application / Compose stack is required.

## Architecture

```text
QNAP / Docker
│
├── home-assistant-agent :8080
│      │
│      ├── OpenAI API
│      └── MCP → home-assistant-mcp :8000
│                         │
│                         ▼
│                  Home Assistant :8123
└────────────────────────────────────────
```

- `agent/` — AI agent and web UI.
- `mcp-server/` — MCP integration layer to Home Assistant.

The MCP currently provides entity search, state lookup and controlled Home Assistant service calls. Configuration editing can be explicitly enabled for test environments.

## Docker Images

GitHub Actions builds and publishes both images to GHCR on pushes to `main`:

```text
ghcr.io/benjaminschmal/home-assistant-agent:latest
ghcr.io/benjaminschmal/home-assistant-mcp:latest
```

Commit-specific tags are also published. The **Docker Build** badge is green when the workflow succeeds.

## Configuration

Keep secrets outside Git. Required variables:

```text
# Agent
OPENAI_API_KEY=<your-openai-api-key>
OPENAI_MODEL=gpt-5
MCP_URL=http://<MCP_HOST>:8000/mcp
MCP_TIMEOUT_SECONDS=15
OPENAI_TIMEOUT_SECONDS=60
MAX_TOOL_ROUNDS=5
LOG_LEVEL=INFO

# MCP
HA_URL=http://<HOME_ASSISTANT_HOST>:8123
HA_TOKEN=<home-assistant-long-lived-access-token>
HA_TIMEOUT_SECONDS=15
MAX_SEARCH_RESULTS=50
```

**Never commit API keys, Home Assistant tokens, passwords or other secrets.**

## QNAP Deployment — Standalone Containers

Create both containers directly in **QNAP Container Station → Create Container**. No Compose/Application is required.

### MCP

```text
ghcr.io/benjaminschmal/home-assistant-mcp:latest
```

Name: `home-assistant-mcp`  
Port: `8000 → 8000/TCP`

Environment: `HA_URL`, `HA_TOKEN`, `HA_TIMEOUT_SECONDS`, `MAX_SEARCH_RESULTS`.

### Agent

```text
ghcr.io/benjaminschmal/home-assistant-agent:latest
```

Name: `home-assistant-agent`  
Port: `8080 → 8080/TCP`

Environment: `OPENAI_API_KEY`, `OPENAI_MODEL`, `MCP_URL`, `MCP_TIMEOUT_SECONDS`, `OPENAI_TIMEOUT_SECONDS`, `MAX_TOOL_ROUNDS`, `LOG_LEVEL`.

Recommended restart policy: `unless-stopped`. The **Application** column should remain `--` for both containers.

## Capabilities

### Read

- `search_entities` — find Home Assistant entities.
- `get_entity_state` — read current state and attributes.

### Control

- `call_service` — execute only services in the MCP allowlist (for example lights, switches, climate, covers and media players).

### Configuration editing — opt-in

Configuration editing is **disabled by default**:

```text
MCP_ALLOW_CONFIGURATION=false
```

For the isolated test Home Assistant it can be enabled with:

```text
MCP_ALLOW_CONFIGURATION=true
```

The MCP then exposes `configuration_status`, `read_config` and `update_config` for these files only:

```text
configuration.yaml
automations.yaml
scripts.yaml
scenes.yaml
```

Before writing, the new content is YAML-validated and the existing file is backed up. The MCP container must have the HA `/config` directory mounted, for example:

```text
/share/Container/Home Assistant Test → /config
```

**Recommended activation checklist for the test HA:**

- [ ] Isolated/test Home Assistant
- [ ] `/config` mounted into MCP
- [ ] `MCP_ALLOW_CONFIGURATION=true`
- [ ] Test read of `configuration.yaml`
- [ ] Test backup + YAML validation + write
- [ ] Verify Home Assistant after the change

Do not enable configuration editing on the production Home Assistant until the complete test flow is validated.

## Manual Docker Deployment

### MCP

```bash
docker run -d --name home-assistant-mcp --restart unless-stopped \
  -p 8000:8000 \
  -e HA_URL=http://<HOME_ASSISTANT_HOST>:8123 \
  -e HA_TOKEN='<HOME_ASSISTANT_TOKEN>' \
  -e HA_TIMEOUT_SECONDS=15 \
  -e MAX_SEARCH_RESULTS=50 \
  ghcr.io/benjaminschmal/home-assistant-mcp:latest
```

### Agent

```bash
docker run -d --name home-assistant-agent --restart unless-stopped \
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

## Testing

The repository contains `mcp-server/test_client.py` for MCP connectivity/tool testing. Basic validation should cover:

1. MCP endpoint and tool discovery.
2. Entity search and state lookup.
3. Agent tool calling through OpenAI.
4. Controlled service calls.
5. Configuration editing only when explicitly enabled.
6. Agent `/health` endpoint.

## Security

This repository is public. Keep all secrets and deployment-specific values outside Git, including API keys, HA tokens, passwords, private keys, IP/MAC-specific values and QNAP paths.

Configuration editing is intentionally opt-in and limited to a small YAML allowlist. Always test it on an isolated Home Assistant before enabling it for production.

## Development

Build locally from the repository root:

```bash
docker build -t home-assistant-agent ./agent
docker build -t home-assistant-mcp ./mcp-server
```

GitHub Actions is the standard production build/publish path.

## Project structure

```text
home-assistant-agent/
├── agent/
├── mcp-server/
├── deploy/qnap/
├── .env.example
├── .gitignore
└── README.md
```

## Current status

The agent, MCP server and Home Assistant integration have been validated end-to-end with the test Home Assistant. Controlled device actions and opt-in YAML configuration editing are the next test stages.
