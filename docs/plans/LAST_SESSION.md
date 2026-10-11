# Last session — master, 2026-10-05 to 2026-10-11 08:00 UTC

## Doing / discussing  (≤5 sentences)
The owner stopped all model testing on 2026-10-10 and ordered the move of skills and tools to the
industry standard (FRE-1573, ADR-0156, project "Skills and Tools Standard"). Seven of its tickets
are Done; the rest run on the build streams (read `next_resolver`). Model testing reopens only when
FRE-1581's gate passes (G1–G4, plus the ADR-0154 D7 planner probe that the owner moved into it).
Explore is on a read-only study: can Neo4j's agent skills improve recall or KG search
(https://neo4j.com/labs/genai-ecosystem/agent-skills/neo4j-skills/). The owner is waiting for
nothing from master except the pending decisions below.

## What was decided and why
- **Testing stopped because the studies measured our broken tools, not the models.** The PR #1242
  audit found 36–45% of Flash-Next's tool calls fixable. Owner: "Tools are not working - we fix
  them, then we come back to testing models." Do not propose model studies, D7 runs or live probes
  until FRE-1581 passes. Owner-approved exceptions shipped: FRE-1568, 1570, 1572, 1583.
- **The production planner prompt is currently unqualified.** FRE-1580 changed it; the owner chose
  "Re-pin now, probe in T9" in build2's seat. FRE-1581 must run D7 on the current prompt.
- **Owner decisions that changed the permission model (all in ADR-0156 or its tickets):**
  `python3`/`awk`/`sed`/`env` ask (reverses the earlier "python3 stays allowed"); an "ask" with no
  approval UI is refused (ADR-0063 Amendment B); `curl` runs unasked only for read requests to
  internal telemetry (FRE-1583); reviewed skill scripts get exact allow rules (FRE-1585).
- **MCP is off by owner decision (O1):** the three MCP copies of native tools were removed, so no MCP
  server remains; `AGENT_MCP_GATEWAY_ENABLED=false` in production `.env` is deliberate.
- **Primary model:** master recommends keeping Flash-Next (Gemma failed D7 three times: decline
  24/30, misses `susan_3` and `tool_logs`; blind quality −7.5 to −8 on s2/s3). The owner has NOT
  ruled. If Flash-Next is kept, remove the study-only Gemma `planner` mode from `config/models.yaml`.
- **FRE-1517 stage-4 rulings:** s2 t13 = PASS (owner). Telemetry turns in stages 3–4 are confounded
  by the tools, not model evidence (owner: "the skills/tools are wrong - for any model").
- **Search:** FRE-1584 removes `google cse` (271 of 290 degraded searches were its quota) and keeps
  `braveapi` + `exa`. Assessed and rejected for now: SerpApi (scraping by proxy), Tavily (overlaps
  Exa; our fetch failures are guessed-URL 404s, 16 of 24). Kagi is first choice only if FRE-1584's
  7-day read shows degraded > 5%; check its API availability first.
- **Span extraction cap 4,096 → 16,384 (owner yes).** Its lane caps are USD 5/day, 7/week; the last
  week spent 5.42, projected ~6.6. Owner informed; no cap changed.
- **Master errors this session, now memories:** a listed field is not a populated one (FRE-1567 AC
  was wrong); a 60-row truncated query read as complete; removing a stream label without clearing
  `dispatch_state.json` wedged two streams; seats sat on "Switch model?" for 15 min to 3 h unseen.

## Worktrees — anything special
- `fre-1565-worker-side-effecting-tools` @ `7d261e99`: FRE-1565 parked (held, no stream label).
  It must rebase on main; its per-call worker asks (D9.3) already shipped in FRE-1574.
- `fre-1571-d7-tool-logs-fixture-truth`: empty branch. The Option A/B analysis is on FRE-1571.

## Answers for the fresh start
- **Held tickets (stream labels removed on purpose):** FRE-1565, 1495, 1510, 1571, 1517. Re-label
  only after FRE-1581 passes, except FRE-1565, which waits for an owner word.
- **After every dispatch launch, look at the seat pane.** The "Switch model?" prompt holds the seat;
  answer by the ticket's Tier label. This session ran a background watcher; it does not survive /clear.
- **Production `.env` edits this session (all deliberate):** MCP gateway off (line 222); dead keys
  commented 2026-10-11 (`AGENT_SKILL_ROUTING_MODE`, `AGENT_PREFER_PRIMITIVES`,
  `AGENT_SECOND_BRAIN_CHECK_INTERVAL_SECONDS`, `AGENT_REFLECTION_RECALL_ENABLED`,
  `AGENT_PLANNER_BRIEF_MODE`); backup `.env.bak-master-config-drift-20261011`. Consolidation
  (`AGENT_ENABLE_SECOND_BRAIN`) is back to true.
- **Owner decisions pending:** the primary model; whether to file the three keys read outside the
  config manager (`AGENT_SESHAT_CHANNEL_SECRET`, `AGENT_SLM_TUNNEL_BASE_URL`, `AGENT_HOST`) for
  the config-manager capstone; FRE-1571 Option A or B (when testing reopens).
- **Live checks that need an owner PWA turn:** FRE-1580 AC-5 (test file
  `/app/agent_workspace/fre1580-review.txt` is in place); FRE-1568 AC-4 (latency p50/p90 by role);
  FRE-1585 AC-4 after it deploys. Test identity convention for master-fired turns:
  a synthetic `fre<ticket>-live@` address on the owner's domain, `channel=EVAL`, consolidation paused first.
- **Timed reads:** FRE-1570 AC-4 on or after 2026-10-17 20:09 UTC; FRE-1584 AC-2 seven days after
  its deploy; FRE-1360 and FRE-1566 prompt-cache comparisons.
- **First turn after a prompt-changing deploy takes ~30 s** (cold prefill, 13k tokens on
  Flash-Next). Expected during this migration; G4 bounds prompt size.
