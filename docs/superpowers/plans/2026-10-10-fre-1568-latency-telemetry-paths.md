# FRE-1568 — latency questions: skill content, a Tempo recipe, and `route_traces.latency_total_ms`

**Ticket:** FRE-1568 (Tier-2:Sonnet, `src/` logic touched: Codex plan review required).
**Design intent:** ADR-0129 (timing lives on OTel spans in Tempo). ADR-0156 names this ticket as
the telemetry content fix that its T6 conversion builds on (migration table, rows
`query-elasticsearch.md` and `query-tempo.md`).
**Follows:** FRE-1567 (`query_telemetry` reads Tempo). **Blocks:** FRE-1578 (ADR-0156 T6).
**Owner limit (2026-10-10, relayed by master):** finish this ticket as written, then no more
skill or tool work until ADR-0156's tickets run. No live test or live query runs on this ticket
until the owner allows it. This plan therefore proves every criterion offline and marks the live
ones as master's.

## 1. Facts read from the repo (no live query)

| Fact | Source |
|------|--------|
| `RouteTraceRow.latency_total_ms` is always `None` since FRE-1067 retired `RequestTimer` | `route_trace/assembler.py:266-270` |
| The old timer started at request receipt, before the gateway pipeline | `git show 45c9a4ba^:src/personal_agent/service/app.py:411` |
| A request-receipt clock already exists: a `ContextVar` set by `start_first_token_clock()` on both chat entry points | `observability/first_token.py`, `service/app.py:317`, `service/app.py:2172` |
| The seam writes the durable row in the request task, so it can read that clock | `observability/topology/seam.py:_write_durable_row` |
| Readers of the column: ES projection, Postgres ledger, three Grafana dashboards, four eval scripts, the `agent-topology` allowlist | `grep latency_total_ms` |
| `query-elasticsearch.md` names `duration_ms`/`latency_ms` in the key-field table and in two recipes (lines 187, 255, 488, 508) | file read |
| A `$(…)` or `docker exec` in a bash command needs approval; `docker exec` is not an allowlisted prefix | `bash_allowlist.py`, `config/governance/tools.yaml` |
| `query-tempo.md` tells the model to run `docker exec cloud-sim-seshat-gateway curl …`, and its Pattern 4 uses `$(date …)`: both miss the allowlist | file read |
| `agent-monitors-slm-health` has no `@timestamp`; its date field is `probed_at` | `docker/elasticsearch/monitors-slm-health-index-template.json`, `FAMILY_TIME_FIELD` |
| `docs/skills/self-telemetry.md` also carries `latency_ms`/`duration_ms` recipes and the keywords `latency`, `latency_ms`, `p95` | file read. **Out of this ticket's scope** (see section 5) |

## 2. Decisions

**D1 — restore `latency_total_ms`, do not remove it.**
Restore is one keyword argument and one seam line. Removal touches the Postgres schema, the ES
template, three Grafana dashboards (a dashboard is built in the Grafana UI, never hand-edited),
four eval scripts and the allowlist, and it discards 472 recorded values. The value is cheap to
get in-process, so the column stays.

* Source: the request-receipt clock, read when the seam writes the durable row. This is the same
  start point as the retired timer, so history stays comparable. The ticket says "from the root
  span": the root span starts at the same request receipt, and the span is still open at write
  time, so its duration is not readable in-process. The clock is the in-process equal.
* Meaning, written in the `RouteTraceRow` docstring: request receipt to the durable row write,
  turn-level row only. It excludes delivery after the write.
* A turn with no running clock (a path that never started it) keeps `None`. No second source is
  mixed in, so one column has one meaning.
* `latency_breakdown` stays `None`: the breakdown buckets came from `RequestTimer`, and the
  ticket does not ask for them.

**D2 — the `latency` recipe in `query-tempo.md` is a bash command that the allowlist accepts.**
* Literal `start` and `end` (Unix seconds). The model reads the clock with `date +%s` and
  `date -d '24 hours ago' +%s` as two plain commands, then writes the numbers. No `$(…)`.
* `curl -s -G … --data-urlencode …`, then `jq`. No `docker exec`: the bash tool runs in the
  gateway container, as the Elasticsearch recipes assume.
* Both caps are named and read back: `limit` (traces) and `spss` (spans per trace). The jq step
  prints `traces_returned`, `trace_limit`, `spans_cut` and `complete`, so a capped reply is not
  read as complete.
