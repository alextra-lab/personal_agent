# FRE-571 — ES mappings for new ADR-0092 `turn_status` session fields

**Ticket:** FRE-571 (Approved, Tier-3:Haiku, Observability Foundation)
**Refs:** ADR-0092 §D9 + verification item 8 · ADR-0090 §D2/§D6 · FRE-570 (final field names, shipped PR #230 → `main`) · FRE-567 (sibling pattern, just shipped 9cb0ca4)

## Problem

ADR-0092 impl 2/5 (FRE-570) added six session-scoped fields to the `turn_status`
projector payload (`src/personal_agent/observability/topology/projector.py:417-447`):

| Field | Python type at source | Trap class |
|-------|----------------------|------------|
| `session_cost_usd` | `float` = `round(sum(costs), 6)` | **float→long** (first value `0.0` infers `long`, truncates later decimals) |
| `session_context_tokens` | `int` | none (integer) — explicit anyway per D6 |
| `compaction_count` | `int` = `len(set)` | none (integer) |
| `cache_reset_count` | `int` = `len(set)` | none (integer) |
| `quality_alert_count` | `int` = `len(set)` | none (integer) |
| `quality_alert` | `dict\|None` `{severity: Literal["high","low"], phases_fired: list[int]}` | object subfield types |

ADR-0090 §D6 done-bar: a new field is not shippable-to-default until its
**trap-class fields are explicitly mapped** in the agent-logs template (Guarded-dynamic:
`dynamic:true` + explicit props for every known numeric/long-text/object field). This
ticket closes that bar for the six new field names so that whenever they reach
`agent-logs-*` they are correctly typed, not silently coerced.

**Note (verified):** these fields currently flow only to Postgres `session_events` +
the live WS queue (`emit_turn_status` → `_push_event`), **not** ES — there is no
structlog/ES emit path today. The mapping is therefore defensive/pre-positioned, exactly
as ADR-0090 Guarded-dynamic intends (explicit props for known field names ahead of any
catch-all landing). Fields are mapped **top-level**, matching every other numeric field in
this flat structlog catch-all template.

**Caveat (codex review):** top-level mapping is correct **only if** a future producer
logs these as flattened structlog kwargs at the call site (the natural pattern, matching
all existing template props). It would NOT fire if a producer instead passes the whole
`StateUpdateEvent` envelope to structlog — those land nested as `value.*`
(`adapter.py:68`). The mapping is pre-positioned for the flattening path, not the
envelope-passthrough path; the latter is not how anything in this codebase logs to ES.

## Design decisions

- `session_cost_usd` → **`double`** (NOT `float`/`scaled_float` as the pre-FRE-570 ticket
  text guessed). Rationale: every sibling `*_usd` money field in this template is `double`
  (`cost_usd`, `amount_usd`, `actual_cost_usd`, `reserved_usd`, `delta_usd`,
  `reservation_amount_usd`, `running_total`, `cap_usd`), guarded by
  `test_logs_cost_gate_money_fields_are_double`. `double` avoids the float→long trap
  (the ticket's real requirement) *and* preserves precision for summed cost. Deviation
  flagged for owner.
- `session_context_tokens` → **`long`** (token magnitude; matches `total_tokens: long`).
- `compaction_count`, `cache_reset_count`, `quality_alert_count` → **`integer`**
  (matches the `*_count` convention: `message_count`, `tool_count`, `sub_agent_count`).
- `quality_alert` → **`object`** with explicit subfields
  `severity: keyword`, `phases_fired: integer`. No text/digest subfield exists in the
  final shape, so no `ignore_above` silent-drop risk; subfields pinned explicitly anyway.

## Steps

1. **TDD — failing test first.**
   Add to `tests/scripts/test_es_templates.py` a new test
   `test_logs_adr0092_turn_status_session_fields_explicit` asserting:
   - `session_cost_usd` → `double`
   - `session_context_tokens` → `long`
   - `compaction_count`, `cache_reset_count`, `quality_alert_count` → `integer`
   - `quality_alert.type == object`, `quality_alert.properties.severity → keyword`,
     `quality_alert.properties.phases_fired → integer`
   Run: `make test-file FILE=tests/scripts/test_es_templates.py` → confirm the new test
   **FAILS** (fields absent), all others pass.

2. **Implement — add explicit properties.**
   In `docker/elasticsearch/index-template.json`, add the six properties to
   `template.mappings.properties` (place near the existing token/cost block for
   readability). Update `_meta.description` to note the ADR-0092 turn_status session
   fields (FRE-571).
   Run: `make test-file FILE=tests/scripts/test_es_templates.py` → all **PASS**.

3. **Quality gates.**
   - `make test-file FILE=tests/scripts/test_es_templates.py` (module) → green
   - `make test` (full unit suite) → green
   - `make mypy` → no new errors (no `src/` change)
   - `make ruff-check` + `make ruff-format` → clean
   - `pre-commit run --all-files` → clean

4. **PR + Linear handoff.** Post-deploy runbook for master:
   - setup script PUTs the template + `apply_live_index_mapping` patches the live write
     index; new field names → additive, no **type** conflict expected (no ES emit path
     today).
   - **Pre-deploy headroom check (codex review):** `ignore_dynamic_beyond_limit` protects
     only *dynamic* ingestion — an explicit-property update on a live index already at the
     300-field cap would 4xx and `apply_live_index_mapping` treats 4xx as a hard failure.
     Master verifies the current write index has ≥6 fields of headroom before deploy:
     `curl -s .../agent-logs-<today>/_mapping | jq '...properties | length'` (expect well
     under 300; +6 must stay under).
   - master verifies applied types via `_field_caps`.
   STOP — no merge/deploy.

## Out of scope
- No projector / emit changes (FRE-570 owns those).
- No new ES emit path for turn_status (none exists; not this ticket).
- No dashboard/NDJSON panel (ADR-0090 D3 — separate if/when surfaced).
