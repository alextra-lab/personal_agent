# Plan: ES Mapping + Multi-Turn Correctness + Streaming Cleanup

**Date:** 2026-05-10
**Driver:** Findings from today's investigation session into healthcheck timeouts and the runaway loop after enabling preserve_thinking.

---

## Context

Today's investigation surfaced several **distinct, real bugs** stacked on top of each other. Two have been shipped (loop gate cross-turn accumulation; tool_call ID prefix). Several remain. Their effects compound: each is annoying alone but together they made the agent unable to complete a healthcheck.

Key findings still unaddressed:

1. **ES index mapping is wrong on every existing daily index.** Template was registered late; daily indices created before then have `text + .keyword` for fields the template declares as pure `keyword`. ES|QL `WHERE level == "ERROR"` silently returns null on every existing index. The agent retries broken queries, eats turns, exposes other bugs.
2. **Index template lacks `dynamic_templates`, compression codec, and a sane refresh interval** — undeclared string fields fall back to ES default (text+keyword), no compression, 1s refresh wastes IO for a write-heavy workload.
3. **`_validate_and_fix_conversation_roles` merges consecutive assistant messages even when separated by tool messages**, dropping the second assistant's `tool_calls`. This is the actual cause of the orphan-stripping cascade we saw — the ID-prefix fix (Bug 2) reduces but doesn't eliminate it because the second assistant's tool_calls are gone before the sanitiser ever sees them.
4. **LLM client doesn't stream.** `client.post()` blocks until the full response arrives. Behind Cloudflare, long generations (thinking + tool synthesis on a 35B local model) hit 524 timeouts. Streaming would keep the connection alive and eliminate today's headline failure.
5. **`preserve_thinking` re-attempt** — agent-side `reasoning_content` plumbing was reverted earlier; with Bug 3 fixed, the re-attempt becomes safer. Server flag was reverted by user.
6. **Skill doc `query-elasticsearch.md` examples are wrong** for the current mapping reality (and right for the intended mapping). After the ES fix, examples become correct as-is.

The user wants Elastic first because fixing the mapping eliminates most agent-driven retry storms — fewer turns means smaller blast radius for the remaining bugs.

**Outcome:** A healthcheck request reliably completes in ≤ 90 s with zero orphaned tool results, zero 524s, and accurate ES query results.

---

## Order Rationale

```
Phase 1 (ES) ──► reduces turn count ──► Phase 2 (merge bug) easier to validate
       │                                        │
       └──► makes skill-doc correct ─►          ▼
                                          Phase 3 (streaming) ──► Phase 4 (preserve_thinking)
                                                                          │
                                                                          ▼
                                                                  Phase 5 (init hardening)
```

ES first because:
- ~80% of today's failure cases trace back to ES|QL returning null
- After fix, the agent makes 3-4 tool calls per healthcheck instead of 14+
- Validating Phase 2 (merge bug) requires multi-turn flows that don't loop on broken queries
- Phase 3 (streaming) is independent but more confidently tested with a working data path

---

## Phase 1 — Elasticsearch infrastructure

**Goal:** Every field the agent queries returns the right type. Existing data is queryable. Storage shrinks. Skill doc examples work as written.

### 1.1 Patch `docker/elasticsearch/index-template.json`

- Add `index.codec: "best_compression"` and `index.refresh_interval: "5s"` to `settings`
- Add `dynamic_templates` array (before existing `properties`):
  - `*_id` strings → `keyword`
  - `*_type`, `*_name`, `*_role`, `*_status`, `*_decision`, `*_strategy`, `*_mode`, `*_phase`, `*_action` → `keyword`
  - `*_message`, `*_content`, `*_description`, `reason`, `hint`, `stderr`, `stdout`, `raw_*` → `text`
  - Default fallback for unmatched strings → `keyword` with `ignore_above: 1024` (NOT text+keyword — drops the wasteful default)

### 1.2 Re-register the template

Run `scripts/setup-elasticsearch.sh` against the running cluster. Idempotent today (PUT replaces). Confirm via `GET /_index_template/agent-logs-template` that `dynamic_templates` is present.

### 1.3 Reindex existing daily indices

Write a one-shot bash script `scripts/reindex-agent-logs.sh`:

```bash
for idx in $(curl -s 'http://localhost:9200/_cat/indices/agent-logs-*?h=index' | grep -v '\-v2$'); do
  curl -X PUT  "$idx-v2"
  curl -X POST "_reindex" -d '{"source":{"index":"'$idx'"},"dest":{"index":"'$idx-v2'"}}'
  curl -X DELETE "$idx"
  curl -X POST "_aliases" -d '{"actions":[{"add":{"index":"'$idx-v2'","alias":"'$idx'"}}]}'
done
```

