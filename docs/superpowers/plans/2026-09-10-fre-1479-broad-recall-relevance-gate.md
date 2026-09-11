# FRE-1479 — Preserve the reranker score to the admission boundary, gate on it, and stop admitting on rank order

**Ticket:** FRE-1479 · **Backing:** ADR-0148 D4 · **Blocked by:** FRE-1477 (`Awaiting Deploy`, merged
`4d4cf93e`) · **Diff class:** self-serve (no production write path; the calibration writes only to the
FRE-375 test substrate) · **Risk tier:** Complex.

**Revision 2**, after codex plan-review returned ten blocking findings. All ten are folded in. §6
records what each changed, because three of them changed the design rather than a detail.

---

## 1. What the code does today, verified in source

| Step | Location | The score |
|---|---|---|
| `rerank()` returns `RerankResult(index, score, document)` | `memory/reranker.py:280-396` | real, from **whichever** backend answered |
| primary fails → `_rerank_fallback` succeeds | `:358`, `:430` | real, from **Qwen3-Reranker-4B**, a different scale |
| both fail → `_passthrough` | `:324`, `:342`, `:428`, `:456` | **fabricated: `1.0 / (i + 1)`** |
| backend returns fewer items than sent | `:227-236` | **partial** — no completeness check |
| `_rerank_fused_items` sorts by `{rr.index: rr.score}`, returns `[items[i] ...]` | `memory/service.py:5333-5338` | discarded after sorting; omitted indices appended unscored |
| `_multipath_broad_entities` resolves via Cypher | `:5391-5445` | no score column |
| `query_memory_broad` returns the dicts unchanged | `:5872-5880` | none |
| `BroadRecallResult` | `memory/protocol.py:136-148` | no score field |
| `_format_broad_recall_context` admits everything | `request_gateway/context.py:115-170` | nothing to gate on |

### The no-score case is four cases, not one, and three of them are worse than the ticket states

ADR-0148 D4 and the ticket both say `_rerank_fused_items` "returns the items unchanged" when the
reranker is disabled, raises, or returns nothing. Only the first is true.

1. **Reranker disabled** — caught by `_rerank_fused_items`'s own guard (`:5307`), before `rerank()` is
   called. Items really do arrive unscored. The ticket's description holds.
2. **Both backends down** — `rerank()` catches the failure itself and returns `_passthrough`, which
   fabricates `score = 1.0 / (i + 1)`. That is **rank order wearing the score field**, and it arrives
   as a full, well-formed result set.
3. **Primary down, fallback up** — `rerank()` returns real scores from **Qwen3-Reranker-4B**
   (`config/models.yaml:557`), not from Voyage `rerank-2.5` (`:541`). FRE-695 states the rule these
   violate: *"reranker score scales are arbitrary and not comparable across arms; the floor is
   per-arm, never transferable."* A Voyage-calibrated bound applied to a Qwen score is a wrong
   admission decision on a routine outage.
4. **A partial response** — `_attempt_rerank` iterates whatever the backend returned with no
   completeness check, so one call can yield some scored and some unscored items.
   (`separation_benchmark.parse_rerank_response:346` **does** validate this and raises on truncation.
   Production does not. The asymmetry is recorded, not fixed here.)

The remedy for all four is one rule, and it is FRE-1477's own, one component over: **a score carries
the identity of the component that produced it, and a score from anything other than the calibrated
component is not a relevance value.**

### A fifth no-score case: the legacy branch

`query_memory_broad` runs `_multipath_broad_entities` only under `multipath_recall_enabled and
query_text` (`:5871`). The `else` branch is ADR-0100 single-path retrieval and never reranks.
Production sets the flag true, but its default is `False`.

### The gate governs two document populations, not one

`_resolve_item_texts` builds **entity** documents as `name + ' ' + description` (`:5255`) and **turn**
documents as `coalesce(t.summary, t.user_message, '')` (`:5266`). Turn hits — surfaced by the lexical
arm — expand into broad entities at `:5417`, and those entities inherit the **turn's** score. So a
bound measured only on entity-shaped documents would gate a materially different population without
evidence. The calibration measures both, as production builds them.

