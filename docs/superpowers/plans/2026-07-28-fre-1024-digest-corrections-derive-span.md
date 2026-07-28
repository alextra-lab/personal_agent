# FRE-1024 — the model points, the code quotes

**Ticket:** [FRE-1024](https://linear.app/frenchforest/issue/FRE-1024) — Approved, Tier-1:Opus, `stream:build1`
**Backing ADR:** ADR-0124 (D3 located-span contract, AC-11; Amendment B; Amendment C2 trim-not-discard precedent) · ADR-0125 (D5 marked-not-silent)
**Related:** FRE-993 (trim, do not discard) · FRE-987 (re-enabled the sweep that surfaced this)

---

## 1. The defect, restated from the code

`generate_session_digest` asks the model for a `corrections[i].span` — *verbatim text copied from the
assistant's own message* — **and** a `locator` naming the capture and field that text came from
(`session_summary.py:142-146`). `validate_digest_provenance` then resolves the locator, reads the
source, and requires the model's transcription to occur there (`session_digest.py:426-479`). Any
mismatch returns `SPAN_VALIDATION_FAILED` and **the whole digest is discarded**
(`session_summary.py:778-784`).

The locator already names the text. Given the locator, quoting is a dictionary lookup. The model is
being asked to do lossless copying — the one thing a sampler is structurally bad at — and the entire
artefact is thrown away when it drifts.

### Live evidence (confirmed this session, not inherited)

```
session_id 73417fbd-d2c6-4aed-9411-88a6f8a8196b
2026-07-28T07:45:33 / 08:00:59  session_summary_failed  span_validation_failed
detail: corrections[0] (tier self_correction): span not found at
        cf2467aa-c564-426b-9df9-fbe670970881/assistant_text
```

Two attempts, byte-identical violation. Capture `cf2467aa` **is** readable (verified in
`agent-captains-captures-*`, 6 captures for the session), so the session's evidence is whole — only
the transcription check killed it. `session_summary_max_attempts = 2`, and
`SPAN_VALIDATION_FAILED ∈ TERMINAL_ELIGIBLE_REASONS`, so **the session is terminally retired right
now**: `find_dirty_idle_sessions` excludes it (`service.py:1738-1739`).

Note honestly: the failure *did* reproduce identically twice, so the ticket's symptom-two claim that
terminalising a stochastic failure is empirically wrong is not what the live data shows. It does not
matter — the fix removes the reason, so the classification argument dissolves rather than being won.
What matters is the consequence: removing the reason from `TERMINAL_ELIGIBLE_REASONS` is precisely
what un-retires this session.

---

## 2. Acceptance criteria (from the ticket — the definition of done)

| # | Criterion | How it is proven |
|---|-----------|------------------|
| **AC-1** | A digest whose corrections item cannot be grounded is still persisted, without that item, and the drop is recorded in a field a reader can find | Drive a payload with one ungrounded correction through the real `generate_session_digest`; assert `status=GENERATED`, the other slots intact, `corrections == []`, `corrections_dropped == 1`, and the drop declared in `render_digest` |
| **AC-2** | The model no longer supplies a span at all; the persisted span equals the text at its locator **by construction** | Assert `_SYSTEM_PROMPT` and `digest_schema()` contain no `span`/`evidence_span`; assert a persisted correction's `span == resolve_locator(correction.locator, captures)` even when the model's payload carries a contradictory `span` key |
| **AC-3** | Session `73417fbd` produces a digest | Test-side: a regression driving the live failure's exact shape through the producer and landing a digest. Live-side: post-deploy sweep verification (master's runbook) |
| **Fails if** | a digest is still discarded because one corrections item failed grounding | Negative test: an unresolvable locator yields `GENERATED`, never `FAILED` |
| **Fails if** | the span is taken from model output **anywhere** on the path | `_parse_item` stops reading `span`/`locator` from raw too — not just `_parse_correction` |

---

## 3. Design decisions (settle before coding)

