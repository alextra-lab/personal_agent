# FRE-1482 — Land Before the Cut (ADR-0149 T1)

**Ticket:** FRE-1482 (Urgent, Tier-1, `stream:build1`) · **Design of record:** ADR-0149 (Accepted 2026-09-10)
**Blocker:** FRE-1483 — `Done`. **Blocks:** FRE-1484 (T2), FRE-1485 (T3).
**Branch:** `fre-1482-subagent-budget-awareness`

---

## 1. Scope

Five worker moves, four terminal-path contracts, one dialect capability table, one planner rule.
No limit changes (AC-8).

1. **Move 1** — the round budget is stated in the worker system prompt, rendered from
   `settings.sub_agent_max_tool_iterations`.
2. **Move 2** — `turn_started_at` travels executor → `ExpansionController.execute` → `_run_dispatch`
   → `SubAgentSpec` → the worker's task user message, rendered by `render_current_datetime_block`.
3. **Move 3** — a countdown user message after every round's tool results.
4. **Move 4** — a landing reserve: do not start a round when
   `remaining_s < mean_round_s + effective_timeout`.
5. **Move 5** — forced synthesis at the cap (not cap+1), in the cache-preserving form (D6).
6. **Terminal paths** — `stop_reason` and `report_kind` on `SubAgentResult` and `SubAgentCapture`,
   plus a deterministic ledger.
7. **D6** — `SYNTHESIS_RETAINS_TOOLS` in `llm_client/models.py` and `dialect_for_role` on the client.
8. **D2** — one planner rule naming the worker's round budget.

Out of scope (other tickets): the primary's countdown and forced-synthesis fixes (T3/FRE-1485),
the caller's pause and trailer (T2/FRE-1484), the shared source registry (T4/FRE-1486).

---

## 2. One ADR gap, filled here

### 2.1 `report_kind == "narration"` is declared and never assigned

D3 declares three values. Every row of the terminal-path table assigns `synthesized` or `ledger`,
and D3 states plainly that a failed or empty synthesis is `ledger` and that
`narrative_synthesized` becomes `report_kind == "ledger"`. Those rows are followed exactly.

That leaves one sub-case D3 describes but assigns no `report_kind` to: *"If this call itself is cut,
its streamed partial content is the report, and the ledger follows it."* That content is neither a
completed report nor a bare ledger. It takes `narration`.

| `report_kind` | Condition | Terminal content |
|---|---|---|
| `synthesized` | A model call wrote a non-empty report — a completed reply, or a completed tools-off synthesis call. | That text. |
| `narration` | The synthesis call was cut mid-write and left a non-empty streamed partial. | The partial, then the ledger. |
| `ledger` | Synthesis failed, returned empty, or was impossible. | The ledger. |

`narrative_synthesized == (report_kind == "ledger")`, exactly as D3 states. This is a gap filled,
not a row contradicted, and it is called out in the PR body for master.

### 2.2 Withdrawn — the budget block is rendered for every worker

An earlier draft suppressed move 1 when `spec.tools` was empty, on the ground that a grant-less
worker has no tool loop. Codex plan review established the claim is false: a grant-less worker runs
`_run_tool_loop` too, returning on its first reply because `tool_defs` is `None`. AC-4a also
requires byte-identical text across workers, and a fan-out can mix granted and grant-less workers.
The block is rendered unconditionally, from the setting.

---

## 3. Files

| File | Change |
|---|---|
| `llm_client/models.py` | `SYNTHESIS_RETAINS_TOOLS`, `synthesis_retains_tools()` |
| `llm_client/litellm_client.py` | `dialect_for_role()` |
| `orchestrator/sub_agent_types.py` | `SubAgentSpec.turn_started_at`; `SubAgentResult.stop_reason` / `.report_kind`; the two `Literal` aliases |
| `orchestrator/sub_agent.py` | moves 1–5, the ledger, the terminal paths, `_ToolLoopState` round records |
| `orchestrator/expansion_controller.py` | planner rule; `turn_started_at` through `execute` / `_run_dispatch` / `_maybe_redispatch_on_gap` |
| `orchestrator/executor.py` | one new keyword argument at the `controller.execute` call site |
| `captains_log/capture.py` | `stop_reason`, `report_kind`, `rounds` on `SubAgentCapture` |
| `docker/elasticsearch/captains-subagents-index-template.json` | the three additive fields |

