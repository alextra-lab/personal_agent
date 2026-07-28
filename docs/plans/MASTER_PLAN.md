# Master Plan — Personal Agent

> **Forward plans only.** What we are going to do, in order. **Not a diary of accomplishments — that is
> the git log.** No history, no state narrative, no post-mortems. What shipped → `git log`; why a
> decision was made → the Linear ticket; this session's decisions → [`LAST_SESSION.md`](LAST_SESSION.md);
> per-ticket state → [Linear](https://linear.app/frenchforest).
> **Last updated**: 2026-07-28

## 0. In flight

- **build1** — **FRE-1037** *LLM role assignment*: 93% of calls report `role=primary` because the call
  path exposes 4 roles while config defines 15. Widen the enum **from config**, thread the real role,
  **then** fail closed. **FRE-989** *cost attribution* is blocked behind it — attribution cannot be fixed
  while the role is untrue.
- **build2** — **FRE-1021** *entity-candidate displacement* (measure the rate; n=4 is a mechanism, not a
  number) → **FRE-1015** *topic-scoped stance enrichment* → **FRE-937** *collapsed per-turn summary*.
- **adrs** — **FRE-1038** *one telemetry naming and structure convention across every substrate, enforced
  at emit*.

## 1. Elasticsearch structure — approved, deliberately held

**FRE-1036** *(480 indices / 627 MB; monthly + ILM; shard ceiling ~34 days out)* is Approved but **must
not start before FRE-1038 settles the naming convention.** It rewrites every index template and is the
cheapest moment to normalise names; running it first bakes the inconsistency in for another cycle.

**FRE-1035** *(ES field-resolution technique)* — resolve every field against the mappings API before
querying; treat zero matches as a hard error. Needs approval. The recipe fix is the smaller half.

## 2. Awaiting an owner decision

- **FRE-1039** — Grafana over Postgres for aggregate cost, and whether it **replaces** Kibana. Amends
  ADR-0090's dashboard corner; inherits the 14 known-broken panels. Prerequisite: no read-only Postgres
  role exists.
- **ADR-0127's seven tickets** (FRE-1026–1032) — the harness-analyser pillar; one batch decision.
- **FRE-1013** — **premise measurably false.** It claims entity class is never emitted; the graph holds
  425 Personal and 708 model-emitted against 6,620 backfilled. Rescope to "is the classification any
  *good*" (a measurement) or cancel.
- **FRE-1033** *(request.completed dead since 2026-06-13)* · **FRE-1014** · **FRE-1009** · **FRE-990** ·
  **FRE-1007** · **FRE-1008** · **FRE-1023** · **FRE-885** · **FRE-805** · **FRE-621**.
- **ADR-0120 cost governance** — Proposed; gates a seven-ticket P0 chain (FRE-898–905).
- **Backlog cull scope + gate** (§6).

## 3. Master's verification backlog — standing debt

**Ten in Awaiting Deploy, all of them deployed.** The column name misleads: what they await is master's
acceptance verification, not a deploy. None closes on "deployed and healthy" — each needs its own
criterion proven, and **UNVERIFIABLE is a first-class verdict**.

| ticket | subject | what it awaits |
|---|---|---|
| **FRE-970** | ES telemetry skills misdirect cost/budget queries | one owner turn — "7-day budget spend, by budget then role". Skills verified correct in-image |
| **FRE-1016** | ADR-0126 T3 — claims reachable via the memory search tool | live AC proof against the graph |
| **FRE-1018** | ADR-0126 T4 — supersession chain on pull | live AC proof; AC-5 chain half |
| **FRE-739** | ADR-0107 T2 — user_id into structured logs | **cannot close.** AC-3a passes 154/154; **AC-3b UNVERIFIABLE** — see below |
| **FRE-998** | the knowledge graph holds no user identity | live proof that Session/Turn now carry `user_id` |
| **FRE-717** | ADR-0105 T4 — outcome ingestion (assembled-ADR seam) | never checked; owns a seam |
| **FRE-986** | ADR-0123 §6 — server-side phase-state projection | never checked |
| **FRE-936** | ADR-0123 T3 — the live phase surface | never checked; PWA, likely needs an owner turn |
| **FRE-972** | compaction gate uses the static 96K qwen window | never checked; needs a non-qwen session |
| **FRE-943** | session config endpoint reports the role-default window | never checked |

**FRE-739 is blocked, not merely unverified.** Its AC-3b requires the hand-rolled request-trace documents
to carry `user_id`, and that path has emitted **nothing since 2026-05-10** (FRE-1033). There is no document
to check. So **ADR-0107 cannot close either**, despite FRE-740 being Done — FRE-739 owns that seam.

Board drift: `reconcile_board.py` reports 3 FAIL — **FRE-432**, **FRE-875**, **FRE-983** — merged PRs
against non-Done states. Not closed blind; they need the same verification.

## 4. ADR-0126 — reading the living-knowledge substrate

T3 (FRE-1016) and T4 (FRE-1018) merged. **FRE-1015** → **FRE-1017** remain; **FRE-1019** is the seam and
**closes the ADR** — removing each of four consumers must turn *named* assertions red from a green
baseline, not the last child merging. The relay-gap check is complete; FRE-1019 carries the binding
comment. A hold set on this chain on 2026-07-28 was lifted the same day as mistaken — **do not re-set it.**

## 5. ADR-0125 — the turn evidence contract (residual)

**FRE-1005** unblocked, parked-Approved. **FRE-1006 closes the ADR** — when a planted machine-readable
false claim is refuted from the stored record by exact comparison, not when fields are populated.
**FRE-1014** — the admission resolver's docstring justifies a multiset match on a property the renderer no
longer has.

Still owed, unticketed: **evidence item 3** (reasoning trace — needs a *feasibility* ticket first, since
the bound Anthropic models never return raw chain-of-thought), and an AST/import-boundary guard forbidding
context-assembly dependency on a dimension-1 producer.

## 6. Cost and summarisation — the open architectural item

The sweep is live and bounded (~8 attempts/day, was 288; ≈$0.040/digest). **That bounds a failing
*session*; it does not bound aggregate spend.**

**Unbounded *input* remains unowned.** ADR-0124 triggers wholesale regeneration on an **idle clock**; the
2026-07-26 explore note (§2) and the summarizer brainstorm (§4A) both conclude rebuild should fire on
**accumulated delta** — a hybrid of incremental deltas plus periodic full rebuild. ADR-0127 **D9** assigns
this fork to ADR-0124's trigger. It is written down and **still has no owner.**

Keep the correction: the cost incident was **a bug, not an indictment of wholesale regeneration**.
Wholesale is right for sessions that end; it breaks under never-ending ones because `f(all captures)`
grows monotonically.

**Still not done:** the March `CONTEXT_INTELLIGENCE_SPEC.md` and its survey
`docs/research/context_management_research.md` cover *within-session* context construction and are cited by
**neither** ADR-0124, ADR-0125 nor ADR-0127. **Two layers to reconcile; do not start a third.**

**Do not re-propagate two false figures** (measured, corrected by ADR-0127): there is **no labelled
corpus** — 1,916 of 1,943 ratings are backfilled defaults, leaving **27**. And the capture corpus is
**1,941 turns, not 8,880**.

## 7. Reduce the backlog

40+ at Needs Approval, ~80 Approved and mostly parked, including **twelve P0s months old** — FRE-940
(replayed approval cards), FRE-927 (broken seat escapes both reconcilers), FRE-867 (seats hang on
non-allowlisted prompts). Method: verify per cluster, cancel the provable with a one-line reason, bring
judgment calls to the owner. Run `scripts/reconcile_board.py` first.

## 8. Pipeline hardening — filed, overdue

**FRE-976** (Linear-reconciled dispatch) · **FRE-975** (gate master on a review-complete signal) ·
**FRE-977** (explore first-class dispatch) · **FRE-1011** (guard docs PRs against ticket tokens).

## 9. Then, in order

Telemetry residuals (FRE-983 ES lifecycle, parked mid-phase) · Configuration Management ·
Linear async feedback · Seshat Inference.

---

## To fix, unscheduled

- **Nothing watches a threshold approaching.** Three hard-threshold cliffs surfaced on 2026-07-28 — the
  log corpus, the container memory limit, the ES shard ceiling — and none had a monitor. Owner priority
  puts monitors second to bugs; this is the standing note that the class exists.
- **The 2 GiB gateway memory limit is no longer load-bearing** — a real turn peaks at 654 MiB, inside the
  original 768. Revert deliberately after a few days of traffic, with sampler evidence.
- **Personal data already committed to the public repo** — cities, venues and a personal name under
  `scripts/study/eval_artifacts/frozen/`, `scripts/eval/fre435_memory_recall/semantic_probe.yaml`,
  `docs/research/EVALUATION_DATASET.md`, `docs/plans/completed/`. **Owner sets scope** — redaction alone
  leaves it in git history.
- **The cost gate reserves against an estimator that runs a third light** — cl100k undercounts billed
  Anthropic input by **1.535×**, and the tool definition adds **1,663 tokens/call** uncounted.
- **D3's loss question is unanswered.** FRE-994's loss endpoint failed its own validity gate (extractor
  recall 0.788 vs 0.80). Any retry needs a different extraction design first.
- **Frozen-reset action never fires on gateway turns** (ADR-0092 #7). FRE-954 sits behind it.
- **FRE-912** — narrowed by FRE-913, not eliminated; parked-Approved.
- **Worker seats strand on non-edit prompts** — FRE-911's `acceptEdits` covers file edits only.
- **Duplicate ADR-0067** — two ADRs share the number; renumber so "supersede ADR-0067" stops being
  ambiguous.
- **Research index unmaintained since March** — `docs/research/README.md` lists no July documents.
- **`master-914`** — stale worktree on the closed `fre-909-seat-rename`, the only reason that branch
  survives.
- **49 orphaned capture files** under `telemetry/captains_log` — pre-containerisation April dev data.
  Owner's call to remove or ignore.
