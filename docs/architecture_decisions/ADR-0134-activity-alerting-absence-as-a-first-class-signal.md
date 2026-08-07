# ADR-0134: Activity Alerting — Absence as a First-Class Signal, on Platform-Native Alerting

**Status:** Proposed
**Date:** 2026-08-07
**Deciders:** Project owner (FRE-1058, owner-directed 2026-08-07)
**Tags:** observability, alerting, absence-detection, telemetry, grafana, elasticsearch

---

## Context

### What is being decided

FRE-1058 asked for a **fourth corner** on ADR-0090's telemetry surface contract — *delivery*, alongside
emit, mapping and dashboard — on the premise that events are emitted and lost in transit and nothing
asks whether they arrived.

The discussion that produced this ADR rejected that framing and arrived somewhere else. The missing
thing is not a corner of the surface contract. It is an **output** for the instruments that already
exist, and the signal class none of them carries is **absence**. This ADR decides what is alerted on,
what discipline every alert obeys, and where alerting lives. It leaves ADR-0090 with a single
one-clause amendment (D6) rather than a fourth corner.

### The instruments exist; the egress does not

Verified against this branch, 2026-08-07:

- Five instruments are built and committed: the joinability probe, the cache-erosion monitor, the
  delivery-ratio probe (FRE-1051), the disk-usage threshold, and the SLM health probe.
- `disk_usage_alert_percent` (`src/personal_agent/config/settings.py:1537`) is the **only** alert
  threshold in settings. `src/personal_agent/telemetry/lifecycle_manager.py:183` computes
  `alert = used_pct >= settings.disk_usage_alert_percent`, and `:206` logs it as a field. **The alert
  is a log line.** It notifies nobody.
- There is **no outbound notification path anywhere in the repository** — no SMTP, no push, no webhook,
  no PWA web push. A search of `src/` for `ntfy`, `pushover`, `telegram`, `slack`, `smtplib` and
  `aiosmtplib` returns nothing.
- `BrainstemScheduler` already runs periodic probes in-process — joinability hourly, SLM health every
  300 s, domain-guard warming. **The periodic execution home exists; it has no outbox.**

So every finding this system produces is written into Elasticsearch and waits for a human to go
looking. For the delivery probe that is circular: it writes its findings *about telemetry loss* into
the telemetry system whose completeness it is measuring.

### The measured failure, and why nothing caught it

FRE-1051 measured **404 `api_cost_recorded` events emitted and never indexed** over 2026-07-23..28 —
82.6 % loss on the 23rd, 47.8 % on the 26th, 52.4 % on the 27th, and zero on the other three days. The
shape was episodic and whole-process, not a sampling rate.

**The diagnosis was corrected during that work, and the correction is what shapes this ADR.** Neither
the unreferenced-task hypothesis nor the shutdown-flush hypothesis measured any loss. The mechanism was
that `add_elasticsearch_handler` has exactly **one** production call site —
`src/personal_agent/service/app.py:754` — and even there it is conditional on the connection
succeeding. A process running outside the FastAPI lifespan binds no sink and ships nothing, silently.
(Still true on this branch: `gateway/app.py:148` constructs an `ElasticsearchHandler` but harvests only
its client for *queries*, never attaching it as a log sink.)

Two facts about that incident matter more than its cause:

1. It ran for at least six days.
2. It was found **weeks later, by accident**, by an explore session reasoning about an unrelated
   question — not by anything watching.

Nothing was watching because there is nothing for anything to watch *with*.

### Why this is not ADR-0090's fourth corner

ADR-0090's three corners share properties that absence does not:

| | emit / mapping / dashboard | absence |
|---|---|---|
| Artifact under audit | committed files in git | production behaviour over time |
| Where it is checked | hermetic CI, no live stack (ADR-0090 D5 floor) | a live window |
| Grain | `(field, family)` — FRE-533's 1023 rows | `(family, window)` |

FRE-533's inventory is keyed `family,field,live_type,…,emit_sites,dashboard_refs` — one row per field.
Delivery cannot become "a fourth column" on it without writing a per-window measurement into a per-field
row. The word *corner* imported a shape that does not fit the obligation.

What ADR-0090 does need from this work is one line in its D6 done-bar. That is decided in **D6** below.

### The platform we have, and why its alerting is off

Verified 2026-08-07, and the single most consequential fact in this ADR:

- Elasticsearch and Kibana are both **8.19.0**; the licence is **`basic`** (`GET /_license`).
- Kibana's `/api/actions/connector_types` returns **HTTP 500**. The cause is **not** licensing. Kibana's
  own startup log states it plainly:

  ```
  [WARN][plugins.encryptedSavedObjects] Saved objects encryption key is not set.
  [WARN][plugins.actions]  APIs are disabled because the Encrypted Saved Objects plugin is
                           missing encryption key.
  [WARN][plugins.alerting] APIs are disabled because the Encrypted Saved Objects plugin is
                           missing encryption key.
  ```

  **Kibana's alerting and actions APIs have been disabled since deployment by a missing
  `xpack.encryptedSavedObjects.encryptionKey`.** Our Kibana service (`docker-compose.cloud.yml:160`)
  sets two environment variables and mounts `docker/kibana/kibana.yml`; the key is in neither. The
  `stackConnectors` plugin loads normally and is waiting behind the disabled API.

This matters because it means **the alerting capability was never absent — it was switched off**, and
the first draft of this ADR wrongly concluded from the 500 that Kibana could not notify, and deferred
the whole decision to Grafana. That is the error this section exists to prevent repeating: a broken
instrument was read as a negative result.

**What remains genuinely unknown** is whether the `basic` tier's connector set includes anything that
leaves the box. Elastic's subscriptions page lists "Server Log and Index" separately from the action
connectors (email, webhook, Slack, PagerDuty, …) without stating the tier boundary. That question is
**now cheaply answerable** — set the key, restart Kibana, enumerate the API — and it is the first
implementation step rather than an assumption baked into the decision.

**FRE-1072** (ADR-0129 B7) brings Tempo and Grafana and retires Kibana. Grafana OSS unified alerting
carries no licence gate and has a first-class **No Data** alert state, so it is the destination for the
full rule set — **planned and ticketed, not hypothetical.** It is also not close: the ADR-0129 chain is
at B2 (FRE-1065 In Progress), with B3–B8 approved behind it, putting FRE-1072 roughly five sequenced
tickets out. That distance, against a failure class that already ran six days undetected, is what
makes the staging in D2a a real decision rather than a formality.

### Scope boundary

This ADR owns **what is alerted on, and the discipline every alert obeys**. It does *not* own:

- **The transport** — notifier, retry, deduplication, grouping, silences. Those are the platform's,
  deliberately (D2).
- **The log-delivery mechanism.** FRE-1055 (handler survives threads and shutdown) and FRE-1056 (bind
  the sink in the standalone gateway, drain before exit) are in flight and fix the measured cause. This
  ADR does not re-decide them and must not duplicate them.
- **ADR-0090's emit / mapping / dashboard corners**, beyond the single amendment in D6.

---

## Decision

### D1 — Absence is a first-class alert condition, and it is the class we lack

Every instrument in this system answers *is this value bad?*. None answers *did this stop happening?*.
FRE-1051 was an absence, and absence is invisible to threshold alerting by construction: **a threshold
rule over a metric that has stopped arriving does not fire — it has nothing left to evaluate.** Silence
and health produce identical evidence.

Absence is therefore configured **explicitly**, as a rule whose no-data outcome is a *firing* state
rather than a quiet one. Where the platform offers this natively — Grafana's `No Data` state, or a
Kibana rule with an explicit no-data action — it is used rather than reconstructed as a
threshold-of-zero, which is fragile and inverts the same way. **A rule authored without deciding its
no-data behaviour has decided it by default, and the default is silence.**

This generalises FRE-1051's own rule — `UNVERIFIABLE` is a verdict, never a silent pass — from one
probe's output to the alerting layer as a whole.

### D2 — Alerting is platform configuration, not application code

**We do not build a notifier, a log shipper, a queue, or delivery guarantees.** Every one of those was
considered and rejected (see Alternatives). The stack already provides the whole of it, and the project
is a small research harness, not an enterprise platform.

Consequences that bind:

- **No notification code lands in `src/personal_agent/`.** No SMTP client, no push integration, no
  webhook poster, no alert-routing logic. D5's heartbeat is the single, stated exception.
- **Alert rules and contact points are version-controlled configuration**, exported to the repository
  and re-importable — the same discipline ADR-0090 D3 applies to dashboards. A rule that exists only in
  a live UI is drift.

