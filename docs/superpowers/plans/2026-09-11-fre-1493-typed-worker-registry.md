# FRE-1493 — ADR-0150 T2: the typed worker registry (D2, D3, D4 removal, D5)

**Ticket:** FRE-1493 (Approved, Tier-1). **Backing ADR:** ADR-0150 D2, D3, D4, D5, "Revisions to
ADR-0149". **Blocked by:** FRE-1492 (merged, PR #1139). **Blocks:** FRE-1494 (T3).

## Scope

- A closed `WorkerType` registry (`researcher`, `general`) in a new `orchestrator/worker_types.py`.
- `PlanTask` loses `tools` and `expected_output` and gains `type` and `thoroughness`. The planner
  prompt, `_validate_plan_json`, and the fallback planner change with it.
- The gap redispatch dispatches the *other type* that declares the gap tool, once.
- `sub_agent_rounds_by_thoroughness`: the budget per level. The number moves from the system prompt
  to the task message. The countdown and the forced synthesis read the task's budget.
- The combine task is removed from the planner prompt, from `_MAX_TASKS`, and from the fallback
  planner. The D5 prompt lines, the `researcher` block, and the sibling line are added.

## Design decisions (each one is a choice the gate must see)

**DD-1 — The rounds setting follows the cap unless a level is set explicitly. Over-cap is refused.**
Master's comment offered "ship 5, clamp with a warning" for the plan to accept or overturn. This
plan overturns it:

- The field stores only explicit per-level values. Its default is empty.
  `AppConfig.sub_agent_rounds_for(level)` returns the explicit value, else
  `sub_agent_max_tool_iterations`.
- So every level equals the cap at ship, in every world. On this deployment today (cap 20, the
  FRE-1487 study value) the budget stays 20 and nothing changes on merge. After the study reverts
  the cap to 5, every level reads 5 and startup does not fail.
- A hardcoded 5 changes this deployment's live worker budget from 20 to 5 on merge. That undoes
  the owner's direction that the study limits stay raised, and it breaks D3's "no budget changes
  on merge" and AC-4 ("every value equals the cap") on this deployment.
- No number is written anywhere, so the study value cannot become a shipped default.
- A value above the cap is refused at load (a `model_validator`), as AC-2 requires. A clamp would
  let an over-cap value load, which AC-2 names as a failure. The one startup failure left is an
  explicit override above the cap. That is a misconfiguration and refusal is correct.

