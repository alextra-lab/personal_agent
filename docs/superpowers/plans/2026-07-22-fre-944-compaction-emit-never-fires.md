# FRE-944 — The per-turn compaction emit has never fired

**Ticket:** [FRE-944](https://linear.app/frenchforest/issue/FRE-944) · **Backing ADR:** ADR-0081 §D3 (cache-aware
compaction scheduler), ADR-0092 §D7/§D8 (cadence monitor + D marker)
**Scope:** visibility only. No change to compaction thresholds or behaviour.

---

## 1. Root cause — named with evidence, not inferred (AC-4)

`step_init`'s gateway-driven branch ends in an **unconditional `return`**, so the entire
post-branch region of `step_init` is dead code on every production turn.

`src/personal_agent/orchestrator/executor.py`:

| Line | Statement | Reached in prod? |
|------|-----------|------------------|
| 2859 | `if ctx.gateway_output is not None:` | yes — 157/157 traces |
| 3031 | `return TaskState.LLM_CALL` (last statement of that block) | yes |
| 3045 | `apply_context_window(...)` | **no** |
| 3058 | `await _maybe_frozen_reset(ctx)` | **no** |
| 3070 | `log.info("conversation_context_loaded", ...)` | **no** |

**Evidence 1 — structural (AST, not eyeballing).** Parsing `step_init` confirms the
`ctx.gateway_output is not None` block spans lines 2859–3031 and its *last* statement is a
`Return`. The `_maybe_frozen_reset` call sits at line 3058, outside and after that block. It is
the only call site in the file.

**Evidence 2 — production (Elasticsearch, `agent-logs-*`, 30 d, live cloud-sim cluster).**

- 157 distinct `trace_id`s reached `function=step_init`.
- **157 of 157** emitted `step_init_gateway_path` (line 2885) — i.e. 100 % of turns take the
  gateway branch. There is no meaningful legacy-path traffic.
- **Zero** log records with `function=step_init` and `line_number > 3031`. Not one, in 30 days.
- Deployed container source was checked directly (`docker exec cloud-sim-seshat-gateway`) and the
  line numbers match this worktree, so the ES line numbers are directly comparable.

**Evidence 3 — the two rejected hypotheses are dead.**

- *Swallowed exception:* ruled out. There is no `except` between the `ctx.session_id` guard and the
  emit, and an exception at 3058 would propagate out of the bare `try:`/`finally:` at 3038 and fail
  the turn. Turns succeed. Furthermore the sibling emit at 3070 — a different function's worth of
  code away, with no shared failure mode — is *also* at exactly zero, which a scheduler exception
  cannot explain but an early `return` explains completely.
- *Logging-pipeline drop:* ruled out. `ElasticsearchHandler.emit` filters only by logger *name*
  (`elastic_transport`, `elasticsearch`, `neo4j`, `httpx`, `httpcore`) — there is no event-name
  allowlist. Neighbouring emits from the same logger and module (`step_init_gateway_path`,
  `memory_enrichment_completed`) ship fine. The index is at 266/300 fields with
  `ignore_dynamic_beyond_limit: true`, so unmapped fields are *ignored*, never doc-rejecting —
  confirmed by `memory_facts_count`, which is absent from the mapping yet whose event indexes 49×.

**Collateral finding (surfaced, NOT fixed here).** `apply_context_window` (line 3045) is skipped on
every production turn for the same reason. This is *not* an unbounded-context bug: gateway Stage 7
(`request_gateway/budget.py::apply_budget`, called from `pipeline.py:165`) performs its own
token-aware history trimming before the executor runs. Worth its own ticket; out of scope here.

**AC-5 answer.** `frozen_reset_fired` is zero for a *stronger* reason than "no reset condition was
met": on the production path the reset action is **structurally unreachable**, because the whole
call site is bypassed. It is not merely un-fired; it is uninvoked. After this change it stays
uninvoked by design (see §2 — visibility only), and the emit's headroom fields are what will tell us
when a reset would have been warranted.

---

## 2. The change — visibility only, no compaction/message-mutation change

Split the evaluate-and-log half of `_maybe_frozen_reset` from its act half, and call the
evaluate-and-log half on the gateway path.

**2a.** New `_emit_cache_reset_decision(ctx) -> tuple[ResetDecision, dict[str, Any], str]` in
`executor.py`, holding today's lines 1317–1341 verbatim plus the two new fields:

```python
accumulated_tokens=inputs["accumulated_tokens"],
accum_max_tokens=inputs["accum_max_tokens"],
```

Both are already computed in `_derive_reset_inputs` — nothing new is derived (AC-2). Together they
make headroom readable off one event with no join.

**2b.** `_maybe_frozen_reset` calls `_emit_cache_reset_decision`, then acts exactly as today.
The legacy path is byte-for-byte unchanged in behaviour.

**2c.** At the **top** of the gateway branch, call `_emit_cache_reset_decision(ctx)` and discard the
result. The scheduler is evaluated and logged; `build_frozen_reset` is **not** invoked. No message
list is mutated. No threshold moves.

> **Corrected during self-review.** The first implementation put this call just before the branch's
> final `return`. That leaves the **enforced-expansion sub-path silent**: it returns from the middle
> of the branch, so those turns would have reproduced this very bug on a subset of production
> traffic. Hoisting the call to the top of the branch covers every gateway sub-path from one call
> site. A regression test pins it, and was verified to fail against the original placement (0 emits).

