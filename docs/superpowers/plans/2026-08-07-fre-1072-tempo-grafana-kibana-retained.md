# FRE-1072 — ADR-0129 B7: Tempo, Grafana, and Grafana's gated endpoint (Kibana retained)

**Ticket:** FRE-1072 (Approved, `Tier-2:Sonnet`, `stream:build2`)
**Backing ADR:** ADR-0129 D6, amended 2026-08-07 (design intent only — its own criteria belong to
its seam ticket FRE-1073, ADR-0130 D1/D2). Also touches ADR-0134 D2a (alerting-rule port obligation).
**Related:** FRE-533 (panel inventory, source for AC-6) · FRE-1187 (Kibana alerting connector
verdict, resolves AC-9) · FRE-1070 (OTel Collector, NOT in scope here — see Finding F1) · FRE-1192
(rules 3–6 + the port of 1–2, a **separate, already-filed ticket** — see Revision 2 §0)

> **Revision 2 (2026-08-07)** — reworked after adversarial codex plan-review. Codex's review and my
> independent verification of every checkable claim are recorded in §0. Net effect: two image pins
> bumped for real, verified CVEs; the Tempo healthcheck design replaced (codex's own proposed fix was
> itself wrong — verified live against the actual image); the dashboard inventory corrected from a
> stale README (12 → the real 15); a `$$`-escaping bug fixed; multi-datasource ES design added; one
> "blocker" (alerting scope) rejected with direct evidence after independent verification found codex
> was reading ADR prose without visibility into Linear's ticket graph, where the work already has its
> own separate ticket.

---

## 0. Codex plan-review outcome (verified, not taken on faith)

Every finding below was independently checked — against live `docker pull`/`docker run` against the
actual pinned images, WebFetch against current upstream docs/advisories, or direct `grep`/`read`
against this repo/Linear — before being accepted or rejected. Codex reviews the diff at every merge
gate too, so a finding accepted here without independent verification would just reappear as an
unverified claim laundered through this plan.

