# Last session — 2026-08-04 → 08-05 (the session that kept mistaking its instruments for evidence)

## Doing / discussing

Seven tickets closed and two production deploys, then the evening turned on a live incident: Seshat
greeted the owner as **Susan**, a different registered user. That is now FRE-1150, re-scoped by build2
after it corrected master's diagnosis. The FRE-1122 baseline is parked at nine of twenty turns — the
probe set is finished and verified and does **not** need re-authoring. Pick up FRE-1150's gate and the
owner's `Awaiting Deploy` decision on FRE-1114.

## What was decided and why

**The owner's ruling that collapsed three tickets: stop maintaining application-log history.** Master
tested it before agreeing and the corpus supported it — the log corpus already had a 47-day hole, so
longitudinal analysis was impossible anyway, and the family's own retention policy is 30 days, so the
migration was fighting to preserve data our own accepted decision would delete. FRE-1036 therefore
closed **by deletion, not migration**. FRE-1109's premise vanished with it and was re-scoped to a
template fix; FRE-1113's tier-two lost its near-term justification. One decision, three tickets simpler.

**Master's dominant failure mode this session, four occurrences: reading a negative result from an
unverified instrument as a finding.** The worst was the identity incident. Master searched 3,556
captures for the operator stanza's rendered text, found zero, and concluded it had never worked — but
**captures store `system_prompt_chars` and never hold prompt text**, so that query returns zero by
construction. build2 caught it in six minutes by reading prompt geometry instead. The real cause: the
stanza *was* present in the cached prefix; ADR-0081's volatile-tail layout inlines the memory section
into the current user turn, so the recalled identity claim sat nearer the query and won. **Override,
not gap** — which is why it survives FRE-674 and earns its own ticket.

**Two PR bounces that were vindicated, and one deliberate departure.** #809 was bounced twice — first
for citing integration tests as evidence when the module's `requires_llm_server` marker made them skip
everywhere including CI, second for a backfill script defaulting to writing to production. Both were
real: unskipping exposed four assertion bugs including one that made a test pass *vacuously*, and the
seat had in fact already written to production ES at 15:50 during its own iteration. Conversely #810
was **merged despite no self-review summary and no codex review on a `src` memory diff** — master
substituted its own review, stated as a judgment call against the letter of the backstop, because the
diff was 23 lines and strictly subtractive. Both calls are defensible; both are worth knowing were made.

**Master recommended superseded work twice in one day.** FRE-1049 was cancelled as superseded by
ADR-0129 — and master had recommended approving it hours earlier without checking the ADR governing its
seam. Then master proposed bolting CF service-token headers onto FRE-1122, which ADR-0132 D1 retires.
Same root: reading a ticket or a codebase precedent without checking the decision record above it.

**The identity leak is not news, and the owner said so.** ADR-0064 recorded cross-user memory sharing
as an *accepted risk* in April under a trusted-group premise, with the three-level private/group/public
model designed and deliberately deferred. That premise expired when a second user's session produced an
entity asserting the user's name. Master over-weighted the framing and reported it as discovery.

**Owner's scope discipline, explicit: fix user session identity only.** Additional features and
controls come when the owner chooses. Do **not** escalate FRE-674 or expand FRE-1150.

**Owner's design steer for FRE-1150:** the connected user's identity is a **static instruction in the
cached portion** of the prompt — it never varies, being derived from the authenticated user — and must
be authoritative over any identity claim arriving through recall. Not to be solved by moving the memory
section or re-ranking recall.

**Eval harnesses reaching a local model must run across the tunnel.** Master instead stood up a second
agent service on the VPS pointed at a model endpoint that only exists on the owner's machine, and burned
an hour on it. The existing pattern was in the repo the whole time.

## Worktrees — anything special

- **build2** holds FRE-1150. Its branch name no longer matches the re-scoped title and it was told to
  recreate it; there were zero commits, so nothing to unwind.
- **explore** stays pinned to the **deployed** SHA, not `origin/main`, for any analysis.

## Sequence position + drift

Still entirely memory/recall + context, owner-directed. The console was **not touched** this session —
38 of its 60 lines, contract intact. The standing four-item sequence (telemetry residuals → Config
Management → Linear async feedback → Seshat Inference) remains unstarted, which is drift only in the
sense that the recall work keeps earning priority over it.

## Answers for the fresh start

- **Why is FRE-1114 sitting in `Awaiting Deploy`?** It is merged but its deploy is a gateway rebuild —
  ask-first. Master asked; the owner has not authorized. Waiting on the owner, not on work.
- **Why is the FRE-1122 baseline parked at 9/20?** Three fixture gaps, all recorded there: no auth path
  on the run phase, all twenty probes share one session (so probe N answers inside probes 1..N-1), and a
  hardcoded 300s per-turn ceiling that one legitimate five-tool-iteration turn exceeded by 18 seconds.
  Turn latency itself is **fine** across the tunnel — median ~40s. The probe set is done: 10/10, zero
  rows on every absent subject, digest `abdd70fb4bc6`.
- **Is the corpus polluted by the failed eval runs?** No. Eleven turns landed, all from *present*
  probes. No absent probe ever fired and all ten absent subjects re-measure at zero. Baseline integrity
  is intact.
- **Was FRE-998 really fixed?** Half. Its write path works — every turn since 07-31 carries `user_id`.
  Its backfill never ran: 2,288 of 2,325 historical turns still have none, and master closed it Done
  with that criterion unmet. Recorded on the ticket, not silently reopened.
- **Why is the owner's patience thin?** Every thread this session spawned more work than it closed, and
  master reported each blocker serially as it was hit rather than tracing the whole path first. That is
  a working-style problem to correct, not a property of the codebase.
