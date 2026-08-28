# FRE-1284 — labelled turn-compliance corpus

The held-out set ADR-0138 D5's **AC-1** is scored against: *"the metric agrees with
independent labelling of the same turns, within a stated tolerance, where a turn counts
compliant only if **every** non-exempt span passes."*

## What it measures, and what it deliberately does not

This corpus scores the **metric's aggregation rule**, not the span extractor.

ADR-0138 AC-1 rules the obvious approach circular: *"spans identified by the independent
labelling of AC-7's corpus, not by the system's own extractor. Scoring against the
extractor's own output would make the check circular — an extractor that recognises nothing
would trivially find nothing uncited."* So the spans here come from hand labelling, and
everything downstream of them is the real production path:

```
corpus labels ─┐
               ├─► SpanExtraction ─► verify_turn ─► build_grounding_record ─┐
sources ─► SourceRegistry ─► parse_citations ────────────────────────────────┤
                                                                             ▼
                                    is_unconfounded_observation → first_generation_compliant
                                                                             │
                                                          compared against ──┘ the hand label
```

The span extractor's own quality is FRE-1281's corpus
(`scripts/eval/fre1281_span_extraction/`). Re-scoring it here would measure the same thing
twice and call the agreement evidence.

## Why the tolerance is zero

`verify_turn` is pure and synchronous, and the corpus deliberately excludes the entity-free
bare-predicate class (*"that one is high in mercury"*) whose verdict escalates to a live
D3(d) judge. So the whole derivation is deterministic: there is no sampling noise for a
tolerance to absorb, and any divergence is a defect. FRE-1286's corpus owns the entity-free
class.

Consequence: `tests/personal_agent/grounding/test_fre1284_compliance_corpus.py` runs as an
ordinary unit test. No model, no network, no test stack.

## What the corpus must keep containing

`Corpus.validate_discriminating()` fails the load — not a test, the *load* — unless the set
contains all four of:

| Requirement | Why |
|---|---|
| a compliant turn | else an always-false metric scores perfect agreement |
| a non-compliant turn | else an always-true metric scores perfect agreement |
| a turn outside the denominator | else the exclusion rule is untested |
| a non-compliant turn with **≥2 non-exempt spans** | else an "at least one citation is present" implementation scores perfect agreement — the failure AC-1 names in so many words |

That last row is `c03-one-cited-one-not`, and
`TestAC1KillerCase::test_an_any_citation_metric_would_disagree` seeds the broken rule
explicitly and asserts it disagrees with the real one. A corpus no wrong implementation
could fail is not a corpus.

## Coverage

Every D3 gate fails somewhere, on its own:

| Document | Outcome exercised |
|---|---|
| `c01`, `c02`, `c09`, `c10` | `passed` — the reachable half of the bar |
| `c03`, `c04`, `c12` | `uncited` — D1's default-deny |
| `c05` | `unverifiable_by_containment` — a limit of our normalizer |
| `c06` | `unresolved` — D3(a) |
| `c07` | `source_not_entitled` — D2, the live 2026-08-26 failure |
| `c08` | outside the denominator entirely |
| `c11` | `not_contained` — D3(c) proper |

`c05` and `c11` are kept apart on purpose: ADR-0138 AC-6 requires a normalizer limit and an
honest no-source outcome never to blur, because a wave of the first is a malfunction and a
wave of the second is the contract working. Both are non-compliant, which is all this
metric asks of them.

`c07` and `c10` are the same turn shape with the entitlement flipped, so `c07`'s failure is
provably entitlement rather than something incidental.

## Editing

- **Bump `corpus_version`** on every label change.
- **Never hand-write a citation identifier.** They are content- and turn-bound; the harness
  mints them and rewrites `{{S1}}` placeholders to match. `{{UNKNOWN}}` is reserved for the
  well-formed marker that resolves to nothing.
- **Never hand-write offsets.** The loader derives them from the quoted text and refuses a
  quote that is absent or ambiguous.
- Labels are authored from the raw reply against ADR-0138 D1's exempt-region table. If you
  find yourself running the extractor to decide a label, stop — that is the circularity the
  ADR forbids.

## Running it

```bash
make test-file FILE=tests/personal_agent/grounding/test_fre1284_compliance_corpus.py
```
