# FRE-994 — Run the digest compression curve ADR-0124 promised

**Ticket:** FRE-994 (Approved, `stream:build2`, `Tier-1:Opus`) · **Backing ADR:** ADR-0124 D3/D4
**Blocks:** FRE-993 (producer fix consumes this result) · **Depends on:** FRE-992 (merged — ES-union capture reader)
**Date:** 2026-07-26 · **Rev 2** — rewritten after codex plan-review (5 findings, 4 fatal; §8 records each and its resolution)

---

## 1. What this settles

ADR-0124 D3 sets the digest budget at "~180 tokens target / 250 hard maximum, **to be set empirically
by a compression curve**". The curve was never run. The provisional number shipped as an enforced
constraint and has been rejecting generations ever since (FRE-993: 446 calls, $14.19, mean output
1,338 tokens, 57% piled against the 2,048-token call ceiling — all billed, all discarded).

This ticket produces the curve and amends D3 with the measured bound. It does **not** change the
producer — that is FRE-993, which is blocked on this result.

**The one thing this plan must not do is pick another number post-hoc.** Every threshold that
selects the answer is precommitted in §4 and §5 *before* any spend, and the run is free to return
"the data does not settle this" — §6 says what that means and what would.

## 2. Acceptance criteria

| # | Criterion | How it is proven |
|---|---|---|
| AC-1 | The amendment states a **number or a rule**, with the curve data behind it and the sample size it was measured over | ADR-0124 Amendment C + `docs/research/2026-07-26-fre994-digest-compression-curve.md`: per-arm table, N, and the CI on every reported quantity |
| AC-2 | The chosen bound is **achievable by the model in practice**, without truncation | At the selected arm: `rendered_tokens ≤ bound`, `finish_reason == "stop"`, validation passed, for ≥90% of sessions |
| AC-3 | The **call output ceiling implied by that bound** is stated | From the §4.4 token decomposition — structural vs content tokens, p95 — not from a ratio of successes |
| AC-4 | Corpus sourced from **Elasticsearch**, not the local telemetry directory | Harness reads via FRE-992's `load_session_captures(es_client=…)`; manifest records per-session `source` and the disk/ES split |
| AC-5 | Producer stays **disabled**; curve runs **out of band**, never on the live sweep path | Harness never calls `generate_session_digest`, never touches the scheduler; `AGENT_SESSION_SUMMARY_ENABLED` unchanged; asserted by a unit test |
| AC-6 | **Expected cost stated before running**; **actual reported against it** | `--dry-run` prints the projection from exactly-counted prompts (free); run report prints actual and the delta |
| AC-7 | The **relative bound** (D4) is considered as a candidate answer, not only an absolute one | §4.5's interval-censored fit, reported **with its CI and its power**, plus the §5.4 held-out confirmation. Precommitted: if the two shapes are not separable at this N, the amendment says so rather than choosing |

## 3. Corpus (free, already measured)

Frame: `agent-captains-captures-*` excluding `…-subagents-*` on `cloud-sim-elasticsearch` (:9200).
Measured 2026-07-26: 2,787 turn captures, 1,183 sessions, of which **314 are real (UUID-keyed) with
≥2 turns** — the `MIN_TURNS_FOR_DIGEST` floor. 12 synthetic `test-*` ids (637 docs) excluded by an
explicit UUID filter.

Size distribution of the 314 (conversation chars, user + assistant):
p50 3 turns / 4,861 · p90 9 turns / 14,727 · max 20 turns / 69,155.

Sample: stratified by conversation-size quartile, equal draw, deterministic seed in the manifest.
Two disjoint draws: a **fit sample** (§5.3) and a **held-out sample** (§5.4), drawn once, up front.

## 4. Measurement — what is being estimated, and by what rule

### 4.0 The bound is expressed in the units the destination constrains (owner, 2026-07-26)

