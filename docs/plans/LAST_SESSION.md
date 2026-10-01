# Last session — master, 2026-09-15 04:40 UTC to 2026-10-01 08:40 UTC

## Doing / discussing  (≤5 sentences)
The owner's primary-model study (FRE-1517) ran to a decision, and then the owner's own production
turns became the evidence stream for three new tickets. Production serves llama.cpp Flash-Next with
workers ON, the briefing planner and the FRE-1522 context reserve. One blocker is physical: the Mac's
external model drive (EnvoyUltra) detached on 2026-09-17 at about 19:35 UTC, so the local model is
down until the owner reconnects it. Nothing is at the gate; build2 heads FRE-1529, adr heads FRE-1525.

## What was decided and why

**Engine: llama.cpp, not MTPLX.** Blind rubric scoring (two Opus scorers, turns 1–5) put llama.cpp
with workers at 17.0/25, every MTPLX setup at 12.0–16.5, Sonnet with no workers at 17.5. The cause is
engine-side: on MTPLX every worker landing re-prefilled cold (0 cached, 2.5–6 min) because the memory
guard cleared the cache near its 103 GB limit and low free disk capped the SSD spill. The owner:
"MLX models are known to struggle with long context."

**Workers stay ON, although delivery alone argued against them.** No-workers delivered 8/8 against
6/8, but scored *worst* (12.0) — its turn 3 answer was truncated at 370 chars and still counted as
delivered. Quality decided it, not the delivered flag. Explore added a separate `truncated` flag
(PR #1182) rather than fold truncation into the pre-registered rubric.

**Master's first root-cause reading was wrong, and explore corrected it.** The two lost owner turns
were not tool output: the skill-bodies block (114,599 chars, ~28.6k tokens) is re-assembled every
round, and each mid-turn user-role message admits the current one again. Three budget warnings
produced four copies and 154,096 prompt tokens. The block is *not* bounded by
`skill_index_max_tokens` (2,048), which covers only the 4,659-char index.

**Holding PR #1183 for a missing codex review paid for itself.** That post-hoc review found five real
bugs, including Stop being ignored during the context retry and an unrelated retry failure being
reported to the user as a context error. The precedent: when a seat records a skipped plan review on
the primary turn loop, ask for it post-hoc rather than bounce or wave it through.

**Consolidation now skips `outcome == "failed"` captures.** Decided at that same gate: FRE-1527 made
every failed turn write a capture, and with memory writes on those would have entered the graph.

**A live probe's capture must be archived before consolidation runs, not after.** The 05:07 AC-5 probe
reached the graph because consolidation ran first; the owner then had its turn removed. The order is:
master pauses consolidation → the seat fires → the seat reports → master archives.

**AC-5 for FRE-1527 waits for a natural overrun** (owner's choice), rather than a designed turn that
would write another synthetic turn into production.

**FRE-1502 was parked** (stream label removed, state untouched) so the adr seat could reach FRE-1525.
A backwards state transition would have wedged the stream.

## Worktrees — anything special
- `explore`: all runners and the blind-export tooling committed and merged (PRs #1181, #1182). Its raw
  run output under `scripts/eval/fre1517/out/` is deliberately untracked.
- The untracked `*.bak*` files at the repo root and under `telemetry/` are the owner's.
- Master's `.env` backups (`env.before-*`) live in a tmpfs scratchpad and may be gone after a reboot;
  the live `.env` carries dated comments for every study setting instead.

## Sequence position + drift
- Master audited all 24 `Awaiting Deploy` tickets on 2026-09-17 and closed three (FRE-1492, FRE-1494,
  FRE-1507) on evidence that already existed in telemetry and had never been folded back. The rest are
  correctly open, each with a named unmet live check.
- **Drift:** three ADR umbrellas (FRE-1118, FRE-1450, FRE-1470) sit in `Awaiting Deploy` although
  nothing about them awaits a deploy. The owner was offered a move to `Backlog` and has not answered.
- **Open finding on FRE-1484:** on owner trace 1ba5cf4f a worker returned `report_kind ledger`, which
  matches `_fanout_pause_tasks`, yet no `sub_agent_fanout_incomplete` pause was emitted. The trailer
  did fire. Not eval mode, no stored preference. Three candidate causes are on the ticket.
- slm_server: PR #16 and PR #18 merged, PR #17 (MTPLX backend) is a draft awaiting a live window.
  Master's reviews are posted; merging there is the owner's.

## Answers for the fresh start
- **Is the local model up?** No. The EnvoyUltra drive is detached (not merely unmounted — the Mac
  lists no external disk). Owner action. Verify recovery with a real completion through `:8600`, never
  with `/health`, which answers from the router.
- **Was any owner turn lost to that outage?** No. Zero turn events between 19:20 and 21:25 UTC.
- **What is at the gate?** Nothing. build2 → FRE-1529 (Urgent, the skill-block duplication),
  adr → FRE-1525 (artifact sharing ADR-0153).
- **What needs the owner?** Reconnect the drive; approve FRE-1524 (the budget warning); decide the
  umbrella move; decide whether slm_server merges PR #17 after its live window.
- **What is deliberately still in `.env`?** `AGENT_EXPANSION_ENABLED=true` and
  `AGENT_PLANNER_BRIEF_MODE=briefing` are the owner's 2026-09-16 production decision, not leftover
  study settings. `AGENT_ENABLE_SECOND_BRAIN=true` since the study ended.
- **Study captures:** 71 archived to `captains_log/captures_archive_fre1517` inside the gateway volume,
  so they can never be consolidated. Do not move them back.
- **Linear MCP:** its token expired at 08:37 UTC on 2026-10-01. Re-authorize before any board work.
- **Master's own correction worth remembering:** master printed a visible "Private list of what I need
  next" block in nearly every reply until the owner asked why. Keep that reasoning internal.
