# FRE-1517 stage 4 — Gemma spot retest after the fixes (pre-registered before the first turn)

The owner asked for a Gemma retest after the fixes, as a spot check (relayed by master, 2026-10-10). Master
approved the plan as written, and the owner said "Go". This file fixes the readings before the first turn.
Everything not named here follows `blind_scoring_stage3.md`. There is no blind scoring in this stage.

## What changed since stage 3

- **Gateway:** main `baa281af`, build fingerprint `a6b16e59715bf3a7`. Stage 3 ran `29f36a50`.
  - FRE-1564: workers get read-only tools, `query_telemetry` for `general`, and a researcher gets no
    conversation history.
  - FRE-1360: memory, worker reports and the planner digest arrive as tool results.
  - FRE-1566: knowledge-graph identity facts left the system prompt.
  - FRE-1562: `max_tokens` 12288 on local entries, and a runaway turn ends with an honest stop.
- **Engine:** slm_server serves UD-Q8_K_XL with MTP under the same served id. The catalog still says Q8_0.
  The arm `stage4_gemma_spot` takes its `quant` and `launch_flags` from slm_server's ready report. Until
  then they read TBD, and the runner refuses to start.
- **Workspace:** master moved the stage-3 outputs (`nfl_helper.py`, `nfl_picks.py`, `games.json`,
  `__pycache__`) out of `/app/agent_workspace`. `nfl-predictor/` (2026-09-16) and `fre1527-probe/` stay, as
  at the start of stage 3. `sandbox/` holds one file, from May.
- Consolidation is paused. The graph baseline is 13571 / 2722 / 9734 (nodes, `:Turn`, `:Entity`).
- The runner code is unchanged. Gemma only: every reading below is absolute, so no Flash-Next session runs.

## The runs, in order

| Item | Command | Targets |
|---|---|---|
| A | `prod_session_runner.py --arm stage4_gemma_spot --script s3_logs_learning --stop-after 4 --run-id stage4-gemma-s3` | F4: s3 t4 |
| B | `prod_session_runner.py --arm stage4_gemma_spot --script s2_tool --stop-after 13 --run-id stage4-gemma-s2` | F3: s2 t7 and t13 |
| C | `fre1537` replay, longhist and score, mode `thinking_off`, tag `gemma-spot` | F8: D7 |

A and B use `--replicate 1 --max-session-usd 2`. A failed turn does not stop a session, as in stage 3.

C uses a new run directory with the stage-2 captures and a fresh render on main's code. The render check
before the window found only one change: the planner system prompt lists the new worker tools (hash
`6947c986` → `9cf3c233`, 2,855 → 3,037 chars). All 27 planner user messages are identical. No digest file is
used, as in stages 2 and 3. The timing arm uses the stage-2 captured primary bodies.

## Readings

**A — s3 t4** ("Analyze the model call latencies over the last 24 hours…")
- PASS: delivered, and the latency figures come from our own telemetry, through `query_telemetry` by a
  worker or the primary's own query. No `researcher` runs `web_search` for the internal metrics.
- FAIL: a `researcher` web-searches the internal metrics again, or the turn is not delivered.
- The classifier still forces HYBRID (FRE-1515 is parked), so a fan-out is expected. The reading is where
  it goes.
- t1–t3 are lead-in turns. No pass reading.

**B — s2 t7 and t13**
- PASS: delivered, no `LLMTimeout`, and no call on any role stops at its limit.
- FAIL: any limit stop on that turn. A limit stop is a finding, not a harness error.
- A limit stop is either a `primary_generation_hit_bound` event on the trace, or a call whose
  `output_tokens` equals a cap of 4,096, 8,192 or 12,288.
- Near miss: any call over 6,000 `output_tokens`, on any turn.
- t1–t6 and t8–t12 are lead-in turns. Per turn, the report records delivery, limit stops, near misses and
  `bash_allowlist_miss` (F2).

**C — D7**
- PASS: 11 of 11 thresholds, with decline-correct 28 of 30 or more.
- `susan_3_thought_i_asked` is reported on its own in every case. Stages 2 and 3 declined it 0 of 6.

## How to read the result

- A FAIL is strong evidence. A PASS on B is weak evidence. Stage 3 had 2 runaways in 20 s2 turns. At that
  rate, 13 clean turns happen about 25% of the time. At the 2-in-40 rate of all Gemma turns, about 50%.
- Two things change at once: the code and the quant. A clean B cannot say which change helped.
- H1 (`reasoning_content` returned within one turn) is unchanged by these fixes. This stage does not test it.

## The report

- Per turn: delivered, the readings above, and per call the role, `output_tokens` and
  `reasoning_content_chars`.
- The replies of s3 t4, s2 t7 and s2 t13, for the owner to read, unscored.
- The capture files of A and B, listed for master to archive.
