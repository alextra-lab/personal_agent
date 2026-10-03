# FRE-1512 — Record the planner's decision, inputs and delay, and the turn's time to first token

Backing design: ADR-0154 D6 and AC-5. Ticket: FRE-1512. Tier: Standard (touches `src/` logic, schema, the
production write path). Codex plan-review: required. One phase, one PR.

## Findings that shape the plan

1. **Nothing streams tokens to the user.** `service/app.py:460` pushes the whole reply in one
   `TextDeltaEvent` after `orchestrator.handle_user_request` returns. `send_text_delta` has no caller.
   For this plan, "the first token streamed to the user" means **the first user-visible push**: the
   `TextDeltaEvent` push on the WebSocket path (`_process_chat_stream_background`), and the point just
   before `_chat_impl` returns its JSON on `/chat`. This is a fact about today's system. The handoff
   comment must tell master, because ADR-0154 AC-5 reads the field as a streaming delay.
2. **The route-trace row is written before that push.** `observe_topology` writes the row in its
   `finally`, inside `handle_user_request`. So `first_token_ms` is unknown at row-write time.
   Decision: the seam writes the row with `first_token_ms = NULL`. The app then runs one conditional
   `UPDATE` (`set_first_token_ms`) after a successful push. The turn-level ES document is projected
   **once**, by the app, from the updated row. While a first-token clock is active, the seam skips
   the turn-level ES projection. Reason: `schedule_es_index` tasks are unordered, so two full-document
   upserts to one `doc_id` can land in the wrong order (codex finding 1).
3. **"Request receipt" is earlier than `ctx.turn_started_monotonic`.** The ctx is built inside
   `handle_user_request`, after the gateway pipeline. The receipt stamp is `time.monotonic()` taken at
   the top of `chat_stream_endpoint` and `chat`, passed down as `received_monotonic`.
4. **`_resolve_topology` runs at seam enter**, before any planner decision exists. The durable row and
   the ES projection are built at seam exit. So the exit path re-resolves the label from
   `ctx.planner_run.decision` and stamps it on `ctx.topology`. The durable write, the segment rows and
   `TurnCompletedEvent` all use that terminal label. The entry events keep the gateway-intent label
   (codex finding 3). One pure function, `topology_label`, holds the rule.
5. **No planner decision exists yet** (FRE-1515 adds the routing). All `planner_*` fields stay NULL on
   every row until then. This ticket adds the columns, the ctx slots, the assembler reads and the
   derived views. It does not make the planner write them.
6. **`expansion_budget` is not always in `gateway_output`.** Both app paths compute the budget, then
   `gateway_output` becomes `None` if the pipeline fails. So the app passes the budget to
   `handle_user_request(expansion_budget=...)`, which stores `ctx.expansion_budget`. The assembler
   prefers it and falls back to `gateway_output.governance.expansion_budget` (codex finding 5).
7. **`_push_event` swallows failures** (persist error, full queue) and returns `None`. So the app
   cannot tell if a push reached the user. `_persist_and_enqueue` and `_push_event` return `bool`
   (`True` when the event was persisted). `first_token_ms` is recorded only on `True`. Existing callers
   ignore the return value (codex finding 4).

## Design

`orchestrator/types.py`
- New frozen dataclass `PlannerRunRecord`: `decision`, `failure_reason`, `deployment`, `mode`,
  `reasoning_chars`, `duration_ms`, `prompt_tokens`, `completion_tokens`, `input_chars`
  (`PlannerInputChars`: `system`, `history`, `digest`, `message`).
- `ExecutionContext` gains: `expansion_budget: int | None = None`, `planner_run: PlannerRunRecord | None = None`,
  `planner_gate_reason: str | None = None`, `conversation_history_chars: int | None = None`,
  `synthesis_appended: bool = False`.
- `record_planner_outcome(ctx, run)` (module function in `orchestrator/types.py`): sets
  `ctx.planner_run` and clears `ctx.expansion_strategy` when `run.decision` is `declined` or `failed`.

`observability/route_trace/types.py`: `RouteTraceRow` gains 14 fields; `schema_version` default 2 → 3
(precedent: migration 0031).
`planner_input_chars` is a JSONB object `{system, history, digest, message}`.

`observability/topology/seam.py`
- New pure `topology_label(strategy, reason, planner_decision) -> str`. Returns `primary` when
  `reason == "planner_asked"` and `planner_decision` (a string, not the record) is `declined` or
  `failed`. Otherwise it applies `_STRATEGY_TO_TOPOLOGY`.