| Codex finding | Verdict | Verification performed |
|---|---|---|
| Tempo healthcheck (`wget`/shell) can't run | **Accepted**, but codex's own fix (`/tempo --health`) is **fabricated** | `docker run --entrypoint sh` → `exec: "sh": executable file not found`; `docker export \| tar -tv` → zero matches for `sh`/`wget`/`busybox`/`curl` anywhere in the image (genuinely distroless); `/tempo --health` → `flag provided but not defined: -health`. Real fix in §3.2/§3.3 below. |
| `grafana/tempo:2.10.1` has a known DoS CVE | **Accepted** | WebFetch of `grafana.com/security/security-advisories/cve-2026-27878/`: confirmed real, CVSS 6.5, affects up to 2.10.1, fixed ≥2.10.2. Re-probed Docker Hub tags: 2.10.7 is the current 2.10.x patch (2.11.0 not yet tagged); pulled and confirmed. **Re-pinning to `2.10.7`.** |
| Grafana's bundled Tempo *datasource plugin* has its own version/CVE independent of Grafana core | **Accepted** | Not a marketplace plugin (`/var/lib/grafana/plugins/` is empty) — it's compiled into the Grafana build, but its `plugin.json` carries its own version string that lags core by a patch or two. Live-inspected the actual pinned image: `grafana/grafana:13.1.1` ships bundled Tempo plugin **`13.1.2`**. WebFetch of the plugin changelog confirmed a real path-traversal fix (`GL-Vuln VUL-2026-0062`) landed in plugin version **13.1.3**. Pulled `grafana/grafana:13.1.3` and confirmed its bundled plugin reports `"version": "13.1.3"` — patched. **Re-pinning Grafana to `13.1.3`.** |
| `$${__span.traceId}` needed doubled `$`, plan had single `$` | **Accepted** — genuine bug, self-inconsistent with the plan's own (correctly escaped) ES→Tempo direction | Re-read of my own draft confirmed the asymmetry. Fixed in §3.5. |
| Missing `spanStartTimeShift`/`spanEndTimeShift` on `tracesToLogsV2` | **Accepted** | Grafana's own trace-to-logs docs warn a zero shift misses log records just outside a span's exact interval. Added `-2s`/`2s`. |
| One Elasticsearch datasource can't serve the real dashboard corpus (multiple index/timeField pairs) | **Accepted** | Direct `grep`/parse of `config/kibana/dashboards/data_views.ndjson`: confirmed distinct `(index-pattern, timeFieldName)` pairs in this repo — `agent-logs*`/`@timestamp` (carries `trace_id`), `agent-captains-reflections-*`/`timestamp`, `agent-captains-captures-*`/`timestamp`, `agent-insights-*`/`timestamp`, `agent-captains-funnel-events-*`/`@timestamp` — plus `started_at` (joinability) and `probed_at` (SLM health) confirmed via the ES index templates. Design updated to multiple provisioned ES datasources (§3.5). |
| F7's "12 dashboards" is a stale README read | **Accepted** — confirmed via the actual import script, not the README | `config/kibana/import_dashboards.sh`'s own `FILES=(...)` array lists **15** dashboard files (16 minus `data_views.ndjson`), matching `ls config/kibana/dashboards/*.ndjson`. The README is missing `self_improvement_funnel`, `cost_budget`, `traversal_gate`, `monitors_joinability_slm`, `turn_session_artifact`, and names two files that don't exist (`reflection_insights.ndjson`, `insights_engine.ndjson`). F7 and AC-6's scope corrected to the real 15 (§3.7); the README's own staleness gets a one-line fix folded into this PR (cheap, adjacent, not a new ticket per Step 5). |
| AC-3's test proves backend query equivalence, not the rendered UI link | **Accepted** — methodological improvement | Test design changed to inspect Grafana's actual computed data-link/frame metadata from a live query response, not a hand-replicated equivalent query. |
| Provisioning needs `prune: true` or a removed datasource/dashboard survives in `grafana.db` | **Accepted** | Matches documented Grafana provisioning behavior; cheap, adopted for both providers. |
| AC-10's local checks don't prove the Cloudflare Access edge (challenge, policy principal, `monitoring` still routing Kibana) | **Not a plan gap** — this is the ticket's own explicit scope boundary, not something codex is asking the plan to add incorrectly | The ticket's own AC-10 text: *"Cloudflare-side exposure is not part of this criterion — the tunnel ingress rule and the Access policy are owner actions in the runbook below, not decidable from this ticket's deliverable (ADR-0130 D6)."* My AC-10 test scope already matches the ticket's own stated bar exactly. No change — noted so this doesn't get re-litigated at the gate. |
| Dev-compose Tempo ports should bind `127.0.0.1` only | **Rejected** | Direct read of the existing `docker-compose.yml`: **zero** existing dev services bind loopback-only (`elasticsearch` `"9200:9200"`, `kibana` `"5601:5601"`, `postgres` `"5432:5432"`, etc. — all open). Adding it only for Tempo/Grafana would be an unrequested, inconsistent deviation from established dev-compose convention (CLAUDE.md § Surgical Changes: match existing style). The cloud compose file — where it actually matters for the real deployment — already correctly loopback-binds every service, Tempo/Grafana included (§3.3). |
| AC-6's "query executes without error" doesn't prove dashboard semantic equivalence | **Partially accepted** | The ticket's own AC-6 text defines its proof bar as exactly "executes... without a query or datasource error" with emptiness explicitly not a failure — I'm not inventing a stricter AC than the ticket specifies. But a lightweight per-dashboard reconciliation note (source panel count → target panel count, one line per of the 15) is cheap and genuinely reduces the risk of silently dropping a panel, so it's added to the PR as documentation, not as a new test assertion beyond what AC-6 asks for. |
| Resource sizing (512Mi Tempo) is asserted, not load-tested | **Accepted as a named, deliberate risk — not building a load-test harness** | This is a research/learning project, not a production platform (`.claude/CLAUDE.md`: *"Type: Research & Learning (not production-ready)"*) — building a dedicated load-test harness for a 14-day-retention single-node trace backend is speculative infrastructure the ticket doesn't ask for (CLAUDE.md § Simplicity First). Noted explicitly as an accepted risk in the PR/handoff instead. |
| Admin password rotation has no documented procedure | **Accepted, cheap** | One paragraph added to the runbook notes (§3.4) — `GF_SECURITY_ADMIN_PASSWORD` only takes effect on first boot into a fresh `grafana.db`; rotating it later needs the admin API or a volume reset, not just an env var change. |
| **[blocker] Alerting scope: ADR-0134 D2a assigns rules 3–6 (and the port of 1–2) "to FRE-1072"** | **Rejected — codex was reading ADR prose without Linear visibility** | Direct `get_issue` on **FRE-1192** ("ADR-0134 T6 — rules 3 to 6 on Grafana, and port rules 1 and 2 off Kibana"): a **separate, already-filed ticket**, `BLOCKED BY FRE-1072`, currently `Needs Approval`, that owns exactly this work with its own 6 acceptance criteria. ADR-0134's own risk table (line 570) states *"FRE-1072's own ticket carries the port [of rules 1–2] as an explicit obligation"* — singular, about rules 1–2 only, which AC-9 already discharges via FRE-1187's quoted abandon verdict (nothing was ever authored on Kibana to port). Rules 3–6 were never FRE-1072's — ADR-0134 D2a's "on Grafana, with FRE-1072" language is a *timing* marker (rules 3-6 wait until Grafana exists), not a scope assignment into FRE-1072's own diff; FRE-1192 is the literal, concrete ticket that resolves the timing marker into actual scope. **No change to the plan.** (Side note, not actionable here: ADR-0134's prose at line 121 and FRE-1192's own description both still say FRE-1072 "retires Kibana" — stale, pre-dating the 2026-08-07 Kibana-retention amendment. Not this ticket's doc to fix; flagged in the Linear handoff for whoever next touches ADR-0134.) |
| One PR is too large for the corrected scope (15 dashboards + everything else) | **Acknowledged, not changed** — reaffirmed as one PR, restructured internally | Matches §6 below (unchanged reasoning: this is one ADR phase, and lifecycle-rules' halt condition prohibits bundling *multiple* phases into one PR, not building one large phase as one PR). Explicitly flagging the size to the owner at approval time, same as originally planned, now with the corrected (larger) dashboard count as part of that flag. |

---

## 1. Findings from pre-implementation research (verified, not assumed)

| # | Finding | How it was verified | Consequence for the plan |
|---|---|---|---|
| F1 | The OTel Collector service does **not exist** in this repo. `otel_bootstrap.py:6` names its attachment as FRE-1070's future work. FRE-1072 is `relatedTo` FRE-1070, not `blockedBy` it. | repo-wide grep (`otel-collector`\|`otelcol`), read `otel_bootstrap.py` | I do **not** build the Collector. Tempo's OTLP receiver is stood up standing-ready; every AC that needs a span uses fixture injection straight at Tempo's own OTLP receiver, exactly as the ticket's AC preamble states. A compose comment notes the Collector attaches under FRE-1070. |
| F2 | FRE-1187's recorded, merged verdict is **abandon the Kibana alerting stage** — no connector under the basic licence delivers outside the box (AC-3/AC-6 of that ticket). Rules 1/2 were never authored on Kibana. | `list_comments` on FRE-1187, its "Handoff for master" comment, quoted in full below | **AC-9 is satisfied by quotation, not by building anything.** No Grafana unified-alerting rule port, no investigation-surface rebuild. This removes what would otherwise have been the single largest scope item after the dashboard rebuild. |
| F3 | Dev `docker-compose.yml` has no `networks:`/resource-limit blocks (lean); `docker-compose.cloud.yml` has full `mem_limit`/`cpus`/`networks: [cloud-sim]`/`container_name` blocks, no separate Kibana data volume (stateless UI), and its `volumes:`/`networks:` sections are declared once at file end. | direct read of both files | New services get lean blocks in dev compose, full resource-limited blocks in cloud compose, matching Kibana's exact shape. Tempo/Grafana each get one new named volume in the cloud file's `volumes:` block (`tempo_data_cloud`, `grafana_data_cloud`), mirroring `es_data_cloud`. |
| F4 | Kibana has **no Caddy site block** — its Cloudflare Tunnel host routes straight to `kibana:5601`, bypassing Caddy, exactly the topology ADR-0129 D6 mandates for Grafana. Confirmed via grep over `config/cloud-sim/Caddyfile` (no `kibana`/`monitoring.` match). | grep + ADR-0129 D6 quote | Grafana gets no Caddy entry either. |
| F5 | `check_no_deployment_identifier.py` denylists the literal domain by regex over all tracked text files (not an allowlist) — any tracked file mentioning the real domain fails pre-commit. Existing placeholder convention: bare `monitoring` / `observe` as inert comment-only host names, `*.example.com` for env-var defaults. | read the script; grep existing compose files | Every new comment/doc referencing Grafana's tunnel host uses **`observe`** only (ADR-0129 D6 names this placeholder explicitly), never a literal domain. |
| F6 | Compose render tests (`tests/scripts/test_kibana_compose_service.py`) use a two-class pattern: a source-only class (`yaml.safe_load`, no docker) that always runs, and a render class (`@skipif(no docker)`) that shells `docker compose ... -f docker-compose.cloud.yml -f tests/scripts/fixtures/gateway_render_override.yml config`. The override fixture neutralizes the gateway's required `env_file` so render tests work off-VPS. | read the test file + fixture + commit `c8c8ed70` | New Tempo/Grafana compose tests mirror this exact two-class shape and **must** pass the gateway override fixture, or they silently pass locally (VPS has the real `/opt/seshat/.env`) and hard-fail in CI/fresh-clone — the same bug FRE-1187's Step-8 review just caught on the Kibana test. |
| F7 | **Corrected in Revision 2 (was wrong — see §0):** the actual dashboard inventory is **15** files, per `config/kibana/import_dashboards.sh`'s own `FILES=(...)` array (source of truth) and confirmed by `ls`: system_health, task_analytics, request_timing, request_traces, self_improvement_funnel, extraction_retry_health, llm_performance, expansion_decomposition, intent_classification, prompt-cost-cache, cost_budget, traversal_gate, monitors_joinability_slm, turn_session_artifact, context_occupancy. The README (`config/kibana/dashboards/README.md`) is stale — missing 5 of these and naming 2 files that don't exist. | `config/kibana/import_dashboards.sh` FILES array + `ls config/kibana/dashboards/*.ndjson` (not the README) | AC-6's inventory is these 15. `request_timing` (E2E duration over time) is the natural home for AC-4's Tempo-sourced duration panel — no 16th dashboard needed. The README's staleness gets a one-line fix folded into this PR. |
| F8 | Grafana env-var config confirmed live against the current docs: `GF_AUTH_ANONYMOUS_ENABLED`, `GF_AUTH_ANONYMOUS_ORG_ROLE`, `GF_AUTH_DISABLE_LOGIN_FORM` (default `false`, only hides `/login`'s form — does **not** affect basic auth), `GF_AUTH_BASIC_ENABLED` (default `true`, unaffected by the above), `GF_SERVER_ROUTER_LOGGING` (default `false`, needed for AC-10's positive control), `GF_SECURITY_ADMIN_PASSWORD`/`GF_SECURITY_ADMIN_USER` (password set **once**, on first boot into the persisted `grafana.db` — a later env-var change alone does not rotate it on a persisted volume). | WebFetch against grafana.com/docs (current, not training-data recall) | Confirms the ticket's own careful wording ("login **form** hidden... basic auth stays enabled") is achievable exactly as specified. The first-boot-only password caveat goes into the plan's runbook notes — relevant if the admin password ever needs rotating later, not for this ticket's ACs. |
| F9 | Tempo `query_frontend.metrics.max_duration` default is **24h** exactly as the ticket states (verified against current Tempo config-reference docs, not recalled). `compactor.compaction.block_retention` default is already **336h** (14d) — no override needed there, only `query_frontend.metrics.max_duration` needs raising. | WebFetch against grafana.com/docs/tempo | Only one Tempo config key changes from default for AC-1; keep the confusion between the two duration settings out of the diff by only touching the one AC-1 actually needs. |
| F10 | Grafana's Elasticsearch datasource supports a logs→trace `dataLinks` entry of shape `{field: <regex-matched-field>, datasourceUid: <tempo-uid>, url: '$${__value.raw}'}`; the Tempo datasource supports `jsonData.tracesToLogsV2` with `datasourceUid`, `filterByTraceID`, `customQuery`, `query` for the trace→logs direction. | WebFetch against grafana.com/docs (provisioning example pages) | Both directions of AC-3 are provisioned via `config/grafana/provisioning/datasources/datasources.yaml` — no UI clicking needed for datasource wiring itself. Exact query-string correctness (matching this repo's `trace_id` field name/format) is verified live against fixture data per AC-3's own test, not assumed from docs. |
| F11 | **Corrected in Revision 2 (was wrong — image existence alone is not a security-current claim; see §0):** Docker and outbound registry access both work in this build environment. Final pins, each verified live (pulled, and for Grafana the bundled Tempo plugin's own version inspected inside the running image): **`grafana/tempo:2.10.7`** (2.10.1 has CVE-2026-27878, a network-triggerable memory-exhaustion DoS, fixed ≥2.10.2; 2.10.7 is the current patch of the 2.10.x line, 2.11.0 not yet tagged — a 3.0.x major line also exists but a major bump carries more config-schema risk than this ticket needs to take on) and **`grafana/grafana:13.1.3`** (13.1.1's *bundled* Tempo datasource plugin reports internal version `13.1.2`, one patch behind the fix for `GL-Vuln VUL-2026-0062`, a path-traversal bug; `13.1.3` core ships bundled plugin `13.1.3`, confirmed patched). | live `docker pull` + Docker Hub tag probes + WebFetch against `grafana.com/security/security-advisories/` and the Tempo plugin changelog + live in-image inspection (`docker run --user root --entrypoint sh ... find .../plugin.json`) | The compose stack can be built and exercised live in this session — ACs 1–5, 9, 10 are not paper criteria, they run for real. Image pins go directly into the compose files, not a floating tag, and are the security-current patch on each line, not merely "exists." |
| F12 | The Tempo image is genuinely distroless: `docker export grafana/tempo:2.10.7 \| tar -tv` finds zero matches for `sh`/`wget`/`busybox`/`curl` anywhere in the filesystem, and `--entrypoint sh` fails with `exec: "sh": executable file not found`. The binary also has **no** `--health`/`-health` flag (`flag provided but not defined: -health`) — no in-container self-check is possible at all, on this image, by any mechanism. | live `docker run`/`docker export` against the actual pinned image | No Docker `HEALTHCHECK`/`test:` block is possible for the `tempo` service. `grafana`'s `depends_on: tempo` downgrades from `condition: service_healthy` to `condition: service_started` — real readiness is proven by the test suite's own live polling against Tempo's HTTP API, which is a stronger integration-level check than Docker's healthcheck field would have been anyway. `grafana`'s own image **does** have `/usr/bin/wget` and `/usr/bin/curl` (confirmed live) — its `wget`-based healthcheck is unaffected. |
| F13 | The dashboard corpus spans multiple distinct `(index-pattern, timeFieldName)` pairs, not one: `agent-logs*`/`@timestamp` (this is the family that carries `trace_id`, per its own `fieldAttrs`), `agent-captains-reflections-*`/`timestamp`, `agent-captains-captures-*`/`timestamp`, `agent-insights-*`/`timestamp`, `agent-captains-funnel-events-*`/`@timestamp`, plus `started_at` (joinability monitor indices) and `probed_at` (SLM health) confirmed via their ES index templates. | direct parse of `config/kibana/dashboards/data_views.ndjson` + grep over `docker/elasticsearch/*.json` | A single generic Elasticsearch datasource cannot serve this corpus — Grafana's ES datasource binds one index expression + one time field per datasource. Provisioning needs one ES datasource per distinct pair actually used by the 15 dashboards (discovered per-dashboard during the rebuild, §3.7); the Tempo↔logs correlation (AC-3) specifically targets the `agent-logs*`/`@timestamp` datasource, since that is where `trace_id` actually lives. |