> Revised after codex plan-review. D5 reversed and D6 added on its findings; D1 kept with the
> consequence measured rather than asserted.

**D1 — What the derived span *is*.** `resolve_locator` is turn-granular: it returns the capture's
whole `assistant_response`. So the persisted span becomes the full cited turn, not the sentence the
model was pointing at. This is what the AC asks for ("the persisted span equals the text at its
locator"). Consequences, **measured** against the live graph rather than guessed:
- Provenance is *stronger* — the citation cannot be a paraphrase.
- Reader precision is *weaker* — a reader gets the turn, not the sentence.
- **Bloat, sized:** the live graph holds 7 stored digests, exactly 1 carrying a correction, whose
  `span` is 57 chars and `evidence_span` 48. Derived full-turn spans for a session of that kind run
  1–5 KB each, so the stored correction grows roughly 40×. Spans are **not rendered**, so
  `trim_digest_to_budget` (which measures the rendered projection) is unaffected — the growth lands
  only in the Neo4j `session_digest` string, which is unbounded by any current check.
- In that same live correction `locator != evidence_locator`, so the claim and its evidence really
  do name different turns; the degenerate case where both name one turn stores the same text twice.

Kept as specified because the AC is explicit ("the persisted span equals the text at its locator"),
and a cap would violate it literally. **Not hidden:** a new `correction_span_chars` telemetry field
is added so the real distribution is visible, and the trade is put to the owner in the handoff.

Rejected alternative: a sub-turn pointer (`{capture_id, field, sentence: N}`). It would fix both the
bloat and the precision loss, but it asks the model for an *index*, and an off-by-one mis-quotes
silently — a strictly worse failure than today's loud one. Not asked for; not built.

**D2 — `SPAN_VALIDATION_FAILED`: remove, not reclassify.** After grounding, nothing can raise it —
an ungrounded item is dropped, not failed. Removal is read-safe: `summary_failure_reason` is stored
and compared as a plain string (`memory/models.py:233`; scheduler writes `.value`; Cypher
`IN $terminal_reasons`) and is never *rehydrated* into the enum — no `SummaryFailureReason(...)`
construction exists in `src/`, `scripts/` or `tests/`. Keeping an unraisable member would invite a
future author to reach for it. **The migration effect is intended and tested**, not incidental:
dropping it from `TERMINAL_ELIGIBLE_REASONS` is exactly what makes the already-retired live sessions
re-eligible (AC-3).

**D3 — Where the drop is recorded.** A new `SessionDigest.corrections_dropped: int = 0`, **not**
`items_dropped`. Two different causes with two different meanings; folding them together would make
the existing render note ("Trimmed to fit the digest budget") lie. New field, new note, same
ADR-0125 D5 rule: content cut on an evidence path is marked, never silent. The note renders
**whenever `corrections_dropped` is set**, without the trim note's `and sections` guard — that guard
is a no-op for trimming (which only fires on content-bearing digests) but would suppress disclosure
on a record that lost everything, which is the one case a reader most needs told.

**D4 — Grounding order.** Parse → ground/drop → empty check → trim. Trimming items that are about to
be dropped wastes budget, and `items_dropped` stays about trimming only.

**D5 — Tighten `Correction.span`/`locator` to required.** *(Reversed from the first draft on codex
review, confirmed by live data.)* Once `validate_digest_provenance` is deleted the **type is the only
remaining invariant boundary**, and `write_session_digest` serialises whatever it is handed without
checking anything (`service.py:1254,1331`). Leaving them optional would permit an in-memory
"grounded" correction with no claim provenance at all.

Read-safety is not assumed, it is verified: the single correction stored in the live graph carries
all four fields (`basis, evidence_locator, evidence_span, locator, span, text, tier`). No tolerance
shim is added to `parse_stored_digest` for a shape that is proven not to exist — that would be
speculative code for a hypothetical row.

**D6 — An all-dropped digest must not be stored as a success.** *(Gap found by codex plan-review.)*
Dropping corrections before the trim step opens a path the trim guard cannot cover: a digest whose
*only* content was corrections, all ungroundable, becomes empty → `GENERATED` → the scheduler writes
it (`scheduler.py:752`) → `write_session_digest` stores the empty record, clears the failure and
retry state and advances `summary_generated_at` (`service.py:1318`) → the session leaves the dirty
population **permanently** (`service.py:1735`). That is the delivery failure ADR-0124 Amendment C5
names, and it directly contradicts the guard the trim path already keeps (`session_digest.py:588`)
and the over-budget path's refusal to store an empty digest.

Fix: after grounding, `digest.is_empty() and digest.corrections_dropped` ⇒ do not return `GENERATED`;
record a new failure reason `UNGROUNDED_DIGEST` and let the attempt loop resample.

- **Classified transient, NOT terminal-eligible.** A resample can plausibly produce `established`/
  `decisions` instead, or a resolvable locator. Making it terminal would repeat the exact mistake
  the ticket's symptom two names — terminalising a failure the code itself retries. Cost is bounded
  by FRE-987's exponential backoff, which exists for precisely this shape.
- **Deliberately narrow.** The condition requires `corrections_dropped`, so it guards only the path
  *this change creates*. A model that legitimately emits an all-empty digest still returns
  `GENERATED` exactly as it does today — pre-existing behaviour, out of this ticket's scope, and
  widening it silently would be scope creep rather than a fix.

---

## 4. Steps

### Step 1 — `src/personal_agent/memory/session_digest.py`
1. Delete `SummaryFailureReason.SPAN_VALIDATION_FAILED` and its `TERMINAL_ELIGIBLE_REASONS` entry;
   add `UNGROUNDED_DIGEST` to the **transient** group (D6).
2. Add `SessionDigest.corrections_dropped: int = 0` with the ADR-0125 D5 rationale docstring.
3. Tighten `Correction.span: str` and `Correction.locator: Locator` to required (D5).
4. Add `ground_correction(*, text, basis, tier, locator, evidence_locator, captures) -> Correction | None`
   — resolves both locators; returns `None` if either is `None`, unresolvable, or resolves to
   blank text; otherwise returns a `Correction` whose `span`/`evidence_span` are the resolved texts.
5. Delete `validate_digest_provenance`, `_check_located_span`, `_normalise` (all three orphaned by 4).
6. `render_digest`: declare dropped corrections, **unguarded by `sections`** (D3).
7. Update the module docstring and `Correction`'s docstring: provenance is by construction, and the
   span is the whole cited turn.

**Verify:** `make test-file FILE=tests/personal_agent/memory/test_session_digest_grounding.py`

### Step 2 — `src/personal_agent/memory/session_digest_wire.py`
1. `WireCorrection`: delete `span` and `evidence_span`.
2. `to_storage(envelope, *, ended_at, captures)` — ground each correction via `ground_correction`,
   drop the ungroundable, set `corrections_dropped`.

**Verify:** `make test-file FILE=tests/personal_agent/memory/test_session_digest_wire.py`

### Step 3 — `src/personal_agent/second_brain/session_summary.py`
1. `_SYSTEM_PROMPT`: remove `span`/`evidence_span` from the `correction` shape; replace the `SPANS`
   paragraph with a `LOCATORS` paragraph (point at the turn, do not quote it); reword `CORRECTIONS`
   to cite locators only.
2. `_parse_item`: stop reading `span`/`locator` from raw (fail-condition two).
3. `_parse_correction(raw, captures) -> Correction | None`: parse text/basis/tier/locators, then
   `ground_correction`. Missing/invalid locator ⇒ `None` (a drop), never a raise.
4. `parse_model_output(content, *, ended_at, captures)`: count drops into `corrections_dropped`.
5. Delete the `validate_digest_provenance` import and the violations block (`:778-784`).
6. **Add the D6 empty-digest guard** after parse: `digest.is_empty() and digest.corrections_dropped`
   ⇒ `UNGROUNDED_DIGEST`, `continue` (resample) rather than return `GENERATED`.
7. Add `corrections_dropped` and `correction_span_chars` to the `session_summary_generated` event.

**Verify:** `make test-file FILE=tests/personal_agent/second_brain/test_session_summary.py`

### Step 4 — call sites made stale by 2 and 3
- `scripts/eval/fre994_digest_compression_curve/generate.py:208` — pass `captures` to `to_storage`.
- `scripts/eval/digest_contract_pilot.py:242` — pass `captures` to `parse_model_output`.
- `tests/personal_agent/memory/test_session_digest_wire.py:138,151,182,185` — direct `to_storage` /
  `parse_model_output` calls that gain the `captures` argument (found by codex review).

**Verify:** `make ruff-check` + `make mypy`

### Step 5 — tests
- `git mv tests/personal_agent/memory/test_session_digest_validator.py …/test_session_digest_grounding.py`,
  rewritten: locator resolution (kept), grounding derives the span, ungroundable ⇒ dropped, render
  declares the drop, rendering/measurement cases (kept).
- `tests/personal_agent/second_brain/test_session_summary.py` — the two `SPAN_VALIDATION_FAILED`
  tests become **drop-and-persist** tests (AC-1, and the "fails if" negative).
- New AC-2 test: a payload carrying a contradictory `span` key still persists the resolved text.
- New AC-3 regression: the live failure's shape (correction whose claim span is a paraphrase of
  `cf2467aa/assistant_text`) now yields `GENERATED`; plus an assertion that
  `"span_validation_failed" not in TERMINAL_ELIGIBLE_REASONS`, which is what re-eligibility rests on.
