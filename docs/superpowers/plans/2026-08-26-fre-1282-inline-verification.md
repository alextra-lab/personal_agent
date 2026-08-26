# FRE-1282 — Inline verification (resolution, reachability, containment) and the block-retry-refuse loop

**Ticket:** [FRE-1282](https://linear.app/frenchforest/issue/FRE-1282) · Tier-1:Opus · stream:build1
**ADR:** ADR-0138 D3 (three checks, containment unit, normalization contract), D4 (bounded retry,
terminal state), D1 (system-record exemption), D2 (vacuous reachability for turn-local sources)
**Depends on (all Done):** FRE-1280 (registry + citation format), FRE-1281 (span extractor),
FRE-1283 (prompt), FRE-1296 (identifiers rendered), FRE-1297 (`fetch_url`)

---

## 1. Scope in five bullets

1. **`grounding/containment.py`** — the D3(c) containment unit and the normalization contract:
   claim-unit extraction (entities, figures, predicate content words), token-boundary matching,
   digit/decimal/unit/alias/case/Unicode tolerance, boilerplate exclusion.
2. **`grounding/verification.py`** — the three checks run per non-exempt span, with outcome kinds
   that keep every failure mode distinguishable (AC-1, AC-3, AC-6). Reachability is decided from
   the **recorded** retrieval, never a re-fetch (§3.2).
3. **`grounding/enforcement.py`** — D4: block → retry with retrieval forced (bounded) → terminal
   explicit no-source statement. Pure decision function + statement builder; no I/O.
4. **`orchestrator/executor.py`** — inline wiring at `step_synthesis`: extract spans → parse
   citations → verify → decide. Retry re-enters `TaskState.LLM_CALL` with a forced-retrieval
   directive. Citation markers are stripped from the delivered reply once verification has
   consumed them.
5. **Telemetry** — `GroundingRecord` on `TurnEvidence` (the ADR's "output side" of the ADR-0125
   contract) plus a structured log event, with containment-unverifiable and true-no-source as
   distinct members (AC-6).

**One phase, one PR.** FRE-1284 (compliance metric), FRE-1285 (light/heavy selection) and
FRE-1286 (entailment) stay out; §3.5 defines the seam each needs.

---

## 2. Answers the ticket thread demands before coding

### 2.1 Master's gate note (FRE-1296): stale citation markers in recalled memory

**Decision: strip at delivery — one place, `step_synthesis`, after verification has consumed the
markers.** The delivered reply is what `capture.py` persists as `assistant_response`, and entity
extraction reads captures, so a single strip at delivery closes storage *and* extraction with one
change and no second rule to keep in sync.

Three reasons this is the right cut rather than stripping at the storage boundary:

- A marker is an artifact of the **verification protocol**, not content. It has done its whole job
  by the time verification returns; carrying it further is pure leakage.
- It is already leaking **today**. FRE-1283 shipped the instruction to emit markers and FRE-1296
  made real identifiers visible to the model, and nothing strips them — so the owner can already
  receive `[S1@a3f91c2b7d4e6f80]` in a reply. This is a live cosmetic defect that this ticket's
  own change closes; folded in, not ticketed.
- Stripping at storage only would leave the marker in the text the user sees, which is worse.

**Residual, stated not hidden:** the AG-UI streaming path emits tokens as they arrive, ahead of
`step_synthesis`, so a marker can still reach a *streaming* client mid-stream. Verification and
storage are unaffected. Recorded in the PR body; not fixed here (it needs a stream-level filter,
which is its own change).

### 2.2 Master's gate note (FRE-1281): the span-extraction budget lane

Span extraction is attributed to `entity_extraction`'s lane (`cost_gate/role_map.py:101`), whose
`on_denial` is `nack` — correct for a background Redis consumer, wrong for an inline blocking one.

**Decision, in two parts:**

- **Lane:** propose `span_extraction` as its own lane in `config/budget.yaml.example`, with the
  live `budget.yaml` edit called out in the handoff as master's to make. The code change is the
  `role_map.py` entry; shipping it *before* the live lane exists would fail
  `validate_role_totality` at startup, so `role_map.py` keeps pointing at `entity_extraction`
  and the split is proposed, not landed. **No behaviour change in this PR.**
- **Denial semantics:** on budget denial (or any extractor failure), verification **cannot run**,
  and the turn is **not blocked**. The outcome is recorded as `VERIFICATION_UNAVAILABLE`,
  distinct from every span-level outcome. Rationale: a budget denial is a fact about Seshat's
  accounting, not evidence about the model's claim; refusing the user's turn because our ledger
  ran dry is punishing the user for our bookkeeping. Fail-open here is a real hole and is
  therefore **recorded** rather than silent — a wave of `VERIFICATION_UNAVAILABLE` reads as an
  infrastructure malfunction, which is exactly what it is.

---

## 3. Design decisions

### 3.1 Containment unit (D3(c)) — required tokens

For each non-exempt span, the required set is **every content word**, where "content word" is
"not in the closed function-word list" (determiners, auxiliaries, copulas, modals, prepositions,
pronouns, conjunctions, common discourse adverbs). For an *atomic* claim — which D1 guarantees
spans are — "every entity, every figure, and every predicate content word" and "every content
word" name the same set. `Paris has 2.1 million residents` requires `paris`, `2100000`,
`residents`; `has` is dropped.

Split of a miss into two outcomes, mechanically:

| Matched | Outcome | Reading |
|---|---|---|
| all required tokens | `PASSED` | contained |
| **none** matched | `NOT_CONTAINED` | the source is unrelated — citation theatre |
| **some but not all** | `UNVERIFIABLE_BY_CONTAINMENT` | topically related; the miss is plausibly paraphrase / translation / unregistered alias |

This is the split ADR-0138 D3 asks telemetry to preserve, and it is decidable without a model.
Both are failures and both take the D4 path; they differ only in what they are recorded as.

**Entity-free predicate spans** (no entity token, no figure token) run containment **and then**
escalate: containment is *necessary but not sufficient* for that class. Since FRE-1286 has not
shipped, the escalation outcome is `ENTAILMENT_REQUIRED`, which is a failure and takes the D4
path. AC-3 is satisfied by the containment half — `this fish is high in mercury` against a source
never mentioning mercury fails before escalation is even reached.

### 3.2 Reachability (D3(b)) — decided from the record, not a re-fetch

**A source has an external referent iff it was retrieved by a call that addressed exactly one
URL** — i.e. `fetch_url`. Everything else (memory, the user's words, `web_search` result sets,
every other typed retrieval tool) is turn-local evidence and passes **vacuously**
(`NOT_APPLICABLE`), which is D2's rule stated literally.

For a `fetch_url` source, reachability is decided from the **recorded** result:

- `fetch_url` raises on non-2xx after redirects, so a failed fetch registers no source at all
  (`register_tool_result(success=False)` → `Admissibility.NO_CONTENT`). Non-2xx is therefore
  already unreachable-by-construction.
- The residual — and the only thing a check can add — is **soft-404 and auth wall**: HTTP 200
  carrying a not-found or login page. Decided on the recorded content: a short extracted body
  (`≤ SOFT_FAILURE_MAX_CHARS`) that matches a soft-404 / auth-wall pattern.

**No live re-probe.** A page fetched seconds ago in this same turn was, by construction,
reachable; re-fetching would measure only whether it broke in the intervening seconds, at the
cost of an inline network round-trip per citation and a non-deterministic verdict. D2 already
says verification for recorded results "resolves against the recorded result, never against a
re-execution"; extending that to the fetched page is the consistent reading. Recorded as a
deliberate v1 decision in the module docstring.

The short-body condition is what bounds false rejections: a legitimate page that merely contains
the words "page not found" somewhere in a long body is not rejected.

### 3.3 Normalization contract (D3(c))

Applied to both the required tokens and the source content, producing comparable token sequences:

- **Unicode / case** — NFKC, then casefold; a diacritic-folded form (NFKD minus combining marks)
  is compared as a fallback, so `Zürich` matches `Zurich` without `Zurich` ever matching `Zulich`.
- **Figures** — a numeric token is parsed to a `Decimal` with group separators removed and
  trailing zeros normalized, so `1,000` ≡ `1000` and `3.0` ≡ `3`. Magnitude words are expanded:
  `2.1 million` ≡ `2100000`.
- **Units** — synonym folding within one unit, never cross-unit conversion: `km` ≡ `kilometre` ≡
  `kilometer`, `%` ≡ `percent`, `°c` ≡ `celsius`. `km`→`mi` is *not* folded: that is a different
  quantity and folding it would let a wrong figure pass.
- **Aliases** — a registered table (`IBM` ≡ `International Business Machines`), matched as a
  contiguous token subsequence. Seed table only; D3 says "where an alias table exists".
- **Token boundary, never substring** — matching is over whole normalized tokens, so `Ham` cannot
  match inside `Birmingham`. Multi-token entities and aliases match as contiguous subsequences.
- **Boilerplate** — excluded at extraction: `nav`, `footer`, `header`, `aside` join `_SKIP_TAGS`
  in `tools/fetch.py`. One line, in the only place that has the DOM to do it correctly.

### 3.4 AC-4's bar — preregistered here, before any result is seen

**Bar: false-rejection rate ≤ 5%** over the variance probe set (≥ 95% of genuinely-supported
spans pass containment), measured per variance class and in aggregate.

- **Justified against the failure it prevents.** A containment miss on a legitimate assertion
  forces the D4 path and produces a refusal the user did not deserve (ADR-0138 D3, "False
  rejections are a first-class cost"). At 5% roughly one turn in twenty carrying a cited claim
  would refuse spuriously — the highest rate at which the contract still reads as grounded
  rather than broken.
- **Demonstrated to reject a broken baseline.** `test_broken_baseline_fails_the_bar` runs the
  same probe set through exact-string matching with normalization disabled and asserts it lands
  **above** the bar. A bar a known-broken implementation would pass is not a bar (ADR-0138,
  "Governance of the set and the bars").

The probe set lives in `tests/personal_agent/grounding/probes/containment_variance.py`, covering
digit grouping, decimal precision, magnitude words, unit synonyms, registered aliases, case, and
Unicode — each with a source that genuinely supports the claim.

### 3.5 Seams left for the downstream tickets

| Ticket | Seam this PR leaves |
|---|---|
| FRE-1286 (entailment) | `ENTAILMENT_REQUIRED` outcome + `EntailmentChecker` Protocol with no implementation registered; wiring it flips that class from failure to a real verdict. |
| FRE-1284 (compliance metric) | `GroundingRecord.first_generation_compliant` on every turn — the metric's numerator, already recorded. |
| FRE-1285 (light/heavy) | `retrieval_forced_before_generation: bool` on the record — the metric's confound flag; this PR always writes `False` except on a D4 retry. |

### 3.6 Deploy control

One setting: `grounding_verification_mode: Literal["off", "observe", "enforce"] = "observe"`.

- `off` — nothing runs.
- `observe` (**default**) — the full pass runs, every outcome is recorded, **nothing blocks**.
  This is what FRE-1284's metric needs to bootstrap, and it is what makes the deploy safe: the
  extractor's production behaviour is unmeasured today.
- `enforce` — D4 blocks, retries and refuses, exactly as D3 requires.

Marker stripping runs in **all three** modes, since the leak in §2.1 is independent of
verification.

D3's "inline and blocking at every enforcement level" is about the *enforcement level* of D5
(light/heavy), which this knob is not — this is the deploy valve that lets master turn the
contract on after watching it, per the trust ladder. Stated plainly in the PR body so it is not
mistaken for a contract-tiering knob.

---

## 4. Atomic steps

Each step: change → verify.

| # | Step | Verify |
|---|---|---|
| 1 | `tests/.../grounding/probes/containment_variance.py` — the variance probe set (no prod code yet). | `make test-file FILE=tests/personal_agent/grounding/probes/containment_variance.py` collects. |
| 2 | `tests/.../test_containment.py` — failing tests for claim-unit extraction, token-boundary, each variance class, the three-way outcome split, AC-3's mercury case, AC-4's bar + broken baseline. | Tests fail with `ModuleNotFoundError`. |
| 3 | `src/personal_agent/grounding/containment.py` — normalization + claim unit + `check_containment`. | Step-2 tests pass. |
| 4 | `tools/fetch.py` — add `nav`/`footer`/`header`/`aside` to `_SKIP_TAGS`; test asserting nav text is excluded. | `make test-file FILE=tests/test_tools/test_fetch.py` (path confirmed at implementation time). |
| 5 | `tests/.../test_verification.py` — AC-1's three seeded negatives, AC-2's positive control, vacuous reachability, soft-404/auth-wall, AC-6's distinctness. | Fails. |
| 6 | `src/personal_agent/grounding/verification.py` — outcomes + `verify_turn`. | Step-5 tests pass. |
| 7 | `tests/.../test_enforcement.py` — AC-5: bound respected, terminal statement reached, no hedge, no named candidate, no silent strip, loop terminates at exactly the bound. | Fails. |
| 8 | `src/personal_agent/grounding/enforcement.py` — `decide` + `build_no_source_statement`. | Step-7 tests pass. |
| 9 | `captains_log/turn_evidence.py` — `GroundingRecord`, optional field on `TurnEvidence`; test that both failure kinds survive round-trip distinctly (AC-6). | New test passes; existing `test_turn_evidence.py` unchanged and green. |
| 10 | `config/settings.py` — `grounding_verification_mode`, `grounding_max_retry_attempts` (default 1). | `make mypy` clean. |
| 11 | `grounding/citations.py` — `strip_citation_markers`; test including the multiply-bound and adjacent-punctuation shapes. | New test passes. |
| 12 | `orchestrator/types.py` + `executor.py` — `grounding_attempts` counter, `step_synthesis` wiring, forced-retrieval directive on retry, strip at delivery. | New `tests/.../test_executor_grounding.py` covers observe-no-block, enforce-retry-then-terminal, strip-always, and `VERIFICATION_UNAVAILABLE` on extractor failure. |
| 13 | `config/budget.yaml.example` — proposed `span_extraction` lane (comment naming the live edit as master's). | `make test` green. |
| 14 | `grounding/__init__.py` docstring — the two halves now meet; ADR-0138 unchanged (nothing here amends a decision). | — |
| 15 | Gates: `make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`. | All clean. |
| 16 | Commit, then self-review: `feature-dev:code-reviewer` + `security-review`, scoped to `git diff origin/main...HEAD`. | Findings fixed on-branch. |

---

## 5. Acceptance criteria → evidence

| AC | Criterion | Proof |
|---|---|---|
| AC-1 | Each check rejects independently, reason names which fired | `test_verification.py::test_unresolvable_identifier_rejects`, `::test_unreachable_source_rejects`, `::test_uncontained_source_rejects` — each asserts the outcome member, not merely a failure |
| AC-2 | A valid citation passes and is delivered | `::test_valid_citation_passes_and_delivers` — positive control; plus `test_enforcement.py::test_passing_turn_is_delivered_unchanged` |
| AC-3 | Containment tests the predicate, not only entities/figures | `test_containment.py::test_entity_free_predicate_is_not_vacuous` — "this fish is high in mercury" vs a mercury-free source |
| AC-4 | Normalization tolerates the variance classes; FRR ≤ 5% | `test_containment.py::test_variance_probe_set_meets_false_rejection_bar` + `::test_broken_baseline_fails_the_bar` |
| AC-5 | Retry loop terminates; terminal state contract-compliant | `test_enforcement.py::test_loop_terminates_at_bound`, `::test_terminal_statement_names_what_was_searched`, `::test_terminal_statement_carries_no_hedge_or_candidate`, `::test_claim_is_never_silently_stripped` |
| AC-6 | Containment-unverifiable ≠ true-no-source in telemetry | `test_turn_evidence.py::test_grounding_record_distinguishes_unverifiable_from_no_source` |

## 6. Diff class

**Escalate.** `step_synthesis` is the production write path for every turn's reply. Flagged in the
PR body for the owner's `/code-review ultra` before merge.

## 7. Risks

- **Extractor cost and latency.** Every turn gains a Sonnet span-extraction call under the
  `observe` default. Mitigated by: the call is skipped when the reply carries no citable
  content, and `off` is one setting away. Surfaced to master in the handoff.
- **False rejections under `enforce`.** Bounded and measured by AC-4; `observe` is the default
  precisely so the rate is seen before it blocks anything.
- **Fail-open on extractor/budget failure.** Deliberate (§2.2), recorded, never silent.
