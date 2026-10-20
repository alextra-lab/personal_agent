# FRE-1622 — ADR-0157 backend: Seshat effort

**Ticket:** FRE-1622 (Approved). **Design:** ADR-0157 (Accepted). **Umbrella:** FRE-1620.
**Tier:** Complex (src logic, schema, routing, telemetry). Codex plan review required.

## Design summary

- One committed file, `config/seshat_effort.yaml`, holds three presets. A loader in
  `src/personal_agent/config/seshat_effort.py` reads it once into frozen Pydantic models and
  validates it against the global ceilings. `service/app.py` `lifespan` calls the loader at start,
  outside every fail-open `try`, so an invalid file stops the gateway.
- The session stores its level in a new column, `sessions.seshat_effort`.
- At turn launch, the chat path resolves the level and sets a context variable
  `_current_effort: ContextVar[EffortSpec | None]`. This is the same mechanism as
  `config/selection.py`. Each `asyncio.Task` copies the context, so a `PATCH` during a turn does not
  change that turn (ADR-0079 invariant 7). The helpers run in tasks that the turn task creates, so
  they inherit the spec.
- `get_current_effort()` returns the spec of `default_level` when no spec is set. This covers
  callers outside a chat turn (tests, scripts, the probe renderer). The chat paths always set it.

### EffortSpec

```python
EffortLevel = Literal["fast", "standard", "deep"]
PlannerPolicy = Literal["baseline", "p2"]
PrimaryModeName = Literal["fast", "deep"]

class EffortSpec(BaseModel):  # ConfigDict(frozen=True, extra="forbid")
    level: EffortLevel
    expansion: bool
    sub_agent_rounds: int | None       # None only when expansion is False
    planner_policy: PlannerPolicy | None  # None only when expansion is False
    primary_mode: PrimaryModeName | None  # None = the deployment's default_mode

class SeshatEffortConfig(BaseModel):  # frozen, extra="forbid"
    default_level: EffortLevel
    levels: Mapping[EffortLevel, EffortSpec]  # exactly the three keys
```

Start-up refusals (`SeshatEffortConfigError`, a `personal_agent.exceptions` subclass):
1. a level's `sub_agent_rounds` above `settings.sub_agent_max_tool_iterations`;
2. `settings.sub_agent_rounds_by_thoroughness` not empty;
3. an unknown level name, a missing level, an unknown `planner_policy`, or an unknown
   `primary_mode` name (Pydantic `Literal` and `extra="forbid"`);
4. `expansion: true` with a null `sub_agent_rounds` or `planner_policy`, or `sub_agent_rounds < 1`.

`rounds_for_turn(level: Thoroughness) -> int` returns the spec's `sub_agent_rounds`, else
`settings.sub_agent_rounds_for(level)` (Fast has no rounds, and Fast starts no helper).

## Steps

Each step: failing test first, then code. Test command per step:
`make test-file FILE=<test file>`.

### 1. Presets and loader (AC-1)
- New `config/seshat_effort.yaml`: fast `{expansion: false, sub_agent_rounds: null,
  planner_policy: null, primary_mode: fast}`, standard `{true, 12, p2, null}`,
  deep `{true, 20, baseline, deep}`, `default_level: standard`. Standard's `planner_policy` is set
  finally by step 12.
- New `src/personal_agent/config/seshat_effort.py`: the models above, `load_seshat_effort(path=None,
  *, settings=None)`, a cached `get_seshat_effort()`, `effort_spec(level)`,
  `set_current_effort`, `get_current_effort`, `reset_current_effort`, `rounds_for_turn`.
- `service/app.py` `lifespan`: call `get_seshat_effort()` after `check_vision_capabilities()`.
- Tests `tests/personal_agent/config/test_seshat_effort.py`: the committed file loads with the
  expected values. Each refusal raises (rounds 21 with ceiling 20, a by-thoroughness map, level
  `turbo`, `primary_mode: ultra`, `planner_policy: p3`, a missing level, `expansion: true` with null
  rounds). The context variable is isolated per task.