**Grafana is the destination, and it is planned rather than hypothetical** — FRE-1072 (ADR-0129 B7).
The full rule set lands there, with its native `No Data` state and unlicensed contact points.

**But the chain is at B2 and FRE-1072 is roughly five sequenced tickets out, so waiting for all of it
is not free.** The detection gap this ADR exists to close stays open for the whole interval, and the
failure it is meant to catch has already run for six days undetected once.

### D2a — Staged delivery, split by a rule that decides itself

The contract lands now; the rules land in two stages, and **the criterion for which stage a rule falls
into is whether it requires a new investigation surface** (D3):

- **Now, on Kibana** — rules whose investigation target is an *existing* surface (a saved Discover
  query, an existing dashboard). Rule 1 (family stopped flowing) and rule 2 (probe stopped reporting)
  both qualify, and they are precisely the two that catch FRE-1051's failure. Kibana alerting is
  available today: it is disabled only by a missing encryption key, not by licence.
- **On Grafana, with FRE-1072** — the full set, including every rule that needs a new investigation
  surface built to satisfy D3.

This split is chosen because it makes the throwaway small and bounded: **a Kibana rule is discarded at
migration, but a dashboard built to satisfy D3 is the expensive artifact, and none is built twice.**
The contract itself (D1, D3, D4, D5) names conditions, disciplines and targets — never a rule syntax —
so it survives the migration untouched. Only the two rule definitions are re-authored.

**One open question, now measurable rather than assumed.** If the `basic` connector set proves to
contain nothing that leaves the box — index and server-log connectors only — then the Kibana stage
reproduces the very "alert is a log line" failure this ADR exists to end, and the interim stage is
worthless. **Establishing which connectors this licence exposes is therefore the first implementation
ticket, and it gates the rest of the Kibana stage.** It is small: set the key, restart, enumerate.

Until that verdict is in, no conclusion drawn from log counts is entitled to assume completeness. The
partial mitigation meanwhile is that FRE-1055/1056 remove the known failure's *cause* while its
*detection* is being built — a mitigation, not coverage.

### D3 — An alert must take the owner somewhere to investigate

**Every alert rule carries a deep link to the surface where its condition is investigated, scoped to
the triggering time window and entity.** An alert that says only *something is wrong* is not an alert;
it is an interruption that transfers the whole investigation onto the reader at the worst moment.

Two things follow, and the second is the load-bearing one:

- A rule with no investigation target is **not an alert — it is a dashboard panel**, and belongs there
  instead. This is the test that keeps the set small.
- **The alert set is coupled to the dashboard set, deliberately: a rule cannot be authored before its
  investigation surface exists.** That ordering constraint is the point. It is what prevents shipping
  six rules that each name a problem and offer nowhere to look at it.

### D4 — The alert set

Six conditions. Each names what it catches and where it lands the owner to investigate. The set is
deliberately short: an alert learned-and-ignored is worse than no alert, because it also discredits its
neighbours. **Stage** is assigned by D2a's rule — *now* iff the investigation target already exists or
is a saved Discover query, *FRE-1072* iff a new surface must be built for it.

| # | Condition | Class | Catches | Investigation target | Stage |
|---|---|---|---|---|---|
| 1 | An `agent-logs` event family stops flowing while the system is active | absence | FRE-1051's exact failure — with no oracle, no ratio and no delivery floor | Saved Discover query on the family, scoped to the silent window | **now** |
| 2 | A scheduled probe stops writing its result document | absence | A dead probe — the meta-alert that keeps every other rule honest | Saved Discover query on that probe's result index | **now** |
| 3 | A probe result reports red | threshold | Joinability orphans, delivery breach, SLM health down — data that already exists and nothing reads | The failing probe's detail panel | FRE-1072 |
| 4 | Spend rate anomaly against the `api_costs` ledger | threshold | Runaway or misattributed cost, on the one substrate with append-only ground truth | Cost surface over Postgres, scoped to the window and model/role | FRE-1072 |
| 5 | Disk or cluster pressure | threshold | The `~10 GiB` box, with a recorded history of index-count and shard pathologies | Cluster/lifecycle surface | FRE-1072 |
| 6 | User-facing turn-failure rate | threshold | Breakage the owner would otherwise discover by hitting it | Turn/error surface, scoped to the window | FRE-1072 |

