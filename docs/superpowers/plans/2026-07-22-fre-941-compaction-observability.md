# FRE-941 — Remove dead flag-off soft-compaction path; hard-code frozen layout

**Ticket:** FRE-941 (retargeted, owner-approved 2026-07-22). Backing ADR: ADR-0081 (cache-aware
context layout + compaction). Related: ADR-0061 (within-session), ADR-0038 (compression).

## Why (settled)

`cache_frozen_layout_enabled` was A/B scaffolding (arms: head layout OFF vs frozen append-only ON).
ADR-0081 records the experiment concluded — "A/B-verified: local KV reuse 0 → ~8,110+; cloud 13,916 →
median 19,542; FRE-407 quality flat" — pinned prod True since 2026-06-02 (FRE-434). The flag-off
soft-compaction code still lives in the tree, which is what made the subsystem look broken. Retire the
flag; make frozen layout unconditional; delete the dead branches.

**Guiding invariant (Steps 1-5, 8):** the deletion is **byte-for-byte unchanged** for the flag-ON path
(the only production path for 7 weeks). Every edit either deletes an unreachable
(`not settings.cache_frozen_layout_enabled`) branch or unwraps an always-taken (`if _frozen_layout:`)
branch. Codex verified this is safe: the frozen-ON path does not mutate `system_prompt` (captures
`inner_system_before_memory` at 3843, replaces `ctx.messages` via `_inline_volatile_into_last_user_
message` at 3873); the deleted `else` only appended to `system_prompt`, which the ON path never used.

**Step 6 is NOT covered by the invariant** — it is a small, explicit **behavior change** on the live
compressor path (`compress_turns` is reached by the hard path + `build_frozen_reset`), with its own
reproducing tests. Kept in this PR (owner-requested fold-in) but delineated as its own change.

### Codex-review revisions (v2)

- **litellm (Step 5) minimized:** change only `litellm_client.py:441` to `frozen_layout=True`. Do NOT
  remove the `frozen_layout` param from `_decorated_anthropic_copy`/`_apply_anthropic_cache_control` or
  touch `tests/test_llm_client/test_anthropic_cache_control.py` — that param is internal, has its own
  cloud-cache-control test surface, and removing it is out of scope (the flag reference is what must go).
- **Atomicity:** remove the `settings` Field together with **all** flag-referencing test updates in one
  pass — no intermediate red. Flag-off test cases (`test_frozen_reset_wiring::test_maybe_frozen_reset_
  noop_when_flag_off`, `test_prompt_layout_order` head-layout case, `test_skill_index_split`,
  `test_skill_injection`, `test_artifact_builder_planning_note_injection`, `test_compression_gate_proof`)
  assert the deleted head layout → remove those cases; keep the frozen-ON assertions.
- **Doc/eval refs (Findings 4/5):** update `docs/reference/CONFIG_INVENTORY.md`, `ADR-0092` (flag
  mentions), `ADR-0061`/`CONTEXT_INTELLIGENCE_SPEC.md` (compression_manager mentions), and the
  `scripts/eval_04b_occupancy_curve.py:612` prompt string. Leave dated audit/research docs as historical.

## Removal surface (grounded)

Flag usages (`grep cache_frozen_layout_enabled src/`): `settings.py:1080` (def) ·
`executor.py:1307` (`_maybe_frozen_reset` guard) · `executor.py:3052-3056` (dead `get_summary`) ·
`executor.py:3845-3896` (`if _frozen_layout:` assembly + dead `else` head-layout) ·
`executor.py:4870-4883` (dead soft-path call) · `litellm_client.py:441` (value passed) ·
`context_window.py:81` (docstring). `compression_manager` callers: only the two dead executor sites
(+ tests). `apply_context_window(compressed_summary=…)`: only executor:3058 passes it.

## Steps (TDD; module tests before full suite)

### Step 1 — executor.py: unwrap the frozen-layout assembly, drop the head-layout else

`executor.py:3845-3896`: replace `_frozen_layout = settings.cache_frozen_layout_enabled` /
`if _frozen_layout:` … `else:` with the **frozen branch body unwrapped** (the `_inline_volatile_into_
last_user_message` path); delete the entire `else` head-layout block (lines 3874-3895). Keep the
surrounding comments trimmed to the now-unconditional behavior.

### Step 2 — executor.py: remove the dead soft-compaction call + legacy summary retrieval

- `3052-3056`: `_summary` is always `None` (flag always True) → delete the `get_summary` conditional;
  drop `compressed_summary=_summary` from the `apply_context_window(...)` call at 3058 (defaults None).
