# FRE-994 — Run the digest compression curve ADR-0124 promised

**Ticket:** FRE-994 (Approved, `stream:build2`, `Tier-1:Opus`) · **Backing ADR:** ADR-0124 D3/D4
**Blocks:** FRE-993 (producer fix consumes this result)
**Depends on:** FRE-992 (merged `fc4553c9` — union capture reader) · FRE-996 (merged `5b0675a5` — output contract)
**Date:** 2026-07-26 · **Rev 4** — rev 3 rewritten after FRE-996 merged; rev 4 after codex plan-review
returned 7 findings (2 critical, 4 major). §9 records what changed and why; §10 records each finding
against its resolution.

---

## 1. What this settles

ADR-0124 D3 sets the digest budget at "~180 tokens target / 250 hard maximum, **to be set empirically
by a compression curve**". The curve was never run. The provisional number shipped as an enforced
constraint — `session_summary.py` rejects any digest whose rendered token count exceeds it — and has
been discarding generations ever since.

The ticket asks how *small* a digest can be before it drops consequential conclusions. FRE-996 added
the other half, and master's dispatch comment makes it co-equal: **where the bound has to sit for the
generator to reach it at all.** Under today's prompt the contract lands at a rendered median of
208–224 tokens with a p90 of 341–389 and an all-pass threshold of 413–419 — so the deployed 250
rejects roughly a quarter of usable output for length rather than for being wrong.

This ticket produces the curve and amends D3. It does **not** change the producer — that is FRE-993.

**The one thing this plan must not do is pick another number post-hoc.** Every threshold that selects
the answer is precommitted in §5 and §6 *before* any spend, and the run is free to return "the data
does not settle this" — §8 says what that means.

## 2. Acceptance criteria

| # | Criterion | How it is proven |
|---|---|---|
| AC-1 | The amendment states a **number or a rule**, with the curve data behind it and the sample size it was measured over | ADR-0124 Amendment C + `docs/research/2026-07-27-fre994-digest-compression-curve.md`: per-arm table, N, and the interval on every reported quantity |
| AC-2 | The chosen bound is **achievable by the model in practice**, without truncation | At the selected arm: `rendered_tokens ≤ bound`, `finish_reason == "stop"`, contract-valid, for ≥90% of content-bearing digests |
| AC-3 | The **call output ceiling implied by that bound** is stated | From §4.4's token decomposition — content vs structural tokens at p95 — not from a ratio of successes |
| AC-4 | Corpus sourced from the **durable capture store**, not the ephemeral disk directory | Harness reads via FRE-992's `load_session_captures(es_client=…)`; the manifest records per-session `source` and the disk/ES split |
| AC-5 | Producer stays **disabled**; curve runs **out of band**, never on the live sweep path | Harness never calls `generate_session_digest`, never touches the scheduler; `AGENT_SESSION_SUMMARY_ENABLED` unchanged; asserted by a unit test |
| AC-6 | **Expected cost stated before running**; **actual reported against it** | `--dry-run` prints the projection from real prompts, corrected to billed tokens (free); the run report prints actual and the delta |
| AC-7 | The **relative bound** (D4) is considered as a candidate answer, not only an absolute one | §4.5's per-cell fit of absolute against size-relative shape, reported **with its interval**. Precommitted: if the two shapes are not separable at this N, the amendment says so rather than choosing |

## 3. Corpus — free, measured 2026-07-26 against the cleaned index

Frame: `agent-captains-captures-*` excluding `…-subagents-*` on `cloud-sim-elasticsearch` (:9200).

| | Count |
|---|---:|
| Sessions in the index | 893 |
| Eligible: UUID-keyed, ≥ `MIN_TURNS_FOR_DIGEST` (2) | **315** |
| Sampled sessions readable in full | 20 / 20 |

Master's same-day cleanup removed 927 test captures, restored 242 real April captures with correct
attribution, and backfilled `user_id`. The consequence for this study is direct and worth stating:
the previous draw lost 14 of 72 sessions to unparsable captures that still reported a *complete*
read, and the current draw loses none. The `user_id` filter stays in the frame as a standing guard
because that failure is silent.

