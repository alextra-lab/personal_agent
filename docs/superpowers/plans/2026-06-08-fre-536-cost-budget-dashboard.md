# FRE-536 (C1) — Cost & Budget Dashboard (cost_gate / ADR-0065)

> **Date:** 2026-06-08 · **Ticket:** FRE-536 (Tier-2:Sonnet) · **Project:** Telemetry Surface Audit
> **Refs:** ADR-0065 (cost gate) · A1 (FRE-533 reconciliation) · A2 (FRE-534 templates, Done) · B1 (FRE-535 dashboard triage) · FRE-546 (prompt-cost-cache import bug, related)
> **Scope decision (owner, 2026-06-08):** *Fix emit first, then full dashboard.* cost_gate dollar fields are emitted via `str()` → keyword; we correct the emit + mapping, then build the dashboard on sound numeric fields.

---

## Problem (verified against emit sites + A1 table + ES handler)

`cost_gate/gate.py` emits its money fields as `str(Decimal)`:
- `cost_gate_reserved`: `amount=str(amount)`
- `cost_gate_committed`: `actual_cost=str(...)`, `reserved=str(...)`, `delta=str(...)`
- `cost_gate_refunded`: `amount=str(amount)`
- denial: `litellm_request_budget_denied` (litellm_client.py): `reservation_amount=str(...)`

**Root cause:** the values are `Decimal`. `telemetry/es_handler.py` does `json.dumps(value)`; a `Decimal` raises `TypeError` → the handler falls back to `str(value)`. So even without the explicit `str()`, a `Decimal` lands as a string. A1 confirms all four fields map as `keyword` (text+keyword default) → **not numerically aggregatable**.

**Fix:** emit `float(amount)` (JSON-serializable number) under **namespaced `*_usd` names** (collision-free, self-describing, matches the `cost_usd` convention), and add explicit `double` props to `index-template.json`.

**Sound numeric spend source already in ES:** `model_call_completed.cost_usd` → `double` (explicit ✅). Budget-role attribution comes from the cost_gate events' `role` field (= `budget_role`, e.g. `main_inference`).

**Out of scope / follow-up:** *cap utilization vs configured caps* — `running_total`/`cap_usd` live only in Postgres `budget_counters`; Kibana can't read Postgres. Requires a new periodic snapshot emitter → **file follow-up ticket**, do NOT fake it.

---

## Field rename map (emit-site fix)

| Event | Old key (str→keyword) | New key (float→double) |
|---|---|---|
| `cost_gate_reserved` | `amount` | `amount_usd` |
| `cost_gate_committed` | `actual_cost` / `reserved` / `delta` | `actual_cost_usd` / `reserved_usd` / `delta_usd` |
| `cost_gate_refunded` | `amount` | `amount_usd` |
| `litellm_request_budget_denied` | `reservation_amount` | `reservation_amount_usd` |

`reservation_id` stays `str()` (UUID → keyword ✅, correct). `cap_count` stays int (long ✅). `cost=cost` (litellm_commit_failed, already float) left unchanged.

Renaming (not reusing) avoids a same-name type conflict in existing indices → **no destructive reindex**; historical string fields are orphaned (acceptable — they were never aggregatable). The reindex/rollover note documents this.

---

## Steps (atomic, TDD)

### 1 — Emit-site fix + unit test (TDD)
- **Test first:** `tests/personal_agent/cost_gate/test_gate_emit_types.py` — capture structlog events (use `structlog.testing.capture_logs` or caplog), assert `amount_usd`/`actual_cost_usd`/`reserved_usd`/`delta_usd` are `float` instances (not `str`). Confirm it fails.
  - Verify command: `make test-file FILE=tests/personal_agent/cost_gate/test_gate_emit_types.py` → fails on `str` vs `float`.
- **Implement:** edit `src/personal_agent/cost_gate/gate.py` lines 221-228 (`cost_gate_reserved`), 318-325 (`cost_gate_committed`), 412-417 (`cost_gate_refunded`): rename keys + `float(...)`. Edit `src/personal_agent/llm_client/litellm_client.py:472-479` denial event: `reservation_amount_usd=float(reservation_amount)`.
  - Verify: same test passes.

### 2 — ES template explicit mappings
- Edit `docker/elasticsearch/index-template.json` `properties`: add
  `amount_usd`, `actual_cost_usd`, `reserved_usd`, `delta_usd`, `reservation_amount_usd` → `{ "type": "double" }`.
- Mirror into `scripts/setup-elasticsearch.sh` if it inlines the template (FRE-534 pattern — check it reads the file vs inlines).
- Extend `tests/scripts/test_es_templates.py` (added by FRE-534) to assert the 5 new props exist and are `double`.
  - Verify: `make test-file FILE=tests/scripts/test_es_templates.py` passes.

