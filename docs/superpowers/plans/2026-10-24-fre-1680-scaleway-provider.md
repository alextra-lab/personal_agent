# FRE-1680 — Scaleway Generative APIs as a cloud provider

Ticket: FRE-1680 (Approved, Tier-2:Sonnet, stream:build2). Risk tier: Standard (touches `src/` logic,
cost and config). Codex plan review: required.

## Scope

Add `scaleway` as a picker-only cloud provider with two models: `glm-5.3` and `deepseek-v4.1-flash`.
No role binding changes (AC-4). Master writes the `.env` line at deploy.

Facts from master's probe (ticket comment, 2026-10-24):

- One OpenAI-style dialect fits both models. Reasoning is always on and arrives in the `reasoning` field.
- `chat_template_kwargs` and `thinking` have no effect. `reasoning_effort` is the only lever.
  Only the value `low` was measured (GLM: 26 reasoning tokens by default, 6 at `low`).
- Usage carries `prompt_tokens_details.cached_tokens` and `completion_tokens_details.reasoning_tokens`.
- Unsupported chat parameters: `frequency_penalty`, `n`, `top_logprobs`, `logit_bias`, `user`.

Facts measured in this session (litellm 1.106.0):

- litellm has a native `scaleway` provider (JSON-declared `openai_like`, base `https://api.scaleway.ai/v1`).
- The provider rides the OpenAI-SDK route. In a first attempt, a `scaleway` call with a passed-in
  `AsyncHTTPHandler` failed with `'AsyncHTTPHandler' object has no attribute 'api_key'`. That is an
  observation from this session, not a test in the PR. The PR test proves the chosen route works.
  So `scaleway` joins `_OPENAI_SDK_ROUTE_PROVIDERS`.
