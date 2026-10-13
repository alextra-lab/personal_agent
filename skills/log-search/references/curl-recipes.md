# log-search — curl recipes for what the tool cannot do

Use these only after the `search_logs` tool has refused the request or cannot take it
(see Step 2 of `SKILL.md`). Spend recipes are in the `spend-and-tokens` skill. Captain's Log recipes are in the `interaction-outcomes` skill.

Rules for every recipe here:

- Use `curl -fsS`. The `-f` flag makes an HTTP error stop the command, so a pipe never reads an error page as data.
- A `POST` to `_search` or `_count` with a body runs without asking. Any other method or path asks.
- Check each field name with `_field_caps` first. A name that does not exist gives empty buckets and no error.
- Time uses date math: `now-1h`, `now-24h`, `now-7d`. No spaces, no plural.

## The `_search` template

A count by level, with a time bound on the family's own date field:

```bash
curl -fsS -X POST 'http://elasticsearch:9200/agent-logs-*/_search?format=json' \
  -H 'Content-Type: application/json' \
  -d '{
    "size": 0,
    "query": {"range": {"@timestamp": {"gte": "now-3h"}}},
    "aggs": {"by_level": {"terms": {"field": "level", "size": 10}}}
  }' | jq '.aggregations.by_level.buckets'
```

## All events of one trace, oldest first

The tool returns 50 events, newest first. This returns up to 200 in time order:

```bash
curl -fsS -X POST 'http://elasticsearch:9200/agent-logs-*/_search?format=json' \
  -H 'Content-Type: application/json' \
  -d '{
    "size": 200,
    "query": {"term": {"trace_id": "<trace_id>"}},
    "sort": [{"@timestamp": "asc"}],
    "_source": ["@timestamp", "event_type", "level", "role", "tool_name", "input_tokens"]
  }' | jq '.hits.hits[]._source'
```

`trace_id` is a plain `keyword`. A `trace_id.keyword` field does not exist and gives no hits.

## Recent error events, with the message text

```bash
curl -fsS -X POST 'http://elasticsearch:9200/agent-logs-*/_search?format=json' \
  -H 'Content-Type: application/json' \
  -d '{
    "size": 20,
    "query": {"bool": {"must": [
      {"term": {"level": "ERROR"}},
      {"range": {"@timestamp": {"gte": "now-3h"}}}
    ]}},
    "sort": [{"@timestamp": "desc"}],
    "_source": ["@timestamp", "event_type", "message", "tool_name", "trace_id", "error"]
  }' | jq '.hits.hits[]._source'
```

## Schema inspection

```bash
curl -fsS 'http://elasticsearch:9200/_cat/indices?format=json&h=index,docs.count,store.size' | jq .
curl -fsS 'http://elasticsearch:9200/agent-logs-*/_field_caps?fields=trace_id,error,message' | jq '.fields'
```

`_field_caps` lists the type of each field. A field that is absent from the reply does not exist.