---

## 2. Scope boundary — what this ticket does not build, and why the first plan's mechanism was wrong

**The four-state status vocabulary is FRE-1476's.** `MemoryContextStatus`, `POPULATED`,
`NOTHING_RELEVANT`, `WITHHELD` and `UNAVAILABLE` appear nowhere in `src/`. FRE-1476 is `Approved`,
unstarted, on `stream:build1`. FRE-1479 is not marked blocked by it.

FRE-1479's AC-4 has two halves. The first — *the items are not admitted* — is fully deliverable here
and is the criterion's substance. The second — *the state is `UNAVAILABLE` rather than
`NOTHING_RELEVANT`* — names values that do not exist.

**Revision 1 proposed returning `None` so `pipeline.py:204-209` would raise
`context_assembly:memory_unavailable`. That does not work, and the ADR says why.**
`assemble_context` passes every result through `_inject_behavioural_stances` (`context.py:598`),
which runs when `memory_context is None` and builds a populated list from the curated standing set
(`:317`). The pipeline only sees the final container. So on any authenticated turn with a standing
stance — the normal condition — the `None` is gone before the check. This is exactly ADR-0148 D2's
*"Reason one: a standing layer populates the context on most turns"*, which is why D2 scopes the
status to the recall layer rather than the container. Returning `None` would also discard
`recent_sessions`, which the same formatter appends to the same list (`:160`).

**The channel is therefore the discard report, which is already a recall-layer channel.**
`RecallDiscardReport` (`context.py:51-70`) travels beside the context, is not touched by the stance
layer, and already feeds the turn-evidence record. Broad recall records:

* `DropReason.RECALL_RELEVANCE_BOUND` — the item had a calibrated relevance value and it was below
  the bound. FRE-1477's member, reused.
* `DropReason.RECALL_RELEVANCE_UNAVAILABLE` — **new** — the item had no calibrated relevance value at
  all: no score, a fabricated passthrough score, or a score from a component other than the
  calibrated one.

Those two are distinguishable in the record, which is AC-4's substance: the path that could not
establish relevance is not reported the same way as the path that established it and admitted
nothing. When FRE-1476 lands it composes the first into `NOTHING_RELEVANT` and the second into
`UNAVAILABLE`. **This is recorded in the PR body and the handoff as an explicit partial on AC-4's
second half.**

---

## 3. Plan

### Step 1 — The reranker's output carries the identity of what scored it (fold-in)

`src/personal_agent/memory/reranker.py`

* `RerankResult` gains `model_id: str | None = None` — the model that produced `score`, or `None`
  when nothing did. Documented as the score's provenance, not a label.
* `_attempt_rerank` stamps its own `model_id` on every result it builds.
* `_passthrough` leaves it `None`, and its docstring states that its scores are rank order, not
  relevance.

This is one flag covering three of the four no-score cases: passthrough (`None`), the fallback model
(a different id), and a partial response (missing items never get a result at all).

**Verify:** `uv run pytest tests/personal_agent/memory/test_reranker.py -q`, plus new tests asserting
`_passthrough` yields `model_id=None`, a primary response yields `rerank-2.5`, and a fallback
response yields the Qwen id.

### Step 2 — The core stops discarding the number it ordered by

`src/personal_agent/memory/fusion.py`

* `FusedResult` gains `rerank_score: float | None = None` and `rerank_model: str | None = None`.
  `score` keeps the RRF value, unchanged. Both default, so no construction site changes.

`src/personal_agent/memory/service.py` — `_rerank_fused_items`

* Every early return (`reranker_enabled` off, blank query, `len(items) <= 1`, the `except`, empty
  results) leaves both fields `None`.
* On a scored return, rebuild each scored item with `dataclasses.replace(item, rerank_score=rr.score,
  rerank_model=rr.model_id)`. Items the response omitted keep `None`, exactly as they already keep
  their fused position.
* **The function stays a permutation of its input.** No item is dropped, no threshold is applied.
  ADR-0103 §4 and ADR-0104 AC-5 are untouched: the core orders, it does not gate.