Sample: stratified by conversation-size quartile, equal draw, round-robin, deterministic seed.

**The seed does not by itself reproduce the draw, and the manifest records the ids because of it.**
Quartiles are assigned over the *live* frame, which grows as sessions are captured — the eligible
count moved 314 → 315 between two dry runs half an hour apart — so the same seed against a later
frame draws a different sample. The manifest records the drawn session ids and the calibration
subset's ids; those, not the seed, are the reproducible artifact. The seed only makes the choice
arbitrary rather than chosen.

## 4. Measurement

### 4.0 The knob is the prompt's stated token policy, because it is the only lever that works

Rev 2 parameterised the arms **structurally** — items per slot × tokens per item — reasoning that the
digest's destination is a JSON-string property on the `Session` node, written and read whole, so the
graph constrains *shape* rather than size. That reasoning about the destination is still correct and
still matters to the amendment. Its conclusion about the instrument does not survive FRE-996:

> Per-slot item ceilings moved the rendered median from 221 to 224 tokens. Item *text* is unbounded,
> so the model satisfies "at most five items" by writing five longer ones, and the schema dialect has
> no `maxLength` (FRE-995 §8.2). **Structure cannot express length.**

What is left is the prompt's own LENGTH rule — and whether *that* lever works is **unknown, not
established**. FRE-996 held the prompt constant across all three of its arms, so a generator told
"180 target / 250 maximum" landing at a rendered median of 208–224 shows a coincidence of numbers,
not that the paragraph caused them; the model and the schema may produce that distribution with the
paragraph deleted. The one controlled contrast FRE-996 ran was on `maxItems`, and it supports exactly
one claim: *those* item ceilings did not shorten output.

So the curve moves the stated policy, and **the first thing it measures is whether the distribution
follows at all.** Prompt inertness is a live hypothesis and a reportable outcome: `t120`, `t180` and
`t250` vary the numbers while `unbounded` deletes the paragraph, so if the four length distributions
are indistinguishable the delivery endpoint says so — and the amendment's conclusion becomes that no
prompt-side bound is an instrument at all, which would be the most useful single thing this ticket
could hand FRE-993.

Three consequences:

1. **Arms are `(target, max)` policy pairs**, moved together at ADR-0124 D3's own 0.72 ratio. An arm
   is therefore a policy pair, not an isolated hard maximum — named as a confound, not averaged over.
2. **Item ceilings survive as one separate arm**, answering FRE-996 §5.1's separate question: the
   bounded variant produced content on 27 of 30 sessions against 25 and 24. Length and completion are
   not the same property and are not measured as one.
3. **The contract is production's**, imported from `session_digest_wire` — `digest_tool()` and
   `digest_tool_choice()`, exactly what the producer sends since FRE-996. A harness-local schema would
   calibrate a contract that is not deployed.

### 4.1 Two endpoints, and only one of them costs money

| Endpoint | Question | Cost |
|---|---|---|
| **Delivery** — rendered tokens, content-bearing rate, empty rate, truncation, contract validity, token decomposition | Where can the bound sit? Does the prompt control length? | **Free** — read off the generation output |
| **Loss** — L(T), the fraction of sessions whose digest omits ≥1 consequential conclusion | Where *should* the bound sit? | One extraction call per session + one judging call per judged arm |

This split is deliberate. The delivery half is guaranteed to land regardless of what the judge does,
and it is the half master's dispatch calls load-bearing for the summarizer redesign. The loss half is
the ambitious one and the one the validity gates in §5.3 can legitimately kill.

### 4.2 Primary endpoint: loss incidence, paired, marginal

ADR-0124 D3's definition of wrong is **per-digest**: a digest is wrong when it "omits a consequential
conclusion needed to avoid repeating settled work". A mean-coverage score answers a different
question and can hide a handful of sessions losing everything behind a healthy average.

> **L(T) = the fraction of sessions whose digest at bound T omits ≥1 consequential conclusion.**

**Marginal, not absolute.** The generator omits things even unbounded, so an absolute L(T) confuses
"the bound is too tight" with "the generator is imperfect". The quantity that answers the ticket's
question is the excess over the unbounded arm:

