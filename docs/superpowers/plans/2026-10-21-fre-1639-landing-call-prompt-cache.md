# FRE-1639 — The helper landing call keeps the prompt cache

> Ticket: FRE-1639 (bugfix, Approved). Backing design: ADR-0150 D1 (the constrained landing),
> ADR-0149 D6 (the cache-preserving landing form). Tier: Standard (touches `src/` logic and cost).

## Cause (measured 2026-10-21, AC-2)

A probe sent the production request shapes to `gpt-6.1-sol` on the guarded Responses route
(`llm_client/responses_route.py`), 8 calls, about $0.10, synthetic content:

| Call | Input tokens | Cached tokens |
| -- | -- | -- |
| Round call, first | 10,518 | 0 |
| Round call, repeat | 10,518 | 10,515 |
| Same history, `tool_choice="none"` only | 10,518 | 10,443 |
| Same history, `response_format` + `tool_choice="none"` (the landing) | 10,819 | **0** |
| The landing, repeated | 10,819 | 10,744 |
| Same history, instruction appended, no schema | 10,534 | 10,443 |

- `response_format` becomes `text.format` on the Responses API. OpenAI renders the schema into
  the prompt ahead of the history (+301 input tokens). The prefix changes from its first token,
  so a landing on a fresh history misses the whole cache. Each helper has its own history, so
  every landing misses.
- `tool_choice="none"`, a larger `max_tokens` and an appended message do not break the cache.
- The same field is harmless where the schema is a sampling grammar and not prompt text
  (llama.cpp, OVH vLLM). Anthropic already takes the prose landing
  (`LANDING_ACCEPTS_JSON_SCHEMA` is `False`). Only `OPENAI_GPT5` is affected. llama.cpp and OVH
  are not probed here (production llama.cpp is held for a measurement window).

## Design

For a dialect whose provider renders `response_format` into the prompt prefix, the landing
call sends **no** `response_format`. It appends one user message after the history that
carries the schema as text. The suffix per path: the voluntary stop adds `[schema]`, the
forced synthesis adds `[synthesis instruction, schema]` (two user messages in a row; the
Responses wire has no alternation rule, and the live check sends this exact shape). The
existing scorer (`_score_schema_landing`, the deterministic
parse / validate backstop of ADR-0150 D1) judges the reply unchanged. Other dialects send the
same request as today.

Rejected: the same `response_format` on every round (a round could answer in JSON and end the
loop; llama.cpp grammar with tools is unsafe) · a `submit_report` strict tool (the tools block
is the first prefix item, so adding a tool to the landing misses the cache the same way, and
adding it to every round gives the model a way to stop early) · the prose landing for OpenAI
(loses the structured report the primary reads).

## Changes

1. `src/personal_agent/llm_client/models.py` — add `LANDING_SCHEMA_BREAKS_PREFIX_CACHE`
   (`Mapping[Dialect, bool]`, `True` for `OPENAI_GPT5` only) and
   `landing_schema_breaks_prefix_cache(dialect)` (`False` for `None`). Comment carries the
   measurement above.
2. `src/personal_agent/orchestrator/sub_agent.py` —
   - `_landing_schema_message()` returns the user message that states the JSON Schema as text.
   - `_write_landing_report`: when `schema_active` and the dialect breaks the prefix cache,
     `response_format = None`. The trim input becomes the new list
     `[*state.messages, schema_message]`, so the suffix counts toward the fit check and
     `state.messages` is never mutated (the trim returns its input list in two no-op cases).
     Add `schema_in_suffix` to the `sub_agent_forced_synthesis` log event.
   - Update the `_score_schema_landing` docstring: it scores any schema-backed landing, sent
     with `response_format` or with the schema in the suffix.
   - Known exception: when the trim fits the window it stubs older tool results, and the
     prefix differs from the first stub on. The trim fires only near a full window (Sol:
     1.05M tokens), so it is outside AC-1. The comment and the handoff say so.