**Verify:** `uv run pytest tests/personal_agent/memory/test_multipath_core.py -q` (the existing
`TestRerankNeverGates` class, `:264`), plus new tests for AC-1 and AC-5.

### Step 3 — The score survives to the boundary

`src/personal_agent/memory/service.py` — `_multipath_broad_entities`

* Each resolved entity dict is rebuilt (not mutated) carrying `relevance_score` and
  `relevance_model` from its source fused item. A turn item expanding to several entities gives each
  the turn's own score — it is the item the reranker scored. Name-dedup keeps the first, which is the
  highest-ranked.
* The legacy single-path branch sets neither, so its items carry no relevance value by construction.

`BroadRecallResult` is **unchanged**. Revision 1's `relevance_available` field is dropped: it was an
aggregate over a per-item fact, and it would have made a partial response look wholly scored. The
per-item keys carry everything the boundary needs.

**Verify:** a new unit test drives `_multipath_broad_entities` with a stubbed core and asserts the
entity dict's `relevance_score` equals the score `_rerank_fused_items` computed for that item, and
that a turn-derived entity carries its turn's score (AC-1).

### Step 4 — The gate at the admission boundary

`src/personal_agent/request_gateway/context.py`

* `_format_broad_recall_context(broad)` → `(items, discards)`.
* **The predicate is per item**, and it is armed only when a bound is configured and
  `broad_recall_relevance_gate_enabled` is True. When armed, an entity item is admitted only if
  **all** of these hold:
  * `relevance_score is not None`, and
  * `relevance_model` equals the calibrated artifact's component model, and
  * `relevance_score >= broad_recall_relevance_bound`.
* Failing the third → `RECALL_RELEVANCE_BOUND`. Failing either of the first two →
  `RECALL_RELEVANCE_UNAVAILABLE`. A `None` score is never compared against a float.
* A `None` bound, or the gate disabled, leaves admission exactly as it is today. Never a silent
  default to zero (D4).
* **`recent_sessions` are unchanged and still admitted.** They are not entity items, the reranker
  never scored them, and this ticket's obligation is stated over items entering `memory_context` from
  the entity path. Recorded as a stated limit rather than silently widened.
* The `MEMORY_RECALL` branch returns the formatted items and the new discard report. It does **not**
  return `None`; see §2.

`src/personal_agent/captains_log/turn_evidence.py`

* `DropReason.RECALL_RELEVANCE_UNAVAILABLE`, documented against `RECALL_RELEVANCE_BOUND`: the bound
  member says "measured, and too low"; this one says "never measured, or measured by something else".

**Verify:** new tests for AC-2, AC-3, AC-4.

### Step 5 — The bound, its artifact and its guard

`src/personal_agent/config/settings.py`

* `broad_recall_relevance_bound: float | None = None` (ge=0, le=1 — Voyage relevance scores are in
  `[0, 1]`). Docstring states the space is the serving **reranker's** own and is not comparable to
  `proactive_memory_relevance_bound` (FRE-695).
* `broad_recall_relevance_gate_enabled: bool = True` — off restores pre-change admission exactly,
  which is what makes AC-3 able to fail.
* **If Step 7's calibration returns a bound, its measured value becomes this field's committed
  default in this same PR**, so the configured value equals the artifact and AC-6 can hold. If it
  returns an incompatibility, the default stays `None` and the gate ships inert, as FRE-1477's did.

`src/personal_agent/config/calibration.py`

* `RelevanceCalibration`, `ParityRecord` and `CalibrationComponent` are **left exactly as they are.**
  No base class, no field rename. Revision 1 proposed extracting a field called `negative_median`;
  the field is `negative_median_neo4j` (`:148`) and `test_relevance_bound_calibration.py:107` reads
  it by that name.
* Add a sibling `RerankerRelevanceCalibration` with its own names: `bound` (the reranker's own
  space), `negative_median_rerank`, and per-population breakdowns
  (`entity_positive_scores` / `turn_positive_scores`) alongside the combined lists AC-7 is judged on.
* Add `RerankerParityRecord` — P1 aggregates plus the instrument-sanity result (see Step 6).
* `BROAD_RECALL_RELEVANCE_BOUND_FILE = "broad_recall_relevance_bound.json"` and its loader.

