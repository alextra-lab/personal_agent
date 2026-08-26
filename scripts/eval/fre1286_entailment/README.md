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

Four classes. Two of them are the residue ADR-0138 records as accepted risk and assigns to
this ticket:

- **`contradicted`** — a source saying *"not sold in France"* contains every token of
  *"sold in France"*. Containment passes it; only entailment sees it.
- **`quantifier_reversal`** — *"some"* passes for *"all"*, because both are function words
  the normalizer drops.
- **`not_supported`** — the source is merely on the topic.
- **`supported`** — the positive class, and **not** a control. A judge answering
  `not_supported` to everything scores 1.0 on the three negative classes; the
  false-rejection rate over this class is what fails it. In production it is also the
  expensive error: under D4 a false rejection costs a refusal the user did not deserve.

`quantifier_reversal` deliberately includes one case in the direction that *does* hold
(*all* → *some*), so the class cannot be aced by rejecting every sentence with a quantifier.

**Partitions.** `dev` is for iterating on the prompt. `heldout` is scored **once**, after
the judge is frozen — reporting a figure tuned against the cases it was measured on turns
an eval into a rehearsal.

## Preregistered bars

Fixed 2026-08-26, before the first scored run. They live as numbers in `metrics.py`; a bar
chosen after seeing the score is a description, not a bar.

| Figure | Bar | |
|---|---|---|
| `accuracy` | ≥ 0.85 | floor |
| `detection_contradicted` | ≥ 0.90 | floor — the most concretely named residue |
| `detection_quantifier_reversal` | ≥ 0.80 | floor — the harder linguistic call |
| `detection_not_supported` | ≥ 0.85 | floor |
| `false_rejection_rate` | ≤ 0.10 | ceiling — this is what a user feels |
| `undecided_rate` | ≤ 0.05 | ceiling — the judge failing to answer |

## Measured, 2026-08-26 — `claude_sonnet`, FRE-1286

| Figure | Bar | dev (n=20) | heldout (n=20) |
|---|---|---|---|
| `accuracy` | ≥ 0.85 | **1.000** | 0.800 ❌ |
| `detection_contradicted` | ≥ 0.90 | **1.000** | **1.000** |
| `detection_quantifier_reversal` | ≥ 0.80 | **1.000** | 0.750 ❌ |
| `detection_not_supported` | ≥ 0.85 | **1.000** | 0.400 ❌ |
| `false_rejection_rate` | ≤ 0.10 | **0.000** | **0.000** |
| `undecided_rate` | ≤ 0.05 | **0.000** | **0.000** |

Held-out scored once, on a frozen judge. **Three bars missed, and the cause is the corpus,
not a fix that was withheld.**

Every one of the four held-out misses is the same direction — a case labelled
`not_supported` that the judge called `contradicted`. There are **zero** misses in the
other direction, and the false-rejection rate is 0.000 on both partitions: the judge never
once called an unsupported claim supported, which is the error that would actually breach
the contract.

Reading the four: `quant-few` ("the side effect is common" against "fewer than one in a
thousand") the judge is simply right and the label is wrong; `ns-future-tense` and
`ns-plan-not-fact` are defensible either way — a passage saying a feature is available
*only behind a flag in nightly* does arguably entail it is not in stable. The class as
built conflates two things: *the passage is silent on the claim* and *the passage implies
the claim is false*. The second is contradiction under this module's own definition.

**The labels were not corrected after scoring.** Three dev cases *were* corrected, before
the held-out run and for a reason independent of their score (`ns-wrong-figure` labelled
`not_supported` while the identically-shaped `con-figure-conflict` was labelled
`contradicted`; two quantifier passages that named their own counterexample, which refutes
rather than fails to support). Editing held-out labels after seeing the score is the
rehearsal the partition exists to prevent, so the numbers above stand as measured.

**What it costs the contract: nothing, today.** `NOT_ENTAILED` and
`CONTRADICTED_BY_SOURCE` take the identical D4 path — both block, both retry, both refuse.
The confusion is visible only in the telemetry split, which is where it should be fixed:
by splitting `not_supported` into *silent* and *implicitly refuted* and scoring against a
**fresh** held-out set.

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
