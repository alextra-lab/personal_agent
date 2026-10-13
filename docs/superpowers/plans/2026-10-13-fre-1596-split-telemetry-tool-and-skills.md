# FRE-1596 — split `query_telemetry`, rename the skills by objective, switch on the reveal (ADR-0156 A3–A5, T-C2)

**Ticket:** FRE-1596. **ADR:** ADR-0156, amendment of 2026-10-12, A3, A4, A5.1, A5.2, A5.10.
**Blocked by (both merged):** FRE-1593 (skills in `skills/`), FRE-1595 (the reveal mechanism, simple form).
**Deploy class:** `seshat-gateway` rebuild. No schema change (the column came with FRE-1595).
**Diff class:** governance rows and permission rules change (a read-only tool). Not a write path, not a
delete path, not a cost path. Self-serve, with the security review of the permission rows.

## Scope in one table

| Part | What changes |
|---|---|
| Tool split | `query_telemetry` → `search_logs` (`search`, `count`) and `measure_model_latency` (`latency`). One module, two definitions, two executors. Allowlists, bounds, D6 codes unchanged. |
| Governance | Two `tools.yaml` tool rows, two `sub_agent_tools` grants, two `permissions.yaml` allow rules, two names in the `general` worker's tool list. |
| Switch | Both definitions set `skill_provided=True`. Seven skills carry `provides-tools`. The primary sees the tools after `read_skill`. |
| Skills | Six renamed or split into seven, each with a "Not for…" line, `provides-tools`, new `evals.yaml`, the A4.4 text fixes. Script rules follow. |
| Validator | `name_words` rule (backend word in a name), `not_for` rule (a "Not for…" line exists, names real things). |
| Consumers | Log events, FRE-1517 audit (history mapping), failure-coverage scanner, settings help, eval harnesses, tests. |
| Worker capture | `scripts/eval/fre1537/capture.py` gets `worker_body`. |

## Design decisions

1. **One module.** `src/personal_agent/tools/telemetry_query.py` keeps its name and its single source of
   allowlists. Two `ToolDefinition`s and two executors are added. A file rename would only churn imports.
2. **`measure_model_latency` has no `action`.** `since` is required. `search_logs` keeps `action`, an enum of
   `search` and `count`.
3. **Foreign arguments.** Each executor takes only its own arguments, so `_refuse_foreign_args`,
   `_ES_ONLY_ARGS` and `_TEMPO_ONLY_ARGS` go. The tool executor already filters unknown arguments
   (`tools/executor.py`: `tool_call_invalid_parameters_filtered`). `search_logs(action="latency")` is
   refused, and the message names `measure_model_latency`.
4. **Log events are renamed with the tool:** `search_logs_{started,refused,connect_failed,timeout,failed,rejected}`
   and `measure_model_latency_{started,refused,connect_failed,timeout,failed,rejected}`. Dashboards keep the
   old name as history, with no alias (ticket scope 5).
5. **The event-history mapping (AC-5).** Two places must read old data and keep the old name, each as a
   named constant: the FRE-1517 audit (`scripts/eval/fre1517/tool_audit/buckets.py`, `calls.py`) and the
   FRE-1594 spike reading a stored capture (`scripts/eval/fre1594/reveal.py`: `REMOVED_TOOL`). The guard test
   lists each file by exact name.
6. **Validator shape.** Two pure functions in `orchestrator/skills.py`, as `provider_violations` is:
   `name_violations(names)` and `not_for_violations(descriptions, known)`. `validate_skill` also reports the
   rules `name_words` and `not_for` (a line exists) for one folder, so the per-skill parametrized test covers
   every skill. A "Not for…" line is the text from `Not for` to the end of the description. A name inside it
   must be in backticks, so ordinary hyphenated words (`model-call`) are not read as names.
7. **Cache hit rate text.** The client records `input_tokens` that include the cached tokens on every path
   in this repository (llama.cpp `prompt_tokens`; litellm adds Anthropic cache reads to `prompt_tokens`;
   OpenAI reports cached tokens inside `prompt_tokens`). `spend-and-tokens` gives `cached / input` and a check
   that detects a provider whose input excludes the cache. The ADR's second formula stays only as that check.
   Recorded as a deviation in the handoff.
