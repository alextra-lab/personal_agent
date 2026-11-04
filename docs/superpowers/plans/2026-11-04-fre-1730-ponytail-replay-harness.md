# FRE-1730: plugin replay harness

**Goal:** build a harness that replays 5 closed tickets with and without a plugin, with CLAUDE.md on. This build makes no paid call and runs no ponytail code.

**Tier:** Standard. It has no `src/` change. It holds a hard spend stop and a sandbox.

## Design decisions

1. **OS jail, not `claude` flags.** `bwrap` hides the main checkout, the real home, `/tmp` and `/run`, and it has its own network namespace. Master's check on 2026-11-04 showed that the flags are not a limit.
2. **Egress gate.** The only way out is a relay to a gate on the host. It allows `CONNECT api.anthropic.com:443`. Each refusal goes in the report.
3. **New `~` per session.** A plugin's writes are attributable, the shared `~/.claude` stays clean, and no flag file passes between arms.
4. **Shallow repo, not a worktree.** A worktree shares refs and the stash with the seats, and it shows the merged fix.
5. **Scoring in the jail.** A session can write `.git/config` and the code under test, so git and pytest do not run on the host against them.
6. **Fail closed.** No reported cost, a timeout, an API error or an invalid arm stops the run. The ledger's spend counts after a restart.
7. **No default for the hard stop.** The number is the owner's.

## Files

`scripts/eval/fre1730/`: `jail.py`, `egress.py`, `stub_model.py`, `session.py`, `transcript.py`, `spend.py`, `footprint.py`, `score.py`, `report.py`, `matrix.py`, `run.py`, `tasks.py`, `tasks.json`, `tasks/`, `fixture_plugin/`, `README.md`.

## Tests

```bash
uv run pytest tests/evaluation/test_fre1730_harness.py -m "not integration"
uv run pytest tests/evaluation/test_fre1730_real_claude.py -m "not integration"
uv run python -m scripts.eval.fre1730.run verify-tasks
uv run python -m scripts.eval.fre1730.run dry-run
```

`test_fre1730_real_claude.py` needs `claude`, `bwrap`, `socat` and `systemd-run`, so it skips on CI. `test_fre1730_harness.py` runs on CI.

## Acceptance criteria

The ticket's AC-1 to AC-4. AC-5 is post-merge, for master and the owner.