`src/personal_agent/config/config_guard.py`

* `check_broad_recall_bound_calibration(root, settings)` — the same five reportable states as
  FRE-1477's check: a bound configured with no artifact; an artifact naming a reranker other than the
  serving one; bound and configured value disagreeing; a bound configured despite a recorded
  incompatibility; a malformed artifact. The serving reranker is read from the `reranker` role
  (`resolve_role_definition`), which is what `rerank()` itself resolves.
* A standing incompatibility with **no** bound configured is not a finding, for FRE-1477's stated
  reason: `check_config.py` fails CI on any finding, which would wedge every build until the owner
  acts.
* Registered in `run_all_checks`.

`docs/reference/CONFIG_INVENTORY.md`

* Regenerated via `scripts/audit/config_inventory.py` and committed.
  `tests/scripts/test_config_inventory.py:50` fails CI on an `AppConfig` field with no inventory row.

**Verify:** `uv run pytest tests/personal_agent/config/ tests/scripts/test_config_inventory.py -q`,
plus new tests for AC-6 including the induced-mismatch case.

### Step 6 — The calibration harness

`scripts/eval/fre1479_reranker_calibration/{__init__,analysis,calibrate}.py` + `README.md`

* **Bound choice reuses FRE-1477's `choose_bound` unchanged.** It is score-space agnostic and already
  carries D4's two constraints and the whole-positive-set denominator. Importing rather than copying
  keeps one definition of the rule.
* **What it measures.** The production `rerank()` call, against the serving reranker, over
  `semantic_probe.yaml` (54 cases — FRE-694's, FRE-695's and FRE-1477's own probe set). Metric is
  FRE-695's: per expected entity a positive, and per query the strongest non-match as the negative.
* **Both document populations**, built the way production builds them:
  * entity documents — `name + ' ' + description`, as `_resolve_item_texts:5255`;
  * turn documents — `coalesce(summary, user_message, '')`, as `:5266`, labelled positive when the
    turn `DISCUSSES` an expected entity, which is exactly how `_multipath_broad_entities:5417`
    expands it.
  One bound governs the union, because one bound governs the path. The artifact records the two
  sub-populations separately so a reader can see whether one shape drags the other.
* **Substrate.** `seed_replay` into the FRE-375 test stack (:7688), as FRE-1477's harness does — the
  turn documents and their `DISCUSSES` edges must exist to be labelled. Test substrate pinned at
  module top before any `personal_agent` import. No production row is read or written.
* **Parity, both checks required before any number is reported.**
  * **P1 — cross-implementation.** `separation_benchmark.py`'s `voyage-rerank-2.5` arm (`:555-560`)
    is an independently authored pipeline with its own corpus build, HTTP client and response parser.
    Compare the three FRE-694 aggregates at FRE-694's own 0.02 tolerance.
  * **P2R — instrument sanity.** FRE-695's own gate (`:620-629`): a trivial relevant/irrelevant pair
    must rank the relevant document first. FRE-1477's P2 was an index-fidelity check and **has no
    reranker analogue** — no index sits between the model and the score. That is recorded rather than
    replaced by a vacuous substitute.
* **Spend.** 54 queries against a paid Voyage endpoint at $0.05/MTok; FRE-695 ran the same shape. The
  key is read from `pass` (`VOYAGEAI_API_KEY`) at run time, never logged, never persisted.

**Verify:** `uv run pytest tests/test_eval/test_fre1479_calibration_analysis.py -q` covers the pure
analysis with no network.

### Step 7 — Run it, commit the artifact, write the research note

`config/calibration/broad_recall_relevance_bound.json` and
`docs/research/2026-09-10-fre-1479-reranker-relevance-calibration.md`.

**The outcome is not predicted here.** FRE-695 measured Voyage rerank-2.5 at Youden J 0.73, and
judged its best reranker arm (J 0.785) at roughly *"88% recall @ ~9% FP"* — under D4's 90%. An
incompatibility is a live possibility. The harness reports what it measures.

* **Compatible** → the measured bound becomes the committed default (Step 5) and the gate is live.
* **Incompatible** → no bound is configured, the gate ships inert, and the finding goes to the owner,
  exactly as on FRE-1477.

### Step 8 — Tests, one per criterion

`tests/personal_agent/memory/test_broad_recall_relevance.py`,
`tests/personal_agent/config/test_broad_recall_bound_calibration.py`,
`tests/test_eval/test_fre1479_calibration_analysis.py`.

| AC | Test asserts |
|---|---|
| AC-1 | the value at `_format_broad_recall_context` equals the score `_rerank_fused_items` computed for that item — driven through the core, the resolver and the adapter, never injected at the boundary |
| AC-2 | a candidate at the calibrated median top-ranked non-match is not admitted |
| AC-3 | the same fixture **is** admitted with `broad_recall_relevance_gate_enabled=False` |
| AC-4 | five no-score cases — reranker disabled, `_passthrough`, the fallback model's id, a partial response, and `multipath_recall_enabled=False` — none admits, each records `RECALL_RELEVANCE_UNAVAILABLE`, and none records `RECALL_RELEVANCE_BOUND` |
| AC-5 | `_rerank_fused_items` returns a permutation of its input — same length, same `item_id` set — for a fully scored, an unscored and a partially scored response |
| AC-6 | the artifact exists, names the serving reranker, its bound equals the configured value; an induced component mismatch raises the `config_guard` finding and leaves the configured value unchanged |
| AC-7 | applying the artifact's bound to its whole combined `positive_scores` admits ≥ 90% — or, if incompatible, that no bound is configured and the artifact records the reason (D4's reserved case) |