- **New D6 tests:** producer returns `UNGROUNDED_DIGEST` (not `GENERATED`) for an all-dropped digest,
  and `UNGROUNDED_DIGEST not in TERMINAL_ELIGIBLE_REASONS`.
- **New sweep-level regression** in `tests/personal_agent/brainstem/test_session_summary_sweep.py`:
  an all-dropped digest does **not** reach `write_session_digest` and does not advance freshness —
  the integration failure a producer-only test cannot expose (codex review).
- **New read-compatibility test:** a pre-FRE-1024 stored digest whose correction carries
  model-supplied `span`/`evidence_span` still parses and renders (`parse_stored_digest`).
- `tests/personal_agent/memory/test_session_digest_wire.py` — schema carries no `span`.
- `tests/personal_agent/memory/test_session_digest_read.py:279` — the tool-evidence coercion fixture
  constructs a correction with no `span`/`locator`; it gains them under D5's tightening.
- `tests/fixtures/session_digest/REGISTRY.md` + the AC-12 fixture pre-validation test — assert the
  reference locators **resolve**, rather than that a reference span is found at them.

**Verify:** `make test` (full)

### Step 6 — docs
- Module docstrings (Steps 1–3).
- **ADR-0124 gains Amendment E in this PR** (owner decision, 2026-07-28, taken in session so no
  doc-drift ships with the code). It rewrites D3's provenance paragraph (`:236-244`) and AC-11's
  *Check* / *Fails if* clauses (`:1281-1292`), which describe the retired mechanism, and records the
  drop-not-discard rule. AC-11's *outcome* is **strengthened**, not weakened: no stored corrections
  entry can carry an unresolvable locator or a span absent from it, because the span is no longer
  transcribed at all.
- Owner decision on D1 recorded: persist the full resolved text exactly as the AC specifies, with
  `correction_span_chars` telemetry making the size distribution visible; a bound is a later call
  driven by that data, not a pre-emptive cap.

---

## 5. Quality gates
`make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files` ·
`code-review` at **high** (src logic on the memory/evidence path) · `security-review` **not**
indicated (no new input, subprocess, file, auth, secret or network surface — the diff removes a
validation step and reads text already read).
