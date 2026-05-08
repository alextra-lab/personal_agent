# FRE-214 Track 2a — Model Endpoint Abstraction Implementation Plan

> **Status**: Draft — written 2026-05-08, **execution deferred** until owner signals (post-backlog reduction).
> **Parent**: [FRE-214 audit](../../architecture/2026-05-08-fre-214-vps-topology-audit.md), [ADR-0045 amendment](../../architecture_decisions/ADR-0045-infrastructure-cloud-knowledge-layer.md).
> **Tier**: 2 (Sonnet — implementation from plan).
> **Branch when executed**: new branch off `main` (e.g. `fre-214-2a-endpoint-abstraction`).
> **Scope guard**: this plan touches only the model registry + endpoint resolution + call sites that read `model_def.endpoint`. No compose changes. No PWA changes. No new compose services. **Track 2b** lands on top of this.

---

## Context

The FRE-214 audit ratified full-harness-on-VPS as the canonical topology and identified a hidden landmine for Track 2b: when the laptop containerizes the gateway, a naive merge of `models.yaml` + `models.cloud.yaml` would route the laptop's containerized harness through Cloudflare back to itself (`slm.frenchforet.com`) just to reach a model running on the same machine.

The mechanism in audit §8 — a single model registry with **ordered candidate endpoints + first-reachable resolution** — eliminates `models.cloud.yaml` entirely and makes endpoint selection a property of *what's reachable*, not *which env file we loaded*. This is the structural change that unblocks Track 2b's compose unification without compromising the "laptop must remain self-contained" requirement (audit §8.1).

This plan ships the abstraction in isolation. Nothing changes about which compose runs where; nothing changes about which models are configured. What changes is the shape of one Pydantic model and the introduction of a small resolver module.

---

## Design decisions (made; do not defer during execution)

1. **Backward compatibility**: existing `endpoint: <url>` (singular) field is preserved. When loaded, a model with only `endpoint:` set is normalized to `endpoints: [endpoint]` internally. This means cloud-only model entries (Anthropic, OpenAI, etc.) and local entries that don't need multi-candidate ordering work without YAML edits.
2. **Resolver scope**: only `provider_type == "local"` models go through the resolver. Cloud models (Anthropic, OpenAI, etc.) are dispatched via LiteLLM, which has its own routing. The resolver returns immediately for cloud models with whatever single endpoint they have (typically none).
3. **Probe mechanism**: synchronous TCP connect via `socket.create_connection((host, port), timeout=probe_timeout_seconds)`. Not an HTTP probe — TCP is sufficient to know the service is listening and saves us from caring about content-type / health-endpoint conventions per backend.
4. **Caching**: process-lifetime cache, keyed by `(model_key, hash_of_endpoints_tuple)`. Cleared on `clear_endpoint_cache()` (used by tests). No TTL — if an endpoint goes down mid-session the next call gets the cached value, fails, and we accept that. A re-probe API exists for tests but is not wired to runtime retry; that complexity is deferred until pain demands it.
5. **Failure semantics**: if no candidate is reachable, raise `EndpointResolutionError` with the full attempted-endpoints list and the per-endpoint failure reason. Calling code does not retry; it surfaces as a clear startup error.
6. **Probe timeout default**: 250 ms per candidate. Configurable per-model via `probe_timeout_ms`. Total worst-case probe time per role = `probe_timeout_ms × len(endpoints)`; with 4 candidates and 250 ms default = 1 s. Acceptable for startup; the cache means we pay it once per process.
7. **Probe timing**: lazy on first call (not eager at app startup). Reasoning: startup-time probing creates a chicken-and-egg with services that are still initialising in the same compose stack; the first real call happens after compose has finished `depends_on healthcheck` cycles.
8. **No async probe**: synchronous, blocking. Called from async contexts via the cache (cache hit = no I/O). Cold start in an async context costs a thread-blocking 250 ms × N — acceptable; avoids the complexity of re-entrant async probe in code paths that aren't always async.

---

## Phase 1 — Schema changes to `ModelDefinition`