Tests: `tests/personal_agent/orchestrator/test_sub_agent.py`,
`test_expansion_controller.py`, `tests/personal_agent/llm_client/test_models.py` (or the nearest
dialect test module).

---

## 4. Steps

Each step names its verification command.

### Step 1 — D6 capability table (`llm_client/models.py`)

```python
SYNTHESIS_RETAINS_TOOLS: Mapping[Dialect, bool] = {
    Dialect.LLAMACPP_QWEN: True,       # ADR-0149 D6 probe, local backend
    Dialect.OVH_QWEN: True,            # probe 2026-09-11, below
    Dialect.OPENAI_GPT5: True,         # probe 2026-09-11, below
    Dialect.ANTHROPIC_ADAPTIVE: True,  # FRE-484
    Dialect.ANTHROPIC_BUDGET: True,    # FRE-484
}

def synthesis_retains_tools(dialect: Dialect | None) -> bool: ...
```

`None` returns `True` and logs `synthesis_dialect_unresolved` at WARNING.
`models.py` has no logger today; add the module-level `structlog` logger.

**Verify:** `make test-file FILE=tests/personal_agent/llm_client/test_models.py`.

### Step 2 — `dialect_for_role` (`llm_client/litellm_client.py`)

```python
def dialect_for_role(self, role: ModelRole) -> Dialect | None:
    provider_def = load_model_config().providers.get(self.provider)
    return self._resolve_dialect(provider_def)
```

The client is bound to one deployment at construction, so `role` is the caller's declaration of
which role it is dispatching as, not a lookup key — documented as such in the docstring.

**Verify:** a unit test asserting the local client resolves `LLAMACPP_QWEN`.

### Step 3 — types (`orchestrator/sub_agent_types.py`)

```python
SubAgentStopReason = Literal[
    "completed", "cap", "time_reserve", "timeout", "deadline", "cancelled", "error"
]
SubAgentReportKind = Literal["synthesized", "narration", "ledger"]
```

`SubAgentSpec.turn_started_at: datetime | None = None`.
`SubAgentResult.stop_reason: SubAgentStopReason = "completed"`,
`.report_kind: SubAgentReportKind = "synthesized"`. Defaults keep every existing constructor valid;
every path in `sub_agent.py` sets both explicitly.

**Verify:** `make test-file FILE=tests/personal_agent/orchestrator/test_sub_agent_types.py`.

### Step 4 — `_ToolLoopState` gains the round record (`orchestrator/sub_agent.py`)

```python
@dataclass(frozen=True)
class _ToolCallRecord:
    round: int
    tool: str
    arguments: str      # the raw JSON argument string, bounded for the ledger
    args_chars: int
    result_chars: int
    wall_s: float       # the wall-clock of the round this call belongs to
```

`_ToolLoopState` gains `tool_calls: list[_ToolCallRecord]` and `round_wall_s: list[float]`.
`wall_s` is back-filled onto the round's records when the round closes, so a kill mid-round leaves
the completed rounds' figures intact — the same reason `_ToolLoopState` exists at all.

`mean_round_s` = `mean(state.round_wall_s)`, or `effective_timeout` before the first round.

`arguments` is capped at `_LEDGER_ARGS_CAP_CHARS = 1000` per call, with an explicit
`…[truncated {n} chars]` marker so a reader never mistakes a clipped argument for a complete one.
The ADR does not bound it; an unbounded model-authored argument string (a `run_python` body, say)
would otherwise reach the primary's synthesis context through the ledger. 1,000 characters is far
above any `web_search` query, so D3's "the primary can re-run one" holds for the case the ADR
names. Recorded as a fold-in.

### Step 5 — the ledger builder

```python
def _build_ledger(state, stop_reason, why, partial_text) -> str
```

Renders exactly the ADR's block: header line, `Tool calls made:`, `Model notes per round:`,
`Partial text from the interrupted call:`. Arguments and result sizes; never a result body.
Sections with nothing to show are omitted.

