# How small can a session digest be? The instrument, not the constant, is wrong

**Ticket:** FRE-994 · **Date:** 2026-07-27 · **Author:** build session (Opus)
**Backing:** ADR-0124 D3/D4 · **Depends on:** FRE-992 (`fc4553c9`), FRE-996 (`5b0675a5`)
**Hands off to:** FRE-993 (producer sizing)
**Plan:** `docs/superpowers/plans/2026-07-26-fre-994-digest-compression-curve.md` (rev 4)
**Raw records:** `telemetry/fre994_curve/` (gitignored — this write-up is the committed artifact)

---

## 1. The question, and the answer

ADR-0124 D3 set the digest budget at "~180 tokens target / 250 hard maximum, **to be set
empirically by a compression curve**". The curve was never run. The placeholder shipped as an
*enforced* constraint — `session_summary.py` rejects any digest whose rendered token count exceeds
it — and has been discarding generations ever since.

This ran that curve. The answer is not a smaller number or a larger one:

> **The prompt's stated bound is a real but very weak lever. Told 120 tokens, the generator writes a
> median of 207; told 250, it writes 241; told nothing at all, it writes 287. Doubling the stated
> maximum moves the output by 17%. No bound this study tested is reachable, including the one
> currently deployed and enforced: at 250, 47% of content-bearing digests are rejected for length.**

So D3's provisional constant is not merely mis-set. **The instrument it is written in — a
rendered-token maximum, enforced by rejecting the generation — cannot be met by asking.**

Two secondary results matter as much:

