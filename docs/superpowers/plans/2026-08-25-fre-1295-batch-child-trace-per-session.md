# FRE-1295: Mint a child trace per session inside batch passes

**Ticket**: https://linear.app/frenchforest/issue/FRE-1295
**Backing ADR**: ADR-0074 §I3 (per-operation traces), §8c (joinability); ADR-0129 D1/D3 (OTel owns trace identity; root spans on background entrypoints)
**Related**: FRE-693 (joinability probe orphan rule), FRE-1069 (the root-span change that introduced this regression)

**Revision note**: this plan was corrected after `codex:rescue` plan-review found the
first draft architecturally wrong (see "Rejected approach" below). This is the
codex-reviewed design.

## Root cause (confirmed by reading, not assumed)

`TraceContext.trace_id` is *read from the active OpenTelemetry span* whenever one is
active (`read_or_mint_trace_id()` → `_active_trace_id()`, `telemetry/trace.py:42-75`).
This is deliberate — it's what keeps a `TraceContext` and the structlog records
emitted alongside it in agreement (module docstring, `telemetry/trace.py:11-15`;
ADR-0129 D1).

FRE-1069 added a **root span wrapping the entire batch tick**:
- `consolidator.py:192` — `open_root_span("consolidation", ...)` wraps all of
  `consolidate_recent_captures`.
- `scheduler.py:505` — `open_root_span("scheduler.session_summary", ...)` wraps the
  whole `run_session_summary_sweep` call.

Before FRE-1069, `SystemTraceContext.new(...)` called once per session/capture inside
these loops minted a genuinely fresh id each time (no span active → `uuid.uuid4()`
fallback). After FRE-1069, every such call *inside* the root span instead reads the
**same** span trace id — so every session processed in one tick collapses onto one
trace id. `LiteLLMClient`/`LocalLLMClient` write that id straight into
`budget_reservations.trace_id` (`litellm_client.py:566-570`, confirmed — it uses
`trace_ctx.trace_id`, not the active span, for the reservation row). Master's
measured evidence (trace `940312e4-…`, 3 sessions, 11 seconds) is exactly this: one
consolidation tick's root-span trace id, stamped onto 3 different sessions'
`entity_extraction` reservations.

This is orthogonal to the parent-level trace mint (`_new_consolidation_trace_id`,
`_new_scheduler_trace_id`) — those correctly mint **one** id per tick, which is the
ADR-0074 §I3 property being preserved. The defect is one level down: nothing mints
a **new** id per capture/session *inside* that tick.

## Rejected approach (first draft, killed by codex plan-review)

The first draft added `SystemTraceContext.new_child(...)` minting a raw
`uuid.uuid4().hex`, bypassing the active-span read entirely, plus a
`parent_trace_id` kwarg threaded through 3 call layers. `codex:rescue` found two
concrete defects:

1. **The AC-2 log line would have silently dropped the child id.**
   `_add_span_context` (`telemetry/logger.py`) unconditionally overwrites
   `event_dict["trace_id"]` from the *active span* on every log call — it does not
   defer to an explicit kwarg. `log.info(..., trace_id=child, parent_trace_id=parent)`
   under the tick's still-active span would have logged `trace_id=parent` (the
   kwarg silently clobbered), not `trace_id=child` — defeating the log-join AC-2
   depends on.
