# Mac SLM Tunnel Design

**Date:** 2026-04-17
**Status:** Approved — pending implementation plan
**Author:** brainstorming session

---

## Problem

The VPS `seshat-gateway` container has `models.cloud.yaml` pointing primary/sub_agent model endpoints to `http://localhost:8000/v1` — the VPS's own loopback. When a user selects the `local` profile in the PWA, the gateway cannot reach the MacBook's MLX slm_server. This design exposes the Mac's slm_server through a Cloudflare Tunnel so the VPS gateway can reach it securely.

---

## Architecture

```
PWA (agent.frenchforet.com)
  └── user selects "Local" profile
        │
        ▼
VPS: seshat-gateway (Docker, cloud-sim network)
  └── LLM call to primary model
        │  endpoint: https://slm.frenchforet.com/v1
        │  headers: CF-Access-Client-Id + CF-Access-Client-Secret
        ▼
Cloudflare Edge
  └── Access policy: service token required → verified ✓
        ▼
MacBook: cloudflared (launchd system daemon)
  └── routes to localhost:8000
        ▼
MacBook: slm_server (MLX, port 8000)
  └── OpenAI-compatible /v1/chat/completions
```

The VPS gateway's LLM client already speaks OpenAI-compatible HTTP — only the endpoint URL and two auth headers change. The slm_server is completely unmodified.

**Embedding and reranker models are unaffected** — they remain pointed at the VPS's own llama.cpp containers (ports 8503/8504) regardless of profile. Only primary LLM inference routes to the Mac.

---

## Components

### 1. New Terraform Module: `infrastructure/terraform-cloudflare-mac/`

Mirrors the structure of `infrastructure/terraform-cloudflare/` exactly, with one addition (`access.tf`):

```
infrastructure/terraform-cloudflare-mac/
├── providers.tf          # Cloudflare provider, same version as VPS module
├── variables.tf          # cloudflare_api_token, zone_id, account_id, domain
├── tunnel.tf             # cloudflare_zero_trust_tunnel_cloudflared.seshat_mac
│                         # cloudflare_zero_trust_tunnel_cloudflared_config.seshat_mac
│                         #   ingress: slm.frenchforet.com → http://localhost:8000
│                         #   catch-all: http_status:404
├── dns.tf                # CNAME slm.frenchforet.com → <tunnel-id>.cfargotunnel.com
├── access.tf             # cloudflare_zero_trust_access_application (slm.frenchforet.com)
│                         # cloudflare_zero_trust_access_service_token
│                         # cloudflare_zero_trust_access_policy (allow: service_token only)
├── outputs.tf            # tunnel_token, cf_access_client_id, cf_access_client_secret
├── terraform.tfvars.example
└── .gitignore            # *.tfvars, .terraform/, terraform.tfstate*
```