**File**: `src/personal_agent/llm_client/models.py`

**Goal**: add `endpoints: list[str] | None`, `resolve: Literal["first_reachable", "static"] | None`, `probe_timeout_ms: int`. Preserve existing `endpoint: str | None` for backward compat. Add a model validator that normalizes singular → plural at load time.

### Edits

Add three new fields to `ModelDefinition` (place them next to the existing `endpoint` field around line 92):

```python
endpoints: list[str] | None = Field(
    None,
    description=(
        "Ordered list of candidate endpoint URLs. The endpoint resolver "
        "(personal_agent.llm_client.endpoint_resolver) probes them in order "
        "and uses the first reachable. Mutually exclusive with the legacy "
        "'endpoint' field. Only used for provider_type == 'local'."
    ),
)
resolve: str | None = Field(
    None,
    description=(
        "Resolution strategy. 'first_reachable' = probe candidates in order, "
        "use first that accepts a TCP connect. 'static' = use endpoints[0] "
        "without probing. Defaults to 'first_reachable' when endpoints[] has "
        "more than one entry; 'static' otherwise."
    ),
)
probe_timeout_ms: int = Field(
    default=250,
    ge=10,
    le=5000,
    description="Per-candidate TCP-connect timeout in milliseconds.",
)
```

Add a `@model_validator(mode="after")` that normalizes singular → plural and validates the strategy value:

```python
@model_validator(mode="after")
def _normalize_endpoint_fields(self) -> "ModelDefinition":
    """Normalize endpoint/endpoints fields and default the resolve strategy.

    - If both `endpoint` and `endpoints` are set: error.
    - If only `endpoint` is set: copy to endpoints=[endpoint].
    - If only `endpoints` is set: leave endpoint=None.
    - If neither is set: leave both None (cloud models, or local using
      settings.llm_base_url default).
    - Default `resolve` to 'first_reachable' when endpoints has > 1 entry,
      'static' otherwise. Validate the value if explicitly set.
    """
    if self.endpoint is not None and self.endpoints is not None:
        raise ValueError(
            "ModelDefinition: 'endpoint' and 'endpoints' are mutually exclusive. "
            f"Got endpoint={self.endpoint!r} and endpoints={self.endpoints!r}."
        )
    if self.endpoint is not None and self.endpoints is None:
        self.endpoints = [self.endpoint]

    if self.resolve is None:
        self.resolve = "first_reachable" if self.endpoints and len(self.endpoints) > 1 else "static"
    elif self.resolve not in ("first_reachable", "static"):
        raise ValueError(
            f"ModelDefinition: 'resolve' must be 'first_reachable' or 'static', got {self.resolve!r}."
        )

    return self
```

### Schema test

**File** (new): `tests/test_llm_client/test_model_definition_endpoints.py`

Cover six cases:
1. Only `endpoint:` set → `endpoints == [endpoint]`, `resolve == "static"`.
2. Only `endpoints:` (single entry) → `resolve == "static"`.
3. Only `endpoints:` (multiple entries) → `resolve == "first_reachable"`.
4. Both `endpoint:` and `endpoints:` set → ValidationError.
5. `resolve: "first_reachable"` set explicitly → preserved.
6. `resolve: "garbage"` → ValidationError.

```bash
uv run pytest tests/test_llm_client/test_model_definition_endpoints.py -v
# Expected: 6 passed
```

---

## Phase 2 — Endpoint resolver module

**File** (new): `src/personal_agent/llm_client/endpoint_resolver.py`

