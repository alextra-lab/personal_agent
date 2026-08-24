# FRE-1281 — span-extraction corpus and scoring (ADR-0138 D1, AC-7)

ADR-0138 says the grounding contract "can never be stronger than its recall." This
directory is the instrument that measures that recall, and the extractor it measures lives
in `src/personal_agent/grounding/`.

## Layout

| File | Role |
|------|------|
| `corpus.yaml` | The labelled corpus — 52 documents, 153 gold claim spans, 13 classes |
| `corpus.py` | Schema, loader, and the discipline guards that make `bars.py`'s arithmetic hold |
| `ADJUDICATION.md` | Versioned labelling guidance (D1 requires it to live with the corpus, not in the ADR) |
| `bars.py` | The preregistered bars, each naming the broken baselines it must reject |
| `metrics.py` | Pure scoring — matching, per-class recall, precision, sweep rate, boundary F1 |
| `baselines.py` | Five deliberately broken extractors plus the oracle |
| `report.py` | Aggregation, bar evaluation, and the held-out reporting contract |
| `iaa.py` | Independent second labeller and Cohen's κ |
| `corpus_agreement.json` | The recorded agreement (committed) |
| `harness.py` | I/O driver |

## Preregistration — read the git log, not this file

The bars were fixed **before the extractor existed**. `git log --oneline
scripts/eval/fre1281_span_extraction/bars.py src/personal_agent/grounding/` shows the
corpus-and-bars commit landing first. That ordering is the AC-5 evidence; a bar set after
inspecting the outcome measures nothing.

Preregistration alone is not enough, so `tests/evaluation/test_fre1281_bar_floor.py`
scores five broken baselines against the real corpus and asserts each fails every bar
naming it — "a bar that a known-broken implementation would pass is not a bar." The oracle
is the positive control: without it, a bar set could be strict by being unsatisfiable.

## Run protocol

```bash
make test-infra-up        # FRE-375: cost substrate on :5433, never production

# dev partition — per-document diffs, for iterating
uv run python -m scripts.eval.fre1281_span_extraction.harness \
    --run-id dev-$(date +%Y%m%d) --partition dev

# the adjudicated figure — whole corpus, 3 samples
uv run python -m scripts.eval.fre1281_span_extraction.harness \
    --run-id full-$(date +%Y%m%d) --samples 3

# compare deployments without editing the role matrix
uv run python -m scripts.eval.fre1281_span_extraction.harness \
    --run-id compare --samples 3 --model-key gpt-5.4-mini

uv run python -m scripts.eval.fre1281_span_extraction.iaa --write

make test-infra-down
```

Reports land in `telemetry/evaluation/fre1281-span-extraction/` (gitignored — raw runs are
never committed). The harness exits nonzero when a bar is unmet, so a failing run cannot
be mistaken for a passing one.

```bash
make test-k K=fre1281     # the pure core: corpus, metrics, bar floor, leakage (no LLM)
```

## Held-out discipline, and its honest limit

Every document carries `partition: dev | heldout`, fixed in the preregistration commit.
`report.py` **cannot** emit per-document diffs or document text for the held-out
partition — a property of the reporter, not a promise by the author.
`test_fre1281_no_corpus_leakage.py` fails if the extractor's source quotes the corpus,
except where the phrase is in ADR-0138 itself (the spec, which both are entitled to quote).

**The residual, stated rather than papered over:** the corpus author and the extractor
author are the same session. A genuinely author-blind corpus needs a second party. The two
mechanisms above prevent the *harm* the held-out rule targets — memorised probes, a
denylist tuned to them — but they are not the same thing as blindness.

The bars are adjudicated on the **full corpus**. A 40% held-out slice would put each class
at ~4 gold spans, where a 0.85 bar is decided by a single span; scoring the full corpus
keeps the ≥10-per-class floor that makes the bars mean anything, and ~40% of those spans
still come from documents whose individual failures were never inspected.

## Measured results — 2026-08-24

