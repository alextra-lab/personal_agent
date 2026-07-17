# FRE-908 — Prove the within-session compression gate can trigger

**Backing:** ADR-0061 (within-session progressive context compression), ADR-0059 (context-quality incidents).
**Also implicated (discovered during investigation, not named in the ticket):** ADR-0081 (cache-aware frozen append-only layout, FRE-434) — see Finding 0 below.

## Scope guard (from ticket)

Measurement and report only. No threshold change, no flag flip, no live gateway turns. All fixtures are synthetic and offline.

## Investigation already done (this session, read-only)

Traced the full call graph across `orchestrator/executor.py`, `orchestrator/within_session_compression.py`,
`orchestrator/compression_manager.py`, `orchestrator/cache_reset_scheduler.py`, `orchestrator/context_window.py`,
`llm_client/message_content.py`, `llm_client/token_counter.py`, `config/settings.py`, `telemetry/within_session_compression.py`,
and `docker-compose.cloud.yml`. Findings below are code-cited, not inferred, and drive the test list.

### Finding 0 — a mechanism the ticket didn't know about pre-empts everything (the actual answer to "the confound")

`_maybe_frozen_reset` (`executor.py:1264`, called from the context-loading step at `executor.py:3077`, i.e. **before**
the LLM-call loop even starts) is ADR-0081's cache-aware compaction scheduler. It fires whenever
`accumulated_tokens >= cache_frozen_accum_max_ratio(0.50) × context_window_max_tokens(96000) = 48,000`, or earlier at
its cost-optimum `L*` (`cache_reset_scheduler.py:88`). `accumulated_tokens` is `estimate_messages_tokens(ctx.messages)`
(`executor.py:1189`) — the **same estimator** the ADR-0061 gates use.

48,000 < 62,400 (ADR-0061 soft, 0.65) < 81,600 (ADR-0061 hard, 0.85). Under `cache_frozen_layout_enabled=True`
(production default since 2026-06-02, `settings.py:1078`), the frozen-reset scheduler resets accumulated tokens back
down once per turn, *before* either ADR-0061 threshold is reached under normal steady growth.

This is not incidental — it's a **documented, deliberate supersession**. `executor.py:4838-4842`:
```
# ADR-0081 §D3 Decision 3: under the frozen layout the reactive 0.65 soft
# trigger is removed — the cache-aware scheduler (step_init) subsumes it;
# firing reactive compaction here would rewrite history off-schedule and
# break the forward-extension.
if ctx.session_id and not settings.cache_frozen_layout_enabled:
    ... compression_manager.maybe_trigger_compression(...)
```
So the **soft trigger (0.65) is dead by design** under the production default, not dead by bug. ADR-0061's own
`context_window.py:54-83` docstring says the same for `apply_context_window`'s `compressed_summary` parameter
("Dead-by-default when `cache_frozen_layout_enabled=True`").

