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
| `manifest.py` | The effective-probe manifest binding the phases together |
| `runner.py` | Four-phase CLI |
| `probe_set.template.yaml` | Construction rules + non-personal worked examples |

## The manifest binds the phases

Preflight writes `effective_manifest.json` — the probes **after** any AC-1
replacement, the owner, a digest of the source YAML, and whether ground truth
held. Every later phase loads it and refuses on mismatch.

That indirection is not ceremony. Without it each phase re-read the source YAML
independently, so a probe preflight replaced was still the probe `run` fired,
and the report labelled a present subject absent. The manifest is also what lets
`report` refuse an empty or stale artifact instead of rendering "0 / 0" and
exiting zero.

Exit codes are load-bearing on `postcheck`: `0` only when cleanup actually ran
and the absent half returned to zero rows; `3` for a dry run (nothing was
deleted, so nothing was demonstrated); `4` when residue remains — a real result
that selects AC-6's test-substrate branch, but not a success.

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

**The residue cleanup cannot undo**, in three measured classes:

| Class | Cause | Reversible? |
|---|---|---|
| `mutated_entities` | `last_seen` / `mention_count + 1` / `entity_type` on every mention (`service.py:1279-1283`) | No |
| `descriptions_filled` | consolidation populating a previously-**empty** description (FRE-711 fill arm) | No |
| `descriptions_rewritten` | consolidation **overwriting** an existing description | Yes in principle — FRE-711 archives the prior text to an `:EntityDescriptionVersion` node stamped with the causing trace |

On the absent half all three are nil by construction. On the present half they
are real, and AC-3 requires each be reported with its size rather than glossed.

**The absent half is checked against four substrates, not three.** Entities,
turns, owner-scoped **current claims**, and the Postgres message history. The
claim surface is the one that matters most: a `:Claim` carries its own
`content`, is written by consolidation on every turn, and is read back by
`search_memory` — so a subject present only there would have been reported
absent, silently invalidating the entire absent half.

**Eval mode does more than the ticket credits it with.** FRE-711's *correction*
arm carries `AND NOT ($eval_mode AND coalesce(_old_eval, false) = false)`, so an
eval-mode description can never overwrite a non-eval one — and the runner fires
every probe on `channel="EVAL"`, which sets `eval_mode` (`app.py:2111`). So the
`descriptions_rewritten` class is structurally suppressed for real descriptions.
The *fill* arm has no such guard, so `descriptions_filled` is live; with FRE-1115
measuring 18.7% of the corpus as empty-description, it is the class most likely
to come back non-zero.

**The run can invalidate real owner facts, and cleanup undoes that.** Asserting
a claim supersedes the claim it replaces — the write path sets `valid_to`,
`invalid_at` and `superseded_by` on the older one (`service.py:2677-2693`). So
deleting only the run's own claims would strand a genuine owner fact as invalid
with a dangling pointer. That is data loss rather than residue, so cleanup
snapshots those claims and restores them to current, and the report states how
many. A dry run restores nothing, which is one more reason it cannot attest to
restoration.

**Deletion refuses what it cannot prove.** Before anything destructive,
`postcheck --execute` verifies the session's turns all belong to the named owner
and that at least one carries a trace id the run recorded — a stale or
hand-edited artifact naming a real production session is refused, not deleted.
Every turn in the session must be the owner's *and* carry a trace id the run
recorded — not merely one of them. Entities are removed only when nothing
outside the probe session references them; a probe-created entity a later turn adopted is retained and reported by
name rather than destroyed along with that turn's edge.

`postcheck` measures all of this and records the substrate decision AC-6 turns
on: if cleanup restores the absent half, the FRE-1118 delta runs live on the
same probes; if it does not, the delta runs on the test substrate so the
comparison stays same-probe either way. Either way the report names the exact
probe identifiers the delta must reuse.
