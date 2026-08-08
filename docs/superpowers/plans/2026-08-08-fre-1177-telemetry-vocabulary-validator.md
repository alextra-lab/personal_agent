# FRE-1177 — ADR-0133 C1: the telemetry vocabulary and the validator inside `es_logger.log_event`

## Scope (from the ticket)

Add `src/personal_agent/telemetry/vocabulary.py` — declared governed names, the spellings each
retires, each name's declared type, and Rule 2's exception list. Apply the validator inside
`ElasticsearchLogger.log_event`, to the assembled `doc` dict, before the write. Three rules in
priority order: exact-match retired spelling (Rule 1) → near-miss at 0.85 `difflib.SequenceMatcher`
similarity with a closed exception list (Rule 2) → declared-type check (Rule 3). Unrecognised keys
pass. Under test/CI a violation **always raises** — no environment split in this ticket (FRE-1178
owns the production never-drop + counter behaviour). `log_batch` has no production caller today and
is explicitly out of scope until it acquires one.

Backing ADR: ADR-0133 D1–D4 (`docs/architecture_decisions/ADR-0133-typed-emit-envelope-residual-log-corpus.md`).
Sequenced after FRE-1064 (done, `Done` state confirmed via `get_issue`).

## This ticket's acceptance criteria (from FRE-1177, not ADR-0133's own)

- **AC-1** — every declared retired spelling rejected, parameterised over the whole committed table.
- **AC-2** — every governed name carrying a declared type rejects a wrong-typed value, parameterised.
- **AC-3** — threshold is 0.85: `tarce_id` (0.875) and `sesion_id` (0.947) raise; `component` (0.857,
  the committed exception) does not.
- **AC-4** — an unrecognised key (`queue_depth`) passes cleanly.
- **AC-5** — a violation reaches the validator via `es_handler`'s plain-`logging` fallback branch
  (record.msg not a dict), not only the structlog path.
- **AC-6** — a violation in a key `log_event` itself merges (`@timestamp`, `trace_id`, `span_id`,
  `event_type`) — not supplied by the caller's `data` dict — is still detected.

## Design decisions

