# Cloudflare Tunnel + Terraform Design

**Date:** 2026-04-16  
**Domain:** frenchforet.com  
**Status:** Approved — ready for implementation planning

---

## Goal

Expose the Seshat personal agent publicly at `frenchforet.com` subdomains without requiring the Cloudflare WARP/One client on any device. Manage the entire Cloudflare configuration as code via Terraform. Enable authorized external agents to call the Seshat API.

---

## Traffic Flow

```
Browser / External Agent
        ↓ HTTPS (TLS terminated at Cloudflare edge)
Cloudflare (frenchforet.com zone)
  ├── agent.frenchforet.com  ─── proxied CNAME ──→ <tunnel-id>.cfargotunnel.com
  └── api.frenchforet.com    ─── proxied CNAME ──→ <tunnel-id>.cfargotunnel.com
        ↓ HTTP/2 over TCP (QUIC blocked by OVH firewall)
cloudflared container (cloud-sim Docker network)
        ↓ HTTP (internal Docker DNS)
Caddy reverse proxy (172.25.0.10:80)
  ├── agent.frenchforet.com → /api/* /chat /stream/* → seshat-gateway:9001
  │                         → /*                     → seshat-pwa:3000
  └── api.frenchforet.com   → seshat-gateway:9001 (all paths)
```

**Key design choice — same-origin for the PWA:**  
`agent.frenchforet.com` uses the existing Caddyfile routing snippet (path-based). The PWA makes API calls to its own domain — no CORS required. `NEXT_PUBLIC_SESHAT_URL` becomes `https://agent.frenchforet.com`.

`api.frenchforet.com` is a dedicated endpoint for external agents only. All traffic routes directly to the gateway.

---

## Authentication

Two-layer defence:

**Layer 1 — Cloudflare WAF (edge):**  
A custom WAF ruleset blocks any request to `api.frenchforet.com` missing an `Authorization` header before it reaches the VPS. Requests to `agent.frenchforet.com` are not blocked at the WAF layer (browser users don't send auth headers).

**Layer 2 — Gateway (origin):**  
`AGENT_GATEWAY_AUTH_ENABLED=true` + `AGENT_GATEWAY_API_KEY=<secret>`. The gateway validates `Authorization: Bearer <key>` on all non-public endpoints. This catches anything that bypasses the WAF and enforces auth uniformly regardless of how the gateway is reached.

External agents include `Authorization: Bearer <key>` in every request to `api.frenchforet.com`.

---

## Terraform Module

### Location

```
infrastructure/
├── terraform/                          # existing — OVH firewall (unchanged)
└── terraform-cloudflare/               # new — Cloudflare DNS + tunnel + WAF
```

Independent root module: own provider, state, and variables. Applied separately from the OVH module.

### File Structure

```
infrastructure/terraform-cloudflare/
├── .gitignore                   # excludes *.tfvars, *.tfstate, .terraform/
├── providers.tf                 # cloudflare/cloudflare ~5.x
├── variables.tf                 # api_token, zone_id, account_id, tunnel_name, domain
├── tunnel.tf                    # tunnel resource + ingress config
├── dns.tf                       # 2× cloudflare_record (agent + api, proxied CNAMEs)
├── waf.tf                       # WAF custom ruleset — block api.* without Authorization
├── outputs.tf                   # tunnel_id, tunnel_token (sensitive)
├── terraform.tfvars.example     # safe to commit — template only
└── terraform.tfvars             # gitignored — actual secrets
```

### Resources

| File | Terraform Resource | Purpose |
|---|---|---|
| `tunnel.tf` | `cloudflare_zero_trust_tunnel_cloudflared` | Creates named tunnel |
| `tunnel.tf` | `cloudflare_zero_trust_tunnel_cloudflared_config` | Ingress routing: `agent.*` → `http://caddy:80`, `api.*` → `http://caddy:80`, catch-all → `http_status:404` |
| `dns.tf` | `cloudflare_record` × 2 | Proxied CNAMEs: `agent` and `api` → `<tunnel-id>.cfargotunnel.com` |
| `waf.tf` | `cloudflare_ruleset` | Custom WAF rule: block `api.frenchforet.com` requests missing `Authorization` |
| `outputs.tf` | `output "tunnel_token"` | Sensitive — used to populate VPS credentials file |

### Variables (`terraform.tfvars.example`)

```hcl
cloudflare_api_token = "YOUR_API_TOKEN"
cloudflare_zone_id   = "YOUR_ZONE_ID"
cloudflare_account_id = "YOUR_ACCOUNT_ID"
tunnel_name          = "seshat-vps"
domain               = "frenchforet.com"
```

### Credentials Flow

```
terraform apply
    ↓
terraform output -raw tunnel_token   (sensitive)
    ↓
CLOUDFLARE_TUNNEL_TOKEN in VPS .env  (set by deploy step)
    ↓
cloudflared container reads TUNNEL_TOKEN env var (unchanged)
```

When using `config_src = "cloudflare"`, cloudflared fetches ingress routing from Cloudflare's API at runtime — no local config file needed. The `TUNNEL_TOKEN` env var approach in docker-compose is unchanged; the token now comes from `terraform output` instead of being manually created in the Cloudflare dashboard. The deploy script (`infrastructure/scripts/deploy.sh`) is updated to write this value into the VPS `.env` before starting docker-compose.

---

## Changes to Existing Files

### `config/cloud-sim/Caddyfile`

Add two new site blocks (plain HTTP — TLS is Cloudflare's responsibility):

```caddyfile
# Cloudflare Tunnel — user-facing PWA (same-origin API routing)
http://agent.frenchforet.com {
    import routing
}

# Cloudflare Tunnel — external agent API access
http://api.frenchforet.com {
    reverse_proxy seshat-gateway:9001 {
        header_up X-Forwarded-For {http.request.header.CF-Connecting-IP}
        header_up X-Forwarded-Proto https
        header_up X-Forwarded-Host {http.request.header.Host}
        transport http {
            dial_timeout 30s
            response_header_timeout 60s
        }
    }
    log {
        output stdout
        format json
    }
    handle_errors {
        @4xx expression `{http.error.status_code} >= 400 && {http.error.status_code} < 500`
        @5xx expression `{http.error.status_code} >= 500`
        respond @4xx "{http.error.status_code} {http.error.status_text}" {http.error.status_code}
        respond @5xx "Internal Server Error" 500
    }
}
```

### `docker-compose.cloud.yml` — cloudflared service

No structural change needed. The service already uses `TUNNEL_TOKEN` from `.env`. The only change is that the token value now comes from `terraform output -raw tunnel_token` rather than being manually configured in the Cloudflare dashboard:

```yaml
cloudflared:
  image: cloudflare/cloudflared:latest
  container_name: cloud-sim-cloudflared
  command: tunnel --no-autoupdate --protocol http2 run   # unchanged
  environment:
    TUNNEL_TOKEN: ${CLOUDFLARE_TUNNEL_TOKEN}             # populated from terraform output
  networks:
    - cloud-sim
  restart: unless-stopped
  depends_on:
    caddy:
      condition: service_healthy
```

### `docker-compose.cloud.yml` — seshat-pwa build arg

```yaml
seshat-pwa:
  build:
    args:
      NEXT_PUBLIC_SESHAT_URL: "https://agent.frenchforet.com"
```

### `docker-compose.cloud.yml` — seshat-gateway env

```yaml
seshat-gateway:
  environment:
    AGENT_GATEWAY_AUTH_ENABLED: "true"
    AGENT_GATEWAY_API_KEY: ${AGENT_GATEWAY_API_KEY}   # added to VPS .env
```

---

## Security Notes

- `.tf` files contain no secrets — safe in a public repo
- `terraform.tfvars` is gitignored — secrets never committed
- Terraform state is gitignored locally — contains `tunnel_token` in plaintext; if remote state is added later, use an encrypted backend (Cloudflare R2 + SSE, or Terraform Cloud)
- The OVH firewall (ports 80 + 443) remains open — required for Cloudflare's edge to reach Caddy. The `cloudflared` tunnel itself operates outbound only; no new inbound ports are needed for the tunnel daemon.
- WAF rule + gateway auth provide defence-in-depth for the API subdomain

---

## Out of Scope

- Remote Terraform state backend (future work if needed)
- Cloudflare Access policies / Zero Trust (not needed — WAF + gateway auth is sufficient)
- OVH firewall changes (no new ports needed)
- Gateway auth middleware implementation detail (verify `AGENT_GATEWAY_AUTH_ENABLED` behaviour during implementation; implement key-validation middleware if not already present)
