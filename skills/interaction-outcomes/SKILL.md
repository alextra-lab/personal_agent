---
name: interaction-outcomes
description: Reads how the agent's recent interactions ended and what its Captain's Log reflections record - the outcome of each interaction, the outcome counts of a window, and the patterns that reflections repeat. Uses curl on the captures and reflections indices, which the search_logs tool does not list. Use when the task asks how the last interactions ended, whether any failed, or which patterns the reflections repeat. Not for counts of log events, which the `log-search` skill gives, or for spend and tokens, which the `spend-and-tokens` skill gives.
metadata:
  requires-tools: bash
---

# interaction-outcomes — How interactions ended, and what the reflections say

## Rules

- Cite real rows from a live query. Never describe what the Captain's Log probably holds.
- Use curl on `elasticsearch:9200`. The `search_logs` tool does not list the captures and reflections families.
- The date field of the captures index is `timestamp`, not `@timestamp`. A range on `@timestamp` returns nothing and no error.
- Write a number and a time bound as a literal in the command. A shell variable or `$(...)` makes the command ask for approval.

## Step 1 — The last interactions, newest first

A capture holds `trace_id`, `outcome`, `total_tokens` and `timestamp`.

```bash
curl -fsS -X POST 'http://elasticsearch:9200/agent-captains-captures-*/_search?format=json' \
  -H 'Content-Type: application/json' \
  -d '{
    "size": 10,
    "sort": [{"timestamp": "desc"}],
    "_source": ["trace_id", "outcome", "total_tokens", "timestamp"]
  }' | jq '.hits.hits[]._source'
```

## Step 2 — Outcomes over a window

```bash
curl -fsS -X POST 'http://elasticsearch:9200/agent-captains-captures-*/_search?format=json' \
  -H 'Content-Type: application/json' \
  -d '{
    "size": 0,
    "query": {"range": {"timestamp": {"gte": "now-24h"}}},
    "aggs": {"by_outcome": {"terms": {"field": "outcome", "size": 10}}}
  }' | jq '{total: .hits.total.value, outcomes: .aggregations.by_outcome.buckets}'
```

## Step 3 — The patterns that reflections repeat

Reflections are in `agent-captains-reflections-*`. They hold `rationale`, `seen_count`,
`category` and `proposed_change_what`. Use the same `_search` form, with
`"sort": [{"seen_count": "desc"}]` and `"query": {"range": {"seen_count": {"gte": 2}}}`.
The date field of this family is `timestamp`.

## Success

Check the result before you answer:

- The answer names the time range of the rows, or the window of the counts.
- The buckets of an outcome count can sum to less than the total: a capture can hold no `outcome`. Say how many hold none.
- A reflection is quoted with its `seen_count`. A pattern that was seen once is not called a recurring pattern.

## Recovery

| Failure | Cause | Next step |
|---|---|---|
| A range on the captures index returns nothing | The range used `@timestamp` | Use `timestamp` |
| `{"hits": {"total": {"value": 0}}}` and the agent ran | The window is too short, or the index pattern is wrong | Widen the window once. Check the family name against the `log-search` skill |
| `could not resolve host: elasticsearch` | The command ran on the host shell | Run it through the `bash` tool, which runs in the gateway container |
| HTTP 400 with `parse_exception` | The request body is not valid JSON | Check the quotes and braces of the body |
| The same failure twice | — | Stop. Tell the user what failed and what was tried |
