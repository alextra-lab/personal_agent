# FRE-1472 — thread the digest into the planner, record its judgment, one terminal event per run

**Ticket:** FRE-1472 · **Design intent:** ADR-0154 D1, D5 (carries ADR-0147 D1, D3, D5) · **Built on:** FRE-1471
(digest builder), FRE-1541 (`build_planner_user_message`, `input_too_large`, planner mode).

**Risk tier:** Standard (`src/` logic, memory). Codex plan review required.

## Owner decisions already taken (ticket comment 2026-10-03 15:55)

1. Sequencing: built on top of the FRE-1541 merge (done: 96fb7e2d).
2. The decline row and the "failed with no fallback" row do not exist as code paths until FRE-1515 (D2).
   AC-4 and AC-5 seed those rows at the pure-function level (the relevance matrix and the terminal-event
   field builder). Every path that exists today is forced end to end through `_run_planner`.
3. This ticket does not call `record_planner_outcome`. FRE-1515 writes `ctx.planner_run`.

## Design

### Types (`orchestrator/expansion_types.py`)

- `MemoryRelevance = Literal["used", "none_relevant", "unstated", "not_applicable"]`.
- `ExpansionPlan.memory_relevance: MemoryRelevance = "not_applicable"`. A fallback plan keeps the default.

### Planner prompt (`expansion_controller._build_planner_system_prompt`)

- Schema gains `"memory_relevance": "used|none_relevant"` after `tasks`. The probe anchor
  `"strategy": "HYBRID|DECOMPOSE"` stays intact.
- One new rule, always present, so the system prompt is byte-identical with and without a digest:
  "When the message holds a memory block, the worker cannot see it: write into each task's goal what
  memory already knows that the task depends on. Set memory_relevance to used when at least one memory
  line shaped a task goal, or to none_relevant when nothing in the block bears on the query."

### Placement (`_join_planner_blocks`, `build_planner_user_message`)

- `_DIGEST_HEADER = "What memory already holds, most relevant first:\n"`, added in `_join_planner_blocks`
  in front of a non-empty digest, as `_HISTORY_HEADER` is for the history. The probe
  (`scripts/eval/fre1537/render.py`) calls `_join_planner_blocks`, so it renders the same text.
- `build_planner_user_message`: `fixed_chars` adds `len(_DIGEST_HEADER)` when a digest is present, so the
  64,000-char bound still holds. `digest_chars` stays `len(digest_text)` (header is framing, as for history).

### Validator (`_validate_plan_json`)

- Reads `memory_relevance`. `used` or `none_relevant` is kept. Anything else (missing, wrong type,
  unknown value, `not_applicable`, `unstated`) becomes `unstated`.

### Pure functions (new, `expansion_controller.py`)

- `resolve_memory_relevance(digest_items: int, decision: PlannerDecision, is_fallback: bool,
  stated: MemoryRelevance) -> MemoryRelevance` — ADR-0154 D5 matrix:
  - `digest_items == 0` → `not_applicable`
  - `decision == "failed"` or `is_fallback` → `not_applicable`
  - otherwise (`declined` / `expanded`) → `stated`, with `not_applicable` coerced to `unstated`.
- `planner_outcome_fields(*, decision, failure_reason, is_fallback, stated_relevance, plan_task_count,
  digest, mode, reasoning_chars, input_chars, system_prompt_sha256) -> dict[str, object]` — the full field
  set of the terminal event. It calls `resolve_memory_relevance`, so no emitter can skip the matrix.
  Digest fields (ADR-0147 D5): `memory_digest_eligible_items`, `memory_digest_items`,
  `memory_digest_items_dropped`, `memory_digest_kinds`, `memory_digest_max_line_chars`,
  `memory_digest_tokens`, `memory_digest_item_keys`, `memory_rendered_item_keys` (keys as
  `{"kind", "identity", "ordinal"}` dicts). With no digest, counts are 0 and key lists are empty.

### `_run_planner`

- New kwarg `memory_digest: PlannerMemoryDigest | None = None`. Its `text` goes to `digest_text`.
- Track `decision`/`failure_reason`/`reasoning_chars`/`system_prompt_sha256` through the existing branches,
  and emit **one** `planner_outcome` event at each of the two exits (LLM plan returned; fallback plan
  returned). Mapping on today's paths:
  - valid plan → `expanded`, `is_fallback=False`, `memory_relevance` resolved from the model's value
  - invalid / truncated → `failed`, `invalid`, then fallback (`is_fallback=True`)
  - timeout → `failed`, `timeout`, fallback
  - `PlannerInputTooLargeError` → `failed`, `input_too_large`, fallback
  - other exception → `failed`, `exception`, fallback
