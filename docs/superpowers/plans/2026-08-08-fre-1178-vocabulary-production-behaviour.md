# FRE-1178 — ADR-0133 C2: production behaviour — never drop, publish violations against a validated denominator

## Scope (from the ticket)

Realizes ADR-0133 D4's production half. FRE-1177 (C1, merged) built the rules and made them raise in
CI. This ticket decides what happens when one fires in production, and makes that fact observable.

In production the validator never drops, rejects or mutates a record — the document is indexed
exactly as assembled, offending key intact. Two numbers are published, not one: violations and
records validated. The denominator is incremented by the validation function itself, after the rules
have run — not beside it, and not by the caller. Both numbers ride the existing joinability monitor
(`observability/joinability/`); no new monitor, no new schedule.

Backing ADR: ADR-0133 D4 (`docs/architecture_decisions/ADR-0133-typed-emit-envelope-residual-log-corpus.md`).
Blocked by FRE-1177 (`Done`).

## This ticket's acceptance criteria (from FRE-1178, not ADR-0133's own)

- **AC-1** — a violating record is stored unchanged in production mode: no exception propagates, the
  record reaches the index call, the document is byte-identical with the offending key and value
  present.
- **AC-2** — both numbers are published; the denominator counts every record that reached the
  validator (N records, M violations, M strictly between 0 and N).
- **AC-3** — the denominator is not incremented for a record the rules never ran on (rule evaluation
  fails internally → neither counter moves).
- **AC-4** — the joinability monitor's published health document carries both numbers.

## Design decisions

1. **Where the environment split and the counters live** — inside `vocabulary.py`, not in
   `es_logger.py` or `es_handler.py`. ADR-0133 D4: "The denominator is incremented by the validation
   function itself, after the rules have run — not beside it, and not by the caller." `validate_document`
   is split into a private `_check_rules` (the original three-rule logic, unchanged, still raises
   unconditionally) and a thin wrapper that calls it, increments module-level `validated`/`violations`
   counters (a lock-protected dict, exposed via `snapshot_counts()` / `VocabularyCounts`), and re-raises
   only when `settings.environment != Environment.PRODUCTION`. `es_logger.log_event`'s call site is
   unchanged — it never branches on environment itself.
2. **AC-3 falls out of the try/except shape** — the counters are only touched inside the
   `except VocabularyViolationError` branch and the no-exception success path. Any other exception
   from `_check_rules` (a bug in the validator, not a governed-vocabulary violation) propagates
   untouched, incrementing neither counter.
3. **Counters are process-lifetime cumulative, not durable across a restart** — a deliberate
   simplification matching the ticket's own scope ("no new monitor, no new schedule"). Exact
   reconciliation across restarts is ADR-0133's own AC-2/AC-5, which belongs to the seam ticket
   (FRE-1176), not this one. Documented as a known limitation on `VocabularyCounts`.
4. **Redaction is untouched and out of scope** — `_index_agent_log` still runs every document through
   `redact_mapping` (FRE-1068) regardless of vocabulary status. "Never drop, reject or mutate" describes
   the validator's own behaviour, not a claim that a separate, pre-existing security control is
   disabled. Documented explicitly (raised in codex review) so a future reader doesn't misread the two
   as in tension.
5. **`ResultDoc` gains two fields, not a new document type** — `vocabulary_validated: int = 0` and
   `vocabulary_violations: int = 0`, populated from `vocabulary.snapshot_counts()` at both places a
   `ResultDoc` is constructed (`JoinabilityWalk._build`, and `scheduler_runner.py`'s early "no eligible
   session" skip path). Kept as defaulted fields rather than required — making them required would
   have forced touching ~14 unrelated tests across `test_joinability_result.py`, `test_joinability_sink.py`
   and `test_substrate_result.py` for a hypothetical future omission codex flagged as low-severity and
   confirmed does not exist on either live call site today.
6. **ES index template updated** — `docker/elasticsearch/monitors-joinability-index-template.json`'s
   mapping is `dynamic: false`; without adding the two new fields explicitly, they would land in
   `_source` but not be indexed/aggregatable, defeating the point of publishing a rate later (ADR-0133
   AC-5, seam ticket). Caught by codex review, fixed here since it's a one-line addition directly
   supporting this ticket's own AC-4.

## Codex review (pre-PR, implementation review)

Findings and disposition:
- **High** — cross-restart counter durability: documented as a known limitation (design decision 3
  above), not built — out of this ticket's stated scope.
- **High** — redaction runs after validation, so "byte-identical" needs scoping: docstrings clarified
  (design decision 4), no functional change (redaction must keep running).
- **Medium** — ES template missing the two new fields: fixed (design decision 6).
- **Low** — `ResultDoc` defaults could mask a future omitted call site: considered and reverted (design
  decision 5) — the blast radius (14 unrelated test failures) outweighed the benefit for a
  hypothetical path codex confirmed doesn't exist today.
- Core `validate_document()` logic (environment predicate, settings singleton read, counter
  thread-safety, AC-3 shape) judged correct by the reviewer.

## Files touched

- `src/personal_agent/telemetry/vocabulary.py` — counters, `VocabularyCounts`, `snapshot_counts`,
  `reset_counts`, `_check_rules` split, environment-aware `validate_document`.
- `src/personal_agent/telemetry/es_logger.py` — docstring/comment only.
- `src/personal_agent/telemetry/es_handler.py` — comment only (the existing `_deliver` safety net).
- `src/personal_agent/observability/joinability/result.py` — two new `ResultDoc` fields.
- `src/personal_agent/observability/joinability/walk.py` — populate on `_build`.
- `src/personal_agent/observability/joinability/scheduler_runner.py` — populate on the skip path.
- `docker/elasticsearch/monitors-joinability-index-template.json` — map the two new fields.
- `tests/test_telemetry/test_vocabulary.py` — AC-1/AC-2/AC-3 at the validator level.
- `tests/test_telemetry/test_es_logger.py` — AC-1 at the `log_event`/index-call level.
- `tests/observability/test_joinability_walk_unit.py` — AC-4.
