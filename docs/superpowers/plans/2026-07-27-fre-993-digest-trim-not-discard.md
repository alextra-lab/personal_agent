# FRE-993 — session digest: trim, don't discard (+ Amendment C sizing, deterministic-call fail-safe)

**Backing:** ADR-0124 D3/D4 + **Amendment C** (Accepted 2026-07-27).
**Upstream:** FRE-994 (compression curve, done — corpus at `telemetry/fre994_curve/main/`), FRE-996 (JSON contract, deployed).
**Boundary:** reasoning configuration belongs to FRE-1007, **not** this ticket.
**Branch:** `fre-993-digest-trim-not-discard`
**Codex plan-review:** run 2026-07-27; revision 2 below. Four findings accepted, one declined with a measurement.

---

## The defect, restated from the verified source

`second_brain/session_summary.py:762-768` — after a digest has been parsed and has passed
provenance validation:

```python
tokens = digest_token_count(digest)
if tokens > settings.session_digest_max_tokens:
    last_validation_failure = (SummaryFailureReason.DIGEST_OVER_BUDGET, ...)
    continue          # ← throws the whole parsed structure away and regenerates
```

At the deployed `session_digest_max_tokens = 250`, FRE-994 measured **47% of content-bearing
digests rejected** on this line. The `continue` re-enters a loop capped at
`_MAX_GENERATION_ATTEMPTS = 2`, and with a measured prompt elasticity of 0.16 the second
attempt lands at roughly the same length and fails identically. Two billed generations,
nothing stored.

---

## Acceptance criteria (ADR-0124 Amendment C + the ticket's PROOF REQUIRED)

| # | Criterion | How it is proven |
|---|---|---|
| AC-1 | An over-long digest is **trimmed and stored**, not discarded and regenerated | Unit test: one model call, `status=GENERATED`, digest ≤ ceiling, items dropped > 0 |
| AC-2 | Drop order is **deterministic**, not model judgment | Unit tests: fixed input → fixed survivors; model ordering used only *within* a slot |
| AC-3 | No slot is annihilated while another still has a droppable item | Unit test: a 6-item `established` + 1-item `unresolved` keeps the `unresolved` item |
| AC-4 | C1 sizing deployed: prompt target 180 → **120**, rendered ceiling 250 → **400**, call ceiling **2,048 unchanged** | Settings defaults + prompt-rendering test; `_MAX_OUTPUT_TOKENS` untouched |
| AC-5 | Rejection rate measured **before and after on FRE-994's own corpus** | `run_curve.py --trim-baseline`; numbers in the handoff |
| AC-6 | A truncated call is **not re-issued** | Unit test: `OutputTruncated` ⇒ exactly **one** `_call_model` call |

---

## Step 1 — `trim_digest_to_budget` (memory/session_digest.py) → verify: new unit tests pass

New public function beside `render_digest` / `digest_token_count` — the rendered projection
is exactly what the bound measures.

**Drop order (least costly to lose first).** Revised after codex review; anchored on
value-to-a-future-reader, which is what D3's definition of a wrong digest is about:

| Order | Slot | Why here |
|---|---|---|
| 1 | `established` | D3 names it the slot "most at risk of re-deriving facts that are already stored elsewhere" — the cheapest loss |
| 2 | `corrections` | The ADR states the asymmetry directly: "a missed error is **recoverable from the raw evidence**, whereas a false error writes self-confirming state". A dropped correction is recoverable; it is also usually empty, so this rarely fires |
| 3 | `unresolved` | No other home, and Amendment C's own instrument post-mortem calls losing explicitly-left-open questions the biased failure mode |
| 4 | `decisions` | The conclusions D3's definition of wrong is *about* — "omits a consequential conclusion needed to avoid repeating settled work". Dropped last |

*(Revision 1 protected `corrections` most and dropped `unresolved` before `decisions`.
Codex refuted both: my rationale was cost-of-production, not reader value, and the ADR
says the opposite about corrections.)*

**Two phases, so no slot is annihilated while another still has slack** — the concrete
failure codex named is a digest that keeps four recoverable corrections while deleting
every trace that work remains open:

1. **Phase 1** — while over budget, drop the **last** item of the earliest slot in the
   order that holds **more than one** item. Every non-empty slot therefore keeps at least
   one item for as long as any slot has slack.
