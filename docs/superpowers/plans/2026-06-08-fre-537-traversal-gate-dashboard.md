# FRE-537 (C2) — Traversal Ledger & Gate-Decision Dashboard

> **Date:** 2026-06-08 · **Ticket:** FRE-537 (Tier-2:Sonnet) · **Project:** Telemetry Surface Audit
> **Refs:** FRE-452 (route-trace ledger, Done) · FRE-506 (gate decision telemetry) · ADR-0088 + FRE-513 (topology spine) · A1 (FRE-533 reconciliation) · A2 (FRE-534 templates, Done) · C1 (FRE-536, the dashboard pattern this mirrors)
> **Pattern:** mirrors C1 (FRE-536) exactly — legacy aggs-based visualizations (not Lens, per FRE-546), shared canonical `agent-logs-pattern` index-pattern, one `*.ndjson` file registered in `import_dashboards.sh`.

---

## Scope decision (measure-first, FRE-536 discipline)

The ticket lists four candidate panel families. I traced each through **emit site → live ES `_field_caps` →
existing dashboards** before designing. The result splits cleanly into *ES-visible / build now* vs
*Postgres-or-transient / defer*:

| Candidate (ticket) | Where the data actually lives | Verdict |
|---|---|---|
| **Gate decisions** (allow/deny by gate, top denial reasons) | `tool_loop_gate` in `agent-logs-*` (8 998 docs) — `decision`,`reason`,`tool_name` all bare `keyword`, aggregatable | **BUILD** (4 panels) |
| **Route/trace ledger** (stimulus → model path → result type) | `route_trace_written` in `agent-logs-*` (72 docs) carries `gateway_label` (stimulus→path label) + `orchestration_event` (primary_handled / delegate_called). **Rich per-turn fields (task_type/complexity/result_type/latency/cost) live only in the Postgres route-trace ledger** | **BUILD the ES slice** (2 panels); defer the Postgres-only rich fields |
| **Execution topology** (primary vs sub-agent rows, `(trace_id, task_id)`) | `turn.topology_entered`/`turn.completed` ES docs are the **consumer's `event_processed` log line** — they do **not** carry the `topology` payload as a field (`_field_caps` for `topology`/`cost_authoritative_usd` → absent). Topology label lives only in (a) the Postgres ledger row and (b) the transient AG-UI `turn_status` STATE_DELTA (ADR-0076, projector → UI only, never persisted) | **DEFER** → follow-up ticket (mirrors FRE-547) |
| **Decomposition / delegation path distribution** | `strategy` on `gateway_output` | **ALREADY VIEWED** — `expansion_decomposition.ndjson` ("Decomposition strategy distribution", "Expansion events over time") + `intent_classification.ndjson` (task_type). Do **not** duplicate. |

**Net:** a new 6-panel dashboard on the two genuinely-unviewed ES-backed traversal surfaces (gate decisions +
route-trace ledger ES slice). The execution-topology view and the rich route-trace cognitive-work fields are
Postgres/transient-only → filed as a follow-up, **not faked** (the exact FRE-536 → FRE-547 move).

### Field verification (live `_field_caps`, all bare `keyword` → aggregatable)

```
decision keyword | reason keyword | tool_name keyword | state_before/after keyword
gateway_label keyword | orchestration_event keyword | task_id keyword
topology  -> ABSENT   | cost_authoritative_usd -> ABSENT   (confirms the defer)
```

Real value distributions (proof the panels populate):
- `decision`: allow 5367 · block_consecutive 1847 · block_identity 1035 · warn_consecutive 505 · advise_identity 237 · block_output 7
- `gateway_label`: conversational/single 46 · tool_use/single 8 · analysis/single 6 · tool_use/hybrid 5 · …
- `orchestration_event`: primary_handled 65 · delegate_called 7

> A1-trap guard: every dimension is **bare `keyword`** — panels reference `decision`, **not** `decision.keyword`
> (the `.keyword`-on-bare-keyword agg-to-nothing trap that broke 9 panels in A1).

---

## Panels (all on verified-typed fields, `agent-logs-pattern` index-pattern)

