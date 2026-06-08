# FRE-535 (B1) — Existing-dashboard triage: keep / retire / fix the 12 dashboards / 57 viz

> **Date:** 2026-06-08 · **Ticket:** FRE-535 (B1, Tier-2:Sonnet) · **Project:** Telemetry Surface Audit
> **Blocked-by (Done):** [FRE-533 (A1)](https://linear.app/frenchforest/issue/FRE-533) — reconciliation table merged (PR #193)
> **Source of truth:** `docs/research/2026-06-08-fre-533-telemetry-surface-reconciliation.md` + `…-reconciliation-table.csv`
> **Refs:** ADR-0074 · ADR-0083 · ADR-0088 · ADR-0089 · related FRE-540 (A3 CI checker), FRE-539 (C4)

---

## Scope (from the ticket + A1's routing table)

A1 routed two work-streams to B1:
1. **Fix the 14 broken Kibana panel field-refs** (9 `.keyword`-on-bare-keyword + 1 text-as-terms + 4 missing-field) — re-verified against live data, no silent-empty panels.
2. **Close the dashboard-provenance gap**: establish canonical export path, load the orphaned `prompt-cost-cache` dashboard, dedupe redundant index-patterns, record a keep/retire/fix decision for **all 12 dashboards / 57 viz**.

Out of scope (explicitly *not* B1): emit-site/code changes and template authoring (that's FRE-534/A2); new index-patterns + dashboards for joinability/slm-health/subagents (FRE-537/538/C*); the CI lint gate (FRE-540/A3).

---

## Ground truth (measured live, 2026-06-08, ES :9200 / Kibana :5601)

Every candidate fix was probed against live ES before planning (measure-don't-assert):

| Panel (dashboard) | current field-ref | live result | decision | target field | live buckets |
|---|---|---|---|---|---|
| LLM Call Count by Model (LLM Performance) | `model.keyword` | 0 buckets | **fix** | `model` | 5 |
| Avg Latency by Model Role (LLM Performance) | `role.keyword` | 0 buckets | **fix** | `role` ⟵ D1=role | 5 |
| LLM Latency Over Time (LLM Performance) | `role.keyword` | 0 | **fix** | `role` | 5 |
| P95 Latency by Role (LLM Performance) | `role.keyword` | 0 | **fix** | `role` | 5 |
| Avg Prompt Tokens by Model Role (LLM Performance) | `role.keyword` | 0 | **fix** | `role` | 5 |
| Prompt Token Percentiles by Role (LLM Performance) | `role.keyword` | 0 | **fix** | `role` | 5 |
| Avg Duration by Phase (Request Timing E2E) | `phase.keyword` | 0 | **fix** | `phase` | 5 |
| Request Phase Details (Request Timing E2E) | `phase.keyword` | 0 | **fix** | `phase` | 5 |
| State Transitions (System Health) | `from_state.keyword` | 0 | **fix** | `from_state` | 3 |
| Insight count by type (Insights Engine) | `insight_type` | `Fielddata disabled` (text) | **fix** | `insight_type.keyword` | 5 |
| Routing Decisions (Task Analytics) | `target_model.keyword` + filter `event_type:routing_decision` | **0 docs — `routing_decision` is a never-wired Day-11.5 emit constant** | **retire** (code-gap, not relic) | — → follow-up ticket | — |
| Rounds needed trend (Delegation Outcomes) | `rounds_needed` | 0 docs ever | **retire** | — | — |
| Delegation satisfaction distribution (Delegation Outcomes) | `user_satisfaction` | 0 docs ever | **retire** | — | — |
| Weekly proposals created (Insights Engine) | `proposals_created` | 0 docs ever | **retire** | — | — |

**Routing Decisions finding (owner-confirmed 2026-06-08):** `telemetry/events.py:111-114` defines `ROUTING_DECISION`/`ROUTING_DELEGATION`/`ROUTING_HANDLED`/`ROUTING_PARSE_ERROR` but **none is emitted** (grep: only the constant defs). Routing happens via `orchestrator/routing.py` `HeuristicRoutingPlan` (a Python object, not a logged event) + `executor.py:2123` `step_llm_call_gateway_model` (carries `model_role`/`task_type`, not `target_model`). Wiring the emit is code-change = out of B1. **B1 retires the panel; a follow-up ticket wires `routing_decision` emission (links FRE-432 tier-routing gap + FRE-539/C4).** So **4 retirements**, not 3.

Live values for reference: `model_role` = {primary, sub_agent}; `role` = {primary, assistant, compressor, sub_agent, gpt-5.4-nano}; `phase` = {setup, llm_inference, tool_execution, synthesis, other}; `from_state` = {llm_call, tool_execution, init}; `insight_type.keyword` = {trend, graph_staleness, missing_skill, anomaly, prompt_composition}.

The remaining ~41 panels were sanity-checked: their referenced fields all resolve non-empty → **keep** (no edit).

### Decision points (surfaced to owner before coding)

- **D1 — RESOLVED (owner, 2026-06-08): `role`.** `role.keyword` (5 LLM Performance panels) + `target_model.keyword` (Routing Decisions repoint) → **`role`** {primary, assistant, compressor, sub_agent, gpt-5.4-nano} (5 buckets). Owner chose the richer breakdown over `model_role` {primary, sub_agent}. Note: `gpt-5.4-nano` leaking into `role` is an emit-side data-quality issue, out of B1 scope (flag for A2/emit follow-up if needed).
- **D2 — index-pattern dedup depth.** A1 found 23 index-pattern objects → 5 distinct, plus a redundant `agent-logs*` / `agent-logs-*` pair. The dup id `eabfafeb-…` (`agent-logs*`) is embedded in the `references[]` arrays of `llm_performance.ndjson`, `request_timing.ndjson`, `request_traces.ndjson` (codex review), so removing it only from `data_views.ndjson` would leave dangling refs. Full dedup = repoint every cross-file reference to one surviving id — high-risk surgery across most files. Recommend **document-only in B1 + file a follow-up ticket** (precise dup map in the triage doc; no `references[]` edits in B1 to avoid breakage).

---

## Provenance work (A1 finding #1 → B1)

- `prompt-cost-cache.ndjson` lives in `docker/kibana/dashboards/` and is **not** loaded live and **not** in `import_dashboards.sh`. → move into `config/kibana/dashboards/` (canonical), add to the import-script `FILES` list + README.
- 3 live-only viz never exported to repo (UI edits): includes a case-dup `Request Count`/`Request count`. → inventory in the triage doc; retire UI-only dups (do not adopt orphans into repo unless they are genuine survivors).
- Redundant `agent-logs*` index-pattern (D2).

---

## Build steps (atomic; exact paths / commands)

### Step 1 — Verification harness first (red) `scripts/audit/verify_fre535_panels.py`
Read-only. Loads every `config/kibana/dashboards/*.ndjson` (+ `prompt-cost-cache.ndjson`), extracts panel→field refs (reuse the parser in `scripts/audit/fre533_reconcile.py`) **and** each panel's query/filter context, maps each panel to its family index via its index-pattern ref, and for every **agg/terms** field runs the panel's filtered `terms` agg against live ES. Codex-review hardening folded in:
- **Query-context-aware, not bare existence**: reproduce the panel's `searchSourceJSON` query + filters (e.g. `event_type:model_call_completed`, `record_type:insight`) so a field that exists but is empty *under the panel's filter* is still caught.
- **Long lookback**: use `@timestamp >= now-90d` for date-histogram/time-bound panels so a short default range can't yield a false zero.
- **Lens-aware**: classic viz refs live in `visState.aggs[].params.field`; Lens (the 2 prompt-cost-cache viz) live in `state.datasourceStates…columns[].sourceField` / `layers[].columns[]` — extractor handles both (skip-with-explicit-note if a Lens shape is unparseable rather than silently passing).

Prints `dashboard · panel · field · index · filter · non_empty` and exits non-zero if any non-retired panel field is empty under its own filter.
- **Verify (red):** `uv run python scripts/audit/verify_fre535_panels.py` → FAILS, listing exactly the 11 fix-target fields (`model.keyword`, `role.keyword`×5, `phase.keyword`×2, `from_state.keyword`, `insight_type`, `target_model.keyword`) as empty.

### Step 2 — `.keyword`-on-bare-keyword fixes (per-file, all occurrences incl. filters)
Edit, replacing **every** occurrence per file (agg `params.field` **and** any `match_phrase`/filter `key`):
- `config/kibana/dashboards/llm_performance.ndjson`: `model.keyword`→`model` (×1), `role.keyword`→`model_role` (×5) *[pending D1]*
- `config/kibana/dashboards/request_timing.ndjson`: `phase.keyword`→`phase` (×2 agg + verify the 2 filter occurrences resolve to a valid `phase` value; if the pre-applied filter value `llm_call:*` is stale, drop that filter)
- `config/kibana/dashboards/system_health.ndjson`: `from_state.keyword`→`from_state` (×1)
- `config/kibana/dashboards/task_analytics.ndjson`: **no `.keyword` repoint** — `Routing Decisions` is retired in Step 4 (never-wired emit), so the `target_model.keyword` ref is removed with the panel.
- **Verify:** `grep -c '\.keyword' config/kibana/dashboards/{llm_performance,request_timing,system_health,task_analytics}.ndjson` → only legitimately-`text+keyword` refs remain (expect 0 for these four after fix).

### Step 3 — text-as-terms fix
- `config/kibana/dashboards/insights_engine.ndjson`: agg `params.field":"insight_type"` → `insight_type.keyword` (leave the `query_string` `insight_type:anomaly` filter untouched — valid on text).

### Step 4 — retire dead panels (4)
Remove each retired visualization from its dashboard NDJSON: the viz saved-object line **and** its entry in the dashboard's `panelsJSON` **and** the matching `references[]` entry (codex: all three, else dangling-ref import error). Do **not** remove any index-pattern object (still used by survivors).
- `delegation_outcomes.ndjson`: `Rounds needed trend`, `Delegation satisfaction distribution`
- `insights_engine.ndjson`: `Weekly proposals created`
- `task_analytics.ndjson`: `Routing Decisions` (never-wired `routing_decision` emit — see finding above)
- **Verify:** re-import succeeds; the dashboards render with the survivor panels only (no dangling references → no Kibana import error).

### Step 5 — provenance consolidation
- `git mv docker/kibana/dashboards/prompt-cost-cache.ndjson config/kibana/dashboards/prompt-cost-cache.ndjson`; add it to `config/kibana/import_dashboards.sh` `FILES` (append after `data_views.ndjson`; it is a self-contained bundle so `_import` resolves its internal data-view→lens→dashboard order) and to the README list.
- D2: **document-only** — record the precise index-pattern dup map (23 objects → 5 distinct titles; `agent-logs*` id `eabfafeb-…` vs `agent-logs-*`) in the triage doc; **do not** edit `references[]` (dangling-ref risk per codex review). File a follow-up ticket for full repoint dedup.
- **Harden the import script (codex review):** `import_dashboards.sh` currently trusts HTTP 200, but `_import` returns 200 with per-object `errors[]`. Capture the JSON body and fail the line if `success:false` / `errors[]` non-empty / `successCount` < objects in file. This is the round-trip discipline A1 flagged as missing.

### Step 6 — re-import to live + green
- `./config/kibana/import_dashboards.sh` → all 200 **and** body reports `success:true` with no `errors[]` per file (hardened check from Step 5).
- **Verify (green):** `uv run python scripts/audit/verify_fre535_panels.py` → exits 0; every non-retired panel field non-empty **under its panel filter**; retired fields absent.
- Spot-check 2-3 fixed panels in Kibana UI render non-empty (manual, recorded in triage doc).

### Step 7 — triage summary doc `docs/research/2026-06-08-fre-535-dashboard-triage.md`
Dated table: all 12 dashboards × 57 viz with keep / retire / fix + one-line reason + (for fix) before→after field + live-bucket evidence. Plus: provenance actions taken, the 3 live-only viz dispositions, D1/D2 decisions, reproduction command.

### Step 8 — quality gates
`make ruff-check` + `make ruff-format` (the new script) · `make mypy` (script typed) · `pre-commit run --all-files` (no personal paths). No pytest changes expected (NDJSON + read-only script); if I add a unit test for the parser, run `make test-file FILE=...`.

---

## Acceptance mapping

| Ticket acceptance | Step |
|---|---|
| Keep/retire/fix decision for all 12 dashboards / 57 viz | Step 7 (table) |
| Every "fix" panel corrected + re-verified live (no silent-empty) | Steps 2-3, 6 (harness green) |
| Retired panels removed; survivors exported to version-controlled NDJSON | Steps 4-5 |
| Triage summary in `docs/research/` (dated) | Step 7 |

## Halt / risk notes
- Retiring 3 panels removes saved objects — these are dead (0 docs ever emitted), not historical *data* rows; surfaced + evidenced above, not a substrate row-drop. Confirm with owner.
- If `import_dashboards.sh` returns non-200 after edits → malformed NDJSON; fix before claiming green (do not leave live in a half-imported state).
- One ticket = one PR. Stop at PR; master merges/deploys/closes.