---

## 2. What is NOT being built (scope boundary, explicit)

- **No OTel Collector service** (F1) — Tempo's receiver stands ready; FRE-1070 attaches the Collector later.
- **No Grafana unified-alerting rules** (F2) — AC-9 closes by quoting FRE-1187's abandon verdict.
- **No Kibana removal, no Caddy site block for Grafana, no `monitoring` tunnel repoint** — explicitly out of scope per the ticket and ADR-0129 D6 amendment.
- **No Cloudflare Tunnel ingress rule or Access policy** — owner action in the ticket's own runbook, outside this repo (ADR-0129 D6, AC-10's own text).
- **No historical backfill/reindex** (AC-8) — nothing in this plan touches pre-cutover ES data.

---

## 3. File-by-file changes

### 3.1 `docker/tempo/tempo.yaml` (new)
```yaml
server:
  http_listen_port: 3200

distributor:
  receivers:
    otlp:
      protocols:
        grpc:
          endpoint: 0.0.0.0:4317
        http:
          endpoint: 0.0.0.0:4318

ingester:
  max_block_duration: 5m

compactor:
  compaction:
    block_retention: 336h   # 14d — already the documented default (F9); explicit for clarity

query_frontend:
  metrics:
    max_duration: 336h   # 14d — AC-1: default is 24h and would reject the fortnight query (F9)

storage:
  trace:
    backend: local
    local:
      path: /var/tempo/traces
    wal:
      path: /var/tempo/wal
```

