# Seshat Cloud Deployment Guide

> **Last updated**: 2026-08-18 (FRE-1244 — retired the private-network IP route)  
> **Target**: your VPS  
> **Access**: Cloudflare Tunnel hostname ingress (`{$AGENT_HOST}`) — no container is addressed by IP

This guide covers the complete Seshat cloud stack: infrastructure provisioning, Docker Compose services, reverse proxy configuration, Cloudflare tunnel, Terraform firewall, and deployment operations.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Prerequisites](#2-prerequisites)
3. [First-Time VPS Setup](#3-first-time-vps-setup)
4. [Terraform: OVH Network Firewall](#4-terraform-ovh-network-firewall)
5. [Service Stack: Docker Compose](#5-service-stack-docker-compose)
6. [Caddy Reverse Proxy](#6-caddy-reverse-proxy)
7. [Cloudflare Tunnel Access](#7-cloudflare-tunnel-access)
8. [Environment Variables](#8-environment-variables)
9. [Model Configuration](#9-model-configuration)
10. [Execution Profiles](#10-execution-profiles)
11. [Deployment Workflow](#11-deployment-workflow)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. Architecture Overview

```
Phone / Mac / browser (WARP enrolled or not — both reach the same hostname)
    │
    │ Cloudflare Tunnel, Host: {$AGENT_HOST}
    ▼
Caddy (Docker, no fixed address — reached by hostname, not IP)
    │
    ├─ /api/*  /chat  /chat/stream  /stream/*
    │     └─→  seshat-gateway:9001   (FastAPI — full service/app.py)
    │
    └─ /*
          └─→  seshat-pwa:3000       (Next.js PWA, no fixed address)

seshat-gateway dependencies (all on cloud-sim bridge network):
  postgres:5432        (pgvector — sessions, history, metrics)
  neo4j:7687           (knowledge graph — memory)
  elasticsearch:9200   (traces, logs, telemetry)
  redis:6379           (event bus — Redis Streams)

seshat-gateway external dependencies (managed endpoints):
  OVH AI Endpoints     (Qwen3-Embedding-8B — semantic search)
  Voyage AI            (rerank-2.5 — ranked retrieval)
  Anthropic/OpenAI     (Claude Sonnet/Haiku — cloud profiles)

Network: cloud-sim bridge, subnet 172.25.0.0/16 (address space only — no service is pinned to a
fixed address within it; see FRE-1244)
OVH firewall: SSH (custom port), HTTP/80, HTTPS/443, ICMP only
Cloudflare: cloudflared tunnel (HTTP/2), public hostname ingress
```

---

## 2. Prerequisites

### Mac (operator)

- SSH key registered on VPS: `~/.ssh/id_ed25519`
- SSH alias configured in `~/.ssh/config`:
  ```
  Host <your-vps-ssh-alias>
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

---

## 3. First-Time VPS Setup

### 3.1 Clone the repository

```bash
ssh <your-vps-ssh-alias>
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

### 3.3 Harden the server

```bash
# On VPS
bash infrastructure/scripts/harden.sh
```

Applies: non-root SSH only, fail2ban, sysctl hardening, unattended-upgrades.

### 3.4 First deploy

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

**Live (started automatically)**

| Service | Image | Port (internal) | RAM limit | Purpose |
|---------|-------|-----------------|-----------|---------|
| `postgres` | pgvector/pgvector:pg17 | 5432 | 512 MB | Sessions, history, metrics |
| `neo4j` | neo4j:5.26-community | 7474/7687 | 1536 MB | Knowledge graph, memory |
| `elasticsearch` | elasticsearch:8.19 | 9200 | 2048 MB | Logs, traces, telemetry |
| `redis` | redis:7-alpine | 6379 | 128 MB | Event bus (Redis Streams) |
| `seshat-gateway` | seshat-seshat-gateway | 9001 | 768 MB | Full service app (FastAPI) |
| `seshat-pwa` | seshat-seshat-pwa | 3000 | 256 MB | Next.js PWA |
| `caddy` | caddy:2-alpine | 80/443 | 64 MB | Reverse proxy |
| `cloudflared` | cloudflare/cloudflared | — | — | Cloudflare Tunnel |

**Total RAM budget**: ~5.3 GB (comfortably within 24 GB)

### Network

All services share the `cloud-sim` bridge network (`172.25.0.0/16`) and reach each other by service
name via Docker's internal DNS. No service declares a fixed address (FRE-1244 — a static-holder being
squatted by a free-floating service on recreation made Caddy fail to start, taking external ingress
down for the duration).

Debug ports are bound to `127.0.0.1` only (SSH tunnel to access):
```bash
ssh -L 5432:localhost:5432 -L 9200:localhost:9200 <your-vps-ssh-alias>
```

### Startup order

```
postgres, neo4j, elasticsearch, redis (no deps)
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

### Reloading Caddy config

**Problem**: `git pull` replaces the Caddyfile inode on the host. Docker bind mounts track inodes, so the container sees the old file until restarted.

**Correct procedure**:
```bash
cd /opt/seshat
git pull
docker compose -f docker-compose.cloud.yml restart caddy
```

Do **not** use `caddy reload` after a git pull — it reads the stale inode.

### Access-log shipping (Filebeat)

Caddy writes its JSON access log to a fixed path (`/var/log/caddy/access.log`), in a Docker
named volume (`caddy_logs_cloud`) shared read-only with the `filebeat` sidecar, which ships it to
`caddy-access-*` in Elasticsearch (ADR-0132 D3). Recreating Caddy — a `SERVICE=caddy` rebuild, a
`--force-recreate`, anything — never requires any companion Filebeat action; the path Filebeat
tails never changes (FRE-1243). For debugging, `docker exec cloud-sim-caddy tail -f
/var/log/caddy/access.log` replaces `docker logs cloud-sim-caddy` for access-log visibility
specifically — Caddy's other runtime logs (startup, TLS, errors) are still on stdout, only the
access log moved.

**One-time migration note (only relevant to the deploy that first lands FRE-1243):** a plain
`make deploy` (pull + restart, no image rebuild) recreates `filebeat`'s container because its
compose-level volume list changed, but reuses whatever `cloud-sim-filebeat` image is already
built — if that's the pre-FRE-1243 image, its entrypoint still expects the now-removed
`/var/lib/docker/containers` mount and will crash-loop. That first deploy must rebuild `filebeat`
before recreating it: `docker compose -f docker-compose.cloud.yml build filebeat && docker
compose -f docker-compose.cloud.yml up -d --force-recreate caddy filebeat` (or `make
build-full`) — not a plain `make deploy`. Every deploy after that first one is back to the normal
no-coordination-needed behavior described above.

---

## 7. Cloudflare Tunnel Access

**Retired 2026-08-18 (FRE-1244)**: Seshat previously used a Cloudflare Zero Trust WARP split-tunnel
route (`172.25.0.0/16` in "include" mode) so WARP-enrolled devices could reach Caddy directly by its
Docker-assigned IP. That mechanism required Caddy to hold a stable, known-in-advance address — the same
requirement that let a free-floating service squat the address whenever Caddy was recreated while
stopped, taking external ingress down. The private-IP route has been removed; no container in
`cloud-sim` is addressed by IP any more (see the Network section above).

Phone/Mac access now goes through the same Cloudflare Tunnel hostname ingress (`{$AGENT_HOST}`) as any
other client — a publicly tunneled hostname needs no private-network CIDR routing to be reachable from a
WARP-enrolled device.

**Manual follow-up, not tracked as code in this repo**: if the Cloudflare Zero Trust dashboard still has
a Split Tunnel route for `172.25.0.0/16` (Settings → WARP Client → Device settings), it is now vestigial
and should be removed by whoever owns Cloudflare Access — this repo has no Terraform for that setting.

### Setup

1. Create a Cloudflare Tunnel:
   - Tunnels → Create tunnel → Docker
   - Copy the `cloudflared` run command token
   - Add to `.env` as `CLOUDFLARE_TUNNEL_TOKEN=<token>`

2. **OVH firewall note**: QUIC (UDP port 443) is blocked by OVH's datacenter firewall. Force HTTP/2 in the cloudflared command:
   ```yaml
   command: tunnel --no-autoupdate --protocol http2 run
   ```

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

Every deployment reads the same catalog, `config/models.yaml`. The single source of truth for all model assignments (ADR-0121). Each role resolves identically on the VPS and locally.

What each compose file declares is `AGENT_DEPLOYMENT_PROFILE` (`local` | `cloud` | `eval`), which keys the required-secret set in `config/model_roles.yaml`.

### Layer 1 — Providers (deployment endpoints and auth)

| Provider | Base URL | Purpose | Auth |
|----------|----------|---------|------|
| `slm_local` | https://slm.example.com/v1 | Owner's Mac GPU via CF-Access tunnel (llama.cpp + MLX) | none |
| `ovh` | https://oai.endpoints.kepler.ai.cloud.ovh.net/v1 | OVH AI Endpoints — managed embedding inference | `managed_embedding_token` |
| `voyage` | https://api.voyageai.com/v1 | Voyage AI — managed reranking service | `voyage_api_key` |
| `anthropic` | (cloud) | Anthropic Claude models | `anthropic_api_key` |
| `openai` | (cloud) | OpenAI models for extraction/compression | `openai_api_key` |

### Layer 2 — Live model assignments (embeddings and reranker)

The gateway resolves embeddings and reranker through `config/models.yaml`, not through local Docker containers:

**Embedding (semantic search):**
```yaml
embedding:
  kind: embedding
  provider: ovh                                    # Managed OVH endpoint
  id: "Qwen3-Embedding-8B"                         # Model ID on OVH
  endpoint: "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1"
  context_length: 32768
  max_concurrency: 50
  input_cost_per_token_eur: 0.0000001              # €0.10/MTok
```

**Reranker (ranked retrieval):**
```yaml
reranker:
  kind: reranker
  provider: voyage                                 # Managed Voyage endpoint
  id: "rerank-2.5"                                 # Model ID at Voyage
  endpoint: "https://api.voyageai.com/v1"
  context_length: 32000
  max_concurrency: 5
  input_cost_per_token: 0.00000005                 # $0.05/MTok
```

**Reranker fallback** (if Voyage is unavailable):
```yaml
reranker_fallback:
  kind: reranker
  provider: slm_local                              # Mac tunnel fallback
  id: "Qwen/Qwen3-Reranker-4B-mxfp8"               # Model ID on Mac
  endpoint: "https://slm.example.com/v1"
  max_concurrency: 1
```

There is no local-Docker-container path for embedding or reranking (FRE-1166 retired the
0.6B llama.cpp provisioning chain — it predated the OVH/Voyage cutover and nothing calls it).

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
2. Ensure referenced model keys exist in `config/models.yaml` (the single catalog)
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
ssh <your-vps-ssh-alias> "cd /opt/seshat && docker compose -f docker-compose.cloud.yml up --build seshat-pwa -d"
```

### Dependency change (`pyproject.toml` / `uv.lock`)

```bash
git push origin main
bash infrastructure/scripts/deploy.sh --full
```

### Caddyfile change

```bash
git push origin main
ssh <your-vps-ssh-alias> "cd /opt/seshat && git pull && docker compose -f docker-compose.cloud.yml restart caddy"
```

### Rollback

```bash
ssh <your-vps-ssh-alias> "cd /opt/seshat && git checkout <previous-commit>"
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
ssh <your-vps-ssh-alias> "cd /opt/seshat && docker compose -f docker-compose.cloud.yml up --build seshat-pwa -d"
```

### Cloudflare tunnel not connecting (QUIC errors)

OVH blocks UDP/443. Ensure `cloudflared` command has `--protocol http2`:
```yaml
command: tunnel --no-autoupdate --protocol http2 run
```

### Services won't start after VPS reboot

Docker services set `restart: unless-stopped` and should auto-restart. If they don't:
```bash
ssh <your-vps-ssh-alias> "cd /opt/seshat && docker compose -f docker-compose.cloud.yml up -d"
```

### Port conflicts

All debug ports are bound to `127.0.0.1` only. To access PostgreSQL locally:
```bash
ssh -L 5432:localhost:5432 <your-vps-ssh-alias>
# Then connect to localhost:5432
```

### Neo4j vector index missing

On first deploy the vector index may need explicit initialization:
```bash
ssh <your-vps-ssh-alias> "curl -s http://localhost:9001/health | python3 -m json.tool"
# neo4j: "connected" means the service started but index creation runs in lifespan
# Check logs for "neo4j_vector_index_ensured"
```
