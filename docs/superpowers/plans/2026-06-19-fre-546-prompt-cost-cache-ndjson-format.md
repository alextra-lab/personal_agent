# FRE-546 — Fix stale Kibana saved-object format for `prompt-cost-cache.ndjson`

- **Ticket:** FRE-546 (Approved, Tier-2:Sonnet, project: Telemetry Surface Audit)
- **Option chosen by owner:** Option 2 — transform the NDJSON to the modern saved-object format.
- **Refs:** FRE-535 (B1 triage + hardened `import_dashboards.sh` + canonical `agent-logs-pattern` dedupe), FRE-406 (origin dashboard), ADR-0078 (prompt identity), ADR-0090 (telemetry surface audit).

## Problem (verified)

`config/kibana/dashboards/prompt-cost-cache.ndjson` is the **only** dashboard file in
an older Kibana export format. The strict `dynamic:strict` `.kibana` mapping rejects it:

- top-level `migrationVersion` (object form `{type: ver}`) on all 4 objects →
  `strict_dynamic_mapping_exception: dynamic introduction of [migrationVersion]`
- `attributes.references` nested inside the 2 `lens` objects →
  `strict_dynamic_mapping_exception: dynamic introduction of [references] within [lens]`

All 17 other dashboards use the modern envelope: `typeMigrationVersion` (string),
references at the top-level envelope only, and the shared `agent-logs-pattern` index-pattern id.

## Owner verification ask ("verify the code produces the data the dashboard visualizes") — DONE

Verified live against `agent-logs-*` (cloud-sim ES :9200) before planning:

| Field | Emitted by | Mapping | Live |
|---|---|---|---|
| `input_tokens` | `llm_client/telemetry.py` `emit_model_call_completed` | `long` | populated |
| `cache_read_tokens` | same | `long` | populated |
| `cost_usd` | same (via `extra=`) | `double` | populated |
| `prompt_callsite` | same (`PromptIdentity.callsite`) | `keyword` | populated |
| `prompt_static_prefix_hash` | same | `keyword` | populated |

`model_call_completed` docs in `agent-logs-*`: 8,538 total, 1,808 with `prompt_static_prefix_hash`.
Index target matches the production cache-erosion monitor (`observability/cache_erosion/monitor.py`,
`agent-logs-*`). FRE-536 (`cost_budget.ndjson`) has **zero overlap** (budget mechanics, not
prompt-identity/cache-erosion), so subsuming would lose this view — confirms Option 2.

## The transform (exact, per-object)

**Codex review finding (incorporated):** the original plan kept the *sparse*
`prompt-cost-cache-data-view` object but renamed its id to `agent-logs-pattern`. Because
`import_dashboards.sh` uses `overwrite=true`, that sparse object would **clobber the rich canonical
`agent-logs-pattern`** (loaded first from `data_views.ndjson`), dropping its `fieldAttrs` / formats.
Fixed below: ship a *verbatim copy* of the canonical object instead — overwrite becomes
canonical→canonical (no loss), matching the self-include convention of every other dashboard
(traversal_gate, cost_budget all self-define `agent-logs-pattern`).

1. **Index-pattern object:** delete the sparse `prompt-cost-cache-data-view` object and replace it
   with a verbatim copy of the canonical `agent-logs-pattern` index-pattern object taken from
   `config/kibana/dashboards/data_views.ndjson`. (Already modern envelope — no further edit.)
2. **Lens objects (×2):**
   - delete `attributes.references` (top-level `references` already carries the identical ref;
     this nested copy is what strict Lens mapping rejects).
   - repoint top-level `references[].id` from `prompt-cost-cache-data-view` → `agent-logs-pattern`
     (keep `name: "indexpattern-datasource-layer-layer1"` unchanged — Lens resolves the ref by name).
   - replace `migrationVersion: {"lens": "8.9.0"}` → `typeMigrationVersion: "8.9.0"` (string).
3. **Dashboard object:** replace `migrationVersion: {"dashboard": "8.9.0"}` →
   `typeMigrationVersion: "8.9.0"`. Panel refs already point at the 2 lens ids — unchanged.

Nothing else changes (panel state, titles, queries, filters preserved verbatim).

## Steps

1. **TDD — failing test first.** Create `tests/scripts/test_prompt_cost_cache_dashboard.py`
   mirroring `tests/scripts/test_traversal_gate_dashboard.py`. Assertions:
   - file parses as NDJSON; exactly 1 dashboard + 2 lens + 1 index-pattern.
   - **no object has top-level `migrationVersion`** (the FRE-546 trap).
   - **no `lens` object has `attributes.references`** (the FRE-546 trap).
   - the only index-pattern id is the canonical `agent-logs-pattern`; every lens references it;
     **the index-pattern object is byte-identical to the canonical copy in `data_views.ndjson`**
     (guards against re-introducing a sparse data-view that would clobber canonical on overwrite).
   - dashboard `panelsJSON` refs match `references` and resolve to the 2 lens ids.
   - data-backing guard: the panels' agg `sourceField`s are exactly the verified-live set
     (`prompt_callsite`, `input_tokens`, `cache_read_tokens`, `cost_usd`, `prompt_static_prefix_hash`, `@timestamp`).
   - registered in `import_dashboards.sh`.
   - **Verify it FAILS** against the current file: `make test-file FILE=tests/scripts/test_prompt_cost_cache_dashboard.py` → migrationVersion + attributes.references + index-pattern-id assertions fail.

2. **Apply the transform** to `config/kibana/dashboards/prompt-cost-cache.ndjson` (hand-edit the 4
   lines per the rules above — deterministic, no script committed).

3. **Verify the test PASSES:** `make test-file FILE=tests/scripts/test_prompt_cost_cache_dashboard.py`.

4. **Confirm parity with a known-good file** (sanity): the transformed file's envelope keysets are a
   subset of `traversal_gate.ndjson`'s, no `migrationVersion`, no `attributes.references`.

## Quality gates

- `make test-file FILE=tests/scripts/test_prompt_cost_cache_dashboard.py` (module) then `make test` (full).
- `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.

## Out of scope / handoff to master (post-deploy runbook, in Linear comment)

- **Live import** is master's job: `KIBANA_URL=<cloud-sim kibana> ./config/kibana/import_dashboards.sh`
  → expect `OK prompt-cost-cache.ndjson` (was the lone `FAIL`). This is the true acceptance proof;
  the build session proves format-correctness statically only (no Kibana write from build).
- **Render smoke (master, per Codex Q4):** open the "Prompt Cost & Cache Attribution (FRE-406)"
  dashboard, set a time range covering the data (last 30d), confirm both panels render non-empty.
  Static tests cannot catch version-specific migrations / time-range / data-view existence.
- Expected live result: the 2 panels render non-empty (data verified present above).

## Not doing

- Option 1 (re-export): not feasible — nothing loaded to export from.
- Option 3 (subsume/retire): rejected — no overlap with FRE-536; would lose the cache-erosion view.
