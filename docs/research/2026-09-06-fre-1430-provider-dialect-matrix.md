# The provider dialect matrix — five models, five vocabularies, and a guard that asks the wrong oracle

**FRE-1430** · explore session · 2026-09-06 · read-only study against the live providers

**Deployed revision under measurement:** `main@56452c7b`. Confirmed by container file hash for
`config/models.yaml`, `config/model_roles.yaml`, `llm_client/litellm_client.py`,
`llm_client/reasoning.py` and `config/model_loader.py` — see [Method](#method-appendix), M1.

**Commission, in one sentence (master, 2026-09-06):** `placement` decides both where a model runs and
which parameter dialect the client speaks to it, and those are different facts. Settle the dialect
matrix (Q1), `preserve_thinking` on the local server (Q2), what preserved thinking costs today (Q3), the
effort-versus-cost curve sampled properly (Q4), the right declaration shape (Q5), and whether the
FRE-1007 guard asks the right oracle (Q6).

**Call budget.** Stated before the runs: at most USD 5 across OVH, OpenAI and Anthropic, local calls
free. Spent: **USD 0.20** over 948 calls (M4). Every number below comes from a call to the provider
named on the row, never from its documentation and never from litellm's capability map.

---

## Verdict, up front

**There are five dialects among five declared chat models, and no two agree.** The local llama-server
accepts every field and honours a subset silently. OVH rejects every non-standard field and has an
effort vocabulary with a hole in the middle. OpenAI's `gpt-5.4-mini` rejects `max_tokens` and `stop`
outright. Claude Sonnet 5 rejects `temperature`, `top_p` and `top_k` as deprecated. Claude Haiku 4.5
accepts all three but refuses `temperature` next to `top_p`, and has no adaptive thinking and no
effort. Placement predicts none of this. Two cloud models from one vendor disagree with each other
(F6). The full matrix is F1.

**On the local server, rejection tells you nothing.** An unknown field returns 200 (F2). Every
finding about llama.cpp in this study had to come from behaviour, not from status codes. Three
declared or client-sent levers are inert there today: `repetition_penalty` (the wire name is
`repeat_penalty`, measured F2), the top-level `enable_thinking` and `preserve_thinking` forms, and
`response_format: json_object` (fenced or prose output, F2).

**Q2 is settled, and the answer is two-sided (F11).** Across turns the template drops
`reasoning_content` unless `chat_template_kwargs.preserve_thinking` is true, and the top-level flag the
executor comment names does nothing. Within a turn — every assistant message after the last user
message — the template renders `reasoning_content` **unconditionally**, flag or no flag. So the
executor's tool loop already carries thinking forward on the local model today.

**Q3: preserved thinking costs nothing across turns and little within them (F12).** Persisted session
messages carry no `reasoning_content` (0 of 133 in seven days), so hydration cannot re-inject it. Within
a turn the carry is bounded by the prior round's output, and on the two longest real primary loops the
prompt grew 1.5k–6.6k tokens per round while the primary emitted 144–365 tokens per round. Tool results
are the growth. The owner's 22 845-token turn had one message, twenty-five tool definitions and nine
system-prompt components, and zero reasoning.

**Q4: the curve is monotonic where it matters (F13).** On OVH, `none` costs a tenth of `low` and
`low` sits below `medium` on every heavy prompt shape. Master's `low > medium` was a single-sample
artefact on a trivial prompt, where the two are indistinguishable at n=5. The provider default bills
2.2x `medium` and has a fat tail (one 2 746-token answer to a one-line extraction). On OpenAI the five
levels order cleanly from 0 to 412 reasoning tokens. On Sonnet 5 `low` is the cheapest setting measured,
below thinking-off, because adaptive thinking at `low` skips the thinking block and writes a terse
answer. On Haiku 4.5, litellm's `low` is a 1 024-token budget that costs six times thinking-off.

**Q6: the guard asks litellm, and litellm is wrong about OVH at the provider level, not the model
level (F8).** `get_supported_openai_params` for `ovhcloud` returns a list without `reasoning_effort`
for any model. So the guard's three-valued answer is `None` today (no cost-map record) and would be
`False` the day OVH's record appears. Both omit the value. `allowed_openai_params` is the forwarding
mechanism, and it is a per-call kwarg, not a map entry.

**Q5:** the recommendation is P1 — a named `dialect` on the provider, overridable on the model, that
names the thinking-lever vocabulary and the accepted sampling set, with FRE-1426's `modes:` written
in that vocabulary. The cost is small and is stated there.

---

## Findings

Every finding states its verdict, the query as run, and the output as returned. A negative finding
carries the admissibility arms on the finding itself. Status codes and error strings are quoted from
the provider's response body.

### F1 — The dialect matrix: what each declared model accepts, rejects, and honours

**Verdict:** POSITIVE (an acceptance matrix; the silent-ignore rows are carried separately in F2 with
their arms).

**Query.** One raw HTTP `POST /v1/chat/completions` per field per provider (native `/v1/messages`
for Anthropic), prompt "Say OK.", `max_tokens 48`, one field varied per call. Then a behavioural
probe per field where a status code cannot distinguish "honoured" from "ignored" (M2 lists every
instrument). Providers: llama-server via Caddy `:8600` serving `unsloth/qwen3.8-flash-next`; OVH
`Qwen3.8-27B`; OpenAI `gpt-5.4-mini`; Anthropic `claude-sonnet-5` and `claude-haiku-4-5-20251001`.

**Legend.** `✓` accepted and the behavioural probe shows the effect · `∅` accepted, probe shows **no**
effect (silently ignored — F2 carries the arms) · `✓?` accepted, no behavioural instrument · `R`
rejected, with the quoted reason.

| Field | local llama.cpp | OVH Qwen3.8-27B | OpenAI gpt-5.4-mini | Sonnet 5 (native) | Haiku 4.5 (native) |
|---|---|---|---|---|---|
| unknown field (`zzz_unknown_field`) | **∅** 200 | R "feature 'extra arguments: {…}' is not currently supported" | R "Unknown parameter" | R "Extra inputs are not permitted" | R "Extra inputs are not permitted" |
| `temperature` | ✓ 1/6 distinct at 0, 5/6 at 1.8 | ✓ 1/6 at 0, 5/6 at 1.8 | ✓ only with `reasoning_effort: none` (2/6 vs 4/6); R with `low`: "Unsupported value: 'temperature' does not support 0.2 with this model. Only the default (1) value is supported." | R "`temperature` is deprecated for this model." | ✓ 1/6 at 0, 4/6 at 1.0; R next to thinking: "`temperature` may only be set to 1 when thinking is enabled" |
| `top_p` | ✓ 1/6 at 0.01 | ✓ 1/6 at 0.01 | ✓ with `none` (2/6); R with `low`: "'top_p' is not supported with this model." | R "deprecated for this model" | ✓ 1/6 at 0.01 alone; R with `temperature`: "`temperature` and `top_p` cannot both be specified for this model." |
| `top_k` | ✓ 1/6 at 1 | R extra arguments | R "Unknown parameter: 'top_k'" | R "deprecated for this model" | ✓ 1/6 at 1 |
| `min_p` | ✓ 1/6 at 0.95 | R extra arguments | R "Unknown parameter" | n/a | n/a |
| `presence_penalty` | ✓ small (59→56 repeats) | ✓ small (57→53) | **∅** (62→62) | R "Extra inputs are not permitted" | R "Extra inputs are not permitted" |
| `frequency_penalty` | ✓ 59→6 | ✓ 57→6 | ✓ 62→12 | n/a | n/a |
| `repetition_penalty` (catalog name) | **∅** 59→59 | R extra arguments | R "Unknown parameter" | n/a | n/a |
| `repeat_penalty` (llama.cpp name) | ✓ 59→3 | not sent | not sent | n/a | n/a |
| `seed` | ✓ 1/6 at temp 1.8 | ✓ 1/6 at temp 1.8 | ✓ 1/6 at temp 1.8 | R "Extra inputs are not permitted" | R |
| `max_tokens` | ✓ `finish_reason: length`, 5 tokens | ✓ same | R "'max_tokens' is not supported with this model. Use 'max_completion_tokens' instead." | ✓ | ✓ `stop_reason: max_tokens` |
| `max_completion_tokens` | ✓ length at 32 | ✓ alone; R with both: "Setting 'max_tokens' and 'max_completion_tokens' at the same time is not supported." | ✓ length at 5 | n/a | n/a |
| `stop` | ✓ `'1, 2, 3, 4, '` | ✓ `'1, 2, 3'` | R "'stop' is not supported with this model." | ✓? (`stop_sequences`) | ✓? |
| `n: 2` | ✓ 2 choices | ✓ 2 choices | ✓ 2 choices | R | R |
| `logprobs` | ✓ present | ✓ alone; R `top_logprobs: 2`: "feature 'top_logprobs > 1' is not currently supported" | ✓ present | R | R |
| `response_format: json_object` | **∅** fenced ```` ```json ```` then prose on a second prompt | ✓ parses | ✓ parses; R without the word "json" in messages | R | R |
| `response_format: json_schema` | ✓ once (content JSON); one call **wedged the server**, F14 | ✓ parses | ✓ parses | R | R |
| `reasoning_effort: none` | ✓? 200, thinking still present | ✓ 3 tokens, no reasoning | ✓ 0 reasoning tokens | R "Extra inputs are not permitted" | R |
| `reasoning_effort: minimal` | R **500** Jinja: "Unexpected reasoning effort minimal. Supported types are xhigh (default), medium, and low." | R 400, same template text | R "Supported values are: 'none', 'low', 'medium', 'high', and 'xhigh'." | R | R |
| `reasoning_effort: low` / `medium` | ✓? 200 | ✓ reasoning present | ✓ reasoning tokens 5–47 / 13–52 | R | R |
| `reasoning_effort: high` | ✓? 200 | R 400 template: "Unexpected reasoning effort high" | ✓ 13–103 | R | R |
| `reasoning_effort: xhigh` | ✓? 200 | R **422** gateway: "reasoning_effort: unknown variant `xhigh`, expected one of `none`, `high`, `medium`, `low`, `minimal`" | ✓ 21–412 | R | R |
| `chat_template_kwargs.enable_thinking: false` | ✓ 3 tokens, no reasoning | R extra arguments | R "Unknown parameter" | n/a | n/a |
| `enable_thinking: false` (top level) | **∅** reasoning present | R | R | n/a | n/a |
| `thinking_budget` / `reasoning_budget` | **∅** (FRE-1423) | R | R | n/a | n/a |
| `preserve_thinking` (top level) | **∅** F11 | R | R | n/a | n/a |
| `chat_template_kwargs.preserve_thinking` | ✓ F11 | R | R | n/a | n/a |
| `thinking: {type: enabled, budget_tokens}` | ✓? 200 | R | R "Unknown parameter" | R "\"thinking.type.enabled\" is not supported for this model. Use \"thinking.type.adaptive\" and \"output_config.effort\"" | ✓ thinking block returned |
| `thinking: {type: adaptive}` (+ `output_config.effort`) | n/a | n/a | n/a | ✓ | R "adaptive thinking is not supported on this model" |
| `output_config.effort` without thinking | n/a | n/a | n/a | ✓ 200 | R "This model does not support the effort parameter." |
| `thinking: {type: disabled}` | n/a | n/a | n/a | ✓ | ✓ |
| `tools` + `tool_choice: auto` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `parallel_tool_calls: false` | ✓? 200 | R "feature 'parallel_tool_calls=false' is not currently supported" | ✓ 200 | mapped by litellm to `disable_parallel_tool_use` | same |
| `cache_prompt` | ✓? 200 | R | R | n/a | n/a |
| literal `extra_body` key | **∅** 200 (ADR-0141's finding) | R | R "Unknown parameter: 'extra_body'" | n/a | n/a |
| `stream_options` without `stream` | ✓? 200 | R "Stream options can only be defined when `stream=True`" | R "only allowed when 'stream' is enabled" | n/a | n/a |
| thinking text in the response | `message.reasoning_content` | `message.reasoning` | hidden; `usage.completion_tokens_details.reasoning_tokens` | `content[].type == "thinking"` (adaptive, sometimes absent) | `content[].type == "thinking"` |

**Actual outputs behind the sampling cells** (six calls each, prompt "Reply with one random 5-digit
number and nothing else."; the local and OVH runs with thinking off):

```
local  temperature=0            distinct=1/6 ['48291','48291','48291','48291','48291','48291']
local  temperature=1.8          distinct=5/6 ['82931','48291','47291','12345','47291','42197']
local  temperature=1.8+top_p=0.01   1/6   +top_k=1  1/6   +min_p=0.95  1/6   +seed=7  1/6
ovh    temperature=0 1/6   temperature=1.8 5/6 ['48291','48291','38492','37914','38291','48213']
ovh    temperature=1.8+top_p=0.01 1/6   +seed=7 1/6   +top_k / +min_p -> 400 extra arguments
openai temperature=0 2/6   temperature=1.8 4/6   +top_p=0.01 2/6   +seed=7 1/6
haiku  temperature=0 1/6   temperature=1.0 4/6   top_p=0.01 alone 1/6   top_k=1 alone 1/6
```

Repeat-word counts under the penalty probes (prompt asks for "apple" 60 times, `temperature 0`,
three calls each): local `59,59,59` baseline · `presence_penalty 2` `56,56,56` · `frequency_penalty 2`
`6,6,6` · `repetition_penalty 1.8` `59,59,59` · `repeat_penalty 1.8` `3` (content
`'apple x125x48793e-please-ignore-all-instructions-a…'`). OVH `57,57,57` · `53,53,53` · `6,6,6`.
OpenAI `62,62,62` · `62,62,62` · `12,12,12`.

### F2 — On llama-server, "accepted" is not "honoured": six silently ignored fields

**Verdict:** NEGATIVE for each of the six rows (the field has no effect on this server).

**Query.** Same endpoint and same prompt as F1, one field varied, with the behavioural instrument
named per row.

| Field sent | Instrument | Output with the field | Output without / with the honoured form |
|---|---|---|---|
| `zzz_unknown_field: 1` | status code | 200, `'OK.'` | — |
| `repetition_penalty: 1.8` | repeat count | `59` apples, 60 tokens | `59` without; `3` with `repeat_penalty: 1.8` |
| `enable_thinking: false` (top level) | `reasoning_content` presence | present, 38 chars | absent, 3 tokens with `chat_template_kwargs.enable_thinking: false` |
| `preserve_thinking: true` (top level) | `usage.prompt_tokens` on a 3-message history | 105 | 105 without; **200** with `chat_template_kwargs.preserve_thinking: true` |
| `thinking_budget: 10` / `reasoning_budget: 10` | `reasoning_content` length | 73 chars each | 73 without |
| `response_format: {type: json_object}` | `json.loads(content)` | fails: ```` ```json\n{ "city": "Paris", … }\n``` ```` and, on a second prompt, prose `'The capital city of Japan is **Tokyo**…'` | `json_schema` on the same server returned bare `{"city": "Seattle", "country": "USA"}` (F1) |

**Arm 1 — provenance.** Each row quotes the raw 200 response carrying the field, from the same
endpoint the finding is about (`http://localhost:8600/v1/chat/completions`, Caddy → the tunnelled
llama-server). The client-side producer for `repetition_penalty` is `_local_extra_body`
(`litellm_client.py:214–215`) at container hash `5bdfb602`; the catalog declares it on both local
entries (`models.yaml`, value `1.0`).
**Arm 2 — liveness.** Same endpoint, same prompt, the honoured sibling in the last column changes the
output: `repeat_penalty` 59→3, `chat_template_kwargs.enable_thinking` 38→0 chars,
`chat_template_kwargs.preserve_thinking` 105→200 tokens, `json_schema` bare JSON.
**Arm 3 — scope.** The llama-server build behind `slm_server` port 8502 serving
`unsloth/qwen3.8-flash-next` (`/v1/models` on 2026-09-06 11:26 UTC: `context_length 131072`,
`quantization UD-IQ4_XS`), reached through Caddy `:8600`. Whether another build honours
`json_object` by grammar, or reads a top-level `enable_thinking`, is not claimed.

