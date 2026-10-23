# FRE-1616 — A worker sees the skills that its tools provide

**Ticket:** FRE-1616. **Design:** ADR-0156, amendment of 2026-10-18 (FRE-1607), sections B1–B5 and V1–V6.
**Risk tier:** Standard (`src/` logic, ES template). Codex plan review required.
**Diff class:** not escalated. The diff has no production write path, no deleting code, no schema change, and no cost or governance code. The ES template change is additive.
**One PR, one phase.** No planner text changes, so no ADR-0154 D7 run.

## Scope

1. **B1.** `skills_for_tools` (`orchestrator/skills.py`) shows a skill when the worker holds every tool in `requires-tools` and every tool in `provides-tools`.
2. **B2.** Move the `bash` steps of `log-search`, `spend-and-tokens` and `turn-explanation` to `references/`. Keep one pointer line in place of each moved span. Drop `requires-tools: bash` from these three and from `model-latency`.
3. **B3.** `SubAgentCapture.skills_loaded: list[str]` in load order, and the ES template maps it as `keyword`.
4. List every edit to the moved text, line by line (V5). The list goes in the PR body.

## Findings from reading the code (decisions the ADR left open)

- V2 forbids a listed command word at the start of any inline code span in a visible body. The scan of the five visible bodies on `main` finds these places beyond the moved steps:
  - `turn-explanation` line 21: the table cell `psql`.
  - `turn-explanation` lines 154–155: the Recovery rows that name `docker logs` and `docker ps`.
  - `model-latency` line 39: the inline `jq`.
  - `turn-explanation` line 54, in Step 1: `` (`wc -c <path>` gives the bytes) ``. `wc` is not in the V2 list, so the detector cannot see it. A worker without `bash` cannot run it, so the parenthetical is removed (edit T5).
- `log-search` holds a second `bash` fence in "Index families" (the index-granularity script). B5 of the ADR names only Step 2, but V2 needs this fence out of the body too.
- Decisions:
  - **`turn-explanation` table cell:** write `psql` without backticks. One edit, listed.
  - **`turn-explanation` Recovery rows:** the six rows that belong to Steps 1b–4 move to the end of the reference file, under `## Recovery of these steps`. Rows "the tool reply holds no event" and "the same failure twice" stay. A pointer line cannot sit inside a table, so these rows are a listed removal, not a pointer swap. A test asserts that each removed row is in the reference file, verbatim.
  - **`model-latency` Step 2:** use the pointer form, and write `jq` without backticks. Two edits in one line, listed.
  - **`log-search` second fence:** the pointer for "Index families" goes in a second reference file.
- `SubAgentResult` gets `skills_loaded: tuple[str, ...] = ()`, like its other tuple fields. The loop derives it from the `loaded_skills` set: the names that one `dispatch_tool_call` adds. A skill that the parent seeded in `spec.loaded_skills` is not a load of this worker.
- AC-3 and AC-4 are live paid runs. This session does not run them. See "Not in this PR".

## Edits to the skill files (the list for V5)

| # | File | Edit |
|---|---|---|
| L1 | `skills/log-search/SKILL.md` | Front matter: delete `  requires-tools: bash`. |
| L2 | same | Rules: `Use curl only for what the tool cannot do. The table below says which.` becomes `Use curl only for what the tool cannot do.` |
| L3 | same | Step 2: the text between the heading and `## Index families` (the table, "Check a field before a curl aggregation", and its `bash` fence) moves to `references/curl-steps.md`. One pointer line stays under the heading. |
| L3a | same | Step 2: one fact line is added before the pointer. It keeps what the typed tool needs from the moved table: the tool takes only `agent-logs`, `agent-topology` and `agent-monitors-slm-health`, it reads only the allowlisted fields (fields that hold conversation text are left out on purpose), it returns at most 50 hits and reads at most 30 days, and it does not run ES\|QL or list indices (ADR B2.2). |
| L4 | same | Index families, end of paragraph 2: `To classify a family, use the tested script:`, the fence, and the paragraph after it move to `references/index-granularity.md`. One pointer line stays. The sentence break before `To classify` becomes a paragraph break. |
| S1 | `skills/spend-and-tokens/SKILL.md` | Front matter: delete `  requires-tools: bash`. |
| S2 | same | Pattern 3b: the `bash` fence and the line `If the reply has no buckets ...` move to `references/budget-snapshot.md`. One pointer line stays. |
| T1 | `skills/turn-explanation/SKILL.md` | Front matter: delete `  requires-tools: bash`. |
| T2 | same | Table "What each substrate answers": `` `psql` `` becomes `psql`. |
| T3 | same | `### Step 1b` to the end of Step 4 moves to `references/other-substrates.md`. One pointer line stays. |
| T5 | same | Step 1: the parenthetical `` (`wc -c <path>` gives the bytes) `` is removed. |
| T4 | same | Recovery: six rows move to `## Recovery of these steps` at the end of `references/other-substrates.md`. |
| M1 | `skills/model-latency/SKILL.md` | Front matter: delete `  requires-tools: bash`. |
| M2 | same | Step 2: `Read` becomes the pointer form `Past the tool's limits, and only with the `bash` tool: read`, and `` `jq` `` becomes `jq`. |

