# FRE-534 (A2) — Correct the ES templates from the reconciliation table

> **Date:** 2026-06-08 · **Ticket:** FRE-534 (A2, Tier-2:Sonnet) · **Project:** Telemetry Surface Audit
> **Blocked by:** FRE-533 (A1, merged) · **Blocks:** FRE-536/537/538/539 (C* dashboards)
> **Source of truth:** A1 reconciliation table (`docs/research/2026-06-08-fre-533-*`), repo template
> files under `docker/elasticsearch/`, `scripts/setup-elasticsearch.sh`, and the live emit sites.
> **Scope guard:** template/setup/docs/test only — **no `src/` changes** (see Decision C).

---

## What A1 actually found (the authority, re-walked field-by-field)

| Family | Template today | A1 verdict | A2 action |
|---|---|---|---|
| `agent-logs-*` | `index-template.json` (`dynamic:true`, **no `ms` rule**) | 28 trap/mismatch rows | add `ms_fields_as_float`, 3 explicit floats, extend `free_text` (selectively) |
| `agent-insights-*` | **none** (ES default) | 33/34 ⚠️; `evidence.component_id` join-key-as-`text` | **author** `insights-index-template.json` |
| `agent-monitors-slm-health-*` | **none** (ES default) | 7 live ⚠️; `trace_id` join-key-as-`text` | **author** `monitors-slm-health-index-template.json` |
| `agent-captains-captures-subagents` | inherits captures glob (wrong shape, 10 fall-through) | the **real** straddle | **carve** its own template |
| `agent-captains-captures-*` / `-reflections-*` | shared `captains-index-template.json` | healthy, **no type collisions** | Decision A |
| `agent-monitors-joinability-*` | `monitors-joinability-index-template.json` | 0 ⚠️ (the model) | none |

---

## DECISION A — the "straddle split" (ticket §2 vs A1 evidence)

The ticket says *"split the straddling template — covers both `captures-*` and `reflections-*` (two doc
shapes)."* A1's measured finding is more specific:

- **captures vs reflections do NOT collide.** Verified against the CSV: the only same-name/different-type
  field is `total_tokens` (captures legacy index = `long`, reflections = `integer`), and the template
  **already pins it `integer`** for both going forward. Splitting them changes nothing today.
- **The real straddle is `agent-captains-captures-subagents`** — its name matches the `captures-*` glob, so
  it inherits a template that doesn't describe its shape; 10 fields (`system_prompt_chars`, `digest_chars`,
  `context_chars`, `full_output_chars`, `skill_index_block_chars`, `context_message_count`, `max_tokens`,
  `memory_in_context`, `success`, `mode`) fall through to dynamic mapping.

**Recommended (Option 1 — 3-way split):** carve three templates — `captures`, `reflections`, `subagents` —
each with explicit props for its own shape. Satisfies the ticket literally **and** fixes the A1-identified
real straddle. `subagents` template gets priority 120 + pattern `agent-captains-captures-subagents*` so it
out-ranks the captures template (110) for that index; captures/reflections stay priority 110 on their
non-overlapping prefixes (no equal-priority pattern overlap → ES accepts).