**Finding.** The digest's destination is a single JSON-string property on the `Session` node:
`write_session_digest` sets `s.session_digest = orjson.dumps(digest.model_dump(mode="json"))`
(`memory/service.py:1253`, `:1267`), and `get_session_digest_views` reads the whole blob back and
renders it. Nothing queries inside it — no item-level nodes, no edges, no index.

**Consequence.** The KG imposes **no token bound**. It imposes a **shape**: four slots, each a list
of items carrying `basis`/`span`/`locator`. ADR-0124 D3's "250 rendered tokens" is therefore not a
destination constraint but a *read-time context-window* constraint, levied on behalf of the Phase-2
consumer that FRE-993 records as parked and non-existent. Calibrating a rendered-token budget
measures the wrong instrument.

**Three changes follow:**

1. **Structured output** (owner instruction). Every call in the harness — generation, extraction,
   judging — uses schema-enforced output via `LiteLLMClient.respond(response_format=…)`. The
   generation schema **is the stored record shape**, so what the model emits is what the graph
   stores, with no lossy re-projection, and `schema_invalid` leaves the measurement instrument.
2. **Arms are structural.** A global token maximum is not expressible in a JSON schema; items-per-slot
   and per-item length are. Arms become `(max_items_per_slot × max_tokens_per_item)` — the units both
   the schema and the KG record actually constrain.
3. **Rendered tokens become a measured output, not the knob.** Still reported per arm — a Phase-2
   consumer pays them and AC-3's call ceiling derives from them — but they no longer parameterise the
   experiment.

**Structured vs free-text contrast (now central).** Today's producer asks for JSON in prose and
parses free text, which is where the 57%-at-2048 truncation lives. One arm is run in **both** modes
so the amendment can tell FRE-993 whether structured output alone eliminates the truncation class.
Structured output is verified to work on both providers in Phase B's first calls, with a documented
fallback to free-text-plus-parse if a provider rejects the schema.

**Scope note.** If the destination constrains shape rather than size, Amendment C will likely replace
D3's *instrument* — a global token maximum → a structural bound plus a separately-sited read-time
projection bound — rather than only its constant. The ticket anticipates this outcome explicitly and
calls it preferable.

### 4.1 Primary endpoint: loss incidence, not mean retention

ADR-0124 D3's definition of wrong is **per-digest**: it "omits a consequential conclusion needed to
avoid repeating settled work." A mean-coverage score answers a different question and can hide a
handful of sessions losing everything behind a healthy average.

> **L(T) = the fraction of sessions whose digest at bound T omits ≥1 consequential conclusion.**

Reported with a 90% bootstrap CI. Mean retention and a severity-weighted variant are reported as
**secondary** descriptors, never as the selector.

**Marginal, not absolute.** The generator omits things even unbounded, so an absolute L(T) confuses
"the bound is too tight" with "the generator is imperfect". The quantity that answers the ticket's
question — *at what point does shrinking the budget start dropping consequential conclusions* — is
the excess over the unbounded arm:

> **ΔL(T) = L(T) − L(unbounded)**

### 4.2 Precommitted decision rule (fixed before any spend)

> **The recommended bound is the smallest arm T where ΔL(T) ≤ 0.10 and the upper end of its 90%
> bootstrap CI ≤ 0.20.**

If no arm satisfies it, the finding is that **no tested bound is safe**, and the amendment says the
bound must come from the Phase-2 consumer instead. That is a legitimate outcome, not a failure to be
argued around.

`partial` coverage is scored **0 in the primary endpoint** (an item half-carried is an item a future
reader cannot rely on) and **0.5 in a reported sensitivity analysis**. Both are precommitted; if they
disagree on which arm is selected, the amendment reports the disagreement rather than picking the
flattering one.

### 4.3 Ground truth: a human-anchored reference set

The metric is only as good as the reference. Making it two `gpt-5.4-mini` calls — one to extract,
one to judge — shares one model's blind spots between the ground truth and its scorer, and nothing
in the design would notice.

- **Calibration subset (8 sessions): I build the reference set by hand**, reading the transcripts,
  before seeing any digest. This is the only genuinely independent ground truth in the study.
