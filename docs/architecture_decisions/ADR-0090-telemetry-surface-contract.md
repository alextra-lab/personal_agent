# ADR-0090 — Telemetry Surface Contract (Three-Way Reconciliation of Emit ↔ Mapping ↔ Dashboard)

**Status:** Accepted — 2026-06-21 (FRE-582; status reconciled — shipping via FRE-533 ✅ / FRE-540 ✅, FRE-555 Approved). Originally Proposed 2026-06-08. **D3/D5/D6 amended 2026-08-08 (FRE-1213)** — the dashboard corner's realization moves from Kibana NDJSON to Grafana JSON; the contract itself is unchanged (see Status Updates).
**Related:** ADR-0088 (execution-topology emission seam — the *runtime* boundary that produces events; this ADR governs the *storage + display surface* those events land on — complementary, non-overlapping), ADR-0074 (joinability / identity — join keys must be `keyword` for exact-match term joins, the single most trap-exposed mapping requirement), ADR-0083 (SLM cross-tunnel health monitor — a currently dynamic-mapped family in scope), ADR-0065 (cost_gate — the cost/budget fields most exposed to the `0.0`→`long` trap), ADR-0069 / ADR-0089 (artifact envelope — `envelope_ok`/`csp_present`/… fields already in the `agent-logs` template), FRE-407 (per-turn ratings — the `dynamic:false` + `_meta` exemplar this ADR generalizes), FRE-452 (route-trace ledger — a consumer surface that must be reconciled). **Complements:** ADR-0088 — together they form the L0 observability contract: 0088 owns *emission* (does an event leave any topology with identity), 0090 owns *the surface* (is the field correctly mapped and faithfully surfaced).
**Implements:** FRE-504 → **Telemetry Surface Audit** project (cross-linked to **Observability Foundation**, L0). FRE-533 (A1 — three-way reconciliation inventory) is this ADR's foundation realization step; FRE-534/535/536–539 are its mapping/dashboard realization plan; this ADR adds one new ticket (the reconciliation checker, D5).
**Spec:** `docs/specs/SESHAT_PROGRAM_ARCHITECTURE.md` (L0 observability substrate — this ADR is its *surface* contract, as ADR-0088 is its *emission* keystone)
**Evidence:** the 2026-05-10 ES-mapping incident documented in `scripts/setup-elasticsearch.sh:10-16`; the FRE-411 recurrence documented in the same script (`:96-102`); live template/dashboard audit cited inline.

---

## Context

### The measured problem (two documented production failures, one root cause)

A telemetry field only does its job if **three corners agree**:

```
                 emit site (code: structlog / ES writer, file:line)
                      /                                    \
        ES index template / mapping  ←———————————→  dashboard / viz  (see D3)
```

*(**Amended 2026-08-08, FRE-1213.** The third corner read "Kibana dashboard / viz". Kibana's retirement
is directed by ADR-0129 D6 and delivered by FRE-1214, which deletes `config/kibana/` — the directory
this ADR names as the sole dashboard location in git. **The corner survives; its realization moves** to
Grafana. Every dashboard-corner path below is amended accordingly, and the amendment's one dependency
is stated in D3.)*

Two corner-pair breakages have already shipped to production, both recorded in the codebase itself:

1. **2026-05-10 — mapping corner silently wrong** (`scripts/setup-elasticsearch.sh:10-16`): the `agent-logs` template was missing for an extended period, so daily indices were created with **default ES dynamic mapping** (text+keyword for every string). ES|QL term equality **silently returned null**; the agent retried broken queries and exposed downstream bugs. The fix was the setup script — but the *failure mode* is structural, not a one-off.
2. **FRE-411 — same trap, different family** (`scripts/setup-elasticsearch.sh:96-102`): the `slm-requests-*` shipper had no template, so the daily index got default dynamic mapping (`text` join keys) and **exact-match term joins on `trace_id`/`span_id` silently returned nothing** — "the exact failure mode this script's header warns about."

