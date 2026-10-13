# model-latency — The curl recipe for model-call latency

Use this recipe only when the `measure_model_latency` tool cannot take the request: a span
attribute or a TraceQL filter that the tool does not list. The tool gives the same summary
for the role and model group keys and for the `model_call` and `slm_chat` spans.

### Pattern 5: Model-call latency by role and model (p50, p90, max)

Use this for "how slow are model calls", "p50 and p90 by role" and any latency over a window.
Elasticsearch holds no model-call timing. Tempo does: one `model_call <model>` span
per model call, with the role in `gen_ai.operation.name` and the model in
`gen_ai.request.model`. The `measure_model_latency` tool returns the same kind of summary. This is the bash path.

1. Read the clock: `date +%s` is the end, `date -d '24 hours ago' +%s` is the start. Tempo
   refuses a window over 168 hours.
2. Write both numbers as literals in `start` and `end`. A `$(…)` makes the command ask for approval.
3. Keep `limit` (traces) and `--argjson limit` equal. `spss` is the spans returned per trace.

```bash
date +%s
date -d '24 hours ago' +%s
curl -s -G 'http://tempo:3200/api/search' \
  --data-urlencode 'q={ span:name =~ "model_call .*" } | select(span.gen_ai.operation.name, span.gen_ai.request.model)' \
  --data-urlencode 'start=1788000000' \
  --data-urlencode 'end=1788086400' \
  --data-urlencode 'limit=500' \
  --data-urlencode 'spss=100' \
| jq --argjson limit 500 'def pct($p): sort | .[((($p * length) | ceil) - 1)];
def num: if type == "number" then . elif type == "string" then (tonumber? // null) else null end;
def attr($k): ([.attributes[]? | select(.key == $k) | .value.stringValue] | first) // "unknown";
[.traces[]? | (.spanSets // [.spanSet])[]? | select(. != null)] as $sets
| (.metrics.totalJobs | num) as $total
| (if $total != null then ((.metrics.completedJobs | num) // 0) else null end) as $done
| [$sets[] | .spans[]? | select((.durationNanos | num) != null)] as $spans
| (.traces | length) as $returned
| ([.traces[]? | select((.spanSets // .spanSet) == null)] | length) as $noset
| ([$sets[] | select((.matched | num) != null) | ((.matched | num) - ((.spans // []) | length)) | select(. > 0)] | add // 0) as $cut
| ([$sets[] | select((.matched | num) == null)] | length) as $nomatch
| ([$sets[] | .spans[]? | select((.durationNanos | num) == null)] | length) as $nodur
| (if $done != null and $total != null and $done < $total then $total - $done else 0 end) as $unread
| {
    traces_returned: $returned,
    trace_limit: $limit,
    spans_used: ($spans | length),
    spans_cut: $cut,
    blocks_unread: $unread,
    sets_without_match: $nomatch,
    spans_without_duration: $nodur,
    traces_without_span_set: $noset,
    complete: ($returned < $limit and $cut == 0 and $unread == 0 and $nomatch == 0 and $nodur == 0 and $noset == 0),
    groups: ($spans | group_by([attr("gen_ai.operation.name"), attr("gen_ai.request.model")])
      | map({
          role: (.[0] | attr("gen_ai.operation.name")),
          model: (.[0] | attr("gen_ai.request.model")),
          spans: length,
          p50_ms: (map((.durationNanos | num) / 1000000) | pct(0.5)),
          p90_ms: (map((.durationNanos | num) / 1000000) | pct(0.9)),
          max_ms: (map((.durationNanos | num) / 1000000) | max)
        })
      | sort_by(-.spans))
  }'
```

**Read `complete` before you report a number.** It is `false` when any of these holds:

| Field | Meaning when not zero (or `traces_returned` equals `trace_limit`) |
|-------|------------------------------------------------------------------|
| `traces_returned` = `trace_limit` | The `limit` cut the search. More traces can match. Narrow the window or add a filter to the TraceQL. |
| `spans_cut` | Tempo matched more spans in a trace than `spss` returned. |
| `blocks_unread` | Tempo stopped before it read every block. |
| `sets_without_match` | A result had no match count, so its completeness is unknown. |
| `spans_without_duration` | A span had no duration. It is left out of the percentiles. |
| `traces_without_span_set` | A trace came back with no span set. Its spans are not counted. |

When `complete` is `false`, say the percentiles come from a sample and name the field. An empty
`groups` means no model-call span matched in the window. It does not mean zero latency.
Percentiles are nearest-rank over `spans_used`. A role or model with few spans gives a
coarse p90.