Whole corpus, 3 samples (156 document-scorings), both models on the **shipping** code,
extractor frozen before the run.

| | `gpt-5.4-mini` | `claude_sonnet` |
|---|---|---|
| **bars met** | 13 / 16 | **15 / 16** |
| overall recall | 0.906 | 0.953 |
| overall precision | 0.831 | 0.862 |
| decomposition boundary F1 | 0.765 | 0.804 |
| factual-entity recall | **0.744** (bar 0.85) | 0.923 |
| prose-in-fence recall | **0.833** (bar 0.85) | 0.967 |
| connective-evaluative sweep rate | **0.300** (bar 0.15) | **0.267** (bar 0.15) |

`claude_sonnet` meets **all eight per-class recall bars**, which is what ADR-0138 AC-7
actually demands ("recall meeting the bar in every class — reporting alone is not
sufficient"). `gpt-5.4-mini` clears the *overall* recall bar at 0.906 while missing two
classes, which is precisely the class-shaped hole an overall figure conceals and the
reason per-class bars exist.

Cohen's κ between labeller A (hand) and labeller B (independent model pass driven by
`ADJUDICATION.md`, **not** the extractor prompt): **0.744**, raw agreement 0.865, against a
preregistered 0.70. The corpus is admissible.

**Why the role is bound to `claude_sonnet`.** Measured, not preferred. `gpt-5.4-mini`
cannot be improved in place: `reasoning_effort` above `none` is an *outage* on that
deployment, not a cost change — litellm rejects it alongside the `temperature: 0.0`
FRE-758 pin, and `entity_extraction` and `compressor` bind to the same deployment. So the
choice is between models, and the contract's strength is bounded by extraction recall.

**The one unmet bar.** `fp_rate.class.connective_evaluative` = 0.267 against 0.15. The
cause is structural rather than a tuning miss: that exemption applies only "over cited
material", and this ticket's extractor has no citation input, because FRE-1280's source
registry and marker format are not wired in yet (FRE-1282). Most of the corpus's documents
in that class express citedness in prose ("the two cited figures") rather than carrying
markers, which is precisely the case the extractor cannot decide. It fails in the **safe**
direction — an exempt span swept into the contract costs a citation, never a missed claim.
The class becomes fully measurable after FRE-1282.

## Known corpus gap — f-strings

The `nl_in_code` class carries 10 gold spans and **not one of them uses an f-string**, so
the corpus did not catch a full bypass that a code review did: PEP 701 means
`print(f"Paris has 9 million residents")` tokenizes with no `STRING` token at all, and the
first implementation left every f-string invisible to prose extraction. Fixed in
`code_regions.py` and covered by unit tests, but the *corpus* should have found it.

Deliberately **not** patched by adding documents now: the corpus is preregistered, and
extending it after seeing results would change the denominator the bars were fixed
against. FRE-1282 should add f-string cases when it next versions the corpus.

## Measurement noise — read before comparing two runs

This is a calibration and regression set, not a statistically powered benchmark. With
10–15 gold spans per class, **one span moves a per-class figure by 0.07–0.10**. Three
same-prompt dev runs during this build swung `factual_entity` across 0.50–0.80 and
`prose_in_fence` across 0.60–1.00 without the prompt touching either class — noise
dominating signal, which is why `--samples` exists and why the report prints the
per-sample spread instead of a single number. Pooled over three samples the overall recall
spread narrowed to ±0.027 (0.924–0.978) for the shipping configuration.

Do not read a few-point per-class move as a change. Grow the corpus toward 300+ spans
before treating one as real.

## Curation discipline (public repo)

Documents are grounded but invented — no transcripts, no PII, no deployment identifiers.
`test_fre1281_corpus.py` enforces a denylist and an email pattern. It differs from the
FRE-630 denylist it otherwise mirrors in one way: the bare `"@"` is replaced by an email
pattern, because this corpus contains real code and a decorator is not a leak.