* The TraceQL is the shape FRE-1567 verified live: `{ span:name =~ "model_call .*" } |
  select(span.gen_ai.operation.name, span.gen_ai.request.model)`.

**D3 — `query-elasticsearch.md` loses the retired fields and the index history.**
* Remove `elapsed_ms / duration_ms` from the key-field table. Remove `duration_ms`, `latency_ms`
  from the two `_source` lists. Remove `duration_ms` from the captures `_source` list: the
  captures template maps it, but the ticket's defect and AC-1 name the field, and a recipe that
  returns a field no current event writes is the defect. (The captures field stays in the
  template. Only the recipe stops asking for it.)
* Add one line near the top: model-call timing is not in Elasticsearch. Use `query_telemetry`
  `latency` or the `query-tempo` recipe.
* Remove the keyword `latency`. Keep `p95` out too: both now point to Tempo. A user who asks
  about latency loads `query-tempo`.
* Cut the "monthly migration" paragraph and the shell-strip story to what a model needs today:
  never guess a date shape, run the tested classifier. Keep the classifier recipe and the
  `@timestamp` / `timestamp` note. State the size before (25,692 chars) and after in the PR.

**D4 — `query_telemetry` description gets one line about the time field** (master's note 2,
a fold-in of one sentence): `since` filters on `@timestamp`, except `agent-monitors-slm-health`,
which uses `probed_at`. The tool reads the value from `FAMILY_TIME_FIELD`, so the line cannot
drift.

**D5 — master's note 1 (`query_telemetry_tempo_started` absent from ES).**
Static read: the event is an `INFO` `log.info` call at `telemetry_query.py:1355`. The logger
sets no event-name filter and no level filter between structlog and the ES handler.
`query_telemetry_refused` is `WARNING`. The likely cause is FRE-1051 (the ES handler drops
events). A live count is needed to confirm, and no live query runs on this ticket. No code
change. Recorded in the handoff for master.

### 2.1 Changes from the Codex plan review (2026-10-10)

| Finding | Verified | Change |
|---------|----------|--------|
| High: `complete` ignored `completedJobs < totalJobs`, a missing `matched`, and a span with no valid `durationNanos` (the native summariser flags all three, `telemetry_query.py:1169-1203`) | yes | The jq step now flags all three. It prints `blocks_unread`, `sets_without_match`, `spans_without_duration` beside `spans_cut`, and `complete` is true only when every one is zero and the trace limit is not reached. New tests for each |
| Medium: routing counts distinct keyword hits and `self-telemetry` keeps `p95` and `latency` (`skills.py:383-400`), so "p95 latency" gave it two hits and `query-tempo` one | yes | Add keywords to `query-tempo`: `p95`, `p50`, `p90`, `percentile`, `latencies`, `response time`. Ties go to file order, and `query-tempo` sorts before `self-telemetry`. A test names the AC-4 sentence and asserts `query-tempo` outranks `self-telemetry` for it |
| Low: the test name claimed more than it proved; Pattern 1 and 3 start with `trace_id=…` (an assignment is not an allowlisted prefix) and Pattern 4 uses `$(date …)` | yes | Rewrite Patterns 1, 3, 4 with literal values. The test now runs **every** bash block of the skill through `check_segment_allowlist` |

Codex also confirmed D1's clock path (both chat entry points keep the `ContextVar` through the
seam) and reported no bash-allowlist problem for the `curl -s -G --data-urlencode | jq` shape.

## 3. Steps

1. **Failing tests first** (section 4), commit nothing yet.
2. `first_token.py`: add `request_elapsed_ms() -> float | None` (reads the `ContextVar`; `None`
   when no clock runs). Reuse `elapsed_ms`.
3. `assembler.py`: `assemble_route_trace(..., latency_total_ms: float | None = None)`. Replace the
   retirement comment and the `None` literal. Update the docstring `Args`.
4. `seam.py` `_write_durable_row`: pass `latency_total_ms=request_elapsed_ms()`.
5. `route_trace/types.py`: update the `latency_total_ms` docstring (meaning from D1).
6. `telemetry_query.py`: replace the "latency_total_ms is empty since 2026-08-08 (FRE-1568)"
   sentence (it becomes false), add the D4 time-field line.
