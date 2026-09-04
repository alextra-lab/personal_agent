# SLM server — client semantics for Qwen3.6-35B and Qwen3.8-Flash-Next

> **Source:** measured by the slm_server session against a live server on 2026-09-03/04.
> llama.cpp build 10770 (pinned, PR #28243), MTP depth 1, temperature 0.
> Relayed to Seshat 2026-09-04. **Measurements, not inference** — do not re-derive them
> from configuration, and do not extend them to a model or build they were not taken on.

This is the client-side contract for the local SLM. Seshat's own catalog decisions that
follow from it are in the last section; everything above it is the server's behaviour.

## Model IDs

| Model ID | Port | State |
|---|---|---|
| `unsloth/qwen3.8-flash-next` | 8502 | **enabled** — 3 slots, ctx 262144 |
| `unsloth/qwen3.6-35-A3B` | 8502 | disabled (shares the port) |
| `unsloth/qwen3.6-35-A3B-subagent` | 8503 | disabled |

Only one of the two 8502 entries can run at a time. `/v1/models` lists enabled entries
only — so an id absent from that listing is not necessarily gone, it may be the other
half of a shared port.

## 1. Thinking control — one portable form

```json
{"chat_template_kwargs": {"enable_thinking": false}}
```

Identical on both models, and the only reasoning parameter that is safe to send.

- **It must be nested.** A top-level `enable_thinking` is inert on both. It worked on
  mtplx, so code ported from there loses the setting silently.
- Absent or `true` means thinking is on. Only an explicit `false` disables it.
- Client kwargs **merge** over the server's launch kwargs — sending one key leaves the
  others intact. Verified on both chat templates.

## 2. `reasoning_effort` — do not send it

| Value | Qwen3.6 | Flash-Next |
|---|---|---|
| `low` / `medium` / `xhigh` | silently ignored | works |
| `high` | silently ignored | silently rewritten to `xhigh` |
| `none` | silently ignored | accepted, **does not disable thinking** |
| `"default"`, `"minimal"`, anything else | silently ignored | **HTTP 500** |

The same request succeeds on one model and 500s on the other, and OpenAI's own
`reasoning_effort: "default"` is among the values that fail. Both the top-level field
and `chat_template_kwargs.reasoning_effort` reach the template, as does the Responses
shape `reasoning: {effort: ...}`; all three carry the same risk.

Effort is **pinned server-side to `medium`**. Leave it there. If a per-request override
is ever unavoidable, send only `low`, `medium` or `xhigh`, and only to Flash-Next.

Measured on Flash-Next, 600-word essay: `low` 89 reasoning tokens, `medium` 177,
`xhigh` 3032. The template default when the key is absent is `xhigh`, so the server-side
pin is doing real work — this is the lever on reasoning depth, and it lives on the server.

## 3. `max_tokens` — the empty-response trap

Reasoning shares the budget with the answer. If the budget runs out during reasoning,
`content` is **empty** and `finish_reason` is `length`. Not a partial answer — nothing.

- Omitting `max_tokens` is safe; the backend default is `--n-predict 49152`.
- A client value overrides that in both directions. There is no server cap; the real
  ceiling is the context window.
- Thinking on, **Qwen3.6**: use >= 4000 (see the cache-state defect below).
- Thinking on, **Flash-Next at `medium`**: about 1077 tokens for a 600-word answer.
- Thinking off: no floor problem — truncation degrades gracefully into usable text.

Minimum reasoning spend even on trivial prompts:

| Model | "What is 2+2?" | "Capital of France?" |
|---|---|---|
| Qwen3.6 | 154 tokens | 143 |
| Flash-Next | 23 | 20 |

Qwen3.6 with `max_tokens: 32` returns empty. Flash-Next does not.

## 4. Response shape

`message.content` is the answer; `message.reasoning_content` is the reasoning, separate,
and empty or absent when thinking is off. Streaming delivers `delta.content` and
`delta.reasoning_content` as distinct fields — no tag parsing needed.

Time to first content on Flash-Next: 0.48 s thinking off, 4.53 s thinking on.

## 5. Tool calls

Work on both models, thinking on or off. `finish_reason: "tool_calls"`, arguments parse
as JSON. No special handling required.

## 6. Vision

Flash-Next is multimodal **and** MTP-accelerated at the same time: an image request
returned the correct answer with speculative decoding still active and decode speed
unchanged (41.5 vs 41.9 tok/s). Send images as `image_url` with a
`data:image/png;base64,...` URL.

Tested with one synthetic image only — validate against representative images before
relying on it.

## 7. Measured performance

Same binary, same prompts, temperature 0, MTP depth 1:

| Prompt | Qwen3.6 tokens / tok/s / wall | Flash-Next tokens / tok/s / wall |
|---|---|---|
| "What is 2+2?" | 158 / 86.9 / 1.97 s | 27 / 41.1 / **0.95 s** |
| "Capital of France?" | 153 / 77.1 / 2.09 s | 31 / 44.7 / **0.92 s** |
| Algebra word problem | 1585 / 81.8 / 19.57 s | 480 / 39.7 / **12.50 s** |
| 600-word essay | 2373 / 79.8 / **29.88 s** | 1104 / 37.2 / 29.99 s |

Two opposing effects: **per token Qwen3.6 is ~2.1x faster** (77-93 vs 36-45 tok/s), but
**per answer Flash-Next emits 2-6x fewer tokens**. Net — Flash-Next wins short prompts by
about 2x and ties on long-form. Agent-shaped traffic favours Flash-Next.

This is why turn wall-clock, not tok/s, is the screening metric for a local primary.

MTP depth (decode tok/s, 400 tokens, thinking off):

| Depth | Qwen3.6 | Flash-Next |
|---|---|---|
| off | 73.2 | 29.8 |
| **1** | **91.7** | **41.3** |
| 2 | 93.1 | 37.2 |
| 3 | 90.2 | 30.7 |

Depth 1 is the operating point; accept rates fall from 92% at depth 1 to 62% at depth 3.
Unsloth's README suggests depth 2 for Flash-Next — depth 1 measured better on this
hardware.

## 8. Known defects

**Qwen3.6 cache-state swing.** The identical request at temperature 0 returns either 1788
or 3330 reasoning tokens depending on which request preceded it. Repeating one request is
stable; an intervening different prompt flips it. This is why `max_tokens >= 4000` is
required with thinking on — the smaller branch fits in 2373 tokens, the larger needs 3846.
Observed on build 10621, not re-probed on 10770.

**Flash-Next 500s on an invalid `reasoning_effort`.** See section 2.

**Teardown assert in the pinned build.** `GGML_ASSERT([rsets->data count] == 0)` in
`ggml-metal-device.m` fires on process exit — after serving, not during inference. Likely
an artefact of the draft PR.

## 9. Client setup (litellm)

```python
litellm.completion(
    model="openai/unsloth/qwen3.8-flash-next",   # "openai/" prefix required
    api_base="http://127.0.0.1:8000/v1",          # MUST end in /v1, else 404
    api_key="none",
    messages=msgs,
    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
)
```

Without `/v1` litellm posts to `/chat/completions` and the router returns 404.
`drop_params=True` does **not** strip `chat_template_kwargs` (verified). Both `extra_body`
and a direct `chat_template_kwargs=` kwarg work; prefer `extra_body`.

The thinking flag should never be defaulted at a call site. A caller that forgets it gets
2168 tokens and 24 s instead of 985 and 11 s, with no error to signal it.

## 10. Inert configuration — ignore these fields

`reasoning_parser` and `tool_call_parser` in the **server's** `models.yaml` are emitted
only for MLX backends. `llama-server` has no such flags (it has `--reasoning-format`), and
neither appears on the running command line. The value `"qwen3"` on the Flash-Next entry
is misleading — its architecture is `qwen4exp` — but changing it has no effect, and
`qwen4` is not a valid value for anything. Reasoning splitting is done by the chat
template, extracted from the GGUF itself.

## 11. Server-side facts the client should not re-derive

- 3 slots, `--kv-unified`. Each slot reports the full 262144 context; the cache is
  **pooled, not partitioned**, so a single request can use the whole window. Do not divide
  `--ctx-size` by `--parallel`.
- Flash-Next needs the pinned build. Homebrew's llama.cpp fails with
  `unknown model architecture: 'qwen4exp'`.
- Metal ceiling on this machine is 107.5 GB of 128 GB (84%). Flash-Next uses 68.9 GB.

---

## What this means for Seshat's catalog

Consequences for `config/models.yaml` and `config/model_roles.yaml`, derived from the
above rather than restating it.

**`enable_thinking` is the only working client lever, and we use it correctly.**
`llm_client/adapters.py` emits it nested under `chat_template_kwargs`. As of ADR-0141 T2
(FRE-1365) the local path dispatches through litellm, whose SDK flattens `extra_body`, so
the key arrives top-level on the wire as required. Before T2 it was wrapped one level too
deep and inert — the defect that ADR-0141 exists to fix.

**We must never send `reasoning_effort` to a local deployment, and the guard enforces it.**
FRE-1007's `reasoning_vocabulary_mismatch` finding rejects `reasoning_effort` on any
local-placement deployment. Section 2 shows that rejection is not merely tidy: an invalid
value returns HTTP 500 on Flash-Next. Verified 2026-09-04 — both local deployments resolve
with `reasoning_effort=None`.

**`thinking_budget_tokens` is inert and the guard has no effective alternative for a
thinking-on local deployment.** Probed against Flash-Next 2026-09-04 (recorded on
FRE-1362): a declared `thinking_budget: 256` left about 924 tokens of reasoning untouched.
Meanwhile FRE-1007 requires every local role-bound deployment to declare either
`disable_thinking` or `thinking_budget_tokens`. For a thinking-**off** deployment that is
satisfiable and effective. For a thinking-**on** one the only accepted declaration does
nothing, so the guard is satisfied by a dead field. The real lever — `reasoning_effort` —
is pinned server-side and is exactly what the guard forbids sending. The guard has no
vocabulary for "reasoning depth is set at the server", and that gap should be closed
deliberately rather than papered over.

**The `max_tokens >= 4000` floor belongs to Qwen3.6, not the model we now run.**
Flash-Next needs about 1077 tokens for a 600-word answer at `medium` and spends 20-23
tokens of reasoning on trivial prompts, against Qwen3.6's 143-154. Our primary omits
`max_tokens` entirely (backend default 49152) and the instruct entry sets 2048 with
thinking hard-off, where section 3 says there is no floor problem. Both are safe. Anyone
citing "4000" against the current model is quoting the retired one.

**Context is 262144 per request, pooled.** Section 11 is why `context_length` is 262144 on
both flash-next entries and not `262144 / 3`. The binding constraint is aggregate KV across
concurrent requests, which a per-deployment integer cannot express.

## References

slm_server session measurements, 2026-09-03/04 · ADR-0141 (one dispatch path) ·
FRE-1365 (the cutover that made `enable_thinking` reach the wire) · FRE-1007 (the
reasoning-declaration guard) · FRE-1362 (the `thinking_budget` probe verdict) ·
FRE-1363 (the model A/B this data largely pre-answers)