> **ΔL(T) = L(T) − L(unbounded)**

**Paired, not unpaired.** Every arm runs on the same sessions, so ΔL is a within-session paired
difference and its interval comes from a paired bootstrap over sessions — equivalently, from the
discordant pairs. Pairing is the *correct* estimator here; how much precision it buys is **not
claimed**, because that depends entirely on the discordance rate, which is unknown until the run and
which no pilot has measured. An earlier revision asserted the paired design needed "roughly half" the
N of an unpaired one. That figure was not computed from anything and is withdrawn. Uncontrollable
temperature (§4.6) further weakens the pairing, since two arms on one session are not deterministic
counterfactuals.

**What N=20 actually resolves.** ΔL moves in steps of 1/20 = 0.05, so the 0.10 decision threshold
sits two discordant sessions away from zero and the 0.20 interval bound four. A handful of judge
verdicts can move the selected arm. The loss endpoint at this budget is therefore **exploratory and
is labelled as such in the write-up** — it can rule an arm out loudly, it can return inconclusive,
and it is not on its own sufficient to move a production constant. The delivery endpoint is the
decision-grade half.

`partial` coverage scores **0 in the primary endpoint** (an item half-carried is an item a future
reader cannot rely on) and **0.5 in a reported sensitivity analysis**. Both precommitted; if they
disagree on which arm is selected, the amendment reports the disagreement rather than picking the
flattering one.

### 4.3 Ground truth: a human-anchored reference set

The metric is only as good as the reference. Making it two `gpt-5.4-mini` calls — one to extract, one
to judge — shares one model's blind spots between the ground truth and its scorer, and nothing in the
design would notice.

- **Calibration subset: the first 8 sessions of the stratified draw**, fixed by the seed rather than
  chosen, and named in the manifest before any reference is written. **I build their reference sets by
  hand**, reading the transcripts, before seeing any digest.
- **LLM extractor** (`gpt-5.4-mini`, independent prompt, cross-family from the generator) runs on
  every sampled session, including the calibration subset.
- **Precommitted validity gate:** on the calibration subset the extractor must recall **≥80% of the
  hand-authored reference items** and introduce **≤20% items I judge not consequential**.
- **Judge agreement:** on the calibration subset, judge verdicts are compared to mine on the same
  items. Precommitted: **item-level agreement ≥80% and Cohen's κ ≥ 0.6**. Below that the judge is not
  measuring what it claims and the loss endpoint is discarded (memory: a bad eval is discarded, not
  reframed). The delivery endpoint is unaffected — it involves no judge.
- **On gate failure the loss endpoint becomes descriptive and cannot select the bound.** An earlier
  revision allowed falling back to "the hand-referenced subset only". That subset is 8 sessions, which
  is far below what §6's rule needs, and running the same rule on it would launder a number nobody
  should trust into a production constant. Precommitted: a failed extractor gate means loss is
  reported for the calibration subset **as illustration only**, and the amendment's bound comes from
  the delivery endpoint or from nowhere.

**What these gates do and do not establish, stated plainly.** I write the reference items, I decide
which extractor additions are not consequential, and I supply the verdicts the judge is compared
against. Every gate above can therefore pass because two models successfully reproduced *one person's
reading* — including if that reading systematically overlooks the kind of conclusion compression
destroys. Writing the references before seeing any digest prevents arm-by-arm tailoring; it does not
make the ground truth independent of the person who also designed the arms and knows the thresholds.
Three things bound that, none of which removes it:

- The hand-authored reference sets and the transcript ids are **committed to the repo**, so the
  owner or a later reader can audit the judgement rather than take it on trust.
- Reference items are written against **ADR-0124 D3's own definition** of a consequential conclusion,
  quoted at the top of the reference file, rather than against an ad-hoc sense of importance.
- The write-up states the limitation in these words, in its Limitations section, rather than in a
  footnote.