- The returned LLM plan carries the resolved `memory_relevance` (`dataclasses.replace`).
- Existing events (`planner_completed`, `planner_failed`, `fallback_planner_used`) are unchanged:
  `scripts/eval/fre1498` and `fre1517` read them. `planner_outcome` is the one terminal event.
- `system_prompt_sha256` = SHA-256 hex of the rendered system prompt; `None` only when the prompt was never
  built (exception before it).

### `ExpansionController.execute` + executor call site

- `execute(..., memory_digest: PlannerMemoryDigest | None = None)` passes it to `_run_planner`.
- `executor.py` at `controller.execute(...)`: `memory_digest=_build_planner_memory_digest(ctx.memory_context
  or [])`. No recall query. `_run_dispatch` is untouched: `spec.context` stays `messages[-4:]`.

## Codex plan review (2026-10-03) — dispositions

1. **High — the event lacked the D6 fields.** Fixed: `planner_outcome` also carries `planner_deployment`,
   `planner_duration_ms`, `planner_prompt_tokens` and `planner_completion_tokens`. AC-5 asserts the values
   per path.
2. **Low — the boundary tests need a digest.** Fixed: exact-fit, one-over and a sweep with a digest in
   `test_planner_input_bound.py`.
3. **Medium — exactly once must be structural.** Both emissions sit outside the `try`, after all success
   bookkeeping, so a failing emitter cannot add a second event. Left as a known limit: if
   `generate_fallback_plan` raises, or the turn is cancelled, no event fires. Neither is a planner outcome,
   and AC-5 does not list them.
4. **High — probe digest runs bypass the history re-trim.** Documented in `scripts/eval/fre1537/README.md`:
   the two can differ only above 64,000 characters in total, which the fixture set does not reach.
5. **Medium — the system prompt hash changes.** Handoff note: FRE-1515 must re-run the D7 probe before the
   routing change ships (ADR-0154 D7 requalification).
6. **Low — reuse `PlannerRunRecord`.** Not taken. FRE-1515 owns the context record. The event builder is
   one pure function.

## Steps (TDD)

1. Tests first in `tests/personal_agent/orchestrator/test_planner_digest_threading.py`:
   - AC-1: `execute(..., memory_digest=<digest with a coined line>)` → the recorded `respond` call's user
     message contains the digest header + line.
   - AC-3: user message order is history < digest < query (index compare); system prompt identical
     between a digest run and a no-digest run.
   - AC-4: `resolve_memory_relevance` parametrized over every matrix row × every stated value,
     including `digest_items>0, decision="failed", is_fallback=False` and `decision="declined"`.
     Validator: missing / bad / `not_applicable` → `unstated`. End to end: empty digest + model says
     `used` → event `not_applicable`; non-empty digest + model `none_relevant` → `none_relevant`.
   - AC-5: force success, invalid JSON, truncated, timeout, exception, `input_too_large` through
     `_run_planner`; each emits exactly one `planner_outcome` with the full field-set keys. Emitter-level
     rows for `declined` and `failed` with `is_fallback=False`.
   - AC-6: `SubAgentSpec.context == messages[-4:]` with a digest passed (regression).
   - Executor wiring: the call site passes a digest built from `ctx.memory_context` (patch
     `ExpansionController.execute`, assert the kwarg).
2. Run → confirm failures. Implement per Design. Run → pass.
3. AC-2 integration test `tests/integration/test_fre1472_digest_changes_plan.py`, `pytestmark = integration`,
   against `get_llm_client(role_name="primary", mode="planner")`: relevant digest with a coined token →
   token in a task goal and `memory_relevance == "used"`; unrelated digest → no coined token in any goal and
   `none_relevant`. **Not run in this session** (CLAUDE.md: integration needs a live LLM).
4. Update `tests/personal_agent/orchestrator/test_planner_input_bound.py` if the header changes a boundary.
5. Docs: ADR-0154 implementation note not needed (ADR holds); note in handoff that the planner system
   prompt hash changes, so FRE-1511's qualification fingerprint must be re-run before FRE-1515 ships (D7
   requalification).
6. Gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`.

## Test commands

```bash
make test-file FILE=tests/personal_agent/orchestrator/test_planner_digest_threading.py
make test-file FILE=tests/personal_agent/orchestrator/test_expansion_controller.py
make test-file FILE=tests/personal_agent/orchestrator/test_planner_input_bound.py
make test
```