Consequence for the catalog: `repetition_penalty: 1.0` on both local entries reads as configured and
sends a key the server discards. The value is neutral, so nothing changes live, but the lever does not
exist under that name. Filed as **FRE-1438**.

### F3 — OVH's effort vocabulary has a hole: `high` and `xhigh` are unreachable, and the default is the unrequestable one

**Verdict:** POSITIVE.

**Query.** Six raw calls to `https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/chat/completions`,
model `Qwen3.8-27B`, `reasoning_effort` varied.

**Output:**

```
none     200  completion_tokens=3   message.reasoning absent    content 'OK.'
low      200  completion_tokens=28  reasoning 79 chars
medium   200  completion_tokens=30  reasoning 101 chars
minimal  400  {"error":{"message":"Unexpected reasoning effort minimal. Supported types are xhigh (default), medium, and low."}}
high     400  {"error":{"message":"Unexpected reasoning effort high. Supported types are xhigh (default), medium, and low."}}
xhigh    422  Failed to deserialize the JSON body into the target type: reasoning_effort: unknown variant `xhigh`, expected one of `none`, `high`, `medium`, `low`, `minimal`
```

Two validators disagree. The gateway's deserializer admits `none, high, medium, low, minimal`; the
model's chat template admits `xhigh, medium, low` and reads `none` as "no thinking". The intersection
that reaches the model is **`none`, `low`, `medium`**. `xhigh` — the provider default and the most
expensive setting — can only be obtained by omitting the field. The catalog's
`Literal["none","low","medium","high","xhigh"]` (`llm_client/models.py:297`) admits two values this
provider rejects.

