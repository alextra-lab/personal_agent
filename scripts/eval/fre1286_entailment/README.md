# FRE-1286 — measuring the D3(d) entailment judge

ADR-0138 rejected per-claim inline entailment (Option 5) in part because *"the entailment
judge is itself a model, with its own error rate, sitting on the critical path"*. D3(d)
puts it on that path anyway, for one class: spans with **no entity and no figure**, which
containment cannot decide (*"a page mentioning `mercury` does not thereby support 'this
fish is high in mercury'"*). That makes measuring the judge a precondition rather than a
follow-up — **an unmeasured judge on the critical path is the exact failure Option 5 was
rejected for**.

Everything else is judged by the **sampled offline arm**, which never touches the turn.

## Two things live here

| | What | Runs |
|---|---|---|
| `corpus.yaml` · `corpus.py` · `metrics.py` · `harness.py` | The judge's own error rate against labelled pairs (**AC-3**, **AC-6**) | by hand, needs a model |
| `miss_rate.py` | The production miss rate per answering model, from the offline arm's telemetry (**AC-4**) | by hand, needs Elasticsearch |

The pure core — corpus loading, validation, scoring — is unit-tested in
`tests/personal_agent/grounding/test_entailment_corpus.py`. Only the drivers need a model,
the same split `fre1281_span_extraction` uses.

## The corpus

Five classes. Three of them are the residue ADR-0138 records as accepted risk, or its
consequence:

- **`contradicted`** — a source saying *"not sold in France"* contains every token of
  *"sold in France"*. Containment passes it; only entailment sees it.
- **`quantifier_reversal`** — *"some"* passes for *"all"*, because both are function words
  the normalizer drops.
- **`silent`** — the passage entails neither the claim nor its negation; merely on-topic.
- **`implicitly_refuted`** — the passage entails the negation, same as `contradicted`, but
  by describing a state of the world rather than an explicit negating word or a directly
  conflicting stated value. Kept apart from `contradicted` for telemetry that can tell "the
  judge misses plainly-worded refutation" from "the judge misses refutation it has to
  infer" (**FRE-1301** — see below; this and `silent` were one `not_supported` class until
  the 2026-08-26 run found it conflating the two).
- **`supported`** — the positive class, and **not** a control. A judge answering
  `not_supported` to everything scores 1.0 on `silent` and nothing else negative; the
  false-rejection rate over this class is what fails it. In production it is also the
  expensive error: under D4 a false rejection costs a refusal the user did not deserve.

`quantifier_reversal` deliberately includes cases in the directions that *do* hold (*all* →
*some*, *all* → *most*), so the class cannot be aced by rejecting every sentence with a
quantifier.

**Partitions.** `dev` is for iterating on the prompt. `heldout` is scored **once**, after
the judge is frozen — reporting a figure tuned against the cases it was measured on turns
an eval into a rehearsal. Every case FRE-1286 scored as `heldout` on 2026-08-26 has since
moved to `dev` (a scored partition cannot serve as held-out again — **FRE-1301** AC-2); a
fresh `heldout` set was authored in its place.

## Preregistered bars

Fixed 2026-08-26, before the first scored run; `detection_silent` and
`detection_implicitly_refuted` re-fixed 2026-08-28 before FRE-1301's fresh held-out run,
replacing the single `detection_not_supported` those two classes were split from. They live
as numbers in `metrics.py`; a bar chosen after seeing the score is a description, not a bar.

| Figure | Bar | |
|---|---|---|
| `accuracy` | ≥ 0.85 | floor |
| `detection_contradicted` | ≥ 0.90 | floor — the most concretely named residue |
| `detection_quantifier_reversal` | ≥ 0.80 | floor — the harder linguistic call |
| `detection_silent` | ≥ 0.85 | floor — the old `detection_not_supported` bar, same class, new name |
| `detection_implicitly_refuted` | ≥ 0.90 | floor — the same named residue as `contradicted`, just inferred rather than read off an explicit negation |
| `false_rejection_rate` | ≤ 0.10 | ceiling — this is what a user feels |
| `undecided_rate` | ≤ 0.05 | ceiling — the judge failing to answer |

## Measured, 2026-08-26 — `claude_sonnet`, FRE-1286 (superseded corpus)

This table used the four-class corpus, before FRE-1301's split. It is kept for history;
`detection_not_supported` no longer exists as a metric, and every case scored here has
since moved to `dev` (FRE-1301 AC-2).

| Figure | Bar | dev (n=20) | heldout (n=20) |
|---|---|---|---|
| `accuracy` | ≥ 0.85 | **1.000** | 0.800 ❌ |
| `detection_contradicted` | ≥ 0.90 | **1.000** | **1.000** |
| `detection_quantifier_reversal` | ≥ 0.80 | **1.000** | 0.750 ❌ |
| `detection_not_supported` | ≥ 0.85 | **1.000** | 0.400 ❌ |
| `false_rejection_rate` | ≤ 0.10 | **0.000** | **0.000** |
| `undecided_rate` | ≤ 0.05 | **0.000** | **0.000** |

