# Last session — 2026-09-05 (early hours)

## Doing / discussing  (≤5 sentences)
The **sub-agent chain completed end to end**: FRE-1387, FRE-1390, FRE-1388, FRE-1389 all shipped,
and for the first time in this system's history a sub-agent held and used a tool. The owner's own
live queries were the verification instrument throughout, and the last one exposed a backend hang
that the **rebuilt llama.cpp then fixed** — proven by re-running the identical query. The live
thread at reset is the three client-side consequences of that incident (FRE-1398) and two silent
losses the working mechanism revealed (FRE-1397, FRE-1399). Nothing is half-merged.

## What was decided and why

**`run_python` was the right first grant, and the ticket's own strawman was inverted.** FRE-1388
filed it under "blast radius is the machine". Reading `tools/primitives/sandbox.py` shows
`--network=none`, `--read-only`, `--cap-drop ALL`, non-root, 512 MB, and one scratch mount that is
not the repo. **No network is the decisive property**: the risk this ticket manages is a
model-authored instruction running unattended, and a tool that cannot reach the network cannot
exfiltrate whatever that instruction says. That makes it *safer* than `web_search`/`fetch_url`,
which the strawman called plausible. Owner accepted, scoped to step one.

**Context isolation is real but smaller than assumed, and the headline number misleads.** First
measurement ever: 4,410 chars absorbed → 3,146 reported, a 29% reduction. The larger containment
does not appear there — sub-agent 4's own input grew 613 → 4,855 tokens across five rounds and the
primary received ~420. **The working context each tool round accretes is the thing that stayed
inside the worker.** A second run measured only 2%, because a worker can write a long report about
a short tool result. Do not quote 29% as the mechanism's value; the distribution is wide and
`run_python` returns compact output. The real win is the deferred `web_search`/`fetch_url` grant.

**The health probe argued against the correct diagnosis, and master believed it.** Master asserted
"not the backend failing" from nine `slm_health_probe_completed` events reading `status: up`. The
backend was hung the whole time. The probe is a plain GET taking no generation slot, so it reports
`up` through a total generation stall. **A green signal that cannot see the failure is worse than
no signal.** Master's time-to-first-byte hypothesis was withdrawn from FRE-1398 rather than left
standing beside the real answer.

**Our retry budget turns one hung request into a full outage.** `llm_max_retries` defaults to 3 and
the local deployment declares `max_concurrency: 3`, so retries alone can occupy every slot on the
box. slm_server observed exactly that and it made the watchdog's recovery harder. Note litellm's
error text read `Retried: 1 times` while the origin saw three — that counter is not the number of
times the backend was hit.

**Cloudflare's 120s proxy read timeout sits underneath both our budgets.** FRE-1379 made
`default_timeout` (600s) a real wall-clock bound yesterday; `orchestrator_task_timeout_seconds` is
900. Neither can fire on a tunnel-routed local call. That is a fifth "reads load-bearing, binds
nothing" instance, after `extra_body`, `worker_timeout_seconds`, the catalog `max_tokens`, and the
`role=` kwarg. It is invisible again only because the origin stopped hanging.

**Haiku 4.5 cannot run auto mode.** The dispatch daemon switched build1 to Haiku for a
`Tier-3:Haiku` ticket and stranded the seat in manual, unable to work. **Any Tier-3 ticket can
strand a seat this way.** FRE-1388 was retiered to Sonnet, which was honest anyway. Seat models are
pinned per seat in `~/.claude/cc-sessions.conf`, so a restart restores them — but `cc-master` is
pinned to `-` and would inherit a changed default.

**ADR status headers are the close gate.** FRE-1328 stayed open because ADR-0139 reads "Proposed —
partially withdrawn". ADR-0142 was merged only after the owner's acceptance was transcribed into
both the header and the index row; the repo's own index hook checks the two agree.

## Worktrees — anything special
`fre-1370-query-paraphrase-pinned-role` still holds its single unpushed WIP commit `7aeef49a` for a
cancelled ticket — the only copy. Everything else is merged.

## Sequence position + drift
The owner fast-tracked the sub-agent chain on 2026-09-04 and it is now **complete**. FRE-1382 is
deliberately sequenced after ("ceiling after"); FRE-1383–1386 stay `Backlog` pending the owner's
read of the router study. **The Observability Foundation directive remains unstarted**, now for a
third consecutive session — worth raising rather than drifting further.

## Answers for the fresh start
- **Is the sub-agent mechanism finished?** The chain is. What it revealed is not: FRE-1397 (nothing
  bounds total dispatch time), FRE-1399 (a capped worker can report an empty digest), FRE-1398
  (retry cap, the blind health probe, the unreachable 600s bound).
- **Was the 524 our bug?** No — an origin hang, fixed by an upstream llama.cpp commit and proven by
  re-running the identical query successfully. Do **not** disable `cache_prompt`; that was a
  mitigation for a cause that no longer exists.
- **Why is FRE-1328 still open?** ADR-0139 is `Proposed` and the sections answering the ticket were
  withdrawn. It needs an owner decision, not a state change.
- **Can we measure whether a change improved answers?** Still no. FRE-453's eval set measures path,
  tokens, cost and latency and states that expectations never gate. There is no ratings table.
- **What did master get wrong?** Two instrument errors, both caught: the health-probe reading
  above, and a `size: 40` ES query that truncated the newest events and produced a wrong "still
  running" report. Sort descending and check the total before concluding.
- **A recurring process lapse.** FRE-1390 sat deployed-but-unclosed for nine hours, caught by this
  reset gate. Second instance after FRE-1375. Closing out is a separate step from advancing
  dispatch, and an interruption between deploy and close is when it goes missing.
