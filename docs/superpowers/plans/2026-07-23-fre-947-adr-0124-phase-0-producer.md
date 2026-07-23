# FRE-947 — ADR-0124 Phase 0: session-summary producer correction

**Backing:** [ADR-0124](../../architecture_decisions/ADR-0124-session-summary-producer-and-phased-consumption.md)
(D1 trigger · D2 input · D3 output) · Ticket FRE-947 · Delivers AC-1 … AC-14.

**Deploy class:** gateway rebuild — ask-first.

---

## 0. Findings that change the plan (surfaced before coding)

**F1 — `TaskCapture` records neither tool arguments nor non-dispatched invocations.** AC-8 requires
the assembled prompt to contain, for **every tool invocation**, "its name, **arguments**, status and
error". Two separate gaps:

- *Arguments.* `ctx.tool_results` entries are `{tool_name, success, output, error, latency_ms}`
  (`orchestrator/executor.py:4709-4717`); arguments survive only in the intra-turn `digest_sidecar`
  (`executor.py:4742-4746`) and are discarded before the capture is written.
- *Non-dispatched invocations.* Three paths append **only** to the local transcript list and
  `continue`, never touching `ctx.tool_results`: malformed-JSON arguments (`executor.py:4560`),
  loop-gate block (`executor.py:4595`), and an escaped dispatch exception (`executor.py:4656`). A
  blocked or malformed invocation is therefore invisible to the capture entirely.

The ADR's Context states `TaskCapture` carries tool data "in full". For arguments and for
non-dispatched calls that is not true, and a one-line fold-in does **not** deliver AC-8
(caught by codex plan-review).

→ **Fold-in:** add `"arguments"` to the `ctx.tool_results` entry, **and** append an entry from each
of the three non-dispatch paths with `success: False` and an explicit `error`. The one behavioural
consumer of `ctx.tool_results` is `_fallback_reply_from_tool_results` (`executor.py:1483-1499`,
the degraded-path reply); the new entries render as `- <tool>: failed (<reason>)`, which is correct
under its existing contract — pinned by a test so the change is deliberate, not incidental.
Historical captures carry neither; the assembler emits an explicit *"tool arguments were not
recorded for this session"* statement, which is a missing-evidence declaration under D2's contract
rather than a silent omission.

**F1b — the fold-in needs an ES mapping change (codex missed this).** `TaskCapture.tool_results` is
indexed to `agent-captains-captures-*`, whose template maps `tool_results` as `nested` with five
explicit properties and **`dynamic: true`** at the mapping root
(`docker/elasticsearch/captains-captures-index-template.json:12,112-120`). An `arguments` dict with
arbitrary keys would be dynamically mapped per key and walk into the 300-field cap.
→ map `arguments` explicitly as `{"type": "text", "index": false}` and JSON-serialise it in
`normalize_capture_doc_for_es` (`captains_log/es_indexer.py:34-46`), exactly as `output` is handled.
Required for the Telemetry-surface-reconciliation CI check as well as for correctness.

**F2 — `session_summary` has no live consumer, confirmed.** `request_gateway/context.py:133`
reads `session.get("session_summary")`, but the broad-recall Cypher (`memory/service.py:4335-4344`)
projects only `session_id`, `dominant_entities`, `turn_count`, `started_at`. The key is always
`None`. The ADR's claim holds. Out of scope to change; recorded so nobody reads that line as a
consumer.

**F3 — ADR-0124's header still says `Status: Proposed`** while FRE-947 states the owner accepted
it on 2026-07-23. Doc drift for master to reconcile at the gate; not touched by this PR.

**F4 — AC-9/AC-10/AC-12/AC-13 are model-quality criteria, not unit tests.** They require running
the real producer against hand-labelled fixtures on `claude_sonnet`. That is live API spend
(≈70–90 calls, ≈2–6k prompt tokens each → order of $1–3). It is **not** a live gateway turn and
writes nothing to the KG, but it is spend and needs an explicit OK before the arm runs.

---

## 1. Design decisions taken here (not deferred)

