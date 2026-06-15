# FRE-544 — Bound `agent-logs-*` dynamic field growth

> **Date:** 2026-06-15 · **Ticket:** FRE-544 (Tier-1:Opus) · **Project:** Telemetry Surface Audit
> **Refs:** ADR-0090 (Telemetry Surface Contract — §D2 designates `agent-logs-*` **Guarded-dynamic**;
> §D5 = FRE-540 reconciliation checker, the emit↔mapping↔dashboard coupling enforcement) ·
> FRE-533 (A1, the 768-field finding + 1023-row CSV) · FRE-534 (A2 trap fixes — must preserve)
>
> **Posture (owner-confirmed 2026-06-15):** this ticket bounds growth *within* ADR-0090's already-decided
> Guarded-dynamic discipline for `agent-logs` (it is an open-ended catch-all; **Locked**/`dynamic:false`
> is reserved for closed field sets like `user-turn-ratings`). The cap is the field-count backstop the
> discipline lacked; FRE-540 is the drift enforcement. Flipping `agent-logs` to Locked would be an
> ADR-0090 revision (adr-session), out of scope here.

## Problem
`docker/elasticsearch/index-template.json` (`agent-logs-*`) is `dynamic:true` with **no field cap**.
A1 measured **768 dynamically-mapped leaves** (only ~98 explicit), **507 of them generic
emitted-but-unmapped sprawl** → unbounded mapping growth, mapping-explosion risk, slow cluster-state.