**Measurement-point caveat (also from self-review).** The two call sites do not measure the same
thing: the gateway one reads *untrimmed* history, the legacy one runs after `apply_context_window`
truncates. Untrimmed is correct here — the §D3 accumulation ceiling (0.50) exists to schedule a reset
*before* the 0.85 hard-truncation backstop engages, so a post-truncation reading would mask the
pressure it watches for. Recorded in code and in ADR-0092 item 7 so no dashboard pools the two.

The two call sites are mutually exclusive (a turn is either gateway-driven or legacy), so the emit
fires **exactly once** per turn either way.

**What "no behaviour change" does and does not claim** (tightened after codex plan-review). It
claims: no compaction fires, no message list is mutated, no threshold moves, `build_frozen_reset` is
never invoked on the gateway path. It does *not* claim the evaluation is free of side effects — on
the gateway path we newly incur, once per turn: one `cache_reset_decision` log record;
`_frozen_backend()`'s config/catalog resolution (`load_model_config` → possible one-time
`model_config_loaded` emit on a cold catalog); and token estimation, which can initialize the
module-level tiktoken encoding cache. All are cheap, idempotent-after-first-use, and already paid on
other turn paths — but they are named here rather than waved away.

### Why evaluate-only rather than moving the whole call up

Moving `_maybe_frozen_reset` above the return would also switch compaction *on* for 100 % of
production turns — a behaviour change the ticket explicitly forbids ("Do not fix compaction
behaviour in this ticket"; "Out of scope: … any change to … behaviour"). Evaluate-only delivers
precisely what the ticket asks — the calculated decision plus the distance to the ceiling, every
turn — and hands the follow-up compaction review live ground truth to decide the behavioural
question on. **This is the one design call in the plan and it is flagged for owner confirmation.**

---

## 3. Tests (TDD — written first, must fail against current code)

New file `tests/test_orchestrator/test_frozen_reset_emit.py`, built on the existing gateway harness
in `tests/personal_agent/orchestrator/test_gateway_integration.py`:

**Capture mechanism.** Patch the module logger (`ex.log`), *not* `structlog.testing.capture_logs()`.
`telemetry/logger.py:252` configures `cache_logger_on_first_use=True` and `executor.py:64`
materializes `log` at import time, so `capture_logs()` is unreliable under the shared suite — the
repo already documents this and established the patch-the-module-logger pattern in FRE-552
(`tests/test_tools/test_executor.py:130`). Reusing it, per codex plan-review.

1. `test_gateway_path_emits_cache_reset_decision_exactly_once` — drive a real `step_init` with a
   `SINGLE`-strategy `GatewayOutput`; assert exactly one `cache_reset_decision` event, with
   `should_reset` a bool and `reason` a non-empty populated string (AC-1).
   **Fails on current code: zero events captured.**
2. `test_emit_carries_headroom_fields` — same turn; assert `accumulated_tokens` and
   `accum_max_tokens` are both present and numeric (AC-2). **Fails on current code.**
3. `test_gateway_path_never_compacts_even_when_reset_worthy` — force `_derive_reset_inputs` to a
   decision with `should_reset=True`, drive `step_init` on the gateway path, and assert
   `build_frozen_reset` was **not** called and `ctx.messages` / `ctx.salient_highlights` are
   untouched. This is the guard that the evaluate-only boundary actually holds; without it nothing
   stops a later edit from sliding the act half onto the gateway path. (Added per codex
   plan-review — it was the plan's most substantive gap.)
4. `test_enforced_expansion_subpath_also_emits_exactly_once` — the mid-branch early-return path
   emits too. Added after self-review; verified to fail (0 emits) against the original call
   placement.
5. `test_legacy_helper_still_emits_exactly_once` — `_maybe_frozen_reset` directly; assert one emit
   and no double-emit regression.
6. Existing `test_frozen_reset_wiring.py` must stay green (act half unchanged).

All 5 new tests were verified red against unmodified `origin/main` source, with the *final* test file
(not an earlier draft), so the red-before-green proof is not stale.

Commands: `make test-file FILE=tests/test_orchestrator/test_frozen_reset_emit.py`, then
`make test-file FILE=tests/test_orchestrator/test_frozen_reset_wiring.py`, then `make test`.

---

## 4. Acceptance criteria

| # | Criterion | How proven |
|---|-----------|-----------|
| 1 | Test drives a turn through step-init, asserts emit fires exactly once with populated decision + reason | Test 1; verified red-before-green |
| 2 | Emit carries accumulated tokens + the ceiling, numeric | Test 2 |
| 3 | Emit fires in production | Post-deploy: non-zero `cache_reset_decision` count correlated to turns in the window, plus a real headroom value read off a live turn |
| 4 | Root cause named with evidence | §1 — AST + 30 d ES, both rival hypotheses positively excluded |
| 5 | `frozen_reset_fired` reachable, or documented zero | §1 — documented: structurally *uninvoked*, stronger than un-fired |

## 5. Deploy

Gateway rebuild — **ask-first** class. Post-deploy runbook in the Linear close-out.