- **LLM extractor** (`gpt-5.4-mini`, independent prompt) runs on every sampled session, including
  the calibration subset.
- **Precommitted validity gate:** on the calibration subset the extractor must recall **≥80% of the
  hand-authored reference items** and introduce **≤20% items I judge not consequential**. Below
  either threshold the LLM reference is not fit for purpose → the study runs on the hand-referenced
  subset only (a smaller, honest N), or is discarded. It is not rescued by relaxing the gate.
- **Judge agreement:** on the calibration subset, judge verdicts are compared to mine on the same
  items. Precommitted: **item-level agreement ≥80% and Cohen's κ ≥ 0.6**. Below that, the judge is
  not measuring what it claims and the eval is discarded (memory: a bad eval is discarded, not
  reframed).
- **Anchors are stop conditions, with numbers.** Empty digest must score **retention ≤ 0.05**;
  reference-scored-against-itself must score **≥ 0.95**. Either anchor failing **invalidates the
  primary metric and discards the run** — stated here so it cannot be downgraded to a caveat later.

### 4.4 Separating instruction-failure from envelope overhead

`output_tokens / rendered_tokens` conflates two different things and cannot answer why the producer
bills 1,338 tokens against a 250-token bound. The decomposition is computable exactly, offline, from
the stored raw JSON:

| Quantity | Definition |
|---|---|
| `content_tokens` | tokens of the JSON's *value strings* — `text`, `span`, `evidence_span`, `label` |
| `structural_tokens` | `output_tokens − content_tokens` — braces, keys, `basis`/`tier` tags, locators |
| `rendered_tokens` | `digest_token_count` — the consumer-facing projection |

This separates the hypotheses cleanly: **content ≈ bound while output ≫ bound ⟹ envelope overhead**
(the fix is a bigger call ceiling); **content ≫ bound ⟹ instruction-following failure** (the fix is a
different bound or a different prompt). AC-3's recommended ceiling is derived from
`structural_tokens` p95 + the selected bound, not from a ratio.

**Truncated and unparsable rows are reported, never dropped.** They have no parseable digest, so they
enter a separate `unusable_rate(T)` column. Silently excluding them would bias every ratio toward
successes — which is exactly how the live producer's failure stayed invisible.

### 4.5 Absolute vs relative bound (AC-7), honestly powered

Codex is right that a 20-point regression on a censored response cannot arbitrate this. Two changes:

1. **Use every cell, not one point per session.** Fit `retention ~ f(bound)` against
   `retention ~ f(bound / conversation_tokens)` across all (session × arm) cells and compare fits
   (AIC + cross-validated error). This uses N×7 observations, not N.
2. **Treat the per-session threshold as what it is — interval-censored.** Each session's
   "smallest safe bound" is known only to lie between two arms, so it is fitted with an
   interval-censored model and reported with its CI, not as a point.

**Precommitted:** if the two shapes are not separable — CIs overlap, or cross-validated error differs
by less than its own uncertainty — the amendment states that **the data does not settle the shape**
and names the sample size that would. It does not pick the more interesting answer.

### 4.6 Variance

One call per cell estimates no variability at all. **3 repeats** at arms `b250` and `b700` on 8
sessions (48 extra generations) estimate generation variance; the judge is re-run on those repeats to
estimate scoring variance. Every reported CI includes both.

### 4.7 Free baseline and a stated confound

- **Trivial baseline (no LLM):** take the `unbounded` digest, drop whole items until it fits T, score
  identically. If an instructed digest at T does not beat mechanical truncation at T, the budget
  instruction buys nothing and the amendment must say so.
- **Stated confound:** each arm moves `target_tokens` and `max_tokens` together at ADR-0124's own
  0.72 ratio. The arm is therefore a **policy pair**, not an isolated hard maximum, and every finding
  is about that pair. Named as a limitation, not silently averaged over.

## 5. Arms and phases

### 5.1 Arms — structural, per §4.0