Key difference from the VPS tunnel: ingress target is `http://localhost:8000` (Mac's own loopback). cloudflared connects outbound to Cloudflare — no firewall rules or port forwarding required.

All resources are on the Cloudflare free tier. Named tunnels, DNS records, Access Applications, and Service Tokens incur no cost.

### 2. Mac-side: cloudflared as launchd system daemon

```bash
brew install cloudflared
sudo cloudflared service install <tunnel_token>
# Writes /Library/LaunchDaemons/com.cloudflare.cloudflared.plist
# Starts immediately; survives logout; restarts on crash
```

The tunnel token (from `terraform output tunnel_token`) is the only secret needed on the Mac. It is stored by cloudflared's service installer — not committed to this repo.

Admin rights required once at install time only.

### 3. VPS: `config/models.cloud.yaml` overrides

Add endpoint overrides to primary and sub_agent. CF credentials are **not** embedded in YAML (YAML has no env var interpolation) — they come from `AgentSettings` and are injected by the config loader at runtime (see Component 5).

```yaml
models:
  primary:
    endpoint: "https://slm.frenchforet.com/v1"
  sub_agent:
    endpoint: "https://slm.frenchforet.com/v1"
  # embedding, reranker, and all cloud models: unchanged
```

### 4. VPS: `.env` and `docker-compose.cloud.yml`

Two new secrets in VPS `.env` (values from `terraform output`):

```bash
CF_ACCESS_CLIENT_ID=<from terraform output>
CF_ACCESS_CLIENT_SECRET=<from terraform output>
```

Injected into the `seshat-gateway` container in `docker-compose.cloud.yml`:

```yaml
seshat-gateway:
  environment:
    CF_ACCESS_CLIENT_ID: ${CF_ACCESS_CLIENT_ID}
    CF_ACCESS_CLIENT_SECRET: ${CF_ACCESS_CLIENT_SECRET}
```

### 5. Gateway code changes

**`AgentSettings` — two new optional fields**

File: `src/personal_agent/config/settings.py`

```python
cf_access_client_id: str | None = Field(default=None, description="CF Zero Trust service token client ID")
cf_access_client_secret: str | None = Field(default=None, description="CF Zero Trust service token secret")
```

Read from env vars `CF_ACCESS_CLIENT_ID` and `CF_ACCESS_CLIENT_SECRET` via Pydantic's standard env loading.

**`ModelDefinition` — two new optional fields**

File: `src/personal_agent/llm_client/` (model config loader)

```python
@dataclass(frozen=True)
class ModelDefinition:
    # ... existing fields ...
    cf_access_client_id: str | None = None
    cf_access_client_secret: str | None = None
```

The config loader populates these from `settings.cf_access_client_id` / `settings.cf_access_client_secret` for any model whose endpoint hostname is `slm.frenchforet.com`. No YAML changes needed for the credentials themselves.

**LLM client — CF header injection**

When building an outbound HTTP request, the client checks for CF fields and injects headers if present:

```python
def _cf_access_headers(self, model_def: ModelDefinition) -> dict[str, str]:
    if model_def.cf_access_client_id and model_def.cf_access_client_secret:
        return {
            "CF-Access-Client-Id": model_def.cf_access_client_id,
            "CF-Access-Client-Secret": model_def.cf_access_client_secret,
        }
    return {}
```

No other changes to the LLM client. The OpenAI-compatible request body is identical.

**New inference status endpoint**

File: `src/personal_agent/service/app.py`

```
GET /api/inference/status
Response: {"local": "up" | "down", "latency_ms": int | null}
```

Makes a `GET https://slm.frenchforet.com/health` request with CF access headers (from `settings`). Timeout: 3 seconds. Returns `"up"` on 2xx, `"down"` on any error or non-2xx. The health path is `/health` (not `/v1/health`) — matches the slm_server's existing health endpoint at `http://localhost:8000/health`.

### 6. PWA: inference availability indicator

File: `seshat-pwa/src/components/ProfileSelector.tsx` and a new `useInferenceStatus` hook.

- On `local` profile selection: immediately call `GET /api/inference/status`
- While `local` is selected: poll every 60 seconds
- Stop polling when `cloud` is selected
- UI: Local profile card shows a status dot (green = up, grey = offline) and latency when up; disables the Local option and shows "Mac inference offline" tooltip when down

Implementation: `setInterval` in `useInferenceStatus` hook — no WebSocket complexity.

---

## Error Handling

| Failure scenario | Behaviour |
|---|---|
| Mac asleep / cloudflared not running | Probe times out in 3s → `"down"`. PWA disables Local button + shows "Mac inference offline" tooltip. |
| slm_server not started (tunnel up, port 8000 closed) | cloudflared returns 502. Probe catches non-2xx → `"down"`. Same PWA behaviour. |
| CF service token invalid / expired | Cloudflare returns 403. Probe catches → `"down"` + gateway logs warning with `trace_id`. Operator rotates token via `terraform apply`. |
| User sends message while local is `"down"` | Gateway returns `503` with `{"error": "local_inference_unavailable"}`. PWA shows inline error. **No silent cloud fallback** — user chose local explicitly. |
| Tunnel up, inference responding slowly | Probe succeeds, `latency_ms` returned. PWA shows latency in tooltip. |

**Core principle:** never silently fall back to cloud when the user selected local. Availability is surfaced; the choice stays with the user.

---

## Testing

| Test | Type | What it verifies |
|---|---|---|
| `test_model_definition_cf_headers` | Unit | `ModelDefinition` with CF fields → `_cf_access_headers()` returns correct dict; empty/None fields → empty dict |
| `test_llm_client_injects_cf_headers` | Unit | Mock httpx; assert CF headers present in outbound request when model has CF fields configured |
| `test_llm_client_no_cf_headers_when_absent` | Unit | Model without CF fields → no CF headers in outbound request |
| `test_inference_status_up` | Unit | Mock httpx GET to return 200 → endpoint returns `{"local": "up", "latency_ms": N}` |
| `test_inference_status_down_timeout` | Unit | Mock httpx to raise `ConnectTimeout` → returns `{"local": "down", "latency_ms": null}` |
| `test_inference_status_down_502` | Unit | Mock httpx to return 502 → returns `{"local": "down", "latency_ms": null}` |
| `test_inference_status_down_403` | Unit | Mock httpx to return 403 → returns `{"local": "down"}` + warning logged |
| `test_models_cloud_yaml_loads` | Integration | `models.cloud.yaml` parses without error; primary/sub_agent endpoints are `slm.frenchforet.com`; CF fields present |
| Manual E2E | Manual | Start slm_server, `terraform apply`, install cloudflared service on Mac, hit `GET /api/inference/status` from VPS → `{"local": "up"}` |

All unit tests use mocked httpx — no real tunnel needed in CI. Manual E2E is the acceptance gate.

---

## File Changelist

| File | Change |
|---|---|
| `infrastructure/terraform-cloudflare-mac/` | New directory — all files new |
| `config/models.cloud.yaml` | Add `endpoint`, `cf_access_client_id`, `cf_access_client_secret` to `primary` and `sub_agent` |
| `docker-compose.cloud.yml` | Add `CF_ACCESS_CLIENT_ID`, `CF_ACCESS_CLIENT_SECRET` to `seshat-gateway` env |
| `src/personal_agent/config/settings.py` | Add `cf_access_client_id`, `cf_access_client_secret` to `AgentSettings` |
| `src/personal_agent/llm_client/` (model config loader) | Add two optional fields to `ModelDefinition`; populate from `settings` for `slm.frenchforet.com` endpoints |
| `src/personal_agent/llm_client/` (HTTP client) | Add `_cf_access_headers()` method; inject into outbound requests |
| `src/personal_agent/service/app.py` | Add `GET /api/inference/status` endpoint |
| `seshat-pwa/src/hooks/useInferenceStatus.ts` | New hook — polls `/api/inference/status` |
| `seshat-pwa/src/components/ProfileSelector.tsx` | Use `useInferenceStatus`; show availability dot + tooltip |
| VPS `.env` (not committed) | Add `CF_ACCESS_CLIENT_ID`, `CF_ACCESS_CLIENT_SECRET` |

---

## References

- Existing VPS tunnel: `infrastructure/terraform-cloudflare/`
- SLM Server integration: `docs/guides/SLM_SERVER_INTEGRATION.md`
- Profile config: `config/profiles/local.yaml`
- Cloud model config: `config/models.cloud.yaml`
- slm_server health endpoint: `http://localhost:8000/health`
- slm_server models endpoint: `http://localhost:8000/v1/models`
