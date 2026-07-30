# Master Plan — Personal Agent

> **Forward plans only.** What we are going to do, in order. **Not a diary of accomplishments — that is
> the git log.** No history, no state narrative, no post-mortems. What shipped → `git log`; why a
> decision was made → the Linear ticket; this session's decisions → [`LAST_SESSION.md`](LAST_SESSION.md);
> per-ticket state → [Linear](https://linear.app/frenchforest).
> **Last updated**: 2026-07-30

## 0. In flight

- **build1** — free.
- **build2** — head is **FRE-1015**. PR #738 needs **re-baselining, not merging** (46 commits behind;
  its tests were written against the pre-split candidate stream). FRE-1062's mentioned-entity pin makes
  its entity-selection precondition satisfiable **by construction** on any turn naming the stance target.
  Then FRE-1017 → FRE-1019 closes ADR-0126.
- **adrs** — head is **FRE-1043** (ADR-0128 chain head, approved 07-30). FRE-1044–1050 still Needs
  Approval behind it.
- **explore** — free.

**Owner actions, blocking:**
- **ADR-0123 closes on three owner turns** — a multi-minute artifact build, the same cancelled
  mid-build, and a short no-tool question. All four children merged and deployed; AC-7 is the only
  thing left and master cannot fire the turns.
- **FRE-989 F9 needs one turn on a *cloud* primary**; local-qwen turns prove nothing (zero cost ⇒
  nothing priced). Its role-distribution criterion needs ~7 days.

**Master's own queue:**
- **FRE-927 verification** — deployed 07-30 05:38Z. The observable signal is the **reason split**
  (`board-churn` vs `owner-acted` now tallying separately), not the alert. **A quiet alert is the steady
  state and is not proof it works.**
- **Watch `main_inference`'s caps** — reachable by streamed chat for the first time, and that lane denies
  with `raise` → user-facing 503. If one fires, ask whether the spend is real; do not raise the cap.

## 0b. Recall — the entity path is fixed; what is NOT yet measured

Entities now reach the model on a turn that names them (FRE-1041/1060/1061/1062, all Done, verified
live 2026-07-30). **Do not re-open the mechanism** — five explanations preceded the real one and the
history is on those tickets.

**Open, and genuinely unmeasured:**
- **Is the entity payload load-bearing?** On the proven turn the answer's substance came from the
  *episode*; the entity carries only name/type/description. Nobody has tested whether admitting the
  entity improves an answer. Measure before building further on the assumption that it does.
- **FRE-1053** — the empty-name topic subscore is a real arithmetic bug, but its "decisive for the melon
  turn" claim is dead. Approve **re-scoped small**, without the census-re-run obligation.
- **FRE-1021's census** must be re-run `--since` the 2026-07-30 deploys. Pre/post candidate populations
  are not comparable (`candidate_count` changed meaning) and a higher participation rate is **not**
  improved recall — it is the same recall with a fuller record.

## 1. Elasticsearch structure — hold LIFTED 2026-07-29

**ADR-0128** *(one telemetry naming convention, enforced at emit)* merged **Proposed**, so the convention
that gated **FRE-1036** is settled. The remaining coupling is narrow: only **FRE-1043** *(rename table)*
must land first — deliberately standalone, days-scale. Late table ⇒ a second reindex; expensive in
machine time, cheap in risk on 635 MB.

**FRE-1036 has an unrecorded scope boundary.** `slm_server` formats its own dated index names
client-side, so rollover-behind-a-write-alias cannot reach it. Verified: **39 `slm-requests` indices,
newest today, 6.6% of 594 primary shards** — the #2 consumer, growing 1/day. FRE-1049 carries the
one-line fix. **Do not block FRE-1036 on it** (approved + deadline behind unapproved); instead state
slm-requests as excluded so the post-completion shard metric isn't misread as underdelivery.

Correction to this plan's own record: it said **four** timestamp spellings. It is **six** — `ts`
(slm-requests) and `rated_at` (user-turn-ratings) are declared `date` fields invisible to a name-based
scan. That undercount is the exact failure ADR-0128 exists to eliminate.

**ADR-0128's chain — FRE-1043–1050, all Needs Approval**, sequenced: 1043 head → 1044/1045/1049/1050
→ 1046 → 1047/1048. Seam stays with the adr session; the ADR does not close when its last child merges.

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

