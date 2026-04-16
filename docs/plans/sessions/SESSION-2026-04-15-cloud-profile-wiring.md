# Session Log: 2026-04-15 — Cloud Profile Orchestrator Wiring

> **Date**: 2026-04-15 (continued 2026-04-16)
> **Phase**: Evaluation Phase — Cloud Deployment
> **Lead**: AI assistant + project owner
> **Commits**: `d7360e0` → `12b30b4` (7 commits)

---

## Session Goal

Wire the Seshat PWA's "cloud" execution profile to the **full orchestrator pipeline** (memory, brainstem, gateway, tools) instead of a direct Anthropic API call. Make the cloud path on the VPS functionally equivalent to the local path, just with Claude Sonnet instead of Qwen3.5.

---

## Planned Batches

1. **Profile dispatch plumbing**: Add `ContextVar` profile propagation and update the LLM factory to resolve models from the active profile.
2. **Fire-and-forget endpoint**: Add `/chat/stream` to `service/app.py` that accepts form-encoded data and drives the full orchestrator pipeline asynchronously.
3. **Infrastructure wiring**: Switch `Dockerfile.gateway` from thin gateway to full service app; update Caddy routing; fix docker-compose healthcheck.

---

## Completed Work

### Bug 1: PWA → 404 on `/chat`

**Root cause**: The VPS ran `gateway/app.py` (thin knowledge layer gateway) which had no `/chat` endpoint. The PWA sent `POST /chat`.

**Fix**: Added `gateway/chat_api.py` with a simple direct-Anthropic `/chat` endpoint as a stopgap. Wired it into `gateway/app.py` alongside the SSE transport router.

### Bug 2: `crypto.randomUUID()` SecurityError in Safari (commit `d7360e0`)

**Root cause**: Safari refuses `crypto.randomUUID()` on non-secure contexts. The PWA is served over plain HTTP from `172.25.0.10` (Cloudflare WARP private IP), which Safari doesn't consider a secure context even for loopback.

**Fix**: Created `seshat-pwa/src/lib/uuid.ts` polyfill:
```typescript
export function generateUUID(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    try { return crypto.randomUUID(); } catch {}
  }
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
    const r = (Math.random() * 16) | 0;
    return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
  });
}
```
Replaced 3 call sites in `StreamingChat.tsx` and `useSSEStream.ts`.

### Bug 3: `cloud.yaml` referencing non-existent model keys

**Root cause**: `config/profiles/cloud.yaml` had `primary_model: claude-sonnet-4-20250514` — a raw model ID, not a `models.yaml` key. The factory does `config.models.get(resolved_key)` which returned `None`, falling back to local inference.

**Fix**: Changed to correct keys: `primary_model: claude_sonnet`, `sub_agent_model: claude_haiku`. Added `claude_haiku` model definition to both `models.yaml` and `models.cloud.yaml`.

### Core Feature: Profile-Aware Orchestrator Dispatch

**Three-layer change:**

**Layer 1 — `config/profile.py`**: Added `ContextVar` for async-safe profile propagation:
```python
_current_profile: ContextVar[ExecutionProfile | None] = ContextVar("current_profile", default=None)
def set_current_profile(profile: ExecutionProfile) -> Token: ...
def get_current_profile() -> ExecutionProfile | None: ...
```

**Layer 2 — `llm_client/factory.py`**: Updated `get_llm_client()` to resolve the model key from the active profile:
```python
profile = get_current_profile()
if profile and role_name == "primary" and profile.primary_model:
    resolved_key = profile.primary_model  # → "claude_sonnet"
```

**Layer 3 — `service/app.py`**: Added `POST /chat/stream` fire-and-forget endpoint and `_process_chat_stream_background` coroutine that runs the full pipeline:
1. Load profile, `set_current_profile()` → ContextVar active for downstream calls
2. Get-or-create DB session with client-provided UUID
3. Hydrate conversation history
4. Run gateway pipeline (intent, decomposition, memory context assembly)
5. Run orchestrator → `reply` string
6. Push `TextDeltaEvent` + `None` sentinel to SSE queue
7. Persist assistant message to DB

