# Last session — 2026-09-04 → 05

## Doing / discussing  (≤5 sentences)
The night's spine was the **sub-agent chain**: repair the expansion path, make its output
observable, then prepare it to hold tools. Five tickets shipped and deployed — FRE-1379,
FRE-1380, FRE-1387, FRE-1390 and the FRE-1377 study — and FRE-1375 closed on the owner's own
live cancel. The owner's queries became the verification instrument, and twice by accident they
produced fresh evidence for FRE-1288. The live thread at reset is **the primary tool loop**,
which three separate turns showed is now the dominant cost. FRE-1388 carries the owner's
`run_python` grant decision and is build1's head.

## What was decided and why

**`run_python` is the sub-agent's first tool grant, and the strawman was inverted.** FRE-1388
filed `run_python` under "blast radius is the machine". Reading `tools/primitives/sandbox.py`
shows otherwise: `--network=none`, `--read-only`, `--cap-drop ALL`, non-root 1000:1000,
`no-new-privileges`, 512 MB with swap capped equal, one CPU, an ephemeral container, and one
writable mount that is a scratch directory rather than the repository. **No network is the
decisive property.** The risk this ticket manages is a model-authored instruction running
unattended; a tool that cannot reach the network cannot exfiltrate whatever that instruction
says. That makes `run_python` safer than `web_search` and `fetch_url`, which the strawman called
plausible and which pull untrusted content into a turn nobody reviews. Owner accepted, scoped to
step one. The read-only set follows once the loop is proven, because that is where the
FRE-1138 payoff is.

**Sub-agents hold no tools in ALERT or DEGRADED.** Added to FRE-1388 on owner direction.
`run_python` declares `requires_approval_in_modes: ["ALERT", "DEGRADED"]`, and a sub-agent runs
unattended, so in those modes there is nobody to approve and the request has no correct outcome.

**The primary tool loop is the real defect, and it was measured three times in one evening.**
Not expansion, which now works. Trace `515625b3`: three sub-agents finished in 91 s with clean
digests, then the primary made its own `web_search` and `fetch_url` calls and grew from 29,527
to 54,639 to 76,706 input tokens across three rounds, hit the 900 s deadline, and returned a
**160-character reply**. Trace at 20:19: 207.8 s total, of which planner 15.5 s, dispatch 27.0 s,
`span_extraction` 16 s — and **144 s, 69 percent, in the primary's own loop**. FRE-1389 owns
this and is blocked only by FRE-1388.

**A fourth inert knob, same family as the other three.** FRE-1390's first commit changed only
the `role=` kwarg on `.respond()`. That kwarg is a telemetry label; `LiteLLMClient` fixes its
deployment at construction, so the planner would have kept generating on the thinking-disabled
deployment. The build seat caught it in its own review and fixed it properly with two distinct
clients. Same shape as `extra_body`, `worker_timeout_seconds` and the catalog `max_tokens`.

**FRE-1390's measured cost was 1 second, not the predicted 7 to 15.** The fixture harness
reported planner median 13.86 s to 20.96 s. On real turns it went 14.5 s to 15.5 s, with output
tokens 389 to 501 — the extra tokens being the reasoning now present. Both arms are n=1, so this
is a data point rather than a contradiction. Do not quote the harness figure as the live cost.

**ADR status headers are the close gate, not the merge.** Clearing the Awaiting Deploy backlog,
four tickets closed on verified evidence and **FRE-1328 did not**. Its deliverable ADR-0139
reads "Proposed — partially withdrawn", and the withdrawn sections D2, D3 and D7 are exactly the
ones answering its questions. ADR-0141 by contrast reads "Accepted — 2026-09-03 (owner)". Read
the header; a merged PR is not a settled decision.

**Master's own instrument errors, both caught and both worth repeating.** A `size: 40` ES query
sorted ascending truncated the newest events, and master told the owner a model call was "still
running at 464 s" when it had completed 328 s earlier. Separately, a cancel-path query returned
zero because the FRE-1375 fix logs `status="user_cancelled"` on `step_planning_completed` rather
than emitting a new event name — the negative result was the instrument, again.

**Haiku 4.5 cannot run auto mode.** The dispatch daemon switched build1 to Haiku for a
`Tier-3:Haiku` ticket and stranded the seat in manual mode, where it cannot work. FRE-1388 was
retiered to `Tier-2:Sonnet`, which is honest anyway — it needs a governance principal, a
deny-by-default and a seeded negative, not a config edit. **Any Tier-3 ticket can strand a seat
this way.** The seat model is pinned per seat in `~/.claude/cc-sessions.conf`, so a restart
restores it, but `cc-master` is pinned to `-` and would inherit a changed default.

## Worktrees — anything special
`build2` holds `fre-1367-delete-localllmclient` with two WIP commits, rebased onto main and
clean. `build` holds the merged `fre-1390` branch. `adrs` is on `fre-1288-adr-intent-taxonomy`
and working. `fre-1370-query-paraphrase-pinned-role` still carries its single unpushed WIP
commit `7aeef49a` for a cancelled ticket; it is the only copy.

## Sequence position + drift
The owner fast-tracked the sub-agent chain ahead of the Approved queue on 2026-09-04, so
FRE-1387, FRE-1390, FRE-1388 and FRE-1389 run before everything else. FRE-1382 is deliberately
sequenced after ("ceiling after"), and FRE-1383 to FRE-1386 stay `Backlog` pending the owner's
read of the router study. **The Observability Foundation directive remains unstarted**, now for
a second consecutive session.

## Answers for the fresh start
- **What is build1 doing?** FRE-1388, the `run_python` grant. Then FRE-1389, then FRE-1381 at
  Medium. The chain is the priority.
- **Why is FRE-1328 still at Awaiting Deploy?** ADR-0139 is `Proposed`, not `Accepted`, and the
  parts answering the ticket were withdrawn. It needs an owner decision, not a state change.
- **Is the expansion path fixed?** Yes, and verified on live turns: digests at
  `truncation_ratio` 1.0, non-overlapping dispatch intervals, planner on the thinking role.
  What is *not* fixed is that sub-agents hold no tools, so it contains nothing that costs
  anything.
- **Can we measure whether a change improved answers?** No. FRE-453's canonical eval set exists
  and measures path, tokens, cost and latency, and it states that expectations never gate. There
  is no ratings table. Answer quality is unmeasurable until something produces labels — FRE-1384
  and the owner's auto-routing-control proposal are the only candidates.
- **Why did the owner have to phrase a question three ways to reach expansion?** That is
  FRE-1288, now on `stream:adr`. `conversational` forces SINGLE regardless of length, and
  `analysis` needs a literal keyword from `_ANALYSIS_PATTERNS`.
