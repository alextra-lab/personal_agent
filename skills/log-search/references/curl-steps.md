# log-search — Step 2: what the tool cannot do, with curl

| Need | Why the tool cannot | Use |
|---|---|---|
| A family that is not `agent-logs`, `agent-topology` or `agent-monitors-slm-health` (captures, reflections, insights, other monitors) | `index` is a fixed list | `_search` with curl: see `references/curl-recipes.md` |
| A field that the tool refuses and that exists | The allowlist is closed | Check the field first (below). Fields that hold conversation text are left out on purpose |
| More than 50 hits, oldest first, or a window over 30 days | The caps | `_search` with curl |
| ES\|QL | Not offered | `references/esql.md` |
| The list of indices, or the fields of an index | Not offered | The two commands below |

**Check a field before a curl aggregation.** Elasticsearch gives empty buckets, not an
error, for a field that does not exist. A field missing from `fields` in this reply does not
exist:

```bash
curl -fsS 'http://elasticsearch:9200/agent-logs-*/_field_caps?fields=event_type,level'
curl -fsS 'http://elasticsearch:9200/_cat/indices?format=json&h=index,docs.count'
```