Held-out scored once, on a frozen judge. Three bars missed, and every one of the four
held-out misses was the same direction — a case labelled `not_supported` that the judge
called `contradicted`, with zero misses the other way. Reading the four: `quant-few` ("the
side effect is common" against "fewer than one in a thousand") the judge was simply right
and the label was wrong (left as filed — the quantifier_reversal class is a separate
review, out of FRE-1301's scope); `ns-future-tense` and `ns-plan-not-fact` were defensible
either way. The class as built conflated two things: *the passage is silent on the claim*
and *the passage implies the claim is false*, the second being contradiction under this
module's own definition. Labels were not corrected after seeing this score — that is the
rehearsal a held-out partition exists to prevent — which is why FRE-1301 exists as a
follow-up rather than an edit to this table.

## Measured, 2026-08-28 — `claude_sonnet`, FRE-1301 (`silent` / `implicitly_refuted` split)

Fresh held-out set (FRE-1301 AC-2), scored once on the frozen judge. Two retries were
needed first: two earlier attempts each hit Postgres deadlocks in the FRE-375
test-substrate cost gate under this harness's concurrent reservations
(`DeadlockDetectedError`, unrelated to the judge or the corpus), which the harness
correctly counts as `undecided` rather than dropping. Those runs are discarded as harness
failures, not as unfavorable scores — the table below is the third attempt, the first to
complete with zero infrastructure failures (`undecided_rate = 0.000`).

| Figure | Bar | dev (n=40) | heldout (n=18) |
|---|---|---|---|
| `accuracy` | ≥ 0.85 | **0.925** | **0.944** |
| `detection_contradicted` | ≥ 0.90 | **1.000** | **1.000** |
| `detection_quantifier_reversal` | ≥ 0.80 | **0.875** | **1.000** |
| `detection_silent` | ≥ 0.85 | 0.778 ❌ | 0.750 ❌ |
| `detection_implicitly_refuted` | ≥ 0.90 | **1.000** | **1.000** |
| `false_rejection_rate` | ≤ 0.10 | **0.000** | **0.000** |
| `undecided_rate` | ≤ 0.05 | **0.000** | **0.000** |

**The split classes work as intended.** `detection_implicitly_refuted` — the class carved
out of the old misses — passes at 1.000 on both partitions: once separated from `silent`
and given its own bar, the judge shows no blindness to inferred refutation at all. The
former `detection_not_supported` failure has narrowed to a single bar, `detection_silent`,
and to a single failure shape on both partitions: a passage that hedges availability in
terms of "under consideration" or "behind a flag" for one channel, which the judge reads as
entailing non-availability elsewhere (`heldout`'s one miss, `sil-different-version`; `dev`
carries the same shape twice, `ns-future-tense` and `quant-few`, plus one converse-causal
case, `ns-reversed-direction`, that a first pass of this ticket had reclassified as
`implicitly_refuted` before a codex review found the entailment insufficiently direct).

**What it costs the contract: still nothing.** `NOT_ENTAILED` and `CONTRADICTED_BY_SOURCE`
take the identical D4 path — both block, both retry, both refuse — so `detection_silent`
missing its bar changes no production behaviour today. What it buys is the thing FRE-1301
set out for: a `detection_implicitly_refuted` figure that is no longer hidden inside a
class it doesn't belong to.

## Running it

```bash
make test-infra-up      # FRE-375: the cost ledger goes to :5433, never production

uv run python -m scripts.eval.fre1286_entailment.harness --partition dev
uv run python -m scripts.eval.fre1286_entailment.harness --partition heldout   # once
uv run python -m scripts.eval.fre1286_entailment.harness                       # everything

make test-infra-down
```

Exit code 0 means every bar was met. Misses are printed per case, by id.

```bash
uv run python -m scripts.eval.fre1286_entailment.miss_rate --days 7
```

Exit code 1 when the window holds no samples — either
`AGENT_GROUNDING_VERIFICATION_MODE` is `off` or
`AGENT_GROUNDING_ENTAILMENT_SAMPLE_RATE` is `0.0`. An empty result is a loud outcome, not
an encouraging blank page.

## Ownership

The offline arm belongs to the **ADR-0087** measurement program. When the miss rate moves,
remediation is a ticket on the Grounding Contract project (FRE-1279's umbrella) — not a
quiet re-tuning of the sample rate or the bars here. The measured rate is also the evidence
ADR-0138 says any future decision to promote entailment inline more generally must rest on.