### 2. Decomposition (D4, AC-2 Fast, AC-7)
- `assess_decomposition(..., effort_expansion: bool = True)`. After the `expansion_disabled`
  check: `if not effort_expansion: return SINGLE, reason="effort_fast"`.
- `request_gateway/pipeline.py`: pass `effort_expansion=get_current_effort().expansion`.
- Tests `tests/personal_agent/request_gateway/test_effort_decomposition.py`: Fast gives `SINGLE` +
  `effort_fast` for every `TaskType` and complexity, with the planner gate open. With
  `expansion_enabled=False`, every level gives `expansion_disabled`. `expansion_denied` and
  `zero_budget` keep their reasons under Fast. A pipeline-level test with Fast set asserts the
  gateway output strategy, and an executor-level test asserts that `ExpansionController.execute` is
  not called for a `SINGLE` gateway output (reuse the FRE-1520 `expansion_disabled` test pattern).

### 3. Planner prompt and P2 (D6, AC-2 rounds, AC-5)
- `expansion_controller.py`: add `_P2_DECOMPOSITION_POLICY` (the 913 characters of ADR-0157 D6,
  copied from `scripts/eval/fre1615/arms/P2.patch`). `_build_planner_system_prompt(surface, *,
  admit_single=False, effort: EffortSpec | None = None)`: `effort` defaults to
  `get_current_effort()`. Every level line renders `rounds_for_turn`-style numbers from the spec.
  When `admit_single` and `effort.planner_policy == "p2"`, the `Rules:\n` replacement becomes
  `P2 + "\n" + "Rules:\n" + DECLINE_RULE` (the P2.patch form).
- `_run_planner`: compute `planner_policy = effort.planner_policy if may_decline else None` from
  the same spec that rendered the prompt, and pass `seshat_effort` and `planner_policy` into
  `planner_outcome_fields` (step 9).
- Tests: the P2 block is a constant of 913 characters and 23 lines. With `p2` + `admit_single`,
  the prompt equals the `baseline` prompt with the block and one `\n` inserted directly before the
  first `Rules:\n`. Without `admit_single`, no level has the block. Standard and Deep render
  "(12 tool round(s) each)" and "(20 tool round(s) each)" with the global ceiling at 30.
  Byte-identity against the pre-change renderer: a frozen copy of the pre-change prompt at 12 and
  at 20 rounds (produced on `main` with `sub_agent_max_tool_iterations` 12 and 20, stored as
  test fixtures under `tests/personal_agent/orchestrator/fixtures/`).

### 4. Helper round budget (D3 item 3, AC-2)
- `sub_agent.py:978`, `:1932`, `:2505`: `settings.sub_agent_rounds_for(spec.thoroughness)` →
  `rounds_for_turn(spec.thoroughness)`. `:461` (`_effective_hard_deadline`) stays global.
- Tests: with global ceiling 30 and the Standard spec set, a stub model that requests a tool on
  every call: the loop makes exactly 12 tool rounds, the task message says "budget of 12 tool
  round(s)", and `sub_agent_start.round_budget == 12`. Deep: 20. Reuse the existing round-cap test
  harness of ADR-0149 / FRE-1389.

### 5. Catalog `fast` modes (D5)
- `config/models.yaml`: `fast` on the five `llamacpp_qwen` entries (`enable_thinking: false` plus a
  copy of that entry's own `worker` sampling), `qwen3.8-27b-ovh` (`reasoning_effort: none`),
  `claude_sonnet` (`effort: low`). None on `claude_haiku` and `gpt-5.4-mini`. No `deep` mode.
- Tests (AC-4 wire part): for each `fast` mode, dispatch through `LiteLLMClient` with the
  captured-kwargs harness of `test_worker_mode_dispatch_per_dialect.py` and assert
  `extra_body.chat_template_kwargs == {"enable_thinking": False}` / `reasoning_effort == "none"` /
  `reasoning_effort == "low"` for Sonnet (litellm's `effort` input). A catalog test asserts that a
  `fast` mode with a field outside its dialect is refused at load. A test asserts that each local
  `fast` sampling equals that entry's `worker` sampling at this date.

### 6. Primary mode on both client paths (D5, AC-4)
- `config/seshat_effort.py`: `effective_primary_mode(spec, model_def) -> str`: the level's mode if
  `model_def.modes` declares it, else `model_def.default_mode`. No warning log on a fallback (Deep
  falls back on every model at this date).
- `llm_client/factory.py`: `get_llm_client_for_key(model_key, *, budget_role, mode=None)` applies
  `with_requested_mode` when `mode` is set.
- `executor.py` `step_llm_call` (~6905): when `model_role is ModelRole.PRIMARY`, resolve the
  answering deployment's definition (the role target, or the escalated key's catalog entry), compute
  the effective mode, and pass `mode=` to `get_llm_client` or `get_llm_client_for_key`. Record
  `ctx.primary_mode` from the built client's `model_def.default_mode` on every pass (the last pass
  wins, as `selected_model_role` does).
