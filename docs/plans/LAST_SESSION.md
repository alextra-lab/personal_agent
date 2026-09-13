# Last session — 2026-09-12 into 2026-09-13

## Doing / discussing  (≤5 sentences)
Eight gates, all merged; the ADR-0150 chain is complete through T3 and the FRE-1498 routing study
has reported. The owner is travelling — the MBP was powered down and slm_server taken offline for
the gap, and **both will be back up when work resumes**. Dispatch is deliberately off. The thread
to pick up is routing: the study answered the owner's question and its proposals (FRE-1502) reshape
how FRE-1394 should be framed. The adr seat has waited on three owner questions since 09-12 06:49.

## `/health` never probes the SLM — verified, and it outlives this gap

`/health` reports database, elasticsearch, neo4j, second_brain and mcp_gateway, and **nothing about
the SLM**. So it returns a fully green `"status": "healthy"` whether or not `primary`
(`qwen3.8-flash-next`) and `sub_agent` (`inherit`) are reachable. FRE-1474 says the SLM probe reads
"up" through an outage; this is a step worse — `/health` does not look at all.

Not a live blocker — the SLM is up when work resumes. It matters because prime-master's step 7
treats a green `/health` as the health answer, so any future local-model outage reads clean.
Everything else is cloud or managed (Sonnet, gpt-5.4-mini, OVH embedding) and unaffected.

## What was decided and why

**Three study limits became permanent baselines, two deliberately did not.** The owner argued for
all five: *"We have found the natural limits of the model/harness... Now our task is to refine the
harness, prompts, routing in order to optimize and lower the durations."* Master held 4 and 5 on
the owner's own rule — a limit rises when the behaviour at the limit is implemented. For the
landing ceiling and the round cap that became true this week by construction; the two turn-level
timeouts had no finer control underneath. Owner accepted, condition set: revisit after FRE-1489 and
FRE-1138. Recorded on FRE-1487 with trigger notes on both.

**The routing question is answered (FRE-1498, merged 29d5d426).** Asked directly, the planner
declines every command, greeting, tool request and follow-up, and expands every research-shaped
question — and **ten of those twelve are what the ladder calls conversational**. The steering verb
flips the classifier every time and moves the planner **not at all**. Register moves neither. The
classifier is register-driven; the model's judgement is not. Susan's four real requests come out
identical in all three regimes. Proposals P1–P3 are the AC-6 bound and live on FRE-1502.

**Master shipped a defect and reported it as verified.** FRE-1498 found every schema-backed landing
on OVH has failed since FRE-1494 merged: `sub_agent.py:1039` shallow-copies
`WORKER_REPORT_RESPONSE_FORMAT`, leaving a nested `MappingProxyType` the ovhcloud provider cannot
serialise. Master's OVH probe on FRE-1494 tested `dict(WORKER_REPORT_JSON_SCHEMA)` — the schema
alone — inside a wrapper it built by hand, never the production object, then reported the dialect
value as evidence-backed. **Verify at the seam production uses, not one that resembles it.**
Correction posted on FRE-1494; fix is FRE-1500.

**A ticket premise that did not survive measurement.** FRE-1489 was filed as a head-placement
defect with a 516-token hypothesis; both were wrong. Its own instruction to settle the hypothesis
*before* designing against it produced the right answer — write that into investigation tickets.

**A criterion's method can be weaker than what is delivered** — FRE-1393's AC-3; now in memory.

**Two master errors worth not repeating.** FRE-1496 item 4 said "update any test that pins the old
values" then named one — four needed it, and each worktree's own `config/` checkout hid the failure
from the seat. And master recommended approving FRE-1394 without reading its three blockers, two of
them themselves unapproved. Instruct a search; read relations before recommending.

## Worktrees — anything special
build2 wedged twice on the model-switch modal after `/clear` → `/model opus`; cleared both times.
It also spent three hours returning "(No response.)" to thirteen watcher pokes after a restart
resumed it into a *previous ticket's* session — that is FRE-1499. Four untracked `*.bak-*` files at
root are the owner's and master's.

## Sequence position + drift
**The intentional dirtiness is gone.** FRE-1496 committed the study's values with better comments,
so master discarded the working copy. The tree is clean — a modification to `config/model_roles.yaml`
now *is* drift, where before it was expected.

`Awaiting Deploy` holds ~19 tickets, most parked on a live check needing the SLM — they unblock
together when it returns.

## Answers for the fresh start

- **First task?** Nothing at the gate — zero open PRs. Read FRE-1502's proposals; answer the adr seat.
- **Why is dispatch off?** Owner asked during wind-down. `telemetry/dispatch.disabled` is a *shared*
  switch — both daemons observe it and both units stay `active` and no-op. `rm` it to resume. Do not
  resume while the SLM is offline unless the ticket needs no local turn.
- **Is the adr seat stuck?** No. It asked three questions on FRE-1328 and is correctly idle, holding
  131.5k tokens. **Do not reset it.** Answer "do we still want `enforce` at all" first — it collapses
  the other two.
- **What is live and broken?** FRE-1500: every OVH schema-backed landing. Local turns are unaffected
  by that bug but impossible while the MBP is down.
- **What needs a live turn once the SLM is back?** FRE-1382's AC-1 (needs load — two concurrent
  turns put the second at budget 1) and FRE-1494's EVAL re-run. Both need the owner's explicit OK.
- **The open hole:** `sub_agent_count` can still exceed the per-turn budget via FRE-1389's gap
  redispatch, so FRE-1382's AC-1 cannot hold as written. Master recommends a follow-up; unanswered.
- **Before dispatching FRE-1394:** its blockers are terminal, but master left it unlabelled on
  purpose. FRE-1502 argues the cutover should be shaped differently. Also: `conversational`'s cap is
  6 and the spend threshold is 6, so FRE-1393's pause is inert for 78% of traffic — FRE-1394 removes
  the ceiling *and* activates that pause in one change. Gate it as a combined step.