| # | Decision | Reason |
|---|---|---|
| D-a | `session_digest` stored as an orjson **string property** on `:Session`; `SessionNode.session_digest` is a typed `SessionDigest` model, serialised at the service boundary. | Neo4j node properties cannot hold nested maps. Precedent: `Turn.properties` / `Entity.properties` (`service.py:1031`, `:1371`). |
| D-b | `summary_generated_at` means **projection freshness**, not "a digest exists". It advances on any *completed* projection — including a deliberate single-turn floor skip — and never on failure. **The floor-skip advance goes through the same `expected_ended_at`-predicated write as a real digest**, so a turn landing mid-skip refuses it. | Reconciles AC-2 (no session left dirty) with AC-7 (single-turn sessions carry no digest). Without this a single-turn session is permanently dirty and AC-2 can never pass. The atomicity clause closes the race codex flagged: an unconditional skip-write could mark a now-multi-turn session clean. |
| D-c | Failure reasons split **terminal-eligible** (`oversized_input`, `schema_invalid`, `span_validation_failed`, `digest_over_budget`) vs **always-retryable** (`budget_denied`, `model_error`, `timeout`, `empty_output`). Terminal = terminal-eligible reason **and** `summary_attempt_count >= session_summary_max_attempts`. The failure write is **also** predicated on `expected_ended_at`. | The AC preamble: "a budget denial is never terminal, since it is transient by nature". The predicate stops a failure record from clobbering a concurrent success. |
| D-d | The legacy `session_summary` property is **never written again and never nulled**. | ADR "Legacy data": the floor applies to sessions summarised after this ships, discriminated by `summary_generated_at`. Nulling 121 historical rows destroys the only pre-correction corpus for no gain. Taken deliberately, not by side effect. |
| D-e | **~~Measure only~~ → the 250-token hard maximum is enforced.** Over budget ⇒ one retry with an explicit tightening instruction ⇒ on second failure, terminal-eligible `digest_over_budget`. `session_digest_target_tokens` (180) / `session_digest_max_tokens` (250) are settings; every generation logs `digest_tokens` so the compression curve is still measurable. | **Revised on codex review.** The first draft measured but did not enforce, on the grounds that no AC tests it. That satisfied the ACs while dropping a normative ADR requirement — D3 says "250 hard maximum" (ADR:267). Enforcing via the existing validate-and-retry path costs nothing extra, and the constants stay tunable when the curve lands. |
| D-f | The sweep is a **new scheduler loop** with its own single-flight guard, which additionally skips while `_consolidation_in_progress` is set. | Reuses the existing guard *pattern* per the ADR; the extra skip prevents the sweep racing a consolidation pass that is itself advancing `ended_at`. |
| D-g | AC-9/10/12/13 fixtures are **pre-registered synthetic**, labelled as such in the result, with the eligible real-corpus population counted and reported alongside. | ADR corpus-feasibility rule. A 59-session corpus cannot supply ≥6 Tier-A contradictions and ≥4 Tier-B evidenced self-corrections; the permitted response is a pre-registered synthetic supplement. |
| D-h | The producer keeps `budget_role="captains_log"`. | ADR D2 explicitly: "budget_role stays captains_log for now… not taken here." |

---

## 2. Files

