# FRE-994 — Run the digest compression curve ADR-0124 promised

**Ticket:** FRE-994 (Approved, `stream:build2`, `Tier-1:Opus`) · **Backing ADR:** ADR-0124 D3/D4
**Blocks:** FRE-993 (producer fix consumes this result)
**Depends on:** FRE-992 (merged `fc4553c9` — union capture reader) · FRE-996 (merged `5b0675a5` — output contract)
**Date:** 2026-07-26 · **Rev 3** — rewritten after FRE-996 merged; §9 records what changed and why

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
| Eligible: UUID-keyed, ≥ `MIN_TURNS_FOR_DIGEST` (2) | **314** |
| Sampled sessions readable in full | 36 / 36 in the N=36 draw |

Master's same-day cleanup removed 927 test captures, restored 242 real April captures with correct
attribution, and backfilled `user_id`. The consequence for this study is direct and worth stating:
the previous draw lost 14 of 72 sessions to unparsable captures that still reported a *complete*
read, and the current draw loses none. The `user_id` filter stays in the frame as a standing guard
because that failure is silent.

Sample: stratified by conversation-size quartile, equal draw, round-robin, deterministic seed
recorded in the manifest. A larger draw is a strict prefix extension of a smaller one (asserted by a
test) — which is what makes §7's extension rule an extension rather than a redraw.

## 4. Measurement

### 4.0 The knob is the prompt's stated token policy, because it is the only lever that works

Rev 2 parameterised the arms **structurally** — items per slot × tokens per item — reasoning that the
digest's destination is a JSON-string property on the `Session` node, written and read whole, so the
graph constrains *shape* rather than size. That reasoning about the destination is still correct and
still matters to the amendment. Its conclusion about the instrument does not survive FRE-996:

> Per-slot item ceilings moved the rendered median from 221 to 224 tokens. Item *text* is unbounded,
> so the model satisfies "at most five items" by writing five longer ones, and the schema dialect has
> no `maxLength` (FRE-995 §8.2). **Structure cannot express length.**

What is left is the prompt's own LENGTH rule, and it appears to be doing real work: told 180 target /
250 maximum, the generator lands at a rendered median of 208–224. The failure is in the tail. So the
curve moves the stated policy, and the first thing it measures is whether the distribution follows.

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
difference and its interval comes from a paired bootstrap over sessions (and, equivalently, from the
discordant pairs). This matters for what N buys: rev 2's power table priced ΔL as a difference of two
independent proportions, which is the wrong estimator for this design and needs roughly twice the N
it actually does.

`partial` coverage scores **0 in the primary endpoint** (an item half-carried is an item a future
reader cannot rely on) and **0.5 in a reported sensitivity analysis**. Both precommitted; if they
disagree on which arm is selected, the amendment reports the disagreement rather than picking the
flattering one.

### 4.3 Ground truth: a human-anchored reference set

The metric is only as good as the reference. Making it two `gpt-5.4-mini` calls — one to extract, one
to judge — shares one model's blind spots between the ground truth and its scorer, and nothing in the
design would notice.

- **Calibration subset (8 sessions): I build the reference set by hand**, reading the transcripts,
  before seeing any digest. This is the only genuinely independent ground truth in the study, and it
  costs labour rather than money.
- **LLM extractor** (`gpt-5.4-mini`, independent prompt, cross-family from the generator) runs on
  every sampled session, including the calibration subset.
- **Precommitted validity gate:** on the calibration subset the extractor must recall **≥80% of the
  hand-authored reference items** and introduce **≤20% items I judge not consequential**. Below either
  threshold the LLM reference is not fit for purpose → the loss endpoint runs on the hand-referenced
  subset only (a smaller, honest N), or is discarded. It is not rescued by relaxing the gate.
- **Judge agreement:** on the calibration subset, judge verdicts are compared to mine on the same
  items. Precommitted: **item-level agreement ≥80% and Cohen's κ ≥ 0.6**. Below that, the judge is not
  measuring what it claims and the loss endpoint is discarded (memory: a bad eval is discarded, not
  reframed). The delivery endpoint is unaffected — it involves no judge.
- **Anchors are stop conditions, with numbers.** An empty digest must score **retention ≤ 0.05**; the
  reference scored against itself must score **≥ 0.95**. Either anchor failing **invalidates the loss
  endpoint** — stated here so it cannot be downgraded to a caveat later.

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

Five arms. `t400` was priced and cut: the `unbounded` arm already supplies the upper anchor for the
controllability question, and the decision the ticket exists to make is about how far the bound can
come *down*. If the answer turns out to be "higher than 250", the evidence for it is the unbounded
arm's own achieved distribution, which the run measures directly.

Judged for loss: `t120`, `t180`, `t250`, `unbounded`. `t250_bounded` is generated and measured for
delivery and completion but not judged — it shares `t250`'s length policy, so judging it would buy a
second estimate of the same point on the curve.