**The known-hard part, named rather than deferred:** rule 1 needs an "*while the system is active*"
qualifier. Without it, it fires every quiet night, and a rule that cries wolf nightly is a rule that
gets muted — which would leave the system in a *worse* state than having no rule, because the mute
looks like coverage. Defining that qualifier is the substance of rule 1's implementation, not an
afterthought to it.

### D5 — Silence must not read as green

An alerting layer that fires only on bad news makes *no alert* and *the alerting path is broken*
indistinguishable — reproducing FRE-1051's exact conflation one level up, where it would be
correspondingly harder to notice. Two mechanisms close it, and they are separate because they fail
differently:

- **Probe liveness, in-platform** — D4 rule 2. Each probe writes a result document on a known interval;
  the absence of that document is itself an alert (D1). This catches an instrument dying while the
  platform is healthy.
- **A dead-man's switch, out-of-platform** — an Elasticsearch-backed rule cannot report that
  Elasticsearch is down, and a Grafana rule cannot report that Grafana is down. This one check is
  therefore a periodic **outward** ping whose *absence* alarms, evaluated somewhere that shares **no
  failure domain** with this stack. It is the single exception to D2's "no application code," and it is
  small by construction: a heartbeat, not a monitor.

### D6 — ADR-0090 gains one done-bar clause and loses one stale open decision

Three edits to ADR-0090, and no fourth corner:

1. **D6 done-bar gains one clause.** A new or changed telemetry surface is not shippable until, in
   addition to its existing conditions, **one event of that family is shown to have actually landed**
   — verified once, at delivery, by whoever ships it. Not a ratio, not an oracle, not a floor, not a
   standing job. This is the "verify shape and context when the functionality is delivered" half.
2. **The stale open decision is struck.** ADR-0090's open-decisions list still carries the field
   registry as unsettled; **ADR-0133 explicitly declined it.** Citing it as open is drift.
3. **No delivery corner is added.** Its production half is D4 rule 1 of this ADR; its ship-time half is
   edit 1 above. The delivery-ratio probe FRE-1051 built remains as a **diagnostic invoked on
   suspicion**, not a standing gate, and its `0.99` floor is FRE-1051's operational default rather than
   a contract obligation — this ADR sets no delivery SLO.

---

## Alternatives Considered

### Option 1: At-least-once delivery semantics in the logging path

**Description:** Make the log path durable — in-process spool, acknowledgement propagated back from the
Elasticsearch bulk response, retry with backoff, replay de-duplication.

**Pros:** Would make delivery a guarantee rather than an observation; no loss to detect because none
occurs.

**Cons:** Every log call acquires a durability cost. Introduces a spool to size, drain, corrupt and
recover. Replay requires document-level idempotency the schema does not have.

**Why Rejected:** Owner-rejected as disproportionate — *"a heavy ask for a logging platform"* — and
correctly so for a small research harness. It is also aimed at the wrong failure: the measured loss was
never in transit, so no acknowledgement scheme would have prevented it. A process that binds no sink
has nothing to acknowledge.

### Option 2: Ship logs via Filebeat, reusing the deployed shipper

**Description:** Stop shipping from in-process and let a shipper tail the durable file instead.
`telemetry/logger.py:217` already attaches a `RotatingFileHandler` to `current.jsonl`
**unconditionally**, and ADR-0132 / FRE-1146 already deploy Filebeat with a persistent filestream
registry (`docker-compose.cloud.yml:454`, `filebeat_registry_cloud`) that survives restarts. That is
at-least-once machinery, already built and running, currently shipping only Caddy access logs.

**Pros:** At-least-once essentially free — the durable buffer is a file written anyway, so the shipper
only tracks an offset. Structurally dissolves the measured failure: a process that binds no ES handler
still writes its file, so "someone forgot to attach the sink" stops being a failure mode because the
sink becomes the filesystem.

**Cons:** Adds tail latency, a shipper config, a registry volume and disk-retention policy. Replaces a
transport that FRE-1055/1056 are actively hardening, wasting that in-flight work.

**Why Rejected:** Owner-rejected — *"we are not building a kafka pipeline."* Genuinely the most
technically attractive rejected option, and recorded in full so a future reader does not re-derive it:
it is a transport migration, and this ADR's problem is that findings have no *output*, not that the
transport is wrong. Revisit only if delivery loss recurs **after** FRE-1055/1056 land.

### Option 3: Build a notifier in the application (ntfy / email / webhook)