**Alternative (Option 2 — carve subagents only):** keep captures+reflections shared (they don't collide),
split off only `subagents`. Less churn, fixes the real defect, but does not literally separate
captures from reflections. → **needs owner call (see questions).**

## DECISION B — `free_text` extension is selective, not blanket

A1 lists **17** `keyword ignore_above:1024` long-text-drop fields but its resolution direction is a
*direction* ("extend regex … **or** explicitly accept truncation for true previews; `content`/
`arguments.content` are highest-risk"). Blindly mapping all 17 → `text` is **wrong**: several are enums/
hashes/short lists where `keyword` is correct and the >1024 drop is purely theoretical.

Classified each leaf (dynamic-template `match` is on the **leaf** name, so nesting is handled):

| → `text` (genuine free text, can exceed 1024) | stays `keyword` (enum / hash / short list / **aggregated**) |
|---|---|
| `response_preview`, `query_preview`, `message_preview`, `content_preview` (`*_preview`) | `error_class`, `error_category` (exception/category enums) |
| `message_excerpt` (`*_excerpt`) | `output_format` (enum: markdown/html/…) |
| `content`, `content_value`, `arguments.content` (leaf `content`) — **highest risk** | `content_hash` (fixed-length hash) |
| `summary` / `arguments.summary` (leaf `summary`) | `message_roles`, `response_keys` (short keyword arrays) |
| | **`denial_reason`** — feeds a terms agg (`extraction_retry_health.ndjson` "Top denial_reason" donut); `text` would break it → keep `keyword` |

**`denial_reason` correction (codex catch):** A1 listed it in the 17 drops, but the CSV shows it is the
agg field of a live donut panel. Mapping it `text` breaks the terms aggregation. Resolution: explicit
`"denial_reason": {"type": "keyword", "ignore_above": 8192}` — donut keeps working, practical drop risk
removed without forcing a B1 dashboard change.

**Implementation:** extend the `free_text` regex in `index-template.json` to add only the genuine-text
leaves: `…|content|content_value|content_preview|.*_preview|.*_excerpt|summary` (verified none of these
leaves have a dashboard agg ref — all `dashboard_refs` empty in the CSV). Add the explicit
`denial_reason` keyword prop. Leave the enum/hash/list leaves on `default_string_keyword`. Each kept-as-
keyword field gets a one-line "why" in the reindex doc, so the choice is auditable (acceptance: "no field
ships unverified").

## DECISION C — scope §4 ("reconcile lying field names at source") is a no-op for A2

A1 routed every renamed/missing-field case to **B1 (FRE-535, dashboards)**: the panels read `role.keyword`/
`target_model.keyword` but the **emit side already uses the correct name** (`model_role`) — the dashboards
are stale, not the code. `event` vs `event_type` is already aligned (both `keyword`). → **A2 makes no
`src/` emit changes**; this is recorded as the reconciliation outcome (so no new `log.*`/`bus.publish`/
Cypher → no ADR-0074 identity surface touched).

---

## The agent-logs edits (verified against A1's trap rows)

**Add `ms_fields_as_float` dynamic rule** (copied verbatim from `captains-index-template.json`, the proven
form), matching `^(.*_ms|.*_seconds|.*_latency|.*_duration|.*_offset)$` → `float`. Fixes the 0.0→long trap
for `sub_agent_duration_ms`, `summariser_duration_ms`, `actual_wall_ms`, `wait_ms`, `timeout_seconds`,
`arguments.timeout_seconds`, and **pins** the ~15 currently-float-by-luck `*_ms`/`*_seconds` fields.
Explicit props win, so existing `latency_ms`(long), `probe_duration_ms`(integer) are unaffected; genuine
ints (`*_count`, `iteration*`) don't match the regex → stay `long`. ✔ checked each.

**Add 3 explicit `float` props** (A1 narrative table): `calibration_threshold`, `governance_threshold`,
`threshold`. **Do NOT** touch `threshold_tokens` (int, OK), `threshold_violations_count`, `iteration`,
`max_iterations`, `iteration_count` (genuine ints — A1 explicitly de-flagged these).

**Extend `free_text`** per Decision B.

---

## New template: `agent-monitors-slm-health` (author from the MODEL, not 7 live fields)

`SlmHealthSnapshot` (frozen Pydantic, `src/.../observability/slm_health/snapshot.py`) emits **14** fields;
A1 saw only 7 because the rest arrive `null` and never triggered a dynamic mapping (A1 method-note #1).
Template covers the full model: `status`/`model_id`/`kind`/`trace_id` → `keyword` (trace_id is the join-key
fix); `reachable`/`model_loaded` → `boolean`; `gpu_util_pct`/`vram_used_mb`/`vram_total_mb`/`latency_ema_ms`/
`probe_latency_ms` → `float`; `queue_depth` → `integer`; `probed_at` → `date`; `error` → `text`.
`dynamic:true` + safety-net `dynamic_templates` (`ms_fields_as_float`, `ids_keyword`,
`default_string_keyword`) for additive growth. No ILM (matches current state; retention = future ticket).

## New template: `agent-insights` (mirror captains, fix join key + cost floats)

`dynamic:true`; `dynamic_templates`: `ms_fields_as_float`, `ids_keyword` (catches leaf `component_id` via
`*_id` → keyword, the join-key fix), `cost_ratio_as_float` (regex `^(.*_cost_usd|ratio|confidence)$` →
`float`), `enums_keyword`, `free_text`, `default_string_keyword`. Explicit props: `timestamp`(date),
`insight_type`/`record_type`(keyword), `title`/`summary`(text), `confidence`(float), `actionable`(boolean),
`analysis_window_days`(integer), `evidence` object with explicit `baseline_cost_usd`/`observed_cost_usd`/
`ratio`(float) + `component_id`(keyword), remaining `evidence.*` counts left to `ids`/default rules (`long`).

## New / split captains templates (per Decision A, Option 1)

- `captains-captures-index-template.json` — pattern `agent-captains-captures-*`, priority 110, current
  captures props (+ `steps.*`, `metrics_summary.*` pinned as explicit float/long so a stray 0 can't flip).
- `captains-reflections-index-template.json` — pattern `agent-captains-reflections-*`, priority 110,
  current props + reflection-only `proposed_change.*` extensions.
- `captains-subagents-index-template.json` — pattern `agent-captains-captures-subagents*`, **priority 120**,
  the subagent shape: `*_chars`→`long`, `context_message_count`/`max_tokens`→`long/integer`,
  `memory_in_context`/`success`→`boolean`, `mode`/`model_role`/`task_id`→`keyword`, plus the shared captains
  `dynamic_templates` and nested `tool_results`/`context_messages`/`telemetry_refs`/`metrics_structured`.
- Delete `captains-index-template.json`; update `setup-elasticsearch.sh` to PUT the three new ones.
  (If Option 2 chosen: keep `captains-index-template.json` for captures+reflections, add only subagents.)

**Stale-template teardown (codex catch — required for clean re-apply).** The retired remote template
`/_index_template/agent-captains-template` (priority 110) matches both `captures-*` and `reflections-*`.
If it is left live, the new captures/reflections templates (also priority 110, overlapping patterns)
**collide at equal priority and the PUT fails**. So `setup-elasticsearch.sh` must `DELETE
/_index_template/agent-captains-template` (idempotent — 404 treated as non-fatal) **before** PUTting the
trio, via a new `delete_resource` helper. Live-apply step adds `GET /_index_template/agent-captains*` to
confirm the resolved priority ladder (captures/reflections=110, subagents=120, old name absent). The
registration-parity test asserts the script no longer PUTs `agent-captains-template` and does DELETE it.

---

## Reindex / rollover plan (acceptance §4)

All `agent-*` families are **daily indices** (`…-YYYY.MM.DD` / `…-YYYY-MM-DD`); `agent-logs` additionally
has the `agent-logs` write-alias + ILM rollover (7d/1gb). New composable templates apply to **new indices
only**:

| Family | Existing data | Plan |
|---|---|---|
| `agent-logs-*` | float-trap fields = `long` in old dailies | **No backfill.** Correct from the next daily/rollover index. Historical `long` durations are usable (just integer-valued); aggregations still work. |
| `agent-insights-*` | `evidence.component_id` = `text` (join broken historically) | **No backfill** — family has no dashboard/index-pattern yet (A1); historical join has no consumer. Correct from next index. Reindex recipe documented if ever needed. |
| `agent-monitors-slm-health-*` | `trace_id` = `text` (join broken) | **No backfill** — no index-pattern yet; correct from next probe's daily index. |
| `agent-captains-captures-subagents` | one live index, dynamic-mapped (types happen to be correct) | No backfill needed (types already correct); template prevents future drift. |
| `agent-captains-captures/-reflections` | healthy | Split is forward-only; no data change. |

Recorded in `docs/research/2026-06-08-fre-534-template-reindex-plan.md` with the exact
`_reindex` curl recipe (source→dest with corrected template) for any family if a consumer later needs
historical correctness.

---

## Build steps (TDD)

1. **Write failing test** `tests/scripts/test_es_templates.py` → verify it fails:
   - every `*.json` under `docker/elasticsearch/` is valid JSON with `index_patterns` + `template.mappings`;
   - `index-template.json` has an `ms_fields_as_float` dynamic rule and explicit `float` `calibration_threshold`/`governance_threshold`/`threshold`; `free_text` regex contains `content`/`_preview`/`_excerpt`/`summary`/`denial_reason`;
   - `insights` + `monitors-slm-health` templates exist; join keys (`evidence.component_id`/`trace_id`) resolve to `keyword`; cost/ratio/confidence resolve to `float`;
   - captains split: three templates exist, patterns don't overlap **at equal priority**, subagents priority > captures;
   - **registration parity:** every `docker/elasticsearch/*-index-template.json` is PUT by `setup-elasticsearch.sh` and vice-versa; the retired `agent-captains-template` is **not** PUT and **is** DELETEd by the script;
   - `denial_reason` resolves to `keyword` (not `text`).
   - `Command: uv run pytest tests/scripts/test_es_templates.py -q` → **expect fail** first.
2. **Edit `index-template.json`** (ms rule, 3 floats, free_text) → test step passes.
3. **Author** `monitors-slm-health-index-template.json` + `insights-index-template.json`.
4. **Split** captains into captures/reflections/subagents (per Decision A).
5. **Update** `scripts/setup-elasticsearch.sh` (PUT the new/split templates; drop the deleted one).
6. **Live apply + spot-check** (local ES :9200 is up): `bash scripts/setup-elasticsearch.sh` applies cleanly
   (incl. the `agent-captains-template` DELETE — no equal-priority collision); `GET /_index_template/agent-captains*`
   confirms the ladder (captures/reflections=110, subagents=120, old name gone); for each corrected family,
   index a sample doc into a fresh `…-tmpcheck` index built from the template and `GET _mapping` to confirm the
   field lands at the intended type and is searchable (esp. `denial_reason`=keyword agg, join keys=keyword,
   `*_ms`=float); delete the tmp indices. (Local-only verification; recorded in the PR description, not the
   checklist.)
7. **Write** the reindex-plan doc (above).
8. **Quality gates:** `uv run pytest tests/scripts/test_es_templates.py -q` · `make mypy` · `make ruff-check`
   + `make ruff-format` · `pre-commit run --all-files`. (No `make test` module is touched beyond the new
   test; full suite run before PR.)
9. **PR** with the template, then **STOP** (no merge/deploy — master closes, deploys, reindexes).

## Follow-ups to file (Needs Approval, Telemetry Surface Audit project)

- (Already covered by FRE-535/C*) — none net-new anticipated unless the spot-check surfaces an emit defect.
- If owner wants ILM/retention on `insights`/`slm-health`: new ticket (out of A2 scope).

## Acceptance mapping (ticket)

- [ ] Every ⚠️ mapping/emit row resolved or explicitly deferred → trap table + Decision B classification.
- [ ] Captures/reflections/subagents split; insights + slm-health authored (the 3 dynamic families).
- [ ] `setup-elasticsearch.sh` + template files updated; re-run applies cleanly (step 6).
- [ ] Reindex/rollover plan recorded per family (doc).
- [ ] Post-change spot check per corrected family (step 6).