Generation uses the **production model and production prompt** (`session_summary` → `claude-sonnet-5`;
`build_prompt` and `system_prompt` imported, never copied). Call ceiling **4,096** on every arm so it
is never binding — FRE-996's largest contract output was 1,050 — with `finish_reason` recorded so
truncation stays distinguishable from a valid stop.

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
against a subsample whose own interval is wider than the effect. The optimism the holdout guards
against — the rule takes the *minimum arm clearing a threshold*, so it is biased — is estimated
directly by a bootstrap optimism correction, which uses every generation and costs nothing.

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

## 7. Sample size, cost, and the extension rule

### 7.1 What the money buys

The `study` lane's standing caps are **$5.00 daily / $7.00 weekly** (FRE-839). No cap is to be
raised — master's dispatch is explicit, and FRE-996 set the precedent by fitting inside it. The
`_total` weekly cap of $30 is at $25.26 for the week ending 2026-07-26, so **the run happens on or
after Monday 2026-07-27**, when the daily, weekly and total windows have all reset.

Projection at N=24 × 5 arms = 120 generation calls, from real assembled prompts:

| Stage | Billed tokens | Upper-bound cost |
|---|---:|---:|
| Generation (input) | 851k | $2.55 |
| Generation (output) | 115k | $1.73 |
| Extraction + judging | 178k in / 72k out | $0.45 |
| **Total** | | **$4.73** |

Inside the $5 daily cap, so **no budget denial is possible mid-run**. Expected actual is materially
lower: FRE-996 spent $1.21 against a $2.33 worst case (52%), because the projection prices output at
the p90 overshoot rather than the median.

**The projection itself is a corrected number, and the correction is the point.** Rev 2 priced this
run at $12.64 using the cl100k estimator alone. Measured against FRE-996's 90 committed call records,
that estimator undercounts Anthropic's billed input by **1.535×**, and the contract's tool definition
adds **1,663 tokens to every call** — neither was counted. The old figure was not conservative, it
was wrong in the direction that gets a run denied halfway through.

### 7.2 Precommitted extension rule

> **If, after the run completes, measured spend leaves ≥ $1.60 of the weekly `study` cap, the sample
> extends from N=24 to N=36 at the same seed** — a strict prefix extension, so the first 24 sessions
> are unchanged — and every reported quantity is recomputed over N=36.

Precommitted rather than decided on seeing results, and stated in the write-up either way. This
converts a deliberately pessimistic projection from wasted statistical power into optional extra N.

### 7.3 What N=24 can and cannot support

Stated plainly, because it is the weakest part of this plan and the owner should see it before
authorising.

- The **delivery endpoint** is well served: 24 sessions × 5 arms is 120 length observations, and the
  quantities of interest (p50, p90, all-pass threshold, content-bearing rate) are the same statistics
  FRE-996 reported usefully at N=30.
- The **loss endpoint** is tighter. As a paired difference its precision depends on discordant pairs
  rather than on N alone, so it is better than rev 2's unpaired table suggested — but a ΔL of exactly
  0.10 will not be separable from 0.20 at this N. The rule can therefore return **inconclusive**, and
  §8 requires it to say so rather than to round toward a decision.

If the owner would rather buy loss precision than breadth, the lever is dropping `t120` and
`t250_bounded` and spending the same money on N≈40 over three arms. **My recommendation is the
five-arm design**: the completion arm answers a question master asked for by name, and a curve with
three points plus an unbounded reference is the minimum that can show a shape at all.

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
| 4 | Fit N=60 + held-out N=12, $11.94 | N=24 with a precommitted extension to 36, $4.73 | The `study` cap is not to be raised; and the rev-2 price was understated by a third |
| 5 | Cost projected from the cl100k estimator | Projected from billed tokens: ×1.535 estimator correction + 1,663 tool tokens | Measured against FRE-996's 90 committed call records |
| 6 | Held-out confirmation | Bootstrap optimism correction | A holdout at this N costs a third of the run to confirm nothing decisively |
| 7 | One endpoint (loss), delivery incidental | Two endpoints, delivery free and guaranteed | Master's dispatch: "where the bound must sit for the generator to reach it at all" is co-equal |
| 8 | Frame excludes 1,169 unparsable captures; 14 of 72 sessions unreadable | Filter retained as a guard; 0 of 36 unreadable | Master's 2026-07-26 corpus cleanup |

Rev 2's five codex findings all still hold and are all still honoured: the precommitted decision rule
(§6), the hand-authored reference set with numeric discard gates (§4.3), the honestly-powered
absolute-vs-relative comparison (§4.5), the content-vs-structural token split (§4.4), and numeric
anchor stop conditions (§8).

## 10. Deliverables

- `docs/research/2026-07-27-fre994-digest-compression-curve.md` — method, corpus, per-arm delivery
  table, ΔL curve with intervals, token decomposition, absolute-vs-relative comparison, trivial
  baseline, limitations, actual-vs-projected spend.
- **ADR-0124 Amendment C** — replaces D3's provisional figure with the measured bound (or records
  that the data does not settle it), and states **three numbers FRE-993 needs kept distinct**: the
  target the prompt states, the rendered ceiling the producer enforces, and the call output ceiling
  the cost gate reserves against. Today those are 180, 250 and 2,048, set by different people solving
  different problems, and their mismatch is where the truncation happens.
