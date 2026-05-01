# Cloudflare Tunnel + Terraform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose Seshat publicly at `agent.frenchforet.com` (PWA) and `api.frenchforet.com` (external agents) via a Terraform-managed Cloudflare Tunnel, with a WAF rule blocking unauthenticated requests to the API subdomain.

**Architecture:** A new `infrastructure/terraform-cloudflare/` Terraform root module creates a named Cloudflare Tunnel (`config_src = "cloudflare"`), two proxied CNAME DNS records, and a WAF custom ruleset. Caddy gets two new HTTP site blocks (one per subdomain). The docker-compose PWA build arg is updated to the public URL. Gateway auth is **out of scope** for this plan (the PWA's `EventSource` SSE connection cannot send custom headers; enabling global gateway auth would break the PWA — track separately).

**Tech Stack:** Cloudflare Terraform Provider v5, HCL 1.9+, Caddy 2, Docker Compose, Bash

**Security note:** The WAF rule blocks `api.frenchforet.com` requests missing an `Authorization` header at the Cloudflare edge. Since `AGENT_GATEWAY_AUTH_ENABLED` stays `false`, the WAF is the primary API defence. The gateway's token-validation middleware is already fully implemented in `src/personal_agent/gateway/auth.py` and `config/gateway_access.yaml` — enabling it requires a separate PWA auth task (add Bearer token to all fetch calls and solve EventSource auth).

---

## Pre-flight Checklist

Before starting, confirm you have these values locally:

- [ ] `CLOUDFLARE_API_TOKEN` — Cloudflare API token (Zone:Edit + DNS:Edit + Account:Cloudflare Tunnel:Edit)
- [ ] `CLOUDFLARE_ZONE_ID` — Zone ID for frenchforet.com (from Cloudflare dashboard → Overview → right sidebar)
- [ ] `CLOUDFLARE_ACCOUNT_ID` — Account ID (same location)
- [ ] Terraform >= 1.9 installed (`terraform version`)
- [ ] Docker available locally (for Caddyfile validation)

---

## File Map

**New files:**

| File | Purpose |
|---|---|
| `infrastructure/terraform-cloudflare/.gitignore` | Excludes tfvars/state/provider cache |
| `infrastructure/terraform-cloudflare/providers.tf` | Cloudflare provider ~5.x |
| `infrastructure/terraform-cloudflare/variables.tf` | Input variable declarations |
| `infrastructure/terraform-cloudflare/tunnel.tf` | Named tunnel + ingress config |
| `infrastructure/terraform-cloudflare/dns.tf` | agent + api CNAME records |
| `infrastructure/terraform-cloudflare/waf.tf` | WAF ruleset: block api.* without Authorization |
| `infrastructure/terraform-cloudflare/outputs.tf` | tunnel_id, tunnel_token |
| `infrastructure/terraform-cloudflare/terraform.tfvars.example` | Template — safe to commit |
| `infrastructure/terraform-cloudflare/terraform.tfvars` | Real secrets — gitignored |

**Modified files:**

| File | Change |
|---|---|
| `config/cloud-sim/Caddyfile` | Add 2 HTTP site blocks (agent.* and api.*) |
| `config/gateway_access.yaml` | Add external-agent token entry (prep — unused until gateway auth enabled) |
| `docker-compose.cloud.yml` | Update PWA build arg to `https://agent.frenchforet.com` |

---

## Task 1: Bootstrap the terraform-cloudflare module

**Files:**
- Create: `infrastructure/terraform-cloudflare/.gitignore`
- Create: `infrastructure/terraform-cloudflare/providers.tf`
- Create: `infrastructure/terraform-cloudflare/variables.tf`
- Create: `infrastructure/terraform-cloudflare/terraform.tfvars.example`

- [ ] **Step 1: Create the directory and .gitignore**

```bash
mkdir -p infrastructure/terraform-cloudflare
```

Create `infrastructure/terraform-cloudflare/.gitignore`:
```gitignore
# Terraform state — never commit
*.tfstate
*.tfstate.*
*.tfstate.backup

# Terraform working directory
.terraform/
.terraform.lock.hcl

# Secret variable files — never commit
*.tfvars
!*.tfvars.example

# Crash logs
crash.log
crash.*.log

# Plan output files (may contain sensitive data)
*.tfplan

# Override files
override.tf
override.tf.json
*_override.tf
*_override.tf.json
```

- [ ] **Step 2: Create providers.tf**

Create `infrastructure/terraform-cloudflare/providers.tf`:
```hcl
terraform {
  required_version = ">= 1.9"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.0"
    }
  }
}

provider "cloudflare" {
  api_token = var.cloudflare_api_token
}
```

- [ ] **Step 3: Create variables.tf**

Create `infrastructure/terraform-cloudflare/variables.tf`:
```hcl
variable "cloudflare_api_token" {
  description = "Cloudflare API token with Zone:Edit, DNS:Edit, Cloudflare Tunnel:Edit permissions"
  type        = string
  sensitive   = true
}

variable "cloudflare_zone_id" {
  description = "Cloudflare Zone ID for the target domain"
  type        = string
  sensitive   = true
}

variable "cloudflare_account_id" {
  description = "Cloudflare Account ID"
  type        = string
  sensitive   = true
}

variable "tunnel_name" {
  description = "Human-readable name for the Cloudflare Tunnel"
  type        = string
  default     = "seshat-vps"
}

variable "domain" {
  description = "Root domain managed in this Cloudflare zone"
  type        = string
  default     = "frenchforet.com"
}
```

- [ ] **Step 4: Create terraform.tfvars.example**

Create `infrastructure/terraform-cloudflare/terraform.tfvars.example`:
```hcl
# Copy this file to terraform.tfvars and fill in real values.
# terraform.tfvars is gitignored — never commit real secrets.

cloudflare_api_token  = "YOUR_CLOUDFLARE_API_TOKEN"
cloudflare_zone_id    = "YOUR_ZONE_ID"
cloudflare_account_id = "YOUR_ACCOUNT_ID"
tunnel_name           = "seshat-vps"
domain                = "frenchforet.com"
```

- [ ] **Step 5: Create terraform.tfvars with real values**

Create `infrastructure/terraform-cloudflare/terraform.tfvars` (this file is gitignored):
```hcl
cloudflare_api_token  = "<your-api-token>"
cloudflare_zone_id    = "<your-zone-id>"
cloudflare_account_id = "<your-account-id>"
tunnel_name           = "seshat-vps"
domain                = "frenchforet.com"
```

- [ ] **Step 6: Run terraform init**

```bash
cd infrastructure/terraform-cloudflare
terraform init
```

Expected output: `Terraform has been successfully initialized!`

- [ ] **Step 7: Commit**

```bash
cd ../..
git add infrastructure/terraform-cloudflare/.gitignore \
        infrastructure/terraform-cloudflare/providers.tf \
        infrastructure/terraform-cloudflare/variables.tf \
        infrastructure/terraform-cloudflare/terraform.tfvars.example
git commit -m "feat(infra): bootstrap terraform-cloudflare module with provider and variables"
```

---

## Task 2: Define the named Cloudflare Tunnel

**Files:**
- Create: `infrastructure/terraform-cloudflare/tunnel.tf`

- [ ] **Step 1: Create tunnel.tf**

Create `infrastructure/terraform-cloudflare/tunnel.tf`:
```hcl
# Named Cloudflare Tunnel — remotely managed config (ingress rules via API)
#
# config_src = "cloudflare" means ingress routing is managed via the
# cloudflare_zero_trust_tunnel_cloudflared_config resource below, not a
# local config.yml file. cloudflared fetches config from Cloudflare's API
# at startup using the tunnel_token.
resource "cloudflare_zero_trust_tunnel_cloudflared" "seshat" {
  account_id = var.cloudflare_account_id
  name       = var.tunnel_name
  config_src = "cloudflare"
}

# Ingress routing — maps public hostnames to internal Docker services.
#
# cloudflared is on the cloud-sim Docker network and resolves service
# hostnames via Docker's internal DNS. Traffic arrives at Caddy with the
# original Host header preserved; Caddy matches the appropriate site block.
#
# Catch-all rule (no hostname) is required by the API — returns 404 for
# any hostname not explicitly listed.
resource "cloudflare_zero_trust_tunnel_cloudflared_config" "seshat" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.seshat.id

  config {
    ingress_rule {
      hostname = "agent.${var.domain}"
      service  = "http://caddy:80"
    }
    ingress_rule {
      hostname = "api.${var.domain}"
      service  = "http://caddy:80"
    }
    # Required catch-all — must be last, no hostname
    ingress_rule {
      service = "http_status:404"
    }
  }
}
```

- [ ] **Step 2: Validate**

```bash
cd infrastructure/terraform-cloudflare
terraform validate
```

Expected output: `Success! The configuration is valid.`

If `terraform validate` reports `An argument named "config_src" is not expected here` or similar, check the provider v5 docs — resource schemas occasionally shift between minor versions. The canonical reference is `registry.terraform.io/providers/cloudflare/cloudflare/latest/docs/resources/zero_trust_tunnel_cloudflared`.

- [ ] **Step 3: Commit**

```bash
cd ../..
git add infrastructure/terraform-cloudflare/tunnel.tf
git commit -m "feat(infra): add Cloudflare named tunnel with agent and api ingress rules"
```

---

## Task 3: Define DNS records

**Files:**
- Create: `infrastructure/terraform-cloudflare/dns.tf`

- [ ] **Step 1: Create dns.tf**

Create `infrastructure/terraform-cloudflare/dns.tf`:
```hcl
# CNAME record: agent.frenchforet.com → tunnel (proxied — TLS at Cloudflare edge)
#
# ttl = 1 means "automatic" when proxied = true. Cloudflare ignores the TTL
# for proxied records but the API requires it to be set.
resource "cloudflare_dns_record" "agent" {
  zone_id = var.cloudflare_zone_id
  name    = "agent"
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.seshat.id}.cfargotunnel.com"
  proxied = true
  ttl     = 1
}

# CNAME record: api.frenchforet.com → tunnel (proxied — TLS at Cloudflare edge)
resource "cloudflare_dns_record" "api" {
  zone_id = var.cloudflare_zone_id
  name    = "api"
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.seshat.id}.cfargotunnel.com"
  proxied = true
  ttl     = 1
}
```

- [ ] **Step 2: Validate**

```bash
cd infrastructure/terraform-cloudflare
terraform validate
```

Expected output: `Success! The configuration is valid.`

If validate reports `resource type "cloudflare_dns_record" not found`, the provider version downloaded may be v4 (which uses `cloudflare_record`). Confirm provider version with `terraform providers` — should show `cloudflare/cloudflare 5.x.x`.

- [ ] **Step 3: Commit**

```bash
cd ../..
git add infrastructure/terraform-cloudflare/dns.tf
git commit -m "feat(infra): add proxied CNAME records for agent and api subdomains"
```

---

## Task 4: Define WAF rule

**Files:**
- Create: `infrastructure/terraform-cloudflare/waf.tf`

- [ ] **Step 1: Create waf.tf**

Create `infrastructure/terraform-cloudflare/waf.tf`:
```hcl
# WAF custom ruleset — block api.frenchforet.com requests without Authorization.
#
# This is the primary security layer for the API subdomain. External agents
# MUST send an Authorization header; requests without it are blocked at the
# Cloudflare edge before reaching the VPS.
#
# Note: the WAF checks header *presence*, not token validity. Full token
# validation requires AGENT_GATEWAY_AUTH_ENABLED=true — tracked separately
# (requires PWA Bearer token support + EventSource auth solution).
#
# Header names in Cloudflare's expression engine are always lowercase.
resource "cloudflare_ruleset" "api_auth_check" {
  zone_id     = var.cloudflare_zone_id
  name        = "Require Authorization on API subdomain"
  description = "Block requests to api.frenchforet.com missing Authorization header"
  kind        = "zone"
  phase       = "http_request_firewall_custom"

  rules {
    action      = "block"
    expression  = "(http.host eq \"api.${var.domain}\" and not any(http.request.headers.names[*] eq \"authorization\"))"
    description = "Block API requests without Authorization header"
    enabled     = true
  }
}
```

- [ ] **Step 2: Validate**

```bash
cd infrastructure/terraform-cloudflare
terraform validate
```

Expected output: `Success! The configuration is valid.`

- [ ] **Step 3: Commit**

```bash
cd ../..
git add infrastructure/terraform-cloudflare/waf.tf
git commit -m "feat(infra): add WAF custom rule blocking api subdomain without Authorization header"
```

---

## Task 5: Add outputs and run terraform plan

**Files:**
- Create: `infrastructure/terraform-cloudflare/outputs.tf`

- [ ] **Step 1: Create outputs.tf**

Create `infrastructure/terraform-cloudflare/outputs.tf`:
```hcl
output "tunnel_id" {
  description = "Cloudflare Tunnel ID"
  value       = cloudflare_zero_trust_tunnel_cloudflared.seshat.id
}

output "tunnel_token" {
  description = "Cloudflare Tunnel token — set as CLOUDFLARE_TUNNEL_TOKEN in VPS .env then restart cloudflared"
  value       = cloudflare_zero_trust_tunnel_cloudflared.seshat.tunnel_token
  sensitive   = true
}

output "agent_cname" {
  description = "CNAME target for agent subdomain"
  value       = cloudflare_dns_record.agent.content
}

output "api_cname" {
  description = "CNAME target for api subdomain"
  value       = cloudflare_dns_record.api.content
}
```

- [ ] **Step 2: Validate**

```bash
cd infrastructure/terraform-cloudflare
terraform validate
```

Expected output: `Success! The configuration is valid.`

- [ ] **Step 3: Run terraform plan**

This requires real credentials in `terraform.tfvars`. It does NOT apply any changes.

```bash
cd infrastructure/terraform-cloudflare
terraform plan
```

Expected output: Plan showing resources to create:
```
Plan: 5 to add, 0 to change, 0 to destroy.
```

Resources: `cloudflare_zero_trust_tunnel_cloudflared.seshat`, `cloudflare_zero_trust_tunnel_cloudflared_config.seshat`, `cloudflare_dns_record.agent`, `cloudflare_dns_record.api`, `cloudflare_ruleset.api_auth_check`

If you see provider errors about unsupported resource types, check whether the provider version downloaded is 5.x — run `terraform providers` to confirm.

- [ ] **Step 4: Commit**

```bash
cd ../..
git add infrastructure/terraform-cloudflare/outputs.tf
git commit -m "feat(infra): add terraform outputs for tunnel id and token"
```

---

## Task 6: Update Caddyfile

**Files:**
- Modify: `config/cloud-sim/Caddyfile`

- [ ] **Step 1: Add the two new site blocks**

Open `config/cloud-sim/Caddyfile` and append the following **after** the existing `http://172.25.0.10` block:

```caddyfile
# Cloudflare Tunnel — user-facing PWA + same-origin API
# Traffic arrives from cloudflared with Host: agent.frenchforet.com.
# Uses the shared routing snippet: /api/* → seshat-gateway:9001, /* → seshat-pwa:3000.
http://agent.frenchforet.com {
	import routing
}

# Cloudflare Tunnel — external agent API access
# All paths route directly to the gateway.
# TLS is terminated at the Cloudflare edge; traffic arrives here as plain HTTP.
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

- [ ] **Step 2: Validate Caddyfile syntax**

```bash
docker run --rm \
  -v "$(pwd)/config/cloud-sim/Caddyfile:/etc/caddy/Caddyfile:ro" \
  caddy:2-alpine caddy validate --config /etc/caddy/Caddyfile
```

Expected output: `Valid configuration`

If you see `unknown directive` errors, check Caddy image version matches `caddy:2-alpine`.

- [ ] **Step 3: Commit**

```bash
git add config/cloud-sim/Caddyfile
git commit -m "feat(caddy): add site blocks for agent.frenchforet.com and api.frenchforet.com"
```

---

## Task 7: Update docker-compose PWA build arg

**Files:**
- Modify: `docker-compose.cloud.yml`

- [ ] **Step 1: Update NEXT_PUBLIC_SESHAT_URL**

In `docker-compose.cloud.yml`, find the `seshat-pwa` service build args section:

```yaml
      args:
        NEXT_PUBLIC_SESHAT_URL: "http://172.25.0.10"
```

Change it to:

```yaml
      args:
        NEXT_PUBLIC_SESHAT_URL: "https://agent.frenchforet.com"
```

The PWA makes same-origin API calls to `/api/*` — since `SESHAT_API` in `seshat-pwa/src/lib/agui-client.ts` is the base URL, calls will now go to `https://agent.frenchforet.com/api/*`, `https://agent.frenchforet.com/stream/*`, etc. Caddy's `import routing` on the `agent.*` block routes these to the gateway. ✅

- [ ] **Step 2: Commit**

```bash
git add docker-compose.cloud.yml
git commit -m "feat(pwa): update NEXT_PUBLIC_SESHAT_URL to public frenchforet.com domain"
```

---

## Task 8: Add external-agent token to gateway_access.yaml

**Files:**
- Modify: `config/gateway_access.yaml`

This is preparatory work. The token entry is loaded from the YAML but unused until `AGENT_GATEWAY_AUTH_ENABLED=true` is set (separate task). Adding it now keeps the config ready.

- [ ] **Step 1: Add the external-agent entry**

Open `config/gateway_access.yaml` and append after the existing `execution-service` entry:

```yaml
  - name: external-agent
    secret: "${GATEWAY_TOKEN_EXTERNAL_AGENT}"
    scopes:
      - knowledge:read
      - knowledge:write
      - sessions:read
      - observations:read
    rate_limit: "500/hour"
```

- [ ] **Step 2: Run existing auth tests to confirm no regression**

```bash
uv run pytest tests/personal_agent/gateway/test_auth.py -v
```

Expected: All tests pass. The YAML change doesn't affect tests since they use `tmp_path` fixtures with their own config files.

- [ ] **Step 3: Commit**

```bash
git add config/gateway_access.yaml
git commit -m "feat(auth): add external-agent token entry to gateway_access.yaml"
```

---

## Task 9: Apply Terraform and update VPS .env

This task runs real Terraform against Cloudflare and updates the live VPS.

**Before starting:** The existing manually-created Cloudflare Tunnel (if any) will be replaced. The old tunnel will still exist in the Cloudflare dashboard — delete it manually after the new one is confirmed working to avoid stale entries.

- [ ] **Step 1: Apply Terraform**

```bash
cd infrastructure/terraform-cloudflare
terraform apply
```

Review the plan output. Type `yes` to confirm.

Expected: `Apply complete! Resources: 5 added, 0 changed, 0 destroyed.`

- [ ] **Step 2: Extract the tunnel token**

```bash
terraform output -raw tunnel_token
```

This prints the tunnel token. Copy it — you'll need it in the next step.

- [ ] **Step 3: Update CLOUDFLARE_TUNNEL_TOKEN on the VPS**

SSH into the VPS and update `.env`:

```bash
ssh <your-vps-ssh-alias>
cd /opt/seshat
# Edit .env and set:
# CLOUDFLARE_TUNNEL_TOKEN=<token-from-step-2>
nano .env
```

Find the `CLOUDFLARE_TUNNEL_TOKEN` line (or add it if absent) and paste the token from Step 2.

- [ ] **Step 4: Verify DNS records propagated**

From your local machine (not VPS):

```bash
dig agent.frenchforet.com CNAME +short
dig api.frenchforet.com CNAME +short
```

Expected: Both should resolve to `<tunnel-id>.cfargotunnel.com`

If DNS hasn't propagated yet, wait 1-2 minutes and retry.

- [ ] **Step 5: Commit Terraform lock file if generated**

```bash
cd ../..
# Only commit if .terraform.lock.hcl exists and is not gitignored
# The current .gitignore pattern excludes it — no action needed
```

---

## Task 10: Deploy and verify end-to-end

- [ ] **Step 1: Deploy with full rebuild**

The PWA image must be rebuilt since `NEXT_PUBLIC_SESHAT_URL` is a build-time arg baked into the Next.js bundle.

```bash
bash infrastructure/scripts/deploy.sh --full
```

Expected: All containers healthy in status output.

- [ ] **Step 2: Verify PWA loads at agent subdomain**

```bash
curl -I https://agent.frenchforet.com
```

Expected: `HTTP/2 200` with `content-type: text/html`

- [ ] **Step 3: Verify WAF blocks api subdomain without auth**

```bash
curl -I https://api.frenchforet.com/health
```

Expected: `HTTP/2 403` (blocked by Cloudflare WAF before reaching the VPS)

- [ ] **Step 4: Verify api subdomain passes with Authorization header**

```bash
curl -I -H "Authorization: Bearer placeholder" https://api.frenchforet.com/health
```

Expected: `HTTP/2 200` (WAF passes it; gateway auth is disabled so any token works for now)

Note: Once `AGENT_GATEWAY_AUTH_ENABLED=true` is enabled (separate task), the gateway will validate the token value. For now, any non-empty Authorization header satisfies the WAF rule.

- [ ] **Step 5: Verify SSE stream works from the PWA**

Open `https://agent.frenchforet.com` in a browser. Send a chat message. Verify a response streams back. Check browser DevTools Network tab — the `/stream/*` and `/chat/stream` calls should all show `200` with `agent.frenchforet.com` as the host.

- [ ] **Step 6: Commit with deployment notes**

```bash
git add -u
git commit -m "deploy: Cloudflare tunnel live at frenchforet.com — agent and api subdomains active"
```

---

## Follow-up Tasks (out of scope for this plan)

These should be tracked as separate Linear issues:

1. **Gateway auth + PWA token** — Enable `AGENT_GATEWAY_AUTH_ENABLED=true`. Requires: adding Bearer token to all PWA `fetch()` calls, and solving SSE auth (EventSource doesn't support custom headers — options: cookie-based session token, query-param token for SSE only, or proxy via fetch/ReadableStream).

2. **Delete old manually-created Cloudflare Tunnel** — After confirming the Terraform-managed tunnel is stable, remove the old one from the Cloudflare dashboard to avoid stale entries.

3. **Remote Terraform state** — If the team grows or a second deployer is added, migrate state to an encrypted backend (Cloudflare R2 + SSE or Terraform Cloud).

4. **Rate limiting** — Add a Cloudflare rate-limit ruleset to `api.frenchforet.com` to cap requests per IP.
