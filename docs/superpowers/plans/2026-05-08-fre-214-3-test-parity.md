# FRE-214 Track 3 — Test Parity Plan (FRE-336 + Embedding-Runtime Parity)

> **Status**: Draft — written 2026-05-08, **execution deferred** until owner signals (post-backlog reduction).
> **Parent**: [FRE-214 audit](../../architecture/2026-05-08-fre-214-vps-topology-audit.md), [ADR-0045 amendment](../../architecture_decisions/ADR-0045-infrastructure-cloud-knowledge-layer.md). Closes [FRE-336](https://linear.app/frenchforest/issue/FRE-336).
> **Blocked by**: Track 2a (endpoint abstraction). Track 2b not strictly required, but the parity verification is more useful after compose unification.
> **Tier**: 1 → 2. The marker rename + probe rewrite has design decisions (Tier-1 to lock); the migration of existing test files is mechanical (Tier-2).
> **Branch when executed**: `fre-214-3-test-parity` off `main` (after Track 2a merges).

---

## Context

The FRE-214 audit's deviation D-7 surfaced FRE-336: integration tests skip silently on the VPS because `tests/conftest.py:_llm_server_reachable()` probes the static `primary` role from `models.yaml` and ignores the active profile. On the VPS, `primary` is Qwen at `slm.frenchforet.com` — when the laptop is offline, the probe fails and *every* `@pytest.mark.requires_llm_server` test silently skips, even though `AGENT_ANTHROPIC_API_KEY` is set and the cloud profile would happily run those tests.

The fix is two-layered:

1. **Reachability-driven LLM marker** that probes whichever model the *active profile* would actually use, not a static role lookup. Skip is loud (collection-time error message), not silent. Most tests get the new `requires_llm` marker; a small number that genuinely require the local Qwen primary keep an explicit `requires_local_llm`.
2. **Embedding-runtime parity test** — the cosine-≥-0.999 verification promised in audit §8.3 + ADR-0045 amendment "Embedding consistency — revised". Runs only when both runtimes (MLX on host, llama.cpp container) are reachable; skips loudly elsewhere with a clear "to run this, expose both endpoints" message.

This is the test-side of the convenience/testability/consistency triad the owner named at audit §7.1.

---

## Design decisions (made; do not defer during execution)

1. **Two markers, not one**:
   * `@pytest.mark.requires_llm` — test needs *any reachable LLM*. Probe checks the active profile's primary model. Used by orchestration tests, multi-turn flows, second-brain extraction, consolidation. Most existing `requires_llm_server` usages migrate here.
   * `@pytest.mark.requires_local_llm` — test needs specifically the local Qwen primary (e.g. testing Qwen-specific tool-calling behavior). Probe checks the static `primary` role from `models.yaml`. Smaller set.
2. **Profile-awareness via existing `resolve_model_key`**: the active profile's primary model key is resolved using `personal_agent.config.profile.resolve_model_key("primary")` — the same call site `factory.py` uses. No new API.
3. **Endpoint reachability uses Track 2a's resolver**: for local models, `endpoint_resolver.resolve_endpoint(...)` returns first-reachable; if it raises `EndpointResolutionError`, the marker reports a loud skip. For cloud models, the probe is "is the API key set?" (current behavior, unchanged).
4. **Loud skip semantics**: when `requires_llm` causes a skip, the reason is printed to stderr at collection time (not just buried in the per-test skip message). This is the "no silent skips on VPS" behavior FRE-336 demands.
5. **Embedding parity as a pytest test with a dedicated marker**: `@pytest.mark.requires_dual_embedding_endpoints`. Reads `PARITY_EMBEDDING_ENDPOINT_A` and `PARITY_EMBEDDING_ENDPOINT_B` from env; auto-skips (loud) if either is missing or unreachable. Invoked manually for now: `PARITY_EMBEDDING_ENDPOINT_A=http://localhost:8503/v1 PARITY_EMBEDDING_ENDPOINT_B=http://localhost:18503/v1 uv run pytest tests/test_parity/`.
6. **CI on both shapes is out of scope for this track**. Adding a CI workflow that runs integration tests on both laptop and VPS is a separate concern — file as a follow-up ticket once the marker rewrite has settled. Track 3 closes FRE-336; the CI extension is FRE-336-followup.
7. **Caching, race conditions**: marker probe runs once per pytest collection phase, cached. `pytest --cache-clear` re-probes. Same model as today's `_LLM_SERVER_RESULT` — no architectural change.
8. **Backward compat**: keep `@pytest.mark.requires_llm_server` working for one release as an alias of `requires_llm` (collection-time deprecation warning). Removed in the release after.

---

## Phase 1 — New marker logic in `tests/conftest.py`

**File**: `tests/conftest.py` (rewrite)

Replace `_llm_server_reachable()` with two probe functions and update `pytest_collection_modifyitems` to handle the three markers.

### 1.1 Probe functions

```python
def _probe_active_profile_llm() -> tuple[bool, str]:
    """Probe whatever LLM the active profile would dispatch for 'primary'.

    Profile-aware: uses resolve_model_key('primary') so cloud-profile sessions
    check Anthropic / OpenAI keys, local sessions check the local Qwen
    endpoint (via Track 2a's endpoint resolver).
    """
    try:
        from personal_agent.config import load_model_config, settings  # noqa: PLC0415
        from personal_agent.config.profile import resolve_model_key  # noqa: PLC0415
    except Exception as e:
        return False, f"Could not import config: {e}"

    try:
        model_config = load_model_config()
    except Exception as e:
        return False, f"models.yaml not loadable: {e}"

    resolved_key = resolve_model_key("primary")
    model_def = model_config.models.get(resolved_key)
    if model_def is None:
        return False, f"Model key {resolved_key!r} (active profile primary) not in models.yaml"

    if model_def.provider_type != "local":
        # Cloud model — check API key
        provider = (model_def.provider or "").lower()
        if provider == "anthropic":
            return (bool(settings.anthropic_api_key),
                    "AGENT_ANTHROPIC_API_KEY not set" if not settings.anthropic_api_key else "")
        if provider == "openai":
            return (bool(settings.openai_api_key),
                    "AGENT_OPENAI_API_KEY not set" if not settings.openai_api_key else "")
        return False, f"Unknown cloud provider {provider!r}"

    # Local model — use Track 2a's endpoint resolver
    from personal_agent.llm_client.endpoint_resolver import (  # noqa: PLC0415
        EndpointResolutionError,
        resolve_endpoint,
    )
    try:
        endpoint = resolve_endpoint(resolved_key, model_def)
        return True, f"local model resolved to {endpoint}"
    except EndpointResolutionError as e:
        return False, str(e)


def _probe_local_primary() -> tuple[bool, str]:
    """Probe the static 'primary' role from models.yaml — for tests that
    specifically require the local Qwen primary, not just any LLM."""
    try:
        from personal_agent.config import load_model_config  # noqa: PLC0415
    except Exception as e:
        return False, f"Could not import config: {e}"

    try:
        model_config = load_model_config()
    except Exception as e:
        return False, f"models.yaml not loadable: {e}"

    primary = model_config.models.get("primary")
    if primary is None or primary.provider_type != "local":
        return False, "No local 'primary' in models.yaml"

    from personal_agent.llm_client.endpoint_resolver import (  # noqa: PLC0415
        EndpointResolutionError,
        resolve_endpoint,
    )
    try:
        endpoint = resolve_endpoint("primary", primary)
        return True, f"local primary resolved to {endpoint}"
    except EndpointResolutionError as e:
        return False, str(e)
```

### 1.2 Collection hook with loud reporting

```python
_PROBE_CACHE: dict[str, tuple[bool, str]] = {}


def _cached(name: str, probe_fn) -> tuple[bool, str]:
    if name not in _PROBE_CACHE:
        _PROBE_CACHE[name] = probe_fn()
    return _PROBE_CACHE[name]


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    """Resolve LLM markers loudly. Two markers apply (plus the deprecated alias):

    - requires_llm        → active profile's primary model
    - requires_local_llm  → static 'primary' role from models.yaml
    - requires_llm_server → DEPRECATED alias of requires_llm
    """
    needs_active = any(item.get_closest_marker("requires_llm") for item in items)
    needs_legacy = any(item.get_closest_marker("requires_llm_server") for item in items)
    needs_local = any(item.get_closest_marker("requires_local_llm") for item in items)

    # Active-profile probe (covers requires_llm + the deprecated alias)
    if needs_active or needs_legacy:
        ok, reason = _cached("active", _probe_active_profile_llm)
        if not ok:
            # Loud — this is the no-silent-skip requirement (FRE-336).
            print(f"\n[requires_llm] SKIP — active profile LLM unreachable: {reason}\n", file=sys.stderr)
            skip_mark = pytest.mark.skip(reason=f"active profile LLM unreachable — {reason}")
            for item in items:
                if (item.get_closest_marker("requires_llm")
                        or item.get_closest_marker("requires_llm_server")):
                    item.add_marker(skip_mark)

        if needs_legacy:
            print(
                "\n[requires_llm_server] DEPRECATED — rename to @pytest.mark.requires_llm "
                "(or @pytest.mark.requires_local_llm if Qwen-specific). "
                "Removal target: next release after this one.\n",
                file=sys.stderr,
            )

    # Local-primary probe
    if needs_local:
        ok, reason = _cached("local", _probe_local_primary)
        if not ok:
            print(f"\n[requires_local_llm] SKIP — local Qwen primary unreachable: {reason}\n",
                  file=sys.stderr)
            skip_mark = pytest.mark.skip(reason=f"local primary unreachable — {reason}")
            for item in items:
                if item.get_closest_marker("requires_local_llm"):
                    item.add_marker(skip_mark)
```

### 1.3 Register the markers in `pyproject.toml`

```toml
[tool.pytest.ini_options]
markers = [
    "integration: mark test as integration test (requires live LLM server)",
    "requires_llm: test needs any reachable LLM via the active profile",
    "requires_local_llm: test needs the local Qwen primary specifically",
    "requires_llm_server: DEPRECATED — alias of requires_llm; remove after one release",
    "requires_dual_embedding_endpoints: test needs both MLX and llama.cpp embedding runtimes (manual run only)",
    "evaluation: large eval (100+ LLM calls)",
]
```

(Adjust to actual existing markers list — preserve them all.)

### 1.4 Tests for the conftest logic

**File** (new): `tests/test_conftest_markers.py`

Cover four cases via subprocess (each runs pytest in a sandbox dir):
1. `requires_llm` skips loudly when no active-profile LLM is reachable; stderr contains `[requires_llm] SKIP`.
2. `requires_llm` runs when active-profile LLM IS reachable.
3. `requires_local_llm` skips when static primary unreachable, regardless of active profile.
4. `requires_llm_server` (deprecated alias) emits a deprecation message and is treated as `requires_llm`.

```bash
uv run pytest tests/test_conftest_markers.py -v
# Expected: 4 passed
```

---

## Phase 2 — Migrate existing `requires_llm_server` usages

Six sites today (per `grep -rn "requires_llm_server" tests/`):

| File | Line | Decision |
|------|------|----------|
| `tests/test_second_brain/test_entity_extraction.py` | 11 | → `requires_llm` (entity extraction uses whatever the active profile resolves) |
| `tests/test_second_brain/test_consolidation_e2e.py` | 37 | → `requires_llm` |
| `tests/test_orchestrator/test_fre37_multi_turn_e2e.py` | 35, 79, 150 | → `requires_llm` (orchestration test, model-agnostic) |
| `tests/AGENTS.md` | 114 | → update example to use new marker |

If during migration any test is found to actually depend on Qwen-specific tool-calling behavior, mark `requires_local_llm` instead. Default decision is `requires_llm`.

```bash
# After migration, the legacy alias should be unused in the actual test files:
grep -rn "requires_llm_server" tests/ --include="*.py"
# Expected: no matches in test bodies; possibly one match in tests/test_conftest_markers.py
# that exercises the deprecation path
```

---

## Phase 3 — Embedding-runtime parity test

### 3.1 Test module

**File** (new): `tests/test_parity/test_embedding_runtime_parity.py`

```python
"""Cosine-similarity parity verification: MLX vs llama.cpp.

The two embedding runtimes serve the same model weights but on different
inference engines (MLX on Apple Silicon, llama.cpp elsewhere). The audit
§8.3 + ADR-0045 amendment "Embedding consistency — revised" requires
that the resulting vectors agree to cosine ≥ 0.999 on a fixed input set
before the multi-instance topology is considered safe.

This test runs only when both endpoints are explicitly provided via env:
    PARITY_EMBEDDING_ENDPOINT_A=http://localhost:8503/v1
    PARITY_EMBEDDING_ENDPOINT_B=http://localhost:18503/v1

Typical setup (laptop with SSH tunnel to VPS):
    ssh -L 18503:embeddings:8503 vps
    PARITY_EMBEDDING_ENDPOINT_A=http://localhost:8503/v1 \
    PARITY_EMBEDDING_ENDPOINT_B=http://localhost:18503/v1 \
        uv run pytest tests/test_parity/test_embedding_runtime_parity.py -v
"""

from __future__ import annotations

import os

import pytest

# Fixed input set — broad enough to surface format/quantization drift,
# small enough to run quickly. Mix English + code + emoji + long-tail.
PARITY_INPUTS = [
    "the quick brown fox jumps over the lazy dog",
    "Personal Agent uses Qwen3-Embedding-0.6B for semantic search",
    "def fibonacci(n: int) -> int: return n if n < 2 else fibonacci(n-1) + fibonacci(n-2)",
    "🌍🚀💡 emoji handling under different tokenizers",
    "À la recherche du temps perdu — Marcel Proust",
    "",  # empty string — both runtimes should produce a stable zero-or-pad vector
    "a" * 8000,  # near-context-limit; ensure both truncate the same way
]

COSINE_THRESHOLD = 0.999


def _cosine(a: list[float], b: list[float]) -> float:
    import math
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 1.0 if na == nb else 0.0
    return dot / (na * nb)


@pytest.mark.requires_dual_embedding_endpoints
@pytest.mark.parametrize("text", PARITY_INPUTS, ids=lambda s: f"len={len(s)}")
def test_mlx_vs_llamacpp_cosine(text: str) -> None:
    import httpx

    endpoint_a = os.environ["PARITY_EMBEDDING_ENDPOINT_A"]
    endpoint_b = os.environ["PARITY_EMBEDDING_ENDPOINT_B"]
    model_id = os.environ.get("PARITY_EMBEDDING_MODEL_ID", "qwen3-embedding-0.6b")

    payload = {"model": model_id, "input": text}

    with httpx.Client(timeout=30.0) as client:
        resp_a = client.post(f"{endpoint_a}/embeddings", json=payload)
        resp_a.raise_for_status()
        vec_a = resp_a.json()["data"][0]["embedding"]

        resp_b = client.post(f"{endpoint_b}/embeddings", json=payload)
        resp_b.raise_for_status()
        vec_b = resp_b.json()["data"][0]["embedding"]

    assert len(vec_a) == len(vec_b), (
        f"dimension mismatch: A={len(vec_a)} B={len(vec_b)} for input len={len(text)}"
    )
    cos = _cosine(vec_a, vec_b)
    assert cos >= COSINE_THRESHOLD, (
        f"parity drift on input len={len(text)}: cosine={cos:.6f} (threshold={COSINE_THRESHOLD})"
    )
```

### 3.2 Marker probe

Extend `tests/conftest.py` (`pytest_collection_modifyitems` from Phase 1) to handle `requires_dual_embedding_endpoints`:

```python
needs_dual = any(item.get_closest_marker("requires_dual_embedding_endpoints") for item in items)
if needs_dual:
    a = os.environ.get("PARITY_EMBEDDING_ENDPOINT_A")
    b = os.environ.get("PARITY_EMBEDDING_ENDPOINT_B")
    missing = []
    if not a: missing.append("PARITY_EMBEDDING_ENDPOINT_A")
    if not b: missing.append("PARITY_EMBEDDING_ENDPOINT_B")

    if missing:
        msg = f"set {' and '.join(missing)} to enable embedding-runtime parity tests"
        print(f"\n[requires_dual_embedding_endpoints] SKIP — {msg}\n", file=sys.stderr)
        skip_mark = pytest.mark.skip(reason=msg)
        for item in items:
            if item.get_closest_marker("requires_dual_embedding_endpoints"):
                item.add_marker(skip_mark)
    # If both vars are set, let the test run; httpx errors will surface as test failures.
```

### 3.3 Make target for convenience

```makefile
# Verify embedding-runtime parity (MLX on host vs llama.cpp container).
# Requires both endpoints reachable; typical setup is a laptop with an SSH
# tunnel forwarding the VPS embedding container to localhost:18503.
verify-embedding-parity:
	@[ -n "$$PARITY_EMBEDDING_ENDPOINT_A" ] || { echo "set PARITY_EMBEDDING_ENDPOINT_A"; exit 1; }
	@[ -n "$$PARITY_EMBEDDING_ENDPOINT_B" ] || { echo "set PARITY_EMBEDDING_ENDPOINT_B"; exit 1; }
	@uv run pytest tests/test_parity/test_embedding_runtime_parity.py -v
```

---

## Phase 4 — Documentation

### 4.1 Update `tests/AGENTS.md`

The marker example currently uses `@pytest.mark.requires_llm_server`. Update to:

```markdown
## LLM markers

- `@pytest.mark.requires_llm` — test needs any reachable LLM via the active
  profile. Use this for orchestration / multi-turn / extraction tests that
  don't depend on a specific model.
- `@pytest.mark.requires_local_llm` — test needs the local Qwen primary
  specifically (e.g. testing Qwen-specific tool-calling behavior).
- `@pytest.mark.requires_llm_server` — DEPRECATED, alias of `requires_llm`.
  Will be removed after one release; rename to `requires_llm`.

The marker probe is profile-aware: when the cloud profile is active, only
the cloud provider's API key is checked. When local profile is active, the
local Qwen endpoint is probed via the endpoint resolver.

Skips are LOUD — at collection time stderr gets a `[requires_llm] SKIP …`
line with the unreachability reason. No silent skips.
```

### 4.2 New runbook: `docs/guides/test-parity.md`

Short doc covering:
* The two LLM markers and when to use which.
* How to run the embedding-runtime parity test (env vars + `make verify-embedding-parity`).
* Why the parity test is verification rather than continuous CI (resource topology).
* What to do if cosine drops below 0.999 (file a regression ticket; investigate runtime updates, quantization changes, or model file integrity).

---

## Phase 5 — Verification

### 5.1 Laptop, native dev (local profile active)

```bash
make up && make dev &
make test-integration
# Expected: tests run against local Qwen via localhost endpoint.
# stderr shows: [requires_llm] resolved to http://localhost:8000/v1
```

### 5.2 Laptop with no SLM running (negative test)

```bash
# stop slm_server
pkill -f slm_server || true
make test-integration
# Expected: stderr shows [requires_llm] SKIP — active profile LLM unreachable: ...
# Per-test skip with the same reason.
# Critically: NOT silent — the message is printed at collection time, visible.
```

### 5.3 VPS (cloud profile active)

```bash
ENV=cloud make test-integration  # would need to be run via SSH on VPS
# Expected: stderr shows [requires_llm] resolved to claude_sonnet (provider=anthropic)
# Tests run; cloud profile dispatches via LiteLLM.
```

### 5.4 VPS with no Anthropic key

```bash
unset AGENT_ANTHROPIC_API_KEY
make test-integration
# Expected: [requires_llm] SKIP — AGENT_ANTHROPIC_API_KEY not set
```

### 5.5 Embedding parity (manual)

```bash
# On laptop, with VPS embedding port forwarded:
ssh -L 18503:embeddings:8503 -N -f $VPS_SSH_HOST
PARITY_EMBEDDING_ENDPOINT_A=http://localhost:8503/v1 \
PARITY_EMBEDDING_ENDPOINT_B=http://localhost:18503/v1 \
    make verify-embedding-parity
# Expected: 7 passed (one per PARITY_INPUTS row); each cosine ≥ 0.999.
```

### 5.6 Test suite

```bash
make test                     # unit
make ruff-check && make mypy  # clean
# Expected: green; new tests/test_conftest_markers.py + tests/test_parity/ both pass
```

---

## Rollback

Conftest changes are reversible by checkout:

```bash
git revert <track-3-commit-sha>
# Restores the old _llm_server_reachable() and the requires_llm_server-only marker.
```

If only the parity test breaks: leave the marker rewrite in place (it's a
clean improvement); revert just `tests/test_parity/`.

---

## Out of scope

* CI workflow on both shapes (laptop + VPS) — file as `FRE-336-followup` after this lands.
* New integration tests — this is meta-test infrastructure, not new test coverage.
* Profile-aware fixtures beyond the marker probe (e.g. `cloud_only_test_fixture`) — separate concern; revisit if specific tests demand it.
* Backend resume-from-offset support for SSE (FRE-236 Phase 2 — independent ticket).

---

## Done means

1. `@pytest.mark.requires_llm` exists; probes the active profile's primary model via the endpoint resolver.
2. `@pytest.mark.requires_local_llm` exists; probes the static `primary` role.
3. `@pytest.mark.requires_llm_server` still works (deprecated alias) with collection-time deprecation warning.
4. All existing `requires_llm_server` usages in `tests/test_*` are migrated to one of the two new markers.
5. Skips are loud — stderr gets a clear `[marker] SKIP — reason` line at collection time.
6. `tests/test_parity/test_embedding_runtime_parity.py` exists; auto-skips when env vars absent; verifies cosine ≥ 0.999 across 7 fixed inputs when env vars set.
7. `make verify-embedding-parity` works.
8. `tests/AGENTS.md` and `docs/guides/test-parity.md` document the new markers + parity verification.
9. `make test` + `make ruff-check` + `make mypy` clean.
10. **The original FRE-336 symptom is gone**: running `make test-integration` on the VPS no longer skips silently; it either runs against Anthropic (loud), runs against local Qwen via tunnel (loud), or skips with a clear stderr message naming the missing prerequisite.

---

*End of plan. Execution gated on owner trigger; do not start until backlog reduction is complete and Track 2a has merged (per audit §8.6 / §8.7).*