Each arm is a `(max_items_per_slot, max_tokens_per_item)` pair, enforced by the response schema and
restated in the prompt. Named by their implied rendered ceiling so the curve stays comparable to
D3's existing figure, which is reported but not enforced:

| Arm | items/slot | tokens/item | implied rendered ceiling | note |
|---|--:|--:|--:|---|
| `s1x25` | 1 | 25 | ~100 | aggressive |
| `s2x30` | 2 | 30 | ~240 | ≈ D3's 250 today |
| `s3x35` | 3 | 35 | ~420 | |
| `s4x45` | 4 | 45 | ~720 | |
| `s6x55` | 6 | 55 | ~1,320 | |
| `unbounded` | — | — | — | schema keeps the shape, drops both caps and the LENGTH rule |

Six arms rather than seven — the structural parameterisation makes the two smallest of rev 1's
token arms indistinguishable. One additional **mode-contrast** arm reruns `s2x30` in today's
free-text mode (§4.0) on the fit sample.

Generation uses the **production model and production prompt** (`session_summary` → `claude_sonnet`;
`build_prompt` and the system prompt imported, never copied — §7.1). Call ceiling **4,096** on every
arm so it is never binding, with `finish_reason` recorded so truncation stays distinguishable from a
valid stop (the distinction FRE-993 §4 asks for).

### 5.2 Phase A — free

Sampling, prompt assembly, exact input-token counts, cost projection. **Zero model calls.**

### 5.3 Phase B — validity gate (cheap, and it can end the study)

Calibration subset only: hand-authored references, extractor agreement, judge agreement, both
anchors. **All four §4.3 thresholds must pass to proceed.** This is a genuine stop — the outcome
"the metric does not work" is a legitimate result of this ticket and is reported as one.

### 5.4 Phase C — main run, then held-out confirmation

Fit sample across all 7 arms + §4.6 repeats. Then the rule selected by §4.2 is run **once on the
held-out sample** it was not fitted on. A rule that does not reproduce out-of-sample is reported as
not reproducing.

### 5.5 Sample size is what the money buys

L(T) is a proportion, so precision scales as √N. Stated up front so the owner is choosing precision,
not a vague "more data":

| Fit N | 90% CI half-width on L(T) | Verdict |
|---|---|---|
| 20 | ~±18pp | too wide to separate a 10pp decision threshold — would be misleading |
| 40 | ~±13pp | marginal |
| 60 | ~±10pp | supports the §4.2 rule as written |

**Recommendation: fit N=60, held-out N=12.** At N=20 the §4.2 rule cannot be applied honestly, which
makes the cheapest option the one most likely to waste its own spend.

### 5.6 Cost posture

- Lane: **`budget_role="study"`** — FRE-839's isolated one-time-corpus lane ($5 daily / $7 weekly),
  *not* `captains_log`, which is capped out and denying. `study` is `on_denial: raise`, so a denial
  stops the run loudly rather than silently thinning the sample.
- **Cap change authorised by the owner 2026-07-26, for this run only.** Mechanism follows the FRE-771
  precedent: a dated `TEMP BUMP` on the `study` lane in `config/governance/budget.yaml` recording the
  authorisation, with the **reset to $5/$7 committed in the same PR** once the run completes — so
  `main` never carries a raised cap and the record stays durable. The exact figure comes from Phase A,
  not from a guess, and goes to the owner before the bump is applied.
- Generation is Sonnet ($3/$15 per MTok); extraction and judging are `gpt-5.4-mini` ($0.75/$4.50) —
  cross-family for the §4.3 control and ~4× cheaper.
- **The full-run projection is recomputed from Phase B's measured output distribution**, because
  output tokens are simultaneously the cost driver and the unknown being measured.

## 6. Halt conditions specific to this ticket

- Any §4.3 gate fails (extractor recall, judge agreement, either anchor) → **discard**, report why.
  Do not weaken a threshold to save a run.
- No arm satisfies §4.2 → report "no tested bound is safe"; the bound must come from the Phase-2
  consumer. Not a licence to pick another constant.
