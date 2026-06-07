# FRE-519 — Sub-agent captures REST read surface (gateway observations API)

**Issue:** FRE-519 (Approved, Tier-2:Sonnet, project *Observability Foundation*)
**Refs:** FRE-505 (write path — `SubAgentCapture` → ES `agent-captains-captures-subagents-*`, merged PR #179), FRE-514 (route-trace read surface — pattern to mirror, merged PR #177), ADR-0074
**Branch:** `worktree-build` → PR (build stops at PR)

## Scope

Expose the per-sub-agent `SubAgentCapture` records (FRE-505) over the Seshat gateway observations API so a decomposition turn's N sub-agents are fetchable by `trace_id` in one call (PWA-consumable, not raw ES). Read-only; no new writes.

## Endpoints (owner steer: full FRE-514 parity — 3 routes)

- `GET /observations/sub-agents/recent` — recent-N captures (newest first); optional `failed_only: bool=False` (the audit analog of FRE-514's boundary filters — surfaces empty-digest/errored subs).
- `GET /observations/sub-agents/session/{session_id}` — a session's captures, newest first.
- `GET /observations/sub-agents/{trace_id}` — the N captures for one turn.

**Route order matters:** declare `/recent` and `/session/{session_id}` BEFORE `/{trace_id}` (mirror `route_trace_api.py`), else the parametrised route swallows the fixed paths.

## Design decisions

1. **Dedicated router module** `gateway/sub_agent_capture_api.py`, prefix `/observations/sub-agents`. Mirrors `route_trace_api.py`'s dedicated `/observations/route-traces/*` module (the established FRE-514 precedent) rather than overloading `observation_api.py`.
2. **Source = ES** (not Postgres): query `app.state.es_client` against the **settings-driven** index pattern `f"{SUBAGENT_CAPTURES_INDEX_PREFIX}-*"` (imported from `captains_log.capture`) so FRE-375 test isolation holds. The closer sibling is `observation_api.py` (ES `/observations/*`), not the ledger.
3. **Empty ≠ 404.** Most turns don't decompose, so "no sub-agents for this trace" is a valid `200` with `{"sub_agents": [], "count": 0}` — a definitive answer to "did this turn decompose?". (404 would be wrong semantics here, unlike route-trace's single-row lookup.)
4. **trace_id is opaque** (term match, no UUID parse) — consistent with `observation_api.get_observation`; captures store the raw parent `trace_id` string.
5. Reuse FRE-514/observation conventions: `observations:read` scope, `get_rate_limiter().check(token)` (as the ES sibling does), `_MAX_LIMIT=200` size cap, `SystemTraceContext` request trace, structured log, 503 on ES-down / query failure.
6. Sort `[{"timestamp":"asc"},{"task_id":"asc"}]` — dispatch order, deterministic tiebreak (ticket said "sort by task_id"; timestamp-then-task_id is strictly more useful and still deterministic).

## Response shape
```json
{"trace_id": "<tid>", "count": 2, "sub_agents": [<SubAgentCapture _source + _id>, ...]}
```

## Atomic steps (TDD)

### Step 1 — failing tests (`tests/personal_agent/gateway/test_sub_agent_capture_api.py`)
Mirror `test_route_trace_api.py` harness: `create_gateway_router()` on a bare `FastAPI`, `TestClient`, set `app.state.es_client = AsyncMock()` with `.search` returning a hits envelope. Auth disabled by the package `conftest.py`.
- `test_returns_sub_agents_for_trace` — es.search → 2 hits → 200, `count==2`, `sub_agents[0]["task_id"]` + `injected_digest` present; assert index arg endswith `-subagents-*`.
- `test_empty_when_no_subagents` — 0 hits → 200, `count==0`, `sub_agents==[]` (NOT 404).
- `test_session_endpoint_returns_rows` — `/session/{sid}` → 200, list; assert `term session_id` in the query body.
- `test_recent_endpoint_and_failed_only` — `/recent` → 200 list; `/recent?failed_only=true` adds a `term success:false` clause.
- `test_recent_limit_clamped` — `?limit=9999` → es `size` ≤ 200; `?limit=0` → 400.
- `test_503_when_es_unavailable` — `app.state.es_client=None` → 503 (each route).
- `test_503_on_es_error` — `.search` raises → 503.
- Verify: `make test-file FILE=tests/personal_agent/gateway/test_sub_agent_capture_api.py` (red first).

### Step 2 — implement `src/personal_agent/gateway/sub_agent_capture_api.py`
- `router = APIRouter(prefix="/observations/sub-agents", tags=["observations"])`, `_MAX_LIMIT=200`.
- helpers: local `_get_es(request)` (503 if missing), `_flatten_hit`, `_clamp_limit` (400 if <1, clamp to `_MAX_LIMIT`) — mirror observation_api / route_trace_api.
- `_search(es, query) -> list[dict]` wrapping `es.search(index=f"{SUBAGENT_CAPTURES_INDEX_PREFIX}-*", body=query)` → 503 on exception (single error path for all 3 routes).
- **Declare `/recent` then `/session/{session_id}` then `/{trace_id}`** (fixed-before-parametrised). All `Depends(require_scope("observations:read"))` + `get_rate_limiter().check(token)` + `SystemTraceContext` + structured `log.info`.
  - `/recent`: `match_all` (+ `term success:false` when `failed_only`), sort `@`→ `[{"timestamp":"desc"},{"task_id":"asc"}]`, size clamped.
  - `/session/{session_id}`: `term session_id`, sort `timestamp desc`, size clamped; opaque string (no UUID parse — captures store strings).
  - `/{trace_id}`: `term trace_id`, sort `[{"timestamp":"asc"},{"task_id":"asc"}]`, size `_MAX_LIMIT`; returns `{trace_id, count, sub_agents}`.
- Google docstrings; modern type hints; `dict[str, Any]` for the ES envelope (as existing files).

### Step 3 — wire router (`src/personal_agent/gateway/app.py`)
- Import `from personal_agent.gateway.sub_agent_capture_api import router as sub_agent_capture_router`.
- `root.include_router(sub_agent_capture_router)` alongside the others (after `route_trace_router`).
- Verify: `make test-file FILE=tests/personal_agent/gateway/test_sub_agent_capture_api.py` (green).

### Step 4 — quality gates
`make test` (gateway module, then full) · `make mypy` · `make ruff-check` + `make ruff-format` · `pre-commit run --all-files`.

### Step 5 — docs
Module docstring documents the endpoint + index; note the read surface in the FRE-505 capture module docstring if useful. No MASTER_PLAN/CLAUDE.md edits (master's role).

## Out of scope
- No `session/` or `recent` endpoints (ticket scope is the per-turn read). No PWA UI wiring. No writes. No new ES index.

## Verify (ticket AC)
`GET /api/v1/observations/sub-agents/{trace_id}` returns the N sub-agent records for a decomposition turn (full input-context breakdown + output + injected digest), reconstructable in one call; empty `200` for non-decomposed turns; `observations:read`-scoped.
