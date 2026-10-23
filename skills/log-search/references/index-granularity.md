# log-search — Classify the indices of one family

To classify a family, use the tested script:

```bash
curl -fsS 'http://elasticsearch:9200/_cat/indices?h=index' \
  | grep '^agent-monitors-joinability-' \
  | python3 skills/log-search/scripts/es_index_granularity.py agent-monitors-joinability
```

The script prints the daily and monthly counts, flags `MIXED`, and lists every name that it
does not recognise. It exits 1 if its input is empty, so a failed `curl` is never read as
"no indices".