- Absolute and relative shapes not separable → say so, and name the N that would settle it.
- Projection exceeds the `study` lane → stop and ask. Never raise a cap to fit a run.
- Any need to set `AGENT_SESSION_SUMMARY_ENABLED=true` → stop. The producer stays disabled.

## 7. Implementation

### 7.1 One production change

`src/personal_agent/second_brain/session_summary.py`: `_system_prompt()` becomes
`system_prompt(*, target_tokens=None, max_tokens=None, max_items_per_slot=None,
max_tokens_per_item=None, include_length_rule=True)`, defaulting to settings, and is exported. A
curve run against a *copy* of the prompt measures a prompt that is not deployed and drifts on the
next edit — the eval-validity failure this project has already paid for. Behaviour-preserving for
every existing caller (defaults reproduce today's string exactly, asserted by a test); the structural
arguments append a LIMITS clause only when supplied, and `include_length_rule=False` serves the
`unbounded` arm.

The response schema (§4.0) is **derived from `SessionDigest`**, not hand-written, so it cannot drift
from the model the graph stores. It lives in the harness — production adopting structured output is
FRE-993's decision to make on this study's evidence, not a change this ticket smuggles in.

### 7.2 New files

```
scripts/eval/fre994_digest_compression_curve/
  README.md        # method, reproduction, cost posture, what a result means
  corpus.py        # ES frame, UUID filter, stratification, fit/held-out split, FRE-992 reader
  arms.py          # arm table, generation, truncate baseline, token decomposition (§4.4)
  scoring.py       # extractor, coverage judge, anchors, agreement stats (§4.3)
  analysis.py      # L(T), ΔL(T), bootstrap CIs, interval-censored fit (§4.5), model comparison
  run_curve.py     # CLI: --dry-run | --phase-b | --execute-full, manifest + JSONL
tests/scripts/eval/test_fre994_curve.py
references/        # hand-authored reference sets for the calibration subset (committed)
```

Raw output to `telemetry/fre994_curve/<run_id>/` (gitignored); only curated summaries are committed.

### 7.3 Deliverables

- `docs/research/2026-07-26-fre994-digest-compression-curve.md` — method, corpus, per-arm table with
  CIs, ΔL curve, token decomposition, absolute-vs-relative comparison, baseline, held-out result,
  limitations, actual-vs-estimated spend.
- **ADR-0124 Amendment C** — replaces D3's provisional figure with the measured bound (or records
  that the data does not settle it), states the implied call output ceiling for FRE-993, records the
  curve and sample size.

## 8. Codex plan-review findings and resolutions

| # | Finding (codex) | Resolution |
|---|---|---|
| 1 | No prespecified rule turning the curve into a bound; retention is an adjacent proxy; `partial` excluded without rationale; arms move two knobs | §4.2 precommits the rule; §4.1 changes the primary endpoint to per-session loss incidence ΔL(T); `partial` treatment precommitted with a sensitivity analysis; §4.7 names the policy-pair confound |
| 2 | Reference set and judge are the same model — shared blind spots; spot-check has no method or threshold | §4.3 adds a hand-authored reference set on a calibration subset, with precommitted recall/precision/κ gates and anchor thresholds that **discard** the run |
| 3 | AC-7 regression underpowered and underspecified; censored response; no variance estimate | §4.5 fits over all cells and treats the threshold as interval-censored with CIs; §4.6 adds repeats; precommitted "not separable ⟹ say so" |
| 4 | `output/rendered` cannot separate instruction-failure from envelope; no policy for truncated rows | §4.4 decomposes content vs structural tokens; truncated/unparsable rows reported as `unusable_rate`, never dropped |
| 5 | Invalidation rule subjective; anchor failure carries no consequence | §4.3 makes both anchors numeric stop conditions; §6 lists every discard trigger |

Codex's verdict on rev 1 was "fatally flawed as written". Rev 2 accepts all five findings; none
required abandoning the approach, and all five are fixed before any spend — which is what the review
was for.