| File | Change |
|---|---|
| `src/personal_agent/second_brain/session_digest.py` **(new)** | `SessionDigest`, `DigestItem`, `Correction`, `Locator`, `SessionSummaryOutcome`, failure-reason codes, the span/locator **validator**. |
| `src/personal_agent/second_brain/session_summary.py` | Rewrite: full input, tool payloads, missing-evidence contract, pre-dispatch token check, min-turns floor, structured output + validate + one retry, own role. |
| `src/personal_agent/second_brain/consolidator.py` | Stop calling the summariser per pass; stop setting `session_summary` on the `SessionNode`. |
| `src/personal_agent/memory/models.py` | `SessionNode`: `session_label`, `session_digest`, `summary_generated_at`, `summary_failure_reason`, `summary_attempt_count`. |
| `src/personal_agent/memory/service.py` | **Clobber fix** (drop `session_summary` from the `create_session` MERGE); `write_session_digest` (atomic conditional); `record_session_summary_failure`; `find_dirty_idle_sessions`. |
| `src/personal_agent/brainstem/scheduler.py` | `_session_summary_sweep_loop` + `_run_session_summary_sweep`. |
| `src/personal_agent/captains_log/capture.py` | `read_session_captures(session_id, start, end)`. |
| `src/personal_agent/orchestrator/executor.py` | **F1 fold-in**: `"arguments"` into the `ctx.tool_results` entry, plus `ctx.tool_results` appends on the three non-dispatch paths (`:4560`, `:4595`, `:4656`). |
| `docker/elasticsearch/captains-captures-index-template.json` | **F1b**: explicit `tool_results.arguments` mapping (`text`, `index: false`) so a dict does not dynamically map per key. |
| `src/personal_agent/captains_log/es_indexer.py` | **F1b**: JSON-serialise `arguments`, as `output` already is. |
| `src/personal_agent/config/settings.py` | `session_summary_idle_threshold_seconds`, `session_summary_sweep_interval_seconds`, `session_summary_max_attempts`, `session_digest_target_tokens`, `session_digest_max_tokens`; reword `session_summary_enabled`. |
| `config/model_roles.yaml` | `session_summary: { all: claude_sonnet }` in `roles:`; `session_summary: { deployment: claude_sonnet }` in `bindings:`. |
| `src/personal_agent/config/config_guard.py` | `session_summary` into `_ROLE_HEADER_RE`'s alternation. |
| `tests/personal_agent/second_brain/test_session_summary.py` | Rewritten against the new contract. |
| `tests/personal_agent/second_brain/test_session_digest_validator.py` **(new)** | AC-11 validator. |
| `tests/personal_agent/memory/test_session_digest_write.py` **(new)** | AC-4/AC-6/AC-7 write path. |
| `tests/personal_agent/brainstem/test_session_summary_sweep.py` **(new)** | AC-1/AC-2/AC-3. |
| `tests/personal_agent/config/test_session_summary_role.py` **(new)** | AC-14. |
| `tests/fixtures/session_digest/*.json` **(new)** | Pre-registered fixture sets for AC-8/9/10/12/13 + `REGISTRY.md`. |
| `scripts/eval/session_digest_eval.py` **(new)** | Runs the pre-registered sets, emits the AC-9/10/12/13 report. |

---

## 3. Output contract (D3)

One model call returns:

```json
{
  "label": "string, <= 90 chars",
  "digest": {
    "established":  [Item, ...],
    "decisions":    [Item, ...],
    "unresolved":   [Item, ...],
    "corrections":  [Correction, ...]
  }
}
```

`Item` = `{ text, basis: tool_evidence|user_statement|assistant_reasoning|mixed, span?, locator? }`
— `span` **and** `locator` are **required** when `basis == "tool_evidence"`.

`Correction` = `Item` + `{ tier: "A"|"B", evidence_span, evidence_locator }`; for tier A the
item's own `span`/`locator` cite the **contradicted assistant claim** and `evidence_*` cite the
**contradicting evidence** (AC-11 "two located spans"); for tier B the item's span cites the
self-correction and `evidence_*` cite its support (AC-12).

`unresolved` items are stamped by the **producer**, not the model, with
`as_of = session.ended_at` (compute state, generate meaning — Risks row 1 / Phase 3 dependency).

`Locator` = `{ capture_id: <capture trace_id>, field: <grammar> }`, grammar being one of
`user_text` · `assistant_text` · `tool_result[<i>].output` · `tool_result[<i>].error`.

**Validator** resolves `capture_id` → capture, `field` → that exact text, and requires the
whitespace-normalised span to occur **there**. Unresolvable locator or absent span ⇒
`span_validation_failed` (retry once, then terminal). Bare session-wide containment never passes.

---

## 4. Steps

Each step ends green before the next starts.

**S1 — clobber fix (prerequisite, lands first).**
Drop `s.session_summary = $session_summary` from `create_session`'s MERGE and the parameter with
it. Remove the `generate_session_summary` call and `session_summary=summary` from
`_consolidate_sessions`. → *verify:* `make test-file FILE=tests/personal_agent/memory/test_memory_service.py`
plus a new test asserting a pre-existing `session_summary` survives a `create_session` round-trip.

**S2 — schema + validator.** `session_digest.py`. → *verify:*
`make test-file FILE=tests/personal_agent/second_brain/test_session_digest_validator.py` (AC-11:
resolves-and-matches passes; wrong-field, wrong-capture, elsewhere-in-session, absent-locator all fail).