**The two anchors are plumbing checks, not validity evidence.** An empty digest must score retention
**≤ 0.05** and the reference scored against itself **≥ 0.95**; either failing means the scorer is
broken and the loss endpoint is discarded. But a scorer can pass both while still misjudging
compressed paraphrases — which are exactly the cases that select the arm — so passing them is
necessary and nowhere near sufficient. Rev 3 implied otherwise.

### 4.4 Separating instruction-failure from envelope overhead

`output_tokens / rendered_tokens` conflates two different things and cannot answer why a call bills
far more than the bound it was given. The decomposition is computable exactly, offline, from the
stored payload:

| Quantity | Definition |
|---|---|
| `content_tokens` | tokens of the payload's *value strings* — `text`, `span`, `evidence_span`, `label` |
| `structural_tokens` | `output_tokens − content_tokens` — braces, keys, `basis`/`tier` tags, locators |
| `rendered_tokens` | `digest_token_count` — the consumer-facing projection, and what D3 bounds |

This separates the hypotheses cleanly: **content ≈ bound while output ≫ bound ⟹ envelope overhead**
(the fix is a larger call ceiling); **content ≫ bound ⟹ instruction-following failure** (the fix is a
different bound or a different prompt). AC-3's recommended ceiling derives from `structural_tokens`
p95 plus the selected bound, not from a ratio of successes.

FRE-996's records already put a number on the envelope: **2.4 billed output tokens per rendered
token at the median, 2.7 at the mean.** So a 250-token digest is a ~600-token call, and the two
numbers being conflated is exactly the eight-fold gap the ticket names.

**Truncated and unparsable rows are reported, never dropped.** They have no parseable digest, so they
enter a separate `unusable_rate(T)` column. Silently excluding them biases every ratio toward
successes — which is how the live producer's failure stayed invisible for fourteen days.

### 4.5 Absolute vs relative bound (AC-7)

Fit `retention ~ f(bound)` against `retention ~ f(bound / conversation_tokens)` across all
(session × arm) cells and compare fits (AIC + cross-validated error). This uses N×arms observations,
not N, and the quartile stratification is what gives the size axis enough spread to separate the two
shapes at all.

**Precommitted:** if the two shapes are not separable — intervals overlap, or cross-validated error
differs by less than its own uncertainty — the amendment states that **the data does not settle the
shape** and names the sample size that would. It does not pick the more interesting answer.

### 4.6 Free baseline and a stated confound

- **Trivial baseline (no LLM):** take the `unbounded` digest and drop whole items until it fits T,
  then score it identically. If an instructed digest at T does not beat mechanical truncation at T,
  the budget instruction buys nothing and the amendment must say so.
- **Stated confound:** temperature cannot be pinned — `claude-sonnet-5` rejects any value but 1
  (litellm `UnsupportedParamsError`, FRE-996 §8.1). The arms therefore carry sampling variance no
  harness discipline removes, and per-arm differences are not deterministic counterfactuals.

## 5. Arms and phases

### 5.1 Arms

| Arm | prompt target / max | contract | role |
|---|---|---|---|
| `t120` | 86 / 120 | plain | aggressive |
| `t180` | 129 / 180 | plain | |
| `t250` | 180 / 250 | plain | **deployed today** — anchors the curve to the incumbent |
| `unbounded` | LENGTH rule removed | plain | the reference ΔL is differenced against |
| `t250_bounded` | 180 / 250 | `bounded=True` | completion contrast (FRE-996 §5.1) |

Five arms, and the registry in `arms.py` contains **exactly these five** — rev 3's plan said five
while its code still carried a sixth (`t400`) that the driver's default would have run, pricing one
experiment and executing another. The registry is the precommitment now, and the driver's defaults
reproduce the design this section prices.

`t400` was cut deliberately: the `unbounded` arm already supplies the upper anchor for the
controllability question, and the decision the ticket exists to make is about how far the bound can
come *down*. If the answer turns out to be "higher than 250", the evidence for it is the unbounded
arm's own achieved distribution, which the run measures directly.

Judged for loss: `t120`, `t180`, `t250`, `unbounded`. `t250_bounded` is generated and measured for
delivery and completion but not judged — it shares `t250`'s length policy, so judging it would buy a
second estimate of the same point on the curve.

