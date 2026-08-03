# FRE-1122 — absence-probe fixture

Measures whether the system can tell **"I have this"** from **"I have nothing"**,
against ground truth known by construction, and establishes the baseline before
[FRE-1118](https://linear.app/frenchforest/issue/FRE-1118) changes anything.

## What this adds, and what it deliberately reuses

`scripts/eval/fre435_memory_recall/` already provides a probe schema, entity
seeding, scoring, reporting, and a bespoke probe set carrying true-negative
abstention controls. It scores the recall **record** — recall@k, precision@k,
MRR — and its harness never calls the orchestrator or an LLM.

That is the gap the FRE-1116 analysis named: the record does not distinguish a
correct answer from a confabulation assembled out of nearest neighbours, and the
middle one is the failure the owner actually reports.

**So the only genuinely new layer here is driving a probe through to a rendered
answer and classifying that answer.** Everything else is reuse. The scope was
held to that deliberately.

## Why there is no judge

The [FRE-1063 decision record](https://linear.app/frenchforest/issue/FRE-1063)
established that an LLM judge cannot be validated at this corpus size — a
calibration set large enough to trust one would be larger than the population
being measured. Building each probe from a fact of *known status* removes the
semantic-obtainability step entirely, and the judge with it. Classification is
decidable from the answer text against content known before the run.

## The six cells

Each answer is classified into exactly one of three outcomes, crossed with the
probe's known status:

| | asserted correct | asserted wrong | declared absence |
|---|---|---|---|
| **present** | correct recall | confabulation over truth | false absence |
| **absent** | *unreachable by construction (AC-7)* | confabulation on nothing | honest absence |

`ASSERTED_CORRECT` is unreachable on the absent half because AC-7 requires every
absent subject be personally scoped and unobtainable by any route — so no
correct answer exists to be produced, from the store or from the model's
weights. An answer that decides neither way is reported `unclassifiable` rather
than defaulted into a cell.

The absent half is the load-bearing part. It is the only thing that separates
the two ways FRE-1118 can go wrong: leaving confabulation in place, or trading
it for arbitrary hedging on questions the system can actually answer.

## Files

| File | Role |
|------|------|
| `probes.py` | Schema + the construction rules, enforced at load |
| `classify.py` | Deterministic three-way classification (AC-4) |
| `ground_truth.py` | The AC-1/AC-2 evidence queries and session-scoped cleanup (AC-3) |
| `runner.py` | Four-phase CLI |
| `probe_set.template.yaml` | Construction rules + non-personal worked examples |

## Where the real probe set lives, and why not here

**This repository is public.** AC-2 requires quoting the stored text a correct
answer must reproduce; AC-7 requires every subject be personally scoped to the
owner. Both force real personal content into the probe set and the report.

So the committed file is `probe_set.template.yaml` — the rules and the shape.
The real set and every run artifact live under
`telemetry/evaluation/fre1122-absence-probe/` (gitignored), following the
FRE-435 precedent: raw runs are never committed, only curated summaries.

## Run protocol

```bash
# 1. Ground truth by query. Read-only, fires no turns. Safe to re-run.
uv run python scripts/eval/fre1122_absence_probe/runner.py preflight \
    --probe-set telemetry/evaluation/fre1122-absence-probe/probe_set.yaml \
    --user-id <owner-uuid>

# 2. The twenty turns. NEEDS THE OWNER'S AUTHORIZATION — see below.
uv run python scripts/eval/fre1122_absence_probe/runner.py run \
    --probe-set telemetry/evaluation/fre1122-absence-probe/probe_set.yaml \
    --user-id <owner-uuid> --authorized-by "<who authorized, when>"

# 3. Pollution → cleanup → re-check. Dry run by default; --execute to delete.
uv run python scripts/eval/fre1122_absence_probe/runner.py postcheck \
    --probe-set telemetry/evaluation/fre1122-absence-probe/probe_set.yaml \
    --user-id <owner-uuid>

# 4. The six-cell report.
uv run python scripts/eval/fre1122_absence_probe/runner.py report \
    --probe-set telemetry/evaluation/fre1122-absence-probe/probe_set.yaml
```

The relational and Elasticsearch side of cleanup is `scripts/cleanup_eval_data.py`,
which purges by `session_id`. The run phase writes a `results.json` in the shape
that script consumes — FRE-1122 is single-arm, so every turn appears as a
control side:

```bash
uv run python scripts/cleanup_eval_data.py \
    telemetry/evaluation/fre1122-absence-probe/results.json --dry-run
```

## The run phase is authorization-gated

`run` fires twenty real turns at the live gateway under the owner's identity and
permanently writes to the real corpus. `--authorized-by` is required, and the
refusal happens before dispatch — it is not reachable as a side effect of
running the other phases. `tests/evaluation/test_fre1122_runner_guard.py` pins
that.

## The substrate question this fixture answers about itself

Extraction runs on every turn, so asking about an absent subject creates
entities and episodes about it — the corruption loop FRE-1116 documented on the
clafoutis entity. The original ticket concluded from this that the absent half
is single-use, which would have meant the FRE-1118 before-and-after compared
*different probes*. A delta measured across two different probe sets is not a
delta.

Master's correction reframed the question from *does pollution happen* to *is it
reversible*, and the answer turns out to be readable from the write path:

- **`:Entity` nodes carry creation provenance.** Both write sites
  (`memory/service.py:1275`, `:2206`) stamp `e.originating_session_id` under
  `ON CREATE SET`. A pre-existing entity keeps its original stamp, so the marker
  separates probe-*created* nodes from nodes the probe merely re-mentioned. On
  the absent half nothing pre-existed, so every stamped node is the probe's own.
- **`:Turn` nodes carry `session_id` and `originating_session_id`** directly
  (`service.py:1194-1223`), and the ADR-0074 joinability walk already queries
  both node kinds by `originating_session_id`.
- **Message history is one row.** `sessions.messages` is a JSONB column
  (`docker/postgres/init.sql:14`) — there is no separate messages table, so
  deleting the session row removes its whole history.

**The residue cleanup cannot undo.** The write path also mutates *pre-existing*
entities on every mention — `e.last_seen`, `e.mention_count + 1`, and
`e.entity_type` when previously empty (`service.py:1279-1283`). Deleting
probe-created nodes does not roll those back. On the absent half this is nil by
construction; on the present half it is real and is reported as residue with its
size, never glossed (AC-3).

`postcheck` measures all of this and records the substrate decision AC-6 turns
on: if cleanup restores the absent half, the FRE-1118 delta runs live on the
same probes; if it does not, the delta runs on the test substrate so the
comparison stays same-probe either way.

## Not yet established

Whether consolidation or episodic→semantic promotion rewrites `description` on
**pre-existing** entities. If it does, that is genuinely irreversible residue,
and it lands on the present half. `postcheck`'s mutated-entity count surfaces the
population it would affect; characterising it is not this fixture's job.