This preserves the `agent-logs-YYYY.MM.DD` query name via alias, while the actual data lives in `-v2` indices created against the now-correct template. Total volume ~600 MB across ~30 indices; expect 2-5 minutes.

### 1.4 Verify

```bash
# Field type after reindex — should be pure keyword
curl 'http://localhost:9200/agent-logs-2026.05.10/_mapping/field/level'
# Should return: { ..., "level": { "mapping": { "level": { "type": "keyword" } } } }

# Skill-doc query works without .keyword suffix
curl -X POST 'http://localhost:9200/_query?format=json' \
  -d '{"query":"FROM agent-logs-* | WHERE level == \"ERROR\" AND @timestamp > NOW()-24hours | STATS c=COUNT(*)"}'
# Should return non-zero count
```

### 1.5 Update `docs/skills/query-elasticsearch.md`

After 1.3 lands, the existing examples become correct. Edits:

- Field type table (line 91-117): keep `keyword` declarations, add a one-line note "After 2026-05-10 reindex these are pure keyword in `agent-logs-*`. Older snapshots used `text + .keyword`."
- "Common mistakes" table: add a row "ES|QL term-equality on text field returns null silently — fixed by 2026-05-10 mapping cleanup. If you ever see `WHERE x == "Y"` returning null, check `_mapping/field/x`."

**Files modified:**
- `docker/elasticsearch/index-template.json`
- `scripts/reindex-agent-logs.sh` (new)
- `docs/skills/query-elasticsearch.md`

---

## Phase 2 — Conversation role merge fix (Bug 3)

**Goal:** Two assistant turns separated by tool messages stay distinct in the message history sent to the LLM. No tool_calls dropped.

### 2.1 Fix `_validate_and_fix_conversation_roles` (executor.py:279-369)

The current logic uses `last_non_tool_role` to detect duplicates. The bug: it doesn't reset when tool messages arrive between two assistants, so `assistant₂` after `[assistant₁, tool, tool, tool]` looks like a duplicate of `assistant₁` and gets merged in.

Fix: track `last_role_seen` (any role, including tool). Only treat as a duplicate when the IMMEDIATELY-prior message in `fixed` was the same user/assistant role with no tool messages in between. Tool messages reset the duplicate detector.

Concretely:
```python
# In the loop, when processing a user/assistant message:
prior_user_or_asst = next(
    (m for m in reversed(fixed) if m.get("role") in ("user", "assistant")),
    None,
)
prior_messages_after = (
    fixed[fixed.index(prior_user_or_asst) + 1:] if prior_user_or_asst else []
)
has_intervening_tool = any(m.get("role") == "tool" for m in prior_messages_after)

if prior_user_or_asst and prior_user_or_asst.get("role") == role and not has_intervening_tool:
    # Real duplicate (no tools between) — merge
    ...
else:
    # Either a different role, no prior, or tools in between — append normally
    fixed.append(msg)
    last_non_tool_role = role
```

When merging is the right answer (rare — only when the model emits two assistants back-to-back with no tool round between), still copy `tool_calls` from the merged-in message rather than discarding them.

### 2.2 Tests

In `tests/test_orchestrator/test_executor.py` add a test class `TestRoleValidatorMergeBug`:

- **`test_two_assistants_with_intervening_tools_not_merged`**: input is `[user, assistant₁{tool_calls=[A,B]}, tool_A, tool_B, assistant₂{tool_calls=[C]}]`. Output preserves both assistants and all three tool_calls. This is the regression test for today's bug.
- **`test_consecutive_assistants_no_tools_still_merge`**: input is `[user, assistant₁, assistant₂]`. Output merges them (preserves original behavior). Also assert that if `assistant₂` had tool_calls, they're preserved.
- **`test_role_alternation_failure_log_does_not_fire_for_tool_separated_assistants`**: ensure the final-validation `conversation_role_alternation_failed` log is NOT emitted for the valid pattern.

### 2.3 Verify

After Phase 2, send a healthcheck request through the PWA. Watch `history_sanitised` events:

- Pre-fix today: `orphaned_results_stripped` grows turn-by-turn (43, 48, …)
- Post-fix expected: `orphaned_results_stripped: 0` on every turn

Also: agent should converge in 2-4 LLM rounds because each round's tool results are visible on the next.