Generation uses the **production model and production prompt** (`session_summary` → `claude-sonnet-5`;
`build_prompt` and `system_prompt` imported, never copied), with `finish_reason` recorded so
truncation stays distinguishable from a valid stop.

Call ceilings differ by arm, and the difference is a measurement choice rather than a budget one.
Bounded arms use production's **2,048** — roughly twice FRE-996's largest observed contract output
(1,050), so it binds nothing at any policy under test. The `unbounded` arm uses **4,096**, because it
is the one arm whose natural length is the open question and a curve measured against a wall measures
the wall. Its truncation rate is reported: if it truncates at 4,096, that is itself the finding that
an unconstrained contract digest exceeds twice the production ceiling.

### 5.2 Phase A — free (complete)

Sampling, prompt assembly, billed-token counts, cost projection. **Zero model calls.** Done; §7
carries its output.

### 5.3 Phase B — validity gate (cheap, and it can end the loss endpoint)

Calibration subset only: hand-authored references, extractor agreement, judge agreement, both
anchors. **All four §4.3 thresholds must pass to proceed to judging.** The outcome "the metric does
not work" is a legitimate result of this ticket and is reported as one — and the delivery endpoint
still lands.

### 5.4 Phase C — the run

All five arms over the sample, arms interleaved per session so provider drift or a mid-run outage
lands on every arm equally rather than contaminating one. Records written incrementally, so a budget
denial stops the run without losing what it already measured.

Selection bias is corrected by bootstrap rather than by a held-out draw. Rev 2 carried a held-out
sample; at the N this budget affords it would spend a third of the generations confirming a rule
against a subsample whose own interval is wider than the effect. The procedure that replaces it is
specified numerically in §6.3 — "a bootstrap optimism correction" on its own is not an executable
precommitment, which is what rev 3 offered.

## 6. Precommitted decision rule (fixed before any spend)

> **The recommended bound is the smallest arm T that satisfies both:**
> 1. **Loss:** ΔL(T) ≤ 0.10, with the upper end of its 90% paired-bootstrap interval ≤ 0.20.
> 2. **Reachability:** ≥90% of that arm's content-bearing digests render within T without truncation.

Condition 2 is FRE-996's half of the question, and it is a gate rather than a tiebreak: a bound the
generator cannot hit is not a bound, it is a rejection rate. Had it been applied to the incumbent,
250 would have failed it — the contract's rendered p90 is 341–389.

If no arm satisfies both, the finding is that **no tested bound is safe**, and the amendment says the
bound must come from the Phase-2 consumer instead, quoting the unbounded arm's achieved distribution
as the floor any such bound has to clear. That is a legitimate outcome, not a failure to argue around.

**Not scored against 250.** Master's warning is precommitted here: 250 is the number this ticket
exists to determine, so no arm is compared against it as a criterion. It appears only as the
incumbent arm, described alongside the others.

### 6.1 Ordering is unambiguous; selection is the risk

Both components of an arm move at the fixed 0.72 ratio, so ordering by stated maximum also orders the
target and "smallest" is well-defined across `t120 < t180 < t250`. The hazard is not semantic, it is
statistical: the rule takes the **minimum over arms of a threshold crossing**, on a small sample, so
noise in a single arm can promote it. With ΔL moving in steps of 0.05, one or two judge verdicts can
make `t120` pass while `t180` fails — a non-monotone ordering that a compression curve should not
produce, and that the rule as stated would happily reward with the smallest bound.

### 6.2 Monotonicity is a precondition, not an observation

> **Precommitted:** if the observed loss ordering is non-monotone — a tighter arm showing *lower* ΔL
> than a looser one by more than one session's worth (0.05) — the curve is **not behaving as a
> compression curve** and the rule returns **inconclusive** for the affected region. It does not
> select the tighter arm.

### 6.3 Selection stability, specified