### 3 — Apply template to local ES + live verification (mappings-first discipline)
- `./scripts/setup-elasticsearch.sh` (re-applies templates).
- **HARD STEP (not advisory — per codex review):** templates apply only to **newly created** indices. The current `agent-logs-<date>` index already exists with its prior mapping, so a `_field_caps` check against it will show the OLD/absent mapping and look like a false failure. **Force a rollover** (or in dev, write into a fresh index) so the explicit `double` template governs the index the new `*_usd` values land in. Confirm the new write target is governed by the updated template before sampling.
- Emit one sample of each event into the fresh index, then **query live ES** to confirm each `*_usd` field is `double` and aggregatable:
  - `curl -s 'localhost:9200/agent-logs-*/_field_caps?fields=amount_usd,actual_cost_usd,reserved_usd,delta_usd,reservation_amount_usd'` → every entry `"type":"double"`, no stray `keyword` sibling on the fresh index.
  - Confirm `model_call_completed.cost_usd` is `double`.
  - **Verify `role` explicitly (codex catch):** `role` is not an explicit property — it falls under the `*_role` dynamic rule → `keyword`. Confirm via `_field_caps?fields=role,event_type` that both are `keyword` and aggregate as bare `role` / `event_type` (use `role`, **not** `role.keyword` — A1 trap) before wiring the terms panels.
- **Gate:** no panel is wired to a field until this `_field_caps` check confirms its type. (Standing rule: mappings wrong first pass — verify every field live.)

> Emit-site confirmation (codex flagged as unverified): `litellm_request_budget_denied` is a `log.warning` in the `except BudgetDenied:` block at `src/personal_agent/llm_client/litellm_client.py:472-479`; `reservation_amount` is emitted there → rename to `reservation_amount_usd`. Confirmed present.

### 4 — Build dashboard NDJSON
- New file `config/kibana/dashboards/cost_budget.ndjson`, modeled on `system_health.ndjson` / `request_timing.ndjson` structure (FRE-535-corrected). Use the shared `agent-logs-*` index-pattern (id `agent-logs-pattern`) via a `references` entry — do NOT bundle a duplicate index-pattern (A1 dedupe lesson).
- Panels (all on verified-typed fields):
  1. **Actual spend over time** — date_histogram × `sum(actual_cost_usd)`, filter `event_type:"cost_gate_committed"`.
  2. **Spend by budget role** — terms on `role` × `sum(actual_cost_usd)`, filter committed.
  3. **Reserve→commit→refund funnel + refund rate** — count by `event_type` over `cost_gate_reserved|committed|refunded`; refund rate = refunded/reserved.
  4. **Settlement delta** — `sum(delta_usd)` (committed; over-estimate refunded, typically ≤0) and `sum(amount_usd)` (refunded = released reservations).
  5. **Budget-denial events** — count of `litellm_request_budget_denied` over time + terms on `budget_role` (top denied).
  6. **Cost per turn / session** — `model_call_completed.cost_usd` avg + p50/p95 by `trace_id`/`session_id`.
- Register in `config/kibana/import_dashboards.sh` `FILES=(...)`.
  - Verify: `./config/kibana/import_dashboards.sh` → `OK cost_budget.ndjson` (no `"errors"`).

### 5 — Populate + verify panels render
- Drive a few real turns (or replay) so committed/reserved/denied events exist; open Kibana, confirm every panel populates (no silent-empty / wrong-agg). Capture the verification in the research doc.
- Optionally extend `scripts/audit/verify_fre535_panels.py` pattern (FRE-535) with a `verify_fre536_panels.py` that asserts each panel's field ref resolves to a mapped, correctly-typed field.

### 6 — Reindex/rollover note + research doc
- `docs/research/2026-06-08-fre-536-cost-budget-dashboard.md`: emit-fix rationale, field rename map, the `str→keyword` root cause, live `_field_caps` evidence, panel→field table, and the rollover note (historical string fields orphaned; dollar panels populate deploy-forward).

### 7 — Follow-up ticket (Needs Approval, Telemetry Surface Audit project)
- **Cap-utilization snapshot emitter:** periodic task logs `budget_counters` (`role`, `time_window`, `running_total`, `cap_usd`, `utilization_ratio`) to ES as a `budget_counter_snapshot` event with explicit `double` mappings → unblocks the cap-utilization panel. Tier-2:Sonnet.

---

## Quality gates (all before PR)
`make test-file FILE=tests/personal_agent/cost_gate/test_gate_emit_types.py` · `make test-file FILE=tests/scripts/test_es_templates.py` · `make test` (cost_gate module then full) · `make mypy` · `make ruff-check` + `make ruff-format` · `pre-commit run --all-files`.

## Acceptance (FRE-536)
- [ ] Emit fix: money fields land as `double` (live `_field_caps` proof).
- [ ] Dashboard live on local Kibana, all panels populated against real data.
- [ ] Every field verified mapped at correct type (no silent-empty / wrong-agg).
- [ ] Exported to version-controlled NDJSON + registered in import script.
- [ ] Reindex/rollover note recorded.
- [ ] Cap-utilization follow-up filed (Needs Approval).

## Halt conditions
- Live `_field_caps` shows a `*_usd` field still `keyword` (template not applied / name collision) → stop, do not wire the panel.
- A field needed by a panel isn't soundly typed and isn't covered by the emit fix → kick to a follow-up, don't dynamic-map.
