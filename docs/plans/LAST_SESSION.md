# Last session — 2026-07-31 → 08-02 (the session that stopped fixing recall and measured it instead)

## Doing / discussing

Began as delivery (ES consolidation, an ADR seam, an ADR acceptance) and became, on the owner's report
that recall quality had "dropped drastically", an end-to-end investigation of the memory process. That
investigation — Fable in the explore seat, read-only — reframed the area and produced the remediation
programme now in flight across all three seats. **Pick that up: the owner asked explicitly that this
focus not be lost.**

## What was decided and why

**Master was wrong three times about which recall layer fails, and every correction came from the
owner rather than from master's own evidence.** First the item cap, then the substance of retrieved
items, then the inability to report absence. That track record is *why* FRE-1116's analysis was run
before any fix shipped, and it is the single most important thing to inherit. A fresh master will
look at recall, find the item cap of 5 stopping every turn with 60% of the token budget unused, and
conclude that is the answer. **It is not.** Do not re-derive it.

**The 455→205 token decline was deploy geometry, not decay.** It dates precisely to FRE-1061's
pair-split on 07-30: each retrieved row now emits two candidates, so the cap of 5 holds half-rows and
delivered tokens halved arithmetically. Answer quality through that window stayed good. Master
presented this as six-day degradation twice before Fable dated it. The same stale reasoning was
written into FRE-1114's body and has been withdrawn in a comment there.

**The dominant layer is absence/confidence, and it had no ticket until this session.** Three
mechanisms compose: subscore floors sum to ~0.355 against a 0.3 threshold so irrelevance is admitted
by construction; the relevance score never reaches the model; and `executor.py:2409` literally
instructs *"Do NOT say you have no memory."* Master verified that line in source. This is FRE-1118 and
it is the failure the owner actually experiences — a confident answer assembled from nearest
neighbours, indistinguishable at the surface from a correct one.

**A corruption loop was caught in the act.** Clafoutis's entity had a *good* description; it was
overwritten with self-referential text ("…in the memory search context") during the owner's own probe
turn. Querying a topic can destroy the knowledge stored about it — worst on topics asked about most.
This makes FRE-1115 a live degradation mechanism, not a static census, and is its first question.

**Three tickets needed the same missing signal, so it is designed once.** FRE-1118 (irrelevant vs
relevant), FRE-1119 (unreachable vs absent), FRE-1120 (embedder-failure vs empty) all need a way to
say "what I returned is not what you asked for". FRE-1120 was re-sequenced behind FRE-1118, and
build1 was told **mid-build** not to answer FRE-1119's third question. Its edge-coverage and
substring-filter halves remain fully its own.

**FRE-1109 is deliberately held unapproved pending FRE-1113.** The owner's diagnosis — too many event
types in one index — was verified correct *for agent-logs specifically* (health probes write
`status: "up"`, the ES tool writes `status: 400`), and wrong for the other three defects found that
day. FRE-1113's Tier-2 ingest pipeline would dissolve the collision per-producer and generalise to
the other 16 conflicting fields, so approving FRE-1109 first could commit to the weaker remedy.

**ADR-0129 was accepted as traces-only, narrower than the owner understood.** They expected
everything through the Collector; D5/D6 keep logs on the bespoke `es_logger` path, and the family that
stays is `agent-logs` — 98.8% of the corpus, not the 0.23% of product data they meant. Owner said "so
be it"; master did **not** read that as approval, held the PR, and merged only on their re-invocation.
Filed as FRE-1113.

**Five instrument errors in two days, one shape, and the fifth reached the owner.** FRE-1117 was
filed Urgent claiming primary cost booking broke and the owner's cap directive had gone inert. It had
not: `budget_reservations`, written *before* any cloud call, is empty for `main_inference` too and
matches `api_costs` at 4 and 4 — no cloud call was attempted, `primary` is pinned local. Master read
one side of a zero. The other four (decorator grep vs module-level marker; `_cat` 220 vs `_count` 4;
`pgrep -f pytest` substring hit; a guessed Neo4j schema) were caught first. **Check the instrument,
and the other table, before believing a surprising number — especially before escalating it.**

## Worktrees — anything special

- **explore** is deliberately **detached at a commit, not on a branch** — it was 59 commits stale and
  would have analysed undeployed code. Keep it pinned to deployed main for any future analysis.
- **build2** holds merged-but-undeleted branches, so `--delete-branch` fails on every merge. Known.

## Sequence position + drift

We deviated entirely into memory/recall — owner-directed and correct — but the console's standing
sequence (telemetry residuals → Config Management → Linear async feedback → Seshat Inference) is
untouched and unstarted.

## Answers for the fresh start

- **Why not just raise the item cap?** It is not the constraint — see above; tuning selection to choose better between empty pointers optimises the wrong layer.
- **Why is FRE-1118 behind FRE-1063 when it is the actual failure?** Its criteria need a probe whose
  answer is known *not* to exist — that comes from FRE-1063's battery; and its own body argues the
  prompt line changes **last**, since removing it without a confidence signal trades confabulation
  for hedging.
- **Why does FRE-1114 look overscoped?** It is. Its population dies when FRE-1115 fixes the single
  generator behind all 1,399 empties. Corrected on the ticket: a defensive invariant, not a remedy.
- **The clafoutis question is unresolved and should stay that way.** The owner is confident a
  recipe-adaptation conversation happened. Full-text search across 1,256 sessions / 4,888 messages
  finds no trace, and capture is *not* lossy (190/198 July turns present). Do not treat either the
  owner's recollection or its absence as settled.
- **`Awaiting Deploy` holds two.** FRE-1036's code half is verified; its ACs need the migration,
  blocked on FRE-1107/1109. FRE-1117 ships a monitor needing an ask-first gateway rebuild.