> Resample **sessions** (not cells) with replacement, B = 10,000. On each replicate, recompute L(T)
> for every judged arm from that replicate's sessions and **re-apply §6's full rule, including §6.2**.
> Record which arm it selects, or that it returned inconclusive.
>
> **The selected arm must be selected in ≥60% of replicates.** Below that, the run reports the
> selection frequency table and returns **inconclusive** rather than a bound. The optimism of the
> point estimate is reported as the gap between the nominal ΔL of the selected arm and the mean ΔL of
> whichever arm each replicate selected.

Resampling sessions rather than cells is what preserves the pairing: a session enters or leaves a
replicate with all four of its judged arms attached.

## 7. Sample size and cost

### 7.1 The caps, and when the run can happen

The `study` lane's standing caps are **$5.00 daily / $7.00 weekly** (FRE-839). No cap is to be
raised — master's dispatch is explicit, and FRE-996 set the precedent by fitting inside it. The
`_total` weekly cap of $30 stands at $25.26 for the week ending 2026-07-26, so **the run happens on or
after Monday 2026-07-27**, when the daily, weekly and total windows have all reset.

### 7.2 Three projections, because one number would be a lie

Rev 3 multiplied a *median* input ratio by a *mean* envelope by a *p90* overshoot and called the
product an upper bound. It is not one — the product of three central-or-tail marginals bounds
nothing — and the conclusion it was used to support ("no budget denial is possible mid-run") was
false. Every projection is now reported on three explicitly-labelled bases:

| Basis | Input ratio | Output | Answers |
|---|---|---|---|
| `expected` | p50 1.535 | p50 envelope × p50 overshoot | what the run most likely costs |
| `planning` | max 1.697 | p90 envelope × p90 overshoot | what it costs if the tail arrives |
| `ceiling` | max 1.697 | **every call at its own output ceiling** | the only true upper bound |

At the precommitted **N=20 × 5 arms = 100 generation calls**, priced from real assembled prompts:

| | expected | planning | **ceiling** |
|---|---:|---:|---:|
| Generation | $3.70 | $4.60 | $5.80 |
| Extraction + judging | $0.38 | $0.38 | $0.38 |
| **Total** | **$4.08** | **$4.98** | **$6.18** |

**The ceiling is the number that faces the cap**: $6.18 against $7.00 weekly, and the `planning`
figure of $4.98 against $5.00 daily. FRE-996 landed at 52% of its worst case, so ~$2.5 actual is the
realistic expectation — but the design is sized so that even the physically-maximal run stays inside
the standing caps, which is what makes "no cap is raised" a property of the plan rather than a hope.

Three residual honesty notes:

- **A denial is still possible and is handled, not denied.** Reservations are taken against each
  call's `max_tokens`, so transient reservation pressure can exceed realised spend. The `study` lane
  is `on_denial: raise`, records are written incrementally, and a denial is therefore a loud,
  resumable stop rather than a silently thinned sample.
- **The unbounded arm is priced at its ceiling on every basis**, including `expected`. There is no
  contract-mode measurement of an unconstrained digest — FRE-996's unconstrained arm was free-text —
  so any expected value would be invention. It is the single largest line in the projection ($1.94 of
  $4.08) and that is the honest cost of not knowing.
- **The bounded arm's tool-definition cost is an estimate, not a measurement.** The plain definition's
  1,663 billed tokens is exact (FRE-996 arm B minus arm A, zero variance across 30 sessions). The
  bounded definition was never sent through a priced call, so its 1,702 is that measurement scaled by
  the two definitions' cl100k ratio, and is labelled as such in the code.

**The correction is itself a finding.** Rev 2 priced this run at $12.64 on the cl100k estimator
alone; the same design bills nearer $17 once the 1.535× estimator gap and the 1,663-token tool
definition are counted. That is the same class of error as the defect this ticket exists to fix — a
number that looks measured because it came out of code — and it is worth FRE-993 knowing that the
cost gate reserves against an estimator that runs a third light.

### 7.3 Sample size is fixed at 20, with no extension rule