The pointer line has the ADR form: ``Past the tool's limits, and only with the `bash` tool: read `references/<file>.md`.``

## Steps

Every step names its test command. Run one pytest process at a time.

### 1. Red: tests first

1. **AC-1.** In `tests/personal_agent/orchestrator/test_worker_skill_catalog.py`:
   - Extend `_write_skill` with an optional `provides` argument.
   - Add seeded skills `provides-nothing-held` (provides `ghost_tool`, which no worker holds) and `provides-search-logs` (provides `search_logs`).
   - `general` sees `provides-search-logs`, and nobody sees `provides-nothing-held`.
   - Change `TestTheRealSkills` to assert exact sets: `general` sees `log-search`, `model-latency`, `sequential-thinking`, `spend-and-tokens`, `turn-explanation`. `researcher` sees `sequential-thinking`. The `read_skill` enum equals the catalog.
   - Add a test for ALERT and DEGRADED: the grant from `evaluate_sub_agent_tool_grant` gives a spec; the catalog is empty; the worker holds no `read_skill`.
   - Add a test that a hidden name is refused (`unknown_skill`) for `general` on the real skills (for example `service-health`, which provides `search_logs` but requires `bash`).
   - Command: `make test-file FILE=tests/personal_agent/orchestrator/test_worker_skill_catalog.py`. Expected: fails.
2. **AC-2.** New `tests/personal_agent/orchestrator/test_worker_visible_bodies.py`:
   - A detector `forbidden_forms(body) -> list[str]`: a fence labelled `bash`, `sh`, `shell`, `console` or with no label; a fence line or an inline code span that starts with `curl`, `date`, `docker`, `grep`, `jq`, `ps`, `psql` or `python3` (word boundary, so `date_histogram` and `psql` do not collide with `ps`).
   - 13 seeded fixtures, each must be flagged: 5 fence labels (`bash`, `sh`, `shell`, `console`, unlabelled with no command word) and 8 words. Four words sit in a `python` fence and four in inline code. Two negative controls must pass: a `python` fence of `search_logs(...)` calls, and `date_histogram` in a fence.
   - A test over the real skills: for each worker type, for each skill in `skills_for_tools(granted)`, the body has no forbidden form.
   - Command: `make test-file FILE=tests/personal_agent/orchestrator/test_worker_visible_bodies.py`. Expected: the real-skill test fails; the fixtures pass.
3. **AC-5, second part.** In the same new file, a test per pointer: each `references/<file>.md` named by a pointer line exists, and `read_skill(name, path)` returns it. A test that each of the six removed Recovery rows is in `references/other-substrates.md`, verbatim. Expected: fails.
4. **AC-6.**
   - In `tests/personal_agent/captains_log/test_sub_agent_capture.py`: `skills_loaded` defaults to `[]`, and records a list in order.
   - In `tests/scripts/test_es_templates.py`: `props["skills_loaded"]["type"] == "keyword"`.
   - In `test_worker_skill_catalog.py`: a scripted worker that calls `read_skill` for two seeded skills, then the same skill again, then a skill the parent seeded in `spec.loaded_skills`. The result and the written capture (patch `write_sub_agent_capture`) hold exactly the two new names, in call order.
   - Also: a `read_skill` call with a `path` and a `read_skill` of a hidden name leave `skills_loaded` empty. `_killed_result` (timeout) and the cancellation capture path carry the names loaded before the kill: test `_killed_result` with a state that holds `skills_loaded`, and `run_sub_agent` with a cancelled loop.
   - Expected: fail.

### 2. Green: B1 and B3

1. `src/personal_agent/orchestrator/skills.py`, `skills_for_tools`:
   ```python
   tools = set(held)
   return {
       name: doc
       for name, doc in _get_cache().docs.items()
       if {*doc.requires_tools, *doc.provides_tools} <= tools
   }
   ```
   Update the docstring (cite ADR-0156 D9.1 as amended by FRE-1607 B1). Update the `assemble_skill_catalog` and `_visible_skill_names` docstrings if they restate the rule.
   `tools_revealed_by(skill, held)` also calls `skills_for_tools`. A principal that reveals a tool does not hold it yet, so B1 must not apply there (`test_skill_provided_rules.py:94` expects `needs-bash` to reveal `measure_latency` to a holder of `bash`). Give `tools_revealed_by` its own requires-only filter, and keep that test unchanged. Add `test_skill_provided_rules.py` to the test commands of Step 4.