The **hard trigger (0.85)** has no such guard (`executor.py:3316`, `needs_hard_compression` only checks its own
`within_session_compression_enabled` master switch) — it remains live, and is structurally the *only* surviving
ADR-0061 mechanism in production. It can still fire because `_maybe_frozen_reset` evaluates once per turn at entry,
while `needs_hard_compression` is checked before every LLM dispatch inside that turn's tool-loop — so a single large
tool response arriving mid-turn can jump the working set from comfortably-under-48k straight past 81,600 before the
next turn's frozen-reset would have caught it. This matches ADR-0061 §D1's own stated rationale ("catches in-flight
overflow caused by large tool responses") exactly — it was designed for this one surviving case, whether or not that
was understood at filing time.

**Consequence for the ticket's "consequence" section:** FRE-367 (ADR-0061 Phase 2) is not just "maybe unbuildable" —
it would be reading a signal (the soft-trigger telemetry) that is architecturally dead by a *later, higher-precedence*
ADR. Phase 2 needs to target the hard-trigger path specifically, or target ADR-0081's own scheduler, not the soft path.

### Finding 1 — the estimator itself is tiktoken-based, not the naive heuristic implied by the ticket

`estimate_tokens` (`llm_client/token_counter.py:26`) already uses `tiktoken.cl100k_base` — it replaced two divergent
word-count/char-count heuristics. `estimate_message_tokens` (`context_window.py:15`) sums
`count_content_tokens(content)` (also tiktoken-backed, `message_content.py:93`) plus `tool_calls` payload tokens. So
for what it counts, the estimate is close to ground truth (small discrepancy from cl100k_base being a Claude/GPT
approximation, not this agent's actual Qwen tokenizer).

**The real gap is a blind spot, not an inaccurate heuristic**: assistant messages in the working history carry a
`reasoning_content` field (`executor.py:4100`, `:4133` — Qwen3.6 unsloth template convention), populated from the
model's thinking trace. Neither `estimate_message_tokens` nor `message_content.py` ever reads `reasoning_content` —
only `content` and `tool_calls`. Every thinking token is invisible to both ADR-0061 gates and the frozen-reset
scheduler's `accumulated_tokens`. This corroborates FRE-755's ~45% undercount finding from a different angle (there:
route_traces vs ES; here: the gate's own live view of the working set).

### Finding 2 — telemetry durability gap confirmed, asymmetric with ADR-0059's sibling stream

`telemetry/within_session_compression.py:71-73`, `_default_output_dir()` returns `Path("telemetry/within_session_compression")`
— relative to CWD. In the gateway container (`WORKDIR /app`, `Dockerfile.gateway:56`), that resolves to
`/app/telemetry/within_session_compression`. `docker-compose.cloud.yml`'s `seshat-gateway` volumes list
(lines ~389-408) mounts durable volumes for `captains_log`, `feedback_history`, `graph_quality`, **`context_quality`
(ADR-0059)**, `error_patterns`, `freshness_review`, and `agent_workspace` — but **not** `within_session_compression`.
ADR-0061 §D7 claims the same ADR-0054 §D4 durable-JSONL guarantee its sibling ADR-0059 stream actually has in
production; ADR-0061's own directory is not wired to survive a rebuild. Confirms AC-4 outright; not previously known
to have been checked post-deploy.

## What this plan builds

One new test file (offline, synthetic, no live turns) plus one findings doc. No src/ behavior changes — this ticket
is measurement-only per its own scope guard, and Finding 2's fix (a one-line docker-compose volume) is **proposed in
the report, not applied**, since AC-5 explicitly limits this ticket to measure-and-report.

### Step 1 — new test file `tests/test_orchestrator/test_compression_gate_proof.py`

Follows the existing `tests/test_orchestrator/test_within_session_compression.py` conventions (`_msg` helper style,
`patch.object(wsc.settings, ...)`, `pytest.mark.asyncio`). Four test classes:

**`TestHardGateFiresAtProductionScale` (AC-1)**

Codex review flagged the original design as partly circular: building messages "until
`estimate_messages_tokens(...)` exceeds the threshold" and then asserting `needs_hard_compression` is true proves
nothing, since `needs_hard_compression` *is* `estimate_messages_tokens(messages) >= threshold`
(`within_session_compression.py:325-342`) — same formula on both sides. Revised to model the actual production
shape instead: the scenario ADR-0061 §D1 was designed for (a single large tool response spiking mid-turn, between
two frozen-reset evaluations).

- Build a *pre-turn* working-messages list (system + first user + several small prior turns) whose
  `estimate_messages_tokens(...)` sits comfortably **below** the frozen-reset ceiling:
  `pre_tokens < int(settings.cache_frozen_accum_max_ratio × settings.context_window_max_tokens)` (0.50 × 96000 =
  48,000) — this is the state `_maybe_frozen_reset` would have just passed as "holding" at turn entry.
- Append **one** large non-error tool result (paired with its assistant `tool_calls` message, valid `tool_call_id`)
  sized so the *post*-append total crosses the hard threshold:
  `post_tokens >= int(settings.within_session_hard_threshold_ratio × settings.context_window_max_tokens)` (0.85 ×
  96000 = 81,600), while asserting `post_tokens < settings.context_window_max_tokens - 4500` (the `apply_context_window`
  reserved-tokens default, `context_window.py:57`) so the fixture stays below Stage-7/step_init's own trim floor and
  isn't confounded by truncation removing the very tool result under test before the hard gate ever sees it.
- Guard the fixture's premise explicitly: `assert settings.tool_result_compression_enabled is False` (production
  default, `settings.py:1003-1013`) — if intra-turn digest were ever flipped on, it would shrink the tool body before
  the hard gate sees it and the fixture's sizing would silently stop meaning what it claims.
- Assert `needs_hard_compression(messages, settings.context_window_max_tokens) is True` on the post-append list, and
  `needs_hard_compression(pre_messages, settings.context_window_max_tokens) is False` on the pre-append list — this
  pair is no longer circular: it demonstrates the *transition* a real mid-turn tool response causes, not just that
  the predicate matches its own inputs.
- `await compress_in_place(messages, trace_id=..., session_id=..., trigger="hard", bus=None)`, with
  `personal_agent.telemetry.within_session_compression._default_output_dir` monkeypatched to return `tmp_path` so
  the test doesn't write into the repo tree.
- Assert: `record.trigger == "hard"`, `record.middle_tokens_out < record.middle_tokens_in`,
  `record.tokens_saved > 0`, and re-estimating the returned compressed message list gives a materially smaller token
  count than the pre-compression post-append total. Pass or fail is a real result (AC-1's own wording) — if any of
  these fail, that's the finding, not a test bug.

**`TestEstimatorReconciliation` (AC-2)**
- cl100k parity check (rephrased per codex review — this documents internal consistency with the encoding the
  estimator itself uses, not "ground truth against the real model tokenizer," which cl100k_base only approximates
  for Qwen): build a plain-text fixture, compare `count_content_tokens(text)` against a direct
  `tiktoken.get_encoding("cl100k_base").encode(text)` count — assert equality (documents that for what the estimator
  counts, it exactly reproduces cl100k_base; no heuristic-vs-encoding gap here, contra the ticket's implicit
  assumption that the estimator itself is crude).
- Thinking blind spot, quantified: construct an assistant message
  `{"role": "assistant", "content": <short>, "reasoning_content": <~5,000-token reasoning trace>, "tool_calls": [...]}`.
  Compute `estimate_message_tokens(msg)` (blind to `reasoning_content`) vs. a ground-truth total that includes it
  (tiktoken over `content` + `reasoning_content` + `tool_calls` str). Assert the gap is large and report the ratio
  (e.g. `assert real_total > estimated * 1.5` given the fixture's proportions, with the exact percentage computed and
  asserted, not just eyeballed) — this is the "thinking gap quantified" the AC demands.

**`TestPrecedenceOrdering` (AC-3)**
- Quantitative ordering test, computed from live `settings` (so it re-validates itself if defaults ever drift):
  `assert frozen_reset_ceiling < soft_threshold < hard_threshold` where each is computed inline from
  `settings.cache_frozen_accum_max_ratio`, `settings.context_compression_threshold_ratio`,
  `settings.within_session_hard_threshold_ratio`, all × `settings.context_window_max_tokens`.
- Structural guard test: `assert settings.cache_frozen_layout_enabled is True` (documents the production default that
  makes the soft-trigger call-site guard at `executor.py:4842` false — i.e. proves the precondition for Finding 0's
  "soft trigger is dead" claim holds today). Paired with a comment citing `executor.py:4838-4842` verbatim so a
  future reader can re-verify the guard hasn't moved.
- The qualitative call-order trace (frozen-reset-before-LLM-loop, hard-trigger-inside-LLM-loop,
  soft-trigger-gated-off) is documented in the findings doc with file:line citations — per the AC's own wording
  ("documented from the code path, not inferred"), this is a traced narrative, not a synthetic re-implementation of
  the state machine; the *quantitative* claims embedded in it (the threshold ordering, the settings default) are
  what's test-backed above.

**`TestTelemetryDurability` (AC-4)**
- `assert not within_session_compression._default_output_dir().is_absolute()` and assert its parts are
  `("telemetry", "within_session_compression")` — documents the CWD-relative resolution that becomes `/app/...`
  under the container's `WORKDIR /app`.
- Parse `docker-compose.cloud.yml` with **PyYAML** (`yaml.safe_load`, already a project dependency per
  `pyproject.toml`) rather than text-slicing — codex review flagged the text-slice approach as brittle. `safe_load`
  handles the `${VAR:?err}` interpolation fine since those are just plain scalar strings to the YAML parser (no
  custom tags involved):
  ```python
  doc = yaml.safe_load(Path("docker-compose.cloud.yml").read_text())
  volumes = doc["services"]["seshat-gateway"]["volumes"]
  targets = [v.split(":", 1)[1].split(":", 1)[0] for v in volumes if isinstance(v, str) and ":" in v]
  assert "/app/telemetry/context_quality" in targets       # ADR-0059 sibling stream — durable
  assert "/app/telemetry/within_session_compression" not in targets  # ADR-0061 — not durable (the finding)
  ```
- This is a regression-documenting test: if someone adds the volume later, this test starts failing and should be
  updated to assert presence — that flip *is* the fix landing.

### Step 2 — findings doc `docs/research/2026-07-17-fre-908-compression-gate-proof.md`

Per-AC findings table + the full Finding 0/1/2 narrative above with file:line citations, plus the proposed (not
applied) fix for AC-4 (one line in `docker-compose.cloud.yml`:
`- seshat_within_session_compression_cloud:/app/telemetry/within_session_compression` + the matching top-level
`volumes:` entry) for master/owner to fold in or ticket separately.

### Step 3 — quality gates

```
make test-file FILE=tests/test_orchestrator/test_compression_gate_proof.py
make test                  # full suite — no other file touched, so this is a regression check
make mypy
make ruff-check
make ruff-format
pre-commit run --all-files
```

### Step 4 — self-review

`code-review` skill at `low` effort (test-only diff, no src/ change) + no `security-review` needed (no inputs/
subprocess/auth/network touched — the docker-compose YAML check reads a repo file, not a subprocess).

## Risk-tier self-classification

**Standard** — no `src/` behavior change, but the diff requires understanding multi-file orchestrator control flow
and asserting real internal behavior (not mechanical/config-only). Codex plan-review requested per skill default
("when in doubt, treat as Standard").
