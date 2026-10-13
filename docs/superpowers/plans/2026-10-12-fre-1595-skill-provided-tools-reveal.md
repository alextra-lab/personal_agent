# FRE-1595 — skill-provided tools: the reveal mechanism (ADR-0156 A5, T-C1)

**Ticket:** FRE-1595. **ADR:** ADR-0156, amendment of 2026-10-12, A5.3–A5.6 and A5.8.
**Input:** `docs/research/2026-10-12-fre-1594-reveal-spike.md` — the spike chose the **simple form for both
dialects**. So this plan builds the simple form only: a revealed tool joins the primary's `tools` array for the
rest of the session. The cache-safe form is not built, and AC-3 (AC-R4) does not apply.

No production tool becomes skill-provided here (T-C2 does that). Every test uses a test tool and a seeded skill.

## Scope

- `ToolDefinition.skill_provided` marks a tool. A skill names the tools that it reveals in
  `metadata.provides-tools`, a comma-separated list.
- `sessions.revealed_tools TEXT[] NOT NULL DEFAULT '{}'`: `init.sql`, migration `0033`, `SessionModel`.
- `SessionRepository.add_revealed_tools` (one `UPDATE`, set union) and `load_revealed_tools`. Both filter on
  `session_id` and `user_id`. `prune_expired` clears the column.
- `step_init` loads the set into `ExecutionContext.revealed_tools` once per turn. A load error gives an empty set
  and a log event.
- The primary's check order for a call to a skill-provided tool: registry and arguments → the reveal → the mode →
  the loop gate → D8 rules and card → execution.
- A successful body read by `read_skill` reveals the tools of that skill: in the context at once, in Postgres
  next. A failed add keeps the reveal for the turn and logs the failure.
- A validator checks the `provides-tools` rules.

## Design decisions

1. **Registry filter.** `list_tools` and `get_tool_definitions_for_llm` take
   `revealed: Collection[str] | None = ()`. A skill-provided tool is listed only if its name is in `revealed`.
   `None` lists every skill-provided tool. This follows the existing idiom (`mode=None` lists every mode,
   `held=None` lists every skill). The worker loop passes `revealed=None`, then filters by its closed tool list
   as it does today (A5.4: workers do not read the record). Every other caller keeps the default `()`, so the
   tool-awareness prompt never names an unrevealed tool.
2. **The two primary listings in `step_llm_call`** (the tool list and the forced-synthesis list) both pass
   `revealed=ctx.revealed_tools`, so the forced-synthesis request keeps the same `tools` as the turn.
3. **Reveal point.** Phase 3 of `step_tool_execution` (sequential), on a `read_skill` result that holds `body`.
   The names come from `tools_revealed_by(skill_name, held=None)`: the primary sees every skill (its catalog is
   `assemble_skill_catalog(None)`). New names are added to `ctx.revealed_tools`, then persisted.
4. **Same-response refusal.** Phase 1 reads `ctx.revealed_tools` before Phase 2 dispatches anything. So a call in
   the same response as the revealing `read_skill` is refused, independent of execution order.
5. **Step 2 and step 3 in Phase 1, before the loop gate, for skill-provided tools only.**
   - Step 2: the name is not in `ctx.revealed_tools` → D6 error, code `tool_not_revealed` (new `DispatchCode`).
     The message lists every provider skill (`providers_of`). `retryable=True`, as `missing_parameter`: a
     follow-up call can succeed. The next action: load one of the skills with `read_skill`, then call the tool
     in a later response.
   - Step 3: the live mode (`get_mode_manager()`, the same source as `execute_tool`) does not allow the tool →
     the existing `mode_refused` error. The mode logic moves out of `_check_permissions` into a public
     `mode_refusal(tool_name, tool_def, mode, governance_config)` in `tools/executor.py`, and both call it.
   - A refused call goes through `undispatched_failure` and `_record_undispatched_invocation`. It never reaches
     `check_before` (no loop count) or `dispatch_tool_call` (no card).
   - The registry used is the module's `_tool_registry` (the one `step_llm_call` lists from), so the listing and
     the check agree.
6. **Load filter.** A name in the record that is not a registered skill-provided tool is dropped at load, with a
   log event `revealed_tools_stale_names_ignored`.
7. **No user id or no UUID session id** (CLI, some tests) → no load and no add, with a log event. The reveal still
   holds for the turn.
8. **PATCH.** `SessionUpdate` gets no field. Pydantic ignores an unknown field, and `update()` writes only the
   columns it is given, so a PATCH cannot set or clear the column and a `metadata` PATCH leaves it.
9. **Concurrency.** `UPDATE sessions SET revealed_tools = ARRAY(SELECT DISTINCT n FROM
   unnest(revealed_tools || CAST(:names AS TEXT[])) AS n ORDER BY n) WHERE session_id = :sid AND user_id = :uid`.
   Under READ COMMITTED a second concurrent `UPDATE` waits for the row lock and re-evaluates on the new row, so
   two reveals both persist.

## Steps (TDD: each test fails first)

1. **ToolDefinition + registry.** `tools/types.py` (`skill_provided: bool = False`), `tools/registry.py`
   (`revealed`), `orchestrator/sub_agent.py:_build_tool_defs` (`revealed=None`).
   Tests: `tests/test_tools/test_registry.py` — hidden by default, listed when revealed, `None` lists all, mode
   still filters a revealed tool.
