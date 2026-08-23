# Home Assistant AI Agent

[![Docker Build](https://github.com/benjaminschmal/home-assistant-agent/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/benjaminschmal/home-assistant-agent/actions/workflows/docker-publish.yml)

An AI assistant for Home Assistant with a dedicated MCP server and selectable LLM providers.

The project uses a **two-container architecture**. Both components are published as independent Docker images to GitHub Container Registry and can be created as normal standalone Docker containers in QNAP Container Station — no Docker Application / Compose stack is required.

## Architecture

```text
QNAP / Docker
│
├── home-assistant-agent :8080
│      │
│      ├── OpenAI / GPT
│      ├── Anthropic / Claude
│      └── Ollama / local (prepared, not active)
│                 │
│                 ▼
│            MCP :8000
│                 │
│                 ▼
│          Home Assistant :8123
└────────────────────────────────────────
```

- `agent/` — AI agent and web UI.
- `mcp-server/` — MCP integration layer to Home Assistant.
- The same MCP tools are used independently of the selected LLM provider.

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
# Agent - OpenAI
OPENAI_API_KEY=<your-openai-api-key>
OPENAI_MODEL=gpt-5

# Agent - Anthropic (optional)
ANTHROPIC_API_KEY=<your-anthropic-api-key>
ANTHROPIC_MODEL=claude-sonnet-4-5
ANTHROPIC_TIMEOUT_SECONDS=60

# Agent - common
MCP_URL=http://<MCP_HOST>:8000/mcp
MCP_TIMEOUT_SECONDS=15
OPENAI_TIMEOUT_SECONDS=60
MAX_TOOL_ROUNDS=8
MAX_HISTORY_MESSAGES=12
LOG_LEVEL=INFO

# MCP
HA_URL=http://<HOME_ASSISTANT_HOST>:8123
HA_TOKEN=<home-assistant-long-lived-access-token>
HA_TIMEOUT_SECONDS=15
MAX_SEARCH_RESULTS=50
```

`ANTHROPIC_API_KEY` is optional. If it is configured, **Claude** becomes available in the Agent's model selector. OpenAI remains the default. Ollama is prepared in the UI but not active yet.

`MAX_HISTORY_MESSAGES` controls how many recent user/assistant messages are supplied to the model. This keeps short follow-up replies such as **"Ja"**, **"Mach das"** or **"Weiter"** in context without creating an unbounded conversation history. Default: `12`.

**Never commit API keys, Home Assistant tokens, passwords or other secrets.**

## Dynamic Home Assistant environment detection

The Agent does not assume Home Assistant OS, Docker, QNAP or the Add-on Store. Before platform-dependent advice — for example Add-ons, Supervisor, MQTT installation, backups or updates — the Agent can call the MCP tool `get_home_assistant_info` and use the capabilities exposed by the connected Home Assistant instance.

The tool reports the Home Assistant Core version and reliable capability signals such as Supervisor/Add-on availability. If Supervisor/Add-ons are not exposed, the Agent must not recommend the Add-on Store and should use a platform-neutral or external-service approach instead.

The exact host type is intentionally not guessed when Home Assistant does not expose a reliable signal. This keeps the same Agent usable with different Home Assistant deployments.

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

Environment: `OPENAI_API_KEY`, `OPENAI_MODEL`, optional `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, `ANTHROPIC_TIMEOUT_SECONDS`, plus `MCP_URL`, `MCP_TIMEOUT_SECONDS`, `OPENAI_TIMEOUT_SECONDS`, `MAX_TOOL_ROUNDS`, `MAX_HISTORY_MESSAGES`, `LOG_LEVEL`.

Recommended restart policy: `unless-stopped`. The **Application** column should remain `--` for both containers.

The Agent UI provides a **KI-Modell** selector. Currently GPT and, when configured, Claude are active. Ollama is shown as unavailable until the local provider is implemented.

## Capabilities

### Read

- `get_home_assistant_info` — detect connected Home Assistant version and exposed platform capabilities.
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
  -e ANTHROPIC_API_KEY='<ANTHROPIC_API_KEY>' \
  -e ANTHROPIC_MODEL=claude-sonnet-4-5 \
  -e ANTHROPIC_TIMEOUT_SECONDS=60 \
  -e MCP_URL=http://<MCP_HOST>:8000/mcp \
  -e MCP_TIMEOUT_SECONDS=15 \
  -e OPENAI_TIMEOUT_SECONDS=60 \
  -e MAX_TOOL_ROUNDS=8 \
  -e MAX_HISTORY_MESSAGES=12 \
  -e LOG_LEVEL=INFO \
  ghcr.io/benjaminschmal/home-assistant-agent:latest
```

## Testing

Basic validation should cover:

1. MCP endpoint and tool discovery.
2. Dynamic Home Assistant environment detection with `get_home_assistant_info`.
3. Entity search and state lookup.
4. GPT tool calling through OpenAI.
5. Claude tool calling through Anthropic.
6. Controlled service calls.
7. Configuration editing only when explicitly enabled.
8. Conversational follow-ups such as `Ja` after an offered search/action.
9. Agent `/health` and `/models` endpoints.
10. Platform-dependent advice must respect detected capabilities and must not assume Home Assistant OS.

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

The agent, MCP server and Home Assistant integration are validated end-to-end with the test Home Assistant. GPT is active, Claude is available when `ANTHROPIC_API_KEY` is configured, Ollama is prepared as a future local provider, recent chat context is preserved, and platform-dependent Home Assistant capabilities are now detected dynamically instead of assuming Home Assistant OS.
