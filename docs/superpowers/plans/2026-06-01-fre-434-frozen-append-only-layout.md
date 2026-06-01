# FRE-434 — Frozen append-only layout + cache-aware compaction (ADR-0081 §D2/D3)

**Status**: implementation plan (awaiting user approval)
**Date**: 2026-06-01
**Ticket**: FRE-434 (Tier-1:Opus) — implements ADR-0081 §D2/D3
**Spec**: `docs/architecture_decisions/ADR-0081-cache-aware-context-layout-and-compaction.md` §D2/D3 (PR #128, on main)
**Branch**: `fre-434-frozen-append-only-layout` (off `origin/main`)
**Flag**: new `cache_frozen_layout_enabled` (default `False`, no-op when off)

## Decisions (user-confirmed 2026-06-01)
- **PR split approved**: PR1 (D2 layout) → PR2 (D3 scheduler).
- **`/no_think` removed**: primary runs with reasoning *enabled*; sub-agent is an instruct variant of
  the same model — the `/no_think` suffix is unnecessary and was a byte-identity hazard (it rewrites the
  *current* last user message each turn). Remove it by flipping `llm_append_no_think_to_tool_prompts`
  default → `False` (call sites become no-ops; reversible; existing tests that override the setting still
  pass). This eliminates the hardest element of the Step-4 byte-identity chain.

## Goal

Land cross-turn KV reuse on the **local** SLM (`:8502`). Today reuse ≈ 0 because per-turn volatile
(recalled memory + selected skill bodies) is appended into the **system message** (message[0], the
head of the wire sequence) and changes every turn → mid-sequence divergence → full re-prefill.

Two properties, jointly necessary (ADR §D2):
1. **Volatile rides its own user turn** (out of the system head).
2. **Prior turns replay byte-identically** (frozen append-only) — turn N+1 reproduces turn N's full
   sequence verbatim, then appends.

## PR split (matches ship-ticket "one phase = one PR"; ticket stays In Progress until both ship)

ADR frames this as **Part A (D2, layout)** + **Part B (D3, scheduler)**. They are separable:
- **PR 1 — Part A (D2): frozen append-only layout + byte-identity guards.** Delivers the headline
  local-reuse gate for sessions below the compaction threshold (exactly the A/B harness scenario).
  The retained `0.85` hard backstop still prevents overflow.
- **PR 2 — Part B (D3): cache-aware compaction scheduler + `within_session_compression`
  reconciliation.** Makes compaction the single scheduled reset that re-establishes a frozen prefix
  (sawtooth). Matters once sessions are long enough to compact.

Both gated behind the single `cache_frozen_layout_enabled` flag.

---

## PR 1 — Part A (D2): frozen append-only layout

### Step 1 — Config flag (`config/settings.py`, near line 557 / the within_session block)
- Add `cache_frozen_layout_enabled: bool = Field(default=False, ...)` with `AGENT_`-prefixed env.
- **Test** (`tests/personal_agent/config/test_settings.py` or nearest): default is `False`.

### Step 2 — Volatile carrier helper (`executor.py`, new pure function near `_append_no_think...`)
- `_inline_volatile_into_last_user_message(messages, volatile_block) -> list[dict]`: prepend a fenced
  block (`<turn_context>…</turn_context>`) above the existing last-user-message content. Returns a new
  list; idempotent re-derivation must produce identical bytes. Independent joiner; empty `volatile_block`
  → no-op (no separator bytes). (ADR §D2 point 2 — inline, *not* a separate message, so role-fix /
  sanitiser have nothing to merge.)
- **Tests** (pure): empty block = no-op (byte-equal to input); non-empty = exactly one fenced block
  prepended; 0-vs-N symmetry (no whitespace leak); re-run on an already-inlined message is stable.

### Step 3 — Branch the assembly (`executor.py:2267-2285`)
- When `cache_frozen_layout_enabled`:
  - message[0] = `inner_system_before_memory` **only** — do **not** append `_skill_bodies_tail` /
    `memory_section` to `system_prompt`.
  - Build `volatile_block = join_nonempty([_skill_bodies_tail, memory_section])` (+ D3 highlights slot,
    inert in PR1) and inline it into the current user turn via Step 2, **before** the no-think /
    role-fix / sanitise chain (so persistence captures the post-transform form).
- When flag off: exact current behavior (byte-for-byte). **Flag-hygiene test.**

### Step 4 — Persist the post-transform wire form into `session.messages` (the property-1→2 converter)
- After the full transform chain (no-think → role-fix → and accounting for `sanitise_messages`),
  write the inlined-volatile user message back into `ctx.messages` so the next turn's
  `list(session.messages)` replay (`executor.py:1398`) reproduces it byte-identically.
- **Byte-identity invariant** (ADR §D2 invariant box): persisted turn-N bytes == wire bytes sent on
  turn N, after **every** transform:
  - **`/no_think` suffix** (`_append_no_think_to_last_user_message`, 2300): suffix targets the *current*
    last user message; on turn N+1 it must not perturb turn N's frozen bytes. **Decision for build:**
    persist the suffixed form (so it's stable in place) — or move thinking-control out of message bytes.
    Plan: persist suffixed form; assert stability.
  - **`_validate_and_fix_conversation_roles`** (2303): apply before persistence (inline carrier means
    nothing to merge — verify).
  - **`sanitise_messages`** (`client.py:322` / `litellm_client.py:212`): runs after role-fix in *both*
    clients. Either persist the post-sanitiser form, or prove each frozen turn is a sanitiser
    fixed-point (matched tool pairs, non-empty content, no truncation) and assert it.
- **Tests**: 2-turn replay — turn-2 request_messages[0:M] byte-identical to turn-1's persisted prefix;
  a deliberate 1-byte perturbation of a frozen turn breaks the prefix (instrument-live probe).

### Step 5 — Cloud history-end `cache_control` breakpoint (`litellm_client.py:_apply_anthropic_cache_control`)
- Add a third `cache_control: ephemeral` marker on the **last frozen message before the volatile tail**
  (i.e. last message of `ctx.messages` that is not the current volatile user turn), in addition to the
  existing system + last-tool markers. Gated by `cache_frozen_layout_enabled`.
- **Tests**: with flag on + ≥1 history message, exactly three ephemeral markers (system, history-end,
  last-tool) and the history-end marker is on the correct message; flag off → unchanged (two markers).

### Step 6 — Component-id / prompt-identity (no new un-joined emit site)
- `skill_bodies` / `memory_section` component markers already exist (2347-2352). Confirm they still
  attribute to the dynamic side (they now live in the user turn, not message[0]). Add a `turn_context`
  volatile marker if needed for FRE-406 attribution. No new emit site (rides existing PromptIdentity).

### PR-1 verification (FRE-433 A/B harness, `scripts/eval/fre433_cache_ab/`)
- `--profile local`: `timings.cache_n > 0` on the **first full-context call of every turn ≥ 2** (today
  0); turn-≥2 `prompt_n` drops from ~8k to ~the new-tail size. (Local truth = SLM `cache_n`/`prompt_n`,
  NOT ES `cache_read_tokens` — per FRE-433 memory.)
- `--profile cloud`: cross-turn reuse ≥ the 17–20k arm-B baseline; assert history-end marker emitted.
- FRE-407 quality flat-or-up vs head-layout baseline (primary rollout gate).
- Flag off → byte-for-byte current D1/D4 behavior.

---

## PR 2 — Part B (D3): cache-aware compaction scheduler

### Step 7 — Config: scheduler params (`config/settings.py`)
- `cache_reset_min_run_turns_local: int = 12`, `cache_reset_min_run_turns_cloud: int = 4`,
  `cache_frozen_accum_max_ratio: float = 0.50`, `cache_quality_token_weight: float = 4000.0` (`w_q`).
- Keep `within_session_hard_threshold_ratio` (0.85) as **overflow backstop**; remove the `0.65` soft
  every-turn trigger (`context_compression_threshold_ratio` soft path).

### Step 8 — Two-object summary (`within_session_compression.py`)
- `compress_in_place` returns `(frozen_narrative, salient_highlights)`:
  - `frozen_narrative` = **cumulative** (prev narrative + new increment), persisted as an **assistant**
    "context recap" message — `SUMMARY_ROLE "system" → "assistant"` on the persisted path (ADR §D2
    Decision 5: role-fix drops non-leading system messages, `executor.py:663`).
  - `salient_highlights` = hard-bounded subset, rides the current turn's volatile block (regenerated
    each turn, not frozen).
- Constrain `_extract_tail` to start the kept band on a **user** turn (recap→tail alternation, §D2).
- `WithinSessionCompressionRecord.trigger` gains `"scheduled_reset"`.
- **Tests**: cumulative narrative across two resets loses no cold context; recap role = assistant;
  tail starts on user; highlights bounded by token cap.

### Step 9 — Scheduler (`executor.py` + small helper module)
- Closed-form optimal run length `L* = sqrt(2·R_backend / c)`, `c = Δ_turn + w_q·Q_slope`.
- Fire reset when `marginal_hold_cost ≥ R_backend / L`, floored by `cache_reset_min_run_turns_{backend}`,
  capped by token ceiling (`0.50·max_tokens`) and FRE-407 quality ceiling.
- `Δ_turn` measured from persisted history; `Q_slope` fit online from FRE-407 trace (fallback: token
  ceiling alone when sparse). Backend detection (local vs cloud) → `R_backend`.
- **Tests**: pure scheduler — given (Δ_turn, R, w_q, Q_slope), `should_reset` matches the formula at the
  boundary; min-run floor prevents thrash; token ceiling forces reset regardless of optimum.

### Step 10 — Reconcile `within_session_compression` (ADR §D3 Decision 4)
- `compress_in_place` invoked **only** when the scheduler decides to reset (not reactively per turn).
- Remove the transient path: `compression_manager._summaries` / `get_summary` /
  `apply_context_window(compressed_summary=…)` re-insertion (`executor.py:1575-1584`). `apply_context_window`
  keeps only truncation / `_sanitize_tool_pairs`.
- Persist the reset output into `session.messages` as canonical history (Decision 5 structure:
  `[system][first user][frozen_narrative recap][last K verbatim turns][new tail]`).
- **Tests**: scheduler-gated reset only; post-reset structure correct; turn after reset forward-extends
  (prefix through assistant-N byte-identical → reuse resumes — sawtooth rising edge).

### PR-2 verification
- Sawtooth: long reuse run → one reset → reuse resumes (observable in `timings.cache_n`).
- Per-turn token growth matches predicted `Δ_turn`; reset fires at computed optimum (not 0.65/0.85).
- Post-compression forgot-fact error rate flat/improved with D3 highlights.

---

## Cross-cutting

- **Quality gates** (both PRs): `make test` (orchestrator module first, then full), `make mypy`,
  `make ruff-check` + `make ruff-format`, `pre-commit run --all-files`.
- **Identity threading**: no new `log.*` / `bus.publish` / Cypher sites expected; if added, thread
  `session_id` + `trace_id` from `TraceContext`.
- **Standards**: Google docstrings, `str | None`, `settings.<field>`, no `os.getenv`/`print`/bare
  `except`/`Any`.
- **Session boundary**: build pushes branch + opens PR; master merges/deploys/verifies on prod and
  closes the ticket.
- **Halt**: if local reuse stays 0 with the flag on after Step 4, the byte-identity guard (Step 4) is
  the first suspect — instrument the perturbation probe before any other change.
