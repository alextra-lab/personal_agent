# Last session — 2026-07-29 (an all-nighter: the PWA was unusable, and it took three wrong theories to find out why)

## READ THIS FIRST

- **Environment is UP and healthy.** Gateway image built **09:35Z**, PWA **09:38Z** serving cache
  `seshat-v39-per-session-seq`. Health green, joinability probe green.
- **One undeployed runtime change: FRE-1041.** Ask-first gateway rebuild, authorisation requested at
  10:36Z and **not given**. Full runbook is on the ticket.
- **`cloud-sim-embeddings` is stopped** — it revived on all three rebuilds today. Standing rule.
- **Postgres migration 0023 applied** (per-session event sequence). Idempotency guard was *proved* by
  re-running it at the only safe moment, not assumed.
- **Treat every `agent-logs` count as provisional** until FRE-1051 closes — see below.

## Doing / discussing (≤5 sentences)

The PWA had been silently unusable since 26 June: responses arrived, persisted, and never rendered, and
only switching conversations recovered them. It took **three wrong theories from master** — a version
skew, a connection leak, and "nothing verifies the state" — before the real cause landed: the transport
numbered events from **one global Postgres sequence** while the client dispatches only a *contiguous*
run per session, so two live conversations holed each other by construction. That is fixed, deployed and
verified. Along the way the owner's challenges cracked open two deeper things: a proper-noun detector
gating entity recall, and **Elasticsearch silently losing up to 83% of an event type on some days**.

## Commits — the story behind the last 10

- **#742 → FRE-1040** (per-session seq). **Codex overruled master's recommended fix** — the bounded
  flush would have re-introduced exactly what FRE-590 removed. The shipped design forces one reconnect
  with the watermark *untouched* and releases the buffer only on a new `REPLAY_COMPLETE` marker. The
  build also caught a self-inflicted regression: at `ackSeq===0` its own timer would have destroyed the
  buffered response, **breaking the one path that still worked.**
- **#743 → FRE-1041** (graph-anchored entity hints). Codex **overturned the root cause master wrote in
  the ticket**: the entity path *was* entered on the melon turn; `Melon` scored 0.563 and lost the fifth
  slot **by 0.002**. It is a rank race, not candidacy. This *unifies* FRE-1021 and FRE-1041.
- **#739 → FRE-989** (cost attribution). Nine findings, four found by the audit itself and one by the
  adversarial review *of the fixes*. F9: **every gateway streaming turn committed $0** — litellm carries
  zero `anthropic/`-prefixed pricing keys, verified independently by master.
- **#744** — explore's convergence study. **Merged; read it.** It is the most useful document produced
  here in weeks.
- **#740 → ADR-0128** merged **Proposed** (telemetry naming). Do *not* flip to Accepted without the
  owner.

## Worktrees — anything special

- **build (build1)** — **FRE-1051** *(ES event loss)*, dispatched 09:53Z. Urgent.
- **build2** — **FRE-927** *(seat-keyed failure counter)*, started 10:44Z.
- **adrs** — **idle**, and will stay idle: FRE-1043–1050 are all Needs Approval.
- **explore** — free; delivered the study, then was cleared and re-tasked.
- **`master-914`** — still stale on the closed `fre-909-seat-rename`. Untouched, harmless.
- **PR #738 (FRE-1015) is deliberately a DRAFT.** The implementation is sound; it was bounced because its
  AC tests monkeypatched `query_memory` to return the target entity and then "checked the precondition"
  by looking for that entity — **validating the stub three lines above.** Do not un-draft it until
  FRE-1041 is deployed and its effect measured.

## Plan position + drift

MASTER_PLAN was consolidated in #745 and is accurate. One deliberate deviation worth not re-litigating:
**FRE-1015 is parked by removing its stream label, not by a `blockedBy` relation.** Master tried the
relation first and it did nothing — workers are explicitly instructed to treat a relation to a terminal
blocker as cleared. **A `blockedBy` does not hold a ticket. Removing the stream label does.**

## Answers for the fresh start

- **Master was wrong three times last night, and every correction came from outside.** The owner supplied
  the decisive observation on the render bug ("new conversation works once, existing ones always fail")
  and caught the entity-hint heuristic; codex overturned two root causes. When a theory feels complete
  at 5am, it is probably a snapshot answering a different question than the one asked.
- **The specific trap, twice: a snapshot cannot answer a "what changed" question.** Master grepped
  current source, saw `AsyncElasticsearch` everywhere, reported "all async, nothing to see" — and walked
  past FRE-1034, which had made that call async *the previous day*. The evidence was in the diff.
- **Two of three stream stalls last night were master's own doing.** A PR *title* containing `FRE-986`
  dragged that ticket backwards and blocked build2; parking FRE-1015 left the daemon holding its slot.
  Both are now filed as the class (FRE-1011 rescope, FRE-1054) rather than the instance.
- **The fre-token rule in memory is INCOMPLETE.** PR #416 proves a bare prose cross-reference in a PR
  **body** pulled a *Done* ticket backwards 3.4s after opening. Branch and title were never sufficient.
  The owner switched the Linear automation off on 2026-07-29 — confirm which levers if it matters.
- **Elasticsearch loses events.** 82.6% on 07-23, 47.8% on 07-26, 52.4% on 07-27, zero on three other
  days. Episodic. **A clean zero from ES now has three indistinguishable causes** — no data, wrong field
  name, or emitted-and-lost. Prefer Postgres, journald and git as evidence sources (FRE-1051).
- **FRE-989's F9 is UNVERIFIED, not passed.** Every post-deploy primary ran on local qwen at zero cost,
  so there was nothing priced to record. It needs **one turn on a cloud primary**. Its headline criterion
  needs ~7 days regardless.
- **`main_inference`'s caps are reachable by streamed chat for the first time**, and that lane denies
  with `raise` → user-facing 503. If a denial appears, ask whether the spend is real. **Do not raise a
  cap to make it stop.**
- **Awaiting owner approval:** FRE-1043–1050 (the ADR-0128 chain, blocks the adrs seat entirely) ·
  FRE-1051 · FRE-1053 · FRE-1054 · the FRE-1011 rescope · and the FRE-1041 deploy.
