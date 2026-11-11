# Last session — master, 2026-11-08 to 2026-11-11

## Doing / discussing  (≤5 sentences)
Master looked for the cause of the false claims in Seshat's answers. A reading test (7 answers ×
3 runs, the same saved sources for every arm) compared qwen3.8-flash-next on three engram tables
and two llama.cpp builds, and GLM-5.3 as a reference. All scores and limits are on FRE-1756. The
owner then chose b12283 with the Q5_1 table, and `slm_server` was switching production at the reset.

## What was decided and why
- **Mac engine: b12283 with Q5_1, pin moved** (owner, 2026-11-11). Q5_1 was the lowest qwen
  version in three blind passes. Q8_0 was never better. The build did not change accuracy. Memory
  `project_slm_engine_b12138_since_2026_11_05` holds the numbers. Rollbacks stay on the Mac.
- **The cause of false claims is split.** qwen makes about 1.5 to 3.5 times as many as GLM on the
  same sources. Flattened tool text defeats both models (FRE-1759). Some misreads are chance slips.
- **Score every arm in one blind pass.** Separate reader passes count at different strictness.
- **FRE-1760 covers all lost structure** (owner "Option 2"): SearXNG line breaks, `fetch_url` JSON
  arrays and HTML tables, and the SearXNG update. AC-6 reruns the reading test after the deploy.
- **A model binding change is the owner's decision.** Master recommended GLM-5.3 from the picker
  for web questions. The owner has not decided.
- **Ask which arm to run.** Master picked Q5_1 for the new-build test without asking, and the owner
  wanted Q8_0. Memory holds the lesson.

## Corrections from this session
- `docker logs … 2>/dev/null` hid the gateway log, because structlog writes to stderr.
- A long local prompt with no streaming hits HTTP 524 at the Cloudflare tunnel. The test driver
  streams every local call.

## Worktrees — anything special
- None changed by master.

## Answers for the fresh start
- **Wait for `slm_server`'s report** (`bridge:session_015csQHYjLLkJLhZGskjvKGM`): build string,
  table and checks after the switch to b12283 with Q5_1. Check it against the owner's decision.
- **Approved paid checks, not run yet** (owner "All approved", recorded on FRE-1760): one Exa and
  one Brave call (do they return line breaks?), and one call per Perplexity model name (`sonar`,
  `sonar-reasoning-pro`, `sonar-deep-research`). A few cents. The FRE-1760 design waits for the
  Exa and Brave result.
- **FRE-1761 in `cc-2build`** waited at a permission prompt for read-only `docker exec` version
  checks. Only the owner answers it.
- **The `/auto-mode-setup` dialog blocked a build seat three times** after a finished ticket. Esc
  clears it. Nobody knows what types it.
- **The model calls two tools that do not exist** (`read_file`, `system_metrics_snapshot`,
  FRE-1759 F9). Not checked, not ticketed.
- **Test data:** `~/.cache/seshat-replay/` (mode 700) holds owner questions and answers. The driver
  is `glmtest/driver.py`. Tickets and the repo hold ids and counts only.
- **FRE-1747** waits for the owner's Google service-account setup.
