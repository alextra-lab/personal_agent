# FRE-1730: replay closed tickets with and without a plugin

This harness asks one question: does a plugin improve our work when our own CLAUDE.md rules are already on? The owner decides on an install. No score decides it.

- **Ticket:** FRE-1730.
- **Plugin under test:** `github.com/DietrichGebert/ponytail`, v5.3.3, commit `7801d061243d7b753bad0e0b3c057cd250b11755`. Use only that repo. Issue 735 reports a trojanized clone.
- **Design:** 5 tasks x 2 arms x 3 runs = 30 sessions, one at a time, with the arms mixed in a fixed order.

## The jail

`claude` flags are not a limit. Master's check on 2026-11-04 used `--allowedTools` and `--permission-mode dontAsk`, and the session still ran Python and wrote to `/tmp`. So each session runs in `bwrap`, in a `systemd-run` scope.

| Limit | How | Proof |
|---|---|---|
| No master tree, no worktree, no `.env` | `tmpfs` over the main checkout | `test_the_jail_hides_the_tree_the_home_and_loopback` |
| No shared `~/.claude` or `~/.config` | `tmpfs` over the real home, and a new `~` for each session | `test_the_real_home_does_not_change_when_a_plugin_writes_to_its_home` |
| No Docker | `tmpfs` over `/run` | the same jail test |
| No production on loopback, no internet | a new network namespace | the same jail test, and `curl` in `test_bash_is_on_and_a_call_sees_no_credential_and_no_network` |
| The model only | a relay to a gate on the host, which allows `CONNECT api.anthropic.com:443` and refuses the rest | `test_the_gate_refuses_a_host_that_is_not_allowed`, `test_the_paid_route_goes_only_through_the_gate` |
| No credential in the environment of Bash or a hook | `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` | `test_bash_is_on_and_a_call_sees_no_credential_and_no_network` |
| 4 GB of memory, 2 CPUs, low priority | the scope and `nice` | `test_the_scope_kills_a_process_over_its_memory_limit` |
| No sight of the merged fix | a repo with the base commit and its past only, with no remote, and no web tools | `test_a_session_cannot_see_the_merged_fix` |

What the jail does not do:

- The session can read `uv` and uv's Python versions, read-only, under the real home. It needs them to run tests.
- A hook or Bash call might read the session's token from `/proc/<pid>/environ`, because it has the same user. So use a token for this run only, and revoke it after the run.
- The Anthropic API is reachable, so a session can send text to it. A hook that holds the token might also ask that API to fetch a URL with a server-side tool, which sends the request from outside the gate. This is not tested.
- The stop runs on our side. A session that does not attack it passes the stop by one call at most. A hook that reads the token can spend through the gate with no limit from this harness, and the Max plan has no limit per token. Only the owner's revocation of the token ends that.

## Commands

Each of these except `paid` makes no paid call.

```bash
uv run python -m scripts.eval.fre1730.run preflight       # the tasks, the cost basis, the billing route, the credit check
uv run python -m scripts.eval.fre1730.run verify-tasks    # each PR's tests fail at base and pass at merge
uv run python -m scripts.eval.fre1730.run dry-run         # the fixture plugin and the stub, through the whole pipeline
```

`verify-tasks` builds one environment per task, once, in `~/.cache/seshat-fre1730/envs` (about 2.8 GB). Results stay in `~/.cache/seshat-fre1730/results`, out of git.

## AC-5 (master and owner)

1. Run `preflight`, and put its cost basis in front of the owner. Hold during a llama.cpp measurement window.
2. Copy the plugin at the pin. This runs nothing:

   ```bash
   git clone https://github.com/DietrichGebert/ponytail ~/.cache/seshat-fre1730/ponytail
   git -C ~/.cache/seshat-fre1730/ponytail checkout 7801d061243d7b753bad0e0b3c057cd250b11755
   ```

3. Repeat the AC-2 and AC-4 checks with the real plugin and the stub. This runs ponytail's hooks in the jail and makes no paid call:

   ```bash
   uv run python -m scripts.eval.fre1730.run check-arms --plugin-dir ~/.cache/seshat-fre1730/ponytail --marker 'PONYTAIL MODE ACTIVE'
   ```

4. Record the owner's go on the ticket, quoted and dated, with the owner's number and the credit reading.
5. Run the trace: one session per arm, on one task. The owner makes the token with `claude setup-token`:

   ```bash
   FRE1730_CLAUDE_OAUTH_TOKEN=... uv run python -m scripts.eval.fre1730.run paid --plugin-dir ~/.cache/seshat-fre1730/ponytail \
     --marker 'PONYTAIL MODE ACTIVE' --hard-stop-usd <owner's number> --projected-usd <estimate> --owner-go '<quoted>' --trace
   ```

6. Read the trace. Project the total from it, then run the same command with no `--trace`. Valid sessions are skipped, and the spend of every session counts.
7. Read `report.md`, read a sample of the diffs, and write the recommendation. Revoke the token.

**Not yet proved.** No test made a real call, because this build must not. A real token through the gate, TLS through the tunnel and a real session's use of `uv run` are first exercised in step 5.

## How the run stops

The run stops, and the ledger keeps each session that ended, on any of these:

- The spend reaches the lower of the owner's number and 1.5 times the projection.
- A session reports no cost, or passes 30 minutes. Its spend is then unknown, so a restart undercounts it. Read the plan's usage before a restart.
- A session ends on an error, which includes a 429 or a 402.
- A session reports success but its process did not exit cleanly, or it reports a cost that is negative or not finite.
- A transcript shows that an arm is invalid: the plugin arm has no marker, the control arm has the marker, or a CLAUDE.md did not reach the model.
- The scoring fails.

A session that ended on an error or failed its check is left out of the report's tables, and the report names it. It runs again after a restart. Its spend still counts. A session that reaches its own $10 budget is left out too, and the run goes on.

`paid` and `check-arms` refuse a plugin copy that is not a clean clone of the pinned repo at the pinned commit.

## Scoring

- **Diff size:** lines changed outside tests and in tests, against the base. A session that adds an ignore rule can hide a file from the diff, so the report lists each ignored file other than the usual caches.
- **The PR's own tests:** the share that pass after the merged test files replace the session's copies.
- **Tests added:** new test functions in the diff. The report gives the rate on tasks marked `risky`.
- **Cost, time, turns:** from the session's result.
- **Noise gap:** per task, the larger range between repeat runs of one arm. A difference counts only if it is larger. One run per arm measures no noise, so it never counts.

Each session is one ticket, with no `/build` steps, no seat plugins, no memory and no output style. The report says so.
