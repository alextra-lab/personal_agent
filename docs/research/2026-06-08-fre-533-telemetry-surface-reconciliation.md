# Telemetry Surface — Three-Way Reconciliation Inventory (FRE-533)

> **Date:** 2026-06-08 · **Ticket:** FRE-533 (A1, Tier-1:Opus) · **Project:** Telemetry Surface Audit
> **Blocks:** FRE-534 (A2 template fixes) · FRE-535 (B1 dashboard triage) · FRE-540 (A3 CI checker)
> **Refs:** ADR-0074 (joinability) · ADR-0083 (SLM health) · ADR-0088 (topology) · ADR-0089 (artifact envelope) · ADR-0065 (cost gate) · FRE-452 (route-trace ledger) · FRE-407/409 (event/event_type split)
> **Reproduce:** `uv run python scripts/audit/fre533_reconcile.py --out /tmp/fre533`
> **Full per-field table (all 1023 rows):** [`2026-06-08-fre-533-reconciliation-table.csv`](./2026-06-08-fre-533-reconciliation-table.csv)

---

## TL;DR

Every field in all six live `agent-*` index families was traced through three corners —
**code emit site → ES mapping (live + repo template, including `dynamic_templates`) → Kibana dashboard
panel** — and classified. The surface is **substantially out of alignment**:

| Headline | Number |
|---|---|
| Total `(field, family)` rows walked | **1023** |
| ✅ aligned (explicit or dynamic-rule) | 304 |
| ⚠️ emitted-but-unmapped (dynamic sprawl) | 643 |
| ⚠️ TRAP — `keyword ignore_above:1024` long-text drop | 17 |
| ⚠️ TRAP — float→`long` (0.0 first-seen) | 11 |
| ⚠️ TRAP — join-key mapped as `text` | 2 |
| ⚠️ type-mismatch (live ≠ template) | 8 |
| **Broken / risky Kibana panel field-refs** | **14 across 6 of 12 dashboards** |

**The owner's observation — "so many dashboards and visualizations do not work because they do not
match the mappings" — is confirmed and quantified.** The dominant breakage: dashboards aggregate on
`model.keyword` / `role.keyword` / `phase.keyword` / `from_state.keyword`, but the templates map those
parents as **bare `keyword` with no `.keyword` multifield** → the terms aggregation resolves to nothing →
**silent empty panel**. LLM Performance alone has 6 such panels.

`agent-logs-*` has accreted **768 dynamically-mapped leaf fields**; only ~155 are explicitly templated.
Two families (`agent-insights-*`, `agent-monitors-slm-health-*`) have **no template at all**, and three
families (`joinability`, `slm-health`, `captains-subagents`) have **no Kibana index-pattern**, so their
data is invisible to every dashboard.

Correction is downstream (A2/B1/C*); this table makes those mechanical.

---

## Method — measure, don't assert

Reproducing the FRE-433/434 methodology: programmatic extraction, no eyeballing (per the standing
"you always get the ES mappings wrong first pass" rule). The harness
(`scripts/audit/fre533_reconcile.py`) walks all three corners and emits a per-field CSV + per-family JSON.
Refined after a Codex methodology review — the key catches:

1. **Union the live `_mapping` across *all* concrete indices in a family**, not just the newest. Old daily
   indices carry fields later dropped, and a `_source` value can be present without ever triggering a
   dynamic mapping (null/`[]`-only).
2. **Resolve every field through the template's `dynamic_templates` block, not just `properties`.** A
   `properties`-only read mislabels `*_id`/`*_type`/`*_message`/`*_ms` fields — they are governed by named
   rules (`ids_keyword`, `enums_keyword`, `free_text`, `ms_fields_as_float`, `default_string_keyword`).
   This is the single biggest first-pass trap and the reason the resolver is encoded, not inferred.
3. **`.keyword` subfield resolution before calling a dashboard ref aligned** — a `field.keyword`
   aggregation is only valid if the parent is `text` *with* a `fields.keyword` subfield. Bare `keyword`
   fields have no `.keyword`.
4. **Grep is fooled by runtime dict keys** (`**data` spreads, `.model_dump()`, `asdict()`); emit sites are
   recorded as *candidate* literal occurrences and reconciled against the live union mapping, which
   reflects what actually landed.

The `event` (log files) vs `event_type` (ES) key split is honored — both are explicitly mapped `keyword`
in `agent-logs`.

---

## Likely-finding #1 — Dashboard provenance & repo↔live drift

**Question:** are the Kibana saved objects in version control, or only in live Kibana?

**Answer: in git, but the repo is _not_ a faithful mirror of live — they have diverged.**

