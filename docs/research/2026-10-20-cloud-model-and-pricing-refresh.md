# Cloud model and pricing refresh (FRE-1621)

> Date: 2026-10-20 · Ticket: [FRE-1621](https://linear.app/frenchforest/issue/FRE-1621) · Input for:
> [FRE-1620](https://linear.app/frenchforest/issue/FRE-1620) · Desk research under the
> [FRE-1573](https://linear.app/frenchforest/issue/FRE-1573) model-testing hold.

## 1. Findings

1. **The `claude_sonnet` price is wrong by 50%.** The catalog records $3 / $15 per MTok. The official
   price of `claude-sonnet-5` is $2 / $10. Anthropic cancelled the planned 2026-09-01 increase. The
   cost gate and the budget lanes therefore record Sonnet spend at about 1.43x the real spend (§3.2).
   This is a config-only correction, and it ships first (§9, ticket A).
2. **Every other recorded price is correct.** `claude_haiku`, `gpt-5.4-mini`, `qwen3.8-27b-ovh`,
   `embedding` and `reranker` match the official price on 2026-10-20 (§3.1).
3. **litellm 1.98.0 cannot send a reasoning effort to any gpt-6 model.** It raises
   `UnsupportedParamsError: openai does not support parameters: ['reasoning_effort']` for
   `gpt-6-luna` and `gpt-6.1-sol`. The gpt-6 support arrived in litellm 1.104.0, and 1.100.5 has a
   backport. litellm must move to 1.104.0 or later before any gpt-6 entry exists (§6.1).
4. **Claude Sonnet 5.5 rejects forced tool use.** Three producers bound to `claude_sonnet` force a
   named tool: `span_extraction`, `entailment` and `session_summary`. A plain id swap to
   `claude-sonnet-5-5` makes each of them fail with a 400. `claude-sonnet-5` stays available until
   2027-06-30 at the earliest, so there is no time pressure (§6.2).
5. **Recommended replacements:**
   - `gpt-5.4-mini` → `gpt-6-luna`. Luna costs $0.10 / $0.50 per MTok, which is 7.5x less on input
     and 9x less on output. At effort `none` it keeps the `temperature: 0.0` pin of FRE-758.
   - `claude_haiku` → `claude-haiku-5-5`. Haiku 5.5 is 10x cheaper below 100K prompt tokens.
     Haiku 4.5 is past its earliest retirement date.
   - `claude_sonnet` → `claude-sonnet-5-5`, but only after a code change for forced tool use and
     new measurements.
6. **No swap of a role binding is safe from this desk research alone.** §7 lists the evidence that
   each binding needs. Each item is an eval run, so each one waits for the FRE-1573 hold to end.

## 2. Method, and what the analysis did not do (AC-5)

The analysis read official provider pages, the provider model-list endpoints, the litellm package and
the litellm GitHub repository, the repo, and one read-only query on the cost ledger.

- **No model calls in the analysis.** No request in the analysis went to any inference endpoint,
  paid or local.
- **Three confirmation calls after the analysis, at the owner's direction.** On 2026-10-20 the
  owner asked for a real call to each cloud model id in the config. The question was whether the
  config names aliases or pinned snapshots (§4.4). I sent one direct API request per id:
  `claude-sonnet-5`, `claude-haiku-4-5-20251001` and `gpt-5.4-mini`. Each request asked for the
  word "ok", with `max_tokens` 16, and used 12 to 15 input tokens and 4 output tokens. The total
  cost was below $0.001. The requests did not go through the gateway, so they wrote nothing to
  memory or to `api_costs`.
- **No config change.** `config/`, `.env` and the deployment are unchanged. The PR diff contains only
  `docs/`.
- **Metadata reads only.** These requests list models or describe a package. None of them runs a
  model:
  - `GET https://api.anthropic.com/v1/models` and `GET /v1/models/{id}` for four ids. These are free
    metadata reads.
  - `GET https://api.openai.com/v1/models`, a free metadata read.
  - `GET https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/models`, the OVH model records with prices.
  - The PyPI JSON for `litellm`. I read ten litellm wheels as zip archives. I did not install or
    run them.
  - `gh search` on `BerriAI/litellm`.
- **litellm checks ran offline.** `litellm.utils.get_optional_params` was called in-process on the
  installed litellm 1.98.0. It builds request parameters and sends nothing.
- **One ledger read.** A `SELECT` on `api_costs` in `cloud-sim-postgres` gave the 30-day token mix
  (§3.2). It wrote nothing.

All provider pages were read on **2026-10-20**. That date is the read date for every source in this
note.

## 3. Pricing (AC-1)

### 3.1 Every catalog entry against the official price

Prices are USD per million tokens unless the row says EUR. "Δ" is the recorded price against the
official price: (recorded − official) / official.

| Catalog key | Model id | Recorded in / out | Official in / out | Cached input (read) | Cache write | Batch | Context tier | Δ | Verdict | Source |
|---|---|---|---|---|---|---|---|---|---|---|
| `gpt-5.4-mini` | `gpt-5.4-mini` | $0.75 / $4.50 | $0.75 / $4.50 | $0.075 | No write charge is listed for this model | $0.375 / $2.25 | None listed | 0% | **Correct** | [OpenAI pricing](https://developers.openai.com/api/docs/pricing), [model page](https://developers.openai.com/api/docs/models/gpt-5.4-mini) |
| `claude_haiku` | `claude-haiku-4-5-20251001` | $1 / $5 | $1 / $5 | $0.10 | $1.25 (5m), $2 (1h) | $0.50 / $2.50 | None | 0% | **Correct** | [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing) |
| `claude_sonnet` | `claude-sonnet-5` | $3 / $15 | **$2 / $10** | $0.20 | $2.50 (5m), $4 (1h) | $1 / $5 | None: the 1M window bills at the standard rate | **+50% / +50%** | **Wrong** | [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing), footnote 3 |
| `qwen3.8-27b-ovh` | `Qwen3.8-27B` | $0.47 / $3.19 | €0.40 / €2.70 list. The OVH record says $0.00000047 / $0.00000319 per token | Not offered: the record says `input_cache_reads` 0 | Not offered | None listed | None listed | 0% against the USD record | **Correct** | [OVH catalog](https://www.ovhcloud.com/en/public-cloud/ai-endpoints/catalog/), OVH `/v1/models` |
| `embedding` | `Qwen3-Embedding-8B` | €0.10 input | €0.10 input list. The OVH record says $0.00000012 per token | n/a | n/a | n/a | n/a | 0% | **Correct** | [OVH catalog](https://www.ovhcloud.com/en/public-cloud/ai-endpoints/catalog/), OVH `/v1/models` |
| `reranker` | `rerank-2.5` | $0.05 input | $0.05 input | n/a | n/a | n/a | n/a | 0% | **Correct** | [Voyage pricing](https://docs.voyageai.com/docs/pricing) |

Anthropic footnote 3, quoted: *"The $2/$10 per million input/output token pricing for Claude Sonnet 5,
announced at launch as introductory pricing through August 31, 2026, is now the standard price. The
previously scheduled increase to $3/$15 per million input/output tokens on September 1, 2026 will not
occur."*

Three side notes. None of them changes a verdict:

- The OVH record for `Qwen3.8-27B` now carries real prices. The catalog comment in
  `config/models.yaml:475` says that the record read 0 on 2026-09-06. The comment is stale, and the
  carried-forward values are now provider truth.
- Voyage now lists `rerank-2.5` under older models and offers `rerank-3` at the same $0.05. A
  reranker swap needs the FRE-695 quality comparison, so it is out of scope here.
- Anthropic adds a 1.1x multiplier for `inference_geo: "us"`, and OpenAI adds 10% for regional
  processing. The repo sets neither, so the standard rates apply.

### 3.2 What the wrong Sonnet price does to the cost gate

**How the gate gets its prices.** `register_model_pricing()` (`src/personal_agent/llm_client/pricing.py`)
pushes `input_cost_per_token` and `output_cost_per_token` from the catalog into `litellm.model_cost`.
The pre-call reservation reads those rates (`cost_estimator.py`). The post-call commit uses
`litellm.completion_cost()`, which reads the same table. The catalog has no field for cache prices.
`register_model` merges the two catalog rates onto litellm's own entry, so cache reads and cache
writes bill at **litellm's** rates. I measured this in-process: after registration, `claude-sonnet-5`
carries input $3 and output $15 from the catalog, with cache read $0.20 and cache write $2.50 / $4
from litellm. The cache rates are correct. Only the two catalog rates are wrong.

**Size of the error on real traffic.** The `api_costs` ledger for the last 30 days records 448 Sonnet
calls and $16.55. The token mix was:

| Uncached input | Output | Cache read | Cache write |
|---|---|---|---|
| 723,256 | 767,406 | 1,032,566 | 486,138 |

Uncached input is `input_tokens` minus the cache reads and cache writes. Priced on that mix:

| Rates | Cost |
|---|---|
| Catalog rates (input $3, output $15) with litellm cache rates | $15.10 |
| Official rates (input $2, output $10) with the same cache rates | $10.54 |

The gate therefore records about **1.43x** the real Sonnet spend (+43%). Input and output tokens are
91% of the cost, which is why the 50% rate error shows up as 43% in total. The ledger total is $16.55,
and my reconstruction at catalog rates is $15.10. I did not explain that 9.6% gap. It does not change
the direction or the size class of the error.

**Effect on the caps.** Each Sonnet-bound producer bills to a lane
(`src/personal_agent/cost_gate/role_map.py`). The caps below come from `config/governance/budget.yaml`.
That file is gitignored. The worktree copy and the `/opt/seshat` copy have the same caps. I did not
read the copy inside the deployed gateway.

| Lane | Sonnet producers | Caps |
|---|---|---|
| `captains_log` | `captains_log`, `session_summary` | $5 daily |
| `span_extraction` | `span_extraction` | $5 daily, $7 weekly |
| `entity_extraction` | `entailment` (shared with the `gpt-5.4-mini` extractor) | $5 daily, $7 weekly |
| `artifact_builder` | `artifact_builder` | $5 daily, $18 weekly |
| `main_inference` | `vision`, and a Sonnet primary when one is selected | $10 daily, $25 weekly |
| `insights` | `insights` | No per-role cap (`uncapped_roles`) |
| `_total` | all | $30 weekly |

A lane that only Sonnet fills reaches its cap at about 70% of the cap in real spend. For example, the
$5 `captains_log` daily cap denies calls at about $3.50 of real spend. The error fails safe, because
the gate over-reserves. It still denies work early, and it overstates spend in every cost panel.

**Cache billing on the new models.** litellm 1.98.0's bundled price map does not contain
`claude-haiku-5-5`, `claude-sonnet-5-5`, `gpt-6-luna` or `gpt-6.1-sol`. They are present today only
because litellm fetches a remote map at import. If that fetch fails at gateway start, litellm warns
that "cache cost fields will default to 0". Cache tokens then bill at $0, and the 5.5 models lose
their adaptive-thinking flag (`reference_model_catalog_and_params` memory, FRE-1007). litellm 1.106.0
bundles all four models (§6.1). The catalog has no cache-price field, so a pinned litellm version is
the only config-side protection.

## 4. Current models (AC-2)

The `/v1/models` lists of both providers were read on 2026-10-20. Anthropic serves 14 ids, and the
newest are `claude-haiku-5-5`, `claude-sonnet-5-5` and `claude-opus-5-5`. OpenAI serves `gpt-6-astra`,
`gpt-6-sol`, `gpt-6.1-sol` and `gpt-6-luna`. It still serves `gpt-5.4-mini` and the `gpt-5.6-*` family.

### 4.1 Candidates

| Field | `gpt-6-luna` | `gpt-6.1-sol` (considered) | `claude-haiku-5-5` | `claude-sonnet-5-5` |
|---|---|---|---|---|
| Replaces | `gpt-5.4-mini` | `claude_sonnet` (alternative) | `claude_haiku` | `claude_sonnet` |
| Context window | 1,050,000; max input 922,000 | 1,050,000; max input 922,000 | 1,000,000 | 1,000,000 |
| Max output | 128,000 | 128,000 | 128,000 (300K on Batch with a beta header) | 128,000 (300K on Batch with a beta header) |
| Price in / out per MTok | $0.10 / $0.50; above 272K: $0.20 / $0.75 | $2 / $10; above 272K: $4 / $15 | $0.10 / $0.50 up to 100K prompt tokens; $0.50 / $2.50 above | $2 / $10 |
| Cached input / cache write | $0.01 / $0.125 (1.25x) | $0.10 / $2.50 | $0.01 / $0.125 (5m), $0.20 (1h); above 100K: $0.05 / $0.625, $1 | $0.10 / $2.50 (5m), $4 (1h) |
| Batch | 50% off: $0.05 / $0.25 | 50% off: $1 / $5 | 50% off | 50% off: $1 / $5 |
| Tool calling | Yes. On Chat Completions, **only with `reasoning_effort: none`** | Yes, but **Responses API only**: "Chat Completions does not support function calling with … GPT-6.1 Sol" | Yes. A forced `tool_choice` works, but the response has no thinking block | Yes. **A forced `tool_choice` (`any`, `tool`) returns 400** |
| Structured output | Yes | Yes | Yes (`structured_outputs.supported: true` in `/v1/models`) | Yes (`structured_outputs.supported: true`) |
| Prompt caching | Yes. Minimum 1,024 tokens. TTL `30m` only, set with `prompt_cache_options.ttl` | Yes, same rules. Cache read is 0.05x input | Yes. Minimum 512 tokens. TTL 5m or 1h | Yes. Minimum 512 tokens. TTL 5m or 1h. Cache read is 0.05x input |
| Reasoning parameter | `reasoning_effort` (Chat), `reasoning.effort` (Responses) | `reasoning_effort` / `reasoning.effort` | `output_config.effort`, with `thinking: {type: adaptive}` | `output_config.effort`, with `thinking: {type: adaptive}` |
| Reasoning values | `none`, `low`, `medium`, `high`, `xhigh`, `max` | `low`, `medium`, `high`, `xhigh`, `max` (`none` and `minimal` are not supported) | `low`, `medium`, `high`, `xhigh`, `max` | `low`, `medium`, `high`, `xhigh`, `max` |
| Reasoning default | `medium` | `medium` | `medium` | `high` |
| Sampling | `temperature` and `top_p` only at effort `none`. Remove them at any other effort | Same rule, so in practice none (no `none` effort) | Omit them. `temperature` must be 1, `top_p` 0.99, and `top_k` always fails | Omit them. Any non-default value returns 400 |
| Thinking off | Effort `none` | Not possible | `thinking: {type: disabled}` at `high` or below | `thinking: {type: between_tools}` at `high` or below. `disabled` returns 400 |
| Tokenizer | Not stated | Not stated | About 30% more tokens than Haiku 4.5 for the same text | Same as Sonnet 5 |
| Released | 2026-09-22 (announcement) | 2026-09-29 (product news) | 2026-10-07 | 2026-09-28 |
| Retirement of this model | Not stated | Not stated | Not sooner than 2027-10-07 | Not sooner than 2027-09-28 |
| Sources | [model page](https://developers.openai.com/api/docs/models/gpt-6-luna), [latest-model guide](https://developers.openai.com/api/docs/guides/latest-model), [prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching), [pricing](https://developers.openai.com/api/docs/pricing), [announcement](https://community.openai.com/t/announcing-gpt-6-sol-and-gpt-6-luna-in-the-api-codex-and-chatgpt/1399925) | [model page](https://developers.openai.com/api/docs/models/gpt-6.1-sol), [reasoning guide](https://developers.openai.com/api/docs/guides/reasoning), [pricing](https://developers.openai.com/api/docs/pricing), [product news](https://openai.com/news/product-releases/) | [model page](https://platform.claude.com/docs/en/models/haiku-5-5/overview), [migration guide](https://platform.claude.com/docs/en/models/haiku-5-5/migration-guide), [effort](https://platform.claude.com/docs/en/build-with-claude/effort), [pricing](https://platform.claude.com/docs/en/about-claude/pricing) | [model page](https://platform.claude.com/docs/en/models/sonnet-5-5/overview), [what's new](https://platform.claude.com/docs/en/models/sonnet-5-5/whats-new-sonnet-5-5), [effort](https://platform.claude.com/docs/en/build-with-claude/effort), [pricing](https://platform.claude.com/docs/en/about-claude/pricing) |

The Anthropic context, output, effort and thinking-type values also come from the live
`/v1/models/{id}` records. Those records agree with the documentation. For Haiku 5.5 the record shows
`thinking.types.disabled: true` and `between_tools: false`. For Sonnet 5.5 it shows `disabled: false`
and `between_tools: true`.

I did not read published benchmarks for any model. This note makes no claim about quality.

### 4.2 Lifecycle of the entries that the candidates replace

| Entry | Status on 2026-10-20 | Retirement | Source |
|---|---|---|---|
| `gpt-5.4-mini` (snapshot `gpt-5.4-mini-2026-03-17`) | Served. No deprecation entry. The model page shows no legacy label | None announced | [deprecations](https://developers.openai.com/api/docs/deprecations), [model page](https://developers.openai.com/api/docs/models/gpt-5.4-mini) |
| `claude-haiku-4-5-20251001` | Active | "Not sooner than October 15, 2026". **That date is past.** Anthropic gives at least 60 days' notice before it retires a model | [deprecations](https://platform.claude.com/docs/en/about-claude/model-deprecations) |
| `claude-sonnet-5` | Active | Not sooner than 2027-06-30 | [deprecations](https://platform.claude.com/docs/en/about-claude/model-deprecations) |

OpenAI deprecated the sibling `gpt-5.4-nano` on 2026-10-01 and named `gpt-6-luna` as its replacement.
The shutdown date is 2027-04-01. This is the closest official signal for the direction of
`gpt-5.4-mini`, but it is not a deprecation of `gpt-5.4-mini`.

### 4.3 Catalog values that are not prices

These are catalog facts that the providers now report differently. They are not in ticket A, because
`context_length` feeds the context budget and is not inert:

| Entry | Catalog `context_length` | Provider value | Source |
|---|---|---|---|
| `claude_sonnet` | 200000 | 1,000,000 | Anthropic `/v1/models/claude-sonnet-5` |
| `gpt-5.4-mini` | 128000 | 400,000 window, 272,000 max input | [model page](https://developers.openai.com/api/docs/models/gpt-5.4-mini) |

### 4.4 Alias or pinned snapshot

The confirmation calls in §2 returned the model that each API reports as served:

| Catalog key | Config id | Served model in the response | Kind |
|---|---|---|---|
| `claude_sonnet` | `claude-sonnet-5` | `claude-sonnet-5` (`msg_011CgCyg82fSkPTHDzDQdJvx`) | Pinned snapshot |
| `claude_haiku` | `claude-haiku-4-5-20251001` | `claude-haiku-4-5-20251001` (`msg_011CgCyhYCzp8cGJo91C7XcC`) | Pinned, dated snapshot |
| `gpt-5.4-mini` | `gpt-5.4-mini` | `gpt-5.4-mini-2026-03-17` | **Alias** to a dated snapshot |

- **Anthropic.** "Every Claude model ID is a pinned snapshot, including the dateless IDs used from
  the 4.6 generation on" ([models overview](https://platform.claude.com/docs/en/models/overview)).
  `claude-sonnet-5` is therefore pinned, although it has no date. The dated Haiku id is pinned.
  Its alias `claude-haiku-4-5` is not in the config.
- **OpenAI.** `gpt-5.4-mini` is an alias. OpenAI can point it at a later snapshot, and the config
  would then run a different model with no change in the repo. The pinned form is
  `gpt-5.4-mini-2026-03-17`, which `/v1/models` lists. The ledger records the request id
  `openai/gpt-5.4-mini` and not the served snapshot, so the ledger cannot show such a move.
- **gpt-6-luna.** Its model page lists one snapshot, `gpt-6-luna`, which is also the default. Today
  the name and the snapshot are the same. A later dated snapshot can make it an alias.
- **The served name is what the API reports.** It is the provider's statement, and it is the
  strongest check available from outside the provider.

**Recommendation.** Pin OpenAI ids to the dated snapshot in the catalog, as the Anthropic ids already
are. For `gpt-5.4-mini` that is `gpt-5.4-mini-2026-03-17`. This is a separate one-line catalog
change. It changes the model string in `api_costs` and in any panel keyed on it, so it does not
belong in ticket A. For `gpt-6-luna`, ticket B uses `gpt-6-luna`, because no dated snapshot
exists yet.

## 5. Where each model is named in the repo (AC-3)

Appendix A lists every reference with file:line: 287 files and 1,188 lines. The pattern is
`gpt-5\.4-mini|claude_sonnet|claude_haiku|claude-sonnet-5|claude-haiku-4-5`. This section classifies
the references that a change must touch.

**Catalog and bindings.**
- `config/models.yaml:557-602` (`claude_sonnet`), `:604-631` (`claude_haiku`), `:634-681`
  (`gpt-5.4-mini`).
- `config/model_roles.yaml`:
  - `gpt-5.4-mini` binds `entity_extraction` (`:88`) and `compressor` (`:101`).
  - `claude_sonnet` binds seven roles: `artifact_builder` (`:66`), `captains_log` (`:89`),
    `session_summary` (`:99`), `insights` (`:100`), `span_extraction` (`:114`), `entailment`
    (`:123`) and `vision` (`:131`).
  - `claude_haiku` binds no role. It is reachable through the open `primary` picker and as eval
    judge 3.

**Code that names a catalog key.** These pin the key and not the model, so they survive an `id` swap.
They break on a key rename:
- `src/personal_agent/config/model_loader.py:732-733`: `_EXPECTED_VISION_ROLES` lists `claude_sonnet`
  and `claude_haiku`.
- `docker/postgres/migrations/0020_session_model_selections.sql:43`: stored selections map `cloud`
  to `claude_sonnet`. Rows in `session_model_selections` hold catalog keys.
- `seshat-pwa/src/__tests__/` (6 files) and about 90 test files under `tests/` use keys as fixtures.

The rest of `src/` names models only in comments and docstrings. Those must change only where the
text describes current behavior, for example `session_summary.py:632` and `session_digest_wire.py:316`.

**Code whose behavior depends on the model.**
- **Forced tool choice.** `grounding/extractor.py:222` (`span_tool_choice`),
  `grounding/entailment.py:185` (`entailment_tool_choice`) and `memory/session_digest_wire.py:337`
  (`digest_tool_choice`, used at `second_brain/session_summary.py:636`). Each one returns
  `{"type": "function", "function": {"name": …}}`. litellm 1.98.0 maps that to Anthropic
  `{"type": "tool", "name": …}`, verified offline, which Sonnet 5.5 rejects. The eval pilot
  `scripts/eval/digest_contract_pilot.py:184` does the same.
- **Call-site temperature.** `orchestrator/context_compressor.py:259` sends `temperature=0.2`, and
  `second_brain/entity_extraction.py:1171` sends the mode temperature. Both are legal on `gpt-6-luna`
  at effort `none`. Both fail on Haiku 5.5, where litellm 1.98.0 raises
  `claude-haiku-5-5 does not support temperature=0.2. Only temperature=1 is supported`.
- **Dialect tables.** `llm_client/models.py:65-69` (`Dialect`), `:120-121` (`ModeSpec`), `:141-180`
  (`DIALECT_FIELDS`, `DIALECT_VALUE_DOMAINS`) and `:928-937` (the `openai_gpt5` temperature rule).
  The `ModeSpec` types are:
  - `reasoning_effort: Literal["none", "low", "medium", "high", "xhigh"]`, which has no `max`.
  - `effort: Literal["low", "medium", "high"]`, which has no `xhigh` and no `max`.
- **Config guard.** `config/config_guard.py:1361-1546` holds the per-dialect reasoning-declaration
  checks, including the `anthropic_budget` branch for Haiku 4.5.
- **Price constants in eval scripts.** `scripts/eval/fre630_extraction_quality/cells.py:138-140`
  hard-codes `$3/$15` for `claude-sonnet-5` and `$0.75/$4.50` for `gpt-5.4-mini`. The Sonnet figure
  is wrong for the same reason as the catalog.

**Eval judges.** `scripts/eval/fre1598/judge.py:51`:
`JUDGES = {"1": "claude_sonnet", "2": "gpt-5.4-mini", "3": "claude_haiku"}`. Judge 3 is the
tie-break judge (`judge third`). `scripts/eval/fre1615/judge.py:229-234` reads the same table. The
judges resolve through **catalog keys**, so an `id` swap under an existing key silently changes the
panel for every later run. `judge_pins()` (`judge.py:170`) records each judge's resolved `model_id`
in the run, so the change is visible in the run files. It is not visible in the code.

**What a judge swap means for comparison with past runs.** A judge is part of the instrument. A run
labelled by `claude-haiku-5-5` as judge 3 is not comparable to a run labelled by `claude-haiku-4-5`.
A difference between the two can come from the judge as well as from the arms. For FRE-1598 and
FRE-1615 a valid comparison needs one of these:
- Re-label the stored replies of the past runs with the new panel. The replies are on disk, so this
  costs only judge calls, and it gives a paired comparison.
- Keep the old panel pinned for the duration of a study.

Agreement between the old and the new judge on the same replies measures the judge change itself. It
must be reported before a new-panel result is set against an old one. Re-labelling is a paid run
under the hold, so it waits for the owner's go.

**Historical text.** References under `docs/` (127 files) and `telemetry/` and `experiments/`
(16 files) record past decisions and measured runs. They must not change.

## 6. API changes that the dialect layer must handle

### 6.1 litellm version

| litellm | Released | gpt-6 in `GPT_REASONING_SERIES_MARKERS` | Bundled map has the new models |
|---|---|---|---|
| 1.98.0 (pinned floor `>=1.98.0`, locked) | 2026-08-22 | No: `"gpt-5" in model` only | None of the four |
| 1.100.0 | 2026-09-06 | No | None |
| 1.100.5 (patch) | 2026-10-07 | Yes | None |
| 1.104.0 | 2026-10-03 | Yes | `gpt-6-luna` |
| 1.105.1 | 2026-10-14 | Yes | `gpt-6-luna`, `gpt-6.1-sol`, `claude-sonnet-5-5` |
| 1.106.0 (latest) | 2026-10-18 | Yes | All four, including `claude-haiku-5-5` |

Sources: the PyPI release history, and the wheels read as archives
(`litellm/llms/openai/chat/gpt_5_transformation.py` and `model_prices_and_context_window_backup.json`).

- **gpt-6 needs an upgrade.** On 1.98.0, `get_optional_params(model="gpt-6-luna",
  reasoning_effort="none")` raises `UnsupportedParamsError`. The same call on `gpt-5.4-mini` and
  `gpt-5.6-luna` passes. Every bound deployment must declare a reasoning effort, and `none` is the
  declaration that keeps the FRE-758 pin. So every call to a bound `gpt-6-luna` entry fails on
  1.98.0. The config guard does not catch this: for the `openai_gpt5` dialect,
  `_dialect_declares_effort` (`config_guard.py:1414-1444`) accepts `none` without the litellm probe.
  The entry loads and boots, and the error appears only at call time. The ticket order is the
  protection: ticket B upgrades litellm in the same PR that adds the entry.
- **Anthropic 5.5 works on 1.98.0, with a condition.** It maps `reasoning_effort` correctly for both
  5.5 models (`thinking: {type: adaptive}`, `output_config: {effort: …}`, verified offline). That
  holds only while the remote map fetch succeeds at import (§3.2).
- **Choose the release by age.** 1.106.0 is two days old. 1.105.1 is six days old and lacks
  `claude-haiku-5-5` in its bundled map. 1.104.x is about two weeks old. The build ticket must name
  the version and record why it was chosen.

Open litellm issues that touch this change, read on GitHub on 2026-10-20:

- [#44298](https://github.com/BerriAI/litellm/issues/44298): Sonnet 5.5 `thinking.disabled` "silently
  drops to full adaptive thinking instead of between_tools". This repo does not send `disabled`, so
  it does not apply today. A future "thinking off" mode on Sonnet 5.5 must send `between_tools`.
- [#46074](https://github.com/BerriAI/litellm/issues/46074): litellm forwards a forced `tool_choice`
  verbatim to models that do not support forced tool use. This matches the offline result in §5.
- [#45998](https://github.com/BerriAI/litellm/issues/45998): `is_gpt_reasoning_series_name` checks
  the raw model path. The repo dispatches `openai/gpt-6-luna`, so the build must verify the
  classification on the prefixed form.

### 6.2 Per-model wire changes

| Change | Affects | What the build must do |
|---|---|---|
| Forced `tool_choice` returns 400 | Sonnet 5.5 | Move the three forced-tool producers to `tool_choice: auto` with strict tool use, or to structured outputs. Then re-measure each producer. Haiku 5.5 accepts a forced tool |
| `temperature` / `top_p` / `top_k` return 400 at any non-default value | Haiku 5.5 (new), Sonnet 5.5 (same as Sonnet 5) | Use dialect `anthropic_adaptive` for Haiku 5.5, which accepts no sampler. Remove or gate the compressor's call-site `temperature=0.2` before any Anthropic model binds `compressor` |
| `thinking: {type: enabled, budget_tokens}` returns 400 | Haiku 5.5 | Do not use dialect `anthropic_budget` for Haiku 5.5. litellm's legacy budget path, which also rewrites `max_tokens` (FRE-1430 F5), must not reach it |
| Assistant prefill returns 400 | Haiku 5.5 | Check that no Haiku-reachable path ends `messages` with an assistant turn |
| Adaptive thinking is on by default, and thinking counts toward `max_tokens` | Haiku 5.5 | An empty mode now means effort `medium` with thinking, not "thinking off". Declare the effort explicitly. Review `default_timeout: 30` on `claude_haiku` |
| About 30% more tokens per text, and a price step above 100K prompt tokens | Haiku 5.5 | Cost estimates measured on Haiku 4.5 do not transfer. The litellm map carries `*_above_100k_tokens` rates. Verify that `completion_cost` applies them before a long-context role binds Haiku 5.5 |
| Function calling on Chat Completions only at effort `none` | gpt-6-luna | A tool-using mode above `none` needs the Responses API. litellm dispatches `openai/` models to Chat Completions. A `deep` mode is safe only on producers that send no tools. The cloud extraction call passes no `tools` (`entity_extraction.py:1167-1174`), and neither does the compressor call (`context_compressor.py:252-262`) |
| `temperature` / `top_p` only at effort `none` | gpt-6-luna | The `openai_gpt5` rule at `models.py:928-937` already enforces exactly this |
| `max` effort exists | gpt-6-luna, Haiku 5.5, Sonnet 5.5 | Widen the `ModeSpec` literals: `reasoning_effort` gets `max`, and `effort` gets `xhigh` and `max`. This is needed only if FRE-1620 declares those levels |
| Effort levels are recalibrated | Sonnet 5.5 | ADR-0145 D6's measured `worker: effort: low` for Sonnet 5 does not transfer. Run a new effort sweep |
| `prompt_cache_options.ttl` replaces `prompt_cache_retention` | gpt-6 family | The repo sets neither (no match in `src/`), so no change is needed |

## 7. Recommendation (scope 4)

### 7.1 Per catalog entry

| Entry | Replacement | Reason | Priority |
|---|---|---|---|
| `claude_sonnet` price | Set $2 / $10 now | The current value is 50% high (§3) | **First (ticket A)** |
| `gpt-5.4-mini` | `gpt-6-luna`, as a **new key** | 7.5x cheaper input and 9x cheaper output, a 1M window, and effort `none` keeps the temperature pin. It is the official replacement for the sibling `gpt-5.4-nano`. `gpt-5.4-mini` itself is not deprecated, so there is no deadline | After the litellm upgrade |
| `claude_haiku` | `claude-haiku-5-5`, as a **new key** | 10x cheaper below 100K prompt tokens, effort support, a 1M window. Haiku 4.5 is past its earliest retirement date | After the litellm upgrade |
| `claude_sonnet` | `claude-sonnet-5-5`, later | Same price, with cheaper cache reads ($0.10). It breaks three producers until the forced-tool change, and its effort levels are recalibrated. Sonnet 5 runs until 2027-06-30 at the earliest | After the forced-tool code change and the measurements in §7.2 |
| `qwen3.8-27b-ovh`, `embedding`, `reranker` | No change | Prices are correct, and no replacement was in scope | n/a |

`gpt-6.1-sol` was considered as a Sonnet alternative at the same $2 / $10. It is rejected for now
because tool calling needs the Responses API, and it has no `none` effort. Every Sonnet producer
either forces a tool or runs as a tool-capable primary.

**Why new keys and not an `id` swap.** A new key keeps three things intact:
- The code that names `claude_haiku` and `claude_sonnet` (§5).
- The stored selections in `session_model_selections`.
- The judge panel, so past runs stay comparable.

The bindings then move one at a time, each with its evidence. This follows the standing rule that a
catalog change and a binding change ship separately (`reference_model_catalog_and_params` memory,
"The catalog and the bindings are two different things"). The old keys are retired after the last
binding moves. A new key appears in the PWA primary picker at once, because `primary` is
`open: true`. That is a visible change, but it is not a binding change.

### 7.2 Per role binding: the evidence a swap needs

All of these are eval runs. Under the FRE-1573 hold, each one needs the owner's go.

| Role | Today | Proposed | Evidence the swap needs |
|---|---|---|---|
| `entity_extraction` | `gpt-5.4-mini` | `gpt-6-luna`, mode `default` (`temperature: 0.0`, `reasoning_effort: none`) | The FRE-630 extraction-quality harness (`scripts/eval/fre630_extraction_quality/`) on the same corpus and bars as the `gpt-5.4-mini` baseline. **Correction to the ticket text:** the "11/16 preregistered bars" figure is `gpt-5.4-mini` on **span extraction** (FRE-1281, `config/model_roles.yaml:105`), not on entity extraction |
| `compressor` | `gpt-5.4-mini` | `gpt-6-luna`, effort `none` | A compression check against the FRE-475 A/B method (`scripts/eval/fre475_compression_ab/`). The call-site `temperature=0.2` stays legal at `none` |
| `span_extraction` | `claude_sonnet` | Stays on Sonnet 5 until Sonnet 5.5 is ready | For Sonnet 5.5: the forced-tool change, then the FRE-1281 corpus (`harness.py --model-key`) against the 15/16 bars that Sonnet 5 met. `gpt-6-luna` is not a candidate here, because the role sits at 15/16 on Sonnet and `gpt-5.4-mini` met 11/16 |
| `entailment` | `claude_sonnet` | Same as above | The forced-tool change, then the FRE-1286 labelled corpus (`scripts/eval/fre1286_entailment/`) for the judge's error rate. ADR-0138's rule applies: the judge must be at least as capable as the primary |
| `session_summary` | `claude_sonnet` | Same as above | The forced-tool change, then the digest contract pilot (`scripts/eval/digest_contract_pilot.py`) and `session_digest_eval.py` |
| `captains_log`, `insights`, `artifact_builder`, `vision` | `claude_sonnet` | Sonnet 5.5 after its effort sweep | An effort sweep on Sonnet 5.5 (ADR-0145 D6 method), because the levels are recalibrated. `vision` also needs an image-turn check: the token count per image changes at high resolution |
| `primary` / `sub_agent` (picker) | local Flash-Next | New keys appear in the picker | Optional selection only. A tool-use probe (FRE-1537 method) is needed before anyone presents Haiku 5.5 or Luna as a recommended primary. Luna at `none` is the only tool-capable Luna mode on Chat Completions |
| Eval judges 2 and 3 | `gpt-5.4-mini`, `claude_haiku` | Keep pinned for the running studies | Re-label the stored replies of one past run with the new panel and report the agreement (§5) before a new panel replaces the old one |

## 8. Reasoning ranges for FRE-1620 (AC-4)

FRE-1620 asks each entry to declare a `fast` mode and a `deep` mode. These are the ranges, and the
modes that the providers' own guidance supports. They are proposals, not measured settings.

| Model | Parameter (catalog field → wire) | Values | Default | `fast` | `deep` | Constraint |
|---|---|---|---|---|---|---|
| `gpt-6-luna` | `reasoning_effort` → `reasoning_effort` | `none`, `low`, `medium`, `high`, `xhigh`, `max` | `medium` | `none` | `high` | Only `none` allows `temperature` and Chat Completions tool calls. A `deep` mode is for tool-free producers. `max` needs the `ModeSpec` literal widened |
| `claude-haiku-5-5` | `effort` → `output_config.effort` (litellm maps `reasoning_effort`) | `low`, `medium`, `high`, `xhigh`, `max` | `medium` | `low` (Anthropic: "the cheapest and fastest level") | `high` (Anthropic: "knowledge work, longer agent tasks, and strict instruction following") | `thinking: disabled` only at `high` or below. `xhigh` and `max` need the literal widened |
| `claude-sonnet-5-5` | `effort` → `output_config.effort` | `low`, `medium`, `high`, `xhigh`, `max` | `high` | `low` or `medium` (Anthropic: chat and latency-sensitive work) | `high` | Levels are recalibrated from Sonnet 5. `between_tools` only at `high` or below |
| `claude-sonnet-5` (kept for now) | `effort` → `output_config.effort` | `low`, `medium`, `high`, `xhigh`, `max` | `high` | `low` (the ADR-0145 D6 measured worker value) | `high` (the current `default`) | The catalog already declares both |
| `gpt-5.4-mini` (kept for now) | `reasoning_effort` | `none`, `low`, `medium`, `high`, `xhigh` | `none` (model page; also FRE-630 `cells.py:142`) | `none` | none available | Any effort above `none` conflicts with the `temperature: 0.0` pin, so this entry has no `deep` mode while the pin stands |

Sources: the gpt-6-luna model page, the OpenAI latest-model guide, the Anthropic effort page, and the
gpt-5.4-mini model page (§4.1 links).

## 9. Proposed catalog diff (not applied)

Ticket A changes only the two Sonnet price lines. Ticket B adds two entries. Neither ticket changes
`config/model_roles.yaml`.

```diff
--- a/config/models.yaml
+++ b/config/models.yaml
@@ claude_sonnet:
-    input_cost_per_token: 0.000003    # $3/MTok (ADR-0101 §8b / FRE-691 — config-owned cost matrix)
-    output_cost_per_token: 0.000015   # $15/MTok
+    # FRE-1621: $2/$10 is the standard price. The $3/$15 rise planned for 2026-09-01 did not
+    # happen (platform.claude.com/docs/en/about-claude/pricing, footnote 3, read 2026-10-20).
+    input_cost_per_token: 0.000002    # $2/MTok (ADR-0101 §8b / FRE-691 — config-owned cost matrix)
+    output_cost_per_token: 0.00001    # $10/MTok
@@ (ticket B, after the litellm upgrade) new entries after gpt-5.4-mini:
+  gpt-6-luna:
+    # OpenAI GPT-6 Luna (FRE-1621). Replacement candidate for gpt-5.4-mini; binds no role yet.
+    # Chat Completions takes temperature, and tool calls, only at reasoning_effort none.
+    id: "gpt-6-luna"
+    provider: "openai"
+    max_tokens: 8192
+    context_length: 1050000
+    min_concurrency: 1
+    max_concurrency: 10
+    default_timeout: 60
+    default_mode: default
+    modes:
+      default:
+        temperature: 0.0
+        reasoning_effort: none
+      worker:
+        reasoning_effort: none
+    input_cost_per_token: 0.0000001    # $0.10/MTok up to 272K (developers.openai.com/api/docs/pricing, 2026-10-20)
+    output_cost_per_token: 0.0000005   # $0.50/MTok
+
+  claude_haiku_5_5:
+    # Anthropic Claude Haiku 5.5 (FRE-1621). Replacement candidate for claude_haiku; binds no role yet.
+    # Rejects temperature/top_p/top_k and budget_tokens thinking, so the dialect is adaptive.
+    id: "claude-haiku-5-5"
+    provider: "anthropic"
+    dialect: anthropic_adaptive
+    max_tokens: 128000        # provider ceiling; policy clamp is settings.artifact_draft_max_tokens
+    context_length: 1000000
+    min_concurrency: 1
+    max_concurrency: 20
+    default_timeout: 60       # adaptive thinking is on by default; Haiku 4.5's 30 s assumed none
+    default_mode: default
+    modes:
+      default:
+        effort: medium        # the provider default, declared rather than inherited
+      worker:
+        effort: low
+    supports_vision: true
+    supports_pdf_document: true
+    input_cost_per_token: 0.0000001    # $0.10/MTok up to 100K prompt tokens (pricing page, 2026-10-20)
+    output_cost_per_token: 0.0000005   # $0.50/MTok; above 100K: $0.50/$2.50 from litellm's map
```

The ticket-B values `max_tokens`, `max_concurrency` and `default_timeout` copy the entries that the
new keys replace, with two exceptions on Haiku:
- `max_tokens` is 128000, the Haiku 5.5 provider ceiling. `claude_haiku` declares 64000, the Haiku
  4.5 ceiling. The artifact clamp `settings.artifact_draft_max_tokens` still applies.
- `default_timeout` is 60, because adaptive thinking is on by default.

All of these are starting values and not measurements.

## 10. Build ticket text (AC-6)

The same text is posted on FRE-1621 for master to file.

### Ticket A: correct the `claude_sonnet` price (config only)

**Why.** `config/models.yaml` records `claude_sonnet` (`claude-sonnet-5`) at $3 / $15 per MTok.
The official price is $2 / $10. The cost gate and the budget lanes record Sonnet spend at about 1.43x
the real spend, and the Sonnet-only caps deny work at about 70% of their nominal value (FRE-1621 note
§3.2).

**Change.**
1. `config/models.yaml`: set `claude_sonnet.input_cost_per_token: 0.000002` and
   `output_cost_per_token: 0.00001`, with the source comment from the FRE-1621 note §9.
2. `tests/personal_agent/config/catalog_snapshot_golden.json`: regenerate it with
   `python -m tests.personal_agent.config.test_catalog_snapshot --write`. Declare the rebaseline in
   that test's docstring. Revert any `slm.*` endpoint that the regenerator copies from a local `.env`.
3. `scripts/eval/fre630_extraction_quality/cells.py:140`: correct `_SONNET_IN, _SONNET_OUT` to
   `2e-06, 1e-05`.
4. `config/models.yaml:475-492`: replace the stale "record reports 0" comment on `qwen3.8-27b-ovh`.
   The OVH record now reports $0.00000047 / $0.00000319, which are the declared values.

**Acceptance criteria.**
- AC-1: after `register_model_pricing(load_model_config())`, the call
  `litellm.cost_per_token(model="anthropic/claude-sonnet-5", prompt_tokens=1_000_000, completion_tokens=1_000_000)`
  returns (2.0, 10.0). A unit test asserts this outcome.
- AC-2: `make test` passes with the regenerated golden. The golden diff changes only
  `input_cost_per_token` and `output_cost_per_token` of `claude_sonnet`. These two values appear in
  five places: the `pricing` entry, and the resolved definitions for `artifact_builder`,
  `captains_log`, `insights` and `vision`.
- AC-3: the PR diff touches only the four places above.
- AC-4: post-deploy, the gateway's `model_pricing_registered` log line lists
  `anthropic/claude-sonnet-5`. The next `api_costs` row for that model satisfies
  `cost_usd ≈ uncached_in×2e-6 + out×1e-5 + cache_read×2e-7 + cache_write×2.5e-6`.

**Deploy class.** Cost and governance: a `seshat-gateway` restart that changes metering. That is the
stricter class, so the owner authorizes it per the trust ladder. No schema change. No model call.

### Ticket B: litellm upgrade and the two new catalog entries (no binding change)

**Why.** litellm 1.98.0 rejects `reasoning_effort` on every gpt-6 model, and its bundled price map
lacks the four new models (FRE-1621 note §6.1). Without the upgrade, a gpt-6 entry loads and boots,
but every call to it fails with `UnsupportedParamsError`. The config guard does not detect this. The
cache rates of the 5.5 models also depend on a network fetch at import.

**Change.**
1. `pyproject.toml` and `uv.lock`: raise `litellm` to a release at or above 1.104.0. Record why that
   release was chosen: its age, and whether its bundled map has `claude-haiku-5-5` (1.106.0 does,
   1.105.1 does not).
2. `config/models.yaml`: add `gpt-6-luna` and `claude_haiku_5_5` as in FRE-1621 note §9. Change no
   existing entry.
3. `tests/personal_agent/config/catalog_snapshot_golden.json`: regenerate it with
   `python -m tests.personal_agent.config.test_catalog_snapshot --write`, and declare the rebaseline
   in the test's docstring. The golden lists every catalog entry in its `concurrency.models` and
   `pricing` sections, so two new entries change it.
4. Unit tests (offline, `LITELLM_LOCAL_MODEL_COST_MAP=True`, no network):
   - `get_optional_params` for `openai/gpt-6-luna` with `reasoning_effort="none", temperature=0.0`
     returns both parameters.
   - The same call with `reasoning_effort="high", temperature=0.0` is refused by the
     `openai_gpt5` mode validator.
   - `anthropic/claude-haiku-5-5` with `reasoning_effort="low"` maps to
     `thinking: {type: adaptive}` and `output_config: {effort: low}`.
   - The registered prices of both new entries equal the official ones.
5. Re-run the litellm-sensitive suites: reasoning, dialect, config guard and catalog snapshot.

**Acceptance criteria.**
- AC-1: the four unit tests above pass on the new litellm with the remote map disabled.
- AC-2: the config guard boots with both new entries. `make test`, `make mypy` and `make ruff-check`
  pass.
- AC-3: `config/model_roles.yaml` is unchanged. No role resolves to a new key.
- AC-4: on the deployed gateway, `role_candidates("primary", …)` (`config/model_loader.py:610`), as
  shown by the PWA model picker, lists `gpt-6-luna` and `claude_haiku_5_5`. No turn selects them
  during verification.

**Deploy class.** `seshat-gateway` rebuild for a dependency upgrade. No schema change. No model call
in the build or in the verification.

**Not in ticket B.** These follow only after the evidence in FRE-1621 note §7.2, and only with the
owner's go under the FRE-1573 hold:
- The forced-tool change for Sonnet 5.5.
- The `ModeSpec` literal widening for FRE-1620.
- Any binding swap.
- Any judge change.

## Appendix A: every reference (AC-3)

Pattern: `gpt-5\.4-mini|claude_sonnet|claude_haiku|claude-sonnet-5|claude-haiku-4-5`. 287 files, 1188 lines. Generated with `git grep -n -o -E` on the branch head.

#### Source code (13 files, 22 lines)

| File | Lines | Names |
|---|---|---|
| `src/personal_agent/captains_log/reflection.py` | 525, 654 | `claude_sonnet` |
| `src/personal_agent/captains_log/reflection_dspy.py` | 349 | `claude_sonnet` |
| `src/personal_agent/config/config_guard.py` | 1317, 1748 | `claude-sonnet-5`, `gpt-5.4-mini` |
| `src/personal_agent/config/model_loader.py` | 725, 732, 733 | `claude_haiku`, `claude_sonnet` |
| `src/personal_agent/config/resolve.py` | 57 | `claude_haiku` |
| `src/personal_agent/llm_client/dspy_gate.py` | 10 | `claude_sonnet` |
| `src/personal_agent/llm_client/factory.py` | 257 | `claude_haiku` |
| `src/personal_agent/llm_client/litellm_client.py` | 858, 1010 | `claude_sonnet`, `gpt-5.4-mini` |
| `src/personal_agent/llm_client/reasoning.py` | 42, 54, 97, 99, 106 | `claude-haiku-4-5`, `claude-sonnet-5`, `gpt-5.4-mini` |
| `src/personal_agent/memory/session_digest_wire.py` | 316 | `claude-sonnet-5` |
| `src/personal_agent/second_brain/entity_extraction.py` | 336 | `gpt-5.4-mini` |
| `src/personal_agent/second_brain/session_summary.py` | 632 | `claude-sonnet-5` |
| `src/personal_agent/ui/service_cli.py` | 166 | `claude_sonnet` |

#### Configuration (4 files, 29 lines)

| File | Lines | Names |
|---|---|---|
| `.env.example` | 481 | `claude_sonnet` |
| `config/model_roles.yaml` | 61, 66, 88, 89, 99, 100, 101, 103, 105, 106, 107, 114, 115, 123, 131 | `claude_sonnet`, `gpt-5.4-mini` |
| `config/models.yaml` | 557, 560, 561, 604, 607, 626, 634, 637, 648, 653, 758 | `claude-haiku-4-5`, `claude-sonnet-5`, `claude_haiku`, `claude_sonnet`, `gpt-5.4-mini` |
| `docker/postgres/migrations/0020_session_model_selections.sql` | 20, 43 | `claude_sonnet` |

#### PWA (6 files, 27 lines)

| File | Lines | Names |
|---|---|---|
| `seshat-pwa/src/__tests__/DecisionCard.test.tsx` | 35, 41, 42, 141, 153 | `claude_haiku`, `claude_sonnet` |
| `seshat-pwa/src/__tests__/ModelPicker.test.tsx` | 28, 29, 50, 55, 69, 83, 92, 93, 100, 108, 114 | `claude-sonnet-5`, `claude_sonnet` |
| `seshat-pwa/src/__tests__/ObserveView.test.tsx` | 36, 53, 73, 101, 140 | `claude_sonnet` |
| `seshat-pwa/src/__tests__/agui-client.attachments.test.ts` | 47, 51 | `claude_sonnet` |
| `seshat-pwa/src/__tests__/useAgentStream.test.tsx` | 203, 210 | `claude_sonnet` |
| `seshat-pwa/src/__tests__/useSessionConfig.test.ts` | 25, 50 | `claude_sonnet` |

#### Eval and other scripts (28 files, 57 lines)

| File | Lines | Names |
|---|---|---|
| `scripts/archive/graphiti_experiment/experiment/config.py` | 28, 29 | `claude-haiku-4-5` |
| `scripts/dispatch/context_probe.py` | 53, 54 | `claude-haiku-4-5`, `claude-sonnet-5` |
| `scripts/eval/digest_contract_pilot.py` | 186 | `claude-sonnet-5` |
| `scripts/eval/fre1281_span_extraction/README.md` | 49, 87, 97, 99, 107 | `claude_sonnet`, `gpt-5.4-mini` |
| `scripts/eval/fre1281_span_extraction/corpus_agreement.json` | 9 | `claude_sonnet` |
| `scripts/eval/fre1286_entailment/README.md` | 72, 99 | `claude_sonnet` |
| `scripts/eval/fre1337_intent_probe/README.md` | 11, 47, 57 | `claude_sonnet` |
| `scripts/eval/fre1337_intent_probe/probe.py` | 40 | `claude_sonnet` |
| `scripts/eval/fre1517/arms.yaml` | 97, 99, 100 | `claude-sonnet-5`, `claude_sonnet` |
| `scripts/eval/fre1537/README.md` | 115, 122, 123, 125, 141 | `claude_sonnet` |
| `scripts/eval/fre1594/spike.py` | 36 | `claude_sonnet` |
| `scripts/eval/fre1598/README.md` | 64 | `claude_haiku`, `claude_sonnet`, `gpt-5.4-mini` |
| `scripts/eval/fre1598/judge.py` | 51 | `claude_haiku`, `claude_sonnet`, `gpt-5.4-mini` |
| `scripts/eval/fre630_extraction_quality/README.md` | 84, 90 | `gpt-5.4-mini` |
| `scripts/eval/fre630_extraction_quality/adr0109_boundary_probe.py` | 7 | `claude-sonnet-5`, `gpt-5.4-mini` |
| `scripts/eval/fre630_extraction_quality/bench.py` | 347 | `gpt-5.4-mini` |
| `scripts/eval/fre630_extraction_quality/cells.py` | 138, 140, 143, 151, 170, 182, 221 | `claude-sonnet-5`, `gpt-5.4-mini` |
| `scripts/eval/fre630_extraction_quality/fre790_phenomenon_domain_boundary_fixture.yaml` | 9, 10 | `claude-sonnet-5`, `gpt-5.4-mini` |
| `scripts/eval/fre630_extraction_quality/harness.py` | 37 | `gpt-5.4-mini` |
| `scripts/eval/fre630_extraction_quality/relabel_v2_rels.py` | 4 | `claude-sonnet-5`, `gpt-5.4-mini` |
| `scripts/eval/fre630_extraction_quality/relabel_v2_types.py` | 3, 144, 146 | `claude-sonnet-5`, `gpt-5.4-mini` |
| `scripts/eval/fre630_extraction_quality/report.py` | 58 | `gpt-5.4-mini` |
| `scripts/eval/fre994_digest_compression_curve/arms.py` | 42 | `gpt-5.4-mini` |
| `scripts/eval/fre994_digest_compression_curve/generate.py` | 136 | `claude-sonnet-5` |
| `scripts/eval/fre994_digest_compression_curve/scoring.py` | 8 | `gpt-5.4-mini` |
| `scripts/migrate_fre865_entity_class_backfill.py` | 448 | `gpt-5.4-mini` |
| `scripts/research/fre802_extraction_cache_probe.py` | 6, 15, 42, 43, 45 | `claude_sonnet`, `gpt-5.4-mini` |
| `scripts/study/eval_artifacts/frozen/ac2_hard_negative_pairs.json` | 6034 | `claude-haiku-4-5` |

#### Tests (93 files, 424 lines)

| File | Lines | Names |
|---|---|---|
| `tests/_helpers/litellm_capability.py` | 6, 35, 44 | `claude-sonnet-5`, `gpt-5.4-mini` |
| `tests/evaluation/test_fre435_model_config_fidelity.py` | 28, 32, 33, 84, 85, 86 | `claude_sonnet`, `gpt-5.4-mini` |
| `tests/evaluation/test_fre630_report.py` | 25, 26, 109, 118 | `gpt-5.4-mini` |
| `tests/fixtures/session_digest/ac12_corrections.json` | 371, 376, 399, 400 | `gpt-5.4-mini` |
| `tests/fixtures/session_digest/build_fixtures.py` | 484, 487, 489, 490 | `gpt-5.4-mini` |
| `tests/migrations/test_0020_session_model_selections_migration.py` | 94, 115 | `claude_sonnet` |
| `tests/personal_agent/config/catalog_snapshot_golden.json` | 5, 9, 17, 83, 89, 95, 115, 169, 215, 223, 269, 302, 348, 356, 402, 596 | `claude-haiku-4-5`, `claude-sonnet-5`, `claude_haiku`, `claude_sonnet`, `gpt-5.4-mini` |
| `tests/personal_agent/config/fixtures/anthropic_budget_exempt/config/model_roles.yaml` | 2 | `claude_haiku` |
| `tests/personal_agent/config/fixtures/anthropic_budget_exempt/config/models.yaml` | 5, 21, 22 | `claude-haiku-4-5`, `claude_haiku` |
| `tests/personal_agent/config/fixtures/anthropic_budget_thinking_on_with_tools/config/model_roles.yaml` | 4, 8 | `claude_haiku` |
| `tests/personal_agent/config/fixtures/anthropic_budget_thinking_on_with_tools/config/models.yaml` | 10, 11 | `claude-haiku-4-5`, `claude_haiku` |
| `tests/personal_agent/config/fixtures/inherit_binding_reasoning/config/model_roles.yaml` | 8 | `claude_sonnet` |
| `tests/personal_agent/config/fixtures/inherit_binding_reasoning/config/models.yaml` | 6, 7 | `claude-sonnet-5`, `claude_sonnet` |
| `tests/personal_agent/config/fixtures/inherit_binding_unresolvable/config/models.yaml` | 6, 7 | `claude-sonnet-5`, `claude_sonnet` |
| `tests/personal_agent/config/fixtures/malformed_binding_shape/config/model_roles.yaml` | 7 | `gpt-5.4-mini` |
| `tests/personal_agent/config/fixtures/reasoning_ineffective/config/model_roles.yaml` | 3, 7 | `claude_sonnet` |
| `tests/personal_agent/config/fixtures/reasoning_ineffective/config/models.yaml` | 9, 10 | `claude-sonnet-5`, `claude_sonnet` |
| `tests/personal_agent/config/fixtures/reasoning_rejected/config/model_roles.yaml` | 4, 8 | `gpt-5.4-mini` |
| `tests/personal_agent/config/fixtures/reasoning_rejected/config/models.yaml` | 9, 10 | `gpt-5.4-mini` |
| `tests/personal_agent/config/fixtures/reasoning_undeclared/config/model_roles.yaml` | 3, 7 | `claude_sonnet` |
| `tests/personal_agent/config/fixtures/reasoning_undeclared/config/models.yaml` | 9, 10 | `claude-sonnet-5`, `claude_sonnet` |
| `tests/personal_agent/config/fixtures/role_model_key_inherit/config/models.yaml` | 8, 9, 19 | `claude-sonnet-5`, `claude_sonnet` |
| `tests/personal_agent/config/fixtures/thinking_disable_tool_model/config/model_roles.yaml` | 3, 7 | `claude_sonnet` |
| `tests/personal_agent/config/fixtures/thinking_disable_tool_model/config/models.yaml` | 9, 10 | `claude-sonnet-5`, `claude_sonnet` |
| `tests/personal_agent/config/test_catalog_snapshot.py` | 75, 78, 86, 95, 114, 118, 150, 158, 159, 161, 164 | `claude_haiku`, `claude_sonnet`, `gpt-5.4-mini` |
| `tests/personal_agent/config/test_config_guard_secret_leaks.py` | 48 | `claude-sonnet-5` |
| `tests/personal_agent/config/test_inherit_deployment_resolution.py` | 227 | `claude_sonnet` |
| `tests/personal_agent/config/test_local_primary_generation_bound.py` | 49, 50, 51 | `claude_haiku`, `claude_sonnet`, `gpt-5.4-mini` |
| `tests/personal_agent/config/test_model_loader_roles.py` | 48, 49, 50, 51, 65, 68, 73, 81, 105, 107 | `claude_sonnet`, `gpt-5.4-mini` |
| `tests/personal_agent/config/test_reasoning_declaration_guard.py` | 12, 65, 76, 85, 94, 105, 205, 311, 312, 313, 379 | `claude-sonnet-5`, `claude_haiku`, `claude_sonnet`, `gpt-5.4-mini` |
| `tests/personal_agent/config/test_resolve_cli.py` | 3, 30, 42, 44, 46, 48, 68, 76 | `claude_haiku`, `claude_sonnet`, `gpt-5.4-mini` |
| `tests/personal_agent/config/test_role_candidates.py` | 40, 41, 42, 126, 135, 145 | `claude-haiku-4-5`, `claude-sonnet-5`, `claude_haiku`, `claude_sonnet`, `gpt-5.4-mini` |
| `tests/personal_agent/config/test_role_resolution_golden.py` | 59 | `gpt-5.4-mini` |
| `tests/personal_agent/config/test_selection_context.py` | 35, 39, 45, 46 | `claude_haiku`, `claude_sonnet` |
| `tests/personal_agent/config/test_selection_resolver.py` | 29, 61, 69, 70, 98, 110, 111, 156 | `claude-sonnet-5`, `claude_sonnet`, `gpt-5.4-mini` |
| `tests/personal_agent/cost_gate/test_role_lane_isolation.py` | 201, 225, 262, 294, 325 | `claude-haiku-4-5`, `gpt-5.4-mini` |
| `tests/personal_agent/cost_gate/test_silence_monitor.py` | 221, 241, 276, 315 | `claude_sonnet` |
| `tests/personal_agent/gateway/test_session_api.py` | 505, 513, 548, 562, 570, 613, 618, 648, 656, 672, 714, 813, 857, 882, 921, 933, 961 | `claude_haiku`, `claude_sonnet`, `gpt-5.4-mini` |
| `tests/personal_agent/grounding/test_entailment_sampling.py` | 180, 192 | `claude_sonnet` |
| `tests/personal_agent/llm_client/test_cost_by_model.py` | 41, 49 | `claude-haiku-4-5` |
| `tests/personal_agent/llm_client/test_cost_tracker_identity.py` | 85, 105 | `gpt-5.4-mini` |
| `tests/personal_agent/llm_client/test_cost_tracker_turn_observed.py` | 80 | `gpt-5.4-mini` |
| `tests/personal_agent/llm_client/test_dialect_modes.py` | 325, 326, 327, 330, 332, 336, 337, 456, 457, 458 | `claude_haiku`, `claude_sonnet`, `gpt-5.4-mini` |
| `tests/personal_agent/llm_client/test_dialect_parameter_block.py` | 196, 232, 254, 278, 298, 316, 333, 356, 357, 363, 364, 371, 382, 389 | `claude-sonnet-5`, `claude_haiku`, `claude_sonnet`, `gpt-5.4-mini` |
| `tests/personal_agent/llm_client/test_dspy_gate.py` | 6 | `claude_sonnet` |
| `tests/personal_agent/llm_client/test_litellm_gate_wiring.py` | 355 | `claude_haiku` |
| `tests/personal_agent/llm_client/test_litellm_trailing_role_guard.py` | 68, 137 | `claude-sonnet-5` |
| `tests/personal_agent/llm_client/test_pricing.py` | 33, 120, 141 | `claude_haiku`, `claude_sonnet`, `gpt-5.4-mini` |
| `tests/personal_agent/llm_client/test_reasoning_effort.py` | 94 | `gpt-5.4-mini` |
| `tests/personal_agent/llm_client/test_reasoning_effort_reaches_provider.py` | 7, 8, 115, 121, 123, 136, 156, 159 | `claude-haiku-4-5`, `claude-sonnet-5`, `claude_sonnet`, `gpt-5.4-mini` |
| `tests/personal_agent/llm_client/test_return_progress_wire.py` | 54, 146, 147 | `claude-sonnet-5`, `claude_sonnet`, `gpt-5.4-mini` |
| `tests/personal_agent/llm_client/test_worker_mode_dispatch_per_dialect.py` | 36, 190, 235, 245, 253 | `claude_haiku`, `claude_sonnet`, `gpt-5.4-mini` |
| `tests/personal_agent/memory/test_session_digest_wire.py` | 273 | `claude-sonnet-5` |
| `tests/personal_agent/orchestrator/test_artifact_builder_planning_note_injection.py` | 19 | `claude_haiku` |
| `tests/personal_agent/orchestrator/test_artifact_builder_turn_start.py` | 103, 123, 124, 131, 145, 147, 219, 249, 257 | `claude_haiku`, `claude_sonnet`, `gpt-5.4-mini` |
| `tests/personal_agent/orchestrator/test_attachment_cost_gate.py` | 76, 93, 254, 393 | `claude_sonnet` |
| `tests/personal_agent/orchestrator/test_constraint_options_computed.py` | 300, 304, 311 | `claude_sonnet` |
| `tests/personal_agent/orchestrator/test_content_widening.py` | 360, 409, 526, 556 | `claude_sonnet` |
| `tests/personal_agent/orchestrator/test_fre1360_untrusted_input_channel.py` | 669 | `claude-sonnet-5` |
| `tests/personal_agent/orchestrator/test_fre972_context_window_selection.py` | 6, 20, 24, 68, 69, 130, 211 | `claude_sonnet`, `gpt-5.4-mini` |
| `tests/personal_agent/orchestrator/test_local_generation_bound.py` | 365, 386 | `claude_sonnet`, `gpt-5.4-mini` |
| `tests/personal_agent/orchestrator/test_planner_response_format.py` | 1, 235, 238, 249, 257, 280, 287, 304, 305, 315, 366, 379 | `claude-sonnet-5`, `claude_sonnet` |
| `tests/personal_agent/request_gateway/test_pipeline.py` | 551, 552, 560, 566 | `claude_haiku`, `claude_sonnet`, `gpt-5.4-mini` |
| `tests/personal_agent/second_brain/test_session_summary_contract.py` | 177, 199, 217, 230, 249, 275, 302 | `claude_sonnet` |
| `tests/personal_agent/service/repositories/test_session_model_selection_repository.py` | 110, 111, 136, 138, 141, 142 | `claude_haiku`, `claude_sonnet` |
| `tests/personal_agent/service/repositories/test_session_repository_retention.py` | 159 | `claude-sonnet-5` |
| `tests/personal_agent/service/test_chat_trace_identity.py` | 207, 226, 267, 280 | `claude-haiku-4-5` |
| `tests/personal_agent/tools/test_artifact_tools.py` | 1080, 1141, 1157, 1158, 1201, 1240, 1254, 1284, 1319, 1323, 1333, 1334, 1351, 1376, 1390, 1480, 1653, 1710, 1717, 1740, 1748, 1778, 1791, 1802, 1807 | `claude_haiku`, `claude_sonnet`, `gpt-5.4-mini` |
| `tests/personal_agent/tools/test_telemetry_query_tempo.py` | 447 | `claude-sonnet-5` |
| `tests/scripts/study/test_categorizer.py` | 163, 165, 169 | `claude-sonnet-5`, `claude_sonnet` |
| `tests/scripts/test_gating_watcher.py` | 1790, 1796 | `claude-haiku-4-5`, `claude-sonnet-5` |
| `tests/scripts/test_migrate_fre772.py` | 392, 398, 399 | `gpt-5.4-mini` |
| `tests/test_captains_log/fixtures/fre1608_traces.json` | 1 | `claude_haiku`, `claude_sonnet`, `gpt-5.4-mini` |
| `tests/test_captains_log/test_feedback_loop.py` | 144, 151, 165, 172 | `claude_sonnet` |
| `tests/test_captains_log/test_reflection_dspy_gated.py` | 3, 63, 69, 80 | `claude_sonnet` |
| `tests/test_captains_log/test_reflection_manual_fallback_role.py` | 5, 32, 49 | `claude_sonnet` |
| `tests/test_captains_log/test_reflection_missing_skills.py` | 172, 189 | `claude_sonnet` |
| `tests/test_config/test_model_loader.py` | 422, 423, 433, 453, 463, 494, 522, 531, 540, 676 | `claude_haiku`, `claude_sonnet`, `gpt-5.4-mini` |
| `tests/test_eval/test_fre1537_cloud.py` | 30, 178 | `claude-sonnet-5`, `claude_sonnet` |
| `tests/test_eval/test_fre1598.py` | 372, 374, 376 | `claude-sonnet-5`, `claude_haiku`, `claude_sonnet` |
| `tests/test_llm_client/test_dspy_adapter.py` | 310, 311, 316, 317 | `claude-sonnet-5`, `claude_sonnet`, `gpt-5.4-mini` |
| `tests/test_llm_client/test_factory_artifact_builder.py` | 7, 22, 31 | `claude_sonnet` |
| `tests/test_llm_client/test_factory_selection.py` | 39, 40, 44, 48, 53, 78, 82, 83, 87, 92 | `claude_sonnet`, `gpt-5.4-mini` |
| `tests/test_llm_client/test_factory_sub_agent_inherit.py` | 76, 119, 127 | `claude-sonnet-5`, `claude_sonnet` |
| `tests/test_orchestrator/test_attachment_cost_confirmation.py` | 51, 176, 179, 185, 188 | `claude_sonnet` |
| `tests/test_orchestrator/test_executor.py` | 1010, 1011, 1018, 1026, 1038, 1056, 2308, 2317, 2349, 2478, 2517, 2523 | `claude_sonnet` |
| `tests/test_orchestrator/test_no_think_gating.py` | 41 | `claude_sonnet` |
| `tests/test_orchestrator/test_routing.py` | 283, 285, 286, 298, 299, 300, 307, 308, 381, 400, 403, 408, 411, 417, 421, 426, 438, 448, 452 | `claude_sonnet` |
| `tests/test_orchestrator/test_turn_status_context_max.py` | 18 | `claude_sonnet` |
| `tests/test_second_brain/test_entity_extraction_child_trace.py` | 56, 63, 67, 104, 111, 115, 146, 153, 157 | `gpt-5.4-mini` |
| `tests/test_second_brain/test_entity_extraction_contract.py` | 796, 803, 807, 822, 955, 962, 966 | `gpt-5.4-mini` |
| `tests/test_service/test_session_selection_resolution.py` | 12, 28 | `claude_sonnet` |
| `tests/test_telemetry/test_spans.py` | 161, 168 | `claude-sonnet-5` |

#### Documentation (history; no change needed) (127 files, 475 lines)

| File | Lines | Names |
|---|---|---|
| `docs/architecture_decisions/ADR-0031-model-config-consolidation.md` | 42, 48, 49, 107 | `claude_sonnet` |
| `docs/architecture_decisions/ADR-0033-multi-provider-model-taxonomy.md` | 184 | `claude_sonnet` |
| `docs/architecture_decisions/ADR-0035-seshat-backend-decision.md` | 40 | `claude-haiku-4-5` |
| `docs/architecture_decisions/ADR-0044-provider-abstraction-dual-harness.md` | 104 | `claude-haiku-4-5` |
| `docs/architecture_decisions/ADR-0051-cloud-profile-orchestrator-dispatch.md` | 51, 52 | `claude_sonnet` |
| `docs/architecture_decisions/ADR-0063-primitive-tools-action-boundary-governance.md` | 145 | `claude_sonnet` |
| `docs/architecture_decisions/ADR-0066-skill-routing-defaults-and-feedback-loop.md` | 26, 29, 170 | `claude_haiku` |
| `docs/architecture_decisions/ADR-0071-two-source-one-gate-memory-model.md` | 18, 23, 347 | `claude_haiku`, `claude_sonnet`, `gpt-5.4-mini` |
| `docs/architecture_decisions/ADR-0073-cross-fact-constraint-layer.md` | 124, 139 | `gpt-5.4-mini` |
| `docs/architecture_decisions/ADR-0076-adaptive-constraint-governance.md` | 15, 17 | `claude_sonnet` |
| `docs/architecture_decisions/ADR-0077-artifact-draft-subagent-generation.md` | 22 | `claude_haiku` |
| `docs/architecture_decisions/ADR-0080-thinking-control-policy.md` | 25 | `claude_sonnet` |
| `docs/architecture_decisions/ADR-0081-cache-aware-context-layout-and-compaction.md` | 239 | `claude_haiku` |
| `docs/architecture_decisions/ADR-0094-deterministic-local-cloud-execution-profile-routing.md` | 69 | `claude_sonnet` |
| `docs/architecture_decisions/ADR-0099-configuration-management-and-validation.md` | 43, 44, 45, 46, 78, 79, 82, 83, 84, 85, 225, 233, 237, 243, 269, 273 | `claude_haiku`, `claude_sonnet`, `gpt-5.4-mini` |
| `docs/architecture_decisions/ADR-0101-agent-vision-ingestion.md` | 78, 160, 167, 315, 500, 501 | `claude_haiku`, `claude_sonnet` |
| `docs/architecture_decisions/ADR-0102-document-ingestion.md` | 130, 412 | `claude_haiku`, `claude_sonnet` |
| `docs/architecture_decisions/ADR-0109-entity-taxonomy-redesign.md` | 40, 50, 128 | `claude-sonnet-5`, `gpt-5.4-mini` |
| `docs/architecture_decisions/ADR-0118-artifact-builder-model-selection.md` | 59, 132, 260, 358 | `claude_haiku`, `gpt-5.4-mini` |
| `docs/architecture_decisions/ADR-0119-config-management-interface.md` | 70, 275 | `claude_haiku` |
| `docs/architecture_decisions/ADR-0121-model-catalog-and-selection-layer.md` | 23, 24, 25, 40, 150, 156, 170, 171, 636, 742, 746, 764, 846, 874, 927, 928, 929, 934, 935, 942, 952, 1164, 1167, 1169, 1177, 1212 | `claude-haiku-4-5`, `claude-sonnet-5`, `claude_haiku`, `claude_sonnet`, `gpt-5.4-mini` |
| `docs/architecture_decisions/ADR-0122-build-time-artifact-builder-selection.md` | 48, 100, 101, 102, 595, 750, 754, 756, 824, 863, 864, 887 | `claude_haiku`, `claude_sonnet`, `gpt-5.4-mini` |
| `docs/architecture_decisions/ADR-0124-session-summary-producer-and-phased-consumption.md` | 33, 184, 191, 192, 1228 | `claude-sonnet-5`, `claude_sonnet`, `gpt-5.4-mini` |
| `docs/architecture_decisions/ADR-0145-two-nouns-and-the-dialect-between-them.md` | 124, 217, 218, 219, 261, 262, 263, 284, 742 | `claude-sonnet-5`, `claude_haiku`, `claude_sonnet`, `gpt-5.4-mini` |
| `docs/architecture_decisions/ADR-0154-what-the-planner-sees-and-what-it-decides.md` | 247, 361 | `claude_sonnet` |
| `docs/guides/CLOUD_DEPLOYMENT.md` | 440, 441, 447, 456, 457 | `claude_haiku`, `claude_sonnet` |
| `docs/guides/SCHEMA_REFERENCE.md` | 432, 433, 449, 450, 456, 535, 551 | `claude_haiku`, `claude_sonnet` |
| `docs/plans/completed/2026-03-24-foundation-model-baseline.md` | 444, 446, 447, 448, 481 | `claude_sonnet` |
| `docs/plans/completed/2026-03-25-multi-provider-taxonomy.md` | 173, 188, 189, 190, 261, 350, 351, 352, 376, 389, 826, 828, 1039 | `claude_sonnet` |
| `docs/plans/completed/2026-03-28-eval-02-graphiti-experiment.md` | 138, 139, 2016 | `claude-haiku-4-5`, `claude_sonnet` |
| `docs/plans/completed/2026-05-10-completed-archive.md` | 31 | `claude_haiku` |
| `docs/plans/completed/2026-05-22-completed-archive.md` | 12 | `claude_sonnet`, `gpt-5.4-mini` |
| `docs/plans/completed/2026-07-04-master-plan-archive.md` | 24, 26 | `claude-sonnet-5`, `claude_sonnet`, `gpt-5.4-mini` |
| `docs/plans/sessions/SESSION-2026-04-15-cloud-profile-wiring.md` | 54, 71, 122, 181, 182 | `claude_haiku`, `claude_sonnet` |
| `docs/reference/CONFIG_INVENTORY.md` | 37, 590, 591, 592, 613, 614, 616, 618, 637, 638, 642, 675, 704 | `claude-haiku-4-5`, `claude-sonnet-5`, `claude_haiku`, `claude_sonnet`, `gpt-5.4-mini` |
| `docs/research/2026-05-21-memory-integration-probe-report.md` | 63, 66, 97, 311, 322 | `gpt-5.4-mini` |
| `docs/research/2026-06-27-memory-recall-quality.md` | 127, 132 | `gpt-5.4-mini` |
| `docs/research/2026-07-03-er-extraction-opinion-vs-pipeline.md` | 59, 108 | `claude-sonnet-5`, `gpt-5.4-mini` |
| `docs/research/2026-07-03-fre-630-extraction-quality-sota.md` | 23, 27, 51, 144, 146, 168, 194, 229, 232, 251, 319 | `claude-sonnet-5`, `gpt-5.4-mini` |
| `docs/research/2026-07-04-fre-770-gold-relabel-iaa.md` | 11, 90 | `claude-sonnet-5`, `gpt-5.4-mini` |
| `docs/research/2026-07-04-fre-771-10type-prompt-swap-powered-ab.md` | 17, 18, 109 | `claude-sonnet-5`, `gpt-5.4-mini` |
| `docs/research/2026-07-04-fre-773-relationship-v2-validation.md` | 42, 43 | `claude-sonnet-5`, `gpt-5.4-mini` |
| `docs/research/2026-07-04-fre-782-knowledgeartifact-quantitymeasure-boundary-iaa.md` | 20 | `claude-sonnet-5`, `gpt-5.4-mini` |
| `docs/research/2026-07-05-fre-790-phenomenon-domain-boundary-iaa.md` | 28, 56, 119 | `claude-sonnet-5`, `gpt-5.4-mini` |
| `docs/research/2026-07-05-fre-797-phenomenon-domain-sharpening.md` | 13 | `claude-sonnet-5`, `gpt-5.4-mini` |
| `docs/research/2026-07-05-fre-802-extraction-prompt-cache.md` | 27, 28, 29, 30, 62, 155, 156 | `claude_sonnet`, `gpt-5.4-mini` |
| `docs/research/2026-07-16-cost-governance-rework-adr-0065.md` | 65 | `gpt-5.4-mini` |
| `docs/research/2026-07-22-session-summary-kg-opportunity.md` | 31, 32, 33, 62, 64, 258, 267, 268, 273 | `claude_sonnet`, `gpt-5.4-mini` |
| `docs/research/2026-07-23-fre-947-session-digest-eval.md` | 4 | `claude_sonnet` |
| `docs/research/2026-07-23-fre-953-session-digest-eval.md` | 4 | `claude_sonnet` |
| `docs/research/2026-07-27-fre994-digest-compression-curve.md` | 45, 53, 319, 324 | `claude-sonnet-5` |
| `docs/research/2026-07-29-fre-989-cost-attribution-audit.md` | 158, 180 | `claude-sonnet-5`, `claude_sonnet` |
| `docs/research/2026-08-28-fre-1013-entity-class-fail-open-rate-measured.md` | 38 | `gpt-5.4-mini` |
| `docs/research/2026-09-04-fre-1377-router-design-review.md` | 690, 709, 745, 754, 770 | `claude_sonnet` |
| `docs/research/2026-09-06-fre-1421-model-management-review.md` | 296, 377, 378, 379, 393, 521 | `claude_haiku`, `claude_sonnet`, `gpt-5.4-mini` |
| `docs/research/2026-09-06-fre-1430-provider-dialect-matrix.md` | 25, 85, 91, 230, 244, 275, 280, 329, 331, 332, 333, 492, 504, 521, 574 | `claude-haiku-4-5`, `claude-sonnet-5`, `claude_haiku`, `claude_sonnet`, `gpt-5.4-mini` |
| `docs/research/2026-09-15-fre-1517-primary-model-long-sessions.md` | 312 | `claude-sonnet-5` |
| `docs/research/2026-10-10-fre-1517-stage-4-gemma-spot-retest.md` | 184 | `claude-sonnet-5` |
| `docs/research/2026-10-11-fre-1581-adr-0156-gate-run.md` | 242 | `gpt-5.4-mini` |
| `docs/research/2026-10-12-fre-1594-reveal-spike.md` | 22, 50 | `claude-sonnet-5`, `claude_sonnet` |
| `docs/research/2026-10-13-fre-1598-benefit-run.md` | 33, 34 | `claude-haiku-4-5`, `claude-sonnet-5`, `claude_haiku`, `claude_sonnet`, `gpt-5.4-mini` |
| `docs/research/2026-10-18-fre-1615-block1-planner-arms.md` | 22 | `gpt-5.4-mini` |
| `docs/research/EVALUATION_PHASE_FINDINGS.md` | 302, 322 | `claude-haiku-4-5` |
| `docs/research/FRE-254-step-count-investigation.md` | 183 | `claude_haiku`, `claude_sonnet` |
| `docs/research/GRAPHITI_EXPERIMENT_REPORT.md` | 17, 38, 157, 179 | `claude-haiku-4-5` |
| `docs/research/digest_contract_pilot_2026-07-26.md` | 255, 276 | `claude-sonnet-5` |
| `docs/research/modeled_output_contract_audit_2026-07-26.md` | 306, 307, 308 | `claude-sonnet-5`, `gpt-5.4-mini` |
| `docs/superpowers/plans/2026-05-05-agent-self-diagnosis-recovery-execution-waves-0-2.md` | 246 | `claude-haiku-4-5` |
| `docs/superpowers/plans/2026-05-08-fre-214-3-test-parity.md` | 574 | `claude_sonnet` |
| `docs/superpowers/plans/2026-05-16-fre-365-cache-diagnostic.md` | 34, 46, 55, 62, 69, 109, 110, 111, 112 | `claude-haiku-4-5`, `claude_sonnet`, `gpt-5.4-mini` |
| `docs/superpowers/plans/2026-05-22-fre-374-cross-fact-constraint-layer.md` | 146, 161 | `gpt-5.4-mini` |
| `docs/superpowers/plans/2026-07-01-fre-665-vision-capability-routing.md` | 26, 60, 61, 67, 68, 78, 87, 154, 287 | `claude_haiku`, `claude_sonnet`, `gpt-5.4-mini` |
| `docs/superpowers/plans/2026-07-02-fre-691-cloud-image-cost.md` | 57, 58, 189 | `claude_haiku`, `claude_sonnet` |
| `docs/superpowers/plans/2026-07-02-fre-734-vision-config-parity-fix.md` | 28, 34, 55, 56 | `claude_haiku`, `claude_sonnet` |
| `docs/superpowers/plans/2026-07-03-fre-630-extraction-quality-benchmark.md` | 23, 137 | `gpt-5.4-mini` |
| `docs/superpowers/plans/2026-07-03-fre-758-temp-pin-ab.md` | 14, 15, 23, 25, 32, 36, 40, 43, 48, 81, 82, 83, 152, 156 | `gpt-5.4-mini` |
| `docs/superpowers/plans/2026-07-03-fre-759-type-claim-prompt-ab.md` | 4, 22, 103 | `gpt-5.4-mini` |
| `docs/superpowers/plans/2026-07-03-fre-766-model-reasoning-benchmark.md` | 22, 23, 26, 27, 43 | `claude-sonnet-5`, `claude_sonnet`, `gpt-5.4-mini` |
| `docs/superpowers/plans/2026-07-04-fre-649-config-guard.md` | 7, 8, 14, 41, 52, 60 | `claude_sonnet`, `gpt-5.4-mini` |
| `docs/superpowers/plans/2026-07-04-fre-650-adr-0099-stage-2-generative-loader.md` | 9, 11, 13, 18, 55, 90 | `claude_sonnet`, `gpt-5.4-mini` |
| `docs/superpowers/plans/2026-07-04-fre-651-adr-0099-stage-3-deployment-provenance.md` | 15, 77, 78, 87 | `gpt-5.4-mini` |
| `docs/superpowers/plans/2026-07-04-fre-652-adr-0099-stage-4-consolidation.md` | 14, 26, 42, 52 | `claude-sonnet-5`, `claude_sonnet`, `gpt-5.4-mini` |
| `docs/superpowers/plans/2026-07-04-fre-770-gold-relabel-iaa.md` | 28, 100 | `claude-sonnet-5`, `gpt-5.4-mini` |
| `docs/superpowers/plans/2026-07-04-fre-773-relationship-v2-validation.md` | 56 | `claude-sonnet-5`, `gpt-5.4-mini` |
| `docs/superpowers/plans/2026-07-05-fre-801-batch-concept-classifier.md` | 7 | `gpt-5.4-mini` |
| `docs/superpowers/plans/2026-07-12-fre-682-document-capability-flag-cloud-pricing.md` | 7, 8, 10, 25, 36, 41, 42 | `claude_haiku`, `claude_sonnet`, `gpt-5.4-mini` |
| `docs/superpowers/plans/2026-07-12-fre-865-entity-class-backfill.md` | 256 | `gpt-5.4-mini` |
| `docs/superpowers/plans/2026-07-12-fre-869-cost-mis-attribution-entity-extraction-billing.md` | 15, 87, 90, 95, 97, 146 | `claude_sonnet`, `gpt-5.4-mini` |
| `docs/superpowers/plans/2026-07-14-fre-686-preflight-document-cost-estimate.md` | 134 | `claude_sonnet` |
| `docs/superpowers/plans/2026-07-15-fre-886-attachment-default-cloud.md` | 38, 150, 151, 154, 258 | `claude_sonnet` |
| `docs/superpowers/plans/2026-07-17-fre-879-artifact-builder-role-corrected.md` | 10, 28, 44, 96, 100, 117 | `claude_haiku` |
| `docs/superpowers/plans/2026-07-19-fre-916-adr-0121-t1-catalog-providers.md` | 72, 73, 74, 80, 181, 186, 187, 214 | `claude_haiku`, `claude_sonnet`, `gpt-5.4-mini` |
| `docs/superpowers/plans/2026-07-19-fre-917-adr-0121-t2-selection-store.md` | 71, 75, 181 | `claude_haiku`, `claude_sonnet` |
| `docs/superpowers/plans/2026-07-20-fre-918-config-read-api.md` | 34, 193, 198, 199 | `claude_haiku`, `claude_sonnet`, `gpt-5.4-mini` |
| `docs/superpowers/plans/2026-07-20-fre-919-adr-0121-t4-telemetry-migration.md` | 75 | `claude-sonnet-5` |
| `docs/superpowers/plans/2026-07-20-fre-920-adr-0121-t5-path-removed.md` | 80, 82, 88, 94, 95, 122, 126 | `claude_haiku`, `claude_sonnet` |
| `docs/superpowers/plans/2026-07-21-fre-921-pwa-card-catalog-detail.md` | 109, 111, 117 | `claude_haiku` |
| `docs/superpowers/plans/2026-07-21-fre-930-adr-0122-t5-builder-turn-start.md` | 151, 153 | `claude_sonnet` |
| `docs/superpowers/plans/2026-07-23-fre-947-adr-0124-phase-0-producer.md` | 56, 92 | `claude_sonnet` |
| `docs/superpowers/plans/2026-07-24-fre-974-managed-embedding-reranker-cost.md` | 21 | `claude_haiku`, `claude_sonnet`, `gpt-5.4-mini` |
| `docs/superpowers/plans/2026-07-26-fre-994-digest-compression-curve.md` | 154, 161, 238, 268 | `claude-sonnet-5`, `gpt-5.4-mini` |
| `docs/superpowers/plans/2026-07-26-fre-996-digest-json-contract.md` | 21, 22, 219 | `claude-sonnet-5` |
| `docs/superpowers/plans/2026-07-28-fre-1037-widen-role-enum-fail-closed.md` | 36, 42, 224, 226 | `claude-sonnet-5`, `claude_sonnet` |
| `docs/superpowers/plans/2026-07-28-fre-989-cost-attribution-audit.md` | 25 | `claude_sonnet` |
| `docs/superpowers/plans/2026-08-01-fre-1117-main-inference-silence-detector.md` | 140 | `claude_sonnet` |
| `docs/superpowers/plans/2026-08-23-fre-1007-mandatory-reasoning-declaration.md` | 11, 19, 22, 40, 41, 42, 47, 48, 53, 60, 88, 89, 115, 116, 126, 127, 138, 181, 183, 195, 196 | `claude-haiku-4-5`, `claude-sonnet-5`, `claude_haiku`, `claude_sonnet`, `gpt-5.4-mini` |
| `docs/superpowers/plans/2026-08-23-fre-1290-web-search-tilt.md` | 297 | `claude-haiku-4-5` |
| `docs/superpowers/plans/2026-08-24-fre-1281-span-extractor-and-labelled-corpus.md` | 219 | `gpt-5.4-mini` |
| `docs/superpowers/plans/2026-08-26-fre-1286-entailment-checking.md` | 165 | `claude_sonnet` |
| `docs/superpowers/plans/2026-08-30-fre-1337-intent-classification-eval-harness.md` | 85, 155 | `claude_sonnet` |
| `docs/superpowers/plans/2026-08-31-fre-1341-eval-gateway-freshness.md` | 8 | `claude-sonnet-5` |
| `docs/superpowers/plans/2026-09-06-fre-1440-dialect-parameter-block.md` | 52, 137, 148 | `claude_sonnet`, `gpt-5.4-mini` |
| `docs/superpowers/plans/2026-09-07-fre-1444-role-owned-worker-timeout.md` | 24, 25 | `claude_haiku`, `claude_sonnet`, `gpt-5.4-mini` |
| `docs/superpowers/plans/2026-09-11-fre-1482-land-before-the-cut.md` | 315 | `gpt-5.4-mini` |
| `docs/superpowers/plans/2026-09-13-fre-1372-eval-prod-parity-extraction-gate.md` | 114 | `gpt-5.4-mini` |
| `docs/superpowers/plans/2026-09-13-fre-1499-dispatch-actuation-effectiveness.md` | 15 | `claude-haiku-4-5` |
| `docs/superpowers/plans/2026-09-14-fre-1508-verifier-unsettled-statements.md` | 66 | `claude_sonnet` |
| `docs/superpowers/plans/2026-10-03-fre-1516-qualify-selectable-planners.md` | 10, 24, 110 | `claude_sonnet` |
| `docs/superpowers/plans/2026-10-09-fre-1548-planner-json-schema-sonnet.md` | 1, 8, 9, 33, 34, 39, 60 | `claude-sonnet-5`, `claude_sonnet` |
| `docs/superpowers/plans/2026-10-10-fre-1360-untrusted-input-tool-result-channel.md` | 29 | `claude-sonnet-5` |
| `docs/superpowers/plans/2026-10-10-fre-1570-span-extraction-output-limit.md` | 47 | `claude_sonnet` |
| `docs/superpowers/plans/2026-10-12-fre-1594-reveal-spike.md` | 213 | `claude_sonnet` |
| `docs/superpowers/plans/2026-10-13-fre-1598-benefit-run.md` | 73, 74 | `claude-haiku-4-5`, `claude-sonnet-5`, `claude_haiku`, `claude_sonnet`, `gpt-5.4-mini` |
| `docs/superpowers/plans/create-the-plan-spec-zany-brooks.md` | 21 | `claude_haiku` |
| `docs/superpowers/plans/this-has-revealed-a-composed-wreath.md` | 7 | `claude_sonnet` |
| `docs/superpowers/specs/2026-03-17-self-analysis-stream-model-assignment-design.md` | 15, 48 | `claude_sonnet` |
| `docs/superpowers/specs/2026-03-28-graphiti-experiment-design.md` | 73, 78, 330 | `claude-haiku-4-5`, `claude_sonnet` |

#### Telemetry and experiments (recorded runs; no change) (16 files, 154 lines)

| File | Lines | Names |
|---|---|---|
| `experiments/concurrency_research/README.md` | 123, 124, 125, 134 | `claude-haiku-4-5` |
| `experiments/concurrency_research/run_experiment.py` | 494, 552, 690, 691 | `claude-haiku-4-5` |
| `telemetry/evaluation/EVAL-07-graphiti-experiment/2026-03-28-14-38-anthropic-report.md` | 4 | `claude-haiku-4-5` |
| `telemetry/evaluation/EVAL-07-graphiti-experiment/2026-03-28-14-38-anthropic.json` | 5, 6 | `claude-haiku-4-5` |
| `telemetry/evaluation/EVAL-skill-routing-2026-05/REVIEW_BUNDLE.md` | 186, 202 | `claude_haiku` |
| `telemetry/evaluation/EVAL-skill-routing-2026-05/cloud-model-decided-2026-05-08-expanded3/skill_routing_summary.json` | 33, 61, 89, 117, 150, 180, 210, 242, 273, 302, 331, 361, 391, 420, 450, 478, 507, 535, 564, 590, 616, 642, 670, 696, 725, 753 | `claude_haiku` |
| `telemetry/evaluation/EVAL-skill-routing-2026-05/cloud-model-decided-2026-05-08-postfix/skill_routing_summary.json` | 33, 62, 92, 120, 149, 177, 205, 231, 260, 288 | `claude_haiku` |
| `telemetry/evaluation/EVAL-skill-routing-2026-05/local-model-decided-2026-05-08-postfix/skill_routing_summary.json` | 33, 62, 90, 119, 147, 175, 201, 230, 258 | `claude_haiku` |
| `telemetry/evaluation/EVAL-skill-routing-2026-05/matrix.yaml` | 43, 68 | `claude_haiku` |
| `telemetry/evaluation/fre1337-intent-probe/2026-08-30-build.json` | 7, 43, 52, 53, 85, 94, 95, 127, 136, 137, 169, 178, 179 | `claude-sonnet-5`, `claude_sonnet` |
| `telemetry/evaluation/fre1337-intent-probe/2026-08-30-build.md` | 21 | `claude_sonnet` |
| `telemetry/evaluation/fre1337-intent-probe/2026-08-30-fre1350.json` | 7, 48, 57, 58, 90, 99, 100, 132, 141, 142, 174, 183, 184, 216, 225, 226, 258, 267, 268, 300, 309, 310, 342, 351, 352, 384, 393, 394, 426, 435, 436, 468, 477, 478, 510, 519, 520, 552, 561, 562, 594, 603, 604, 636, 645, 646, 678, 687, 688, 720, 729, 730, 762, 771, 772, 804, 813, 814, 846, 855, 856, 888, 897, 898 | `claude-sonnet-5`, `claude_sonnet` |
| `telemetry/evaluation/fre1337-intent-probe/2026-08-30-fre1350.md` | 23 | `claude_sonnet` |
| `telemetry/evaluation/fre1337-intent-probe/2026-08-30-master.json` | 7, 43, 52, 53, 85, 94, 95, 127, 136, 137, 169, 178, 179 | `claude-sonnet-5`, `claude_sonnet` |
| `telemetry/evaluation/fre1337-intent-probe/2026-08-30-master.md` | 20 | `claude_sonnet` |
| `telemetry/evaluation/fre996-pilot-final.json` | 3 | `claude-sonnet-5` |

## Appendix B: sources

All sources were read on 2026-10-20.

**OpenAI**
- [Models](https://developers.openai.com/api/docs/models)
- [gpt-6-luna](https://developers.openai.com/api/docs/models/gpt-6-luna)
- [gpt-6.1-sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol)
- [gpt-6-astra](https://developers.openai.com/api/docs/models/gpt-6-astra)
- [gpt-5.4-mini](https://developers.openai.com/api/docs/models/gpt-5.4-mini)
- [Pricing](https://developers.openai.com/api/docs/pricing)
- [Deprecations](https://developers.openai.com/api/docs/deprecations)
- [Reasoning](https://developers.openai.com/api/docs/guides/reasoning)
- [Latest model](https://developers.openai.com/api/docs/guides/latest-model)
- [Prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)
- [GPT-6 Sol and Luna announcement](https://community.openai.com/t/announcing-gpt-6-sol-and-gpt-6-luna-in-the-api-codex-and-chatgpt/1399925)
- [Product news](https://openai.com/news/product-releases/)

**Anthropic**
- [Models overview](https://platform.claude.com/docs/en/models/overview)
- [Pricing](https://platform.claude.com/docs/en/about-claude/pricing)
- [Model deprecations](https://platform.claude.com/docs/en/about-claude/model-deprecations)
- [Effort](https://platform.claude.com/docs/en/build-with-claude/effort)
- [Haiku 5.5](https://platform.claude.com/docs/en/models/haiku-5-5/overview)
- [Haiku 5.5 migration guide](https://platform.claude.com/docs/en/models/haiku-5-5/migration-guide)
- [Sonnet 5.5](https://platform.claude.com/docs/en/models/sonnet-5-5/overview)
- [What's new in Sonnet 5.5](https://platform.claude.com/docs/en/models/sonnet-5-5/whats-new-sonnet-5-5)
- `GET https://api.anthropic.com/v1/models` and `/v1/models/{id}`

**OVH and Voyage**
- [OVH AI Endpoints catalog](https://www.ovhcloud.com/en/public-cloud/ai-endpoints/catalog/)
- `GET https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/models`
- [Voyage pricing](https://docs.voyageai.com/docs/pricing)

**litellm**
- [PyPI release history](https://pypi.org/project/litellm/#history)
- [#44298](https://github.com/BerriAI/litellm/issues/44298)
- [#46074](https://github.com/BerriAI/litellm/issues/46074)
- [#45998](https://github.com/BerriAI/litellm/issues/45998)