**Description:** Add an outbound notification module and wire the existing instruments to it via
`BrainstemScheduler`.

**Pros:** Works today, on the current stack, with no dependency on FRE-1072. Small first version.

**Cons:** We would own transport, retry, deduplication, grouping, silences, rate-limiting and an
escalation model — all of which Grafana provides free. A hand-rolled notifier with no deduplication
becomes noise on its first flapping condition.

**Why Rejected:** Owner-rejected — *"we are not going to develop readily available functionality… we
will use the functionality of these platforms."* Correct: this is the single clearest case of
re-implementing a solved problem, and the cost is not the first version but every subsequent one.

### Option 4: A delivery-ratio SLO as a standing gate

**Description:** Promote FRE-1051's probe to a standing obligation — per-family delivery measured
against an independent oracle, breaching below a 0.99 floor.

**Pros:** Directly measures the thing that failed; the instrument already exists and already treats
`UNVERIFIABLE` as a first-class verdict.

**Cons:** Only `api_costs` has a validated 1:1 oracle. FRE-1051 explicitly refused to wire the others
because `turn.model_call_completed` runs 2:1 against the ledger and *"would have reported 200 percent
over-delivery every day forever and taught everyone to ignore the monitor."* Nearly every family would
report `UNVERIFIABLE` indefinitely. And a delivery *floor* is an SLO, which invites exactly the
machinery Option 1 was rejected for.

**Why Rejected:** An obligation that is structurally unverifiable for almost every subject is not an
obligation. Absence detection (D1) catches the same failure without needing a twin store for any
family.

### Option 5: Wait for Grafana — defer all alerting to FRE-1072

**Description:** Make Grafana the alerting home and author nothing until FRE-1072 (ADR-0129 B7)
delivers it. This was **the first draft of this ADR's decision**, and it is recorded here as a rejected
alternative because the reasoning that produced it was wrong in an instructive way.

**Pros:** Grafana OSS alerting is unlicensed, has a native `No Data` state, and includes grouping,
deduplication and silences. Rules authored there are not thrown away when Kibana is retired.

**Cons:** Grafana **does not exist yet** and FRE-1072 is `Approved` but not started, so this defers all
detection by an unbounded interval — during which the failure class this ADR exists to catch remains
exactly as invisible as it was when it cost six days of telemetry and was found by accident.

**Why Rejected:** Rejected as the *whole* answer, not as the destination — Grafana remains where the
full set lands (D2). Owner-rejected — *"Grafana does not exist yet."* The draft reached this position
by misreading a broken instrument as a negative result: Kibana's `connector_types` returned HTTP 500,
the draft inferred a licence limitation, and it deferred the entire decision to unbuilt infrastructure.
The 500 was a **missing encryption key** (Context, above) — alerting we already own, switched off by
one unset config value. Deferring everything to a platform five sequenced tickets away, while the
available one sat disabled by a typo-sized omission, holds the detection gap open for no gain;
**diagnose the instrument before accepting its verdict.**

### Option 6: Author the full set on Kibana now and port everything to Grafana later

**Description:** Do not stage. Build all six rules and every investigation surface D3 requires on
Kibana immediately, then rebuild them on Grafana when FRE-1072 lands.

**Pros:** Fastest possible closure of the detection gap; no rule waits on the ADR-0129 chain.

**Cons:** Maximises rework in the expensive place. A Kibana *rule* is cheap to discard, but the
**dashboards built to satisfy D3 are the costly artifact**, and this option builds every one of them
twice on a platform FRE-1072 deletes.

**Why Rejected:** Owner-chose the staged split (D2a) instead. The rework is concentrated in surfaces
rather than rules, so staging by "does this rule need a new surface" captures nearly all of the
benefit at a fraction of the waste.

---

## Consequences

### Positive Consequences

- **The failure class that took six days and was found by accident becomes detectable** — and detectable
  without an oracle, a twin store or a delivery SLO.
- **Five existing instruments acquire an output.** The joinability probe, cache-erosion monitor,
  delivery-ratio probe, disk threshold and SLM health probe stop being write-only.
- **The circularity is broken** — findings about telemetry loss stop being filed exclusively into the
  telemetry system whose completeness is in question.
- **No new application subsystem.** D2 keeps the transport where it is already solved; the only code
  this ADR admits is D5's heartbeat.