- Tool calling works on both models (master's probe, ticket comment 2026-10-24: `finish_reason: tool_calls`).
- The context window is 256K (owner's console, ticket body). The catalog declares 262144.
- The egress guard default mode is `blocklist`. No allowlist entry exists for any cloud provider, so none is added.
  The provider `base_url` is checked by layer 1 and the guarded client carries layer 2.

## Design decisions

| Decision | Choice | Reason |
|---|---|---|
| Setting | `scaleway_secret_key` (`AGENT_SCALEWAY_SECRET_KEY`), `json_schema_extra={"secret": True}` | Same shape as `openai_api_key`. `env_file` passes it to the gateway container. |
| Dialect | New `Dialect.SCALEWAY = "scaleway"` | No existing dialect fits. `ovh_qwen` allows `frequency_penalty`, which Scaleway rejects. |
| Dialect fields | `reasoning_effort`, `temperature`, `top_p`, `presence_penalty`, `seed` | Documented supported parameters minus `frequency_penalty`. |
| Effort domain | `{low, medium, high}` on the dialect, and `reasoning_effort_domain: [low, medium, high]` on both models | `none` is excluded: no off switch was found. Only `low` is measured. See risk R1. The dialect domain checks catalog modes only. The per-model domain makes the runtime gate refuse a call-site override such as `none` (codex finding 2). |
| Modes | `default` = `medium`, `worker` = `low`, `fast` = `low` | The guard needs a declared effort. `low` is the measured lever. |
| `SYNTHESIS_RETAINS_TOOLS` | `False` | `tool_choice="none"` with tools is not probed. `False` costs a cache miss only. |
| `LANDING_ACCEPTS_JSON_SCHEMA` | `False` | `response_format` with tools is not probed. Prose landing is the safe form. |
| `LANDING_SCHEMA_BREAKS_PREFIX_CACHE` | `False` | The prose landing sends no schema. |
| Prices | EUR x 1.18, as the OVH chat entry | GLM 2.124e-6 in / 6.49e-6 out. DeepSeek 7.08e-7 in / 2.124e-6 out (USD per token). |
| Cached price | Not modelled | `ModelDefinition` has no cached-price field. DeepSeek cached input (EUR 0.12) bills at the full input price. This over-counts. Follow-up only if the owner wants it. |
| Catalog keys | `glm-5.3-scaleway`, `deepseek-v4.1-flash-scaleway` | Follows `qwen3.8-27b-ovh`. |
| Provider ceiling | `max_concurrency: 50`; model ceiling 10 | Matches other cloud providers. |

## Steps

1. Egress route (DONE, test first). `tests/personal_agent/llm_client/test_litellm_client_egress_guard.py`
   gains `test_scaleway_request_reaches_the_guarded_transport` and
   `test_scaleway_blocked_host_never_reaches_a_transport`. `litellm_client.py` adds `scaleway` to
   `_OPENAI_SDK_ROUTE_PROVIDERS`.
   Verify: `uv run pytest tests/personal_agent/llm_client/test_litellm_client_egress_guard.py -q` -> 12 passed.
2. Failing tests first: `tests/personal_agent/llm_client/test_fre1680_scaleway.py` (see the AC table).
   Verify: `uv run pytest tests/personal_agent/llm_client/test_fre1680_scaleway.py -q` -> fails for the right reason.
3. `src/personal_agent/llm_client/models.py`: add `Dialect.SCALEWAY`, `DIALECT_FIELDS`, `DIALECT_VALUE_DOMAINS`,
   `SYNTHESIS_RETAINS_TOOLS`, `LANDING_ACCEPTS_JSON_SCHEMA`, `LANDING_SCHEMA_BREAKS_PREFIX_CACHE`. Update the
   "Five dialects" docstring.
4. `src/personal_agent/llm_client/litellm_client.py`: add `case Dialect.SCALEWAY` in `_dialect_params`.
5. `src/personal_agent/config/settings.py`: add `scaleway_secret_key` next to `voyage_api_key`.
6. `config/models.yaml`: add the `scaleway` provider and the two model entries. `.env.example` and
   `docs/guides/CLOUD_DEPLOYMENT.md`: add the key and the provider row.
7. `tests/personal_agent/config/catalog_snapshot_golden.json`: hand-edit five cells: provider concurrency
   `providers.scaleway.limit` 50, model concurrency for both new keys (limit 10), and two pricing entries.
   Resolution changes in zero cells. Add a "rebaselined" paragraph to the `test_catalog_snapshot.py` docstring.
   Also add both keys to `test_the_real_repos_six_kind_llm_entries_are_all_reachable` in
   `tests/personal_agent/config/test_reasoning_declaration_guard.py` (codex finding 1).
8. Gates: `make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.
9. Commit. Self-review with `feature-dev:code-reviewer` on `git diff origin/main...HEAD`. `security-review`
   (the diff touches a secret and network egress).
10. Rebase, push, open the PR, post the handoff comment on FRE-1680.

## Acceptance criteria and proof

| AC | Proof in this PR | Proof after deploy (master) |
|---|---|---|
| AC-1 both models list in the picker and a turn completes | Test: with `scaleway_secret_key` set, both keys appear in `role_candidates`; with it unset, they do not. Test: a dispatch through `LiteLLMClient` to a mock transport returns the reply. | Owner turn on each model. Logs show `model_call_completed` with `provider=scaleway`. |
| AC-2 ledger records provider and a non-zero cost matching the table | Test: after `register_model_pricing`, `litellm.cost_per_token` equals the table. Test: `record_api_call` is called with `provider="scaleway"` and `cost_usd` > 0 (`completion_cost` is not patched). | Query the cost ledger for the turn. |
| AC-3 thinking control works as declared | Test: the wire body carries `reasoning_effort` and carries no `chat_template_kwargs` and no `thinking`. Test: a mode with `frequency_penalty` or `enable_thinking` is refused at load. | Master measures reasoning tokens at `low`, `medium` and `high` on the deployed path (risk R1). |
| AC-4 no role binding changes | Test: every role resolves to a pre-existing key. `git diff origin/main -- config/model_roles.yaml` is empty. | Same diff at merge. |

## Risks

- R1. Only `reasoning_effort: low` is measured. The `default` mode declares `medium`, and the dialect domain
  lists `high`. If Scaleway rejects a value, the owner sees a 400 on the first turn. The AC-3 check on the
  deployed path covers `low`, `medium` and `high`. A rejection means a narrower domain and a one-line fix.
  This build makes no paid call. The probe was master's.
- R2. The `deepseek-v4.1-flash` call hung once for 120 s in master's probe. The model `default_timeout` of 180 s
  bounds it. No retry logic is added.
- R3. `SYNTHESIS_RETAINS_TOOLS=False` loses the prefix cache on the forced-synthesis call for these models
  when they serve as `sub_agent` through `inherit`. This is a cost and latency effect only.
- R4. The eval compose file (`docker-compose.eval.yml`) lists secrets one by one. It is not changed. An eval
  gateway has no Scaleway key, so the models do not list there.

## Out of scope

Role swaps (owner decision). A cached-price field. Reading the `reasoning` field into a trace.
The `.env` line (master, at deploy).