- Tests: for each of the nine LLM deployments × three levels, both paths: the built client's
  `model_def.default_mode` is the level's mode when declared, else `default`.

### 7. Session storage and the D2 rules (AC-3)
- `docker/postgres/migrations/0034_sessions_seshat_effort.sql` (idempotent, admin URL):
  `ADD COLUMN IF NOT EXISTS seshat_effort VARCHAR(16) NOT NULL DEFAULT 'standard'` (this backfills
  every row), and a named `CHECK (seshat_effort IN ('fast','standard','deep'))` added only if
  absent. The DB default stays, as `revealed_tools` keeps its default in 0033 (Codex review: a
  dropped default breaks every fixture and raw-SQL insert that names no level). The application
  still writes the level explicitly on every creation path (D2.1). Same column and check in
  `init.sql`.
- `service/models.py` `SessionModel.seshat_effort = Column(String(16), nullable=False,
  server_default="standard")`. `SessionCreate.seshat_effort: EffortLevel | None = None`. New
  `SessionEffortUpdate` (`seshat_effort: EffortLevel`, frozen).
- `SessionRepository.create(..., seshat_effort: EffortLevel | None = None)` writes the given level,
  else `default_level`, explicitly.
  `SessionRepository.set_effort(session_id, user_id, level) -> SessionModel | None`
  (user-scoped, `None` on a mismatch).
- Every creation site writes a level: `/chat/stream` background (`app.py:376`), `/chat`
  (`app.py:2262`), `POST /sessions` (`app.py:1880`, `data.seshat_effort or default_level`).
- `service/app.py` `_resolve_session_effort(session_id, supplied, user_id, *, trace_id) ->
  (EffortLevel, provenance)`: existing session → stored (supplied ignored); new session → supplied,
  else `default_level`. `/chat/stream` takes a form field `seshat_effort: EffortLevel | None`; `/chat`
  a query parameter of the same type. Both resolve at launch, set `set_current_effort` before the
  gateway pipeline (the `/chat/stream` background task sets it at its top, beside
  `set_current_selection`), and write the level on session creation.
- `gateway/session_api.py`: `PATCH /sessions/{session_id}/effort` with `sessions:write`, rate limit,
  `_require_request_user_id`, UUID parse, user-scoped write, 404 on mismatch (no write), then
  `emit_session_effort`. Session GET and `GET /{session_id}/config` add `seshat_effort` and
  `effort_provenance` (`server-hydrated`; `default` only when the read fails).
- `transport/agui/transport.py`: `emit_session_effort(*, session_id, seshat_effort)` →
  `StateUpdateEvent(key="session_effort", value={"seshat_effort", "effort_provenance"})`.
- `transport/agui/ws_endpoint.py` `_send_attach_replay`: read the session's level in the same DB
  block and send a `STATE_DELTA` `session_effort` frame (seq None) before `REPLAY_COMPLETE`.
- Tests: migration test on the test Postgres (backfill, idempotent, init.sql parity, null refused,
  `turbo` refused); repository; PATCH (updates + emits, 404 other user and no write, 422 on
  `turbo`); GET and config GET fields; attach replay frame; emit envelope; for each chat path:
  new session stores the sent level, new session without a level stores `standard`, existing
  session ignores a sent level, and a `PATCH` committed after launch does not change the spec that
  the running turn reads.

