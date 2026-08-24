# FRE-1281 — Span extractor and labelled corpus (ADR-0138 D1, AC-7)

**Ticket:** [FRE-1281](https://linear.app/frenchforest/issue/FRE-1281) — Approved, Urgent, Tier-1:Opus, `stream:build1`
**ADR:** [ADR-0138](../../architecture_decisions/ADR-0138-the-model-may-generate-but-may-not-assert.md) — D1 (default-deny, exempt regions, precedence, atomicity), AC-7
**Parallel:** FRE-1280 (PR #951, open, unmerged) — `src/personal_agent/grounding/{source_registry,citations}.py`
**Blocks:** FRE-1282 (D3(b)(c) + the D4 loop)

---

## Scope

- A **span extractor**: partitions model output into non-overlapping atomic-claim spans and
  labels each `EXEMPT` (naming the D1 region) or `NON_EXEMPT`. Lives in `src/`, since it is on
  the inline turn path FRE-1282 will call.
- A **labelled corpus**: a versioned artifact under `scripts/eval/`, hand-labelled, with recorded
  inter-labeller agreement, per-class tags, and **bars preregistered in a commit that precedes
  any scoring run**.
- A **scoring core** (pure, unit-tested, no LLM) plus a **live harness** (LiteLLM, run against the
  test substrate per FRE-375).
- **Not in scope:** D3(b)(c) verification, the D4 retry loop, prompt changes, executor wiring.
  Nothing in this PR blocks a turn.

## Sequencing against FRE-1280

FRE-1280 is unmerged, so this branch cuts from `origin/main` and does **not** import
`grounding.source_registry` or `grounding.citations`. The two meet in FRE-1282. The only shared
file is `src/personal_agent/grounding/__init__.py`, which both create — a one-hunk conflict master
resolves at the second merge. Recorded here so it is expected, not discovered.

---

## Design decisions

> **Revised after codex plan-review round 1** (4 blocking, 6 major, 1 minor — all taken).
> The changes are marked **[R1-n]** against the finding they close. Two findings changed the
> design rather than tightening it: layer 1 no longer grants exemption it cannot prove (R1-2),
> and layer 2 now **tiles** its regions rather than emitting a sparse span list (R1-4), which
> also resolves anchoring under repeated text (R1-10) as a side effect.

### D-A — Three layers, and only the middle one is a model

The ADR says span classification "is a named component, not a regex". It does **not** say every
part of D1 is semantic. Splitting them is what makes most of the contract unit-testable:

| Layer | Job | Determinism |
|---|---|---|
| **1. Region partition** (`code_regions.py`) | Where, if anywhere, can exemption be *proved*? Fences, declared language, parse check, comment/string scan inside code, dependency declarations. | Fully deterministic |
| **2. Classification** (`extractor.py`) | Tile each remaining region into atomic segments and label each. | Model pass, forced tool schema |
| **3. Policy post-pass** (`span_policy.py`) | Enforce D1's invariants over layer 2's tiling, and fail closed on any gap. | Fully deterministic |

Layer 1 answers a syntactic question (*where is a comment?*) that a scanner legitimately answers.
Layer 2 answers the semantic one (*is this string literal a claim about the world?*). The ADR's
`print("Paris has 9 million residents")` example needs exactly this split: layer 1 finds the
literal, layer 2 decides it is a claim.

**[R1-2] Layer 1 grants exemption only where it can prove code-ness.** It is not a classifier and
must never act like one. A region is exempt-by-construction *only* if a parser is available for
the declared language **and** the content parses. Everything else — unknown language, absent
parser, parse failure, no fence at all — is handed to layer 2. This closes the bypass codex found:
under the previous draft, arbitrary prose in a ```` ```js ```` fence was exempt because no JS
parser exists here, which is exactly what ADR AC-5's "*fails if* fencing or mere parseability buys
exemption" forbids. It does not block generation either, since layer 2 can still call a `bash`
block code — the difference is that the judgement is now made by the component the ADR designates
for it, rather than assumed by a table lookup.

**[R1-4] Layer 2 tiles; it does not emit a sparse list.** Every character of every region layer 2
receives must belong to exactly one returned segment, each labelled `CLAIM_EXEMPT`,
`CLAIM_NON_EXEMPT`, or `NOT_A_CLAIM`. The distinction this buys is the whole point: *"layer 2
examined this text and judged it not a claim"* is a decision the corpus can score, whereas *"layer
2 never mentioned this text"* is a silent seam through which a claim leaves the contract with no
record. Under the previous draft, layer 3 could only govern spans layer 2 chose to return.

**Layer 3 only ever moves a label toward `NON_EXEMPT`.** That is what makes it safe: it can
manufacture a false positive (measured by the precision bar) but can never rescue a claim from the
contract. Four rules:

- **Coverage is conserved, and a gap fails closed** — any character of a region not covered by
  layer 2's tiling, and any overlap in the tiling, is emitted as `NON_EXEMPT`. A malfunction must
  cost precision, never recall. **[R1-4]**
- **Ambiguity resolves to assertion** — layer 2 returning `AMBIGUOUS`, an unknown region, or an
  unanchorable quote forces `NON_EXEMPT`.
- **Non-exempt wins on overlap** — where an exempt and a non-exempt span overlap, the exempt span
  is dropped, not trimmed (trimming would emit a partial claim).
- **Categorical pins** — two classes are forced `NON_EXEMPT` regardless of what layer 2 said:
  **dependency declarations** located by layer 1 **[R1-5]**, and spans carrying a
  **checkable-predicate** (`well regarded`, `safe`, `popular`, `recommended`, `reliable` and
  inflections). Both are categorical in D1, so neither is the model's call.

### D-B — Anchoring: sequential, exact quote, default-deny on failure

Layer 2 returns exact substrings, not offsets (models are unreliable at arithmetic over character
positions). **[R1-10] Anchoring is sequential**: because the tiling is ordered and exhaustive, each
segment's quote is searched from the end of the previous segment, so repeated identical text
resolves unambiguously — which matters directly, since AC-4's own probe repeats a package name
first as the user's words and then as the model's recommendation. A quote that cannot be anchored
(after whitespace normalisation) is dropped and **its whole region is marked `NON_EXEMPT`**.

### D-C — Which languages can be proved to be code

Per D1, a fence "whose content does not parse as the declared language" is not exempt.

- **Parser available and content parses** — `python` (`ast`), `json`, `toml`, `yaml` → exempt code
  region, minus dependency declarations and minus the comment/string content the scanner finds.
- **Everything else** — `text`/`markdown`/no fence, an unknown language, **and any code language
  without a parser here** (`bash`, `js`, `ts`, `sql`, `go`, `rust`, …) → handed to layer 2. **[R1-2]**

The comment/string scanner tracks quote state, so `#` inside a string is not a comment and `//`
inside a URL is not one either. Python uses `tokenize` rather than the scanner — exact, and it is
the language the ADR's own example is written in.

### D-D — Bars: each one names the broken baseline it rejects

AC-5's floor principle ("a bar a known-broken implementation passes is not a bar") is made
machine-checked rather than argued. Every bar in `bars.py` carries `justification` and
`rejects_baselines`, and `test_fre1281_bar_floor.py` proves the claim by scoring five deliberately
broken extractors against the real corpus:

| Baseline | What it does | Must fail |
|---|---|---|
| `null` | recognises nothing | every recall bar |
| `exempt_all` | every span exempt | every recall bar |
| `accept_all` | all prose non-exempt, no code awareness | precision, and every exempt-class FP bar |
| `entity_triggered` | the draft D1 rejected in review — fires only on a named entity **or a figure** | `factual_bare_predicate` recall |
| `fence_trusting` | exempts anything inside a fence | `prose_in_fence` and `nl_in_code` recall |

**[R1-6] Two of those five only bite if the corpus guarantees it, so the corpus enforces it.**
Codex showed both claims were asserted rather than secured:

- `entity_triggered` fires on an entity **or** a figure, so the class it must miss has to be free
  of *both*. The class is therefore defined as **`factual_bare_predicate`** — a checkable claim
  carrying no named entity **and no numeral** (*"this fish is high in mercury"*). `corpus.py`
  rejects any span tagged into that class that contains a capitalised non-initial token or a digit,
  so the guarantee is a load-time invariant rather than a labelling intention.
- `accept_all`'s precision ceiling is `non_exempt / (non_exempt + exempt)`. With the exempt
  fraction held at **≥ 0.30** by a corpus test, that ceiling is **≤ 0.70 < 0.80**, so it fails the
  precision bar by arithmetic rather than by hoping the counts come out right. The test states the
  arithmetic in its assertion message.

A **positive control** (the oracle extractor, replaying gold) must pass every bar — otherwise the
bars are unsatisfiable rather than strict.

**[R1-8] Minimum class size is 10, not 6.** At 6 examples a 0.85 recall bar means 6/6 and a 0.15
FP bar means 0/6 — both collapse into perfection tests, and whether one error is survivable becomes
an artefact of how many examples a class happens to have. At 10, each bar tolerates exactly one
error, which is a bar rather than a coin flip.

**[R1-7] The scoring unit, stated so it cannot be chosen favourably later.** A predicted span
matches a gold span when their character ranges reach **IoU ≥ 0.5**; matching is one-to-one, greedy
by descending IoU. *Recall (per class)* = gold `CLAIM_NON_EXEMPT` spans of that class matched by a
predicted non-exempt span ÷ all gold non-exempt spans of that class. *Precision* = predicted
non-exempt spans matching some gold non-exempt span ÷ all predicted non-exempt spans. *FP rate (per
exempt class)* = gold exempt spans of that class overlapped by a predicted non-exempt span ÷ all
gold exempt spans of that class. Vacuous denominators return `None` and are excluded, never scored
as `1.0`.

Preregistered values, fixed in the first commit, before any extractor exists:

| Bar | Value | Failure it prevents |
|---|---|---|
| per-class recall (each of 8 non-exempt classes) | **≥ 0.85** | a class-shaped hole — the bare-predicate hole D1's inversion exists to catch |
| overall recall | **≥ 0.90** | the contract being weaker than its weakest measured claim class |
| overall precision | **≥ 0.80** | blocking legitimate generation (D1's usability bound) |
| per-exempt-class false-positive rate (each of 5 exempt classes) | **≤ 0.15** | code bodies / restatement / cited arithmetic being swept in (AC-3) |
| **[R1-11]** corpus admissibility: Cohen's κ | **≥ 0.70** | a corpus too noisy to measure the distinction it claims to measure |
| **[R1-9]** decomposition boundary-F1 over the whole corpus | **≥ 0.75** | atomicity claimed from one memorable example |

### D-E — Inter-labeller agreement, with a bar

Labeller A is this session, by hand, against `ADJUDICATION.md`. Labeller B is an independent model
pass driven by the **adjudication guidelines**, deliberately not the extractor prompt — otherwise
agreement would measure the extractor. Cohen's κ on the exempt/non-exempt decision over gold spans,
plus boundary F1, stamped with model, date and commit into `corpus_agreement.json`.

**[R1-11]** κ ≥ 0.70 is preregistered alongside the other bars as a condition on the *corpus*, not
on the extractor: below it, the guidelines are too ambiguous for the corpus to measure anything and
`ADJUDICATION.md` is revised before the extractor is scored. "κ recorded" is not a bar.

### D-F — Held-out discipline **[R1-1]**

Codex is right that AC-7 says *held-out* and that preregistration alone does not stop an
implementation from special-casing documents it has seen. It is also true that a single session
authoring both the corpus and the extractor cannot make itself blind by assertion. So the intent is
implemented by two mechanisms and one honestly-stated residual:

1. **Partitioned corpus.** Every document carries `partition: dev | heldout` (≈60/40), fixed in the
   preregistration commit. `harness.py --partition dev` emits per-document diffs; `--partition
   heldout` emits **aggregate per-class numbers only, and never document text or per-document
   diffs** — a property of the reporter, not a promise. Iteration happens on `dev`; `heldout` is
   scored after the extractor and its prompt are frozen.

   **The bars are adjudicated on the full corpus, and this is a deliberate trade.** R1-8 requires
   ≥10 gold spans per class for a bar to tolerate one error rather than demand perfection; a 40%
   held-out slice would put each class at ~4, making every per-class bar a coin flip again. Scoring
   the full corpus keeps the granularity that makes the bars real, and ~40% of the spans in that
   figure still come from documents whose individual failures were never inspected. The
   heldout-only aggregate is reported alongside as a secondary read, with its small per-class
   denominators stated rather than presented as equivalent.
2. **No-corpus-literals guard.** `test_fre1281_no_corpus_leakage.py` fails if any string of ≥20
   characters drawn from `corpus.yaml` appears anywhere under `src/personal_agent/grounding/`. This
   is the mechanism that actually prevents the harm codex names — memorised documents, surface
   phrases, or a denylist tuned to the probes — and it is seeded with a deliberate negative so it
   is not a vacuous check.
3. **Residual, recorded rather than papered over.** The author of the corpus and the author of the
   extractor are the same session. A genuinely author-blind corpus needs a second party; that is
   named in the README and the handoff as the residual, not claimed as done.

---

## Files

```
src/personal_agent/grounding/
  __init__.py          NEW (conflicts with FRE-1280; expected)
  spans.py             Span, SpanLabel, ExemptRegion, SpanExtraction
  code_regions.py      layer 1 — fences, parse check, comment/string scan, dependency decls
  span_policy.py       layer 3 — invariants, ambiguity→assertion, overlap precedence, denylist
  extractor.py         layer 2 — SpanExtractor Protocol + ModelSpanExtractor + tool schema

config/model_roles.yaml            + span_extraction role → gpt-5.4-mini
src/personal_agent/llm_client/types.py   + ModelRole.SPAN_EXTRACTION

scripts/eval/fre1281_span_extraction/
  __init__.py
  README.md            run protocol, layout, limitations
  ADJUDICATION.md      versioned labelling guidance (the ticket requires it live with the corpus)
  corpus.yaml          ≥130 gold spans, ≥10 per class, 13 classes, dev/heldout partitioned
  corpus.py            schema + loader + discipline guards
  bars.py              preregistered bars with justification + rejects_baselines
  metrics.py           pure: IoU matching, per-class recall, precision, per-class FP rate
  baselines.py         the five broken extractors + the oracle
  report.py            ScoreReport, bar evaluation, JSON/markdown emit
  iaa.py               independent second labeller + Cohen's κ
  harness.py           I/O driver (CLI)
  corpus_agreement.json   committed, stamped

tests/personal_agent/grounding/
  test_code_regions.py  test_span_policy.py  test_extractor.py  test_spans.py
tests/evaluation/
  test_fre1281_corpus.py  test_fre1281_metrics.py  test_fre1281_bar_floor.py
  test_fre1281_no_corpus_leakage.py
```

### The 13 classes **[R1-7]**

Non-exempt (a citation is required): `factual_entity` · **`factual_bare_predicate`** (no named
entity, no numeral) · `prose_in_fence` · `nl_in_code` · `dependency_declaration` ·
`prose_about_code` · `checkable_evaluative` · `unattributed_restatement`.

Exempt: `code_body` · `attributed_restatement` · `derived_arithmetic` · `connective_evaluative` ·
`system_record`.

At ≥10 spans each that is ≥130 gold spans, of which ≥50 exempt — comfortably above the ≥0.30
exempt fraction that makes `accept_all` fail precision by arithmetic.

---

## Steps

**S1 — Preregistration commit.** `corpus.yaml`, `corpus.py`, `ADJUDICATION.md`, `bars.py`,
`test_fre1281_corpus.py`. No extractor exists yet; commit order is the AC-5 evidence.
→ verify: `make test-k K=fre1281` green; `git log --oneline` shows this commit before any scoring.

**S2 — Pure scoring core.** `metrics.py`, `baselines.py`, `report.py`,
`test_fre1281_metrics.py`, `test_fre1281_bar_floor.py`.
→ verify: each broken baseline fails its named bars; the oracle passes all.

**S3 — IAA.** `iaa.py`; run it; commit `corpus_agreement.json`.
→ verify: κ recorded, stamped with model + commit.

**S4 — Layer 1.** TDD `code_regions.py` against `test_code_regions.py`.
→ verify: `make test-k K=code_regions`.

**S5 — Layer 3.** TDD `span_policy.py` against `test_span_policy.py` — including AC-4's
user-supplied-package-repeated-as-recommendation case.
→ verify: `make test-k K=span_policy`.

**S6 — Layer 2.** `spans.py`, `extractor.py`, the `span_extraction` role,
`ModelRole.SPAN_EXTRACTION`. Stub-client tests for prompt assembly, tool-schema parsing, anchoring,
and default-deny on anchor failure.
→ verify: `make test-k K=extractor`; `make test-k K=test_types` (matrix↔enum parity stays green).

**S7 — Harness + live run, and S7 gates S8. [R1-3]** `harness.py`, `README.md`;
`make test-infra-up`; score `dev`, iterate the prompt and the deterministic layers against its
per-document diffs; freeze; then score `heldout` **once**, aggregate-only.
→ verify: every preregistered bar — overall recall, overall precision, all 8 per-class recalls, all
5 per-exempt-class FP rates, decomposition boundary-F1 — is met on `heldout`.
→ **If a bar misses:** the fix may only be justified from first principles (I will not have
heldout per-document diffs), each re-score is appended to the report history, and if it still
misses, the number ships as measured and the AC is reported **not met** to master. A failing bar
does not silently become a passing PR. Bars are never retuned.

**S8 — Gates + self-review + PR + handoff.** `make test` · `make mypy` · `make ruff-check` ·
`make ruff-format` · `pre-commit run --all-files`; commit; `feature-dev:code-reviewer` scoped to
`git diff origin/main...HEAD`. Diff class: **self-serve** — no production write path (nothing calls
the extractor yet), no schema change, no destructive code, no cost/governance logic. The one cost
surface is the eval harness, which routes through the existing gate against the test substrate.

---

## Acceptance criteria

| AC | Criterion | Evidence |
|---|---|---|
| **AC-1** | Non-overlapping atomic-claim spans; the Paris conjunction yields two | **[R1-9]** decomposition boundary-F1 ≥ 0.75 over the *whole* corpus (not one example), plus the named Paris case; `test_span_policy.py` proves non-overlap/no-nest and coverage conservation deterministically |
| **AC-2** | Recall meets bar **in every class**, reported per class, incl. entity-free, prose-in-fence, NL-in-code | `report.md` per-class table from S7 |
| **AC-3** | Precision meets bar; code bodies / restatement / cited arithmetic not swept in | Same report: overall precision + per-exempt-class FP rate |
| **AC-4** | Non-exempt wins on overlap | `test_span_policy.py::test_repeated_user_package_as_recommendation_is_not_rescued` |
| **AC-5** | Versioned corpus, recorded IAA, bars fixed before results, and no bar a broken baseline passes | `git log` commit order; `corpus_agreement.json` with κ ≥ 0.70; `test_fre1281_bar_floor.py` (5 baselines fail their *named* bars, oracle passes all); `test_fre1281_no_corpus_leakage.py` |

## Risks

- **Bars may not be met.** They are preregistered and will be reported as measured, not retuned.
  A miss is a finding about the extractor, and the ticket says so: the contract's strength *is*
  extraction recall. S7 now gates S8, so a miss is surfaced rather than absorbed.
- **Corpus is a calibration set, not a powered benchmark** (~130–160 spans). Stated in the README,
  as FRE-630 states it for its own gold set.
- **Held-out is enforced mechanically, not by a second party** — the residual under D-F.
- **`__init__.py` conflicts with FRE-1280** at master's second merge. Expected, one hunk.

## Pre-existing baseline (measured on `origin/main`, before any edit)

`make mypy` clean (324 files) · `make ruff-check` clean · `make test` **8 failed / 7802 passed**.
The 8 are pre-existing and **environmental**, not main being red: CI's own *Backend unit tests* job
is green on `main` (the red CI run there is `Caddyfile validate`, unrelated), and this worktree's
`.env` sets recall/stance flags CI does not. Recorded in
`scratchpad/baseline_failures.txt`; the gate at S8 is *no new failures against this set*, with CI
as the authority.