Rev 3 carried a rule extending N from 24 to 36 if measured spend left room in the weekly cap. It is
withdrawn: **spend is driven by realised output length, and realised output length *is* the
reachability endpoint.** A run whose outputs are long and poorly controlled would stop at the small
N; a run whose outputs are short and well controlled would buy more observations. Precommitting the
rule prevents discretion, but it does not make fixed-N bootstrap intervals valid under an
outcome-dependent sample size, and no adjustment for it was specified. N is fixed at 20 before the
run, in code (`PRECOMMITTED_N`), and is not revised on seeing any result.

### 7.4 What N=20 can and cannot support

Stated plainly, because it is the weakest part of this plan and the owner should see it before
authorising.

- The **delivery endpoint is decision-grade**: 20 sessions × 5 arms is 100 length observations, and
  the quantities of interest — p50, p90, the all-pass threshold, content-bearing rate, empty rate,
  truncation rate, the content/structural token split — are the same statistics FRE-996 reported
  usefully at N=30. This half lands regardless of what the judge does.
- The **loss endpoint is exploratory** (§4.2): ΔL moves in steps of 0.05, so the rule can rule an arm
  out, and can return inconclusive, but should not on its own move a production constant.

If the owner would rather buy loss precision than breadth, the lever is dropping `t120` and
`t250_bounded` and spending the same ceiling on N≈33 over three arms. **My recommendation is the
five-arm design**: the completion arm answers a question master asked for by name, and three policy
points plus an unbounded reference is the minimum that can show a shape at all — and the shape, not
the threshold crossing, is what this budget can actually establish.

## 8. Halt conditions

- Any §4.3 gate fails (extractor recall, judge agreement, either anchor) → **the loss endpoint is
  discarded** and reported as discarded; the delivery endpoint still ships. Do not weaken a threshold
  to save a run.
- No arm satisfies §6 → report "no tested bound is safe". Not a licence to pick another constant.
- Absolute and relative shapes not separable → say so, and name the N that would settle it.
- Projection exceeds the `study` lane → stop and ask. **Never raise a cap to fit a run.**
- Any need to set `AGENT_SESSION_SUMMARY_ENABLED=true` → stop. The producer stays disabled.
- Consecutive provider errors → abort. A run of uniform failures reads like a result rather than a
  broken harness, which is how FRE-996's first attempt produced 90 empty rows.

## 9. What changed from rev 2, and why

| # | Rev 2 | Rev 3 | Cause |
|---|---|---|---|
| 1 | Arms are structural (items/slot × tokens/item) | Arms are prompt token-policy pairs; one structural arm survives for the completion question | FRE-996 §5 measured item ceilings moving the rendered median by 3 tokens |
| 2 | Harness declares its own response schema and uses `response_format` | Harness sends production's `digest_tool()` | FRE-996 shipped the contract, and proved `response_format` overwrites `finish_reason` on this model |
| 3 | `system_prompt()` takes five parameters | Three — the structural pair had no caller left | Change 1 |
| 4 | Fit N=60 + held-out N=12, $11.94 | N=20 fixed, $6.18 ceiling / $4.08 expected | The `study` cap is not to be raised; and the rev-2 price was understated by a third |
| 5 | Cost projected from the cl100k estimator | Projected from billed tokens: ×1.535 estimator correction + 1,663 tool tokens | Measured against FRE-996's 90 committed call records |
| 6 | Held-out confirmation | Bootstrap optimism correction | A holdout at this N costs a third of the run to confirm nothing decisively |
| 7 | One endpoint (loss), delivery incidental | Two endpoints, delivery free and guaranteed | Master's dispatch: "where the bound must sit for the generator to reach it at all" is co-equal |
| 8 | Frame excludes 1,169 unparsable captures; 14 of 72 sessions unreadable | Filter retained as a guard; 0 of 20 unreadable | Master's 2026-07-26 corpus cleanup |

Rev 2's five codex findings all still hold and are all still honoured: the precommitted decision rule
(§6), the hand-authored reference set with numeric discard gates (§4.3), the honestly-powered
absolute-vs-relative comparison (§4.5), the content-vs-structural token split (§4.4), and numeric
anchor stop conditions (§8).

## 10. Codex plan-review of rev 3 — findings and resolutions