### 3.2 `docker-compose.yml` (dev) — add `tempo` and `grafana` services + 2 named volumes
- `tempo`: image `grafana/tempo:2.10.7` (F11 — patched), mounts `docker/tempo/tempo.yaml` read-only,
  exposes `3200` (HTTP/query), `4317`/`4318` (OTLP gRPC/HTTP receiver — fixture injector target for
  AC-2), named volume `tempo_data`. **No `healthcheck:` block** — the image is distroless with no
  shell and no self-check flag (F12); readiness for dependents is proven by the test suite's live
  polling, not Docker's healthcheck field. Comment notes the OTel Collector attaches here under
  FRE-1070; nothing in this ticket depends on it. Ports stay unbound (all interfaces), matching every
  other dev-compose service — not loopback-restricted (that's the cloud file's job; see the rejected
  finding in §0).
- `grafana`: image `grafana/grafana:13.1.3` (F11 — patched bundled Tempo plugin), env vars per F8
  (anonymous Viewer, login form hidden, basic auth on, router logging on, admin password from
  `${GRAFANA_ADMIN_PASSWORD:-grafana_dev_password}`), mounts `config/grafana/provisioning` and
  `config/grafana/dashboards` read-only, named volume `grafana_data`, `depends_on: tempo (started —
  not healthy, F12), elasticsearch (healthy)`, healthcheck against `/api/health` (Grafana's image has
  a real shell + `wget`, confirmed live — unaffected by F12), port `3000`.