### 8. route_traces and ES (D8, AC-6)
- `docker/postgres/migrations/0035_route_trace_seshat_effort.sql`: `seshat_effort VARCHAR(16)`,
  `primary_mode VARCHAR(40)`, `schema_version` default 4. Same in `init.sql`.
- `RouteTraceRow` gains both fields, `schema_version = 4`. `ExecutionContext` gains
  `seshat_effort: str | None` (set at context creation from `get_current_effort().level`) and
  `primary_mode: str | None` (step 6). Assembler, `_INSERT_SQL`, `write`, `_row_from_record`,
  `build_topology_doc`, and `docker/elasticsearch/topology-index-template.json` (keyword).
- Tests: extend the 0032 chain test (`_CHAIN` + 0035), ledger bind + read-back, ES doc and the
  template-coverage test, assembler for each level.

### 9. planner_outcome (D8)
- `planner_outcome_fields(..., seshat_effort, planner_policy)`. Test: for each level, the event
  carries `seshat_effort`, and `planner_policy` agrees with the hash (`p2` ⇔ the block is in the
  hashed prompt, null when `admit_single` is false).

### 10. model_call_completed (D8)
- `litellm_client.py`: both emit sites add `"mode": self.model_def.default_mode if self.model_def
  else None` to `extra`. Test both branches.

### 11. Launch events (D8)
- `chat_stream.launched` adds `seshat_effort`. New `chat.launched` in `_chat_impl`, after the
  duplicate check and the session validation, before the gateway pipeline, with `trace_id`,
  `session_id`, `seshat_effort`, `effort_provenance`. Tests capture both.

### 12. The probe and the D7 run (D7 rules 5–6, AC-5, AC-7)
- `scripts/eval/fre1537/render.py`: `render_system_prompt(surface, level=None)`. It keeps its own
  oracle: the production `baseline` prompt without `admit_single` at the level's rounds, then its own
  copy of the decline rule and, for a `p2` level, its own copy of the P2 block, inserted as
  P2.patch does. `main()` takes `--level` (default: the committed `default_level`) and writes
  `level` into `prompts.json`. `gateway render --level`. The fingerprint records `seshat_effort`.
- `tests/personal_agent/orchestrator/test_fre1515_decline_prompt.py`: production
  `admit_single=True` equals the probe rendering for each level and each surface.
- The D7 run (Standard, `p2`, 12 rounds, default local binding, eval stack): after the code
  steps, ask master for a llama.cpp measurement window (every seat holds `make test`,
  `tests/test_eval` and evals, mine included). Preflight first: eval env versus production, a
  1-fixture trace, and a time projection. Steps 1–7 of the probe README with `--mode planner`,
  `--level standard`. Pass → Standard keeps `p2`. Any FAIL → set Standard to `baseline` and run the
  probe on that prompt. Post each `report.txt` with its fingerprint on FRE-1622, and the hash of
  Deep's D2 prompt (`render --level deep`, no model call).
- Pin test `tests/personal_agent/orchestrator/test_seshat_effort_prompt_pin.py`: the SHA-256 of
  Standard's D2 prompt, rendered from the committed yaml with the committed governance surface,
  equals the posted hash.

### 13. Docs
- `config/AGENTS.md` / `docs/reference` mention of the new yaml if a config index lists files.
- Probe `README.md`: the `--level` flag, and the obsolete "production does not carry the decline
  rule" sentence corrected.

## Gates
`make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`,
then the self-review (`feature-dev:code-reviewer`, `security-review`).

**Diff class: escalated** (schema change, a migration, the production turn path). Flag for owner
`/code-review ultra` in the PR body and the handoff.

## Deploy runbook (master)
1. Run `0034` then `0035` as `agent` via `AGENT_DATABASE_ADMIN_URL`, before the rebuild (the ORM
   names the column).
2. Remove `AGENT_SUB_AGENT_MAX_TOOL_ITERATIONS=12` from `.env`, chained with `&&` to the rebuild.
3. Re-apply the topology index template (`scripts/setup-elasticsearch.sh`) for the two keywords.
4. AC-9 checks.