- `_resolve_topology(ctx)` calls it with `ctx.planner_run.decision` (or `None`).
- In the `finally` of `observe_topology`, before any write: `topology = _resolve_topology(ctx)` and
  `ctx.topology = topology`. `_write_durable_row`, `_write_segment_rows` and `TurnCompletedEvent` use it.
- `_write_durable_row` skips `project_route_trace_to_es` for the turn-level row when a first-token clock
  is active (`observability/first_token.py`, below). Segment rows still project at the seam.

`observability/first_token.py` (new, small): a `ContextVar[float | None]` holding the request-receipt
`time.monotonic()`. API: `start_first_token_clock(received: float | None = None) -> float`,
`first_token_clock_active() -> bool`, `elapsed_ms(received: float) -> float`. The app sets it at endpoint
entry. The seam only reads it.

`observability/route_trace/ledger.py`
- `_INSERT_SQL`, `write`, `_row_from_record`: 14 new columns.
- `_LABEL_LIE_SQL`: AND NOT (`decomposition_reason = 'planner_asked'` AND `planner_decision IN
  ('declined','failed')`).
- New `set_first_token_ms(trace_id, first_token_ms) -> RouteTraceRow | None` (`None` when no turn row
  exists, for example when the seam write failed):
  `UPDATE route_traces SET first_token_ms = $2 WHERE trace_id = $1 AND task_id IS NULL AND
  first_token_ms IS NULL RETURNING *`. First write wins. Returns the updated row.

`observability/topology/es_projection.py` + `docker/elasticsearch/topology-index-template.json`
- Explicit types: keywords for `planner_decision`, `planner_failure_reason`, `planner_deployment`,
  `planner_mode`, `planner_gate_reason`; `integer` for the counts; `float` for `planner_duration_ms` and
  `first_token_ms`; `boolean` for `synthesis_appended`; `object` with four integer sub-fields for
  `planner_input_chars`. Null fields are omitted, as today.
- `scripts/setup-elasticsearch.sh` already applies the template to the live index
  (`put_and_apply_template`). No script change.

`service/app.py`
- Both chat paths call `start_first_token_clock()` at endpoint entry. `chat_stream_endpoint` passes the
  receipt value into the background task, which calls `start_first_token_clock(received)` in its own
  context, because `asyncio.create_task` copies the context at creation. The app also passes
  `expansion_budget` to `handle_user_request`.
- After the push (WS: only if `_push_event` returned `True`) or just before the return (`/chat`), call new
  helper `_record_first_token(trace_id, received)`. The helper calls `ledger.set_first_token_ms`, then
  `project_route_trace_to_es(row, topology=topology_label(row.decomposition_strategy,
  row.decomposition_reason, row.planner_decision))`. If the push failed, the helper skips the update and
  projects the row as is. It is best-effort: any failure is logged and swallowed.
- A turn whose seam write failed has no PG row and no turn-level ES document. This keeps ES a faithful
  projection of the ledger.

`orchestrator/executor.py`
- `conversation_history_chars`: set once, before the expansion block, for the four register types
  (`CONVERSATIONAL`, `TOOL_USE`, `ANALYSIS`, `PLANNING`), from the new helper
  `planner_history_text(messages, query, max_chars)` in `expansion_controller.py`. The planner call now
  uses the same helper, so the two cannot drift. Uses `settings.planner_history_max_chars`.
  Other task types stay NULL.
- `synthesis_appended = True` at the `ctx.messages.append(synthesis_msg)` site.

`orchestrator/expansion_controller.py`: extract the history-selection lines (drop the last message when
it equals the query, then `_render_planner_history`) into `planner_history_text`. No behaviour change.

`observability/route_trace/assembler.py`: read the new ctx slots. `expansion_budget` comes from
`gateway_output.governance.expansion_budget`. Segment rows leave all new fields NULL.

`docker/postgres/migrations/0032_route_trace_planner_decision.sql` and `docker/postgres/init.sql`:
`ADD COLUMN IF NOT EXISTS` for the 14 columns; `schema_version` default 3. Idempotent.

## Steps (TDD: each test first, confirm it fails, then implement)

1. **Migration + parity.** Test `tests/migrations/test_0032_route_trace_planner_decision_migration.py`
   (AC-1): in two ephemeral schemas, build A from the `route_traces` statement in `init.sql`, build B from
   migrations 0009, 0010, 0031, 0032. Apply 0032 twice. Assert no error and equal
   `(column_name, data_type, udt_name, character_maximum_length, numeric_precision, numeric_scale,
   is_nullable, column_default)` sets.
   Run: `make test-infra-up && make test-file FILE=tests/migrations/test_0032_route_trace_planner_decision_migration.py`