- **Alerts stay actionable by construction** (D3) — the link requirement is what keeps the set from
  growing into noise, because a condition with nowhere to look fails the bar and becomes a panel.
- **ADR-0090 is left coherent** — one done-bar clause and one stale open decision struck, rather than a
  fourth corner whose grain does not fit its inventory.

### Negative Consequences

- **Two rules are authored twice.** Rules 1 and 2 land on Kibana and are re-authored on Grafana at
  FRE-1072. Accepted deliberately (D2a): the discarded artifact is a rule definition, not a dashboard,
  and the alternative is leaving FRE-1051's failure class undetected for five sequenced tickets.
- **Alerting spans two platforms during the interval**, which is a real if temporary split-brain: two
  places to look for a rule, and a migration step that must not silently drop one.
- **One blocking unknown remains** — whether the `basic` connector set can deliver anything out of the
  box. If it cannot, the Kibana stage is worthless and everything waits for Grafana after all. This is
  established first, before rules are authored, precisely so it cannot be discovered late.
- **Enabling Kibana alerting requires a secret and a restart.** The encryption key must come from the
  environment rather than committed config, and it is a live-service change — master's call, not a
  worker's.
- **Rule 1's "system is active" qualifier is genuinely hard** and under-specified here; getting it wrong
  produces nightly false alarms, and a muted rule is worse than an absent one.
- **The dead-man's switch needs somewhere outside this stack to live** — a dependency on infrastructure
  not otherwise required, however small.
- **Alert rules become another version-controlled surface** with an export discipline, inheriting
  exactly the git-vs-live drift problem ADR-0090 D3 already documents for dashboards.
- **The delivery-ratio probe is demoted** from would-be standing gate to on-suspicion diagnostic. If
  loss recurs in a family without an oracle, absence detection catches a full stop but *not* a partial
  one — a family losing 40 % still flows, and rule 1 stays quiet.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| Rule 1 false-alarms on idle periods and gets muted; the mute then reads as coverage | High | The activity qualifier is rule 1's core deliverable, not a detail; its ticket's criteria assert quiet-period behaviour explicitly |
| The `basic` connector set turns out to deliver nothing out-of-box, reproducing "the alert is a log line" | Medium | Established as the **first** implementation step, before rules are authored; if confirmed, D2's contingency routes notification to Grafana while the rules themselves still land |
| Rules 1 and 2 are authored on Kibana and thrown away at FRE-1072 | Low | Bounded by D2a's split: only rules needing *no new surface* land early, so no dashboard is built twice |
| The Kibana stage is forgotten at migration, leaving rules 1–2 behind on a retired platform | Medium | FRE-1072's own ticket carries the port as an explicit obligation; the seam ticket's AC-2 walk would catch an unresolvable link |
| Partial loss in an oracle-less family stays invisible (absence ≠ completeness) | Medium | Stated as a known limit in Consequences; the FRE-1051 probe remains available as an on-suspicion diagnostic |
| Alert rules drift between the live platform and git | Medium | Rules are version-controlled per D2, inheriting ADR-0090 D3's export discipline |
| The alert set grows until it is ignored | Medium | D3's investigation-link test is the gate; a condition with no target becomes a panel instead |
| The dead-man's switch is itself unmonitored and dies quietly | Low | Its absence *is* its alarm — it fails safe by design |

---

## Implementation Notes

- **First step, and it gates everything else:** set `xpack.encryptedSavedObjects.encryptionKey` for
  Kibana, restart, then enumerate `/api/actions/connector_types` and record which connectors this
  `basic` licence actually exposes. The key is a **secret** — it comes from the environment, never from
  committed config (the config-guard pre-commit hook enforces this) — and the restart is a live-service
  change, so it is master's to authorize.
- **Staged against FRE-1072** (D2a). Rules 1 and 2 land on Kibana now because their investigation
  targets already exist; the rest land on Grafana with FRE-1072, along with the surfaces D3 requires.
  D1/D3/D4/D5 carry over unchanged because none of them names a rule syntax.
- **FRE-1072's ticket must carry the port of rules 1–2** as an explicit obligation, or the Kibana stage
  is orphaned on a retired platform.
- **Must not collide with FRE-1055 / FRE-1056**, in flight in `build1`, which bind the Elasticsearch
  sink correctly and harden the handler. This ADR consumes their outcome and re-decides none of it.
