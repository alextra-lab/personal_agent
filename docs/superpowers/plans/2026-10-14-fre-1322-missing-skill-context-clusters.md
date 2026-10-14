# FRE-1322 — Missing-skill aggregation: group by the host the turn reached

**Ticket:** FRE-1322 · **Classification:** Standard (touches `src/` logic) · **Backing ADRs:** ADR-0066, ADR-0105, ADR-0156 (design intent only)

## Owner decisions (2026-10-14, recorded on the ticket)

- Grouping: turn context first. No model call. No embeddings.
- AC-4 surface: a Grafana panel on `agent-insights-*`. The owner flags `AGENT_INSIGHTS_ENABLED` and `AGENT_INSIGHTS_WIRING_ENABLED` stay `false`.

## Measured cause (AC-1)

- Bucketing key: exact `requested_name`. Emission: count >= 3 and distinct sessions >= 2 per bucket.
- `observability-fetcher` and `grafana-fetcher` are two singletons. Merged, they are 2 requests. Both the key and the bar bind.
- The turns that produced these names ran `curl` against `tempo:3200`. The key "host reached by bash in the same trace" groups them. Measured on all 152 events: 23 events reach a host. Three clusters clear 3 requests and 2 sessions: `tempo:3200`, `elasticsearch:9200`, `site.api.espn.com`. The recipe-site clusters have 1 session each and stay silent.

## Design

Add a second aggregation next to the exact-name one. Keep the exact-name path and its thresholds unchanged.

1. `telemetry/queries.py`
   - `extract_hosts(command: str) -> frozenset[str]`: pure. Reads `http(s)://host[:port]`.
   - `MissingSkillRequest`: frozen dataclass. Fields: `requested_name`, `session_id`, `trace_id`, `host`. `host` is one value or `None`.
   - `TelemetryQueries.get_missing_skill_requests(days, until=None)`:
     1. Clamp `until` to now. The window is `[until - days, until]`.
     2. Read `missing_skill_requested` events in the window, newest first, cap 2000. Log `missing_skill_requests_truncated` when the total exceeds the cap.
     3. Drop empty `trace_id` values before the tool-event query. Such requests keep `host=None` and stay on the name path.
     4. Read `bash_started` and `tool_call_started` events for the remaining `trace_id` values, in chunks of 20 traces, size 5000 per chunk. Log truncation per chunk.
     5. `host` is the dominant host of the trace: the host with the most `bash_started` commands. Ties break alphabetically. One request therefore counts in one cluster only.
     6. When the request has no `session_id`, take it from a tool event of the same trace.
   - `get_missing_skill_buckets` gains `until: datetime | None = None`. Default behaviour is unchanged.
2. `insights/engine.py`
   - `detect_missing_skill_patterns(days=7, until=None)`.
   - Cluster requests by host. A cluster fires at `MIN_MISSING_SKILL_REQUESTS` requests and `MIN_MISSING_SKILL_SESSIONS` sessions.
   - Cluster insight title has no counts: ``Create skill for `<host>` ``. The title feeds the fingerprint, so a stable title keeps the dedup stable.
   - Evidence: `service_host`, `request_count`, `distinct_sessions`, `window_days`, `requested_names`. `requested_names` is one comma-joined string, because `Insight.evidence` allows `float | int | str` only.
   - A name-bucket insight is dropped when its name belongs to a fired cluster. The cluster insight replaces it.
   - A failure in the new query must not remove name-bucket insights. Log `missing_skill_context_analysis_failed` and go on.
3. Grafana (AC-4)
   - Datasource `es-agent-insights` in `config/grafana/provisioning/datasources/datasources.yaml`: index `agent-insights-*`, time field `timestamp`.
   - One table panel on `config/grafana/dashboards/self_improvement_funnel.json`. Build it in the Grafana UI, then export. Never hand-author the JSON.

## Out of scope

- `emit_missing_skill_warnings` and the `read_skill` emitter. The null `session_id` is handled at query time.
- `.env` flags. Owner setting.

## Steps and verification

| # | Step | Verify |
|---|------|--------|
| 1 | Write failing tests for `extract_hosts` | `make test-file FILE=tests/test_telemetry/test_missing_skill_requests.py` fails with ImportError |
| 2 | Implement `extract_hosts`, `MissingSkillRequest`, `get_missing_skill_requests`, `until` | same file passes |
| 3 | Write failing engine tests (AC-2 shape, AC-3 negatives, subsume, query failure). Add an autouse fixture that mocks `get_missing_skill_requests` in every existing test of the file | `make test-file FILE=tests/personal_agent/insights/test_missing_skill_patterns.py` fails |
| 4 | Implement the cluster path in `detect_missing_skill_patterns` | same file passes, old tests included |
| 5 | Read-only replay of the 2026-08-29 window on production ES, `until=2026-08-29T06:06:25Z` | prints one insight for `tempo:3200`; replay of `until=2026-08-29T04:53:06Z` prints none |
| 6 | Add the datasource and the panel through the Grafana UI | panel shows one row per gap (4 on 2026-10-14) and agrees with a raw-document aggregation |
| 7 | Gates | `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files` |

## Acceptance criteria (the ticket's own)

| AC | Proof |
|----|-------|
| AC-1 | Comment on FRE-1322 (posted 2026-10-14): key, condition, which one excluded the events |
| AC-2 | Step 5 output: the real events produce a `tempo:3200` insight |
| AC-3 | Step 3 tests (single request, 2 requests, 3 requests in 1 session, no-host noise, a request with several hosts counts once, a missing `trace_id` stays on the name path) and the Step 5 pre-event window |
| AC-4 | Step 6: the panel and the datasource, named in the handoff |

## Risks

- A public host (for example `github.com`) can form a cluster. The data shows no such false cluster in 70 days. The bar of 3 requests and 2 sessions stays the guard.
- The new query adds 2 + ceil(traces / 20) ES reads per scan. The scan runs on consolidation, about 3 times a day. A 7-day window holds fewer than 200 requests.
- Existing unit tests build `InsightsEngine()` with a real `TelemetryQueries`. Step 3 must mock the new query in them, so no test reaches Elasticsearch (FRE-375).