2. **Skills.** `orchestrator/skills.py`: `SkillDoc.provides_tools`, `providers_of`, `tools_revealed_by`,
   `provider_violations(skills, skill_provided)`. Tests: `tests/personal_agent/orchestrator/test_skills.py`
   (parse), `tests/personal_agent/orchestrator/test_skill_provided_rules.py` (AC-2 seeded cases + real repo).
3. **Schema.** `docker/postgres/init.sql`, `docker/postgres/migrations/0033_sessions_revealed_tools.sql`,
   `service/models.py`. Tests: `tests/migrations/test_0033_sessions_revealed_tools_migration.py` (column equal
   to the `init.sql` column; idempotent), plus the existing `test_init_sql_model_parity.py`. Apply 0033 to the
   test stack (:5433) with the admin URL.
4. **Repository.** `add_revealed_tools`, `load_revealed_tools`, `prune_expired` clears. Tests (real Postgres
   :5433): `tests/personal_agent/service/repositories/test_session_revealed_tools.py` — union, 10 concurrent
   adds all persist, wrong `user_id` reads and writes nothing, no row → 0, prune clears, the PATCH endpoint
   function with `revealed_tools` and `metadata` leaves the record.
5. **Executor.** `orchestrator/types.py` (`revealed_tools: set[str]`), `orchestrator/executor.py`
   (`_load_revealed_tools`, `_persist_revealed_tools`, `step_init`, `step_llm_call`, `step_tool_execution`),
   `tools/contract.py` (`tool_not_revealed`), `tools/executor.py` (`mode_refusal`).
   Tests: `tests/personal_agent/orchestrator/test_skill_provided_reveal.py` (AC-1 executor cases) and one real
   Postgres case: turn 1 reveals and persists, a new context runs `step_init`, the tool is listed and its call
   runs.
6. **Docs.** `skills/SKILL_TEMPLATE.md` (`provides-tools`), `docs/reference/TOOL_INTEGRATION_GUIDE.md`
   (`skill_provided`).

Test commands: `make test-file FILE=<each file above>`, then the Step-6 gates (`make test`, `make mypy`,
`make ruff-check`, `make ruff-format`, `pre-commit run --all-files`).

## Acceptance criteria → test

| AC | Case | Test |
|---|---|---|
| AC-1 | before any reveal: refused, providers listed, no card, no loop count | `test_skill_provided_reveal.py::test_a_call_before_the_reveal_is_refused` |
| AC-1 | same response as the revealing `read_skill`: refused | `::test_a_call_beside_the_revealing_read_skill_is_refused` |
| AC-1 | next round runs | `::test_a_call_in_the_next_round_runs` |
| AC-1 | second turn, no new `read_skill` | `::test_a_later_turn_lists_and_runs_the_tool` |
| AC-1 | new gateway process reads Postgres | `::test_a_new_context_reads_the_reveal_from_postgres` |
| AC-1 | mode change: absent and refused | `::test_a_mode_that_refuses_the_tool_hides_and_refuses_it` |
| AC-1 | PATCH cannot set or clear; metadata PATCH keeps it | `test_session_revealed_tools.py::test_a_patch_*` |
| AC-1 | two concurrent reveals persist | `::test_concurrent_adds_all_persist` |
| AC-1 | wrong `user_id` | `::test_a_wrong_user_reads_and_changes_nothing` |
| AC-1 | failed load: empty set, log event, turn runs | `test_skill_provided_reveal.py::test_a_failed_load_gives_an_empty_set` |
| AC-1 | failed add keeps the reveal for the turn | `::test_a_failed_add_keeps_the_reveal_for_the_turn` |
| AC-1 | pruning clears | `test_session_revealed_tools.py::test_pruning_clears_the_record` |
| AC-2 | unknown tool / not skill-provided / no provider / hidden skill | `test_skill_provided_rules.py` |

## Codex plan review (2026-10-12)

No critical or high finding. Dispositions:

1. Medium — the reveal check is only in `step_tool_execution`. **Accepted:** `dispatch_tool_call` takes
   `revealed_tools` and refuses an unrevealed skill-provided tool for the primary principal (default: nothing
   revealed, so a caller that forgets the argument fails closed). Test: a direct primary dispatch is refused.
2. Medium — the listing filters on `allowed_modes`, execution on the governance row. **Not in this ticket:** the
   mismatch holds for every tool today. Step 3 uses the governance logic, so a revealed tool never runs in a
   refused mode. Recorded in the handoff.
3. Low — the same-response test must not depend on order. **Accepted:** the test runs both orders.
4. Low — missing assertions. **Accepted:** the mode test checks that the record is unchanged. A stale-name test
   and both failed-add paths (zero rows, exception) are added, each with its log event.
5. Low — the concurrency test must force contention. **Accepted:** a third connection holds the row lock while
   two adds start on their own sessions, then releases it.

## Deploy

Postgres migration 0033 as the `agent` superuser through `AGENT_DATABASE_ADMIN_URL`, then a `seshat-gateway`
rebuild. The migration must run first: the new model column makes every `SELECT` of `sessions` name
`revealed_tools`.