7. `docs/skills/query-tempo.md`: add the recipe (D2), fix the `docker exec` lines, replace
   Pattern 4's `$(date …)` with the two-command form, correct the "Known limitations" line on
   boolean queries only if the recipe contradicts it (TraceQL does support `&&`).
8. `docs/skills/query-elasticsearch.md`: D3.
9. `docs/skills/seshat-observations.md` line 137 mentions the column: leave unchanged (the
   column still exists).
10. Gates: `make test` (one pytest at a time), `make mypy`, `make ruff-check`, `make ruff-format`,
    `pre-commit run --all-files`.

## 4. Tests

| Test file | Test | Shows |
|-----------|------|-------|
| `tests/personal_agent/orchestrator/test_latency_skill_paths.py` (new) | `test_elasticsearch_skill_names_no_retired_latency_field` — every line that holds `duration_ms`, `latency_ms`, `elapsed_ms` or `response_time_ms` also holds "retired" | AC-1 |
| same | `test_checker_flags_a_retired_field_line` — the same checker run on the old line `elapsed_ms / duration_ms` returns it | seeded negative for AC-1 |
| same | `test_elasticsearch_skill_sends_latency_to_tempo` — body names `query-tempo`; `latency` and `p95` are not keywords | scope 1 |
| same | `test_tempo_recipe_is_auto_approved` — each bash line of the recipe passes `check_segment_allowlist` with the real NORMAL list | scope 4 |
| same | `test_tempo_skill_has_no_unapproved_bash` — no fenced bash block in the skill holds `$(`, a backtick or `docker exec` | scope 4 |
| same | `test_tempo_recipe_jq_percentiles_and_completeness` — run the recipe's `jq` on a fixture reply (two roles, two models, known durations): p50/p90/max per group are exact; a fixture with `matched` greater than `spans` gives `complete: false`; a fixture with `limit` traces gives `complete: false` | AC-2, offline half |
| `tests/observability/route_trace/test_assembler.py` | extend: the new argument reaches the row; the default stays `None` | AC-3 |
| `tests/observability/topology/test_seam_latency.py` (new) | `_write_durable_row` under `start_first_token_clock(t0)` writes a row whose `latency_total_ms` is within bounds; with no clock it writes `None` | AC-3 |
| `tests/personal_agent/tools/test_telemetry_query_tempo.py` | extend: the description states `probed_at` for slm-health, and no longer says the column is empty | D4 |

`test_tempo_recipe_jq_percentiles_and_completeness` skips when `jq` is not installed. The CI
image has `jq` (checked in section 6).

## 5. Out of scope, surfaced

* **`docs/skills/self-telemetry.md`** has the same retired fields in its recipes and the keywords
  `latency`, `latency_ms`, `p95`. A latency question can still load it. ADR-0156 T6 (FRE-1578)
  converts it. This ticket does not edit it: the owner limited skill work to this ticket "as
  written", and the ticket names two skills. Master decides whether AC-4 needs this fold-in.
* Per-turn breakdown (`latency_breakdown`) stays empty.
* Workers reading Tempo through bash: the security decision stays the owner's.

## 6. Acceptance criteria — how each is shown

| AC | Evidence | Status |
|----|----------|--------|
| AC-1 no retired field recommended | `test_elasticsearch_skill_names_no_retired_latency_field` and its seeded negative | offline, in the PR |
| AC-2 the Tempo recipe runs | the offline jq test proves the filter and the cap flags. The live run (command and real output) needs a Tempo query | **open: master or owner must allow one read-only Tempo query** |
| AC-3 turn latency recorded | the assembler and seam tests. After deploy, `route_traces.latency_total_ms` is filled on new rows | code in the PR, live check master's |
| AC-4 live agent question | master runs it after deploy and after the owner lifts the test hold | master's |

## 7. Post-deploy runbook (master)

* Gateway rebuild class (a `src/` change). The FRE-1517 window is closed (FRE-1567 deployed).
* Verify: `SELECT count(*) FILTER (WHERE latency_total_ms IS NOT NULL), count(*) FROM
  route_traces WHERE created_at > now() - interval '1 hour' AND task_id IS NULL;` — expect the
  first number to equal the second for turns that came through chat.
* Verify: the `agent-topology` document of one new turn has `latency_total_ms` set.
