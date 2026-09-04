# Last session — 2026-09-03 → 04

## Doing / discussing  (≤5 sentences)
The night's spine was **owner-directed single-model serving**: ADR-0141 (unify LLM dispatch on
litellm) authored, accepted and T1–T3 shipped, then the catalog repointed so `qwen3.8-flash-next`
serves both primary and sub_agent, split only by the thinking flag. Two owner test turns then
exposed three defects nothing in code review had found — sub-agent timeouts, a stop button that
stopped nothing, and a research query routed to unimplemented delegation. The live thread at reset
is **whether to serialize sub-agents** (see below) and a design conversation on router/harness
architecture now carried in FRE-1377. Nothing is half-merged; two seats are working.

## What was decided and why

**The `extra_body` bug is why ADR-0141 exists.** Measured live: Seshat posted `extra_body` as a
literal wire key, so `enable_thinking`, `thinking_budget`, `top_k`, `min_p` and
`repetition_penalty` were ALL inert. Nothing looked broken because backend 8503's launch flag was
doing the work. Collapsing to one model removed that accident — which is why the cutover had to
land before the single-model config, not after.

**`thinking_budget` is inert; `reasoning_effort` is the real lever and must never be sent.**
Probed directly. Master first concluded "max_tokens is the only lever" — wrong, corrected on
FRE-1362. `reasoning_effort` is pinned server-side to `medium`; sending an invalid value returns
500, and slm_server's watchdog treats 5xx as backend failure and **restarts the model** under
every other worker.

**SERIALIZATION — ruled, and the framing matters more than the ruling.** The owner ruled
"Serialize", then corrected the justification mid-turn: **"the goal is context management, not
latency."** That reframing is the durable part. The benchmark argument (aggregate throughput rises
only 18.8% from concurrency 1→3 while per-request drops 2.57x, so fan-out buys 15.8% wall-clock,
not 3x) is real but secondary — sub-agents exist so raw tool output stays in the sub-agent and only
a digest reaches the primary, and that property is indifferent to whether they ran side by side.
Filed as FRE-1380 with criteria that measure context, not speed. Two things master found while
scoping it, both now in the ticket: serialization must be a **loop, not a semaphore of 1** (a
semaphore leaves the 180s admission race, and worker 4 would be silently dropped with a fabricated
result), and `_build_synthesis_context` already composes from `r.summary` — so isolation holds
today, but `full_output_chars`/`digest_chars` have been logged all along and **nobody has ever
looked at them**. The justification for the whole mechanism is unmeasured.

**Four concurrent requests WEDGE the server.** All four 504, backend unresponsive, forced restart
required. Three is a hard cap, not a target. Master verified Seshat cannot exceed it today
(provider semaphore 3, health probe is a GET, local reranker unserved).

**The taxonomy is thinner than it looks.** `_MAX_TASKS = {"HYBRID": 4, "DECOMPOSE": 6}` — two
named strategies one integer apart, same code path. Owner's framing: model choice is
capability/cost/latency and is the user's; how the response is produced is harness architecture.
Whole conversation is on FRE-1377, including the owner's routing-control-with-auto proposal.

**A third inert knob, same family.** The sub-agent token ceiling is declared twice —
`models.yaml` says `max_tokens: 2048`, `settings.sub_agent_max_tokens` says 4096, and the settings
value is what reaches the wire. Joins the inert 60s `worker_timeout_seconds` and (post-FRE-1380)
`worker_global_timeout_seconds`. Three knobs in one subsystem that read as load-bearing and bound
nothing; assume a fourth until proven otherwise.

**Master's own errors, because they recurred.** A `LIMIT 8` on a 12-row aggregate read as the
whole population ("delegation has never fired" — it had, 3 times). A grep for `error` matched
`error-monitor` and `failed_count=0`, producing a false deploy alarm — the same unbounded-substring
bug filed as FRE-1376 twenty minutes earlier. Named the wrong setting on FRE-1374
(`sub_agent_timeout_seconds`, actually `worker_timeout_seconds`) and corrected mid-flight, costing
the seat a rework cycle.

**Worker A was normal; D was the anomaly** — 12.7 tok/s matches the benchmarked 14.4 at
concurrency 3; D's 65 exceeds the single-stream ceiling. Full reasoning on FRE-1379.

## Worktrees — anything special
`build` holds `fre-1370-query-paraphrase-pinned-role` with one unpushed WIP commit (`7aeef49a`)
for a **cancelled** ticket. Kept deliberately — it is the only copy, and cancellation is not
reason to force-delete an unmerged branch.

## Sequence position + drift
**Total drift from the Observability Foundation directive, owner-directed throughout.** The owner
fast-tracked single-model serving at 11:39; master then let two self-generated side-quests
(FRE-1370 paraphrase pin, FRE-442 citations) occupy both seats while the fast-tracked work waited,
until the owner said "stop everything but what asked for". Both were real; neither was asked for.
The lesson is not "don't surface findings" but "don't put them in front of the directive".

## Answers for the fresh start
- **Is serialization done?** No — FRE-1380 is filed and Approved, queued behind FRE-1379 on
  build1 so the instrumentation exists before anything is measured. Both were dispatched 2026-09-04
  and both seats were mid-work at reset.
- **Why is FRE-1375 not Done?** Deployed 08:51, but AC-2's backend half needs an owner-side live
  check: press Stop mid-generation, then send a second query immediately. If it stalls, the backend
  kept its slot — that is a *separate, more serious* ticket, not a fold-in.
- **Why did master's own tickets sit at In Progress?** They shouldn't have. FRE-1375 sat merged and
  deployed at In Progress for an hour, holding `stream:build1` and blocking FRE-1379. Caught by the
  reset gate, not at the merge. Advance dispatch AT the merge.
- **Is the citation work dead?** No — FRE-442 is parked and rescoped around the owner's actual ask
  (clickable internet links, not memory provenance). Its usage counts predate the model change and
  need refreshing before being quoted.
- **What did the owner originally want?** Verifiable links to pages Sasha actually read. Three ADRs
  grew around that want while FRE-442 sat at no priority since June.
