# Last session — 2026-08-12/15 (four guards that reported green without guarding)

## Doing / discussing

Nothing in flight: no open PR, nothing in Awaiting Deploy, all gated work closed out. **build1 is free
and needs feeding** — the strongest unassessed candidate is FRE-1216, unchecked for criteria. build2 and
adr are mid-ticket. Four owner decisions are parked and listed at the bottom; none blocks work.

## What was decided and why

**The session's real finding is a pattern, not any one ticket: a guard whose name outruns what it
checks, four times.** An ast-grep rule matched only when another argument followed the retired kwarg,
so it caught 2 of 4 seeded cases. A vocabulary check asserted subset while its class name, docstring and
handoff all said equality. The ADR-0074 identity hook tests that a `trace_id` kwarg is *present*, never
that its value survives to the emitted document. The SDK-confinement guard matched only litellm while
its failure message claimed to confine "the model SDK" generally. **The unifying cause is that a clean
tree cannot distinguish a working guard from a vacuous one** — so every guard needs a seeded negative
proving it fires. FRE-1262 established that for its own instance; whether to generalise it is
dispositioned as an `adr` candidate, deliberately not settled inside one guard's repair.

**Master's own instruments were wrong three times, and re-measuring is what caught each.** A CI waiter
reported settled against a still-empty check rollup — an empty set satisfies a universal quantifier. A
post-deploy check found 1,504 documents carrying no retired field names, which measured nothing: every
one was background housekeeping and none of the affected event types had fired. And the miscount that
mattered most — the standalone gateway app was described to the owner as having *two* unique dormant
routers when it had one, because a stale inline comment called a WebSocket router an SSE endpoint and
that comment was read instead of the router. **The owner formed a view from that wrong summary**, so it
was corrected on the record before the retirement was scoped. Prior session logged the same lesson;
two sessions running makes it a property of the work, not an incident.

**A criterion written at dispatch can be invalidated by a ticket that merges before the build starts.**
FRE-1261's AC-5 said the SDK allowlist should end up empty — true when written, false ninety minutes
later once FRE-1262 added a second entry waiving a real, live, correctly cost-tracked import. Building
to the original wording would have turned the guard red on an unrelated module. Corrected before pickup.
Worth carrying: re-read a queued ticket's criteria against anything that merged since they were written.

**Escalation waivers should be sized by measured source surface, and the measurement hides the risk.**
Four escalated diffs were waived this session after sizing. The method that worked: take a
whitespace-blind diff to separate reindentation from logic — one 800-line diff was 271 real lines — and
then check the thing that measurement *conceals*. Wrapping existing writes in `try/finally` is a
control-flow change that presents as pure reindentation, so the useful check was counting genuinely new
`except` handlers (there were none) rather than trusting the reduced line count.

**An acceptance criterion decidable from a ticket's own deliverable will never ask whether anything
reaches the code.** FRE-1231 named an owner, carried four decidable criteria, passed every gate, and
instrumented an endpoint no deployment serves. This is the mirror of what ADR-0137 D3 now catches from
the other side, and it is why FRE-1205 was held at dispatch rather than built: same dormant module.

**Guard-before-deletion was sequenced deliberately, not stylistically.** The dormant module was the only
living violation of the SDK guard. Deleting it first would have left the widened guard passing cleanly
over a tree with no violations, making the hole invisible; fixing the guard first meant it could be
proven against a real violation. Widening it immediately surfaced a second live import nobody knew about.

## Worktrees — anything special

**adr** carries `context:keep` for FRE-1254 — that seat authored ADR-0137 and T1 is a transcription of
its Decision section into four contract documents, where paraphrase is the failure mode. Do not clear it.
**build1** is free and clean. **build2** is mid-ticket, nothing unusual.

## Sequence position + drift

The console's Observability directive governed the first half. The gateway-retirement arc that followed
was **owner-directed and off that directive** — a deliberate deviation, taken because the dormancy was
discovered at a gate and the owner decided retirement on the spot. No console write this session; the
file sits at 41 of its 60-line bound.

## Answers for the fresh start

- **Why is build1 empty?** Its chain finished. Not a stall — feed it.
- **Why was an ADR contradicted by the session that commissioned it?** FRE-1221 asserted no ticket
  inherited the OTLP-ingress obligation. False: the FRE-1043 mapping owns it at row D5.d with proving
  criteria named. The check that ticket proposed was a presence test, and a presence test passes that
  case. ADR-0137's split and sufficiency rules are what actually catch it. Do not re-litigate.
- **Why does FRE-1223 still sit In Progress since 2026-08-10?** Mac-side bookkeeping lag; closing it on
  FRE-1230's evidence has been recommended twice and not answered. Still the recommendation.
- **Is the Anthropic SDK really gone?** Yes — verified in the running container, not just the tree. One
  model-call path remains, through `llm_client/`, which is what makes the cost boundary meaningful.
- **Open for the owner:** FRE-1223 closure · the ADR status-drift sweep (ADR-0133 reads Proposed while
  its children shipped; ADR-0093's own text admits its status line is stale) · the PR #905
  security-review anomaly, where tool output carried an embedded instruction to withhold information ·
  whether the four-instance guard pattern above wants one generalised rule.