| | Repo NDJSON | Live Kibana |
|---|---|---|
| dashboards | **13** | **12** |
| visualizations | 54 (+2 lens) | 57 |
| index-patterns (objects / distinct) | 23 / 5 | 7 / 5 |

- NDJSON lives in **two** locations: `config/kibana/dashboards/*.ndjson` (12 dashboards) and
  `docker/kibana/dashboards/prompt-cost-cache.ndjson` (1 dashboard, lens-based).
- **`Prompt Cost & Cache Attribution (FRE-406)` exists in the repo but is NOT loaded in live Kibana.**
- **Live has 3 visualizations the repo does not** (57 vs 54+2). Edits made in the Kibana UI were never
  exported back to git.
- Index-patterns are **bundled-and-duplicated** — 23 saved objects collapse to 5 distinct titles, each
  dashboard file shipping its own copies with distinct IDs. There is also a redundant pair
  `agent-logs*` **and** `agent-logs-*`.

**Prerequisite gap for A2/B1:** there is no single source-of-truth export, no round-trip discipline, and
no index-pattern for 3 of the 6 families. B1 should establish a canonical export path
(`config/kibana/dashboards/`) and a re-import/diff step before any dashboard edits.

---

## The triangle, per family

Index → template coverage map (live ES + repo):

| Family | Live indices | Template (repo) | `dynamic` | Index-pattern? |
|---|---|---|---|---|
| `agent-logs-*` | ✓ | `index-template.json` | `true` | ✓ (`agent-logs-*` + dupe `agent-logs*`) |
| `agent-captains-captures-*` | ✓ | `captains-index-template.json` | `true` | ✓ |
| `agent-captains-reflections-*` | ✓ | `captains-index-template.json` (shared) | `true` | ✓ |
| `agent-captains-captures-subagents` | ✓ | **inherits** captains template via `captures-*` glob | `true` | ✗ |
| `agent-insights-*` | ✓ | **none** | (ES default) | ✓ |
| `agent-monitors-joinability-*` | ✓ | `monitors-joinability-index-template.json` | `false` | ✗ |
| `agent-monitors-slm-health-*` | ✓ | **none** | (ES default) | ✗ |

Two corrections to the ticket's assumptions, both verified:
- **`agent-captains-captures-subagents` is _not_ untemplated** — its name matches the
  `agent-captains-captures-*` glob, so it inherits `agent-captains-template`. The sub-agent doc shape
  (`system_prompt_chars`, `digest_chars`, …) differs from the captures shape, so it inherits a template
  that doesn't describe it: 10 fields fall through to dynamic mapping.
- **`agent-monitors-slm-health-*` has no matching template.** `slm-requests-index-template.json` targets
  `slm-requests-*` (a *sibling*, live and well-formed, the FRE-411 join target) — **not** the in-scope
  `agent-monitors-slm-health-*` family, which is fully dynamic-mapped.

### `agent-logs-*` — 768 fields, the sprawl + the traps

155 aligned, 581 generic emitted-but-unmapped, **28 trap/mismatch rows**. The template is `dynamic:true`
with no upper bound, so every new nested key (`arguments.*`, `context.*`, `messages_preview.*`, …) becomes
a permanent mapping entry → unbounded field growth.

Critically, `index-template.json` is the **only** production template **without** the `ms_fields_as_float`
dynamic rule (captains and slm-requests both have it). So any `*_ms`/`*_duration`/threshold field not in
explicit `properties` freezes to `long` on its first integer value:

| Field | live | Should be | Emit (candidate) | Resolution |
|---|---|---|---|---|
| `sub_agent_duration_ms` | `long` | float | `tools/artifact_tools.py:1516` | add `ms_fields_as_float` rule to `index-template.json` |
| `summariser_duration_ms` | `long` | float | `telemetry/within_session_compression.py:146` | ″ |
| `actual_wall_ms` | `long` | float | `orchestrator/executor.py:3348` | ″ |
| `wait_ms` | `long` | float | `llm_client/concurrency.py:333` | ″ |
| `calibration_threshold` | `long` | float | `brainstem/consumers/mode_controller.py:150` | add explicit `float` prop |
| `governance_threshold` | `long` | float | `service/app.py:889` | ″ |
| `threshold` | `long` | float | `insights/engine.py:458` | ″ |
| `threshold_tokens` | `long` | float? | `orchestrator/within_session_compression.py:288` | confirm intended type; likely int (OK) |
| `timeout_seconds` / `arguments.timeout_seconds` | `long` | float | `tools/primitives/run_python.py:60` | add `*_seconds`→float (captains rule already does this) |

