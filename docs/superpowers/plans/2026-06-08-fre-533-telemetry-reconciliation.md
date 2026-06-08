# FRE-533 — Three-way reconciliation inventory (emit-site ↔ ES mapping ↔ dashboard)

**Ticket:** FRE-533 (Approved, Tier-1:Opus) · Project: Telemetry Surface Audit
**Blocks:** FRE-534 (A2 template fixes), FRE-535 (B1 dashboard triage), FRE-540 (A3 CI checker)
**Refs:** ADR-0074 (joinability), ADR-0083 (SLM health), ADR-0088 (topology), ADR-0089 (artifact envelope), ADR-0065 (cost gate), FRE-452 (route trace ledger)

## Deliverable

A single dated research doc: `docs/research/2026-06-08-fre-533-telemetry-surface-reconciliation.md`
containing the three-way reconciliation table + per-family live-vs-repo divergence + the
dashboard-provenance answer. **No `src/` change.** PR is docs-only (plus any committed extraction
helper under `scripts/` if reused by A3).

## Method — measure, don't assert (FRE-433/434 methodology)

Every field walked through all three corners programmatically. Per the standing "you always get ES
mappings wrong first pass" rule, extraction is script-driven, not eyeballed.

### Corner 1 — Mapping (live + repo), per family
For each of the 6 families:
1. **Union** `_mapping` across ALL concrete indices in the family (`GET /<family-*>/_mapping`), not just
   the newest — older daily indices carry fields later dropped from emit, and a field's `_source` may be
   present without ever triggering a dynamic mapping (null/`[]`-only values). Flatten to
   `field → {type, ignore_above, fields.keyword?, dynamic-template-hit?}`.
   - For `dynamic:false` / `enabled:false` subtrees (e.g. `orphans.detail` in joinability is
     `object/enabled:false`), the keys live in `_source` but never appear in the mapping — extract them by
     sampling actual docs per `event_type`, and classify as **source-only / intentionally-unindexed**, not
     "emitted-but-unmapped".
2. Read repo template source-of-truth:
   - `agent-logs-*` → `docker/elasticsearch/index-template.json`
   - `agent-captains-captures-*` + `agent-captains-reflections-*` → `docker/elasticsearch/captains-index-template.json` (ONE template, TWO doc shapes — verify both)
   - `agent-captains-captures-subagents` → inherits `agent-captains-template` via the `agent-captains-captures-*` pattern (recon finding; ticket said "none")
   - `agent-insights-*` → no template (pure dynamic)
   - `agent-monitors-joinability-*` → `docker/elasticsearch/monitors-joinability-index-template.json`
   - `agent-monitors-slm-health-*` → NO matching template (recon finding; `slm-requests-index-template.json` targets `slm-requests-*`, zero live indices)
3. **Resolve each field through the template's `dynamic_templates` block, not just `properties`** (Codex
   review — this is the first-pass-wrong trap). Named rules observed in the repo templates:
   - `ids_keyword` (`*_id` → keyword), `enums_keyword` (`*_type|*_name|*_role|*_status|*_decision|...` → keyword)
     — a field hitting these is *explicitly typed*, not unmapped.
   - `free_text` (`*_message|*_content|*_description|reason|hint|stderr|stdout|raw_*|*_text|*_prompt` → `text`)
     — dashboard `.keyword` aggs on these silently fail unless a `fields.keyword` subfield exists.
   - `ms_fields_as_float` (`*_ms|*_latency|*_duration` → float) is present in `captains-` and `slm-requests-`
     templates but **ABSENT from `index-template.json`** → `agent-logs-*` timing fields hit the default
     `0.0`→`long` trap while captains/slm are protected. Track this asymmetry per family.
   - `default_string_keyword` (unmatched string → `keyword ignore_above:1024`) → long values silently
     truncated at 1024 bytes; classify as type-mismatch-risk, not aligned.
4. Note `dynamic: true|false|strict` at each object root. `strict` turns "emitted-but-unmapped" into
   **index-rejected** (whole-doc drop) — qualitatively worse; check whether any template uses it.
5. `.keyword` subfield resolution: before calling a dashboard `field.keyword` reference aligned, confirm the
   parent is `text` *with* a `fields.keyword` subfield (bare `keyword` fields have no `.keyword`).
6. Diff live mapping vs repo template → record divergence (drift is itself a finding).
7. Cross-check ES setup path: `scripts/setup-elasticsearch.sh` (+ `docker/elasticsearch/*`).

### Corner 2 — Emit sites (code)
Grep every ES writer + structlog field across emit modules:
`telemetry/`, `captains_log/`, `observability/`, `cost_gate/`, `insights/`, `second_brain/`,
`orchestrator/`, `brainstem/`, `transport/`. Record `file:line`, field name, emitted Python type/value.
Honor the `event` (log files) vs `event_type` (ES) key split (FRE-407/409 trap).

**Grep alone is fooled — supplement with source-of-truth tracing (Codex review).** Field names are often
runtime dict keys, not string literals. Concrete cases to trace, not grep:
- `**data` / `**event_dict` spreads in `telemetry/es_logger.py` → field set is the caller's dict; capture an
  actual emitted `_source` shape (fixture record → inspect doc) rather than reading literals.
