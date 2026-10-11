# FRE-1589 — the absolute `/app/` form of a reviewed skill script runs without a card

**Ticket:** FRE-1589 (Approved, master Tier-3 bugfix grant) · **Backing:** ADR-0156 D8.2, FRE-1585 amendment
**Tier:** Standard (governance logic in `src/`) → Codex plan review required.

## Problem

`read_skill` returns `base_dir = /app/docs/skills/<skill>`. The model then runs
`python3 /app/docs/skills/<skill>/scripts/<file>.py …`. The allow rule names
`python3 docs/skills/<skill>/scripts/<file>.py *`, and the matcher compares words exactly, so the
call asks (trace af6191140444476c992911290d718aaa).

## Design

The change is in the matcher, `PermissionRule.allows_subcommand` in
`src/personal_agent/governance/permissions.py`. The loader and the rules file do not change.

1. A skill-script rule is an allow rule with `bash_prefix`, two words, `bash_words[0] == "python3"`
   and `_SKILL_SCRIPT.fullmatch(bash_words[1])`. Only `_check_allow_rule` lets such a rule load,
   and the matcher checks the form again so that it does not depend on the loader.
2. For such a rule, a subcommand also matches when its words start with
   `("python3", "/app/" + bash_words[1])`. The comparison is exact string equality. No
   normalisation, no symlink resolution, no `..` handling. So `/app//docs/…`, `/app/./docs/…`,
   `/app/docs/…/../…`, `/opt/seshat/docs/…` and `/app/docs/…` for a script with no rule do not
   match.
3. **Guard (beyond the ticket text):** the alias applies only when the bash working directory is
   `/app`. `bash.py:360` calls `create_subprocess_exec` with no `cwd=`, and
   `Dockerfile.gateway` sets `WORKDIR /app`, so in the image the two forms name one file. On the
   VPS host, `/app` exists (root-owned, holds `agent_workspace/`) and is not the repo, so for a
   `make dev` run the `/app/` form names a different file and must keep asking.
   The working directory is read through one new function `_bash_workdir() -> Path` (returns
   `Path.cwd()`). `_skill_script_refusal` uses the same function, so the loader and the matcher
   share one source for "the directory where bash starts". Tests patch this function.
4. The alias inherits the rule's modes and reason, because it is the same rule. The decision's
   `rule` field names the rule as written.
5. Every other part of D8.2 applies unchanged: `sub.refusal` (globs, expansions, redirections,
   assignments) and separators are decided before the words are compared.

Constant: `_IMAGE_WORKDIR = "/app"`, with a comment that names `Dockerfile.gateway` and `bash.py`.

## Steps

1. Tests first, in `tests/personal_agent/governance/test_skill_script_rules.py`:
   - AC-1: for each of the 5 scripts, the documented command with the `/app/` prefix evaluates to
     allow with `_bash_workdir` patched to `/app`, in NORMAL/ALERT/DEGRADED for 4 scripts and
     NORMAL only for `validate_mermaid.py` (ALERT asks).
   - AC-2: remove `f"python3 /app/{_PROBE} all"` from the ask list (comment: FRE-1589 allows it
     when bash starts in `/app`). New parametrized refusals with the workdir patched to `/app`:
     `/app/./docs/…`, `/app//docs/…`, `/app/docs/skills/x/scripts/../../y/scripts/z.py`,
     `/app/docs/skills/neo4j-direct/scripts/../../infrastructure-health/scripts/probe_services.py`,
     `/opt/seshat/docs/…`, `/app/docs/skills/neo4j-direct/scripts/other.py`, `/app` + glob,
     `/app/…` plus `; rm x`, `$(id)`, `> /tmp/out`, `PATH=/tmp` assignment, `python /app/…`,
     `python3 -I /app/…`, `/app/docs/…x` (suffix), `app/docs/…` (no leading slash).
   - Guard: with `_bash_workdir` patched to the repo (not `/app`), `python3 /app/{_PROBE} all`
     asks. The test does not depend on the directory pytest starts in.
   - Codex plan review: the comparison is on the argv word after quote removal, so
     `"/app/docs/…"` and `/app/'docs'/…` are the same word and are allowed (a test states it).
     Refusals added: backslash-newline inside the path, ANSI-C quoting `$'…'`, and Unicode
     lookalike slashes (U+2215, U+FF0F).
   - Loader: `bash(python3 /app/{_CYPHER} *)` stays refused (existing case, unchanged).
   Run `make test-file FILE=tests/personal_agent/governance/test_skill_script_rules.py` → the new
   AC-1 cases fail.
2. Implement in `permissions.py` (constant, `_bash_workdir`, alias branch in
   `allows_subcommand`, `_skill_script_refusal` reads `_bash_workdir()`). Rerun the file → pass.
3. Docs: ADR-0156 FRE-1585 amendment bullet "The matcher is unchanged" gets a dated FRE-1589
   note. `config/governance/permissions.yaml` comment and `src/personal_agent/governance/AGENTS.md`
   get one sentence each.
4. Gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`,
   `pre-commit run --all-files`. Commit, then self-review (`feature-dev:code-reviewer`,
   `security-review`, because the diff changes a permission decision).

## Acceptance criteria → proof

| AC | Proof |
|----|-------|
| AC-1 absolute form allowed, same modes | `test_ac1_the_absolute_form_runs_without_a_card` (5 scripts × modes), `validate_mermaid` NORMAL-only case |
| AC-2 nothing else gains an allow | existing `test_ac2_every_other_python3_command_asks` minus the `/app/` case, plus `test_ac2_no_other_absolute_form_is_allowed` and the guard test |
| AC-3 live, no card | master after deploy: owner asks the PWA for an infrastructure health check; `permission_evaluated` shows allow |

## Diff class

Escalated: governance code (a permission decision). Flag for owner `/code-review ultra` in the PR
body and the handoff.
