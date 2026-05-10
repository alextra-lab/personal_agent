# Plan — FRE-353: Reconcile ES `agent-logs-*` index template with actual emit payloads

> Tier: **Tier-3:Haiku** · ADR-0068 D4 · Filed 2026-05-10
> Branch: `fre-353-es-template-reconcile` (new)
> PR: target `main`

---

## Context

**Why this exists.** ADR-0068 (FRE-258) audited agent self-telemetry and found that `docker/elasticsearch/index-template.json` declares mappings for fields that the code never emits (`input_tokens`, `output_tokens`, `model_name`, `tokens_used`), while the fields that *are* emitted (`prompt_tokens`, `completion_tokens`, `cache_read_tokens`, `cache_creation_input_tokens`, `model`, `model_id`, `endpoint`, `api_type`, `model_role`) get dynamically typed because nothing declares them. Dead declarations are misleading; uncoordinated dynamic typing risks type drift between indices on rollover.

**Why now.** PR #30 (FRE-351 cloud emit parity, FRE-352 step rename) just locked the canonical emit shape on `origin/main`. The template needs to follow.

**Outcome.** Template reflects ground truth. Dead fields gone. Token/latency/cost/cache fields explicitly typed so Kibana aggregations and ES|QL queries are stable across rolled-over indices.

---

## Prerequisite

