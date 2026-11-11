# Last session — master, 2026-11-08 to 2026-11-11

## Doing / discussing  (≤5 sentences)
Master looked for the cause of the false claims in Seshat's answers. The owner said a misread is a
model problem, so master ran a reading test (7 answers × 3 runs) on qwen3.8-flash-next, GLM-5.3 and
two engram tables of qwen. The results and their limits are on FRE-1756. A Q8_0 engram arm was
running at the reset, and `slm_server` waits for master before it tests a new llama.cpp build.

## What was decided and why
- **The cause is split.** qwen makes about 2 to 3.5 times as many false claims as GLM on the same
  sources. Flattened tool text defeats both models. 4 of the original misreads were chance slips.
- **Score every arm in one blind pass.** Two reader passes counted at different strictness. Only a
  comparison inside one pass is valid. That is why the Q8_0 arm needs a final pass of all four arms.
- **The owner kept the Q5_1 engram table** ("the model is the same"). It showed no measurable gain.
  The IQ4_NL shard stays on the Mac as the rollback.
- **FRE-1760 is one ticket for all lost structure** (owner "Option 2"): SearXNG line breaks,
  `fetch_url` JSON arrays, `fetch_url` HTML tables, and the SearXNG update. Its AC-6 reruns the
  reading test after the deploy.
- **The 2026-11-08 replay excluded self-questions.** The owner named Seshat's statements about
  itself as the main failure, and the test did not cover them. Memory holds the lesson.
- **A model binding change is the owner's decision.** Master recommended GLM-5.3 from the picker
  for web questions. The owner has not decided.

## Corrections from this session
- Master stated "17 claims, 11 unconfirmed". The data held 12 unconfirmed. Corrected on FRE-1756.
- `docker logs … 2>/dev/null` hid the gateway log, because structlog writes to stderr. Use `2>&1`
  into a file. Memory holds this.
- The first qwen arm hit HTTP 524: a long prompt with no streaming passes the 100 s Cloudflare limit.
  The driver now streams every local call.

## Worktrees — anything special
- None changed by master.

## Answers for the fresh start
- **In flight at the reset: the Q8_0 arm.** Driver: `~/.cache/seshat-replay/glmtest/driver.py`.
  Run files: `glmtest/runs/q80_*.json`, 21 expected. If fewer, resume with
  `uv run python ~/.cache/seshat-replay/glmtest/driver.py q80 3 A008 A015 A022 A027 A045 A050 A051`
  from `/opt/seshat` (it skips files that exist). Free calls, but only while :8502 serves Q8_0.
- **Then send `slm_server` the free message** (`bridge:session_015csQHYjLLkJLhZGskjvKGM`). Its
  build test (llama.cpp f3b407ca1) runs on Q5_1 with b12138 as rollback. It reports the build string.
- **Then the final blind pass:** build a new blind set of all 84 run files (pattern in
  `glmtest/pass2/`), two readers (15 facts, and all false claims), unblind, and post on FRE-1756.
- **Then the owner's choice of table** (Q5_1 or Q8_0). `slm_server` asks the owner after master's run.
- **Approved paid checks, not run yet** (owner "All approved", 2026-11-11, recorded on FRE-1760):
  one Exa and one Brave call (do they return line breaks?), and one call per Perplexity model name
  (`sonar`, `sonar-reasoning-pro`, `sonar-deep-research`). A few cents. The FRE-1760 design waits
  for the Exa and Brave result.
- **The model calls two tools that do not exist** (`read_file` 180 times, `system_metrics_snapshot`
  60 times, FRE-1759 F9). Nobody checked why. Not ticketed.
- **Private data stays private.** `~/.cache/seshat-replay/` (mode 700) holds owner questions and
  answers. Tickets and the repo hold ids and counts only.
- **FRE-1747** waits for the owner's Google service-account setup (owner: "in progress").