Both are the **dynamic-mapping trap**: a field is emitted, lands in ES under an *inferred* type that is wrong for how it is queried/aggregated, and **fails silently** — no error, just empty results. The agent (and the human reading a dashboard) cannot tell a correct-but-empty panel from a misconfigured one.

### The trap is narrower and sharper than "dynamic mapping is bad"

The live `agent-logs-*` template (`docker/elasticsearch/index-template.json`) is actually sophisticated: `dynamic:true` **plus** `dynamic_templates` that make *strings* safe (`*_id`→`keyword`, enum-suffixes→`keyword`, free-text-suffixes→`text`, default string→`keyword ignore_above:1024`) **plus** ~100 explicit properties. But two gaps remain by construction:

- **Numerics are uncovered by the dynamic_templates.** A *new, not-yet-explicit* float/ratio/cost field first seen as `0.0` still infers `long` — every subsequent non-integer value is then rejected or truncated. The explicit `properties` block catches the *known* numerics (`cost_usd`→`double`, `confidence`→`float`, `duration_ms`→`float`); a new unlisted one is unsafe.
- **Long text/error/digest under the default rule** is `keyword ignore_above:1024` → values over 1024 chars are **silently not indexed** (present in `_source`, absent from search/agg).

So the rule is not "ban dynamic mapping." The repo already shows **two sanctioned disciplines**:
- `agent-logs-*`: `dynamic:true` + dynamic_templates (string-safe) + explicit props for every numeric/long-text field.
- `user-turn-ratings-*` (`docker/elasticsearch/user-turn-ratings-index-template.json`): `dynamic:false` + fully explicit + an `_meta` block that documents *why* (`"dynamic:false prevents prompt_component_ids from drifting to text"`, `managed_by`, `retention_days`).

The failure is **ungoverned** mapping — a field reaching ES with no explicit decision about its type.

### What exists at each corner (the real boundary picture)

| Corner | Mechanism today | State |
|--------|-----------------|-------|
| **Emit** | scattered structlog calls + ES writers across `telemetry/`, `captains_log/`, `observability/` | no registry; the `event` (log-file key) vs `event_type` (ES key) split is papered over by mapping *both* as `keyword` (`index-template.json:49-50`) rather than resolved |
| **Mapping** | `docker/elasticsearch/*.json` templates applied idempotently by `scripts/setup-elasticsearch.sh` | explicit for **5** families (`agent-logs`, `agent-captains-*`, `agent-monitors-joinability`, `slm-requests`, `user-turn-ratings`); **dynamic-mapped** (trap-exposed) for `agent-captains-captures-subagents` (FRE-505/519), `agent-insights-*`, `agent-monitors-slm-health-*` (ADR-0083) |
| **Dashboard** | *(as measured 2026-06)* `config/kibana/dashboards/*.ndjson` imported via `config/kibana/import_dashboards.sh`; **manual** re-export. **Superseded 2026-08-08** — the platform is Grafana, provisioned from files in `config/grafana/dashboards/` (D3) | 12 dashboards (+ `data_views.ndjson`) tracked in git — **but** a second location exists (`docker/kibana/dashboards/prompt-cost-cache.ndjson`), and re-export is a manual curl + a one-off reconstruction script (`config/kibana/dashboards/README.md:33-43`, "see FRE-313 plan"). This row is the *measured* state that motivated D3 and is deliberately left as measured; the platform change does not retroactively fix the asymmetry it records |

The three corners are governed by **three different, unsynchronized mechanisms**. Nothing checks that the triangle closes: a field can be emitted, mapped wrong (or dynamically), and panel-referenced — each corner edited independently. The human-process evidence is blunt (owner, recorded): *"you always get the mappings wrong first pass — always."* Ad-hoc, per-field mapping does not converge; a standing reconciliation does.

### Scope boundary

This ADR owns **the surface contract**: how emit ↔ mapping ↔ dashboard are kept reconciled, version-controlled, and checkable. It does **not** own:

- *What* gets emitted, or with what identity — that is ADR-0088 (the emission seam) and each feature's own ADR (ADR-0074 joinability keys, ADR-0065 cost fields, ADR-0089 envelope fields, …). 0090 governs whether those fields land correctly and surface faithfully, **not** whether they are produced.
- The route-trace ledger schema (FRE-452) or the result-type taxonomy (FRE-451) — 0090 reconciles their *surface* once defined.
- Postgres / Neo4j substrate observability (owner-flagged future work) — this ADR is scoped to the **Elasticsearch** telemetry surface (`agent-*` index families and their dashboards). The *contract shape* generalizes, but the realization here is ES + the dashboard platform. *(Amended 2026-08-08: read "Elasticsearch + Kibana" before that date. **The scope exclusion itself is unchanged** — Grafana's Postgres datasource makes substrate observability newly reachable, but reaching it is a decision for another ADR, not a consequence of swapping dashboard tools.)*

---

## Decision

### D1 — A telemetry field is a three-cornered contract

Every telemetry field is defined by its three corners: **emit site (code) ↔ ES mapping (explicit type) ↔ dashboard reference**. A field is *consistent* only when all three agree, **or** a corner is *intentionally* absent and that absence is documented (e.g. a write-time denorm with no panel yet; a debug field deliberately unmapped). Checking any single pair misses the failure class — the 2026-05-10 and FRE-411 incidents were each invisible to two of the three corners. Reconciliation is **always three-way**.

### D2 — Mapping is governed, never inferred; the trap is numeric + long-text

Every **telemetry index family** — the `agent-*` families **and** the named non-`agent-*` families `slm-requests-*` and `user-turn-ratings-*` — **must** have an explicit index template applied by `scripts/setup-elasticsearch.sh` (the single sanctioned mapping path). "`agent-*`" is shorthand for the in-scope set throughout this ADR; the contract is the *telemetry surface*, not the name prefix. The three currently dynamic-mapped families (`agent-captains-captures-subagents`, `agent-insights-*`, `agent-monitors-slm-health-*`) must get templates (FRE-534). A family uses one of two sanctioned disciplines:

- **Locked** — `dynamic:false` + fully explicit `properties` (the `user-turn-ratings` model). Unknown fields are silently dropped from indexing *by design*; use when the field set is closed.
- **Guarded-dynamic** — `dynamic:true` + `dynamic_templates` that cover **strings *and* numerics** + explicit `properties` for every known numeric/long-text field (the `agent-logs` model, **extended** so a new numeric is not first-inferred as `long`).

Two field classes are mandatory-explicit regardless of discipline, because they fail *silently*:

1. **Numeric / float / ratio / cost / duration** → explicit `double`/`float`/`scaled_float` (never let a first `0.0` infer `long`). This is the `cost_usd`/`confidence`/`*_ms`/budget-ratio class — ADR-0065 cost fields live exactly here.
2. **Long text / error / digest / prompt-blob** → explicit `text` (or `keyword` with a *deliberately chosen* `ignore_above`, not the default 1024) so values are not silently truncated out of the index.

**Join keys are `keyword`** (ADR-0074): `trace_id`, `session_id`, `task_id`, `span_id`, and every `*_id` must be exact-match-safe — the FRE-411 failure was a join key inferred as `text`.

Each template carries a `_meta` block — `managed_by: scripts/setup-elasticsearch.sh`, `retention_days`, and a one-line `description` of any non-obvious choice (e.g. *why* `dynamic:false`). This makes the mapping corner self-describing and ties it back to its source-of-truth path. **Only `user-turn-ratings-index-template.json:25-29` carries this block today**; the exemplar `agent-logs` template (`docker/elasticsearch/index-template.json`) and the other existing templates do **not** — retrofitting `_meta` onto every template is part of FRE-534's mapping-correction pass, not an assumed-present property. The requirement is forward-binding (a new template without `_meta` fails the done-bar) and backfilled for the existing set under the baseline reconciliation (D4/D5).

### D3 — Dashboards are version-controlled files; the live dashboard platform is downstream

> **Amended 2026-08-08 (FRE-1213): the platform is Grafana, not Kibana — directed, not yet realized.**
> The *decision* — dashboards are version-controlled artifacts in git, and the live UI is reconstructed
> from them, never the reverse — is unchanged and is the whole point of D3. What changes is its
> realization: the canonical location **becomes `config/grafana/dashboards/*.json`**, provisioned from
> files as ADR-0129 D6 requires, once `config/kibana/dashboards/*.ndjson` is deleted by FRE-1214.
>
> **State of play as this is written, because an amendment that describes a migration as finished is
> the same defect ADR-0129's own amendment was careful to avoid.** `config/grafana/dashboards/*.json`
> **exists and is committed**; `config/kibana/dashboards/` **also still exists**, and D5's checker
> still defaults to it (`scripts/audit/telemetry_surface_check.py`, `DEFAULT_DASHBOARDS_DIR`), with CI
> still running the reconciliation against Kibana NDJSON. **Both corners are live today.** This
> amendment is therefore **forward-binding** — it fixes which platform the contract points at, so that
> FRE-1214's deletion does not falsify this ADR — and it is **not** a report that the move is done.
> Until T8.1 lands, a reader checking the repo will find the Kibana path still wired, and that is
> expected rather than drift.
>
> **The one dependency, stated rather than assumed.** This amendment presumes the reconciliation
> checker is **repointed** at Grafana panel JSON (`panels[].targets[].{query,metrics[].field,
> bucketAggs[].field}`) — option (a) of the FRE-1203 program's T8.1. If that ticket instead **removes**
> the dashboard corner from the triangle (option (b)), D1's three-cornered contract is reduced to two
> and this ADR needs a larger amendment than this one, not a smaller one. **Option (b) is not
> authorized by this amendment.**
>
> **This amendment strengthens D3 rather than merely relocating it** — but only once realized: Grafana's
> file provisioning makes git → live automatic *and* removes the manual re-export loop that was D3's
> known weak point (below), so the asymmetry D5 was written to make checkable stops existing rather
> than staying checkable.

The git artifact is the **source of truth**; the live dashboard platform is reconstructed from it. The **import direction (git → live) is automated today** via `config/kibana/import_dashboards.sh`. The **export direction (live → git) is manual today** — a hand-run `curl …/_export` plus a per-dashboard reconstruction step the README still flags as planned work ("see FRE-313 plan", `config/kibana/dashboards/README.md:32-43`). Closing that asymmetry (so an edit made in live Kibana reliably round-trips to committed NDJSON) is exactly what D5 makes checkable. Consequences:

- **One canonical location.** All dashboards live under **`config/grafana/dashboards/`** (amended 2026-08-08; previously `config/kibana/dashboards/`). A second location is itself drift — the clause that caught the stray `docker/kibana/dashboards/prompt-cost-cache.ndjson` (FRE-535), and it binds unchanged on the new platform. **During the migration there are legitimately two trees**; "one canonical location" resumes binding as an *invariant* once FRE-1214 deletes the Kibana one, and until then it states which of the two is canonical rather than asserting the other is gone.
- **Git ↔ live drift is a defect.** A panel that exists in the live UI but not in git, or references a field the reconciliation table marks missing, is a finding to fix, not a state to tolerate.
- ~~**The re-export loop is the sync discipline**~~ — **retired 2026-08-08, by removal of its cause, effective as the Kibana tree goes.** Under Kibana, git → live was scripted but live → git was a hand-run `curl …/_export` plus a per-dashboard reconstruction step, and D5 existed partly to make "did you re-export?" checkable. Grafana provisions dashboards *from files* (ADR-0129 D6), so a panel edited in the UI is not the source of truth and cannot silently become one. **The discipline is not merely relocated — the asymmetry it policed ceases to exist**, for Grafana dashboards immediately and altogether once FRE-1214 removes the Kibana tree. What remains is the ordinary loop: edit the committed JSON, redeploy.

### D4 — The reconciliation table is a standing artifact, not a one-off audit

FRE-533 produces the first three-way table (one row per `(field, family)`: emit site `file:line` · emitted type · mapped type · dashboard refs · classification), written dated to `docs/research/`. This ADR makes it a **living artifact**: it is regenerated whenever a telemetry surface changes, not audited once and abandoned. The table is the authoritative input that FRE-534 (fix mappings) and FRE-535 (triage dashboards) execute off — and the fixture the D5 checker is validated against.

**The first table is expected to be full of drift — that is the point, not a failure.** The *current* committed surface already violates the contract: tracked panels reference fields that no template explicitly maps — `role.keyword` and `target_model.keyword` (`config/kibana/dashboards/llm_performance.ndjson`, `task_analytics.ndjson`; the `agent-logs` template maps `model_role`/`model`, not `role`/`target_model`, and its default string rule produces no `.keyword` subfield), and numerics `rounds_needed`/`user_satisfaction` (`delegation_outcomes.ndjson`) that are dynamically inferred. These are **grandfathered baseline drift**: catalogued by the FRE-533 table, resolved by FRE-534/535, and the reason D5's gate is *report-only* until the baseline is triaged (below). The contract does not assume the existing surface is clean; it makes the existing mess legible and fixable.

### D5 — CI teeth: a three-way reconciliation checker, with a decided floor

D1–D4 are enforced by a **checkable reconciliation**, not by review alone (matching ADR-0088's D7 observable-first done-bar). To avoid claiming enforcement the mechanism cannot deliver, this ADR **decides the minimum (floor) checker now** and defers only its *hardening*.

**Decided floor — the mandatory CI check (both corners are fully in-repo, no live stack needed):**

- **Mapping ↔ dashboard, statically.** Parse the family templates (`docker/elasticsearch/*.json`) and the committed dashboard files — **`config/grafana/dashboards/*.json` once T8.1 repoints the checker (amended 2026-08-08); `config/kibana/dashboards/*.ndjson` is what it still reads today.** The field-extraction walk changes shape with the platform: Grafana panels expose their field references as `panels[].targets[].{query,metrics[].field,bucketAggs[].field}` rather than Kibana's `visState` / `kibanaSavedObjectMeta` / `attributes.state`, which is why the repoint is real schema work rather than a path edit. Assert every field a panel references is explicitly mapped in its family's template (no panel reading a never-mapped field → silent-empty), and report mapped-but-never-referenced fields. Both corners are committed files, so this runs in a **hermetic CI job** with no Elasticsearch.
- **Trap-class mapping lint.** For every template, assert numeric/float/ratio/cost and long-text/error/digest fields named by the family's allowlist are explicitly typed (not left to a numeric inference), join keys are `keyword`, and the `_meta` block is present (D2). Pure static lint over the template JSON.

**Additional checks (run where the environment allows, not part of the hermetic floor):**

- **Emit → mapping.** Grep the known emit sites for emitted fields; assert each trap-class emitted field is explicitly mapped. Heuristic at the emit corner (no runtime hook) — a *report*, not a hard gate, until a field registry (open decision) makes it mechanical.
- **Repo template ↔ live mapping.** Where ES is reachable (local/staging), assert `GET /<family>/_mapping` matches the repo template; divergence means the idempotent setup script was not re-run or a field was hot-added live. Environment-gated; cannot run in the hermetic pass.

**Phasing — report-only, then gate.** The checker ships in **report mode** first: it runs against the grandfathered baseline (D4) and prints findings without failing the build, while FRE-534/535 burn the baseline drift down. Once the affected families are triaged green, the floor check **flips to a hard gate** for new or changed `(field, family)` rows. This is the same baseline-then-enforce pattern the project sequence already implies — the gate is honest because it is not asserted to pass on a surface that currently fails it.

**Enforcement honesty (per the ADR-0088 precedent):** even gated, the checker is a *structural + CI* guard over the surface, **not** a mechanical runtime invariant like cost's identity guard. It catches drift at build time; it cannot prevent a field being emitted at runtime that no one mapped — that case surfaces as an `emitted-but-unmapped` row on the *next* run. Stated as convention-plus-CI, not as an impossibility proof. This ADR adds **one new Needs-Approval ticket** for the checker, sequenced after FRE-533's table (the checker is validated against it).

### D6 — Done-bar: a new telemetry surface is not shippable until its three corners reconcile and commit together

A **new or changed** field, index family, or dashboard is **not shippable-to-default** until: (a) its trap-class fields are explicitly mapped in a template (with `_meta`) applied by the setup script (D2); (b) if surfaced, its panel is committed in the canonical location (D3) — **as Grafana JSON since 2026-08-08, previously as Kibana NDJSON**; and (c) the reconciliation checker (D5) passes for the affected family. This binds *new/changed* surfaces; the existing surface is brought up to the bar by FRE-534/535 against the baseline table, not assumed to already pass it (D4). This is the **surface analogue** of ADR-0088's D7 — 0088 gates "is it emitted observably," 0090 gates "is it correctly mapped and *surfaced*" (the persisted storage + dashboard display layer — Grafana since 2026-08-08 — distinct from 0088's live `turn_status` meter). The two done-bars compose: a new orchestration capability passes 0088's bar to be *emitted live*, and 0090's bar to be *persisted, queryable, and dashboarded*.

---

## Consequences

### Positive

- **The two documented silent failures (2026-05-10, FRE-411) become a checked-against class**, not a recurring surprise — the trap (numeric `0.0`→`long`, long-text truncation, `text` join keys) is named and gated.
- **Every `agent-*` family is governed-mapped** — the three dynamic-mapped families are closed; new families inherit the discipline by the done-bar rather than re-discovering the trap.
- **Dashboards have a single source of truth** — git NDJSON, one location, reconstructable; the live/repo drift that hid behind manual re-export becomes a CI finding.
- **Reconciliation is three-way and standing** — the structural blind spot (each corner edited independently) is closed by an artifact that is regenerated, not audited once.
- **Clean complement to ADR-0088** — emission (0088) and surface (0090) are separately owned but compose into one L0 done-bar; neither re-implements the other.
- **Self-describing surface** — `_meta` on every template means the mapping corner explains its own choices and points back to its source path.

### Negative / tradeoffs

- **A checker to build and maintain (D5).** Walking emit-grep ↔ mapping ↔ committed dashboard files is heuristic at the emit corner (grep/registry, not a runtime hook) and will have edge cases; it is a build-time guard, weaker than a mechanical invariant — acknowledged, not hidden. **Added 2026-08-08:** the checker is coupled to a *dashboard schema*, so a platform change rewrites its field-extraction walk. That cost is real and is **owed, not yet paid** — FRE-1203's T8.1 is the ticket that pays it, and until it lands the checker still walks Kibana NDJSON. A second platform change would charge it again.
- ~~**Re-export discipline is now load-bearing.**~~ **Retired 2026-08-08** — Grafana provisions dashboards from files, so there is no live → git export step to remember. See D3.
- **Up-front mapping cost.** Mandatory-explicit numeric/long-text fields mean a new field needs a template edit before it is safely queryable — slightly slower than emit-and-see, deliberately (emit-and-see is exactly what failed twice).
- **Scoped to the Elasticsearch surface.** Postgres/Neo4j substrate observability is out of scope; the contract *shape* generalizes but is not realized for them here, so "telemetry surface" is not yet portfolio-complete. *(Amended 2026-08-08 — read "Scoped to ES/Kibana" before that date; the exclusion is unchanged.)*
- **Heuristic emit corner.** Without a field registry, the emit→mapping check relies on grepping known sites; a field emitted from an unscanned path is caught only when its index is sampled. A future field registry would harden this (open decision).

---

## Verification

- Re-running `scripts/setup-elasticsearch.sh` against a clean ES applies an explicit template for **every** `agent-*` family (no family left to default dynamic mapping); `GET /<family>/_mapping` shows explicit types for all trap-class fields.
- A synthetic doc with a new float field first valued `0.0` lands as `double`/`float` (not `long`) for any family under the contract; a >1024-char error/digest field is searchable/aggregatable, not silently dropped.
- A term join on `trace_id`/`span_id` returns rows for every family (no `text` join key) — the FRE-411 failure mode is absent.
- The reconciliation checker (D5) run against the live stack passes; a deliberately introduced drift (a panel referencing an unmapped field; a numeric field emitted but left to inference; a live mapping diverging from the repo template) **fails** it with the specific row.
- **`config/grafana/dashboards/` is the sole dashboard location in git** (no second tree); Grafana's file provisioning reconstructs the live UI from it; a live-only panel is reported as drift. *(Amended 2026-08-08 — this line named `config/kibana/dashboards/` and `import_dashboards.sh` before that date; it is the line FRE-1214 would otherwise have falsified by deleting the directory it asserts on.* **This verification step is not satisfiable until FRE-1214 lands** *— both trees exist today, so a run against the current repo correctly reports the Kibana tree as the second location. That is the migration, not a defect.)*
- The `docs/research/` reconciliation table (FRE-533) classifies every `(field, family)`; every ⚠️ row carries a one-line resolution direction; FRE-534/535 execute mechanically off it.

## Open decisions (data-gated / to settle in implementation tickets)

- **Hardening the emit corner (D5 floor decided as report-only grep):** whether to promote the emit→mapping check from a heuristic grep to a hard gate backed by a declared **field registry** (a typed catalog the emit sites and templates both derive from). The registry makes emit↔mapping mechanical but is a larger lift; settle once FRE-533's table shows how stable the field set is. The *floor* (static mapping↔dashboard + trap-class lint in hermetic CI) is decided in D5; this is only its upgrade.
- **Adding pre-commit placement (D5 floor is the hermetic CI job):** whether to *also* run the static floor as a pre-commit hook for faster local feedback. The CI floor and the environment-gated live-mapping check are decided (D5); a pre-commit copy is an optional convenience, not a missing decision.
- **`event` vs `event_type` resolution:** whether to converge the two keys at the emit corner (one canonical key) or keep both mapped and documented as an intentional split — currently both are mapped `keyword` and read via dual-key fallback. Settle with the emit-site owners (cross-refs the recorded log-vs-ES key split).
- **Retention/ILM as a fourth surface attribute:** whether ILM policy per family belongs inside this contract (it is adjacent — `_meta.retention_days` already hints it) or stays an ADR-0074-style separate concern.

## References

- Spec: `docs/specs/SESHAT_PROGRAM_ARCHITECTURE.md` (L0 observability substrate — surface vs emission)
- Code / config: `scripts/setup-elasticsearch.sh` (the single mapping path; incident header `:10-16`, FRE-411 recurrence `:96-102`), `docker/elasticsearch/index-template.json` (the guarded-dynamic exemplar), `docker/elasticsearch/user-turn-ratings-index-template.json` (the locked + `_meta` exemplar), `config/grafana/dashboards/` (the committed dashboard source of truth since 2026-08-08, provisioned from files per ADR-0129 D6); *(historical, deleted by FRE-1214: `config/kibana/dashboards/` NDJSON + its `README.md` re-export loop, and `config/kibana/import_dashboards.sh`)*
- Code / CI: `scripts/audit/telemetry_surface_check.py`, wired into `.github/workflows/ci.yml` — D5's floor checker, and the artifact whose Kibana-schema dependency made this amendment necessary rather than cosmetic. Its siblings `scripts/audit/fre533_reconcile.py` and `scripts/audit/verify_fre535_panels.py` are one-shot audit artifacts with the same dependency and no CI wiring
- Research: `docs/research/` reconciliation table (FRE-533, dated, the standing artifact)
- Linear: **Telemetry Surface Audit** project — FRE-533 (A1, foundation, this ADR's first realization), FRE-534 (A2, fix mappings), FRE-535 (B1, dashboard triage + location consolidation), FRE-536–539 (C1–C4, new/enhanced dashboards), + the new D5 checker ticket; cross-link Observability Foundation (FRE-504)
- ADRs: ADR-0088 (emission seam — complement), ADR-0074 (join-key discipline), ADR-0083 (SLM health family), ADR-0065 (cost fields), ADR-0069 / ADR-0089 (envelope fields), FRE-407 (ratings template exemplar)
- ADR-0129 — D6 directs Kibana's retirement (amended 2026-08-08) and requires Grafana's dashboards to be provisioned from files in this repository; that provisioning model is what lets D3 retire its re-export loop rather than reproduce it. Status: Accepted
- ADR-0134 — its D6 amends **this ADR's D6 done-bar** at field grain, applied by its own separate ticket. **Orthogonal to the 2026-08-08 platform amendment** and must not be conflated with it: one changes *where the dashboard corner lives*, the other changes *what the done-bar demands of a changed field*. Status: Proposed
- Linear FRE-1203 / FRE-1213 / FRE-1214 — the Grafana migration program, this amendment's ticket, and the retirement that deletes `config/kibana/`. T8.1 of that program repoints the D5 checker; **option (a) — repoint — is what this amendment assumes** (D3)

---

## Status Updates

### 2026-08-08 — D3's dashboard corner moves from Kibana to Grafana
**Changed By:** `adr` session (FRE-1213).
**Reason:** ADR-0129 D6 was amended the same day to direct Kibana's retirement, delivered by FRE-1214.
That ticket deletes `config/kibana/`, which this ADR names in D3 as *"the sole dashboard location in
git"*, parses in D5's CI checker, requires in D6's done-bar, and asserts in Verification. **This ADR
would have been left contradicting the repository** — not by asserting anything about Kibana's
retention, but by naming a directory that no longer exists as the source of truth for one of its three
corners.

**The decision is unchanged; only its realization moves.** D1's three-cornered contract, D2's mapping
discipline, D4's standing table, D5's checker and D6's done-bar all stand exactly as written. The
canonical dashboard location becomes `config/grafana/dashboards/*.json`.

**This amendment is forward-binding, not a report of a completed migration** — and that distinction is
recorded because adversarial review caught the first draft asserting the move as done. As this is
written, `config/grafana/dashboards/*.json` is committed **and** `config/kibana/dashboards/` still
exists, with D5's checker still defaulting to the Kibana tree and CI still reconciling against it. The
amendment fixes which platform the contract *points at*, so FRE-1214's deletion cannot falsify this
ADR; it does not claim the wiring has moved. A reader finding the Kibana path still live has found the
migration in progress, not drift.

**One clause is retired by removal of its cause, not relocated.** D3's re-export loop existed because
Kibana's git → live direction was scripted while live → git was a hand-run export plus a
reconstruction step, and D5 was partly there to make *"did you re-export?"* checkable. Grafana
provisions dashboards **from files** (ADR-0129 D6), so a panel edited in the UI is not the source of
truth and cannot silently become one. The asymmetry is gone rather than made checkable on a new
platform — a genuine strengthening, recorded as such because the reflexive move would have been to
carry the discipline across unexamined.

**One new cost is recorded honestly.** D5's checker is coupled to a *dashboard schema*: Kibana's
`visState` / `kibanaSavedObjectMeta` / `attributes.state` become Grafana's
`panels[].targets[].{query,metrics[].field,bucketAggs[].field}`. A platform change therefore rewrites
the field-extraction walk. That cost is **owed rather than paid** — T8.1 is the ticket that pays it —
and a second platform change would charge it again. It is now stated in Negative Consequences, where
it was not visible before.

**This amendment has exactly one dependency and it is named rather than assumed.** It presumes the
FRE-1203 program's T8.1 takes option **(a)** — repoint the checker at Grafana panel JSON. If T8.1
instead removes the dashboard corner from the triangle (option (b)), D1's three-cornered contract
collapses to two corners and this ADR needs a substantially larger amendment. **Option (b) is not
authorized here**, and a session that takes it must return to this ADR rather than treat this update
as covering it.

**Deliberately not touched.** The *measured* state table in Context still records the Kibana NDJSON
layout, the second-location drift and the manual re-export as they stood in 2026-06 — that is the
evidence D3 was decided on, and rewriting it to match the current platform would destroy the record of
why the decision was made. **ADR-0134's pending D6 amendment to this ADR's done-bar is orthogonal** and
is applied by its own ticket; the two must not be merged into one edit.