> Note: `threshold_violations_count`, `iteration`, `max_iterations`, `iteration_count` are genuine
> integers — `long` is correct (initial heuristic false-positives on the substring "ratio"/"threshold",
> corrected here).

**`keyword ignore_above:1024` long-text drop (17 fields).** These free-text-ish fields don't match the
`free_text` regex (`*_message|*_content|reason|hint|stderr|stdout|raw_*|*_text|*_prompt`) and fall to
`default_string_keyword` → values over 1024 bytes are **silently not indexed**:

`response_preview`, `query_preview`, `message_excerpt`, `message_preview`, `content`, `content_value`,
`arguments.content`, `arguments.content_hash`, `arguments.summary`, `context_messages.content_preview`,
`messages_preview.content_preview`, `error_category`, `error_class`, `denial_reason`, `output_format`,
`message_roles`, `response_keys`.

> **Resolution direction:** extend the `free_text` regex to add `*_preview|*_excerpt|*_summary|error_*` (so
> they map to `text` and are fully searchable) **or** explicitly accept the truncation for true previews.
> `arguments.content` / `content` are the highest-risk (can be long). The `event`/`event_type` split is
> clean.

### `agent-insights-*` — 34 fields, zero template (33/34 ⚠️)

Entirely dynamic. Every numeric `evidence.*` and the cost fields work by luck of first-value inference;
`evidence.baseline_cost_usd` / `evidence.observed_cost_usd` / `evidence.ratio` / `confidence` are `float`
today but one `0` away from `long`. Join/category keys are `text` (ES default, with `.keyword` subfield):
`evidence.component_id` (**join key — should be keyword**), `insight_type`, `record_type`, `evidence.entity`.

> **Resolution direction:** author `docker/elasticsearch/insights-index-template.json` (mirror the
> captains `dynamic_templates`, add explicit `keyword` for `*_id`/`insight_type`/`record_type`, explicit
> `float`/`double` for `*_cost_usd`/`ratio`/`confidence`). This is an A2 deliverable.

### `agent-monitors-slm-health-*` — 7 fields, zero template (ADR-0083)

| Field | live | Issue | Resolution |
|---|---|---|---|
| `trace_id` | `text` | **join key analyzed** — UUID tokenized on `-`; exact term join to `slm-requests`/`agent-logs` needs `.keyword` | template: `keyword` |
| `kind`, `status` | `text` | enum-as-text | template: `keyword` |
| `probe_latency_ms` | `float` | OK today, but no `ms` rule guard | template: `float` |
| `reachable` | `boolean` | OK | — |
| `probed_at` | `date` | OK | — |

> **Resolution direction:** add `docker/elasticsearch/monitors-slm-health-index-template.json`
> (ADR-0083). The `trace_id`-as-`text` defect directly undermines the joinability story.

### `agent-captains-captures-*` / `-reflections-*` / `-subagents` — mostly aligned

The captains template is the **healthiest** surface: 50/43/44 aligned respectively. Remaining ⚠️ are
dynamically-mapped extensions that *happen* to be correctly typed but aren't pinned in the template
(`metrics_summary.cpu_*` float, token counts long, `proposed_change.*` keyword/date, the whole sub-agent
`*_chars` block). Risk is future type-drift, not present breakage.

> **Resolution direction (A2):** add the observed sub-agent fields (`system_prompt_chars`, `digest_chars`,
> `context_chars`, `memory_in_context`, …) and `metrics_summary.*` to the template as explicit props so a
> stray `0` can't flip a float to long. Consider splitting the subagents shape to its own template.

### `agent-monitors-joinability-*` — 23 fields, 0 ⚠️ (the model)

The only `dynamic:false` family and the only one with **zero** findings. `orphans.detail` is
`object/enabled:false` (source-only, intentional). This is the template every other family should look
like — explicit props, no dynamic sprawl, join keys as `keyword`.

---

## Broken & risky Kibana panels (the owner's point, enumerated)

14 panel field-references across **6 of the 12 live dashboards** do not match the mappings:

| Dashboard | Panel | Field ref | Why it's broken | Fix |
|---|---|---|---|---|
| LLM Performance | LLM Call Count by Model | `model.keyword` | parent `model` is **bare keyword**, no `.keyword` | point panel to `model` |
| LLM Performance | Avg Latency by Model Role | `role.keyword` | bare keyword `role` (likely `model_role`) | `model_role` |
| LLM Performance | Avg Prompt Tokens by Model Role | `role.keyword` | ″ | ″ |
| LLM Performance | LLM Latency Over Time | `role.keyword` | ″ | ″ |
| LLM Performance | P95 Latency by Role | `role.keyword` | ″ | ″ |
| LLM Performance | Prompt Token Percentiles by Role | `role.keyword` | ″ | ″ |
| Request Timing (E2E) | Avg Duration by Phase | `phase.keyword` | bare keyword `phase` | `phase` |
| Request Timing (E2E) | Request Phase Details | `phase.keyword` | ″ | `phase` |
| System Health | State Transitions | `from_state.keyword` | bare keyword `from_state` | `from_state` |
| Insights Engine | Insight count by type | `insight_type` | analyzed `text` used as terms agg | `insight_type.keyword` |
| Delegation Outcomes | Rounds needed trend | `rounds_needed` | **no field emits/maps this** | fix emit or retire panel |
| Delegation Outcomes | Delegation satisfaction distribution | `user_satisfaction` | **missing field** | fix emit or retire |
| Insights Engine | Weekly proposals created | `proposals_created` | **missing field** | fix emit or retire |
| Task Analytics | Routing Decisions | `target_model.keyword` | **missing field** (likely `model_role`) | repoint or retire |

Two distinct root causes, both for B1 (FRE-535):
1. **`.keyword` on bare-keyword fields** (9 panels) — the panels assume ES-default string mapping
   (`text`+`.keyword`), but the template pins them as plain `keyword`. Drop the `.keyword` suffix.
2. **Missing/renamed fields** (4 panels) — the panel reads a field nothing emits; either the emit was
   renamed (e.g. `target_model`→`model_role`) or never shipped. Repoint or retire.

---

## Resolution routing (this table → downstream tickets)

| Finding class | Count | Ticket |
|---|---|---|
| Correct/extend `index-template.json` (add `ms_fields_as_float`, explicit float thresholds, extend `free_text`) | ~28 | **FRE-534 (A2)** |
| Author `insights` + `slm-health` templates; split/extend `subagents` | 2 families + 1 shape | **FRE-534 (A2)** |
| Fix `.keyword`-on-bare-keyword + missing-field panels | 14 panels | **FRE-535 (B1)** |
| Establish canonical export + round-trip; load `prompt-cost-cache`; dedupe index-patterns | provenance gap | **FRE-535 (B1)** |
| Add index-patterns + dashboards for joinability / slm-health / subagents | 3 families | **FRE-537/538 (C2/C3)** |
| Static mapping↔dashboard + trap-class lint as CI floor | this whole table | **FRE-540 (A3)** |

---

## Artifacts & reproduction

- **`scripts/audit/fre533_reconcile.py`** — read-only extractor (ES `:9200`, Kibana `:5601`). Unions live
  mappings, resolves the `dynamic_templates` block, greps candidate emit sites, parses dashboard field
  refs, lists live saved objects. Emits per-family JSON + the consolidated CSV. Reusable by FRE-540 (A3).
- **`docs/research/2026-06-08-fre-533-reconciliation-table.csv`** — all 1023 `(field, family)` rows with
  live type, template-expected type + resolution path, classification, candidate emit sites, and dashboard
  refs. This is the "every field" acceptance artifact.
- **`scripts/audit/telemetry_surface_check.py`** (FRE-540, ADR-0090 D5) — the standing **hermetic** CI
  guard derived from this table: parses the committed templates + dashboard NDJSON (no live stack) and
  reproduces the static subset of these classifications (mapping↔dashboard `.keyword`/unmapped findings +
  trap-class lint). Reuses this script's primitives so its taxonomy matches. Run:
  `uv run python -m scripts.audit.telemetry_surface_check` (report-only) or `--gate` to fail on floor
  findings. The hermetic floor catches the `.keyword`-on-bare-keyword + unmapped subset; the
  emit-corner renames in the broken-panel table (`role`→`model_role`, `target_model`, `rounds_needed`)
  are surfaced report-only (the floor cannot prove "mapped-but-unemitted" without the emit/live corners).

```bash
uv run python scripts/audit/fre533_reconcile.py --out /tmp/fre533
# -> /tmp/fre533/family_*.json, dashboards.json, kibana_live.json, reconciliation_table.csv
```

## Acceptance (FRE-533)

- [x] Reconciliation table — 6 families, every field (1023 rows), all three corners populated (CSV).
- [x] Each row classified; every ⚠️ row has a one-line resolution direction (per-family sections + routing table).
- [x] Live-mapping vs repo-template divergence documented per family (template-coverage map + per-family notes).
- [x] Dashboard-provenance question answered (in git at two paths; repo↔live drift quantified; 3 families have no index-pattern).
- [x] Findings written to `docs/research/` (this doc, dated) so A2/B1/C* execute mechanically off the table.