```python
"""Endpoint resolver — picks the first reachable candidate from a model's endpoints[].

Replaces the old "one endpoint per model per env file" pattern with one model
registry + ordered candidate endpoints + first-reachable resolution. Solves the
laptop-tunnels-to-itself trap when the gateway containerizes (FRE-214 §8).

Probe mechanism: TCP connect with timeout. Synchronous; cached for process
lifetime. No HTTP probe — service-listening is enough.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from threading import Lock
from urllib.parse import urlparse

import structlog

from personal_agent.llm_client.models import ModelDefinition

log = structlog.get_logger(__name__)


class EndpointResolutionError(RuntimeError):
    """Raised when no endpoint candidate is reachable for a local model."""


@dataclass(frozen=True)
class _CacheKey:
    model_key: str
    endpoints_hash: tuple[str, ...]  # hashable tuple


_cache: dict[_CacheKey, str] = {}
_cache_lock = Lock()


def _probe_tcp(url: str, timeout_seconds: float) -> tuple[bool, str | None]:
    """Return (reachable, error_reason). TCP connect only — no HTTP."""
    parsed = urlparse(url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not host:
        return False, f"no host in url {url!r}"
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            return True, None
    except (OSError, socket.timeout) as exc:
        return False, f"{type(exc).__name__}: {exc}"


def resolve_endpoint(model_key: str, model_def: ModelDefinition) -> str:
    """Return the first reachable endpoint for a local model.

    Caches per (model_key, endpoints_tuple). Process-lifetime; clear via
    `clear_endpoint_cache()` for tests.

    Raises:
        EndpointResolutionError: if no candidate is reachable.
        ValueError: if called with a model that has no endpoints[] (e.g. cloud
            model — caller must check provider_type first).
    """
    if not model_def.endpoints:
        raise ValueError(
            f"resolve_endpoint called for model {model_key!r} with no endpoints[]; "
            f"caller must dispatch cloud models via LiteLLM and not invoke the resolver."
        )

    cache_key = _CacheKey(model_key=model_key, endpoints_hash=tuple(model_def.endpoints))
    with _cache_lock:
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached

    timeout_seconds = model_def.probe_timeout_ms / 1000.0

    if model_def.resolve == "static":
        winner = model_def.endpoints[0]
        with _cache_lock:
            _cache[cache_key] = winner
        log.info("endpoint_resolved", model_key=model_key, endpoint=winner, strategy="static")
        return winner

    failures: list[tuple[str, str]] = []
    for candidate in model_def.endpoints:
        ok, reason = _probe_tcp(candidate, timeout_seconds)
        if ok:
            with _cache_lock:
                _cache[cache_key] = candidate
            log.info(
                "endpoint_resolved",
                model_key=model_key,
                endpoint=candidate,
                strategy="first_reachable",
                tried=len(failures) + 1,
            )
            return candidate
        failures.append((candidate, reason or "unknown"))

    log.error("endpoint_resolution_failed", model_key=model_key, failures=failures)
    raise EndpointResolutionError(
        f"No reachable endpoint for model {model_key!r}. Tried: " + ", ".join(
            f"{url} ({reason})" for url, reason in failures
        )
    )


def clear_endpoint_cache() -> None:
    """Clear the resolution cache. Tests use this; runtime does not."""
    with _cache_lock:
        _cache.clear()
```

### Resolver tests

**File** (new): `tests/test_llm_client/test_endpoint_resolver.py`

Six cases, all using `monkeypatch` on `socket.create_connection`:

1. `static` strategy returns endpoints[0] without probing.
2. `first_reachable` returns first when first probe succeeds (no probes after).
3. `first_reachable` skips first when first fails, returns second.
4. All candidates fail → `EndpointResolutionError` with all failures listed.
5. Cache hit on second call (probe count is 1 across two calls).
6. `clear_endpoint_cache()` causes re-probe.

```bash
uv run pytest tests/test_llm_client/test_endpoint_resolver.py -v
# Expected: 6 passed
```

---

## Phase 3 — Wire resolver into call sites

Five files currently read `model_def.endpoint` directly. Each gets a one-line change.

### 3.1 `src/personal_agent/memory/embeddings.py:49`

```python
# Before
endpoint = model_def.endpoint or "http://localhost:8503/v1"

# After
from personal_agent.llm_client.endpoint_resolver import resolve_endpoint
endpoint = resolve_endpoint("embedding", model_def)
```

The `or "http://localhost:8503/v1"` fallback was always wrong (silently accepted absent config). After this change the resolver raises `EndpointResolutionError` if nothing is configured — louder, correct.

### 3.2 `src/personal_agent/memory/reranker.py:56`

