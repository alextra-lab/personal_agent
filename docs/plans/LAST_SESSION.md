# Last session — master, 2026-10-20 to 2026-10-21 19:40 UTC

## Doing / discussing  (≤5 sentences)
All of this session's deployed fixes are verified live and Done: FRE-1646, FRE-1638 and FRE-1648
(owner test turns on 2026-10-22, traces `805c8877…` and `cf2bcc6c…`). FRE-1637 closed with them.
FRE-1620 (ADR-0157 effort selector) still owes master's adjudication from its ticket comments.
A 6-step Sonnet turn hit the `spend_threshold` pause (iteration 6 of 6) at $0.05. The pause counts
iterations, not dollars. The owner found it interesting and chose `answer_now`, with no decision yet.

## What was decided and why
- **Owner: "I decide if we swap. Not you."** Master merged #1320 (Haiku 5.5 on `captains_log`)
  because a bar passed. #1321 reverted it. Eval tickets report results and never swap. Memory:
  `feedback_owner_decides_model_swaps.md`.
- **`captains_log` → `gpt-6.1-sol`:** the owner said "Sol" after the FRE-1640 study (FRE-1646).
- **Sonnet 5 stays on all six roles.** The owner skipped the FRE-1647 paid run ("Ok.  Skip.") and
  said of Sonnet 5.5: "It is more expensive". The per-token price is the same, but FRE-1640 measured
  $8.53 against $7.95 per 30 days on `captains_log`, and Sonnet 5.5 scored lowest (17/20).
  `claude_sonnet_5_5` stays picker-only. The `auto` tool-choice code from #1333 stays.
- **Sol as an LLM judge prefers OpenAI models** (120/120 in FRE-1640). Master applied a stricter
  rule: a pair is worse if either judge prefers the control in both orders. Use two judges from
  different vendors in every blind comparison.
- **Paid search testing is over.** Owner: "Search testing has reached my decided budget". The
  owner then said "cancel the rest of adr-0158" (T2 FRE-1642 and T3 FRE-1643 canceled).
- **FRE-1641 spent about $50 and decided nothing.** The harness had no baseline arm and no noise
  arm. Memory: `feedback_harness_must_answer_the_decision.md`. The paid-run rules that came from it
  are in `.claude/skills/lifecycle-rules.md` § Paid runs (#1328).
- **The $0 post-mortem on FRE-1641:** search drift is mostly real vendor variation plus the
  FRE-1584 engine change. Today's 10k-character fetch cut keeps 97.5%, better than every focused
  cell. Do not reopen the fetch-size question without a new reason.
- **Turn performance is accepted.** Owner: "That performance is also acceptable to me" (3 min 53 s
  and $0.87, against 10 min and $2.08 before FRE-1634, FRE-1635 and FRE-1639).
- **Sol's slow planner (72 s against 30 s)** was provider-side output speed, not our code.

## Worktrees — anything special
- `explore` has `docker/searxng/settings.yml.example` modified. It is the known uid-977 SearXNG
  file problem, not a study change. Check it with the explore seat before any cleanup.
- `agent-ad5db2001c2a5c5f9` (`fre-1590-artifact-design-out-of-catalog`) is still not this session's.

## Answers for the fresh start
- **Exa and Brave credit:** FRE-1641 drained the shared credit mid-run on 2026-10-21, which also
  degraded production search. Master saw no top-up recorded. Ask the owner before you assume
  search works.
- **Owner alerts:** `.env` holds `AGENT_OWNER_NTFY_URL` with a random topic, and the owner is
  subscribed. The topic is a secret. Never write it into Linear or the repo. The sender forces
  IPv4 (FRE-1632), because the VPS IPv6 /64 already used up the ntfy.sh quota.
- **FRE-1637 is in Verify Failed** because the PWA shows `session_cost_usd`. FRE-1648 is the fix.
  If the owner's turn verifies FRE-1648, close FRE-1637 with a pointer to it.
- **Awaiting Deploy holds about 25 tickets.** Only FRE-1646, FRE-1648 and FRE-1638 are from this
  session. The rest are older. The 2026-10-16 file listed timed reads for some of them. Master did
  not check them in this session, so triage each one from its comments.
- **Owner-pending:** the PWA turn only. The 2026-10-16 file also listed FRE-1607 approval and
  Streams 2 and 4. This session did not raise them. Check FRE-1607 in Linear.