8. **Script file names stay** (`es_index_granularity.py` and the others). A3 governs skill and tool names.
   Only the folder part of each skill-script rule changes.

## Steps (TDD: each test fails first)

1. **Tool tests, then the split.**
   - Edit `tests/personal_agent/tools/test_telemetry_query.py` and `test_telemetry_query_tempo.py` to the new
     API. Add: both tools are `skill_provided`; parameter sets; `search_logs(action="latency")` refused with
     `measure_model_latency` in the message; each description has a "Not for…" line naming the other tool;
     the renamed events.
   - Implement in `telemetry_query.py` and `tools/__init__.py`.
   - Run: `make test-file FILE=tests/personal_agent/tools/test_telemetry_query.py`, then the Tempo file.
2. **Governance and workers.**
   - `config/governance/tools.yaml`, `config/governance/permissions.yaml`,
     `src/personal_agent/orchestrator/worker_types.py`, settings help text, `.env.example`,
     `docker-compose.eval.yml` comments.
   - Tests: `tests/test_config/test_governance_loader.py`, `tests/personal_agent/governance/test_permissions_config.py`,
     `test_sub_agent_tools.py`, `tests/personal_agent/orchestrator/test_worker_types.py`,
     `test_worker_tool_split.py`, `test_expansion_controller.py`, `test_worker_skill_catalog.py`,
     `tests/personal_agent/tools/test_tool_contract.py`, `tests/personal_agent/grounding/test_source_registry.py`
     (one non-citable test for each tool), `tests/scripts/test_fre1564_telemetry_routing_probe.py`.
3. **Validator (functions and seeded negatives only).** Tests first in
   `tests/personal_agent/orchestrator/test_skill_naming_rules.py`: the seeded cases that use a temporary
   skills folder. Then `orchestrator/skills.py`. The real-inventory assertions (every real name passes, every
   real skill and tool has a real "Not for…" line, the provider set) are written in Step 4, so the tree is
   never red because a rule runs before the renames land.
4. **Skills.**
   - `git mv` the four folders that keep one identity: `query-elasticsearch`→`log-search`,
     `turn-forensics`→`turn-explanation`, `infrastructure-health`→`service-health`,
     `neo4j-direct`→`graph-inspection`. `git mv skills/query-tempo/references/latency-recipe.md` to
     `skills/model-latency/references/`. New folders: `model-latency`, `spend-and-tokens`,
     `interaction-outcomes`. Delete `query-tempo` and `self-telemetry` after their content moves.
   - Rewrite each `SKILL.md` and `evals.yaml` (at least 3 scenarios that need the skill, not the tool).
   - Add a "Not for…" line to `mermaid-diagrams`, `sequential-thinking`, `system-diagnostics`,
     `system-metrics` (backticks around the named skill or tool).
   - `permissions.yaml` skill-script rules follow the four moved folders.
   - Tests: `tests/docs/test_skill_conversion.py`, `test_skill_scripts.py`, `test_skill_validator.py`,
     `tests/personal_agent/governance/test_skill_script_rules.py`, `test_skill_commands_permissions.py`,
     `tests/personal_agent/orchestrator/test_skill_contract.py` (line 73 indexes `query-elasticsearch`),
     `test_latency_skill_paths.py`, `test_cost_skill_total_spend.py`, `test_skill_body_kept_in_turn.py`,
     `tests/evaluation/*`, `tests/test_eval/test_fre1590_*`, `test_fre1581_g2.py`. Find the rest with a
     search for each old skill name outside the history folders, then with `make test`.
   - Real-inventory assertions (AC-1, AC-2, AC-4), each a test that fails before the renames:
     - every real skill name and registered tool name passes `name_violations`;
     - every real skill, and every skill-provided tool description, has a "Not for…" line whose backticked
       names are all real skills or tools;
     - the set of `provides-tools` values over all real skills equals `{search_logs, measure_model_latency}`,
       and each of the two has at least one provider.