### Step 9 — Gates

`make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`,
then `feature-dev:code-reviewer` and `security-review` scoped to `git diff origin/main...HEAD`.

---

## 4. Acceptance criteria — how each is decided

Every criterion is decided from this branch's own deliverable: a named test with its result, or the
committed artifact read directly. AC-6 and AC-7 read the artifact the calibration wrote, so master
decides them without re-running the measurement.

## 5. Risks

| Risk | Mitigation |
|---|---|
| A fabricated or foreign-model score is gated as if calibrated | Step 1 carries the model id; the gate compares it; AC-4 drives all five cases |
| A partial response admits its unscored items on rank order | The predicate is per item and a `None` score is never compared; AC-4 and AC-5 both drive it |
| The bound is applied to a document population it never measured | Step 6 measures entity and turn documents as production builds them |
| The plumbing changes admission before a bound exists | The gate is inert while the bound is `None`; the only change with no bound is the score riding along |
| The calibration returns incompatible and the ticket looks unfinished | D4 reserves that case. The mechanism, the artifact and the finding are the deliverable, as on FRE-1477 |
| AC-4's state half is not deliverable | Stated in §2, in the PR body and in the handoff; FRE-1476 completes it |

## 6. What codex plan-review changed

Ten blocking findings, all verified against source before acceptance. Three changed the design:

| # | Finding | Change |
|---|---|---|
| 1 | A successful **fallback** returns real Qwen scores that a Voyage-calibrated bound would gate | Provenance carries the **model id**, not a bool |
| 2 | A **partial** response mixes scored and unscored items in one call | The predicate is per item; the aggregate `relevance_available` field is dropped |
| 3 | `_inject_behavioural_stances` converts `None` into a list, so the `memory_unavailable` signal never fires | The unavailability channel is the **discard report**, not the container |
| 4 | Returning `None` would also discard `recent_sessions` | No `None` return; sessions unchanged |
| 5 | Turn documents are `summary`/`user_message`, not `name + description` | The calibration measures both populations |
| 6 | The field is `negative_median_neo4j`, not `negative_median` | No base class, no rename; a sibling model instead |
| 7 | A compatible bound had no configuration write | The measured value becomes the committed default |
| 8 | A required dataclass field breaks five constructors | The field is dropped (see 2); any added field defaults |
| 9 | `CONFIG_INVENTORY.md` is CI-enforced | Regenerated and committed |
| 10 | `test_multipath_recall.py` does not exist | `test_multipath_core.py` |
