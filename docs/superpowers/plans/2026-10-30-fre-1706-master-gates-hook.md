# FRE-1706 — master-only allow hook for merge and rebuild, direct psql in worker skills

Ticket: FRE-1706 (Approved, `stream:build2`). Diff class: escalated (permission boundary). The owner
waived `/code-review ultra` on the ticket (2026-10-30). Tier: Standard — codex plan review required.

## Design

A PreToolUse hook `allow-master-gates.sh` (wrapper) plus `allow-master-gates.py` (decider), built
like `allow-readonly-psql`. It emits `allow` or prints nothing. It never emits `deny`.

**Wrapper (`allow-master-gates.sh`):**
1. Run `git rev-parse --show-toplevel` in the hook working directory. If it fails, or the result is
   not exactly `/opt/seshat`, exit 0 with no output. There is no `|| pwd` fallback, because a
   fallback that fails open is wrong for an allow hook.
2. `exec python3 allow-master-gates.py`, so the decider inherits stdin.

**Decider (`allow-master-gates.py`):** `re.fullmatch` of the raw command against two forms.
Regex, not `shlex`, because the grammar is small and closed. A command outside the grammar
cannot match, so a smuggled segment has nowhere to hide.

- Reject first: any `$` or backtick anywhere in the command (bash expands both inside double quotes).
- Form A: `gh pr merge <N> --merge --match-head-commit <40 hex>` then `--delete-branch`, `--subject
  <quoted>`, `--body <quoted>`, each at most once, in any order, separated by spaces.
  - Single-quoted text: `'[^']*'`. Double-quoted text: `"(?:[^"\\]|\\.)*"` with DOTALL.
  - `$` and backtick are already rejected, so quoted text is inert. A newline can appear only
    inside quoted text, because the grammar has no other place for it.
- Form B: `ENV=cloud make rebuild SERVICE=seshat-gateway` or `SERVICE=seshat-pwa`.
- Digits and hex use ASCII classes (`[0-9]`, `[0-9a-fA-F]`), not `\d`.

**Registration:** `.claude/settings.json`, PreToolUse `Bash`, `_dispatch.sh allow-master-gates.sh open`.
`open` means a missing script falls back to the prompt.

**Fold-in:** add `cloud-sim-postgres-eval` to `ALLOWED_CONTAINERS` in `allow-readonly-psql.py`.

**Skills:** one rule in `build`, `adr` and `explore` `SKILL.md`: query the database with the direct
`docker exec cloud-sim-postgres psql -U agent -d personal_agent -tAc "<SELECT ...>"` form.

## Steps

1. Write `.claude/hooks/test_allow_master_gates.sh` first. Run it. Confirm every positive fails.
   Command: `bash .claude/hooks/test_allow_master_gates.sh`.
2. Write `allow-master-gates.py` and `allow-master-gates.sh`. Make the wrapper executable. Re-run the
   test until `ALL PASS`.
3. Add the eval container to `allow-readonly-psql.py`. Add two cases to `test_allow_readonly_psql.sh`
   (allow SELECT on the eval container, decline a DROP on it).
4. Register the hook in `.claude/settings.json`.
5. Add the rule to the three `SKILL.md` files.
6. Run AC-5: `test_allow_readonly_psql.sh`, `test_deploy_approval_gate.sh`, `test_hook_dispatch.sh`.
7. Run AC-2 and AC-3 live from `/opt/seshat` and from a worktree. Paste the output in the PR body.
8. Gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`.

## Test matrix (`test_allow_master_gates.sh`)

Positives, cwd `/opt/seshat`:
- merge with the three required flags
- merge with `--delete-branch`
- merge with `--subject '...'` and `--body "..."` (single and double quotes, multi-line body)
- merge with all optional flags in a different order
- `ENV=cloud make rebuild SERVICE=seshat-gateway`
- `ENV=cloud make rebuild SERVICE=seshat-pwa`

Seeded negatives (each must print nothing):
- the merge and the rebuild from `.claude/worktrees/build`, `build2`, `adrs`
- a cwd that is not a git repository
- `--squash`, `--rebase`, `--admin`, `--auto`
- a missing `--match-head-commit`, a 39-character SHA, a 41-character SHA, a non-hex SHA
- a non-numeric PR number
- another SERVICE, `make rebuild` without `ENV=cloud`, `ENV=local make rebuild`
- a rebuild with extra SERVICE or a second target
- `&& git push`, `; rm x`, `|`, `&`, a redirect, a trailing newline plus a second command
- `$(...)`, `${...}` and a backtick inside `--body`, inside `--subject`, and outside the quotes
- an unterminated quote, adjacent quoted strings, a repeated optional flag
- `--repo other/repo`, `--body-file`

## Codex plan review (2026-10-30) — findings applied

- HIGH: ambient `GIT_WORK_TREE`/`GIT_DIR` made a worktree read as the main tree. The wrapper now clears
  every `GIT_*` variable before the lookup. Three seeded tests cover it. The same weakness exists in
  `deploy-approval-gate.sh`. That file is out of scope for this ticket and is noted in the handoff.
- MEDIUM: `!` inside double quotes expands when history expansion is on. The grammar no longer
  matches an unescaped `!` inside double quotes. Single quotes stay allowed.
- LOW: a NUL character is declined.
- Tests added: subdirectory of the main tree (allow), subdirectory of a worktree (decline), quoted
  metacharacters, quoted carriage return, backslash-newline in quotes, trailing carriage return.
- Not added: a symlink case and an integration run of all four Bash hooks. The role check uses the
  canonical path that git reports, and the deploy gate already has its own test.

## Risks

- The role check depends on the hook working directory. A session that changes directory into a
  worktree is correctly refused. A session that sits in a non-git directory is refused.
- The allowed `gh pr merge` can still merge a wrong PR number. The pinned head SHA limits this to the
  exact commit that master reviewed. This is the intent of the ticket.
