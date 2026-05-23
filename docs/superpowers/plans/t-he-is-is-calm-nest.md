# FRE-376 Phase 5 — Joinability Probe & 7-Day Green Gate

**Linear:** FRE-376 (final phase) · **ADR:** ADR-0074 (Proposed → Accepted on gate-green)
**Date:** 2026-05-23 · **Tier:** Sonnet-implementable from this plan

---

## Context

ADR-0074 ("End-to-End Traceability") established six identity invariants spanning Postgres, Elasticsearch, Neo4j, and Redis. Phases 1–4 shipped: schema, dual-path model telemetry, Cypher MERGE identity, and `TraceContext` becoming non-optional on internal APIs. The remaining work — Phase 5 — is the **runtime counterpart to the static lint**: a probe that picks a real session and verifies that the substrates actually agree on it.

The deliverable closes the ADR loop. Without it, the invariants are *asserted* but not *observed*: a future regression that breaks joinability (e.g. a code path that silently drops `originating_trace_id` on a Cypher MERGE) would survive `make mypy`, `make test`, and the AST lint, because none of those touch live substrate data.

Phase 5 also surfaces the explicit signal that flips ADR-0074 Proposed → Accepted and FRE-376 In Progress → Done.

A sibling slice — Captain's Log `CaptureEntry` token-field canonicalization (`prompt_tokens`/`completion_tokens` → `input_tokens`/`output_tokens`) — was deferred from Phase 2/3 by design and is filed here as a **new Linear ticket** rather than implemented.

---

## Goals & non-goals

**Ship:**
- `scripts/monitors/joinability_probe.py` — one-shot CLI invoking a shared walk library.
- `src/personal_agent/observability/joinability/` — `walk.py`, `result.py`, `sampling.py`, `sink.py`, `status.py`.
- ES index `agent-monitors-joinability-*` with template + ILM policy.
- Brainstem scheduler entry (hourly, `SystemTraceContext.new("joinability_probe")`).
- `make joinability-status` — 7-day gate query, exit-code-driven.
- Unit + integration tests; CI smoke target.
- Sibling Linear FRE filed (`Needs Approval`) in same PR — Captain's Log token canonicalization.

**Defer / out of scope:**
- Probe-driven self-healing (auto-quarantine, auto-file tickets on red). Probe **reports**, does not remediate.
- Sidecar container (docker-compose service). Brainstem-hosted is sufficient; brainstem outage produces a gap in `agent-monitors-joinability-*`, which the 7-day gate aggregation already treats as failure.
- Backfilling/probing pre-cutoff (pre-Phase-1) sessions.
- Pre-commit lint and AST contract test — already shipped in Phase 3.

---

## Architecture

```
scripts/monitors/joinability_probe.py    # thin CLI (argparse → walk)
src/personal_agent/observability/
  joinability/
    walk.py        # JoinabilityWalk: cross-substrate walk algorithm
    result.py      # ResultDoc, SubstrateCheck, Orphan (Pydantic)
    sampling.py    # deterministic session selection (seeded)
    sink.py        # write ResultDoc → ES (and stdout for --dry-run)
    status.py      # compute_seven_day_gate() ES aggregation
```

**CLI flags** (joinability_probe.py):

```
python -m scripts.monitors.joinability_probe \
    [--window-hours 24]    [--seed UINT]         [--session-id UUID]
    [--write-es / --no-write-es]   [--dry-run]   [--source NAME]
    [--fail-on yellow|red|never]
```

**Exit codes:** `0`=green, `1`=yellow, `2`=red, `3`=skipped (no eligible sessions), `64`=usage.

**Scheduling — brainstem only** (decision rationale §Risks below). Add to `BrainstemScheduler._lifecycle_loop` mirroring the disk-check pattern at `src/personal_agent/brainstem/scheduler.py:432-436`:

