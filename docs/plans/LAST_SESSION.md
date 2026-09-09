# Last session — 2026-09-08 into 09-09

## Doing / discussing  (≤5 sentences)
The owner's own research turn failed three times on the same question, and chasing that produced
most of the day. It opened a design conversation the owner drove — **memory belongs to the
planner** — which became ADR-0147, merged Proposed. The owner corrected master twice, and the
second is the most important thing on this page. Nine PRs merged; the reset gate then caught a
ticket master had left half-closed.

## What was decided and why

**The owner's correction: `sub_agent` was ALWAYS local. Master said otherwise and was wrong.**
Master claimed ADR-0145 D1's `inherit` moved sub-agents from OVH to local, and built that on a
*comment inside the `qwen3.8-flash-next` catalog entry* explaining why that entry does not
self-pair. That is not the role binding. `git log` on `config/model_roles.yaml` settles it:
`sub_agent` read `qwen3.8-flash-next-instruct` from `d044003c` until `inherit` landed. So the
local model has been carrying every sub-agent since then. **This is a standing condition the
owner's OVH selection revealed, not a regression we introduced.** Read a binding from the
binding, never from a neighbouring comment.

**The local model cannot do sub-agent work reliably, and this is now measured.** All 10
`model_call_error` events on 2026-09-08 are on local models; zero on OVH, OpenAI or Anthropic.
Both malformed tool calls — an empty required `query` — are local. The cleanest comparison is
two turns on the same question in the same minute: local 17 searches / ~15 min / 3,445 chars;
OVH 8 / 5.7 min / 5,094 chars. In-flight model calls **never exceeded 1** — the fan-out is fully
serialised behind one llama-server. Not a general weakness: five short factual probes and a
four-step arithmetic probe were all clean. The failure is specific to multi-round tool use.
**Open decision:** bind `sub_agent` to `qwen3.8-27b-ovh`. Owner has not ruled.

**A master hypothesis was disproven by test, and the disproof found the real defect.** Master
believed a client-side abort left the llama.cpp slot busy, poisoning the next call. Tested
against the live server: three aborts, each followed immediately by a 200, plus a clean control.
False. What it exposed instead: the SLM health probe is liveness-only, reads `up` on both sides
of a 20-second window where every generation returned 503, and its own `degraded` branch depends
on `model_loaded`, which is always `None` — a check that cannot fire. FRE-1474.

**The owner's two challenges each moved ADR-0147, and the second retracted master's advice.**
First: a sub-agent's task is a better recall query than the raw question — correct, and gateway
recall runs one query on `user_message`. Second: *"subagents have low reasoning — are they
capable of deciding they need a memory recall search?"* That killed master's pull-at-the-worker
recommendation. The codebase already agreed: FRE-1390 put the planner on `PRIMARY` precisely
because judgment is not worker work.

**ADR-0147 then corrected three premises the owner and master had agreed.** The "cheap variant"
is ~700 estimated tokens unbounded — *dearer* than the 516 full section — so D4 bounds it at
20 items / 120 chars / 300 tokens. FRE-960 is a quality ceiling, not a blocker: `ctx.memory_context`
is already in hand when the planner runs, so no design here issues a recall query. And
`ctx.memory_context` is a **superset** of what the primary renders, so a digest over the raw set
could show a worker a fact the answering model never sees.

**FRE-1467 removed the ability to distinguish principals downstream** — a sub-agent's
`TraceContext` is now identical to the primary's, so any code needing to tell them apart must be
*passed* the principal, never infer it. FRE-1473's clamp does exactly that.

**Escalated diffs merged without owner ultra** under the 2026-08-31 directive, except one: the
`recall_personal_history` grant, which reversed a refusal the owner made twice. Owner ruled
merge-and-bound. Live at 30 days / 10 turns, **pending confirmation** — those are master's numbers.

## Worktrees — anything special
Nothing unpushed. `telemetry/dispatch_state.json.bak-163808` is untracked and the owner's.

## Sequence position + drift
**FRE-1426's thirteen ACs were master's declared first task at 17:35 and were never adjudicated.**
It is still Awaiting Deploy. That is the one real drift of the day and it should lead the next
session.

The 2026-09-05 fast-track directive's retirement condition is **unmet**: it retires when FRE-1394
reaches Done, and FRE-1394 is `Needs Approval` and never started. The previous session delta
called the directive discharged because ADR-0145 completed — that is a session judgment, not the
console's recorded condition. Do not retire it.

Seats wedged on interactive dialogs **five times**, each cleared by hand. FRE-1457's detector
shipped and is deployed, but its production path has never been observed firing — master cleared
each wedge faster than the two-to-three tick threshold. Watch whether the next one surfaces alone.

## Answers for the fresh start

- **First task?** Adjudicate **FRE-1426**. Owed since yesterday, still Awaiting Deploy.
- **What is dispatchable?** **Nothing.** All three lanes computed NONE at 13:00. PR #1113
  (FRE-1475) is the only work in flight. Progress is owner-gated now, not seat-gated.
- **Read FRE-1118's 2026-09-09 comment before touching memory work.** The adr seat found it
  half-shipped — the prompt prohibition landed in PR #955 on 2026-08-25 — which makes ADR-0147's
  reservation partly stale and FRE-1122's run no longer the "pre-FRE-1118" baseline its own
  ticket describes. Two owner questions sit unanswered there.
- **What is blocked on the owner?** The 30/10 ceilings (FRE-1473) · ADR-0147 acceptance ·
  FRE-1471/1472/1469/1474 approval · FRE-1359 · the `sub_agent` binding · FRE-1122's run ·
  FRE-1427 AC-3 · FRE-1402's three live turns · FRE-1467 AC-2.
- **Anything the owner parked?** The PWA context meter, explicitly **not** a priority until the
  local model is usable again.
- **Is `main` green?** Yes. Gateway rebuilt seven times today, healthy each time.
