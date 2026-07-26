# FRE-997 — Signal on the four entity-extraction fail-open defaults

**Ticket:** FRE-997 (Approved, `stream:build2`, `Tier-2:Sonnet`)
**Backing docs:** `docs/research/modeled_output_contract_audit_2026-07-26.md` §5.1 · ADR-0115 D4 (fail-open) · ADR-0109 (entity-type vocabulary, prompt-only)
**Related:** FRE-995 (the audit that produced this ticket)
**Date:** 2026-07-26

## 1. What this ticket is, and is not

Four helpers in `entity_extraction.py` silently default off-vocabulary model output to a fixed
value, each already commented `fail-open, ADR-0115 D4`. None emits anything when the default is
taken, so the semantic failure mode — the model returning something outside the vocabulary — is
currently unmeasurable. This ticket makes it measurable. It does **not**:

- change fail-open to fail-closed (a separate, evidence-driven call)
- add a receiving-side schema or structured-output contract to this path
- validate `entity_type` (the audit's second gap — explicitly out of scope, could be a follow-up
  ticket if it grows beyond a line noting the decision)

## 2. The four sites (current names — drifted from the audit's line numbers/names)

All four live inside `_finalize_extraction` (`entity_extraction.py:577`), which already has
`trace_id`, `session_id` in scope:

| Helper | Line | Off-vocabulary → | ADR/ticket |
|---|--:|---|---|
| `_normalize_entity_class` | 539 | `World` | ADR-0115 D4 |
| `_normalize_output_kind` | 559 | `knowledge` | ADR-0115 D4 |
| `_normalize_description_update_kind` | ~465 | `new` | FRE-725 |
| `_normalize_update_kind` | ~455 | `new` | FRE-712 |

`_normalize_output_kind` is the one the ticket calls out as consequential — `output_kind` is
ADR-0115's routing axis, so a wrong guess routes as `knowledge` regardless and nothing records that
a guess was substituted.

## 3. Acceptance criteria (from the ticket)

| # | Criterion | How it is proven |
|---|---|---|
| AC-1 | Each of the four coercion sites emits on the default path, carrying field, rejected value, trace_id, session_id | One test per helper feeding an off-vocabulary value, asserting the log event fired with those fields |
| AC-2 | A test asserts the signal fires for an off-vocabulary value and does not fire for a valid one | Paired test per helper (fires / does not fire) |
| AC-3 | Events are queryable in Elasticsearch; the ticket states the query | Query stated in §6 below, carried into the PR/ticket handoff |
| AC-4 | Fail-open behaviour unchanged | Existing `TestOutputKindAxis` / `TestFacetAndUpdateKind` / `TestDescriptionUpdateKind` tests in `test_entity_extraction_contract.py` continue to pass unmodified — same return values, only a log call added |

## 4. Design

**Rev 2 — revised after codex plan-review.** Codex reviewed rev 1 and returned findings ranked by
severity, one flagged fatal. §4a below is the mechanism (updated); §4b records each finding against
its resolution, including where I pushed back and why.

### 4a. Mechanism

**One log event, one shape, one place.** A single small helper does the check-and-log, called from
each of the four `_normalize_*` functions' `else` branch:

```python
def _log_fail_open_default(
    *, field_name: str, rejected_value: str, default_value: str,
    trace_id: UUID | str | None, session_id: str | None,
) -> None:
    """Emit once, whenever a fail-open helper substitutes its default (FRE-997).

    ``rejected_value`` is already the normalize helper's own stringified candidate —
    never a raw object re-stringified here — so this call introduces no new
    serialization risk. Truncated to the same bound this file already applies to
    diagnostic model output (``content_preview=content[:200]``, line 878): the
    field is enum-typed and normally a single short token, but the fail-open path
    exists precisely because the model can emit something unexpected, and this
    bounds how much of that ever reaches the log index.
    """
    log.warning(
        "entity_extraction_fail_open_default",
        field_name=field_name,
        rejected_value=rejected_value[:200],
        default_value=default_value,
        trace_id=str(trace_id) if trace_id else None,
        session_id=session_id,
    )
```

Each `_normalize_*` helper computes its candidate as a plain `str` *before* the vocabulary check
(`candidate = str(entity.get("class", "")).strip().capitalize()`, etc.) — so `rejected_value` here
is always that already-stringified candidate, never the raw model value. This is what closes the
"unusual object with a raising `__str__`" risk: there is no second stringification to raise.

