# Grafana migration program — retire Kibana, rebuild the dashboards properly

> **Status:** Draft, awaiting owner approval
> **Date:** 2026-08-08
> **Anchor ticket:** FRE-1203 (Approved, `stream:build2`, Tier-2:Sonnet)
> **Owner directive (2026-08-08, verbatim):** *"if I can do everything in Grafana and more than what I
> can do in Elastic and the visualizations are better and I can access more data sources then fucking
> remove Kibana and replace it with Grafana but do it in a way that makes sense, is efficient. We're in
> no hurry. There's no stress — just make the move and be done with it."*
> **No ADR.** FRE-1039 was canceled precisely because the decision was already taken. This plan records
> *how*, not *whether*. Two existing ADRs need **amendments** (not new ADRs) because they currently
> record the opposite ruling — see T8.

---

## 1. Why this plan exists

FRE-1072 shipped 16 Grafana dashboards and 68 panels. They are not usable. This is measured, not
asserted — every figure below is script-computed from the committed JSON, cross-checked with `grep`.

| Defect | Measurement |
|---|---|
| Panels with **no `fieldConfig` key at all** | **68 of 68** — key absent, not empty |
| Panels with a unit | **0** — `latency_ms`, `cost_usd`, `input_tokens` all render as bare numbers |
| Panels with a threshold | **0** — nothing shows good vs bad |
| Dashboards with a description | **1 of 16** |
| Panel types used | **4** — `timeseries` 30, `barchart` 28, `table` 5, `stat` 5 |
| `heatmap` / `logs` / `trace` / `state-timeline` / `bargauge` / `nodeGraph` panels | **0 of each** |
| Panels querying **Tempo** | **1 of 68**, on the project whose acceptance bar was a span waterfall |
| Panels that are a bare `count()` stub | **37 of 68 (54.4%)** |
| Panels querying a **dead event family** | **6** — `request_trace`/`request_trace_step` stopped emitting 2026-06-07 |

The panels are not misconfigured. They are **unconfigured** — seven keys each (`id`, `type`, `title`,
`description`, `gridPos`, `datasource`, `targets`) and nothing else, relying entirely on Grafana
defaults. That is the signature of hand-authored JSON written against a schema nobody read.

### 1.1 Six defects that make specific panels actively misleading

These are worse than ugly — a human reads them and concludes something false.