- `4870-4883`: delete the whole `if ctx.session_id and not settings.cache_frozen_layout_enabled:` block
  (the `maybe_trigger_compression` soft trigger — unreachable).
- Remove `from personal_agent.orchestrator import compression_manager` (line 26) once unused.

### Step 3 — executor.py: simplify `_maybe_frozen_reset` guard

`1307`: `if not settings.cache_frozen_layout_enabled or not ctx.session_id: return` →
`if not ctx.session_id: return`. Trim the docstring line 1300 flag reference. (This is the sole
compaction path now; it already logs `cache_reset_decision` every eval.)

### Step 4 — Delete `orchestrator/compression_manager.py`

No src callers remain after Steps 2. Delete the module; delete `tests/test_orchestrator/
test_async_compression.py` (tests only that module). Update the `events/models.py:832` docstring
mention and `orchestrator/AGENTS.md:26` line. Grep-verify zero dangling `compression_manager` refs.

### Step 5 — litellm_client.py + settings.py: retire the flag

- `litellm_client.py:441`: `frozen_layout=_settings.cache_frozen_layout_enabled` → `frozen_layout=True`.
  Then collapse the now-constant param in `_decorated_anthropic_copy` / `_apply_anthropic_cache_control`
  (`frozen_layout: bool = False` default + `if frozen_layout and messages:` at :231): remove the param,
  make the history-end breakpoint unconditional (`if messages:`). Keep existing
  `_apply_anthropic_cache_control` tests green (byte-identical for the True case they already cover).
- `settings.py:1080`: delete the `cache_frozen_layout_enabled` Field. Grep-verify no remaining refs.

### Step 6 — Fold in the compressor hardening (context_compressor.py)

Narrowed per the earlier codex review (findings 1/4/5):
- `classify_compression_failure(exc) -> str`: `ModelRoleError` OR a `ModelConfigError` whose message
  starts with "No configuration found for role" → `role_missing` (NARROW — other `ModelConfigError`
  stays a real failure); `BudgetDenied` → `budget_denied` (structured only — no broad message match);
  `LLMClientError` with ratelimit/quota/429 → `rate_limited`, timeout → `timeout`, else `llm_error`;
  else `assembly_error`.
- In `compress_turns`: add `except ModelConfigError` **before** the generic handler — if
  `_is_role_config_missing(exc)` route to the graceful once-gated role-missing skip (matching the
  existing `except ModelRoleError`), else treat as a real failure. Add a `cause` field to the
  `context_compression_failed` event. Keep existing event names (`context_compressor_role_missing`,
  `context_compression_empty_response`) to avoid dashboard breakage; just add `cause`.

Reproducing test: `respond` raises `ModelConfigError("No configuration found for role: compressor")` →
result is FALLBACK and **no** `context_compression_failed` is emitted (graceful skip). Plus
`classify_*` unit tests per cause.

### Step 7 — ADR-0081 amendment

Add a dated status note: the `cache_frozen_layout_enabled` A/B flag is **retired** (experiment
concluded — reference the A/B result already in the status line); the frozen append-only layout +
cache-aware scheduler are now the **unconditional** layout. No design change.

### Step 8 — Update flag-referencing tests

`tests/` files patching `cache_frozen_layout_enabled` (test_frozen_reset_wiring, test_skill_index_split,
test_prompt_layout_order, test_skill_injection, test_artifact_builder_planning_note_injection,
test_compression_gate_proof): remove the flag-OFF cases/patches; keep the flag-ON assertions as the
now-unconditional expectation. `test_error_fallback.py` / `test_compression_gate_proof.py`: drop any
`compression_manager`/`maybe_trigger_compression` references (the hard path + `build_frozen_reset`
remain).

### Step 9 — Quality gates

`make test` (orchestrator + llm_client modules, then full) · `make mypy` · `make ruff-check` +
`ruff-format` · `pre-commit run --all-files`. code-review (effort **high** — critical orchestrator +
serialization path) + security-review (serialization/cache-control touched). Fix findings on-branch.

## AC mapping

- Flag gone from `src/`, no flag-off branch, gates green → Steps 1-5,9.
- Frozen on-path byte-identical → the deletion invariant + Step 8 assertions (existing frozen-layout
  tests still pass unchanged).
- `compression_manager.py` deleted, no dangling imports → Step 4 grep.
- Compressor `ModelConfigError`→graceful + `cause` dimension, reproducing test → Step 6.
- ADR-0081 updated → Step 7.
