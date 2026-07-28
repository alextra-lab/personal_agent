# Last session — 2026-07-28 evening (two outages, three hard-threshold cliffs, twelve tickets closed)

## READ THIS FIRST — environment is UP and healthier than this morning

- **Deployed 17:30:31Z at `8df85524`**, owner-authorised. Health green on all five components. Four
  tickets shipped together: FRE-740, FRE-1016, FRE-1018, FRE-1034. Zero undeployed source commits.
- **Gateway memory limit is 2 GiB**, raised from 768 MiB during a live outage. Committed to compose
  *and* applied to the running container, so a recreate is safe either way.
- **`cloud-sim-embeddings` re-stopped after the rebuild** — standing rule, it revives every time.
- **The summary sweep is ON** (re-enabled this morning, owner-authorised). It is working: 8 digests in
  24h at **≈$0.040 each**, against $2+/day during the regression.
- **`seshat-neo4j-study` and `build2-postgres-test-1` are STOPPED**, reclaiming 1.19 GB. Study data is
  safe in the named volume `seshat_neo4j_study`; `docker start` restores it untouched.

## Doing / discussing (≤5 sentences)

Two production outages. The gateway was OOM-killed four times because Captain's Log reflection parsed the
entire 83 MB / 262k-line log corpus on **every turn** — root-caused by memory-triggered live stack sampling
(not inference), fixed, deployed, verified: the transient fell from ~578 MB to ~53 MB. Separately a qwen
call hung **75 minutes past its own 600 s timeout** and, because qwen is `max_concurrency: 1`, wedged the
model entirely while `/health` stayed green — the owner manages the SLM server and asked that it **not** be
filed. The through-line was **wrong field names returning empty results instead of errors** — five
occurrences in one session across master and the agent — which escalated into an approved ADR on telemetry
naming. Twelve tickets closed with live evidence; the Cost/Audit project's Awaiting-Deploy column is empty.

## Commits — the story behind the last 10

- **#729 → FRE-740** (probe user_id check). **Bounced for a missing self-review summary**, and the bounce
  paid: the review it forced found a real defect master's own diff-read had missed — the Neo4j check
  silently dropped Claim rows whose Person had no `user_id`, blinding the probe to the exact corruption it
  exists to catch.
- **#730 → FRE-1018** (supersession chain on pull). Codex found two design gaps pre-implementation; TDD
  caught a third. **The included plan doc describes the pre-hardening design and misleads a naive grep** —
  verify against `service.py`, not the plan. Master nearly bounced this on that basis.
- **#731 → FRE-1034** (reflection stops parsing the corpus). **Bounced once, correctly**: the build chose
  the fallback over the ES route on a "four callers" argument that did not survive checking — only one
  caller is a hot path, the rest are CLI/offline. The second attempt exceeded the brief, *empirically
  reproducing* the ES refresh-window bug (0/6 docs visible at 1.5 s, 6/6 at 5.51 s) rather than asserting
  the wait sufficed, and adding a guard test that reads the real `refresh_interval` from the template.

## Worktrees — anything special

- **build (build1)** — **FRE-1037** dispatched 20:28Z (LLM role assignment), with **FRE-989** blocked
  behind it as a genuine dependency.
- **build2** — on **FRE-1021**, which it **self-escalated to**: it refused to build FRE-1015 blind,
  correctly arguing that a stance surface riding an entity selection that returns zero entities would bake
  in the exact fade the ADR chose that consumer to avoid. It renamed its branch and moved itself to Opus.
  Then **FRE-1015 → FRE-937**.
- **adrs** — **FRE-1038** dispatched 20:45Z (telemetry naming ADR).
- **`master-914`** — still stale on the closed `fre-909-seat-rename`. Untouched again.

## Plan position + drift

MASTER_PLAN carried two errors, both now fixed: it claimed FRE-937's design was "reversed to fade" (the
owner confirmed **collapse stands** — the ticket was right, the plan was wrong), and its header was a day
stale. Twelve closures are now reflected.

**Deliberate deviation, and it is right:** **FRE-1036** (ES consolidation) is approved but **held, not
dispatched**, because FRE-1038's naming ADR must settle the convention first. FRE-1036 rewrites every index
template and is the cheapest moment to normalise names; running it first bakes today's inconsistency in.

## Answers for the fresh start

- **Three hard-threshold cliffs surfaced today and none had a monitor.** The 83 MB log corpus (fixed), the
  768 MiB container limit (raised), and the **ES shard ceiling — 586/1000 on a single node, ~34 days out**
  (FRE-1036, approved, held). Assume more exist: nothing in this system watches a threshold approaching.
- **The 2 GiB limit is no longer load-bearing.** A real turn now peaks at 654 MiB, inside the original 768.
  Leave it for a few days of traffic, then revert deliberately with sampler evidence rather than by guess.
- **FRE-1013's premise is measurably false.** It claims entity class is never emitted; the live graph holds
  **425 Personal** and **708 model-emitted** classes against 6,620 backfilled. Do not approve as written —
  the real question is whether the model's classification is any *good*, a measurement not a fix.
- **`request.completed` has been dead since 2026-06-13** (FRE-1033). Consequence: FRE-739's AC-3b is
  UNVERIFIABLE and **ADR-0107 cannot close**, even though FRE-740 is now Done.
- **Ten tickets remain in Awaiting Deploy**, all deployed, all awaiting master's acceptance verification.
  That column is a verification backlog, not a deploy queue. It is master's standing debt.
- **Owner priority, stated plainly: bugs first; configs and checks are second layer.** The shard-headroom
  monitor was stripped out of FRE-1036 on that basis.
- **The owner manages the SLM server.** The hung-model defect was deliberately not filed, at their direction.
- **Cite tickets by subject, never a bare FRE-XXX.** The owner corrected this twice in one session.
- **Awaiting owner approval:** FRE-1035 (ES field-resolution technique), FRE-1039 (Grafana/Kibana ADR),
  FRE-1033, FRE-1014, FRE-1009, FRE-1013, and the seven ADR-0127 tickets (FRE-1026–1032).
