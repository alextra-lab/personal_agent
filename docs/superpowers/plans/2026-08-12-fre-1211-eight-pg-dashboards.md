# FRE-1211 — Rebuild the remaining eight dashboards on Postgres

**Backing plan:** `docs/superpowers/plans/2026-08-08-fre-1203-grafana-migration-program.md` § T5.
**Exemplar pattern:** FRE-1209 (`cost_budget.json`, PR #897, merged). **Render verdicts:** FRE-1207
audit, `docs/research/2026-08-08-grafana-dashboard-render-audit.md` § 5.
**Blocked-by:** FRE-1209 — verified `Done`, merged.

## The eight, with corrected per-panel dispositions

The ticket's table implies one source per dashboard. The FRE-1207 audit's per-panel dispositions are
finer-grained than that and are what actually governs each panel — two dashboards mix sources:

| Dashboard | Panels | Disposition |
|---|---|---|
| `intent_classification` | 1–5 | all `rebuild-on-pg` — `route_traces.task_type/complexity/intent_confidence` |
| `request_timing` | 1–3 | `rebuild-on-pg` — `route_traces.latency_breakdown` JSONB. Panel 4 (Tempo timeseries) `keep-as-is`, add `fieldConfig.unit: s`. **Plus a new 5th panel**: Grafana `traces` panel type on the `tempo` datasource — AC-3, a panel type used zero times in the corpus |
| `task_analytics` | 1–5 | all `rebuild-on-pg` — `route_traces.tool_iteration_count/tools_used/skills_loaded` |
| `expansion_decomposition` | 1–5 | all `rebuild-on-pg` — `route_traces.decomposition_strategy/reason/sub_agent_count/sub_agents` |
| `llm_performance` | 1–5 | `rebuild-on-pg` — `route_traces.model_role/thinking_enabled/routing_history/fallback_triggered` + `api_costs`. Panel 6 (model call errors) — audit flags this **conditional**: rebuild on `route_traces.error_type` only if it carries an equivalent signal; otherwise keep ES-sourced and disclose the staleness honestly rather than reproduce a permanently-empty panel |
| `system_health` | mixed | Panel 2 (error events) → `rebuild-on-pg`, `route_traces.error_type/error_class/degraded_stages` — this is the one the ticket names. **Panels 1 (CPU/memory) and 4 (state transitions) rebuild-in-the-UI-on-ES** — same UI/gates loop, just against the Elasticsearch datasource, not "leave the existing unconfigured panel alone." Audit found no PG table for host sensor metrics or state-transition events anywhere in the plan; forcing them onto PG would be inventing a source. **Panel 3 (consolidation activity) is `delete`** — 38 days stale and redundant with `extraction_retry_health`'s PG rebuild |
| `extraction_retry_health` | 1–4 | all `rebuild-on-pg` — `consolidation_attempts` (5,083 rows) |
| `turn_session_artifact` | 1–5 | all `rebuild-on-pg` — `artifacts` (97) + `sessions` (1,282) + `session_model_selections` |

`system_health` is the one dashboard where "PG source" from the ticket table is a simplification I'm
deliberately not following blindly — mixing in dead-source panels would fail T5-1 (no forbidden source)
in spirit even if not by grep, and inventing a PG table that doesn't exist would fail T4-2's honesty bar.

`llm_performance` panel 6's "equivalent signal" test (codex plan-review flagged this as underspecified):
during Step 0's raw-table read, compare `route_traces.error_type` population and time coverage against
the historical ES `model_call_error` docs (531 total, 2026-04-13→2026-06-01, already stale at audit
time) — if `route_traces.error_type` is non-null on a comparable share of recent rows, rebuild-on-pg; if
it's structurally sparse/absent for model-call errors specifically, keep ES-sourced and disclose the
staleness in the panel description rather than force a PG query that just returns empty. Record which
was true and why in the handoff.

## Execution — batched subagent fan-out, not all 8 at once

The ticket says "one subagent per dashboard." The `create-visualization` skill's Grafana arm requires
standing up an **ephemeral Editor Grafana container** per build (own port, own name) plus a Playwright
browser session. Current VPS load is already ~9GB across running containers (`docker stats`, checked
live) against a shared box. Running all 8 authoring flows concurrently risks resource exhaustion on
shared infra other worktrees depend on.

**Plan: 3 batches of 2–3 concurrent subagents each** (not a single 8-way parallel fan-out), each agent
assigned a **unique container name and host port** to avoid collision:

| Batch | Dashboards | Ports |
|---|---|---|
| 1 | `intent_classification`, `task_analytics`, `expansion_decomposition` | 3010, 3011, 3012 |
| 2 | `llm_performance`, `extraction_retry_health`, `turn_session_artifact` | 3013, 3014, 3015 |
| 3 | `request_timing`, `system_health` | 3016, 3017 |

**Preflight before each batch**: `docker stats --no-stream` + `free -m`. If headroom looks tight
(< ~2GB free after accounting for the batch's containers), drop that batch to sequential (one subagent
at a time) rather than concurrent — this is a runtime judgment call at batch-launch time, not a number
fixed in this plan up front.

Each subagent:
1. Reads its dashboard's FRE-1207 audit section + this plan's disposition row.
2. Reads the raw Postgres table(s) first (`psql`, column names/meaning/denominators) — skill Step 0.
3. Stands up its own ephemeral Editor instance (unique name/port from the table above), builds each
   panel in the UI, extracts via `GET /api/dashboards/uid/<uid>` (not the Settings JSON Model tab —
   different schema, per the skill's FRE-1209 finding), tears the container down when done (always,
   including on failure).
4. Writes the new `config/grafana/dashboards/<name>.json` in this worktree. **Does not commit** — I
   integrate all 8 in one pass afterward to avoid concurrent-branch git races.
5. Drops `rebuilt-from-kibana`, adds `grafana-native` in the file's `tags`. **Does not touch the test
   file** — I decrement the shared assertion once, centrally, after all 8 land (13 → 5).
6. Runs Gate 0 (field-config jq) + the four gates itself and reports pass/fail evidence per panel,
   including the raw-data cross-check (hand-run SQL matching the panel's rendered value) — this is the
   proof I read at integration, not re-derived.
7. Fixes AC-2 for its dashboard: `time.from` must be ≥ the longest window any surviving panel
   description names; no description may claim a window the dashboard cannot show.
8. Adds a dashboard-level `description` stating the one question the dashboard answers (AC-4).

After each batch returns, I verify the gate evidence, fix anything not credible, then move to the next
batch — so a batch's problems don't compound into the next one's build.

## AC-3 wiring — the concrete drill-down mechanics

`request_timing` has no cost panel of its own — "a cost panel" in AC-3 means the *already-shipped*
`cost_budget.json` (FRE-1209), whose "Cost reconciliation" table panel selects `trace_id` directly
(`SELECT trace_id, cost_reconciled, cost_live_usd, cost_authoritative_usd, … FROM route_traces`).
Verified live: that panel currently has no `fieldConfig.defaults.links` and `cost_budget.json` has no
`templating`. Concrete plan:
1. Add a `$trace_id` dashboard variable to `request_timing.json` (`templating.list`), consumed by the
   new Trace panel's Tempo query.
2. Add a **data link** (Grafana field-level link, per
   https://grafana.com/docs/grafana/latest/panels-visualizations/configure-data-links/) on
   `cost_budget.json`'s reconciliation panel's `trace_id` field →
   `/d/<request_timing-uid>?var-trace_id=${__value.text}` (exact templating syntax confirmed against
   the docs at build time, not guessed).
3. This is a **small supporting edit to the already-shipped `cost_budget.json`**, required to satisfy
   this ticket's own AC-3 — folded into this PR per the build skill's §5 ("fold in, don't over-ticket"),
   not a new ticket.
4. Verified live: click a trace id in `cost_budget`, confirm navigation lands on `request_timing` with
   that trace's waterfall rendered in the new Trace panel — a Playwright click-through, not just that
   the link URL looks right.

## Integration (after all 3 batches)

0. **Gate before decrementing anything**: confirm all 8 expected files exist, each dropped
   `rebuilt-from-kibana` and gained `grafana-native`, and each batch's gate evidence (Gate 0 jq +
   per-panel four-gate report, including the raw-SQL cross-check) is present and credible — a partial
   batch does not get folded into the count change.
1. Grep the 8 new files for `metrics`, `session_events`, `sysgraph`, `request_trace` — zero hits (AC-1).
   By construction, none of the 8 rebuilt files should reference the ES `request_trace`/`request_trace_step`
   family after rebuild (the 3 `request_timing` panels move onto `route_traces`/Postgres; its one
   Tempo panel queries Tempo, not that ES family) — the ticket's note that `request_trace` is "not
   forbidden" is about not blanket-failing dashboards elsewhere in the 16-dashboard corpus that still
   legitimately reference it, not license to skip checking these 8.
2. `jq` Gate 0 across the 8 files — zero rejects.
3. **Orchestrator independently re-verifies, not just reads subagent self-reports** (codex plan-review
   flagged trusting self-reports as a gap): full Playwright render-assert pass over all 8 dashboards
   myself (Gate 1, independently), plus a spot-check raw-SQL cross-check on at least one panel per
   dashboard myself (Gate 4 sample), before treating any batch as done. Master still independently
   re-applies all four gates at the PR gate — this spot-check is mine, not a substitute for that.
4. Decrement `tests/integration/test_fre1072_tempo_grafana_acceptance.py`'s `len(ours) == 13` to `== 5`,
   update the trailing comment listing what decremented it (mirrors the existing FRE-1209/FRE-1212
   comment style).
5. Confirm the two dropped-tag/mixed-source dashboards read correctly: `system_health.json` keeps 2
   ES-sourced panels + 1 PG panel + drops 1 dead panel (4 → 3 panels), still tagged `grafana-native`
   (a mixed-source dashboard is still "rebuilt," not still "rebuilt-from-kibana" — the tag tracks
   provenance of the *file*, not purity of every panel's datasource).
6. `request_timing.json` verified to have 5 panels (3 rebuilt + 1 kept Tempo timeseries + 1 new Trace
   panel) and the AC-3 click-through above.
7. Full Playwright render pass over all 8 on the real (worktree-mounted) instance per the skill's
   worktree-mounting caveat — not `cloud-sim-grafana`, which mounts `/opt/seshat` main, not this
   worktree.

## Quality gates (Step 8)

- `make test` (no src/ change expected, but confirms the test-file edit parses and the rest of the
  suite is unaffected)
- `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`
- Diff class: **self-serve** — dashboard JSON + one test-file line are not a production write path,
  not schema, not cost/governance code. Both reviewers (`feature-dev:code-reviewer`, `security-review`)
  run against `git diff origin/main...HEAD` before PR, per the build skill.

## Acceptance criteria (from the ticket) — how each will be proven in the handoff

| # | Criterion | Evidence to capture |
|---|---|---|
| AC-1 | No rebuilt panel queries a forbidden source | grep output (zero hits) across the 8 files |
| AC-2 | Time range matches panel claims | Per-dashboard `time.from` vs. surviving panel descriptions, stated explicitly |
| AC-3 | Span waterfall reachable | `request_timing` Trace panel screenshot + a followed trace-id link |
| AC-4 | Each dashboard states its question | `description` field quoted per dashboard |
| T4-1/T4-5/T4-6 per dashboard | unit+UI-authored+renders-correctly | Gate 0 jq output + Playwright screenshots + raw-SQL cross-check per panel |
