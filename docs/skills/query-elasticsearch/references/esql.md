# query-elasticsearch — ES|QL, for one-off analytics only

ES|QL (`/_query`) can chain `STATS ... BY ...` in one request. It has gotchas that the
`_search` DSL does not have. Use `_search` (the tool, or `references/curl-recipes.md`) first.

| Gotcha | Symptom | Fix |
|---|---|---|
| A time unit needs a plural and a space: `NOW() - 3 hours` | Parse error, or an empty result | Write `NOW() - 3 hours` |
| The column command is `KEEP`. `FIELDS` and `PROJECT` do not exist | Parse error | Use `KEEP` |
| A conditional count is `COUNT_IF`, not a ternary | Parse error | Use `COUNT_IF(...)` or a `_search` aggregation |
| Term equality on a `text` field (`message`, `error`) | An empty result, no error | Use `_search` with `match`, or `error.keyword` |

A command that was run live on 2026-10-11 and returned rows:

```bash
curl -fsS -X POST 'http://elasticsearch:9200/_query?format=json' \
  -H 'Content-Type: application/json' \
  -d '{"query": "FROM agent-logs-* | WHERE @timestamp > NOW() - 3 hours | STATS count=COUNT(*) BY level | SORT count DESC"}' \
  | jq '{columns: [.columns[].name], values}'
```

If `values` is empty, switch to `_search`. Do not try more ES|QL variants.