2. `src/personal_agent/orchestrator/sub_agent_types.py`: `SubAgentResult.skills_loaded: tuple[str, ...] = ()`, with a docstring entry.
3. `src/personal_agent/orchestrator/sub_agent.py`:
   - `_ToolLoopState.skills_loaded: list[str]`.
   - In the dispatch loop, around `dispatch_tool_call`: `loaded_before = frozenset(loaded_skills)` before, `state.skills_loaded.extend(sorted(loaded_skills - loaded_before))` after.
   - Both `SubAgentResult(...)` constructors (`_killed_result` and the normal path) pass `skills_loaded=tuple(state.skills_loaded)`.
   - `_emit_sub_agent_capture` passes `skills_loaded=list(result.skills_loaded)`.
4. `src/personal_agent/captains_log/capture.py`: `SubAgentCapture.skills_loaded: list[str] = Field(default_factory=list)`, with a one-line comment.
5. `docker/elasticsearch/captains-subagents-index-template.json`: `"skills_loaded": { "type": "keyword" }`, next to `tools_used`.
6. Command: `make test-file FILE=tests/personal_agent/captains_log/test_sub_agent_capture.py`, then the other two files. Expected: AC-1 and AC-6 pass. AC-2 still fails on the real skills.

### 3. Green: B2

1. Create the four reference files from the exact moved text. Use a script to cut the spans, not hand copying, so the bytes match.
2. Edit the four `SKILL.md` files per the table above.
3. Check V5 with a one-off script in the scratchpad. For each of the three skills, replace each pointer line with its reference text, apply the listed edits (L2, L4 break, T2, T4) to the old body from `git show origin/main:<path>`, and compare. Post the output in the handoff.
4. Add a short rule to `skills/SKILL_TEMPLATE.md`: a body that a worker without `bash` sees holds no `bash` command, and a new command word is added to the V2 list in the test.
5. Command: `make test-file FILE=tests/personal_agent/orchestrator/test_worker_visible_bodies.py`. Expected: pass.

### 4. Existing tests that read these bodies

Run, one process at a time:
`tests/personal_agent/orchestrator/test_latency_skill_paths.py`, `test_cost_skill_total_spend.py`, `test_telemetry_tools_visibility.py`, `test_skill_contract.py`, `test_skill_body_kept_in_turn.py`, `tests/personal_agent/governance/test_skill_commands_permissions.py`, `test_skill_script_rules.py`, `tests/docs/test_skill_scripts.py`, `test_skill_conversion.py`, `test_skill_validator.py`, `tests/test_eval/test_fre1598.py`, `test_fre1597_prompt_size.py`. The baseline on `main` is 896 passed for the first 13 files. Fix any test that pins old body text or the old catalog size. Also run the pinned planner hash test, `test_fre1549_reasoning_discipline.py`, and expect no change.

### 5. Gates and PR

`make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`. Commit. Run `feature-dev:code-reviewer` on `git diff origin/main...HEAD`. Run `security-review` only if the diff touches inputs, subprocess, files, auth, secrets or network (expected: no). Rebase on `origin/main`, push, open the PR with the V5 edit list. Post the handoff comment.

## Acceptance criteria and their proof

| AC | Proof in this PR | Proof after merge |
|---|---|---|
| AC-1 | `test_worker_skill_catalog.py` exact sets, ALERT and DEGRADED, enum, refusal, seeded `ghost_tool` skill | — |
| AC-2 | `test_worker_visible_bodies.py`: real bodies clean, 13 fixtures flagged | — |
| AC-3 | none (paid run) | Live run, after the owner's go and a preflight |
| AC-4 | Offline: the `general` catalog length before and after, and the body length of each moved skill before and after, computed from the skill files and `git show origin/main:` | The AC-4 figures that need telemetry (served token count, `system_prompt_chars`, `read_skill_invoked.body_chars`) are read from the captures and logs of the AC-3 window. They are not in this PR. |
| AC-5 | Pointer tests, row test, and the one-off rebuild script output | — |
| AC-6 | Capture, loop and template tests | Capture documents in the AC-3 window |

## Not in this PR

- **AC-3** needs the eval stack, paid calls, the owner's go and a preflight on the ticket (lifecycle rules, "Paid runs"). The PR does not start it. The handoff comment proposes the 15 + 6 goals and asks master for the owner's go.
- **AC-4**, the served token count, needs the same window.
- The planner half (FRE-1617) is a separate ticket.

## Risks

- A reference file that `read_skill` lists must pass the path rules in `read_skill_file`. Step 3 tests this per pointer.
- The `general` catalog grows by about 2,067 chars (ADR B5 estimate). Step 4 computes the exact figure. If the growth is above 2,100, stop and tell master.
- `service-health` also provides `search_logs` and still requires `bash`. B1 keeps it hidden from `general`. The test names it.
