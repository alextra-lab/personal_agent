# VPS + Cloudflare + Local Topology Audit (FRE-214)

> **Status**: Verdict received 2026-05-08 — **RATIFY** (VPS is canonical; laptop must mirror). See §7 forward plan.
> **Date**: 2026-05-08
> **Author**: Wave D kickoff (Tier-1:Opus)
> **Tracks**: [FRE-214](https://linear.app/frenchforest/issue/FRE-214)
> **Reconciled against**: [ADR-0045](../architecture_decisions/ADR-0045-infrastructure-cloud-knowledge-layer.md), [ADR-0044](../architecture_decisions/ADR-0044-provider-abstraction-dual-harness.md), [ADR-0043](../architecture_decisions/ADR-0043-three-layer-separation.md)

---

## 1. Why this audit exists

ADR-0045 (2026-04-13) sketched a **thin Knowledge-Layer gateway on the VPS** and an **Execution Layer on the laptop**. Six weeks later (2026-05-08) the deployed VPS runs the **full execution stack** — orchestrator, primary agent loop, MCP gateway, reranker/embedding inference, the Next.js PWA, Caddy, a Cloudflare Tunnel, and the six datastores. That divergence is acknowledged in the codebase itself: `Dockerfile.gateway:58–60` reads

> *"Run the full Seshat service app (orchestrator, memory, brainstem, AG-UI endpoints). ADR-0044/FRE-207: switched from thin gateway to full service app so the cloud profile can dispatch to LiteLLM via the profile-aware LLM factory."*

…and `docker-compose.cloud.yml:276` captions the gateway service as *"Full agent harness — Knowledge API, Observation API, **orchestration**"*. The reframing in FRE-214 is correct: this audit is **not** a redesign. It is a parity audit that produces the deliverables listed below and ends with one open question for the owner.

**Deliverables, in order:**
1. Feature-parity matrix (§2)
2. Deviation log with remediation decisions (§3)
3. Topology diagram of the actually-deployed state (§4)
4. ADR action — recommendation + open question for owner verdict (§5)

---

## 2. Feature-parity matrix

Rows are agent-visible capabilities. Columns are the two deployment shapes the codebase actively supports today.

| # | Capability | Local (`make up` + `make dev`) | VPS (`docker-compose.cloud.yml`) | Notes |
|---|------------|--------------------------------|----------------------------------|-------|
| **Datastores** ||||
| 1 | PostgreSQL 17 + pgvector | ✅ port 5432, no resource caps | ✅ 127.0.0.1:5432, 512 MB / 0.5 CPU | Same image. Cloud caps memory; local doesn't. |
| 2 | Neo4j 5.26 Community | ✅ ports 7474/7687, APOC | ✅ 127.0.0.1:7474/7687, heap 1G + pagecache 256M, `bolt_advertised_address=graph.frenchforet.com:443` | Cloud advertises Bolt over CF for Browser-side WebSocket split. |
| 3 | Elasticsearch 8.19 | ✅ port 9200, heap 512m | ✅ 127.0.0.1:9200, heap 512m–1g, 2 GB / 1 CPU | Same. |
| 4 | Redis 7 | ✅ port 6379 | ✅ 127.0.0.1:6379, AOF on, 128 MB | Cloud explicitly enables `--appendonly yes`; local relies on default. |
| 5 | SearXNG | ✅ port 8888 (`SEARXNG_BASE_URL=http://localhost:8888`) | ✅ 127.0.0.1:8888 (`SEARXNG_BASE_URL=http://searxng:8080`) | Internal hostname differs — cosmetic. |
| 6 | Kibana 8.19 | ✅ port 5601 | ✅ 127.0.0.1:5601 (CF Tunnel → `monitoring.frenchforet.com`) | Kibana **is** present on cloud — FRE-214 description is now stale on this point. |
| **Inference plane** ||||
| 7 | Primary LLM (`primary` role) | Local SLM Server at `http://localhost:8000/v1` (Qwen3.6-35B-A3B) | Cloud profile: Anthropic Claude Sonnet via LiteLLM. Local profile-on-VPS: reverse-tunnel to `https://slm.frenchforet.com/v1` (Mac SLM). | Identical model registry, different endpoints per profile. |
| 8 | Sub-agent LLM | Same SLM Server | Cloud: Claude Haiku via LiteLLM. Local-on-VPS: same Mac SLM tunnel. | — |
| 9 | Embeddings (`Qwen3-Embedding-0.6B`) | Local SLM Server `:8503/v1` | Dedicated `cloud-sim-embeddings` (llama.cpp container) `:8503/v1`, models mounted from `/opt/seshat/models/` | Same model file, **different runtime** (llama.cpp vs slm_server). Vector parity must be verified — see §3 D-7. |
| 10 | Reranker (`Qwen3-Reranker-0.6B`) | Local SLM Server `:8504/v1` | Dedicated `cloud-sim-reranker` (llama.cpp container) `:8504/v1` | Same model file, different runtime. Same parity concern. |
| 11 | Background-task models (entity extraction, Captain's Log, insights) | Currently `gpt-5.4-nano` via OpenAI on both | `gpt-5.4-nano` via OpenAI | Parity ✅ |
| **Service surface** ||||
| 12 | Personal Agent service | Native uvicorn on host port 9000 (`make dev`) | Containerized as `seshat-gateway`, port **9001**, full harness | Port differs deliberately — see Makefile lines 32–35. |
| 13 | PWA (Next.js) | ❌ not deployed locally | ✅ containerized, port 3000, `NEXT_PUBLIC_SESHAT_URL=https://agent.frenchforet.com` baked in at build time | Local dev runs PWA via `npm run dev` if at all. |
| 14 | Reverse proxy | ❌ none | ✅ Caddy (static IP 172.25.0.10) — TLS termination, route split, WebSocket upgrade for Bolt | Local accesses services on raw ports. |
| 15 | Cloudflare Tunnel | ❌ none | ✅ `cloudflared` daemon — inbound: `agent.frenchforet.com`, `api.frenchforet.com`, `graph.frenchforet.com`, `monitoring.frenchforet.com` | All public ingress runs through CF (no public IPs exposed). |
| 16 | Mac SLM tunnel (reverse direction) | n/a | Configured via `infrastructure/terraform-cloudflare-mac/` (private repo). Mac runs cloudflared client → exposes `slm.frenchforet.com` → VPS calls back for local-profile inference. | Only matters if **local profile is selected from a phone** — see §5. |
| **Tools and MCP** ||||
| 17 | MCP gateway enabled servers | Default: `docker mcp gateway run` (whatever the local Docker Desktop profile has authorized) | **Hardcoded `--servers "sequentialthinking,context7"` in `docker/mcp/run-gateway.sh:67`**, ignoring `AGENT_MCP_GATEWAY_ENABLED_SERVERS` | See §3 D-1. Likely root cause of FRE-223. |
| 18 | MCP secrets path | `mcp-secrets.env` lookup via Docker Desktop secrets engine | `MCP_SECRETS_FILE=/opt/seshat/mcp-secrets.env` (host bind into MCP gateway container via Docker socket) | No local analog — local uses Docker Desktop OAuth (DCR) path that the VPS can't replicate. |
| 19 | Linear MCP | ✅ via Docker Desktop OAuth (DCR) | ❌ excluded by design — DCR not available on plain VPS (`run-gateway.sh:13–22`) | Means agent self-filing of Linear tickets only works in Claude Code sessions, not VPS-runtime. |
| 20 | Native Python tools (fetch_url, run_python sandbox, etc.) | ✅ | ✅ | Parity ✅ |
| **Identity and auth** ||||
| 21 | Gateway auth tokens | `gateway_auth_enabled: false` (dev) | `AGENT_GATEWAY_AUTH_ENABLED=true`; tokens defined in `config/gateway_access.yaml`, secrets via env | Five token roles; `execution-service` token is currently unused because the gateway *is* the execution service (deviation D-2). |
| 22 | Cloudflare Access (Zero Trust) | n/a | ✅ Service tokens for backend + monitoring; user-facing PWA gated by CF Access policy | — |
| **Persistence / disk-durable state** ||||
| 23 | Captain's Log captures | Host filesystem | Named volume `seshat_captures_cloud` mounted at `/app/telemetry/captains_log/captures` | Cloud explicitly persists across container restarts. |
| 24 | Feedback history + poller state | Host filesystem | Named volume `seshat_feedback_history_cloud` | ADR-0054 conformance on cloud is intentional. |
| 25 | run_python sandbox workspace | Host filesystem | Named volume `seshat_workspace_cloud` (`AGENT_SANDBOX_SCRATCH_ROOT`) | Same. |
| **Bring-up / control** ||||
| 26 | First-time stand-up | `make up && make dev` | `bash infrastructure/scripts/transfer-models.sh` + `make build-full` (or `vps-bootstrap`) | Asymmetric: VPS bootstrap requires a one-time model transfer that local dev never does. |
| 27 | Day-to-day control surface | `make ps / logs / restart / shell SERVICE=…` | `ENV=cloud make ps / logs / restart …` (over SSH) | The Makefile **already supports** ENV=cloud uniformly — partial solve for FRE-215. |
| 28 | Code deploy | n/a (live reload) | `make deploy / build / build-full` (SSH → git pull + compose) | Documented and used; works. |
| **Dev / test environment parity** ||||
| 29 | Integration tests (`@pytest.mark.requires_llm_server`) | Pass — local Qwen on `:8000` is reachable | **Skip silently** — `conftest.py` probes `localhost:8000`, finds nothing, skips. Cloud profile keys are ignored. | This is the surface area of [**FRE-336**](https://linear.app/frenchforest/issue/FRE-336) (related Tier-1 ticket). |
| 30 | Eval harness | `make eval-…` | `ENV=cloud make eval-skill-routing-cloud` (Wave J shipped on this) | Works on both — eval harness was made env-aware during Wave J. |

**Summary**: of 30 capability rows, 11 are full parity, 14 are intentional asymmetry (cloud has more — proxy, PWA, tunnel, dedicated inference containers), 5 are deviations worth a remediation decision. Those 5 are §3.

---

## 3. Deviation log

Each entry has the form: **what** · **where** (file:line) · **why-as-coded** · **remediation**.

### D-1 — MCP enabled-server set is hardcoded in a shell script

* **What**: VPS exposes only `sequentialthinking` and `context7` to the agent. The intended config knob `AGENT_MCP_GATEWAY_ENABLED_SERVERS` is ignored.
* **Where**: `docker/mcp/run-gateway.sh:67–68` hardcodes `--servers "sequentialthinking,context7"`. `docker-compose.cloud.yml:300` points `AGENT_MCP_GATEWAY_COMMAND` at that script.
* **Why-as-coded**: `docker/mcp/run-gateway.sh:13–22` documents that Linear MCP needs Docker Desktop OAuth (DCR) which doesn't exist on a plain VPS. The two server names became hardcodes during the workaround.
* **Remediation — bring to parity**: read the server list from env, fall back to the default. File the work as the existing FRE-223 (Sonnet tool-use bug) once a fix lands. Keep Linear's exclusion explicit + commented.

### D-2 — VPS runs the full execution layer, not the thin gateway

* **What**: ADR-0045 sketches a thin gateway exposing only Knowledge + Observation APIs. The deployed gateway is the entire `personal_agent.service.app:app` — orchestrator, MCP, brainstem, sub-agents, primary loop.
* **Where**: `Dockerfile.gateway:58–62` (`uv run uvicorn personal_agent.service.app:app --port 9001`). `docker-compose.cloud.yml:276` ("Full agent harness — Knowledge API, Observation API, orchestration"). FRE-207 is the historical commit reference.
* **Why-as-coded**: The implementer concluded that ADR-0044 cloud-profile dispatch through LiteLLM required the full service. That conflates *where the service runs* with *which profile is active*. Per ADR-0044, both profiles should coexist inside one service instance regardless of host. The actual driver is **ADR-0048** (mobile/multi-device UI): the PWA needs a 24/7 reachable execution endpoint, which a thin gateway alone doesn't provide.
* **Remediation — see §5**. This is the deviation that gates the verdict. Two options: (a) **ratify** by amending ADR-0045 to re-scope the VPS as "always-on harness host", or (b) **roll back** by relocating execution off the VPS (laptop or separate cloud worker), reducing the VPS to the thin gateway as originally specified. Recommendation in §5.

### D-3 — `NEXT_PUBLIC_SESHAT_URL` baked into PWA image at build time

* **What**: The Next.js PWA image is built with `NEXT_PUBLIC_SESHAT_URL=https://agent.frenchforet.com` baked in via `ARG`. Same for `NEXT_PUBLIC_GATEWAY_TOKEN`. Image is therefore deployment-specific.
* **Where**: `Dockerfile.pwa:22–26`, passed via `docker-compose.cloud.yml:380–381`.
* **Why-as-coded**: Next.js statically inlines `NEXT_PUBLIC_*` at build time. Currently the only deployed environment is `frenchforet.com`, so it works in practice.
* **Remediation — feeds [FRE-216](https://linear.app/frenchforest/issue/FRE-216)** (externalize hardcoded env params). Real fix: load PWA backend URL from `/runtime-config.json` fetched at first paint, or bake a placeholder + rewrite at container start. Don't block FRE-214 on it.

### D-4 — Embedding/reranker runtime differs between local and cloud

* **What**: Local uses `slm_server` (custom MLX wrapper) hosting both models on ports 8503/8504. Cloud uses two dedicated `llama.cpp` containers (`Dockerfile.llmserver`, GGUF files mounted from `/opt/seshat/models/`). Same weights, different inference runtime.
* **Where**: `Dockerfile.llmserver`, `docker-compose.cloud.yml:176–248`, `infrastructure/scripts/transfer-models.sh:22–23`. Local: `config/models.yaml:188,200`. Cloud: `config/models.cloud.yaml:193,205`.
* **Why-as-coded**: VPS has no Apple Silicon — MLX path doesn't apply. llama.cpp runs the same GGUF on x86. Pragmatic.
* **Remediation — codify and verify, don't unify**. This is intentional divergence. Risk: vector mismatch between environments could corrupt similarity search if both write to the same Knowledge Layer. Action: write a parity test that hashes the embedding output for a fixed input across both runtimes; alarm if the cosine drops below ~0.999. File as an FRE-336 sub-task (test-environment parity is FRE-336's brief anyway).

### D-5 — `transfer-models.sh` hardcodes a developer-specific source path

* **What**: `EMBEDDING_SRC=/Volumes/EnvoyUltra/lm-studio/models/Qwen/...`, similar for reranker. Ties bootstrap to one user's external drive.
* **Where**: `infrastructure/scripts/transfer-models.sh:22–23`.
* **Why-as-coded**: It was written as a one-shot for the project owner.
* **Remediation — codify**. Make `EMBEDDING_SRC`/`RERANKER_SRC` env-overridable with sensible defaults; document in the script header. Trivial — fold into FRE-216 or a new chore.

### D-6 — `execution-service` gateway token is unused

* **What**: `config/gateway_access.yaml:40–49` defines a token role intended for an external execution service to authenticate against a thin VPS gateway. With the full harness on VPS (D-2), nothing authenticates with that role — the orchestrator and the gateway are the same process.
* **Where**: `config/gateway_access.yaml:40–49`.
* **Why-as-coded**: Designed for the ADR-0045 thin-gateway shape. Carried forward as dead config.
* **Remediation — depends on §5 verdict**. If we ratify D-2: delete the token role and the `knowledge:write/sessions:write` scopes that only exist for it. If we roll back: keep the token, deploy a separate execution service against it.

### D-7 — Test harness assumes local Qwen on `:8000`

* **What**: `@pytest.mark.requires_llm_server` skips silently on VPS because `conftest.py` probes `localhost:8000`, ignores Anthropic/OpenAI keys that the cloud profile would actually use.
* **Where**: `conftest.py` (per FRE-336 description).
* **Why-as-coded**: Pre-dates the dual-harness profiles; never updated.
* **Remediation**: Already ticketed as **FRE-336** (Tier-1:Opus). Surfacing it here so the parity matrix accounts for it.

---

## 4. Deployed topology — actual state (2026-05-08)

```
                                 ┌─────────────────────────────┐
                                 │  Cloudflare edge            │
                                 │                             │
   Phone / iPad / laptop ──HTTPS─▶  agent.frenchforet.com      │
   browser                       │  api.frenchforet.com        │
                                 │  graph.frenchforet.com      │
   admin (CF Access)  ──HTTPS───▶  monitoring.frenchforet.com  │
                                 └────────────┬────────────────┘
                                              │  CF Tunnel (HTTP/2)
                                              ▼
   ┌────────────────────────── VPS (Docker network 172.25.0.0/16) ──────────────────────────┐
   │                                                                                          │
   │     cloudflared ──────────────────────────────────────────┐                              │
   │                                                            │                              │
   │  ┌─────────────────┐    ┌──────────────────┐    ┌─────────▼─────────┐                    │
   │  │  caddy (172.25  │    │ seshat-pwa       │    │  Caddy routing    │                    │
   │  │  .0.10)         │◀──▶│ Next.js :3000    │    │  /api,/chat,/docs │                    │
   │  │  TLS, WS upgrade│    │ (built with hard-│    │      → gateway    │                    │
   │  │                 │    │  coded URL)      │    │  /  → PWA         │                    │
   │  └────────┬────────┘    └──────────────────┘    │  WS → neo4j:7687  │                    │
   │           │                                      │  HTTP→ neo4j:7474 │                    │
   │           │                                      └───────────────────┘                    │
   │           ▼                                                                                │
   │  ┌──────────────────────────────────────┐                                                  │
   │  │  seshat-gateway :9001                │  ← FULL HARNESS                                  │
   │  │   uvicorn personal_agent.service.app │     (orchestrator + MCP + brainstem +            │
   │  │   /chat, /api/*, /stream/*, /health  │      LLM client + native tools + AG-UI)          │
   │  │   AGENT_MODEL_CONFIG_PATH=models.cloud.yaml                                              │
   │  │   spawns docker/mcp-gateway via Docker socket → "sequentialthinking,context7"           │
   │  └────┬──────┬───────┬───────┬────────┬─────────┬─────────────┐                            │
   │       │      │       │       │        │         │             │                            │
   │       ▼      ▼       ▼       ▼        ▼         ▼             ▼                            │
   │   postgres  neo4j  elastic  redis  kibana   embeddings   reranker    searxng              │
   │   :5432    :7687  :9200   :6379   :5601    :8503        :8504        :8888                │
   │                                            (llama.cpp,  (llama.cpp,                        │
   │                                             Qwen3-Emb)   Qwen3-Rerank)                     │
   │                                                                                            │
   │  Volumes (persistent): postgres_data_cloud, neo4j_data_cloud, es_data_cloud,               │
   │                        redis_data_cloud, caddy_data, caddy_config,                         │
   │                        seshat_captures_cloud, seshat_feedback_history_cloud,               │
   │                        seshat_workspace_cloud,  /opt/seshat/models (host bind, ro)         │
   └──────────────────────────────────────────────────────────────────────────────────────────┘

           ▲ outbound: LiteLLM → Anthropic / OpenAI APIs (cloud profile)
           ▲ outbound: GET https://slm.frenchforet.com/v1   ← only when local profile selected
                     │
                     │  Mac runs cloudflared client → exposes Mac SLM Server :8000
                     │  (configured via private repo `personal_agent_secrets/terraform-cloudflare-mac`)
                     ▼
                 Mac (local SLM Server :8000, MLX/Qwen3.6-35B)
```

**Local-only counterpart** (when developing on the Mac, `make up + make dev`):

```
   developer browser/CLI ───▶  uvicorn :9000  (full app, not in Docker)
                                │
                                ▼
   docker-compose.yml:  postgres / neo4j / elasticsearch / kibana / redis / searxng
                       (no PWA, no Caddy, no embeddings/reranker container,
                        no cloudflared — agent uses local SLM Server on :8000)
```

The two shapes share the **datastore tier** topology and very little else.

---

## 5. ADR action — recommendation and the open question

### The question that gates the verdict

FRE-214 names the open question explicitly:

> *"Is the full-harness-on-VPS pattern the intended target going forward, or a stopgap for always-on?"*

That is the only decision the audit cannot make on the owner's behalf, because it is a goal question, not an evidence question. The remediations for D-2 and D-6 (and to a lesser extent D-3, D-5) flip sign based on the answer.

### Recommendation: **ratify D-2**, then tighten

1. **Ratify the full-harness-on-VPS pattern** by amending ADR-0045 — not by replacing it. The driver is ADR-0048, not ADR-0044: a 24/7 PWA needs a 24/7 execution endpoint. A thin gateway alone does not satisfy ADR-0048. The shortest path to "always-on phone access" runs the harness on the VPS.
2. **Reframe the laptop's role**: it remains the *primary developer harness* and an *optional execution profile* (via the Mac SLM tunnel for local-Qwen-from-phone), but it is no longer the canonical execution host.
3. **Mark the Mac SLM tunnel as conditional infrastructure** — only required when "local profile selected from a non-laptop client" is a supported use case. If we decide that's a low-value mode, the tunnel can be deprecated and the local profile becomes laptop-only (which it functionally already is).
4. **Land the small fixes immediately** regardless of verdict: D-1 (MCP env-driven), D-4 verification (vector parity test), D-5 (transfer-models.sh portability). These are unambiguously good.
5. **Park D-6** until the verdict on D-2 is final — no point pruning gateway tokens before we decide whether a separate execution service exists.

### Why ratify (rather than roll back)

* **Cost**: the VPS is sized for it (24 GB / 8 vCPU per `docker-compose.cloud.yml` header). Daily-cost telemetry hasn't shown VPS-side resource pressure during Wave J runs.
* **Architectural clarity**: ADR-0044's "two profiles in one service" is *easier* to deliver when there is one service host, not two.
* **Operational simplicity**: rollback would require standing up a separate execution worker somewhere always-on (laptop ≠ always-on, second VPS = doubled ops). That moves complexity, doesn't reduce it.
* **Reversibility**: ratifying does not foreclose a future thin-gateway shape if a real driver emerges (security boundary, data-sovereignty for execution traces, multi-tenant). The pieces are still modular.

### What "ratify" does *not* mean

* **Not** a green-light to add more cloud-only features without local parity. Wave A added telemetry features that broke locally before being patched — the discipline of "if it runs on cloud it must run on local" still applies.
* **Not** a deprecation of `models.yaml` / local profile. Developers still need to run the agent against local Qwen for reasoning evals and offline work.
* **Not** acceptance of the build-time URL bake (D-3) or hardcoded transfer paths (D-5). Those are bugs we kept; ratification doesn't make them OK.

### What I need from you

A single answer to FRE-214's open question. Two options:

1. **"Ratify"** — I'll write the ADR-0045 amendment in the same session, then unblock D2–D6 of the master plan (FRE-217 / FRE-238 / FRE-240 / FRE-241 / FRE-236) under the ratified pattern.
2. **"Roll back"** — I'll write a rollback plan that splits the VPS gateway from a separate execution worker, and the matrix above becomes the migration checklist.

Until that verdict, the immediate tickets are unblocked:

* **D-1** (MCP server list from env) → coupled to FRE-223
* **D-4 verification** (embedding/reranker vector parity test) → small Tier-2 ticket, file under FRE-336 if not standalone
* **D-5** (`transfer-models.sh` portability) → fold into FRE-216

---

## 6. Linked / downstream tickets

This audit produces concrete handles for the issues that FRE-214 was already linked to:

| Issue | Status after audit |
|-------|-------------------|
| [FRE-215](https://linear.app/frenchforest/issue/FRE-215) — Port Makefile control to VPS | **Largely solved already** (Makefile has `ENV=cloud` + `_ON_VPS` detection + cloud targets). Remaining gap: `vps-bootstrap` UX vs ad-hoc `transfer-models.sh`. |
| [FRE-216](https://linear.app/frenchforest/issue/FRE-216) — Externalize hardcoded env in compose | Still valid. D-3 + D-5 are concrete inputs. |
| [FRE-217](https://linear.app/frenchforest/issue/FRE-217) — Containerization review | This audit subsumes most of it. FRE-217 can close as duplicated by FRE-214 once verdict lands, OR remain as the "should `make dev` containerize too?" thread. |
| [FRE-218](https://linear.app/frenchforest/issue/FRE-218) — Brainstem broken on VPS | Not directly visible from this audit (no telemetry inspection). Re-evaluate after verdict. |
| [FRE-222](https://linear.app/frenchforest/issue/FRE-222) — Single UI path | Caddy already routes everything through one host on cloud. Local doesn't have this concern. |
| [FRE-223](https://linear.app/frenchforest/issue/FRE-223) — Sonnet tool-use bug | D-1 is the most likely root cause. Fix D-1 first, retest. |
| [FRE-336](https://linear.app/frenchforest/issue/FRE-336) — Environment parity test layer | Surfaced by D-7. Remains the right home for fixing `requires_llm_server` and the embedding-parity verification from D-4. |

---

---

## 7. Forward plan — convenience, testability, consistency

### 7.1 Verdict (recorded)

**The VPS is the canonical execution host.** The laptop should mirror it (same shape, smaller scale) rather than diverge. Day-to-day development happens on the VPS via Claude Code remote control; the laptop's role narrows to *peer deployment* + *local-profile inference target*.

This was the right call. It also means the audit's §5 recommendation now becomes the implementation brief, weighted by three axes the owner named explicitly:

* **Convenience** — dev loop must stay fast. Code change → restart < 10 s. Claude-Code-on-VPS is the canonical IDE.
* **Testability** — the same tests must run on both deployments without environment-specific skips, and ideally without environment-specific assertions.
* **Consistency** — one compose file, one model config, one bring-up procedure. Code does not branch on "where am I".

Each track below is scored against those three axes so trade-offs are explicit.

### 7.2 Tracks

#### Track 1 — Ratify in ADR-0045 (Tier-1, ~30 min)

Amend (do not supersede) ADR-0045. The Knowledge Layer + thin gateway sketch becomes the *historical* target; the ratified target is **canonical full harness on VPS, laptop is a mirror, profiles select inference path**. Move the relevant audit text into the ADR's "Update — 2026-05-08" section so the ADR remains the source of truth.

| Axis | Why this matters |
|------|------------------|
| Consistency | Future readers must not be misled by the original sketch. |
| Convenience | One-time work, unblocks everything downstream. |
| Testability | Indirect — sets the contract that tests are then written against. |

**Output**: ADR-0045 update + master plan note.

#### Track 2 — Compose unification (Tier-2, ~1 day)

Merge `docker-compose.yml` and `docker-compose.cloud.yml` into a single file driven by **compose profiles** (Docker's native feature, not our `config/profiles/`). Bring-up:

```bash
make up                    # laptop mirror — gateway, pwa, caddy, embeddings, reranker, datastores
make up ENV=cloud          # VPS — adds cloudflared, larger resource caps
make dev                   # gateway with bind-mount + uvicorn --reload (laptop only)
```

Concrete changes:
* One `docker-compose.yml` with `profiles: [cloud]` markers on `cloudflared` and any other VPS-only service.
* Resource caps moved into `docker-compose.cloud.override.yml` (Docker's standard override pattern).
* Gateway service in laptop mode uses `develop.watch` for hot reload — no rebuild on Python edits.
* `make dev` becomes a wrapper that runs `compose up --watch` for the gateway service.
* Embeddings/reranker/primary/sub_agent **stay native on the host as MLX `slm_server`** on laptop — Apple Silicon GPU is not accessible to Docker, so containerizing them on Mac would force CPU-only llama.cpp (slow, pointless when MLX is right there). Containerized services on laptop are limited to gateway + PWA + Caddy + datastores + SearXNG. The gateway container reaches MLX via `host.docker.internal`.
* On VPS the `embeddings` / `reranker` services tag with `profiles: [cloud]` in the unified compose so they only spin up there.

| Axis | Why |
|------|-----|
| Consistency | One compose file, one bring-up. The biggest single payoff. The asymmetry around MLX is forced by hardware, not by config drift. |
| Convenience | `make dev` still gives < 5 s reload. PWA + Caddy locally means UI dev mirrors prod. |
| Testability | Same services in same shape on both hosts → integration tests run identically. Inference parity comes from the runtime-parity test in Track 3. |

**Cost**: laptop resource footprint goes up modestly — gateway container + PWA + Caddy ≈ ~500 MB – 1 GB extra. (MLX inference stays native on host as it does today, so no change there.) Trivial on any modern Mac.

#### Track 3 — Test parity (Tier-1, ~1-2 days; covers FRE-336 + D-4 + D-7)

Today, integration tests skip silently on the VPS because `requires_llm_server` probes `localhost:8000`. The fix has two layers:

1. **Reachability-driven probe**: rename to `requires_llm`. The fixture probes (in order) the active profile's primary model: cloud profile → check `AGENT_ANTHROPIC_API_KEY` + a 1-call ping; local profile → check Qwen on local SLM Server. If any reachable → run. If none → skip *with a loud error* (not silent).
2. **Parity test for embedding/reranker** (D-4): a small fixture that hashes the embedding output for ~10 fixed inputs across MLX (laptop dev mode) and llama.cpp (cloud mode + laptop mirror mode). Cosine ≥ 0.999 → pass. Below → fail with the offending input. Runs in CI on both shapes.

| Axis | Why |
|------|-----|
| Testability | Direct — this *is* the testability fix. |
| Consistency | A test that passes locally but skips on VPS is a consistency hole; closing it is structural. |
| Convenience | Removes "why did this test skip?" from every cross-machine debugging session. |

**Output**: rename `requires_llm_server` → `requires_llm`; new `tests/test_parity/test_embedding_runtime.py`; CI workflow runs both shapes.

This is FRE-336 in disguise. File the parity test as a sub-task of FRE-336 once the fixture lands.

#### Track 4 — Surface fixes (Tier-2/3, cumulative ~half day)

Small cleanups, parallelizable across sessions:

| ID | What | Where | Estimated |
|----|------|-------|-----------|
| D-1 | MCP enabled-server list driven by `AGENT_MCP_GATEWAY_ENABLED_SERVERS` | `docker/mcp/run-gateway.sh` | 30 min — likely closes [FRE-223](https://linear.app/frenchforest/issue/FRE-223) |
| D-3 | PWA fetches `/runtime-config.json` instead of build-time bake | `Dockerfile.pwa`, PWA bootstrap, Caddy route | 2-3 hrs — feeds [FRE-216](https://linear.app/frenchforest/issue/FRE-216) |
| D-5 | `transfer-models.sh` reads `EMBEDDING_SRC` / `RERANKER_SRC` env, with default | `infrastructure/scripts/transfer-models.sh` | 15 min |
| D-6 | Prune `execution-service` gateway token + scope | `config/gateway_access.yaml` | 15 min — defer until Track 2 lands so we're sure it's truly dead |

#### Track 5 — Existing Wave D issues, unblocked under ratified pattern

The original Wave D backlog can now proceed. Re-cast under the ratified shape:

* [**FRE-217**](https://linear.app/frenchforest/issue/FRE-217) — "Containerization review" largely consumed by this audit. **Recommendation: close as duplicate of FRE-214** once the ADR-0045 amendment lands. The remaining "should `make dev` containerize too?" thread is now Track 2.
* [**FRE-238**](https://linear.app/frenchforest/issue/FRE-238) — SLM circuit breaker. Scope unchanged; relevant to Mac SLM tunnel reliability.
* [**FRE-240**](https://linear.app/frenchforest/issue/FRE-240) — Reranker fallback. Now applies to the llama.cpp container too, not just MLX.
* [**FRE-241**](https://linear.app/frenchforest/issue/FRE-241) — slm_server supervisor. Stays Mac-side (the VPS's llama.cpp containers are already supervised by Docker).
* [**FRE-236**](https://linear.app/frenchforest/issue/FRE-236) — PWA iOS SSE. Independent of this audit.
* [**FRE-218**](https://linear.app/frenchforest/issue/FRE-218) — Brainstem broken on VPS. Re-test after Track 2 — likely surfaces from one of the deviations.

### 7.3 Recommended sequencing

```
Track 1 (ADR amendment)   ───▶  Track 4 (D-1, D-5)             ───▶  …
                                Track 2 (compose unification)  ───▶  Track 3 (test parity)  ───▶  Track 5
                                Track 4 (D-3, D-6 deferred)
```

Notes on the order:
* **Track 1 first** — locks direction so subsequent work doesn't waste effort.
* **Track 4 D-1/D-5 in parallel** — they're tiny and each closes a known bug surface.
* **Track 2 before Track 3** — test parity is much easier when the two deployments have the same shape; doing Track 3 against today's divergent shape is the painful path.
* **Track 5 last** — the existing Wave D items get cleaner ground to land on after Tracks 1–3.

### 7.4 What this means for the master plan

* Wave D's stated scope ("containerization decision; SLM circuit breaker; reranker fallback; slm_server supervisor; PWA iOS SSE") absorbs Tracks 1–4 above and FRE-217 closes.
* FRE-336 stays a Tier-1 ticket; Track 3 is its execution plan.
* New tickets to file (small): one each for D-1, D-3, D-5, plus the embedding-parity test under FRE-336.
* No budget impact on the cost-gate side; Track 2 may bump laptop RAM consumption ~3 GB (one-time check before committing).

### 7.5 What I need from you

A pick on what comes next. Three reasonable orderings:

1. **"Land Track 1 now, then start Track 2"** — the recommended order. ADR amendment is small, then we tackle the structural payoff.
2. **"Land Track 1 + Track 4 quick fixes first, defer Track 2"** — if you want momentum on small wins before the bigger refactor.
3. **"Skip ahead to Track 3 (test parity)"** — if FRE-336 is the pain you feel most. Doable but harder before Track 2.

I'd recommend (1). If you confirm, I'll write the ADR-0045 amendment in the next session and then plan Track 2 properly (it deserves its own implementation plan in `docs/superpowers/plans/`).

---

---

## 8. Constraint added 2026-05-08 — model-endpoint abstraction

### 8.1 What the owner asked for

Two coupled requirements:

1. **Hide cloud-vs-local model access from callers.** Tests, eval harness, agent code — none of them should know whether a given model role is served by a local SLM Server, a Cloudflare-tunnel reverse hop, or a LiteLLM cloud provider. They ask for "primary" or "embedding" and the system resolves the right endpoint.
2. **The laptop harness must remain self-contained.** When the harness runs on the laptop (or, soon, on the stationary home server), it must reach local models via direct localhost / host-network paths — *never* through `slm.frenchforet.com` (the reverse Cloudflare tunnel that exists for the VPS to call back to the Mac). Stronger phrasing the owner used: if all cloud models were removed, the laptop must still work end-to-end.

### 8.2 Why this matters for Track 2

Today's laptop config is already self-contained: `config/models.yaml:61` points `primary` at `http://localhost:8000/v1`. The risk is that **Track 2's compose unification, done naively, breaks this**. If the gateway containerizes and the merged model config keeps `slm.frenchforet.com` as the canonical endpoint, the containerized laptop gateway would tunnel out to Cloudflare just to reach a model running 3 inches away. That is exactly the topology the owner is asking us to prevent.

The same problem arrives for embeddings/reranker: a containerized laptop gateway has to reach the laptop's MLX embeddings (if we preserve MLX as an opt-in) without leaving the host.

### 8.3 The mechanism — endpoint resolution

Replace the current "one endpoint per model per env file" pattern with **one model registry + ordered candidate endpoints + first-reachable resolution**:

```yaml
# config/models.yaml  (single source of truth — config/models.cloud.yaml deleted)
primary:
  id: "qwen3.6-35b-a3b"
  provider_type: local
  endpoints:
    # Order = preference. First reachable wins. Probed at client init + cached.
    - http://localhost:8000/v1            # laptop native (uvicorn outside docker)
    - http://host.docker.internal:8000/v1 # laptop containerized (Docker Desktop)
    - https://slm.frenchforet.com/v1      # remote (VPS → Mac reverse tunnel)
  resolve: first_reachable
  probe_timeout_ms: 250

embedding:
  id: "qwen3-embedding-0.6b"
  endpoints:
    - http://localhost:8503/v1                   # laptop native dev (slm_server / MLX on host)
    - http://host.docker.internal:8503/v1        # laptop containerized → MLX on host
    - http://embeddings:8503/v1                  # VPS in-compose llama.cpp container
    - https://slm.frenchforet.com/embedding/v1   # remote tunnel (last resort)
  resolve: first_reachable

reranker:
  id: "qwen3-reranker-0.6b"
  endpoints:
    - http://localhost:8504/v1
    - http://host.docker.internal:8504/v1
    - http://reranker:8504/v1                    # VPS in-compose llama.cpp container
    - https://slm.frenchforet.com/reranker/v1
  resolve: first_reachable
```

**Note on always-on availability**: embedding and reranker run in two places by design — the **Mac as native MLX `slm_server` on the host** (when laptop is online; fast) and the **VPS as `llama.cpp` containers** (CPU, always-on, the availability guarantee). On the VPS, `localhost` and `host.docker.internal` candidates are unreachable, so the in-compose `embeddings` / `reranker` candidate resolves first — VPS uses its own llama.cpp container even when the laptop is online (no point paying tunnel round-trip latency when an equivalent endpoint is one container hop away).

**Important — Apple Silicon GPU constraint**: MLX inference cannot be containerized on Mac. Docker on macOS runs a Linux VM that does not have Metal/MPS passthrough; a containerized embedding service on Mac would fall back to CPU llama.cpp (slow, competing for resources with native MLX). Therefore the laptop mirror is **structurally asymmetric** to the VPS:

| Service | Laptop (native dev *and* containerized mirror) | VPS |
|---------|-----------------------------------------------|-----|
| `primary`, `sub_agent`, `embedding`, `reranker` | **Native MLX `slm_server` on the host**. Always native; never inside docker. | `llama.cpp` Docker containers (embedding + reranker). `primary`/`sub_agent` not deployed; cloud profile uses LiteLLM, local profile uses tunnel. |
| Gateway, PWA, Caddy, datastores, SearXNG | Docker | Docker |

The gateway container on laptop reaches the host's MLX servers via `host.docker.internal:{8000,8503,8504}`. In the unified compose (Track 2b), the `embeddings` and `reranker` Docker services are tagged `profiles: [cloud]` so they only spin up on the VPS — the laptop mirror has nothing to gain from running CPU llama.cpp alongside MLX.

Properties this gives us:

| Property | How |
|----------|-----|
| **Topology-agnostic call sites** | Code calls `get_llm_client("primary")`. The factory resolves at startup. No env-specific branches. |
| **Self-contained laptop** | First candidate is always localhost. The Cloudflare tunnel candidate is last and only relevant on the VPS, where the localhost candidates are unreachable. |
| **No `models.cloud.yaml`** | One file. Eliminates D-2/D-7-style drift between env-specific configs. |
| **Stationary-server ready** | Adding the home server is a new endpoint candidate, not a new config file. |
| **Cloud-fallback is policy, not topology** | If `primary` resolves to no reachable local endpoint AND the active profile permits cloud fallback (per `config/profiles/*.yaml`), the factory escalates. Otherwise it raises — which is what the owner wants when cloud models are intentionally absent. |

The probe is cheap: a 250 ms TCP connect to each candidate at process start, cached for the process lifetime. A `force_reprobe()` API for tests. Failures during a session degrade to the next candidate at the cost of one timeout — acceptable.

### 8.4 Honest discussion of what this *doesn't* solve

* **macOS Docker host networking.** `host.docker.internal` works on Docker Desktop for Mac out of the box. On the upcoming stationary server (likely Linux), the equivalent is `--add-host=host.docker.internal:host-gateway` in compose, which is a one-line change but worth being explicit about.
* **Cloud profile is unaffected.** When a conversation runs on the cloud profile, primary = Claude Sonnet via LiteLLM regardless of host. Endpoint resolution doesn't change anything there.
* **Local-profile-on-VPS still goes through the tunnel.** That's correct behavior — the VPS has no local model, so the only "local" candidate that resolves is the remote one via the tunnel. The owner's constraint applies to the *laptop*, not to "anywhere we configured a local profile".

### 8.5 Track plan — split Track 2 to honor this constraint

Track 2 in §7.2 was a single block. Split it:

| Track | What | Why this order |
|-------|------|----------------|
| **2a — Model endpoint abstraction** | Add `endpoints[]` + first-reachable resolution to `config/models.yaml`. Delete `config/models.cloud.yaml`. Update `llm_client/factory.py`. | **Must land before 2b**, otherwise containerizing the laptop gateway breaks self-containment. Independently shippable on its own. |
| **2b — Compose unification** | Merge `docker-compose.yml` + `docker-compose.cloud.yml` via compose profiles. Containerize laptop gateway with `develop.watch`. | Lands on top of 2a. Confidence that endpoints resolve correctly inside containers comes from 2a's probe logic. |

Track 3 (test parity) becomes much simpler after 2a — `requires_llm` is just "does any candidate resolve?" plus a profile check.

### 8.6 What this does to §7.5

The recommended sequence becomes:

```
Track 1 (ADR amendment) ──▶ Track 2a (endpoint abstraction) ──▶ Track 2b (compose unification) ──▶ Track 3 (test parity) ──▶ Track 5
                            Track 4 (D-1, D-5) in parallel anywhere after Track 1
```

2a is a small, self-contained code change (~half day). It also resolves D-7 for the model-registry side and unblocks FRE-336 partially.

---

### 8.7 Session scope and execution timing

Recorded 2026-05-08 alongside the verdict:

* **This planning work is docs-only.** No implementation in this session. Deliverables are: ADR-0045 amendment (in-place), implementation plans for Tracks 2a / 2b / 3 (under `docs/superpowers/plans/`), Linear tickets filed for Track 4 items, master plan update. Nothing under `src/`, `config/`, `docker-compose*.yml`, or any `Dockerfile` will be touched.
* **The "big conversion" — Tracks 2a, 2b, 3 — is deferred.** The owner intends to reduce the existing backlog before triggering structural change. Plans get written now so they are ready to execute on demand; execution timing is the owner's call.
* **Track 4 (D-1, D-3, D-5, D-6) ships as Linear tickets, not plan docs.** Each is small enough to fit in one Linear issue. Filing them in `Needs Approval` puts them in the visible backlog the owner is working to reduce.
* **Review cadence: one deliverable at a time.** The ADR amendment lands and gets reviewed before Track 2a's plan starts, and so on. Avoids a single huge diff and keeps each artifact independently reviewable.
* **Branch continuity**: all of the above lands on `fre-214-vps-topology-audit`. The PR (when opened) carries audit + verdict + ADR amendment + plans + ticket links as one cohesive change set.

---

*End of audit. §5 verdict received; §7 forward plan recorded; §8 endpoint-abstraction constraint added; §8.7 scope and timing locked. Next action: ADR-0045 amendment.*
