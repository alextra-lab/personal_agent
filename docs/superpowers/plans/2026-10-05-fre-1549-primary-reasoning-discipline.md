# FRE-1549 — the primary applies the sequential-thinking discipline on every non-simple turn

Ticket: FRE-1549 (supersedes the delivery path of FRE-1402, not its intent).
Tier: Standard (edits a `src/` prompt; planner prompt must stay byte-identical, ADR-0154).

## Scope

1. Add one constant `REASONING_DISCIPLINE_PROMPT` to `src/personal_agent/orchestrator/prompts.py`.
   About five lines. A standing instruction the model applies itself. No keyword, no classifier.
2. Splice it into the primary `system_prompt` seed in `src/personal_agent/orchestrator/executor.py`
   (the line `system_prompt: str | None = GROUNDING_CONTRACT_PROMPT`, about line 6438).
   That seed is the STATIC part of the prompt, before `inner_system_before_memory` is captured,
   so the cached prefix holds. The text has no per-turn value in it.
3. Keep `docs/skills/sequential-thinking.md`. Add one provenance line that points to FRE-1549.
4. Do not edit `_build_planner_system_prompt` or `_PLANNER_BRIEFING_RULES`
   (`orchestrator/expansion_controller.py`). Do not edit `config/governance/tools.yaml`.
   Do not add a `tools` key.

## Prompt text (draft)

```
## Reasoning
For anything beyond a simple question, work out the steps before you answer. Keep that
working to yourself: an ordinary linear chain of reasoning stays invisible, and a short or
conversational reply gets no numbered steps and no scaffold.
When the obvious first approach to the problem really fails, show it in the answer in one
short line: the step you tried, and what was wrong with it, before the approach that works.
Show a false start only if you actually took one in your own reasoning. Never invent a
wrong step you did not take: a manufactured one is confabulation, and showing none is better.
```

## Steps (TDD)

| # | Step | Verify |
|---|------|--------|
| 1 | Write `tests/personal_agent/orchestrator/test_fre1549_reasoning_discipline.py` (below). | `make test-file FILE=tests/personal_agent/orchestrator/test_fre1549_reasoning_discipline.py` — red state: collection fails with ImportError, because the test imports `REASONING_DISCIPLINE_PROMPT` (added in step 2). |
| 2 | Add `REASONING_DISCIPLINE_PROMPT` to `prompts.py`. | import succeeds |
| 3 | Splice into the executor seed. | step 1 command — all PASS |
| 4 | Edit the provenance block of `docs/skills/sequential-thinking.md`. | `make test-file FILE=tests/personal_agent/orchestrator/test_skills.py` — PASS |
| 5 | Run gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`. | all exit 0 |
| 6 | Commit, self-review (`feature-dev:code-reviewer`) against `git diff origin/main...HEAD`, fix findings. | no confirmed finding open |
| 7 | Rebase on `origin/main`, push, open PR, post the handoff comment on FRE-1549. | PR open |

## Tests and acceptance criteria

AC-3, AC-4 and AC-5 are live turns. Master asks the owner for them after deploy. The build cannot fire a live gateway turn (owner OK needed).

- **AC-1** — drive the real `execute_task_safe` pipeline (the harness in
  `test_grounding_contract_prompt.py`), with and without tools, on a message that holds no skill
  keyword. Assert the first `respond()` call's `system_prompt` holds `## Reasoning`,
  `false start` and `Never invent`. Assert the same text is not in the last user message
  (the volatile part) and that the prefix before the volatile tail is the same for two
  different user messages.
- **AC-2** — assert `sha256(_build_planner_system_prompt(["web_search", "run_python"]))` equals
  `af31d592...6ae5`, and for `[]` equals `b2da2a94...f7e9` (full values in the test). Both were
  computed on unchanged `origin/main` (e0bb6e7b) before any `src/` edit, with `get_settings`
  stubbed to fixed round budgets so an `AGENT_SUB_AGENT_*` override cannot move them. Also assert the planner prompt holds no
  `REASONING_DISCIPLINE_PROMPT`.
- **AC-6** — `config/governance/tools.yaml` has no `sequential-thinking` or `sequential_thinking`
  key (the FRE-1402 test also covers this), and the skill declares `tools == ()`.

## Plan review

Codex was unavailable (3 attempts: capacity error twice, then an unsupported-model error). A
read-only `feature-dev:code-architect` review replaced it. Findings folded in: the AC-1 test
checks the `static_prefix_hash` sent with the call and varies memory and clock; the AC-2 hash is
pinned under stubbed settings; a trivially true tools test was dropped. Left out on purpose: a
`component_ids` taxonomy entry (an audit label, out of scope for this ticket). Sub-agent, planner,
router and artifact prompts are separate call sites and are not targets.

## Risks

- The primary `static_prefix_hash` changes on deploy, so one cache break on `orchestrator.primary`
  is expected (`observability/cache_erosion/monitor.py`).

- The rule makes the model more likely to write scaffold on short turns. The text says the
  opposite in two places. AC-4 and AC-5 (live) are the check.
- The planner hash depends on `sub_agent_rounds_for` settings. The test pins the value with the
  test environment's default settings. If an env override moves it, the test fails loudly, not silently.