### Infrastructure Switches

**`Dockerfile.gateway`**: Changed from `gateway/app.py:create_gateway_app --factory` to `service/app.py:app`. The full service app is required because:
- It initializes memory (Neo4j), Elasticsearch, brainstem scheduler
- It runs event bus consumers and MCP gateway
- It has the `/chat`, `/chat/stream`, and `/stream/{session_id}` endpoints

**`docker-compose.cloud.yml`**:
- Healthcheck: `/api/v1/health` → `/health` (service app's actual endpoint)
- `start_period`: 30s → 60s (full service app takes longer to initialize)
- `mem_limit`: 512m → 768m (heavier initialization footprint)

### Bug 4: Caddy routing `/chat/stream` → 404

**Root cause**: `Caddyfile` path matcher had exact `/chat` but not `/chat/stream`. Next.js PWA received the request and returned its own 404.

**Fix**: Added `/chat/stream` to the `@backend` path matcher.

**Complication**: `git pull` on the VPS replaced the Caddyfile inode. Docker bind mounts track inodes, not paths, so the container still saw the old file after pull. Fix: `docker compose restart caddy` re-establishes the mount to the new inode.

### Bug 5: LiteLLM `AuthenticationError` — missing API key

**Root cause**: LiteLLM reads `ANTHROPIC_API_KEY` from environment. The service uses `AGENT_ANTHROPIC_API_KEY` (Pydantic settings with `env_prefix="AGENT_"`). LiteLLM never found the key.

**Fix**: Pass the key explicitly in `litellm_client.py`:
```python
if self.provider == "anthropic":
    api_key = _settings.anthropic_api_key or None
if api_key:
    litellm_kwargs["api_key"] = api_key
```

---

## Blockers Encountered

| Blocker | Impact | Resolution | Time Lost |
|---------|--------|------------|-----------|
| `crypto.randomUUID()` SecurityError (Safari) | Cloud path totally broken | UUID polyfill | ~30 min |
| `cloud.yaml` wrong model keys | Profile dispatch silently fell back to local | Fix key names, add claude_haiku | ~20 min |
| Caddy `path` matcher exact-match only | `/chat/stream` routed to Next.js → 404 | Add path to matcher | ~20 min |
| Docker bind-mount inode invalidation on git pull | Caddy reload read stale config | `docker compose restart caddy` | ~15 min |
| LiteLLM `ANTHROPIC_API_KEY` env var lookup | All cloud calls failed with AuthError | Explicit api_key in kwargs | ~15 min |

---

## Decisions Made

1. **Add `/chat/stream` instead of modifying `/chat`**: The existing `/chat` is used by the CLI (`service_client.py`) with query params and a synchronous response. Adding a parallel endpoint avoids breaking the CLI while giving the PWA its fire-and-forget, form-encoded path.

2. **Set profile inside background task (not in endpoint handler)**: The `asyncio.Task` inherits the parent context, so `set_current_profile()` called at the start of `_process_chat_stream_background` works correctly. This is cleaner than setting it in the endpoint handler before task creation.

3. **Get-or-create session with client UUID**: The PWA generates its own UUID and uses it for both `/chat/stream` (POST) and `/stream/{session_id}` (SSE). Rather than requiring a prior `POST /sessions` call, the background task creates the DB session with the client's UUID if it doesn't exist, enabling conversation continuity across turns.

4. **Full service app in gateway container**: The thin `gateway/app.py` would need all the same initialization wired in (memory, scheduler, etc.) to run the orchestrator. It's simpler to run `service/app.py:app` directly.

---

## Velocity

- **Planned batches**: 3
- **Completed batches**: 3 + 5 bug fixes
- **Assessment**: Above target. All planned work shipped plus significant debugging.

---

## What Went Well

- The ContextVar approach for profile propagation was elegant — zero changes to orchestrator internals, the model swap is entirely in the factory layer.
- The existing SSE queue infrastructure (`transport/agui/endpoint.py`) needed no changes.
- `deploy.sh` made iterative VPS rebuilds fast once the pattern was established.

## What Didn't Go Well

- Several hours spent on infrastructure debugging (Safari polyfill, Caddy routing, inode binding) before reaching the core feature work.
- The `cloud.yaml` model key mismatch was silent — the factory fell back to local inference with no error, making the issue hard to spot.

## Surprises

- Docker bind mounts track inodes, not paths. A `git pull` that replaces a file breaks the mount until the container is restarted.
- LiteLLM's env var naming is `ANTHROPIC_API_KEY` regardless of what prefix your app uses — always pass explicitly when using a custom prefix.
- Safari's secure context policy rejects `crypto.randomUUID()` even on private IPs served over plain HTTP, despite Chrome allowing it.

---

## Artifacts Created / Modified

| Type | File | Description |
|------|------|-------------|
| Code | `src/personal_agent/service/app.py` | `/chat/stream` endpoint + background pipeline coroutine |
| Code | `src/personal_agent/config/profile.py` | `_current_profile` ContextVar + get/set helpers |
| Code | `src/personal_agent/llm_client/factory.py` | Profile-aware model key resolution |
| Code | `src/personal_agent/llm_client/litellm_client.py` | Explicit API key pass-through |
| Code | `src/personal_agent/gateway/chat_api.py` | Thin cloud chat endpoint (stopgap) |
| Code | `src/personal_agent/gateway/app.py` | Include chat + transport routers |
| Code | `seshat-pwa/src/lib/uuid.ts` | `crypto.randomUUID()` polyfill |
| Code | `seshat-pwa/src/lib/agui-client.ts` | Updated endpoint to `/chat/stream` |
| Config | `config/profiles/cloud.yaml` | Fixed model keys |
| Config | `config/models.yaml` | Added `claude_haiku` |
| Config | `config/models.cloud.yaml` | Added `claude_haiku` |
| Config | `config/cloud-sim/Caddyfile` | Added `/chat/stream` to backend path |
| Infra | `Dockerfile.gateway` | Switch to `service/app.py:app` |
| Infra | `docker-compose.cloud.yml` | Healthcheck, mem_limit, start_period |
| Doc | `docs/guides/CLOUD_DEPLOYMENT.md` | New: full cloud deployment guide |
| Doc | `docs/guides/AGUI_STREAMING_REFERENCE.md` | New: AG-UI streaming architecture |
| Doc | `docs/architecture_decisions/ADR-0051-cloud-profile-dispatch.md` | New: ADR for this change |

---

## Next Session

### Prerequisites

- [ ] Verify full conversation continuity (multi-turn) on cloud profile
- [ ] Check cost tracking works with LiteLLM (CostTrackerService records)

### Proposed Goal

Evaluate cloud profile quality against local profile on the same prompt set. Capture cost-per-conversation data.

### Proposed Batches

1. **Evaluation harness**: Run 10 prompts against cloud + local profiles, capture response quality and latency.
2. **Cost tracking verification**: Confirm `CostTrackerService` records LiteLLM calls correctly in PostgreSQL.
3. **Streaming improvement**: Replace single `TextDeltaEvent` (full response at once) with token-level streaming from LiteLLM.

---

## References

- **ADR-0044**: Execution profile system
- **ADR-0046**: AG-UI transport protocol
- **ADR-0048**: Mobile/multi-device UI
- **ADR-0051**: Cloud profile orchestrator dispatch (new, this session)
- **Commits**: `d7360e0`, `c5cbfd0`, `49216a5`, `cfe0c77`, `f7566e8`, `f86a435`, `0bc8441`, `12b30b4`
