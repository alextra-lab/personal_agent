# FRE-930 (ADR-0122 T5) — move the artifact-builder ask to turn start

**Ticket:** FRE-930 (Approved, Tier-1:Opus, stream:build2, parent FRE-878)
**ADR:** ADR-0122 — Decision §§2, 3b, 3d, 4; Sequencing step 5. **Owns AC-10, AC-11, AC-14.**
**Depends on:** FRE-929 (T4 — `artifact_build_intent` signal) — merged to main (PR #608).
**Explicitly NOT in scope:** `_draft_max_tokens()` sizing (that is T6/FRE-931, AC-12/13); AC-7 seam.

---

## Problem

T1–T3 shipped the artifact-builder DecisionCard **at the build boundary** (inside `artifact_draft`).
The first live AC-7 run raised the card **117 s after the request** — the user had backgrounded the
phone. The card determination depends on nothing the turn computes, so it can be made at turn start.

## Design (from the ADR)

Raise the decision in `step_init`, off the `artifact_build_intent` signal (T4), **after** the
`attachment_cost` gate and **before** the gateway block. Carry the resolution **turn-scoped**. The
build boundary stops raising a card and instead **reads** the turn-scoped resolution, staying
fail-closed; a build reached with **no** resolution degrades to the configured default **and logs**
`artifact_build_intent_missed`.

### The one non-obvious plumbing fact + carrier design (revised per codex plan-review)

`artifact_draft` — like every tool executor — receives `ctx = trace_ctx` (a `TraceContext`), **never**
the `ExecutionContext` (`tools/executor.py:438`). So the turn-scoped resolution set in `step_init`
cannot reach the build boundary via a dataclass field alone. Codex flagged three real gaps in the
"ContextVar-only" v1; the carrier is now a **paired design** (dataclass field = authoritative + async
`ContextVar` = tool-boundary bridge, properly reset), matching `observability/topology/seam.py`:

- **`ExecutionContext.artifact_builder_resolution: ConstraintDecision | None`** — the authoritative
  turn-scoped state, **literally** "on the execution context" as AC-10(a) words it; fresh per turn (new
  ctx), asserted directly by the AC-10(a) test.
- **A `ContextVar[ArtifactBuilderTurnState | None]`** — the bridge across the tool boundary, carrying
  BOTH the `resolution` AND a bounded **`request_shape`** (a ≤120-char preview of the user message) so
  the AC-11 miss log names the phrasing the vocabulary missed (codex BLOCKING #1 — the `TraceContext`
  has no request text, so the miss payload must ride the carrier). It is `.set()` every turn in
  `step_init` and **token-reset in `execute_task`'s `finally`** so no value outlives its turn (codex
  BLOCKING #2 — production lifecycle, not just a test fixture).

`asyncio.gather` child tasks inherit the creating task's context, so the value set in `step_init`
survives to an `artifact_draft` dispatched several tool iterations later (AC-1). This is **proved by a
test that drives `step_init → asyncio.gather → artifact_draft`**, not an immediate post-`step_init`
read (codex BLOCKING #3).

---

## Changes

### 1. `orchestrator/constraint_options.py` — turn-scoped carrier (new)

Add `ArtifactBuilderTurnState` (frozen) + a ContextVar, after the `ConstraintDecision` class:

```python
import contextvars  # top of file

@dataclass(frozen=True)
class ArtifactBuilderTurnState:
    resolution: "ConstraintDecision | None"   # None → the turn-start ask did not run (no signal, §3b)
    request_shape: str                         # ≤120-char user-message preview, for the AC-11 miss log

_artifact_builder_turn_state: contextvars.ContextVar[ArtifactBuilderTurnState | None] = (
    contextvars.ContextVar("artifact_builder_turn_state", default=None)
)

def set_artifact_builder_turn_state(state):   -> Token   # publish for this async context
def get_artifact_builder_turn_state():        -> ArtifactBuilderTurnState | None
def reset_artifact_builder_turn_state(token): -> None    # execute_task finally + test isolation
```

Full Google docstrings; the module comment states the tool-boundary rationale.

### 2. `orchestrator/executor.py` — raise at turn start + lifecycle reset

`ExecutionContext` (types.py): add `artifact_builder_resolution: ConstraintDecision | None = None`
(authoritative, AC-10a).

`execute_task`: token-reset the ContextVar for the turn's lifetime (codex BLOCKING #2) — capture the
token at the top and `reset_artifact_builder_turn_state(token)` in a `finally` around the state loop,
mirroring the `observe_topology` token pattern.

Insert **after** the attachment-cost gate (`:2733`, the `return TaskState.SYNTHESIS` on decline) and
**before** the gateway block (`:2735`):

```python
    await _maybe_resolve_artifact_builder(ctx)
```

New helper (near the other `_maybe_*` step_init helpers) — sets the state **unconditionally** so a miss
still carries `request_shape`:

```python
async def _maybe_resolve_artifact_builder(ctx: ExecutionContext) -> None:
    shape = _request_shape(ctx.user_message)   # ctx.user_message[:120]
    signals = ctx.gateway_output.intent.signals if ctx.gateway_output is not None else []
    if "artifact_build_intent" not in signals:
        # No prediction: record the shape so a build that still reaches artifact_draft
        # logs a tunable miss (§3b/AC-11); resolution stays None.
        set_artifact_builder_turn_state(ArtifactBuilderTurnState(None, shape))
        return
    decision = await _maybe_pause_for_constraint(
        session_id=ctx.session_id, trace_id=ctx.trace_id, user_id=ctx.user_id,
        constraint="artifact_builder", context="Choose the model to build this artifact.",
    )
    ctx.artifact_builder_resolution = decision
    set_artifact_builder_turn_state(ArtifactBuilderTurnState(decision, shape))
    log.info("artifact_builder_resolved_at_turn_start", trace_id=..., action_id=str(decision),
             resolution=decision.resolution)
```

`_maybe_pause_for_constraint` already: consults the stored preference (deployment key → silent,
`always_pause`/none → card), computes catalog options, and returns a safe default on
timeout/no-socket (ADR §4 table). **Ordering + isolation come for free:** the attachment gate blocks
and returns before this runs (§3d, AC-14 a/d); this is a distinct `request_id`-keyed waiter resolvable
only by an explicit deployment-key option id (`ws_endpoint.py:354` substitutes any non-option answer —
e.g. a bare "yes" — with the default), so no shared-affirmative contamination (§3d/FRE-749, AC-14 b/c).

### 3. `tools/artifact_tools.py` — read, don't raise

Replace the build-boundary `_maybe_pause_for_constraint` call (`:1458-1504`) with a **read** of the
turn-scoped resolution (`state = get_artifact_builder_turn_state()`; `resolution = state.resolution if
state else None`):

- `resolution` is `None` → **missed prediction (AC-11)**: `log.info("artifact_build_intent_missed", …,
  slug, title, task_id, request_shape=state.request_shape if state else "")` + `get_llm_client(
  role_name="artifact_builder")` (configured default). Build completes; never errors. The
  `request_shape` names the phrasing the regex missed (codex BLOCKING #1).
- resolution in `{"user_choice", "preference_applied"}` → **by-key path** (unchanged): fail-closed
  `resolve_artifact_builder_key` + `get_llm_client_for_key(key, budget_role="artifact_builder")`
  (AC-1/AC-2/AC-4).
- else (`timeout_default` / `connection_lost` / `user_cancel`) → role-name default path (FRE-879, AC-2).

Remove the now-orphaned `_maybe_pause_for_constraint` import and the `user_id` local (used only by the
removed call — verify no other use).

---

## Tests (TDD — write failing first)

### Executor level — `tests/personal_agent/orchestrator/test_artifact_builder_turn_start.py` (new)

- **AC-10(a)** `test_step_init_populates_builder_resolution_when_signal_present`: gateway_output with
  `signals=["tool_intent_pattern","artifact_build_intent"]`, mock `_maybe_pause_for_constraint` →
  decision; after `step_init`, `ctx.artifact_builder_resolution` == decision. **Fails on old code**
  (never set in step_init).
- **AC-10(a) neg** `test_step_init_no_resolution_without_signal`: signals without the intent →
  `ctx.artifact_builder_resolution` is `None`; the pause helper is **not** called (state carries
  `resolution=None` + `request_shape`).
- **AC-1/AC-10 survival (codex BLOCKING #3)** `test_resolution_survives_step_init_to_gather_dispatch`:
  run `step_init` (signal present, mocked pause → `user_choice(claude_sonnet)`), then invoke
  `artifact_draft_executor` **inside `asyncio.gather`** (mimicking real dispatch) and assert it takes
  the by-key path with `claude_sonnet` — proving the carrier survives the task boundary and intervening
  work.
- **AC-14(a)/(d)** `test_attachment_gate_precedes_and_gates_builder`: with a call-order spy, mock
  `_maybe_confirm_attachment_cost`. When it returns `False`, `step_init` returns `SYNTHESIS` and the
  builder pause is **never** called (declining short-circuits). When it returns `True`, attachment is
  invoked **before** the builder pause.
- **AC-14(b)** covered by the True-leg above (attachment resolves, builder still raised, separate
  waiter). **AC-14(c)** (bare-"yes" → default) is `ws_endpoint.py:354`'s validation — already covered
  by `test_constraint_governance.py`; cite it, add one assertion that the builder waiter is raised with
  `constraint="artifact_builder"` (its own waiter).

### Build boundary — rewrite `tests/personal_agent/tools/test_artifact_tools.py`

- Add an **autouse fixture** that captures a token and `reset_artifact_builder_turn_state(token)` in
  teardown (token-based, not bare `set(None)` — codex Q4: protects against cross-test leakage).
- `_mock_no_builder_decision` → leave the ContextVar `None` (the legacy tests' true state: no step_init
  ran) — they keep the role-name path **via the missed branch**. Update its docstring to state
  explicitly that these direct/background calls now emit `artifact_build_intent_missed` (codex
  non-blocking #4); legacy assertions are unaffected (they filter events by name).
- `_install_builder_decision` → `set_artifact_builder_turn_state(ArtifactBuilderTurnState(
  ConstraintDecision(...), "req"))` instead of mocking the pause helper.
- **Remove** `test_artifact_draft_pause_raised_with_artifact_builder_constraint` (boundary no longer
  raises) → replace with `test_artifact_draft_does_not_call_pause_helper` (spy asserts 0 calls).
- Keep (drive via ContextVar): user_choice/preference by-key, no-decision role-name, invalid-key
  fallback+substitution-log, no-substitution-when-match.
- **AC-11** `test_missing_resolution_emits_intent_missed`: state with `resolution=None,
  request_shape="build me a widget"` → `artifact_build_intent_missed` logged **carrying request_shape**
  + role-name default client + build completes.
- **AC-10(c)** `test_two_builds_one_selection`: set ContextVar once (user_choice) → two
  `artifact_draft` calls both take the by-key path with the same key (one selection, both builds).
  `test_resolution_does_not_leak` (codex non-blocking #3): a false-positive turn's state, then
  `reset` → a subsequent build reads `None` → missed prediction (proves no cross-turn leak).

---

## Quality gates
`make test-file FILE=tests/personal_agent/orchestrator/test_artifact_builder_turn_start.py` ·
`make test-file FILE=tests/personal_agent/tools/test_artifact_tools.py` · full `make test` ·
`make mypy` · `make ruff-check` + `make ruff-format` · `pre-commit run --all-files` ·
`code-review` (high — executor/state-machine) on the diff.

## Post-implementation review (code-review, high effort)

Ran the workflow-backed code-review on the branch. 7 confirmed findings; resolution:
- **Fixed** — dropped `request_shape` from the miss log + carrier (PII: the full request is already
  on `task_started` under the same `trace_id`, so re-logging it was both a redundant PII surface and
  unnecessary — the miss's `trace_id` joins to the phrasing). This also collapsed the carrier to a bare
  `ContextVar[ConstraintDecision | None]` (no `ArtifactBuilderTurnState` dataclass) and removed the
  field↔carrier divergence. Removed the no-op `_mock_no_builder_decision` test helper (autouse fixture
  covers it).
- **ADR-sanctioned (documented, not changed)** — a stored preference not honoured on a classifier
  *miss* (§3b/AC-11 = configured default; loading it on every no-signal turn would add a hot-path DB
  read); the false-positive card at turn start (§3b accepts it as "mildly annoying and self-limiting");
  one selection per turn rather than per-artifact (§3d + AC-10c mandate it).
- **Kept** — the `ExecutionContext.artifact_builder_resolution` field: AC-10(a) literally requires the
  resolution "on the execution context, asserted directly"; write-only-in-prod is inherent to the
  tool-boundary architecture (the tool can't read `ctx`), and it now mirrors the carrier exactly.

## Out of scope / follow-ups
- `_draft_max_tokens()` sizing → T6/FRE-931 (AC-12/13). Do not touch.
- AC-7 live seam → owned by the seam ticket; master asserts on the deployed stack.
- FRE-928 (waiter no-socket timeout bypass) → independent, ships on its own merits (§6).
