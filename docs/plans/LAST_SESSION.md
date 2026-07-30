# Last session — 2026-07-29 → 07-30 04:00Z (the PWA render bug, and 16 hours lost to two dialogs)

## READ THIS FIRST

- **Environment is UP and healthy.** Gateway image built 2026-07-29 **09:35Z**, PWA **09:38Z** serving
  `seshat-v39-per-session-seq`. Health green, joinability probe green.
- **BOTH BUILD SEATS WERE STALLED ~16.5 HOURS** on interactive dialogs — unblocked at 04:05Z. Nothing
  merged between 11:31Z and 04:00Z as a result. **Check the panes directly at every prime**; the daemon
  reported `await in-flight` with zero warnings the whole time. See "Answers", first item.
- **One undeployed runtime change: FRE-1041.** Ask-first gateway rebuild, no migration. Authorisation
  requested 07-29 10:36Z, **not yet given**. Runbook on the ticket.
- **`cloud-sim-embeddings` is stopped** — it revives on every rebuild. Standing rule.
- **Postgres migration 0023 applied** (per-session event seq). Idempotency guard *proved* by re-run.
- **Treat every `agent-logs` count as provisional** — ES loses events (FRE-1051, in progress).

## Doing / discussing (≤5 sentences)

The PWA had been silently unusable since 26 June — responses arrived, persisted, and never rendered.
Root cause: the transport numbered events from **one global Postgres sequence** while the client
dispatches only a *contiguous* run per session, so two live conversations holed each other by
construction. Fixed, deployed, verified live, closed. Three master theories were wrong first (version
skew, a connection leak, "nothing verifies state") and every correction came from the owner or codex.
Two deeper things fell out: a proper-noun detector gating entity recall (FRE-1041, merged) and
**Elasticsearch silently losing up to 83% of an event type on some days** (FRE-1051, now being worked).

## Commits — the story behind the last 10

- **#742 → FRE-1040** (per-session seq). **Codex overruled master's recommended fix** — the bounded flush
  would have re-introduced what FRE-590 removed. Shipped design forces one reconnect with the watermark
  *untouched*, releasing the buffer only on a new `REPLAY_COMPLETE` marker.
- **#743 → FRE-1041** (graph-anchored entity hints). Codex **overturned the root cause master wrote in
  the ticket**: the entity path *was* entered on the melon turn; `Melon` scored 0.563 and lost the fifth
  slot **by 0.002**. A rank race, not candidacy. This *unifies* FRE-1021 and FRE-1041.
- **#739 → FRE-989** (cost attribution). Nine findings; F9 = **every gateway streaming turn committed
  $0** (litellm carries no `anthropic/`-prefixed pricing keys).
- **#744** — explore's convergence study. **Read it.** It attacked master's hypothesis and beat it.
- **#740 → ADR-0128** merged **Proposed**. Do *not* flip to Accepted without the owner.

## Worktrees — anything special

- **build (build1)** — **FRE-1051** *(ES event loss)*, Urgent. Was stalled on a permission dialog for a
  **read-only** `docker exec … psql … SELECT`; approved at 04:05Z, now working. **It has already found
  something that invalidates both hypotheses master put on the ticket:** whole hours lose 100% while
  adjacent hours lose 0%, which is neither GC-scatter nor a circuit-breaker blackout. Wait for its
  handoff before theorising.
- **build2** — **FRE-927** *(seat-keyed failure counter)*. Was stalled at a **plan gate** after a clean
  codex review (5 questions, 0 issues). Master approved **option 1** — counter + the §3.3 reason split +
  the one narrowed FRE-923 assertion — because dropping §3.3 would preserve exactly the defect the
  convergence study identifies (diagnostics describing the branch taken, not the state observed).
- **adrs** — **idle and will stay idle**: FRE-1043–1050 are all Needs Approval.
- **PR #738 (FRE-1015) is deliberately DRAFT and now also CONFLICTING.** Bounced because its AC tests
  stubbed `query_memory` to return the target entity and then "checked the precondition" by looking for
  it — validating the stub three lines above. It now conflicts with main in **four files** because
  FRE-1041 replaced the entity-resolution path it rides on. **Order: deploy FRE-1041 → measure → then
  rebase and rewrite the tests.** Not the other order.
- **`master-914`** — stale on the closed `fre-909-seat-rename`. Harmless.

## Plan position + drift

MASTER_PLAN is accurate and its header is de-narrated (36 lines). One deliberate deviation not to
re-litigate: **FRE-1015 is parked by removing its stream label, not by a `blockedBy` relation** — master
tried the relation first and it did nothing, because workers are instructed to treat a relation to a
terminal blocker as cleared. Parking a ticket a stream is holding also leaves the daemon holding its
slot (FRE-1054); check the release.

## Answers for the fresh start

- **A stalled seat is invisible. Check the panes at every prime and wind-down.**
  `tmux capture-pane -p -t cc-1build -S -8`. A line ending `Do you want to proceed?` or
  `Enter to select · ↑/↓ to navigate` = stalled. Cheap independent tell: no merged PR for hours while
  Linear shows `In Progress`. Bare **Enter** selects the highlighted menu option; typing the digit puts
  text in the box. **Read the dialog before answering** — never approve blind.
- **Master was wrong three times, and every correction came from outside.** The specific recurring trap:
  **a snapshot cannot answer a "what changed" question.** Master grepped current source, saw
  `AsyncElasticsearch` everywhere, reported "all async, nothing to see" — and walked past FRE-1034,
  which had made that call async *the previous day*. The evidence was in the diff.
- **Two of three stream stalls on 07-29 were master's own doing** — a PR *title* containing `FRE-986`
  dragged that ticket backwards and blocked build2; parking FRE-1015 left the daemon holding its slot.
  Both now filed as the class (FRE-1011 rescope, FRE-1054) rather than the instance.
- **The owner switched the Linear on-PR-opened automation off on 07-29.** Team settings → Issue statuses
  & automations. That ends a 12-occurrence class at source, and it makes **FRE-1011's guard the wrong
  layer** — rescope it to a warning or close it.
- **FRE-989's F9 is UNVERIFIED, not passed.** Every post-deploy primary ran on local qwen at zero cost,
  so nothing priced was recorded. Needs **one turn on a cloud primary**. Role distribution needs ~7 days.
- **`main_inference`'s caps are reachable by streamed chat for the first time**, and that lane denies
  with `raise` → user-facing 503. If one fires, ask whether the spend is real. **Do not raise the cap.**
- **FRE-1053 is the sting under the recall work:** every episode row carries a free **+0.020** from an
  empty-name bug in the topic subscore — **10× the 0.002 margin** that decided the melon turn.
- **Awaiting owner approval:** FRE-1043–1050 (blocks the adrs seat entirely) · FRE-1051 · FRE-1053 ·
  FRE-1054 · the FRE-1011 rescope · and the **FRE-1041 deploy**.