2. **Phase 2** — only once every slot is down to ≤1 item, drop whole slots in the same
   order.
3. **Never trims to empty.** Amendment C5 names the empty digest as *the* remaining
   delivery failure — it returns `GENERATED`, marks its session clean and is never
   retried. Trimming to nothing would manufacture exactly that.

*Last* item within a slot, because the prompt asks for most-consequential-first (Step 2).
The model supplies ordering *within* a slot; the table plus "from the tail" is the
deterministic mechanism underneath it, so drop order never rests on model judgment alone.

Returns `(trimmed, dropped_count)`. `SessionDigest` is frozen → `model_copy(update=...)`.

Tests (`tests/personal_agent/memory/test_session_digest_trim.py`, new): already fits ⇒
unchanged and `dropped == 0` · over ⇒ fits after, `established` goes first · phase-1
preserves one item per slot (AC-3) · phase-2 order is corrections → unresolved →
decisions · a single item alone over the ceiling survives and stays over budget · order
stable across repeated runs.

## Step 2 — producer: trim instead of discard → verify: `make test-file FILE=tests/personal_agent/second_brain/test_session_summary.py`

```python
digest, dropped = trim_digest_to_budget(digest, settings.session_digest_max_tokens)
tokens = digest_token_count(digest)
if tokens > settings.session_digest_max_tokens:
    last_validation_failure = (SummaryFailureReason.DIGEST_OVER_BUDGET, ...)
    continue
```

Reachable only when a **single item** exceeds the whole ceiling. **Measured**: across
FRE-994's 100 calls, 549 items, the largest single item renders at **118 tokens** (p99 98,
p50 37) against a 400 ceiling — 3.4× beyond anything observed. Codex proposed a corrective
resample for this case; declined, because building a recovery path for a case the corpus
never produces is machinery that can only be tested synthetically. It keeps the one retry
it has today and fails loudly after.

`session_summary_generated` gains `digest_items_dropped` and `pre_trim_digest_tokens` —
the live before/after signal once deployed, and the only way this rate is observable in
production rather than only in the FRE-994 corpus.

Prompt: one sentence in the SLOTS paragraph — items are emitted **most-consequential-first
within each slot**. Required by the ticket ("must not be the only mechanism") and it is
the half the drop-order table cannot supply. It adds no ceiling of any kind, so C4 is
untouched.

## Step 3 — the deterministic-call fail-safe, scoped to truncation → verify: new test asserts one call

`session_summary.py:722-727` hands exactly this decision to this ticket:

> *"Retrying a deterministic truncation is unlikely to pay, but changing that bound is a
> sizing decision and belongs to FRE-993, not to re-labelling the reason."*

So the fail-safe is **`OUTPUT_TRUNCATED` alone**: the reply hit the 2,048 call ceiling, and
the retry re-issues a byte-identical request against that same ceiling. Median output is
~250 tokens, so a call that reached 2,048 did not do it by sampling noise — and unlike a
format fault there is no cheap recovery. It becomes a `break`, not a `continue`.

**Revision 1 collapsed the retry for every `TERMINAL_ELIGIBLE_REASONS` member. Codex
refuted it and I accept:** a resample of an identical request *can* move
`SCHEMA_INVALID`/`SPAN_VALIDATION_FAILED` invalid→valid, and Amendment C5 measured 2%
contract drift that a retry plausibly recovers. Killing that retry would spend real
delivery to save a call the ticket does not ask to save. Those two reasons keep their
retry; the existing `test_schema_violation_retries_once_then_fails` and
`test_retry_can_succeed` are therefore **unchanged**.

`DIGEST_OVER_BUDGET` also keeps its retry — post-trim it means one pathological item, and
a resample is the only thing that could shorten it.

**Master correction, 2026-07-27: this is a guard, not a saving.** FRE-994 measured zero
truncations in 100 calls, so as scoped it protects a case that does not currently fire.
Build it; claim no cost win from it anywhere in the handoff.

## Step 4 — Amendment C sizing (config/settings.py, .env.example) → verify: `make test-k K=session_digest`

- `session_digest_target_tokens`: `180` → **`120`**
- `session_digest_max_tokens`: `250` → **`400`**
- `_MAX_OUTPUT_TOKENS` stays **2,048** (C1: never binding — zero truncations in 100 calls,
  largest billed output 1,568). Its comment says "bounded at ~250 tokens" → update to 400.