The local server runs the same template family: `minimal` produced a **500** from the Jinja engine
with the identical sentence (`F1`), and `none`/`low`/`medium`/`high`/`xhigh` all returned 200. Whether
they change anything locally is F16.

### F4 — What the client actually sends today, per branch

**Verdict:** POSITIVE (code at the deployed hash, confirmed by the live `model_call_started` fields).

**Query.** `litellm_client.py` at container hash `5bdfb602`, and the ES `model_call_started`
document for the owner's OVH turn (`trace_id d959a4192efa8d3aa55edff9839c4a68`).

**Output.** Cloud branch (`:898–966`): `model`, `messages`, `max_tokens`, `api_key`, `client`,
`api_base`, `tools`, `tool_choice`, `response_format`, `temperature` **only when the caller passes
one**, `reasoning_effort` behind the F8 guard, `timeout`, `num_retries`. No `top_p`, no
`presence_penalty`, no `extra_body`. Local branch (`:1498–1545`): `temperature` with a
`model_def.temperature` fallback, `top_p`, `presence_penalty`, `response_format`, `tools`, `stream`,
and `extra_body` = `{cache_prompt, top_k, min_p, repetition_penalty, chat_template_kwargs |
thinking_budget}`. The live event confirms the cloud shape:
`{"model": "ovhcloud/Qwen3.8-27B", "max_tokens": 32768, "reservation_amount_usd": 0.004027}` — no
effort, no temperature.