### Step 6 — the synthesis call

```python
async def _forced_synthesis(state, spec, llm_client, tool_defs, trace_ctx, ..., reason) -> str
```

- Appends the move-5 user message ("Your tool budget is spent." / "Your time budget is nearly
  spent.").
- Resolves `synthesis_retains_tools(dialect_for_role(spec.model_role))`; `True` → `tools=tool_defs,
  tool_choice="none"`; `False` → `tools=None, tool_choice=None` plus a
  `forced_synthesis_cache_miss_declared` WARNING.
- **`remaining_s` is recomputed at entry**, from `deadline_monotonic - time.monotonic()`. When it is
  `<= 0` the call is not made at all: the outcome is a ledger with `stop_reason = "deadline"`. This
  is the case codex flagged — a synthesis call dispatched with a non-positive timeout.
- `timeout_s = min(effective_timeout, remaining_s)`.
- **Its own `GenerationProgress` sink.** A cut synthesis call leaves its streamed partial there. A
  non-empty partial becomes the report with the ledger appended and `report_kind = "narration"`
  (§2.1); an empty one yields the ledger alone.
- Exactly one attempt. `except Exception` — never a bare `except`, and explicitly **not**
  `BaseException`, so a `CancelledError` raised inside the synthesis call propagates to
  `run_sub_agent`'s cancel handler instead of being turned into a ledger. That is codex finding 3.3.
- On an exception the outcome is a ledger whose `why` names the provider and the dialect. No
  drop-tools retry.

A client without `dialect_for_role` (every existing test double) resolves to `None`, which returns
`True` and warns — the same branch a catalog-less client takes.

### Step 7 — restructure `_run_tool_loop`

Returns a frozen `_ToolLoopOutcome(content, stated_tool_gap, stop_reason, report_kind)`.
`_ToolIterationLimitReached` is deleted; its "no injected wrap-up round" docstring is the statement
ADR-0149 reverses, so the class goes rather than the comment being left to contradict the code.

Per iteration, **in this order** — the ordering is the correctness property, and codex finding 3.1
is that an earlier draft had it wrong:

1. **Cap check first (move 5).** `state.tool_iterations >= N` → forced synthesis, `stop_reason =
   "cap"`. At the **top** of the iteration, before any inference call. With `N = 5` the worker makes
   five tool rounds and then one synthesis call: six inference calls, the same count as today, with
   the sixth writing the report instead of emitting tool calls that are discarded. An earlier draft
   left this check after the inference call, which preserved the discarded generation — the defect
   ADR-0149 D1 §2 exists to remove — and added a seventh call.
2. **Reserve check (move 4), only when a further tool round is still permitted.**
   `remaining_s = deadline_monotonic - time.monotonic()`;
   `remaining_s < mean_round_s + effective_timeout` → forced synthesis,
   `stop_reason = "time_reserve"`.
3. **Inference call**, with `LLMTimeout` caught. On a timeout, `remaining_s` is **recomputed** (the
   value from step 2 is stale by one whole generation budget): `> 0` → one forced-synthesis attempt,
   `stop_reason = "timeout"`; otherwise a ledger with the same stop reason.
4. **No tool calls in the reply** → `stop_reason = "completed"`. Non-empty after `strip()` and after
   the `TOOL_GAP:` line is removed → `report_kind = "synthesized"`; empty → the ledger,
   `report_kind = "ledger"`, `why = "the model returned no text"`. Step 8's `success` rule reads
   `report_kind`, so an empty completed reply is a failure, not a success carrying a ledger.
5. Otherwise execute the round, close its wall-clock record, then append the move-3 countdown after
   the tool results.

### Step 8 — `run_sub_agent`

- Builds the system prompt through `_build_sub_agent_system_prompt(spec)` (move 1).
- Renders the task message with the datetime block (move 2); `None` → no block plus a
  `sub_agent_no_turn_timestamp` WARNING.
- Maps the outcome onto `SubAgentResult`:
  `success = (stop_reason == "completed" and report_kind == "synthesized")`. Reading `report_kind`
  rather than "content is non-empty" is codex finding 1.2: the ledger is always non-empty, so the
  content test alone would report a failed landing as a success.
- `tokens_generated` stays derived from what the model actually generated
  (`state.progress.content`), never from the ledger's deterministic text.
- `asyncio.TimeoutError` → `deadline`; `CancelledError` → `cancelled` (capture then re-raise);
  `LLMTimeout` escaping the loop → `timeout`; any other exception → `error`. Each builds its
  content through `_build_ledger`, replacing `_killed_result`'s bare `progress.content`.

### Step 9 — planner rule (D2) and `turn_started_at` threading

- `_build_planner_system_prompt` gains the round-budget rule, `{N}` read from
  `get_settings().sub_agent_max_tool_iterations`.
- `execute(..., turn_started_at: datetime | None = None)` → `_run_dispatch` → the per-task specs and
  the `_maybe_redispatch_on_gap` replacement spec (`replace()` carries it, so only the signature
  changes there).
- `executor.py` passes `ctx.turn_started_at` at the call site.

### Step 10 — capture and ES template

`SubAgentCapture` gains `stop_reason: str`, `report_kind: str`, `rounds: list[dict[str, Any]]`.
The template gains `stop_reason` (keyword), `report_kind` (keyword) and `rounds` (nested:
`round` integer, `tool` keyword, `args_chars` integer, `result_chars` long, `wall_s` float).
Additive fields only — the reversible deploy class.

---

## 5. Acceptance criteria — where each is proved

The ticket's own six criteria, mapped onto ADR-0149's numbering where the ADR supersedes.

| Criterion | Test | Seeded negative |
|---|---|---|
| FRE-1482 AC-1 / ADR AC-4 — countdown every round | `test_countdown_follows_every_round` — assert a user message with the right remaining count, absorbed chars and seconds after each round's tool results, and none before round 1 | countdown suppressed → no such message |
| ADR AC-4a — budget at round 1 | `test_budget_stated_in_system_prompt` — the move-1 text with `N` from settings, byte-identical across two workers **in one turn and across two turns**, and rendered for a grant-less worker too | move 1 suppressed → text absent |
| FRE-1482 AC-2 / ADR AC-1 — forced synthesis | `test_capped_worker_reports_markers_not_narration` — seeded fixture: `K` coined markers with coined sources; the stub model narrates on tool rounds and reports every marker only on the `tool_choice="none"` call. Assert all markers and sources present, no narration line, `report_kind == "synthesized"`, `stop_reason == "cap"`, and the synthesis request carried the tool definitions with `tool_choice="none"` | forced synthesis disabled → zero markers, every narration line |
| FRE-1482 AC-3 / ADR AC-2 — every terminal path | `test_terminal_paths_declare_a_report` — `LLMTimeout` after two rounds, outer `TimeoutError`, a generic exception, an empty completed reply, and `CancelledError`. Assert the stop reason and that the ledger lists both completed rounds' calls with arguments and result sizes; for cancel, assert the capture carries it and the exception still propagates | ledger disabled → `progress.content` only |
| ADR AC-3 — the landing reserve | `test_worker_lands_before_the_cut` — stub clock, remaining below `mean_round_s + effective_timeout` before round `k`; assert round `k` never runs, the next call is tools-off, `stop_reason == "time_reserve"` | reserve disabled → the round runs and the deadline fires |
| FRE-1482 AC-4 / ADR AC-5 — the date | `test_task_message_carries_the_date` + `test_none_turn_started_at_warns` | move 2 disabled → no block |
| ADR AC-6 — the declared form is the form sent | `test_synthesis_retains_tools_and_pins_none`; `test_declared_false_drops_tools_and_warns`; `test_runtime_rejection_yields_one_attempt_and_a_ledger` | `SYNTHESIS_RETAINS_TOOLS` patched `False` → no `tools` on the request |
| FRE-1482 AC-5 (as amended) | measurement only, reported in the handoff comment. No value changes | — |
| FRE-1482 AC-6 / ADR AC-8 | `git diff origin/main...HEAD` shows no change to `sub_agent_max_tool_iterations`, `sub_agent.default_timeout`, `orchestrator_task_timeout_seconds` | — |

### 5.1 What this branch cannot discharge, named rather than implied

Four checks need a live gateway turn. A live turn is never fired from this seat without the owner's
explicit word, so each is named in the handoff comment as outstanding, with the command that runs
it — not quietly counted as passed.

| Check | What it needs | Why it cannot run here |
|---|---|---|
| ADR AC-1, live half | The owner's 2026-09-10 research query re-run; the capped worker's report names at least one event dated inside the asked week. Today 0 of 5 workers did. | A live gateway turn. |
| ADR AC-3, live half | A local three-worker fan-out in which no capture carries `stop_reason` of `timeout` or `deadline`. | A live gateway turn. |
| ADR AC-5, live half | A date-relative task whose `web_search` arguments name the current year. | A live gateway turn. |
| ADR AC-6, cache ratio on the worker path | `cache_read_tokens` ≥ 90% of prompt tokens on the worker's own forced-synthesis call. | A live local backend call inside a real worker. |

The unit tests prove **the form sent** — tools present, `tool_choice="none"`, and the declared-false
branch dropping them. The **cache consequence** of that form is already measured three times: by the
adr seat, by master's independent re-run, and by this ticket's cloud probe (§6). What remains
unmeasured is the ratio on a live worker call specifically.

**Required observation (ADR, not a criterion).** Per-worker wall-clock, rounds used, characters
absorbed, and the `stop_reason` / `report_kind` distributions, local and cloud, belong in T1's
**close** comment — master's, after deploy. The handoff comment names the queries that produce them.

---

## 6. The D6 cloud probe — run 2026-09-11, owner-authorised

Same five calls as ADR-0149 D6, one fixed ~6,000-token prefix, `max_tokens=4`, two tool
definitions. Run from the VPS through litellm against each provider's live endpoint.

**OVH — `ovhcloud/Qwen3.8-27B` (`ovh_qwen`).** The gateway declares no cache metrics, so wall time
is the only instrument.

| Call | Prompt tokens | Cached | Wall | Tool call emitted |
|---|---|---|---|---|
| tools + `auto`, cold | 6,171 | not reported | 0.98 s | no |
| tools + `auto`, warm | 6,171 | not reported | 0.84 s | no |
| **tools + `none`** | **6,171** | not reported | **0.85 s** | **no** |
| **tools dropped** | **5,821** | not reported | **5.90 s** | no |
| tools + `none`, restored | 6,171 | not reported | 0.73 s | no |

**OpenAI — `openai/gpt-5.4-mini` (`openai_gpt5`).**

| Call | Prompt tokens | Cached | Wall | Tool call emitted |
|---|---|---|---|---|
| tools + `auto`, cold | 5,863 | 0 | 2.78 s | no |
| tools + `auto`, warm | 5,863 | 5,632 | 1.34 s | no |
| **tools + `none`** | **5,863** | **5,504 (94%)** | 2.36 s | **no** |
| **tools dropped** | **5,779** | **0** | 0.77 s | no |
| tools + `none`, restored | 5,863 | 5,504 | 2.09 s | no |

**Reading.** Both dialects accept `tool_choice="none"` and emit no tool call under it. Dropping the
tools array changes the prefix on both (6,171 → 5,821 and 5,863 → 5,779) and loses the cache:
measured directly on OpenAI (5,504 → 0) and by a 7× latency penalty on OVH (0.85 s → 5.90 s), the
same direction and magnitude as ADR-0149's local probe. Both entries are therefore `True`, measured.

One caveat, stated rather than hidden: OVH reports no `cached_tokens`, so its entry rests on
latency alone. OpenAI's wall time is *higher* on the cached calls than on the dropped-tools call,
which is why latency is not the instrument there and `cached_tokens` is.

---

## 7. Quality gates

`make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.
Self-review: `feature-dev:code-reviewer` scoped to `git diff origin/main...HEAD`, plus
`security-review` (the diff touches model output rendered into a downstream prompt).

**Diff class:** production write path — the sub-agent loop runs on every HYBRID/DECOMPOSE turn.
Escalated: flagged for `/code-review ultra` in the PR body and the handoff.