```python
if self.joinability_probe_enabled and (
    self._last_joinability_probe_run is None
    or (now - self._last_joinability_probe_run).total_seconds() >= self.joinability_probe_interval_seconds
):
    try:
        from personal_agent.observability.joinability.walk import run_scheduled_probe
        await run_scheduled_probe(es_client=self._lifecycle_es_client)
        self._last_joinability_probe_run = now
    except Exception as probe_err:
        log.warning("joinability_probe_failed", error=str(probe_err), exc_info=True, trace_id=iteration_trace_id)
```

**Settings** (`config/settings.py`, near `quality_monitor_*` block):
- `joinability_probe_enabled: bool = True`
- `joinability_probe_interval_seconds: int = 3600`
- `joinability_probe_window_hours: int = 24`
- `joinability_probe_index_prefix: str = "agent-monitors-joinability"`

Settings accessed via `from personal_agent.config import settings` — never `os.getenv`.

---

## Joinability walk algorithm

**Sampling:** seed = `int(started_at.timestamp())` rounded to hour; `eligible = SELECT id FROM sessions WHERE created_at BETWEEN now()-window AND now()-5min ORDER BY id`. The 5-minute trailing window avoids ES eventual-consistency false positives on hot writes. `random.Random(seed).choice(eligible)`. The reproducer command (with explicit `--seed`) is logged on every run.

**Substrate order** (cheap → expensive, each substrate produces independent `SubstrateCheck`s):

| # | Substrate | Assertion |
|---|---|---|
| 1 | PG `sessions` (anchor) | row exists; `primary_model_at_creation` + `model_config_path` non-null (§I3); every assistant `messages[]` entry has `model`, `model_role`, `model_config_path` |
| 2 | PG `api_costs` | every row: `trace_id` + `session_id` non-null (§I4); collect distinct trace_ids |
| 3 | PG `metrics` | every row's `trace_id` ∈ collected trace_ids |
| 4 | PG `captains_log_captures` + `_reflections` | trace_id non-null; reflection.trace_id ∈ captures |
| 5 | PG `consolidation_attempts` | conditional — see expected-absence rules |
| 6 | PG `budget_reservations`, `artifacts` | conditional |
| 7 | ES `agent-logs-*` | for each trace_id: at least one `model_call_completed` event (§I2 dual-path); every event with this trace_id carries session_id (§I1) |
| 8 | ES `agent-captains-captures-*` / `-reflections-*` | doc_id=trace_id reconciles three-way: PG row ↔ ES doc ↔ FS file at `telemetry/captains_log/captures/<date>/<trace_id>.json` |
| 9 | Neo4j `(:Turn)` | `originating_session_id` + `originating_trace_id` populated for every turn in this session |
| 10 | Neo4j `(:Entity)` / `(:Relationship)` | `originating_trace_id` + `originating_session_id` populated; Entity has `extractor_model` |
| 11 | Redis streams (best-effort) | `XRANGE` recent entries on `stream:request.captured`, `.completed`, `consolidation.completed`, `memory.accessed`; tolerate absence (MAXLEN trim) |

