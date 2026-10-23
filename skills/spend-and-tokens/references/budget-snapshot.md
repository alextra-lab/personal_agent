# spend-and-tokens — Pattern 3b: the budget snapshot, with curl

```bash
curl -fsS -X POST 'http://elasticsearch:9200/agent-logs-*/_search?format=json' \
  -H 'Content-Type: application/json' \
  -d '{
    "size": 0,
    "query": {"bool": {"filter": [
      {"term": {"event_type": "budget_counter_snapshot"}},
      {"range": {"@timestamp": {"gte": "now-5m"}}}
    ]}},
    "aggs": {"by_window": {
      "terms": {"field": "time_window", "size": 5},
      "aggs": {"by_role": {
        "terms": {"field": "role", "size": 20},
        "aggs": {
          "running_total": {"max": {"field": "running_total"}},
          "cap_usd": {"max": {"field": "cap_usd"}},
          "utilization_ratio": {"max": {"field": "utilization_ratio"}}
        }
      }}
    }}
  }' | jq '.aggregations.by_window.buckets'
```

If the reply has no buckets, the last 5 minutes hold no snapshot. Widen `gte` to `now-30m` once.