5. **Consumers of the old tool name.** `scripts/eval/fre1517/tool_audit/{buckets,calls}.py` (history
   mapping), `scripts/check_tool_failure_coverage.py`, `src/personal_agent/tools/read_skill.py` and
   `captains_log/reflection_dspy.py` (example skill names), `scripts/eval/fre453_canonical_evalset/dataset.yaml`,
   `scripts/eval/fre1564/prompt_cost.py`, `scripts/eval/fre1590/*`, the FRE-1594 spike harness (imports and
   the history constant), `docs/reference/PROMPT_CORPUS.md` (regenerate with `scripts/render_prompt_corpus.py`),
   live guides.
6. **Worker capture (AC-3).** `scripts/eval/fre1537/capture.py`: `worker_body(call_files, task)` and
   `tool_names(body)`. Tests in `tests/test_eval/test_fre1537_capture.py` use recorded-body files. A second
   test records the real request: it runs the primary's `step_llm_call` and a `general` worker's loop in
   process with a recording model client, and asserts the `tools` argument that each one sends. So the test
   checks request assembly, not only the registry. The live capture gateway run belongs to T-D1 (FRE-1597).
7. **AC-5 guard.** `tests/personal_agent/tools/test_old_tool_name_guard.py` scans `src/`, `config/`,
   `scripts/`, `tests/`, `skills/`, `.env.example`, `Dockerfile*`, `docker-compose*` and every file under
   `docs/` except `docs/architecture_decisions/`, `docs/research/` and `docs/superpowers/plans/`. It allows
   an exact line, not a whole file: each history file keeps the old name on one line, a named constant, and
   the guard lists that file with that exact line. A second occurrence in an allowed file fails. A seeded
   file fails. The guard builds the old name from two parts, so its own text does not match.
8. **Gates.** `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`.
   Commit, then review `git diff origin/main...HEAD` with `feature-dev:code-reviewer` and `security-review`.

## Acceptance criteria → test

| AC | Case | Test |
|---|---|---|
| AC-1 | `query-elasticsearch` and `query_telemetry` fail; every real skill and tool name passes | `tests/personal_agent/orchestrator/test_skill_naming_rules.py::test_seeded_backend_names_fail`, `::test_the_real_names_pass` |
| AC-2 | a seeded line that names a missing skill fails; every skill and skill-provided tool has a real line | `::test_a_not_for_line_that_names_a_missing_skill_fails`, `::test_every_skill_and_skill_provided_tool_names_something_real` |
| AC-3 | the first primary request holds neither tool; a `general` worker request holds both | `tests/personal_agent/orchestrator/test_telemetry_tools_visibility.py` (in-process recorded requests), `tests/test_eval/test_fre1537_capture.py::test_worker_body_*` |
| AC-4 | each tool has a provider; the set of `provides-tools` values equals the two names; a seeded orphan fails | `test_telemetry_tools_visibility.py::test_each_tool_has_a_provider_and_no_third_name_is_provided`, `test_skill_provided_rules.py` (existing seeded cases) |
| AC-5 | the old name is only on the named history lines | `tests/personal_agent/tools/test_old_tool_name_guard.py` |

## Codex plan review (2026-10-13)

No critical finding. Dispositions:

1. Medium — `test_skill_contract.py:73` indexes `query-elasticsearch`. **Accepted:** listed in Step 4.
2. High — the validator ran over the real inventory before the renames. **Accepted:** Step 3 holds the
   functions and seeded cases only. The real-inventory assertions move to Step 4.
3. High — AC-3 used registry lists, not requests. **Accepted in part:** Step 6 records the real request in
   process. The live capture gateway needs the eval stack and the owner's OK, so T-D1 owns it. The handoff
   says so.
4. High — AC-4 did not bound the provider set. **Accepted:** the set equals the two names.
5. High — AC-5 allowed a whole file. **Accepted:** the guard allows one exact line per history file, and it
   scans the full scope that the ticket names.
6. No finding: primary sees every skill; the worker list is closed; the four modes stay; governance rows,
   rules and exact-name script rules are complete; AC-1 and AC-2 seeded cases are present.

## Not in this ticket

- The live capture run, the routing run and the cache bound belong to FRE-1597 (T-D1) and FRE-1598 (T-D2).
  The build seat records the structural proof only.
- Grafana dashboards keep the old `tool_name` as a separate series.

## Deploy

`seshat-gateway` rebuild. No migration. One prompt-cache miss from the changed `tools` array. The primary
loses the `query_telemetry` definition (about 870 tokens) and gains three catalog lines.
