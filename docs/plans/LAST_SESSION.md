# Last session — 2026-09-10 into 09-11

## Doing / discussing  (≤5 sentences)
The owner's own research turn failed three times, and chasing it consumed the day and produced
most of what shipped. It ended with **FRE-1487's study running live**: four sub-agent limits are
raised on the deployed gateway, query 1 of six is done, and **the owner directed that the limits
stay raised across the reset**. Read "The restore obligation" below before doing anything else.
Two deploys are deliberately held behind the study.

## What was decided and why

**The sub-agent was never incapable. It was interrupted.** Four hypotheses were tested and
falsified against live evidence: the local model's capability (OVH failed *worse*, on a different
limit), search quality (Exa live at ~1,900 chars/result — identical outcome), the wrong year (stated
explicitly — identical outcome), and thin results forcing repeated searching (154,755 characters
absorbed, same behaviour). What survived: **the worker was never told it had a budget.** It paced
for an open-ended search and was guillotined. The primary — same model — gets a budget warning, a
forced-synthesis pass and the date; the worker got none of the three.

**Master was wrong twice and both errors are instructive.** First: master told the build seat to
copy the primary's forced synthesis *verbatim*, including its tools-off behaviour. Fable's design
review measured that dropping the tools array discards the entire cached prefix — master re-ran it
independently (4 of 2,083 prefilled retained vs 1,832 dropped; ~7x on a 2k prefix). That call is the
*last* one a capped worker makes, on the path that already dies at 90s under a cloud primary — the
"fix" would have made the failing case fail more often. ADR-0149 D6 keeps the tools and pins
`tool_choice="none"`. Second: master judged worker reports by character count, which the owner
rejected as insufficient; the actual instrument is *what the text says*.

**The owner's design review earned its cost, and that is the transferable lesson.** Master judged
the fix "almost copy-and-paste". The owner insisted a more capable model review the thing being
copied. It found a live defect in the primary nobody had noticed (`executor.py:3126`, now FRE-1485).

**"No limit raises until the at-limit behaviour is implemented"** (owner, 09-10) shaped FRE-1482's
scope and is now satisfied — the behaviour shipped, so FRE-1487 measures what the limits should be.

**Exa was added to SearXNG's `general` category at weight 3** (owner-directed; they bought it for
this). Measured 15,701 chars vs 2,171 same-query. **This makes every `web_search` bill Exa** — the
cost note in `config/governance/tools.yaml` ("only opt-in `categories=exa` reaches a paid vendor")
is now stale and was not corrected.

**Two wedges cost ~13 hours between them, both self-inflicted by polling loops.** A hung
`codex-rescue` kept Remote Control "busy" while the pane was idle (38 dispatch ticks blocked). And a
seat's own watcher ran `pgrep -f "fre1479_reranker_calibration.calibrate"` — which **matched its own
command line**, so the loop could never exit. Both memory files were updated.

## Worktrees — anything special
`build` and `build2` hold merged branches that could not be deleted (worktree-held) — harmless.
Three untracked `*.bak-*` files at root are deliberate backups, named below.

## Sequence position + drift
**The working tree is intentionally dirty.** `config/model_roles.yaml` carries the study edit, by
owner direction. This is the one deviation from the normal clean-tree invariant and it is not drift.

## Answers for the fresh start

- **First task?** Decide with the owner whether to continue FRE-1487 (five queries remain, ~1h
  each) or restore the limits. **Do not silently revert** — the owner said keep them.
- **The restore obligation.** Four limits are raised on the live gateway:
  `AGENT_SUB_AGENT_MAX_TOOL_ITERATIONS=20` and `AGENT_ORCHESTRATOR_TASK_TIMEOUT_SECONDS=3600` (appended
  to `.env`, marked "FRE-1487 STUDY ONLY — REVERT"), plus `max_tokens: 8192` and
  `default_timeout: 600` on the `sub_agent` binding in `config/model_roles.yaml`. Baselines: 5, 900,
  2048, 90. Backups: `.env.bak-fre1487-043817`, `config/model_roles.yaml.bak-fre1487`. Restoring
  needs a `seshat-gateway` rebuild. **FRE-1487 AC-6 requires the study to change no limit** — the
  raise is the owner's decision on the evidence.
- **What the study has found so far (query 1, session `31e8f1a1`).** Worker 1: `stop_reason:
  completed` at **6 rounds of 20** — the model stopped by itself, so *we were one round short*.
  14,592 chars out, 201s generation. Worker 2: 7 rounds, 30% more absorbed (250,797), and **2.2x the
  wall-clock** — one call exceeded 600s, then the outer deadline cut it at 1,213s; it returned a
  2,780-char ledger rather than nothing. **Cost appears superlinear in accumulated context**, which
  points at prefill and therefore cache behaviour on the worker path. That is the open question.
- **Deploys held (do not ship until the limits are restored).** FRE-1480 is merged (`58660071`) and
  undeployed. If FRE-1478 lands, hold it too.
- **What is blocked on the owner?** FRE-1484 and FRE-1485 (ADR-0149 T2/T3, both `Needs Approval`,
  both unblocked). FRE-1478's stream label. Whether to continue the study.
- **A correction master owes the owner.** Master said Perplexity was unavailable, having checked only
  SearXNG's engine list. `AGENT_PERPLEXITY_API_KEY` **is** set in the environment — there may be a
  separate integration. Unverified; do not repeat the original claim.
- **Is `main` green?** Yes. Gateway rebuilt six times, healthy each time. Disk went 84% → 38% after
  a `docker builder prune` (94 GB reclaimed). `%commit` runs 107–126% daily with no swap — stable,
  but it is why background tasks get OOM-killed at ~65% real usage.