Same shape as 3.1.

```python
endpoint = resolve_endpoint("reranker", model_def)
```

### 3.3 `src/personal_agent/llm_client/dspy_adapter.py:135`

```python
# Before
effective_base_url = base_url or model_def.endpoint or settings.llm_base_url

# After
from personal_agent.llm_client.endpoint_resolver import resolve_endpoint
if base_url is not None:
    effective_base_url = base_url
elif model_def.endpoints:
    effective_base_url = resolve_endpoint(<model_key>, model_def)
else:
    effective_base_url = settings.llm_base_url
```

`<model_key>` — DSPy adapter does not currently know its model key; thread it through the call site or reuse `model_def.id` as a stable proxy (verify in execution that adapter callers pass it).

### 3.4 `src/personal_agent/llm_client/client.py:103, 603`

`LocalLLMClient` reads `model_def.endpoint` in two places to derive a per-model override URL. Both become resolver calls. The model key is available at the call site (it's the role name being executed).

### 3.5 Update all five call sites' tests

Tests currently mock `model_def.endpoint = "http://test"`. Update to set `model_def.endpoints = ["http://test"]` and patch `endpoint_resolver._probe_tcp` to return `(True, None)`. Use a shared fixture in `tests/conftest.py`:

```python
@pytest.fixture
def reachable_endpoints(monkeypatch):
    """Make all endpoints appear reachable; clear resolver cache."""
    from personal_agent.llm_client import endpoint_resolver
    monkeypatch.setattr(endpoint_resolver, "_probe_tcp", lambda url, t: (True, None))
    endpoint_resolver.clear_endpoint_cache()
    yield
    endpoint_resolver.clear_endpoint_cache()
```

```bash
# Verify all existing tests still pass after Phase 3 edits
uv run pytest tests/test_llm_client tests/test_memory -q
# Expected: green (no new failures, no new skips)
```

---

## Phase 4 — Config consolidation

### 4.1 Rewrite `config/models.yaml`

For the four local roles (`primary`, `sub_agent`, `embedding`, `reranker`), replace the singular `endpoint:` with the multi-candidate `endpoints:` list per audit §8.3. Cloud models (Anthropic, OpenAI) are unchanged — they have no `endpoints:` field.

Example (primary role):

```yaml
primary:
  id: "qwen3.6-35b-a3b"
  provider_type: "local"
  endpoints:
    - http://localhost:8000/v1            # laptop native dev (slm_server / MLX on host)
    - http://host.docker.internal:8000/v1 # laptop containerized → host MLX
    - https://slm.frenchforet.com/v1      # remote tunnel (last resort, used by VPS local profile)
  probe_timeout_ms: 250
  context_length: 32768
  # … (rest unchanged: max_concurrency, default_timeout, quantization, …)
```

Same shape for `sub_agent`, `embedding` (paths `8503`), `reranker` (paths `8504`). Embedding and reranker append the `slm.frenchforet.com/embedding/v1` and `/reranker/v1` paths per audit §8.3.

### 4.2 Delete `config/models.cloud.yaml`

```bash
rm config/models.cloud.yaml
git add -A
```

### 4.3 Remove `AGENT_MODEL_CONFIG_PATH` override on cloud

**File**: `docker-compose.cloud.yml:301`

```yaml
# Before
AGENT_MODEL_CONFIG_PATH: /app/config/models.cloud.yaml

# After
# (deleted — both deployments now read config/models.yaml)
```

### 4.4 Clean up `.env.example`

Remove the cloud-specific override block:

- Line 248: `# AGENT_MODEL_CONFIG_PATH=config/models.yaml` → keep (still valid as default)
- Line 578: `# AGENT_MODEL_CONFIG_PATH=/app/config/models.cloud.yaml` → delete this line and the surrounding "VPS-specific" comment block.

### 4.5 Sanity check

```bash
# Confirm models.cloud.yaml is gone everywhere
grep -rn "models.cloud.yaml" . --include="*.py" --include="*.yaml" --include="*.yml" --include="*.example" 2>/dev/null
# Expected: no matches

# Confirm AGENT_MODEL_CONFIG_PATH no longer overridden in compose
grep -n "AGENT_MODEL_CONFIG_PATH" docker-compose*.yml
# Expected: no matches (or only commented-out reference in .env.example default)
```

---

## Phase 5 — Verification & rollout

### 5.1 Local laptop (no containerization yet — Track 2b lands that)

```bash
make up                              # datastores up
make dev                             # uvicorn --reload, native gateway
# Expected log entries on first request:
#   endpoint_resolved model_key=primary endpoint=http://localhost:8000/v1 strategy=first_reachable tried=1
#   endpoint_resolved model_key=embedding endpoint=http://localhost:8503/v1 strategy=first_reachable tried=1
```

Verify the laptop never tries `slm.frenchforet.com` (would indicate localhost probe wrongly failing):

```bash
# In one terminal
make logs SERVICE=seshat-gateway 2>&1 | grep endpoint_resolved
# In another, send a request
uv run agent "smoke test"
```

Expected: no `slm.frenchforet.com` in resolved endpoints when MLX is healthy on localhost.

### 5.2 Negative test — laptop with SLM down

Stop `slm_server` on the host. Re-run a request. Expected:
- `endpoint_resolution_failed` log entry listing all four candidates with their failure reasons
- `EndpointResolutionError` surfaced to the caller (PWA renders an error card; CLI prints traceback)

This is the failure mode the owner asked for (audit §8.4) — laptop must work when self-contained, fail loudly when no local model is reachable rather than silently tunneling out.

### 5.3 VPS

```bash
ENV=cloud make deploy
# Expected on the gateway's first request:
#   endpoint_resolved model_key=primary endpoint=https://slm.frenchforet.com/v1 strategy=first_reachable tried=4
#   endpoint_resolved model_key=embedding endpoint=http://embeddings:8503/v1 strategy=first_reachable tried=3
#   endpoint_resolved model_key=reranker endpoint=http://reranker:8504/v1 strategy=first_reachable tried=3
```

`tried=N` confirms localhost / `host.docker.internal` candidates were probed and failed before the in-compose / tunnel candidate succeeded — the expected VPS resolution path.

### 5.4 Test suite

```bash
make test          # unit
# Expected: same baseline pass rate as before (no new failures, no new skips)

make ruff-check
make mypy
# Expected: clean
```

`make test-integration` is unaffected here — that's Track 3's territory.

---

## Rollback

This change is structurally additive (new fields, new module) plus one breaking change (deletion of `models.cloud.yaml`). Rollback in one commit:

```bash
git revert <track-2a-commit-sha>
# Restores models.cloud.yaml + the AGENT_MODEL_CONFIG_PATH override.
# Schema fields revert; existing models.yaml entries with endpoints[] would
# now fail validation, so revert before the next gateway restart on VPS.
```

If a partial rollback is needed (e.g. resolver works but one call site is wrong): the resolver itself is harmless — leave it in place, revert just the offending call site.

---

## Out of scope (do not pull in)

* Compose unification (Track 2b).
* `requires_llm_server` rename / probe behavior in tests (Track 3 / FRE-336).
* MLX-vs-llama.cpp embedding parity test (Track 3).
* Tunnel-mode laptop dev (Track 2b deliverable iii).
* PWA runtime config (Track 4 D-3 ticket).

---

## Done means

1. `config/models.cloud.yaml` no longer exists.
2. `AGENT_MODEL_CONFIG_PATH` is no longer overridden anywhere.
3. `make test` passes; `make mypy` clean; `make ruff-check` clean.
4. Laptop request resolves to `http://localhost:8000/v1` (or `host.docker.internal` once 2b lands).
5. VPS request resolves to `embeddings:8503` / `reranker:8504` / `slm.frenchforet.com/v1` per role.
6. Stopping `slm_server` on laptop produces a loud `EndpointResolutionError`, never a silent tunnel-out.

---

*End of plan. Execution gated on owner trigger; do not start until backlog reduction is complete (per audit §8.7).*
