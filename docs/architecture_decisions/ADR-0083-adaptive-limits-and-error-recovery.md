# ADR-0083 — Adaptive Limits & Error Recovery: Layer 3 SLM Health Observability

| Field | Value |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-06-02 |
| **Author** | FRE-399 |
| **Implements** | FRE-399 Layer 3 |
| **Related** | ADR-0076 (constraint governance), FRE-389, FRE-391, FRE-411 |
| **Ticket** | [FRE-399](https://linear.app/frenchforest/issue/FRE-399) |

---

## Context

FRE-399 opened a tracking thread on "adaptive limits & error recovery" motivated by a
Cloudflare **524** (Mac SLM origin timeout ~251s) that killed a whole turn with N×
wasted re-generation (trace `73efd74a`).

### Root cause — already resolved

The 524 was caused by the SLM's streaming response **lacking**
`Cache-Control: no-cache` + `Content-Type: text/event-stream`. Cloudflare
re-buffered the stream and re-applied its ~100s origin timeout — even though
`LocalLLMClient` was already streaming (`stream=True`). Adding those headers to
the slm_server fixed it end-to-end (**493s → 91s, one generation, zero real
errors**). This is documented in the MASTER_PLAN (commit `cbd6f45`, 2026-05-28).
Raising gateway/model timeouts would not have helped; the ceiling was upstream.

### What remains genuinely open

1. **Layer 3 — cross-tunnel SLM observability (THIS ADR):** in the VPS↔Mac split
   the gateway sees the SLM as a black box. A failure returns "an error occurred"
   with no indication of whether the Mac is asleep, the GPU is pinned, the model
   isn't loaded, or the queue is saturated. This is the real gap — Four-Level
   Observability Level 1, extended across the tunnel.

2. **Layer 2 — genuine-failure recovery (DEFERRED):** a cost-gated local→cloud
   fallback via the shipped FRE-389 `DecisionCard` (`transient_inference_failure`
   constraint). Now that spurious 524s are gone, genuine failures are low-frequency
   and each fallback spends real money. Design sketched below; implementation is a
   preference-gated child ticket.

3. **Dynamic thresholds (DEFERRED):** adaptive timeouts / context-aware `max_tokens`
   overlap with unshipped **FRE-391**; coordinated there.

---

## Decision

### Layer 3: gateway-side SLM health monitor

Ship a **gracefully-degrading gateway-side monitor** now, before the Mac-side rich
endpoint exists. The monitor works with today's liveness-only `/health` response
(rich fields → `None`) and automatically populates when the Mac-side enrichment
child ticket lands.

#### `SlmHealthSnapshot` (frozen Pydantic)

```python
class SlmHealthSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)
    status: Literal["up", "degraded", "down"]
    reachable: bool
    model_loaded: bool | None = None
    gpu_util_pct: float | None = None
    vram_used_mb: float | None = None
    vram_total_mb: float | None = None
    queue_depth: int | None = None
    latency_ema_ms: float | None = None
    model_id: str | None = None
    probe_latency_ms: float | None = None
    probed_at: datetime
    trace_id: str
    error: str | None = None
    kind: str = "system:slm_health_probe"
```

**Degraded vs down:**
- `down` = not reachable (HTTP error, timeout, 403).
- `degraded` = reachable but `model_loaded is False`, or `gpu_util_pct ≥ 95%`, or `queue_depth ≥ 4`.
- `up` = reachable and all thresholds OK (or rich fields absent).

Thresholds are settings-configurable (`AGENT_SLM_GPU_UTIL_DEGRADED_PCT`,
`AGENT_SLM_QUEUE_DEPTH_DEGRADED`).

#### Module layout: `observability/slm_health/`

Mirrors the **wired** joinability monitor (`observability/joinability/`):

| File | Role |
|---|---|
| `snapshot.py` | Frozen `SlmHealthSnapshot` + `degrade_reason()` |
| `probe.py` | `probe_slm_health(url, cf_headers, …) → SlmHealthSnapshot` — never raises |
| `sink.py` | `write_result(es, snapshot, prefix)` → `<prefix>-YYYY.MM.DD` |
| `cache.py` | Process-global latest snapshot + TTL check (no lock needed on CPython) |
| `scheduler_runner.py` | `run_scheduled_slm_health_probe(*, es_client)` — best-effort entry point |
| `__init__.py` | Public exports |

#### Brainstem scheduler wiring

Added after the joinability probe block in `brainstem/scheduler.py` — interval-gated at 5 min (`AGENT_SLM_HEALTH_PROBE_INTERVAL_SECONDS=300`), same `try/except → log.warning` shape.

#### Enriched `/api/inference/status`

`inference_status` in `service/app.py` now calls `probe_slm_health` and returns:
- Backward-compatible keys: `status`, `profile`, `local`, `latency_ms` (FRE-421 PWA pill unchanged).
- New optional keys: `gpu_util_pct`, `queue_depth`, `model_loaded`, `degrade_reason` (all `None` today; filled once Mac enrichment lands).
- CF Access headers factored into a shared `_cf_access_headers()` helper (removes the duplication between the old inline implementation and `client.py`).

#### Executor error-reason hint

On a transient `LLMClientError` (non-rate-limit, non-budget-denied), the executor
error path reads `get_cached_snapshot(ttl=…)` (no new network call) and, when the
snapshot is `degraded`/`down`, appends `degrade_reason()` to the classified error
reason string. Example: `"Inference timed out [GPU pinned (98.3%)]"`. Best-effort —
any exception in the hint path is swallowed; the FAILED path is never impaired.

#### Settings added (all `AGENT_` prefixed)

| Setting | Default | Description |
|---|---|---|
| `AGENT_SLM_HEALTH_URL` | `https://slm.example.com/health` | SLM health endpoint URL (shared by probe + endpoint) |
| `AGENT_SLM_HEALTH_PROBE_ENABLED` | `true` | Master switch |
| `AGENT_SLM_HEALTH_PROBE_INTERVAL_SECONDS` | `300` | Probe cadence (seconds) |
| `AGENT_SLM_HEALTH_INDEX_PREFIX` | `agent-monitors-slm-health` | ES index prefix |
| `AGENT_SLM_HEALTH_CACHE_TTL_SECONDS` | `45` | Cache freshness window |
| `AGENT_SLM_GPU_UTIL_DEGRADED_PCT` | `95.0` | GPU degraded threshold |
| `AGENT_SLM_QUEUE_DEPTH_DEGRADED` | `4` | Queue depth degraded threshold |

---

### Layer 2 (deferred): genuine-failure cloud fallback — design sketch

When a local call fails with a transient error after retries (today's `TaskState.FAILED`),
offer a cost-gated choice via the shipped ADR-0076 `DecisionCard`:

```
constraint: "transient_inference_failure"
options: ["retry_local", "fallback_cloud", "stop_here"]  # stop_here = safe default
```

`stop_here` preserves today's behavior. `fallback_cloud` re-issues the call via
`get_llm_client_for_key(settings.llm_cloud_fallback_model_key, budget_role="main_inference")`;
cost-gate enforces the cap automatically. The preference table
(`user_constraint_preferences`) stores a remembered choice.

**Implementation gate:** create a child Linear ticket (Needs Approval). Build only
after a genuine-failure trace is observed in production to justify the complexity
and spend.

### Layer 3 child: Mac-side `/health` enrichment (separate repo)

The Mac SLM server (separate repo, MLX/Apple Silicon) should expose structured fields
matching the `SlmHealthSnapshot` contract:

```json
{
  "model_loaded": true,
  "gpu_util_pct": 62.4,
  "vram_used_mb": 18432,
  "vram_total_mb": 24576,
  "queue_depth": 0,
  "latency_ema_ms": 310.2,
  "model_id": "qwen3-14b-q6_k"
}
```

The gateway probe is already forward-compatible — absent keys stay `None` without
breaking the snapshot. Modelled on FRE-411 (`slm-requests-*` telemetry interface
contract, separate CC).

---

## Consequences

### Positive

- Gateway now has a live, ES-trended view of SLM health — first real signal from
  Four-Level Observability Level 1 across the tunnel.
- Error messages improve immediately (from "an error occurred" → "GPU pinned (98%)")
  even before the Mac-side child ships, via the `down` snapshot on a dead endpoint.
- `/api/inference/status` gains `degraded` as a third state — the PWA pill can show
  "local model busy" without a code change (it just needs to handle the new value).
- No breaking change to existing callers — all backward-compatible keys preserved.
- Deferred Layer 2 avoids complexity and spend until a real-failure trace justifies it.

### Negative / risks

- The probe runs every 5 min, adding 1 HTTP call per interval across the CF tunnel.
  At `timeout_s=3.0` this is negligible; CF Access headers are already used on every
  inference call.
- Rich fields stay `None` until the Mac-side child ships — the monitor is "wired but
  quiet" for GPU/VRAM signals.
- Layer 2 cloud fallback deferred — genuine failures (Mac asleep / OOM) still kill
  the turn with FRE-398 partial salvage only. Acceptable given low post-fix frequency.

---

## Alternatives considered

| Option | Decision |
|---|---|
| Build Layer 2 first (cloud fallback) | Rejected — root cause was the header fix; fallback is now low-frequency and expensive. |
| Poll the SLM on every turn | Rejected — 3s timeout per turn is too high; cadence probe + cache is the right pattern. |
| Use brainstem MetricsDaemon | Rejected — MetricsDaemon senses the *agent host* (VPS); SLM is on a remote Mac; different topology. |
| Copy the cache_erosion monitor pattern | Rejected — that module is orphaned (defined but never wired); the joinability monitor is the correct template. |