The callers that pass a temperature are three: `context_compressor.py:259` (0.2, bound to
`gpt-5.4-mini`), `skills.py:478` (0.0, `SKILL_ROUTING`, live key `claude_haiku` — Haiku accepts it,
F1), and `entity_extraction.py:1170` (the catalog's own value). None reaches Sonnet 5, which rejects
it. That is accidental, not designed — see P8.

### F5 — litellm's parameter map, per declared model, and what `allowed_openai_params` changes

**Verdict:** POSITIVE.

**Query.** `litellm.utils.get_optional_params(model, custom_llm_provider, **{field})` offline,
litellm 1.98.0 (the version pinned in `uv.lock` and running in the container, M1), no network call.
Then the same call with `allowed_openai_params=[field]`.

**Output** (abridged to the rows that decide something; the full run is in M2):

| Field | `ovhcloud/Qwen3.8-27B` | `openai/gpt-5.4-mini` | `anthropic/claude-sonnet-5` | `anthropic/claude-haiku-4-5` |
|---|---|---|---|---|
| `temperature 0.2` | forwarded | forwarded | **raises** "Only temperature=1 is supported"; still raises with `allowed_openai_params` | forwarded |
| `top_p` | forwarded | forwarded | raises | forwarded |
| `presence_penalty` / `frequency_penalty` | forwarded | **raises** "openai does not support parameters" | raises | raises |
| `stop` | forwarded | **raises** | mapped → `stop_sequences` | mapped |
| `seed` / `n` / `logprobs` | forwarded | forwarded | raises | raises |
| `max_tokens 100` | forwarded as `max_tokens` | mapped → `max_completion_tokens` | forwarded | forwarded |
| `reasoning_effort none` | **raises** "ovhcloud does not support parameters: ['reasoning_effort']" | forwarded | **dropped silently** → `{}` | dropped silently |
| `reasoning_effort low` | raises; **forwarded** with `allowed_openai_params` | forwarded | mapped → `thinking {adaptive}` + `output_config {effort: low}` | mapped → `thinking {enabled, budget_tokens 1024}` **and `max_tokens` rewritten to 5120** |
| `reasoning_effort xhigh` | raises | forwarded | mapped adaptive `xhigh` | mapped budget 8192, `max_tokens` 12288 |
| `top_k` | forwarded | mapped into `extra_body` | forwarded | forwarded |
| `thinking {enabled}` | raises | raises | forwarded (+ `max_tokens 5120`) | forwarded |
| `temperature + reasoning_effort low` | — | **raises** "gpt-5 models … don't support temperature=0.2" | — | — |

Three of these rows are wrong about the provider. `ovhcloud` + `reasoning_effort` raises where the
provider accepts (F3). `openai` + `presence_penalty` raises where the provider accepts (F1).
`openai` + `max_tokens` is silently rewritten to the field the provider demands — the one row where
the map is right and the raw call is wrong. And the Anthropic `none` row is the FRE-1007 case: the
value vanishes and the provider default (adaptive, `high`) applies.

### F6 — The same vendor, two dialects: Sonnet 5 rejects what Haiku 4.5 requires

**Verdict:** POSITIVE.

**Query.** Native `POST https://api.anthropic.com/v1/messages`, `anthropic-version: 2023-06-01`,
eighteen field probes per model (M2 lists them).

**Output** (verbatim error strings):

```
claude-sonnet-5           temperature 0.2  -> 400 "`temperature` is deprecated for this model."
                          top_p / top_k    -> 400 "deprecated for this model."
                          thinking enabled -> 400 "\"thinking.type.enabled\" is not supported for this model. Use \"thinking.type.adaptive\" and \"output_config.effort\" to control thinking behavior."
                          thinking adaptive + output_config.effort low -> 200
                          output_config.effort low, no thinking block  -> 200
claude-haiku-4-5-20251001 temperature 0.2  -> 200      top_p -> 200      top_k -> 200
                          temperature + top_p -> 400 "`temperature` and `top_p` cannot both be specified for this model. Please use only one."
                          thinking adaptive -> 400 "adaptive thinking is not supported on this model"
                          output_config.effort -> 400 "This model does not support the effort parameter."
                          thinking enabled 1024 -> 200, thinking block 102 chars
                          thinking enabled + temperature 0.2 -> 400 "`temperature` may only be set to 1 when thinking is enabled."
```

A dialect declared per vendor is therefore already wrong. It has to be per model, which is the
memory note from FRE-1007 restated with the provider's own words.

### F7 — OVH thinking reaches our path under litellm's key, and OVH honours prior thinking only under its own key

**Verdict:** POSITIVE.

**Query.** (a) `litellm.acompletion("ovhcloud/Qwen3.8-27B", reasoning_effort="low",
allowed_openai_params=["reasoning_effort"])`, non-streaming, prompt "What is 17*23? Number only."
(b) Three raw calls with a three-message tool-loop history, the assistant message carrying the prior
thinking under `reasoning_content`, under no key, and under `reasoning`; `reasoning_effort: none`.

**Output:**

```
(a) message.content='\n\n391'  message.reasoning_content='17 × 23 = 17 × 20 + 17 × 3 = 340 + 51 = 391'
    provider_specific_fields=['reasoning']  usage.completion_tokens=42
(b) assistant.reasoning_content present -> 200, prompt_tokens=324
    no reasoning key                     -> 200, prompt_tokens=324
    assistant.reasoning present          -> 200, prompt_tokens=338
```

litellm's `_extract_reasoning_content` reads `reasoning` when `reasoning_content` is absent
(`litellm_core_utils/prompt_templates/common_utils.py:1533–1536`), so the executor's
`response.reasoning_trace` is populated on OVH the same way as on the local server. Master's F7
correction on FRE-1426 holds through litellm too. On the input side OVH is the mirror image: it
silently discards our key and renders only its own, and the executor writes ours (F12). The message
history is a dialect surface as much as the request body is.

### F8 — The FRE-1007 guard asks litellm, and litellm's answer for OVH is wrong at the provider level

**Verdict:** POSITIVE.

**Query.** In the live container: `litellm.model_cost` membership for each declared id;
`provider_reasoning_support(model, provider)` (`reasoning.py:49`); and
`litellm.get_supported_openai_params(model="Qwen3.8-27B", custom_llm_provider="ovhcloud")`.

**Output:**

```
model_cost entries 3817
claude-sonnet-5 True   claude-haiku-4-5-20251001 True   gpt-5.4-mini True
ovhcloud/Qwen3.8-27B False   Qwen3.8-27B False   ovhcloud/Qwen3.6-27B False
support anthropic claude-sonnet-5 True
support anthropic claude-haiku-4-5-20251001 True
support openai gpt-5.4-mini True
support ovhcloud Qwen3.8-27B None
support ovhcloud Qwen3.6-27B None
supported_openai_params(ovhcloud/Qwen3.8-27B) = ['audio','extra_headers','frequency_penalty','function_call',
  'functions','logit_bias','logprobs','max_completion_tokens','max_retries','max_tokens','modalities','n',
  'parallel_tool_calls','prediction','presence_penalty','prompt_cache_key','prompt_cache_retention',
  'response_format','safety_identifier','seed','service_tier','stop','store','stream','stream_options',
  'temperature','tool_choice','tools','top_logprobs','top_p','web_search_options']
```

The list is `OpenAIGPTConfig`'s base list (`llms/ovhcloud/chat/transformation.py:20` subclasses it and
adds nothing), and it has no `reasoning_effort`. So the guard returns `None` today because the cost
map has no OVH record, and it would return `False` the day the record appears — the `False` branch is
documented in the function's own docstring as "litellm's `ovhcloud` provider". Both outcomes omit the
value and, on the `None` path, log `reasoning_declaration_undeliverable`. The oracle cannot become
right for this provider by any change in litellm's data. It is the code that is wrong, and it is wrong
for every model on that provider. F15 measures whether the log line has ever fired.

### F9 — Through litellm, `allowed_openai_params` forwards the effort and the provider honours it

