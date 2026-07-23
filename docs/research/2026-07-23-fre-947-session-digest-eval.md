# FRE-947 — session-digest producer evaluation (ADR-0124 Phase 0)

**Date:** 2026-07-23 · **Ticket:** FRE-947 · **Backing:** ADR-0124
**Arms run against:** the `session_summary` role's live deployment (`claude_sonnet`),
through the real `generate_session_digest` — no test-only path.
**Harness:** `scripts/eval/session_digest_eval.py` · **Fixtures:**
`tests/fixtures/session_digest/` (pre-registered, see `REGISTRY.md`)

---

## Summary

| Criterion | Verdict | Evidence |
|---|---|---|
| **AC-8** input completeness | **PASS** | 3/3 cases, scored offline against the capture records |
| **AC-9** tool-only facts survive | **PASS** | **5/5** reproduced, one per distinct tool |
| **AC-13** missing evidence | **PASS** | 3/3 — silence where the payload is absent, corrections where status/self-correction carry it |
| **AC-12** corrections precision/recall | **PASS after a producer fix** | run 1: recall 10/10 but **1 false positive** → run 2: **0 false positives, recall 10/10**, every Tier-B evidence span present |
| **AC-10** basis tagging | **NOT PROVEN** | the measurement is inadequate, not the producer — see §3 |

Two of these deserve more than a row, and neither should be read as a pass.

---

## 1. What the corpus could not supply (and why that matters first)

ADR-0124 requires corpus feasibility to be established before any criterion naming a
sample size. It was, and the answer changed the shape of this evaluation.

The ADR reasoned about the **graph** population — 121 `Session` nodes, 59 multi-turn —
and this reproduces those numbers exactly. But the producer reads **captures from
disk**, and those are a different population. Retention has purged the captures for
**every one of the 59 multi-turn sessions in the graph**; the intersection is empty.
The on-disk store holds 553 session ids, of which 13 are multi-turn and 2 carry tool
results.

So no criterion here could be evaluated on real sessions. Per the ADR's stated
remedy, every set is a **pre-registered synthetic supplement, labelled as such**, and
no criterion was shrunk to fit. This is a real limitation on what the arms prove:
they establish the producer *can* do the job on well-formed evidence, not how it
behaves on the messy real corpus. That is what Phase 1's human review exists to
supply, once post-deploy sessions accumulate.

**Operational consequence:** the first sweep after deploy will digest nothing. It will
find 121 dirty sessions, read zero captures for each, and mark them clean with no
digest. The sweep counts `no_captures` separately from `skipped` so this reads as what
it is rather than as a successful floor application.

## 2. AC-12 — a genuine producer defect, found and fixed

**Run 1:** recall **10/10** (all six Tier-A contradictions and all four Tier-B
evidenced self-corrections fired, every Tier-B carrying its supporting-evidence span),
and **11 of 12** Tier-C negatives correctly stayed silent.

The one false positive is worth quoting, because it is exactly the failure mode D3's
precision-first stance exists to prevent:

> Case `c12_subjective_priority`. The assistant said *"I would treat it as low
> priority."* The tool returned `{"error_rate": 0.001, "severity_label": "high"}`.
> The producer emitted: *"Assistant assessed the issue as low priority, but queried
> severity data labeled it 'high'."*

That is not a contradiction. The assistant asserted what it **would do**; the data
reports what something **is**. ADR-0124 D3 lists "disagreement with a subjective
judgment or recommendation" as Tier C explicitly, and Tier A requires evidence to
contradict *the same proposition*. The producer collapsed a judgment into a factual
claim — the precise error that, shipped, writes self-confirming false state into the
graph.

**Fix:** the system prompt now carries an explicit same-proposition test before Tier A
may be asserted, naming the three collapses that produce false positives — judgment vs
fact, approximation vs wrong number, scoped vs universal claim — and requiring the
producer to be able to name the single proposition asserted and denied.

**Run 2** (same frozen fixtures, prompt fixed): **0 false positives**, recall
**10/10**, every Tier-B correction carrying its supporting-evidence span. All twelve
Tier-C negatives — including the one that failed — stayed silent. AC-12 passes.

**This fix was made after seeing run 1's result, and that is disclosed rather than
hidden.** What the fixture discipline forbids is editing a *set* to fit an outcome;
the fixtures are byte-identical between the two runs and remain frozen. Fixing a
defect an eval reveals is what running the eval was for. Both runs are committed
verbatim (`…-run1.json`, `…-run2-ac12.json`) so the delta is auditable rather than
asserted.

Worth stating plainly: the recall side was never the weak point — 10/10 on both runs,
across six Tier-A contradictions and four Tier-B self-corrections. The producer's
failure mode was over-firing on a judgment, which is exactly the asymmetry D3
predicts, and exactly the one that matters most to get right.

## 3. AC-10 — the instrument does not measure the criterion

AC-10 asks whether basis tagging *discriminates*: ≥85% agreement with labelled truth,
and no single tag exceeding 60% of emissions.

Run 1 returned agreement 0.364 and dominant-tag share 0.667 — but **only 11 of the 40
labelled items matched an emitted item at all**. That number is the finding.

The harness scores agreement by matching each emitted digest item to the labelled item
it most plausibly restates, via token overlap. That assumes the producer emits roughly
one item per labelled item. It does not, and by design must not: the digest is a
compressed epistemic record bounded at ~180–250 tokens that deliberately omits most of
what a session contains. Across 8 sessions it emitted 24 items against 40 labels, and
only 11 could be matched with any confidence. An agreement figure over 11 fuzzy
matches does not measure what AC-10 asks, and the dominant-share figure is likewise
confounded — 2 of every 4 fixture items carry tool output, so a producer that genuinely
reads full payloads will legitimately skew toward `tool_evidence`.

**Verdict: NOT PROVEN, in either direction.** I am not reporting this as a producer
failure, because the measurement cannot support that claim; and I am not reframing it
into a pass. A bad eval is discarded, not reinterpreted.

**What AC-10 needs instead** — to be pre-registered fresh, before it is run:
a design where the ground truth is attached to items the producer is *obliged* to
emit, or a labelling pass over items the producer actually emitted, scored by two
independent reviewers rather than by token overlap. That is a real design task, not a
threshold adjustment, and it is called out as outstanding rather than quietly folded
into a green table.

## 4. What none of these arms prove

AC-11's located-span validation, which every emitted item passed, is a **necessary**
condition only: it proves a citation resolves to the field it names, not that the span
*supports* the proposition. A fabricated item citing a real but irrelevant span at a
valid locator passes it. Mechanical entailment is not available to us; semantic support
rests on AC-12's labelled cases and, in Phase 1, on human review.

---

## Reproduce

```bash
# offline only, no spend
uv run python scripts/eval/session_digest_eval.py --dry-run

# a single set
uv run python scripts/eval/session_digest_eval.py --set ac12

# full arm (~38 model calls)
uv run python scripts/eval/session_digest_eval.py --out report.json
```

The build worktree's `.env` is a stub; the harness needs `AGENT_DATABASE_URL` and the
provider keys from the primary repo's `.env`, and registers a real `CostGate` so the
arm's spend lands on the same ledger as production's rather than being invisible to it.

## References

- ADR-0124 — session-summary producer correction and phased consumption
- `tests/fixtures/session_digest/REGISTRY.md` — the pre-registered sets and the corpus count
- `docs/research/2026-07-23-fre-947-session-digest-eval-run1.json` — run 1, verbatim