**Twelve in Awaiting Deploy, all of them deployed.** The column name misleads: what they await is master's
acceptance verification, not a deploy. None closes on "deployed and healthy" — each needs its own
criterion proven, and **UNVERIFIABLE is a first-class verdict**.

**Verify from the substrate before asking for an owner turn.** FRE-970 was closed 2026-07-28 on captures
already in ES — five spend-query turns straddling the deploy gave a same-model before/after. The
"needs an owner turn" note on a row is a hypothesis, not a fact; check `agent-captains-captures-*` first.

| ticket | subject | what it awaits |
|---|---|---|
| **FRE-989** | cost attribution audit | **F9 UNVERIFIED** — needs one turn on a *cloud* primary; all post-deploy primaries were local/zero-cost. Role distribution needs ~7 days |
| **FRE-1021** | entity-kind fused items resolve to entities, not their turns | **re-read against §0b.** Its premise was the rank race, now named. The census re-run is still owed, but no longer gates anything |
| **FRE-1037** | widen the LLM role enum, thread the real role, fail closed | **~7 days of post-deploy traffic** before the role distribution is comparable to the 93%-primary baseline. Slowest item in this column |
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

**FRE-1015 is parked** (Approved, stream label removed) with an open **draft** PR #738 carrying sound
implementation. It was bounced 2026-07-29: its AC tests monkeypatched `query_memory` to return the target
entity, then "checked the precondition" by looking for that entity — **the check validated the stub three
lines above it and could never fail**, which is the one behaviour ADR-0126's INCONCLUSIVE clause exists to
produce. Now `blockedBy` **FRE-1041** (merged, undeployed), not FRE-1021.

**Parking = remove the stream label.** A `blockedBy` relation does *not* hold a ticket — workers are
instructed to treat a relation to a terminal blocker as cleared. Master tried that first and it failed.

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

40+ at Needs Approval, ~80 Approved and mostly parked, including P0s months old — FRE-940
(replayed approval cards) chief among them; FRE-927 and FRE-867 have now moved. Method: verify per cluster, cancel the provable with a one-line reason, bring
judgment calls to the owner. Run `scripts/reconcile_board.py` first.

## 8. Pipeline hardening — now has a theory, not just a backlog

**The convergence law** (`docs/research/2026-07-29-why-pipeline-point-fixes-do-not-converge.md`), from
mapping each shipped fix to its failure and checking recurrence after its merge date:

> **Fixes that removed the operation that could fail converged. Fixes that improved or observed an
> inference did not.** Two held with zero relapse; four did not. The line is not tier or effort.

Use it as an acceptance test on every proposed fix here: *does this remove an inference, or observe one
better?* Master's "nothing verifies the state" framing was **half wrong** — verification was added four
times and the pipeline still stalled. FRE-927 is the proof: two observers compose into a *gap*.

- **FRE-1054** *(filed)* — the worked specimen: the daemon determines a fact correctly, discards it, and
  logs its negation. `reason=` names the **branch taken**, not the state observed, so every fix authored
  from a reason string is authored against a fiction.
- **FRE-927** — **merged, awaiting deploy.** Seat-keyed rather than ticket-keyed; the only filed ticket
  that measures the seat. Two findings worth carrying forward. **Under churn a dispatch attempt costs
  two ticks**, not one — the dispatch, then a tick spent clearing when NEXT changes underneath it — so
  the silent-failure window is slower and harder to notice than the ticket described. And the FRE-922
  wedge helper still carries the **same equality-crossing defect** the build fixed in its own code
  (`count == wedge_ticks + 1` never fires for a negative `--wedge-ticks`); left deliberately, since
  AC-3 required FRE-922 unchanged, and not ticketed because the trigger is operator error.
- **The three reconcilers now want consolidating.** `run_once` carries six parameters that are one
  concept (wedge counts/ticks, held latch/age, delivery failures/threshold), and three long constant
  comments each independently re-derive the same in-memory-vs-persisted rationale. A mutable
  `SeatHealth` plus a frozen `SeatHealthPolicy` collapses six to two. Unticketed on purpose — this is
  the shape to adopt when a fourth reconciler is proposed, not a refactor to schedule on its own.
- **FRE-1011** — **rescope to a warning or close.** Its guard infers a docs branch from a prefix to dodge
  an automation the owner switched off in team settings today. It also could not have caught PR #416,
  whose trigger was a **prose cross-reference in the body** — so "no token in branch or title" was never
  a sufficient rule.
