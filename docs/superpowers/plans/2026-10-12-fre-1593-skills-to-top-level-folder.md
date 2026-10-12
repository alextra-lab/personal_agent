# FRE-1593 — Move the runtime skills to a top-level `skills/` folder

**Ticket:** FRE-1593 (ADR-0156 amendment A1, task T-A). **Design intent:** ADR-0156 A1.1 to A1.5.
**Deploy class:** `seshat-gateway` rebuild. One prompt-cache miss.
**One phase, one PR.** A partial move is not allowed: the path is part of the security model.

## Facts that the plan depends on

- 29 tracked files move: 10 skill folders and `docs/skills/SKILL_TEMPLATE.md`.
- 100 files name `docs/skills`. About 60 are live. The others are history.
- 17 code sites build the path as `"docs" / "skills"`. A literal search misses them.
- No compose file mounts `docs/` or `skills/` into the gateway. The image `COPY` is the only path.
- `.gitignore` and `.dockerignore` do not hide a top-level `skills/` folder (checked).
- No relative `../` link inside the skill folders changes depth (checked).
- 8 files inside the skill folders name their own path. Their text must change, so their hashes change.

## Assumptions to flag in the handoff

1. **AC-1 and the 8 files.** A literal hash match cannot hold for the 8 files, because `SKILL.md` tells the
   model to run `python3 docs/skills/...`. A skill that keeps the old text points to a file that does not
   exist. The proof is: 21 files have an identical blob hash, and the 8 files are identical after the
   replacement `docs/skills` to `skills`. This is a reading of AC-1 that master must accept. A comment on the
   ticket states the conflict before the work starts.
2. **History stays.** `docs/architecture_decisions/`, `docs/research/`, `docs/superpowers/plans/` and
   `telemetry/evaluation/` keep the old path. Two dated records are exact-name exceptions in the CI check:
   `docs/plans/completed/2026-05-22-completed-archive.md` and
   `docs/superpowers/specs/2026-05-14-fre-343-personal-time-window-retrieval-design.md`.
   One test file keeps the old path on purpose and is an exact-name exception too:
   `tests/docs/test_old_skills_path_guard.py`, which holds the pattern. The rule test builds the old
   path from `docs/{script}`, so it needs no exception.
3. **The CI check also finds the join form** `"docs" / "skills"`, which the ticket text does not name.
   Reason: this is the form that 17 code sites use, and a test that scans an empty folder passes silently.
4. **Diff class: escalated.** The diff touches governance code (`governance/permissions.py`).

## Steps

Run `make test-file FILE=<file>` for each file. Run one pytest process at a time.

### 1. Failing tests first

1. Create `tests/docs/test_old_skills_path_guard.py` (AC-2, last bullet). It holds:
   - the check function: it reads a list of relative paths under a root and returns the files that name
     `docs/skills` or the join form, outside the exception list;
   - a test on the real repo (`git ls-files`): no hit;
   - a seeded negative matrix on `tmp_path`, one case for each path category of AC-2: `src/`, `config/`,
     `scripts/`, `tests/`, `skills/`, `.env.example`, `Dockerfile.gateway`, `docker-compose.test.yml`,
     `docs/guides/`, `docs/plans/`, `docs/superpowers/specs/`. Each case makes the check fail;
   - a form matrix: the literal, the one-line join `"docs" / "skills"`, the multi-line join, and the
     `parents[N] / "docs" / "skills"` join. Each makes the check fail;
   - a seeded pass: the same text under each of the three history directories does not fail. The history
     rule is a prefix rule for those three directories only. Every other exception is an exact file name;
   - a test that each exact-name exception still exists and still names the old path.
2. Edit `tests/scripts/test_gateway_freshness_fingerprint.py`: the fixture builds `skills/`, and a new test
   edits one file under `skills/` and asserts that the fingerprint changes.
3. Edit `tests/personal_agent/governance/test_skill_script_rules.py`:
   - constants and cases use `skills/<s>/scripts/<f>.py` and `/app/skills/...`;
   - a rule that names `docs/skills/<s>/scripts/<f>.py` is refused when the rules load;
   - the `write` and `edit` test names `/app/skills/<s>/SKILL.md`.
4. Add a loader test in `tests/personal_agent/orchestrator/test_skills.py`: `SKILLS_DIR` is
   `<repo>/skills` and the folder exists. The test `test_the_ten_agent_skills_load` already fails on an
   empty catalog, so a missing folder cannot pass.