2. **Row type, ledger round trip, label-lie.** Extend `tests/observability/route_trace/test_ledger.py`:
   insert/read a row with all new fields; `_LABEL_LIE_SQL` test with seeded `planner_asked` rows
   `declined`, `failed`, `expanded` (AC-4, first half); `set_first_token_ms` writes once.
   Add an integration test on the test Postgres: real `write` then `set_first_token_ms` fills the
   column; `set_first_token_ms` on a missing row returns `None`; a second call does not overwrite.
   Run: `make test-file FILE=tests/observability/route_trace/test_ledger.py`
3. **Topology label + ES doc.** New `tests/observability/topology/test_planner_decision_topology.py`
   (AC-4, second half): `topology_label` returns `primary` for declined and failed, `hybrid_fanout` for
   expanded; `build_topology_doc` carries every new field with the declared Python type; an end-to-end
   seam test that sets `ctx.planner_run` mid-turn and checks the projected `topology`, the durable
   write and the `TurnCompletedEvent` label (all `primary` for declined and failed); a seam test that no
   turn-level ES projection is scheduled while the first-token clock is active.
4. **Assembler + `record_planner_outcome`.** Extend `tests/observability/route_trace/test_assembler.py`:
   fields map from ctx; `expansion_budget` from governance; segment rows NULL. New test for
   `record_planner_outcome` clearing `ctx.expansion_strategy`.
5. **`conversation_history_chars`** (AC-3). Test in
   `tests/personal_agent/orchestrator/test_expansion_controller.py` and the executor test file: on a
   `SINGLE` turn where no planner runs, the stamped value equals `len(_render_planner_history(...))` for the
   same messages and budget; NULL for a non-register type; the planner call still produces identical
   output (existing FRE-1521 tests stay green).
6. **`first_token_ms`** (AC-2). Test in `tests/personal_agent/service/`: a stub orchestrator that sleeps a
   known time, a stub ledger; assert the recorded value is within 50 ms of the delay on both the WS path
   and `/chat`; a ledger failure does not fail the turn; a failed push (`_push_event` returns `False`)
   leaves `first_token_ms` unset; exactly one turn-level ES projection happens per turn.
   Also a test that a gateway-pipeline failure still stores `expansion_budget` on the row.
7. **ES template.** Test that `topology-index-template.json` declares every new field with the type the
   projection emits (AC-5, offline half). Live half is in the runbook.
8. Gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`.
9. Commit. Self-review on `git diff origin/main...HEAD`: `feature-dev:code-reviewer`, `security-review`
   (SQL and ES write paths). Diff class: **escalated** (production write path, schema change).

## Acceptance-criteria map

| AC | Proof |
|---|---|
| AC-1 | step 1 |
| AC-2 | step 6 test; live query after deploy |
| AC-3 | step 5 test; live query after deploy |
| AC-4 | steps 2 and 3 |
| AC-5 | step 7 test; live `_mapping/field` after deploy |

## Post-deploy runbook (for the handoff)

1. Run the migration as `agent` through `AGENT_DATABASE_ADMIN_URL`: `psql "$AGENT_DATABASE_ADMIN_URL" -f
   docker/postgres/migrations/0032_route_trace_planner_decision.sql`, twice.
2. Run `scripts/setup-elasticsearch.sh` to update the template. That script skips date-partitioned
   indices, so also patch the current month: `PUT agent-topology-YYYY-MM/_mapping` with the template's
   `properties` (exact command in the handoff).
3. Rebuild `seshat-gateway`.
4. Verify AC-5: `GET agent-topology-*/_mapping/field/first_token_ms` (and the other new names).
5. Verify AC-2/AC-3 after 20 turns: `route_traces` rows with `first_token_ms IS NULL`, and register-type
   rows with `conversation_history_chars IS NULL`: both must be 0.

## Risks and open points for review

- Post-hoc `UPDATE` of a durable row (finding 2). The alternative is to stamp at reply-final inside the
  orchestrator. That loses the push delay and cannot test a delayed first chunk.
- The app projects the ES label from the row with `topology_label`. A test must show it equals the
  seam's terminal label for every strategy.
- Codex review round 1 (6 blocking) is folded into this revision.
- `/chat` returns JSON. Its "first token" is the response hand-off. Eval and CLI turns use this path.
- `schema_version` 2 → 3: confirm no reader keys on the value.