## Decision — bounded-dynamic (measured; alternatives refuted on the live local ES)
The containment strategy was **chosen by measurement**, not assertion (per the standing "you always
get ES mappings wrong first pass" rule). Probes against local ES (8.19):

| Candidate | Result | Verdict |
|---|---|---|
| `dynamic:"runtime"` | A novel **nested object** (`context.deep`, …) → `illegal_state_exception` **HTTP 500, whole doc dropped**. Also: the existing `default_string_keyword` catch-all still **indexes** every string (runtime never engaged). | **Refuted** — drops telemetry; doesn't bound. |
| `dynamic:"strict"` | (ticket) rejects unknown-field docs | Refuted — drops telemetry. |
| `dynamic:false` everywhere | Safe for nested objects, but **disables `dynamic_templates`** (loses `*_ms`→float, `*_id`→keyword, free_text typing) and makes all 507 sprawl leaves **`_source`-only / unqueryable**. High maintenance for a sprawling catch-all. | Rejected — too costly; kills ad-hoc queryability the "agent reads its own logs" goal needs. |
| **`dynamic:true` + `total_fields.limit` + `ignore_dynamic_beyond_limit:true` + collapse biggest subtree** | Over-cap doc (40 novel leaves, cap 12) → **HTTP 201, indexed**; excess fields silently skipped (kept in `_source`), **doc NOT dropped**. Nested objects index fine. `dynamic_templates` + FRE-534 typing intact. `arguments` as `object/dynamic:false` collapses 66 leaves → keeps 3 explicit, ignores new args (measured 201, 3000-char arg fine). | **Chosen.** |

**Why this fits `agent-logs-*` specifically:** it is a heterogeneous telemetry catch-all that (a) must
**never drop docs**, (b) receives **arbitrary nested objects**, (c) needs **ad-hoc queryability**
during incidents. `ignore_dynamic_beyond_limit:true` is the key safety: hitting the cap skips new
*dynamic* fields without erroring the doc. Existing 768-field indices **age out via the 30d ILM**
(`ilm-policy.json`); the bound applies to new daily indices.

## Files

### 1. `docker/elasticsearch/index-template.json`
- **settings** — add:
  ```json
  "index.mapping.total_fields.limit": 300,
  "index.mapping.total_fields.ignore_dynamic_beyond_limit": true
  ```
  (Cap is generous over the ~110 legitimate explicit fields and well under the 768 runaway; the
  `ignore_*` flag makes mis-sizing non-catastrophic — fields skipped, docs kept.)
- **`arguments`** — collapse the largest + fastest-growing subtree (66 leaves, only 3 panel-referenced):
  ```json
  "arguments": { "type": "object", "dynamic": false, "properties": {
    "name": {"type":"keyword"}, "title": {"type":"keyword"},
    "trace_id": {"type":"keyword"} } }
  ```
  (`dynamic:false` keeps the 3 dashboard-referenced subfields indexed, sends new tool-arg keys to
  `_source` only — measured. `arguments.trace_id` stays `keyword`: join key.)
- **Promote ALL 45 dashboard-referenced fields to explicit so the cap can never skip a panel field**
  (codex Q2). 25 are already explicit; the other 20 (types = live, no behaviour change):
  - **8 scalars:** `attempt_number` long · `cpu_load` float · `memory_used` float · `from_state`
    keyword · `role` keyword · `target_agent` keyword · `title` keyword · `trimmed` boolean.
  - **3 via the `arguments` collapse** (above): `arguments.name/title/trace_id`.
  - **nested objects** (referenced sub-fields made explicit; parents stay `dynamic:true` so their
    small non-referenced leaves keep current typing — no FRE-534 regression — and the cap bounds them):
    - `context_messages`: `{properties:{role: keyword}}`
    - `messages_preview`: `{properties:{role: keyword}}`
    - `phases`: `{properties:{offset_ms: float}}`
    - `phases_summary`: `{properties:{ llm_inference|other|persistence|setup|synthesis|tool_execution:
      {properties:{duration_ms: float}} }}` (6 phases)
  - Promoting these `*_ms` durations as explicit `float` also hardens them against the 0→long trap.
  - (The `role.keyword`/`from_state.keyword` *dashboard* mismatch is FRE-535/B1's scope — **not**
    changed here; keeping them bare `keyword` preserves current live behaviour.)
- **`_meta`** — record the strategy + cap rationale + that FRE-534 fixes are preserved.
- **Preserve unchanged:** `dynamic:true`, all 98 existing explicit props, all 5 `dynamic_templates`
  (incl. `ms_fields_as_float`), `event`/`event_type` keyword, the FRE-534/536 trap fixes.

### 2. `docs/research/2026-06-15-fre-544-agent-logs-field-growth-containment.md` — decision doc
The "ADR-style decision doc" acceptance item: the table above + the measured probes (commands +
results) + the reindex/age-out plan. (Build session writes a research/decision doc, not a numbered
ADR — that is the `adr` session's lane; note in handoff that master/adr may promote it.)

### 3. `tests/scripts/test_es_templates.py` — guards
- `total_fields.limit == 300` and `ignore_dynamic_beyond_limit is True` in settings.
- `arguments` is `object` with `dynamic:false` and explicit `name`/`title`/`trace_id` (trace_id keyword).
- the 8 promoted scalars are explicit with the expected types.
- regression: `mappings.dynamic` still `true`; `event` & `event_type` still `keyword`;
  `ms_fields_as_float` rule still present (guards against FRE-534 regression — some already covered).

## Verification (TDD order)
1. Add guards → `make test-file FILE=tests/scripts/test_es_templates.py` → **fail**.
2. Edit template → tests **pass**.
3. JSON valid (`python -c json.load`); `make test` (regression); `make mypy` (no py change, sanity);
   `make ruff-check`/`format`; `pre-commit run --all-files`.
4. (Done during planning) live-ES probe confirms cap+ignore+arguments behaviour; probe indices cleaned up.

## Post-deploy (Linear comment for master — NOT in PR checklist)
- Re-run `scripts/setup-elasticsearch.sh` (or `ENV=cloud`) to register the updated `agent-logs-template`.
- Applies to **new** daily indices only. Existing 768-field `agent-logs-*` indices are left as-is and
  **age out via the 30d ILM** — no reindex, no back-fill, no field drop on historical data.
- Verify on the next new daily index: `GET agent-logs-<new>/_settings` shows the limit +
  `ignore_dynamic_beyond_limit`; `GET .../_mapping` shows `arguments.dynamic:false`; field count grows
  bounded.

## Halt-condition check
One template + decision doc + test guards = **one PR**. No historical rows dropped (existing indices
untouched; age out via ILM — surfaced, not quarantined). No ADR-phase bundling. No Python source
touched → no mypy regression. FRE-534 trap fixes explicitly preserved + guarded.