- **Files touched by the D6 amendment:** `docs/architecture_decisions/ADR-0090-telemetry-surface-contract.md`
  (D6 done-bar clause; strike the field-registry open decision).
- **Existing instruments to wire, not rebuild:** `scripts/monitors/joinability_probe.py`,
  `scripts/monitors/cache_erosion_monitor.py`, `scripts/monitors/delivery_ratio_monitor.py`,
  `src/personal_agent/telemetry/lifecycle_manager.py` (disk threshold), the SLM health probe.
- **Note for whoever schedules probes:** `delivery_ratio_monitor.py` has no cron entry, no CI job and
  no Makefile target, unlike `joinability_probe` and `cache_erosion_monitor` which both have Makefile
  targets. It is invoked by documentation only.
- **Kibana defect worth its own ticket:** `/api/actions/connector_types` returns HTTP 500 on the live
  8.19.0 instance. Filed separately; it blocks confirming Option 5's premise either way.

---

## Verification / Acceptance Criteria

**How will we know this decision actually delivered — not just merged?**

- **AC-1** — A telemetry family that stops flowing produces an alert **that reaches the owner's
  device**. · **Check:** in a live window with the system otherwise active, stop the producer of one
  measured family (or unbind its sink); observe the notification arrive out-of-band. · *Fails if* the
  rule fires only into an index document or a server-log entry (the "alert is a log line" failure this
  ADR exists to end), **or** if the rule never fires because the metric simply stopped being evaluated
  (a threshold masquerading as absence detection).

- **AC-2** — Every alert rule resolves to a scoped investigation surface. · **Check:** for each rule in
  the committed rule set, follow its link. · *Fails if* any rule carries no link, the link does not
  resolve, or it lands on an unscoped home dashboard rather than the triggering window and entity.

- **AC-3** — A dead probe is detected as absence, not as silence. · **Check:** stop one scheduled probe;
  wait its stated interval plus margin. · *Fails if* stopping the probe produces no alert — i.e. a
  probe that has stopped reporting is indistinguishable from a probe reporting healthy.

- **AC-4** — The stack going down is reported from outside it. · **Check:** in a controlled window, stop
  Elasticsearch (or the stack); observe the dead-man's switch alarm. · *Fails if* the only would-be
  reporter shares the failure domain with what it is reporting on.

- **AC-5** — ADR-0090's amended done-bar rejects an emit-but-never-lands surface. · **Check:** introduce
  a new telemetry family from a process that binds no sink and run the ship-time check. · *Fails if* the
  check passes on the strength of the emit call existing in source — which is precisely how FRE-1051's
  404 events passed every corner ADR-0090 had.

- **AC-6** — Rule 1 stays quiet through a genuine idle period. · **Check:** over a stated multi-night
  window with no traffic, count rule-1 firings. · *Fails if* it fires on ordinary quiet hours — the
  false-alarm mode that ends in the rule being muted, which is worse than never having shipped it.

- **AC-7** — The set has not become noise. · **Check:** over the 30 days after the rule set goes live,
  every firing has an owner disposition — acted on, or the rule amended. · *Fails if* any rule fired
  repeatedly and was neither acted on nor changed; an ignored rule is a defect in the set, not a
  neutral outcome. *(Assembled, long-horizon and owner-involving — permitted for an ADR's own criteria
  under ADR-0130 D1.)*

**Seam ticket:** **FRE-1185** — *ADR-0134 SEAM — adjudicate the activity-alerting criteria*.
**Due date: 2026-09-30.** All seven criteria become adjudicable once the rule set is live and AC-6/AC-7
accumulate their stated observation windows — a multi-night idle period and 30 days of live operation
respectively — which is what sets the date rather than any platform dependency. Filed parked
(`Backlog`); master activates it at the first advance-dispatch on or after the due date, and an `adr`
session adjudicates it. This ADR does not close because its last child merged.

---

## References

