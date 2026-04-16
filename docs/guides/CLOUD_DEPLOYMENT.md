# Seshat Cloud Deployment Guide

> **Last updated**: 2026-04-16  
> **Target**: OVH VPS-3 (24 GB RAM, 8 vCPU, 160 GB SSD)  
> **Access**: Cloudflare WARP private network → `172.25.0.10`

This guide covers the complete Seshat cloud stack: infrastructure provisioning, Docker Compose services, reverse proxy configuration, Cloudflare tunnel, Terraform firewall, and deployment operations.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Prerequisites](#2-prerequisites)
3. [First-Time VPS Setup](#3-first-time-vps-setup)
4. [Terraform: OVH Network Firewall](#4-terraform-ovh-network-firewall)
5. [Service Stack: Docker Compose](#5-service-stack-docker-compose)
6. [Caddy Reverse Proxy](#6-caddy-reverse-proxy)
7. [Cloudflare WARP Tunnel](#7-cloudflare-warp-tunnel)
8. [Environment Variables](#8-environment-variables)
9. [Model Configuration](#9-model-configuration)
10. [Execution Profiles](#10-execution-profiles)
11. [Deployment Workflow](#11-deployment-workflow)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. Architecture Overview

```
Phone / Mac (WARP enrolled)
    │
    │ Cloudflare WARP private network (172.25.0.0/16)
    ▼
172.25.0.10  ←─ Caddy (Docker, static IP)
    │
    ├─ /api/*  /chat  /chat/stream  /stream/*
    │     └─→  seshat-gateway:9001   (FastAPI — full service/app.py)
    │
    └─ /*
          └─→  seshat-pwa:3000       (Next.js PWA, static IP 172.25.0.11)

seshat-gateway dependencies (all on cloud-sim bridge network):
  postgres:5432        (pgvector — sessions, history, metrics)
  neo4j:7687           (knowledge graph — memory)
  elasticsearch:9200   (traces, logs, telemetry)
  redis:6379           (event bus — Redis Streams)
  embeddings:8503      (Qwen3-Embedding-0.6B via llama.cpp)
  reranker:8504        (Qwen3-Reranker-0.6B via llama.cpp)

Network: cloud-sim bridge, subnet 172.25.0.0/16
OVH firewall: SSH (custom port), HTTP/80, HTTPS/443, ICMP only
Cloudflare: WARP Zero Trust + cloudflared tunnel (HTTP/2)
```

---

## 2. Prerequisites

### Mac (operator)

- SSH key registered on VPS: `~/.ssh/id_ed25519`
- SSH alias configured in `~/.ssh/config`:
  ```
  Host vps-5a0f676b
      HostName <VPS_IP>
      Port <SSH_PORT>
      User debian
      IdentityFile ~/.ssh/id_ed25519
  ```
- Docker Desktop or `docker` CLI (for local builds if needed)
- Terraform ≥ 1.9 (for firewall management)
- OVH API credentials (for Terraform)
- Cloudflare account with Zero Trust configured

### VPS (first time)

- Debian 12 (Bookworm)
- Docker Engine + Docker Compose v2 (`apt install docker-compose-plugin`)
- `uv` not required on VPS (installed inside containers)
- `/opt/seshat/` deployment directory
- `/opt/seshat/models/` — embedding + reranker GGUF files (see §3)

---

## 3. First-Time VPS Setup

### 3.1 Clone the repository

```bash
ssh vps-5a0f676b
sudo mkdir -p /opt/seshat && sudo chown debian:debian /opt/seshat
cd /opt/seshat
git clone https://github.com/alextra-lab/personal_agent.git .
```

### 3.2 Create the `.env` file

```bash
cp .env.example .env
nano .env
```

Required variables (see §8 for full list):
```dotenv
AGENT_ANTHROPIC_API_KEY=sk-ant-...
AGENT_OPENAI_API_KEY=sk-...
POSTGRES_PASSWORD=<strong-password>
NEO4J_PASSWORD=<strong-password>
CLOUDFLARE_TUNNEL_TOKEN=<token-from-cloudflare>
```

### 3.3 Transfer embedding and reranker models

From your Mac (requires ~1.2 GB):
```bash
bash infrastructure/scripts/transfer-models.sh
```

This copies GGUF files to `/opt/seshat/models/embedding/` and `/opt/seshat/models/reranker/` on the VPS.

### 3.4 Harden the server

```bash
# On VPS
bash infrastructure/scripts/harden.sh
```

Applies: non-root SSH only, fail2ban, sysctl hardening, unattended-upgrades.

### 3.5 First deploy

```bash
cd /opt/seshat
docker compose -f docker-compose.cloud.yml up -d
```

Initial startup takes ~5 minutes (Neo4j, Elasticsearch initialization, model loading).

---

## 4. Terraform: OVH Network Firewall

The OVH network-level firewall (stateless, applied before traffic reaches the OS) is managed by Terraform.

### Location

```
infrastructure/terraform/
├── main.tf         # Firewall resource + rules
├── providers.tf    # OVH provider, pinned to 1.8.0
├── variables.tf    # vps_ip, ssh_port, OVH API credentials
├── outputs.tf      # Firewall status + rule sequences
└── terraform.tfvars.example  # Copy to terraform.tfvars
```

### Rules (in order)

| Sequence | Protocol | Action | Description |
|----------|----------|--------|-------------|
| 0 | TCP | permit | Established connections (return traffic) |
| 1 | TCP | permit | SSH on custom port (non-standard) |
| 2 | TCP | permit | HTTP/80 (Caddy + Cloudflare tunnel) |
| 3 | TCP | permit | HTTPS/443 (Caddy TLS) |
| 4 | ICMP | permit | Ping / diagnostics |
| 19 | IPv4 | deny | Catch-all deny |

**Note**: The OVH firewall is stateless. Rule 0 (permit established) is required for outbound-initiated connections to receive return traffic (package downloads, API calls, etc.).

### Applying the firewall

```bash
cd infrastructure/terraform
cp terraform.tfvars.example terraform.tfvars
# Fill in: ovh_application_key, ovh_application_secret, ovh_consumer_key, vps_ip, ssh_port

terraform init
terraform plan
terraform apply
```

### Getting OVH API credentials

1. Go to https://eu.api.ovh.com/createApp
2. Create an application → get `application_key` and `application_secret`
3. Generate a consumer key with the required scopes:
   ```bash
   curl -XPOST -H "X-Ovh-Application: <app_key>" \
     -H "Content-type: application/json" \
     https://eu.api.ovh.com/1.0/auth/credential \
     -d '{"accessRules": [{"method": "GET", "path": "/ip/*"}, {"method": "POST", "path": "/ip/*"}, {"method": "PUT", "path": "/ip/*"}, {"method": "DELETE", "path": "/ip/*"}]}'
   ```
4. Visit the `validationUrl` to authorize, then use the returned `consumerKey`.

---

## 5. Service Stack: Docker Compose

File: `docker-compose.cloud.yml`

### Services

| Service | Image | Port (internal) | RAM limit | Purpose |
|---------|-------|-----------------|-----------|---------|
| `postgres` | pgvector/pgvector:pg17 | 5432 | 512 MB | Sessions, history, metrics |
| `neo4j` | neo4j:5.26-community | 7474/7687 | 1536 MB | Knowledge graph, memory |
| `elasticsearch` | elasticsearch:8.19 | 9200 | 2048 MB | Logs, traces, telemetry |
| `redis` | redis:7-alpine | 6379 | 128 MB | Event bus (Redis Streams) |
| `embeddings` | llmserver (local build) | 8503 | 2048 MB | Qwen3-Embedding-0.6B |
| `reranker` | llmserver (local build) | 8504 | 1024 MB | Qwen3-Reranker-0.6B |
| `seshat-gateway` | seshat-seshat-gateway | 9001 | 768 MB | Full service app (FastAPI) |
| `seshat-pwa` | seshat-seshat-pwa | 3000 | 256 MB | Next.js PWA |
| `caddy` | caddy:2-alpine | 80/443 | 64 MB | Reverse proxy |
| `cloudflared` | cloudflare/cloudflared | — | — | WARP tunnel |

**Total RAM budget**: ~8.4 GB (comfortably within 24 GB)

### Network

All services share the `cloud-sim` bridge network (`172.25.0.0/16`). Static IPs assigned only to Caddy (`172.25.0.10`) and the PWA (`172.25.0.11`) — these are the addresses WARP routes to.

Debug ports are bound to `127.0.0.1` only (SSH tunnel to access):
```bash
ssh -L 5432:localhost:5432 -L 9200:localhost:9200 vps-5a0f676b
```

### Startup order

```
postgres, neo4j, elasticsearch, redis (no deps)
  → embeddings, reranker (no deps)
    → seshat-gateway (depends_on: all above, condition: service_healthy)
      → seshat-pwa (depends_on: seshat-gateway)
        → caddy (depends_on: seshat-gateway + seshat-pwa)
          → cloudflared (depends_on: caddy)
```

Full cold-start: ~5 minutes. Warm restart (no image rebuild): ~90 seconds.

### Healthchecks

```bash
# Check all service states
docker compose -f docker-compose.cloud.yml ps

# Gateway health (full status)
curl http://localhost:9001/health

# Check logs
docker logs cloud-sim-seshat-gateway --tail 50
docker logs cloud-sim-caddy --tail 20
```

---

## 6. Caddy Reverse Proxy

File: `config/cloud-sim/Caddyfile`  
Container: `cloud-sim-caddy`  
Config path inside container: `/etc/caddy/Caddyfile` (bind-mounted read-only)

### Routing rules

```
@backend path /api/* /chat /chat/stream /stream/* /docs /docs/* /openapi.json /redoc
handle @backend {
    reverse_proxy seshat-gateway:9001
}
handle {
    reverse_proxy seshat-pwa:3000
}
```

**Important**: Caddy's `path` directive does exact matching for paths without wildcards. Adding a new backend endpoint requires updating the `@backend` matcher explicitly.

### Site blocks

- `localhost` — HTTPS with local self-signed cert (for SSH tunnel dev access)
- `http://172.25.0.10` — Plain HTTP (for WARP device access)

### Reloading Caddy config

**Problem**: `git pull` replaces the Caddyfile inode on the host. Docker bind mounts track inodes, so the container sees the old file until restarted.

**Correct procedure**:
```bash
cd /opt/seshat
git pull
docker compose -f docker-compose.cloud.yml restart caddy
```

Do **not** use `caddy reload` after a git pull — it reads the stale inode.

---

## 7. Cloudflare WARP Tunnel

Seshat uses Cloudflare Zero Trust + WARP for private network access. WARP-enrolled devices route `172.25.0.0/16` through a Cloudflare tunnel to the VPS Docker network.

### Architecture

```
WARP device
  → Cloudflare Zero Trust (routes 172.25.0.0/16)
    → cloudflared (Docker container on VPS)
      → Docker bridge network (cloud-sim)
        → Caddy at 172.25.0.10
```

### Setup

1. **Cloudflare Zero Trust dashboard** → Settings → WARP Client → Device settings
2. Create a Split Tunnel with `172.25.0.0/16` in "include" mode
3. Create a Cloudflare Tunnel:
   - Tunnels → Create tunnel → Docker
   - Copy the `cloudflared` run command token
   - Add to `.env` as `CLOUDFLARE_TUNNEL_TOKEN=<token>`

4. **OVH firewall note**: QUIC (UDP port 443) is blocked by OVH's datacenter firewall. Force HTTP/2 in the cloudflared command:
   ```yaml
   command: tunnel --no-autoupdate --protocol http2 run
   ```

### Enrolling a device

1. Install Cloudflare WARP app (macOS/iOS/Android)
2. Settings → Account → Login with Zero Trust org
3. Once enrolled, `172.25.0.10` is reachable

---

## 8. Environment Variables

The `.env` file lives at `/opt/seshat/.env` on the VPS (gitignored, never committed).

```dotenv
# ── PostgreSQL ────────────────────────────────────────────────────────────────
POSTGRES_PASSWORD=<strong-random-password>

# ── Neo4j ─────────────────────────────────────────────────────────────────────
NEO4J_PASSWORD=<strong-random-password>

# ── Cloud LLM APIs ────────────────────────────────────────────────────────────
AGENT_ANTHROPIC_API_KEY=sk-ant-api03-...
AGENT_OPENAI_API_KEY=sk-...
AGENT_PERPLEXITY_API_KEY=pplx-...   # optional

# ── Cloudflare Tunnel ─────────────────────────────────────────────────────────
CLOUDFLARE_TUNNEL_TOKEN=eyJ...

# ── OVH Terraform (local only, NOT on VPS) ────────────────────────────────────
# ovh_application_key = "..."
# ovh_application_secret = "..."
# ovh_consumer_key = "..."
```

**API key naming**: The service uses Pydantic settings with `env_prefix="AGENT_"`. LiteLLM expects bare env vars (`ANTHROPIC_API_KEY`). The `LiteLLMClient` resolves keys from settings and passes them explicitly — no bare env vars needed.

---

## 9. Model Configuration

The VPS uses `config/models.cloud.yaml` (set via `AGENT_MODEL_CONFIG_PATH` in docker-compose):

```yaml
# Cloud primary + sub-agent models
claude_sonnet:
  id: "claude-sonnet-4-6"
  provider: "anthropic"
  provider_type: "cloud"
  max_tokens: 8192

claude_haiku:
  id: "claude-haiku-4-5-20251001"
  provider: "anthropic"
  provider_type: "cloud"
  max_tokens: 4096

# Background task models (entity extraction, Captain's Log)
gpt-5.4-nano:
  id: "gpt-5.4-nano"
  provider: "openai"
  provider_type: "cloud"
  max_tokens: 4096

# Local embedding + reranker (Docker service hostnames)
embedding:
  id: "Qwen/Qwen3-Embedding-0.6B"
  provider_type: "local"
  endpoint: "http://embeddings:8503/v1"

reranker:
  id: "ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF"
  provider_type: "local"
  endpoint: "http://reranker:8504/v1"
```

---

## 10. Execution Profiles

Profiles live in `config/profiles/`. The active profile is set per-conversation in the PWA.

### `config/profiles/cloud.yaml`

```yaml
name: cloud
description: "Cloud inference via LiteLLM (Claude Sonnet + Haiku)"
primary_model: claude_sonnet      # maps to models.yaml key
sub_agent_model: claude_haiku
provider_type: cloud
cost_limit_per_session: 2.00
delegation:
  allow_cloud_escalation: true
  escalation_provider: anthropic
  escalation_model: claude_sonnet
```

### Profile dispatch flow

```
PWA: POST /chat/stream  profile=cloud
  → background task: load_profile("cloud") → set_current_profile(profile)
    → orchestrator calls get_llm_client("primary")
      → factory: profile.primary_model = "claude_sonnet"
      → models["claude_sonnet"].provider_type = "cloud"
      → return LiteLLMClient(model_id="claude-sonnet-4-6", provider="anthropic")
        → litellm.acompletion(model="anthropic/claude-sonnet-4-6", api_key=...)
```

### Adding a new profile

1. Create `config/profiles/<name>.yaml` with the required fields
2. Ensure referenced model keys exist in `config/models.yaml` AND `config/models.cloud.yaml`
3. Add the profile ID to the PWA's profile selector in `StreamingChat.tsx`
4. Rebuild PWA container on VPS

---

## 11. Deployment Workflow

### Code-only change (no new deps)

```bash
# From Mac:
git push origin main
bash infrastructure/scripts/deploy.sh --build
```

`deploy.sh --build` does: `git pull` + rebuild `seshat-gateway` + `docker compose up -d`.

### PWA change

```bash
git push origin main
bash infrastructure/scripts/deploy.sh --build
# deploy.sh rebuilds seshat-gateway; for PWA changes, rebuild that too:
ssh vps-5a0f676b "cd /opt/seshat && docker compose -f docker-compose.cloud.yml up --build seshat-pwa -d"
```

### Dependency change (`pyproject.toml` / `uv.lock`)

```bash
git push origin main
bash infrastructure/scripts/deploy.sh --full
```

### Caddyfile change

```bash
git push origin main
ssh vps-5a0f676b "cd /opt/seshat && git pull && docker compose -f docker-compose.cloud.yml restart caddy"
```

### Rollback

```bash
ssh vps-5a0f676b "cd /opt/seshat && git checkout <previous-commit>"
bash infrastructure/scripts/deploy.sh --build
```

---

## 12. Troubleshooting

### Gateway returns 404 on /chat/stream

Check Caddyfile has `/chat/stream` in the `@backend` path matcher. After editing:
```bash
docker compose -f docker-compose.cloud.yml restart caddy
```
Do not use `caddy reload` — git pull invalidates the bind-mount inode.

### Gateway returns "An error occurred while processing your request"

Check orchestrator logs:
```bash
docker logs cloud-sim-seshat-gateway --tail 100 2>&1 | grep "error"
```

Common causes:
- **AuthenticationError**: LiteLLM can't find API key → check `AGENT_ANTHROPIC_API_KEY` in `.env`
- **FileNotFoundError on profile**: `config/profiles/cloud.yaml` not found → check `config/` is in the Docker image
- **DB connection error**: PostgreSQL not ready → `docker compose ps` to check health

### PWA shows crypto.randomUUID SecurityError (Safari)

This is fixed by `seshat-pwa/src/lib/uuid.ts` polyfill. If you see this, the PWA container has old code — rebuild:
```bash
ssh vps-5a0f676b "cd /opt/seshat && docker compose -f docker-compose.cloud.yml up --build seshat-pwa -d"
```

### Cloudflare tunnel not connecting (QUIC errors)

OVH blocks UDP/443. Ensure `cloudflared` command has `--protocol http2`:
```yaml
command: tunnel --no-autoupdate --protocol http2 run
```

### Embeddings / reranker container won't start

GGUF model files missing. Run:
```bash
bash infrastructure/scripts/transfer-models.sh
```

### Services won't start after VPS reboot

Docker services set `restart: unless-stopped` and should auto-restart. If they don't:
```bash
ssh vps-5a0f676b "cd /opt/seshat && docker compose -f docker-compose.cloud.yml up -d"
```

### Port conflicts

All debug ports are bound to `127.0.0.1` only. To access PostgreSQL locally:
```bash
ssh -L 5432:localhost:5432 vps-5a0f676b
# Then connect to localhost:5432
```

### Neo4j vector index missing

On first deploy the vector index may need explicit initialization:
```bash
ssh vps-5a0f676b "curl -s http://localhost:9001/health | python3 -m json.tool"
# neo4j: "connected" means the service started but index creation runs in lifespan
# Check logs for "neo4j_vector_index_ensured"
```
