# Last session — 2026-09-08

## Doing / discussing  (≤5 sentences)
The owner's own research turn failed three times on the same question, and chasing that produced
most of the day. It opened a design conversation the owner drove — **memory belongs to the
planner** — which became ADR-0147, merged Proposed. The owner corrected master twice, and the
second correction is the single most important thing on this page. Seven PRs merged; the day
ended at the reset gate catching a ticket master had left half-closed.

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
`TraceContext` is now identical to the primary's. That is why FRE-1473's clamp passes
`principal` explicitly: any inference would rebuild what FRE-1467 deleted.

**Six escalated diffs merged without owner ultra** under the 2026-08-31 directive. One went to
the owner instead — the `recall_personal_history` grant, which reversed a refusal the owner made
twice and ran against ADR-0147 merged hours earlier. Owner ruled: merge, bound the window. Live
at 30 days / 10 turns, **pending the owner's confirmation** — those are master's numbers.

## Worktrees — anything special
Nothing unpushed. `telemetry/dispatch_state.json.bak-163808` is untracked, pre-existing and the
owner's.

## Sequence position + drift
**FRE-1426's thirteen ACs were master's declared first task at 17:35 and were never adjudicated.**
It is still Awaiting Deploy. That is the one real drift of the day and it should lead the next
session.

The 2026-09-05 fast-track directive's retirement condition is **unmet**: it retires when FRE-1394
reaches Done, and FRE-1394 is `Needs Approval` and never started. The previous session delta
called the directive discharged because ADR-0145 completed — that is a session judgment, not the
console's recorded condition. Do not retire it.

FRE-1359 stayed unapproved all day — the sole reason `adr` computed no eligible work until
FRE-1122 unblocked FRE-1118.

A seat wedged on an interactive dialog **four times** today, each cleared by hand. FRE-1457's
detector shipped; watch whether the next one surfaces on its own.

## Answers for the fresh start

- **First task?** Adjudicate **FRE-1426**. Owed since yesterday, still Awaiting Deploy.
- **What is dispatchable?** build2 → FRE-1466 then FRE-1402. **build1 and adr have empty queues**
  — the adr seat is mid-flight on FRE-1118, build1 has nothing behind it.
- **What is blocked on the owner?** The 30/10 ceiling numbers (FRE-1473) · accepting ADR-0147 ·
  FRE-1471/1472 and FRE-1469/1474 approval · FRE-1359 (the adr blocker) · the `sub_agent`
  binding decision · authorising FRE-1122's baseline run · FRE-1427 AC-3.
- **Anything the owner parked?** The PWA context meter is broken and explicitly **not** a
  priority until the local model is usable again.
- **Is `main` green and healthy?** Yes. Gateway rebuilt four times, healthy each time.
