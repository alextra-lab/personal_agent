# FRE-1219 — Retire live emissions of duration_ms / latency_ms / prompt_tokens / completion_tokens

Ticket: https://linear.app/frenchforest/issue/FRE-1219 (In Progress)
Backing: ADR-0133 (`telemetry/vocabulary.py`), ADR-0129 D3 (span-based duration)
Risk tier: Standard/Complex — touches `src/` logic across 12 files including cost-tracker-adjacent
code, plus 1 Grafana dashboard JSON file (owner-approved scope addition, scope narrowed by master to
exclude `expansion_decomposition.json` — see AC-3 item 2) → codex plan-review required + explicit
owner approval before coding (this document, both now satisfied — implementation starting).

## Acceptance criteria (verbatim from the ticket, written by master at the dispatch gate)

| # | Criterion |
|---|---|
| AC-1 | No live call site emits any of the four retired spellings — a structural search over `src/` for emit sites passing `duration_ms`, `latency_ms`, `prompt_tokens` or `completion_tokens` into a log or event payload returns zero. Uses the FRE-1177 vocabulary's own retired-spelling list as authority, not a hand-written one. |
| AC-2 | The two renames preserve their values, not just their absence — fails if the keys are merely deleted, which would satisfy AC-1 while silently losing token accounting on `api_cost_recorded` and `model_call_completed`. |
| AC-3 | The two removals lose nothing that had no replacement — for each site dropping `duration_ms`/`latency_ms`, state where the measurement now lives (the expected answer is the span, per ADR-0129); any site with no span coverage is reported, not silently dropped. |
| AC-4 | The validator agrees, on a corpus this ticket generates — run `validate_document()` over documents the test suite actually emits and assert zero violations for these four names; fails if only historical prod docs are checked. |

AC-2 is the discriminating one: deleting all four keys would satisfy a careless reading of AC-1 while
destroying token accounting on `api_cost_recorded` and `model_call_completed`.

## Master gate round 1 (PR #904) — two regression-guard defects, fixed

Behaviour was sound; the guards meant to keep it fixed were not. Both verified by master against
seeded fixtures (never inferred from the already-clean tree — "a vacuous rule and a clean tree are
indistinguishable"):

1. **The ast-grep rule was partially vacuous.** Every pattern's trailing `$$$` did not match zero args
   in argument position, so the retired kwarg was only caught when another argument followed it —
   missing the common real shape (kwarg last). Master seeded 6 cases; the rule caught 2 of 4 true
   positives. Fixed with a `kind: keyword_argument` rule scoped `inside` a `log.*`/`logger.*` call's
   `argument_list` (position-independent) — master's own verified pattern. Re-encoded master's 6-case
   set as `TestSeededViolationFiresAstGrep` in the test file, invoking the real rule via subprocess, so
   this can't silently regress to vacuous again.
2. **The vocabulary-sync check asserted a subset while claiming equality.** `FRE_1219_NAMES <=
   RETIRED_SPELLINGS.keys()` catches a name being removed from the vocabulary but not one being added —
   the exact direction the docstring promised. Literal equality is also wrong (4 names vs. 11). Fixed
   per master's description: scan `src/` for all 11 `RETIRED_SPELLINGS`, assert the "overflow" (an
   undocumented violation for a name outside `FRE_1219_NAMES`) is empty — `TestVocabularyIsTheAuthority`.

That scan surfaced two real findings outside this ticket's scope, documented (not silently fixed or
dropped) in the test file's `_KNOWN_OUT_OF_SCOPE_OVERFLOW_REASONS`: the known `event=event_type`/
structlog-reserved-parameter collision (5 sites, `es_logger.py` — the `elasticsearch_not_connected`
bug master is filing to Backlog), and a **newly-found** genuine `timestamp` kwarg on
`telemetry/metrics.py:217`'s rarely-exercised `invalid_timestamp` warning — a real, currently-live
violation for a name outside this ticket's 4-name scope, flagged to master rather than fixed here.

## Codex review disposition

Reviewed by `codex-rescue` (session `019ff508-b46d-7e30-ac0c-5c92d5ffcd4e`) against this plan before any
code was written. Verdict: needs revision — 6 concrete findings, all addressed in this version:

1. Site inventory itself was confirmed complete (codex independently re-ran the ast-grep sweep) —
   but the regression-*rule* was narrower than the emission path (dict-literal payloads feeding
   `**`-unpacked log calls could regress undetected) → **fixed**: added a second, narrower ast-grep
   rule/AST check plus a seeded-fixture test for the dict-unpack case (AC-1 section).
