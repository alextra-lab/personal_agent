# FRE-514 — Route-trace ledger: REST read surface

**Ticket:** FRE-514 (Approved, Tier-2:Sonnet, Observability Foundation)
**Refs:** FRE-452 (service-level ledger), FRE-206 (gateway observations surface),
ADR-0088 (execution-topology observability contract), ADR-0074 (identity)
**Branch:** `worktree-build` → PR

## Goal

Expose the FRE-452 route-trace ledger for read access over HTTP so a turn's
stimulus → model-path → result-type record is fetchable without direct SQL, on the
Seshat gateway observations API surface. Row shape stays as-is (seam-neutral DTO).

## Scope (3 endpoints + 2 new ledger read methods)

1. `GET /api/v1/observations/route-traces/{trace_id}` — single row (wraps the existing
   `RouteTraceLedger.get_by_trace_id`).
2. `GET /api/v1/observations/route-traces/session/{session_id}?limit=` — list-by-session,
   newest first (uses the existing `idx_route_traces_session_id` + `created_at DESC`).
3. `GET /api/v1/observations/route-traces/recent?limit=&label_lie=&fallback_triggered=&not_reconciled=`
   — recent-N with the three optional deterministic-shell boundary filters.

All require the `observations:read` scope (same convention as the sibling ES
`/observations/*` endpoints — these are system observability rows, not user content;
the table has no `user_id`, so no per-user ownership check, consistent with FRE-206).

### Filter semantics (deterministic SQL, documented as *candidate* heuristics)

- `fallback_triggered=true` → `WHERE fallback_triggered = TRUE` (exact column).
- `not_reconciled=true` → `WHERE cost_reconciled = FALSE` (exact column).
- `label_lie=true` → the gateway-declared expansion plan disagrees with what
  orchestration actually did (the "lying gateway label" gap, FRE-452 commit msg):
  ```sql
  (
    (decomposition_strategy IS NOT NULL AND decomposition_strategy <> 'single'
         AND orchestration_event = 'primary_handled')
    OR
    (decomposition_strategy = 'single'
         AND orchestration_event IN
             ('delegate_called','delegate_result_used','delegate_result_discarded'))
  )
  ```
  Kept orthogonal to `fallback_triggered` (fallback has its own value
  `orchestration_event='fallback_triggered'`, so it never overlaps these clauses).
  Multiple filters compose with `AND`.

## Design decisions (for owner sign-off)

- **D1 — Ledger resolution:** endpoints resolve the process-wide singleton via
  `get_route_trace_ledger()` (its own asyncpg pool, connected by the main-service
  lifespan in local-mount mode). `pool is None` → 503. Tests patch the accessor.
  *Also add* `connect()`/`disconnect()` of the singleton to the standalone
  `_gateway_lifespan` so a standalone `:9001` gateway serves these too. **No change to
  `service/app.py`** (singleton already connected there since FRE-452).
- **D2 — New file** `gateway/route_trace_api.py` (PG-backed) kept separate from
  `observation_api.py` (ES-backed), both under the `/observations` surface. Distinct
  3+-segment paths avoid the `/observations/{trace_id}` single-segment catch-all; within
  the router `/recent` and `/session/...` are declared before `/{trace_id}`.
- **D3 — Response shape:** serialize the frozen `RouteTraceRow` via
  `fastapi.encoders.jsonable_encoder(dataclasses.asdict(row))` — single source of truth
  (the DTO), UUIDs/datetimes handled. No duplicated 40-field Pydantic mirror.
- **D4 — `label_lie` predicate** is a deterministic *candidate* filter, not an
  authoritative classifier (matches the ticket's "label-lie candidates" wording).
  Codex note: the `delegate_result_used/discarded` arms can't fire yet (classifier emits
  only the programmatic floor), so today the predicate effectively flags
  declared-expansion-but-primary-handled — accepted as a forward-compatible candidate.

## Codex review fixes (folded in)

- **Server-side limit clamp:** module constant `_MAX_LIMIT = 200`; every endpoint does
  `min(limit, _MAX_LIMIT)` and rejects `limit < 1` → `400 invalid_parameter`. Add an
  over-limit test asserting the bound is applied.
