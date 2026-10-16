# Last session — master, 2026-10-13 to 2026-10-16 08:30 UTC

## Doing / discussing  (≤5 sentences)
The owner ordered a full fix of the self-analysis pipeline in four parts: FRE-1608 (analysis), FRE-1609
(history), FRE-1610 (backlog) and FRE-1611 (creation). Parts 1–3 are merged and deployed. Part 4,
PR #1294, was bounced to build2 on 2026-10-16 for two reasons: the codex review did not run, and the
dry-run runbook ran on the host instead of in the gateway. When #1294 returns, follow the order below.
The owner approved the llama.cpp pin move, and production runs b11689 (0f4376268) since 08:16Z.
The pin commit is d4a4fb3 in the slm_server repo. Rollback: point `SLM_LLAMA_SERVER_BIN` back to llama.cpp-master.

## What was decided and why
- **Owner: clear the Captain's Log history and start from zero.** The bad analysis already filed 6
  tickets (FRE-1373, 1378, 1404, 1412, 1418, 1429, all canceled). Master ran the FRE-1609 reset on
  2026-10-14 at 16:32Z. Verified backup: `~/seshat-backups/fre1609-20261014T163213Z`. **Delete it
  when FRE-1611 closes.** The six fingerprints are suppressed in the gateway volume (FRE-1610 replay).
- **Ticket filing is held.** `.env` has `AGENT_SESHAT_OPEN_TICKET_CAP=4`, with a comment, since
  2026-10-14. The order for FRE-1611:
  1. Merge #1294.
  2. Set `AGENT_PROMOTION_PIPELINE_ENABLED=false` in `.env`. The cap starts counting
     `pipeline-filed` only, so its count drops to 0, and the next consolidation can file up to 4
     tickets unseen.
  3. Deploy.
  4. Run the dry run **inside the gateway**: `docker cp` the script, then run it with
     `/app/.venv/bin/python`.
  5. Post the list on FRE-1611.
  6. The owner reads the list. Only then set the cap to 10, set the pipeline flag to true, and restart.

  Streams 2 and 4 (feedback polling, insights) stay off. They are the owner's decision.
- **Escalated diffs were waived** for #1290, #1291, #1292 under the 2026-08-31 directive ("Waive
  unless seriously necessary"). Each one was test-isolated or refused to run without a verified backup.
- **Secrets rotated, all owner-approved, all in `pass`:**
  - sysgraph_role password (2026-10-14). A helper printed the `.env` URL.
  - `AGENT_SESHAT_CHANNEL_SECRET` (2026-10-14). A prefix grep printed it.
  - `GATEWAY_TOKEN_PWA` (2026-10-16). Master's `curl localhost:3000` printed the PWA page, which embeds it.

  Check: the new token gives 200 and the old one 401 on
  `/api/v1/observations/route-traces/session/<nil-uuid>`. Open PWA tabs need a reload.
- **FRE-1600:** no storage change. Stored history costs about 256 tokens per follow-up turn.
  `preserve_thinking` saves at most about 0.6 s per turn but costs about 4,500 tokens per stored turn.
- **FRE-1606:** the owner said "Go" in the adrs seat to all three recommendations.
- **Owner: "The elastic dashboards should become grafana dashboards."** The five panels moved to
  FRE-1612, filed by build2 and in Needs Approval for the owner.
- **The owner's model-testing hold (FRE-1573) still stands.** The llama.cpp pin move is an engine
  fix, not a model test.

## Worktrees — anything special
- `build2` holds `fre-1611-creation-cap-flags-docs-dashboards`, which is bounced PR #1294.
- `agent-ad5db2001c2a5c5f9` holds `fre-1590-artifact-design-out-of-catalog`, which is not this
  session's work. Check it before removing it.

## Answers for the fresh start
- **The master channel (port 8789) held the OLD secret until this restart.** That is why the watcher's
  triggers for #1290, #1289 and #1291 never arrived. After the restart, check that the new secret is
  loaded:
  1. Read `ss -ltnpH` for the pid on 127.0.0.1:8789.
  2. Read `SESHAT_CHANNEL_SECRET` from `/proc/<pid>/environ`.
  3. Compare it in Python with the `.env` value, and print only a boolean.

  Ports 8790, 8791 and 8792 were True on 2026-10-14.
- **A seat can sit on a Claude Code dialog for hours** (2026-10-16: `/auto-mode-setup`, 7 h on
  build2). The dispatcher still saw the seat as busy after Esc. `cc-sessions restart cc-2build`
  cleared the stale status, and the next tick launched the ticket.
- **Timed reads that master owes** (each ticket's comment has the query):
  - FRE-1602 (overdue since 2026-10-14 13:38Z).
  - FRE-1570 AC-4, on or after 2026-10-17 20:09Z.
  - FRE-1584 AC-2, on or after 2026-10-18.
  - FRE-1601, on or after 2026-10-20.
  - FRE-1600 AC-4 re-baseline, on or after 2026-10-21, from ES only.
  - FRE-1605 AC-4, on or after 2026-11-13.
  - FRE-1608 live check: the first non-eval reflection after 2026-10-14 08:53Z must show no "0ms" or
    "2.00/3", and a failed turn must give `had_errors=true`.
  - FRE-1322: an integer `host_clusters` field on `missing_skill_pattern_analysis_complete`.

  `Awaiting Deploy` holds more tickets from earlier sessions. Triage them from their comments.
- **Owner-pending:**
  - Approve FRE-1612 (dashboards).
  - Approve FRE-1607 (the planner and sub-agents never see the skill catalog on an expanded turn).
  - Turn on Streams 2 and 4, or keep them off.
  - Restart cloudflared on the Mac. It was planned for Friday 2026-10-16. Only the owner can do it.
- **Stale `/master <n>` text** can collect in master's input box (1217–1219 on 2026-10-16). Check
  `gh pr view` before you gate.