**Orphan vs expected-absence rules** — codified in `SubstrateCheck.expected ∈ {"required", "conditional", "absent_ok"}`:
- **required + missing** → red orphan
- **conditional + missing** → green (it's allowed); yellow only if the trigger fired (e.g. ≥50 captures observed but `consolidation_attempts` empty)
- **conditional + present** → green
- **absent_ok** → green either way

**Outcome aggregation:** `max(severity)` across all checks where green=0, yellow=1, red=2.

---

## Result schema

ES index `agent-monitors-joinability-YYYY.MM.DD` (daily rollover, mirrors `agent-logs-*` naming). One doc per probe run:

```jsonc
{
  "run_id": "<uuid>",
  "started_at": "2026-05-23T14:00:00Z",
  "duration_ms": 482.3,
  "source": "scheduler",                   // scheduler | cli | ci | manual
  "window_hours": 24,
  "random_seed": 1748016000,
  "sampled_session_id": "<uuid>",
  "sampled_trace_ids": ["<uuid>", "..."],
  "outcome": "green",                      // green | yellow | red | skipped
  "trace_id": "<probe-run-trace>",         // probe is itself joinable
  "kind": "system:joinability_probe",
  "substrate_checks": [                    // nested type
    { "substrate": "postgres.api_costs", "expected": "required",
      "observed_count": 3, "status": "green", "duration_ms": 12.4 },
    ...
  ],
  "orphans": []                            // nested type; empty when green
}
```

Template + ILM policy at `docker/elasticsearch/monitors-joinability-index-template.json` + `monitors-joinability-ilm-policy.json` (hot 7d → warm 30d → delete 180d — cheaper than agent-logs since ~24 docs/day). Both PUT via extending `scripts/setup-elasticsearch.sh` with two `put_resource` calls.

**Why ES, not Postgres:** Kibana already operates on `agent-*`; ILM/archival handlers reusable; daterange aggregation native. New PG table would require migration + new query layer + new dashboard surface.

---

## 7-day green gate

ES aggregation (`status.py::compute_seven_day_gate()`):

```jsonc
GET agent-monitors-joinability-*/_search
{
  "size": 0,
  "query": { "range": { "started_at": { "gte": "now-7d/d", "lte": "now/d" } } },
  "aggs": {
    "by_day": {
      "date_histogram": { "field": "started_at", "calendar_interval": "1d", "min_doc_count": 0 },
      "aggs": {
        "worst_outcome": { "scripted_metric": { ... } },   // pseudocode in status.py
        "runs_count":   { "value_count": { "field": "run_id" } }
      }
    }
  }
}
```

**Definition of GREEN:** 7 daily buckets present, every bucket's `worst_outcome == "green"`, every bucket's `runs_count ≥ 12` (tolerates a few missed scheduler ticks). `outcome == "skipped"` is a no-op in the aggregation.

**Surface:**
- `make joinability-status` — prints 7-line ASCII table, exit 0 on green / 1 otherwise. Backed by `status.py::compute_seven_day_gate()`.
- `config/kibana/joinability-monitor-dashboard.ndjson` — daily outcome histogram, top orphan substrates over 30d, p50/p95 walk duration. Imported via existing Kibana import flow.
- Linear comment automation — **deferred**. Single human-driven check is sufficient to flip the ADR.

**Flip signal (AC-9):** `make joinability-status` exits 0 with 7 green buckets ≥12 runs/day → post comment on FRE-376 with the table output → edit `ADR-0074 Status: Proposed → Accepted` in same commit → close FRE-376.

---

## Files to create / modify

**Create:**
- `scripts/monitors/__init__.py`, `scripts/monitors/joinability_probe.py`
- `src/personal_agent/observability/__init__.py`
- `src/personal_agent/observability/joinability/{__init__,walk,result,sampling,sink,status}.py`
- `docker/elasticsearch/monitors-joinability-index-template.json`
- `docker/elasticsearch/monitors-joinability-ilm-policy.json`
- `config/kibana/joinability-monitor-dashboard.ndjson`
- `tests/observability/{test_joinability_walk_unit,test_joinability_status}.py`
- `tests/integration/test_joinability_walk.py`
- `tests/scripts/test_joinability_probe.py`

**Modify:**
- `src/personal_agent/brainstem/scheduler.py` — `_last_joinability_probe_run`, lifecycle-loop branch (mirror disk-check at line 432).
- `src/personal_agent/config/settings.py` — 4 new settings.
- `scripts/setup-elasticsearch.sh` — 2 new `put_resource` calls.
- `Makefile` — `joinability-status` target.
- `docs/architecture_decisions/ADR-0074-end-to-end-traceability.md` — flip `Status: Proposed → Accepted` (separate commit, post-gate-green).

---

## Test strategy

| Layer | Coverage |
|---|---|
| **Unit (mocked clients)** `tests/observability/test_joinability_walk_unit.py` | green path, red path (NULL trace_id orphan), red path (ES/PG/FS three-way mismatch), yellow path (Neo4j connection error → one yellow check, others green), expected-absence (no consolidation, no entities), reproducibility (same seed → same session), `ResultDoc.model_dump_json()` round-trip |
| **Unit** `tests/observability/test_joinability_status.py` | 7 green ≥12 → green; 6 buckets (gap) → yellow with `reason=gap_<date>`; 7 buckets, one red → red with `reason=red_on_<date>`; skipped buckets ignored |
| **Integration** `tests/integration/test_joinability_walk.py` — `@pytest.mark.requires_test_infra` | Against `make test-infra-up` (PG :5433, ES :9201, Neo4j :7688). Seed happy-path session → green. Mutate `UPDATE api_costs SET trace_id=NULL` → red with `kind="missing_identity"`. Two captures pointing at one Entity → assert first-write-wins on `originating_trace_id`. Per `tests/conftest.py:18-26`, `APP_ENV=test` redirects substrates automatically. |
| **CI smoke** `make test-joinability-ci` | `python -m scripts.monitors.joinability_probe --dry-run --fail-on yellow --source ci --session-id <seeded>` |
| **Prod-only** | 7-day gate itself; cross-replica observability — not testable pre-merge |

Per FRE-375: tests never write to prod substrates. Pre-commit hook `scripts/check_no_direct_substrate_in_tests.py` enforces.

---

## Acceptance criteria

Per `[[feedback_plans_acceptance_criteria]]` — pre-merge, post-deploy (same session), and 7-day-gate items all listed.

| AC | Phase | Description |
|---|---|---|
| **AC-1** | pre-merge | `tests/observability/test_joinability_walk_unit.py` + `test_joinability_status.py` pass; `make mypy` + `make ruff-check` clean |
| **AC-2** | pre-merge | Integration test under `make test-infra-up`: seeded session → green; mutated row → red with correct orphan kind |
| **AC-3** | pre-merge | ES index template + ILM policy validated via `scripts/setup-elasticsearch.sh --dry-run` |
| **AC-4** | pre-merge | Sibling FRE filed in Linear (`Needs Approval`, label `PersonalAgent`, Tier-2:Sonnet) — title and body per §Sibling FRE below; link in PR description |
| **AC-5** | post-deploy, same session | Deploy `docker-compose.cloud.yml` to VPS via `make deploy`. Drive one authenticated chat turn. Wait ≤60s. Verify one ES doc in `agent-monitors-joinability-*` with `outcome=green`, sampled session_id resolves, all expected substrate checks present, zero orphans |
| **AC-6** | post-deploy, same session | `make joinability-status` returns exit 0 OR exit 1 with `reason=insufficient_history` (both acceptable on day 0 — the probe wrote, the query reads it back) |
| **AC-7** | post-deploy, same session | Force red: `UPDATE api_costs SET trace_id = NULL WHERE id = $1` against test-substrate (not prod). Run probe with `--session-id`. Verify `outcome=red`, `kind="missing_identity"`. Restore row |
| **AC-8** | post-deploy, same session | Kibana dashboard `joinability-monitor` imports cleanly and renders at least one bucket |
| **AC-9 (FLIP)** | 7-day gate (deferred) | `make joinability-status` exit 0 with 7 daily buckets, each `worst_outcome=green`, each ≥12 runs. Post output as comment on FRE-376. Edit `ADR-0074 Status: Proposed → Accepted` in same commit. FRE-376 → Done. MASTER_PLAN.md updated per `[[feedback_update_master_plan]]` |

---

## Sibling FRE outline (filed during Phase 5 PR)

**Title:** Canonicalize `TaskCapture` token fields to `input_tokens`/`output_tokens` (ADR-0074 §I2 follow-up)

**Summary:** After FRE-376 Phase 2, canonical event-bus payloads use `input_tokens`/`output_tokens` per ADR-0074 §I2. `TaskCapture` (`src/personal_agent/captains_log/capture.py:61-63`) still writes `prompt_tokens`/`completion_tokens`/`total_tokens`. The joinability probe shipped in Phase 5 checks identity tuples, not field names — but the drift means a single trace has different token field names depending on which substrate is queried. This FRE makes `TaskCapture` schema match the canonical event shape with Pydantic `AliasChoices` for back-compat on legacy on-disk JSONs.

**Scope (in):**
- Field rename in `TaskCapture` with `Field(validation_alias=AliasChoices("prompt_tokens", "input_tokens"))` etc.
- ES `agent-captains-captures-*` template gets new fields without removing old (read-tolerant).
- Consumer updates: `second_brain/consolidator.py`, `second_brain/session_summary.py`.
- Tests for legacy + canonical replay via `backfill.py` (idempotent via `doc_id=trace_id`).

**Scope (out):**
- Bulk-rewriting on-disk legacy JSONs (use aliases instead — mirrors `[[project_nil_uuid_pattern_for_invariant_tightening]]`).
- New substrates for CaptureEntry.
- Multi-user attribution (already FRE-343).

**Acceptance:** Pre-merge — unit tests over both shapes pass; mypy/ruff clean. Post-deploy — one fresh capture file has canonical fields only; one legacy file still reads through aliases. 1-day soak — ES template + consumers stable. **Tier:** Sonnet. **State:** Needs Approval. **Label:** PersonalAgent. **Blocks-on:** FRE-376 Phase 5 merge.

---

## Risks & open questions

1. **Probe self-reference.** Probe writes its own `trace_id` to ES. Sampling must filter `kind != "system:joinability_probe"` so probe traces are never anchor-session candidates. **Open:** do we want a *second-level* monitor that audits the audit? No — recursion without base case.

2. **Low-traffic days.** Weekends may yield 0–1 eligible sessions, defeating "random." Mitigation: `outcome=skipped` (exit 3) — gate aggregation ignores skipped buckets. **Open:** is `runs_count ≥ 12` too generous? If we see frequent skips, raise to 20.

3. **Conditional check ambiguity.** `consolidation_attempts` triggers via `BrainstemScheduler._should_consolidate` — host-metric gated. Probe can't perfectly mirror. Mitigation: yellow only on ≥50 captures + 0 attempts. **Open:** should the probe query `agent-logs-*` for `consolidation_skipped_*` events to confirm "scheduler chose not to, that's fine"?

4. **ES eventual consistency.** Hot writes 200ms before walk may legitimately miss in ES. Mitigation: 5-minute trailing filter on anchor sessions. **Open:** is 5 minutes enough vs the 600s captures backfill cadence (`scheduler.py:63`)?

5. **Brainstem-blind regression.** Brainstem crash kills probe → ES gap → gate goes yellow. *That is the signal.* If we ever observe a gateway crash that brainstem survives but joinability silently regresses, we ship Phase 5b (sidecar). Memory hook for future-me: probe gap ≠ probe orphan; treat them differently in any future Linear automation.

---

## Verification (end-to-end)

```bash
# 1. Pre-merge
make mypy && make ruff-check
make test                                                # unit tests including walk + status
make test-infra-up && make test-integration              # integration walk
make test-infra-down

# 2. ES template smoke
bash scripts/setup-elasticsearch.sh                      # idempotent PUTs
curl -s localhost:9200/_index_template/agent-monitors-joinability-template | jq

# 3. Post-deploy (VPS, same session)
ENV=cloud make deploy
ENV=cloud make health
uv run agent chat "verify probe" --new                   # one fresh authenticated turn
sleep 60                                                 # next scheduler tick
ENV=cloud make joinability-status                        # expects insufficient_history OR exit 0
curl -s 'http://localhost:9001/api/es/agent-monitors-joinability-*/_search?q=outcome:green' | jq '.hits.hits[0]._source'

# 4. Force-red rehearsal (test substrate only — never prod)
make test-infra-up
python -m scripts.monitors.joinability_probe \
    --session-id <seeded-uuid> --no-write-es --dry-run --fail-on red
# inspect exit=2, orphans[0].kind == "missing_identity"

# 5. Sibling FRE filed
# Verify on https://linear.app — new FRE in Needs Approval, labeled PersonalAgent, linked from PR

# 6. (Day 7) Flip
ENV=cloud make joinability-status                        # expect exit 0, 7 green buckets
# → comment FRE-376 with output
# → edit ADR-0074 Status: Proposed → Accepted (one commit, pushed direct to main per [[feedback_branch_pr_for_code]] — ADRs are docs)
# → close FRE-376
# → update docs/plans/MASTER_PLAN.md
```
