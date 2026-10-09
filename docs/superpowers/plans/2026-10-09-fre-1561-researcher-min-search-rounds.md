# FRE-1561 — Researcher minimum-search-rounds variant (setting, default off)

Ticket: FRE-1561. Backing design: ADR-0150 (worker types and round budgets), D3 and D5.
Study source: FRE-1517 stage 2, findings F7 and F8.

## Scope of this PR

This PR delivers item 1 only. The A/B on Gemma runs under FRE-1517 stage 2c, by explore.

Deliverables:

- One setting. The default leaves the worker system prompt byte-identical to today.
- One variant of the researcher stop rule, rendered when the setting is above 0.
- AC-1 and AC-2 (unit level) as tests.

The default does not change under this ticket (AC-6).

## Proposal (stated before any run, as the ticket requires)

Setting:

- Python name: `sub_agent_researcher_min_search_rounds`
- Env name: `AGENT_SUB_AGENT_RESEARCHER_MIN_SEARCH_ROUNDS`
- Type: `int`, `ge=0`, default `0`. The value `0` means off.

Proposed value for the A/B variant arm: **5**.

Reason for 5: the Flash-Next control ended its workers after 5 to 10 rounds. The Gemma workers
ended after 1 to 4 rounds. The value 5 is the lowest round count that the control reached.

The unit is a *round*, not a search. A round is one reply that calls tools, and it can call
several tools in parallel. The round is the unit that `_BUDGET_MECHANISM` and the budget line
already use, so the worker reads one unit only.

Variant text (the new paragraph sits between the existing body and the existing stop rule):

```
When your thoroughness is standard, you must make at least 5 rounds of searches before you apply the stop rule below, unless your budget ends first. Use a different query in each round. A first search that looks complete is not a reason to stop.
```

The existing stop rule follows it, unchanged:

```
Stop searching when your last two searches returned the same facts. To finish, reply with the single word DONE and no tool calls. You will then be asked for your report.
```

Why the text names the level: the system prompt is shared by all thoroughness levels
(ADR-0150 revision of ADR-0149 AC-4a). The level is stated in the task message. A flat
"search 5 times" rule would also bind `quick` workers, which have a smaller budget. The text
therefore names `standard`. `quick` and `thorough` workers keep today's rule.

Prefix-cache effect: the setting is a process constant. Every researcher in one process still
renders the same bytes, so the shared prefix across a fan-out holds.

## Design

`worker_types.py` stays pure. It gains one function. `sub_agent.py` reads the setting and
passes it in.

1. Split `_RESEARCHER_BLOCK` into `_RESEARCHER_BODY` (every line up to and including
   "Absence you did not search for is not a finding.\n") and `_RESEARCHER_STOP_RULE` (the last
   sentence group). Define `_RESEARCHER_BLOCK = _RESEARCHER_BODY + _RESEARCHER_STOP_RULE`. The
   bytes of `_RESEARCHER_BLOCK` do not change.
2. Add `_RESEARCHER_MIN_ROUNDS_RULE` (a template with `{n}`, ending in `"\n"`).
3. Add `render_prompt_block(worker_type, researcher_min_search_rounds=0) -> str`. It returns
   `WORKER_TYPES[worker_type].prompt_block` unless the type is `RESEARCHER` and the number is
   above 0. In that case it returns body + rule + stop rule.
4. `_build_sub_agent_system_prompt` calls `render_prompt_block(spec.worker_type,
   settings.sub_agent_researcher_min_search_rounds)` in place of the direct `prompt_block` read.
5. Add the setting to `settings.py` (next to `sub_agent_rounds_by_thoroughness`) and a
   commented entry to `.env.example`.

`WORKER_TYPES[...].prompt_block` stays the default text. The planner-facing registry
(`expansion_controller`, `fallback_planner`) reads descriptions, tools and default levels only.
It does not read `prompt_block`. No change there.

## Steps (TDD)

1. Write the failing tests (below). Run them. Confirm they fail.
   `uv run pytest tests/personal_agent/orchestrator/test_worker_types.py tests/personal_agent/orchestrator/test_sub_agent.py -k "min_search or default_prompt" -q`
2. Implement design items 1 to 5.
3. Run the same command. Confirm the tests pass.
4. Run the gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`,
   `pre-commit run --all-files`.

## Tests

In `tests/personal_agent/orchestrator/test_worker_types.py`:

- `test_default_researcher_block_is_byte_identical_to_main` — **AC-1.** Compares
  `render_prompt_block(RESEARCHER)` and `WORKER_TYPES[RESEARCHER].prompt_block` with a literal
  copy of today's text taken from `origin/main`. Fails if any byte changes.
- `test_variant_renders_the_setting_value` — the variant holds "at least 3 rounds" for 3 and
  "at least 5 rounds" for 5. A hard-coded number fails this test.
- `test_zero_leaves_every_type_unchanged` — `render_prompt_block(t, 0)` equals the registry
  block for both types.
- `test_variant_adds_the_rule_before_the_stop_rule` — with 5, the block holds the exact variant
  paragraph, the paragraph sits before the stop rule, and the body and stop rule are intact.
- `test_variant_does_not_touch_the_general_type` — `render_prompt_block(GENERAL, 5)` is `""`.

In `tests/personal_agent/orchestrator/test_sub_agent.py` (next to
`test_researcher_gets_its_block_and_general_gets_none`):

- `test_system_prompt_default_is_todays_text` — **AC-1, worker level.** With the setting at its
  default, the full system message (base, budget mechanism, type block, separators) has the
  length and SHA-256 that unchanged `origin/main` code produced. Researcher: 2116 characters,
  `de436ddd...1538`. General: 1180 characters, `00b9e9dc...bed7`.
- `test_system_prompt_holds_the_variant_when_the_setting_is_on` — **AC-2, unit level.** With the
  setting monkeypatched to 5, the first model call's system message holds the variant paragraph.
  With the setting at 0, it does not.
- `test_variant_is_the_same_bytes_at_every_level` — the system message is equal for `quick`,
  `standard` and `thorough` with the setting on (ADR-0150 prefix guarantee).

In `tests/personal_agent/config/` (next to `test_planner_input_settings.py`), two tests:

- The default is `0`. **AC-6.**
- `AGENT_SUB_AGENT_RESEARCHER_MIN_SEARCH_ROUNDS=3` loads as `3`, and `-1` is refused.

Docstring: update `_build_sub_agent_system_prompt` so it names the setting as a second input.

## Known limits (from the codex plan review)

- The rule is a prompt instruction, not an enforced floor. A round counts when the model
  replies with tool calls, so five rounds can hold more than five searches, or fewer than five
  successful ones. The A/B measures whether the instruction changes behaviour. That is the
  ticket's question. An enforced floor is a different design and needs the owner's decision.
- Captures keep the system-prompt character count and the thoroughness, not the text. The
  arms differ in that count, so explore can tell them apart. The PR states the exact
  difference.
- The value 5 is a hypothesis for the A/B, not an optimum. The source study has one replicate
  per model.

## Out of scope

- The A/B run, AC-2 live, AC-3, AC-4, AC-5 (FRE-1517 stage 2c, explore).
- Any change to the default value (the owner decides, after the A/B).
- A min-search rule for `general` workers.