- **The relative bound (D4's principle) describes the data better than any constant.** Rendered
  length correlates with conversation size at r = +0.56 to +0.77 on every arm, and varies less as a
  ratio than as an absolute on four of five. The measured ratio is about **4% of input tokens**.
- **The 2,048 call ceiling is not the problem and does not need changing.** Zero truncations in 100
  calls; the largest billed output was 1,568. The eight-fold gap the ticket describes was a property
  of the *free-text* producer, which FRE-996 already fixed. What remains is over-rejection at 250.

## 2. What was run

| | |
|---|---|
| Corpus | `agent-captains-captures-*` on the durable store; 894 sessions, **315 eligible** (UUID-keyed, ≥2 turns) |
| Sample | **20 sessions**, stratified by conversation-size quartile, seed 994, ids in the run manifest |
| Arms | 5 — `t120`, `t180`, `t250` (deployed today), `t250_bounded` (per-slot `maxItems`), `unbounded` (LENGTH rule deleted) |
| Calls | **100 generations** on `claude-sonnet-5`, production prompt and production contract, arms interleaved per session |
| Ceilings | 2,048 for bounded arms (production's), 4,096 for `unbounded` |
| Cost | **$1.74 actual** against $4.08 expected and a $6.18 physical ceiling. No cap raised. |
| Producer | Disabled throughout. No session marked clean; nothing written to any substrate. |

Every arm is a **policy pair** — target and maximum move together at D3's own 0.72 ratio — so every
finding below is about that pair, not about an isolated hard maximum.

**Temperature could not be pinned**: `claude-sonnet-5` rejects any value but 1. The arms therefore
carry sampling variance no harness discipline removes, which is why the length findings lean on
paired within-session comparisons rather than on arm means alone.

## 3. Delivery — the decision-grade half

| Arm | stated max | min | p50 | p90 | max | mean | outcomes |
|---|---:|---:|---:|---:|---:|---:|---|
| `t120` | 120 | 92 | 207 | 361 | 373 | 212 | 19 ok, 1 contract drift |
| `t180` | 180 | 111 | 263 | 417 | 433 | 256 | 20 ok |
| `t250` | **250 (deployed)** | 139 | 241 | 367 | 465 | 249 | 19 ok, 1 empty |
| `t250_bounded` | 250 + `maxItems` | 129 | 266 | 399 | 436 | 264 | 19 ok, 1 empty |
| `unbounded` | — | 148 | 287 | 527 | 598 | 299 | 19 ok, 1 contract drift |

### 3.1 The lever works, and it is weak

The prompt is not inert — the direction is consistent across sessions, which arm means alone could
not establish under uncontrollable temperature:

| Comparison | median Δ | mean Δ | sessions where the looser arm is longer |
|---|---:|---:|---|
| `t250` − `t120` | +36 | +41 | 16 / 18 |
| `unbounded` − `t120` | +80 | +84 | 17 / 18 |
| `unbounded` − `t250` | +25 | +51 | 16 / 19 |
| `t250` − `t180` | +5 | +2 | 11 / 19 |

But the magnitude is small. Raising the stated maximum from 120 to 250 — **+108% of permission** —
raises mean rendered length from 212 to 249, **+17%**. That is an elasticity of **0.16**. Deleting
the length rule entirely buys only 40% over the tightest instruction. And between 180 and 250 the
lever does essentially nothing: +5 tokens, 11 sessions of 19, indistinguishable from noise.

**The generator writes roughly 200–290 rendered tokens whatever it is told.** The prompt nudges that
distribution; it does not control it.

### 3.2 No tested bound is reachable

§6 of the plan requires ≥90% of an arm's content-bearing digests to render within its own bound
without truncation. Precommitted as a **gate, not a tiebreak**: a bound the generator cannot hit is
not a bound, it is a rejection rate.

| Arm | reachability at its own bound |
|---|---:|
| `t120` | **0.05** |
| `t180` | **0.25** |
| `t250` (deployed) | **0.53** |
| `t250_bounded` | **0.42** |

Every bounded arm fails, and not marginally. **The decision rule returns no bound.** This conclusion
rests on the delivery endpoint alone, so it is unaffected by the loss endpoint's failure (§5).

Read against the deployed configuration: **the enforced 250 rejects 47% of content-bearing digests
for length.** They parse, they carry content, they are not truncated — and they are thrown away.

### 3.3 Where a bound would have to sit

Share of content-bearing digests rendering within a candidate threshold:

| Arm | 120 | 180 | 250 | 300 | 350 | 400 | 450 | 500 | 600 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `t120` | 0.05 | 0.42 | 0.79 | 0.84 | 0.89 | **1.00** | 1.00 | 1.00 | 1.00 |
| `t180` | 0.05 | 0.25 | 0.50 | 0.70 | 0.85 | **0.90** | 1.00 | 1.00 | 1.00 |
| `t250` | 0.00 | 0.21 | 0.53 | 0.74 | 0.89 | **0.95** | 0.95 | 1.00 | 1.00 |
| `t250_bounded` | 0.00 | 0.16 | 0.42 | 0.74 | 0.89 | **0.95** | 1.00 | 1.00 | 1.00 |
| `unbounded` | 0.00 | 0.21 | 0.42 | 0.58 | 0.74 | 0.84 | **0.89** | 0.89 | 1.00 |

**About 400 rendered tokens is where ≥90% pass, under any instruction.** This lands on FRE-996's
independently measured all-pass thresholds of 413 and 419, from a different sample and a different
arm design — two studies agreeing on the number is worth more than either alone.

### 3.4 The counterintuitive, directly usable result

**The tightest instruction gives the best delivery at every threshold.** At a 250-token rejection
bound, telling the model **120** delivers 79%; telling it **250** delivers 53%.

This is not a paradox — it follows from §3.1. The instruction shifts the distribution by a fraction
of what it asks, so aiming below the target lands closer to it than aiming at it. It is immediately
actionable for FRE-993: **if a 250-token enforced bound is kept, the prompt should ask for roughly
120.** That single change lifts delivery from 53% to 79% at no cost.

It is a workaround, not a fix. Even at 120 the arm only reaches 89% at 350 and 100% at 400.

### 3.5 Truncation is gone; completion did not move

- **Truncation: 0 of 100 calls.** FRE-996's result holds across a 2× range of stated bounds and up
  to a 4,096 ceiling. The largest billed output was 1,568 tokens.
- **Empty digests: 2 of 100** (one `t250`, one `t250_bounded`) — the failure mode that returns
  `GENERATED`, marks the session clean and is never retried.
- **Contract drift: 2 of 100 (2%)** — one label of 92 characters against a 90 limit, one
  off-vocabulary `basis` enum. This is the *guided-not-guaranteed* class FRE-996 predicted, because
  Anthropic's strict tool use is unreachable through litellm. **This is its first measured rate.**

**FRE-996's completion signal does not replicate.** That pilot found the bounded arm produced content
on 27 of 30 sessions against 25 and 24, and flagged item ceilings as a possible lever for
*completion* even though they were the wrong lever for *length*. Here `t250_bounded` and `t250` both
produce content on 19 of 20 — identical. The original signal was two or three sessions at N=30, and
it does not survive replication. **Item ceilings are the wrong lever for both properties.**

### 3.6 The implied call output ceiling (AC-3)

Derived from the token decomposition, not from a ratio of successes:

| Arm | billed output p50 | content p50 | structural p50 | structural p95 | output / rendered |
|---|---:|---:|---:|---:|---:|
| `t120` | 521 | 193 | 319 | 591 | 2.47 |
| `t180` | 608 | 244 | 363 | 881 | 2.36 |
| `t250` | 590 | 239 | 363 | 648 | 2.33 |
| `t250_bounded` | 618 | 242 | 373 | 749 | 2.36 |
| `unbounded` | 687 | 271 | 421 | 912 | 2.30 |

Most of a digest call's billed output is **envelope** — braces, keys, `basis` tags, locators — at a
consistent ~2.3–2.5 billed tokens per rendered token, matching FRE-996's 2.4. Pooled structural p95
is **648**.

| Rendered bound | + structural p95 | × 1.2 safety | Implied call ceiling |
|---:|---:|---:|---:|
| 250 | 898 | | **1,078** |
| 350 | 998 | | **1,198** |
| 400 | 1,048 | | **1,258** |
| 450 | 1,098 | | **1,318** |

**Production's 2,048 already covers every one of these.** The ticket's premise — that an eight-fold
gap between 250 and 2,048 produces the truncation — was true of the free-text producer and is no
longer true of the contract one. The gap that remains is not between the bound and the ceiling; it
is between the bound and **what the generator produces**.

## 4. The relative bound (AC-7)

D4 states the principle the design actually intends: annotation may never exceed the token count of
the facts it annotates. The data supports the *shape*, though not decisively at this N.

| Arm | Pearson r (rendered vs input) | rendered/input p50 | range | CV absolute | CV relative |
|---|---:|---:|---|---:|---:|
| `t120` | +0.71 | 0.034 | 0.021–0.055 | 0.35 | **0.25** |
| `t180` | +0.77 | 0.039 | 0.024–0.057 | 0.35 | **0.26** |
| `t250` | +0.56 | 0.041 | 0.023–0.061 | 0.33 | **0.26** |
| `t250_bounded` | +0.68 | 0.042 | 0.024–0.067 | 0.30 | 0.29 |
| `unbounded` | +0.74 | 0.048 | 0.029–0.075 | 0.40 | **0.26** |

Digest length scales with conversation size on every arm, and the ratio is a **tighter** description
of the data than the absolute on four arms of five. The measured ratio is roughly **4% of input
tokens** (0.034 under the tightest instruction, 0.048 with none).

**Precommitted honesty:** the plan requires this to be reported as unsettled unless the two shapes
separate. They do not separate decisively at N=20 — the CV gaps are 0.04–0.14 with no interval
computed on them. So: **the relative shape describes the data better on every arm but one, and is
directionally clear, but this sample does not formally establish it.** Separating them would need
the size axis widened rather than N raised — this sample's inputs span roughly 2.5k to 20k tokens,
and a study that deliberately sampled the extremes would settle it at a similar N.

## 5. The loss endpoint failed its validity gate, and was barred from the answer

The plan's second endpoint — does a tighter bound drop consequential conclusions — required a
reference set of what each session concluded. Phase B tested that instrument against eight
hand-authored reference sets (52 conclusions, written from the transcripts before any digest
existed, committed under `scripts/eval/fre994_digest_compression_curve/references/`).

| Gate | Precommitted | Measured | |
|---|---|---:|---|
| Extractor recall | ≥ 0.80 | **0.788** | **FAIL** |
| Extractor spurious rate | ≤ 0.20 | 0.132 | pass |
| Judge agreement | ≥ 0.80 | 0.811 | pass |
| Judge Cohen's κ | ≥ 0.60 | 0.625 | pass |

**The judge is fit for purpose. The extractor is not** — by 1.2 points, which is one item of 52.
This is precisely the near miss where relaxing is most tempting, and the plan precommitted that it is
not rescued by relaxing the gate. Per §4.3 the loss endpoint therefore becomes **descriptive only and
is barred from selecting the bound**, and the full judging pass was not run. That saved $0.45 and,
more importantly, avoided publishing a number that could not be used.

### 5.1 Why it failed is more useful than that it failed

The misses are not random. The extractor systematically drops:

- **Explicitly-left-open questions** — 4 of the 11 misses. Every session where the assistant offered
  a next step and got no answer, the extractor recorded the offer and not the silence.
- **Concrete implementation recommendations** — 3 misses in one session, where a cache key
  convention, a serialisation choice and a caching pattern were all recommended and none survived.
- **Parts of composite findings** — latency figures dropped from a metrics block, a full service
  health check reduced to its one unhealthy component.

And in one session it added seven items that are retrieved *reference content* — church architecture,
artworks, a spelling correction — rather than conclusions the session reached.

**This matters beyond this study.** ADR-0124 D3 gives `unresolved` its own slot precisely because a
reader who thinks an open question was settled is wrong. An automated reference set that omits open
questions cannot detect a digest dropping them — so the loss endpoint, as instrumented, would have
been **blind to the failure mode the design cares most about**, and biased toward flatness in the
dangerous direction: tighter digests drop open questions, and the reference would not have noticed.

### 5.2 What the gate does not establish

Stated because the plan requires it. These gates check whether two models reproduce **one person's
reading**. The same person wrote the references, designed the arms, and knew the thresholds. If that
reading systematically overlooks the kind of conclusion compression destroys, every gate still
passes. The references are committed so the judgement can be audited rather than trusted; that bounds
the problem, it does not remove it.

The two anchors were not run. They are plumbing checks — a scorer can score an empty digest at zero
and a reference against itself at one while still misjudging compressed paraphrases, which are the
cases that would have selected the arm — and with the endpoint already barred they would have
established nothing.

## 6. Cost

| | Expected | Planning | Ceiling | **Actual** |
|---|---:|---:|---:|---:|
| Phase B | — | — | — | $0.23 |
| Phase C | $4.08 | $4.98 | $6.18 | $1.51 |
| **Total** | **$4.08** | **$4.98** | **$6.18** | **$1.74** |

43% of the expected figure, 28% of the physical ceiling. Billed to the `study` lane throughout
($5 daily / $7 weekly, FRE-839). **No cap was raised**, no denial occurred, and the producer stayed
disabled.

**The projection method is itself a finding.** An earlier revision priced this run at $12.64 using
this repo's cl100k estimator alone. Measured against FRE-996's 90 committed call records, that
estimator **undercounts Anthropic's billed input by 1.535×**, and the contract's tool definition adds
**1,663 tokens to every call**. Neither was counted. The cost gate reserves against the same
estimator, so **every reservation in this system runs about a third light on input** — worth FRE-993
and the cost audit knowing independently of this ticket.

## 7. Limitations

1. **N = 20.** Adequate for the delivery statistics (100 length observations) and the paired
   comparisons, which is why those carry the conclusion. Nothing here rests on a small difference.
2. **Temperature could not be controlled** (`claude-sonnet-5` rejects any value but 1), so arm means
   are not deterministic counterfactuals. The paired within-session comparisons in §3.1 are the
   evidence for the lever's direction; the arm means alone would not support it.
3. **The loss endpoint did not run** (§5). This study says where the bound *can* sit and does not say
   where it *should*. The two halves were meant to answer together, and only one did.
4. **One generator, one prompt.** Everything here describes `claude-sonnet-5` under ADR-0124's
   current system prompt. A different model, or a prompt that argued for brevity differently, could
   have a different elasticity. The elasticity is a property of the pair, not a law.
5. **Arms are policy pairs**, target and maximum moving together at 0.72. No arm isolates the hard
   maximum from the target.
6. **The corpus skews small.** Median 3 turns; the largest session in the frame is 20 turns. The
   production regime that caused the original incident had larger sessions, and §4's ratio would
   predict proportionally longer digests there.

## 8. What follows

For **FRE-993**, in descending order of confidence:

1. **Stop enforcing 250.** It rejects 47% of usable output. If a rendered bound is kept at all, ~400
   is where the generator can meet it.
2. **If 250 must be kept, ask for 120.** Delivery goes 53% → 79% for a one-line prompt change (§3.4).
3. **Leave the 2,048 call ceiling alone.** It is not binding: zero truncations in 100 calls, largest
   output 1,568. A 400-token bound implies ~1,260 (§3.6).
4. **Drop the per-slot `maxItems`** from any sizing role. They do not bound length (FRE-996) and do
   not improve completion (§3.5, non-replication).
5. **Prefer a size-relative bound** — about 4% of input tokens — over a constant, with the caveat in
   §4 that this sample does not formally separate the two shapes.
6. **Watch the empty rate, not the parse rate.** 2 of 100 here; it is the failure that marks a
   session clean and is never retried.

Two things this study did not settle, and one it uncovered:

- Where the bound *should* sit on loss grounds is still open (§5), and needs a reference instrument
  that captures open questions.
- Whether the absolute or relative shape is right needs a sample spanning wider conversation sizes,
  not a larger one (§4).
- The cost gate reserves against an estimator that runs a third light on input (§6).