| # | Title | Source event | Agg | Maps to ticket candidate |
|---|---|---|---|---|
| 1 | Gate Decisions Over Time | `tool_loop_gate` | area: date_hist × count, split `decision` | gate decisions (FRE-506) |
| 2 | Gate Decision Outcomes | `tool_loop_gate` | donut: count by `decision` | gate allow/deny split |
| 3 | Gate Activity by Tool | `tool_loop_gate` | table: `tool_name` × `decision` × count | gate by tool |
| 4 | Top Gate Block / Warn Reasons | `tool_loop_gate AND NOT decision:allow` | table: count by `reason` | top denial reasons |
| 5 | Route-Trace: Stimulus → Path Label | `route_trace_written` | donut: count by `gateway_label` | route ledger: stimulus→path (FRE-452) |
| 6 | Route-Trace: Orchestration Outcome Over Time | `route_trace_written` | bar: date_hist × count, split `orchestration_event` | route ledger: result/outcome |

2×3 grid (w24×h15 each), mirroring `cost_budget.ndjson` layout.

---

## Steps

1. **TDD — failing test first.** New `tests/scripts/test_traversal_gate_dashboard.py`:
   - asserts `config/kibana/dashboards/traversal_gate.ndjson` exists and is valid NDJSON (one JSON object/line);
   - exactly one `dashboard` object; every `panel_N` reference resolves to a `visualization` object in the file;
   - every visualization references index-pattern id `agent-logs-pattern`;
   - **A1-trap guard:** no visualization aggregates on a `*.keyword` field for the known-bare-keyword dims
     (`decision`,`reason`,`tool_name`,`gateway_label`,`orchestration_event`,`state_before`,`state_after`).
   - Run → **fails** (file absent). Verify: `make test-file FILE=tests/scripts/test_traversal_gate_dashboard.py` → red.
2. **Create `config/kibana/dashboards/traversal_gate.ndjson`** — 1 index-pattern (canonical `agent-logs-pattern`,
   identical to cost_budget) + 6 visualizations + 1 dashboard, legacy-aggs visState (copy cost_budget shapes).
   Verify: test from step 1 → **green**.
3. **Register in `config/kibana/import_dashboards.sh`** — append `"traversal_gate.ndjson"` to `FILES`.
4. **Local import + aggregation proof** (local Kibana :5601 / ES :9200 — *not* a deploy):
   `./config/kibana/import_dashboards.sh` → expect `OK traversal_gate.ndjson`; spot-run each panel's agg via
   `_search` and confirm non-empty buckets. Record output in the research doc.
5. **Research doc** `docs/research/2026-06-08-fre-537-traversal-gate-dashboard.md` (mirror FRE-536 doc):
   scope-decision table, field-verification proof, panel table, import proof, defer note. Add to
   `docs/research/README.md` index.
6. **Follow-up ticket (Needs Approval, Telemetry Surface Audit):** "Execution-topology ES projection emitter —
   persist `(trace_id, task_id, topology, primary|sub_agent, authoritative_cost_usd, result_type)` to ES so the
   topology view + rich route-trace fields become Kibana-visible" (mirrors FRE-547). File via Linear.
7. **Quality gates:** `make test-file` (the new test) → `make test` (module) → `make mypy` → `make ruff-check` +
   `make ruff-format` → `pre-commit run --all-files`.
8. **PR** with `.github/PULL_REQUEST_TEMPLATE.md`, pre-merge checklist only. **STOP** (master merges/deploys/closes).

## Acceptance (FRE-537)

- [ ] Dashboard NDJSON in repo, registered in import script, imports clean on local Kibana.
- [ ] Panels populated against real data (import + per-panel agg proof recorded).
- [ ] Every field verified mapped `keyword`/aggregatable before wiring; no `.keyword`-on-bare-keyword refs (test-enforced).
- [ ] Exported to version-controlled NDJSON.
- [ ] Execution-topology + rich route-trace defer filed as a Needs-Approval follow-up (not faked).

## Out of scope / deferred (not faked)

- Execution-topology view (primary vs sub-agent `(trace_id, task_id)` rows, topology label, authoritative
  per-turn cost) — Postgres ledger + transient AG-UI `turn_status` only → follow-up ES emitter.
- Rich route-trace cognitive-work fields (task_type / complexity / result_type / latency / token cost) — Postgres-only.
- Decomposition / delegation strategy distribution — already on `expansion_decomposition` + `intent_classification`.
