# FRE-1509 — `enforce` retries shape B once and cites only; the refusal and pre-generation forcing are withdrawn

**Ticket:** FRE-1509 (ADR-0151 T2). **ADR:** `docs/architecture_decisions/ADR-0151-a-turn-that-cannot-be-grounded-says-so.md` D3, D4 (withdrawal half), D5. Amends ADR-0138 D4 and D5.
**Tier:** Standard (touches `src/` grounding and orchestrator logic). Codex plan-review required.
**Blocked by:** FRE-1507 (merged, PR #1168): `TurnShape`, `classify_turn_shape`, `TurnVerification.settled_failures`.

## Scope

- D3: `decide()` retries only a shape B turn with one or more settled failures, on attempt 1. The retry cites, it does not retrieve.
- D3: the retry request pins `tool_choice="none"` and keeps the tool list. No retrieval grant.
- D3: `grounding_max_generation_attempts` narrows to `le=2`.
- D4: `TurnDecision.TERMINAL_NO_SOURCE` and `build_no_source_statement` are withdrawn. The turn delivers its last generation with the FRE-1507 declaration.
- D5: the heavy directive and the heavy `"required"` pin never apply. `retrieval_forced` is true exactly when `attempts >= 2`.
- No production behaviour changes. Production runs `observe`. Every change acts only in `enforce`, except the `retrieval_forced` rule, which is inert in `observe`.

## Decisions this ticket owns

### The heavy code (D5): delete the request mechanics, keep the selector

- **Delete** `_append_heavy_directive`, `_resolve_heavy_gate` and their two call sites in `step_llm_call`. Delete `build_forced_retrieval_directive` (`grounding/enforcement_selection.py`). Delete the grant in `_select_enforcement`. Delete `EnforcementSelection.retrieval_forced`.
  - Reason: code that can never apply is a second, false account of what a turn does. The ADR withdraws the forcing in every mode, and nothing reads these after the change.
- **Keep** the selector: `select_enforcement`, `EnforcementState`, probation, the band, `_resolve_enforcement`, the `grounding_enforcement` table and its repository, and the `grounding_enforcement_selected` log line.
  - Reason: deleting it removes a Postgres table's writer and reader. That is a schema-class change outside this ticket. ADR-0151 AC-8 expects "a retained but inert selector still logs it". A later ticket can retire the selector if nothing reuses it.
  - `_select_enforcement` keeps running in `enforce` only. It changes no request. Its docstring states that.

### The rename

`TurnDecision.RETRY_WITH_FORCED_RETRIEVAL = "retry_with_forced_retrieval"` → `TurnDecision.RETRY_CITE_ONLY = "retry_cite_only"`. No ES template, Grafana panel or script reads the old string (checked with `grep -rn retry_with_forced_retrieval` outside `docs/`).

### Orphans removed

Each item below has no reader after the change:
- `GROUNDING_RETRY_TOOL_GRANT` (executor.py).
- `build_no_source_statement`, `MAX_LISTED_SEARCHES` (enforcement.py).
- `_describe_retrieval`, `_MAX_DESCRIBED_RETRIEVAL_CHARS`, its call in `_register_tool_source` (executor.py ~1637), and `ExecutionContext.retrieval_attempts`. The terminal statement was their only reader.

**Kept:** `ExecutionContext.grounding_retrieval_grant` and its read in `_resolve_max_iterations`. After this change nothing adds to it, so it stays 0. It is kept because AC-3 asserts on it, and `route_trace/types.py` documents the ceiling formula that includes it. Removing it is a one-line follow-up if master prefers; it does not change behaviour.

## Design

### 1. `grounding/enforcement.py`

```python
class TurnDecision(StrEnum):
    DELIVER = "deliver"
    RETRY_CITE_ONLY = "retry_cite_only"


def decide(
    verification: TurnVerification,
    *,
    shape: TurnShape | None,
    attempt: int,
    max_attempts: int,
) -> EnforcementDecision:
    deliver = EnforcementDecision(decision=TurnDecision.DELIVER, attempt=attempt, max_attempts=max_attempts)
    if shape is not TurnShape.B or attempt >= max_attempts:
        return deliver
    outcomes = _distinct_outcomes(verification.settled_failures)
    if not outcomes:
        return deliver
    return EnforcementDecision(
        decision=TurnDecision.RETRY_CITE_ONLY,
        attempt=attempt,
        max_attempts=max_attempts,
        blocking_outcomes=outcomes,
    )
```

- `shape` is `None` when verification did not run or the turn has no non-exempt span, so the unavailable and no-span cases fall out of the shape check. A compliant turn has no settled failures, so it delivers.
- `blocking_outcomes` is empty on every `DELIVER`, because nothing blocked. It lists settled outcomes only on a retry.
- `attempt >= max_attempts`: with the cap at 2, a retry happens only on attempt 1. `max_attempts=1` means no retry.

`build_retry_directive(verification)`:
- Names each **settled** failure by identifier and outcome, never by its text (unchanged rule).
- Tells the model to cite each statement with the identifier of a source already in this conversation, and to leave out a statement that no such source supports.
- States that no tool is available on this reply. It contains no instruction to retrieve or search.

The module docstring is rewritten for ADR-0151: one cite-only retry on shape B, no terminal statement.

### 2. `orchestrator/executor.py` — `step_synthesis`

```python
decision = decide(verification, shape=turn_shape, attempt=ctx.grounding_attempts,
                  max_attempts=settings.grounding_max_generation_attempts)
log.info("grounding_enforcement_decision", ...)  # unchanged fields
if decision.decision is TurnDecision.RETRY_CITE_ONLY:
    ctx.grounding_disclosure = None
    ctx.messages.append({"role": "user", "content": build_retry_directive(verification)})
    ctx.grounding_retry_pending = True
    ctx.final_reply = None
    return TaskState.LLM_CALL
```

- No grant. The `TERMINAL_NO_SOURCE` branch is removed. A delivered turn keeps the disclosure that `_unsourced_assertion_disclosure` computed from the last attempt.
- The stale `TERMINAL_NO_SOURCE` mention in the fan-out trailer comment (~7704) is updated. The sync logic stays.

### 3. `orchestrator/executor.py` — `step_llm_call`, the retry request and its response

**Revised after codex plan review** (two blocking findings, both checked in the code). The first draft reused `_forced_synthesis_tool_overrides`. That helper re-reads the tool list and sends an API tool list even for a `PROMPT_INJECTED` model, and `LiteLLMClient` strips `tools` and `tool_choice` for every non-native strategy (`litellm_client.py`, "tools_filtered_by_strategy"). Also, a response with tool calls always went to `TOOL_EXECUTION`, so a backend that ignored the pin could still retrieve and loop.

```python
cite_only_retry = ctx.grounding_retry_pending
if cite_only_retry:
    ctx.grounding_retry_pending = False
    ctx.force_synthesis_from_limit = False
    log.info("grounding_cite_only_retry", trace_id=..., session_id=..., attempt=ctx.grounding_attempts)
```

- **Request.** The normal tool block runs unchanged, so the tool list and any prompt-injected tool text are the same as on the earlier passes (the prompt cache, ADR-0149 D6). After it: `if cite_only_retry and tools: tool_choice = "none"`. The budget-warning `elif` gains `and not cite_only_retry`: a countdown that invites "another round" contradicts a reply that cannot call a tool.
- **Response.** `if cite_only_retry and response_tool_calls:` log `grounding_cite_only_retry_tool_calls_dropped` at WARNING with the count and the strategy, and set `response_tool_calls = []`. The step then goes to `SYNTHESIS` with the reply text. This closes the retry on every strategy, including one that cannot carry the pin.
- The forced-synthesis branch is unchanged.

### 4. `_record_grounding`

`retrieval_forced=ctx.grounding_attempts >= 2`. The FRE-1285 comment block is replaced by one stating ADR-0151 D5: the selection level never sets it.

### 5. Settings

`grounding_max_generation_attempts: int = Field(default=2, ge=1, le=2, ...)`. Description: 1 means no retry, 2 means one cite-only retry (ADR-0151 D3).

### 6. Comments and docs touched

- `orchestrator/types.py`: comments on `grounding_attempts`, `grounding_retry_pending`, `grounding_retrieval_grant`; remove `retrieval_attempts`.
- `grounding/compliance.py`: the "Retrieval was not forced" bullet states that the field reads the attempt count only (ADR-0151 D5).
- `config/settings.py`: the `grounding_verification_mode` description drops "and refuses".
- `docs/reference/CONFIG_INVENTORY.md`: no change (default stays 2).

## Tests (TDD: each written first and run red)

Command for each file: `make test-file FILE=<path>`.

### `tests/personal_agent/grounding/test_enforcement.py` (rewritten)

| Test | Asserts |
|---|---|
| `test_ac1_an_unsettled_only_shape_b_turn_delivers` | shape B, spans all `unverifiable_by_containment`, attempt 1, max 2 → `DELIVER`, `blocking_outcomes == ()` |
| `test_ac2_only_shape_b_retries` (parametrized A, B, C, None) | settled failure, attempt 1, max 2 → B `RETRY_CITE_ONLY`, others `DELIVER` |
| `test_shape_b_retries_at_most_once` | attempt 2 → `DELIVER` |
| `test_a_cap_of_one_never_retries` | max 1 → `DELIVER` |
| `test_blocking_outcomes_list_settled_outcomes_only` | mixed `uncited` + `unverifiable_by_containment` → `(UNCITED,)` |
| `test_a_passing_turn_is_delivered` / `test_an_unavailable_turn_is_delivered` | kept, with `shape` passed |
| `test_ac4_terminal_no_source_is_withdrawn` | `{d.value for d in TurnDecision} == {"deliver", "retry_cite_only"}`; `build_no_source_statement` is not importable |
| `test_retry_directive_cites_only` | names `uncited`; does not contain the claim; contains no "retriev" and no "search" |

### `tests/personal_agent/orchestrator/test_executor_grounding.py`

- `test_enforce_blocks_and_returns_to_llm_call_with_retrieval_forced` → `test_enforce_retries_a_shape_b_turn_once_without_a_grant`: registry with one admitted `fetch_url` source, reply uncited. `LLM_CALL`, `grounding_retry_pending`, `grounding_retrieval_grant == 0`, directive has no "Retrieve".
- `test_enforce_reaches_the_terminal_statement_at_the_bound` → **AC-4** `test_ac4_a_shape_b_turn_failing_both_attempts_delivers_its_second_generation`: attempt 1 runs (retry), then a second generation text is set and synthesis runs again. `COMPLETED`; `await _deliver(ctx)` equals the second generation plus the shape B declaration; the reply does not contain "I could not find a source for".
- **AC-2** `test_ac2_enforce_never_retries_shape_a_or_c`: parametrized over a shape A ctx (`tool_iteration_count=1`, empty registry) and a shape C ctx, one uncited span, `enforce` → `COMPLETED` on attempt 1, the declaration is delivered.
- **AC-1** `test_ac1_enforce_an_unsettled_only_shape_b_turn_delivers_without_note`: `enforce`, shape B registry, verification fixed to unsettled spans → `COMPLETED`, `grounding_enforcement_decision` logs `decision="deliver"` and `blocking_outcomes=[]`.
- **AC-5 (second)** `test_ac5_a_retried_turn_is_forced_and_confounded`: two synthesis attempts on shape B; the second `grounding_verification_completed` event has `attempts=2`, `first_generation_compliant=False`, `compliance_observation="confounded"`, and `ctx.grounding_record.retrieval_forced is True`.
- `test_a_blocked_turn_is_not_sampled_on_the_generation_that_failed` and `test_each_generation_attempt_emits_its_own_document`: give the registry an admitted typed source so the turn is shape B and still retries. The second keeps its refused `bash` origin.
- `_synthesize_with_verification` gains a `mode` keyword (default `"observe"`).

### `tests/personal_agent/orchestrator/test_executor_cite_only_retry.py` (new) — AC-3 and AC-5 (first)

Drives the real `step_llm_call` with the harness from `test_fre1489_volatile_duplication.py`: a mock client with `dialect_for_role` returning `Dialect.LLAMACPP_QWEN`, and a patched registry returning one `web_search` definition.

- **AC-3** `test_ac3_the_retry_pins_none_keeps_the_tools_and_grants_nothing`: first call (attempt 1) captures `tools`. Then the transcript gains a tool call and result, `ctx.grounding_attempts = 1`, `grounding_retry_pending = True`, and the call runs again. The retry's `respond` kwargs have `tool_choice == "none"` and `tools` equal to the first call's list. `ctx.grounding_retrieval_grant` equals its value before the retry. No budget-warning message was appended.
- Seeded negative `test_a_normal_pass_leaves_tool_choice_unpinned`: the same pass without `grounding_retry_pending` has `tool_choice is None`, so the AC-3 assertion can fail.
- **AC-3 (response)** `test_ac3_a_tool_call_on_the_retry_is_dropped_not_executed`: the retry response carries prose and a tool call. The step goes to `SYNTHESIS`, `final_reply` is the prose, the assistant message has no `tool_calls`, and `tool_iteration_count` is unchanged.
- Seeded negative `test_a_tool_call_on_a_normal_pass_is_executed`: the same response outside a retry goes to `TOOL_EXECUTION`.
- **AC-5 (first)** `test_ac5_a_heavy_selection_forces_nothing`: `enforce`, `_resolve_enforcement` patched to return `applied=HEAVY`. After `step_llm_call`: `tool_choice` is not `"required"`, no request message contains the old heavy directive text ("Before you answer: retrieve first"), and `ctx.grounding_retrieval_grant == 0`. Then `_record_grounding` at attempts 1 records `retrieval_forced is False`.

### `tests/personal_agent/orchestrator/test_executor_enforcement_selection.py`

- Delete the heavy-gate tests (`test_heavy_pins_tool_choice_required` … `test_the_gate_never_overrides_forced_synthesis`) and the directive tests (`test_heavy_directive_is_appended_to_the_request_only` … `test_the_directive_follows_the_user_turn_it_must_not_displace`). Their subject is deleted.
- `test_a_heavy_turn_is_recorded_as_pre_forced` → `test_a_heavy_turn_is_not_recorded_as_forced` (asserts False).
- `test_a_d4_retry_is_still_pre_forced_under_light` → asserts True at attempts 2 under any selection.
- `test_heavy_reserves_the_iteration_grant_but_never_touches_history` → `test_heavy_selection_changes_nothing_on_the_turn` (grant 0, messages empty).
- `test_a_store_failure_falls_back_to_heavy`: drop the `retrieval_forced` line.

### `tests/personal_agent/grounding/test_enforcement_selection.py`

- `test_probation_turn_is_an_unconfounded_observation`: assert on `applied` instead of the deleted property.
- `_simulate`: `if selection.applied is EnforcementLevel.HEAVY: continue`. The simulation tests the selector's own dynamics under its original premise, and the selector is retained unchanged.
- Delete `test_forced_retrieval_directive_does_not_hand_back_a_claim`.

### `tests/personal_agent/orchestrator/test_fanout_incomplete_pause.py`

- `test_grounding_retry_leaves_the_carrier_set_for_the_next_pass`: give the registry an admitted source so the turn is shape B and retries.
- `test_grounding_terminal_replacement_syncs_history_to_final_reply` → `test_grounding_delivery_at_the_bound_syncs_history_to_final_reply`: at attempt 2 the claim reply is delivered; `final_reply` ends with the trailer; `messages[-1]["content"] == final_reply`.

### `tests/personal_agent/config/test_grounding_max_generation_attempts.py` (new) — AC-6

- `AppConfig(grounding_max_generation_attempts=3)` raises `ValidationError`.
- Seeded positive: 1 and 2 construct.

## Acceptance criteria → proof

| AC | Proof |
|---|---|
| AC-1 unsettled never retries | `test_ac1_an_unsettled_only_shape_b_turn_delivers` (unit), `test_ac1_enforce_an_unsettled_only_shape_b_turn_delivers_without_note` (turn path) |
| AC-2 only shape B retries | `test_ac2_only_shape_b_retries` (unit), `test_ac2_enforce_never_retries_shape_a_or_c` and `test_enforce_retries_a_shape_b_turn_once_without_a_grant` (turn path) |
| AC-3 the retry cannot retrieve | `test_ac3_the_retry_pins_none_keeps_the_tools_and_grants_nothing`, `test_ac3_a_tool_call_on_the_retry_is_dropped_not_executed`, and one seeded negative for each |
| AC-4 no refusal | `test_ac4_a_shape_b_turn_failing_both_attempts_delivers_its_second_generation`, `test_ac4_terminal_no_source_is_withdrawn` |
| AC-5 `retrieval_forced` tracks attempts | `test_ac5_a_heavy_selection_forces_nothing`, `test_ac5_a_retried_turn_is_forced_and_confounded` |
| AC-6 the cap is validated | `test_grounding_max_generation_attempts.py` |

## Steps

1. Write the enforcement unit tests → run red → change `enforcement.py` → green.
2. Write the AC-6 settings test → red → change `settings.py` → green.
3. Write the turn-path tests (`test_executor_grounding.py`, new cite-only file, fan-out) → red → change `step_synthesis`, `step_llm_call`, `_record_grounding` → green.
4. Delete the heavy mechanics and orphans; update the selector tests → green.
5. Comments and docstrings (`types.py`, `compliance.py`, `settings.py`, `enforcement.py`).
6. Gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`.

## Diff class

Not escalated: no production write path (inert in `observe`), no schema change, no deletion of stored data, no cost or budget code. The retained selector still writes its table only in `enforce`, unchanged.