- `**_identity_fields` / `payload.update(extra)` in `llm_client/telemetry.py` → grep `_identity_fields =`
  and trace callers passing `extra`.
- `.model_dump(mode="json")` in `observability/joinability/sink.py` + `observability/slm_health/sink.py` →
  field names come from the Pydantic models (`result.py`, `snapshot.py`); read the model field defs.
- `asdict(record)` dataclasses in `telemetry/tool_result_digest.py`, `telemetry/within_session_compression.py`
  → read the dataclass `__annotations__`.
- Where static tracing is ambiguous, **capture the live doc shape**: the union-mapping from Corner 1 already
  reflects what actually landed — reconcile grep-found literals against that union and flag any
  mapping-present field with no locatable emit literal as **needs-runtime-confirmation** rather than dead.
(`structlog .bind()` is not used in the telemetry paths — confirmed, not a blind spot.)

### Corner 3 — Dashboards
1. Parse all repo NDJSON: `config/kibana/dashboards/*.ndjson` (12 dashboards) + `docker/kibana/dashboards/prompt-cost-cache.ndjson` (1) → extract field references per visualization/lens (searchSourceJSON, aggs `field`, lens column `sourceField`).
2. Query LIVE Kibana saved objects (`POST /api/saved_objects/_find?type=dashboard&type=visualization&type=lens`) → diff against repo NDJSON to answer the provenance/drift question (repo complete? stale? live-only dashboards?).
3. Confirm/correct the "12 dashboards / 57 visualizations" count (recon: repo viz sum ≈ 66 + 3 lens — reconcile the discrepancy).

### Synthesis — the reconciliation table
One row per `(field, family)`. Columns: field · emit site(s) `file:line` · emitted type · mapped type
(explicit / dynamic) · dashboard refs · **classification**:
- ✅ aligned
- ⚠️ emitted-but-unmapped (flag trap classes: float/cost/ratio `0.0`→`long`; long text/error/digest under `keyword ignore_above:1024` silent drop)
- ⚠️ type-mismatch
- ⚠️ mapped-but-dead
- ⚠️ dashboard-references-missing-field (silent empty panel)
- ⚠️ key-drift (emit name ≠ mapped name ≠ panel field)
- ⚠️ index-rejected (field violates a `dynamic: strict` root → whole-doc drop)
- ℹ️ source-only / intentionally-unindexed (`dynamic:false` or `enabled:false` subtree — in `_source`, not searchable)

Every ⚠️ row gets a one-line resolution direction (fix mapping / fix emit / fix panel / retire) so
A2/B1/C* execute mechanically.

## Steps (atomic)

1. **Extraction harness** — write `scripts/audit/fre533_reconcile.py` (or a small set of shell+python steps under a scratch dir) that dumps, per family: flattened live mapping JSON, repo template explicit props, and dashboard field refs. Deterministic, re-runnable. Output JSON intermediates to a gitignored scratch dir (`/tmp/fre533/`), not committed unless reused by A3.
   - verify: re-run produces identical output; all 6 families + 13 dashboards parsed without error.
2. **Corner 1 data** — produce flattened live mapping + repo-template prop table per family; record live-vs-repo divergence list.
   - verify: every leaf field in each live mapping appears in the table with a type.
3. **Corner 2 data** — emit-site grep table (`file:line` → field → emitted type) for each family's writer path.
   - verify: each family has at least its primary writer located; `event`/`event_type` split recorded.
4. **Corner 3 data** — dashboard field-reference table + live-vs-repo saved-object diff + corrected dash/viz counts.
   - verify: every dashboard's index-pattern resolves to a known family; provenance question answered yes/no with location.
5. **Synthesize** the reconciliation table; classify every row; one-line resolution per ⚠️.
   - verify: each row has all three corners populated or an explicit "—" with reason; trap-class scan applied to every float/cost/ratio and long-text field.
6. **Write** `docs/research/2026-06-08-fre-533-telemetry-surface-reconciliation.md` (dated, indexed in the research README if one exists; include a Method/Process section per the research-doc convention).
   - verify: all 5 acceptance checkboxes satisfied; doc renders.
7. **Quality gates** — no src change, so: `make ruff-check`/`ruff-format` only if a helper script is committed; `pre-commit run --all-files` (path/secret guard); confirm `make test` is untouched-green is N/A (no code). 
8. **PR** (docs-only) → STOP.

## Acceptance (from ticket)
- [ ] Reconciliation table — 6 families, every field, all three corners.
- [ ] Every ⚠️ row has a one-line resolution direction.
- [ ] Live-vs-repo template divergence documented per family.
- [ ] Dashboard-provenance question answered (NDJSON in git? where? complete vs live?).
- [ ] Findings in `docs/research/` (dated).

## Open decisions for owner
- **Commit the extraction harness?** Recommend yes under `scripts/audit/` since FRE-540 (A3 CI checker) will reuse the static mapping↔dashboard parse. Alternative: keep it scratch-only and let A3 rebuild.
- **Depth of Corner-2 for `agent-logs-*`:** it carries dozens of distinct event shapes (gate/cost/turn/topology). Propose: enumerate every field present in the live mapping, but locate emit sites per *event family* (group by `event_type`) rather than per individual structlog call, to keep it tractable. Flag if you want exhaustive per-call.
