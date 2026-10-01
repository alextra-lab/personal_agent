# FRE-1529 — inline the volatile block once per turn, and bound the skill bodies

**Ticket:** [FRE-1529](https://linear.app/frenchforest/issue/FRE-1529) · **Backing ADR:** ADR-0081 D2 (frozen
append-only layout) and D4 (skill block volatility split) · **Related:** FRE-1524, FRE-1527, FRE-1138.

## The defect, as the code shows it

1. `step_llm_call` runs once per tool round. Each round, it calls `_inline_volatile_with_outcome`, which
   targets the **last user message**. Four sites append a user-role message mid-turn: the tool-budget
   warning, the forced-synthesis prompt, the cite-only grounding retry, and the worker-synthesis prompt.
   Each one becomes the new last user message, so the next round adds another `<turn_context>` fence.
2. The keyword match for skill bodies reads the text of the last user message. After round 1, that is the
   fenced query (memory, bodies, highlights) or an injected directive, not the user's question. So the
   match admits about 7x more bodies (15,215 → 113,615 chars on trace 1cd3b49e).
3. Nothing bounds the bodies. `skill_index_max_tokens` bounds only the index.

## Design

**A. One fence per turn.** Add `ExecutionContext.turn_context_inlined: bool = False`. In `step_llm_call`,
call the inliner only while the flag is False. Set it True on `INLINED` or `ALREADY_WRAPPED`. On later
rounds, report `ALREADY_WRAPPED` and leave `ctx.messages` untouched. The block owner is the last user
message on the turn's first primary call: the user's own query, or, on an expansion turn, the
worker-synthesis prompt that `step_init` appends before that call. The inliner's own wrapped-check stays.

Why a flag and not an index: `apply_context_window` and `build_frozen_reset` can rebuild `ctx.messages`
before the first call, so a stored index is fragile. A per-request flag cannot drift. `ExecutionContext`
is built once per request (`orchestrator.py:114`).

**B. Match keywords against the turn's query.** Use `ctx.user_message` as the routing text for the
keyword match and the Phase C router, not the last user message.

**C. Bound the bodies.** New setting `skill_bodies_max_tokens` (default 8,192, env
`AGENT_SKILL_BODIES_MAX_TOKENS`), with the same 4 chars/token estimate as the index. New public
`fit_skill_bodies(docs, *, cap_tokens, separator, fixed_chars, trace_id)` in `skills.py` keeps bodies in
priority order while the joined block fits, skips the rest, and logs `skill_bodies_truncated` (info) with
`dropped_count`, `dropped_skills`, `kept_skills`, `cap_tokens`, `trace_id`. Priority order: `bash` first,
then keyword matches by distinct-keyword hit count, descending, file order on ties. `get_skill_bodies`
(keyword + hybrid) and the `model_decided` pre-loaded path both go through it. If nothing fits, the block
is empty (no orphan header).

Default rationale: the largest body (`query-elasticsearch`, 23,013 chars) plus `bash` (5,497) plus header
and separator is 28,749 chars, about 7,190 tokens. So 8,192 admits `bash` plus any one body, and no skill
becomes unreachable by keyword injection. A dropped skill stays reachable through the index and
`read_skill` in hybrid mode. Worst case per turn falls from about 28,600 tokens to 8,192, once.

**Out of scope:** FRE-1524 (how the warning is delivered), FRE-1138 (governor), FRE-1527 (recovery).

## Steps

1. Tests first (`tests/personal_agent/orchestrator/test_fre1529_volatile_once_per_turn.py`), confirm red:
   - AC-1: drive `step_llm_call` for round 1, append an assistant tool call and a tool result, set
     `tool_iteration_count` so that the call itself injects the budget warning, drive round 2. Assert the
     round-2 request holds exactly one `<skill_library>` and one `<turn_context>`.
   - AC-1b: the keyword match receives `ctx.user_message` on every round.
   - AC-2: with a prior turn in history, the system prompt and every prior-turn message are byte-identical
     across both rounds, and the query message bytes do not change between round 1 and round 2.
   - AC-3 (`tests/personal_agent/orchestrator/test_skills.py`): with a small cap, the block length is never
     above `cap * 4`, `skill_bodies_truncated` carries the dropped count and names, ranking keeps the
     higher-hit skill, a cap below every body gives `("", ())`, and the `model_decided` path is bounded.
2. `src/personal_agent/orchestrator/types.py`: add the flag.
3. `src/personal_agent/orchestrator/executor.py`: guard the inline (A), routing text (B), `model_decided`
   bound (C), update the ADR-0081 D2 comment to name the block owner.
4. `src/personal_agent/orchestrator/skills.py`: `fit_skill_bodies`, ranking, cap in `get_skill_bodies`.
5. `src/personal_agent/config/settings.py`, `.env.example`, `docs/reference/CONFIG_INVENTORY.md`
   (`uv run python scripts/audit/config_inventory.py generate` then `verify`).
6. Gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`.

## Codex plan review (one round) — dispositions

Codex confirmed the flag on every live path: `step_init` loads history, applies the context window and
the frozen reset before the first primary call. After the first inline, the only replacement of
`ctx.messages` is the FRE-1527 trim, which stubs tool results only. A new `ExecutionContext` serves each
request.

| # | Finding | Disposition |
|---|---------|-------------|
| 2 | Later rounds still re-select bodies, so the drop log and the `skill_bodies` component fire per round | **Folded in.** Selection runs only while `turn_context_inlined` is False |
| 5 | The cap is a 4 chars/token estimate; state what it covers; scan past an oversized body | **Folded in.** Cap covers header, separators and bodies. Usage directives are outside it. The scan continues past a body that does not fit |
| 6 | Cover the other injectors and the next turn | **Folded in.** Parametrized over all four injector shapes, plus a next-turn test |
| 4 | `ctx.user_message` is the request's ingress text: empty on an image-only turn, "yes" on a resumed attachment turn | **Accepted as stated.** Same text the round-1 match read before this change |
| 1 | `model_decided` priority under the cap is name order, not router order | **Declined.** Mode is not live (`hybrid` is). `ctx.loaded_skills` is an unordered set that `read_skill` also writes |
| 3 | Usage directives read `ctx.loaded_skills`, not the kept body names | **Declined.** Changes hybrid nudge behaviour, outside this ticket |

## Acceptance criteria → proof

| AC | Proof |
|----|-------|
| AC-1 one copy per turn | `test_round_two_after_budget_warning_has_one_skill_library` |
| AC-2 prefix stable | `test_prior_turns_and_system_prompt_byte_identical_across_rounds` |
| AC-3 bodies bounded | `test_get_skill_bodies_never_exceeds_cap`, `test_drop_is_logged_with_count` |
| AC-4 live | master's replay: one block in the final call, peak prompt tokens below 154,096, delivered |