- **Bad-UUID status (no 422/500 leak):** path params typed `str` and parsed with
  `UUID(...)` in the handler. `/{trace_id}` bad id → `404 not_found` (don't leak existence,
  matches feedback_api); `/session/{session_id}` bad id → `400 invalid_parameter`.
- **`connect()` idempotency:** add a guard in `RouteTraceLedger.connect()` —
  `if self.pool is not None: return` — so a double-connect (defensive, e.g. if mount
  topology changes) can't leak the first pool. Tiny, safe hardening of the FRE-452 method.
- **Serialization tradeoff acknowledged:** `jsonable_encoder(asdict(row))` keeps one
  source of truth but yields a generic OpenAPI schema (`list`/`object`); accepted — no
  `Decimal`/`Enum` traps (`_row_from_record` already casts to `float`; `orchestration_event`
  is a `str` Literal).

## Files

| File | Change |
|------|--------|
| `src/personal_agent/observability/route_trace/ledger.py` | + `list_by_session_id`, `list_recent` (with `_LABEL_LIE_SQL` module const) |
| `src/personal_agent/gateway/route_trace_api.py` | **new** router, 3 GET endpoints + `_serialize_row`, `_get_ledger` |
| `src/personal_agent/gateway/app.py` | include router in `create_gateway_router`; connect/disconnect singleton in `_gateway_lifespan` |
| `tests/observability/route_trace/test_ledger.py` | + unit tests for the 2 new methods (mocked `pool.fetch`) + integration filter round-trip |
| `tests/personal_agent/gateway/test_route_trace_api.py` | **new** endpoint tests (patched ledger): 200 single, 404 single, list-by-session, recent + each filter, 503 unconnected, 400/404 bad UUID |

## Steps (TDD)

1. **Ledger unit tests first** — add to `test_ledger.py`:
   `test_list_by_session_id_orders_desc_and_binds`, `test_list_by_session_empty_when_unconnected`,
   `test_list_recent_no_filters_sql`, `test_list_recent_label_lie_predicate`,
   `test_list_recent_combines_filters`, `test_list_recent_empty_when_unconnected`.
   Mock `pool.fetch` (AsyncMock) returning a list of dict-like records; assert SQL fragments
   + bound params. Confirm they fail: `make test-file FILE=tests/observability/route_trace/test_ledger.py`.
2. **Implement ledger methods** — `list_by_session_id(session_id, limit=50)` and
   `list_recent(*, limit=50, label_lie=False, fallback_triggered=False, not_reconciled=False)`;
   reuse `_row_from_record`; clamp nothing here (API clamps). Re-run → green.
3. **API endpoint tests first** — `test_route_trace_api.py` modeled on `test_knowledge_api.py`
   (`create_gateway_router`, `TestClient`); patch
   `personal_agent.gateway.route_trace_api.get_route_trace_ledger` to an `AsyncMock` ledger
   with `.pool` set. Cases listed in the Files table. Confirm fail.
4. **Implement `route_trace_api.py`** + register in `create_gateway_router` + standalone
   lifespan connect/disconnect. Re-run API + ledger tests → green.
5. **Identity threading (ADR-0074):** every new `log.*` carries `trace_id` (request-scoped
   `SystemTraceContext.new("route_trace_api")`, mirroring `observation_api`). No new
   `bus.publish` / Cypher.
6. **Docs:** docstrings on all new public functions; note the new endpoints + `label_lie`
   semantics in the `route_trace_api` module docstring. (No dedicated README for gateway
   endpoints exists; OpenAPI is the surface doc.)

## Quality gates (all before PR)

```
make test-file FILE=tests/observability/route_trace/test_ledger.py
make test-file FILE=tests/personal_agent/gateway/test_route_trace_api.py
make test            # full unit suite
make mypy
make ruff-check && make ruff-format
pre-commit run --all-files
```

## Out of scope (follow-ups if surfaced)

- `observe_topology` seam, live projector, `report_degradation` (rest of ADR-0088 spine;
  FRE-513/515).
- Pagination cursors / time-range filters on `recent` (only `limit` now).
- Authoritative label-lie classifier (this is the candidate-filter floor).