Local `main` is 2 commits behind `origin/main` (PR #30 merge `0dfefaf` not pulled). Before branching:

```bash
cd /opt/seshat && git pull origin main
```

After pull, `HEAD` should be `0dfefaf` (PR #30 merge).

---

## Single file to modify

`docker/elasticsearch/index-template.json`

### Field inventory (verified against `origin/main`)

Canonical emit fields, by emit site:

| Field | Emit site | Type to declare |
|-------|-----------|-----------------|
| `model` | `litellm_client.py:404` (cloud, `litellm_request_complete`) | `keyword` |
| `model_id` | `client.py:455` (local, `model_call_completed`) | `keyword` |
| `endpoint` | both clients | `keyword` |
| `api_type` | local client only | `keyword` |
| `model_role` | `executor.py:1772` (`llm_step_completed`) | `keyword` |
| `prompt_tokens` | both clients | `long` |
| `completion_tokens` | both clients | `long` |
| `total_tokens` | both clients (cloud also writes legacy `tokens`) | `long` (already declared as `integer` — change) |
| `cache_read_tokens` | both clients | `long` |
| `cache_creation_input_tokens` | cloud client (cloud also writes legacy `cache_write_tokens`) | `long` |
| `latency_ms` | both clients (now int ms post-FRE-351) | `long` (already declared as `float` — change) |
| `cost_usd` | cloud client | `double` (already declared as `float` — change) |

Backward-compat double-writes from FRE-351 (keep as auto-typed, don't add to template — they're transitional and slated for deprecation):
- `tokens` (alias for `total_tokens`)
- `elapsed_s` (alias for `latency_ms / 1000`)
- `cache_write_tokens` (alias for `cache_creation_input_tokens`)

### Concrete changes

**Delete (dead declarations, lines 59–62):**
```json
"model_name":          { "type": "keyword" },
"tokens_used":         { "type": "integer" },
"input_tokens":        { "type": "integer" },
"output_tokens":       { "type": "integer" },
```

**Modify type (existing declarations with wrong type for new emits):**
- `total_tokens`: `integer` → `long`
- `latency_ms`: `float` → `long`
- `cost_usd`: `float` → `double`

**Add (currently dynamic, should be explicit):**
```json
"model":                       { "type": "keyword" },
"model_id":                    { "type": "keyword" },
"endpoint":                    { "type": "keyword" },
"api_type":                    { "type": "keyword" },
"prompt_tokens":               { "type": "long" },
"completion_tokens":           { "type": "long" },
"cache_read_tokens":           { "type": "long" },
"cache_creation_input_tokens": { "type": "long" }
```
(`model_role` is already declared as `keyword` at line 58 — leave it.)

**Note on existing data.** Type changes only apply to indices created *after* the template re-PUT. The current write index `agent-logs-000001` keeps its dynamic types. New rolled-over indices (next ILM rollover) pick up the new template. No data migration. Kibana may show a runtime field-type warning if querying across indices with different declared types — acceptable per ADR-0068 D4 ("dynamic:true catches runtime divergence; explicit declaration is for coordination, not safety").

---

## Verification

1. **Lint the JSON** (no functional check, just syntax):
   ```bash
   python -m json.tool docker/elasticsearch/index-template.json > /dev/null && echo OK
   ```

2. **Apply the template against running ES** (cloud-host or local):
   ```bash
   make up SERVICE=elasticsearch    # if not already running
   bash scripts/setup-elasticsearch.sh
   ```
   Expected: `✓ Index template created` with no errors.

3. **Inspect the registered template:**
   ```bash
   curl -s "${ES_URL:-http://localhost:9200}/_index_template/agent-logs-template?pretty" \
     | jq '.index_templates[0].index_template.template.mappings.properties
            | { model_name, tokens_used, input_tokens, output_tokens,
                model, model_id, endpoint, api_type,
                prompt_tokens, completion_tokens, total_tokens,
                cache_read_tokens, cache_creation_input_tokens,
                latency_ms, cost_usd }'
   ```
   Expected: dead fields are `null`; new fields show declared types.

4. **(Optional, smoke test on rollover index)** force a rollover and verify the new index inherits the template:
   ```bash
   curl -X POST "${ES_URL:-http://localhost:9200}/agent-logs/_rollover"
   curl -s "${ES_URL:-http://localhost:9200}/agent-logs-*/_mapping?pretty" \
     | jq 'to_entries | .[-1].value.mappings.properties.cache_creation_input_tokens'
   ```
   Expected: `{"type": "long"}` on the newest index.

5. **No code or test changes** — JSON-only edit; no pytest run required. (Pre-commit will still run `check_no_personal_paths.py` against the JSON.)

---

## Out of scope (explicitly)

- `tool_calls`, `fallback_used`, `role` — emitted but not requested by ticket; auto-typing is fine.
- Captain's Log template (`captains-index-template.json`) — separate template, outside D4 scope.
- Kibana index-pattern refresh — caller of dashboards (FRE-348/356 work) handles it.
- Backward-compat aliases (`tokens`, `elapsed_s`, `cache_write_tokens`) — transitional per FRE-351; declaring them would entrench the legacy names. Leave dynamic.
- Removing the legacy aliases in `litellm_client.py` itself — separate ticket if/when ADR-0068 D4 follow-up is filed.

---

## Rollback

JSON-only revert. If template fails to apply or breaks Kibana queries:
```bash
git revert <commit-sha>
bash scripts/setup-elasticsearch.sh   # re-PUT old template
```

---

## PR

Title: `fix(telemetry): FRE-353 reconcile ES agent-logs-* template with emit shape (ADR-0068 D4)`

Body (HEREDOC):
```
## Summary
- Delete 4 dead field declarations (model_name, tokens_used, input_tokens, output_tokens) — never emitted
- Add explicit typed mappings for actually-emitted fields: model, model_id, endpoint, api_type, prompt_tokens, completion_tokens, cache_read_tokens, cache_creation_input_tokens
- Fix type drift on 3 existing fields: total_tokens (integer→long), latency_ms (float→long), cost_usd (float→double)

## Test plan
- [ ] `python -m json.tool docker/elasticsearch/index-template.json` succeeds
- [ ] `bash scripts/setup-elasticsearch.sh` against cloud ES succeeds
- [ ] `curl …/_index_template/agent-logs-template` confirms dead fields gone, new fields declared
- [ ] (Optional) post-rollover index inherits new mapping
```

Refs ADR-0068 (D4), closes FRE-353.

---

## Fast-follow (FRE-356, separate plan/PR)

User said "All approved." After FRE-353 ships, FRE-356 (`docs/skills/self-telemetry.md`) is the next unblocked Tier-3 task — it can reference the now-explicit field types from this PR's template in its canonical-pattern recipes (e.g. `agg cache_read_tokens, agg cost_usd` typed correctly). FRE-356 will be planned separately.

FRE-265 (calendar gate ≥ 2026-05-12) and FRE-326 (gate ≥ 2026-05-13) remain calendar-blocked regardless of approval.
