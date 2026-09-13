# Last session — 2026-09-12 into 2026-09-13

## Doing / discussing  (≤5 sentences)
Seven gates, all merged and deployed, and the ADR-0150 chain is complete through T3. The thread
worth picking up is **routing**: the owner's own agent misclassified a real user's tool request
as conversational, which turned out to be a defect already diagnosed, decided and left unbuilt.
**Dispatch is paused** at the owner's request — the kill switch explains itself. The explore seat
is mid-study on FRE-1498 and the adr seat has been waiting on three owner questions since
2026-09-12 06:49 UTC; neither is stuck.

## What was decided and why

**Three study limits became permanent baselines, two deliberately did not.** The owner argued for
all five: *"We have found the natural limits of the model/harness. We have our max values. Now our
task is to refine the harness, prompts, routing in order to optimize and lower the durations."*
Master agreed on 1–3 and held 4–5, on the owner's own rule rather than caution: a limit rises when
the behaviour at the limit is implemented. For the landing ceiling and the round cap that became
true by construction this week — `worker_report_v1` bounds the report structurally, and
thoroughness became the operative round control — so the blunt limit became headroom. The two
turn-level timeouts had no finer control ship underneath them. The owner accepted and set the
condition: revisit after FRE-1489 and FRE-1138. Recorded on FRE-1487 with trigger notes on both.

**The ticket premise that did not survive measurement.** Master filed FRE-1489 as a head-placement
defect with a 516-token hypothesis. Both were wrong. A plain tool loop was always a strict forward
extension; the defect was a *duplication* — the role fixer merging through the caller's own dict,
so every iteration appended another copy of the whole fence. The ticket's own instruction to settle
the hypothesis before designing against it is what produced the right answer. Write that instruction
into future investigation tickets.

**A criterion's prescribed method can be weaker than what is delivered.** FRE-1393's AC-3 said to
seed a preference row at the storage layer. The seat instead proved the loader is never called,
because `allow_preference=False` short-circuits the read — so no stored row can be honoured *by
construction*. Verified in source before accepting. Judge criteria on what they are protecting, not
on their letter.

**An under-specified ticket cost a bounce, and master wrote it.** FRE-1496 item 4 said "update any
test that pins the old values" and then named one. Four needed it. Compounding it: each build
worktree holds its own `config/model_roles.yaml` at the committed value, so those tests passed
locally for the seat and only failed in CI. Instruct a *search*, never name the one you happen to
know.

**Susan's case and what it exposed.** A real user asked the agent to run a tool it had built for
her. Three of her four turns classified `conversational` and every one went `single` — the fallback
did the routing, not the ladder. That is verbatim FRE-1377's finding and ADR-0142's thesis, and
ADR-0142 has been **Accepted since 2026-09-05 with `conversational_always_single` still live**.
FRE-1394 is the unbuilt implementation. Master's first recommendation on it was incomplete —
recommended approving it without checking its three blockers. Check blockers before recommending.

## Worktrees — anything special
build2 wedged twice on the Claude Code model-switch modal after `/clear` → `/model opus`; master
cleared it both times. Yesterday it also spent three hours returning "(No response.)" to thirteen
identical watcher pokes because a restart had resumed it into a *previous ticket's* session —
that incident is FRE-1499. Four untracked `*.bak-*` files at root are the owner's and master's.

## Sequence position + drift
**The intentional dirtiness is gone.** `config/model_roles.yaml` carried the study's uncommitted
values since 2026-09-11; FRE-1496 committed identical values with better comments, so master
discarded the working copy. LAST_SESSION's standing note that the dirty tree "is not drift" no
longer applies — the tree is clean, and a modification there now *is* drift.

`Awaiting Deploy` holds 19 tickets. Most are deployed and parked on a live check nobody has fired,
which is this board's normal state, not a queue master created.

## Answers for the fresh start

- **First task?** Nothing is at the gate — zero open PRs. Read the explore seat's FRE-1498 output
  when it lands, and answer the adr seat.
- **Why is dispatch off?** The owner asked during wind-down. `telemetry/dispatch.disabled` is a
  *shared* switch — both daemons observe it, both units stay `active` and no-op. `rm` it to resume.
  No systemctl needed.
- **Is the adr seat stuck?** No. It asked the owner three questions on FRE-1328 and is correctly
  idle. It holds 131.5k tokens of live deliberation. **Do not reset it.** Its questions: is the
  obligation-satisfiability distinction real; do we still want `enforce` at all; does the
  exempt-but-checked gap deserve its own ADR. Answer the second first — it collapses the others.
- **What still needs a live turn?** FRE-1382's AC-1 needs the budget to actually *bind*, which
  needs load: two concurrent turns put the second at `active_inference_count >= 1` and budget 1.
  FRE-1494's EVAL re-run is also outstanding. Both need the owner's explicit OK.
- **What is the open hole?** `sub_agent_count` can still exceed the per-turn budget via FRE-1389's
  gap redispatch, so FRE-1382's AC-1 invariant cannot hold as literally written. Master recommends
  a follow-up ticket; the owner has not answered.
- **When FRE-1394 is gated, know this:** `conversational`'s cap is 6 and the spend threshold is 6,
  so FRE-1393's pause is currently inert for 78% of traffic. FRE-1394 removes the ceiling *and*
  activates that pause for that population in one change. Gate it as a combined step.
- **Should FRE-1394 be dispatched?** Its three blockers are now terminal, but master deliberately
  left it unlabelled: FRE-1498 exists to measure the unforced regime *before* the cutover, and
  FRE-1394's own AC-5 wants the cost change reported against a pre-change window.