- `volumes:` block gains `tempo_data:` and `grafana_data:`.

### 3.3 `docker-compose.cloud.yml` — add `tempo` and `grafana` services + 2 named volumes
Mirrors Kibana's cloud block shape exactly (F3, F4, F5):
```yaml
  # Tempo 2.10.7 — trace storage/query backend (ADR-0129 D6). Internal only, no tunnel host —
  # Grafana is the UI; nothing external talks to Tempo directly. No Docker healthcheck: the image
  # is distroless (no shell, no wget, no self-check flag — verified live) so no in-container check
  # is possible; Grafana depends on tempo's container having started, not a health condition.
  # Resource limit: 512MB RAM, 0.5 CPU
  tempo:
    image: grafana/tempo:2.10.7
    container_name: cloud-sim-tempo
    volumes:
      - ./docker/tempo/tempo.yaml:/etc/tempo.yaml:ro
      - tempo_data_cloud:/var/tempo
    command: ["-config.file=/etc/tempo.yaml"]
    mem_limit: 512m
    cpus: 0.5
    networks:
      - cloud-sim
    restart: unless-stopped

  # Grafana 13.1.3 — trace/log visualization UI behind an observe Cloudflare Tunnel host
  # Purpose: Dashboard UI for Tempo traces + Elasticsearch logs (ADR-0129 D6). Kibana is
  # retained, not replaced — see the kibana block above and ADR-0129 D6's 2026-08-07 amendment.
  # Access: CF Zero Trust backend_admin policy, scoped to the owner alone (runbook action, not
  # decided in this repo — ADR-0129 D6's own AC-10 text)
  # Tunnel: observe → http://grafana:3000 (bypasses Caddy, follows Kibana's topology, not Caddy's)
  # Resource limit: 512MB RAM, 0.5 CPU
  grafana:
    image: grafana/grafana:13.1.3
    container_name: cloud-sim-grafana
    environment:
      - GF_AUTH_ANONYMOUS_ENABLED=true
      - GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer
      - GF_AUTH_DISABLE_LOGIN_FORM=true
      - GF_AUTH_BASIC_ENABLED=true
      - GF_SECURITY_ADMIN_PASSWORD=${GRAFANA_ADMIN_PASSWORD}
      - GF_SERVER_ROUTER_LOGGING=true
    volumes:
      - ./config/grafana/provisioning:/etc/grafana/provisioning:ro
      - ./config/grafana/dashboards:/etc/grafana/dashboards:ro
      - grafana_data_cloud:/var/lib/grafana
    ports:
      # Localhost only — use SSH tunnel: ssh -L 3000:localhost:3000 <your-vps-ssh-alias>
      - "127.0.0.1:3000:3000"
    depends_on:
      tempo:
        condition: service_started
      elasticsearch:
        condition: service_healthy
    healthcheck:
      test: ["CMD-SHELL", "wget -q --spider http://localhost:3000/api/health || exit 1"]
      interval: 10s
      timeout: 5s
      retries: 10
      start_period: 30s
    mem_limit: 512m
    cpus: 0.5
    networks:
      - cloud-sim
    restart: unless-stopped
```
`volumes:` block gains `tempo_data_cloud:` / `grafana_data_cloud:` (`driver: local`, matching
`es_data_cloud`'s comment style).

### 3.4 `.env.example` — document `GRAFANA_ADMIN_PASSWORD`
One line in the same style as `POSTGRES_PASSWORD`/`NEO4J_PASSWORD` (line ~12-14 block), plus a
commented default near the dev section, matching existing convention. Runbook note added alongside
it (adopted from codex review, §0): `GF_SECURITY_ADMIN_PASSWORD` only takes effect on **first boot**
into a fresh `grafana.db` — changing the env var later, against an already-initialized persisted
volume, does not rotate the password. Rotating it on a running instance needs the Grafana admin API
(`PUT /api/admin/users/:id/password` as an already-authenticated admin) or a volume reset, not just
an env var change.

### 3.5 `config/grafana/provisioning/datasources/datasources.yaml` (new)
Corrected from Revision 1 (§0): `$$`-escapes the Tempo-side query (Grafana provisioning expands
single-`$` as an env var — this was a real, self-inconsistent bug against the plan's own correctly-
escaped ES-side link), adds non-zero span time shifts, and — per F13 — provisions **one Elasticsearch
datasource per distinct `(index-pattern, timeFieldName)` pair** the 15 dashboards actually use, not
one generic datasource. The exact set of ES datasources is finalized during §3.7's per-dashboard
rebuild (each dashboard's original Kibana index-pattern reference tells us which one it needs); the
five confirmed up front (F13) are enough to unblock AC-1–AC-5 and the trace-to-logs pair:
```yaml
apiVersion: 1
prune: true   # F0 — a datasource removed from this file is removed from grafana.db, not left stale

datasources:
  - name: Tempo
    type: tempo
    uid: tempo
    access: proxy
    url: http://tempo:3200
    jsonData:
      tracesToLogsV2:
        datasourceUid: es-agent-logs
        spanStartTimeShift: "-2s"
        spanEndTimeShift: "2s"
        filterByTraceID: true
        customQuery: true
        query: 'trace_id:"$${__span.traceId}"'   # note the doubled $ — F0, provisioning expands single-$
      nodeGraph:
        enabled: true

  # AC-3's pair: agent-logs* is where trace_id actually lives (F13).
  - name: Elasticsearch - agent-logs
    type: elasticsearch
    uid: es-agent-logs
    access: proxy
    url: http://elasticsearch:9200
    jsonData:
      index: "agent-logs*"
      timeField: "@timestamp"
      dataLinks:
        - field: trace_id
          datasourceUid: tempo
          url: '$${__value.raw}'

  - name: Elasticsearch - captains-captures
    type: elasticsearch
    uid: es-captains-captures
    access: proxy
    url: http://elasticsearch:9200
    jsonData:
      index: "agent-captains-captures-*"
      timeField: "timestamp"

  - name: Elasticsearch - captains-reflections
    type: elasticsearch
    uid: es-captains-reflections
    access: proxy
    url: http://elasticsearch:9200
    jsonData:
      index: "agent-captains-reflections-*"
      timeField: "timestamp"

  - name: Elasticsearch - insights
    type: elasticsearch
    uid: es-insights
    access: proxy
    url: http://elasticsearch:9200
    jsonData:
      index: "agent-insights-*"
      timeField: "timestamp"

  # Additional ES datasources (joinability/started_at, SLM-health/probed_at, user-turn-ratings,
  # funnel-events, etc.) added in §3.7 as each dashboard's own rebuild identifies its source.
```
Exact query correctness (matching this repo's `trace_id` field format) is verified live against
fixture data (AC-2/AC-3's own test), not assumed from docs — this is genuinely new wiring.

### 3.6 `config/grafana/provisioning/dashboards/dashboards.yaml` (new)
Standard file-provider provisioning block, `path: /etc/grafana/dashboards`, `allowUiUpdates: false`
(load-bearing per ADR-0129 D6: dashboards are files, not UI-assembled — F7/D6 quote), `prune: true`
(§0 — adopted so a dashboard removed from this repo is removed live, not left stale in `grafana.db`).

### 3.7 `config/grafana/dashboards/*.json` (new, 15 files + the AC-4 panel folded into `request_timing`)
Corrected from Revision 1 (§0): the real inventory is **15** files (F7), not 12 —
system_health, task_analytics, request_timing, request_traces, self_improvement_funnel,
extraction_retry_health, llm_performance, expansion_decomposition, intent_classification,
prompt-cost-cache, cost_budget, traversal_gate, monitors_joinability_slm, turn_session_artifact,
context_occupancy. One JSON dashboard model per Kibana equivalent. Built by running the dev compose
stack locally with fixture data loaded (AC-2/AC-3's fixtures plus a synthetic day of turn-duration
spans for AC-4), constructing each panel's query in Grafana's own UI/API against the live
datasources — **not hand-authored blind** (same reasoning `create-visualization` applies to Kibana
Lens: query DSL/TraceQL correctness is exactly the kind of thing that looks plausible and is wrong
when typed without a live datasource to check against) — then exporting the resulting dashboard JSON
model into this directory. `request_timing`'s Grafana equivalent gets the AC-4 panel: TraceQL
metrics query against Tempo for per-day p50/p95 span duration, no ES field in that panel's query.
Each dashboard's rebuild also records, one line per dashboard, source panel count → target panel
count (§0 — adopted lightweight reconciliation note, not a new AC beyond what AC-6 itself asks for)
in the PR description. `config/kibana/dashboards/README.md` gets its stale list corrected to the
real 15 as part of this same PR (cheap, adjacent, folded in per Step 5 — not a new ticket).

### 3.8 Tests
- `tests/scripts/test_tempo_compose_service.py`, `tests/scripts/test_grafana_compose_service.py`
  (new) — mirror `test_kibana_compose_service.py`'s two-class shape (F6): source-class assertions
  (image pins, no secret under `environment:`, volume mounts present, **no `healthcheck:` on the
  `tempo` service** — F12) always run; render-class assertions (`docker compose ... config` fully
  resolves) skip without docker, **and pass the `gateway_render_override.yml` fixture** for any
  `docker-compose.cloud.yml` render (F6's own bug, not to be reintroduced).
- `tests/integration/test_fre1072_tempo_grafana_acceptance.py` (new, `@pytest.mark.integration`,
  requires the dev compose stack up) — one test function per AC-1 through AC-5, AC-9, AC-10, run
  against a live `docker compose up -d tempo grafana elasticsearch` with fixture injection. Since
  `tempo` has no Docker healthcheck (F12), this test module's own fixture setup polls Tempo's
  `/ready` HTTP endpoint directly (from the test runner, which has real HTTP tooling, unlike the
  distroless container) before proceeding — that poll **is** the readiness gate. AC-3's test inspects
  Grafana's actual computed data-link/frame metadata from a live query response (§0 — adopted), not
  a hand-replicated equivalent query. AC-6/AC-7 are proven by the existing per-module test suites
  (F7's real 15 dashboards; the four ES-consumer test files identified in research) plus one script
  that walks all 15 provisioned dashboards' panel queries via Grafana's API and asserts no
  query/datasource error.
- AC-8 is proven by diff inspection (no reindex/backfill code), stated directly in the PR/handoff,
  no test needed.

---

## 4. Acceptance-criteria → proof mapping

| AC | Proof |
|---|---|
| AC-1 | Integration test: TraceQL metrics query with a 14-day window against live Tempo returns a normal (possibly empty) response, not a `max_duration` error. |
| AC-2 | Integration test: inject fixture span at Tempo's OTLP receiver (`4317`), fetch by trace id via Tempo's query API, assert id equality. |
| AC-3 | Integration test: fixture span (AC-2) + a fixture ES log record sharing its `trace_id`; follow Grafana's trace→logs link, assert the ES record returns; follow logs→trace, assert the same trace id resolves. |
| AC-4 | Dashboard-panel test: `request_timing`'s Grafana panel query (TraceQL metrics, span duration only) against fixture spans returns a non-empty point for the day they exist on; source-inspect the panel JSON to assert no ES field is unioned in. |
| AC-5 | (a) `GET /api/health` on Grafana returns healthy; named dashboard panel (fixture-backed) returns the exact injected fixture. (b) `docker-compose.cloud.yml` still declares `kibana`; live `/api/status` (VPS-only skip, matching F6's pattern) reports available. |
| AC-6 | Script iterates all 15 provisioned dashboards' panel queries via Grafana's HTTP API, asserts none errors; PR description carries the per-dashboard panel-count reconciliation note (§3.7, §0). |
| AC-7 | Existing named tests for the four consumers (F-table in research: `test_es_indexer.py`, `test_engine.py`'s ES-linkage tests, `test_feedback_api.py`'s ES-shape tests, `test_snapshotter.py`/`test_silence_monitor.py` for cost_gate's Postgres-primary path) re-run post-change, all green — recorded as the AC-7 evidence, no new test needed since nothing in this diff touches those code paths. |
| AC-8 | Diff inspection: no reindex/backfill script anywhere in the change. Stated directly. |
| AC-9 | Quoted FRE-1187 verdict (abandon the Kibana alerting stage) — no rule was ever authored, nothing to port (F2). |
| AC-10 | Integration test, three behavioral checks per the ticket's own spec: (a) anonymous `POST /api/ds/query` succeeds, anonymous `POST /api/dashboards/db` returns 403; (b) `/login` fetched, login form markup absent; (c) `GET /api/admin/settings` denied anonymously, succeeds with admin basic auth. |

---

## 5. Atomic steps

1. `docker/tempo/tempo.yaml` — write config (§3.1). Verify: `docker run --rm -v $PWD/docker/tempo/tempo.yaml:/etc/tempo.yaml grafana/tempo:2.10.7 -config.file=/etc/tempo.yaml -config.verify` (or equivalent dry parse) does not error.
2. `docker-compose.yml` — add `tempo` service, no healthcheck block (§3.2, F12). Verify: `docker compose config` resolves; `docker compose up -d tempo` reaches `running`; `curl localhost:3200/ready` from the host succeeds.
3. `docker-compose.yml` — add `grafana` service, `depends_on: tempo: condition: service_started`, no provisioning files yet (§3.2). Verify: container starts (will show unprovisioned Grafana), `curl localhost:3000/api/health` healthy.
4. `config/grafana/provisioning/datasources/datasources.yaml` (5 datasources per F13) + `dashboards.yaml`, both with `prune: true` (§3.5, §3.6). Verify: Grafana restart picks up all datasources with no provisioning error in logs.
5. Inject fixture span (AC-2) + fixture ES log record sharing `trace_id` (AC-3) via a small script under `scripts/`. Verify manually via Tempo/Grafana UI/API that AC-2 and AC-3 hold, before writing the assertions.
6. Write `tests/integration/test_fre1072_tempo_grafana_acceptance.py` AC-1/AC-2/AC-3 cases against the running stack (TDD: write, confirm fail pre-config where applicable, confirm pass now). AC-3's assertion inspects Grafana's actual returned link/frame metadata, not a hand-replicated query (§0).
7. Set `query_frontend.metrics.max_duration: 336h` in `docker/tempo/tempo.yaml` if step 6's AC-1 test fails first against the 24h default (confirms the test actually exercises the setting) — this is the deliberate TDD ordering the ticket's own AC-1 wording implies.
8. Build the `request_timing` dashboard's AC-4 panel live in Grafana against fixture spans; export JSON to `config/grafana/dashboards/request_timing.json`; write the AC-4 test.
9. Docker-compose.cloud.yml — add `tempo`/`grafana` cloud blocks (§3.3, no Tempo healthcheck, `service_started` dependency), `.env.example` entry + rotation note (§3.4). Verify: `docker compose -f docker-compose.cloud.yml -f tests/scripts/fixtures/gateway_render_override.yml config` resolves.
10. `tests/scripts/test_tempo_compose_service.py`, `test_grafana_compose_service.py` (§3.8, F6 shape) — source-class assertion confirms no `healthcheck:` key exists on the `tempo` service, guarding against a future contributor adding back an unusable one.
11. Grafana auth env vars (already in step 3/9's blocks) — write and run the AC-10 integration test against the running stack; fix config until all three checks pass.
12. AC-5 test (health + fixture-backed dashboard panel + Kibana-still-present check).
13. Build remaining 14 dashboards (§3.7) one at a time against the running stack with real fixture/live data, adding each dashboard's own ES datasource to `datasources.yaml` as its index-pattern/timeField becomes known (F13); export each dashboard; extend the AC-6 panel-walk script to cover all 15; confirm no query/datasource error per dashboard as each lands; record the per-dashboard panel-count reconciliation line (§0) in the PR description as each lands. Fix `config/kibana/dashboards/README.md`'s stale list in the same pass.
14. Re-run the four AC-7 consumer test files unmodified; record pass.
15. Write the AC-8/AC-9 statements directly into the PR description and Linear handoff (no code); AC-9 quotes FRE-1187's abandon verdict and notes FRE-1192 owns rules 3–6 + the port, per §0.
16. Full quality gates (Step 8 of the build skill) — `make test`, `make mypy`, `make ruff-check`/`format`, `pre-commit run --all-files`.
17. Self-review (`feature-dev:code-reviewer` + `security-review`, scoped `git diff origin/main...HEAD`) — this diff is **escalated** (production-adjacent: stands up two new services with anonymous network exposure and an admin credential, direct precedent to FRE-1187's own escalation reasoning) — self-serve review still runs and fixes land on-branch, flagged for owner `/code-review ultra` before merge per the skill.
18. PR + Linear handoff comment.

---

## 6. Risk / escalation notes (post codex plan-review, §0)

- Step 13 (14 remaining dashboards, corrected from 11 — F7) is the dominant size driver, now larger
  than Revision 1 estimated. Reaffirming: one PR is still the right shape (this is one ADR phase,
  ADR-0129 B7 — lifecycle-rules' halt condition prohibits bundling *multiple* phases into one PR, not
  building one large phase as one PR), structured as many small reviewable commits. **Flagging the
  size explicitly to the owner at approval time**, given both codex's independent size concern and
  the corrected (larger) dashboard count.
- The ES `dataLinks` → Tempo internal-link mechanism (F10) and the multi-datasource design (F13) are
  the two pieces of wiring not confirmed against a worked example from current docs — flagging as the
  highest-uncertainty items; AC-2/AC-3's own tests are the actual verification, not the docs excerpts.
- Two image pins were security-bumped past what "confirmed to exist and pull" alone would have caught
  (F11: Tempo 2.10.1→2.10.7 for CVE-2026-27878; Grafana 13.1.1→13.1.3 for the bundled Tempo plugin's
  path-traversal fix) — both verified live against the actual pinned images, not taken on report.
- Anonymous Viewer access to *every* Grafana datasource org-wide is a known, ADR-accepted residual
  (ADR-0129 D6 states it explicitly, justified as a non-regression vs. Kibana's identical exposure
  today) — not re-litigating it here, but naming it so codex doesn't flag it as a fresh finding.