**Threading trace_id/session_id.** `_finalize_extraction` already receives both as parameters. All
four helpers gain `*, trace_id: UUID | str | None = None, session_id: str | None = None`
keyword-only parameters (ADR-0074 §I3 identity threading); their **return values are unchanged**
(rev 1 imprecisely said signatures stay "intact" — they don't; behavior does).

**Why a log event and not a counter, and why `.warning`.** `structlog` warnings already ship to
`agent-logs-*` via dynamic mapping (§4b, item 3) — a bespoke counter would need its own pipeline for
no benefit that index doesn't already give. `.warning` matches this file's existing precedent for
"the model got something wrong" (`extraction_empty_response`, `entity_extraction_timeout`).

**Accepted, not defended against:** the log call itself raising and interrupting extraction. Every
value passed is a plain `str`/`None` (never a collection, never an un-stringified object), and
nothing else in this file wraps its ~15 existing `log.warning`/`log.info` calls in exception
handling. Adding one here would be inconsistent with the file's own convention and defends against a
failure mode with no precedent of occurring (CLAUDE.md §2: no error handling for impossible
scenarios).

### 4b. Codex findings and resolutions

| # | Finding | Resolution |
|---|---|---|
| 1 | Shared-helper-in-each-normalizer vs. logging centrally in `_finalize_extraction` | Codex confirmed the shared-helper shape is correct: each normalizer, not the caller, knows whether its candidate was accepted. Kept. Fixed the plan's imprecise "signatures intact" claim. |
| 2 (major) | Passing tests ≠ proof of unchanged behaviour; exception-safety, identity-threading-at-production-call-sites uncovered | `rejected_value` is now always the already-stringified candidate — removes the re-stringification risk entirely, at the design level, not by adding a try/except. New tests (§5) drive the **full** `extract_entities_and_relationships` path via the existing `_run_extractor` harness — the same path every other test in this file uses — so identity threading through `_finalize_extraction`'s real call sites is what's actually exercised, not the bare helper in isolation. |
| 3 (major) | AC-3's zero-hit query proves syntax, not that fields are mapped/indexed/aggregatable | Checked `docker/elasticsearch/index-template.json`: `"dynamic": true` with named `dynamic_templates` (`enums_keyword` matches `*_name`/`*_kind`/etc. → `keyword`; everything else falls to `default_string_keyword` → `keyword`, `ignore_above: 1024`). Renamed `field` → `field_name` so it lands in `enums_keyword` (matches the existing convention) instead of the generic catch-all; `rejected_value`/`default_value` fall to `default_string_keyword`, which is the correct type for a terms aggregation. This is the same mechanism every other ad-hoc log field in this codebase already relies on — stated explicitly in §6 rather than assumed. |
| 4 (fatal, per codex; downgraded here with reasoning) | Logging arbitrary model output risks PII/secrets | The value is a single enum-typed field's off-vocabulary token, not free-form content — and this file already logs truncated raw model output for diagnosis at equal or greater exposure (`content_preview=content[:200]`, line 878; `session_summary.py`'s `_MAX_FAILURE_DETAIL_CHARS`). I don't read this as a new risk class the codebase hasn't already accepted, but the mitigation (200-char truncation, matching that exact existing precedent) is added anyway since it's nearly free and directly bounds the pathological case codex is pointing at. |
| 5 (minor) | "Emit once" is claimed, not asserted | One test asserts exactly one log call for one off-vocabulary field. |
| 5 (minor) | Rate denominator imprecise — `entity_extraction_completed` has no per-field breakdown | Fixed in §6: overall rate = fail-open events (any field) ÷ completed events; per-field breakdown is for diagnosing *which* field, not a second rate. |

## 5. Steps

| # | Step | Verify |
|---|---|---|
| 1 | Failing tests: for each of the 4 helpers, an off-vocabulary value fires the log event with the right field/rejected_value/trace_id/session_id, and a valid value fires nothing | `make test-file FILE=tests/test_second_brain/test_entity_extraction_contract.py` — new tests fail (no such log event yet) |
| 2 | Add `_log_fail_open_default`; thread `trace_id`/`session_id` through the four helpers and their call sites in `_finalize_extraction` | New tests pass |
| 3 | Confirm existing tests (`TestOutputKindAxis`, `TestFacetAndUpdateKind`, `TestDescriptionUpdateKind`, `TestExtractionEmissionContract`) are unmodified and still pass — proves fail-open behaviour is unchanged | Full file green |
| 4 | State the ES query for the rate (AC-3) in the PR/ticket handoff | Query runs against `agent-logs-*` locally, returns 0 hits (no live traffic yet) without erroring |
| 5 | Quality gates | `make test` (module → full) · `make mypy` · `make ruff-check` + `make ruff-format` · `pre-commit run --all-files` |
| 6 | Self-review (`code-review` at `low` — small, localized, no schema/security/cost/memory surface) | Findings fixed on-branch |
| 7 | PR + ticket handoff | Per-AC evidence, self-review summary, the ES query, context disposition |

## 6. The ES query (AC-3 — stated so a later reader doesn't rediscover field names)

```json
GET agent-logs-*/_search
{
  "query": { "term": { "event": "entity_extraction_fail_open_default" } },
  "aggs": {
    "by_field": {
      "terms": { "field": "field_name" },
      "aggs": { "rejected_values": { "terms": { "field": "rejected_value", "size": 20 } } }
    }
  }
}
```

Both `field_name` and `rejected_value` are indexed as `keyword` under `index-template.json`'s
dynamic templates — `field_name` matches the `enums_keyword` template (`*_name` → keyword);
`rejected_value` falls to `default_string_keyword` (keyword, `ignore_above: 1024`). No mapping
change needed; this is the same mechanism every other ad-hoc structlog field in this codebase
already relies on.

**Overall rate** over a window = count of `entity_extraction_fail_open_default` events (any
`field_name`) ÷ count of `entity_extraction_completed` events (already emitted,
`entity_extraction.py:903`) over the same window. The `by_field` aggregation above breaks that total
down by which of the four fields is defaulting most — a diagnostic split, not a second rate,
since `entity_extraction_completed` has no per-field count to divide by.

## 7. Halt conditions

- Any existing test's asserted return value changes → stop; that would be a fail-open behaviour
  change, out of scope.
- `entity_type` validation scope creep beyond a one-line note → split into its own ticket per the
  ticket's own instruction.