Seven findings: two critical, four major, one minor. All seven are accepted; none required abandoning
the approach, and all are fixed before any spend, which is what the review was for.

| # | Severity | Finding | Resolution |
|---|---|---|---|
| 1 | **Critical** | The executable arm set contradicted the design: `ARMS` still carried `t400` and the driver defaulted to N=36 over all registered arms, so the defaults would have run 216 calls against a plan that priced 120 — pricing one experiment and executing another | `t400` removed from the registry; `PRECOMMITTED_N` and the judged-arm set live in code and **are** the defaults; §5.1 states that the registry is the precommitment |
| 2 | **Critical** | "Upper bound" was a median input ratio × a mean envelope × a p90 overshoot — a product of marginals bounds nothing; the unbounded arm was priced at 2,048 while its ceiling was 4,096; scoring counts were "rough", not maxima. So "no budget denial is possible mid-run" was false | Three labelled bases (`expected` / `planning` / `ceiling`); `ceiling` prices every call at its own output ceiling with the worst observed input ratio and is the only figure compared against a cap; the false claim is withdrawn and replaced by "a denial is a loud, resumable stop" (§7.2) |
| 2b | — | The 1,663-token tool increment was measured for the *plain* definition and applied to the bounded one, which carries extra `maxItems` fields | Separate constant, scaled by the two definitions' cl100k ratio and **labelled an estimate, not a measurement**, in both code and §7.2 |
| 3 | Major | The N=24→36 extension was outcome-dependent optional stopping: spend is driven by output length, and output length *is* the reachability endpoint | Extension rule **withdrawn**. N fixed at 20 in code before the run (§7.3) |
| 4 | Major | The validity gates check agreement with the author, who also designs the arms and knows the thresholds; the extractor-failure fallback allowed selecting a production bound from 8 sessions | Fallback narrowed: a failed gate makes the loss endpoint **descriptive only, unable to select the bound**; calibration subset fixed as the first 8 of the draw; hand references committed for audit; anchors reclassified as plumbing checks; the limitation stated in the write-up's own words (§4.3) |
| 5 | Major | "Pairing needs roughly half the N" was asserted, not computed; N=24 cannot separate ΔL=0.10 from 0.20 — the exact boundary the rule uses | Claim withdrawn; ΔL granularity (0.05 at N=20) stated; the loss endpoint is explicitly **exploratory** and insufficient on its own to move a production constant (§4.2, §7.4) |
| 6 | Major | "Smallest passing arm" can select a noise winner; the promised "bootstrap optimism correction" named no statistic, algorithm or pass/fail effect, so it was not an executable precommitment | §6.2 adds monotonicity as a **precondition** (non-monotone ⟹ inconclusive); §6.3 specifies B=10,000 session-level resampling, re-application of the full rule per replicate, and a **≥60% selection-frequency requirement** or the run returns inconclusive |
| 7 | Minor | "The prompt rule appears to be doing real work" is an uncontrolled inference — FRE-996 held the prompt constant, so its numbers cannot support it | §4.0 rewritten: prompt inertness is named as a live hypothesis the design detects, not a prior it assumes. Codex confirmed the experimental contrast itself survives |

One correction to the review, recorded because it matters to the design rather than to the score:
codex noted it could not reproduce the 1.535 and 2.7 constants because FRE-996's raw records are
gitignored. They are not — `telemetry/evaluation/fre996-pilot-final.json` was committed with PR #683,
and both constants are recomputed from it in this branch's own measurement step.

## 11. Deliverables

- `docs/research/2026-07-27-fre994-digest-compression-curve.md` — method, corpus, per-arm delivery
  table, ΔL curve with intervals, token decomposition, absolute-vs-relative comparison, trivial
  baseline, limitations, actual-vs-projected spend.
- **ADR-0124 Amendment C** — replaces D3's provisional figure with the measured bound (or records
  that the data does not settle it), and states **three numbers FRE-993 needs kept distinct**: the
  target the prompt states, the rendered ceiling the producer enforces, and the call output ceiling
  the cost gate reserves against. Today those are 180, 250 and 2,048, set by different people solving
  different problems, and their mismatch is where the truncation happens.
