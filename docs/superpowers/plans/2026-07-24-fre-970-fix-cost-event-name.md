# FRE-970 — ES telemetry skills misdirect cost/budget queries

**Ticket:** [FRE-970](https://linear.app/frenchforest/issue/FRE-970) · **Tier:** 2 (Sonnet) · **Risk:** Trivial (docs + guard test only, no `src/` logic change)

## Root cause (verified live against ES, 2026-07-24)

`docs/skills/self-telemetry.md` and `docs/skills/query-elasticsearch.md` tell the agent that
cloud LLM cost lives in event `litellm_request_complete`. That event was removed in FRE-376
Phase 3 (`src/personal_agent/llm_client/telemetry.py` docstring: "the legacy
`litellm_request_*` event names have been removed"). Live ES confirms:

- `litellm_request_complete`: **0** docs in the last 7 days.
- `model_call_completed` (`src/personal_agent/telemetry/events.py:MODEL_CALL_COMPLETED`,
  emitted by `llm_client/telemetry.py:emit_model_call_completed`, called from **both**
  `LocalLLMClient` and `LiteLLMClient`): 436 docs/7d, 309 with `cost_usd`. Fields: `model`,
  `provider`, `role`, `endpoint`, `latency_ms`, `input_tokens`, `output_tokens`,
  `total_tokens`, `cache_read_tokens`, `cost_usd` (via `extra`). **Not local-only** — the
  existing doc table row mislabels it "Local LLM (llama.cpp/MLX)"; it is the unified event
  for cloud + local since FRE-376 Phase 3.
- `api_cost_recorded` (`llm_client/cost_tracker.py`): 309 docs/7d, all with `cost_usd`. A
  parallel ledger — `provider`, `model`, `cost_usd`, `latency_ms`, `record_id`, `trace_id`,
  `session_id`, `cache_read_input_tokens`, `cache_creation_input_tokens`. **No `role` field.**
- `budget_counter_snapshot` (`cost_gate/gate.py:CostGate.snapshot_counters`, FRE-547): emitted
  once per configured cap every 60s. Fields: `role` (the **budget-role** key —
  `main_inference`, `entity_extraction`, `captains_log`, `skill_routing`, `study`,
  `artifact_builder`), `time_window` (`daily`/`weekly`), `window_start`, `running_total`,
  `cap_usd`, `utilization_ratio`. This is the direct route for "spend by budget cap" — the
  role-to-cap grouping (`cost_gate.budget_role_for()`,
  `config/governance/budget.yaml` — **not** `config/budget.yaml` as the ticket text says) is
  already folded into `running_total`, so no client-side join is needed for that recipe.
- `docs/skills/seshat-observations.md:31` also names `litellm_request_complete` as an example
  `event_type` value — same stale fact, same skills directory; folding in the one-line fix
  (ticket §5 "fold in, don't over-ticket" — non-ADR supporting fix in the same file class).

## Changes

1. **`docs/skills/self-telemetry.md`**
   - Frontmatter: drop `litellm_request_complete` from `canonical_patterns`; add
     `model_call_completed` / `api_cost_recorded`. Replace the `known_bad_patterns` entry that
     claims "Cloud path emits `litellm_request_complete`" (backwards) with one warning against
     the removed legacy event name.
   - Event-types table: replace the `litellm_request_complete` row and correct the
     `model_call_completed` row (unified, not local-only); add `api_cost_recorded` and
     `budget_counter_snapshot` rows.
   - Rewrite Patterns 1–3 (token stats, cache hit rate, cost breakdown) against
     `model_call_completed` (drop the cloud/local split — one query, `BY provider` or `BY
     role`).
   - Add new patterns: daily spend trend, spend by model, spend by budget-cap (using
     `budget_counter_snapshot`).
2. **`docs/skills/query-elasticsearch.md`**
   - Line ~314 `known_bad_patterns`/mistakes table: drop `litellm_request_complete` from the
     "known event_type values" list, add `model_call_completed` context + `api_cost_recorded`
     + `budget_counter_snapshot`.
   - Add a canonical recipe: cost/budget breakdown (trend, by role, by model, by budget-cap).
3. **`docs/skills/seshat-observations.md`** — one-line fix: swap the example `event_type`
   value from `litellm_request_complete` to `model_call_completed`.
4. **New guard test** — `tests/personal_agent/docs/test_skill_cost_event_names.py`: asserts
   `litellm_request_complete` does not appear in any `docs/skills/*.md` file (prevents the
   stale fact from returning).

## Acceptance criteria (from ticket)

- [ ] Corrected skills point at `model_call_completed` / `api_cost_recorded` as the cost
      source, not `litellm_request_complete` — proven by the guard test.
- [ ] Cost dimensions on `model_call_completed` documented (role, model, provider) so
      "breakdown by role"/"by model" is one aggregation each — proven by the new recipes.
- [ ] Budget/cap model documented (`config/governance/budget.yaml` +
      `cost_gate.budget_role_for()` + `budget_counter_snapshot`) so "breakdown by budget" is
      answerable — proven by the new budget-cap recipe, verified against live ES in this
      session (see Root cause above — real non-empty `running_total`/`cap_usd` values
      returned).
- [ ] Canonical recipes added for: daily spend trend, spend by role, spend by model, spend by
      budget-cap — using `_search`/ES\|QL that return values directly (no jq-iterate pitfall).

Live-turn verification ("fresh 7-day budget spend, trend, by budget then by role") is a
post-deploy step (skills are baked into the gateway image) — noted in the PR/ticket handoff,
not a pre-merge gate item.

## Test plan

- New test: `make test-file FILE=tests/personal_agent/docs/test_skill_cost_event_names.py`
  (grep-based, no LLM) — must fail before the doc fix, pass after.
- `make ruff-check` / `make ruff-format` / `make mypy` — new test file only, no `src/` change.
- No live gateway rebuild in this PR (docs baked into image — ask-first per ticket "Deploy"
  section); post-deploy live-turn verification goes in the Linear handoff comment.