2. The claimed "exhaustive `**`-unpack sweep" had a real gap: `orchestrator/sub_agent.py:194` was missed
   by a too-narrow grep context window. Turned out benign (clean payload), but the exhaustiveness claim
   was false as stated → **fixed**: re-verified without a line-window limit, corrected the claim (Scope
   boundary section).
3. Vocabulary-sync test used a subset check, which can't detect a new relevant name being added to the
   authority → **fixed**: equality check instead (AC-1 section).
4. `log_batch()` is a second `validate_document()`-bypassing document-construction path, not classified
   in the original plan → **fixed**: named explicitly, confirmed inert today (Scope boundary + Out of
   scope sections).
5. AC-4's original test design (`capture_logs()` → `validate_document()` directly) would test the wrong
   document — the raw structlog dict still carries the retired `"event"` key that the real assembly path
   strips/renames before validation → **fixed**: redesigned to exercise the real `log_event()` assembly
   boundary (AC-4 section).
6. A second live Grafana dashboard (`expansion_decomposition.json`) was missed, and the original "report
   it in the PR" disposition for both dashboards was judged too weak for repository-owned active
   dashboards → **fixed**: both panels found; owner decided (2026-08-12) to fold cleanup into this PR.
   Same day, master corrected scope: `expansion_decomposition.json` is being concurrently rewritten by
   `build2` on FRE-1211, which already removed the dead panel — so this PR's fold-in applies to
   `turn_session_artifact.json` only, and the PR body credits FRE-1211 for the other (see AC-3 item 2).

No issues found in: the four excluded scope categories (captures family, provider-response mirrors,
joinability probe, `METRIC_FIELD_MAP`), the `memory/reranker.py` FRE-851 cascade (full removal +
test deletion confirmed correct, not a shortcut), or AC-2's test design.

## Scope boundary (why this list is exact, not "everything matching the string")

