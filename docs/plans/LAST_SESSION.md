# Last session — 2026-09-14 05:40 to ~15:00 UTC

## Doing / discussing  (≤5 sentences)
The owner held both build streams from 07:05 to 14:46 UTC while the adr seat ran model tests on the
local SLM, and released them at 14:46. At the reset, build1 is running the FRE-1508 AC-2/AC-3 replay
for PR #1166, build2 is due FRE-1507 from dispatch, and adr holds FRE-1502. FRE-1505 passed its live eval check at 14:51 and is Done.
No PR is mid-merge.

## What was decided and why

**FRE-1372 (PR #1161) merged as infrastructure, not closed.** The first gate bounced it: no codex
plan-review on a diff that moved a `settings.py` guard and the eval credential set. Codex then found the
real trade: copying production's `AGENT_SUBSTRATE_PROFILE=managed_embedder` into eval switches off
`_validate_owner_storage_allowlist` there, and the new `_validate_eval_deployment_isolation` replaces it,
now covering `sysgraph_database_url` too. The ticket stays Awaiting Deploy because AC-1 fails (a
`:Turn` node leaks across arms) and AC-2 was never measured (the probe has no `reseed`). Both criteria
now live on FRE-1506. FRE-1372 closes when FRE-1506's live two-arm probe passes both. The owner approved
the `gpt-5.4-mini` extraction those probes use. The approval was given outside the seat transcript, so
master first reported it as missing.

**FRE-1505's exposure was live, not prospective.** The eval-treatment gateway already held the
Anthropic and OpenAI keys, with bash and curl auto-approved. Master added outcome criteria, including
`cat /proc/1/environ`, because scrubbing only the child environment does not close the hole. The fix
drops eval bash to `nobody` with an allowlisted environment, and Linear refuses on eval. Master did not
rebuild the production gateway: the code is inert there, and a restart could have disrupted adr's tests.

**ADR-0151 accepted by the owner, D2 as written.** Master's reading, which the owner took: `unresolved`,
`source_not_entitled`, `unreachable` and `contradicted_by_source` are all repairable by a cite-only retry.
Pre-generation forcing (heavy) is withdrawn. The owner approved FRE-1507, 1508, 1509 and 1510.

**PR #1166 (FRE-1508) is on HOLD, not bounced.** The design is reject-only: a `supported` verdict passes
nothing, and the cost cap is 8 extra judge calls per turn. Master waived `/code-review ultra` under the
standing directive. AC-2 and AC-3 wait on the replay. The owner refused to release that replay during
model testing, even though it calls only the cloud `entailment` role.

**The model-testing hold protocol is now memory** (`feedback_owner_model_testing_hold`): pause builds
with stream labels, not the kill switch, and hold even GPU-safe paid work until the owner says done.

**Master errors worth not repeating.**
1. Relayed "nothing that calls an LLM" to the seats, which was broader than the owner's GPU rule.
2. Reported "3 pytest processes running". The count matched master's own command: the fifth repeat
   of the pgrep lesson, now updated in memory.
3. Set the kill switch before the owner specified stream labels, and removed it about 2 minutes later.

## Worktrees — anything special
- `build` (build1): FRE-1508's branch. Its context disposition is **keep**. The capture export and the
  "before" worktree for the replay live in that seat's tmpfs scratchpad, so a reboot means re-exporting.
- The untracked `*.bak*` files at the root and in `telemetry/` are the owner's.

## Sequence position + drift
- build1: FRE-1508 (PR on hold until the replay posts), then FRE-1506 (High).
- build2: FRE-1507, then FRE-1509. FRE-1510 and FRE-1495 are blocked by FRE-1506.
- adr: FRE-1502.
- The production gateway was last rebuilt for FRE-1501 (`5342e698`). The code from #1161 and #1165 is
  on main but inert in production, and it ships with the next routine rebuild.

## Answers for the fresh start
- **What returns to the gate first?** PR #1166, when build1 posts the replay. Gate it against the
  pre-registered bar on FRE-1508: unsettled falls by 20 or more, no unsettled span reaches `passed`,
  no settled outcome changes, and 10 or more faithful turns. `/code-review ultra` is already waived.
  After merge it needs a `seshat-gateway` rebuild, which adds up to 8 `claude_sonnet` calls per turn.
- **Is the eval stack safe to run?** Yes, for credentials. FRE-1505's live probe passed at 14:51: eval bash runs as `nobody` and leaks nothing. The Turn-node leak (FRE-1506) is a separate contamination problem, so multi-arm evals still wait on FRE-1506.
- **What closes FRE-1372?** FRE-1506's live probe passing AC-1 and AC-2 together.