- ADR-0090 — Telemetry Surface Contract (emit ↔ mapping ↔ dashboard); amended by D6 above. Status: Accepted.
- ADR-0129 — OpenTelemetry Instrumentation and Trace Visibility; its B7 phase (FRE-1072) brings Grafana and retires Kibana. Status: Accepted.
- ADR-0133 — The Typed Emit Envelope for the Residual Log Corpus; declined the field registry that ADR-0090 still lists as an open decision. Status: Proposed.
- ADR-0132 — Egress chain (Caddy → Filebeat → DomainGuard); the deployed Filebeat and its persistent filestream registry referenced in Alternatives Option 2. Status: Accepted.
- ADR-0088 — Execution-topology emission seam; the emission complement to ADR-0090's surface contract. Status: Accepted.
- ADR-0130 — Acceptance-criteria hierarchy; D1/D2 govern the seam ticket above. Status: Accepted.
- ADR-0074 — End-to-end traceability; the joinability probe referenced in D4 rule 3. Status: Accepted.
- Linear FRE-1058 — this ADR's ticket (ADR-0090's fourth corner), Observability Foundation.
- Linear FRE-1051 — the delivery measurement, the corrected diagnosis, and the probe. Done 2026-07-31.
- Linear FRE-1055 / FRE-1056 — Elasticsearch handler hardening and sink binding; in flight, fix the measured cause.
- Linear FRE-1072 — ADR-0129 B7, Tempo + Grafana + Kibana retirement; the destination for the full rule set under D2a, and the ticket that must carry the port of rules 1–2.
- Linear FRE-533 — the 1023-row three-way reconciliation inventory whose grain is discussed in Context.
- Linear FRE-1039 — Grafana over Postgres for aggregate cost and ledger truth; D4 rule 4's surface.
- Code: `src/personal_agent/telemetry/lifecycle_manager.py:183,206` — the alert that is a log line.
- Code: `src/personal_agent/config/settings.py:1537` — `disk_usage_alert_percent`, the only alert threshold.
- Code: `src/personal_agent/service/app.py:754` — the single conditional `add_elasticsearch_handler` call site.
- Code: `src/personal_agent/telemetry/logger.py:217` — the unconditional `RotatingFileHandler`, referenced in Option 2.
- Code: `src/personal_agent/observability/delivery_ratio/` + `scripts/monitors/delivery_ratio_monitor.py` — FRE-1051's probe, demoted to on-suspicion diagnostic by D6.
- Config: `docker-compose.cloud.yml:160` — the Kibana service, with no `xpack.encryptedSavedObjects.encryptionKey` set; `docker/kibana/kibana.yml` is the mounted config it would go in.
- External: [Kibana — configure alerting (`xpack.encryptedSavedObjects.encryptionKey` is required for alerting and actions)](https://www.elastic.co/guide/en/kibana/current/alerting-setup.html)
- External: [Grafana unified alerting — No Data and Error handling](https://grafana.com/docs/grafana/latest/alerting/fundamentals/alert-rules/state-and-health/)
- External: [Elastic subscriptions — connector availability by tier](https://www.elastic.co/subscriptions) (consulted 2026-08-07; does not state the tier boundary for action connectors, which is why D2 makes it a measurement)

---

## Status Updates

### 2026-08-07 - Proposed
**Changed By:** `adr` session (FRE-1058)
**Reason:** Authored after owner-led design discussion. The ticket's premise — add a fourth *delivery*
corner to ADR-0090 — was rejected in discussion on two grounds: FRE-1051's corrected diagnosis showed
the loss was never in transit, and absence does not share the grain, artifact or check-location of
ADR-0090's three corners. Four successively lighter designs (at-least-once semantics, a Filebeat
transport migration, an in-house notifier, a delivery SLO) were each raised and rejected by the owner
as disproportionate for a small research harness, converging on platform-native alerting with absence
as the missing signal class.

**Correction made before this ADR was opened, recorded because the error is the instructive part.** The
first draft made Grafana the alerting home and stated that no actionable alerting was possible until
FRE-1072 landed. That rested on reading Kibana's HTTP 500 from `/api/actions/connector_types` as a
`basic`-licence limitation. The owner's challenge — *"Grafana does not exist yet"* — prompted an actual
diagnosis, which found the API disabled by a **missing `xpack.encryptedSavedObjects.encryptionKey`**,
not by licensing. **A broken instrument had been accepted as a negative result** — the same failure
mode FRE-1051 exists to end, committed inside the ADR written to address it.

The second draft then over-corrected, treating Grafana as incidental and Kibana as the platform. The
owner's *"Grafana is planned, there are tickets for it"* settled it: Grafana is the destination, the
distance to it is real, and the answer is **staged delivery (D2a)** — owner-chosen from three options,
splitting on whether a rule needs a new investigation surface, so that the artifact thrown away at
migration is a rule definition and never a dashboard.