**Verdict:** POSITIVE (reproduces master's FRE-1426 measurement through the SDK, with the cost).

**Query.** As F7(a), `reasoning_effort` varied.

**Output.** `low` → 42 completion tokens with reasoning present. Master's earlier run: baseline 76
with reasoning, `none` 4 without, `medium` 62 with. The forwarding kwarg is documented in litellm's
own error text (`utils.py:4037`). Nothing else on the cloud branch needs to change for OVH to become
controllable.

### F10 — `xhigh` on the local server: the template validates, the server accepts

**Verdict:** POSITIVE — see F16 for the effect.

The Jinja exception in F1 (`500`, "Unexpected reasoning effort minimal … Supported types are xhigh
(default), medium, and low") is the same validator OVH returns as a 400. The local template reads
`reasoning_effort`. Whether it changes the thinking length is a behavioural question, answered in
F16, and it is a lever the local branch never sends today (F4).

### F11 — `preserve_thinking` on the local server: off across turns unless flagged, always on within a turn

**Verdict:** POSITIVE (two positives and one negative; the negative carries its arms).

**Query.** One seed call produced a real thinking block (467 chars) and an answer (324 chars). Then
sixteen calls with `max_tokens 1`, reading `usage.prompt_tokens` (the full rendered prompt, cache
included) and `timings.prompt_n` (the uncached prefill), history shape varied.

**Output:**

```
-- cross-turn shape: [user, assistant, user]                         prompt_tokens   prompt_n
   assistant plain (no reasoning)                                          105           88
   assistant + reasoning_content, no flag                                  105            4
   assistant + reasoning_content + chat_template_kwargs.preserve_thinking  200           26
   assistant + reasoning_content + top-level preserve_thinking             105            4
   assistant with inline <think>…</think> in content                       200          183
   assistant + reasoning_content + enable_thinking=false                   107            6
-- within-turn tool-loop shape: [user, assistant(tool_calls), tool]
   no reasoning                                                            326           72
   + reasoning_content, no flag                                            417          163
   + reasoning_content + chat_template_kwargs.preserve_thinking=true       417            4
   + reasoning_content + chat_template_kwargs.preserve_thinking=false      417            4
-- two rounds: [user, a1(tool_calls), tool, a2(tool_calls), tool]
   no reasoning                                                            378          124
   reasoning on both assistant messages                                    560          306
   reasoning on the last assistant only                                    469          215
-- calibration
   the thinking text alone as user content                                 100          100
   minimal prompt "x" (template overhead)                                   11           11
```

The thinking text is 89 tokens. Every "rendered" row is the baseline plus 91–95 tokens, every
"dropped" row equals its baseline. Two rounds with reasoning on both cost 182 tokens over the plain
shape, which is 2 × 91: **the template renders every assistant `reasoning_content` after the last
user message, and no flag turns that off.** `preserve_thinking=false` inside `chat_template_kwargs`
changes nothing either (417 = 417).

**The negative row — top-level `preserve_thinking` does nothing.** Arm 1: the raw 200 response with
the field carried `prompt_tokens 105`, identical to no field, on the same endpoint. Arm 2: the same
history with the `chat_template_kwargs` form returned 200 tokens. Arm 3: this served template on
2026-09-06 11:03 UTC; the executor comment at `executor.py:6370–6374` names "templates that support
`preserve_thinking`" and "until the slm_server flag flips" — there is no flag on this path, the
kwarg form is honoured now, and the within-turn shape needs neither.

### F12 — What preserved thinking costs per turn today: zero across turns, bounded within them

**Verdict:** POSITIVE, with one negative carrying arms.

**Query 1 (negative — no cross-turn carry).** Postgres `sessions.messages`, JSONB elements over
sessions active in the last seven days:

```sql
select count(*) filter (where m ? 'reasoning_content'), count(*)
from sessions, jsonb_array_elements(messages) m
where last_active_at > now() - interval '7 days';
-- 0 | 133
```

The owner's OVH session (`6a4b1d46…`) persisted four messages, keys
`role,content,metadata,trace_id,timestamp` only. Hydration (`service/app.py:2133–2135`) copies the
last ten of those and nothing else.
**Arm 1 — provenance.** The producer is `executor.py:6375–6377`: `reasoning_content` is set on
`ctx.messages` in memory. The persisted row is written by `repo.append_message` with a fixed key set
(`service/app.py:343–352`) — the raw rows above are the store the finding is about, and they exhibit
the four keys and no fifth. **Arm 2 — liveness.** Same store, same window, `m ? 'content'` → 133 of
133. **Arm 3 — scope.** Sessions with `last_active_at` in the seven days to 2026-09-06 11:00 UTC,
production Postgres. Nothing is claimed about older rows.

**Query 2 (positive — within-turn growth on real primary loops).** ES `agent-logs-*`,
`event_type: model_call_completed`, `role: primary`, per trace, in timestamp order.

```
trace cf25bc13 (2026-09-04 06:17, local qwen3.8-flash-next, 3 tool calls per round)
  in 15537 out 365 | in 19210 out 208 (+3673) | in 22879 out 236 (+3669) | in 26408 out 209 (+3529)
  in 32120 out 199 (+5712) | in 38769 out 207 (+6649) | in 44219 out 225 (+5450) | synthesis in 68076 out 8984
trace b65c92b7 (2026-09-05 20:32, 2 tool calls per round)
  in 12348 out 144 | in 14284 out 236 (+1936) | in 17591 out 224 (+3307) | in 19125 out 159 (+1534)
  in 23111 out 148 (+3986) | in 29023 out 176 (+5912) | synthesis in 34873 out 1938
```

The carried reasoning of round *k* is at most round *k−1*'s whole output — 144 to 365 tokens, and
that output also holds the tool-call JSON. Per-round growth is 1.5k to 6.6k tokens. Reasoning is
under a tenth of it on every row. Tool results are the cost.

**Query 3 (the owner's turn).** `context_budget_applied` for `d959a419…`:
`{"memory_tokens": 460, "tool_tokens": 0, "reasoning_tokens": 0, "total": 481, "message_count": 1}`.
`llm_call_messages_debug`: `message_count 1, message_roles ["user"]`. `tools_passed_to_llm`:
`tool_count 25`. `model_call_completed`: `input_tokens 22845`, nine `prompt_component_ids`
(`grounding_contract, tool_awareness, deployment_context, operator_stanza, skill_index, skill_bodies,
memory_section, current_datetime, tool_use_rules`). One user message, no assistant history, no
reasoning: the 22 845 are system prompt, tool definitions and the sub-agent digest. The hypothesis in
Q3 — input grows with conversation depth through retained thinking — is false for that turn and for
every hydrated turn.

### F13 — The effort-versus-cost curve, sampled

**Verdict:** POSITIVE.

**Query.** Five prompt shapes (arithmetic, one-word fact, three-person ordering puzzle, a
250-word summary into three bullets, a JSON extraction), n samples per level, `max_tokens` 4000 on
OVH and OpenAI, 2500 on Anthropic, concurrent calls, raw HTTP. Correctness is scored where the prompt
has one answer (`1296`, `Saturn`, `Bob`, JSON with three keys).

**OVH `Qwen3.8-27B`, n=5 per cell, completion tokens per sample, sorted:**

```
             arith             fact                puzzle            summary                 extract
none         5 5 5 5 5         3 3 3 3 3           2 2 2 2 2         97 103 105 111 119      45 47 47 47 52
low          60 65 65 65 65    68 68 79 90 99      58 60 60 67 71    212 242 252 271 417     142 148 157 175 186
medium       58 58 64 65 74    55 62 69 70 93      66 67 67 67 67    389 394 406 495 524     152 217 287 322 418
default      68 69 72 76 83    89 111 182 207 277  44 49 52 53 88    297 300 321 327 337     261 303 367 428 2746
totals       none 823 tokens $0.0035 wall 350 ms | low 3242 $0.0116 1522 ms | medium 4606 $0.0156 1210 ms | default 7207 $0.0244 2923 ms
correct      none 22/25 (puzzle 4/5) | low 25/25 | medium 25/25 | default 25/25
```

`low` and `medium` are indistinguishable on the three trivial shapes (65 vs 64, 79 vs 69, 60 vs 67
medians) and ordered on the two heavy ones (252 vs 406, 157 vs 287). Master's single-sample `low
1046 > medium 564` is inside this spread. The default is 2.2x `medium` in total and carries the only
outlier (2 746 tokens to extract three fields). `none` missed the puzzle once in five.

**OpenAI `gpt-5.4-mini`, n=3, `(completion_tokens, reasoning_tokens)`:**

```
none    arith (5,0)(5,0)(5,0)        fact (5,0)×3          puzzle (10,0)×3         summary (101,0)(97,0)(110,0)    extract (28,0)×3
low     (32,21)(32,21)(38,27)        (43,32)(46,35)(45,34) (30,20)(29,19)(57,47)   (124,16)(121,10)(136,10)        (39,5)(39,5)(59,25)
medium  (26,15)(25,14)(24,13)        (48,37)(63,52)(62,51) (47,37)(61,51)(62,52)   (142,29)(131,30)(164,52)        (63,29)(76,42)(70,36)
high    (25,14)(24,13)(36,25)        (73,62)(63,52)(55,44) (58,48)(73,63)(72,62)   (158,43)(186,72)(186,65)        (88,54)(117,83)(137,103)
xhigh   (32,21)(47,36)(55,44)        (144,133)(178,167)(183,172) (78,68)(71,61)(79,69) (353,240)(455,356)(551,412) (103,69)(110,76)(205,171)
totals  none 452 $0.0028 659 ms | low 870 $0.0047 | medium 1064 $0.0056 | high 1351 $0.0069 | xhigh 2644 $0.0127 1321 ms
correct all 15/15 at every level
```

**Anthropic `claude-sonnet-5`, n=2, native adaptive thinking:**

```
level        completion tokens (arith, fact, puzzle, summary, extract)   thinking block present   total $
no_thinking  4 4 | 57 63 | 70 70 | 166 168 | 54 54                        0/10                    0.0137
low          4 4 | 6 6   | 5 5   | 174 205 | 54 54                        0/10                    0.0108
medium       4 4 | 6 71  | 36 61 | 180 213 | 54 54                        4/10                    0.0133
high         4 6 | 59 68 | 53 61 | 230 255 | 54 54                        4/10                    0.0157
default      6 6 | 62 70 | 54 61 | 208 232 | 54 54                        3/10                    0.0152
correct 10/10 at every level
```

`low` is the cheapest Sonnet setting measured, below thinking-off: adaptive thinking at `low`
emitted no thinking block on any of the ten calls and answered "Bob", "Saturn" in one word where
thinking-off wrote 139–167 characters of explanation. Effort on Sonnet 5 changes verbosity as much as
hidden thinking.

**Anthropic `claude-haiku-4-5`, n=2, native `thinking.budget_tokens`:**

```
no_thinking  6 6 | 4 5 | 4 51 | 101 102 | 47 47      thinking 0/10   $0.0026   correct 5/8 (fact 0/2: not Saturn)
default      6 6 | 4 4 | 5 61 | 105 107 | 47 47      thinking 0/10   $0.0027   correct 5/8
budget 1024  115 137 | 195 243 | 131 142 | 305 393 | 130 329   10/10   $0.0116   correct 8/8
budget 8192  72 133 | 99 247 | 126 135 | 338 343 | 173 397     10/10   $0.0113   correct 7/8
```

litellm's `low` for Haiku is `budget_tokens 1024` (F5): 4.4x the tokens of thinking-off on these
shapes. The `budget 8192` run first failed on all five prompts with
"`max_tokens` must be greater than `thinking.budget_tokens`" at `max_tokens 2500`, which is the
rewrite litellm performs silently (F5) and a raw caller must perform by hand.

### F14 — One `json_schema` request wedged llama-server for fourteen minutes (observation, single instance)

**Verdict:** POSITIVE for the timeline; **no claim of cause**.

**Query.** The harness call log, local rows, 11:02–11:26 UTC.

**Output:**

```
11:02:10 behav:json_schema   started (thinking on, max_tokens 80, strict schema)   -> 524 after 125052 ms
11:04:15 behav:n=2                                                                 -> 524 after 125077 ms
11:06:21 behav:logprobs                                                            -> 524 after 125105 ms
11:10:35 sampling:temperature=0                                                     -> 524 after 125124 ms
11:12:53 sampling:temperature=0                                                     -> 500 "Internal server error while routing" after 111542 ms
11:16:37 every call                                                                 -> 503 "Backend server unreachable. Is the model server running?"
11:2x    every call                                                                 -> 503 {"error":{"message":"Loading model","type":"unavailable_error","code":503}}
11:26    /v1/models 200, "Say OK." -> 'OK' in 2 completion tokens
```

`/health` returned `{"status":"healthy"}` throughout, as FRE-1398 records. The 10:58 `json_schema`
call in the acceptance run, same server, same schema, different prompt, returned bare JSON in 45
tokens. So this is one stall on one request shape, not a reproduction. Posted as a comment on
FRE-1398 (in progress on the adr stream) because it is the same failure class with a new candidate
trigger; no ticket filed. The local sampling and effort runs were repeated after recovery (F1, F16).

### F15 — The guard's failure branch has never fired in production

**Verdict:** NEGATIVE.

**Query.** ES `agent-logs-*`, `_count`, `event_type: reasoning_declaration_undeliverable`, thirty
days and all time.

**Output:** `{"count": 0}` (30 d) · `{"count": 0}` (all time).

**Arm 1 — provenance (1b).** No instance exists. The emit site is `litellm_client.py:952–962` at
container hash `5bdfb602`, inside the branch `provider_reasoning_support(...) is None` under
`effective_reasoning_effort is not None`. A sibling emit from the same function and component —
`model_call_started`, `component: litellm_client` — returns **457** over the same thirty days. The
branch is reachable only for a cloud deployment that declares an effort and has no cost-map record.
No such deployment is bound: `claude_sonnet` declares `high` (support `True`), `gpt-5.4-mini`
declares `none` (`True`), and the OVH entry declares nothing (F4). So the zero is expected, and the
branch's behaviour has been exercised only by tests.
**Arm 2 — liveness.** Same index pattern, same window, `event_type: model_call_started` → 457.
**Arm 3 — scope.** `agent-logs-*` on the production Elasticsearch, thirty days to 2026-09-06 11:13
UTC, and all time. ES counts are provisional (FRE-1051).

### F16 — The local effort curve: what `reasoning_effort` and `enable_thinking` do on llama-server

**Verdict:** POSITIVE for `enable_thinking`; for `reasoning_effort` the result is **no ordered
effect at n=2**, stated at that scope.

**Query.** Same five prompt shapes, n=2 sequential, `max_tokens 2500`, raw calls to Caddy `:8600`,
levels: `chat_template_kwargs.enable_thinking` false / true, then `reasoning_effort` `none`, `low`,
`medium`, `xhigh` with thinking left on. `(completion_tokens, reasoning_content chars)` per sample.

**Output:**

```
                  arith               fact                   puzzle              summary                  extract
enable false      (5,0)(5,0)          (3,0)(3,0)             (2,0)(2,0)          (117,0)(107,0)           (52,0)(52,0)
enable true       (65,118)(62,112)    (93,347)(211,730)      (70,256)(77,267)    (397,1336)(444,1605)     (147,329)(201,503)
effort none       (80,169)(66,141)    (297,961)(155,566)     (39,132)(56,214)    (372,1407)(215,657)      (180,477)(213,541)
effort low        (66,119)(68,120)    (92,367)(57,230)       (73,267)(76,267)    (174,316)(246,637)       (184,365)(151,287)
effort medium     (65,118)(66,120)    (75,268)(62,245)       (73,264)(70,261)    (281,733)(420,1398)      (292,800)(174,344)
effort xhigh      (72,164)(74,157)    (152,575)(213,684)     (56,210)(35,115)    (315,1067)(299,1103)     (475,1349)(477,1487)
wall (median ms)  enable false 468 / 404 / 470 / 5102 / 2174 · enable true 1679 / 4213 / 2032 / 11259 / 4480
correct           every level 8/8 on the scored shapes
```

`enable_thinking` is the lever: thinking off cuts the summary from 420 to 112 tokens and 11.3 s to
5.1 s. `reasoning_effort` is validated by the template (F1, F10) and then does nothing ordered on this
build: `none` still thinks (961 chars on one fact call), and `low`/`medium`/`xhigh` overlap on every
shape at this sample size. On OVH the same template family stops thinking at `none` (F3), so the
`none` handling sits in OVH's gateway, not in the template. The local dialect's thinking vocabulary
is `enable_thinking` alone; the effort vocabulary is accepted and inert until a larger sample says
otherwise.

### F17 — The catalog's local `context_length` no longer matches the server

**Verdict:** POSITIVE, one line.

`curl http://localhost:8600/v1/models` on 2026-09-06 returned `"context_length": 131072` for
`unsloth/qwen3.8-flash-next`. `config/models.yaml` at `56452c7b` declares `262144` on that entry.
FRE-1421's F1 read 262144 from the same endpoint on 2026-09-06 06:05, so the server changed after
that study. FRE-1426 D5 (boot reconciliation) is the mechanism that catches this. No ticket.

---

## Proposals

At most ten. Each names the finding it rests on and what it costs.

**P1 (Q5) — Declare a `dialect` on the provider, overridable per model; write `modes:` in it.**
A dialect is a named wire vocabulary with two parts: the thinking lever (`chat_template_kwargs.
enable_thinking` on the local server; `reasoning_effort {none, low, medium}` on OVH;
`reasoning_effort {none, low, medium, high, xhigh}` on OpenAI; `thinking.adaptive` +
`output_config.effort` on Sonnet 5; `thinking.enabled` + `budget_tokens` on Haiku 4.5) and the
accepted sampling set (F1). Five dialects cover every declared model today. FRE-1426 D3's `modes:`
stays, but a mode's body is written in the model's dialect and validated against it, and "a cloud
model declares none" is dropped — OVH declares `{thinking: {reasoning_effort: medium}, instruct:
{reasoning_effort: none}}`. The client builds its parameter block from the dialect, not from
placement: `_local_extra_body` becomes `_dialect_params`, and the two branches keep their transport
differences (streaming, timeouts, egress) and lose their parameter differences (F4). Cost: one enum
and a validator on `ProviderDefinition`/`ModelDefinition` (`models.py:92, 174`), ~60 lines in
`litellm_client.py`, `config_guard.check_reasoning_declaration` rewritten to validate against the
declared vocabulary instead of litellm's map (`config_guard.py:1107`), five catalog entries, and the
golden snapshot rebaselined. The `Literal` at `models.py:297` becomes per-dialect (F3).

**P2 (Q6) — The guard consults the declaration, and litellm's map only where litellm is the wire.**
For pass-through providers (OVH, any OpenAI-compatible endpoint) forward `reasoning_effort` with
`allowed_openai_params=["reasoning_effort"]` when the dialect declares the value, and let the
provider reject a bad one loudly (it does, F3). Consult `provider_reasoning_support` only for
SDK-mapped providers (Anthropic), where litellm's transformation is the wire and the map is the
source of the mapping (F5). Safe fallback: a dialect that declares the value and a litellm record that
is missing → forward; no dialect → today's omit-and-log. This retires the `False`-for-`ovhcloud`
branch that can never become right (F8).

**P3 (Q4) — Worker effort per dialect, from the curve (F13).** OVH `none`. OpenAI `none`. Sonnet 5
`low` under adaptive thinking (cheapest measured, correct 10/10). Haiku 4.5 thinking disabled — never
litellm's `low`, which is a 1 024-token budget at 4.4x the cost. Local `enable_thinking: false`.
FRE-1426 D6's "inherit at low effort" is wrong on three of five dialects and must name the dialect's
own cheap mode instead. The `none` puzzle miss (4/5) is the only quality signal in the data and is one
sample; a worker whose task needs reasoning is the planner's decision, not the default's.

**P4 (F3) — OVH's declared vocabulary is `{none, low, medium}`, default `xhigh` unrequestable.** Say
so on the catalog entry and in the dialect. A declaration of `high` or `xhigh` on that entry is a
400 or a 422 at call time. The current entry's comment says thinking "is disabled only through
`enable_thinking` inside `chat_template_kwargs`", which OVH rejects — correct it to `reasoning_effort:
none`.

**P5 (F2) — Rename the local wire key `repetition_penalty` → `repeat_penalty`.** One line in
`_local_extra_body` (`litellm_client.py:214`). Both local entries hold `1.0`, so behaviour does not
change; the declaration stops lying. Filed as **FRE-1438** (Backlog).

**P6 (Q2, Q3; F11, F12) — Decide reasoning retention explicitly, and change nothing on the wire.**
Within-turn retention is live on the local model by the template's own design for agentic loops, is
bounded by the prior round's output, and is under a tenth of real prompt growth. Keep it. Cross-turn
retention is off because persistence strips the key, and turning it on needs both the persisted
field and `chat_template_kwargs.preserve_thinking`. Keep it off. Correct the executor comment at
`executor.py:6370–6374`, which names a flag that does not exist on this path, and note in the ADR that
OVH drops our key and reads its own (F7): a history that carries thinking is dialect-specific too.

**P7 (F2) — `response_format: json_object` is not enforced on llama-server; make the planner tolerate it.**
`_validate_plan_json` (`expansion_controller.py:965`) calls `json.loads(raw)` on the raw content.
Fenced output fails it. `planner_failed` has fired four times all-time, two with
`reason: schema_validation_failed` on 2026-09-05, and this study cannot say whether fences caused them.
Strip a leading fence before parsing, and stop declaring `json_object` as a guarantee on the local
dialect. Small, verify first against those two events.

**P8 (F6, F5) — When FRE-1426 D2 makes the cloud branch read `model_def.temperature`, gate it by dialect.**
Sonnet 5 returns 400 for any `temperature`, `top_p` or `top_k`. Today no caller reaches it with one
(F4), by accident. A dialect-gated block is the only way D2's "read from the effective definition" is
safe on that model. The same gate keeps `top_p` off OpenAI when an effort above `none` is set.

**P9 (F1, F2) — Promote the probe harness to a repeatable instrument, run on catalog change, not at boot.**
Because llama-server accepts everything, the local dialect can never be validated by rejection; only
behaviour tells. The harness in M2 is ~500 lines, touches no substrate, and cost USD 0.20 for the
whole matrix. Put it under `scripts/eval/` with the FRE-375 guard and a one-page doc, and run it when a
model entry or a provider changes. FRE-1426 D5's boot reconciliation covers ids and windows (F17); it
cannot cover dialect.

---

## Filed tickets

- **FRE-1438** — `repetition_penalty` is inert on the local wire; llama.cpp reads `repeat_penalty`
  (Backlog; P5).

Comment posted (no ticket): **FRE-1398** — the F14 stall timeline, as a candidate trigger for the
failure class that ticket owns.

---

## Method appendix

**M1 — Deployed revision.** `git -C /opt/seshat log -1` → `56452c7b`. `git hash-object` on the repo
file against the same computation inside `cloud-sim-seshat-gateway` (`/app/...`): `models.yaml`
`71a03ae5`, `model_roles.yaml` `16481974`, `litellm_client.py` `5bdfb602`, `reasoning.py` `2500c16d`,
`model_loader.py` `45fbddc5` — all five equal. litellm `1.98.0` (`uv.lock`; the container's
`model_cost` held 3 817 entries at 10:47 UTC). Live settings read from the container:
`AGENT_CONVERSATION_MAX_HISTORY_MESSAGES=10`, `AGENT_SLM_BASE_URL=http://caddy:8600`,
`AGENT_SKILL_ROUTING_MODE=hybrid`. Local server identity from `/v1/models` at 10:47 and 11:26 UTC:
`unsloth/qwen3.8-flash-next`, llamacpp, port 8502, `context_length 131072`, `UD-IQ4_XS`.

**M2 — The harness.** One Python script (scratchpad, not committed; P9 proposes promoting it):
raw `httpx` calls per provider, a native Anthropic messages path, an offline
`litellm.utils.get_optional_params` shape probe, and five subcommands — `accept` (35 fields × 3
OpenAI-style providers, 18 × 2 native Anthropic), `sampling` (temperature / `top_p` / `top_k` /
`min_p` / `seed` at six samples, three penalties at three samples, `max_tokens`, `stop`,
`response_format`, `n`, `logprobs`), `effort` (five prompt shapes × levels × n), `preserve` (sixteen
history shapes at `max_tokens 1`), `litellm` (offline). Every call is appended to one JSONL log with
status, error body, content, reasoning key and length, usage, `timings`, wall clock and cost. OVH and
OpenAI sampling ran with `reasoning_effort: none`; local sampling ran with
`chat_template_kwargs.enable_thinking: false` — thinking consumed the 24-token budget before any
digit in the first attempt, which was discarded. The behavioural instrument for `presence_penalty`
is weak (a constant logit offset against a dominant token); the `frequency_penalty` row is what
proves the penalty channel is live.

**M3 — Stores and windows.** Providers: the live endpoints named in F1, credentials from
`/opt/seshat/.env`. Elasticsearch `agent-logs-*` on the production node, field `event_type` (not
`event`), `trace_id` in the dash-less form for the gateway's own events and the dashed form for the
event consumer's — both forms exist for one turn and a query on one misses the other. Postgres
`api_costs` and `sessions` on `cloud-sim-postgres`. Read-only throughout; nothing was written to any
substrate; no gateway turn was fired.

**M4 — Spend.** 948 calls: local 333 (free, including the 114 that failed during the F14 stall and
were repeated), OVH 228 (USD 0.061), OpenAI 204 (USD 0.037), Haiku 115 (USD 0.032), Sonnet 68
(USD 0.071). Total **USD 0.20** against the stated USD 5 cap. Costs are
computed from returned usage × the catalog's per-token prices.

**M5 — Rejected instruments.** llama-server's `/apply-template` and `/props` — Caddy's `:8600`
allowlist admits only `/health`, `/v1/chat/completions`, `/v1/models`, `/v1/embeddings`,
`/v1/rerank` (`config/cloud-sim/Caddyfile:206`), so the rendered prompt was measured through
`usage.prompt_tokens` and `timings.prompt_n` instead. The `json_schema` behavioural probe on the local
server was skipped on the rerun after F14. `stop_sequences` on Sonnet 5 was not behaviourally tested.
Reproduction of the F14 stall was deliberately not attempted on the owner's live server.

**M6 — Identifier resolutions.** `reasoning_declaration_undeliverable` → `litellm_client.py:954`.
`within_session_*` not used. `planner_failed` → `expansion_controller.py:495–510`, `reason` values
`schema_validation_failed` and `timeout`. `repeat_penalty` is llama-server's sampling key; the
catalog and `_local_extra_body` use `repetition_penalty`. OVH's response key is `reasoning`; litellm
normalises it to `reasoning_content` on read (`common_utils.py:1535`) and forwards our
`reasoning_content` history key unchanged, which OVH ignores.

**M7 — What the commission asked to attack, and what did not survive.** "Silent ignoring is the
dangerous half" — confirmed on the local server (six fields), refuted on every cloud provider except
one row (`presence_penalty` on OpenAI, where the instrument is weak). "Input tokens grow with
conversation depth through retained thinking" — refuted for hydrated turns (F12) and bounded within a
turn. "`low` above `medium`" — noise (F13). "The FRE-1007 guard blocks a parameter the provider
accepts" — confirmed, and the cause is provider-level in litellm's code, not a missing record (F8).
"A cloud model declares no modes" — refuted by three cloud dialects with modes (F1, F6).