- **FRE-975** (gate master on review-complete) · **FRE-977** (explore first-class dispatch).
- **FRE-867 — a seat blocked on a dialog is invisible, and it just cost 16.5h × 2 seats.** On 2026-07-30
  both build seats had sat since ~11:30 the previous day: build1 on a permission prompt for a
  **read-only** `docker exec … psql … SELECT` (`Bash(docker exec:*)` is not allowlisted), build2 at a
  plan gate. The daemon logged `await reason=in-flight` throughout with **zero warnings** — the same
  branch-not-state defect as FRE-1054. FRE-911's `acceptEdits` covers file edits only.
  **Allowlist half shipped** (PR #749): a PreToolUse hook allows a psql invocation only when the whole
  command is provably read-only, rather than a per-container rule that would also authorize DROP
  against production. It is the *shape* that is allowlisted, not the container. **Now dispatched to
  build2 is the robust half** — the watcher detecting a parked seat and surfacing it to master.
  **Three bypasses were found before it was right, and all three are the same mistake — a denylist of
  the bad thing where an allowlist of the good was needed.** `SELECT ... INTO` creates a table while
  reading as a projection (missing from a keyword denylist); psql's `-o`/`-L`/`-v` write files or
  expand a variable into the statement *after* validation (missing from a flag denylist); and a
  **newline** separates commands while `shlex` treats it as whitespace, so a second line landed inside
  an already-approved segment and bypassed the `rm` ask-rule (missing from a separator denylist). The
  first two came from automated review; **the third came from master re-attacking its own merged work
  at the gate**, which is the only reason it was caught. The flag set is now an allowlist and multi-line
  commands are refused outright. **The keyword list is still a denylist** — the remaining soft spot,
  and the one place a full allowlist is impractical short of parsing SQL.
- **Master-authored changes to `src/` or a security boundary have no independent gate.** The hook
  shipped with a live bypass because master was both author and reviewer. Route them through a build
  seat. Open question, not ticketed.
- **Four mutually inconsistent readiness oracles** exist in `scripts/dispatch/`, two documenting their own
  unreliability in their own headers. The wedge alarm is *defined* as a disagreement between two of them,
  which is why it logs 14 warnings and escalates nothing. **Finishing the ADR-0116 channel migration** —
  so "did this land?" is an acknowledgement, not a guess about terminal rendering — is the largest single
  move available and larger than anything currently ticketed.

**Recommended ADR, two decisions** (not the one master framed): **D1 decision provenance** — every control
decision is a function of *named facts logged with it*, and an undeterminable fact is `UNKNOWN`, never
`False`. **D2 transition ownership** — the integration owns exactly one transition (on-merge →
Awaiting Deploy); every other is master's. FRE-1011 becomes D2's first child.

## 9. Then, in order

Telemetry residuals (FRE-983 ES lifecycle, parked mid-phase) · Configuration Management ·
Linear async feedback · Seshat Inference.

---

## To fix, unscheduled

- **ES silently loses up to 83% of emitted events on some days** — **FRE-1051**, now build1's head.
  Measured against the Postgres oracle: 82.6% loss on 07-23, 47.8% on 07-26, 52.4% on 07-27, zero on the
  other three days. Episodic, not sampled. **ADR-0090 has no delivery corner** — all its "silently
  dropped" references are *mapping* drops; nothing asks whether the event arrived. Cost is only where it
  surfaced, because it is the one event type with an independent oracle. **Treat any agent-logs count as
  provisional until this closes**, including figures master published this week.
- **Nothing watches a threshold approaching.** Three hard-threshold cliffs surfaced on 2026-07-28 — the
  log corpus, the container memory limit, the ES shard ceiling — and none had a monitor. Owner priority
  puts monitors second to bugs; this is the standing note that the class exists.
- **The 2 GiB gateway memory limit is no longer load-bearing** — a real turn peaks at 654 MiB, inside the
  original 768. Revert deliberately after a few days of traffic, with sampler evidence.
- **A local-qwen primary fabricated a spend report rather than reporting retrieval failure.** One
  instance, 2026-07-25 06:59Z, on the pre-FRE-970 image: zero tool calls, invented budget IDs and
  October-2023 dates, presented as live data. The trigger observed is fixed and three post-deploy turns
  behave; the *class* — invent rather than admit — has no guard. Owner's call whether to ticket.
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