5. Edit every other test that reads the skills folder. Make each one assert that the runtime skill set it
   scans is not empty. `tests/test_skills/test_cost_event_names.py:36` globs `skills/*/SKILL.md` from the
   docs folder, so it needs the new base and a separate non-empty check for runtime skills.
   List: `tests/docs/test_skill_conversion.py`,
   `test_skill_scripts.py`, `test_skill_validator.py`, `test_es_index_granularity.py`,
   `test_validate_mermaid_script.py`, `test_artifact_design_split.py`;
   `tests/evaluation/test_fre453_canonical_evalset.py`;
   `tests/personal_agent/governance/test_permissions_config.py`, `test_skill_commands_permissions.py`;
   `tests/personal_agent/orchestrator/test_latency_skill_paths.py`, `test_fre1549_reasoning_discipline.py`;
   `tests/personal_agent/tools/test_artifact_tools.py`; `tests/test_eval/test_fre1590_help.py`;
   `tests/test_skills/test_cost_event_names.py`; `tests/test_tools/primitives/test_bash_shell_contract.py`.
6. Red phase: run only the focused tests of steps 1 to 4 (the guard, the fingerprint, the rule refusal, the
   forbidden write and the loader test). Expected: they fail, because the folder has not moved.
   The mechanical edits of step 5 need no red run. A path edit in a comment cannot make a test fail.

### 2. Move and edit

1. `git mv docs/skills skills`. Verify: `git diff --cached -M --summary | grep -c 'rename'` is 29.
2. Edit the security paths:
   - `src/personal_agent/orchestrator/skills.py:30` `SKILLS_DIR` and the docstrings;
   - `src/personal_agent/governance/permissions.py:100` `_SKILL_SCRIPT` and the messages and docstrings;
   - `config/governance/permissions.yaml:137-157` the five rules and the comment;
   - `config/governance/tools.yaml:339` `/app/skills/**`;
   - `Dockerfile.gateway:71` `COPY skills/ skills/`.
3. Edit the stale-image guard: `scripts/eval/gateway_freshness.py:38`.
4. Edit the loaders and mounts: `scripts/render_prompt_corpus.py`, `scripts/eval/fre1581/g2_skill_choice.py`,
   `scripts/eval/fre1590/help.py`, `triggering.py`, `part2_scenarios.yaml`, `README.md`,
   `docker-compose.fre1590.yml`, and `scripts/eval/fre453_canonical_evalset/` (`README.md`, `dataset.yaml`).
5. Edit the prompt and comments: `captains_log/reflection_dspy.py:178`, `orchestrator/prompts.py:71`,
   `governance/AGENTS.md:85`, `.env.example:855`.
6. Edit the 8 files inside `skills/` (SKILL.md files, two script docstrings, `SKILL_TEMPLATE.md`).
7. Edit the live documents: `docs/guides/TELEMETRY_ELASTICSEARCH_INTEGRATION.md`,
   `docs/reference/TOOL_INTEGRATION_GUIDE.md`, `docs/reference/artifact-builder/SKILL.md`,
   `docs/specs/PROMPT_MANAGEMENT_SPEC.md`, `docs/reference/PROMPT_CORPUS.md`.
8. Edit `.github/workflows/ci.yml`: add `'skills/**'` to the `backend` filter. Without it, a change that
   touches only `skills/` skips the backend jobs.
9. Review each changed line with `git diff -M origin/main --` (the working tree against `origin/main`,
   before the commit). Do not edit a line that is not a path.

### 3. Prove the criteria

- AC-1: compare each blob of `git ls-tree -r origin/main docs/skills` with the same file in the working
  tree under `skills/`. Record 21 equal hashes and 8 files that are equal after the replacement.
  Run the loader on `skills/` and record the skill names. Compare them with the names at `origin/main`.
- AC-2: the tests of steps 1.1 to 1.3 pass.
- AC-3: master checks after the deploy. The runbook is in the handoff comment.

### 4. Gates

`make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.
Commit. Then run `feature-dev:code-reviewer` and `security-review` on `git diff origin/main...HEAD`.

## Risks

| Risk | Control |
|------|---------|
| A reference is missed and a test passes on an empty folder | The CI check covers the literal and the join forms. Each scanning test asserts a non-empty set. The loader test and the step 3 probe list the skills. |
| A form that no pattern finds, such as `DOCS.glob("skills/*")` | Step 1.5 edits the one known case. The non-empty assertions catch the others. |
| `verify_docs_links.py` stops scanning the skill markdown | The script runs from no CI job, hook or test. No automatic coverage is lost. Noted in the handoff. |
| A security rule keeps the old path | `_SKILL_SCRIPT` no longer matches `docs/skills`, so the loader refuses an old rule. Test 1.3. |
| The image build copies no skills | `Dockerfile.gateway` and the fingerprint tuple change together. Test 1.2. |