2. **A UUID with no corresponding OTel span breaks Tempo↔cost correlation.**
   `LiteLLMClient`'s own `model_call_span()` stays a child of whatever OTel span is
   *actually* current (the tick's root span) regardless of what `trace_id` value
   sits in the `TraceContext` passed alongside it — `start_as_current_span(...)` is
   called with no explicit context, so it inherits the ambient one. Meaning: the
   model-call span in Tempo would carry the *tick's* trace id, while the
   `budget_reservations` row for that exact call would carry the *fabricated*
   UUID — two different systems now disagree about which trace this call belongs
   to, which is the same class of defect this ticket exists to fix, just moved to
   a new pair of substrates (Tempo ↔ Postgres) instead of the old one (Postgres ↔
   session).

## Fix — nested real root spans, not fabricated ids

`open_root_span()` (`telemetry/spans.py:80-121`) already does exactly what's
needed: `context=Context()` forces a **genuine new root** (fresh trace id, no
parent) regardless of any span already current — its own docstring calls this out
explicitly ("without it, a span opened inside e.g. the scheduler's lifecycle loop
would silently parent onto whatever happened to be active"). Nesting it — opening
a *second* root span while the tick's root span is still current, doing one
session/capture's model call, then closing it — restores the tick's span as
current afterward via ordinary `contextvars.Token` attach/detach semantics, which
already support nesting correctly.

This means: **no new factory method, no `parent_trace_id` threaded through 3
call layers, no changes to `telemetry/trace.py` at all.** The existing
`SystemTraceContext.new("entity_extraction", session_id=session_id)` /
`SystemTraceContext.new("session_summary", session_id=session_id)` calls are
**unmodified** — they now correctly mint a fresh id because a fresh span is
active when they run. The nested span is real, so `model_call_span()`'s child
span, the `trace_ctx` handed to `respond()`, and the `budget_reservations` row it
writes all agree on one new trace id, itself genuinely present in Tempo — improving,
not just preserving, the Tempo↔cost correlation ADR-0129 established.

### 1. `second_brain/entity_extraction.py`

- `extract_entities_and_relationships(...)` gains `tracer: "Tracer | None" = None`
  (`TYPE_CHECKING`-only import, matching the existing convention in
  `consolidator.py`/`scheduler.py`).
- Wrap **only** the existing `if provider is not None: ... else: ...` dispatch
  block (currently ~lines 993-1101 — the cloud/local branch that builds
  `trace_ctx` and calls `respond()`) in an `open_root_span("entity_extraction",
  tracer=tracer)` / `close_root_span(...)` pair via `try/finally`. This is a
  self-contained re-indent: the block already sits inside the function's outer
  `try/except (BudgetDenied, Exception)`, and its own nested `except
  (LLMTimeout, InferenceSlotTimeout): return ...` early-return is unaffected —
  `finally` runs on every return path.
- Capture `parent_trace_id = read_or_mint_trace_id()` **before** opening the
  child span (reads the tick's still-active trace), and log
  `batch_child_trace_opened` with `parent_trace_id` + `session_id` right after
  opening — this one log line's own `trace_id` field auto-stamps to the *new*
  child span (AC-2: query logs by the child trace id, read `parent_trace_id` off
  this line to recover the owning tick).
- `SystemTraceContext.new("entity_extraction", session_id=session_id)` at the two
  existing call sites (cloud path, local path) is untouched — now correct by
  construction.

### 2. `second_brain/consolidator.py`

- `_process_capture`'s call to `extract_entities_and_relationships(...)` adds
  `tracer=self._tracer` — no new parameter needed on `_process_capture` itself
  (`self._tracer` is already an attribute, set in `__init__`, same tracer the
  tick's own root span uses).

### 3. `second_brain/session_summary.py`

- `_call_model(...)` gains `tracer: "Tracer | None" = None`. Wrap its whole body
  (both branches simply `return` — no nested early-return complications) in the
  same `open_root_span("session_summary", tracer=tracer)` / `close_root_span(...)`
  + `parent_trace_id` log pattern.
- `generate_session_digest(...)` gains `tracer: "Tracer | None" = None`, threaded
  into its one call to `_call_model(...)`.

### 4. `brainstem/scheduler.py`

- `_sweep_one_session`'s call to `generate_session_digest(...)` (line 819-821)
  adds `tracer=self._tracer`.

## Why this satisfies each AC

- **AC-1** — each capture/session gets a genuine fresh OTel trace id (via nested
  `open_root_span`) for its `SystemTraceContext`, which `LiteLLMClient`/
  `LocalLLMClient` write straight into `budget_reservations.trace_id`. N sessions
  in one tick → N distinct trace ids on their reservations.
- **AC-2** — `batch_child_trace_opened` logs `parent_trace_id` (the tick's own
  trace, captured via `read_or_mint_trace_id()` before the nested span opens)
  alongside the auto-stamped child `trace_id` — queryable, no schema change. The
  tick's own root span, its logs, and its `kind` are completely unaffected outside
  the nested block's lifetime.
- **AC-3** — enumeration table below; both producers found by inspection, not
  just consolidation.
- **AC-4** — the joinability probe discovers a session's `trace_ids` from
  `api_costs`/`budget_reservations` rows matching that `session_id`
  (`walk.py:172-178`). Once each session's reservation carries its own unique
  trace id, `_walk_budget_reservations`'s `WHERE trace_id = ANY(trace_ids)` query
  returns only that session's own rows. Live verification
  (`scripts/monitors/joinability_probe.py` against a session touched by a batch
  pass) happens post-deploy per the runbook; unit tests cover AC-1/AC-2/AC-3
  statically against real `InMemorySpanExporter` spans.
- **AC-5** — purely additive: new optional `tracer` kwargs (all default `None`,
  matching existing convention), a couple of re-indented existing blocks, no
  migration, no `UPDATE`/`DELETE` on `budget_reservations`. Historical rows
  untouched.

## AC-3 enumeration — every mint site, classified

| Mint site | Iterates real sessions with own cost reservations? | Verdict |
|---|---|---|
| `consolidator.py: _new_consolidation_trace_id` (parent, once per tick) | N/A — parent id, correct as-is | No change |
| `entity_extraction.py:1023,1076` — `SystemTraceContext.new("entity_extraction", session_id=...)`, called once per capture inside the tick's root span | **Yes** — one call per capture (captures may share a `session_id`; the fix mints per capture, which still guarantees no trace id is shared *across* sessions — AC-1's actual bar) | **Fix: nested root span** |
| `scheduler.py:507,516` — `_new_scheduler_trace_id("scheduler.session_summary")` (parent, once per tick) | N/A — parent id, correct as-is | No change |
| `session_summary.py:610,631` — `SystemTraceContext.new("session_summary", session_id=...)`, called once per session inside `_sweep_one_session`, itself inside the tick's root span | **Yes** — one call per session in the sweep | **Fix: nested root span** |
| `scheduler.py:258,293` — `_new_scheduler_trace_id("scheduler.lifecycle")` (start/stop, one-shot) | No — not a batch loop at all | Not in scope |
| `scheduler.py:1053` — `iteration_trace_id` for the lifecycle tick | No — disk check / ES backfill / joinability probe trigger; none book a per-session cost reservation | Not in scope |
| `scheduler.py:1101` — `eb_trace_id` for `backfill_missing_embeddings` | No — entity/Claim embedding backfill batches under `SYSTEM_SESSION_ID`, a fixed synthetic session, not real per-user sessions | Not in scope |
| `scheduler.py:1528` — `_run_quality_monitoring` | No — read-only graph/entity-quality checks, no LLM call, no cost reservation | Not in scope |
| `scheduler.py:1608` — `graph_quality_anomaly` | No — anomaly records are not session-scoped cost reservations | Not in scope |

Repo-wide search (per codex review) found no other production batch-loop-under-root-span
site — Captain's Log reflection and the grounding extractor's fallback trace mint are
single-call, not batch loops.

## Tests (TDD — write failing first)

Extend `tests/test_second_brain/test_consolidator_root_span.py` (already has the
exact harness — local `TracerProvider` + `InMemorySpanExporter`, tracer injected
via `SecondBrainConsolidator(tracer=tracer)`):
- Run `consolidate_recent_captures` over ≥3 captures spanning ≥2 distinct
  `session_id`s (mock `extract_entities_and_relationships` one level down — mock
  the LLM client's `respond()`/local client — to capture the `trace_ctx` each
  invocation receives, OR assert directly on `exporter.get_finished_spans()`:
  expect 1 tick root span + N per-capture child spans, all children with distinct
  `trace_id`s that also differ from the tick root's).
- Assert the tick's own span count/attributes (existing coverage) still holds —
  the nested spans must not appear as children of the tick span in the exported
  list (each is `span.parent is None`, matching how the tick's own root span is
  asserted today).

New/extended test in `tests/personal_agent/brainstem/test_session_summary_sweep.py`
or `tests/test_second_brain/test_session_summary.py`: same pattern — sweep ≥2
dirty-idle sessions (mocked) under a real root span, assert the per-session model
call each got a distinct trace id.

## Quality gates

`make test` · `make mypy` · `make ruff-check` + `make ruff-format` · `pre-commit run --all-files`.
Diff class: **escalate** — production cost/observability write path (budget_reservations,
consolidation, session digest — all in the live turn/batch path).