- Both field descriptions re-cite Amendment C and drop "to be set empirically by a
  compression curve" — the curve has run.
- `.env.example` commented defaults updated (they read 180 / 250).

No env override exists for either key on the deployed stack (verified: only commented
lines in `.env.example`; master independently confirmed none in `.env` or the running
container), so the defaults are the live values.

**Deploy note (master, 2026-07-27): deploys are on HOLD for a batched deploy.** These
defaults are baked into the gateway image, so the new sizing is **not live at merge** —
the runbook must say batched-deploy-pending rather than implying it takes effect on
merge. Trimming has the same property; both arrive together at the batched rebuild.

## Step 5 — *(removed)*

Revision 1 proposed adding `max_items_per_slot` / `max_words_per_item` knobs to
`system_prompt()` for the ticket's item three. **Dropped**, on two independent grounds
codex surfaced and I verified:

1. Amendment **C4** removed per-slot item ceilings from *any* sizing role on measured
   evidence (rendered median 221 → 224; completion 19/20 with and without).
2. `test_system_prompt_takes_no_argument_the_curve_does_not_use` is an explicit
   regression guard that pins the signature to exactly `{target_tokens, max_tokens,
   include_length_rule}`, with a docstring saying an earlier revision accepted structural
   per-slot limits and FRE-996 measured them not to work: *"Production surface added for
   an eval that then does not use it is production surface nobody maintains."*

**Master correction, 2026-07-27 — do not record item three as already refuted.** Both
grounds above bear on **items-per-slot only**. Ticket item three was explicitly the
*combination* items-per-slot **and words-per-item**, on the argument that tokens are
invisible to a generator while words are countable. Nothing in C4 or in that regression
test touches words-per-item, which is untested in either direction.

The correct record, and the premise for the study ticket:

1. The untested half needs a **measured curve arm** — so it is a study, not a build.
2. Once trimming exists the urgency drops anyway: landing outside the bound stops being
   destructive, which is the same logic the ticket already applies to the 250→400 flip.

## Step 6 — measure before/after on FRE-994's corpus → verify: run it, record the numbers

`analysis.trim_baseline(records, bounds=(250, 400))` + `run_curve.py --trim-baseline`.
Rebuilds each stored `SessionDigest` via `parse_stored_digest`, applies
`trim_digest_to_budget`, reports per arm and pooled:

- `rejected_before` — content-bearing digests over the bound under discard semantics
- `rejected_after` — over the bound after trimming (expected: 0)
- `items_dropped` distribution and the share of digests trimmed at all

Zero model calls, zero dollars. This is the trivial baseline FRE-994's plan named and
never ran — the ticket calls it "the clearest gap in that study". Numbers go in the
handoff (AC-5).

## Step 7 — docs → verify: read back

- ADR-0124 Amendment C gains an **"Implemented by FRE-993"** note recording the mechanism
  C2 called for, **and** the consumer-side caveat codex raised: D4 measures five digests
  at ~250 tokens as ~74% of a p50 assembled context, so **400 is a producer rejection
  ceiling, not a hydration entitlement** — D4's relative bound ("annotation may never
  exceed the token count of the facts it annotates") governs the consumer. Verified: no
  consumer hydrates digests into an assembled context today — the only reader is the
  Phase-1 gateway view (`memory/service.py:get_session_digest_views` →
  `gateway/app.py`). Recorded so Phase 2 inherits the constraint rather than the number.
- ADR **status is not** changed — master's call.
- Module docstrings in `session_summary.py` / `session_digest.py` that quote 250.

## Step 8 — gates

`make test` (module, then full) · `make mypy` · `make ruff-check` · `make ruff-format` ·
`pre-commit run --all-files` · **code-review `high`** (src + memory + cost) ·
**security-review** (prompt surface changed) · fix findings on-branch · PR.

---

## Halt conditions honoured

- One ADR phase, one PR — Amendment C only; FRE-1007's reasoning declaration explicitly out
  (codex confirmed no leak: "most-consequential-first" is content ordering, not reasoning config).
- No historical rows dropped or quarantined; no migration; no substrate write path changed.
- Nothing here re-enables the summary sweep (FRE-987 still open).