1. **Time-range contradiction.** Every dashboard except `health_check` is `time.from: now-24h`, but 9
   dashboards carry panel descriptions naming a 90d / 30d / 1-year window as *load-bearing to the
   finding* (`llm_performance`, `cost_budget`: *"Uses a 1-year time range to keep this historical
   signal visible"*). Those panels render **empty on load** while their description asserts a finding.
2. **`turn_session_artifact` panel 4** runs an **empty query string** against `es-agent-logs` while its
   description claims to show artifact gate/envelope outcomes. It counts *every document in
   agent-logs*. The number is real, large, and means nothing it claims to mean.
3. **All 5 `table` panels are non-functional.** Each uses ES `raw_document` with zero column config.
   Three carry descriptions narrating intended columns (*"Columns in source: trace_id, task_type,
   complexity, strategy, token_count, mode"*) that no `overrides` or `transformations` implement.
   Grafana dumps the raw `_source` blob.
4. **`terms` size is hardcoded `10` on all 48 terms aggregations**, contradicting four descriptions and
   one title outright: `Slowest traces (top 20)` returns 10.
5. **4 of 5 `stat` panels aggregate over a `date_histogram`** — wrong for a single-value display; shows
   the last bucket or a series, not the stat.
6. **Two panels hardcode a trace id** (`763278fa-…`) with the description instructing the user to
   hand-edit the query bar. No dashboard variables exist anywhere — `templating` is absent from all 16.

Layout compounds it: every panel is `w:12` of a 24-column grid but `y` steps by 8 with `x` alternating
12,0,12,0 — **no two panels ever share a row band**, so all 16 render as one vertical column of
half-width panels beside a checkerboard of empty space.

### 1.2 Root cause, and why it will recur without T0

The guardrail existed and did not fire.

`.claude/skills/create-visualization/SKILL.md` is a graduated skill whose **absolute rule** is *"Never
hand-author Lens JSON… Let Kibana author the object via its UI."* It carries four scrutiny gates —
Renders → Accurate → Useful to a human → **Verified against real recent data** — and defines done as
*"a human reads the correct story from real data."*

**It is scoped to Kibana by name.** Its trigger words are *"Kibana chart", "Lens visualization"*; its
absolute rule names *Lens* JSON; line 78 instructs committing to `config/kibana/dashboards/`. So 68
panels of hand-authored **Grafana** JSON sailed past a rule written to prevent exactly that.

ADR-0129 D6 then mandated file-provisioned dashboards *"so equivalence is reviewable in a diff"*, and
`provisioning/dashboards/dashboards.yaml` sets `allowUiUpdates: false`. The acceptance test became
**"an equivalent file exists"** — which a transcription passes and a useful dashboard is never required
to. The dashboards are literally tagged `rebuilt-from-kibana`.

**Owner ruling 2026-08-08: *"Grafana should have the same rules applied."*** T0 implements that, and it
lands first because it governs every rebuild ticket after it.

---

## 2. Scope

**In scope** — the five things named by the owner, plus the retirement they serve:

1. Playwright-driven analysis of all 16 existing dashboards (T2)
2. PG-backed cost dashboard (T4)
3. New PG dashboards unlocked by Grafana's Postgres connectivity (T5)
4. KG metrics (T6)
5. KG heatmap (T6)
6. Kibana retirement (T7–T9)
7. The skill rules that make 1–6 produce good artifacts rather than a second transcription (T0)

**Explicitly out of scope**

- Any new ADR. Two **amendments** to existing ADRs are in scope (T8) because they currently record a
  superseded ruling and enforce it in tests.
- Log migration or historical trace backfill — ADR-0129 D6 rules both out and nothing here changes that.
- Elasticsearch retirement. **Elasticsearch stays.** Only Kibana, the UI, retires.

---

## 3. The division of labour (settles where any future panel goes)

> **If the turn wrote a row, read the row. If it only wrote a log line, read the log. If you want the
> shape of the turn, read the trace.**

| Source | Use for | Panel types |
|---|---|---|
| **Postgres** | anything counted, summed or averaged; exact aggregates | timeseries, barchart, stat, table, heatmap |
| **Elasticsearch** | raw log lines, free-text search, error triage | **Logs** panel, table |
| **Tempo** | span waterfalls, per-stage latency of a real turn | **Trace** panel, timeseries |

The justification is measured, from FRE-1051 and this session:

| | Elasticsearch `agent-logs-*` | Postgres |
|---|---|---|
| Record types per container | **492 distinct `event_type` in one index family** | 1 per table |
| Schema width | **722 distinct mapped fields** (median 285/index, max 670) | widest real table `route_traces` = **42** |
| Typical document fill | **15 fields — 2.1% of the mapping** | `route_traces`: 472–518 of 518 rows on nearly every column |
| Event loss | **up to 83% dropped on bad days** (FRE-1051) | ledger, no cleanup task purges `api_costs` |

`agent-logs-*` is 492 record types wearing one shared schema; the mapping is the union of everything any
event ever emitted. That is why panels silently return empty — you query a field 98% of documents were
never going to have, and nothing distinguishes "no data" from "wrong field."

### 3.1 Forbidden sources — name these in every rebuild ticket

| Source | Why forbidden |
|---|---|
| `metrics` table | **No live write path.** `MetricsRepository` is never instantiated outside its own module (`docs/reference/POSTGRES_SCHEMA_DEBT_AUDIT.md`). A panel on it renders empty forever and looks like a query bug. |
| `session_events` table | Purged at 24h (`ws_event_ttl_hours`). Cannot back anything long-horizon; invalid as a denominator. |
| `sysgraph.*` schema | **Isolated by ADR-0105 AC-2** — `seshat_app` and `recall_role` are both explicitly denied `USAGE`, and the isolation is *proven* by a permission-denied test. See §3.2. |
| ES cost events for **aggregate** cost | Carries neither purpose nor token counts; up to 83% loss. Use `api_costs`. |
| `request_trace` / `request_trace_step` | Dead since 2026-06-07. 6 panels currently query it. |

### 3.2 Open decision for the owner — sysgraph exposure

Earlier in session I proposed a single "proposals detected vs promoted" panel. **That would breach
ADR-0105 AC-2's isolation posture.** Three options, and this plan does not pick one:

- **(a) Leave sysgraph unexposed.** `grafana_ro` gets nothing on the schema; the panel isn't built.
  Zero risk, keeps the proven boundary intact. **Recommended** — the signal is low-volume and can be
  read by hand.
- **(b) Grant `grafana_ro` SELECT on `sysgraph.stat` and `sysgraph.proposal` only.** Needs an ADR-0105
  amendment saying the isolation is against *the app path*, not against a read-only BI role.
- **(c) Project the two counts into a `public` table** via the same job as T6. No grant needed, no
  amendment; costs a little machinery.

**T5 assumes (a) until the owner rules.**

---

## 4. Ticket chain

Nine tickets. `⇒` = hard dependency. Tickets are filed after approval; Linear ids assigned then.

```
T0  skill rules (Grafana arm + compose-a-dashboard)
     ⇒ governs T4, T5, T6 — must merge before any rebuild starts
T1  FRE-1203 part 1 — Explore log-line rendering            [already Approved]
T2  FRE-1203 part 2 — grafana_ro role + PG datasource       [already Approved]
     ⇒ T4, T5, T6
T3  Playwright audit of all 16 dashboards  (⇐ needs nothing; run in parallel with T0/T1/T2)
     ⇒ defines the disposition list T4/T5/T7 execute against
T4  cost_budget rebuilt on Postgres — the exemplar          (⇐ T0, T2, T3)
T5  the remaining 8 PG-backed dashboards                    (⇐ T4 sets the pattern)
T6  KG metrics + heatmap: Neo4j→PG projection + dashboard   (⇐ T0, T2)
T7  delete the dead panels and dead datasources             (⇐ T3)
T8  docs + ADR amendments; retarget the CI gate             (⇐ nothing; blocks T9)
T9  Kibana retirement — the deletion                        (⇐ T7, T8)
```

**Parallelism for the build session:** T0, T1, T2, T3 and T8 have no interdependencies and can run
concurrently. T4 is a single-threaded exemplar. T5 fans out one subagent per dashboard. T9 is last and
alone.

---

### T0 — Apply the visualization rules to Grafana

**Tier:** 1 (Opus) — this is contract authoring, not implementation.
**Files:** `.claude/skills/create-visualization/SKILL.md`, new
`.claude/skills/compose-a-dashboard/SKILL.md`, memory
`feedback_kibana_lens_build_in_ui_not_hand_authored.md`.

**T0.1 — Generalize `create-visualization`.** Keep the loop, the four gates, Step 0 (inspect raw data),
the documentation-first rule and the anti-patterns *unchanged* — they are tool-agnostic and correct.
Change only what is Kibana-shaped:

- **Description/trigger** — add Grafana, panel, dashboard, Postgres datasource. Today it says only
  *"Kibana dashboard widget / Lens chart"*, which is why it never fired.
- **Absolute rule, restated tool-neutrally:** *never hand-author visualization JSON in any tool; let the
  tool's UI author it and extract a stable artifact.* Keep the Lens `visualizationType` story as the
  worked example, and **add the Grafana instance of the same failure** — 68 panels with no `fieldConfig`.
- **Add a Grafana arm** beside the Kibana one:
  - Build at `http://127.0.0.1:3003` (dev; anonymous Viewer works, Grafana 13.1.3).
  - `allowUiUpdates: false` means the UI **cannot save**. The build loop is: create the panel in the UI
    → **Dashboard settings → JSON Model** (or `/api/dashboards/uid/<uid>`) → copy → commit to
    `config/grafana/dashboards/<name>.json`. State this explicitly; it is the non-obvious step.
  - Export hygiene: strip `id`, set `version: 1`, keep `uid` stable across re-exports so provisioning
    overwrites in place rather than duplicating.
  - Render-assert selector for Grafana (Kibana's `data-ech-render-complete` does not exist here):
    assert the panel's data-testid `data-testid panel content` is present **and** that no
    `[data-testid="data-testid Panel status error"]` exists on the dashboard.
- **Grafana documentation-first links** (mandatory, matching the running 13.1.3):
  - Panels & visualizations: https://grafana.com/docs/grafana/latest/panels-visualizations/
  - Field config / units / thresholds: https://grafana.com/docs/grafana/latest/panels-visualizations/configure-standard-options/
  - Postgres data source: https://grafana.com/docs/grafana/latest/datasources/postgres/
  - Provisioning: https://grafana.com/docs/grafana/latest/administration/provisioning/
- **Add a mandatory field-config gate** — the specific defect that produced this program. A panel is not
  done unless `fieldConfig.defaults.unit` is set to a real unit
  (`currencyUSD`, `ms`, `short`, `percentunit`, `bytes`), and any panel with a good/bad reading carries
  thresholds. *A number without a unit is not a measurement.*

**T0.2 — Author `compose-a-dashboard`.** The memory parked this deliberately: *"authored once a real
multi-panel dashboard earns it — do NOT write from theory."* Sixteen dashboards and 68 panels, every one
failing at the whole-artifact level, is that moment. It must be written **from the T3 audit evidence**,
not from theory, and cover exactly the defects T3 measures:

- One dashboard answers **one coherent question**; a dashboard-level `description` states it (1 of 16 has one).
- **Time range must match the panels' claims** (defect §1.1.1) — and where panels need different windows,
  use a dashboard variable, not a description that lies.
- **Shared control group**: a time picker and the filters every panel honours. **Never hardcode an id in
  a query** (defect §1.1.6) — that is a `templating` variable.
- **Layout is reading order**: related panels share a row band; the most important sits top-left.
  Explicitly forbid the 12/0 checkerboard.
- **No redundant or contradictory panels**; the set must not answer the same question twice.
- Cross-panel and dashboard→dashboard drill-down (Grafana data links; Tempo↔ES `trace_id` links already
  exist in the datasource config and are unused by any panel).

**T0.3 — Update the memory** to record that the rule is tool-agnostic and that the Kibana-scoped trigger
is what let FRE-1072 through.

**Acceptance**

| # | Criterion | How checked |
|---|---|---|
| T0-1 | The skill's trigger fires on a Grafana task | The description names Grafana, panel and dashboard; a fresh session asked to "add a Grafana panel" invokes it |
| T0-2 | The absolute rule forbids hand-authoring in **any** tool | Grep the skill for `Lens`-only phrasing; the rule sentence names no single vendor |
| T0-3 | The Grafana build loop is executable as written | A build seat follows T0.1's steps and produces one committed panel JSON without asking a clarifying question |
| T0-4 | The field-config gate can fail | Applying the gate to any of today's 68 panels **rejects it** — this is the discriminating test; a gate that passes the current corpus is not a gate |
| T0-5 | `compose-a-dashboard` cites measured evidence | Every rule in it traces to a numbered defect in the T3 audit; no rule is present that T3 did not observe |

---

### T1 — Explore renders log lines (FRE-1203 part 1)

**Already Approved.** No re-litigation. Recorded here for sequencing only.

The ES datasources declare `index` and `timeField` but **no `messageField` and no `levelField`**, so
Explore renders the raw source document per row instead of a message column with level colouring. That
is the whole reason Grafana's log view felt worse than Kibana's.

**Files:** `config/grafana/provisioning/datasources/datasources.yaml` (7 ES entries).

**The trap, already named in the ticket:** a `messageField` naming a key no record carries produces an
empty column that looks identical to a datasource that was never configured. So **verify the field name
against a sampled document from each family** before declaring it — do not assume `message`.

---

### T2 — `grafana_ro` role + Postgres datasource (FRE-1203 part 2)

**Already Approved.** Mechanics pinned by this session's investigation.

**T2.1 — Migration `docker/postgres/migrations/0025_grafana_readonly_role.sql`** (next free number
confirmed; highest existing is `0024_drop_dead_embeddings_table.sql`). Mirror `0015_app_role_grants.sql`
exactly: the `=`-rule banner, the `-- Migration: 0025 — … (FRE-1203)` line, the idempotency +
apply line, the init.sql-mirroring sentence, a `WHY.` paragraph, `BEGIN; … COMMIT;`.

```sql
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'grafana_ro') THEN
        CREATE ROLE grafana_ro LOGIN PASSWORD 'grafana_ro_dev_password';
    END IF;
END
$$;

GRANT CONNECT ON DATABASE personal_agent TO grafana_ro;
GRANT USAGE ON SCHEMA public TO grafana_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE agent IN SCHEMA public
    GRANT SELECT ON TABLES TO grafana_ro;

-- Intentionally NO grant on schema sysgraph — ADR-0105 AC-2 isolation holds against
-- this role exactly as it does against seshat_app and recall_role.
```

Mirror the same block into `docker/postgres/init.sql` (fresh installs only run init.sql).
Apply: `psql $AGENT_DATABASE_ADMIN_URL -f docker/postgres/migrations/0025_grafana_readonly_role.sql`
— **as the `agent` superuser**, and note `psql` cannot parse the `postgresql+asyncpg://` prefix
(`sed 's|postgresql+asyncpg|postgresql|'`).

**T2.2 — Datasource entry** in `datasources.yaml`, matching house style (`name`, `type`, `uid`,
`access: proxy`, `url`, `jsonData`):

```yaml
  - name: PostgreSQL - ledger
    type: grafana-postgresql-datasource
    uid: pg-ledger
    access: proxy
    url: postgres:5432
    user: grafana_ro
    jsonData:
      database: personal_agent
      sslmode: disable
      postgresVersion: 1700
    secureJsonData:
      password: $GRAFANA_RO_PASSWORD
```

Three facts that make this work, all verified:
- The `grafana-postgresql-datasource` plugin is **already installed** in the running Grafana 13.1.3.
- `postgres:5432` resolves from Grafana in **both** compose files (dev shares the implicit default
  bridge; cloud shares the `cloud-sim` network) — one entry works everywhere.
- **Single-`$` interpolation is the secret mechanism.** The file already uses `$$` to *escape* Grafana
  template vars precisely because provisioning env-expands single `$`. So `$GRAFANA_RO_PASSWORD` is
  substituted from the environment and **no literal secret enters a tracked file** — which is the
  ticket's own acceptance criterion. This is the file's first `secureJsonData`; there is no precedent
  because ES runs with security disabled.

**T2.3 — Wire the env var** into the `grafana` service `environment:` in **both**
`docker-compose.yml` (soft default, matching `GRAFANA_ADMIN_PASSWORD`'s
`${GRAFANA_RO_PASSWORD:-grafana_ro_dev_password}`) and `docker-compose.cloud.yml` (hard guard,
`${GRAFANA_RO_PASSWORD:?GRAFANA_RO_PASSWORD required — set in .env}`, because
`tests/scripts/test_grafana_compose_service.py:88` asserts the `:?` form on cloud). Add
`postgres: {condition: service_healthy}` to Grafana's `depends_on` in both. Document both in
`.env.example` beside `GRAFANA_ADMIN_PASSWORD`.

**The security reason this role is not optional.** ADR-0129 already records it:

> *"`Viewer` in Grafana OSS is not 'may read the dashboards.' It can issue arbitrary queries against
> **every datasource in the org** … Per-datasource permissions are a Grafana Enterprise feature and are
> not available to narrow this."*

Grafana runs `GF_AUTH_ANONYMOUS_ENABLED=true` at `Viewer`. The instant a Postgres datasource exists,
anonymous callers can run arbitrary SQL as its role — and Postgres holds verbatim turns in
`captains_log_captures`. **`grafana_ro` is the containment boundary, not hygiene.**

**Acceptance** (from the ticket, kept verbatim in intent)

| # | Criterion | How checked |
|---|---|---|
| T2-1 | `grafana_ro` can SELECT from the cost ledger | `psql` as `grafana_ro`: `SELECT count(*) FROM api_costs` returns a row |
| T2-2 | It is **refused** on INSERT, UPDATE and DELETE | Three statements against `api_costs`, each returning `permission denied` — demonstrated, not asserted |
| T2-3 | It is refused on `sysgraph` | `SELECT * FROM sysgraph.proposal` → `permission denied for schema sysgraph` |
| T2-4 | No literal secret in any tracked file | `git grep` for the password value returns nothing; the datasource entry contains only `$GRAFANA_RO_PASSWORD` |
| T2-5 | The datasource reaches the ledger | `POST /api/ds/query` against uid `pg-ledger` returns a **per-role spend aggregate over a stated window** — proving reachability, not merely that Grafana accepted the config |

---

### T3 — Playwright audit of all 16 dashboards

**Tier:** 2 (Sonnet). **Depends on:** nothing. **Run early and in parallel.**

This is the *"analyse each one as they were built"* work that was promised on 2026-08-08 at 16:00 and
never done. The JSON audit in §1 is a static measurement; it cannot tell you whether a panel is
*readable*. Only rendering can.

**Method.** Grafana is live and reachable at `http://127.0.0.1:3003` (v13.1.3, anonymous Viewer). For
each of the 16 dashboards, drive Playwright:
1. Navigate to `/d/<uid>`, wait for panels to settle.
2. Screenshot full-page at 1920×1400.
3. Record per panel: does it draw? is it empty? does it show a `Panel status error`?
4. Apply the four gates from T0 to each panel and record a verdict with the reason.

**Deliverable:** `docs/research/2026-08-08-grafana-dashboard-render-audit.md` — a per-dashboard section
with the screenshot, a per-panel verdict table, and a one-line **disposition**: `rebuild-on-pg` /
`rebuild-on-es` / `keep-as-is` / `delete`. Plus a program-level summary: how many panels render empty,
how many are misleading, how many are salvageable.

**Predictions to test, not assume** (each is a §1.1 defect; the audit confirms or refutes it):
- The 9 dashboards whose descriptions claim a 90d/1yr window should render **empty** at `now-24h`.
- The 6 panels on `request_trace*` should render **empty** (dead since 2026-06-07).
- All 5 `table` panels should dump raw `_source` blobs.
- The 4 `stat`-over-`date_histogram` panels should show a series or last-bucket, not a stat.
- `turn_session_artifact` panel 4 should show a large count of *everything in agent-logs*.

**Playwright gotchas already learned** (from the FRE-593 research note — reuse, don't rediscover): use
`browser_evaluate` element `.click()` to bypass overlay hit-tests; use real keyboard typing for any
field gating a submit; **assert a render selector, do not eyeball a screenshot** — the screenshot is
evidence for the human, the assertion is the gate.

**Acceptance**

| # | Criterion | How checked |
|---|---|---|
| T3-1 | All 16 rendered against real data | 16 screenshots committed; each names the data window used |
| T3-2 | Every one of the 68 panels has a verdict and a disposition | Row count in the audit doc = 68; no cell blank |
| T3-3 | The empty-panel count is measured, not estimated | Each "renders empty" verdict cites the panel's own query returning 0 rows via `POST /api/ds/query` |
| T3-4 | The audit is falsifiable | Each of the five §1.1 predictions is marked confirmed or refuted with its evidence; a refuted prediction is reported, not quietly dropped |

---

### T4 — Rebuild `cost_budget` on Postgres (the exemplar)

**Tier:** 2 (Sonnet). **Depends on:** T0 (rules), T2 (datasource), T3 (verdict).

Owner directive: *"Waste no time rebuilding a broken cost dashboard. Build it using pg, and move on."*

This ticket is deliberately **one dashboard**, done to the full standard, because it becomes the
reference pattern every T5 subagent copies. Doing it well once is cheaper than doing eight badly.

**Sources** (all verified present and populated):
- `api_costs` — per-call billing ledger. `timestamp`, `provider`, `model`, `input_tokens`,
  `output_tokens`, `cost_usd DECIMAL(18,12)`, `cache_read_input_tokens`,
  `cache_creation_input_tokens`, `trace_id`, `session_id`, `purpose`, `latency_ms`. History from
  2026-05-22. **No cleanup task purges it.**
- `budget_policies` (`role`, `cap_usd`, `time_window`), `budget_counters` (`role`, `running_total`,
  `window_start`), `budget_reservations` (`status`, `amount_usd`, `actual_cost_usd`, `created_at`,
  `settled_at`).

**Panels to build** (replacing 6 ES panels, two of which admit degradation in their own titles):

| Panel | Query shape | Unit | Fixes |
|---|---|---|---|
| Budget utilisation by role | `budget_counters.running_total / budget_policies.cap_usd` joined on `role` + `time_window` | `percentunit` + thresholds at .75/.9 | Kills *"utilization scorecard not reproduced"* — it is a two-table join |
| Spend over time by purpose | `SUM(cost_usd)` from `api_costs`, `$__timeGroupAlias(timestamp, $__interval)`, `GROUP BY purpose` | `currencyUSD` | Exact, replaces the 83%-lossy ES mirror |
| Reserve→commit→refund funnel | `COUNT(*) … GROUP BY status` from `budget_reservations` | `short` | Exact state counts, not event-count guessing |
| Settlement latency | `settled_at - created_at` percentiles | `s` | New — impossible in ES |
| Top sessions by spend | `SUM(cost_usd) … GROUP BY session_id ORDER BY … LIMIT 20` | `currencyUSD` | Honest limit, unlike the hardcoded `10` behind a "top 20" title |
| **Cost reconciliation** | `route_traces.cost_live_usd` vs `cost_authoritative_usd` where `cost_reconciled` | `currencyUSD` | **New capability.** A self-checking instrument showing where the live estimate diverges from the ledger. Elasticsearch cannot do this at all |

**Two column traps to get right** (both found in this session's investigation):
- `api_costs` has **`purpose`**, not `role`. Per-role spend comes from `budget_counters.role` /
  `budget_policies.role`. Confusing them silently answers a different question.
- `route_traces` and `api_costs` join on `trace_id`, but **FRE-1186/FRE-1204 record a dashed-vs-hex
  `trace_id` representation split across substrates** and a resulting `session_cost_usd`
  double-count. Any join must normalize, and the panel must state which representation it assumes.

**Tag discipline — this will break a test otherwise.**
`tests/integration/test_fre1072_tempo_grafana_acceptance.py:286` hard-asserts
`len(ours) == 15` for dashboards tagged `rebuilt-from-kibana`. A rebuilt dashboard must **drop** that
tag (it is no longer a transcription) and take `grafana-native` — and the assertion's expected count
must be decremented **in the same commit**. Every T5 rebuild repeats this.

**Acceptance**

| # | Criterion | How checked |
|---|---|---|
| T4-1 | Every panel carries a unit | `jq` over the file: all panels have `fieldConfig.defaults.unit` set to a real unit |
| T4-2 | Utilisation is a real ratio | The panel's SQL joins `budget_counters` to `budget_policies`; a hand-run of the same SQL matches the rendered value |
| T4-3 | Spend figures match the ledger | Panel value for a stated 30-day window equals `SELECT SUM(cost_usd) FROM api_costs WHERE …` run directly — same number, not "approximately" |
| T4-4 | The reconciliation panel discriminates | On a trace where `cost_live_usd ≠ cost_authoritative_usd`, the panel shows a non-zero divergence; on a reconciled one, zero |
| T4-5 | Built in the UI, not hand-authored | The committed JSON contains `fieldConfig`, `options` and `transformations` keys that hand-authoring omits — the T0 tell |
| T4-6 | Renders correctly for a human | Playwright screenshot + the T0 four gates, with the raw-data cross-check of T4-3 |

---

### T5 — The remaining eight PG-backed dashboards

**Tier:** 2 (Sonnet). **Depends on:** T4 (pattern), T3 (dispositions). **Fan out: one subagent per dashboard.**

| Dashboard | Postgres source | What Postgres fixes |
|---|---|---|
| `intent_classification` | `route_traces.task_type, complexity, intent_confidence` | 472/518 filled; currently inferred from log lines |
| `request_timing` | `route_traces.latency_total_ms` + `latency_breakdown` (JSONB per stage) | **Replaces 3 dead panels** — real per-stage breakdown, one row per turn |
| `task_analytics` | `route_traces.tool_iteration_count, tools_used, skills_loaded` | 518/518 filled |
| `expansion_decomposition` | `route_traces.decomposition_strategy/reason, sub_agent_count, sub_agents` | 472/518 |
| `llm_performance` | `route_traces.model_role, thinking_enabled, routing_history, fallback_triggered` + `api_costs` | Exact per-model latency and token cost |
| `system_health` | `route_traces.error_type/error_class/degraded_stages` | Typed, dense |
| `extraction_retry_health` | `consolidation_attempts` (5,083 rows: `attempt_number`, `outcome`, `denial_reason`) | Actual attempts-to-success, not a log-derived median |
| `turn_session_artifact` | `artifacts` (97) + `sessions` (1,282) + `session_model_selections` | Fixes the empty-query panel that counts everything |

**Per-dashboard subagent brief** (identical shape for all eight):
1. Read the T3 verdict for this dashboard.
2. Read the raw table first (T0 Step 0) — fields, meaning, denominators.
3. Build in the Grafana UI, export the JSON Model, commit.
4. Run the four gates including the raw-data cross-check.
5. Drop `rebuilt-from-kibana`, add `grafana-native`, decrement the count assertion.

**`request_timing` gets one extra panel — a real Tempo trace view.** Exactly 1 of 68 panels touches
Tempo today, on the project whose acceptance bar was *"open a real turn and see its span waterfall."*
FRE-1067 shipped the step/model-call/tool-call span tree with gen_ai semconv. Add a **Trace** panel
(a panel type used zero times) so that waterfall is actually visible.

**Acceptance** — T4-1, T4-5 and T4-6 applied per dashboard, plus:

| # | Criterion | How checked |
|---|---|---|
| T5-1 | No rebuilt panel queries a forbidden source | Grep the eight files for `metrics`, `session_events`, `sysgraph`, `request_trace` — zero hits |
| T5-2 | Each dashboard's time range matches its panels' claims | For each, `time.from` ≥ the longest window any description names; no description claims a window the dashboard cannot show |
| T5-3 | The span waterfall is reachable | `request_timing` has a Trace panel; clicking a trace id from a cost panel lands on it |

---

### T6 — KG metrics and the heatmap

**Tier:** 2 (Sonnet). **Depends on:** T0, T2.

#### The mechanism already exists — verified live this session

**ADR-0042 / FRE-161 KG Freshness** maintains four properties per Entity, distinct from the
ingestion-side `last_seen`/`mention_count`: `first_accessed_at`, `last_accessed_at`, `access_count`,
`last_access_context`. Written by the `cg:freshness` Redis consumer from `memory.accessed` events.
`AGENT_FRESHNESS_ENABLED=true` in `.env`.

**The instrument is verified, not assumed:**
- `last_accessed_at > first_accessed_at` on **4,709 of 6,686** entities — it genuinely moves; it is not
  a creation timestamp in disguise.
- `last_access_context` records real recall: `tool_call` 2,253 · `consolidation` 1,597 ·
  `context_assembly` 48 · `created` 2,788.

**This is already load-bearing, not just observability.** `freshness_relevance_weight=0.15` and
`boost = min(1 + 0.1·ln(1+access_count), 1.5)` mean access heat **already reranks recall**. It is a live
control loop with no instrument on it.

#### The finding that motivates the dashboard

| Cohort | n | share |
|---|---:|---:|
| **Never read** (`access_count = 0`) | 2,788 | 36.3% |
| **Unmeasurable** (no access properties at all) | 1,003 | 13.0% |
| Read 1–2× | 2,710 | 35.2% |
| Read 3–10× | 1,010 | 13.1% |
| Read 11+× | 178 | 2.3% |

At least 36% of the graph has never been retrieved once. The 1,003 unmeasured entities are a **separate
defect** — they predate the tracker or come from a write path that does not publish `memory.accessed`
— and get their own ticket (§5).

#### T6.1 — The projection: `freshness_review` already computes this, and throws it away

`brainstem/jobs/freshness_review.py` runs weekly (`0 3 * * 0`), scans every Entity and relationship,
and classifies each via `classify_staleness` into warm/cooling/cold/dormant. It writes to
`telemetry/freshness_review/FR-<iso_week>.jsonl` and publishes an event. **Nothing lands in a queryable
store, and the JSON snapshot path is a single fixed file.**

Neo4j keeps no history of its own shape — `access_count` is a running total, `last_accessed_at` a single
scalar. So **snapshot heat is free today; heat-over-time requires a projection.** That is why option 3
(project into Postgres) beat a Cypher datasource: a Cypher panel answers *"what is it now"*; it cannot
answer *"what has it been doing"*, and every question worth asking about the graph is a trend.

**Build `kg_stats`** — new table, `init.sql` + migration `0026_kg_stats.sql`, and note this drags in
`tests/migrations/test_init_sql_model_parity.py` (init.sql must match the SQLAlchemy model).

```sql
CREATE TABLE IF NOT EXISTS kg_stats (
    id              BIGSERIAL PRIMARY KEY,
    observed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metric_name     VARCHAR(64) NOT NULL,
    dimension       VARCHAR(64),          -- entity_type, recency bucket, tier …
    metric_value    DOUBLE PRECISION NOT NULL,
    UNIQUE (observed_at, metric_name, dimension)
);
CREATE INDEX idx_kg_stats_metric_time ON kg_stats(metric_name, observed_at DESC);
```

Extend `freshness_review` to write its counts here **in addition to** the existing JSONL (do not remove
the durable write — ADR-0054 D4 orders durable-first, bus-second). **Change the cadence to daily**;
weekly is too coarse for a trend and the scan is cheap.

**Metrics to emit** (each becomes a panel):

| `metric_name` | `dimension` | Meaning |
|---|---|---|
| `entity_count` | entity_type | Node counts by type |
| `relationship_count` | rel_type | Edge counts by type |
| `access_count_bucket` | 0 / 1-2 / 3-5 / 6-10 / 11-25 / 26+ | The heat histogram |
| `recency_bucket` | 0-1d / 2-7d / 8-14d / 15-30d / 31-60d / 60d+ | The recency histogram |
| `cold_mass_ratio` | — | **The headline number**: never-read ÷ total |
| `unmeasured_ratio` | — | No access properties ÷ total |
| `embedding_missing` | — | Unreachable by vector recall |
| `duplicate_group_count` | — | Case-insensitive name collisions |
| `type_disagreement_count` | — | Same name, different `entity_type` |
| `turns_without_entities_ratio` | — | Turns discussing no entity |

#### T6.2 — The KG Health dashboard

Ten panels on `pg-ledger`, tagged `grafana-native`:

1. **Cold-mass trend** — `cold_mass_ratio` over time, `percentunit`, threshold at 0.3.
   *The single most useful number in the whole KG, and one nobody can currently see.*
2. **Heat histogram** — `access_count_bucket`, **`bargauge`** (a type used zero times today).
3. **Recency × frequency heatmap** — the classic RFM grid, **`heatmap`** panel. The 49% cold mass
   becomes a visible block in the corner.
4. **Type × recency heatmap** — decay profile per entity type. Already shows real signal:
   `MethodOrConcept` is the coldest mass (2,636 entities, 34% of the graph, mostly stale) while
   `Person` and `KnowledgeArtifact` skew hot — a recall-tuning input, not decoration.
5. Node counts by type over time.
6. Edge counts by type over time.
7. Embedding reachability trend.
8. Duplicate-group and type-disagreement counts.
9. Turns-without-entities rate.
10. Growth per active day.

**Two limits to state honestly on the dashboard, not paper over:**
- **Access-over-time (day × hour) is impossible.** Only the *last* access survives; every prior one is
  overwritten. The history exists transiently in the `memory.accessed` stream and is discarded. Do not
  build a panel that implies otherwise.
- Panels 1–4 have **no history until the projection has run for some days.** They will be near-empty at
  merge. State this in the dashboard description rather than shipping a chart that looks broken.

**Acceptance**

| # | Criterion | How checked |
|---|---|---|
| T6-1 | The projection writes real rows | After one scheduled run, `SELECT count(*) FROM kg_stats WHERE metric_name='cold_mass_ratio'` ≥ 1, and its value matches a hand-run Cypher count of `access_count = 0` ÷ total |
| T6-2 | The heat is discriminating, not decorative | The top-10 by `access_count · e^(−λ·age)` are entities the owner recognises as actually in use; a cohort with `access_count=0` never appears in it |
| T6-3 | The cold-mass number can move | Simulate: accessing a previously-cold entity decrements the never-read count on the next run |
| T6-4 | The heatmap renders with real buckets | Playwright screenshot showing a populated `heatmap` panel, cross-checked against the same buckets computed directly in Cypher |
| T6-5 | The impossible panel is absent | No panel claims access-over-time; the dashboard description states why |

---

### T7 — Delete the dead panels and dead datasources

**Tier:** 3 (Haiku). **Depends on:** T3.

Pure deletion, driven by the T3 dispositions. Nothing is rebuilt here.

- **6 panels on the dead `request_trace*` family** — `request_timing` 1,2,3 and `request_traces` 1,2,3.
  Superseded by T5's `route_traces` rebuild and FRE-1067's span tree.
- **2 panels hardcoding trace id `763278fa-…`** (`request_traces` 2,3) — subsumed above.
- **2 provisioned datasources no panel queries** — `es-captains-captures`, `es-insights`.
- **`turn_session_artifact` panel 4** — the empty-query panel counting all of agent-logs. Delete or fix
  in T5; do not leave it rendering a true number that means nothing it claims.

**Acceptance**

| # | Criterion | How checked |
|---|---|---|
| T7-1 | No panel queries a dead event family | Grep the dashboards for `request_trace` — zero hits |
| T7-2 | No datasource is unreferenced | Every uid in `datasources.yaml` appears in ≥1 panel, or is removed |
| T7-3 | No panel runs an empty query against `agent-logs` | Grep for `"query": ""` on `es-agent-logs` — zero hits |

---

### T8 — Docs, ADR amendments, and the CI gate

**Tier:** 1 (Opus) for the amendments, 2 (Sonnet) for the CI gate. **Blocks T9.**

#### T8.1 — The CI gate (the one genuine blocker; land it first and alone)

`scripts/audit/telemetry_surface_check.py` is wired into CI (`.github/workflows/ci.yml:238`) and reads
`config/kibana/dashboards/` as the "dashboard corner" of ADR-0090's three-way reconciliation. It parses
**Lens-specific keys** — `visState`, `kibanaSavedObjectMeta`, `attributes.state` — none of which exist
in Grafana panel JSON. **Deleting `config/kibana/` fails `main`.**

Two options; the plan recommends the first:
- **(a) Repoint at `config/grafana/dashboards/*.json`** and rewrite the field-extraction walk for
  Grafana's shape (`panels[].targets[].{query,metrics[].field,bucketAggs[].field}`). Preserves ADR-0090's
  dashboard corner. Real schema work, one focused ticket.
- (b) Remove the dashboard corner from the triangle. Cheaper, but weakens an ADR-0090 contract and needs
  its own amendment.

Its two siblings, `scripts/audit/fre533_reconcile.py` and `scripts/audit/verify_fre535_panels.py`, are
**one-shot audit artifacts** with the same Kibana-schema dependency but no CI wiring. Archive them.

#### T8.2 — ADR-0129 D6, second amendment

D6 currently records the owner's 2026-08-07 ruling verbatim (*"I accept maintaining the 2 UI until
Grafana has shown its superior functionality"*) and states retirement is **deferred**. The 2026-08-08
directive supersedes it. A **Status Update** records the new ruling in the owner's words, exactly as
FRE-1193 did in the other direction. Also:
- `:140` — *"The `monitoring` host stays pointed at Kibana"* inverts.
- **AC-10(e) becomes false** if `monitoring` is repointed and must be rewritten.
- **Do not resurrect the 551 MiB argument.** It was withdrawn and corrected in place: Kibana measures
  562.6 MiB against a 1 GiB cap with 6.0 GiB available. Running both was affordable; the retirement
  rests on Grafana's demonstrated superiority, which is what T3–T6 establish.

#### T8.3 — ADR-0134

D2a's Kibana staging branch is **already dead** — FRE-1187 enumerated 29 connectors, found only
`.index` and `.server-log` enabled under the basic licence, neither leaving the box, and the ADR's own
stated contingency fired: *"the Kibana stage is abandoned outright."* Mark D2a's Kibana branch
superseded and fix line 121's stale *"FRE-1072 retires Kibana"*.

#### T8.4 — Ticket-state consequences (master's action, recorded here)

- **FRE-1190** (rules 1&2 *on Kibana*) — obsolete, not blocked. Cancel.
- **FRE-1192** (rules 3–6 on Grafana + port 1&2 off) — collapses to "all six rules on Grafana."
- **FRE-1202** (docs say FRE-1072 retires Kibana) — resolved by T8.2/T8.3; close as folded in.

**Acceptance**

| # | Criterion | How checked |
|---|---|---|
| T8-1 | CI passes with `config/kibana/` absent | Delete the directory locally, run the gate — exits 0 |
| T8-2 | The gate still detects a real drift | Introduce a panel referencing a nonexistent field; the gate fails. *A gate that cannot fail verifies nothing* |
| T8-3 | No ADR asserts Kibana is retained | Grep ADR-0129/0134 for retention claims; each is amended or dated as superseded |
| T8-4 | No ADR cites the 551 MiB figure as live | Grep — the only occurrence is inside its own correction |

---

### T9 — Kibana retirement

**Tier:** 2 (Sonnet). **Depends on:** T7, T8. **Last, and alone.**

Ordered by risk, from the full inventory:

1. **Invert the retention tests.** `tests/integration/test_fre1072_tempo_grafana_acceptance.py:538-556`
   currently asserts *"Kibana retention is a deliberate design decision (ADR-0129 D6) — must stay
   declared"* plus a live `localhost:5601` health check. Both invert.
2. **Delete `config/kibana/`** — 16 NDJSON (108 saved objects: 15 dashboards, 62 lens, 5 searches, 26
   index-patterns, 222 KB), `README.md`, `import_dashboards.sh`, `setup_dashboards.py`.
3. **Delete the 15 `tests/scripts/test_*_dashboard.py` modules** bound to those paths, plus
   `test_es_templates.py:248-259` which asserts on `setup_dashboards.py`'s source text.
4. **Delete both compose service blocks**, `docker/kibana/`, `docker-compose.cloud.yml:11`,
   `tests/scripts/test_kibana_compose_service.py`; fix the now-dangling references in
   `test_tempo_compose_service.py` and `test_grafana_compose_service.py`.
5. **Retire the `.env.kibana` custody apparatus** (`.env.example:48-54,747-749`, `.gitignore:174`,
   three `.claude/settings.local.json` entries). It existed *solely* for Kibana alerting, which was
   abandoned — dead weight already.
6. **Scripts and user-facing strings**: `scripts/init-services.sh:83`,
   `scripts/setup-elasticsearch.sh:401,430`, `scripts/README.md:56`,
   `events/pipeline_handlers.py:610,746` (remediation advice shown to the owner — repoint at Grafana),
   `scripts/eval/recovery_survey.py:435`.
7. **Docs**: `README.md`, the three `docs/guides/KIBANA_*.md`, `docs/reference/*`,
   `docs/skills/query-elasticsearch.md`. Leave dated research/plan records alone — they are history.
8. **Runbook, not diff** — repoint or retire the `monitoring` Cloudflare hostname and its Access
   policy. Ingress is **remotely managed by Cloudflare**; the repo holds only a comment. Grafana already
   has its own `observe` host. **This is an owner action.**

**Deliberately untouched:** the `rebuilt-from-kibana` tags on any dashboard not yet rebuilt (a live test
filters on them — see T4's tag discipline); the frozen eval corpora under
`scripts/study/eval_artifacts/`; `.kibana` ES-system-index handling in
`test_migrate_fre1036_monthly_indices.py`; and `request_gateway/intent.py:110,120` — **add `grafana`
alongside `kibana` rather than removing it**, so a user saying either word still routes to telemetry.

**Acceptance**

| # | Criterion | How checked |
|---|---|---|
| T9-1 | The stack comes up with no Kibana | `make ps` shows no kibana container; every other service healthy |
| T9-2 | `make test` and CI pass | Full run, green, with `config/kibana/` gone |
| T9-3 | No runtime code references Kibana | Grep `src/` — remaining hits are comments or the intent regex that gained `grafana` |
| T9-4 | The owner reaches every dashboard | `observe` host serves Grafana behind CF Access; the owner confirms interactively |
| T9-5 | Nothing that was reachable is now unreachable | Every T3 disposition of `keep-as-is` or `rebuild-*` has a live Grafana equivalent; the audit doc's disposition column has no orphan |

---

## 5. Defects found while planning — file separately, do not fold in

Each is independently verified and none is this program's work.

| Finding | Evidence |
|---|---|
| **`USEs` vs `USES`** | A casing variant with 1 edge beside `USES` with 2,939 — silently splits every traversal |
| **`first_seen` is a STRING, `last_seen` a DATE_TIME** on the same node | Any range query over `first_seen` compares lexicographically |
| **1,202 entities (15.6%) lack `description`, `embedding`, `entity_id` and `properties`** — all four, same cohort | Unreachable by vector recall. Cohort stopped accruing 2026-08-03 while the enriched cohort runs to 2026-08-08 — worth confirming, not concluding |
| **258 duplicate-name groups covering 526 entities**, disagreeing on *type* not just name | "cold start" is both `Phenomenon` and `MethodOrConcept`; "vector search" both `MethodOrConcept` and `DomainOrTopic`; "load average" ×3 |
| **1,003 entities carry no access properties at all** | Unmeasurable by the freshness tracker — predate it or come from a write path that never publishes `memory.accessed` |
| **212 Claim nodes hang off exactly 1 entity** | `HAS_FACT` is effectively unused |
| **`visibility` is `group` on all 7,689 entities** | No owner-scoped entity exists |
| **409 of 2,416 turns (16.9%) discuss no entity** | Extraction gap |
| **`sysgraph` provenance edges are all empty** | `derives_from`, `promoted_to`, `produced`, `correlates_with`, `influence`, `signal` — 0 rows each, though `proposal` (26) and `stat` (1,302) have data. Not one proposal links to the statistic that produced it. *Correction from earlier in session: 0 tickets is Linear-side suppression, not a dead pipeline — the provenance gap is the real finding* |

---

## 6. Program acceptance — what "done" means

| # | Criterion | How checked |
|---|---|---|
| **P-1** | **Kibana is gone and nothing was lost** | No Kibana container, no `config/kibana/`; every T3 disposition has a live Grafana equivalent |
| **P-2** | **Every shipped panel carries a unit** | `jq` across `config/grafana/dashboards/*.json`: zero panels lacking `fieldConfig.defaults.unit`. *Today this returns 68; done means 0* |
| **P-3** | **No panel renders empty on load** | Playwright pass over every dashboard at its default time range; every panel draws data or is deleted |
| **P-4** | **Cost figures match the ledger exactly** | A stated-window total from the dashboard equals the same `SUM` run directly against `api_costs` |
| **P-5** | **The cold-mass number is visible and moves** | `kg_stats` has ≥7 days of `cold_mass_ratio`; the panel renders a trend |
| **P-6** | **The rules bind Grafana** | A fresh session asked to add a Grafana panel invokes `create-visualization`; the field-config gate rejects a unit-less panel |
| **P-7** | **The owner reaches a correct conclusion** | The owner opens the rebuilt cost dashboard and the KG Health dashboard and confirms each answers its question without explanation. *This is the gate the JSON audit cannot substitute for* |

**P-7 is the real criterion.** Every other row is instrumentation for it. The failure this program exists
to end is a dashboard that renders, passes its tests, and tells a human nothing — or worse, something
false.

---

## 7. Sequencing for the build session

**Wave 1 (parallel, no interdependencies):** T0 · T1 · T2 · T3 · T8.1
**Wave 2:** T4 (single-threaded exemplar) · T6 (independent of T4) · T8.2–T8.4
**Wave 3:** T5 (fan out — one subagent per dashboard) · T7
**Wave 4:** T9 (alone)

Owner directive: *"We're in no hurry. There's no stress."* Wave 2's T4 should not be rushed — it is the
pattern eight subagents will copy, and a flaw there multiplies by eight.