1. **Vocabulary data model** (`vocabulary.py`, pure data + one pure function, no I/O):
   - `RETIRED_SPELLINGS: dict[str, str | None]` — retired spelling → canonical name, or `None` when
     there is no canonical log field (the `duration_ms` / `latency_ms` row — intrinsic span duration).
   - `DECLARED_TYPES: dict[str, type]` — governed name → its declared Python type.
   - `NEAR_MISS_THRESHOLD: float = 0.85`.
   - `NearMissException` frozen dataclass + `NEAR_MISS_EXCEPTIONS: dict[str, NearMissException]`
     keyed by the excepted key, each entry naming the governed name it was mistaken for, the measured
     similarity, and a reason (ADR-0133 D3: "An exception without a stated reason is a defect").
   - `GOVERNED_NAMES: frozenset[str]` derived from the canonical sides of the two tables above — the
     near-miss comparison set. A key equal to a governed name is never itself a near-miss.
   - `validate_document(doc: Mapping[str, object]) -> None` — the validator, in Rule 1 → 2 → 3 order,
     raising `VocabularyViolationError` (new, in `personal_agent/exceptions.py`, `ValueError`
     subclass per file convention) on the first violation found.
   - Seeded content: the 5 divergence rows ADR-0133 D3 tabulates (11 individual retired spellings
     across them) and the spine fields already load-bearing in this codebase's own write path
     (`@timestamp`, `event_type`, `trace_id`, `span_id`, `session_id`, `component_id`, `user_id`,
     `input_tokens`, `output_tokens`) with types. **Not** attempting to reproduce the ADR's full
     "59 cross-family names" census — that figure came from an ephemeral `ast-grep` run during ADR
     authoring with no committed data file, and full corpus coverage is explicitly AC-7's job, which
     belongs to the seam ticket FRE-1176 (parked, due 2026-10-15), not this one. Documented in the
     module docstring so it isn't mistaken for the finished census.
   - `component_id` must be a declared governed name for the `component` exception to mean anything
     structurally (0.857 similarity is `component` against `component_id`, verified against the
     ADR's own worked figure).

2. **Wiring** (`es_logger.py`): one import + one call. `validate_document(doc)` runs immediately after
   the `doc = {...}` dict literal (currently ~`es_logger.py:236-242`) and before the
   `self._index_agent_log(doc, index=index)` call — critically **outside** the existing
   `try/except Exception` that wraps that call, so the raise actually propagates (a raise caught by
   that handler would just log `elasticsearch_log_failed` and return `None`, silently defeating
   AC-1..AC-6's "raises" requirement).

3. **No environment split in this ticket, but `log_event`'s "always raises" is scoped to its own
   callers, not the whole queued pipeline** — a distinction the original plan missed and a codex
   plan-review (post-implementation, 2026-08-08) surfaced. `es_logger.log_event` itself always
   raises `VocabularyViolationError` under test/CI, proven directly (AC-1..AC-6). But
   `ElasticsearchHandler._deliver()` — the real `emit()` -> queue -> `_deliver()` path every
   structlog call in the app goes through — cannot let that propagate: the existing corpus already
   carries retired spellings (`duration_ms`/`latency_ms`) at several live, frequently-hit call sites
   (`llm_client/client.py`, `llm_client/cost_tracker.py`, `orchestrator/executor.py`'s skill-routing
   path, `memory/service.py`'s recall path, `second_brain/`, `captains_log/feedback.py`,
   `orchestrator/context_compressor.py`). Letting a violation kill the background delivery consumer
   task would break ES telemetry delivery almost immediately on any real run, before those call
   sites are ever cleaned up. `_deliver()` instead catches `VocabularyViolationError` distinctly from
   `write_failures` — logs it at ERROR with the violated field/rule, counts it under a new
   `vocabulary_violations` stat, and keeps the consumer alive. FRE-1178 (C2) still owns wiring that
   counter into the joinability monitor for real production observability; this ticket only keeps a
   violation from being indistinguishable from a transient ES error at delivery time. Cleaning up
   the existing `duration_ms`/`latency_ms` call sites is real, valuable follow-up work, but is a
   larger diff across unrelated files and belongs in its own ticket, not folded into this one.

## Files touched

- `src/personal_agent/telemetry/vocabulary.py` — new.
- `src/personal_agent/telemetry/es_logger.py` — import + one `validate_document(doc)` call in
  `log_event`, placed before the existing try/except.
- `src/personal_agent/exceptions.py` — add `VocabularyViolationError(ValueError)`.
- `tests/test_telemetry/test_vocabulary.py` — new; AC-1..AC-4, parameterised over the vocabulary.
- `tests/test_telemetry/test_es_logger.py` — add AC-6 case(s) (merged-key violation via `timestamp=`/
  `span_id=` kwargs, not `data`).
- `tests/test_telemetry/test_es_handler.py` — add AC-5 case (plain-`logging` fallback branch via
  `_build_item`, then `log_event` raises).

Chosen test location: `tests/test_telemetry/` (not `tests/personal_agent/telemetry/`) — matches where
the existing `es_logger.py`/`es_handler.py` tests already live in this tree.

## Steps

1. Add `VocabularyViolationError` to `exceptions.py`.
2. Write `tests/test_telemetry/test_vocabulary.py` (failing first) covering AC-1..AC-4.
3. Write `vocabulary.py` to make those tests pass.
4. Wire `validate_document` into `es_logger.log_event`; add the AC-6 test case to
   `test_es_logger.py` (failing first, then passing).
5. Add the AC-5 test case to `test_es_handler.py` (failing first, then passing).
6. `make test-file FILE=tests/test_telemetry/test_vocabulary.py`,
   `make test-file FILE=tests/test_telemetry/test_es_logger.py`,
   `make test-file FILE=tests/test_telemetry/test_es_handler.py`, then full `make test`.
7. `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`.
8. Self-review: `feature-dev:code-reviewer` + `security-review` against
   `git diff origin/main...HEAD`. This diff sits directly in the `agent-logs` production write
   path (`es_logger.log_event`'s single production caller is the ES write chokepoint) — **escalates**
   per Step 8 trigger 1. Self-serve review still runs and fixes land on-branch; PR is flagged for
   owner `/code-review ultra` before merge.
9. PR + Linear handoff comment per skill Step 9.

## Risk tier

**Standard** — touches `src/` logic on a production write path and implements a new ADR's design.
Codex plan-review required before coding.