**Revised after codex plan-review (see "Codex review disposition" below).** `validate_document()`
(`telemetry/vocabulary.py`) is invoked from `ElasticsearchLogger.log_event()`, and there are two
document-construction paths that bypass it entirely: `index_latency_breakdown` (manual ES docs, fixed
in this ticket — see site #19) and `log_batch()` (`es_logger.py:273`, generic `list[tuple[event_type,
data, trace_id]]` passthrough to `async_bulk`, no `validate_document()` call). `log_batch()` has
**zero callers in `src/`** — its only caller anywhere is `tests/test_telemetry/test_es_logger_redaction.py:73`,
which passes `{"command": ...}`, no retired name. Nothing to fix here today (it emits nothing, because
nothing calls it), but it's named explicitly rather than silently omitted: if `log_batch()` ever grows
a live caller, whatever `data` dict that caller builds needs the same governance `log_event()` gets for
free, and today it wouldn't get it.

So "emit site" means: a keyword argument literally named `duration_ms`, `latency_ms`, `prompt_tokens`,
or `completion_tokens`, passed either (a) directly into a `log.<method>(...)` / `logger.<method>(...)`
call, (b) into such a call via `**dict` unpacking where the unpacked dict itself carries that literal
key — including when that dict is built by a separate small helper function and unpacked at the call
site (e.g. `llm_client/telemetry.py`'s `emit_model_call_started`/`emit_model_call_completed`, which
build a `payload` dict across several lines and then call `log.info(EVENT, **payload)`), or (c) as a
string key in one of `index_latency_breakdown`'s two hand-built ES document dicts.

This was verified exhaustively for *current* sites, with one confirmed methodology gap flagged by
codex review (not a missed site, but a real hole in the sweep — see disposition below):
- ast-grep structural sweep for `log.$METHOD($$$, name=$VAL, $$$)` / `logger.$METHOD(...)` across all
  of `src/`, for all four names — found every direct-kwarg site (script in the "Verification commands"
  section below reproduces it). Codex independently re-ran this and confirmed no additional direct-kwarg
  site exists.
- Grep swept every `**`-unpacked `log.*`/`logger.*` call. First pass used a `grep -A3` window (3 lines
  of context after the call), which found 6 sites and missed a 7th: `orchestrator/sub_agent.py:194`
  (`logger.info("sub_agent_start", ..., **_context_breakdown)`) — the call has more kwargs before the
  `**` unpack than a 3-line window covers. Codex caught this. Re-checked directly: `_context_breakdown`
  is built by `_summarize_input_context()` (fixed, known key set, visible at `sub_agent.py:96`) — clean,
  not a missing removal site — but the sweep's claim of exhaustiveness was wrong as stated; it's exhaustive
  now, verified without a line-window limit (`grep -A20`, then confirmed by reading every unpacked dict's
  full construction), covering 7 `**`-unpack sites total. None of the 7 carry an exact retired name.
- Grep swept every quoted dict-literal key matching the four names across `src/` and classified each:
  captures-family (`ToolResult.latency_ms`, `SubAgentCapture.duration_ms`, Neo4j `TurnNode.properties`,
  MCP `ToolResult`-shaped dicts, orchestrator `ctx.tool_results`/`ctx.steps` internal state) — all
  explicitly out of ADR-0133 D1's scope, since `validate_document` never sees them (confirmed for the
  captures path specifically: it persists via `captains_log/capture.py` → `captains_log/es_indexer.py`,
  never through `log_event`); provider-response mirrors (`llm_client/adapters.py`, `litellm_client.py`
  `usage` dicts) — nested under an outer `usage` key, and even at the top level they mirror the
  *provider's* wire format, not our log vocabulary; `observability/joinability/` — a self-contained probe
  subsystem writing directly via `es.index()` in `observability/joinability/sink.py` to its own monitor
  indices, never routed through `log_event`; `telemetry/queries.py`'s `METRIC_FIELD_MAP` — consumed only
  while building read-side percentile queries (`queries.py:206`), not an emit site.

Distinct-but-similar names are deliberately untouched (Rule 1 in `vocabulary.py` is exact-match only,
and `GOVERNED_NAMES` for Rule 2's near-miss check excludes `duration_ms`/`latency_ms` since their
canonical value is `None` — retired outright, not renamed, so they're never in the near-miss
comparison set either): `sub_agent_duration_ms`, `total_duration_ms`, `probe_duration_ms`,
`summariser_duration_ms`, `avg_duration_ms`, `recall_latency_ms`, `call_latency_ms`,
`_routing_latency_ms` (this one IS a local variable name whose *value* feeds an in-scope kwarg —
see executor.py below), `max_latency_ms`, `step_planning_duration_ms` (same — local var name, in-scope
kwarg). The governing fact is always the **keyword argument name at the log call**, never the local
variable name holding the value.

`request_trace_step` (36 of the ticket's measured docs) has zero live emit site — confirmed via grep:
`RequestTimer` and `index_request_trace_from_snapshot` were already fully retired under ADR-0129 D3 /
FRE-1067, before this ticket. Only a stale `config/kibana/setup_dashboards.py` panel definition still
references it (a dashboard config, not an emit site — out of scope, noted under AC-3 below).

## Site inventory

### Rename (AC-2): `prompt_tokens`→`input_tokens`, `completion_tokens`→`output_tokens`

**`src/personal_agent/llm_client/cost_estimator.py:252-259`** — `log.info("actual_cost_fallback_priced",
..., prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, ...)`. This is the *only* site
in `src/` where these two names appear as log-call kwargs. Edit: rename the two kwargs to
`input_tokens=prompt_tokens, output_tokens=completion_tokens` — keep the local variable names as-is
(they still hold the right values; only the wire field name changes).

### Remove (AC-3): `duration_ms` / `latency_ms`

For each site: the kwarg is deleted; the "span coverage" column is the AC-3 answer.

| # | File:line(s) | Local-var cascade | Span coverage (AC-3) |
|---|---|---|---|
| 1 | `captains_log/feedback.py:451` | none (literal `duration_ms=0`) | No span — this is a Linear-poller feedback-processing loop, not a model/tool call; not covered by ADR-0129's span tree |
| 2 | `captains_log/feedback.py:457,471,478` | delete `t0` (457) and `duration_ms` local (471) — both orphaned | Same as above — no span |
| 3 | `llm_client/client.py:371,665,674` | delete `start_time` (371) and `duration_ms` local (665) — both orphaned; fix the stale comment at 369-370 which narrates the now-deleted "start_time/duration_ms stopwatch boundary" | **Covered** — `model_call_span` wraps this exact retry loop (ADR-0129 D3); this is the error path of the same call the success path already reports via span duration |
| 4 | `llm_client/cost_tracker.py:252` | kwarg only — `latency_ms` local (159) stays, feeds the Postgres `api_costs.latency_ms` INSERT (line ~220-235), which is out of scope (a durable ledger column, not a log emission) | Partially covered — the model-call span covers call latency; this specific event also carries `cost_usd`/`record_id` which have no span equivalent, so the *event* still exists, just without duration |
| 5 | `memory/embeddings.py:405` | kwarg only — `latency_ms` local (371) stays, feeds `record_vendor_cost(..., latency_ms=latency_ms)` (417), a Postgres-ledger call parallel to #4, confirmed via ast-grep to have no log emission of its own | No span — embedding calls are not currently wrapped by a span in `spans.py`'s tree (model-call spans cover LLM inference only) — **reported per AC-3, no span exists** |
| 6 | `memory/reranker.py` — see below | see below | No span — reranker calls are not currently wrapped; **reported per AC-3** |
| 7 | `memory/service.py:4848,4898,4900-4911` | delete `started` (4848) and `latency_ms` local (4898) — both orphaned | No span — multipath fan-out (dense/lexical/multi-query arms + fusion + rerank) has no span wrapper; **reported per AC-3** |
| 8 | `second_brain/attempts.py:118` | inline expression; confirm `started_at`/`completed_at` still used elsewhere in the function (they back a capture record) before deleting the kwarg only | No span — consolidation-attempt bookkeeping, not a model/tool call |
| 9 | `second_brain/session_summary.py:730,869` | delete `started_at` (730) — orphaned | No span — session-digest generation LLM call itself is span-covered elsewhere, but this specific `duration_ms` measured the whole retry+validate+trim loop around it, which has no span; **reported per AC-3** |
| 10 | `orchestrator/context_compressor.py:234,268,275,290,297` | delete `start_ms` (234) and both `duration_ms` locals (268, 290) — all orphaned once both consumers (275, 297) are removed | No span — the compressor LLM call is a `client.respond()` call that itself opens a model-call span internally, but this field measured the compressor function's *own* wrapper time (formatting + call + parse); **reported per AC-3** |
| 11 | `orchestrator/executor.py:4294` | delete `_routing_latency_ms`/`_routing_start` (~4274/4282) — orphaned | No span — skill-routing classification call; **reported per AC-3** |
| 12 | `orchestrator/executor.py:4823,4886` | inline; `step_start_time` stays (used elsewhere in the deadline-check branches) | **Covered** — deadline-exceeded branches of the step-planning call, same span as #14 |
| 13 | `orchestrator/executor.py:4900,4915` | kwarg only — `duration_ms` local (4900) **stays**, feeds the out-of-scope captures-family `OrchestratorStep` metadata dict at 4926 | **Covered** — step-planning model call |
| 14 | `orchestrator/executor.py:5059-5067` | delete `step_planning_duration_ms` (5058) — orphaned | **Covered** |
| 15 | `orchestrator/executor.py:5084-5090,5166` | delete the except-block `duration_ms` (5084) — orphaned; confirm whether 5090 and 5166 share this one local or are separate before editing (re-verify at implementation time) | **Covered** — model-call-error path |
| 16 | `tools/artifact_tools.py:1619,1638,1664` | kwargs only — `start_ms`/`sub_agent_duration_ms` **stay** (feed the result dict at 1714 and the distinctly-named `sub_agent_completed`'s `sub_agent_duration_ms=` kwarg at 1726, out of scope) | No span — artifact-builder sub-agent dispatch is not span-wrapped; **reported per AC-3** |
| 17 | `orchestrator/expansion_controller.py:301-303,319-322,479-482` | `duration_ms` locals here feed `PhaseResult` dataclass construction too (out of scope) *and* these three log kwargs — only the kwargs are removed, the locals stay (still feed `PhaseResult`) | No span — planner-call and sub-agent-dispatch phase timing; **reported per AC-3** |
| 18 | `orchestrator/sub_agent.py:313` | kwarg only — `result.duration_ms` stays (it's a `SubAgentResult`/`SubAgentCapture` field, captures-family, out of scope) | No span — sub-agent inference call; **reported per AC-3** |
| 19 | `telemetry/es_logger.py::index_latency_breakdown` (lines 418,422,449,458) | delete `dur = row.get("duration_ms")` and the `"duration_ms": ...` dict entries at both the `phases_payload` construction and the `flat_doc` construction | N/A — this method is dead code (zero callers in `src/`; still exercised by `tests/test_telemetry/test_es_logger_redaction.py::test_latency_breakdown_redacts_summary_and_phase_docs`, whose assertions don't touch `duration_ms` so it keeps passing). Fixed anyway because it still constructs literal `"duration_ms"` ES-document keys that a structural search over `src/` rightly catches — same treatment as every live site, not a special case. `total_duration_ms` (412,434) is a distinct name and stays untouched. |

**#6, `memory/reranker.py` — the FRE-851 cascade (flag prominently, see "Tradeoffs" below):**

`rerank()` (line 344 `start = time.monotonic()`) and `_rerank_fallback()` (its `overall_start` param,
existing specifically to fix FRE-851 — a real historical bug where the fallback path's clock silently
restarted instead of measuring from the original attempt) have **no other purpose** than feeding
`duration_ms`. Once the three `log.*` kwargs are removed (`reranker_failed` @369, `reranker_fallback_failed`
@467, and `_log_reranker_applied`'s internal `reranker_applied` @526), every consumer of `start`/
`overall_start`/`duration_ms` in this file is gone. Full removal required, not a partial kwarg strip:

- `rerank()`: delete `start = time.monotonic()` (344), delete `duration_ms = ...` (360, 386), delete
  `duration_ms=round(duration_ms, 1),` (369), delete `overall_start=start,` from the `_rerank_fallback`
  call (377), delete `duration_ms=duration_ms,` from the `_log_reranker_applied` call (397).
- `_rerank_fallback()`: delete the `overall_start: float,` parameter (410) and the docstring paragraph
  explaining it (420-424, since it now describes a removed parameter), delete `duration_ms = ...`
  (458, 473), delete `duration_ms=round(duration_ms, 1),` (467), delete `duration_ms=duration_ms,` from
  the `_log_reranker_applied` call (484).
- `_log_reranker_applied()`: delete the `duration_ms: float,` parameter (503) and
  `duration_ms=round(duration_ms, 1),` from its body (526).

`memory/reranker.py:225,265` (`call_latency_ms`/`latency_ms=call_latency_ms`) is a **separate,
unrelated** per-call cost-latency timer inside `_attempt_rerank` (confirmed via ast-grep: not a
`log.*`/`logger.*` kwarg site) — untouched.

## AC-3 items that must be named explicitly in the PR/handoff (not just in this plan)

1. **`memory/reranker.py` loses the FRE-851 regression test.**
   `tests/personal_agent/memory/test_reranker.py::TestFallbackClock::test_fallback_success_does_not_restart_the_clock`
   asserts `mock_monotonic.call_count == 6` as an indirect proxy for "the fallback measured from the
   original attempt, not a fresh clock" — 3 calls from the now-removed `duration_ms` mechanism + 3 from
   the unrelated `call_latency_ms` timer. After this fix there is no `duration_ms` mechanism left to
   count, so the assertion becomes `== 3` and no longer protects anything — the specific bug FRE-851
   fixed (fallback clock silently restarting, hiding a failed primary's latency from telemetery) has no
   remaining regression coverage. Delete this test; do not weaken it to `== 3`, since a passing-but-
   meaningless assertion is worse than an honestly-removed one. If this guarantee needs to keep being
   regression-tested, it would need new instrumentation (e.g., an OTel span timestamp check) — that is
   new-instrumentation work, out of this ticket's scope, and is a candidate follow-up ticket.

2. **Two live Grafana dashboards go dark on duration, not one — and this PR fixes both dashboards'
   dead panels/descriptions rather than only reporting them (revised after codex review).**
   - `config/grafana/dashboards/turn_session_artifact.json`'s "Session activity" panel
     (`turn_session_artifact.json:34,44`): ES query `session_id: *` — unfiltered by `event_type` —
     averaging `latency_ms` across the whole `agent-logs-*` index. Multiple sites in this fix feed that
     average, including `api_cost_recorded` (site #4 — the ticket's own single largest contributor at
     146/24h of the measured 309 `latency_ms` docs) and `multipath_recall` (site #7).
   - `config/grafana/dashboards/expansion_decomposition.json`'s "Sub-agent duration by outcome" panel
     (`expansion_decomposition.json:197,225`) — found by codex, missed in the first pass of this plan —
     reads `duration_ms` from `sub_agent_complete` (site #18, `orchestrator/sub_agent.py:309`). A
     neighboring success-rate panel's description (`expansion_decomposition.json:154`) also cites
     duration conclusions drawn from this now-dead field.
   - **Out of scope for this PR (master correction 2026-08-12):** `build2` is rewriting this entire
     dashboard file on `fre-1211-rebuild-eight-dashboards-postgres` (FRE-1211, ES→Postgres migration)
     concurrently, and has already removed this exact panel — repurposed to "Sub-agent cost by outcome"
     precisely because `sub_agents` carries no duration field. Do **not** touch
     `expansion_decomposition.json` in this PR: editing it buys nothing (FRE-1211 already fixes it) and
     guarantees a conflict on a 600-line JSON file being actively rewritten elsewhere. The PR body notes
     this panel is handled by FRE-1211.
   - Repointing the remaining panel at OTel span duration (ADR-0129 D3) is real instrumentation work —
     the `turn_session_artifact.json` panel specifically can't just swap fields, because the root span
     deliberately carries no `session_id` (`telemetry/otel_middleware.py:3`) — genuinely out of this
     ticket's scope, and belongs in a follow-up ticket if session-scoped duration views are still wanted.
   - **Decided (owner sign-off 2026-08-12, scope narrowed by master same day):** fold cleanup into this
     PR for `turn_session_artifact.json` only. Remove or relabel its now-dead "Session activity" latency
     average — not a full redesign, just stopping this one live dashboard from silently showing an
     empty/frozen metric with no visible indication anything changed. File a follow-up ticket for real
     span-based replacement instrumentation if the duration view is still wanted. This extends the PR's
     file list beyond `src/`+`tests/` to include this one dashboard JSON file.

3. **No automated consumer, despite one test's docstring claim.** `test_multipath_core.py`'s
   `TestLatencyTelemetry::test_latency_ms_emitted_as_float` docstring says multipath's `latency_ms`
   "is the durable signal ... the standing auto-rollback guard watches" (citing FRE-724 AC-6c). Checked:
   no such automated guard exists anywhere in `src/` (grepped for `rollback`/`auto-rollback`/`p50`/`p95`
   tied to multipath — the only p50/p95 code found is an unrelated skill-index-size monitor). FRE-724's
   own AC-6 text describes a one-time manual gate exercised during rollout ("if the measured median
   exceeds the ceiling, hold the flag off"), not a standing automated mechanism. This test's docstring
   overstates the guarantee; update it to drop the "auto-rollback guard" claim when updating the
   assertion (see AC-1 test-update list below) rather than treating it as a blocker.

4. **`config/kibana/setup_dashboards.py`** still defines panels querying `event_type:request_trace_step`
   with `duration_ms` (lines ~540-611) — dead already (no live emit site, see Scope boundary above).
   Noted, not fixed — it's a dashboard config, not an emit site, and Kibana dashboards are being retired
   in favor of Grafana per FRE-1208 (recently merged) so this is likely moot already.

## AC-1 implementation — structural-search regression test

**Implementation note (post-review-round-2):** the ast-grep YAML rule below is included per
project convention (CLAUDE.md §3b) and rides the existing `check_egress_bypass_rules.py`
pre-commit/CI wiring, but its exact pattern-matching behavior could not be locally verified this
session (ast-grep CLI invocations were intermittently unavailable in this environment). The
**authoritative** AC-1 proof actually implemented and verified passing is a plain `ast`-module scan
(`tests/test_security/test_no_retired_telemetry_spellings.py`), which does not depend on ast-grep's
pattern semantics and covers both the direct-kwarg and dict-unpack shapes in one mechanism. Noted in
the PR as a known limitation, not silently glossed over.

**Revised after codex review**: the direct-kwarg ast-grep rule alone is too narrow as a *regression*
guard — it would catch nothing if someone later added a retired name to a dict that's built separately
and `**`-unpacked into a log call (the exact shape `llm_client/telemetry.py`'s `emit_model_call_started`/
`emit_model_call_completed` already use for their clean payloads — codex's point is that the rule
wouldn't notice if that payload dict regressed). Three mechanisms now, not two:

1. **ast-grep rule** (`.ast-grep/rules/no-retired-telemetry-spellings.yml`), auto-enforced via the
   existing `scripts/check_egress_bypass_rules.py` pre-commit/CI wiring (it scans the whole
   `.ast-grep/rules/` directory — confirmed by reading `sgconfig.yml` and the script). Pattern: a
   keyword argument named one of the four retired names, inside a call whose callee is `log.<method>`
   or `logger.<method>` for `method` in `{info, warning, error, debug, exception}`. Covers every direct-
   kwarg site (the majority) and is cheap to run in CI on every commit.
2. **A second, narrower ast-grep rule (or a plain-Python AST check in the same test module) for dict
   literals** whose string keys include one of the four retired names, specifically inside functions
   whose name matches `emit_*`, `_log_*`, or whose body contains a `log.*`/`logger.*` call using `**`
   on that same dict variable — scoped tightly enough not to false-positive on the already-classified
   out-of-scope dict producers (captures-family, provider-response mirrors, tool-result dicts). This is
   deliberately narrower than "any dict with this key anywhere in `src/`" (which would false-positive on
   `telemetry/vocabulary.py`'s own `RETIRED_SPELLINGS` definition and on the captures-family dicts) —
   it targets payload-builder functions feeding a `**`-unpacked log call specifically.
3. **Pytest regression test** (`tests/test_security/test_no_retired_telemetry_spellings.py`, mirroring
   `tests/test_security/test_bypass_rules.py`'s two-pronged shape):
   - `test_real_tree_is_clean` — runs both ast-grep rules against `src/` and asserts zero matches from
     each.
   - `test_derived_names_match_vocabulary_exactly` — asserts the YAML rules' name list is **exactly
     equal** to `{"duration_ms", "latency_ms", "prompt_tokens", "completion_tokens"}` derived from
     `vocabulary.RETIRED_SPELLINGS` (not a subset check, which codex correctly flagged as unable to
     detect a *new* relevant spelling being added to the authority — equality catches both directions:
     the rule silently going stale after a vocabulary edit, and the vocabulary drifting ahead of the
     rule).
   - `test_seeded_violation_fires` — writes two temp `.py` fixtures: one with `log.info("x",
     duration_ms=1)` (direct kwarg), one with `payload = {"latency_ms": 1}; log.info("x", **payload)`
     (dict-literal-into-unpack) — asserts both rules catch their respective case, proving neither is
     vacuously passing.
   - A dedicated test for `index_latency_breakdown` (the one non-ast-grep-reachable case, since it's a
     dict literal built at runtime from caller input, not a static kwarg or literal): call it with a
     `duration_ms`-bearing `breakdown` input and assert neither the summary doc nor the flat phase docs
     contain a `duration_ms` key in what gets passed to `_index_agent_log` (extend
     `tests/test_telemetry/test_es_logger_redaction.py` or add alongside it).

## AC-2 implementation — value-preservation test

New test in `tests/personal_agent/llm_client/test_cost_estimator.py` (or wherever this module's tests
live — confirm exact path at implementation time): call the fallback-pricing function with known
`prompt_tokens`/`completion_tokens` values on the mocked `usage` object, capture the `log.info` call via
`structlog.testing.capture_logs()` or a mock, and assert the emitted event's `input_tokens` /
`output_tokens` equal exactly the values that were on `usage.prompt_tokens` / `usage.completion_tokens`
— not just that the keys are present. This is the literal AC-2 text: "fails if the keys are merely
deleted."

## AC-4 implementation — validator-agreement test

**Original design was wrong — fixed after codex review.** `structlog.testing.capture_logs()` captures
the *raw* structlog event dict, which still carries the `event` key (the event name, under the literal
key `"event"`) — but `"event"` is itself one of the 11 retired spellings in `RETIRED_SPELLINGS`
(→ `event_type`). The real assembly path extracts/renames it before `validate_document()` ever sees the
document: `telemetry/es_handler.py`'s record-flattening step (`~line 476-497`) pulls `event_type` out
and drops the reserved keys, and separately `ElasticsearchLogger.log_event()` (`es_logger.py:~240`)
builds the final `doc` as `{"@timestamp": ..., "event_type": event_type, "trace_id": ..., "span_id":
..., **data}` before calling `validate_document(doc)`. Passing a raw `capture_logs()` dict straight into
`validate_document()` would fail on the `"event"` key — a real violation, but the *wrong* one; it proves
nothing about the four names this ticket targets and would give a false sense of AC-4 coverage.

Fixed design: exercise the real assembly boundary, matching the existing pattern in
`tests/test_telemetry/test_es_handler.py` (e.g. its violation-raises test at `test_es_handler.py:768`,
which calls `handler.es_logger.log_event("task_started", {"duration_ms": 12})` directly and asserts
`VocabularyViolationError`). For each fixed site (the rename + at least 2 removal sites), do the
inverse: call the real production emit path so it reaches `log_event()` with real production-shaped
data (either via `handler.emit(record)` + `handler.drain()` per that file's `_connected_handler()`
helper, or by calling `es_logger.log_event(event_type, data)` directly with the exact `data` dict the
production emit function now sends — reconstructed by capturing what the emit function actually passes,
not hand-authored), and assert **no** `VocabularyViolationError` is raised. This exercises
`validate_document()` at its real call site, on documents this ticket's test suite generates, per AC-4's
literal text — and is strictly more faithful than calling `validate_document()` a second time by hand,
since it also catches a document-shape bug the direct-call approach would miss entirely.

## Existing tests requiring updates (beyond the two above)

- `tests/personal_agent/memory/test_reranker.py::TestFallbackClock::test_fallback_success_does_not_restart_the_clock`
  — delete (see AC-3 item 1).
- `tests/personal_agent/memory/test_multipath_core.py::TestLatencyTelemetry::test_latency_ms_emitted_as_float`
  — replace with an assertion that `multipath_recall` does *not* carry `latency_ms`; also strip the
  docstring's unverified "auto-rollback guard" claim (AC-3 item 3).
- Any test asserting on the exact kwargs of the log calls being edited in the site inventory above needs
  a sweep for `latency_ms`/`duration_ms`/`prompt_tokens`/`completion_tokens` assertions tied to *these
  specific events* (not the already-checked `test_cost_tracker_vendor.py` / `test_chat_api_records_cost.py`,
  which assert on the Postgres-bound `record_api_call` kwargs, a different call — confirmed no change
  needed there). Do this sweep per-file during implementation, immediately after editing each site, by
  running that file's test module before moving to the next site.

## Verification commands (for implementation-time re-derivation, not to be hand-copied)

```bash
# Direct-kwarg sweep (source of the site inventory above)
for recv in log logger; do
  for name in duration_ms latency_ms prompt_tokens completion_tokens; do
    ast-grep run -p "$recv.\$METHOD(\$\$\$, $name=\$VAL, \$\$\$)" -l py src/
  done