**DD-2 — The `researcher` block ships without its two `DONE` sentences until T3.** The ADR's
last two sentences ("To finish, reply with the single word DONE and no tool calls. You will then
be asked for your report.") need T3's voluntary-stop report-writing call. Under T2 the completed
path returns the no-tool-call reply as the report, so a worker that obeys would report the single
word `DONE` with `report_kind = synthesized` and `success = True`. T2 ships the block verbatim up
to "Stop searching when your last two searches returned the same facts." and ends it with "Then
write your report." T3 replaces that sentence with the ADR's two `DONE` sentences, with its landing.

**DD-3 — `SubAgentSpec.output_format` is removed.** Its only source was `PlanTask.expected_output`.
Its two renders are replaced: the task message line becomes "Report in text." (D5), and the forced
synthesis instruction's "Output format: {output_format}." becomes "Report in text.". T3 replaces
both for schema-backed workers.

**DD-4 — `Thoroughness` is a `Literal` in `config/settings.py`, re-exported from
`worker_types.py`.** `personal_agent.orchestrator/__init__.py` imports the executor, so config
cannot import from the orchestrator package without a cycle.

**DD-5 — The type's prompt block is chosen from `spec.worker_type` inside
`_build_sub_agent_system_prompt(spec)`.** No new spec field carries the text. The skill index
socket is still appended after it. So the system bytes are a function of the type only.

**DD-6 — The planner prompt lists each type with the part of its tool list granted now.**
Today the prompt renders the live grantable tool surface (FRE-1389 AC-1) and never advertises a
refused tool (FRE-1463). The registry render keeps that property: each type line ends with "Tools
granted now: {granted subset, or none}". A type is never called unavailable, because a `general`
worker can answer from its own knowledge with no tools (codex finding 2). The prompt also states
that no task may combine or synthesise the others' results (see DD-14).

**DD-7 — Plan parsing.** A missing or unknown `type` makes the plan invalid (fallback planner). A
missing `thoroughness` takes the type's default. An unknown `thoroughness` makes the plan invalid.
A legacy `tools` or `expected_output` key is ignored.

**DD-8 — `_MAX_TASKS = {"HYBRID": 3, "DECOMPOSE": 5}` and the slice is `tasks_raw[:max_tasks]`.**

**DD-9 — The fallback planner drops its synthesize/recommend task.** It is the combine worker. An
entity plan is one task per entity. The generic plan is one research task. Every task takes
`researcher` when any `researcher` tool is in the grantable surface, else `general`, at the type's
default thoroughness. `generate_fallback_plan` gains `sub_agent_tool_surface: Sequence[str]`.

**DD-10 — The closed redispatch.** Gap names come from `refused_tool_attempts` and
`stated_tool_gap` as today, filtered to registered tools as today. The target is the set of types
other than `spec.worker_type` that declare any gap name. If that set has exactly one type, and the
gap tool is in that type's fresh grant, dispatch that type once: same `task.goal` with the suffix
` (retry: {type} worker)`, same thoroughness, same siblings, same context, the new type's grant. Else
no replacement, and the gap stays on the original result. The replacement's own gap is never read.

**DD-11 — Observability for the live check.** `sub_agent_start` gains `worker_type`,
`thoroughness`, `round_budget`, `sibling_tasks`. `planner_completed` gains `task_types`. These are
log fields only. No ES template change.

**DD-12 — The owner's absence requirement (FRE-1493 comment, 2026-09-11).** Neutral wording. No
positive adjective. Tied to what was searched. Two added sentences, outside the ADR's verbatim
lines:

- Base prompt: "When you tried and could not determine something, saying so and naming what you
  tried and where is a complete answer for that part. It needs no apology and no guess in its
  place."
- `researcher` block: "A report that something is absent, naming what you searched and where, is
  complete for that part. It needs no apology and no substitute answer. Absence you did not search
  for is not a finding."

**DD-13 — AC-1 (the cache ratio) is proved by a unit proxy here and measured live by master.** The
unit test asserts the bytes that make the prefix: the system message and the tools array are
identical for a `quick` and a `thorough` `researcher`, and differ for a `general`. The seeded
negative renders the budget into the system prompt and the bytes differ. The live
`cache_read_tokens ≥ 0.95 × P` measurement needs the local backend. This seat does not run live LLM
calls, so that half is master's post-deploy check.

**DD-14 — The recorded 2026-09-11 plan.** ES (`agent-captains-captures-subagents-2026-09`, session
`bbc0ddaa`) shows the real plan held **two** research tasks and one combine task, plus the combine
task's gap retry. The ticket's AC-4 text says three plus one. Two fixtures: the ticket's 3+1 under
HYBRID yields 3 tasks. The real 2+1 still yields 3 tasks, the combine task included. The validator
cannot tell a combine task from a research task. For that plan the removal rests on the planner
prompt alone. This is reported at the gate, not solved here.

## Prompt text

Base system prompt (`_SUB_AGENT_SYSTEM_PROMPT`), one sentence group per line:

```
You are a focused sub-agent executing a specific sub-task. Do not ask follow-up questions.
Do not invent missing facts, inputs, tool results, or assumptions. When the evidence is not there, say what you could not determine.
Complete only the assigned task. Do not broaden its scope or solve the parent task.
Only what you write in your report reaches the parent. Nothing else you read or did survives.
When you tried and could not determine something, saying so and naming what you tried is a complete answer for that part. It needs no apology and no guess in its place.
You cannot request additional tools mid-task. If you cannot complete this task because you lack a specific tool, do the best you can with what you have, then end your response with a final line reading exactly "TOOL_GAP: <tool_name>" (one tool name, no other text on that line).
```

Budget mechanism (`_BUDGET_MECHANISM`, appended to the system prompt):

```
Your task states your budget of tool rounds. A round is one reply that calls tools; it may call several tools in parallel. After each round you will be told how much budget remains. When the budget is spent, your next reply will have no tools available, and you must write your report from the results you already hold.
```

Dropped from today's base prompt: "Be concise and direct. Respond with the requested output format
only." and "Do not add preamble or explanation beyond what was requested." D5 lists what the base
keeps, and these two are not on the list.

`researcher` block (T2 form, see DD-2 and DD-12):

```
You research one bounded question on the open web.
Prefer primary sources: the organiser, the venue, the official listing, the publisher. A news article that names its source is second. An aggregator is last, and never the only source for a claim.
Record a claim only when a fetched result states it. Quote a source's exact words only when the wording is load-bearing. Do not recap pages you merely read.
If you find nothing for part of the task, say so and say what you searched — a report that names nothing you looked for is indistinguishable from never having looked.
A report that something is absent, naming what you searched and where, is complete for that part. It needs no apology and no substitute answer. Absence you did not search for is not a finding.
Stop searching when your last two searches returned the same facts. Then write your report.
```

Task message (after the date block):

```
Task: {spec.task}
Other workers in this turn own: {sibling task names, comma-separated, or "none"}. Stay inside your task.
Thoroughness: {level}. You have a budget of {n} tool round(s).
Report in text.
```

## Steps

1. `config/settings.py`: `Thoroughness`, `THOROUGHNESS_LEVELS`, the field, `sub_agent_rounds_for`,
   the validator. `.env.example`: a commented line. → verify: `make test-file
   FILE=tests/test_config/test_sub_agent_rounds_by_thoroughness.py`.
2. `orchestrator/worker_types.py`: `WorkerType`, `WorkerTypeSpec`, `WORKER_TYPES`,
   `worker_types_declaring`. → verify: `tests/personal_agent/orchestrator/test_worker_types.py`
   (shape, and the union of the two tool lists equals the live grant set).
3. `expansion_types.PlanTask` and `sub_agent_types.SubAgentSpec` field changes; update every
   constructor in `tests/`.
4. `sub_agent.py`: prompt constants, `_build_sub_agent_system_prompt(spec)`, `_build_task_message`,
   `_SYNTHESIS_INSTRUCTION`, `_run_tool_loop` budget, `sub_agent_start` fields. → verify:
   `make test-file FILE=tests/personal_agent/orchestrator/test_sub_agent.py`.
5. `expansion_controller.py`: `_MAX_TASKS`, planner prompt, `_validate_plan_json`,
   `_compute_sub_agent_grants`, `_run_planner` (surface to fallback, `task_types`), `_run_dispatch`
   specs, `_maybe_redispatch_on_gap`. → verify: `make test-file
   FILE=tests/personal_agent/orchestrator/test_expansion_controller.py`.
6. `fallback_planner.py`. → verify: `make test-file
   FILE=tests/personal_agent/orchestrator/test_fallback_planner.py`.
7. Seeded negatives by hand (AC-7), each reverted after the red run.
8. Gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run
   --all-files`.

## Acceptance criteria → proof

| AC | Test (file :: name) | Seeded negative |
|---|---|---|
| AC-1 prefix per type | `test_sub_agent.py::TestTypedPrefix::test_same_type_workers_share_system_and_tools_bytes_across_levels`, `::test_type_boundary_changes_the_prefix` | budget rendered back into the system prompt → first test red. Live ratio: master |
| AC-2 level binds | `test_sub_agent.py::TestThoroughnessBinds::test_quick_task_spends_two_rounds_then_forced_synthesis`; `test_sub_agent_rounds_by_thoroughness.py::test_over_cap_value_is_refused_at_load` | loop reads the global cap → red (third round runs) |
| AC-3 registry types | `test_expansion_controller.py::TestPlanTypes::test_unknown_type_is_rejected`, `::test_mixed_plan_dispatches_both_with_their_tools`; `test_fallback_planner.py::TestTypeAssignment::*` | type check removed → red |
| AC-4 no limit, no combine | `test_sub_agent_rounds_by_thoroughness.py::test_every_level_equals_the_cap_by_default`, `::test_guarded_limit_defaults_unchanged`; `test_expansion_controller.py::TestNoCombineTask::*` | `+1` restored → the 3+1 fixture yields 4 |
| AC-5 siblings | `test_expansion_controller.py::TestSiblingLine::*`; `test_sub_agent.py::TestTaskMessage::test_base_prompt_carries_the_d5_lines_verbatim` | sibling line removed → red |
| AC-6 closed redispatch | `test_expansion_controller.py::TestSubAgentGapRedispatch::*` (rewritten) | — |

## Codex plan review (2026-09-11) — disposition

| # | Finding | Disposition |
|---|---|---|
| 1 | The skill index socket breaks "system bytes are a function of type" | Accepted as a wording fix. No dispatch sets it today (ADR-0150 Context: 0 characters). The docstring now names it as the one exception. |
| 2 | DD-6 conflates tool availability with type availability | Accepted. DD-6 revised: each type shows its granted tools, and no type is called unavailable. |
| 3 | DD-14 leaves the real 2+1 combine task unresolved | Not blocking. The validator cannot read meaning. The prompt now forbids a combine task in words, and a test records the limit for the gate. |
| 4 | "complete" reads as permission to stop, and the base line lacks "where" | Split. "where" added to the base line. "complete" kept: the owner's comment asks to "state that a report of absence is complete", and "for that part" limits its scope. |
| 5 | DD-3 misses the `sub_agent_start` log read | Already handled: the log field is removed. |
| 6 | Planner-prompt test updates underspecified | Accepted: tests assert the type lines, the levels, the schema keys, and that `tools` and `expected_output` are gone. |
| 7 | AC-7's live planner-selection half is missing | Not accepted. The ticket scopes AC-3 to the "fixture half". The ADR gives the labelled query set to T4 (FRE-1495). |

## Out of scope

The schema, `response_format`, the validity table, `report` on the result, synthesis-context
rendering (T3). The thoroughness numbers (FRE-1487). Memory in the worker (FRE-1469). A `recaller`
type. The ADR-0149 Status Update (master's, on merge). `SubAgentSpec.background` is never rendered
into any worker message today, so plan `constraints` never reach the worker. D5's task message
format does not include them. Reported, not changed.