**S3 — capture completeness (F1 + F1b) + `read_session_captures`.** Arguments on the dispatched
path; `ctx.tool_results` appends on the malformed / gate-blocked / exception paths; ES template +
normaliser. → *verify:* unit tests that a dispatched entry carries `arguments`; that each of the
three non-dispatch paths produces a `success: False` entry with an `error`; that
`_fallback_reply_from_tool_results` renders those as `failed (<reason>)` (pinning the behaviour
change); that `normalize_capture_doc_for_es` stringifies `arguments`; and that the reader returns
only the named session's captures in timestamp order.

**S4 — producer rewrite.** Input assembly (all turns, full text, full tool payloads + args +
status + error, explicit missing-evidence statements), pre-dispatch token check against the
resolved deployment's `context_length`, min-turns floor, structured parse + validate (schema +
spans + **250-token budget, D-e**) + one retry, `SessionSummaryOutcome`. → *verify:* rewritten
`test_session_summary.py` — AC-5 (zero model calls + failure event + no model telemetry on
oversize), AC-8 (prompt completeness against the capture), floor, retry, over-budget rejection,
each failure code.

**S5 — write path.** `write_session_digest` (single Cypher, `MATCH … WHERE s.ended_at =
$expected_ended_at`, returns accepted/refused) — used for a real digest **and** for a floor skip
(D-b); `record_session_summary_failure`, also `expected_ended_at`-predicated, never advancing
freshness; `find_dirty_idle_sessions` (with the `IS NULL` disjunct AC-2 names). → *verify:*
`test_session_digest_write.py` — AC-6(a) loser refused, AC-6(b) concurrent integration,
AC-4 four-way assertion, AC-7 recount, plus `::test_floor_skip_write_is_refused_when_ended_at_moved`.

**S6 — sweep.** Scheduler loop; per dirty+idle session: read captures, generate, conditional write
or failure record. → *verify:* `test_session_summary_sweep.py` — AC-1 (exactly one generation for a
single idle window; none before the threshold), AC-2 (query empty after sweep), AC-3 (regenerates
and reflects appended turns).

**S7 — role + guard + settings.** → *verify:* `test_session_summary_role.py` — AC-14: producer
resolves `session_summary`; key present in both blocks; guard clean; resolved deployment key
byte-identical to `captains_log`'s.

**S8 — fixtures + eval harness.** Author and **freeze** the fixture sets *before* any arm runs
(`REGISTRY.md` records the selection rule, counts and labels, committed in its own commit ahead of
any tuning). Count and report the eligible real-session population. → *verify:* harness runs
offline-parsable; the model arm is held for owner OK (F4).

**S9 — quality gates + self-review.** `make test` · `make mypy` · `make ruff-check` ·
`make ruff-format` · `pre-commit run --all-files`; `code-review` at **high** (src, schema, memory)
and `security-review` (tool payloads now reach a cloud model — egress boundary).

---

## 5. Acceptance criteria → evidence

Four criteria were **fig-leaf** in the first draft — the test asserted a weaker claim than the AC
makes. Codex caught AC-1, AC-5, AC-6, AC-7; each is strengthened below.