done

# Post-fix: this must return nothing
```

## Out of scope (confirmed, do not touch)

- Captures family: `TaskCapture`, `SubAgentCapture`, `CaptureRecord.duration_ms`, `ToolResult.latency_ms`,
  Neo4j `TurnNode.properties["duration_ms"]` (`second_brain/consolidator.py:658,800`), orchestrator
  `ctx.tool_results`/`ctx.steps` internal dicts (`orchestrator/tool_dispatch.py`, `executor.py:875,5519,5534`,
  `mcp/types.py::mcp_result_to_tool_result`, `tools/location.py::_executor_output`).
- Provider-response-shape mirrors: `llm_client/adapters.py`, `llm_client/litellm_client.py:644-645`
  (`usage` dicts nested under an outer key, mirroring OpenAI/Anthropic wire format).
- `observability/joinability/` — self-contained probe subsystem, own `ResultDoc`/`SubstrateCheck` schema,
  never routed through `log_event`.
- `telemetry/queries.py::METRIC_FIELD_MAP` — read-side ES query field map, not an emit site.
- `telemetry/es_logger.py::log_batch()` — bypasses `validate_document()` (generic passthrough to
  `async_bulk`), but has zero callers in `src/` and its one caller anywhere (a redaction test) emits no
  retired name. Nothing to fix — noted explicitly per codex review rather than left unclassified, since
  a future live caller would need its own governance.
- `llm_client/telemetry.py::emit_model_call_completed` — already compliant (verified: carries no
  `latency_ms`/`duration_ms`, already uses `input_tokens`/`output_tokens`; its own docstring cites
  ADR-0129 D3/FRE-1067 AC-9). None of its 3 call sites (`client.py`, `litellm_client.py`, `chat_api.py`)
  inject `latency_ms`/`duration_ms` via `extra=`. The ticket's measured "model_call_completed 27" docs
  are historical, predating this compliance.
- `config/kibana/setup_dashboards.py` — stale dashboard config, not an emit site (AC-3 item 4).

## Quality gates (Step 8, after implementation)

`make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files` ·
self-review via `feature-dev:code-reviewer`. This touches cost-tracker-adjacent code
(`cost_tracker.py`, `cost_estimator.py`) — not the cost-governance/budget-enforcement logic itself, but
per the skill's diff-class routing, treat as self-serve-with-care rather than auto-escalate, and call
out explicitly in the PR that no Postgres-bound cost/budget value changes (only ES log field names).