**Files modified:**
- `src/personal_agent/orchestrator/executor.py` (function `_validate_and_fix_conversation_roles`, lines 279-369)
- `tests/test_orchestrator/test_executor.py` (new test class)

---

## Phase 3 — Streaming LLM client

**Goal:** Eliminate Cloudflare 524s on long local-model generations. Connection stays alive byte-by-byte instead of waiting silently for the whole response.

### 3.1 Switch `_do_request` to streaming

Current code (`src/personal_agent/llm_client/client.py:389-392`):
```python
async with httpx.AsyncClient(timeout=timeout_config, ...) as client:
    response = await client.post(current_endpoint, json=payload, headers=cf_headers or None)
```

Change to chunked streaming with the OpenAI `stream: true` payload flag:

```python
payload["stream"] = True
async with httpx.AsyncClient(timeout=timeout_config, ...) as client:
    async with client.stream("POST", current_endpoint, json=payload, headers=cf_headers or None) as response:
        response.raise_for_status()
        chunks = []
        async for line in response.aiter_lines():
            if not line or line.startswith(":"):
                continue
            if line.startswith("data: "):
                data = line[6:]
                if data == "[DONE]":
                    break
                chunks.append(json.loads(data))
        response_data = _aggregate_streaming_chunks(chunks)
```

### 3.2 Add `_aggregate_streaming_chunks` helper

Reassembles the OpenAI streaming protocol into the same dict shape `adapt_chat_completions_response` already consumes:

- Concatenate `delta.content` into `message.content`
- Merge `delta.tool_calls` by index — concatenate `function.arguments` (which arrive as JSON-fragments), pick first `id` and `function.name`
- Take final `usage` block from the last chunk (vLLM/llama-server emit it when `stream_options: {"include_usage": true}` is set — add that to the payload)
- Reuse the existing `reasoning_content` extraction path; it handles the field whether content arrives in one shot or assembled from deltas

### 3.3 Tests

`tests/test_llm_client/test_streaming_aggregation.py` (new) — feed canned chunk sequences to `_aggregate_streaming_chunks` and assert the final shape matches what `adapt_chat_completions_response` expects. Cases:

- Plain text response (no tools)
- Single tool call
- Multiple parallel tool calls (chunks arrive interleaved)
- Mid-stream connection error (raises, does not silently truncate)

### 3.4 Verify

Send the healthcheck through PWA. Expect:
- `latency_ms` per call still in the 30-60 s range for hard prompts (model speed, not network)
- No 524 errors even on 60+ second generations
- `prompt_tokens` and `completion_tokens` correctly populated from the aggregated `usage`

**Files modified:**
- `src/personal_agent/llm_client/client.py`
- `src/personal_agent/llm_client/adapters.py` (add `_aggregate_streaming_chunks`)
- `tests/test_llm_client/test_streaming_aggregation.py` (new)

---

## Phase 4 — Re-attempt `preserve_thinking`

**Goal:** Qwen3.6's preserve_thinking template kwarg actually preserves prior-turn thinking, reducing repeated re-derivation of strategy on multi-turn tool flows. Safe now because Phase 2 keeps message history clean across turns.

### 4.1 Re-add agent-side `reasoning_content` plumbing

Reapply the change reverted earlier in `src/personal_agent/orchestrator/executor.py`:

```python
# At the assistant-append site (~line 1874)
assistant_message: dict[str, Any] = {"role": "assistant", "content": response_content}
if response.get("reasoning_trace"):
    assistant_message["reasoning_content"] = response["reasoning_trace"]
if response_tool_calls:
    assistant_message["tool_calls"] = _build_assistant_tool_calls(...)
```

Same change at the HYBRID expansion site (~line 1852). Tests already verified this is type-clean and behaves correctly when `reasoning_trace` is None (cloud paths, sub-agents).

### 4.2 Server-side flag

In `slm_server` config (separate repo), edit `config/models.yaml` `reasoning` block:
```yaml
chat_template_kwargs: {"enable_thinking": true, "preserve_thinking": true}
```
Restart the `reasoning` model's llama-server. The template at `config/templates/qwen3.6-unsloth.jinja` already supports `preserve_thinking` (verified earlier — it reads `message.reasoning_content` first, falls back to `<think>` tags in content).

### 4.3 Verify

Send a multi-turn tool-using request:
- First-call `prompt_tokens`: ~12,000
- Second-call `prompt_tokens`: should grow by tool-result size **plus** prior `<think>` blocks (typically +500 to +2,500 tokens)
- Second-call latency: should drop vs. baseline because the model isn't re-deriving its strategy