| AC | Evidence |
|---|---|
| AC-1 | `::test_single_idle_window_generates_exactly_once` **+ `::test_multi_gap_session_generates_once_per_gap`** (two idle gaps ⇒ exactly 2, which is the bound the AC states and the single-window test cannot exercise) + `::test_no_generation_before_threshold`. **Post-deploy:** the AC's own check — `session_summary_generated` counts per `session_id` in `agent-logs-*` joined against inter-turn gaps from the captures — runs in the runbook. |
| AC-2 | `::test_no_dirty_idle_sessions_after_sweep` (Cypher incl. `IS NULL` disjunct, terminal-failure exclusion, exclusion count + reasons reported alongside the result) |
| AC-3 | `::test_regenerates_after_new_turns_and_reflects_them` |
| AC-4 | `test_session_digest_write.py::test_failure_is_inert_and_loud` — digest, label, freshness, event, retry-eligibility |
| AC-5 | `::test_oversized_input_rejected_before_model_call` — zero client calls **and** a `session_summary_failed` event naming `oversized_input` **and** zero model-call telemetry for the attempt (the AC asks for all three; the draft asserted only the first) |
| AC-6 | Two tests. (a) `::test_stale_writer_is_refused` — unit, asserts the loser's return is `False`, which a re-read-then-write implementation returns `True` for, so the test *discriminates the implementation* rather than observing a surviving value. (b) `::test_concurrent_sweeps_one_refused` — **integration**, marked, real Neo4j on `:7688`, two sweeps with a barrier and an `ended_at` advance between their reads. Without (b) the predicate is asserted but its atomicity is not. |
| AC-7 | `::test_single_turn_session_has_no_digest`, `::test_turn_count_matches_recount` (unit, recount logic) **+ the AC's two population Cypher counts keyed on `summary_generated_at`, run post-deploy** in the runbook — the criterion is a population check and unit tests cannot stand in for it |
| AC-8 | `::test_prompt_input_completeness` over the frozen fixture set — every turn, full untruncated user/assistant text, and per invocation name + **arguments** + status + error + payload under the stated canonical serialisation. Fixtures include a gate-blocked and a malformed-argument invocation, which only exist in the capture after the F1 fold-in. |
| AC-9 | eval report — tool-only facts, ≥5 sessions, all reproduced |
| AC-10 | schema check across stored digests + labelled-set agreement ≥85%, no tag >60% |
| AC-11 | `test_session_digest_validator.py` (4 negative cases: wrong field, wrong capture, span-elsewhere-in-session, absent locator) |
| AC-12 | eval report — 0 false positives, ≥80% recall, Tier-B evidence spans present |
| AC-13 | eval report — the fixture triple |
| AC-14 | `test_session_summary_role.py` |

AC-9/10/12/13 depend on the F4 model arm.

**Test substrate (FRE-375):** unit tests run under `make test` against the redirected stack; the
AC-6(b) integration test needs `make test-infra-up` (Neo4j `:7688`, ES `:9201`, Postgres `:5433`)
and carries the `integration` marker so `make test` skips it. It is run explicitly and its output
quoted in the handoff.

---

## 6. Codex plan-review record

Risk tier **Standard/Complex** (src logic · schema · memory · new-ADR implementation) ⇒ codex
plan-review required and run. Findings and disposition:

| Finding | Disposition |
|---|---|
| AC-8 not delivered — non-dispatch tool paths never reach the capture | **Accepted.** F1 rewritten; fold-in covers all three paths. Chasing it also surfaced F1b (ES dynamic mapping), which codex did not raise. |
| D-e drops the ADR's 250-token hard maximum | **Accepted.** D-e reversed to enforce. |
| AC-6 test cannot prove atomicity | **Accepted.** Split into a discriminating unit test + a real-Neo4j concurrent integration test. |
| AC-1 tests one window, not the multi-gap bound | **Accepted.** Multi-gap test added + the AC's ES/capture join in the runbook. |
| AC-5 asserts only zero client calls | **Accepted.** Failure event + zero model telemetry added. |
| AC-7 population checks not implemented | **Accepted.** Population Cypher moved into the post-deploy runbook; unit test retained for the recount logic only. |
| Failure write has no staleness guard | **Accepted.** Predicated on `expected_ended_at` (D-c). |
| D-b floor-skip watermark not atomic | **Accepted.** Skip write goes through the conditional write; race test added. |
| FRE-375 substrate not named in test commands | **Accepted.** Stated in §5. |
| Steps are not 2–5 minute Sonnet-ready atoms | **Rejected — not applicable.** FRE-947 is `Tier-1:Opus` and is built in this session, not handed to a Sonnet implementer; the routing policy's plan-readiness bar governs handoffs. |
| Plan "allows landing with AC-9/10/12/13 unproven" | **Rejected as stated — already the halt condition, not a loophole.** §7 requires they be reported not-yet-proven and Phase 1's gate stay shut. Resolved properly by getting the F4 eval arm authorised up front rather than by loosening the report. |

## 7. Halt conditions specific to this build

- If the frozen fixture sets cannot be authored to the ADR's per-class minima, that is a finding to
  surface, not a criterion to shrink.
- If the eval arm is not authorised, the PR still lands with AC-1…AC-8, AC-11, AC-14 proven and
  AC-9/10/12/13 **explicitly reported as not-yet-proven** — never as passed. Phase 1's gate stays shut.