3. Docs: one line on `LANDING_ACCEPTS_JSON_SCHEMA`'s comment and ADR-0150 D1 pointing at the
   new mapping (only if the ADR text names the OpenAI wire form).

## Tests (TDD — write first, confirm they fail)

- `tests/personal_agent/llm_client/test_landing_schema_breaks_prefix_cache.py` — mapping per
  dialect; `None` returns `False`.
- `tests/personal_agent/orchestrator/test_sub_agent_landing_cache_prefix.py`
  - **AC-1** (when no trim fires) voluntary-stop path and forced-synthesis path,
    `OPENAI_GPT5`: snapshot (at call time, `json.dumps`) the last round's `messages` and
    `tools`; the landing's `tools` is byte-identical and its `messages[:n]` is byte-identical;
    the suffix is exactly `[schema]` (voluntary) or `[instruction, schema]` (forced);
    `response_format is None`; `tool_choice == "none"`.
  - `state.messages` is unchanged after the run, for the no-op trim cases (no context
    length, and no tool messages). A spy on `_trim_for_landing` sees the schema message
    as the last item of its input.
  - **AC-2** wire check through `to_responses_request`: the pre-fix landing request carries a
    `text` key and the round request does not (the cause field, seeded negative); the new
    landing request carries no `text`, and its `tools` and `input[:n]` equal the round's.
  - The landing still scores `synthesized` from a valid JSON reply, and `ledger` from prose.
  - Negative: `LLAMACPP_QWEN` keeps `response_format` and appends no schema message.
- Limit of the AC-2 wire test: it rebuilds the request by hand, so it shows the causal field
  and the translated prefix, not provider cache reuse. The end-to-end script below and AC-4
  carry that proof.
- **AC-3**: `uv run pytest tests/personal_agent/orchestrator/test_sub_agent_report_schema.py
  tests/personal_agent/orchestrator/test_sub_agent.py -q` passes with no edit to either file.

## Live evidence (offline script, scratchpad, not committed)

Run `run_sub_agent` end to end with a stub client that forwards each request to the real
Responses route on `gpt-6.1-sol`, for both paths (voluntary stop, forced synthesis), 5 runs
each. Record `cached_tokens / input` per landing and the share of replies that pass
`_parse_and_validate_worker_report`. Acceptance: at least 80% cached on every landing and
10 of 10 valid reports. If a reply arrives in a code fence, add fence stripping to the parse
and test it. If the pass rate is lower, stop and surface the design to the owner.

## Gates

`make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.
Self-review on the committed diff. Diff class: touches no write path, deletion, schema, cost or
governance code → self-serve.

## Post-deploy (master)

Deploy class: `seshat-gateway` rebuild (held while the FRE-1633 binding decision is open).
On the owner's next cloud HYBRID turn on `gpt-6.1-sol`, each landing call's `cache_read_tokens`
is at least 80% of its input (AC-4), and `report_kind` is `synthesized` for each helper.

## Result (2026-10-21)

- Unit and wire tests: new files pass. With `OPENAI_GPT5` set to `False`, 5 of the 6 new tests
  in `test_sub_agent_landing_cache_prefix.py` fail (the grammar-dialect test stays green).
- End to end on `gpt-6.1-sol` (real `run_sub_agent`, real Responses route, about 12K-token
  histories, unique per run): 5 voluntary stops and 5 forced syntheses. Landing cached
  12,114/12,657 · 11,833/12,376 · 11,271/11,814 · 11,833/12,376 · 12,394/12,937 (stop) and
  12,676/13,408 · 11,271/12,003 · 12,395/13,127 · 11,552/12,284 · 12,956/13,688 (forced):
  93.9% to 95.8%. All 10 reports parsed and scored `synthesized`. Probe spend about $0.70.
- The 4-6% uncached part is the schema message and the `DONE` note. It shrinks as the history
  grows (a 100K history leaves under 1%).