Without Phase 2 fix, Phase 4 caused a runaway loop. With Phase 2 in place, this should be a clean win.

**Files modified:**
- `src/personal_agent/orchestrator/executor.py` (reasoning_content plumbing, both append sites)
- `slm_server/config/models.yaml` (separate repo — coordinate with user)

---

## Phase 5 — Init hardening (best-effort, lower priority)

**Goal:** ES setup script runs as part of `make up` so a fresh environment never falls into today's "template added too late" trap.

### 5.1 Make `setup-elasticsearch.sh` idempotent

Today it uses `PUT` for templates (already idempotent) but the "Create initial index with alias" step at line 46+ fails on second run. Add `if curl -s -f -o /dev/null ...; then skip; else create; fi` guards.

### 5.2 Wire into `make up`

Add a service-dependent step in `docker-compose.cloud.yml` and `docker-compose.yml` — either an init container that runs `setup-elasticsearch.sh` after Elasticsearch becomes healthy and before `seshat-gateway` starts, or a post-`up` hook in the Makefile.

### 5.3 Verify

Run `make down -v` then `make up`. Confirm fresh template registration before any agent-logs index gets a doc.

**Files modified:**
- `scripts/setup-elasticsearch.sh`
- `docker-compose.yml`, `docker-compose.cloud.yml`
- `Makefile`

---

## Out of scope (track separately)

- **162 pre-existing mypy errors** — not from today's bugs, separate quality-gate FRE. Should be triaged and either fixed or ratcheted via CI baseline.
- **Loop gate `consecutive_count` threshold of 10** — even with parallel-batch fix, healthcheck-style fan-out at 10 different services is plausible. Tunable later; not blocking.
- **`tool_call_parser: "qwen3_coder"` on `standard` (sub_agent)** model — wrong parser for general 9B sub_agent. Verify tool calls still parse correctly; separate FRE.

---

## Critical files reference

| File | Phase | What changes |
|---|---|---|
| `docker/elasticsearch/index-template.json` | 1 | Add dynamic_templates, codec, refresh |
| `scripts/reindex-agent-logs.sh` | 1 | New one-shot script |
| `docs/skills/query-elasticsearch.md` | 1 | Update field-type note + common-mistakes |
| `src/personal_agent/orchestrator/executor.py` | 2, 4 | Fix `_validate_and_fix_conversation_roles`; re-add reasoning_content plumbing |
| `tests/test_orchestrator/test_executor.py` | 2 | New `TestRoleValidatorMergeBug` class |
| `src/personal_agent/llm_client/client.py` | 3 | Switch to httpx streaming |
| `src/personal_agent/llm_client/adapters.py` | 3 | Add `_aggregate_streaming_chunks` |
| `tests/test_llm_client/test_streaming_aggregation.py` | 3 | New test file |
| `slm_server/config/models.yaml` (separate repo) | 4 | Add preserve_thinking |
| `scripts/setup-elasticsearch.sh` | 5 | Idempotency guards |
| `docker-compose*.yml`, `Makefile` | 5 | Wire init step |

---

## End-to-end verification

After all phases:

1. **PWA healthcheck request**:
   - Total time ≤ 90 s
   - 3-4 LLM rounds max
   - Reply contains real status of Postgres / ES / Neo4j / Redis with the actual recipes from `infrastructure-health.md`
   - `orphaned_results_stripped: 0` on every `history_sanitised` log
   - No 524 errors in the entire trace
   - `routing_mode: model_decided` with Haiku selecting `infrastructure-health` (already working from earlier today)

2. **ES query smoke test**:
   - `WHERE level == "ERROR"` returns hits (not null)
   - `level.keyword == "ERROR"` ALSO returns hits (back-compat check on reindexed data)
   - Index sizes for reindexed daily snapshots are 15-25% smaller than originals

3. **Test suites**:
   - `make test` (fast unit suite) green
   - `make test-file FILE=tests/test_orchestrator/test_loop_gate.py` green
   - `make test-file FILE=tests/test_orchestrator/test_executor.py` green
   - `make test-file FILE=tests/test_llm_client/test_streaming_aggregation.py` green

4. **Quality gates**:
   - `uv run ruff check src/` clean
   - `uv run mypy src/` no NEW errors above today's 162 baseline (track separately)

5. **Captain's Log capture**: confirm a successful healthcheck request emits a capture with `outcome: success` and `total_tokens` reflects the saved tokens from preserve_thinking on the second call.
